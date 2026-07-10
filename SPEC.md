# cross-copy — Design Spec (v0.1.0)

Network clipboard for files and text between Windows, Mac, and Linux machines on the same LAN.
UX metaphor: `ccp copy file.txt` on machine A, `ccp paste` on machine B — the file appears.

## Components

- **Python package** `crosscopy`, distribution name `cross-copy`, CLI entry point `ccp`.
- **Daemon** (`python -m crosscopy.daemon` / `ccp daemon run`): Flask HTTP server + zeroconf
  (mDNS) discovery. Serves the peer-facing transfer API, the local control API, and the web UI.
- **CLI** (`crosscopy/cli.py`): thin HTTP client talking to the *local* daemon only.
  Auto-starts the daemon in the background if it isn't running.
- **Context-menu integration** (`crosscopy/contextmenu.py`): installs per-user Finder,
  Explorer, and Linux file-manager actions backed by the local CLI/API.
- **Web UI** (`crosscopy/webui/` — `index.html`, `app.js`, `style.css`): static files served
  by the daemon at `/`. Talks to the local control API via fetch().

Python >= 3.9. Dependencies: `flask`, `requests`, `zeroconf>=0.100`. Nothing else.

## Constants & configuration

- Default port: **7373**. Env override: `CROSSCOPY_PORT`.
- Config/home dir: `~/.crosscopy/`. Env override: `CROSSCOPY_HOME`.
  Contents: `config.json` (device name, device id (uuid4), manual peers list),
  `daemon.json` (written by running daemon: `{"pid": int, "port": int}`, removed on clean exit),
  `daemon.log`, `staging/` (web-UI uploaded files), `clipboard.json` (current manifest),
  `resumes/` (registry pointers for incomplete destination-local transfers).
- Zeroconf service type: `_crosscopy._tcp.local.`; service name `<device_id>._crosscopy._tcp.local.`;
  TXT properties: `id`, `name`, `platform` (`darwin`/`linux`/`win32`), `version`.
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
  - Windows: hidden `pythonw.exe`/`.pyw` launcher registered in the current
    user's `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` key. It avoids
    a console flash and forwards `CROSSCOPY_HOME`/`CROSSCOPY_PORT`.
    Installation immediately starts the detached daemon; no admin rights are required.
  - Stops any already-running daemon first so the service owns the port.
- `ccp daemon uninstall` — stop + disable + remove the unit/plist.
- install.sh sets up autostart by DEFAULT by calling `ccp daemon install`
  (opt-out flag `--no-service`; `--service` kept as a no-op alias for back-compat).
- install.sh also adds `~/.local/bin` to the detected user's shell startup
  configuration. Bash interactive and login profiles, zsh, fish, csh/tcsh,
  and POSIX-compatible shells are handled with idempotent managed blocks.
  Interactive installs start a refreshed login shell in the same terminal;
  non-interactive installs skip the reload. `--path-only` repairs only this
  configuration. uninstall.sh removes only the managed blocks it created.
- Both installers call `ccp context install` after package verification. Uninstallers call
  `ccp context uninstall` before removing the environment, with exact-path or registry
  fallback cleanup for incomplete installations.

## Native file-manager actions

- Two actions accept multi-file and directory selections:
  - **Share to all devices** calls local `/api/copy` with `op=copy`, placing the selection on
    the network clipboard for any peer to paste.
  - **Share to a device…** fetches `/api/peers`, opens a native chooser, then calls `/api/send`
    without waiting for the targeted offer to finish.
- macOS installs Automator Quick Action workflows under `~/Library/Services/` for Finder.
- Windows installs a per-user cascading Explorer verb under
  `HKCU\Software\Classes\AllFilesystemObjects\shell\CrossCopy`. Commands use the branded
  `Cross Copy.exe` GUI launcher, `%*` multi-selection, and require no administrator access.
- Linux installs executable scripts for Nautilus/GNOME Files, Nemo, and Caja, plus KDE 5/6
  Dolphin service menus. The device chooser uses Zenity or KDialog; a single known peer can
  be selected without either helper.
