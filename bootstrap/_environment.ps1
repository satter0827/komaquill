# Share environment paths; commands remain in the CLI and quality gates.
$ErrorActionPreference = 'Stop'
$KomaquillRoot = Split-Path -Parent $PSScriptRoot
$env:KOMAQUILL_PROJECT_DIR = $KomaquillRoot
$env:UV_PROJECT_ENVIRONMENT = Join-Path $KomaquillRoot '.artifacts/runtime/venv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $KomaquillRoot '.artifacts/runtime/python'
$env:UV_CACHE_DIR = Join-Path $KomaquillRoot '.artifacts/cache/uv'
$env:PYTHONPYCACHEPREFIX = Join-Path $KomaquillRoot '.artifacts/cache/pycache'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HF_HUB_CACHE = Join-Path $KomaquillRoot '.artifacts/cache/huggingface/hub'
$env:HF_XET_CACHE = Join-Path $KomaquillRoot '.artifacts/cache/xet'
$env:TMP = Join-Path $KomaquillRoot '.artifacts/tmp'
$env:TEMP = $env:TMP
New-Item -ItemType Directory -Path $env:TMP -Force | Out-Null
$KomaquillUv = Join-Path $KomaquillRoot '.artifacts/runtime/uv/uv.exe'
$KomaquillPython = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe'

$env:KOMAQUILL_UV = $KomaquillUv

# Read text without depending on the Python environment being installed.
$KomaquillMessages = Get-Content -LiteralPath (Join-Path $KomaquillRoot 'scripts/messages.json') -Raw -Encoding utf8 | ConvertFrom-Json -AsHashtable
function Get-KomaquillMessage([string]$Key) {
    if (-not $KomaquillMessages.ContainsKey($Key)) { throw 'Message definitions are unavailable. Restore the source files.' }
    return $KomaquillMessages[$Key]
}
