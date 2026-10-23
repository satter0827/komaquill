. "$PSScriptRoot/../bootstrap/env.ps1"
& $KomaquillPython "$PSScriptRoot/security.py"
if ($LASTEXITCODE -ne 0) { throw (Get-KomaquillMessage 'bootstrap.security') }