- File-manager launchers call the active environment's absolute Python path instead of
  relying on a graphical session's PATH. macOS/Linux wrappers detach immediately; Explorer
  uses the no-console branded launcher.
- `ccp context install|uninstall` owns the lifecycle. `share-all` and
  `share-to [--to peer]` are supported invocation actions used by native registrations.
- Context-action errors use `crosscopy.popup info --show-panel`, not an AppleScript-owned
  notification. Its **Show** button opens `/widget` through the native macOS panel (or the
  browser fallback on other platforms), so notification clicks remain owned by Cross Copy.
- CI performs install→artifact assertions→uninstall assertions on macOS, Linux, Windows
  PowerShell 5.1, and Windows PowerShell 7. Windows checks the real per-user registry key;
  macOS/Linux checks the installed workflow/script/service-menu files.

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
- Windows network changes are polled every 15 seconds. The daemon rescans
  zeroconf interfaces on every pass, reannounces once per minute, and rebuilds
  the mDNS registration when the local IPv4 set changes.
- A versioned UDP broadcast beacon on port 7374 supplements mDNS. It contains
  only device metadata and the daemon port; receiving it records the source IP
  and immediately attempts the normal reciprocal HTTP hello. Directed subnet
  broadcasts and `255.255.255.255` are both used. `CROSSCOPY_DISCOVERY_PORT`
  overrides the beacon port. `CROSSCOPY_NO_MDNS=1` disables both mDNS and the
  broadcast fallback for isolated tests/manual-only operation.

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
  load the new code (PID preserved where the OS supports exec); with auto_update off, just
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
"kind": "files"|"text", "files": [{"index","rel_path","size","sha256"}] (files kind),
"text": "..." (text kind, full string ≤1MB), "total_size": int, "created_at": epoch,
"status": "pending"|"accepted"|"declined"|"completed"|"failed"|"expired"}`.
Pending offers expire after 300 s; accepted resumable transfers remain active
for 24 hours by default (`CROSSCOPY_ACCEPTED_TTL` overrides this).
Expired/terminal offers are pruned.
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
  offer is pending/accepted; supports HTTP byte ranges and returns checksum/size headers;
  410 otherwise.
- `POST /api/offer/<offer_id>/result` (sender hosts): body `{"result": "accepted"|"declined"|
  "completed"|"failed"}` — receiver reports outcomes; sender updates state, fires SSE
  `offers` event + notification on declined/completed/failed.

Receiver local API:
- `GET /api/offers` → `{"offers": [pending incoming offers]}` (text offers include the text).
- `POST /api/offers/<offer_id>/accept` body `{"dest": optional abs dir}` → text: returns
  `{"kind":"text","text",...}` immediately; files: pulls all files from the sender into dest
  (collision renames, persistent partial state, range resume, and SHA-256 verification),
  reports `accepted` then `completed`. An interruption returns the offer to pending so a
  later accept resumes it, and returns `{"kind":"files","files_written",...}` on success.
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
- `crosscopy/popup.py`: standalone popup processes (`python -m crosscopy.popup
  offer <id>` / `info "<title>" "<body>"` / `share <peer> <clip_id>`), frameless,
  always-on-top, top-right stacked (`--slot N`), glass-styled. Offer popups show
  Accept/Decline buttons wired to the local offers API and auto-close when the offer
  resolves elsewhere or expires; share popups show Dismiss + Save here/Get text
  (auto-dismiss 60 s); info popups auto-dismiss ~6 s. Run as subprocesses so the GUI
  toolkit never contends with pystray's main thread.
- Backend per platform: **darwin** renders each card as a native AppKit window
  (borderless non-activating `NSPanel`, `NSStatusWindowLevel`, accessory activation
  policy — no Dock icon, rounded dark layer-backed card, native `NSButton`s,
  target/selector `NSTimer`s for auto-dismiss/offer-poll, HTTP on worker threads with UI
  dispatched back via `NSOperationQueue.mainQueue`), because tkinter
  `overrideredirect` windows are unreliable on aqua Tk (cards never appear / can't take
  clicks). PyObjC is guaranteed present there (pystray depends on it). **Windows/Linux**
  use tkinter cards (Segoe UI on Windows). `--dry-run` prints computed
  geometry/content JSON on every platform.
- Last-resort fallback: if no windowing backend can show a card (AppKit **and** tkinter
  fail on mac; tkinter fails on Windows/Linux), the popup process fires a plain OS notification
  via `crosscopy.notify`'s platform helpers directly (Shell_NotifyIconW / osascript / notify-send),
  bypassing notify()'s widget-connected suppression — the popup IS the widget's
  notification path, so the user is never left with silence.
- The widget subscribes to `/api/events?client=widget`. While any `client=widget`
  subscriber is connected, `notify()` suppresses OS notifications entirely (the widget pops
  its own cards on `offers` events by diffing `/api/offers`). No widget running → OS
  notifications remain as fallback.
- `ccp update` restarts a running tray widget after a successful self-update (process termination
  via `widget.json` pid, then respawn — unless launchd/autostart already brought it
  back), so the widget and its popup cards never keep running stale code. It also
  verifies the restarted daemon reports the just-installed version and warns loudly when
  it doesn't (daemon running from a different Python environment); `ccp status` /
  `ccp daemon start` print a one-line version-mismatch warning too. All best-effort,
  never fatal.

## Tray widget (v0.4)

`ccp widget` runs a system-tray / menu-bar companion (`crosscopy/widget.py`):
- Deps `pystray` + `Pillow` are an OPTIONAL extra (`cross-copy[widget]`); missing deps →
  friendly install hint, exit 1. Tray icon (simple generated glyph) with menu: per-peer
  "Send files…" (native NSOpenPanel on macOS, Windows common dialog, and
  GTK/KDE chooser on Linux; tkinter only as a last resort) and "Send clipboard
  text" (tkinter clipboard), pending
  offers with Accept/Decline, "Open panel", "Open web UI", Quit. Menu content refreshes from
  the local API; a background SSE listener triggers notifications-driven refresh.
- "Open panel" opens `http://localhost:<port>/widget` — a compact liquid-glass panel page
  (`crosscopy/widgetui/{widget.html,widget.js,widget.css}`, served by the daemon at
  `/widget`): peer list with per-peer send (text box + file input), incoming offers with
  accept/decline, live via SSE. macOS: a native floating NSWindow + WKWebView
  (`crosscopy/macpanel.py`, own subprocess, 420x680 top-right, floating level, no Dock
  icon; needs `pyobjc-framework-WebKit`, in the widget extra; exit code 3 = missing deps →
  fall back). Windows/Linux fallback: browser app-mode (`--app=`
  `--window-size=420,680` for Edge/Chrome/Chromium/Brave; Windows install
  locations and macOS bundle paths are checked too), last resort plain tab.

