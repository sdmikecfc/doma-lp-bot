"""
test_swap.py — REAL ON-CHAIN $0.10 round-trip test.

Sequence:
  1. Read wallet balances (USDC.e + SOFTWARE.ai)
  2. Approve USDC.e + SOFTWARE.ai to Permit2 (one-time)
  3. Permit2 approve both → Universal Router (one-time)
  4. Swap $0.10 USDC.e → SOFTWARE.ai
  5. Swap all received SOFTWARE.ai → USDC.e
  6. Report round-trip cost

Max possible loss: $0.10 + ~$0.001 gas. If anything reverts mid-way you get a
small slippage of expected fees, but balances should reconcile to within ~$0.001
of where they started (minus 2x V3 fees = 0.10%).

Run: python3 test_swap.py
"""
import sys

from web3 import Web3

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from pool_state import read_pool_state, sqrt_price_x96_to_price
from swap_executor import (
    ensure_swap_setup, swap_exact_in,
)


TEST_AMOUNT_USD = 0.0001  # 100 raw USDC.e — minimum that still produces non-zero output


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

    if u0 < TEST_AMOUNT_USD:
        err(f"Need at least ${TEST_AMOUNT_USD:.6f} USDC.e to test, have ${u0:.6f}")
        sys.exit(1)

    gas_price = max(int(w3.eth.gas_price * 1.5), 1_000_000)

    # ── Step 1: Set up Permit2 approvals for both tokens ─────────────
    info("")
    info("─── Setting up Permit2 approvals ───")
    if not ensure_swap_setup(w3, wallet, private_key, config.USDCE_ADDRESS, gas_price):
        err("USDC.e Permit2 setup failed — aborting")
        sys.exit(1)
    if not ensure_swap_setup(w3, wallet, private_key, config.LP_TOKEN_ADDRESS, gas_price):
        err("Token Permit2 setup failed — aborting")
        sys.exit(1)

    # ── Step 2: Read pool state ────────────────────────────────────
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    info("")
    info(f"Pool tick: {pool_state['tick']}  price: ${price:.8f}")

    # ── Step 3: Swap $0.10 USDC.e → token ───────────────────────────
    info("")
    info(f"─── Forward swap: ${TEST_AMOUNT_USD:.4f} USDC.e → {config.LP_TOKEN_SYMBOL} ───")
    amount_in_raw = int(TEST_AMOUNT_USD * 10 ** config.USDCE_DECIMALS)
    ok, tokens_received = swap_exact_in(
        w3, wallet, private_key, pool_state,
        config.USDCE_ADDRESS, config.LP_TOKEN_ADDRESS,
        amount_in_raw, max_slippage=0.01, gas_price=gas_price,
    )
    if not ok:
        err("Forward swap failed — STOP")
        sys.exit(1)

    u1, t1 = balances()
    info(f"AFTER forward — USDC.e: {u1:.6f}  {config.LP_TOKEN_SYMBOL}: {t1:.6f}")
    info(f"  Δ USDC.e: {u1 - u0:+.6f}  Δ token: {t1 - t0:+.6f}")
    info(f"  Received {tokens_received} raw token = {tokens_received / 10 ** config.TOKEN_DECIMALS:.6f}")

    # ── Step 4: Swap received tokens back to USDC.e ────────────────
    info("")
    info(f"─── Reverse swap: {tokens_received / 10 ** config.TOKEN_DECIMALS:.6f} {config.LP_TOKEN_SYMBOL} → USDC.e ───")

    # Re-read pool state (price moved slightly)
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)

    ok, usdce_received = swap_exact_in(
        w3, wallet, private_key, pool_state,
        config.LP_TOKEN_ADDRESS, config.USDCE_ADDRESS,
        tokens_received, max_slippage=0.01, gas_price=gas_price,
    )
    if not ok:
        err("Reverse swap failed — token now stuck in wallet, but no MEV risk")
        sys.exit(1)

    u2, t2 = balances()
    info(f"AFTER reverse — USDC.e: {u2:.6f}  {config.LP_TOKEN_SYMBOL}: {t2:.6f}")

    # ── Step 5: Report ──────────────────────────────────────────────
    print()
    print("=" * 70)
    print("ROUND-TRIP RESULTS")
    print("=" * 70)
    print(f"  Started:  USDC.e {u0:.6f}  token {t0:.6f}")
    print(f"  Ended:    USDC.e {u2:.6f}  token {t2:.6f}")
    print(f"  Net USDC.e change:  {u2 - u0:+.6f}  (cost = {(u0 - u2):.6f})")
    print(f"  Net token change:   {t2 - t0:+.6f}")
    print()
    expected_cost = TEST_AMOUNT_USD * 0.001  # 2x 0.05% fees
    print(f"  Expected round-trip cost (2x 0.05% fees): ~${expected_cost:.6f}")
    actual_cost = u0 - u2
    print(f"  Actual cost:                              ${actual_cost:.6f}")
    if actual_cost < TEST_AMOUNT_USD * 0.005:  # less than 0.5% total
        print()
        print("  ✓ PASS — Permit2 swap pattern works. Safe to proceed to LP mint test.")
    else:
        print()
        print("  ✗ COST UNUSUALLY HIGH — investigate before proceeding.")


if __name__ == "__main__":
    main()
