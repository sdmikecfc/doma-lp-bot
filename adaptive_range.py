"""
adaptive_range.py — Phase 1: realized-volatility adaptive LP range.

Replaces the static `config.LP_RANGE_PCT` with a function that reads the last
N hours of price snapshots and sets the range based on realized volatility.

Logic:
  1. Pull last 24h of price_snapshots from DB
  2. Compute realized range = (high - low) / median
  3. Target LP range = realized_range × 1.5 / 2  (the /2 because LP range is ±)
  4. Clamp to [LP_RANGE_MIN, LP_RANGE_MAX]
  5. Fall back to LP_RANGE_DEFAULT if not enough snapshots yet

Tunable via env vars:
  ADAPTIVE_RANGE_ENABLED      true / false   (master switch)
  ADAPTIVE_RANGE_LOOKBACK_HRS 24             (history window)
  ADAPTIVE_RANGE_MULTIPLIER   1.5            (buffer above realized vol)
  LP_RANGE_MIN                0.005          (±0.5% floor)
  LP_RANGE_MAX                0.05           (±5% ceiling)
  LP_RANGE_DEFAULT            0.010          (±1% if no history)
"""
import os
import sqlite3
from typing import Optional

from shared import info, warn


def _get_env_float(key: str, default: float) -> float:
    try: return float(os.getenv(key, default))
    except (TypeError, ValueError): return default


def _get_env_int(key: str, default: int) -> int:
    try: return int(os.getenv(key, default))
    except (TypeError, ValueError): return default


def _get_env_bool(key: str, default: bool) -> bool:
    v = os.getenv(key, "").strip().lower()
    if v in ("true", "1", "yes"):  return True
    if v in ("false", "0", "no"):  return False
    return default


ADAPTIVE_ENABLED       = _get_env_bool("ADAPTIVE_RANGE_ENABLED", True)
ADAPTIVE_LOOKBACK_HRS  = _get_env_int("ADAPTIVE_RANGE_LOOKBACK_HRS", 24)
ADAPTIVE_MULTIPLIER    = _get_env_float("ADAPTIVE_RANGE_MULTIPLIER", 1.5)
LP_RANGE_MIN           = _get_env_float("LP_RANGE_MIN", 0.005)
LP_RANGE_MAX           = _get_env_float("LP_RANGE_MAX", 0.05)
LP_RANGE_DEFAULT       = _get_env_float("LP_RANGE_DEFAULT", 0.010)


def record_price_snapshot(con: sqlite3.Connection, price: float) -> None:
    """
    Append a price observation to the price_snapshots table.
    Call this once per main-loop iteration in lp_bot.py.
    """
    if price <= 0:
        return
    con.execute("""
        INSERT INTO price_snapshots (snapshot_at, price_usd)
        VALUES (datetime('now'), ?)
    """, (price,))
    con.commit()


def compute_adaptive_range(con: sqlite3.Connection) -> float:
    """
    Returns the target LP range as a fraction (e.g. 0.012 = ±1.2%).

    Falls back to LP_RANGE_DEFAULT if:
      - Adaptive mode is disabled
      - Fewer than 10 snapshots in the lookback window
    """
    import config

    if not ADAPTIVE_ENABLED:
        return config.LP_RANGE_PCT      # honor static config when disabled

    rows = con.execute(f"""
        SELECT price_usd FROM price_snapshots
        WHERE snapshot_at > datetime('now', '-{ADAPTIVE_LOOKBACK_HRS} hours')
          AND price_usd > 0
        ORDER BY snapshot_at
    """).fetchall()

    if len(rows) < 10:
        return LP_RANGE_DEFAULT

    prices = [r[0] for r in rows]
    high   = max(prices)
    low    = min(prices)
    median = sorted(prices)[len(prices) // 2]

    if median <= 0:
        return LP_RANGE_DEFAULT

    realized_full_range = (high - low) / median   # peak-to-trough fraction
    target_half_range   = realized_full_range * ADAPTIVE_MULTIPLIER / 2

    # Clamp
    target = min(LP_RANGE_MAX, max(LP_RANGE_MIN, target_half_range))
    return target


def log_adaptive_state(con: sqlite3.Connection) -> None:
    """One-line log of the current adaptive range decision."""
    if not ADAPTIVE_ENABLED:
        info(f"  Adaptive range: DISABLED (using static {get_static_range()*100:.2f}%)")
        return

    rng = compute_adaptive_range(con)
    n = con.execute(
        f"SELECT COUNT(*) FROM price_snapshots "
        f"WHERE snapshot_at > datetime('now', '-{ADAPTIVE_LOOKBACK_HRS} hours')"
    ).fetchone()[0]

    info(f"  Adaptive range: ±{rng*100:.3f}%  (from {n} snapshots over {ADAPTIVE_LOOKBACK_HRS}h)")


def get_static_range() -> float:
    """Convenience for log_adaptive_state when adaptive is off."""
    import config
    return config.LP_RANGE_PCT
