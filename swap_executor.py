"""
swap_executor.py — Universal Router V3 swap via Permit2.

Pattern: Permit2 holds the allowance, router pulls atomically during swap.
No transfer-first / no leftover tokens / no MEV-sweepable window.

One-time setup per token:
  1. ERC20.approve(token → Permit2, MAX_UINT256)
  2. Permit2.approve(token, ROUTER, MAX_UINT160, far_future_expiration)

Per swap:
  router.execute(
      commands = [V3_SWAP_EXACT_IN],
      inputs = [encode(recipient, amountIn, amountOutMin, path, payerIsUser=true)],
      deadline,
  )

Permit2 is atomic with the swap call — no window for MEV to claim funds.
"""
import time

from eth_abi import encode
from eth_abi.packed import encode_packed
from web3 import Web3

import config
from shared import info, warn, err, ERC20_ABI, ROUTER_ABI, MAX_UINT256


# Canonical Permit2 address (same on every chain that uses Uniswap V3)
PERMIT2_ADDRESS = "0x000000000022D473030F116dDEE9F6B43aC78BA3"

# Permit2's amount field is uint160, expiration is uint48
MAX_UINT160 = 2 ** 160 - 1
MAX_UINT48  = 2 ** 48 - 1
# Set expiration to year 2100 — effectively never expires
PERMIT2_EXPIRATION = 4102444800   # 2100-01-01 UTC

# Universal Router commands
CMD_V3_SWAP_EXACT_IN = 0x00


PERMIT2_ABI = [
    {"name": "approve", "type": "function", "stateMutability": "nonpayable",
     "inputs": [
         {"name": "token",      "type": "address"},
         {"name": "spender",    "type": "address"},
         {"name": "amount",     "type": "uint160"},
         {"name": "expiration", "type": "uint48"},
     ],
     "outputs": []},
    {"name": "allowance", "type": "function", "stateMutability": "view",
     "inputs": [
         {"name": "user",    "type": "address"},
         {"name": "token",   "type": "address"},
         {"name": "spender", "type": "address"},
     ],
     "outputs": [
         {"name": "amount",     "type": "uint160"},
         {"name": "expiration", "type": "uint48"},
         {"name": "nonce",      "type": "uint48"},
     ]},
]


def send_tx(w3, tx, private_key, label: str):
    """Sign, send, wait. Returns receipt or None."""
    try:
        signed = w3.eth.account.sign_transaction(tx, private_key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        info(f"{label} tx: 0x{tx_hash.hex().lstrip('0x')}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        if receipt.status != 1:
            err(f"{label} FAILED (status=0)")
            return None
        info(f"{label} ✓ block {receipt.blockNumber}")
        return receipt
    except Exception as e:
        err(f"{label} exception: {e}")
        return None


def ensure_erc20_to_permit2(w3, wallet, private_key, token_addr: str,
                             gas_price: int) -> bool:
    """Standard ERC20 approve from user → Permit2 contract. One-time, MAX."""
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
    )
    permit2 = Web3.to_checksum_address(PERMIT2_ADDRESS)

    current = token.functions.allowance(wallet, permit2).call()
    if current >= MAX_UINT256 // 2:
        info(f"  ERC20 allowance {token_addr[:10]} → Permit2 already set")
        return True

    info(f"  Approving {token_addr[:10]} → Permit2 (ERC20 approve, one-time)")
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = token.functions.approve(permit2, MAX_UINT256).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 80_000

    tx = token.functions.approve(permit2, MAX_UINT256).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID,
    })
    return send_tx(w3, tx, private_key, "ERC20 approve→Permit2") is not None


def ensure_permit2_to_router(w3, wallet, private_key, token_addr: str,
                              gas_price: int) -> bool:
    """Set Permit2 allowance: router can pull `token` from user up to MAX_UINT160."""
    permit2 = w3.eth.contract(
        address=Web3.to_checksum_address(PERMIT2_ADDRESS), abi=PERMIT2_ABI
    )
    router = Web3.to_checksum_address(config.UNIVERSAL_ROUTER)

    current = permit2.functions.allowance(
        wallet, Web3.to_checksum_address(token_addr), router
    ).call()
    current_amount, current_expiration, _ = current

    # Already set with adequate amount and not expired?
    if current_amount >= MAX_UINT160 // 2 and current_expiration > int(time.time()) + 86400:
        info(f"  Permit2 allowance {token_addr[:10]} → Router already set")
        return True

    info(f"  Setting Permit2 allowance {token_addr[:10]} → Router (one-time)")
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = permit2.functions.approve(
            Web3.to_checksum_address(token_addr), router,
            MAX_UINT160, PERMIT2_EXPIRATION
        ).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 80_000

    tx = permit2.functions.approve(
        Web3.to_checksum_address(token_addr), router,
        MAX_UINT160, PERMIT2_EXPIRATION
    ).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID,
    })
    return send_tx(w3, tx, private_key, "Permit2 approve→Router") is not None


def ensure_swap_setup(w3, wallet, private_key, token_addr: str, gas_price: int) -> bool:
    """Run both approval steps for a token. Idempotent — no-op if already set."""
    if not ensure_erc20_to_permit2(w3, wallet, private_key, token_addr, gas_price):
        return False
    if not ensure_permit2_to_router(w3, wallet, private_key, token_addr, gas_price):
        return False
    return True


