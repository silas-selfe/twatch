# Deploying the twatch web UI (twatch.info)

The UI is a small container (`ghcr.io/silas-selfe/twatch-web`) that reads the
central store with a read-only role and authenticates users against
`monitor_traffic.users`. No camera, no GPU -- it runs anywhere. Recommended:
AWS App Runner (lowest-ops container hosting, managed TLS, custom domains).

## 1. Database roles (once, as admin)

```sql
-- read-only login for the web app (schema came from webapp/schema.sql)
CREATE ROLE twatch_web LOGIN PASSWORD '...' IN ROLE tw_web;
```

Create UI users from your laptop (writes need an admin DSN):

```bash
TW_ADMIN_DSN='postgresql://<admin>@<rds-host>:5432/twatch?sslmode=require' \
  .venv/bin/python -m webapp.adduser silas
```

## 2. App Runner service

Console -> App Runner -> Create service:
- Source: **Container registry -> ghcr.io/silas-selfe/twatch-web:latest**
  (public image, no registry credentials needed)
- Port: **8080**  |  CPU/mem: 0.25 vCPU / 0.5 GB is plenty
- Environment variables:
  - `TW_CENTRAL_DSN` = `postgresql://twatch_web:...@<rds-host>:5432/twatch?sslmode=require`
  - `TW_SECRET_KEY`  = a long random string (`openssl rand -hex 32`)
- Health check path: `/healthz`
- Networking: if the RDS instance is not publicly accessible, attach the
  service to your VPC (VPC connector) so it can reach the DB privately --
  this is the better posture anyway: with the web UI inside the VPC, RDS
  can drop public accessibility entirely.

App Runner does not auto-pull `:latest`; either enable its automatic
deployments (ECR only) or redeploy after image updates. Simplest cron-free
option: the "Deploy" button, or `aws apprunner start-deployment` in a GitHub
Actions step when twatch-web builds.

## 3. twatch.info DNS

App Runner -> Custom domains -> add `twatch.info` (and `www`). It gives you
CNAME/ALIAS records + certificate validation records; add them at your DNS
host (or Route 53 if you move the zone there). TLS is managed for you.

## 4. Smoke test

- `https://twatch.info/healthz` -> `{"ok": true}`
- Sign in, fleet page shows `home-window` reporting with today's count.

## Alternative hosts

Same image runs on Lightsail Containers (~$7/mo), ECS Fargate, or the
downstairs server rack via compose + watchtower behind a Cloudflare Tunnel.
Nothing in the app assumes App Runner.
