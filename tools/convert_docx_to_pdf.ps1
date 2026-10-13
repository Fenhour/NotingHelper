param(
    [Parameter(Mandatory = $true)][string]$SourcePath,
    [Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Write-Result {
    param([bool]$Ok, [string]$Engine, [string]$Path, [string]$ErrorMessage)
    [ordered]@{
        ok = $Ok
        engine = $Engine
        path = $Path
        error = $ErrorMessage
    } | ConvertTo-Json -Compress
}

function Find-LibreOffice {
    $command = Get-Command "soffice.exe" -ErrorAction SilentlyContinue
    if ($null -ne $command) { return [string]$command.Source }
    foreach ($candidate in @(
        "C:\Program Files\LibreOffice\program\soffice.exe",
        "C:\Program Files (x86)\LibreOffice\program\soffice.exe"
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

function Convert-WithLibreOffice {
    param([string]$Executable, [string]$Source, [string]$Output)
    $outputDirectory = Split-Path -LiteralPath $Output -Parent
    $generated = Join-Path $outputDirectory (([IO.Path]::GetFileNameWithoutExtension($Source)) + ".pdf")
    & $Executable --headless --convert-to "pdf:writer_pdf_Export" --outdir $outputDirectory $Source | Out-Null
    if (-not (Test-Path -LiteralPath $generated -PathType Leaf)) {
        throw "LibreOffice 未生成 PDF。"
    }
    if ([IO.Path]::GetFullPath($generated) -ne [IO.Path]::GetFullPath($Output)) {
        Move-Item -LiteralPath $generated -Destination $Output -Force
    }
}

function Convert-WithComWriter {
    param([string]$ProgId, [string]$Source, [string]$Output)
    $application = $null
    $document = $null
    try {
        $application = New-Object -ComObject $ProgId
        $application.Visible = $false
        $application.DisplayAlerts = 0
        $document = $application.Documents.Open($Source, $false, $true)
        $document.ExportAsFixedFormat($Output, 17)
    } finally {
        if ($null -ne $document) {
            $document.Close(0)
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($document)
        }
        if ($null -ne $application) {
            $application.Quit()
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($application)
        }
    }
}

try {
    $source = [IO.Path]::GetFullPath($SourcePath)
    $output = [IO.Path]::GetFullPath($OutputPath)
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "DOCX 来源文件不存在。" }
    if ([IO.Path]::GetExtension($source).ToLowerInvariant() -ne ".docx") { throw "来源文件不是 DOCX。" }
    if ([IO.Path]::GetExtension($output).ToLowerInvariant() -ne ".pdf") { throw "输出文件必须是 PDF。" }
    $outputDirectory = Split-Path -LiteralPath $output -Parent
    [void](New-Item -ItemType Directory -Path $outputDirectory -Force)

    $libreOffice = Find-LibreOffice
    if ($null -ne $libreOffice) {
        Convert-WithLibreOffice $libreOffice $source $output
        Write-Result $true "libreoffice" $output ""
        exit 0
    }

    $converterErrors = @()
    foreach ($converter in @(
        @{ ProgId = "Word.Application"; Engine = "microsoft-word" },
        @{ ProgId = "KWPS.Application"; Engine = "wps-writer" }
    )) {
        try {
            Convert-WithComWriter $converter.ProgId $source $output
            if (
                -not (Test-Path -LiteralPath $output -PathType Leaf) -or
                (Get-Item -LiteralPath $output).Length -le 0
            ) { throw "$($converter.Engine) 未生成有效 PDF。" }
            Write-Result $true $converter.Engine $output ""
            exit 0
        } catch {
            $converterErrors += "$($converter.Engine): $($_.Exception.Message)"
        }
    }
    throw ($converterErrors -join "; ")
} catch {
    Write-Result $false "" "" ([string]$_.Exception.Message)
    exit 1
}
