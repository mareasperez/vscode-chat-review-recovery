param(
    [ValidatePattern('^[A-Za-z0-9._-]+$')]
    [string]$OutputName = 'VSCodeChatRecovery'
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Tests failed. Executable was not built.' }
    # External tool directories can inject incompatible ICU DLLs into Qt bundles.
    $originalPath = $env:PATH
    try {
        $pythonDirectory = python -c "import sys; from pathlib import Path; print(Path(sys.executable).parent)"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve Python runtime.' }
        $env:PATH = @(
            $pythonDirectory
            (Join-Path $pythonDirectory 'Scripts')
            (Join-Path $env:SystemRoot 'System32')
            $env:SystemRoot
            (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0')
        ) -join ';'
        & (Join-Path $pythonDirectory 'python.exe') -m PyInstaller --noconfirm --clean --onefile --windowed --name $OutputName --distpath dist --workpath build --specpath build app.py
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
    } finally {
        $env:PATH = $originalPath
    }
    Get-FileHash -LiteralPath ".\dist\$OutputName.exe" -Algorithm SHA256
} finally {
    Pop-Location
}
