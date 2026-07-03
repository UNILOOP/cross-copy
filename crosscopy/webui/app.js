/* cross-copy web UI — vanilla JS, no dependencies. */
(function () {
  "use strict";

  var POLL_MS = 5000;
  var DEST_KEY = "crosscopy.dest";

  var $ = function (id) { return document.getElementById(id); };

  var els = {
    banner: $("reconnect-banner"),
    deviceName: $("device-name"),
    devicePlatform: $("device-platform"),
    daemonVersion: $("daemon-version"),
    localClipboard: $("local-clipboard"),
    opBadge: $("local-op-badge"),
    clearBtn: $("clear-btn"),
    dropZone: $("drop-zone"),
    pickBtn: $("pick-btn"),
    fileInput: $("file-input"),
    uploadProgress: $("upload-progress"),
    progressBar: $("progress-bar"),
    progressLabel: $("progress-label"),
    destInput: $("dest-input"),
    peersList: $("peers-list"),
    addPeerForm: $("add-peer-form"),
    peerHost: $("peer-host"),
    peerPort: $("peer-port"),
    addPeerBtn: $("add-peer-btn"),
    toasts: $("toasts")
  };

  var state = {
    busy: false,          // upload/paste in flight -> pause polling
    connected: true,
    pollTimer: null
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

  function renderLocal(status) {
    els.deviceName.textContent = status.name || "?";
    els.devicePlatform.textContent = platformIcon(status.platform) + " " + (status.platform || "");
    els.daemonVersion.textContent = status.version ? "v" + status.version : "";

    var m = status.clipboard;
    els.localClipboard.textContent = "";
    if (m && m.files && m.files.length) {
      els.localClipboard.appendChild(renderFileList(m));
      els.opBadge.textContent = m.op;
      els.opBadge.className = "op-badge op-" + m.op;
      els.clearBtn.classList.remove("hidden");
    } else {
      var empty = el("p", "empty-state");
      empty.innerHTML = 'Clipboard is empty. Use <code>ccp copy &lt;file&gt;</code> or drop files below.';
      els.localClipboard.appendChild(empty);
      els.opBadge.className = "op-badge hidden";
      els.clearBtn.classList.add("hidden");
    }
  }

  function renderPeers(peers) {
    els.peersList.textContent = "";
    if (!peers.length) {
      els.peersList.appendChild(el("p", "empty-state",
        "No devices found yet. Make sure cross-copy is running on your other machines, " +
        "or add one manually below."));
      return;
    }
    peers.forEach(function (peer) {
      var card = el("div", "card peer-card");

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
      if (m && m.files && m.files.length) {
        var badge = el("span", "op-badge op-" + m.op, m.op);
        head.appendChild(badge);
        card.appendChild(head);
        card.appendChild(renderFileList(m));

        var actions = el("div", "peer-actions");
        var pasteBtn = el("button", "btn btn-primary", "Paste here");
        pasteBtn.type = "button";
        pasteBtn.addEventListener("click", function () {
          doPaste(peer, pasteBtn);
        });
        actions.appendChild(pasteBtn);
        card.appendChild(actions);
      } else {
        card.appendChild(head);
        card.appendChild(el("p", "empty-state small", "Clipboard empty"));
      }
      els.peersList.appendChild(card);
    });
  }

  /* ---------- polling ---------- */

  function refresh() {
    if (state.busy) return Promise.resolve();
    return Promise.all([
      api("/api/status"),
      api("/api/peers?with_clipboard=1")
    ]).then(function (results) {
      setConnected(true);
      renderLocal(results[0]);
      renderPeers(results[1].peers || []);
    }).catch(function () {
      setConnected(false);
    });
  }

  function startPolling() {
    if (state.pollTimer) clearInterval(state.pollTimer);
    state.pollTimer = setInterval(refresh, POLL_MS);
  }

  /* ---------- actions ---------- */

  function doPaste(peer, btn) {
    var dest = els.destInput.value.trim();
    if (!dest) {
      toast("Enter a destination directory first (absolute path).", "error");
      els.destInput.focus();
      return;
    }
    localStorage.setItem(DEST_KEY, dest);
    state.busy = true;
    btn.disabled = true;
    btn.textContent = "Pasting…";
    api("/api/paste", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dest: dest, peer_id: peer.id })
    }).then(function (res) {
      var n = (res.files_written || []).length;
      toast("Pasted " + n + " file" + (n === 1 ? "" : "s") +
        " (" + humanSize(res.total_bytes) + ") from " +
        ((res.from && res.from.name) || peer.name), "success");
    }).catch(function (err) {
      toast("Paste failed: " + err.message, "error");
    }).then(function () {
      state.busy = false;
      btn.disabled = false;
      btn.textContent = "Paste here";
      refresh();
    });
  }

  function doClear() {
    els.clearBtn.disabled = true;
    api("/api/clipboard/clear", { method: "POST" }).then(function () {
      toast("Clipboard cleared", "success");
    }).catch(function (err) {
      toast("Clear failed: " + err.message, "error");
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
        toast("Copied " + n + " file" + (n === 1 ? "" : "s") +
          " (" + humanSize(manifest.total_size) + ") to the network clipboard", "success");
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

  /* ---------- wiring ---------- */

  els.clearBtn.addEventListener("click", doClear);
  els.addPeerForm.addEventListener("submit", doAddPeer);

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

  /* ---------- init ---------- */

  var savedDest = localStorage.getItem(DEST_KEY);
  if (savedDest) els.destInput.value = savedDest;

  refresh();
  startPolling();
})();
