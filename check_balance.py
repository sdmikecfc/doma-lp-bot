"""Quick wallet balance + activity check after the failed swap."""
from web3 import Web3
import config
from shared import connect, load_wallet, ERC20_ABI


def main():
    w3 = connect()
    wallet, _ = load_wallet()

    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    token = w3.eth.contract(address=config.LP_TOKEN_ADDRESS, abi=ERC20_ABI)

    eth = w3.from_wei(w3.eth.get_balance(wallet), "ether")
    usdce_bal = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    token_bal = token.functions.balanceOf(wallet).call() / 10 ** config.TOKEN_DECIMALS

    print()
    print("=" * 60)
    print(f"Wallet:  {wallet}")
    print(f"  ETH:        {float(eth):.6f}")
    print(f"  USDC.e:     {usdce_bal:.6f}")
    print(f"  {config.LP_TOKEN_SYMBOL}:    {token_bal:.6f}")
    print("=" * 60)
    print()
    print("Expected breakdown:")
    print(f"  Started with:  150.0000 USDC.e")
    print(f"  Transferred:   -74.2500  (to router, swept by someone else)")
    print(f"  Should have:    75.7500 USDC.e remaining")
    print(f"  Diff vs above: {usdce_bal - 75.75:+.4f}")


if __name__ == "__main__":
    main()
