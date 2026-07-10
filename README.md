# Cross Copy

Cross Copy is a network clipboard for Windows, macOS, and Linux. It lets you
copy files, folders, or text on one computer and paste them on another.

Transfers happen directly over your local network. There is no cloud service,
account, or remote storage involved.

## Before you install

You need:

- Two or more computers on the same local network
- Windows 10 or 11, a supported macOS release, or a modern desktop Linux
  distribution
- Python 3.9 or newer

Cross Copy currently trusts the local network. It does not yet provide pairing,
authentication, or encryption, so use it only on a network you trust. See
[Security](#security) for details.

## Install Cross Copy

Install Cross Copy on every computer that you want to connect. The installer
starts the background service automatically. In a graphical desktop session,
it also enables the tray or menu-bar widget.

### Windows

Administrator access is not required.

1. Open PowerShell.
2. Run the installer:

   ```powershell
   irm https://raw.githubusercontent.com/UNILOOP/cross-copy/main/install.ps1 | iex
   ```

3. If Windows Defender Firewall asks for permission, allow **Cross Copy** on
   Private networks. The prompt identifies the app as Cross Copy, not Python.

The installer creates a dedicated environment in
`%LOCALAPPDATA%\CrossCopy`, adds `ccp` to your user PATH, and configures the
daemon and notification-area widget to start when you sign in.

The installer updates both your persistent user PATH and the current
PowerShell session, so `ccp` is available immediately.

To install from a cloned repository instead:

```powershell
git clone https://github.com/UNILOOP/cross-copy.git
cd cross-copy
.\install.ps1
```

Use `.\install.ps1 -NoService` if you do not want Cross Copy to start at
sign-in.

### macOS and Linux

Run:

```sh
curl -fsSL https://raw.githubusercontent.com/UNILOOP/cross-copy/main/install.sh | bash
```

To install from a cloned repository instead:

```sh
git clone https://github.com/UNILOOP/cross-copy.git
cd cross-copy
./install.sh
```

The installer uses `pipx` when it is available. Otherwise, it creates a
self-contained virtual environment and links `ccp` into `~/.local/bin`.

It adds `~/.local/bin` to the startup file for your default shell. Interactive
installs then reload that shell in the same terminal, so `ccp` is available as
soon as installation finishes. Bash, zsh, fish, csh, tcsh, and
POSIX-compatible shells are supported. The PATH update is safe to run more
than once.

The background daemon starts at login through launchd on macOS or a systemd
user service on Linux. Use `./install.sh --no-service` to skip this step. You
can enable it later with `ccp daemon install`.

To repair only the shell PATH configuration without reinstalling the package,
run `./install.sh --path-only` from a cloned repository.

### Confirm the installation

On each computer, run:

```sh
ccp status
ccp devices
```

`ccp status` confirms that the local daemon is running. After Cross Copy is
installed on another computer, `ccp devices` should list it. Device discovery
can take a few seconds.

## Make your first transfer

### Copy a file

On the computer that has the file:

```sh
ccp copy notes.pdf
```

On another computer:

```sh
ccp paste
```

Files are saved in the current directory unless you provide another one:

```sh
ccp paste received-files
```

You can copy several files or an entire folder in one command:

```sh
ccp copy photos/ report.docx notes.txt
```

The clipboard remains available for repeated pastes until you replace or clear
it.

### Copy text

Use `--text` when you want to be explicit:

```sh
ccp copy --text "meeting at 5"
```

On another computer:

```sh
ccp paste
```

Text is written to standard output, so it works naturally with pipes and file
redirection:

```sh
cat log.txt | ccp copy
ccp paste > received-log.txt
```

If none of the arguments passed to `ccp copy` is an existing path, Cross Copy
treats the arguments as text. Using `--text` avoids ambiguity.

### Move files instead of copying them

`ccp move` works like `ccp copy`, but removes the source files after another
computer completes a successful paste:

```sh
ccp move archive.zip
```

Source files are not removed if the transfer fails.

## Send to a specific computer

The regular clipboard is available to any connected computer. When you want
one particular computer to receive something, use `ccp send`.

On the sending computer:

```sh
ccp send report.pdf --to office-pc
```

On the receiving computer:

```sh
ccp offers
ccp accept
```

Use `ccp decline` to reject the offer. If several offers are waiting, pass the
offer ID shown by `ccp offers`:

```sh
ccp accept 3f9c1a2b ~/Documents
ccp decline 712bc890
```

Important behavior:

- `--to` accepts a device name or ID. It can be omitted when exactly one other
  device is available.
- Text can be sent with `ccp send --text "message" --to office-pc`.
- Offers expire after five minutes.
- Accepted files go to `~/Downloads/cross-copy` by default.
- Accepted text is printed to standard output.

## Use the graphical interface

The command-line interface and graphical tools use the same daemon and network
clipboard. You can use either at any time.

### Web interface

Run:

```sh
ccp ui
```

This opens `http://localhost:7373` in your browser. From there you can:

- See connected computers and their shared clipboard contents
- Share files by dragging them into the page
- Share text
- Save files from another computer
- Copy received text to the browser clipboard
- Stop sharing the current clipboard

The page updates automatically when devices, clipboards, or offers change.

### Tray or menu-bar widget

The platform installer enables the widget automatically in graphical desktop
sessions. To manage it yourself, use:

```sh
ccp widget install
ccp widget uninstall
ccp widget
```

`ccp widget install` starts the widget now and at login. `ccp widget` runs it
in the foreground, which is also useful when diagnosing startup problems.

The widget appears in the Windows notification area, the macOS menu bar, or
the Linux system tray. It can send files or text, show pending offers, and
accept or decline incoming transfers. Cross Copy remains a background
application and does not appear in the macOS Dock.

On Windows, the official Python installer includes Tcl/Tk, which Cross Copy
uses for file selection, clipboard access, and interactive offer cards.

On Ubuntu and other GNOME desktops, tray support uses AppIndicator. If the
icon does not appear, make sure the Ubuntu AppIndicators extension is enabled
and `gir1.2-ayatanaappindicator3-0.1` is installed.

For a manual Python installation, install the widget dependencies with:

```sh
pip install "cross-copy[widget]"
```

For a manual `pipx` installation:

```sh
pipx install cross-copy
pipx inject cross-copy pystray Pillow
```

## Command reference

### Clipboard commands

| Command | Purpose |
|---|---|
| `ccp copy <paths...>` | Share files or folders on the network clipboard |
| `ccp copy --text <text>` | Share text on the network clipboard |
| `ccp move <paths...>` | Share files and remove them after a successful paste |
| `ccp paste [directory]` | Paste the newest clipboard from another computer |
| `ccp paste --from <name-or-id>` | Paste from a specific computer |
| `ccp clear` | Clear this computer's shared clipboard |

### Direct-send commands

| Command | Purpose |
|---|---|
| `ccp send <paths...> --to <device>` | Offer files to one computer |
| `ccp send --text <text> --to <device>` | Offer text to one computer |
| `ccp offers` | List incoming offers |
| `ccp accept [id] [directory]` | Accept an offer |
| `ccp decline [id]` | Decline an offer |

### Device and application commands

| Command | Purpose |
|---|---|
| `ccp devices` | List connected computers and their clipboard contents |
| `ccp add <host> [port]` | Add a computer manually by IP address |
| `ccp name <new-name>` | Change how this computer appears to others |
| `ccp status` | Show daemon, update, and local clipboard status |
| `ccp ui` | Open the web interface |
| `ccp widget` | Run the tray or menu-bar widget in the foreground |
| `ccp widget install` | Start the widget now and at login |
| `ccp widget uninstall` | Stop the widget and remove it from login startup |
| `ccp daemon start` | Start the background daemon |
| `ccp daemon stop` | Stop the background daemon |
| `ccp daemon status` | Show daemon status |
| `ccp daemon install` | Start the daemon now and at login |
| `ccp daemon uninstall` | Stop the daemon and remove it from login startup |
| `ccp update` | Install the latest version and restart Cross Copy |
| `ccp update --check` | Check for a newer version without installing it |
| `ccp version` | Print the installed version |

`ccp list` is an alias for `ccp devices`.

## Configuration

Cross Copy creates its configuration file on first use:

- Windows: `%USERPROFILE%\.crosscopy\config.json`
- macOS and Linux: `~/.crosscopy/config.json`

Restart the daemon after editing the file:

```sh
ccp daemon stop
ccp daemon start
```

| Setting | Default | Purpose |
|---|---|---|
| `device_name` | Computer hostname | Name shown to other computers |
| `receive_dir` | `~/Downloads/cross-copy` | Destination for accepted file offers |
| `notifications` | `true` | Show notifications for offers and completed transfers |
| `auto_update` | `true` | Install new Cross Copy versions automatically |

You can change the device name without editing the file:

```sh
ccp name office-pc
```

The `CROSSCOPY_HOME` environment variable changes the data directory, and
`CROSSCOPY_PORT` changes the default TCP port from `7373`.

## Updates

Automatic updates are enabled by default. The daemon checks shortly after it
starts and then every six hours. When an update is installed, the daemon and
running widget restart with the new version.

To update manually:

```sh
ccp update
```

To check without installing:

```sh
ccp update --check
```

Set `"auto_update": false` in the configuration file to disable automatic
installation. Update availability will still appear in `ccp status` and the
web interface.

## How Cross Copy works

Each computer runs a small background daemon. The daemons advertise themselves
over mDNS as `_crosscopy._tcp` and communicate directly over HTTP on the local
network.

Cross Copy also sends a small UDP broadcast beacon as a fallback when a router
or Windows network adapter handles multicast unreliably. The beacon contains
device metadata only; clipboard contents and files are still transferred
directly over HTTP after a peer is found.

When you run `ccp copy`, Cross Copy records a clipboard manifest. Files are not
transferred until another computer runs `ccp paste`. The receiving computer
then downloads the files directly from the source.

`ccp send` sends only an offer first. The receiving computer pulls the content
from the sender after the offer is accepted.

For `ccp move`, the source files are deleted only after the receiving computer
confirms that the complete transfer succeeded.

## Troubleshooting

### A computer does not appear in `ccp devices`

Some corporate networks, VLANs, and routers block mDNS. Add the computer by IP
address:

```sh
ccp add 192.168.1.5
```

If one computer can discover the other, Cross Copy normally restores
two-direction visibility within about a minute.

On Windows, Cross Copy automatically rescans network adapters when Wi-Fi,
Ethernet, VPN, or virtual-network addresses change. If discovery remains
intermittent, confirm that Windows Defender Firewall allows Cross Copy on
Private networks and that UDP port `7374` is not blocked.

### Computers appear, but transfers fail

A firewall is probably blocking TCP port `7373`. Allow that port, or the port
set through `CROSSCOPY_PORT`, on both computers.

On Windows, confirm that the network is marked Private and that Windows
Defender Firewall allows Python or Cross Copy on Private networks.

### The Windows notification-area icon is missing

Run the widget in PowerShell to see its startup error:

```powershell
ccp widget
```

If Python was installed from a custom distribution, make sure it includes
Tcl/Tk. After correcting the Python installation, run `ccp widget install`
again.

### The Linux tray icon is missing

Run `ccp widget` in a terminal. On GNOME, verify that AppIndicator support is
installed and enabled.

### Test two instances on one computer

Use separate data directories and ports:

```sh
# Terminal 1
CROSSCOPY_HOME=/tmp/cross-copy-a CROSSCOPY_PORT=7373 CROSSCOPY_NO_MDNS=1 ccp daemon run

# Terminal 2
CROSSCOPY_HOME=/tmp/cross-copy-b CROSSCOPY_PORT=7474 CROSSCOPY_NO_MDNS=1 ccp daemon run

# Terminal 3
CROSSCOPY_HOME=/tmp/cross-copy-b CROSSCOPY_PORT=7474 ccp add 127.0.0.1 7373
```

## Uninstall

### Windows

For an installation made with the one-line command:

```powershell
irm https://raw.githubusercontent.com/UNILOOP/cross-copy/main/uninstall.ps1 | iex
```

From a cloned repository:

```powershell
.\uninstall.ps1
```

Add `-RemoveData` to remove the device configuration, clipboard state, and
logs as well:

```powershell
.\uninstall.ps1 -RemoveData
```

### macOS and Linux

From the cloned repository, run:

```sh
./uninstall.sh
```

The script asks whether it should also remove your Cross Copy data.

## Security

Cross Copy version 0.5.2 uses a trusted-LAN model. It does not currently
authenticate devices or encrypt transfers. Any device that can reach the
Cross Copy daemon on your network may be able to read the shared clipboard or
send offers.

Use Cross Copy on home, small-office, or other trusted private networks. Do
not use it on public or untrusted Wi-Fi. Pairing and encrypted transport are
planned for a future release.

## License

Cross Copy is available under the [MIT License](LICENSE).

Cross Copy is an open source project by [UNILOOP LLC](https://uniloop.com).
