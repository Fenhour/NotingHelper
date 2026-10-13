@echo off
setlocal
cd /d "%~dp0"
set "NOTING_HELPER_DIR=%~dp0"

echo 正在下载 ECDICT，文件约 75 MB，请稍候...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop';" ^
  "$dataDir = Join-Path $env:NOTING_HELPER_DIR 'data';" ^
  "$target = Join-Path $dataDir 'ecdict.csv';" ^
  "$temporary = Join-Path ([IO.Path]::GetTempPath()) ('notinghelper-ecdict-' + [guid]::NewGuid().ToString('N') + '.zip');" ^
  "New-Item -ItemType Directory -Path $dataDir -Force | Out-Null;" ^
  "try {" ^
  "  Invoke-WebRequest -UseBasicParsing -Uri 'https://codeload.github.com/skywind3000/ECDICT/zip/refs/heads/master' -OutFile $temporary;" ^
  "  Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
  "  $archive = [System.IO.Compression.ZipFile]::OpenRead($temporary);" ^
  "  try {" ^
  "    $entry = $archive.GetEntry('ECDICT-master/ecdict.csv');" ^
  "    if ($null -eq $entry) { throw '压缩包中未找到 ecdict.csv' }" ^
  "    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true);" ^
  "  } finally { $archive.Dispose() }" ^
  "  $header = Get-Content -LiteralPath $target -TotalCount 1;" ^
  "  if ($header -notlike 'word,phonetic,definition,translation,pos,*') { throw '词典 CSV 表头不正确' }" ^
  "  Write-Host ('下载完成：' + $target);" ^
  "} finally {" ^
  "  if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }" ^
  "}"

if errorlevel 1 (
  echo.
  echo 下载失败，请检查网络后重试，或手动从 ECDICT 项目下载 ecdict.csv。
  pause
  exit /b 1
)

echo.
echo 请启动划词助手并点击“检查词典”建立索引。
pause
endlocal
