param(
    [Parameter(Mandatory = $true)][int]$ProcessId,
    [string]$ProcessName = "",
    [string]$WindowTitle = ""
)

$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function New-Result {
    param(
        [string]$Application,
        [string]$Path
    )
    if ([string]::IsNullOrWhiteSpace($Path)) { return $null }
    return [ordered]@{
        application = $Application
        path = $Path
        window_title = $WindowTitle
    }
}

function Get-WordContext {
    param([string]$ProgId, [string]$Application)
    try {
        $app = [Runtime.InteropServices.Marshal]::GetActiveObject($ProgId)
        $document = $app.ActiveDocument
        if ($null -eq $document) { return $null }
        return New-Result $Application ([string]$document.FullName)
    } catch {
        return $null
    }
}

function Get-ExcelContext {
    param([string]$ProgId, [string]$Application)
    try {
        $app = [Runtime.InteropServices.Marshal]::GetActiveObject($ProgId)
        $workbook = $app.ActiveWorkbook
        if ($null -eq $workbook) { return $null }
        return New-Result $Application ([string]$workbook.FullName)
    } catch {
        return $null
    }
}

function Get-PresentationContext {
    param([string]$ProgId, [string]$Application)
    try {
        $app = [Runtime.InteropServices.Marshal]::GetActiveObject($ProgId)
        $presentation = $app.ActivePresentation
        if ($null -eq $presentation) { return $null }
        return New-Result $Application ([string]$presentation.FullName)
    } catch {
        return $null
    }
}

function Get-AcrobatContext {
    try {
        $app = [Runtime.InteropServices.Marshal]::GetActiveObject("AcroExch.App")
        $document = $app.GetActiveDoc()
        if ($null -eq $document) { return $null }
        $pdf = $document.GetPDDoc()
        return New-Result "Adobe Acrobat" ([string]$pdf.GetFileName())
    } catch {
        return $null
    }
}

function Test-WindowMatchesPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    $leaf = [IO.Path]::GetFileName($Path)
    if ([string]::IsNullOrWhiteSpace($leaf)) { return $false }
    if ($WindowTitle.IndexOf($leaf, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $true
    }
    $stem = [IO.Path]::GetFileNameWithoutExtension($leaf)
    return (
        $stem.Length -ge 2 -and
        $WindowTitle.IndexOf($stem, [StringComparison]::OrdinalIgnoreCase) -ge 0
    )
}

function Get-RecentDocumentContext {
    try {
        $recent = [Environment]::GetFolderPath('Recent')
        if ([string]::IsNullOrWhiteSpace($recent) -or -not (Test-Path -LiteralPath $recent)) {
            return $null
        }
        $shell = New-Object -ComObject WScript.Shell
        $shortcuts = Get-ChildItem -LiteralPath $recent -Filter '*.lnk' -File |
            Sort-Object LastWriteTime -Descending
        foreach ($shortcutFile in $shortcuts) {
            $shortcut = $shell.CreateShortcut($shortcutFile.FullName)
            $target = [string]$shortcut.TargetPath
            if ((Test-Path -LiteralPath $target -PathType Leaf) -and (Test-WindowMatchesPath $target)) {
                return New-Result $ProcessName ([IO.Path]::GetFullPath($target))
            }
        }
    } catch {}
    return $null
}

function Get-CommandLinePath {
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
        $commandLine = [string]$process.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) { return $null }
        $extensions = '\.(docx?|wps|pdf|pptx?|xlsx?|odt|ods|odp|rtf|txt|md|epub)$'
        $matches = [regex]::Matches($commandLine, '"([^"]+)"|([^\s]+)')
        foreach ($match in $matches) {
            $candidate = if ($match.Groups[1].Success) { $match.Groups[1].Value } else { $match.Groups[2].Value }
            if (
                $candidate -match $extensions -and
                (Test-Path -LiteralPath $candidate -PathType Leaf) -and
                (Test-WindowMatchesPath $candidate)
            ) {
                return New-Result $ProcessName ([IO.Path]::GetFullPath($candidate))
            }
        }
    } catch {}
    return $null
}

$name = $ProcessName.ToLowerInvariant()
$result = $null
switch -Regex ($name) {
    '^wps(\.exe)?$'      {
        if ($WindowTitle -notmatch '(?i)\.pdf(?:\s|$)') {
            $result = Get-WordContext "KWPS.Application" "WPS Writer"
        }
        break
    }
    '^winword(\.exe)?$'  { $result = Get-WordContext "Word.Application" "Microsoft Word"; break }
    '^et(\.exe)?$'       { $result = Get-ExcelContext "KET.Application" "WPS Spreadsheets"; break }
    '^excel(\.exe)?$'    { $result = Get-ExcelContext "Excel.Application" "Microsoft Excel"; break }
    '^wpp(\.exe)?$'      { $result = Get-PresentationContext "KWPP.Application" "WPS Presentation"; break }
    '^powerpnt(\.exe)?$' { $result = Get-PresentationContext "PowerPoint.Application" "Microsoft PowerPoint"; break }
    '^(acrord32|acrobat)(\.exe)?$' { $result = Get-AcrobatContext; break }
}

if ($null -ne $result -and -not (Test-WindowMatchesPath ([string]$result.path))) {
    $result = $null
}
if ($null -eq $result -and $WindowTitle -match 'WPS' -and $WindowTitle -notmatch '(?i)\.pdf(?:\s|$)') {
    $result = Get-WordContext "KWPS.Application" "WPS Writer"
}
if ($null -eq $result -and $WindowTitle -match 'Word') {
    $result = Get-WordContext "Word.Application" "Microsoft Word"
}
if ($null -eq $result -and $WindowTitle -match 'Excel') {
    $result = Get-ExcelContext "Excel.Application" "Microsoft Excel"
}
if ($null -eq $result -and $WindowTitle -match 'PowerPoint') {
    $result = Get-PresentationContext "PowerPoint.Application" "Microsoft PowerPoint"
}
if ($null -ne $result -and -not (Test-WindowMatchesPath ([string]$result.path))) {
    $result = $null
}
if ($null -eq $result) {
    $result = Get-RecentDocumentContext
}
if ($null -eq $result) {
    $result = Get-CommandLinePath
}
if ($null -ne $result) {
    $result | ConvertTo-Json -Compress
}
