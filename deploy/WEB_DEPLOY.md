# Deploying the twatch web UI (twatch.info) on ECS Fargate

The UI is a small container (`ghcr.io/silas-selfe/twatch-web`, ~230 MB) that
reads the central store with a read-only role and authenticates users against
`monitor_traffic.users`. No camera, no GPU. Target: **ECS on Fargate** behind
an Application Load Balancer with an ACM certificate for twatch.info.

Shape of the deployment:

```
twatch.info (DNS) -> ALB :443 (ACM cert) -> ECS Fargate task :8080 -> RDS (same VPC)
```

## 1. Database roles (once, as admin)

```sql
-- read-only login for the web app (schema came from webapp/schema.sql)
CREATE ROLE twatch_web LOGIN PASSWORD '<generate a long one>' IN ROLE tw_web;
```

Never put a real password in any file in this repo -- it is public. Generate
with `openssl rand -base64 24` and store it only in Secrets Manager (below).

Create UI users from your laptop (writes need an admin DSN):

```bash
TW_ADMIN_DSN='postgresql://<admin>@<rds-host>:5432/twatch?sslmode=require' \
  .venv/bin/python -m webapp.adduser silas
```

## 2. Secrets (once)

Console -> Secrets Manager -> Store a new secret (type: other), two entries:
- `twatch/web/dsn`    = `postgresql://twatch_web:...@<rds-host>:5432/twatch?sslmode=require`
- `twatch/web/secret` = output of `openssl rand -hex 32`

(Plain env vars on the task definition also work, but they're visible to
anyone who can read the task definition; secrets are the right habit.)

## 3. Certificate (once)

Console -> ACM (same region as the ALB) -> Request certificate ->
`twatch.info` + `www.twatch.info`, DNS validation. Add the two CNAME
validation records at your DNS host; wait for "Issued".

## 4. ECS cluster, task definition, service

**Cluster**: ECS -> Create cluster -> Fargate only, e.g. `twatch`.

**Task definition** (Fargate, Linux/X86_64 or ARM64 -- image is multi-arch):
- CPU `.25 vCPU`, memory `.5 GB`
- Container `web`, image `ghcr.io/silas-selfe/twatch-web:latest`
  (public on GHCR -- no registry auth needed), port mapping **8080/tcp**
- Environment -> from Secrets Manager:
  - `TW_CENTRAL_DSN`  <- `twatch/web/dsn`
  - `TW_SECRET_KEY`   <- `twatch/web/secret`
  (the console adds the required `secretsmanager:GetSecretValue` permission
  to the task execution role when you pick "ValueFrom")
- Logging: awslogs (the console default) so `docker logs` equivalents land
  in CloudWatch

**Service**: cluster -> Create service:
- Launch type Fargate, desired tasks **1**
- Networking: the VPC that can reach RDS; private subnets if you have a NAT
  (public subnets + public IP is fine to start -- the security group is the
  gate). Task security group: allow inbound 8080 **from the ALB's security
  group only**.
- Load balancing: create an **Application Load Balancer**, listener **443**
  with the ACM cert, plus a listener 80 that redirects to 443. Target group:
  type **IP**, port 8080, health check path **`/healthz`**.
- RDS security group: allow 5432 from the task security group. Once that
  works, you can turn OFF the RDS "publicly accessible" flag and do admin
  psql over a bastion/SSM tunnel -- the strictly better posture.

## 5. twatch.info DNS

The apex (`twatch.info`) needs an ALIAS/ANAME record to the ALB's DNS name;
plain CNAME is not allowed at an apex. Route 53 does this natively (alias
A-record), so the smoothest path is pointing the domain's nameservers at a
Route 53 hosted zone, then:
- `twatch.info`     -> Alias A -> the ALB
- `www.twatch.info` -> CNAME  -> the ALB DNS name

## 6. Smoke test

- `https://twatch.info/healthz` -> `{"ok": true}`
- Sign in; the fleet page shows `home-window` reporting with today's count.

## Updating the running UI

ECS does not watch `:latest`. After a new `twatch-web` image builds, roll
the service:

```bash
aws ecs update-service --cluster twatch --service twatch-web --force-new-deployment
```

That one-liner can live at the end of the GitHub Actions build job later
(GitHub OIDC -> a deploy role) so pushes to main roll the UI automatically,
same spirit as Watchtower on the nodes.

## Cost note & budget alternative

Fargate at .25 vCPU is a few dollars a month, but the **ALB is ~$16-20/mo**
-- it's the expensive part of this stack. If that stings for a hobby fleet:
**Lightsail Containers** (~$7/mo total) bundles HTTPS + custom domains with
no ALB, using this exact same image. ECS is the more standard, more scalable
answer; Lightsail is the cheaper one. The server rack + Cloudflare Tunnel
remains the $0 option. Nothing in the app assumes any particular host.
