param([switch]$InstallExtensions)
. "$PSScriptRoot/env.ps1"
$uvVersion = '0.13.5'
$architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString().ToLowerInvariant()
if ($architecture -notin @('arm64', 'x64')) { throw (Get-KomaquillMessage 'bootstrap.architecture') }
$uvArchitecture = if ($architecture -eq 'arm64') { 'aarch64' } else { 'x86_64' }
$toolRoot = Join-Path $KomaquillRoot '.artifacts/runtime'
New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null

function Install-VerifiedZip([string]$Url, [string]$Digest, [string]$Destination, [string]$ArchiveName) {
    $archivePath = Join-Path $toolRoot $ArchiveName
    Invoke-WebRequest -Uri $Url -OutFile $archivePath
    if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Digest) {
        throw (Get-KomaquillMessage 'bootstrap.checksum')
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $Destination -Force
}

if (-not (Test-Path -LiteralPath $KomaquillUv)) {
    $uvUrl = "https://github.com/astral-sh/uv/releases/download/$uvVersion/uv-$uvArchitecture-pc-windows-msvc.zip"
    $uvChecksum = Invoke-RestMethod -Uri ($uvUrl + '.sha256')
    Install-VerifiedZip $uvUrl (($uvChecksum.Trim() -split '\s+')[0]) (Split-Path -Parent $KomaquillUv) 'uv.zip'
}
$KomaquillPythonVersion = (Get-Content -LiteralPath (Join-Path $KomaquillRoot '.python-version') -Raw).Trim()
& $KomaquillUv sync --locked --group dev --extra colab-client --python "cpython-$KomaquillPythonVersion-windows-x86_64-none" --project $KomaquillRoot
if ($LASTEXITCODE -ne 0) { throw (Get-KomaquillMessage 'bootstrap.python') }

git -C $KomaquillRoot config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) { throw (Get-KomaquillMessage 'bootstrap.hooks') }
$editorEnvironment = Join-Path $toolRoot 'vscode.env'
@(
    "KOMAQUILL_PROJECT_DIR=$KomaquillRoot"
    "PYTHONUTF8=1"
    "PYTHONIOENCODING=utf-8"
    "PYTHONPYCACHEPREFIX=$env:PYTHONPYCACHEPREFIX"
    "HF_HOME=$env:HF_HOME"
    "HF_XET_CACHE=$env:HF_XET_CACHE"
    "TMP=$env:TMP"
    "TEMP=$env:TEMP"
) | Set-Content -LiteralPath $editorEnvironment -Encoding utf8
if ($InstallExtensions) {
    $recommendations = Get-Content -LiteralPath (Join-Path $KomaquillRoot '.vscode/extensions.json') -Raw | ConvertFrom-Json
    foreach ($extension in $recommendations.recommendations) {
        & code --install-extension $extension
        if ($LASTEXITCODE -ne 0) { throw (Get-KomaquillMessage 'bootstrap.extensions') }
    }
}
Write-Output (Get-KomaquillMessage 'bootstrap.ready')
