# Binding any agent / model to the Computer Runtime

Inference stays **outside** this project. Your agent leases a desktop, loops (screenshot → model → action), then releases.

Default display: **1280×800**. Gateway: `http://localhost:9000` (Compose).

## 1. Auth

```bash
export COMPUTER_API_TOKEN=dev-token
export GW=http://localhost:9000
export AUTH="Authorization: Bearer $COMPUTER_API_TOKEN"
```

## 2. Lease → act → release

```bash
# Lease
SESSION=$(curl -s -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"agent_id":"demo","ttl_sec":600}' \
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

# Release (recycles Firefox on that worker)
curl -s -H "$AUTH" -X DELETE "$GW/v1/sessions/$SID"
```

When the pool is full, `POST /v1/sessions` returns **503** — retry or wait for a free seat.

## 3. Agent loop (pseudocode)

```
session = POST /v1/sessions
try:
  while not done:
    shot = GET  .../screenshot          # → { image: base64 }
    decision = your_model(shot, tools)  # any vendor
    if decision is tool call:
      map tool → POST .../action  (or GET screenshot)
    POST .../heartbeat periodically
finally:
  DELETE /v1/sessions/{id}
```

OpenAPI live docs: `$GW/docs` · schema: `$GW/openapi.json`

## 4. Tool schemas (drop into your host)

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

## 5. Minimal Python bind

```python
import os, httpx

GW = os.environ.get("GW", "http://localhost:9000")
TOKEN = os.environ["COMPUTER_API_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}"}

with httpx.Client(base_url=GW, headers=H, timeout=60) as c:
    lease = c.post("/v1/sessions", json={"agent_id": "py"}).raise_for_status().json()
    sid = lease["session_id"]
    try:
        img = c.get(f"/v1/sessions/{sid}/screenshot").json()["image"]
        # … pass img to your model …
        c.post(
            f"/v1/sessions/{sid}/action",
            json={"type": "key", "text": "ctrl+l"},
        ).raise_for_status()
    finally:
        c.delete(f"/v1/sessions/{sid}")
```

## 6. Minimal fetch bind

```js
const GW = process.env.GW ?? "http://localhost:9000";
const auth = { Authorization: `Bearer ${process.env.COMPUTER_API_TOKEN}` };

const lease = await fetch(`${GW}/v1/sessions`, {
  method: "POST",
  headers: { ...auth, "Content-Type": "application/json" },
  body: JSON.stringify({ agent_id: "js" }),
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
  await fetch(`${GW}/v1/sessions/${sid}`, { method: "DELETE", headers: auth });
}
```

## 7. Ops tips

- Prefer `lease.api_base` from the lease response as the session prefix.
- Send `X-Job-Id` on requests for gateway logs.
- Open `vnc_url` for human watch / HITL; do not expose worker `:8000` publicly.
- Compose: `docker compose up --build` then bind against port **9000**.
