param([ValidateSet('daily', 'ci', 'all')][string]$Scope = 'daily', [switch]$Plan)
. "$PSScriptRoot/../bootstrap/_environment.ps1"
$arguments = @("$PSScriptRoot/check.py", '--scope', $Scope)
if ($Plan) { $arguments += '--plan' }
& $KomaquillPython @arguments
exit $LASTEXITCODE
