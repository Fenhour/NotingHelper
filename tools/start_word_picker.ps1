# Resolve an installed interpreter without depending on pythonw being on PATH.
[CmdletBinding()]
param([switch]$DebugMode, [switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot

function Find-Python {
    foreach ($relative in @('.venv\Scripts\python.exe', 'venv\Scripts\python.exe')) {
        $candidate = Join-Path $projectRoot $relative
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $launcher = Get-Command py.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($launcher) {
        try {
            $resolved = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        } catch {
            $resolved = $null
        }
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath ([string]$resolved) -PathType Leaf)) {
            return ([string]$resolved).Trim()
        }
    }
    foreach ($name in @('python.exe', 'pythonw.exe')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notlike '*\Microsoft\WindowsApps\*') {
            return $command.Source
        }
    }
    # Standard per-user and system installations, including Python without PATH/py.
    $bases = @("$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles, ${env:ProgramFiles(x86)})
    foreach ($base in $bases) {
        if (-not $base -or -not (Test-Path -LiteralPath $base -PathType Container)) { continue }
        $directories = Get-ChildItem -LiteralPath $base -Directory -Filter 'Python*' | Sort-Object Name -Descending
        foreach ($directory in $directories) {
            $candidate = Join-Path $directory.FullName 'python.exe'
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    throw 'Python 3 was not found. Install Python 3 with tkinter, then try again.'
}

try {
    $python = Find-Python
    if (-not $DebugMode) {
        $windowless = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
        if (Test-Path -LiteralPath $windowless -PathType Leaf) { $python = $windowless }
    }
    if ($CheckOnly) { Write-Output $python; exit 0 }
    $script = Join-Path $projectRoot 'word_picker.py'
    if ($DebugMode) {
        Set-Location -LiteralPath $projectRoot
        & $python $script
        exit $LASTEXITCODE
    }
    Start-Process -FilePath $python -ArgumentList ('"{0}"' -f $script) -WorkingDirectory $projectRoot -WindowStyle Hidden
} catch {
    if ($DebugMode -or $CheckOnly) { Write-Error $_; exit 1 }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'NotingHelper startup failed') | Out-Null
    exit 1
}
