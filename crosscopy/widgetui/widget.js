/* cross-copy widget panel — vanilla JS, no dependencies. */
(function () {
  "use strict";

  var POLL_MS = 30000;           // slow fallback poll; SSE drives live updates
  var DEBOUNCE_MS = 250;         // coalesce SSE bursts into one refetch
  var SEND_POLL_MS = 1500;       // outgoing-offer status polling
  var SEND_POLL_MAX = 200;       // ~300 s, matches offer expiry

  var els = {
    name: document.getElementById("device-name"),
    dot: document.getElementById("live-dot"),
    offers: document.getElementById("offers"),
    resumesCard: document.getElementById("resumes-card"),
    resumes: document.getElementById("resumes"),
    peers: document.getElementById("peers"),
    toasts: document.getElementById("toasts")
  };

  var state = { es: null, opened: false, timer: null, watch: {},
                sendStatus: {}, sendDraft: {} };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function autoGrow(ta, maxLines) {
    // Grow a rows=1 textarea with its content, up to maxLines, then scroll.
    var lines = maxLines || 6;
    ta.style.height = "auto";
    var cs = window.getComputedStyle(ta);
    var line = parseFloat(cs.lineHeight) || 19;
    var borders = (parseFloat(cs.borderTopWidth) || 0) +
                  (parseFloat(cs.borderBottomWidth) || 0);
    var max = Math.ceil(lines * line +
      (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0) +
      borders);
    var want = ta.scrollHeight + borders;
    ta.style.height = Math.min(want, max) + "px";
    ta.style.overflowY = want > max ? "auto" : "hidden";
  }

  /* ---------- compact panel window ----------
     `--app=` windows inherit the browser's last window size, which is often
     huge/maximized (`--window-size` is ignored when an existing browser
     process adopts the profile). Best-effort self-correction: resize to a
     compact panel after load and when content changes. Browsers refuse
     resizeTo for normal tabs — that's fine, fail silently. Stops as soon as
     the user resizes the window themselves. */
  var fit = { timer: null, user: false, w: 0, h: 0 };

  window.addEventListener("resize", function () {
    if (fit.w &&
        (Math.abs(window.outerWidth - fit.w) > 4 ||
         Math.abs(window.outerHeight - fit.h) > 4)) {
      fit.user = true; // not a size we set — user took over
    }
  });

  function fitPanelWindow() {
    if (fit.user || fit.timer) return;
    fit.timer = setTimeout(function () {
      fit.timer = null;
      if (fit.user) return;
      try {
        var chromeH = Math.max(0, (window.outerHeight || 0) - (window.innerHeight || 0));
        var content = document.documentElement.scrollHeight + chromeH;
        var h = Math.min(Math.max(content, 420), 760);
        fit.w = 420;
        fit.h = h;
        window.resizeTo(420, h);
      } catch (e) { /* not allowed for this window — fine */ }
    }, 350);
  }

  function toast(msg, kind) {
    var t = el("div", "toast " + (kind || ""), msg);
    els.toasts.appendChild(t);
    void t.offsetWidth;
    t.classList.add("show");
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 300);
    }, 4000);
  }

  function humanSize(bytes) {
    if (!bytes && bytes !== 0) return "?";
    var units = ["B", "KB", "MB", "GB"], i = 0, n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  }

  function platformIcon(p) {
    return p === "darwin" ? "🍎" : p === "linux" ? "🐧" :
      p === "win32" ? "🪟" : "💻";
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var e = new Error(body && body.error ? body.error : "HTTP " + res.status);
          e.status = res.status;
          throw e;
        }
        return body;
      });
    });
  }

  /* ---------- offers ---------- */

  function offerSummary(o) {
    if (o.kind === "text") {
      var t = o.text || "";
      var prev = t.replace(/\s+/g, " ").slice(0, 60);
      return "text (" + t.length + " chars) “" + prev + (t.length > 60 ? "…" : "") + "”";
    }
    var n = (o.files || []).length;
    return n + " file" + (n === 1 ? "" : "s") + " · " + humanSize(o.total_size);
  }

  function renderOffers(offers) {
    els.offers.textContent = "";
    offers.forEach(function (o) {
      var card = el("div", "glass offer");
      card.appendChild(el("div", "offer-from",
        "📥 " + ((o.from && o.from.name) || "?") + " wants to send"));
      card.appendChild(el("div", "offer-what", offerSummary(o)));
      var actions = el("div", "offer-actions");
      var accept = el("button", "btn btn-primary", "Accept");
      var decline = el("button", "btn btn-danger", "Decline");
      accept.addEventListener("click", function () {
        accept.disabled = decline.disabled = true;
        accept.textContent = "Receiving…";
        api("/api/offers/" + o.offer_id + "/accept", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}"
        }).then(function (res) {
          toast(res.kind === "text"
            ? "Got text (" + (res.text || "").length + " chars)"
            : "Saved " + (res.files_written || []).length + " file(s)", "success");
        }).catch(function (err) {
          toast("Accept failed: " + err.message, "error");
        }).then(refresh);
      });
      decline.addEventListener("click", function () {
        accept.disabled = decline.disabled = true;
        api("/api/offers/" + o.offer_id + "/decline", { method: "POST",
          headers: { "Content-Type": "application/json" }, body: "{}"
        }).catch(function () {}).then(refresh);
      });
      actions.appendChild(accept);
      actions.appendChild(decline);
      card.appendChild(actions);
      els.offers.appendChild(card);
    });
  }

  function renderResumes(resumes) {
    els.resumes.textContent = "";
    els.resumesCard.classList.toggle("hidden", !resumes.length);
    resumes.forEach(function (session) {
      var row = el("div", "resume");
      var source = (session.source && session.source.name) || "unknown";
      var received = session.received_bytes || 0;
      var total = session.total_bytes || 0;
      var percent = total ? Math.min(100, Math.round(received * 100 / total)) : 0;
      var head = el("div", "resume-head");
      head.appendChild(el("strong", null, source));
      head.appendChild(el("span", "resume-percent", percent + "%"));
      row.appendChild(head);
      var track = el("div", "resume-track");
      var bar = el("div", "resume-bar");
      bar.style.width = percent + "%";
      track.appendChild(bar);
      row.appendChild(track);
      row.appendChild(el("div", "resume-meta",
        humanSize(received) + " / " + humanSize(total)));
      if (!session.available) {
        row.appendChild(el("div", "resume-unavailable",
          session.unavailable_reason || "No longer shared"));
      }
      var actions = el("div", "resume-actions");
      var resume = el("button", "btn btn-primary", "Resume");
      resume.disabled = !session.available;
      resume.addEventListener("click", function () {
        resume.disabled = true;
        api("/api/resumes/" + session.id + "/resume", { method: "POST" })
          .then(function () { toast("Transfer completed and verified", "success"); })
          .catch(function (err) { toast("Resume failed: " + err.message, "error"); })
          .then(refresh);
      });
      var remove = el("button", "btn btn-danger", "Remove");
      remove.title = "Remove saved partial files";
      remove.addEventListener("click", function () {
        if (!window.confirm("Remove the saved partial files?")) return;
        remove.disabled = true;
        api("/api/resumes/" + session.id + "/remove", { method: "POST" })
          .then(function () { toast("Partial files removed", "success"); })
          .catch(function (err) { toast("Remove failed: " + err.message, "error"); })
          .then(refresh);
      });
      actions.appendChild(resume);
      actions.appendChild(remove);
      row.appendChild(actions);
      els.resumes.appendChild(row);
    });
  }

  /* ---------- outgoing send + status watch ---------- */

  /* Rows are re-rendered on every refresh, so the inline status lives in
     state (keyed by peer id) and is re-applied by renderPeers. */
  function setPeerStatus(peerId, text, cls) {
    state.sendStatus[peerId] = { text: text, cls: cls || "" };
    var rows = els.peers.querySelectorAll(".peer");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].dataset.peer === peerId) {
        var s = rows[i].querySelector(".peer-status");
        s.textContent = text;
        s.className = "peer-status " + (cls || "");
      }
    }
  }

  function watchSend(offerId, peerId) {
    if (state.watch[offerId]) return;
    var ticks = 0;
    var labels = { pending: "waiting…", accepted: "accepted…",
                   completed: "delivered", declined: "declined",
                   failed: "failed", expired: "expired" };
    state.watch[offerId] = setInterval(function () {
      api("/api/send/" + offerId).then(function (o) {
        var s = o.status || "pending";
        setPeerStatus(peerId, labels[s] || s,
          s === "completed" ? "ok" :
          (s === "declined" || s === "failed" || s === "expired") ? "bad" : "");
        if (s === "pending") {
          ticks++;
          if (ticks >= SEND_POLL_MAX) stop();
        } else {
          ticks = 0;
        }
        if (s !== "pending" && s !== "accepted") stop();
      }).catch(stop);
    }, SEND_POLL_MS);
    function stop() {
      clearInterval(state.watch[offerId]);
      delete state.watch[offerId];
    }
  }

  function doSend(peer, body, done) {
    setPeerStatus(peer.id, "sending…");
    api("/api/send", body).then(function (offer) {
      setPeerStatus(peer.id, "waiting…");
      if (offer.offer_id) watchSend(offer.offer_id, peer.id);
    }).catch(function (err) {
      setPeerStatus(peer.id, "failed", "bad");
      toast("Send to " + peer.name + " failed: " + err.message, "error");
    }).then(done);
  }

  /* ---------- peers ---------- */

  function renderPeers(peers) {
    // Preserve focus + caret of a send box across re-renders (SSE bursts
    // used to wipe half-typed text; drafts live in state.sendDraft).
    var focusPeer = null, selStart = 0, selEnd = 0;
    var active = document.activeElement;
    if (active && active.classList && active.classList.contains("send-input") &&
        els.peers.contains(active)) {
      focusPeer = active.dataset.peer;
      selStart = active.selectionStart;
      selEnd = active.selectionEnd;
    }

    els.peers.textContent = "";
    if (!peers.length) {
      els.peers.appendChild(el("p", "empty", "No devices found on the network."));
      return;
    }
    peers.forEach(function (peer) {
      var row = el("div", "peer");
      row.dataset.peer = peer.id;
      var head = el("div", "peer-head");
      head.appendChild(el("span", null, platformIcon(peer.platform)));
      head.appendChild(el("span", "peer-name", peer.name || peer.id));
      var saved = state.sendStatus[peer.id];
      var status = el("span",
        "peer-status" + (saved && saved.cls ? " " + saved.cls : ""),
        saved ? saved.text : "");
      head.appendChild(status);
      row.appendChild(head);

      var send = el("div", "peer-send");
      // A textarea (not <input>) so multi-line text stays visible and
      // scrollable: grows with content up to ~6 lines, then scrolls.
      var input = el("textarea", "send-input");
      input.rows = 1;
      input.placeholder = "Send text…";
      input.title = "Enter sends · Shift+Enter adds a line";
      input.spellcheck = false;
      input.dataset.peer = peer.id;
      input.value = state.sendDraft[peer.id] || "";
      input.addEventListener("input", function () {
        state.sendDraft[peer.id] = input.value;
        autoGrow(input);
      });
      var sendBtn = el("button", "btn btn-primary", "Send");
      sendBtn.addEventListener("click", function () {
        var text = input.value;
        if (!text.trim()) return;
        sendBtn.disabled = true;
        doSend(peer, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ peer_id: peer.id, text: text })
        }, function () {
          sendBtn.disabled = false;
          input.value = "";
          delete state.sendDraft[peer.id];
          autoGrow(input);
        });
      });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          sendBtn.click();
        }
      });

      var fileBtn = el("label", "btn file-btn", "📁");
      fileBtn.title = "Send files…";
      var fileInput = el("input");
      fileInput.type = "file";
      fileInput.multiple = true;
      fileInput.addEventListener("change", function () {
        if (!fileInput.files.length) return;
        var form = new FormData();
        form.append("peer_id", peer.id);
        Array.prototype.forEach.call(fileInput.files, function (f) {
          form.append("files", f, f.name);
        });
        fileInput.value = "";
        doSend(peer, { method: "POST", body: form }, function () {});
      });
      fileBtn.appendChild(fileInput);

      send.appendChild(input);
      send.appendChild(sendBtn);
      send.appendChild(fileBtn);
      row.appendChild(send);
      els.peers.appendChild(row);
    });

    // Size restored drafts now that the textareas are in the DOM.
    var tas = els.peers.querySelectorAll("textarea.send-input");
    for (var i = 0; i < tas.length; i++) autoGrow(tas[i]);

    if (focusPeer) {
      for (var j = 0; j < tas.length; j++) {
        if (tas[j].dataset.peer === focusPeer) {
          tas[j].focus();
          try { tas[j].setSelectionRange(selStart, selEnd); } catch (e) { /* ok */ }
          break;
        }
      }
    }
  }

  /* ---------- refresh: SSE + slow fallback poll ---------- */

  function refresh() {
    return Promise.all([
      api("/api/status"),
      api("/api/peers?with_clipboard=1"),
      api("/api/offers").catch(function () { return { offers: [] }; }),
      api("/api/resumes").catch(function () { return { resumes: [] }; })
    ]).then(function (r) {
      els.name.textContent = r[0].name || "cross-copy";
      renderPeers(r[1].peers || []);
      renderOffers(r[2].offers || []);
      renderResumes(r[3].resumes || []);
      fitPanelWindow(); // content changed — keep the app window compact
      if (!state.es) connectEvents();
    }).catch(function () {
      setLive(false);
    });
  }

  function scheduleRefresh() {
    if (state.timer) return;
    state.timer = setTimeout(function () {
      state.timer = null;
      refresh();
    }, DEBOUNCE_MS);
  }

  function setLive(ok) {
    els.dot.classList.toggle("live", ok);
    els.dot.title = ok ? "live" : "disconnected";
  }

  function connectEvents() {
    if (!window.EventSource || state.es) return;
    var es = new EventSource("/api/events");
    state.es = es;
    state.opened = false;
    es.onopen = function () {
      state.opened = true;
      setLive(true);
      scheduleRefresh();
    };
    es.onmessage = scheduleRefresh;
    es.onerror = function () {
      setLive(false);
      if (!state.opened || es.readyState === EventSource.CLOSED) {
        es.close();
        if (state.es === es) state.es = null; // poll re-establishes
      }
    };
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
  });

  refresh();
  connectEvents();
  setInterval(refresh, POLL_MS);
  fitPanelWindow();
})();
