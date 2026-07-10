# cross-copy

**A network clipboard for files *and text*, between your Windows, Mac, and Linux machines.**

`ccp copy` a file — or a snippet of text — on one machine, `ccp paste` on
another — that's it. cross-copy discovers your machines automatically over mDNS
and transfers everything directly between them over your LAN. No cloud, no
account, no config: your data never leaves your network. Want to hand a file
to one machine in particular? `ccp send` offers it AirDrop-style — nothing
transfers until the other side accepts.

## 20-second demo

```console
# On your Windows PC, Mac, or Linux box
$ ccp copy notes.pdf
Copied 1 file (2.4 MB) to the network clipboard.

# On another computer
$ ccp paste
Pasted notes.pdf (2.4 MB) from sayeed-macbook.
```

Text works exactly the same way:

```console
# On your Mac
$ ccp copy "meeting at 5"
Copied text (12 chars) to the network clipboard.

# On your Linux box
$ ccp paste
meeting at 5
```

`ccp paste` prints text to stdout, so it pipes — and stdin works too:

```sh
cat log.txt | ccp copy      # copy a command's output as text
ccp paste > out.txt         # paste it into a file on the other machine
```

Works with multiple files and whole directories too: `ccp copy photos/ report.docx`.

## Send to a specific device (AirDrop-style) 📨

The clipboard is a *pull* model — whoever pastes first gets it. `ccp send` is
a *push*: you pick the target machine, and nothing transfers until someone
there says yes.

```console
# On your Mac
$ ccp send report.pdf --to linux-box
📨 Offered 1 file (2.4 MB) to linux-box — waiting for them to accept...
✅ linux-box accepted — 1 file delivered
```

```console
# Meanwhile on linux-box, a desktop notification pops up:
#   📥 sayeed-macbook wants to send 1 file (2.4 MB)
$ ccp offers
ID        FROM            CONTENTS        AGE
3f9c1a2b  sayeed-macbook  1 file, 2.4 MB  4s

Accept with: ccp accept [id] [dir]   ·   Decline with: ccp decline [id]

$ ccp accept
📥 Accepted 1 file (2.4 MB) from sayeed-macbook
   /home/you/Downloads/cross-copy/report.pdf
```

Good to know:

- `--to` takes a device name or id, and is **optional when there's exactly
  one other device** on the network.
- Text works too — `ccp send "the wifi password" --to macbook` — with the
  same path-vs-text rules as `ccp copy` (`-t`/`--text` forces text, and piped
  stdin works: `ccp send --to macbook < notes.txt`). Accepted text prints to
  stdout, so it pipes.
