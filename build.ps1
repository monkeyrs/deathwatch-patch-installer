$ErrorActionPreference = 'Stop'
$installer = $PSScriptRoot
$pyinstaller = Join-Path $installer '.venv\Scripts\pyinstaller.exe'
$spec = Join-Path $installer 'DeathwatchPatchInstaller.spec'
$dist = Join-Path $installer 'dist'
$work = Join-Path $installer 'build\real'

& $pyinstaller --noconfirm --clean --distpath $dist --workpath $work $spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$exe = Join-Path $dist 'DeathwatchPatchInstaller.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw "Installer executable was not created: $exe" }
Write-Host "Installer executable ready: $exe"
