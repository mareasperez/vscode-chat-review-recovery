$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Executable was not built.' }
    python -m PyInstaller --noconfirm --clean --onefile --windowed --name VSCodeChatRecovery --distpath dist --workpath build app.py
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    Get-FileHash -LiteralPath '.\dist\VSCodeChatRecovery.exe' -Algorithm SHA256
} finally {
    Pop-Location
}
