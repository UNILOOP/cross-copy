# cross-copy — Design Spec (v0.1.0)

Network clipboard for files between Mac and Linux machines on the same LAN.
UX metaphor: `ccp copy file.txt` on machine A, `ccp paste` on machine B — the file appears.

## Components

- **Python package** `crosscopy`, distribution name `cross-copy`, CLI entry point `ccp`.
- **Daemon** (`python -m crosscopy.daemon` / `ccp daemon run`): Flask HTTP server + zeroconf
  (mDNS) discovery. Serves the peer-facing transfer API, the local control API, and the web UI.
- **CLI** (`crosscopy/cli.py`): thin HTTP client talking to the *local* daemon only.
  Auto-starts the daemon in the background if it isn't running.
- **Web UI** (`crosscopy/webui/` — `index.html`, `app.js`, `style.css`): static files served
  by the daemon at `/`. Talks to the local control API via fetch().

Python >= 3.9. Dependencies: `flask`, `requests`, `zeroconf`. Nothing else.

## Constants & configuration

- Default port: **7373**. Env override: `CROSSCOPY_PORT`.
- Config/home dir: `~/.crosscopy/`. Env override: `CROSSCOPY_HOME`.
  Contents: `config.json` (device name, device id (uuid4), manual peers list),
  `daemon.json` (written by running daemon: `{"pid": int, "port": int}`, removed on clean exit),
  `daemon.log`, `staging/` (web-UI uploaded files), `clipboard.json` (current manifest).
- Zeroconf service type: `_crosscopy._tcp.local.`; service name `<device_id>._crosscopy._tcp.local.`;
  TXT properties: `id`, `name`, `platform` (`darwin`/`linux`), `version`.
- Env `CROSSCOPY_NO_MDNS=1` disables zeroconf (for tests; manual peers still work).
- Device name defaults to hostname; changeable via `config.json` / `ccp name <newname>`.

## Text clipboards (v0.2)

The clipboard holds either **files** or **text**. Manifests carry `"kind": "files"` or
`"kind": "text"` (missing kind ⇒ `files`, for back-compat). Text manifests have a `"text"`
field (the full string, UTF-8, max 1 MB — reject larger with 400), no `files` array, and
`total_size` = byte length. Text is never written to staging; it lives in `clipboard.json`.

- `POST /api/copy` accepts EITHER `{"paths": [...], "op": ...}` OR `{"text": "...", "op": ...}`
  (both present → 400).
- `GET /api/clipboard/meta` returns text manifests verbatim (text included).
- `POST /api/paste` when the chosen peer's clipboard is text: no files are written; `dest` is
  ignored; response is `{"from": {...}, "kind": "text", "text": "...", "op": "copy"|"move"}`.
  The puller still POSTs `consumed`; for a text move the source just clears its clipboard
  (`{"deleted": true}`).
- File-paste responses gain `"kind": "files"`.
- CLI: `ccp copy <args...>` — if every arg is an existing path ⇒ files; if NO arg exists as a
  path ⇒ the args joined with single spaces are copied as text; mixed ⇒ error suggesting
  `--text`. `--text`/`-t` forces text. No args + piped stdin ⇒ copy stdin as text
  (`echo foo | ccp copy`). `ccp paste` with a text clipboard prints the text verbatim to
  stdout (pipeable); the "from device" info line goes to stderr.
- Clipboard summaries (status/devices) show `text (52 chars) "preview…"`.

## Daemon autostart (v0.2)

- `ccp daemon install` — set up start-at-login, then start it now:
  - Linux: systemd user unit `~/.config/systemd/user/cross-copy.service`,
    `ExecStart=<sys.executable> -m crosscopy.daemon`, `Restart=on-failure`;
    `systemctl --user daemon-reload && systemctl --user enable --now cross-copy`.
    No systemd ⇒ print a warning with manual instructions, exit 1.
  - macOS: launchd plist `~/Library/LaunchAgents/com.crosscopy.daemon.plist`,
    `ProgramArguments = [<sys.executable>, "-m", "crosscopy.daemon"]`, `RunAtLoad`,
    `KeepAlive.SuccessfulExit=false`; load via `launchctl bootstrap gui/<uid>` with
    `launchctl load -w` fallback (older macOS).
  - Stops any already-running daemon first so the service owns the port.
