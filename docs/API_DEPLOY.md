# LP Bot API — Deploy Guide

The LP bot's dashboard API runs as a separate supervisord process alongside `lp_bot`. It serves on **port 5002** (auto-sniper is on 5001).

## What's in this commit

```
lp-bot/
├── api.py                  ← FastAPI service: /healthz, /api/lp/summary, /api/lp/logs
├── api.supervisor.conf     ← supervisor program block for systemd-style management
└── requirements.txt        ← +fastapi, +uvicorn[standard]
```

## One-time deploy

### 1. Pull the new files to the droplet

```bash
ssh root@143.110.183.157
cd /root/doma-automation/lp-bot
git pull   # or scp the three files manually if no repo
```

### 2. Install the new Python deps

```bash
/root/doma-automation/venv/bin/pip install -r requirements.txt
```

### 3. Generate an API key and add it to the LP bot's `.env`

```bash
echo "LP_API_KEY=$(openssl rand -hex 32)" >> /root/doma-automation/lp-bot/.env
```

If the log file path differs from the default, also add:
```
LP_LOG_PATH=/var/log/lp_bot.out.log
```

### 4. Wire up supervisor

```bash
cp /root/doma-automation/lp-bot/api.supervisor.conf /etc/supervisor/conf.d/lp_api.conf
supervisorctl reread
supervisorctl update
supervisorctl start lp_api
supervisorctl status lp_api
```

You should see something like:
```
lp_api    RUNNING   pid 12345, uptime 0:00:05
```

### 5. Smoke test from the droplet

```bash
# Public health check
curl http://localhost:5002/healthz

# Authenticated summary
curl -H "X-API-Key: $(grep ^LP_API_KEY /root/doma-automation/lp-bot/.env | cut -d= -f2)" \
     http://localhost:5002/api/lp/summary | jq '.state'

# Logs
curl -H "X-API-Key: ..." "http://localhost:5002/api/lp/logs?lines=20"
```

### 6. Open the firewall (if needed)

If the droplet has a firewall and the dashboard's Vercel proxy can't reach 5002:

```bash
ufw allow 5002/tcp
```

(The auto-sniper is on 5001, so 5002 needs separate clearance.)

### 7. Add the key to Vercel

In the Vercel dashboard for `web3guides`:

- `LP_BOT_API_KEY` = the value of `LP_API_KEY` from step 3
- `LP_BOT_BASE` = `http://143.110.183.157:5002`

Redeploy. The LP panel on `/dashboard` should light up within 30 seconds.

---

## What each env var does

| Var | Where | Purpose |
|---|---|---|
| `LP_API_KEY` | LP bot `.env` on droplet | Required. The shared secret. |
| `LP_LOG_PATH` | LP bot `.env` on droplet | Optional. Defaults to `/var/log/lp_bot.out.log`. |
| `LP_BOT_API_KEY` | Vercel env | Same value as `LP_API_KEY`. Read by the Next.js proxy. |
| `LP_BOT_BASE` | Vercel env | `http://143.110.183.157:5002`. Override only if API moves. |

**Tip:** the dashboard proxy will fall back to `BOT_API_KEY` if `LP_BOT_API_KEY` is unset, but they're separate bots — keep the keys distinct so you can rotate either independently.

---

## Endpoint reference

### `GET /healthz`
Unauthenticated. For uptime monitoring.
```json
{ "ok": true, "service": "lp_bot_api", "version": "1.0.0" }
```

### `GET /api/lp/summary`  · header `X-API-Key`
Returns the full `LPSummary` payload defined in `LP_BOT_DASHBOARD_CONTEXT.md` § 6.

State derives from:
- **DB** (`lp_bot.db`): historical positions, rebalances, fee_collections
- **Chain**: live position liquidity / uncollected fees via `position_manager.get_position()`
- **Pool**: current price + tick via `pool_state.read_pool_state()`
- **Wallet**: USDC.e + LP token balances via ERC20 `balanceOf`

Failure modes:
- Pool RPC fails → returns the empty-state shape with a `note` field, never 500
- Single position fails to read → that position gets a stub entry with `error`, others continue
- DB doesn't exist yet → `init_db()` runs at startup and creates empty tables
- Wallet/key not set → bubbles up as 500 (config error, fix `.env`)

### `GET /api/lp/logs?lines=100`  · header `X-API-Key`
Tails the supervisord stdout log. `lines` clamped to `[1, 2000]`. Returns oldest-first; the dashboard reverses for newest-first display.

```json
{
  "process": "lp_bot",
  "lines": ["2026-04-26 ...", "2026-04-26 ..."]
}
```

---

## Troubleshooting

**`lp_api` won't start**
```bash
tail -50 /var/log/lp_api.err.log
```
Most common: `LP_API_KEY` not in `.env`, or `uvicorn` not installed in the venv.

**API returns 401 from curl**
The header is **case-sensitive** in some setups — use exactly `X-API-Key`. Check the value matches `.env`:
```bash
grep ^LP_API_KEY /root/doma-automation/lp-bot/.env
```

**Dashboard shows "LP bot unreachable"**
1. `curl` the endpoint from the droplet — works?
2. `curl` it from your laptop — works? (firewall check)
3. Vercel env vars set and project redeployed?

**Position shows liquidity but everything else is zero**
The on-chain read for that NFT failed — check `lp_api.err.log` for the exception. Position will have an `error` field in the response.

**`lp_bot.db` not writeable**
Make sure both processes (`lp_bot` and `lp_api`) run as the same user (`root` per the supervisor configs) so neither hits permission errors writing/reading the DB.

---

## Operational notes

- The API does **read-only** queries against the DB. It will not mutate state.
- `init_db()` runs at startup, but it uses `CREATE TABLE IF NOT EXISTS` — safe even when `lp_bot` is also running.
- One web3 connection is reused (lazy-init). If the RPC drops, the connection is recreated on next request via the `connect()` helper.
- The summary endpoint reads several on-chain calls per request (pool state, NPM position, wallet balances). At a 30-second dashboard polling cadence the RPC load is trivial.
- Logs endpoint shells out to `tail` for performance — won't OOM on a 100MB log file the way `read().splitlines()` would.
