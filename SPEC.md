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