- `ccp daemon uninstall` — stop + disable + remove the unit/plist.
- install.sh sets up autostart by DEFAULT by calling `ccp daemon install`
  (opt-out flag `--no-service`; `--service` kept as a no-op alias for back-compat).

## Reciprocal discovery — "hello" (v0.3)

Fixes one-way mDNS visibility (e.g. Mac sees Linux but not vice versa). If either side can
reach the other, both end up knowing each other.

- New peer-facing endpoint `POST /api/hello`, body `{"id","name","platform","version","port"}`.
  Receiver records the sender as a peer (host = request source IP, `source: "hello"`,
  `last_seen` = now), publishes a `peers` event (see SSE below), and responds with its own
  device info + port. Unknown/duplicate ids just update the existing record (id is the key;
  mDNS/manual/hello records for the same id merge, freshest wins).
- Sender behavior (discovery thread): send hello to every known peer (a) on daemon start,
  (b) every 60 s, (c) immediately when the local clipboard changes/clears (so remote UIs
  update live), (d) once when a new peer is first discovered via mDNS.
- Peers with `source: "hello"` expire after 10 min without contact. Manual peers never expire.
- mDNS hardening: register with ALL non-loopback IPv4 addresses and browse/register on all
  interfaces (zeroconf InterfaceChoice.All).

## Server-sent events (v0.3)

- `GET /api/events` → `text/event-stream`. Events are JSON lines like
  `data: {"type": "clipboard"}` / `{"type": "peers"}` / `{"type": "update"}` — they carry no
  payload; clients refetch `/api/status` / `/api/peers?with_clipboard=1` on receipt.
  Heartbeat comment (`: ping`) every 15 s. Published on: local clipboard set/clear/consumed,
  peer added/updated/expired, device rename, update state change.
- Web UI uses EventSource for instant reactivity, keeps a slow 30 s poll as fallback, and
  refetches on `visibilitychange` (tab refocus).

## Updates & auto-update (v0.3)

- Version source: fetch `https://raw.githubusercontent.com/UNILOOP/cross-copy/main/crosscopy/__init__.py`
  and parse `__version__`. Package source for installs:
  `https://github.com/UNILOOP/cross-copy/archive/refs/heads/main.tar.gz` (pip installs tarball
  URLs directly — no git needed). Both overridable via env `CROSSCOPY_UPDATE_URL` /
  `CROSSCOPY_UPDATE_PKG` (also enables testing against a local HTTP server).
- Self-update procedure (shared by daemon auto-update and `ccp update`):
  `[sys.executable, -m, pip, install, --upgrade, <PKG_URL>]`, adding `--user` when not in a
  venv (`sys.prefix == sys.base_prefix`). Failure is reported, never crashes the daemon.
- Daemon auto-update: config `auto_update` (default **true**). Background thread checks
  ~90 s after start and every 6 h. If a newer version is found: with auto_update on, run
  self-update, then re-exec (`os.execv(sys.executable, [python, -m, crosscopy.daemon])`) to
  load the new code (PID preserved — safe under systemd/launchd); with auto_update off, just
  record it. `/api/status` gains `"update": {"current","latest","available": bool,
  "last_checked", "auto_update": bool}` (latest/last_checked null before first check).
- CLI: `ccp update` — check + self-update + daemon restart (stop/start) + print old→new;
  `ccp update --check` — check only, exit 0 with "up to date" or print available version.
  `ccp status` shows an update notice when one is known to be available.
