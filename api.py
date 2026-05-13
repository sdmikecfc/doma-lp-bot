"""
api.py — FastAPI service exposing LP bot state for the web3guides.com dashboard.

Endpoints:
  GET /healthz                       — unauthenticated health check
  GET /api/lp/summary  (X-API-Key)   — full LPSummary payload
  GET /api/lp/logs?lines=N (X-API-Key) — tail of the supervisord log

Run with:
  uvicorn api:app --host 0.0.0.0 --port 5002

Designed to run alongside lp_bot.py under supervisord (see api.supervisor.conf).
"""
import os
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader
from web3 import Web3

import config
from state import get_db, init_db
from shared import connect, ERC20_ABI
from pool_state import (
    read_pool_state, sqrt_price_x96_to_price, tick_to_price,
    position_proximity_to_edge, position_in_range,
    calculate_amounts_for_liquidity, tick_to_sqrt_price_x96,
    compute_uncollected_fees,
)
from position_manager import get_position


# ── Config ────────────────────────────────────────────────────────────────
API_KEY  = os.getenv("LP_API_KEY", "").strip()
LOG_PATH = os.getenv("LP_LOG_PATH", "/var/log/lp_bot.out.log")

# Token-side label (for "pool" field). Read from config so it tracks the bot's target.
POOL_LABEL = f"{config.LP_TOKEN_SYMBOL}/USDC.e"


# ── Auth ──────────────────────────────────────────────────────────────────
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def require_key(key: Optional[str] = Security(api_key_header)) -> str:
    if not API_KEY:
        raise HTTPException(status_code=500, detail="LP_API_KEY not configured on server")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key


# ── App + lazy w3 connection ──────────────────────────────────────────────
app = FastAPI(title="LP Bot API", version="1.0.0")

_w3 = None
def w3() -> Web3:
    """Lazy-init the web3 connection so startup doesn't fail if RPC is briefly down."""
    global _w3
    if _w3 is None:
        _w3 = connect()
    return _w3


@app.on_event("startup")
def _startup():
    # Make sure DB tables exist (idempotent — uses CREATE TABLE IF NOT EXISTS).
    init_db()


# ── Health ────────────────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "lp_bot_api", "version": "1.0.0"}


# ── Helpers ───────────────────────────────────────────────────────────────
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wallet_address() -> str:
    """Resolve the wallet address from config without exposing private key material."""
    from eth_account import Account
    if config.MNEMONIC:
        Account.enable_unaudited_hdwallet_features()
        acct = Account.from_mnemonic(
            config.MNEMONIC,
            account_path=f"m/44'/60'/0'/0/{config.MNEMONIC_ACCOUNT_INDEX}",
        )
    else:
        acct = Account.from_key(config.PRIVATE_KEY)
    return acct.address


def _read_balances(wallet: str, current_price: float) -> tuple[float, float, float]:
    """
    Returns (usdce_bal_usd, token_bal_usd, total_idle_usd).
    `current_price` is USDC.e per LP token from the pool.
    """
    chain = w3()
    usdce = chain.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = chain.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)
    usdce_raw = usdce.functions.balanceOf(wallet).call()
    token_raw = token.functions.balanceOf(wallet).call()
    usdce_usd = usdce_raw / (10 ** config.USDCE_DECIMALS)
    token_usd = (token_raw / (10 ** config.TOKEN_DECIMALS)) * current_price
    return usdce_usd, token_usd, usdce_usd + token_usd


