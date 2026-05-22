# Kubernetes deployment for Odoo.ErpNet.FP proxy

Manifests for a k3s-based hardware-proxy stack. Targets the MEC cluster
(`srv03` control plane at `192.168.3.118`), but the manifests are
generic — just replace the namespace + hostnames in your `kustomization`
or in-place sed.

## Topology

```
                          Internet
                              │
                    ┌─────────▼─────────┐
                    │ Cloudflare tunnel │   (terminates HTTPS)
                    │ (cloudflare-tunnel │
                    │  namespace)        │
                    └─────────┬─────────┘
                              │ HTTP
                  ┌───────────▼────────────┐
                  │ Traefik (k3s built-in) │
                  │ Ingress: HTTP only      │
                  └───────────┬────────────┘
                              │
                  ┌───────────▼────────────┐
                  │  svc-erpnet-fp         │  ClusterIP :80 → :8001
                  └───────────┬────────────┘
                              │
                              │           ┌──────── LAN-only ─────────┐
                              │           │                            │
                  ┌───────────▼────────────┐       ┌──────────────────┐│
                  │   deployment.erpnet-fp │       │ svc-erpnet-fp-lan ││
                  │   pod: odoo-erpnet-fp  │◀──────│ LoadBalancer :80 │┘
                  │   container port :8001 │       │ (svclb-traefik)  │
                  └───────────┬────────────┘       └──────────────────┘
                              │
                              ▼
                   Polimex iCON @ 192.168.3.151    (talks plain HTTP)
                   Fleet receiver @ iot.mcpworks.net (talks HTTPS)
```

Both HTTP and HTTPS are accepted on different surfaces:
- **External HTTPS** — `https://fp-mec.odoo-shell.space` (Cloudflare-terminated)
- **LAN HTTP** — `http://<lb-ip>/` for the Polimex bridge to POST to

The pod itself listens on plain HTTP 8001; TLS is handled by Cloudflare
and Traefik upstream, never inside the container.

## Files (apply order)

| File | What |
|------|------|
| `00-namespace.yaml` | namespace `erpnet-fp-mec` |
| `10-pvc.yaml`       | 1Gi local-path PVC for `/app/data` (admin_token, registry_secret, rescue_token) |
| `20-configmap.yaml` | `config.yaml` — registry pointed at iot.mcpworks.net |
| `30-secret.example.yaml` | Operator copies → `30-secret.yaml` + fills `ERPNET_FP_RESCUE_TOKEN` |
| `40-deployment.yaml` | `vladimirovrosen/odoo-erpnet-fp:0.9.0` |
| `50-service.yaml`   | 2 services: ClusterIP (for Ingress) + LoadBalancer (for LAN) |
| `60-ingress.yaml`   | Traefik Ingress for `fp-mec.odoo-shell.space` |

## Apply

```bash
# From an operator workstation with `kubectl` configured against srv03
cd kubernetes/
cp 30-secret.example.yaml 30-secret.yaml
$EDITOR 30-secret.yaml      # set ERPNET_FP_RESCUE_TOKEN (master rescue token)
kubectl apply -f 00-namespace.yaml
kubectl apply -f .
kubectl -n erpnet-fp-mec rollout status deploy/erpnet-fp
```

## Verify

```bash
# Pod is up and healthz works
kubectl -n erpnet-fp-mec port-forward svc/erpnet-fp 8001:80 &
curl -s http://127.0.0.1:8001/healthz | jq .

# External (Cloudflare path)
curl -s https://fp-mec.odoo-shell.space/healthz | jq .

# LAN LoadBalancer (whatever IP MetalLB / svclb assigned)
LBIP=$(kubectl -n erpnet-fp-mec get svc erpnet-fp-lan -o jsonpath='{.status.loadBalancer.ingress[0].ip}')
curl -s http://$LBIP/healthz | jq .
```

The proxy auto-enrols on its first heartbeat to `iot.mcpworks.net`
(see config.yaml `server.registry`). It should appear in
**Fleet → Proxies** within 60 s of pod start, named `erpnet-fp-mec`.

## Update to a new image

```bash
kubectl -n erpnet-fp-mec set image \
  deploy/erpnet-fp \
  proxy=vladimirovrosen/odoo-erpnet-fp:0.10.0
```

Or via the proxy's own landing-page `Self-update` button (it spawns a
sibling watchtower-style updater — for k8s, the `kubectl set image`
path is simpler and rolls cleanly).

## Configure Polimex

Set the Polimex Web Module's outbound URL (in its HTTP-push mode) or
its SDK accessor (in pull mode) to point at the LAN LoadBalancer IP:

  - Pull mode: nothing on the Polimex side — proxy talks to it.
  - Push mode: set Polimex `HTTP Push URL` to `http://<lb-ip>/polimex/heartbeat`
    (endpoint lands in proxy v0.10+).
