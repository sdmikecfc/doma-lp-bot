"""
lp_bot.py — main loop. Mints, monitors, rebalances LP position on a single pool.

Run with: python3 lp_bot.py
"""
import sys
import time

from web3 import Web3

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from pool_state import (
    read_pool_state, sqrt_price_x96_to_price, calculate_range_ticks,
    position_proximity_to_edge, position_in_range,
    calculate_optimal_deposit_ratio,
)
from swap_executor import swap_exact_in, ensure_swap_setup
from position_manager import (
    mint_position, get_position, decrease_liquidity, collect_fees,
    burn_position, increase_liquidity,
)
from state import (
    init_db, get_active_position, record_mint, record_burn, record_rebalance,
    record_fees, get_last_fee_collect, set_last_fee_collect,
)
from adaptive_range import (
    compute_adaptive_range, record_price_snapshot, log_adaptive_state,
    ADAPTIVE_ENABLED,
)
from datetime import datetime, timezone


def get_balances(w3, wallet):
    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)
    return (
        usdce.functions.balanceOf(wallet).call(),
        token.functions.balanceOf(wallet).call(),
    )


def to_usd(amount0_raw, amount1_raw, price_usdc_per_token, token_is_token0):
    """Convert (amount0, amount1) raw to total USD value."""
    if token_is_token0:
        token_raw, usdc_raw = amount0_raw, amount1_raw
    else:
        usdc_raw, token_raw = amount0_raw, amount1_raw
    usdc_human = usdc_raw / 10 ** config.USDCE_DECIMALS
    token_human = token_raw / 10 ** config.TOKEN_DECIMALS
    return usdc_human + token_human * price_usdc_per_token


def swap_to_optimal_ratio(w3, wallet, private_key, pool_state, tick_lower, tick_upper,
                          target_total_usd: float, price: float, gas_price: int,
                          *,
                          db_con=None, reason: str | None = None) -> bool:
    """
    Swap wallet contents so USDC.e/token ratio matches the optimal mint ratio for
    the given tick range. Returns True if balanced (or no swap needed).

    Replaces the simple "swap to 50/50 USD" approach with the actual V3 optimal
    ratio for the chosen range. Reduces post-mint dust from ~9% to <2% on tight
    ranges.

    If `db_con` and `reason` are provided, the resulting swap (if any) is
    recorded to the swap_costs table. `reason` is propagated to the recorder.
    """
    usdce_frac, token_frac = calculate_optimal_deposit_ratio(
        pool_state, tick_lower, tick_upper,
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS, price
    )
    target_usdce_usd = target_total_usd * usdce_frac
    target_token_usd = target_total_usd * token_frac
    info(f"  Optimal ratio: {usdce_frac*100:.1f}% USDC.e / {token_frac*100:.1f}% token")
    info(f"  Target: ${target_usdce_usd:.4f} USDC.e + ${target_token_usd:.4f} token")

    usdce_raw, token_raw = get_balances(w3, wallet)
    usdce_bal = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal = token_raw / 10 ** config.TOKEN_DECIMALS
    token_value = token_bal * price

    diff = usdce_bal - target_usdce_usd  # +ve = excess USDC, -ve = excess token

    if abs(diff) < 0.50:
        info(f"  Wallet within $0.50 of target — no swap needed")
        return True

    if diff > 0:
        swap_amt_raw = int(diff * 10 ** config.USDCE_DECIMALS)
        info(f"  Swap ${diff:.4f} USDC.e → token")
        ok, _ = swap_exact_in(
            w3, wallet, private_key, pool_state,
            config.USDCE_ADDRESS, config.LP_TOKEN_ADDRESS,
            swap_amt_raw, config.LP_SWAP_MAX_SLIPPAGE_PCT, gas_price,
            db_con=db_con, reason=reason,
        )
    else:
        swap_amt_token = abs(diff) / price
        swap_amt_raw = int(swap_amt_token * 10 ** config.TOKEN_DECIMALS)
        info(f"  Swap {swap_amt_token:.4f} token → USDC.e (≈${abs(diff):.4f})")
        ok, _ = swap_exact_in(
            w3, wallet, private_key, pool_state,
            config.LP_TOKEN_ADDRESS, config.USDCE_ADDRESS,
            swap_amt_raw, config.LP_SWAP_MAX_SLIPPAGE_PCT, gas_price,
            db_con=db_con, reason=reason,
        )
    return ok


