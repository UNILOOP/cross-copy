# cross-copy

**A network clipboard for files, between your Mac and Linux machines.**

`ccp copy` a file on one machine, `ccp paste` on another — that's it. cross-copy
discovers your machines automatically over mDNS and transfers files directly
between them over your LAN. No cloud, no account, no config: files never leave
your network.

## 20-second demo

```console
# On your Mac
$ ccp copy notes.pdf
Copied 1 file (2.4 MB) to the network clipboard.

# On your Linux box
$ ccp paste
Pasted notes.pdf (2.4 MB) from sayeed-macbook.
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
creates a self-contained venv and links `ccp` into `~/.local/bin`. Add
`--service` to also start the daemon at login (systemd user unit on Linux,
launchd agent on macOS). To remove everything: `./uninstall.sh`.

### Requirements

- Two or more machines on the **same LAN** (macOS and/or Linux)
- Python **3.9+**
- That's it — the daemon auto-starts on first use

## Commands

| Command | What it does |
|---|---|
| `ccp copy <path...>` | Put files/dirs on the network clipboard (stays valid for repeated pastes) |
| `ccp move <path...>` | Like copy, but source files are deleted after a successful paste |
| `ccp paste [dir]` | Paste the newest peer clipboard into `dir` (default: current dir). `--from <name\|id>` to pick a machine |
| `ccp devices` | List machines on the LAN with their clipboard contents (alias: `ccp list`) |
| `ccp status` | Show daemon status and your current local clipboard |
| `ccp clear` | Clear your local clipboard |
| `ccp add <host> [port]` | Manually add a peer by IP (when mDNS doesn't work) |
| `ccp name <newname>` | Rename this device (defaults to hostname) |
| `ccp daemon run\|start\|stop\|status` | Manage the background daemon directly |
| `ccp ui` | Open the web UI in your browser |
| `ccp version` | Print the version |

## Web UI 🖱️

```sh
ccp ui
```

Opens `http://localhost:7373` — see every device on your network and what's on
its clipboard, and **drag & drop** files into the browser to put them on the
clipboard without touching the terminal.

## How it works

- Each machine runs a small daemon (auto-started on first use) that announces
  itself via mDNS as `_crosscopy._tcp`, so machines find each other with zero
  config.
- `ccp copy` just records a manifest — nothing is transferred yet. `ccp paste`
  finds the newest clipboard on the network and downloads the files **directly
  from the source machine over HTTP**. Nothing ever leaves your LAN.
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

**v0.1.0 trusts your LAN.** There is no authentication or encryption yet: any
device on your network can read your cross-copy clipboard and send you files.
Use it on networks you trust (home, small office) — **not** on public or
untrusted Wi-Fi. Pairing codes and TLS are on the roadmap.

## License

MIT
