"""
recover_position.py — Recover funds from a stuck LP position.

Pass the tokenId that needs recovery. Calls decreaseLiquidity (if needed)
then collect(). Funds return to wallet.

Usage: python3 recover_position.py <tokenId>
"""
import sys
from web3 import Web3

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from position_manager import get_position, decrease_liquidity, collect_fees


def main():
    if len(sys.argv) < 2:
        err("Usage: python3 recover_position.py <tokenId>")
        sys.exit(1)
    token_id = int(sys.argv[1])

    w3 = connect()
    wallet, private_key = load_wallet()
    info(f"Wallet: {wallet}")
    info(f"Recovering tokenId: {token_id}")

    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)

    u0 = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    t0 = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS
    info(f"BEFORE — USDC.e: {u0:.6f}  {config.LP_TOKEN_SYMBOL}: {t0:.6f}")

    pos = get_position(w3, token_id)
    info(f"Position state:")
    info(f"  liquidity:     {pos['liquidity']}")
    info(f"  tokens_owed_0: {pos['tokens_owed_0']}")
    info(f"  tokens_owed_1: {pos['tokens_owed_1']}")

    gas_price = max(int(w3.eth.gas_price * 1.5), 1_000_000)

    if pos["liquidity"] > 0:
        info("decreasing liquidity to 0...")
        ok, _, _ = decrease_liquidity(
            w3, wallet, private_key, token_id, pos["liquidity"], gas_price
        )
        if not ok:
            err("decreaseLiquidity failed")
            sys.exit(1)

    info("collecting all owed tokens...")
    ok, amt0, amt1 = collect_fees(w3, wallet, private_key, token_id, gas_price)
    if not ok:
        err("collect failed")
        sys.exit(1)

    u1 = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    t1 = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS
    info(f"AFTER — USDC.e: {u1:.6f}  {config.LP_TOKEN_SYMBOL}: {t1:.6f}")
    info(f"Recovered — USDC.e: {u1-u0:+.6f}  {config.LP_TOKEN_SYMBOL}: {t1-t0:+.6f}")


if __name__ == "__main__":
    main()
