# TLS via Let's Encrypt + Cloudflare DNS-01

How the proxy gets a browser-trusted wildcard TLS certificate without
opening any inbound port and without manual cert acceptance.

---

## TL;DR

* Wildcard cert: `*.lan.mcpworks.net` (+ SAN `lan.mcpworks.net`)
* Issuer: Let's Encrypt (browser-trusted, no manual accept)
* Validity: 90 days, **auto-renewed by Traefik** when <30 days remain
* Validation: DNS-01 challenge via Cloudflare API (no inbound HTTP needed)
* Storage: `./letsencrypt/acme.json` (chmod 0600, gitignored)
* Trigger: Traefik issues on first request matching the wildcard hostname

Once configured, `https://erpnet.lan.mcpworks.net/`,
`https://kasa1.lan.mcpworks.net/`, `https://anything.lan.mcpworks.net/`
all serve a valid cert with no warning. CORS still applies.

---

## Why this setup (and not the others)

| Approach | Browser warning | Inbound port | Auto-renew | Cost |
|---|---|---|---|---|
| Self-signed cert | YES — manual accept per device | none | no | free |
| Cloudflare Origin CA | YES — only valid through CF proxy | none | no (15-yr) | free |
| Let's Encrypt + HTTP-01 | NO | port 80 must be public | yes | free |
| **Let's Encrypt + DNS-01 (this)** | **NO** | **none** | **yes** | **free** |

DNS-01 is the only way to get a real public wildcard cert without
opening ports. Crucial for in-shop proxy installs that sit behind NAT.

---

## Files involved

```
docker-compose.yml             traefik service: env, volumes, labels
docker/traefik/traefik.yml     entryPoints + certResolver `cf`
docker/traefik/dynamic.yml     fallback static cert for legacy hostnames
.env                           CLOUDFLARE_DNS_API_TOKEN (gitignored)
letsencrypt/acme.json          issued cert + ACME account (gitignored, mode 0600)
```

Anything in `letsencrypt/` is generated at runtime — never commit.

---

## One-time setup on a new machine

### 1. Cloudflare API token

Permissions required:

* Zone → Zone → **Read** (for the zone holding the wildcard)
* Zone → DNS → **Edit** (for the same zone)
* Zone Resources → Include → Specific zone → **mcpworks.net**

Create at <https://dash.cloudflare.com/profile/api-tokens> → Create
Token → Custom token. Copy the `cfut_...` value.

### 2. `.env` file

```bash
cd /path/to/Odoo.ErpNet.FP
cat > .env <<EOF
CLOUDFLARE_DNS_API_TOKEN=cfut_...your-token-here...
EOF
chmod 600 .env
```

### 3. Storage for ACME state

```bash
mkdir -p letsencrypt
touch letsencrypt/acme.json
chmod 600 letsencrypt/acme.json
```

Traefik refuses to use `acme.json` if mode is not 0600.

### 4. Bring it up

```bash
docker compose up -d
```

Traefik will:
1. Register an ACME account with Let's Encrypt (one-time)
2. On first request to `*.lan.mcpworks.net`, perform DNS-01:
   * Create `_acme-challenge.lan.mcpworks.net` TXT via Cloudflare API
   * Wait for propagation (1.1.1.1 + 8.8.8.8 cross-check)
   * Let's Encrypt validates → issues cert
   * Cleanup TXT record
3. Cache cert in `acme.json` for 90 days
4. Auto-renew at <30 days remaining (no human action)

Verify:

```bash
echo | openssl s_client -connect 127.0.0.1:443 \
  -servername erpnet.lan.mcpworks.net 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# Should print:
# subject=CN=*.lan.mcpworks.net
# issuer=...Let's Encrypt...R12
```

---

## Auto-add a new subdomain

DNS for `*.lan.mcpworks.net` already resolves through Cloudflare-via-token.
For LAN-only access (proxy on `127.0.0.1`), add a `/etc/hosts` line on
each machine that needs it:

```bash
echo "127.0.0.1 kasa1.lan.mcpworks.net" | sudo tee -a /etc/hosts
```

The Traefik label

```yaml
- "traefik.http.routers.erpnet-lan.rule=...HostRegexp(`{sub:[a-z0-9-]+}.lan.mcpworks.net`)"
```

automatically routes ANY `<name>.lan.mcpworks.net` to the proxy. No
Traefik restart needed for new subdomains.

---

## Two deployment models

### Model A — On-prem ACME (current setup)

Each client machine runs Traefik with its own Cloudflare token. Pros:
fully autonomous renewal. Cons: every client needs the same token, OR
you generate per-client tokens.

Best when: 1–3 trusted machines you operate yourself.

### Model B — Centralized cert distribution (recommended for multi-client)

A single "cert-renewer" service (e.g. on `main 62.171.156.220`) runs
Traefik + ACME, holds the only Cloudflare token, and pushes the
renewed cert + key to every client machine via:

* `rsync` over SSH (cron weekly)
* Docker image rebuild — bake `cert.pem` + `key.pem` into a
  `cert-bundle:YYYY-MM-DD` image and `docker pull` on clients
* Webhook from Traefik renewal → `curl PUT /cert` to client

Client Traefik just reads static `/certs/cert.pem` + `key.pem` and
auto-reloads when files change (Traefik watches them with inotify).

Best when: 5+ shop-floor proxies you don't own.

This repo currently implements Model A. To switch to Model B, remove
the `certResolver: cf` block from `traefik.yml` and `docker-compose.yml`,
keep only the static cert mount in `dynamic.yml`, and arrange external
cert delivery into `./certs/`.

---

## Troubleshooting

### `defaultCertificate and defaultGeneratedCert cannot be defined at the same time`

Cosmetic warning if both are set in `tls.stores.default`. Either
remove `defaultGeneratedCert` (use static cert) or `defaultCertificate`
(let ACME provide everything). Current config keeps the static cert as
fallback for `erpnet.local` / `localhost` (which Let's Encrypt cannot
issue for) and ACME issues the wildcard separately via router rules.

### `unable to obtain ACME certificate ... timeout during the propagation`

Cloudflare token does not have DNS:Edit on the zone, OR the zone is
not the parent of the requested name. Check:

```bash
curl -s https://api.cloudflare.com/client/v4/user/tokens/verify \
  -H "Authorization: Bearer $CLOUDFLARE_DNS_API_TOKEN"
```

Should return `success: true`.

### `429 Too Many Requests` from Let's Encrypt

Production rate limits: 5 duplicate certs / week, 50 certs / week
per registered domain. If hit, switch to staging:

```yaml
# traefik.yml
certificatesResolvers:
  cf:
    acme:
      caServer: https://acme-staging-v02.api.letsencrypt.org/directory
```

Staging certs are NOT browser-trusted but exhaust no quota. Switch
back to production after debugging.

### Reset and re-issue

```bash
docker compose stop traefik
rm letsencrypt/acme.json
touch letsencrypt/acme.json && chmod 600 letsencrypt/acme.json
docker compose up -d traefik
```

Traefik re-registers ACME account and issues fresh certs.

---

## Security notes

* `.env` and `letsencrypt/` are gitignored — never commit
* Cloudflare token can edit DNS for the entire zone — limit scope
  to single zone in the dashboard
* `acme.json` contains the ACME account private key + issued cert
  private keys — protect like any other private-key file (mode 0600)
* If the token leaks, revoke at Cloudflare dashboard immediately,
  generate a new one, update `.env`, restart traefik