- Web UI: subtle banner when `update.available` ("Update vX.Y.Z available — updating
  automatically" if auto_update, else "run `ccp update`").

## Web UI wording (v0.3)

The copy/paste clipboard metaphor confused users in the UI. CLI keeps copy/paste; the UI
switches to share/receive language:
- Local card title: "Share from this device"; drop zone: "Drag & drop files to share" +
  "Choose files to share" button; text button: "Share text"; when holding content:
  "Currently sharing" + a "Stop sharing" button (replaces Clear).
- Peer cards: "<name> is sharing:"; file receive button: "Save to this device"; text receive
  button: "Get text"; destination input label: "Save into folder".
- Empty states follow the same language ("Nothing shared yet", "Not sharing anything").

## Targeted send with accept/reject — "offers" (v0.4, AirDrop-style)

Push model alongside the pull clipboard: pick a target, target must accept before anything
transfers. Files land in the receiver's `receive_dir` (config, default `~/Downloads/cross-copy`,
`~` expanded server-side; accept may override with an explicit dest).

Offer object: `{"offer_id": uuid, "from": {"id","name","platform"}, "sender_port": int,
"kind": "files"|"text", "files": [{"index","rel_path","size"}] (files kind),
"text": "..." (text kind, full string ≤1MB), "total_size": int, "created_at": epoch,
"status": "pending"|"accepted"|"declined"|"completed"|"failed"|"expired"}`.
Offers expire 300 s after creation (both sides); expired/terminal offers are pruned.
All offer state is in-memory in the daemon (lost on restart — fine).

Sender local API:
- `POST /api/send` body `{"peer_id", "paths": [...] XOR "text": "..."}` → builds outgoing
  offer (dir expansion like /api/copy), POSTs it to the target's `/api/offer`, returns the
  offer object (status pending). 404 unknown peer, 502 target unreachable, 400 bad body.
- `GET /api/send/<offer_id>` → current outgoing offer object (404 unknown/pruned).

Peer-facing (both directions):
- `POST /api/offer` (receiver hosts): body = offer object (minus status) — receiver stores it
  as a pending incoming offer, fires SSE `offers` event + desktop notification, returns
  `{"status": "pending"}`.
- `GET /api/offer/<offer_id>/file/<index>` (sender hosts): streams the offered file while the
  offer is pending/accepted; 410 otherwise.
- `POST /api/offer/<offer_id>/result` (sender hosts): body `{"result": "accepted"|"declined"|
  "completed"|"failed"}` — receiver reports outcomes; sender updates state, fires SSE
  `offers` event + notification on declined/completed/failed.

Receiver local API:
- `GET /api/offers` → `{"offers": [pending incoming offers]}` (text offers include the text).
- `POST /api/offers/<offer_id>/accept` body `{"dest": optional abs dir}` → text: returns
  `{"kind":"text","text",...}` immediately; files: pulls all files from the sender into dest
  (collision renames, partial cleanup like paste), reports `accepted` then `completed`
  (or `failed`) to the sender, returns `{"kind":"files","files_written",...}`.
- `POST /api/offers/<offer_id>/decline` → reports `declined` to sender, removes offer.

CLI:
- `ccp send <path...|text> --to <name|id>` (`-t/--text` forces text; same path-vs-text
  detection as copy). `--to` optional when exactly one peer exists. Prints
  `📨 Waiting for <name> to accept...`, polls `GET /api/send/<id>` (~1 s interval, up to
  300 s), then reports accepted/declined/completed. Exit 0 on completed, 1 on decline/timeout.
- `ccp offers` — list pending incoming offers (index, from, contents, age).
- `ccp accept [offer_id] [dir]` — newest offer if id omitted; text prints to stdout like paste.
- `ccp decline [offer_id]` — newest if omitted.

## Desktop notifications (v0.4)

