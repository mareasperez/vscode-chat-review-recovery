param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$OutputName = 'VSCodeChatRecovery'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Executable was not built.' }
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name $OutputName --distpath dist --workpath build --specpath build qt_app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    Get-FileHash -LiteralPath ".\dist\$OutputName.exe" -Algorithm SHA256
} finally {
    Pop-Location
}