def execute_initial_mint(w3, wallet, private_key, con, pool_state, gas_price):
    """First-time setup: balance wallet to 50/50 USD ratio, then mint LP.

    Reads current USDC.e + token balance. Calculates the correct swap amount
    to reach 50/50 USD target (handles cases where wallet has leftover token
    from a previous LP exit). Then mints with the balanced amounts.
    """
    info("=" * 80)
    info("INITIAL MINT — no active position, deploying capital")
    info("=" * 80)

    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    info(f"Current price: ${price:.8f}")

    usdce_raw, token_raw = get_balances(w3, wallet)
    usdce_bal   = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal   = token_raw / 10 ** config.TOKEN_DECIMALS
    token_value = token_bal * price
    total_value = usdce_bal + token_value
    info(f"Wallet: ${usdce_bal:.4f} USDC.e + ${token_value:.4f} token (≈{token_bal:.4f} {config.LP_TOKEN_SYMBOL}) = ${total_value:.4f} total")

    # Cap deployment to total wallet value (1% buffer for swap slippage)
    deploy_usd = min(config.LP_TOTAL_USD, total_value * 0.99)
    if deploy_usd < 1:
        err(f"Wallet too small to deploy: ${total_value:.4f}")
        return False
    info(f"Target deploy: ${deploy_usd:.4f}")

    # Calculate range first so we can compute the OPTIMAL ratio for it.
    range_pct = compute_adaptive_range(con)
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], range_pct, pool_state["tick_spacing"]
    )
    info(f"Step 1: balance wallet for ticks [{tick_lower}, {tick_upper}]  (range ±{range_pct*100:.3f}%)")

    if config.DRY_RUN:
        info("[DRY RUN] would balance + swap")
        return True

    if not swap_to_optimal_ratio(w3, wallet, private_key, pool_state,
                                  tick_lower, tick_upper, deploy_usd, price, gas_price,
                                  db_con=con, reason="INITIAL_MINT"):
        err("Setup swap failed")
        return False

    # Re-read pool + balances (price shifted slightly from our swap)
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    usdce_raw, token_raw = get_balances(w3, wallet)

    # Recompute ticks if pool shifted (it may have moved 1-2 ticks from our swap).
    # Re-use the same range_pct chosen above — don't re-compute mid-mint.
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], range_pct, pool_state["tick_spacing"]
    )

    # amounts in token0/token1 order
    if pool_state["token_is_token0"]:
        amount0_desired = token_raw
        amount1_desired = usdce_raw
    else:
        amount0_desired = usdce_raw
        amount1_desired = token_raw

    # amount_min=0: NPM uses whatever ratio fits the tick range. Front-running
    # protection isn't needed at our position size on a low-volatility pool.
    amount0_min = 0
    amount1_min = 0

    info(f"Step 2: mint LP at ticks [{tick_lower}, {tick_upper}]")
    ok, token_id, liq, amt0_used, amt1_used = mint_position(
        w3, wallet, private_key, pool_state,
        tick_lower, tick_upper,
        amount0_desired, amount1_desired,
        amount0_min, amount1_min, gas_price
    )
    if not ok:
        err("Mint failed")
        return False

    initial_value_usd = to_usd(amt0_used, amt1_used, sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    ), pool_state["token_is_token0"])

    record_mint(con, token_id, config.LP_POOL_ADDRESS, tick_lower, tick_upper,
                liq, amt0_used, amt1_used, initial_value_usd)
    info(f"Position deployed: tokenId={token_id}  value≈${initial_value_usd:.4f}")
    return True


