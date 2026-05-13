"""
dry_run.py — read-only simulation of LP deployment.

Reads SOFTWARE.ai pool, calculates exactly what would happen if we deployed
$LP_TOTAL_USD at ±LP_RANGE_PCT range right now. NO TRANSACTIONS SENT.

Run: python3 dry_run.py
"""
import config
from shared import info, connect, load_wallet, ERC20_ABI
from pool_state import (
    read_pool_state, sqrt_price_x96_to_price, calculate_range_ticks,
    tick_to_sqrt_price_x96, liquidity_for_amount0, liquidity_for_amount1,
    calculate_amounts_for_liquidity, tick_to_price,
)

from web3 import Web3


def main():
    print("=" * 80)
    print("LP DRY RUN — read-only simulation")
    print("=" * 80)

    w3 = connect()
    wallet, _ = load_wallet()

    # ── 1. Wallet balances ──
    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)

    eth_bal_wei = w3.eth.get_balance(wallet)
    eth_bal     = w3.from_wei(eth_bal_wei, "ether")
    usdce_raw   = usdce.functions.balanceOf(wallet).call()
    token_raw   = token.functions.balanceOf(wallet).call()
    usdce_bal   = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal   = token_raw / 10 ** config.TOKEN_DECIMALS

    print(f"\n── Wallet: {wallet} ──")
    print(f"  ETH (gas):  {float(eth_bal):.6f}")
    print(f"  USDC.e:     {usdce_bal:.4f}")
    print(f"  {config.LP_TOKEN_SYMBOL}:  {token_bal:.4f}")

    # ── 2. Pool state ──
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )

    print(f"\n── Pool: {config.LP_POOL_ADDRESS} ({config.LP_TOKEN_SYMBOL}/USDC.e {config.LP_FEE_TIER/10000:.2f}%) ──")
    print(f"  token0:        {pool_state['token0']}  ({'token' if pool_state['token_is_token0'] else 'USDC.e'})")
    print(f"  token1:        {pool_state['token1']}  ({'USDC.e' if pool_state['token_is_token0'] else 'token'})")
    print(f"  fee:           {pool_state['fee']} ({pool_state['fee']/10000:.2f}%)")
    print(f"  tick:          {pool_state['tick']}")
    print(f"  tick_spacing:  {pool_state['tick_spacing']}")
    print(f"  liquidity:     {pool_state['liquidity']:,}")
    print(f"  current price: ${price:.8f} USDC.e per {config.LP_TOKEN_SYMBOL}")

    # ── 3. Range calculation ──
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], config.LP_RANGE_PCT, pool_state["tick_spacing"]
    )
    price_lower = tick_to_price(
        tick_lower, pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    price_upper = tick_to_price(
        tick_upper, pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )

    print(f"\n── Range (±{config.LP_RANGE_PCT*100:.2f}%) ──")
    print(f"  tick_lower:    {tick_lower}  → ${min(price_lower, price_upper):.8f}")
    print(f"  tick_upper:    {tick_upper}  → ${max(price_lower, price_upper):.8f}")
    print(f"  range width:   {tick_upper - tick_lower} ticks")

    # ── 4. Capital allocation ──
    # At the midpoint of a symmetric range, V3 needs ~50/50 by USD value.
    half_usd        = config.LP_TOTAL_USD / 2.0
    needed_usdce    = half_usd
    needed_tokens   = half_usd / price if price > 0 else 0
    needed_usdce_raw  = int(needed_usdce  * 10 ** config.USDCE_DECIMALS)
    needed_tokens_raw = int(needed_tokens * 10 ** config.TOKEN_DECIMALS)

    print(f"\n── Capital deployment plan (${config.LP_TOTAL_USD:.2f}) ──")
    print(f"  USDC.e needed:  {needed_usdce:.4f}  (raw: {needed_usdce_raw})")
    print(f"  {config.LP_TOKEN_SYMBOL} needed:  {needed_tokens:.4f}  (raw: {needed_tokens_raw})")

    # ── 5. Required swap to balance ──
    swap_usdce_for_tokens = max(0, needed_tokens_raw - token_raw)  # raw token shortfall
    if swap_usdce_for_tokens > 0:
        # Convert token shortfall to USDC.e amount we'd need to swap in
        token_shortfall_human = swap_usdce_for_tokens / 10 ** config.TOKEN_DECIMALS
        usdce_swap_needed     = token_shortfall_human * price
        usdce_swap_raw        = int(usdce_swap_needed * 10 ** config.USDCE_DECIMALS)
        print(f"\n── Setup swap required ──")
        print(f"  swap ~${usdce_swap_needed:.4f} USDC.e → ~{token_shortfall_human:.4f} {config.LP_TOKEN_SYMBOL}")
        print(f"  (real slippage will be measured at execution time via on-chain swap)")

    # ── 6. Liquidity calculation for the planned mint ──
    sqrt_lower = tick_to_sqrt_price_x96(tick_lower)
    sqrt_upper = tick_to_sqrt_price_x96(tick_upper)
    sqrt_curr  = pool_state["sqrt_price_x96"]

    # Determine amount0/amount1 by token orientation
    if pool_state["token_is_token0"]:
        amount0_desired = needed_tokens_raw
        amount1_desired = needed_usdce_raw
    else:
        amount0_desired = needed_usdce_raw
        amount1_desired = needed_tokens_raw

    print(f"\n── Mint parameters (NPM.mint params) ──")
    print(f"  token0:         {pool_state['token0']}")
    print(f"  token1:         {pool_state['token1']}")
    print(f"  fee:            {pool_state['fee']}")
    print(f"  tickLower:      {tick_lower}")
    print(f"  tickUpper:      {tick_upper}")
    print(f"  amount0Desired: {amount0_desired}")
    print(f"  amount1Desired: {amount1_desired}")
    print(f"  recipient:      {wallet}")

    # ── 7. Sanity warnings ──
    print(f"\n── Sanity checks ──")
    issues = []
    if eth_bal < 0.0001:
        issues.append(f"  ✗ ETH balance too low for gas: {float(eth_bal):.6f}")
    if usdce_bal < config.LP_TOTAL_USD:
        issues.append(f"  ✗ USDC.e balance below deployment target: have ${usdce_bal:.2f}, want to deploy ${config.LP_TOTAL_USD:.2f}")
        issues.append(f"    → either fund more, or lower LP_TOTAL_USD in .env to ${usdce_bal * 0.99:.2f}")
    else:
        # Estimate slippage cost on the half-swap
        est_slippage_cost = (config.LP_TOTAL_USD / 2) * 0.005  # ~0.5% on the swapped half
        net_deployed = config.LP_TOTAL_USD - est_slippage_cost
        print(f"  ℹ USDC.e ${usdce_bal:.2f} ≥ deploy target ${config.LP_TOTAL_USD:.2f}")
        print(f"  ℹ Expected net LP value after swap slippage: ~${net_deployed:.2f}")
        print(f"     (gross deploy ${config.LP_TOTAL_USD:.2f} − ~${est_slippage_cost:.2f} swap cost)")
    if pool_state["liquidity"] == 0:
        issues.append(f"  ✗ Pool has zero liquidity")
    if not pool_state["unlocked"]:
        issues.append(f"  ✗ Pool is locked (unlocked=False)")
    if config.DRY_RUN:
        issues.append(f"  ℹ DRY_RUN=true — no transactions would be sent even if invoked")

    if issues:
        for issue in issues:
            print(issue)
    else:
        print("  ✓ All checks passed — ready for live deployment")

    print("\n" + "=" * 80)
    print("DRY RUN COMPLETE — no transactions sent")
    print("=" * 80)


if __name__ == "__main__":
    main()
