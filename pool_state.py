"""
pool_state.py — read SOFTWARE.ai V3 pool and compute prices, ticks, ranges.
"""
import math
from decimal import Decimal, getcontext

from web3 import Web3

import config
from shared import POOL_ABI, info

getcontext().prec = 50


def read_pool_state(w3, pool_address: str) -> dict:
    """Returns full pool state from on-chain."""
    pool = w3.eth.contract(
        address=Web3.to_checksum_address(pool_address), abi=POOL_ABI
    )
    slot0 = pool.functions.slot0().call()
    state = {
        "pool":          pool,
        "token0":        Web3.to_checksum_address(pool.functions.token0().call()),
        "token1":        Web3.to_checksum_address(pool.functions.token1().call()),
        "fee":           pool.functions.fee().call(),
        "liquidity":     pool.functions.liquidity().call(),
        "tick_spacing":  pool.functions.tickSpacing().call(),
        "sqrt_price_x96": slot0[0],
        "tick":          slot0[1],
        "unlocked":      slot0[6],
    }
    state["token_is_token0"] = state["token0"].lower() == config.LP_TOKEN_ADDRESS.lower()
    return state


def sqrt_price_x96_to_price(sqrt_price_x96: int, token_is_token0: bool,
                             token_decimals: int, usdce_decimals: int) -> float:
    """Convert sqrtPriceX96 to USDC.e per token (human-readable)."""
    if sqrt_price_x96 == 0:
        return 0.0
    raw = Decimal(sqrt_price_x96) ** 2 / Decimal(2 ** 192)
    multiplier = Decimal(10 ** token_decimals) / Decimal(10 ** usdce_decimals)
    if token_is_token0:
        return float(raw * multiplier)
    else:
        return float(multiplier / raw)


def tick_to_price(tick: int, token_is_token0: bool,
                  token_decimals: int, usdce_decimals: int) -> float:
    """Convert a tick value to USDC.e per token."""
    raw = Decimal("1.0001") ** tick
    multiplier = Decimal(10 ** token_decimals) / Decimal(10 ** usdce_decimals)
    if token_is_token0:
        return float(raw * multiplier)
    else:
        return float(multiplier / raw)


def price_to_tick(price: float, token_is_token0: bool,
                  token_decimals: int, usdce_decimals: int) -> int:
    """Convert a USDC.e per token price to the V3 tick."""
    multiplier = Decimal(10 ** token_decimals) / Decimal(10 ** usdce_decimals)
    if token_is_token0:
        raw = Decimal(str(price)) / multiplier
    else:
        raw = multiplier / Decimal(str(price))
    return int(math.log(float(raw)) / math.log(1.0001))


