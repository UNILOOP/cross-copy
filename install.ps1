[CmdletBinding()]
param(
    [switch]$NoService,
    [switch]$Service
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Info([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Blue
}

function Write-Warn([string]$Message) {
    Write-Warning $Message
}

function Publish-EnvironmentChange {
    try {
        if (-not ("CrossCopy.NativeMethods" -as [type])) {
            Add-Type -Namespace CrossCopy -Name NativeMethods -MemberDefinition @"
                [System.Runtime.InteropServices.DllImport("user32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
                public static extern System.IntPtr SendMessageTimeout(
                    System.IntPtr hWnd, uint Msg, System.UIntPtr wParam,
                    string lParam, uint flags, uint timeout,
                    out System.UIntPtr result);
"@
        }
        $result = [UIntPtr]::Zero
        [void][CrossCopy.NativeMethods]::SendMessageTimeout(
            [IntPtr]0xffff, 0x1a, [UIntPtr]::Zero, "Environment",
            0x2, 5000, [ref]$result)
    } catch {
        Write-Warn "The PATH was saved, but Windows could not notify other applications about the change."
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "install.ps1 is for Windows. Use ./install.sh on macOS or Linux."
}

$InstallRoot = Join-Path $env:LOCALAPPDATA "CrossCopy"
$VenvDir = Join-Path $InstallRoot "venv"
$BinDir = Join-Path $InstallRoot "bin"
$CcpExe = Join-Path $VenvDir "Scripts\ccp.exe"
$CcpCmd = Join-Path $BinDir "ccp.cmd"
$TempRoot = $null

# Resolve an actual Python executable. The py launcher itself cannot be used
# as a venv's recorded base executable, so ask it for sys.executable first.
$Python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $resolved = & py -3 -c "import sys; print(sys.executable)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $resolved) {
        $candidate = ($resolved | Select-Object -First 1).Trim()
        & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Python = $candidate }
    }
}
if (-not $Python) {
    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        & $command.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $Python = $command.Source
            break
        }
    }
}
if (-not $Python) {
    throw "Python 3.9+ was not found. Install Python from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH'."
}
$PythonVersion = & $Python -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
Write-Info "Using Python: $Python ($PythonVersion)"

# A running Windows executable locks its venv. Stop an existing Cross Copy
# install before replacing it, while its old CLI still exists.
if (Test-Path $CcpExe) {
    Write-Info "Stopping the existing Cross Copy installation ..."
    & $CcpExe widget uninstall *> $null
    & $CcpExe daemon uninstall *> $null
    & $CcpExe daemon stop *> $null
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
}

# Use this checkout when invoked from source. For the one-line installer,
# download and unpack the repository archive without requiring Git.
$Source = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot "pyproject.toml"))) {
    $Source = $PSScriptRoot
    Write-Info "Installing from local checkout: $Source"
} else {
    $TempRoot = Join-Path ([IO.Path]::GetTempPath()) ("cross-copy-" + [guid]::NewGuid())
    $Archive = Join-Path $TempRoot "cross-copy.zip"
    $Extracted = Join-Path $TempRoot "source"
    New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null
    $RepoArchive = if ($env:CROSSCOPY_REPO_ARCHIVE) {
        $env:CROSSCOPY_REPO_ARCHIVE
    } else {
        "https://github.com/UNILOOP/cross-copy/archive/refs/heads/main.zip"
    }
    Write-Info "Downloading $RepoArchive ..."
    Invoke-WebRequest -UseBasicParsing -Uri $RepoArchive -OutFile $Archive
    Expand-Archive -Path $Archive -DestinationPath $Extracted -Force
    $Source = Get-ChildItem $Extracted -Directory -Recurse |
        Where-Object { Test-Path (Join-Path $_.FullName "pyproject.toml") } |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $Source) { throw "The downloaded archive did not contain pyproject.toml." }
}

try {
    if (Test-Path $VenvDir) {
        Remove-Item $VenvDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Write-Info "Creating the dedicated environment at $VenvDir ..."
    & $Python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "Python could not create $VenvDir." }
    $Pythonw = Join-Path $VenvDir "Scripts\pythonw.exe"
    if (-not (Test-Path $Pythonw)) {
        throw "pythonw.exe is missing. Reinstall Python with the Tcl/Tk option enabled."
    }
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $VenvPython -c "import tkinter; print(tkinter.TkVersion)"
    if ($LASTEXITCODE -ne 0) {
        throw "Python's Tcl/Tk support is unavailable. Reinstall Python from python.org with the 'tcl/tk and IDLE' feature enabled."
    }
    $Pip = Join-Path $VenvDir "Scripts\pip.exe"
    & $Pip install --quiet --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not upgrade pip in $VenvDir." }
    & $Pip install --quiet "${Source}[widget]"
    if ($LASTEXITCODE -ne 0) { throw "Could not install Cross Copy with widget support." }
    if (-not (Test-Path $CcpExe)) {
        throw "Installation finished but $CcpExe is missing."
    }

    New-Item -ItemType Directory -Path $BinDir -Force | Out-Null
    $Wrapper = "@echo off`r`n`"$CcpExe`" %*`r`n"
    [IO.File]::WriteAllText($CcpCmd, $Wrapper, [Text.UTF8Encoding]::new($false))

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $PathEntries = @($UserPath -split ";" | Where-Object { $_ })
    $PathChanged = $false
    if (-not ($PathEntries | Where-Object { $_.TrimEnd("\") -ieq $BinDir.TrimEnd("\") })) {
        $NewPath = if ($UserPath) { "$BinDir;$UserPath" } else { $BinDir }
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        $PathChanged = $true
        Write-Info "Added $BinDir to your user PATH."
    }
    $PersistedPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (-not ($PersistedPath -split ";" | Where-Object { $_ -and $_.TrimEnd("\") -ieq $BinDir.TrimEnd("\") })) {
        throw "Cross Copy was installed, but $BinDir could not be saved to your user PATH."
    }
    if (-not ($env:Path -split ";" | Where-Object { $_ -and $_.TrimEnd("\") -ieq $BinDir.TrimEnd("\") })) {
        $env:Path = "$BinDir;$env:Path"
    }
    if ($PathChanged) { Publish-EnvironmentChange }

    Write-Info "Verifying install ..."
    & $CcpExe version
    if ($LASTEXITCODE -ne 0) { throw "The installed ccp command failed verification." }

    Write-Info "Installing Explorer context-menu actions ..."
    & $CcpExe context install
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Explorer actions could not be installed. Retry with: ccp context install"
    }

    if (-not $NoService) {
        Write-Info "Enabling daemon autostart ..."
        & $CcpExe daemon install
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Daemon autostart could not be enabled. Retry with: ccp daemon install"
        }
        Write-Info "Enabling the notification-area widget ..."
        & $CcpExe widget install
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "Widget autostart could not be enabled. Retry with: ccp widget install"
        }
    } else {
        Write-Info "Skipping login autostart (-NoService). Enable it later with ccp daemon install and ccp widget install."
    }
} finally {
    if ($TempRoot -and (Test-Path $TempRoot)) {
        Remove-Item $TempRoot -Recurse -Force
    }
}

Write-Host ""
Write-Info "Cross Copy installed!"
Write-Host "If Windows Defender Firewall prompts, allow Python/Cross Copy on Private networks so other devices on your LAN can connect."
Write-Host "The ccp command is available in this PowerShell session. Try: ccp devices"
