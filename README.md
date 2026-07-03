# cross-copy

**A network clipboard for files *and text*, between your Mac and Linux machines.**

`ccp copy` a file — or a snippet of text — on one machine, `ccp paste` on
another — that's it. cross-copy discovers your machines automatically over mDNS
and transfers everything directly between them over your LAN. No cloud, no
account, no config: your data never leaves your network.

## 20-second demo

```console
# On your Mac
$ ccp copy notes.pdf
Copied 1 file (2.4 MB) to the network clipboard.

# On your Linux box
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

## Install

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

The installer prefers [pipx](https://pipx.pypa.io) if you have it, otherwise it
creates a self-contained venv and links `ccp` into `~/.local/bin`. It also
enables daemon autostart at login **by default** (systemd user unit on Linux,
launchd agent on macOS) — pass `--no-service` to skip that, and enable it later
with `ccp daemon install`. To remove everything: `./uninstall.sh`.

### Requirements

- Two or more machines on the **same LAN** (macOS and/or Linux)
- Python **3.9+**
- That's it — the daemon auto-starts on first use

## Commands

| Command | What it does |
|---|---|
| `ccp copy <path...\|text>` | Put files/dirs on the network clipboard (stays valid for repeated pastes). If no argument is an existing path, the arguments are copied as **text**; `--text`/`-t` forces text. `echo foo \| ccp copy` copies stdin |
| `ccp move <path...>` | Like copy, but source files are deleted after a successful paste (a text move clears the source clipboard) |
| `ccp paste [dir]` | Paste the newest peer clipboard into `dir` (default: current dir); a **text** clipboard is printed verbatim to stdout instead (pipeable: `ccp paste > out.txt`). `--from <name\|id>` to pick a machine |
| `ccp devices` | List machines on the LAN with their clipboard contents (alias: `ccp list`) |
| `ccp status` | Show daemon status and your current local clipboard |
| `ccp clear` | Clear your local clipboard |
| `ccp add <host> [port]` | Manually add a peer by IP (when mDNS doesn't work) |
| `ccp name <newname>` | Rename this device (defaults to hostname) |
| `ccp daemon run\|start\|stop\|status` | Manage the background daemon directly |
| `ccp daemon install` | Set up start-at-login (systemd user unit / launchd agent) and start the daemon now. `install.sh` runs this by default (`--no-service` to opt out) |
| `ccp daemon uninstall` | Stop, disable, and remove the start-at-login service |
| `ccp ui` | Open the web UI in your browser |
| `ccp version` | Print the version |

## Web UI 🖱️

```sh
ccp ui
```

Opens `http://localhost:7373` — see every device on your network and what's on
its clipboard, and **drag & drop** files into the browser to put them on the
clipboard without touching the terminal. There's a **text box** too: type or
paste text on one machine, then hit "Paste text here" on the other — the text
lands right there and is put on the browser clipboard as well.

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

## Troubleshooting

- **A device doesn't show up in `ccp devices`** — some networks (corporate
  Wi-Fi, VLANs, some routers) block mDNS. Add the peer manually by IP:
  `ccp add 192.168.1.5`.
- **Devices see each other but transfers fail** — a firewall is likely blocking
  the daemon port. Open **TCP 7373** (or your `CROSSCOPY_PORT`) on both
  machines.
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

**v0.2.0 trusts your LAN.** There is no authentication or encryption yet: any
device on your network can read your cross-copy clipboard (files *and* text)
and send you files.
Use it on networks you trust (home, small office) — **not** on public or
untrusted Wi-Fi. Pairing codes and TLS are on the roadmap.

## License

MIT — see [LICENSE](LICENSE).

---

<p align="center">An open source initiative by <a href="https://uniloop.com">UNILOOP LLC</a></p>