def calculate_range_ticks(current_tick: int, range_pct: float,
                          tick_spacing: int) -> tuple[int, int]:
    """
    Given current tick and a desired ±range_pct, return the tick range
    snapped to tick_spacing.

    1 tick = 0.01% price change. So ±0.5% = ±50 ticks roughly.
    More precisely: range_ticks = log(1 + range_pct) / log(1.0001)
    """
    range_ticks = int(math.log(1 + range_pct) / math.log(1.0001))

    raw_lower = current_tick - range_ticks
    raw_upper = current_tick + range_ticks

    # Snap to tick_spacing — round outward to ensure range covers our intent
    tick_lower = (raw_lower // tick_spacing) * tick_spacing
    tick_upper = ((raw_upper + tick_spacing - 1) // tick_spacing) * tick_spacing

    # Clamp to valid V3 tick range
    tick_lower = max(tick_lower, config.MIN_TICK)
    tick_upper = min(tick_upper, config.MAX_TICK)

    return tick_lower, tick_upper


def position_in_range(current_tick: int, tick_lower: int, tick_upper: int) -> bool:
    return tick_lower <= current_tick < tick_upper


def position_proximity_to_edge(current_tick: int, tick_lower: int, tick_upper: int) -> float:
    """
    Returns 0.0 if at center, 1.0 if at edge or out of range.
    Used to decide when to rebalance.
    """
    if not position_in_range(current_tick, tick_lower, tick_upper):
        return 1.0
    range_size = tick_upper - tick_lower
    if range_size == 0:
        return 1.0
    center = (tick_lower + tick_upper) / 2
    distance_from_center = abs(current_tick - center)
    half_range = range_size / 2
    return min(distance_from_center / half_range, 1.0)


def liquidity_for_amount0(sqrt_price_lower_x96: int, sqrt_price_upper_x96: int,
                          amount0: int) -> int:
    """V3 liquidity math: how much L for a given amount of token0 across a range."""
    if sqrt_price_lower_x96 > sqrt_price_upper_x96:
        sqrt_price_lower_x96, sqrt_price_upper_x96 = sqrt_price_upper_x96, sqrt_price_lower_x96
    intermediate = (sqrt_price_lower_x96 * sqrt_price_upper_x96) // (2 ** 96)
    return (amount0 * intermediate) // (sqrt_price_upper_x96 - sqrt_price_lower_x96)


def liquidity_for_amount1(sqrt_price_lower_x96: int, sqrt_price_upper_x96: int,
                          amount1: int) -> int:
    """V3 liquidity math: how much L for a given amount of token1 across a range."""
    if sqrt_price_lower_x96 > sqrt_price_upper_x96:
        sqrt_price_lower_x96, sqrt_price_upper_x96 = sqrt_price_upper_x96, sqrt_price_lower_x96
    return (amount1 * (2 ** 96)) // (sqrt_price_upper_x96 - sqrt_price_lower_x96)


def tick_to_sqrt_price_x96(tick: int) -> int:
    """Convert a tick value to sqrtPriceX96 using the V3 formula."""
    sqrt_price = (1.0001 ** (tick / 2))
    return int(sqrt_price * (2 ** 96))


def calculate_optimal_deposit_ratio(pool_state: dict, tick_lower: int, tick_upper: int,
                                     token_decimals: int, usdce_decimals: int,
                                     price_usdc_per_token: float) -> tuple[float, float]:
    """
    For a V3 position at the given tick range, compute the optimal USD ratio for
    depositing. Returns (usdce_fraction, token_fraction) summing to 1.0.

    Ratio depends on where current price sits within the range. At midpoint of
    a symmetric range it's roughly 50/50. Off-center, it skews toward the side
    with more "room left" before exit.
    """
    sqrt_p = pool_state["sqrt_price_x96"]
    sqrt_lower = tick_to_sqrt_price_x96(tick_lower)
    sqrt_upper = tick_to_sqrt_price_x96(tick_upper)

    # Use a large reference L; actual L scales linearly so ratio is invariant
    L_ref = 10 ** 18
    amount0_raw, amount1_raw = calculate_amounts_for_liquidity(
        sqrt_p, sqrt_lower, sqrt_upper, L_ref
    )

    # Map token0/token1 → USDC/token based on pool orientation
    if pool_state["token_is_token0"]:
        token_raw, usdce_raw = amount0_raw, amount1_raw
    else:
        usdce_raw, token_raw = amount0_raw, amount1_raw

    usdce_usd = usdce_raw / 10 ** usdce_decimals             # USDC at $1
    token_usd = token_raw / 10 ** token_decimals * price_usdc_per_token

    total = usdce_usd + token_usd
    if total <= 0:
        return (0.5, 0.5)
    return (usdce_usd / total, token_usd / total)


Q128 = 1 << 128
Q256 = 1 << 256


def compute_uncollected_fees(pool, position_data: dict, current_tick: int) -> tuple[int, int]:
    """
    Returns (uncollected_fees_0, uncollected_fees_1) in raw token units —
    the *real* current accrued fees on the position, not just the stale
    `tokensOwed*` values that only refresh when the position is poked.

    Mirrors Uniswap V3's NonfungiblePositionManager `_collect()` logic:

        feeGrowthInside  = feeGrowthGlobal − feeGrowthBelow_lower − feeGrowthAbove_upper
        delta            = feeGrowthInside − position.feeGrowthInside_last
        accrued_fees     = (delta × liquidity) >> 128
        total_fees       = tokensOwed + accrued_fees

    All subtractions are mod 2^256 to mirror Solidity's unchecked-block wraparound.

    Args:
      pool          : web3 contract handle for the V3 pool (POOL_ABI)
      position_data : dict from position_manager.get_position() — must include
                      tick_lower, tick_upper, liquidity,
                      fee_growth_inside_0, fee_growth_inside_1,
                      tokens_owed_0, tokens_owed_1
      current_tick  : int — slot0().tick at the moment of this call
    """
    tick_lower = position_data["tick_lower"]
    tick_upper = position_data["tick_upper"]
    liquidity  = position_data["liquidity"]
    owed_0     = position_data["tokens_owed_0"]
    owed_1     = position_data["tokens_owed_1"]

    # Position has been fully decreased — only `tokensOwed` matter
    if liquidity == 0:
        return owed_0, owed_1

    # Pool-wide accumulators
    fg_global_0 = pool.functions.feeGrowthGlobal0X128().call()
    fg_global_1 = pool.functions.feeGrowthGlobal1X128().call()

    # Tick boundary state (8-tuple per ticks(int24))
    tick_lo = pool.functions.ticks(tick_lower).call()
    tick_hi = pool.functions.ticks(tick_upper).call()
    fg_out_lo_0 = tick_lo[2]
    fg_out_lo_1 = tick_lo[3]
    fg_out_hi_0 = tick_hi[2]
    fg_out_hi_1 = tick_hi[3]

    # Fee growth below the lower tick
    if current_tick >= tick_lower:
        fg_below_0 = fg_out_lo_0
        fg_below_1 = fg_out_lo_1
    else:
        fg_below_0 = (fg_global_0 - fg_out_lo_0) % Q256
        fg_below_1 = (fg_global_1 - fg_out_lo_1) % Q256

    # Fee growth above the upper tick
    if current_tick < tick_upper:
        fg_above_0 = fg_out_hi_0
        fg_above_1 = fg_out_hi_1
    else:
        fg_above_0 = (fg_global_0 - fg_out_hi_0) % Q256
        fg_above_1 = (fg_global_1 - fg_out_hi_1) % Q256

    # Inside = global − below − above
    fg_inside_0 = (fg_global_0 - fg_below_0 - fg_above_0) % Q256
    fg_inside_1 = (fg_global_1 - fg_below_1 - fg_above_1) % Q256

    # Delta vs the position's last checkpoint
    delta_0 = (fg_inside_0 - position_data["fee_growth_inside_0"]) % Q256
    delta_1 = (fg_inside_1 - position_data["fee_growth_inside_1"]) % Q256

    accrued_0 = (delta_0 * liquidity) >> 128
    accrued_1 = (delta_1 * liquidity) >> 128

    return owed_0 + accrued_0, owed_1 + accrued_1


def calculate_amounts_for_liquidity(sqrt_price_x96: int, sqrt_price_lower_x96: int,
                                    sqrt_price_upper_x96: int,
                                    liquidity: int) -> tuple[int, int]:
    """How much token0 and token1 are needed (or returned) for a given L at a price/range."""
    if sqrt_price_x96 <= sqrt_price_lower_x96:
        amount0 = (liquidity * (2 ** 96) * (sqrt_price_upper_x96 - sqrt_price_lower_x96)) // (sqrt_price_lower_x96 * sqrt_price_upper_x96)
        amount1 = 0
    elif sqrt_price_x96 < sqrt_price_upper_x96:
        amount0 = (liquidity * (2 ** 96) * (sqrt_price_upper_x96 - sqrt_price_x96)) // (sqrt_price_x96 * sqrt_price_upper_x96)
        amount1 = (liquidity * (sqrt_price_x96 - sqrt_price_lower_x96)) // (2 ** 96)
    else:
        amount0 = 0
        amount1 = (liquidity * (sqrt_price_upper_x96 - sqrt_price_lower_x96)) // (2 ** 96)
    return amount0, amount1
