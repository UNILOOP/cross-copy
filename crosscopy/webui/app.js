/* cross-copy web UI — vanilla JS, no dependencies. */
(function () {
  "use strict";

  var POLL_MS = 30000;            // slow fallback poll; SSE drives instant updates
  var REFRESH_DEBOUNCE_MS = 250;  // coalesce SSE event bursts into one refetch
  var SEND_POLL_MS = 1500;        // outgoing-offer status polling
  var SEND_POLL_MAX = 200;        // ~300 s, matches offer expiry
  var DEST_KEY = "crosscopy.dest";
  var UPDATE_DISMISS_KEY = "crosscopy.updateDismissed";

  var $ = function (id) { return document.getElementById(id); };

  var els = {
    banner: $("reconnect-banner"),
    updateBanner: $("update-banner"),
    updateText: $("update-text"),
    updateDismiss: $("update-dismiss"),
    deviceName: $("device-name"),
    devicePlatform: $("device-platform"),
    daemonVersion: $("daemon-version"),
    localTitle: $("local-card-title"),
    localClipboard: $("local-clipboard"),
    opBadge: $("local-op-badge"),
    clearBtn: $("clear-btn"),
    dropZone: $("drop-zone"),
    pickBtn: $("pick-btn"),
    fileInput: $("file-input"),
    uploadProgress: $("upload-progress"),
    progressBar: $("progress-bar"),
    progressLabel: $("progress-label"),
    tabFiles: $("tab-files"),
    tabText: $("tab-text"),
    textPane: $("text-pane"),
    textInput: $("text-input"),
    shareTextBtn: $("copy-text-btn"),
    destInput: $("dest-input"),
    offersStack: $("offers-stack"),
    peersList: $("peers-list"),
    addPeerForm: $("add-peer-form"),
    peerHost: $("peer-host"),
    peerPort: $("peer-port"),
    addPeerBtn: $("add-peer-btn"),
    toasts: $("toasts")
  };

  var state = {
    busy: false,          // upload/receive in flight -> pause refetching
    connected: true,
    pollTimer: null,
    refreshTimer: null,   // pending debounced refresh
    es: null,             // EventSource, when connected
    sseOpened: false,     // stream has successfully opened at least once
    sendStatus: {},       // peer id -> {text, cls}; survives re-renders
    sendDraft: {},        // peer id -> unsent text draft; survives re-renders
    watch: {}             // offer id -> interval handle (outgoing status poll)
  };

  /* ---------- helpers ---------- */

  function humanSize(bytes) {
    if (bytes === 0) return "0 B";
    if (!bytes && bytes !== 0) return "?";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(1)) + " " + units[i];
  }

  function relTime(epochSeconds) {
    if (!epochSeconds) return "";
    var diff = Math.max(0, Date.now() / 1000 - epochSeconds);
    if (diff < 10) return "just now";
    if (diff < 60) return Math.floor(diff) + " sec ago";
    if (diff < 3600) return Math.floor(diff / 60) + " min ago";
    if (diff < 86400) return Math.floor(diff / 3600) + " hr ago";
    return Math.floor(diff / 86400) + " day" + (diff >= 172800 ? "s" : "") + " ago";
  }

  function platformIcon(platform) {
    if (platform === "darwin") return "🍎"; // apple
    if (platform === "linux") return "🐧";  // penguin
    return "💻";                            // laptop
  }

  function opBadgeText(op) {
    // Internal API ops stay "copy"/"move"; the UI speaks share language.
    return op === "move" ? "move (removes from sender)" : "share";
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function toast(message, kind) {
    var t = el("div", "toast " + (kind || "info"), message);
    els.toasts.appendChild(t);
    // force reflow so the enter transition runs
    void t.offsetWidth;
    t.classList.add("show");
    setTimeout(function () {
      t.classList.remove("show");
      setTimeout(function () { t.remove(); }, 300);
    }, 4500);
  }

  function api(path, options) {
    return fetch(path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (body) {
        if (!res.ok) {
          var msg = body && body.error ? body.error : ("HTTP " + res.status);
          var err = new Error(msg);
          err.status = res.status;
          throw err;
        }
        return body;
      });
    });
  }

  function setConnected(ok) {
    if (ok === state.connected) return;
    state.connected = ok;
    els.banner.classList.toggle("hidden", ok);
    if (ok) toast("Reconnected to daemon", "success");
  }

  /* ---------- rendering ---------- */

  function isTextManifest(m) {
    // Manifests without "kind" are file manifests (pre-v0.2 back-compat).
    return !!m && m.kind === "text";
  }

  function renderTextClip(manifest, preview) {
    var wrap = el("div", "text-clip");
    wrap.appendChild(el("div", "text-block" + (preview ? " preview" : ""), manifest.text || ""));
    var total = el("div", "file-total");
    total.appendChild(el("span", null, "text · " + (manifest.text || "").length + " chars"));
    total.appendChild(el("span", "file-time", relTime(manifest.created_at)));
    wrap.appendChild(total);
    return wrap;
  }

  function renderFileList(manifest) {
    var wrap = el("div", "file-list");
    manifest.files.forEach(function (f) {
      var row = el("div", "file-row");
      row.appendChild(el("span", "file-name", f.rel_path));
      row.appendChild(el("span", "file-size", humanSize(f.size)));
      wrap.appendChild(row);
    });
    var total = el("div", "file-total");
    total.appendChild(el("span", null,
      manifest.files.length + " file" + (manifest.files.length === 1 ? "" : "s") +
      " · " + humanSize(manifest.total_size)));
    total.appendChild(el("span", "file-time", relTime(manifest.created_at)));
    wrap.appendChild(total);
    return wrap;
  }

  function renderUpdate(update) {
    // `update` may be absent on older daemons — never crash, just hide.
    if (!update || update.available !== true || !update.latest) {
      els.updateBanner.classList.add("hidden");
      return;
    }
    var latest = String(update.latest);
    if (localStorage.getItem(UPDATE_DISMISS_KEY) === latest) {
      els.updateBanner.classList.add("hidden");
      return;
    }
    els.updateBanner.dataset.version = latest;
    els.updateText.textContent = "";
    els.updateText.appendChild(document.createTextNode(
      "Update v" + latest + " available — "));
    if (update.auto_update) {
      els.updateText.appendChild(document.createTextNode("installing automatically soon"));
    } else {
      els.updateText.appendChild(document.createTextNode("run "));
      els.updateText.appendChild(el("code", null, "ccp update"));
      els.updateText.appendChild(document.createTextNode(" in a terminal"));
    }
    els.updateBanner.classList.remove("hidden");
  }

  function renderLocal(status) {
    els.deviceName.textContent = status.name || "?";
    els.devicePlatform.textContent = platformIcon(status.platform) + " " + (status.platform || "");
    els.daemonVersion.textContent = status.version ? "v" + status.version : "";
    renderUpdate(status.update);

    var m = status.clipboard;
    var hasContent = isTextManifest(m) || (m && m.files && m.files.length);
    els.localTitle.textContent = hasContent ? "Currently sharing" : "Share from this device";
    els.localClipboard.textContent = "";
    if (hasContent) {
      els.localClipboard.appendChild(
        isTextManifest(m) ? renderTextClip(m, false) : renderFileList(m));
      els.opBadge.textContent = opBadgeText(m.op);
      els.opBadge.className = "op-badge op-" + m.op;
      els.clearBtn.classList.remove("hidden");
    } else {
      els.localClipboard.appendChild(el("p", "empty-state",
        "Nothing shared yet — drop files or type text below."));
      els.opBadge.className = "op-badge hidden";
      els.clearBtn.classList.add("hidden");
    }
  }

  /* ---------- incoming offers (v0.4) ---------- */

  function offerWhat(o) {
    if (o.kind === "text") {
      return "some text (" + ((o.text || "").length) + " chars)";
    }
    var n = (o.files || []).length;
    return n + " file" + (n === 1 ? "" : "s") + " (" + humanSize(o.total_size) + ")";
  }

  function renderOffers(offers) {
    els.offersStack.textContent = "";
    offers.forEach(function (o) {
      var card = el("div", "glass card offer-card");

      var head = el("div", "offer-head");
      var title = el("div", "offer-title");
      title.appendChild(el("span", "offer-icon", "📥"));
      title.appendChild(el("span", "offer-title-text",
        ((o.from && o.from.name) || "Unknown device") +
        " wants to send you " + offerWhat(o)));
      head.appendChild(title);
      head.appendChild(el("span", "offer-time", relTime(o.created_at)));
      card.appendChild(head);

      if (o.kind === "text") {
        card.appendChild(el("div", "text-block preview", o.text || ""));
      } else {
        var files = o.files || [];
        var listWrap = el("div", "offer-files");
        files.slice(0, 4).forEach(function (f) {
          var row = el("div", "file-row");
          row.appendChild(el("span", "file-name", f.rel_path));
          row.appendChild(el("span", "file-size", humanSize(f.size)));
          listWrap.appendChild(row);
        });
        if (files.length > 4) {
          listWrap.appendChild(el("div", "offer-more",
            "+ " + (files.length - 4) + " more"));
        }
        card.appendChild(listWrap);
      }

      var actions = el("div", "offer-actions");
      var accept = el("button", "btn btn-primary", "Accept");
      var decline = el("button", "btn btn-danger", "Decline");
      accept.type = "button";
      decline.type = "button";
      accept.addEventListener("click", function () {
        doAcceptOffer(o, accept, decline);
      });
      decline.addEventListener("click", function () {
        doDeclineOffer(o, accept, decline);
      });
      actions.appendChild(accept);
      actions.appendChild(decline);
      card.appendChild(actions);

      els.offersStack.appendChild(card);
    });
  }

  function doAcceptOffer(offer, acceptBtn, declineBtn) {
    acceptBtn.disabled = true;
    declineBtn.disabled = true;
    acceptBtn.textContent = offer.kind === "text" ? "Getting text…" : "Receiving…";
    state.busy = true; // keep SSE re-renders from wiping the buttons mid-flight
    api("/api/offers/" + offer.offer_id + "/accept", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (res) {
      var fromName = (res.from && res.from.name) ||
        (offer.from && offer.from.name) || "peer";
      if (res.kind === "text") {
        var text = res.text || "";
        els.textInput.value = text;
        updateShareTextBtn();
        setMode("text");
        return copyToBrowserClipboard(text).then(function (ok) {
          toast("Got text (" + text.length + " chars) from " + fromName +
            (ok ? " — copied to your clipboard" : ""), "success");
        });
      }
      var n = (res.files_written || []).length;
      toast("Saved " + n + " file" + (n === 1 ? "" : "s") +
        " (" + humanSize(res.total_bytes) + ") from " + fromName +
        (res.dest ? " into " + res.dest : ""), "success");
    }).catch(function (err) {
      toast("Could not accept: " + err.message, "error");
    }).then(function () {
      state.busy = false;
      refresh();
    });
  }

  function doDeclineOffer(offer, acceptBtn, declineBtn) {
    acceptBtn.disabled = true;
    declineBtn.disabled = true;
    api("/api/offers/" + offer.offer_id + "/decline", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function () {
      toast("Declined offer from " +
        ((offer.from && offer.from.name) || "peer"));
    }).catch(function () {
      /* offer likely already gone; refresh sorts it out */
    }).then(refresh);
  }

  /* ---------- outgoing targeted send (v0.4) ---------- */

  /* Peer cards are re-rendered on every refresh, so the inline send status
     lives in state keyed by peer id and is re-applied by renderPeers. */
  function setSendStatus(peerId, text, cls) {
    state.sendStatus[peerId] = { text: text, cls: cls || "" };
    var nodes = els.peersList.querySelectorAll(".send-status");
    for (var i = 0; i < nodes.length; i++) {
      if (nodes[i].dataset.peer === peerId) {
        nodes[i].textContent = text;
        nodes[i].className = "send-status" + (cls ? " " + cls : "");
        nodes[i].dataset.peer = peerId;
      }
    }
  }

  function watchSend(offerId, peerId) {
    if (state.watch[offerId]) return;
    var ticks = 0;
    var labels = { pending: "waiting for accept…", accepted: "accepted…",
                   completed: "delivered", declined: "declined",
                   failed: "failed", expired: "expired" };
    function stop() {
      clearInterval(state.watch[offerId]);
      delete state.watch[offerId];
    }
    state.watch[offerId] = setInterval(function () {
      ticks++;
      api("/api/send/" + offerId).then(function (o) {
        var s = o.status || "pending";
        setSendStatus(peerId, labels[s] || s,
          s === "completed" ? "ok" :
          (s === "declined" || s === "failed" || s === "expired") ? "bad" : "");
        if (s !== "pending" && s !== "accepted") stop();
      }).catch(stop);
      if (ticks >= SEND_POLL_MAX) stop();
    }, SEND_POLL_MS);
  }

  function doSendText(peer, input, btn) {
    var text = input.value;
    if (!text.trim()) return;
    btn.disabled = true;
    setSendStatus(peer.id, "sending…");
    api("/api/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ peer_id: peer.id, text: text })
    }).then(function (offer) {
      setSendStatus(peer.id, "waiting for accept…");
      delete state.sendDraft[peer.id];
      input.value = "";
      if (offer.offer_id) watchSend(offer.offer_id, peer.id);
    }).catch(function (err) {
      setSendStatus(peer.id, "failed", "bad");
      toast("Could not send to " + (peer.name || "peer") + ": " + err.message,
        "error");
    }).then(function () {
      btn.disabled = false;
    });
  }

  /* ---------- peers ---------- */

  function buildSendBlock(peer) {
    var block = el("div", "send-block");

    var labelRow = el("div", "send-label-row");
    labelRow.appendChild(el("span", "send-label", "Send directly"));
    var saved = state.sendStatus[peer.id];
    var status = el("span",
      "send-status" + (saved && saved.cls ? " " + saved.cls : ""),
      saved ? saved.text : "");
    status.dataset.peer = peer.id;
    labelRow.appendChild(status);
    block.appendChild(labelRow);

    var row = el("div", "send-row");
    var input = el("input", "send-input");
    input.type = "text";
    input.placeholder = "Text to send to " + (peer.name || "this device") + "…";
    input.spellcheck = false;
    input.dataset.peer = peer.id;
    input.value = state.sendDraft[peer.id] || "";
    input.addEventListener("input", function () {
      state.sendDraft[peer.id] = input.value;
    });
    var sendBtn = el("button", "btn btn-primary", "Send");
    sendBtn.type = "button";
    sendBtn.addEventListener("click", function () {
      doSendText(peer, input, sendBtn);
    });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") sendBtn.click();
    });
    row.appendChild(input);
    row.appendChild(sendBtn);
    block.appendChild(row);

    // Honest capability note: the browser can't hand local file *paths* to
    // /api/send, so targeted file sends live in the widget / CLI for now.
    var hint = el("p", "send-hint");
    hint.appendChild(document.createTextNode(
      "Text goes straight to this device (they accept first). To send files " +
      "to just this device, use the tray widget or "));
    hint.appendChild(el("code", null, "ccp send"));
    hint.appendChild(document.createTextNode(
      " — drag & drop above shares with everyone."));
    block.appendChild(hint);

    return block;
  }

  function renderPeers(peers) {
    // Preserve focus + caret of a per-peer send input across re-renders.
    var focusPeer = null, selStart = 0, selEnd = 0;
    var active = document.activeElement;
    if (active && active.classList && active.classList.contains("send-input") &&
        els.peersList.contains(active)) {
      focusPeer = active.dataset.peer;
      selStart = active.selectionStart;
      selEnd = active.selectionEnd;
    }

    els.peersList.textContent = "";
    if (!peers.length) {
      els.peersList.appendChild(el("p", "empty-state",
        "No devices found yet. Make sure cross-copy is running on your other machines, " +
        "or add one manually below."));
      return;
    }
    peers.forEach(function (peer) {
      var card = el("div", "glass card peer-card");

      var head = el("div", "peer-head");
      var title = el("div", "peer-title");
      title.appendChild(el("span", "peer-icon", platformIcon(peer.platform)));
      var nameBlock = el("div", "peer-name-block");
      nameBlock.appendChild(el("span", "peer-name", peer.name || peer.id));
      nameBlock.appendChild(el("span", "peer-host",
        peer.host + ":" + peer.port + (peer.source === "manual" ? " · manual" : "")));
      title.appendChild(nameBlock);
      head.appendChild(title);

      var m = peer.clipboard;
      var textClip = isTextManifest(m);
      if (textClip || (m && m.files && m.files.length)) {
        var badge = el("span", "op-badge op-" + m.op, opBadgeText(m.op));
        head.appendChild(badge);
        card.appendChild(head);
        card.appendChild(el("div", "peer-sharing-label",
          (peer.name || peer.id) + " is sharing:"));
        card.appendChild(textClip ? renderTextClip(m, true) : renderFileList(m));

        var actions = el("div", "peer-actions");
        var receiveBtn = el("button", "btn btn-primary",
          textClip ? "Get text" : "Save to this device");
        receiveBtn.type = "button";
        receiveBtn.addEventListener("click", function () {
          doReceive(peer, receiveBtn, textClip);
        });
        actions.appendChild(receiveBtn);
        card.appendChild(actions);
      } else {
        card.appendChild(head);
        card.appendChild(el("p", "empty-state small", "Not sharing anything"));
      }

      card.appendChild(buildSendBlock(peer));
      els.peersList.appendChild(card);
    });

    if (focusPeer) {
      var inputs = els.peersList.querySelectorAll(".send-input");
      for (var i = 0; i < inputs.length; i++) {
        if (inputs[i].dataset.peer === focusPeer) {
          inputs[i].focus();
          try { inputs[i].setSelectionRange(selStart, selEnd); } catch (e) { /* ok */ }
          break;
        }
      }
    }
  }

  /* ---------- refresh: SSE + slow fallback poll ---------- */

  function refresh() {
    if (state.busy) return Promise.resolve();
    return Promise.all([
      api("/api/status"),
      api("/api/peers?with_clipboard=1"),
      // Older daemons have no /api/offers — treat 404 (or any error) as none.
      api("/api/offers").catch(function () { return { offers: [] }; })
    ]).then(function (results) {
      setConnected(true);
      renderLocal(results[0]);
      renderPeers(results[1].peers || []);
      renderOffers((results[2] && results[2].offers) || []);
      // Daemon reachable: if the event stream is down (e.g. it was an older
      // daemon without /api/events that has since updated), try it again.
      if (!state.es) connectEvents();
    }).catch(function () {
      setConnected(false);
    });
  }

  function scheduleRefresh() {
    // Debounced refresh so bursts of SSE events coalesce into one refetch.
    if (state.refreshTimer) return;
    state.refreshTimer = setTimeout(function () {
      state.refreshTimer = null;
      refresh();
    }, REFRESH_DEBOUNCE_MS);
  }

  function connectEvents() {
    if (!window.EventSource || state.es) return;
    var es = new EventSource("/api/events");
    state.es = es;
    state.sseOpened = false;
    es.onopen = function () {
      state.sseOpened = true;
      setConnected(true);
      scheduleRefresh(); // catch anything missed while the stream was down
    };
    es.onmessage = function () {
      scheduleRefresh();
    };
    es.onerror = function () {
      if (!state.sseOpened) {
        // Never got a stream — the endpoint is probably missing (older
        // daemon, mixed versions). Stay quiet: no banner, the fallback poll
        // carries the day, and each successful poll retries the stream.
        es.close();
        if (state.es === es) state.es = null;
        return;
      }
      // Had a live stream and lost it — daemon likely went away.
      setConnected(false);
      if (es.readyState === EventSource.CLOSED) {
        // Browser gave up auto-reconnecting; the poll will re-establish.
        if (state.es === es) state.es = null;
      }
      // else: EventSource auto-reconnects; onopen clears the banner.
    };
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(refresh, POLL_MS);
  }

  /* ---------- actions ---------- */

  function copyToBrowserClipboard(text) {
    // Resolves true if the text landed in the browser clipboard, false otherwise.
    // Never rejects and never hangs (short timeout guard).
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return new Promise(function (resolve) {
        var timer = setTimeout(function () { resolve(false); }, 1500);
        navigator.clipboard.writeText(text).then(
          function () { clearTimeout(timer); resolve(true); },
          function () { clearTimeout(timer); resolve(false); }
        );
      });
    }
    return Promise.resolve(false);
  }

  function doReceive(peer, btn, textMode) {
    // Getting text needs no destination folder; saving files keeps requiring one.
    var body = { peer_id: peer.id };
    if (!textMode) {
      var dest = els.destInput.value.trim();
      if (!dest) {
        toast("Enter a folder to save into first (absolute path).", "error");
        els.destInput.focus();
        return;
      }
      localStorage.setItem(DEST_KEY, dest);
      body.dest = dest;
    }
    var label = btn.textContent;
    state.busy = true;
    btn.disabled = true;
    btn.textContent = textMode ? "Getting text…" : "Saving…";
    api("/api/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (res) {
      var fromName = (res.from && res.from.name) || peer.name;
      if (res.kind === "text") {
        var text = res.text || "";
        els.textInput.value = text;
        updateShareTextBtn();
        setMode("text");
        return copyToBrowserClipboard(text).then(function (ok) {
          toast("Got text (" + text.length + " chars) from " + fromName +
            (ok ? " — copied to your clipboard" : ""), "success");
        });
      }
      var n = (res.files_written || []).length;
      toast("Saved " + n + " file" + (n === 1 ? "" : "s") +
        " (" + humanSize(res.total_bytes) + ") from " + fromName, "success");
    }).catch(function (err) {
      toast((textMode ? "Could not get text: " : "Could not save files: ") +
        err.message, "error");
    }).then(function () {
      state.busy = false;
      btn.disabled = false;
      btn.textContent = label;
      refresh();
    });
  }

  function updateShareTextBtn() {
    els.shareTextBtn.disabled = !els.textInput.value.trim();
  }

  function setMode(mode) {
    var textMode = mode === "text";
    els.tabFiles.classList.toggle("active", !textMode);
    els.tabText.classList.toggle("active", textMode);
    els.tabFiles.setAttribute("aria-selected", String(!textMode));
    els.tabText.setAttribute("aria-selected", String(textMode));
    els.dropZone.classList.toggle("hidden", textMode);
    els.textPane.classList.toggle("hidden", !textMode);
  }

  function doShareText() {
    var text = els.textInput.value;
    if (!text.trim()) return;
    els.shareTextBtn.disabled = true;
    api("/api/copy", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text, op: "copy" })
    }).then(function () {
      toast("Now sharing text (" + text.length + " chars)", "success");
      els.textInput.value = "";
      refresh();
    }).catch(function (err) {
      toast("Could not share text: " + err.message, "error");
    }).then(updateShareTextBtn);
  }

  function doStopSharing() {
    els.clearBtn.disabled = true;
    api("/api/clipboard/clear", { method: "POST" }).then(function () {
      toast("Stopped sharing", "success");
    }).catch(function (err) {
      toast("Could not stop sharing: " + err.message, "error");
    }).then(function () {
      els.clearBtn.disabled = false;
      refresh();
    });
  }

  function doUpload(fileList) {
    var files = Array.prototype.slice.call(fileList);
    if (!files.length) return;
    state.busy = true;
    els.uploadProgress.classList.remove("hidden");
    els.progressBar.style.width = "0%";
    els.progressLabel.textContent = "Uploading " + files.length +
      " file" + (files.length === 1 ? "" : "s") + "…";

    var form = new FormData();
    files.forEach(function (f) { form.append("files", f, f.name); });

    var xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    xhr.upload.addEventListener("progress", function (e) {
      if (e.lengthComputable) {
        var pct = Math.round((e.loaded / e.total) * 100);
        els.progressBar.style.width = pct + "%";
        els.progressLabel.textContent = "Uploading… " + pct + "%";
      }
    });
    xhr.addEventListener("load", function () {
      state.busy = false;
      els.uploadProgress.classList.add("hidden");
      if (xhr.status >= 200 && xhr.status < 300) {
        var manifest = {};
        try { manifest = JSON.parse(xhr.responseText); } catch (e) { /* ignore */ }
        var n = (manifest.files || files).length;
        toast("Now sharing " + n + " file" + (n === 1 ? "" : "s") +
          " (" + humanSize(manifest.total_size) + ")", "success");
      } else {
        toast("Upload failed (HTTP " + xhr.status + ")", "error");
      }
      refresh();
    });
    xhr.addEventListener("error", function () {
      state.busy = false;
      els.uploadProgress.classList.add("hidden");
      toast("Upload failed: network error", "error");
      refresh();
    });
    xhr.send(form);
  }

  function doAddPeer(e) {
    e.preventDefault();
    var host = els.peerHost.value.trim();
    var port = parseInt(els.peerPort.value, 10) || 7373;
    if (!host) return;
    els.addPeerBtn.disabled = true;
    api("/api/peers/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: host, port: port })
    }).then(function (peer) {
      toast("Added " + (peer.name || host), "success");
      els.peerHost.value = "";
      refresh();
    }).catch(function (err) {
      toast("Could not reach " + host + ":" + port + " — " + err.message, "error");
    }).then(function () {
      els.addPeerBtn.disabled = false;
    });
  }

  function dismissUpdate() {
    var v = els.updateBanner.dataset.version;
    if (v) localStorage.setItem(UPDATE_DISMISS_KEY, v);
    els.updateBanner.classList.add("hidden");
  }

  /* ---------- wiring ---------- */

  els.clearBtn.addEventListener("click", doStopSharing);
  els.addPeerForm.addEventListener("submit", doAddPeer);
  els.updateDismiss.addEventListener("click", dismissUpdate);

  els.tabFiles.addEventListener("click", function () { setMode("files"); });
  els.tabText.addEventListener("click", function () { setMode("text"); });
  els.textInput.addEventListener("input", updateShareTextBtn);
  els.shareTextBtn.addEventListener("click", doShareText);

  els.pickBtn.addEventListener("click", function () { els.fileInput.click(); });
  els.fileInput.addEventListener("change", function () {
    doUpload(els.fileInput.files);
    els.fileInput.value = "";
  });

  ["dragenter", "dragover"].forEach(function (evt) {
    els.dropZone.addEventListener(evt, function (e) {
      e.preventDefault();
      e.stopPropagation();
      els.dropZone.classList.add("dragging");
    });
  });
  ["dragleave", "drop"].forEach(function (evt) {
    els.dropZone.addEventListener(evt, function (e) {
      e.preventDefault();
      e.stopPropagation();
      els.dropZone.classList.remove("dragging");
    });
  });
  els.dropZone.addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length) {
      doUpload(e.dataTransfer.files);
    }
  });

  els.destInput.addEventListener("change", function () {
    localStorage.setItem(DEST_KEY, els.destInput.value.trim());
  });

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") refresh();
  });

  /* ---------- init ---------- */

  var savedDest = localStorage.getItem(DEST_KEY);
  if (savedDest) els.destInput.value = savedDest;

  refresh();
  connectEvents();
  startPolling();
})();
