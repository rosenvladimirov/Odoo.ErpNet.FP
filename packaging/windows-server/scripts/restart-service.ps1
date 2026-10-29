# Рестарт на една наша услуга, който не засяда: Stop-Service чака до 30 s; ако
# услугата остане в StopPending (видяно на 3.0.26 при OdooErpNetFP), убива се
# САМО нейният процес; после Start-Service и чакане до Running.
param([Parameter(Mandatory = $true)] [string] $Name)

$svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
if (-not $svc) { Write-Output "[restart] $Name is not installed"; exit 0 }
Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Service -Name $Name).Status -ne 'Stopped' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
if ((Get-Service -Name $Name).Status -ne 'Stopped') {
    $procId = (Get-CimInstance Win32_Service -Filter "Name='$Name'").ProcessId
    if ($procId) { Write-Output "[restart] $Name stuck - killing pid $procId"; Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}
Start-Service -Name $Name
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Service -Name $Name).Status -ne 'Running' -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
Write-Output "[restart] $Name = $((Get-Service -Name $Name).Status)"
if ((Get-Service -Name $Name).Status -ne 'Running') { exit 1 }
exit 0
