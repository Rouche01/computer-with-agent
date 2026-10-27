# AGENTS.md — Computer Runtime

Guidance for agents working in this repo. Inference stays **outside**; this project is a model-agnostic desktop control plane.

## What this is

Self-hosted **computer-use** stack: warm Linux desktops (XFCE + Firefox) behind a **gateway** that leases exclusive sessions. Agents call HTTP (screenshot / mouse / keyboard); humans watch via noVNC.

Sibling consumer: `~/Projects/relay-os` (`@relay/engines-computer`, CommunityEngager `REDDIT_TRANSPORT=computer`).

## Layout

| Path | Role |
|------|------|
| `Dockerfile` + `entrypoint.sh` + `server/app.py` | **Worker** — Xvfb, desktop, noVNC `:6080`, control API `:8000` |
| `Dockerfile.gateway` + `gateway/` | **Gateway** — lease/park/release, proxy, Bearer auth, OpenAPI `:9000` |
| `docker-compose.yml` | `gateway` + `worker-1` + `worker-2` on `computer_internal` |
| `schemas/` + `BINDING.md` | OpenAI / Anthropic-shaped tool maps for any agent host |
| `profile/` + `scripts/` | Firefox profile / launch helpers |

## Run (local)

```bash
cp -n .env.example .env
docker compose up --build -d
curl -s http://localhost:9000/health
curl -s -H "Authorization: Bearer $COMPUTER_API_TOKEN" -X POST http://localhost:9000/v1/sessions
```

- Gateway: `http://localhost:9000` (`/docs`, `/openapi.json`)
- noVNC: `http://localhost:6081/vnc.html?…` and `:6082` (lease `vnc_url` includes `autoconnect=1&resize=scale`)

## Agent-facing API (gateway)

Base: `/v1` · Auth: `Authorization: Bearer $COMPUTER_API_TOKEN` (optional if token unset — dev only).

| Method | Path | Notes |
|--------|------|--------|
| `POST` | `/v1/sessions` | Lease a free worker |
| `POST` | `/v1/sessions/{id}/heartbeat` | Extend lease TTL |
| `POST` | `/v1/sessions/{id}/acquire` | Re-lease a **parked** desktop |
| `DELETE` | `/v1/sessions/{id}` | Soft release → **park** (warm); `?recycle=true` wipes + frees |
| `POST` | `/v1/sessions/{id}/recycle` | Hard wipe + free |
| `GET` | `/v1/sessions/{id}/screenshot` | Base64 PNG |
| `POST` | `/v1/sessions/{id}/action` | Forward to worker (`click`, `type`, `key`, …) |
| `GET` | `/v1/seats` | Per-worker inventory (`free`/`leased`/`parked` + `session_id`/`vnc_url`/`job_id`) |
| `GET` | `/health` · `/ready` | Ops aggregates |

Worker (internal): `GET /health`, `GET /screenshot`, `POST /action`, `POST /session/reset`.

Pool states: **free** → **leased** → **parked** (idle TTL) → free after reset. Capacity is **fixed** (Compose workers), not on-demand.

## Conventions

- **Do not** put LLM calls in the gateway or worker. Bind models via `BINDING.md` / `schemas/`.
- Prefer **warm pool + park/reset** over spinning containers or K8s unless capacity/isolation forces it.
- Keep worker `:8000` off the public/tailnet face; publish noVNC + gateway via Caddy (or localhost only).
- Display: set **`ASPECT_RATIO`** (e.g. `16:9`, `16:10`) + **`WIDTH`**; `entrypoint.sh` derives `HEIGHT` (default `16:10` → 1280×800). Explicit `WIDTH`+`HEIGHT` with empty `ASPECT_RATIO` still works. VNC uses **local** `resize=scale`, not remote desktop resize.
- Gateway injects `autoconnect=1&resize=scale` on lease `vnc_url` if missing.

### Env that matters

| Variable | Meaning |
|----------|---------|
| `COMPUTER_API_TOKEN` | Bearer for gateway |
| `GATEWAY_PUBLIC_BASE` | Origin baked into `api_base` (e.g. `http://desk.hsv` on ThinkPad) |
| `WORKER_URLS` | Internal worker APIs (Compose: `http://worker-1:8000,…`) |
| `WORKER_VNC_URLS` | Browser-reachable noVNC URLs (parallel list; use `.hsv` hostnames in deploy) |
| `SESSION_TTL_SEC` / `PARKED_TTL_SEC` | Lease and parked idle TTLs |

## ThinkPad / Caddy (`.hsv`)

Match relay-os deploy style: backends on `127.0.0.1`, Caddy Host routing, laptop `/etc/hosts` → Tailscale IP.

Suggested names: **`desk.hsv`** (gateway), **`desk1.hsv`** / **`desk2.hsv`** (noVNC).

On deploy hosts, bind Compose ports to loopback:

```yaml
ports:
  - "127.0.0.1:9000:9000"
  - "127.0.0.1:6081:6080"
  - "127.0.0.1:6082:6080"
```

Set `GATEWAY_PUBLIC_BASE` / `WORKER_VNC_URLS` to those hostnames. Leave `WORKER_URLS` as Docker DNS.

## Relay OS

- Package: `@relay/engines-computer` (`ExecutionEngine` type `"computer"`)
- CommunityEngager: `REDDIT_TRANSPORT=computer` is **evidence-only** (lease → open thread URL → screenshot → release; **never posts**). Needs `COMMUNITY_POST_MODE=auto` to reach execute; `COMPUTER_GATEWAY_URL` + matching token.

## Out of scope (for now)

- On-demand container/Pod per lease
- Gateway-hosted vision/LLM loops
- Auto CAPTCHA solve
- Reliable Cmd+V into noVNC canvas (use `type` action or noVNC clipboard UI)

## Quick bind loop

```
POST /v1/sessions → screenshot → model → POST .../action → … → DELETE /v1/sessions/{id}
```

See `BINDING.md` for curl / Python / fetch examples and tool JSON.