`crosscopy/notify.py`: `notify(title, body)` — best-effort, never raises, no new deps.
macOS: `osascript -e 'display notification ...'`. Linux: `notify-send` if on PATH, else
`gdbus call org.freedesktop.Notifications.Notify`, else silent no-op. Config key
`"notifications": true` (default). Daemon fires them on: incoming offer ("📥 machine-a wants
to send 3 files (2.1 MB) — accept in the cross-copy UI or `ccp accept`"), offer
declined/completed (sender side), incoming files saved.

## Widget-owned notifications (v0.4.1)

The tray widget is the primary notification surface — OS notification centers gave no
action buttons and looked bad on Linux:
- `crosscopy/popup.py`: standalone tkinter popup processes (`python -m crosscopy.popup
  offer <id>` / `info "<title>" "<body>"`), frameless, always-on-top, top-right stacked
  (`--slot N`), glass-styled. Offer popups show Accept/Decline buttons wired to the local
  offers API and auto-close when the offer resolves elsewhere or expires; info popups
  auto-dismiss ~6 s. Run as subprocesses so tkinter never contends with pystray's main
  thread (critical on macOS).
- The widget subscribes to `/api/events?client=widget`. While any `client=widget`
  subscriber is connected, `notify()` suppresses OS notifications entirely (the widget pops
  its own cards on `offers` events by diffing `/api/offers`). No widget running → OS
  notifications remain as fallback.

## Tray widget (v0.4)

`ccp widget` runs a system-tray / menu-bar companion (`crosscopy/widget.py`):
- Deps `pystray` + `Pillow` are an OPTIONAL extra (`cross-copy[widget]`); missing deps →
  friendly install hint, exit 1. Tray icon (simple generated glyph) with menu: per-peer
  "Send files…" (tkinter file dialog) and "Send clipboard text" (tkinter clipboard), pending
  offers with Accept/Decline, "Open panel", "Open web UI", Quit. Menu content refreshes from
  the local API; a background SSE listener triggers notifications-driven refresh.
- "Open panel" opens `http://localhost:<port>/widget` — a compact liquid-glass panel page
  (`crosscopy/widgetui/{widget.html,widget.js,widget.css}`, served by the daemon at
  `/widget`): peer list with per-peer send (text box + file input), incoming offers with
  accept/decline, live via SSE. Tries browser app-mode (`--app=` for chrome/chromium/brave,
  fallback plain tab).

## Liquid glass design (v0.4)

Both the main web UI and the widget panel adopt a "liquid glass" look: layered translucent
panels (`backdrop-filter: blur(…) saturate(…)`, rgba fills), soft specular top-edge
highlights, 16-20px radii, subtle inner/outer borders (white/alpha), depth via large soft
shadows, a colorful ambient gradient page background so the blur reads, smooth micro
transitions. Must stay dependency-free (pure CSS) and legible (WCAG-ish contrast for text).

## Clipboard manifest (JSON)

```json
{
  "clipboard_id": "uuid4-string",
  "op": "copy",                      // or "move"
  "created_at": 1730000000.0,        // epoch float
  "host_id": "device-uuid",
  "host_name": "sayeed-macbook",
  "total_size": 12345,
  "files": [
    {"index": 0, "rel_path": "notes.txt", "size": 100, "source_path": "/abs/path/notes.txt"}
  ]
}
```

- Directories are expanded recursively **at copy time**; each entry's `rel_path` is a POSIX
  relative path that includes the top-level dir name (e.g. `photos/2024/img.jpg`).
- `source_path` (absolute path on the source machine) is stored locally but **stripped** from
  manifests served to peers.

## Peer-facing HTTP API (used machine→machine)

- `GET /api/ping` → `{"id","name","platform","version"}`
- `GET /api/clipboard/meta` → manifest (without `source_path`), or 404 `{"error":"clipboard empty"}`
- `GET /api/clipboard/file/<clipboard_id>/<index>` → file bytes stream
  (`application/octet-stream`); 410 if `clipboard_id` no longer current; 404 bad index.
- `POST /api/clipboard/consumed` body `{"clipboard_id": "..."}` → if current clipboard matches
  and op is `move`: delete the source files (and now-empty source dirs), clear clipboard,
  return `{"deleted": true}`. Otherwise `{"deleted": false}`.

## Local control API (used by CLI and web UI; same server)

- `GET /api/status` → `{"id","name","port","platform","version","clipboard": manifest|null}`
- `GET /api/peers?with_clipboard=1` → `{"peers":[{"id","name","host","port","platform",
  "version","last_seen","source":"mdns"|"manual","clipboard": manifest|null}]}`
  (`clipboard` only when `with_clipboard=1`; fetched live from each peer, short timeout ~2s)
- `POST /api/peers/add` body `{"host": "192.168.1.5", "port": 7373}` → pings it, saves to
  `config.json` manual peers, returns peer info. Error 502 if unreachable.
- `POST /api/copy` body `{"paths": ["/abs/path", ...], "op": "copy"|"move"}` → validates paths
  exist, expands dirs, writes `clipboard.json`, returns manifest. 400 on missing paths.
- `POST /api/paste` body `{"dest": "/abs/dir", "peer_id": optional, "clipboard_id": optional}` →
  chooses peer: `peer_id` if given, else the peer with the **newest** non-empty clipboard.
  Downloads all files into `dest` preserving `rel_path` subdirs. Name-collision handling:
  suffix ` (1)`, ` (2)`... before the extension. After success, POSTs `consumed` to the source.
  Returns `{"from": {"id","name"}, "files_written": ["/abs/..."], "total_bytes": int, "op": "copy"|"move"}`.
  Errors: 404 no peer has a clipboard; 502 transfer failure (partial files cleaned up).
- `POST /api/clipboard/clear` → `{"cleared": true}`
- `POST /api/name` body `{"name": "new-device-name"}` → updates device name in config, returns
  device info. (mDNS TXT name refreshes on next daemon restart.)
- `POST /api/upload` multipart form, field `files` (repeatable) → saves into
  `~/.crosscopy/staging/<clipboard_id>/`, sets clipboard (op=copy), returns manifest.
- `GET /` and static assets → web UI from `crosscopy/webui/`.

## CLI commands (`ccp`)

- `ccp copy <path...>` — put files/dirs on the network clipboard (op=copy). Auto-starts daemon.
- `ccp move <path...>` — same with op=move (source files deleted after successful paste).
- `ccp paste [dir]` — paste newest peer clipboard into dir (default: CWD). `--from <name|id>`.
- `ccp devices` (alias `ccp list`) — table: name, ip, platform, clipboard summary ("3 files, 2.1 MB" or "-").
- `ccp status` — daemon status + current local clipboard.
- `ccp clear` — clear local clipboard.
- `ccp add <host> [port]` — add manual peer (when mDNS is blocked).
- `ccp name <newname>` — set device name.
- `ccp daemon run|start|stop|status` — `run` foreground, `start` background (detached
  subprocess of `[sys.executable, "-m", "crosscopy.daemon"]`, stdout/err → `daemon.log`).
- `ccp ui` — open `http://localhost:<port>/` in browser (webbrowser module).
- `ccp version`.

Auto-start logic: before any command needing the daemon, `GET /api/ping` on localhost; if it
fails, spawn detached daemon, poll ping up to ~5s. Human-friendly output, sizes formatted
(KB/MB/GB), non-zero exit codes on error.

## Move semantics

Source machine keeps files in place at `ccp move` time. When a peer finishes pasting it calls
`POST /api/clipboard/consumed`; only then does the source delete the files and clear its
clipboard. `ccp copy` clipboard stays valid for repeated pastes until replaced/cleared.

## Security (MVP)

Trusted-LAN model: no auth/encryption in v0.1.0. Server binds 0.0.0.0. README must state
this clearly with a roadmap note (pairing codes + TLS planned).

## Testing on one machine

Two instances: `CROSSCOPY_HOME=/tmp/a CROSSCOPY_PORT=7373` and
`CROSSCOPY_HOME=/tmp/b CROSSCOPY_PORT=7474`, `CROSSCOPY_NO_MDNS=1`, wire with `ccp add`.
