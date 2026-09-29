<#
.SYNOPSIS
    Registers (or removes) the Windows scheduled task that runs "fpreporter collect" once a day.

.DESCRIPTION
    The task runs as the current user, only while that user is logged on (no password is stored).
    If the PC is off or you are logged out at the scheduled time, it runs as soon as possible
    afterwards; the collector's watermark makes a late run cover the whole gap.

    It uses pythonw.exe, so no console window pops up. Output goes to data\fpreporter.log,
    and the exit code (0 ok, 1 run failed, 2 config error) appears as "Last Run Result".

    JENKINS_USER and JENKINS_TOKEN must be persistent *user* environment variables; values set with
    $env:... in a shell are not visible to scheduled tasks.

.EXAMPLE
    .\scripts\register-task.ps1
.EXAMPLE
    .\scripts\register-task.ps1 -Time 18:30
.EXAMPLE
    .\scripts\register-task.ps1 -Unregister
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'FpReporter Collect',
    [ValidatePattern('^([01]\d|2[0-3]):[0-5]\d$')]
    [string]$Time = '17:00',
    [switch]$Unregister
)

$ErrorActionPreference = 'Stop'

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    } else {
        Write-Host "No scheduled task named '$TaskName'."
    }
    return
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $projectRoot '.venv\Scripts\pythonw.exe'
$config = Join-Path $projectRoot 'config.toml'

if (-not (Test-Path $pythonw)) {
    throw "Not found: $pythonw. Create the virtualenv and run 'pip install -e .' first."
}
if (-not (Test-Path $config)) {
    throw "Not found: $config. Copy config.example.toml to config.toml and adjust it."
}

foreach ($name in 'JENKINS_USER', 'JENKINS_TOKEN') {
    if (-not [Environment]::GetEnvironmentVariable($name, 'User')) {
        Write-Warning ("$name is not set as a persistent user environment variable, so the task will fail with exit code 2. " +
            "Set it with: [Environment]::SetEnvironmentVariable('$name', '<value>', 'User')")
    }
}

$at = [datetime]::ParseExact($Time, 'HH:mm', [Globalization.CultureInfo]::InvariantCulture)

$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument "-m fpreporter --config `"$config`" collect" `
    -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $at
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "Collects FORCE_PASS usage from Jenkins into $projectRoot\data (fpreporter)." `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Registered '$TaskName': daily at $Time, next run $($info.NextRunTime)."
Write-Host "Run it now with:  Start-ScheduledTask -TaskName '$TaskName'"
