[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Info([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Blue
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
        Write-Warning "The PATH was updated, but Windows could not notify other applications about the change."
    }
}

$InstallRoot = Join-Path $env:LOCALAPPDATA "CrossCopy"
$VenvDir = Join-Path $InstallRoot "venv"
$BinDir = Join-Path $InstallRoot "bin"
$CcpExe = Join-Path $VenvDir "Scripts\ccp.exe"
$DataDir = if ($env:CROSSCOPY_HOME) { $env:CROSSCOPY_HOME } else { Join-Path $HOME ".crosscopy" }

if (Test-Path $CcpExe) {
    Write-Info "Stopping Cross Copy and removing login autostart ..."
    & $CcpExe context uninstall *> $null
    & $CcpExe widget uninstall *> $null
    & $CcpExe daemon uninstall *> $null
    & $CcpExe daemon stop *> $null
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
} else {
    Remove-Item "HKCU:\Software\Classes\AllFilesystemObjects\shell\CrossCopy" -Recurse -Force -ErrorAction SilentlyContinue
    $RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    Remove-ItemProperty $RunKey -Name "Cross Copy Daemon" -ErrorAction SilentlyContinue
    Remove-ItemProperty $RunKey -Name "Cross Copy Widget" -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DataDir "Cross Copy Daemon.pyw") -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DataDir "Cross Copy Widget.pyw") -Force -ErrorAction SilentlyContinue
}

# Exact fallback cleanup also handles a damaged or older CLI.
Remove-Item "HKCU:\Software\Classes\AllFilesystemObjects\shell\CrossCopy" -Recurse -Force -ErrorAction SilentlyContinue

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Kept = @($UserPath -split ";" | Where-Object {
    $_ -and $_.TrimEnd("\") -ine $BinDir.TrimEnd("\")
})
[Environment]::SetEnvironmentVariable("Path", ($Kept -join ";"), "User")
$env:Path = (@($env:Path -split ";" | Where-Object {
    $_ -and $_.TrimEnd("\") -ine $BinDir.TrimEnd("\")
}) -join ";")
Publish-EnvironmentChange

if (Test-Path $InstallRoot) {
    Write-Info "Removing $InstallRoot"
    Remove-Item $InstallRoot -Recurse -Force
} else {
    Write-Info "No installed environment found at $InstallRoot"
}

$DeleteData = $RemoveData
if ((Test-Path $DataDir) -and -not $RemoveData) {
    if ([Console]::IsInputRedirected) {
        Write-Info "Non-interactive shell: keeping $DataDir"
    } else {
        $Answer = Read-Host "Remove $DataDir (device config, clipboard, and logs)? [y/N]"
        $DeleteData = $Answer -match "^(y|yes)$"
    }
}
if ($DeleteData -and (Test-Path $DataDir)) {
    Remove-Item $DataDir -Recurse -Force
    Write-Info "Removed $DataDir"
} elseif (Test-Path $DataDir) {
    Write-Info "Keeping $DataDir"
}

Write-Info "Cross Copy uninstalled and removed from PATH."
