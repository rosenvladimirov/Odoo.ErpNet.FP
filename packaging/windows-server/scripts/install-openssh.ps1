# OpenSSH сървър като зависимост на проксито (ADR erpnet-kasi-fleet/0003).
# Вика се от installer.nsi с права на администратор, в 64-битов PowerShell.
#
# Правилото „чуждото не се пипа“:
#  - има ли вече sshd (вграденият на Windows или друг) — оставяме го както е:
#    не го махаме, не пипаме sshd_config, не пипаме защитната стена;
#  - няма ли — слагаме Win32-OpenSSH от MSI-то и ТОЗИ sshd е наш: правилото на
#    MSI-то в защитната стена се изключва, т.е. от LAN не се стига до него.
#    Тунелът идва на собствения LAN адрес на машината (не на 127.0.0.1 —
#    loopback Gateway връща непроменен, измерено 05.10) — локален трафик,
#    който защитната стена не спира. Белегът sshd-installed-by-erpnet-fp
#    казва, че е наш — и при обновяване го обновяваме само тогава.
#
# Изход: 0 = sshd има и върви; 1 = грешка (NSIS прекъсва инсталацията).

param(
    [Parameter(Mandatory = $true)] [string] $MsiPath,
    [Parameter(Mandatory = $true)] [string] $MsiVersion,
    [string] $DataDir = "$env:ProgramData\Odoo.ErpNet.FP"
)

$ErrorActionPreference = 'Stop'
$Marker = Join-Path $DataDir 'sshd-installed-by-erpnet-fp'

function Say($msg) { Write-Output "[openssh] $msg" }

function Get-ExeVersion([string] $path) {
    if (-not (Test-Path $path)) { return $null }
    $vi = (Get-Item $path).VersionInfo
    $v = New-Object System.Version $vi.FileMajorPart, $vi.FileMinorPart, $vi.FileBuildPart, $vi.FilePrivatePart
    if ($v -eq [version]'0.0.0.0') { return $null }
    return $v
}

try {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'logs') | Out-Null
    $svc = Get-CimInstance Win32_Service -Filter "Name='sshd'" -ErrorAction SilentlyContinue
    $ours = Test-Path $Marker

    if ($svc -and -not $ours) {
        # Чужд sshd — само отчитаме
        $exe = ($svc.PathName -replace '^"([^"]+)".*$', '$1') -replace '^(\S+\.exe).*$', '$1'
        Say "Existing sshd kept as is: $exe (version $(Get-ExeVersion $exe)), state $($svc.State), start $($svc.StartMode)"
        if ($svc.State -ne 'Running') {
            Say "WARNING: sshd is not running - remote SSH will not work until the owner starts it."
        }
        exit 0
    }

    $sshdExe = Join-Path $env:ProgramFiles 'OpenSSH\sshd.exe'
    $have = Get-ExeVersion $sshdExe
    if ($have -and $have -ge [version]$MsiVersion) {
        Say "Win32-OpenSSH $have already installed by Odoo.ErpNet.FP (bundled $MsiVersion)"
    } else {
        Say "Installing Win32-OpenSSH $MsiVersion (server component)"
        $log = Join-Path $DataDir 'logs\openssh-msi.log'
        $p = Start-Process msiexec.exe -Wait -PassThru -ArgumentList @(
            '/i', "`"$MsiPath`"", 'ADDLOCAL=Server', '/qn', '/norestart', '/l*v', "`"$log`"")
        if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { throw "msiexec exit code $($p.ExitCode), see $log" }
        New-Item -ItemType File -Force -Path $Marker | Out-Null
    }

    # Нашият sshd: първият старт създава ключовете и sshd_config
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    Get-NetFirewallRule -ErrorAction SilentlyContinue |
        Where-Object { $_.Direction -eq 'Inbound' -and $_.DisplayName -like '*OpenSSH*' -and $_.Enabled -eq 'True' } |
        ForEach-Object { Disable-NetFirewallRule -Name $_.Name; Say "Disabled firewall rule '$($_.DisplayName)' (sshd is reached only through the tunnel)" }

    Say "OK: Win32-OpenSSH $(Get-ExeVersion $sshdExe), sshd $((Get-Service sshd).Status)"
    exit 0
} catch {
    Write-Output "[openssh] ERROR: $($_.Exception.Message)"
    exit 1
}
