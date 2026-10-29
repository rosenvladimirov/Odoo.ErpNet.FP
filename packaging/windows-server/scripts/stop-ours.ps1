# Спира НАШИТЕ услуги и процеси, преди да се подмени рънтаймът (или при
# деинсталация). И двете услуги вървят от вградения Python — докато някоя е
# жива, python3.dll е заключен и NSIS пада с „Can't write“ (видяно 05.10).
# sc stop е асинхронен ⇒ чакаме до 30 s; накрая убиваме само процеси, пуснати
# от нашата папка (pythonservice.exe, cloudflared.exe, traefik.exe) — чужди не се пипат.

param([Parameter(Mandatory = $true)] [string] $InstallDir)

$names = 'OdooErpNetFP', 'ErpNetFP-Tunnel', 'ErpNetFP-Edge'   # Edge: traefik.exe и Python-ът му
Stop-Service -Name $names -Force -ErrorAction SilentlyContinue
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Service -Name $names -ErrorAction SilentlyContinue | Where-Object Status -ne 'Stopped') -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
}
$prefix = $InstallDir.TrimEnd('\') + '\'
Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } |
    ForEach-Object { Write-Output "[stop] killing $($_.Path) (pid $($_.Id))"; Stop-Process -Id $_.Id -Force }
Write-Output "[stop] our services: $((Get-Service -Name $names -ErrorAction SilentlyContinue | ForEach-Object { "$($_.Name)=$($_.Status)" }) -join ', ')"
exit 0