def maybe_compound_position(w3, wallet, private_key, con, pool_state,
                            active_position, gas_price) -> bool:
    """
    Periodic compound: collect fees + swap idle to balance + increaseLiquidity.

    Runs at most once per LP_COMPOUND_INTERVAL_SEC.
    Skips if total wallet idle (USDC + token in USD) is below LP_COMPOUND_MIN_USD.

    1. Collect fees → moves owed fees from position to wallet
    2. Read wallet idle (USDC + token)
    3. If total idle > threshold:
       a. Swap to ~50/50 USD ratio
       b. increaseLiquidity → add idle to existing position (same range)
    """
    last_compound = get_last_fee_collect(con)  # reusing the column
    now = datetime.now(timezone.utc)
    if last_compound:
        try:
            last_dt = datetime.fromisoformat(last_compound)
            elapsed = (now - last_dt).total_seconds()
            if elapsed < config.LP_COMPOUND_INTERVAL_SEC:
                return False
        except Exception:
            pass

    token_id = active_position["token_id"]

    info("=" * 80)
    info(f"COMPOUND — collecting fees + redeploying to position {token_id}")
    info("=" * 80)

    # Step 1: collect fees from position
    ok, amt0_collected, amt1_collected = collect_fees(
        w3, wallet, private_key, token_id, gas_price
    )
    set_last_fee_collect(con)  # bump timestamp regardless of success
    if not ok:
        warn("Fee collect failed — will retry next interval")
        return False

    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )

    if amt0_collected > 0 or amt1_collected > 0:
        fee_value_usd = to_usd(amt0_collected, amt1_collected, price,
                               pool_state["token_is_token0"])
        record_fees(con, active_position["id"], amt0_collected, amt1_collected, fee_value_usd)
        info(f"  Fees collected: ≈${fee_value_usd:.6f}")
    else:
        info(f"  No fees accrued since last compound")

    # Step 2: read total wallet idle (includes just-collected fees + previous dust)
    usdce_raw, token_raw = get_balances(w3, wallet)
    usdce_bal   = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal   = token_raw / 10 ** config.TOKEN_DECIMALS
    token_value = token_bal * price
    total_idle  = usdce_bal + token_value

    info(f"  Wallet idle: ${usdce_bal:.4f} USDC + ${token_value:.4f} token = ${total_idle:.4f}")

    if total_idle < config.LP_COMPOUND_MIN_USD:
        info(f"  Idle below threshold (${config.LP_COMPOUND_MIN_USD:.2f}) — skipping compound")
        return True

    # Step 3: swap to OPTIMAL ratio for this position's tick range
    tick_lower = active_position["tick_lower"]
    tick_upper = active_position["tick_upper"]
    if not swap_to_optimal_ratio(w3, wallet, private_key, pool_state,
                                  tick_lower, tick_upper, total_idle, price, gas_price,
                                  db_con=con, reason="COMPOUND"):
        warn("Balance swap failed — skipping increaseLiquidity")
        return False

    # Step 4: increaseLiquidity with whatever's now in wallet
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    usdce_raw, token_raw = get_balances(w3, wallet)
    if pool_state["token_is_token0"]:
        amount0_desired = token_raw
        amount1_desired = usdce_raw
    else:
        amount0_desired = usdce_raw
        amount1_desired = token_raw

    info(f"  Adding to position: amount0={amount0_desired}  amount1={amount1_desired}")
    ok, liq_added, amt0_used, amt1_used = increase_liquidity(
        w3, wallet, private_key, token_id,
        amount0_desired, amount1_desired, gas_price
    )
    if not ok:
        warn("increaseLiquidity failed — capital remains in wallet, will retry next interval")
        return False

    added_value = to_usd(amt0_used, amt1_used, price, pool_state["token_is_token0"])
    info(f"  ✓ COMPOUND COMPLETE: added ${added_value:.4f} to position (liquidity +{liq_added})")
    return True


