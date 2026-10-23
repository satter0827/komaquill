. "$PSScriptRoot/../bootstrap/env.ps1"
& $KomaquillPython "$PSScriptRoot/security.py"
if ($LASTEXITCODE -ne 0) { throw '公開前の検査に失敗しました。' }