def _build_position_payload(db_pos: dict, pool_state: dict) -> dict:
    """Turn a row from `positions` table + on-chain data into an LPPosition dict."""
    token_id    = db_pos["token_id"]
    tick_lower  = db_pos["tick_lower"]
    tick_upper  = db_pos["tick_upper"]
    tok_is_t0   = pool_state["token_is_token0"]
    cur_tick    = pool_state["tick"]
    sqrt_now    = pool_state["sqrt_price_x96"]
    sqrt_lo     = tick_to_sqrt_price_x96(tick_lower)
    sqrt_hi     = tick_to_sqrt_price_x96(tick_upper)

    cur_price = sqrt_price_x96_to_price(
        sqrt_now, tok_is_t0, config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    range_lo = tick_to_price(tick_lower, tok_is_t0, config.TOKEN_DECIMALS, config.USDCE_DECIMALS)
    range_hi = tick_to_price(tick_upper, tok_is_t0, config.TOKEN_DECIMALS, config.USDCE_DECIMALS)
    if range_lo > range_hi:
        range_lo, range_hi = range_hi, range_lo

    # Live on-chain read of the NPM position
    onchain   = get_position(w3(), token_id)
    liquidity = onchain["liquidity"]

    # Real uncollected fees — uses V3's feeGrowthInside math, not stale tokensOwed.
    # Falls back to tokensOwed if the chain read fails for any reason.
    try:
        owed0, owed1 = compute_uncollected_fees(pool_state["pool"], onchain, cur_tick)
    except Exception:
        owed0 = onchain["tokens_owed_0"]
        owed1 = onchain["tokens_owed_1"]

    # Current USD value of the position's deposits
    amt0, amt1 = calculate_amounts_for_liquidity(sqrt_now, sqrt_lo, sqrt_hi, liquidity)

    if tok_is_t0:
        # token0 = LP_TOKEN (priced by `cur_price`), token1 = USDC.e
        amount0_usd = (amt0 / (10 ** config.TOKEN_DECIMALS)) * cur_price
        amount1_usd =  amt1 / (10 ** config.USDCE_DECIMALS)
        fees_usd    = (owed0 / (10 ** config.TOKEN_DECIMALS)) * cur_price + \
                       owed1 / (10 ** config.USDCE_DECIMALS)
    else:
        # token0 = USDC.e, token1 = LP_TOKEN
        amount0_usd =  amt0 / (10 ** config.USDCE_DECIMALS)
        amount1_usd = (amt1 / (10 ** config.TOKEN_DECIMALS)) * cur_price
        fees_usd    =  owed0 / (10 ** config.USDCE_DECIMALS) + \
                      (owed1 / (10 ** config.TOKEN_DECIMALS)) * cur_price

    liquidity_usd = amount0_usd + amount1_usd

    # Impermanent loss vs HODL benchmark
    init_amt0 = int(db_pos["initial_amount0"])
    init_amt1 = int(db_pos["initial_amount1"])
    if tok_is_t0:
        hodl_usd = (init_amt0 / (10 ** config.TOKEN_DECIMALS)) * cur_price + \
                    init_amt1 / (10 ** config.USDCE_DECIMALS)
    else:
        hodl_usd =  init_amt0 / (10 ** config.USDCE_DECIMALS) + \
                   (init_amt1 / (10 ** config.TOKEN_DECIMALS)) * cur_price

    impermanent_loss = liquidity_usd - hodl_usd  # negative = LP underperforming HODL

    return {
        "id":                f"pos_{token_id}",
        "nft_token_id":      token_id,
        "pool":              POOL_LABEL,
        "pool_address":      config.LP_POOL_ADDRESS,
        "protocol":          "Doma V3",
        "chain":             "doma",
        "fee_tier":          config.LP_FEE_TIER / 1_000_000,   # 500 → 0.0005
        "tick_lower":        tick_lower,
        "tick_upper":        tick_upper,
        "range_low":         range_lo,
        "range_high":        range_hi,
        "current_price":     cur_price,
        "current_tick":      cur_tick,
        "in_range":          position_in_range(cur_tick, tick_lower, tick_upper),
        "proximity_to_edge": position_proximity_to_edge(cur_tick, tick_lower, tick_upper),
        "liquidity_usd":     liquidity_usd,
        "fees_earned_usd":   fees_usd,
        "impermanent_loss":  impermanent_loss,
        "net_pnl":           fees_usd + impermanent_loss,
        "opened_at":         db_pos["minted_at"],
        "amount0_usd":       amount0_usd,
        "amount1_usd":       amount1_usd,
    }


def _empty_summary(error_note: Optional[str] = None) -> dict:
    """Fallback shape when nothing is initialised yet (no DB rows, no positions)."""
    out = {
        "state": {
            "total_value":         0.0,
            "deployed_in_lp":      0.0,
            "idle_balance":        0.0,
            "total_fees_earned":   0.0,
            "total_il_usd":        0.0,
            "total_pnl_usd":       0.0,
            "total_pnl_pct":       0.0,
            "active_positions":    0,
            "rebalance_count":     0,
            "lifetime_fees":       0.0,
            "wallet_address":      "",
            "first_deployed_at":   None,
            "updated_at":          _now_iso(),
        },
        "positions":  [],
        "rebalances": [],
        "config": {
            "target_capital_usd":    config.LP_TOTAL_USD,
            "range_pct":             config.LP_RANGE_PCT,
            "rebalance_trigger_pct": config.LP_REBALANCE_TRIGGER_PCT,
            "fee_tier":              config.LP_FEE_TIER,
            "loop_interval_sec":     config.LP_LOOP_INTERVAL_SEC,
        },
    }
    if error_note:
        out["note"] = error_note
    return out


# ── Summary endpoint ──────────────────────────────────────────────────────
@app.get("/api/lp/summary")
def summary(_: str = Security(require_key)):
    try:
        wallet = _wallet_address()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"wallet resolve failed: {e}")

    try:
        ps = read_pool_state(w3(), config.LP_POOL_ADDRESS)
    except Exception as e:
        # If the chain read fails we still return a usable shape rather than 500.
        empty = _empty_summary(error_note=f"pool read failed: {e}")
        empty["state"]["wallet_address"] = wallet
        return empty

    cur_price = sqrt_price_x96_to_price(
        ps["sqrt_price_x96"], ps["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS,
    )

    # Wallet
    try:
        usdce_idle, token_idle, idle_balance = _read_balances(wallet, cur_price)
    except Exception:
        usdce_idle = token_idle = idle_balance = 0.0

    # DB reads
    con = get_db()
    db_positions = con.execute(
        "SELECT * FROM positions WHERE status='ACTIVE' ORDER BY id DESC"
    ).fetchall()

    positions = []
    for row in db_positions:
        try:
            positions.append(_build_position_payload(dict(row), ps))
        except Exception as e:
            # Skip a position that fails to read on-chain rather than killing the whole call.
            positions.append({
                "id":                f"pos_{row['token_id']}",
                "nft_token_id":      row["token_id"],
                "pool":              POOL_LABEL,
                "pool_address":      config.LP_POOL_ADDRESS,
                "protocol":          "Doma V3",
                "chain":             "doma",
                "fee_tier":          config.LP_FEE_TIER / 1_000_000,
                "tick_lower":        row["tick_lower"],
                "tick_upper":        row["tick_upper"],
                "range_low":         0,
                "range_high":        0,
                "current_price":     cur_price,
                "current_tick":      ps["tick"],
                "in_range":          False,
                "proximity_to_edge": 1.0,
                "liquidity_usd":     0.0,
                "fees_earned_usd":   0.0,
                "impermanent_loss":  0.0,
                "net_pnl":           0.0,
                "opened_at":         row["minted_at"],
                "amount0_usd":       0.0,
                "amount1_usd":       0.0,
                "error":             str(e),
            })

    deployed_in_lp  = sum(p["liquidity_usd"]   for p in positions)
    uncollected     = sum(p["fees_earned_usd"] for p in positions)
    total_il        = sum(p["impermanent_loss"] for p in positions)
    total_value     = idle_balance + deployed_in_lp

    # Lifetime fees = collected (in DB) + uncollected (on-chain right now)
    collected_row = con.execute(
        "SELECT COALESCE(SUM(value_usd), 0) AS s FROM fee_collections"
    ).fetchone()
    collected_fees = float(collected_row["s"] or 0.0)
    lifetime_fees  = collected_fees + uncollected

    # Lifetime swap costs (slippage + pool fee paid on every rebalance/compound swap)
    swap_row = con.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n FROM swap_costs"
    ).fetchone()
    total_swap_fees_paid = float(swap_row["s"] or 0.0) if swap_row else 0.0
    swap_count           = int(swap_row["n"] or 0)    if swap_row else 0

    # First deploy + capital basis
    first_row = con.execute(
        "SELECT minted_at, initial_value_usd FROM positions ORDER BY id ASC LIMIT 1"
    ).fetchone()
    first_deployed_at  = first_row["minted_at"]            if first_row else None
    capital_originally = float(first_row["initial_value_usd"]) if first_row else 0.0

    # P&L: total_value vs capital basis
    if capital_originally > 0:
        total_pnl_usd = total_value - capital_originally
        total_pnl_pct = total_pnl_usd / capital_originally
    else:
        total_pnl_usd = 0.0
        total_pnl_pct = 0.0

    # Rebalances (newest first, capped to 50 for response size)
    rebal_rows = con.execute(
        "SELECT * FROM rebalances ORDER BY id DESC LIMIT 50"
    ).fetchall()
    rebalances = []
    for r in rebal_rows:
        rd = dict(r)

        # Match this rebalance to its actual swap cost via a tight timestamp
        # window. The swap happens within seconds of the rebalance event, so a
        # ±5min window safely captures it without picking up unrelated swaps.
        try:
            rebal_dt = datetime.fromisoformat(rd["timestamp"].replace("Z", "+00:00"))
            window_lo = (rebal_dt - timedelta(minutes=5)).isoformat()
            window_hi = (rebal_dt + timedelta(minutes=5)).isoformat()
            sw_row = con.execute(
                """SELECT COALESCE(SUM(cost_usd), 0) AS s, COUNT(*) AS n
                   FROM swap_costs
                   WHERE timestamp > ? AND timestamp < ? AND reason = 'REBALANCE'""",
                (window_lo, window_hi),
            ).fetchone()
            swap_cost = float(sw_row["s"] or 0.0) if sw_row else 0.0
            swap_n    = int(sw_row["n"] or 0)    if sw_row else 0
        except Exception:
            swap_cost = 0.0
            swap_n    = 0

        # Fees collected DURING this specific rebalance event (timestamp window).
        # Compounds on the position earlier in its life don't count toward the
        # rebalance row — those are tracked separately in the position's life history.
        try:
            fee_row = con.execute(
                """SELECT COALESCE(SUM(value_usd), 0) AS s FROM fee_collections
                   WHERE position_id = ? AND timestamp > ? AND timestamp < ?""",
                (rd["position_id"], window_lo, window_hi),
            ).fetchone()
            fees_collected = float(fee_row["s"] or 0.0) if fee_row else 0.0
        except Exception:
            fees_collected = 0.0

        rebalances.append({
            "id":                  f"rb_{rd['id']}",
            "executed_at":         rd["timestamp"],
            "pool":                POOL_LABEL,
            "reason":              rd["reason"],
            "from_tick_lower":     rd["old_tick_lower"],
            "from_tick_upper":     rd["old_tick_upper"],
            "to_tick_lower":       rd["new_tick_lower"],
            "to_tick_upper":       rd["new_tick_upper"],
            "from_price":          float(rd["current_price"]) if rd["current_price"] is not None else 0.0,
            "to_price":            float(rd["current_price"]) if rd["current_price"] is not None else 0.0,
            "swap_cost_usd":       swap_cost,        # what the bot paid to do the rebalance swap
            "swap_count":          swap_n,
            "fees_collected_usd":  fees_collected,   # LP fees claimed on this position (usually 0 during rebal)
            "gas_usd":             0.0,              # not yet tracked
        })

    rebal_count_row = con.execute("SELECT COUNT(*) AS c FROM rebalances").fetchone()
    rebalance_count = int(rebal_count_row["c"]) if rebal_count_row else 0

    return {
        "state": {
            "total_value":         total_value,
            "deployed_in_lp":      deployed_in_lp,
            "idle_balance":        idle_balance,
            "total_fees_earned":   lifetime_fees,
            "total_il_usd":        total_il,
            "total_pnl_usd":       total_pnl_usd,
            "total_pnl_pct":       total_pnl_pct,
            "active_positions":    len(positions),
            "rebalance_count":     rebalance_count,
            "lifetime_fees":       lifetime_fees,
            "total_swap_fees_paid_usd": total_swap_fees_paid,
            "swap_count":          swap_count,
            "wallet_address":      wallet,
            "first_deployed_at":   first_deployed_at,
            "updated_at":          _now_iso(),
        },
        "positions":  positions,
        "rebalances": rebalances,
        "config": {
            "target_capital_usd":    config.LP_TOTAL_USD,
            "range_pct":             config.LP_RANGE_PCT,
            "rebalance_trigger_pct": config.LP_REBALANCE_TRIGGER_PCT,
            "fee_tier":              config.LP_FEE_TIER,
            "loop_interval_sec":     config.LP_LOOP_INTERVAL_SEC,
        },
    }


# ── Logs endpoint ─────────────────────────────────────────────────────────
@app.get("/api/lp/logs")
def logs(lines: int = 100, _: str = Security(require_key)):
    n = max(1, min(int(lines), 2000))
    if not os.path.exists(LOG_PATH):
        return {
            "process": "lp_bot",
            "lines":   [f"Log file not found at {LOG_PATH}. Set LP_LOG_PATH env var if it lives elsewhere."],
        }
    try:
        out = subprocess.check_output(
            ["tail", "-n", str(n), LOG_PATH],
            stderr=subprocess.STDOUT, timeout=5,
        )
        text = out.decode("utf-8", errors="replace")
        return {
            "process": "lp_bot",
            "lines":   text.splitlines(),
        }
    except Exception as e:
        return {
            "process": "lp_bot",
            "lines":   [],
            "error":   f"tail failed: {e}",
        }