def execute_rebalance(w3, wallet, private_key, con, pool_state,
                      active_position, gas_price):
    """Burn old, swap to balance, mint new at fresh range."""
    info("=" * 80)
    info(f"REBALANCE — current tick {pool_state['tick']} hit edge of position range")
    info("=" * 80)

    token_id = active_position["token_id"]
    onchain = get_position(w3, token_id)

    info(f"Old position: tokenId={token_id}  liquidity={onchain['liquidity']}")

    if config.DRY_RUN:
        info("[DRY RUN] would decrease + collect + mint new")
        return True

    # 1. Decrease liquidity to 0 — capture principal amounts so we can
    #    separate them from fees collected in step 2.
    principal_0 = principal_1 = 0
    if onchain["liquidity"] > 0:
        ok, principal_0, principal_1 = decrease_liquidity(
            w3, wallet, private_key, token_id, onchain["liquidity"], gas_price
        )
        if not ok:
            err("decreaseLiquidity failed")
            return False

    # 2. Collect everything (decreased principal + accumulated fees, both in tokens_owed)
    ok, amt0_collected, amt1_collected = collect_fees(
        w3, wallet, private_key, token_id, gas_price
    )
    if not ok:
        err("collect failed")
        return False

    # Separate the fee portion: collected − principal = the fees that accrued
    # since the last poke. Record them as a fee_collection so the dashboard's
    # lifetime fee count survives rebalances. Without this, every rebalance
    # silently swallows whatever fees had built up on the burned position.
    fee_0 = max(0, amt0_collected - principal_0)
    fee_1 = max(0, amt1_collected - principal_1)

    price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )

    if fee_0 > 0 or fee_1 > 0:
        fee_value_usd = to_usd(fee_0, fee_1, price, pool_state["token_is_token0"])
        record_fees(con, active_position["id"], fee_0, fee_1, fee_value_usd)
        info(f"  Fees harvested during rebalance: ≈${fee_value_usd:.6f}")

    final_value = to_usd(amt0_collected, amt1_collected, price,
                         pool_state["token_is_token0"])
    record_burn(con, active_position["id"], amt0_collected, amt1_collected, final_value)
    info(f"Old position closed: returned ≈${final_value:.4f} (principal + fees)")

    # Optionally burn the empty NFT (saves nothing meaningful, can skip)

    # 3. Re-read pool state after our actions
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)

    # 4. Calculate new range (adaptive — based on recent realized volatility)
    range_pct = compute_adaptive_range(con)
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], range_pct, pool_state["tick_spacing"]
    )

    usdce_raw, token_raw = get_balances(w3, wallet)
    usdce_bal = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal = token_raw / 10 ** config.TOKEN_DECIMALS
    total_wallet_usd = usdce_bal + token_bal * price
    info(f"Rebalance: wallet ${total_wallet_usd:.4f}  range ±{range_pct*100:.3f}%  "
         f"ticks [{tick_lower}, {tick_upper}]")

    if not swap_to_optimal_ratio(w3, wallet, private_key, pool_state,
                                  tick_lower, tick_upper, total_wallet_usd, price, gas_price,
                                  db_con=con, reason="REBALANCE"):
        warn("Rebalance balance-swap failed, proceeding with current ratio")

    # 5. Re-read after possible swap. Re-use the range_pct chosen above.
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    tick_lower, tick_upper = calculate_range_ticks(
        pool_state["tick"], range_pct, pool_state["tick_spacing"]
    )

    usdce_raw, token_raw = get_balances(w3, wallet)
    if pool_state["token_is_token0"]:
        amount0_desired = token_raw
        amount1_desired = usdce_raw
    else:
        amount0_desired = usdce_raw
        amount1_desired = token_raw
    amount0_min = 0
    amount1_min = 0

    record_rebalance(con, active_position["id"], "EDGE_TRIGGER",
                     active_position["tick_lower"], active_position["tick_upper"],
                     tick_lower, tick_upper, pool_state["tick"], price)

    ok, new_token_id, liq, amt0_used, amt1_used = mint_position(
        w3, wallet, private_key, pool_state,
        tick_lower, tick_upper,
        amount0_desired, amount1_desired,
        amount0_min, amount1_min, gas_price
    )
    if not ok:
        err("Mint of new position failed")
        return False

    new_value = to_usd(amt0_used, amt1_used, price, pool_state["token_is_token0"])
    record_mint(con, new_token_id, config.LP_POOL_ADDRESS, tick_lower, tick_upper,
                liq, amt0_used, amt1_used, new_value)
    info(f"Rebalance complete: new tokenId={new_token_id}  value≈${new_value:.4f}")

    # ── 6. Post-rebalance dust cleanup ──────────────────────────────────
    # Mint frequently leaves dust because the optimal ratio shifts when the
    # swap moves the price. Run a second swap-to-optimal + increaseLiquidity
    # using the NEW position's actual ticks. Cheap operation that absorbs
    # what the mint couldn't.
    pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
    usdce_raw, token_raw = get_balances(w3, wallet)
    usdce_bal = usdce_raw / 10 ** config.USDCE_DECIMALS
    token_bal = token_raw / 10 ** config.TOKEN_DECIMALS
    cur_price = sqrt_price_x96_to_price(
        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
    )
    leftover_usd = usdce_bal + token_bal * cur_price

    if leftover_usd >= 1.0:
        info(f"Post-rebalance cleanup: ${leftover_usd:.4f} idle, absorbing into new position")

        # Swap to optimal ratio for the NEW position's actual ticks
        if not swap_to_optimal_ratio(w3, wallet, private_key, pool_state,
                                      tick_lower, tick_upper, leftover_usd,
                                      cur_price, gas_price,
                                      db_con=con, reason="REBALANCE_CLEANUP"):
            warn("Cleanup swap failed — leftover stays for next compound")
            return True

        # Re-read after swap
        pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
        usdce_raw, token_raw = get_balances(w3, wallet)
        if pool_state["token_is_token0"]:
            amount0_des = token_raw
            amount1_des = usdce_raw
        else:
            amount0_des = usdce_raw
            amount1_des = token_raw

        ok2, liq_added, a0u, a1u = increase_liquidity(
            w3, wallet, private_key, new_token_id,
            amount0_des, amount1_des, gas_price
        )
        if ok2:
            cleanup_value = to_usd(a0u, a1u, cur_price, pool_state["token_is_token0"])
            info(f"Post-rebalance cleanup ✓: added ${cleanup_value:.4f} (liquidity +{liq_added})")
        else:
            warn("Cleanup increaseLiquidity failed — leftover stays for next compound")
    else:
        info(f"Post-rebalance: only ${leftover_usd:.4f} dust, skipping cleanup")

    return True


