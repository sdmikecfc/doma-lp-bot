"""
state.py — minimal SQLite for tracking the active LP position and history.
"""
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lp_bot.db")


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id INTEGER NOT NULL,
            pool_address TEXT NOT NULL,
            tick_lower INTEGER NOT NULL,
            tick_upper INTEGER NOT NULL,
            initial_liquidity TEXT NOT NULL,
            initial_amount0 TEXT NOT NULL,
            initial_amount1 TEXT NOT NULL,
            initial_value_usd REAL NOT NULL,
            minted_at TEXT NOT NULL,
            burned_at TEXT,
            final_amount0 TEXT,
            final_amount1 TEXT,
            final_value_usd REAL,
            status TEXT NOT NULL DEFAULT 'ACTIVE'
        );

        CREATE TABLE IF NOT EXISTS rebalances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            timestamp TEXT NOT NULL,
            reason TEXT,
            old_tick_lower INTEGER,
            old_tick_upper INTEGER,
            new_tick_lower INTEGER,
            new_tick_upper INTEGER,
            current_tick INTEGER,
            current_price REAL
        );

        CREATE TABLE IF NOT EXISTS fee_collections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            timestamp TEXT NOT NULL,
            amount0 TEXT,
            amount1 TEXT,
            value_usd REAL
        );

        CREATE TABLE IF NOT EXISTS bot_state (
            id INTEGER PRIMARY KEY,
            active_position_id INTEGER,
            last_check TEXT,
            last_volume_check TEXT,
            consecutive_low_volume_hours INTEGER DEFAULT 0,
            last_fee_collect_at TEXT
        );

        CREATE TABLE IF NOT EXISTS swap_costs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            reason TEXT,                     -- 'INITIAL_MINT', 'REBALANCE', 'EXIT', etc.
            direction TEXT NOT NULL,         -- 'usdce_to_token' | 'token_to_usdce'
            pool_address TEXT,
            amount_in_raw TEXT NOT NULL,     -- raw token units (string for big-int safety)
            amount_out_raw TEXT NOT NULL,
            amount_in_usd REAL NOT NULL,     -- USD value pre-swap
            amount_out_usd REAL NOT NULL,    -- USD value of received tokens at pre-swap price
            cost_usd REAL NOT NULL           -- amount_in_usd − amount_out_usd (slippage + pool fee)
        );

        CREATE TABLE IF NOT EXISTS price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_at TEXT NOT NULL,
            price_usd REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_price_snapshot_at
          ON price_snapshots(snapshot_at);

        INSERT OR IGNORE INTO bot_state (id) VALUES (1);
    """)
    # Migration: add column if missing (for existing DBs)
    try:
        con.execute("ALTER TABLE bot_state ADD COLUMN last_fee_collect_at TEXT")
    except sqlite3.OperationalError:
        pass  # already exists
    con.commit()
    return con


def get_last_fee_collect(con) -> str | None:
    row = con.execute("SELECT last_fee_collect_at FROM bot_state WHERE id=1").fetchone()
    return row["last_fee_collect_at"] if row else None


def set_last_fee_collect(con):
    now = datetime.now(timezone.utc).isoformat()
    con.execute("UPDATE bot_state SET last_fee_collect_at=? WHERE id=1", (now,))
    con.commit()


def get_active_position(con):
    row = con.execute(
        "SELECT * FROM positions WHERE status='ACTIVE' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def record_mint(con, token_id, pool_address, tick_lower, tick_upper,
                liquidity, amount0, amount1, value_usd):
    now = datetime.now(timezone.utc).isoformat()
    cur = con.execute("""
        INSERT INTO positions
            (token_id, pool_address, tick_lower, tick_upper,
             initial_liquidity, initial_amount0, initial_amount1, initial_value_usd, minted_at)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (token_id, pool_address, tick_lower, tick_upper,
          str(liquidity), str(amount0), str(amount1), value_usd, now))
    pos_id = cur.lastrowid
    con.execute("UPDATE bot_state SET active_position_id=? WHERE id=1", (pos_id,))
    con.commit()
    return pos_id


def record_burn(con, position_id, amount0, amount1, value_usd):
    now = datetime.now(timezone.utc).isoformat()
    con.execute("""
        UPDATE positions
        SET status='BURNED', burned_at=?, final_amount0=?, final_amount1=?, final_value_usd=?
        WHERE id=?
    """, (now, str(amount0), str(amount1), value_usd, position_id))
    con.commit()


def record_rebalance(con, position_id, reason, old_tl, old_tu, new_tl, new_tu,
                     current_tick, current_price):
    now = datetime.now(timezone.utc).isoformat()
    con.execute("""
        INSERT INTO rebalances
            (position_id, timestamp, reason, old_tick_lower, old_tick_upper,
             new_tick_lower, new_tick_upper, current_tick, current_price)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (position_id, now, reason, old_tl, old_tu, new_tl, new_tu, current_tick, current_price))
    con.commit()


def record_fees(con, position_id, amount0, amount1, value_usd):
    now = datetime.now(timezone.utc).isoformat()
    con.execute("""
        INSERT INTO fee_collections (position_id, timestamp, amount0, amount1, value_usd)
        VALUES (?,?,?,?,?)
    """, (position_id, now, str(amount0), str(amount1), value_usd))
    con.commit()


def record_swap_cost(con, reason: str, direction: str, pool_address: str,
                     amount_in_raw: int, amount_out_raw: int,
                     amount_in_usd: float, amount_out_usd: float):
    """
    Record the slippage + pool-fee haircut from a swap.

    `direction` is 'usdce_to_token' or 'token_to_usdce'.
    `cost_usd = amount_in_usd − amount_out_usd` (always positive on a real swap).
    """
    now = datetime.now(timezone.utc).isoformat()
    cost_usd = max(0.0, amount_in_usd - amount_out_usd)
    con.execute("""
        INSERT INTO swap_costs
            (timestamp, reason, direction, pool_address,
             amount_in_raw, amount_out_raw, amount_in_usd, amount_out_usd, cost_usd)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (now, reason, direction, pool_address,
          str(amount_in_raw), str(amount_out_raw),
          amount_in_usd, amount_out_usd, cost_usd))
    con.commit()
