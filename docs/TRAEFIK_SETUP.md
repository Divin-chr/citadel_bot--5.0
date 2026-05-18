# Traefik Setup for Citadel Bot

This repository now includes a Traefik configuration to route the Citadel Bot application and dashboard through a secure reverse proxy.

## What is included

- `docker-compose.yml` — launches Traefik and the Citadel Bot application
- `traefik/traefik.yml` — Traefik static configuration
- `traefik/dynamic.yml` — Traefik dynamic routes for dashboard and API
- `traefik/acme.json` — ACME storage file placeholder

## Local secure usage

1. Install `mkcert` for local trusted certificates:
   - https://github.com/FiloSottile/mkcert

2. Generate certificates for local hostnames:

```powershell
mkcert -install
mkcert dashboard.localhost api.localhost
```

3. Place the generated cert and key files in `traefik/certs` and rename them to:

- `dashboard-localhost.pem`
- `dashboard-localhost-key.pem`

4. Start the stack:

```powershell
docker compose up --build
```

5. Open the secure dashboard:

- `https://app.localhost/dashboard`

6. Open the secure API host:

- `https://app.localhost/api/status`

## Optional Local Dev Override

Use `docker-compose.override.yml` for a local dev setup that keeps the same single-domain path routing.

```powershell
docker compose up --build
```

This override updates the dashboard app to use:
- `https://app.localhost/dashboard`
- `https://app.localhost/api/status`

It also configures the dashboard to call the local control API on `http://127.0.0.1:8765/api` from inside the container.

## VPS deployment

For a production VPS deployment, use a single public domain and path-based routing.

1. Set DNS records:
   - `app.yourdomain.com` → your VPS public IP

2. Update `docker-compose.yml` and `traefik/dynamic.yml` with your actual domain.

3. Open firewall ports `80` and `443` only.

4. Start the stack:

```bash
docker compose up --build
```

5. Verify the deployment in a browser:

- `https://app.yourdomain.com/dashboard`
- `https://app.yourdomain.com/api/status`

## Notes

- The Traefik router now uses a single domain with path-based routing on `/dashboard` and `/api`.
- If you want public TLS with Let's Encrypt, set a real domain and update `traefik/traefik.yml` with a valid email.
- Keep `CITADEL_CONTROL_API_KEY` secret and rotate it regularly.

## Recommended environment updates

- Use a strong value for `CITADEL_CONTROL_API_KEY`
- If you deploy Traefik publicly, add `TLS` and `HTTP Strict Transport Security` policies.