def main():
    if config.DRY_RUN:
        info("=" * 80)
        info("  DRY RUN MODE — no real transactions")
        info("=" * 80)

    con = init_db()
    w3 = connect()
    wallet, private_key = load_wallet()
    info(f"Wallet: {wallet}")
    info(f"Pool:   {config.LP_POOL_ADDRESS} ({config.LP_TOKEN_SYMBOL} 0.05%)")
    if ADAPTIVE_ENABLED:
        info(f"Range:  ADAPTIVE (computed from realized 24h volatility per cycle)")
    else:
        info(f"Range:  ±{config.LP_RANGE_PCT*100:.2f}% (static)")
    info(f"Loop:   every {config.LP_LOOP_INTERVAL_SEC}s")

    # ETH balance check — at Doma's gas prices each tx costs ~0.0000005 ETH.
    # 0.0001 ETH covers ~200 txs, plenty for many rebalances.
    eth_bal = w3.eth.get_balance(wallet)
    if eth_bal < w3.to_wei(0.0001, "ether"):
        err(f"ETH balance too low for gas: {float(w3.from_wei(eth_bal, 'ether')):.6f}")
        err("Fund wallet with at least 0.0005 ETH before running.")
        sys.exit(1)
    info(f"ETH balance: {float(w3.from_wei(eth_bal, 'ether')):.6f}")

    # One-time Permit2 setup for both tokens (idempotent — skips if already set)
    if not config.DRY_RUN:
        gas_price = max(int(w3.eth.gas_price * 1.2), 1_000_000)
        info("Verifying Permit2 setup...")
        if not ensure_swap_setup(w3, wallet, private_key, config.USDCE_ADDRESS, gas_price):
            err("USDC.e Permit2 setup failed")
            sys.exit(1)
        if not ensure_swap_setup(w3, wallet, private_key, config.LP_TOKEN_ADDRESS, gas_price):
            err("Token Permit2 setup failed")
            sys.exit(1)

    cycle = 0
    last_log = 0.0
    last_snapshot = 0.0
    while True:
        cycle += 1
        try:
            pool_state = read_pool_state(w3, config.LP_POOL_ADDRESS)
            gas_price = max(int(w3.eth.gas_price * 1.2), 1_000_000)

            # Record price snapshot for adaptive ranging (every ~30s, not every loop)
            now = time.time()
            if now - last_snapshot >= 30:
                last_snapshot = now
                price_now = sqrt_price_x96_to_price(
                    pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
                    config.TOKEN_DECIMALS, config.USDCE_DECIMALS,
                )
                record_price_snapshot(con, price_now)

            active = get_active_position(con)

            if not active:
                # No position — initial deployment
                execute_initial_mint(w3, wallet, private_key, con, pool_state, gas_price)
            else:
                # Have position — check if rebalance needed
                proximity = position_proximity_to_edge(
                    pool_state["tick"], active["tick_lower"], active["tick_upper"]
                )
                in_range = position_in_range(
                    pool_state["tick"], active["tick_lower"], active["tick_upper"]
                )

                # Rebalance trigger: at edge OR fully out of range
                if not in_range or proximity >= (1 - config.LP_REBALANCE_TRIGGER_PCT):
                    execute_rebalance(w3, wallet, private_key, con,
                                      pool_state, active, gas_price)
                else:
                    # Periodic compound (collect fees + redeploy to position)
                    maybe_compound_position(w3, wallet, private_key, con,
                                            pool_state, active, gas_price)

                # Periodic status log (every ~60s)
                if now - last_log >= 60:
                    last_log = now
                    price = sqrt_price_x96_to_price(
                        pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
                        config.TOKEN_DECIMALS, config.USDCE_DECIMALS
                    )
                    info(f"[cycle {cycle}] tick={pool_state['tick']}  "
                         f"range=[{active['tick_lower']}, {active['tick_upper']}]  "
                         f"in_range={in_range}  proximity={proximity:.2f}  "
                         f"price=${price:.8f}")
                    log_adaptive_state(con)

        except Exception as e:
            err(f"loop error: {e}")
            time.sleep(10)
            continue

        time.sleep(config.LP_LOOP_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        info("Stopped by user.")
    except Exception as e:
        err(f"Fatal: {e}")
        raise