def estimate_amount_out(pool_state: dict, amount_in_raw: int,
                        token_in_is_token0: bool) -> int:
    """Estimate output from slot0 price. Used for slippage protection."""
    sqrt_p = pool_state["sqrt_price_x96"]
    if sqrt_p == 0:
        return 0
    if token_in_is_token0:
        return (amount_in_raw * sqrt_p * sqrt_p) // (2 ** 192)
    else:
        return (amount_in_raw * (2 ** 192)) // (sqrt_p * sqrt_p)


def swap_exact_in(w3, wallet, private_key, pool_state: dict,
                  token_in_addr: str, token_out_addr: str,
                  amount_in_raw: int, max_slippage: float,
                  gas_price: int,
                  *,
                  db_con=None, reason: str | None = None) -> tuple[bool, int]:
    """
    Swap exact amount in for token out via Permit2 + Universal Router.
    Returns (success, amount_out_received_raw).

    Caller must have called ensure_swap_setup(token_in_addr) at least once.

    If `db_con` and `reason` are provided, the swap's slippage + pool-fee
    cost is recorded to the swap_costs table on success. Cost is computed
    using the pool's pre-swap price for both sides:
        cost_usd = amount_in_usd − amount_out_usd
    """
    token_in_is_token0 = token_in_addr.lower() == pool_state["token0"].lower()
    expected_out = estimate_amount_out(pool_state, amount_in_raw, token_in_is_token0)
    min_out = int(expected_out * (1 - max_slippage))

    # Skip dust swaps that would round to zero anyway
    if amount_in_raw < 100 or expected_out < 100:
        info(f"swap skipped: amount_in={amount_in_raw} expected_out={expected_out} too small")
        return True, 0

    info(f"swap: {amount_in_raw} (in) → expect {expected_out}, min {min_out}, slip {max_slippage*100:.2f}%")

    # Track received amount via balance delta (definitive)
    token_out = w3.eth.contract(
        address=Web3.to_checksum_address(token_out_addr), abi=ERC20_ABI
    )
    bal_before = token_out.functions.balanceOf(wallet).call()

    # Build the V3_SWAP_EXACT_IN command input
    # path = tokenIn + fee(3 bytes) + tokenOut
    path = encode_packed(
        ["address", "uint24", "address"],
        [Web3.to_checksum_address(token_in_addr), pool_state["fee"],
         Web3.to_checksum_address(token_out_addr)],
    )

    # V3_SWAP_EXACT_IN input: (recipient, amountIn, amountOutMin, path, payerIsUser)
    swap_input = encode(
        ["address", "uint256", "uint256", "bytes", "bool"],
        [wallet, amount_in_raw, min_out, path, True],   # payerIsUser=True (Permit2)
    )

    router = w3.eth.contract(
        address=Web3.to_checksum_address(config.UNIVERSAL_ROUTER), abi=ROUTER_ABI
    )
    deadline = int(time.time()) + 600

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = router.functions.execute(
            bytes([CMD_V3_SWAP_EXACT_IN]), [swap_input], deadline
        ).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception as e:
        warn(f"  swap gas estimate failed: {e}, using fallback")
        gas_limit = 400_000

    tx = router.functions.execute(
        bytes([CMD_V3_SWAP_EXACT_IN]), [swap_input], deadline
    ).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })

    receipt = send_tx(w3, tx, private_key, "swap")
    if receipt is None:
        return False, 0

    bal_after = token_out.functions.balanceOf(wallet).call()
    received = bal_after - bal_before
    if received <= 0:
        err(f"swap returned 0 tokens (bal before={bal_before}, after={bal_after})")
        return False, 0

    info(f"swap ✓ received {received} (raw)")

    # Record slippage + pool-fee cost to DB if caller passed a connection
    if db_con is not None and reason is not None:
        try:
            from pool_state import sqrt_price_x96_to_price
            from state import record_swap_cost

            # Use pre-swap price for both sides — captures the haircut from
            # price impact + pool fee in a single number.
            pre_price = sqrt_price_x96_to_price(
                pool_state["sqrt_price_x96"], pool_state["token_is_token0"],
                config.TOKEN_DECIMALS, config.USDCE_DECIMALS,
            )

            is_usdce_in = token_in_addr.lower() == config.USDCE_ADDRESS.lower()
            if is_usdce_in:
                # USDC.e → token: input is $-pegged, output uses pre_price
                amount_in_usd  = amount_in_raw / (10 ** config.USDCE_DECIMALS)
                amount_out_usd = (received / (10 ** config.TOKEN_DECIMALS)) * pre_price
                direction = "usdce_to_token"
            else:
                amount_in_usd  = (amount_in_raw / (10 ** config.TOKEN_DECIMALS)) * pre_price
                amount_out_usd = received / (10 ** config.USDCE_DECIMALS)
                direction = "token_to_usdce"

            record_swap_cost(
                db_con, reason, direction,
                pool_address=str(pool_state.get("pool").address) if hasattr(pool_state.get("pool"), "address") else "",
                amount_in_raw=amount_in_raw, amount_out_raw=received,
                amount_in_usd=amount_in_usd, amount_out_usd=amount_out_usd,
            )
            info(f"  swap cost recorded: ${(amount_in_usd - amount_out_usd):.6f} ({reason})")
        except Exception as e:
            warn(f"  swap cost recording failed: {e}")

    return True, received
