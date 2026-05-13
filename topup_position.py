"""
topup_position.py — Add idle wallet balance to the active LP position
without burning/reminting.

Use case: you sent more USDC.e (or SOFTWARE.ai) to the wallet and want
to redeploy it into the existing position. Cheaper than exit+remint
(~$0.10 vs ~$0.40 in swap fees).

Flow:
  1. Read active position from DB (tick_lower, tick_upper)
  2. Read wallet's idle USDC.e + SOFTWARE balances
  3. Compute optimal deposit ratio for the position's range
  4. Swap to balance (if either side is far off)
  5. Call NPM.increaseLiquidity() with the balanced amounts

NOT a substitute for rebalance — this keeps the SAME position range.
If you want to deploy at a new (e.g. tighter/wider) range, use
exit_position.py then restart the bot.

Usage:
    python3 topup_position.py             # do the topup
    python3 topup_position.py --dry-run   # show plan, no txs
"""
import sys

from web3 import Web3

import config
from shared import (
    connect, load_wallet, info, warn, err, ERC20_ABI,
)
from state import init_db, get_active_position
from pool_state import (
    read_pool_state, calculate_optimal_deposit_ratio,
    sqrt_price_x96_to_price,
)
from swap_executor import ensure_swap_setup, swap_exact_in
from position_manager import increase_liquidity


DRY_RUN = "--dry-run" in sys.argv


def main():
    w3 = connect()
    wallet, pkey = load_wallet()
    con = init_db()

    info("=" * 70)
    info("  LP position top-up")
    info("=" * 70)
    info(f"  Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    info(f"  Wallet: {wallet}")

    active = get_active_position(con)
    if not active:
        err("No active position in DB. Use exit + restart flow instead.")
        sys.exit(1)

    token_id   = active["token_id"]
    tick_lower = active["tick_lower"]
    tick_upper = active["tick_upper"]
    info(f"  Active position: tokenId={token_id}  ticks=[{tick_lower},{tick_upper}]")

    # ── Read pool state + current price ───
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS,
    )
    info(f"  Current price: ${price:.6f}")

    # ── Wallet balances ───
    usdce = w3.eth.contract(
        address=Web3.to_checksum_address(config.USDCE_ADDRESS), abi=ERC20_ABI
    )
    soft = w3.eth.contract(
        address=Web3.to_checksum_address(config.LP_TOKEN_ADDRESS), abi=ERC20_ABI
    )
    u_raw = usdce.functions.balanceOf(wallet).call()
    s_raw = soft.functions.balanceOf(wallet).call()
    u = u_raw / 10 ** config.USDCE_DECIMALS
    s = s_raw / 10 ** config.TOKEN_DECIMALS
    total_usd = u + s * price
    info(f"  Wallet: ${u:.4f} USDC.e + {s:.4f} SOFT (${s*price:.4f}) = ${total_usd:.4f}")

    if total_usd < 5.0:
        warn("Wallet has <$5 — nothing meaningful to top up.")
        sys.exit(0)

    # ── Optimal ratio for the current range ───
    u_frac, t_frac = calculate_optimal_deposit_ratio(
        pool_state, tick_lower, tick_upper,
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS, price,
    )
    info(f"  Optimal ratio: {u_frac*100:.1f}% USDC.e / {t_frac*100:.1f}% token")

    target_u_usd = total_usd * u_frac
    target_s_usd = total_usd * t_frac
    target_s     = target_s_usd / price if price > 0 else 0
    info(f"  Target: ${target_u_usd:.4f} USDC.e + {target_s:.4f} SOFT")

    # ── Decide if a swap is needed ───
    delta_u = u - target_u_usd        # positive = excess USDC.e
    info(f"  Delta:  ${delta_u:+.4f} USDC.e off-target")

    gas_price = int(w3.eth.gas_price * 1.5)

    if abs(delta_u) > 0.50:
        if delta_u > 0:
            swap_usd = delta_u
            info(f"  Swap ${swap_usd:.4f} USDC.e → SOFT to balance")
            token_in, token_out = config.USDCE_ADDRESS, config.LP_TOKEN_ADDRESS
            amount_in = int(swap_usd * 10 ** config.USDCE_DECIMALS)
        else:
            swap_usd = -delta_u
            info(f"  Swap ${swap_usd:.4f} worth SOFT → USDC.e to balance")
            token_in, token_out = config.LP_TOKEN_ADDRESS, config.USDCE_ADDRESS
            amount_in = int((swap_usd / price) * 10 ** config.TOKEN_DECIMALS)

        if DRY_RUN:
            info("  [DRY RUN] would execute balance swap")
        else:
            if not ensure_swap_setup(w3, wallet, pkey, token_in, gas_price):
                err("Approval setup failed"); sys.exit(1)
            ok, received = swap_exact_in(
                w3, wallet, pkey, pool_state,
                token_in, token_out, amount_in,
                config.LP_SWAP_MAX_SLIPPAGE_PCT, gas_price,
                db_con=con, reason="MANUAL_TOPUP",
            )
            if not ok:
                err("Balance swap failed — abort"); sys.exit(1)
    else:
        info("  Already balanced — no swap needed")

    # ── Re-read final balances after swap ───
    u_raw = usdce.functions.balanceOf(wallet).call()
    s_raw = soft.functions.balanceOf(wallet).call()
    u_final = u_raw / 10 ** config.USDCE_DECIMALS
    s_final = s_raw / 10 ** config.TOKEN_DECIMALS
    info(f"  Balanced wallet: ${u_final:.4f} USDC.e + {s_final:.4f} SOFT")

    # ── Increase liquidity on the existing NFT ───
    if DRY_RUN:
        info(f"  [DRY RUN] would call increaseLiquidity(tokenId={token_id}, "
             f"amount0={u_raw}, amount1={s_raw})")
        sys.exit(0)

    info(f"  Calling increaseLiquidity on tokenId={token_id}...")
    result = increase_liquidity(
        w3, wallet, pkey, token_id, u_raw, s_raw, gas_price,
    )
    if not result:
        err("✗ increase_liquidity failed")
        sys.exit(1)

    info(f"✓ Top-up complete:")
    info(f"    liquidity added:   {result.get('liquidity_added', '?')}")
    info(f"    amount0 used:      {result.get('amount0_used', '?')}")
    info(f"    amount1 used:      {result.get('amount1_used', '?')}")
    info(f"    tx:                {result.get('tx_hash', '?')}")


if __name__ == "__main__":
    main()