## Windows support

- `install.ps1` installs a dedicated venv under `%LOCALAPPDATA%\CrossCopy`,
  includes the `widget` extra, creates a `ccp.cmd` user-PATH shim, and enables
  daemon/widget login startup by default. It persists the bin directory in
  the user PATH, updates the current PowerShell process, and broadcasts the
  environment change to Windows. `uninstall.ps1` reverses it and keeps
  `~\.crosscopy` unless asked to remove data.
- Detached child processes use `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`;
  the login launcher uses a venv-local `pythonw.exe` copy named `Cross Copy.exe`
  and redirects missing stdio to daemon/widget logs.
- The pystray Win32 backend provides the notification-area menu. Tk provides
  native file selection, clipboard text access, and actionable popup cards.
- With no connected widget, `crosscopy.winnotify` uses the inbox Win32
  `Shell_NotifyIconW` API to display a banner without third-party packages.

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
    {"index": 0, "rel_path": "notes.txt", "size": 100,
     "sha256": "64-lowercase-hex-digest", "source_path": "/abs/path/notes.txt"}
  ]
}
```

- Directories are expanded recursively **at copy time**; each entry's `rel_path` is a POSIX
  relative path that includes the top-level dir name (e.g. `photos/2024/img.jpg`).
- `source_path` and `mtime_ns` are stored locally but **stripped** from manifests served to
  peers. The public per-file `sha256` is the expected final digest.

## Peer-facing HTTP API (used machine→machine)

- `GET /api/ping` → `{"id","name","platform","version"}`
- `GET /api/clipboard/meta` → manifest (without `source_path`), or 404 `{"error":"clipboard empty"}`
- `GET /api/clipboard/file/<clipboard_id>/<index>` → file bytes stream
  (`application/octet-stream`) with HTTP Range support plus
  `X-CrossCopy-SHA256`/`X-CrossCopy-Size`; 410 if `clipboard_id` no longer current;
  404 bad index; 409 if the source changed after sharing.
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
  suffix ` (1)`, ` (2)`... before the extension. Partial bytes and transfer state are retained
  under `dest/.crosscopy-resume/`; retries request only missing ranges. A file is moved to its
  final path only after size and SHA-256 verification. After complete success, POSTs `consumed`
  to the source and removes the resume state.
  Returns `{"from": {"id","name"}, "files_written": ["/abs/..."], "total_bytes": int, "op": "copy"|"move"}`.
  Errors: 404 no peer has a clipboard; 502 transfer interruption (partial progress retained).
- `POST /api/clipboard/clear` → `{"cleared": true}`
- `POST /api/name` body `{"name": "new-device-name"}` → updates device name in config, returns
  device info. (mDNS TXT name refreshes on next daemon restart.)
- `POST /api/upload` multipart form, field `files` (repeatable) → saves into
  `~/.crosscopy/staging/<clipboard_id>/`, sets clipboard (op=copy), returns manifest.
- `GET /api/resumes` → incomplete transfers with source, destination, byte/file progress,
  and live `available`/`unavailable_reason`. Availability requires the source to confirm the
  exact active clipboard/offer manifest and current local file metadata. Checks run
  concurrently with a bounded response deadline and do not rehash large files during menu
  refreshes. This endpoint is loopback-only.
- `POST /api/resumes/<id>/resume` → revalidates availability, then resumes missing ranges;
  the source rereads and verifies every full checksum with a long-running timeout before the
  transfer continues. Returns 409 when the source is offline, changed, or no longer sharing
  the transfer. Loopback-only.
- `POST /api/resumes/<id>/remove` → removes partial bytes and recovery state while preserving
  files that had already completed checksum verification; 409 while the transfer is actively
  writing. Unsafe symlinked state is unregistered without following the link. Loopback-only.
- `GET /` and static assets → web UI from `crosscopy/webui/`.

## CLI commands (`ccp`)

- `ccp copy <path...>` — put files/dirs on the network clipboard (op=copy). Auto-starts daemon.
- `ccp move <path...>` — same with op=move (source files deleted after successful paste).
- `ccp paste [dir]` — paste newest peer clipboard into dir (default: CWD). `--from <name|id>`.
- `ccp transfers [--resume ID|--remove ID]` — show incomplete progress, resume only while the
  exact source share remains available, or discard unusable partial files.
- `ccp devices` (alias `ccp list`) — table: name, ip, platform, clipboard summary ("3 files, 2.1 MB" or "-").
- `ccp status` — daemon status + current local clipboard.
- `ccp clear` — clear local clipboard.
- `ccp context install|uninstall` — add/remove native file-manager sharing actions.
- `ccp context share-all <path...>` — put a file-manager selection on the network clipboard.
- `ccp context share-to [--to peer] <path...>` — choose a peer and create a targeted offer.
- `ccp add <host> [port]` — add manual peer (when mDNS is blocked).
- `ccp name <newname>` — set device name.
- `ccp daemon run|start|stop|status` — `run` foreground, `start` background (detached
  subprocess of `[executable, "-m", "crosscopy.daemon"]`, stdout/err → `daemon.log`).
  On Windows, `executable` is a venv-local `Cross Copy.exe` with Cross Copy
  `VERSIONINFO`; every daemon start and self-restart uses it so Windows Defender
  Firewall identifies the network listener as Cross Copy rather than Python.
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
