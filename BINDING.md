# Binding any agent / model to the Computer Runtime

Inference stays **outside** this project. Your agent leases a desktop, loops (screenshot → model → action), then soft-releases (or recycles when the job is done).

Default display: **1280×800**. Gateway: `http://localhost:9000` (Compose).

## Lease vs park vs recycle

| Concept | What it means |
|---------|----------------|
| **Lease** | Exclusive mouse/keyboard claim on a worker (`POST /v1/sessions`). |
| **Park** | Soft `DELETE` — drop the claim, **keep Firefox warm** for the same `session_id` / `job_id`. Worker is not free for other jobs. |
| **Acquire / reattach** | Take the claim again: `POST …/acquire` (by `session_id`) or `POST /v1/sessions` with the same `job_id`. |
| **Recycle** | Wipe Firefox and free the worker: `DELETE …?recycle=true` or `POST …/recycle`. Also happens after parked idle (`PARKED_TTL_SEC`, default 1800s). |

Lease TTL expiry **parks** (does not wipe). Use recycle when the job is truly finished so the next unrelated lease gets a clean browser.

Workers use a dedicated `relay-agent` Firefox profile; recycle / `/session/reset` clears session-restore artifacts so “Restore Session” does not appear.

## 1. Auth

```bash
export COMPUTER_API_TOKEN=dev-token
export GW=http://localhost:9000
export AUTH="Authorization: Bearer $COMPUTER_API_TOKEN"
```

## 2. Lease → act → soft release

```bash
# Lease
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","job_id":"demo-job","ttl_sec":600}' \
  "$GW/v1/sessions")
echo "$SESSION" | jq .
SID=$(echo "$SESSION" | jq -r .session_id)
VNC=$(echo "$SESSION" | jq -r .vnc_url)
echo "Watch: $VNC"

# Screenshot (base64 PNG in .image)
curl -s -H "$AUTH" "$GW/v1/sessions/$SID/screenshot" | jq -r .image | head -c 64
echo …

# Focus Firefox address bar and open a URL
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"ctrl+l"}' \
  "$GW/v1/sessions/$SID/action"
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"type","text":"https://example.com"}' \
  "$GW/v1/sessions/$SID/action"
curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"type":"key","text":"Return"}' \
  "$GW/v1/sessions/$SID/action"

# Heartbeat if the job is long
curl -s -H "$AUTH" -X POST "$GW/v1/sessions/$SID/heartbeat"

# Soft release — parks the warm desktop (tabs/cookies kept)
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID"
# → { "ok": true, "session_id": "...", "parked": true }
```

When the pool has no **free** workers (parked seats do not count as free), `POST /v1/sessions` returns **503** — retry, acquire an existing park, or wait for recycle / parked TTL.

## 3. Serial leases (same job)

Keep the desktop across claim gaps (HITL pause, staged steps):

```bash
# … after soft DELETE above …

# Option A — same session_id
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"ttl_sec":600}' \
  -X POST "$GW/v1/sessions/$SID/acquire")
echo "$SESSION" | jq .

# Option B — same job_id (reattaches the parked desktop)
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","job_id":"demo-job","ttl_sec":600}' \
  "$GW/v1/sessions")
SID=$(echo "$SESSION" | jq -r .session_id)

# … more act/screenshot …

# Job finished — wipe Firefox and free the worker
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID?recycle=true"
# or: curl -s -H "$AUTH" -X POST "$GW/v1/sessions/$SID/recycle"
```

Mid-lease wipe without releasing: `POST /v1/sessions/$SID/reset`.

## 4. Agent loop (pseudocode)

```
session = POST /v1/sessions   # optional job_id for reattach
try:
  while not done:
    if need_to_yield_claim:          # HITL / pause
      DELETE /v1/sessions/{id}       # park
      # later: POST …/acquire  or  POST /v1/sessions { job_id }
    shot = GET  .../screenshot
    decision = your_model(shot, tools)
    if decision is tool call:
      map tool → POST .../action
    POST .../heartbeat periodically
finally:
  DELETE /v1/sessions/{id}?recycle=true   # job done
```

OpenAPI live docs: `$GW/docs` · schema: `$GW/openapi.json`

## 5. Tool schemas (drop into your host)

| File | Use when |
|------|----------|
| [`schemas/openai-tools.json`](schemas/openai-tools.json) | OpenAI / compatible `tools` function calling |
| [`schemas/anthropic-computer-use.json`](schemas/anthropic-computer-use.json) | Map Anthropic-style `action` names → gateway bodies |

OpenAI-shaped tools expose only the `function` objects to the model; use each tool’s `http` hint in your executor.

Example executor mapping:

| Tool / Anthropic action | Gateway |
|-------------------------|---------|
| `computer_screenshot` / `screenshot` | `GET .../screenshot` |
| `computer_click` / `left_click` | `{ "type":"click", "x", "y" }` |
| `computer_type` / `type` | `{ "type":"type", "text" }` |
| `computer_key` / `key` | `{ "type":"key", "text" }` |
| `computer_scroll` / `scroll` | `{ "type":"scroll", "direction", "amount" }` |

## 6. Minimal Python bind

```python
import os, httpx

GW = os.environ.get("GW", "http://localhost:9000")
TOKEN = os.environ["COMPUTER_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

with httpx.Client(base_url=GW, headers=H, timeout=60) as c:
    lease = c.post(
        "/v1/sessions",
        json={"agent_id": "py", "job_id": "py-job"},
    ).raise_for_status().json()
    sid = lease["session_id"]
    try:
        img = c.get(f"/v1/sessions/{sid}/screenshot").json()["image"]
        # … pass img to your model …
        c.post(
            f"/v1/sessions/{sid}/action",
            json={"type": "key", "text": "ctrl+l"},
        ).raise_for_status()
    finally:
        # Soft park: c.delete(f"/v1/sessions/{sid}")
        # Job done — wipe:
        c.delete(f"/v1/sessions/{sid}", params={"recycle": "true"})
```

## 7. Minimal fetch bind

```js
const GW = process.env.GW ?? "http://localhost:9000";
const auth = { Authorization: `Bearer ${process.env.COMPUTER_API_TOKEN}` };

const lease = await fetch(`${GW}/v1/sessions`, {
  method: "POST",
  headers: { ...auth, "Content-Type": "application/json" },
  body: JSON.stringify({ agent_id: "js", job_id: "js-job" }),
}).then((r) => r.json());

const sid = lease.session_id;
try {
  const { image } = await fetch(`${GW}/v1/sessions/${sid}/screenshot`, {
    headers: auth,
  }).then((r) => r.json());
  // … model …
  await fetch(`${GW}/v1/sessions/${sid}/action`, {
    method: "POST",
    headers: { ...auth, "Content-Type": "application/json" },
    body: JSON.stringify({ type: "click", x: 100, y: 200 }),
  });
} finally {
  await fetch(`${GW}/v1/sessions/${sid}?recycle=true`, {
    method: "DELETE",
    headers: auth,
  });
}
```

## 8. Ops tips

- Prefer `lease.api_base` from the lease response as the session prefix.
- Send `X-Job-Id` (or body `job_id`) so serial steps can reattach and logs correlate.
- Open `vnc_url` for human watch / HITL; do not expose worker `:8000` publicly.
- `/health` and `/ready` report `free` / `leased` / `parked` — only **free** seats accept unrelated new leases.
- `GET /v1/seats` (auth) lists each worker with `state`, `session_id`, `vnc_url`, `job_id` so operators can Watch / Acquire / Recycle abandoned parks without guessing ids.
- Compose: `docker compose up --build` then bind against port **9000**.
