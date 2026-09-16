$dir = (Get-Location).Path
$pythonw = Join-Path (Split-Path (Get-Command python.exe).Source) 'pythonw.exe'
$script = Join-Path $dir 'run_daily.py'
$action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $script + '"') -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday -At '14:05'
Register-ScheduledTask -TaskName 'FundWatch' -Action $action -Trigger $trigger -Description 'fund watch daily AI report' -Force | Out-Null
Get-ScheduledTask -TaskName 'FundWatch' | Format-List TaskName, State
