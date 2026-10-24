param([ValidateSet(1, 2, 3)][int]$Level = 2, [switch]$Plan, [switch]$Json, [string[]]$Check)
. "$PSScriptRoot/../bootstrap/_environment.ps1"
$arguments = @("$PSScriptRoot/check.py", '--level', "$Level")
if ($Plan) { $arguments += '--plan' }
if ($Json) { $arguments += '--json' }
foreach ($identity in $Check) { $arguments += @("--check", $identity) }
& $KomaquillPython @arguments
exit $LASTEXITCODE
