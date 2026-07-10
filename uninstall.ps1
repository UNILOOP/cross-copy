[CmdletBinding()]
param([switch]$RemoveData)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Write-Info([string]$Message) {
    Write-Host "==> $Message" -ForegroundColor Blue
}

$InstallRoot = Join-Path $env:LOCALAPPDATA "CrossCopy"
$VenvDir = Join-Path $InstallRoot "venv"
$BinDir = Join-Path $InstallRoot "bin"
$CcpExe = Join-Path $VenvDir "Scripts\ccp.exe"
$DataDir = if ($env:CROSSCOPY_HOME) { $env:CROSSCOPY_HOME } else { Join-Path $HOME ".crosscopy" }

if (Test-Path $CcpExe) {
    Write-Info "Stopping Cross Copy and removing login autostart ..."
    & $CcpExe widget uninstall *> $null
    & $CcpExe daemon uninstall *> $null
    & $CcpExe daemon stop *> $null
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($VenvDir, [StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
} else {
    $RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    Remove-ItemProperty $RunKey -Name "Cross Copy Daemon" -ErrorAction SilentlyContinue
    Remove-ItemProperty $RunKey -Name "Cross Copy Widget" -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DataDir "Cross Copy Daemon.pyw") -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DataDir "Cross Copy Widget.pyw") -Force -ErrorAction SilentlyContinue
}

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Kept = @($UserPath -split ";" | Where-Object {
    $_ -and $_.TrimEnd("\") -ine $BinDir.TrimEnd("\")
})
[Environment]::SetEnvironmentVariable("Path", ($Kept -join ";"), "User")

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

Write-Info "Cross Copy uninstalled. Open a new terminal to refresh PATH."
