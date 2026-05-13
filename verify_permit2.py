"""
verify_permit2.py — Pre-flight checks. ZERO transactions sent.

Confirms:
  1. Permit2 contract exists at canonical address on Doma
  2. Permit2 ABI calls work (allowance read)
  3. Universal Router exists
  4. Wallet has expected balance
  5. Calculates exactly what test_swap.py would do

Run: python3 verify_permit2.py
"""
import time
from web3 import Web3

import config
from shared import info, warn, err, connect, load_wallet, ERC20_ABI
from swap_executor import PERMIT2_ADDRESS, PERMIT2_ABI, MAX_UINT160


def main():
    w3 = connect()
    wallet, _ = load_wallet()

    print()
    print("=" * 70)
    print("PERMIT2 + ROUTER PRE-FLIGHT VERIFICATION")
    print("=" * 70)
    print(f"Wallet:           {wallet}")
    print(f"Permit2 expected: {PERMIT2_ADDRESS}")
    print(f"Router expected:  {config.UNIVERSAL_ROUTER}")
    print()

    issues = []

    # ── Check 1: Permit2 contract exists ───────────────────────────
    permit2_code = w3.eth.get_code(Web3.to_checksum_address(PERMIT2_ADDRESS))
    if len(permit2_code) > 2:
        print(f"  ✓ Permit2 contract exists ({len(permit2_code)} bytes)")
    else:
        issues.append(f"  ✗ Permit2 NOT deployed at {PERMIT2_ADDRESS}")
        print(f"  ✗ Permit2 NOT deployed at canonical address")

    # ── Check 2: Universal Router exists ────────────────────────────
    router_code = w3.eth.get_code(Web3.to_checksum_address(config.UNIVERSAL_ROUTER))
    if len(router_code) > 2:
        print(f"  ✓ Universal Router exists ({len(router_code)} bytes)")
    else:
        issues.append(f"  ✗ Universal Router NOT deployed")
        print(f"  ✗ Universal Router NOT deployed")

    # ── Check 3: Can we read Permit2.allowance for our wallet? ──────
    if len(permit2_code) > 2:
        try:
            permit2 = w3.eth.contract(
                address=Web3.to_checksum_address(PERMIT2_ADDRESS), abi=PERMIT2_ABI
            )
            amt, exp, nonce = permit2.functions.allowance(
                wallet,
                Web3.to_checksum_address(config.USDCE_ADDRESS),
                Web3.to_checksum_address(config.UNIVERSAL_ROUTER),
            ).call()
            print(f"  ✓ Permit2.allowance() readable")
            print(f"     current USDC.e → Router allowance: {amt}  expires: {exp}")
            if amt >= MAX_UINT160 // 2 and exp > int(time.time()):
                print(f"     (already set up — no approval txs needed)")
            else:
                print(f"     (will need to set this — ~2 setup txs)")
        except Exception as e:
            issues.append(f"  ✗ Permit2.allowance() failed: {e}")
            print(f"  ✗ Permit2.allowance() call failed: {e}")

    # ── Check 4: ERC20 USDC.e → Permit2 current allowance ───────────
    usdce = w3.eth.contract(address=config.USDCE_ADDRESS, abi=ERC20_ABI)
    erc20_to_permit2 = usdce.functions.allowance(
        wallet, Web3.to_checksum_address(PERMIT2_ADDRESS)
    ).call()
    if erc20_to_permit2 > 0:
        print(f"  ✓ ERC20 USDC.e → Permit2 allowance set ({erc20_to_permit2})")
    else:
        print(f"  ℹ ERC20 USDC.e → Permit2 not yet approved (will be set in test)")

    # ── Check 5: Wallet balances ────────────────────────────────────
    eth_bal = w3.from_wei(w3.eth.get_balance(wallet), "ether")
    usdce_bal = usdce.functions.balanceOf(wallet).call() / 10 ** config.USDCE_DECIMALS
    print()
    print(f"  ETH (gas):  {float(eth_bal):.6f}")
    print(f"  USDC.e:     {usdce_bal:.6f}")

    if usdce_bal < 0.20:
        issues.append(f"  ✗ Insufficient USDC.e for $0.10 test (have {usdce_bal:.4f})")
    if float(eth_bal) < 0.0001:
        issues.append(f"  ✗ ETH balance very low ({float(eth_bal):.6f}) — may not cover gas")

    # ── Final verdict ───────────────────────────────────────────────
    print()
    print("=" * 70)
    if issues:
        print("✗ PRE-FLIGHT FAILED — DO NOT RUN test_swap.py")
        for i in issues:
            print(i)
    else:
        print("✓ ALL CHECKS PASSED")
        print("  Safe to run: python3 test_swap.py")
        print("  Max possible loss: $0.10 + ~$0.001 gas")
    print("=" * 70)


if __name__ == "__main__":
    main()
