. "$PSScriptRoot/_environment.ps1"
if (-not (Test-Path -LiteralPath $KomaquillPython)) { throw (Get-KomaquillMessage 'bootstrap.missing') }
$env:PATH = (Split-Path -Parent $KomaquillPython) + [IO.Path]::PathSeparator + (Split-Path -Parent $KomaquillUv) + [IO.Path]::PathSeparator + $env:PATH
function global:komaquill {
    & $KomaquillPython -m komaquill @args
}
Write-Output (Get-KomaquillMessage 'bootstrap.command_ready')
