"""
exit_position.py — Cleanly closes the active LP position.

Decreases liquidity to 0, collects all owed fees, marks position BURNED in DB.
After running this, the bot will see no active position on next start and
will redeploy with current .env config (e.g., a tighter LP_RANGE_PCT).

Usage: python3 exit_position.py
"""
import sys
from datetime import datetime, timezone

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from web3 import Web3
from pool_state import read_pool_state, sqrt_price_x96_to_price
from position_manager import get_position, decrease_liquidity, collect_fees
from state import init_db, get_active_position, record_burn


def to_usd(amount0_raw, amount1_raw, price, token_is_token0):
    if token_is_token0:
        token_raw, usdc_raw = amount0_raw, amount1_raw
    else:
        usdc_raw, token_raw = amount0_raw, amount1_raw
    usdc_human = usdc_raw / 10 ** config.USDCE_DECIMALS
    token_human = token_raw / 10 ** config.TOKEN_DECIMALS
    return usdc_human + token_human * price


def main():
    w3 = connect()
    wallet, private_key = load_wallet()
    info(f"Wallet: {wallet}")

    con = init_db()
    active = get_active_position(con)
    if not active:
        warn("No active position in DB. Nothing to exit.")
        sys.exit(0)

    info(f"Active position: id={active['id']}  token_id={active['token_id']}")
    info(f"  range:  [{active['tick_lower']}, {active['tick_upper']}]")
    info(f"  minted: {active['minted_at']}")

    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    onchain = get_position(w3, active['token_id'])
    info(f"On-chain liquidity: {onchain['liquidity']}")

    gas_price = max(int(w3.eth.gas_price * 1.5), 1_000_000)

    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)
    u0 = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    t0 = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS
    info(f"BEFORE — USDC.e: {u0:.6f}  {config.LP_TOKEN_SYMBOL}: {t0:.6f}")

    # Step 1: decreaseLiquidity to 0
    if onchain['liquidity'] > 0:
        info("Decreasing liquidity to 0...")
        ok, _, _ = decrease_liquidity(w3, wallet, private_key,
                                       active['token_id'], onchain['liquidity'], gas_price)
        if not ok:
            err("decreaseLiquidity failed — aborting")
            sys.exit(1)

    # Step 2: collect everything owed
    info("Collecting all owed tokens...")
    ok, amt0, amt1 = collect_fees(w3, wallet, private_key,
                                   active['token_id'], gas_price)
    if not ok:
        err("collect failed — aborting")
        sys.exit(1)

    # Step 3: mark position as BURNED in DB
    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    final_value = to_usd(amt0, amt1, price, pool_state["token_is_token0"])
    record_burn(con, active['id'], amt0, amt1, final_value)

    u1 = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    t1 = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS
    info(f"AFTER  — USDC.e: {u1:.6f}  {config.LP_TOKEN_SYMBOL}: {t1:.6f}")
    info(f"Recovered ≈${final_value:.4f}")
    info("")
    info("✓ Position closed and marked BURNED in DB.")
    info("  Edit .env (e.g., LP_RANGE_PCT=0.001) then `supervisorctl restart lp_bot`")
    info("  Bot will redeploy at the new range automatically.")


if __name__ == "__main__":
    main()
