"""
test_lp.py — REAL ON-CHAIN $1 LP mint + immediate burn.

Validates the full LP flow at small risk:
  1. Permit2 swap $0.50 USDC.e → SOFTWARE.ai (already proven to work)
  2. Approve USDC.e + token to NPM
  3. Mint LP position at ±0.5% range
  4. Immediately decreaseLiquidity + collect to unwind
  5. Swap any received SOFTWARE.ai back to USDC.e
  6. Report net cost

If this passes cleanly, we deploy the full ~$75 with confidence.
Max risk: $1 (capital) + ~$0.005 gas.

Run: python3 test_lp.py
"""
import sys

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from web3 import Web3
from pool_state import (
    read_pool_state, sqrt_price_x96_to_price, calculate_range_ticks,
)
from swap_executor import ensure_swap_setup, swap_exact_in
from position_manager import (
    mint_position, get_position, decrease_liquidity, collect_fees,
)


TEST_AMOUNT_USD = 1.00


def main():
    w3 = connect()
    wallet, private_key = load_wallet()
    info(f"Wallet: {wallet}")

    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)

    def balances():
        u = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
        t = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS
        return u, t

    u0, t0 = balances()
    info(f"START — USDC.e: {u0:.6f}  {config.LP_TOKEN_SYMBOL}: {t0:.6f}")

    if u0 < TEST_AMOUNT_USD * 1.05:
        err(f"Need at least ${TEST_AMOUNT_USD * 1.05:.4f} USDC.e to test, have ${u0:.4f}")
        sys.exit(1)

    gas_price = max(int(w3.eth.gas_price * 1.5), 1_000_000)

    # ── Step 1: Permit2 setup (idempotent — already done from test_swap) ──
    info("─── Permit2 setup ───")
    if not ensure_swap_setup(w3, wallet, private_key, config.USDCE_ADDRESS, gas_price):
        err("USDC.e Permit2 setup failed"); sys.exit(1)
    if not ensure_swap_setup(w3, wallet, private_key, config.LP_TOKEN_ADDRESS, gas_price):
        err("Token Permit2 setup failed"); sys.exit(1)

    # ── Step 2: Pool state + range calculation ──
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], config.LP_RANGE_PCT, pool_state["tick_spacing"]
    )
    info(f"")
    info(f"Pool tick: {pool_state['tick']}  price: ${price:.8f}")
    info(f"Mint range: ticks [{tick_lower}, {tick_upper}]  (±{config.LP_RANGE_PCT*100:.2f}%)")

    # ── Step 3: Setup swap — only if we don't already have enough token ──
    # If a prior failed attempt left us with token, skip the swap to save fees.
    target_token_value = TEST_AMOUNT_USD / 2
    have_token_value = t0 * price
    info(f"")
    if have_token_value < target_token_value * 0.5:
        info(f"─── Setup swap: ${TEST_AMOUNT_USD/2:.4f} USDC.e → token ───")
        half_raw = int((TEST_AMOUNT_USD / 2) * 10 ** config.USDCE_DECIMALS)
        ok, _ = swap_exact_in(
            w3, wallet, private_key, pool_state,
            config.USDCE_ADDRESS, config.LP_TOKEN_ADDRESS,
            half_raw, max_slippage=0.01, gas_price=gas_price,
        )
        if not ok:
            err("Setup swap failed"); sys.exit(1)
        u1, t1 = balances()
        info(f"AFTER setup swap — USDC.e: {u1:.6f}  {config.LP_TOKEN_SYMBOL}: {t1:.6f}")
    else:
        info(f"─── Skipping setup swap — already have {t0:.6f} token (~${have_token_value:.4f}) ───")

    # Re-read pool state (may have shifted slightly)
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)

    # ── Step 4: Mint LP position ──
    info("")
    info("─── Mint LP position ───")
    usdce_raw = usdce.functions.balanceOf(wallet).call()
    token_raw = token.functions.balanceOf(wallet).call()

    # Cap to half of total to avoid exceeding LP target
    target_usdc_raw = int((TEST_AMOUNT_USD / 2) * 10 ** config.USDCE_DECIMALS)
    if usdce_raw > target_usdc_raw:
        usdce_raw = target_usdc_raw

    if pool_state["token_is_token0"]:
        amount0_desired = token_raw
        amount1_desired = usdce_raw
    else:
        amount0_desired = usdce_raw
        amount1_desired = token_raw

    # No min — let NPM use whatever ratio it wants for this tick/range.
    # The "amount_desired" values cap the maximum we'd contribute. The actual
    # used amounts will be whatever fits the V3 math at the current tick.
    amount0_min = 0
    amount1_min = 0

    ok, token_id, liquidity, amt0_used, amt1_used = mint_position(
        w3, wallet, private_key, pool_state,
        tick_lower, tick_upper,
        amount0_desired, amount1_desired,
        amount0_min, amount1_min, gas_price
    )
    if not ok:
        err("Mint failed"); sys.exit(1)
    info(f"Position created: tokenId={token_id}  liquidity={liquidity}")
    info(f"  amount0 used: {amt0_used}  amount1 used: {amt1_used}")

    u2, t2 = balances()
    info(f"AFTER mint — USDC.e: {u2:.6f}  {config.LP_TOKEN_SYMBOL}: {t2:.6f}")

    # ── Step 5: Immediately unwind: decrease + collect ──
    info("")
    info("─── Unwind: decrease + collect ───")
    onchain = get_position(w3, token_id)
    info(f"On-chain liquidity: {onchain['liquidity']}")

    ok, _, _ = decrease_liquidity(
        w3, wallet, private_key, token_id, onchain["liquidity"], gas_price
    )
    if not ok:
        err("decreaseLiquidity failed — position has stuck capital. Manual recovery needed."); sys.exit(1)

    ok, amt0_back, amt1_back = collect_fees(
        w3, wallet, private_key, token_id, gas_price
    )
    if not ok:
        err("collect failed"); sys.exit(1)
    info(f"Collected — amount0: {amt0_back}  amount1: {amt1_back}")

    u3, t3 = balances()
    info(f"AFTER unwind — USDC.e: {u3:.6f}  {config.LP_TOKEN_SYMBOL}: {t3:.6f}")

    # ── Step 6: Swap any token back to USDC.e ──
    if t3 > 0.0001:
        info("")
        info(f"─── Closing swap: {t3:.6f} token → USDC.e ───")
        token_raw = token.functions.balanceOf(wallet).call()
        pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
        ok, _ = swap_exact_in(
            w3, wallet, private_key, pool_state,
            config.LP_TOKEN_ADDRESS, config.USDCE_ADDRESS,
            token_raw, max_slippage=0.01, gas_price=gas_price,
        )
        if not ok:
            warn("Closing swap failed — token left in wallet")

    u4, t4 = balances()

    # ── Final report ──
    print()
    print("=" * 70)
    print("LP TEST RESULTS")
    print("=" * 70)
    print(f"  Started:  USDC.e {u0:.6f}  token {t0:.6f}")
    print(f"  Ended:    USDC.e {u4:.6f}  token {t4:.6f}")
    print(f"  Net USDC.e change: {u4 - u0:+.6f}")
    print(f"  Net token change:  {t4 - t0:+.6f}")
    print()
    cost = u0 - u4
    pct = cost / TEST_AMOUNT_USD * 100
    print(f"  Round-trip cost: ${cost:.6f}  ({pct:.4f}% of ${TEST_AMOUNT_USD:.2f} test)")
    print()
    if cost < TEST_AMOUNT_USD * 0.005:  # less than 0.5% cost
        print("  ✓ PASS — LP mint/burn pattern works. Safe to deploy ~$75.")
    elif cost < TEST_AMOUNT_USD * 0.02:  # 0.5-2% — high but not catastrophic
        print("  ⚠ HIGH COST but pattern works. Investigate before full deploy.")
    else:
        print("  ✗ COST TOO HIGH — investigate before deploy.")


if __name__ == "__main__":
    main()
