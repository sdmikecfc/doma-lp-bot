"""
recover_router_tokens.py — Recover USDC.e stuck in the Universal Router.

The router's SWEEP command (0x04) sends any tokens it holds back to a recipient.
Format: execute(commands=0x04, inputs=[abi.encode(token, recipient, minAmount)])
"""
import time
from eth_abi import encode
from web3 import Web3

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI, ROUTER_ABI


def main():
    w3 = connect()
    wallet, private_key = load_wallet()
    info(f"Wallet: {wallet}")

    # 1. Verify router has our tokens
    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    router_addr = Web3.to_checksum_address(config.UNIVERSAL_ROUTER)
    router_bal = usdce.functions.balanceOf(router_addr).call()
    router_bal_human = router_bal / 10 ** config.USDCE_DECIMALS

    print()
    print("=" * 70)
    print(f"Router USDC.e balance: {router_bal_human:.6f}")
    print("=" * 70)

    if router_bal == 0:
        warn("Router holds 0 USDC.e — nothing to recover")
        return

    # 2. Build SWEEP command
    # SWEEP input format: abi.encode(token, recipient, minAmountOut)
    sweep_input = encode(
        ["address", "address", "uint256"],
        [Web3.to_checksum_address(config.USDCE_ADDRESS), wallet, 0],
    )

    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
    deadline = int(time.time()) + 600

    info(f"Attempting SWEEP of {router_bal_human:.4f} USDC.e back to wallet...")

    nonce = w3.eth.get_transaction_count(wallet)
    gas_price = max(int(w3.eth.gas_price * 1.5), 1_000_000)

    try:
        gas_est = router.functions.execute(
            bytes([0x04]), [sweep_input], deadline
        ).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
        info(f"Gas estimate: {gas_est}")
    except Exception as e:
        warn(f"Gas estimation failed: {e}")
        gas_limit = 200_000

    tx = router.functions.execute(
        bytes([0x04]), [sweep_input], deadline
    ).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })

    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    info(f"SWEEP tx: {tx_hash.hex()}")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    if receipt.status == 1:
        info("SWEEP confirmed")
        new_router_bal = usdce.functions.balanceOf(router_addr).call() / 10 ** config.USDCE_DECIMALS
        new_wallet_bal = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
        print()
        print("=" * 70)
        print(f"Router balance after: {new_router_bal:.6f} USDC.e")
        print(f"Wallet balance:       {new_wallet_bal:.6f} USDC.e")
        print("=" * 70)
    else:
        err("SWEEP failed")
        info(f"Check on explorer: https://explorer.doma.xyz/tx/0x{tx_hash.hex()}")


if __name__ == "__main__":
    main()
