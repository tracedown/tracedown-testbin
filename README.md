# tracedown-testbin

Deterministic HTTP target for testing Lace probes and the Tracedown
platform — an httpbin replacement tailored to cover **every** Lace assertion
scope, call-config field, and Tracedown pipeline behavior (notification
transitions, retries, baseline spikes, body storage, cookie jars, redirects,
timeouts, connection errors, TLS).

Tracedown is a self-hosted API monitoring platform — docs at
[tracedown.dev](https://tracedown.dev). The probe language is
[Lace](https://lacelang.dev).

Stack: FastAPI + uvicorn, no other dependencies. Three listeners:

| Port (env) | What |
|---|---|
| `20780` (`PORT`) | HTTP API |
| `20781` (`TLS_PORT`) | same API behind a **self-signed** certificate (`TLS_ENABLED`) |
| `20782` (`CHAOS_PORT`) | raw-TCP connection-failure modes (`CHAOS_ENABLED`) |

## Running

```bash
PYTHONPATH=src uvicorn main:app --host 0.0.0.0 --port 20780   # local
docker build -f docker/Dockerfile -t tracedown-testbin .      # container
docker compose -f docker/docker-compose.yml up -d --build      # standalone (all 3 listeners)
```

## Deploying (Railway / PaaS)

`railway.toml` forces the **Dockerfile** build — otherwise Railway's Railpack
buildpack auto-detects Python, looks for a root `main.py` (this app's
entrypoint lives under `src/`), and fails with "No start command". The image
honors the platform-injected `PORT`, so deploying the repo as-is works.

Recommended settings:

| Env | Value | Why |
|---|---|---|
| `ADMIN_TOKEN` | a secret | guards `/state` and rate-limit admin ops on a public host |
| `RATE_LIMIT_WHITELIST` | agent egress IP/CIDR | otherwise the probing agents self-ban on the first burst |
| `TLS_ENABLED` | `false` | PaaS routes exactly one HTTP port and already terminates real TLS — the self-signed listener is unreachable |
| `CHAOS_ENABLED` | `false` | raw-TCP listener is likewise unroutable over HTTP ingress; on Railway a TCP proxy targeting `CHAOS_PORT` can expose it if connection-error testing over the wire is needed |
| `WEB_CONCURRENCY` | `2`+ (optional) | extra uvicorn workers; requires TLS/chaos disabled (per-worker listeners would collide) — a single async worker already sustains hundreds of req/s on these endpoint shapes |

### Persistence (volume)

State is SQLite at `STATE_DB_PATH` (default under `/tmp`, i.e. **ephemeral**
on a PaaS — reset on every redeploy/restart). What that costs, and whether a
volume is needed:

- **Keyed endpoint state** (`/flap`, `/counter`, …) is test-run-scoped and
  expires after `STATE_TTL_SECONDS` anyway — losing it on restart is fine.
- **Allow/deny lists** live in the same SQLite. If the allow list is supplied
  via `RATE_LIMIT_WHITELIST`, it is **re-seeded on every boot**, so it
  survives restarts without a volume. Only rules added *at runtime* via the
  admin API are lost on restart.

So a volume is **optional**: seed the whitelist via env and none is needed.
To persist runtime-added rules and keyed state, mount a volume and point the
DB at it — on Railway, attach a volume at e.g. `/data` and set
`STATE_DB_PATH=/data/state.db`.

### Sustained load-test notes (probing this instance at scale)

Reference figures at 12 000 probes/min (≈ 200 req/s):

- **Compute**: trivial — the API is async and serves ~200 req/s at ~12% of
  one CPU. A small instance suffices.
- **Egress** dominates cost: at ~600 B/response, 200 req/s ≈ 0.12 MB/s ≈
  ~10 GB/day.
- **Connection churn**, not bandwidth, is the real network risk: probes do
  not reuse connections, so 200 req/s means 200 fresh TCP (+TLS) handshakes
  per second from one source IP, arriving as a burst at the top of every
  minute. Platform edges may rate-limit or flag this as abuse. Laddering the
  load (2k → 6k → 12k probes/min) while watching for 429s / connection resets
  is safer than starting at full rate.
- Over HTTPS each probe pays the platform-edge TLS handshake (~1–2 ms CPU on
  the probing agent, 2 extra RTTs of latency) — expect ~20–30% lower
  per-agent probe capacity than plain-HTTP LAN figures.

## Multi-tenancy

The API is safe to host publicly:

- Stateful endpoints are keyed by a **caller-chosen `{key}`** — use a unique
  value (UUID) per test run. State lives in SQLite (`STATE_DB_PATH`, default
  `/tmp/tracedown-testbin-state.db`) — bounded on disk rather than RAM,
  consistent across uvicorn workers, restart-safe. Entries expire after
  `STATE_TTL_SECONDS` (1h) and the table is capped at `STATE_MAX_KEYS`
  (10k, oldest-touched evicted).
- Global state listing/reset (`GET/DELETE /state`) require
  `Authorization: Bearer $ADMIN_TOKEN` when `ADMIN_TOKEN` is set.
- Expensive knobs are capped: `MAX_DELAY_MS` (120s), `MAX_BYTES` (25MB),
  `MAX_REDIRECT_HOPS` (20), `MAX_DRIP_CHUNKS` (100), `HANG_SECONDS` (300).
- `/redirect-to` accepts **relative targets only** (no open redirects).

### Rate limiting / auto-ban

On by default. Any IP exceeding **`RATE_LIMIT_RPS`** (100) requests in a
1-second window is auto-banned for **`RATE_LIMIT_BAN_SECONDS`** (3600) —
`429` with `Retry-After`. Denied IPs get `403`. The window counter and bans
are in-memory (per-process, bounded to `RATE_LIMIT_MAX_TRACKED` IPs);
`/health` is exempt.

Allow/deny lists are persisted in SQLite (the same DB as keyed state),
**runtime-mutable via the admin API** — no redeploy needed. Each worker holds
an in-memory snapshot refreshed every `RATE_LIMIT_RELOAD_SECONDS`, so an admin
change on one worker reaches the rest within that window.

| Env | Default | Purpose |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | master switch |
| `RATE_LIMIT_RPS` | `100` | per-IP per-second ceiling |
| `RATE_LIMIT_BAN_SECONDS` | `3600` | ban duration |
| `RATE_LIMIT_WHITELIST` | — | CSV seed of allow IPs/CIDRs (inserted on startup if absent) |
| `RATE_LIMIT_BLACKLIST` | — | CSV seed of deny IPs/CIDRs |
| `RATE_LIMIT_RELOAD_SECONDS` | `10` | how often a worker reloads the lists from SQLite |
| `TRUST_FORWARDED_FOR` | `true` | read client IP from `X-Forwarded-For` (required behind a PaaS proxy) |

**Allow-listing is the only bypass — no networks are trusted implicitly.**
A probing agent exceeds 100 req/s, so it must be allow-listed:

- **Load-testing a public instance**: add the agents' egress IP/CIDR to the
  allow list (obtain it with `curl ifconfig.me` from the agent host, or from
  any request's `X-Forwarded-For`) — seeded via `RATE_LIMIT_WHITELIST` at
  deploy time, or added live via the admin API.
- **Local docker stack / e2e**: agents connect over the docker network at
  hundreds of req/s. Either allow-list the bridge subnet
  (`RATE_LIMIT_WHITELIST=172.16.0.0/12`) or disable the limiter for the
  trusted local run:

  ```bash
  docker run ... -e RATE_LIMIT_ENABLED=false tracedown-testbin
  ```

#### Admin API (gated by `ADMIN_TOKEN`)

| Route | Action |
|---|---|
| `GET /ratelimit` | config, persisted `allow`/`deny` lists, active bans (ip → seconds left) |
| `POST /ratelimit/allow` · `POST /ratelimit/deny` | add a rule — body `{"cidr": "203.0.113.0/24"}` (bare IP → `/32`) |
| `DELETE /ratelimit/allow?cidr=…` · `DELETE /ratelimit/deny?cidr=…` | remove a rule |
| `DELETE /ratelimit/bans/{ip}` | lift one auto-ban |

Rules persist across restarts; bans do not (they self-expire).

## Endpoints

### Echo (headers/cookies/body config, `${}` interpolation, variables)
| Route | Behavior |
|---|---|
| `ANY /anything[/{path}]` | JSON echo: method, path, url, args, headers, cookies, origin, raw body, parsed json/form. All five Lace methods. |
| `GET /get` | httpbin-compatible echo (no body fields) |
| `GET /health` | `{"status":"ok"}` |

### Status control
| Route | Behavior |
|---|---|
| `ANY /status/{code}` | arbitrary status; `?body=`, `?location=/x` |
| `GET/POST /flip` | 200/500 by `success` param (JSON body, form, or query; accepts `true/false`, `0/1` and their strings); body returns `next` (the negation) so a probe can flip itself via writeback: `.store({ "$n": this.body.next })` |
| `GET /random?p=0.5&codes=200,500` | codes[0] with probability p, else random rest |
| `GET /flap/{key}?codes=200,500` | cycles codes one step per request — state transitions, `silentOnRepeat`, `prev` |
| `GET /sequence/{key}?codes=200,200,500` | plays codes once, holds the last |
| `GET /fail-then-succeed/{key}?fails=2&code=500[&slow_ms=]` | N failures (or N slow responses) then 200 — `timeout: {action: "retry"}` |

### Timing (totalDelayMs / ttfb / transfer scopes, timeouts, laceBaseline)
| Route | Behavior |
|---|---|
| `GET /delay/{ms}?status=&bytes=` | sleep, then respond |
| `GET /ttfb/{ms}?bytes=` | hold first byte only |
| `GET /drip?duration_ms=&bytes=&chunks=` | headers instantly, body dripped — transfer ≫ ttfb |
| `GET /hang` | never answers within probe timeouts |
| `GET /spike/{key}?after=5&fast_ms=10&slow_ms=2000&grow_bytes=` | fast for N hits, then slow/bigger — baseline spike |

### Bodies / size / schema
| Route | Behavior |
|---|---|
| `GET /bytes/{n}` | exactly n deterministic bytes (`size: eq`, `bodySize`, bodyTooLarge, storage checksums) |
| `GET /raw?body=OK&content_type=text/plain` | byte-exact raw body (literal match) |
| `GET /json/{exact\|extra\|wrong\|nested}` | schema-testing payloads, see below |
| `GET /uuid` | fresh `{"uuid"}` per call (`.store()` writeback) |
| `GET /counter/{key}` | incrementing `{"count"}` (metric presets, prev) |
| `POST /login` | credential-checked login (JSON/form `{username, password}`): any creds pass except password `invalid`/missing → 401. Returns `{token, user}` |
| `GET /token` → `GET /protected` | two-call auth chain (`Bearer` echo, else 401) |

Reference schema satisfied *strictly* by `/json/exact` (`extra` adds fields →
loose passes / strict fails; `wrong` makes `checks` a string; `nested` adds
depth):

```json
{
  "type": "object",
  "properties": {
    "service": {
      "type": "object",
      "properties": {
        "name":    {"type": "string"},
        "healthy": {"type": "boolean"},
        "checks":  {"type": "integer"}
      },
      "required": ["name", "healthy", "checks"]
    },
    "tags": {"type": "array", "items": {"type": "string"}}
  },
  "required": ["service", "tags"]
}
```

### Cookies (jar modes: inherit / fresh / named / selective_clear)
| Route | Behavior |
|---|---|
| `GET /cookies` | echoes inbound cookies |
| `GET /cookies/set?a=1&b=2` | Set-Cookie per query param |
| `GET /cookies/delete?a=` | expires named cookies |

### Redirects (redirects scope, follow/max, match first/last/any)
| Route | Behavior |
|---|---|
| `GET /redirect/{n}?absolute=&code=302` | n-hop chain → 200 at `/anything` |
| `GET /redirect-to?url=/x&code=302` | single hop, relative targets only |
| `GET /redirect-loop` | self-loop — exceeds any `redirects.max` |

### Connection errors (laceNotifications `error` trigger)
Raw-TCP listener on `CHAOS_PORT` (`GET /chaos` documents it):
`/rst` (TCP reset), `/empty` (close before response), `/partial` (half a
head, then close). Connection-*refused*: target any unused port.

### State
`GET/DELETE /state/{key}` (own-key ops, open) ·
`GET/DELETE /state` (global, admin-gated when `ADMIN_TOKEN` set)

## Example Lace scripts

```lace
// assertion failure + notification on transition (silentOnRepeat default)
get("http://tracedown-testbin:20780/flap/my-unique-key")
.expect(status: 200)

// timeout outcome
get("http://tracedown-testbin:20780/delay/5000", { timeout: { ms: 3000, action: "fail" } })
.expect(status: 200)

// connection-error trigger
get("http://tracedown-testbin:20782/rst")
.expect(status: 200)

// writeback
get("http://tracedown-testbin:20780/uuid")
.expect(status: 200)
.store({ "$lastUuid": this.body.uuid })

// TLS metadata (self-signed → rejectInvalidCerts must be false)
get("https://tracedown-testbin:20781/get", { security: { rejectInvalidCerts: false } })
.expect(status: 200, tls: { value: 5000, op: "lt" })
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
