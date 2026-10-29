# Пред касата на Windows: Traefik с *.erpnet.mcpworks.net + certpull (ADR erpnet-kasi-fleet/0005).
# Същото като packaging/edge/install-edge.sh на Linux; услугата е ErpNetFP-Edge.
#
#   powershell -ExecutionPolicy Bypass -File install-edge.ps1 `
#       -EdgeName kasa.<клиент>.erpnet.mcpworks.net -PinpadUpstream https://<BlueCash>:9443 `
#       -CertpullUrl https://<сървърът> -SecretFile <файл с тайната на машината> [-SourceDir <папка с .py>]
#   powershell -ExecutionPolicy Bypass -File install-edge.ps1 -Remove
#
# Идемпотентно. Пипа само нашето: своя услуга, свой блок в hosts, свои папки.
param(
    [string]$EdgeName,
    [string]$PinpadUpstream,
    [string]$ProxyUpstream = "http://127.0.0.1:8001",
    [string]$CertpullUrl,
    [string]$SecretFile,
    [string]$SourceDir,
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TraefikVersion = "v3.7.14"
$TraefikSha256  = "47b5d11c5d8b2d798357edf9d73e33a40bb045b4c7371da5e322c5083501c5c4"
$Inst  = Join-Path $env:ProgramFiles "Odoo.ErpNet.FP"
$Py    = Join-Path $Inst "python\python.exe"
$Pkg   = Join-Path $Inst "python\Lib\site-packages\odoo_erpnet_fp"
$Data  = Join-Path $env:ProgramData "Odoo.ErpNet.FP\edge"
$Dyn   = Join-Path $Data "dynamic"
$Certs = Join-Path $Data "certs"
$Hosts = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
$Begin = "# BEGIN erpnet-fp-edge (managed by Odoo.ErpNet.FP - do not edit inside)"
$End   = "# END erpnet-fp-edge"

function Set-HostsBlock([string]$Name) {
    $lines = Get-Content -LiteralPath $Hosts -Encoding ASCII
    $out = New-Object System.Collections.Generic.List[string]; $skip = $false
    foreach ($l in $lines) {
        if ($l -eq $Begin) { $skip = $true; continue }
        if ($l -eq $End) { $skip = $false; continue }
        if (-not $skip) { $out.Add($l) }
    }
    if ($Name) { $out.Add($Begin); $out.Add("127.0.0.1 $Name"); $out.Add($End) }
    $new = ($out -join "`r`n") + "`r`n"
    if ($new -ne ((Get-Content -LiteralPath $Hosts -Raw -Encoding ASCII))) {
        Copy-Item -LiteralPath $Hosts -Destination "$Hosts.bak-erpnet-edge" -Force
        [System.IO.File]::WriteAllText($Hosts, $new, [System.Text.Encoding]::ASCII)
    }
}

function Write-Text([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Protect-AdminOnly([string]$Path) {
    # само SYSTEM и администраторите (както токена на тунела)
    & icacls $Path /inheritance:r /grant:r "*S-1-5-18:F" "*S-1-5-32-544:F" | Out-Null
}

if ($Remove) {
    if (Get-Service ErpNetFP-Edge -ErrorAction SilentlyContinue) {
        Stop-Service ErpNetFP-Edge -ErrorAction SilentlyContinue
        & $Py -m odoo_erpnet_fp.server.win_edge_service remove
    }
    Set-HostsBlock ""
    Remove-Item -LiteralPath $Dyn -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "edge: removed (certificate and secrets in $Data are kept)"
    exit 0
}

foreach ($p in "EdgeName","PinpadUpstream","CertpullUrl","SecretFile") {
    if (-not (Get-Variable $p -ValueOnly)) { throw "missing -$p" }
}
if ($EdgeName -notmatch '^[a-z0-9-]+(\.[a-z0-9-]+)?\.erpnet\.mcpworks\.net$') {
    throw "EdgeName must be <kasa>.<client>.erpnet.mcpworks.net"
}
if (-not (Test-Path $Py)) { throw "Odoo.ErpNet.FP is not installed ($Py)" }

# 1) кодът: certpull + услугата (докато не влязат в инсталатора на проксито)
if ($SourceDir) {
    Copy-Item (Join-Path $SourceDir "certpull.py") (Join-Path $Pkg "certpull.py") -Force
    Copy-Item (Join-Path $SourceDir "win_edge_service.py") (Join-Path $Pkg "server\win_edge_service.py") -Force
}
& $Py -c "import odoo_erpnet_fp.certpull, odoo_erpnet_fp.server.win_edge_service" 2>$null
if ($LASTEXITCODE -ne 0) { throw "certpull/win_edge_service are not in $Pkg - pass -SourceDir" }

# 2) Traefik (закована версия, проверена по sha256)
$EdgeBin = Join-Path $Inst "edge"; New-Item -ItemType Directory -Force -Path $EdgeBin | Out-Null
$Exe = Join-Path $EdgeBin "traefik.exe"
$Stamp = Join-Path $EdgeBin "traefik.version"
if (-not (Test-Path $Exe) -or ((Get-Content $Stamp -ErrorAction SilentlyContinue) -ne $TraefikVersion)) {
    $zip = Join-Path $env:TEMP "traefik_$TraefikVersion.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/traefik/traefik/releases/download/$TraefikVersion/traefik_${TraefikVersion}_windows_amd64.zip" -OutFile $zip
    if ((Get-FileHash $zip -Algorithm SHA256).Hash.ToLower() -ne $TraefikSha256) { Remove-Item $zip; throw "traefik zip: sha256 mismatch" }
    if (Get-Service ErpNetFP-Edge -ErrorAction SilentlyContinue) { Stop-Service ErpNetFP-Edge -ErrorAction SilentlyContinue }
    Expand-Archive -LiteralPath $zip -DestinationPath $EdgeBin -Force
    Remove-Item $zip
    Write-Text $Stamp $TraefikVersion
}

# 3) настройката
New-Item -ItemType Directory -Force -Path $Dyn, $Certs | Out-Null
Protect-AdminOnly $Certs
$CertsFwd = $Certs -replace '\\', '/'
$DynFwd = $Dyn -replace '\\', '/'
$Secret = Join-Path $Data "certpull.secret"
Copy-Item -LiteralPath $SecretFile -Destination $Secret -Force
Protect-AdminOnly $Secret
Write-Text (Join-Path $Data "certpull.env") @"
CERTPULL_URL=$CertpullUrl
CERTPULL_SECRET_FILE=$Secret
CERTPULL_DIR=$Certs
CERTPULL_TRAEFIK_TLS=$DynFwd/tls.yml
CERTPULL_TRAEFIK_CERTS=$CertsFwd
CERTPULL_TRAEFIK_ALLOW=$DynFwd/allow.yml
"@
Protect-AdminOnly (Join-Path $Data "certpull.env")

if ((Test-Path (Join-Path $Certs "wildcard.crt")) -and (Test-Path (Join-Path $Certs "wildcard.key"))) {
    & $Py -c "import pathlib; from odoo_erpnet_fp import certpull; certpull.write_traefik_tls(pathlib.Path(r'$Dyn\tls.yml'), r'$CertsFwd')"
} else {
    Write-Text (Join-Path $Dyn "tls.yml") "# waiting for certpull`n"
}
& $Py -c "import pathlib; from odoo_erpnet_fp import certpull; certpull.write_traefik_allow(pathlib.Path(r'$Dyn\allow.yml'), certpull.own_ipv4())"
Write-Text (Join-Path $Dyn "routes.yml") @"
# managed by install-edge.ps1
http:
  routers:
    pinpad:
      rule: "Host(``$EdgeName``) && PathPrefix(``/pinpads``)"
      entryPoints: [websecure]
      middlewares: [only-local]
      service: pinpad
      tls: {}
      priority: 100
    proxy:
      rule: "Host(``$EdgeName``)"
      entryPoints: [websecure]
      middlewares: [only-local]
      service: proxy
      tls: {}
      priority: 10
  services:
    pinpad:
      loadBalancer:
        serversTransport: device
        servers: [{url: "$PinpadUpstream"}]
    proxy:
      loadBalancer:
        servers: [{url: "$ProxyUpstream"}]
  serversTransports:
    device:
      insecureSkipVerify: true
"@

# 4) името → тази машина
Set-HostsBlock $EdgeName

# 5) услугата
if (-not (Get-Service ErpNetFP-Edge -ErrorAction SilentlyContinue)) {
    & $Py -m odoo_erpnet_fp.server.win_edge_service --startup auto install
}
Restart-Service ErpNetFP-Edge -ErrorAction SilentlyContinue
if ((Get-Service ErpNetFP-Edge).Status -ne "Running") { Start-Service ErpNetFP-Edge }
Write-Host "edge: https://$EdgeName -> /pinpads: $PinpadUpstream ; else: $ProxyUpstream"
