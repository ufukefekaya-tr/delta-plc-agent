# DVP-only local Python environment. Never opens WPLSoft or a serial port.
[CmdletBinding()]
param(
    [string]$PythonExe,
    [string]$EnvironmentPath = (Join-Path $PSScriptRoot '../.agent-state/dvp-venv'),
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envRoot = [IO.Path]::GetFullPath($EnvironmentPath)
$envPython = Join-Path $envRoot 'Scripts/python.exe'
function Test-UsablePython([string]$Candidate) {
    if (-not $Candidate -or -not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    & $Candidate -I -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] < (4,0) else 1)' 2>$null
    return $LASTEXITCODE -eq 0
}
if (Test-UsablePython $envPython) { $PythonExe = $envPython }
if (-not $PythonExe) {
    foreach ($candidate in @(Get-Command python.exe -All -ErrorAction SilentlyContinue)) {
        if ($candidate.Source -notlike '*WindowsApps*' -and (Test-UsablePython $candidate.Source)) {
            $PythonExe = $candidate.Source; break
        }
    }
}
if (-not $PythonExe -and -not $CheckOnly) {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
        throw 'PYTHON_INSTALL_UNAVAILABLE: bootstrap download is Windows x64 only.'
    }
    $recipe = (Get-Content -Raw -LiteralPath (Join-Path $repoRoot 'packaging/dvp-prerequisites.json') | ConvertFrom-Json).python
    $cacheDir = Join-Path $env:LOCALAPPDATA 'UniversalPLC/installers'
    New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
    $installer = Join-Path $cacheDir ('python-' + $recipe.bootstrap_version + '-amd64.exe')
    Invoke-WebRequest -Uri $recipe.url -OutFile $installer -TimeoutSec 120
    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation(?:,|$)') {
        throw 'INSTALLER_VERIFICATION_FAILED: Python publisher signature missing or invalid.'
    }
    $pythonRoot = Join-Path $env:LOCALAPPDATA ('UniversalPLC/Python-' + $recipe.bootstrap_version)
    $arguments = @($recipe.silent_arguments) + ('TargetDir="' + $pythonRoot + '"')
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(300000)) { throw 'INSTALL_TIMEOUT: inspect installer; do not launch a second installation.' }
    if ($process.ExitCode -ne 0) { throw "INSTALL_INCOMPLETE: exit $($process.ExitCode); no automatic reboot." }
    $PythonExe = Join-Path $pythonRoot 'python.exe'
}
if (-not (Test-UsablePython $PythonExe)) { throw 'PYTHON_REQUIRED: provide an existing Python 3.11+ executable.' }
if ($CheckOnly) {
    @{status='PYTHON_PRESENT'; python=$PythonExe; environment=$envRoot; mutations=$false} | ConvertTo-Json
    exit 0
}
if (-not (Test-Path -LiteralPath $envPython)) {
    if (Test-Path -LiteralPath $envRoot) { throw 'ENVIRONMENT_INCOMPLETE: choose a new empty environment path.' }
    & $PythonExe -I -m venv $envRoot
    if ($LASTEXITCODE -ne 0) { throw 'VENV_CREATION_FAILED' }
}
& $envPython (Join-Path $repoRoot 'tools/install.py') --profile dvp
if ($LASTEXITCODE -ne 0) { throw 'DVP_PACKAGE_INSTALL_FAILED' }
& $envPython -I -m universal_plc doctor
if ($LASTEXITCODE -ne 0) { throw 'DVP_DOCTOR_FAILED' }
@{status='SOFTWARE_INSTALLED'; python=$envPython; mcp_arguments=@('-I','-m','universal_plc','serve'); live_verified=$false} | ConvertTo-Json