- Offers **expire after 5 minutes** if nobody answers (Ctrl-C while waiting
  doesn't cancel the offer — it stays acceptable until it expires). The
  sender is told whether you accepted or declined.
- Accepted files land in `~/Downloads/cross-copy` by default — change that
  with the `receive_dir` config key, or per-accept: `ccp accept [id] ~/dir`.

## Install

### Windows

Open **PowerShell** (no administrator access required) and run:

```powershell
irm https://raw.githubusercontent.com/UNILOOP/cross-copy/main/install.ps1 | iex
```

Or install from a cloned checkout:

```powershell
git clone https://github.com/UNILOOP/cross-copy.git
cd cross-copy
.\install.ps1
```

The Windows installer creates a dedicated environment under
`%LOCALAPPDATA%\CrossCopy`, adds `ccp` to your user PATH, and starts both the
daemon and notification-area widget now and at login. Open a new PowerShell
window after installation. If Windows Defender Firewall asks, allow Cross Copy
on **Private networks** so your other LAN devices can connect.

Use `.\install.ps1 -NoService` from a checkout to skip login autostart. To
uninstall a one-line installation, run:

```powershell
irm https://raw.githubusercontent.com/UNILOOP/cross-copy/main/uninstall.ps1 | iex
```

From a checkout, use `.\uninstall.ps1`; add `-RemoveData` to also remove
device configuration and logs.

### macOS and Linux

One-liner:

```sh
curl -fsSL https://raw.githubusercontent.com/UNILOOP/cross-copy/main/install.sh | bash
```

Or from source:

```sh
git clone https://github.com/UNILOOP/cross-copy.git
cd cross-copy
./install.sh
```

The macOS/Linux installer prefers [pipx](https://pipx.pypa.io) if you have it, otherwise it
creates a self-contained venv and links `ccp` into `~/.local/bin`. It also
enables daemon autostart at login **by default** (systemd user unit on Linux,
launchd agent on macOS) — pass `--no-service` to skip that, and enable it later
with `ccp daemon install`. To remove everything: `./uninstall.sh`.

### Requirements

- Two or more machines on the **same LAN** (Windows, macOS, and/or Linux)
- Windows 10/11, a supported macOS release, or a modern desktop Linux distribution
- Python **3.9+**
- That's it — the daemon auto-starts on first use

## Commands

| Command | What it does |
|---|---|
| `ccp copy <path...\|text>` | Put files/dirs on the network clipboard (stays valid for repeated pastes). If no argument is an existing path, the arguments are copied as **text**; `--text`/`-t` forces text. `echo foo \| ccp copy` copies stdin |
| `ccp move <path...>` | Like copy, but source files are deleted after a successful paste (a text move clears the source clipboard) |
| `ccp paste [dir]` | Paste the newest peer clipboard into `dir` (default: current dir); a **text** clipboard is printed verbatim to stdout instead (pipeable: `ccp paste > out.txt`). `--from <name\|id>` to pick a machine |
| `ccp send <path...\|text> --to <name\|id>` | Offer files or text to **one specific device** and wait for them to accept (AirDrop-style). Same path-vs-text rules as `ccp copy`; `--to` is optional when there's exactly one other device |
| `ccp offers` | List pending incoming offers (id, sender, contents, age) |
| `ccp accept [id] [dir]` | Accept an offer — newest one if no id (a short id prefix is enough). Text prints to stdout; files go to `dir`, or your `receive_dir` if omitted |
| `ccp decline [id]` | Decline an offer (newest if no id) — the sender is notified |
| `ccp devices` | List machines on the LAN with their clipboard contents (alias: `ccp list`) |
| `ccp status` | Show daemon status and your current local clipboard |
| `ccp clear` | Clear your local clipboard |
| `ccp add <host> [port]` | Manually add a peer by IP (when mDNS doesn't work) |
| `ccp name <newname>` | Rename this device (defaults to hostname) |
| `ccp daemon run\|start\|stop\|status` | Manage the background daemon directly |
| `ccp daemon install` | Set up start-at-login (Windows user login entry / systemd user unit / launchd agent) and start the daemon now. The platform installer runs this by default |
| `ccp daemon uninstall` | Stop, disable, and remove the start-at-login service |
| `ccp ui` | Open the web UI in your browser |
| `ccp widget` | Run the menu-bar / system-tray companion in the foreground (see below) |
| `ccp widget install\|uninstall` | Start the tray widget now **and at every login** / remove that. `install.sh` runs `install` by default in graphical sessions |
| `ccp update` | Update cross-copy to the latest version (and restart the daemon) |
| `ccp update --check` | Only check whether a newer version is available |
| `ccp version` | Print the version |

## Updates

cross-copy keeps itself up to date **by default**: the daemon checks for a new
version shortly after it starts and every 6 hours, installs it, and restarts
itself. You don't have to do anything.

- **Turn auto-update off** by setting `"auto_update": false` in
  `~/.crosscopy/config.json`. You'll still see an update notice in `ccp status`
  and the web UI when a new version is out.
- **Update manually** any time with `ccp update` (or just see what's out there
  with `ccp update --check`).

## Configuration

Everything lives in `~/.crosscopy/config.json` (created on first run; edit
and restart the daemon to apply):

| Key | Default | What it does |
|---|---|---|
| `device_name` | your hostname | How this machine appears to others — or just run `ccp name <newname>` |
| `receive_dir` | `~/Downloads/cross-copy` | Where files from **accepted offers** (`ccp send` → `ccp accept`) are saved; `~` is expanded. Override per-accept with `ccp accept [id] <dir>` |
| `notifications` | `true` | Desktop notifications (Windows banners, macOS Notification Center, `notify-send`/D-Bus on Linux) for incoming offers, declines, and finished transfers. Set to `false` to silence them |
| `auto_update` | `true` | Let the daemon update itself automatically (see [Updates](#updates)) |

## Web UI 🖱️

```sh
ccp ui
```

Opens `http://localhost:7373` — see every device on your network and what each
one is sharing, live: the page updates the moment something changes, no
refreshing needed. **Drag & drop** files into the browser to share them from
this device without touching the terminal, or use **"Share text"** to share a
snippet. On the other machine, hit **"Save to this device"** to receive files
(pick the folder with "Save into folder") or **"Get text"** to grab shared text
— it lands right there and goes onto the browser clipboard as well. Done
sharing? Hit **"Stop sharing"**.

### Tray widget

```sh
ccp widget install   # start now + at every login (install.sh does this by default)
ccp widget           # or run it in the foreground once
```

Puts cross-copy in your **notification area (Windows) / menu bar (macOS) /
system tray (Linux)**: send
files or your clipboard text to any device in a couple of clicks, and accept
or decline incoming offers without opening a terminal. **"Open panel"** pops
a compact panel with the same controls, live-updating — a **native floating
window** on macOS (WKWebView), and a compact Edge/Chrome app window on Windows
and Linux. Remove the autostart with `ccp widget uninstall`.

On Windows, Python's standard Tcl/Tk option supplies the file picker,
clipboard integration, and interactive notification cards. It is enabled by
default in the official Python installer. Incoming offers have real
**Accept/Decline** buttons; accepted text is placed on the Windows clipboard,
and files are saved to `Downloads\cross-copy` by default.

> **Ubuntu/GNOME note:** tray icons render through AppIndicator — the
> installer builds its venv with `--system-site-packages` so the widget can
> use the system's PyGObject. If the icon doesn't appear, check that the
> "Ubuntu AppIndicators" GNOME extension is enabled and
> `gir1.2-ayatanaappindicator3-0.1` is installed.

The widget needs two optional dependencies (`pystray` and `Pillow`) —
`install.sh` includes them by default; for manual installs they're the
`widget` extra:

```sh
pip install "cross-copy[widget]"
# pipx users:
pipx install cross-copy && pipx inject cross-copy pystray Pillow
```

Desktop notifications don't need the extra: on Windows, macOS, and Linux the daemon
fires them natively whenever an offer arrives, is declined, or a transfer
finishes.

## How it works

- Each machine runs a small daemon (started at login by the installer, and
  auto-started on first use otherwise) that announces itself via mDNS as
  `_crosscopy._tcp`, so machines find each other with zero config.
- `ccp copy` just records a manifest — nothing is transferred yet. `ccp paste`
  finds the newest clipboard on the network and downloads the files (or fetches
  the text) **directly from the source machine over HTTP**. Nothing ever leaves
  your LAN.
- `ccp move` is safe: the source files are only deleted after the receiving
  machine confirms a fully successful paste.
- `ccp send` works the other way around: the sender posts an *offer* (just
  metadata) to the target, and accepting it pulls the files straight from the
  sender — nothing is transferred until then, and unanswered offers expire
  after 5 minutes.

## Troubleshooting

- **A device doesn't show up in `ccp devices`** — some networks (corporate
  Wi-Fi, VLANs, some routers) block mDNS. Add the peer manually by IP:
  `ccp add 192.168.1.5`.
- **One-way visibility** (machine A sees B, but B doesn't see A) — as of v0.3
  this self-heals: machines that can reach each other introduce themselves
  directly, so as long as *either* machine can reach the other, both show up
  within about a minute. If neither direction works, fall back to
  `ccp add <ip>` on one side.
- **Devices see each other but transfers fail** — a firewall is likely blocking
  the daemon port. Open **TCP 7373** (or your `CROSSCOPY_PORT`) on both
  machines. On Windows, make sure the network is marked **Private** and allow
  Python/Cross Copy through Windows Defender Firewall on Private networks.
- **The Windows tray icon is missing** — run `ccp widget` in PowerShell to see
  the startup error. The official Python installer includes Tcl/Tk; if you used
  a custom Python distribution, reinstall with Tcl/Tk enabled and run
  `ccp widget install` again.
- **Want to try it on a single machine?** Run two instances side by side:

  ```sh
  # Terminal 1
  CROSSCOPY_HOME=/tmp/a CROSSCOPY_PORT=7373 CROSSCOPY_NO_MDNS=1 ccp daemon run
  # Terminal 2
  CROSSCOPY_HOME=/tmp/b CROSSCOPY_PORT=7474 CROSSCOPY_NO_MDNS=1 ccp daemon run
  # Terminal 3 — wire them together and use them
  CROSSCOPY_HOME=/tmp/b CROSSCOPY_PORT=7474 ccp add 127.0.0.1 7373
  ```

## ⚠️ Security

**v0.5.0 trusts your LAN.** There is no authentication or encryption yet: any
device on your network can read your cross-copy clipboard (files *and* text)
and offer you files.
Use it on networks you trust (home, small office) — **not** on public or
untrusted Wi-Fi. Pairing codes and TLS are on the roadmap.

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">An open source initiative by <a href="https://uniloop.com">UNILOOP LLC</a></p>
