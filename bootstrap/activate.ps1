. "$PSScriptRoot/env.ps1"
if (-not (Test-Path -LiteralPath $KomaquillPython)) { throw '先にbootstrap/setup.ps1を実行してください。' }
$env:PATH = (Split-Path -Parent $KomaquillPython) + [IO.Path]::PathSeparator + (Split-Path -Parent $KomaquillUv) + [IO.Path]::PathSeparator + $env:PATH
function global:komaquill {
    & $KomaquillPython -m komaquill @args
}
Write-Output 'komaquill コマンドを使用できます。'
