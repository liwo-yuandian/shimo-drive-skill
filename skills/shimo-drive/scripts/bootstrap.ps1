# Windows entrypoint; does not require Python, change execution policy or edit PATH.
param(
    [ValidateSet('check', 'ensure', 'run')][string]$Mode = 'check',
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
)
$ErrorActionPreference = 'Stop'

function Find-Python {
    $candidates = @()
    if ($env:SHIMO_DRIVE_PYTHON) {
        if (-not [System.IO.Path]::IsPathRooted($env:SHIMO_DRIVE_PYTHON)) { return $null }
        $candidates = @($env:SHIMO_DRIVE_PYTHON)
    } else {
        foreach ($name in @('python3', 'python', 'python3.13')) {
            $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($command -and $command.Source -notlike '*\WindowsApps\*') { $candidates += $command.Source }
        }
        $launcher = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($launcher) {
            try {
                $found = & $launcher.Source -3 -c 'import sys; print(sys.executable) if sys.version_info >= (3,9) else sys.exit(1)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $candidates += [string]$found }
            } catch { }
        }
        if ($env:LOCALAPPDATA) {
            $candidates += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe') -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
        if ($env:ProgramFiles) {
            $candidates += @(Get-ChildItem -Path (Join-Path $env:ProgramFiles 'Python*\python.exe') -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
        }
    }
    foreach ($candidate in $candidates) {
        try {
            & $candidate -c 'import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)' 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch { }
    }
    return $null
}
function Stop-Setup([string]$Code, [string]$Next) {
    @{ready=$false; error=$Code; next=$Next} | ConvertTo-Json -Compress
    exit 2
}
$pythonExe = Find-Python
if (-not $pythonExe) {
    if ($env:SHIMO_DRIVE_PYTHON) { Stop-Setup 'python_override_invalid' 'Fix SHIMO_DRIVE_PYTHON; Python 3.9+ is required.' }
    if ($Mode -ne 'ensure') { Stop-Setup 'python_missing' 'Run bootstrap.ps1 ensure, or follow references/setup.md.' }
    $winget = Get-Command winget -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $winget) { Stop-Setup 'python_install_required' 'Use the official Python installer; see references/setup.md.' }
    # Do not accept source/package agreements or elevate silently.
    & $winget.Source install --id Python.Python.3.13 --exact --source winget --scope user --disable-interactivity | Out-Host
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'python_install_required' 'WinGet could not finish. Check its output for network, policy or interactive agreement requirements; see references/setup.md.' }
    $pythonExe = Find-Python
    if (-not $pythonExe) { Stop-Setup 'python_install_required' 'Python 3.9+ is still unavailable. Reopen the terminal or set SHIMO_DRIVE_PYTHON to its absolute path, then rerun ensure.' }
}
if ($Mode -eq 'run') {
    if (-not $Arguments -or $Arguments[0] -notin @('drive_api.py', 'document.py', 'environment.py')) { Stop-Setup 'invalid_script' 'Choose drive_api.py, document.py or environment.py.' }
    $scriptName = $Arguments[0]
    $remaining = @($Arguments | Select-Object -Skip 1)
    & $pythonExe (Join-Path $PSScriptRoot $scriptName) @remaining
} else {
    & $pythonExe (Join-Path $PSScriptRoot 'environment.py') $Mode @Arguments
}
exit $LASTEXITCODE
