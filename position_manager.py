"""
position_manager.py — V3 LP NFT lifecycle via NonfungiblePositionManager.

mint, decreaseLiquidity, collect, burn.
"""
import time

from web3 import Web3

import config
from shared import info, warn, err, ERC20_ABI, NPM_ABI, MAX_UINT128, MAX_UINT256
from swap_executor import send_tx


def ensure_approval(w3, wallet, private_key, token_addr: str, spender: str,
                    label: str, gas_price: int) -> bool:
    """Ensure spender has max approval for token. No-op if already approved."""
    token = w3.eth.contract(
        address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
    )
    current = token.functions.allowance(wallet, Web3.to_checksum_address(spender)).call()
    if current >= MAX_UINT256 // 2:
        return True
    info(f"Approving {label} for {spender[:10]}...")
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = token.functions.approve(
            Web3.to_checksum_address(spender), MAX_UINT256
        ).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 80_000
    tx = token.functions.approve(
        Web3.to_checksum_address(spender), MAX_UINT256
    ).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID,
    })
    return send_tx(w3, tx, private_key, f"approve {label}") is not None


def mint_position(w3, wallet, private_key, pool_state: dict,
                  tick_lower: int, tick_upper: int,
                  amount0_desired: int, amount1_desired: int,
                  amount0_min: int, amount1_min: int,
                  gas_price: int) -> tuple[bool, int, int, int, int]:
    """
    Mint a new V3 LP NFT position.
    Returns (success, tokenId, liquidity, amount0_used, amount1_used).
    """
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )

    # Approvals: NPM must be allowed to pull both tokens
    if not ensure_approval(w3, wallet, private_key, pool_state["token0"],
                           config.NPM_ADDRESS, "token0→NPM", gas_price):
        return False, 0, 0, 0, 0
    if not ensure_approval(w3, wallet, private_key, pool_state["token1"],
                           config.NPM_ADDRESS, "token1→NPM", gas_price):
        return False, 0, 0, 0, 0

    deadline = int(time.time()) + 600
    params = (
        Web3.to_checksum_address(pool_state["token0"]),
        Web3.to_checksum_address(pool_state["token1"]),
        pool_state["fee"],
        tick_lower,
        tick_upper,
        amount0_desired,
        amount1_desired,
        amount0_min,
        amount1_min,
        wallet,
        deadline,
    )

    info(f"mint params: ticks=[{tick_lower}, {tick_upper}]  amount0={amount0_desired}  amount1={amount1_desired}")

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = npm.functions.mint(params).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception as e:
        warn(f"mint gas estimate failed: {e}, using fallback")
        gas_limit = 600_000

    tx = npm.functions.mint(params).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })

    receipt = send_tx(w3, tx, private_key, "mint")
    if receipt is None:
        return False, 0, 0, 0, 0

    # Parse the IncreaseLiquidity event from logs to extract tokenId etc.
    # IncreaseLiquidity(tokenId, liquidity, amount0, amount1)
    # topic0 = keccak256("IncreaseLiquidity(uint256,uint128,uint256,uint256)")
    INCREASE_LIQ_SIG = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
    token_id = liquidity = amt0_used = amt1_used = 0
    for log in receipt.logs:
        if log.address.lower() == config.NPM_ADDRESS.lower() and len(log.topics) > 0:
            topic0 = log.topics[0].hex() if hasattr(log.topics[0], 'hex') else log.topics[0]
            if not topic0.startswith("0x"):
                topic0 = "0x" + topic0
            if topic0.lower() == INCREASE_LIQ_SIG:
                # tokenId is indexed (topic[1])
                token_id = int(log.topics[1].hex(), 16) if hasattr(log.topics[1], 'hex') else int(log.topics[1], 16)
                # liquidity, amount0, amount1 are in data
                data = log.data
                if isinstance(data, (bytes, bytearray)):
                    data = data.hex()
                if data.startswith("0x"):
                    data = data[2:]
                liquidity = int(data[0:64], 16)
                amt0_used = int(data[64:128], 16)
                amt1_used = int(data[128:192], 16)
                break

    info(f"mint success: tokenId={token_id}  liquidity={liquidity}  amount0_used={amt0_used}  amount1_used={amt1_used}")
    return True, token_id, liquidity, amt0_used, amt1_used


def get_position(w3, token_id: int) -> dict:
    """Read on-chain state of a position."""
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )
    p = npm.functions.positions(token_id).call()
    return {
        "nonce": p[0], "operator": p[1], "token0": p[2], "token1": p[3],
        "fee": p[4], "tick_lower": p[5], "tick_upper": p[6],
        "liquidity": p[7],
        "fee_growth_inside_0": p[8], "fee_growth_inside_1": p[9],
        "tokens_owed_0": p[10], "tokens_owed_1": p[11],
    }


def increase_liquidity(w3, wallet, private_key, token_id: int,
                       amount0_desired: int, amount1_desired: int,
                       gas_price: int) -> tuple[bool, int, int, int]:
    """
    Add more liquidity to an existing position. Same tick range, more capital.
    Returns (success, liquidity_added, amount0_used, amount1_used).
    Uses amount_min=0; NPM uses whatever ratio fits the existing range.
    """
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )
    # NPM must have approval for both tokens (already set during initial mint)
    deadline = int(time.time()) + 600
    params = (
        token_id,
        amount0_desired,
        amount1_desired,
        0,                   # amount0Min
        0,                   # amount1Min
        deadline,
    )

    info(f"increaseLiquidity params: amount0={amount0_desired}  amount1={amount1_desired}")

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = npm.functions.increaseLiquidity(params).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception as e:
        warn(f"increaseLiquidity gas estimate failed: {e}, using fallback")
        gas_limit = 400_000

    tx = npm.functions.increaseLiquidity(params).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })

    receipt = send_tx(w3, tx, private_key, "increaseLiquidity")
    if receipt is None:
        return False, 0, 0, 0

    # Parse the IncreaseLiquidity event (same signature as mint)
    INCREASE_LIQ_SIG = "0x3067048beee31b25b2f1681f88dac838c8bba36af25bfb2b7cf7473a5847e35f"
    liq_added = amt0_used = amt1_used = 0
    for log in receipt.logs:
        if log.address.lower() == config.NPM_ADDRESS.lower() and len(log.topics) > 0:
            topic0 = log.topics[0].hex() if hasattr(log.topics[0], 'hex') else log.topics[0]
            if not topic0.startswith("0x"):
                topic0 = "0x" + topic0
            if topic0.lower() == INCREASE_LIQ_SIG:
                data = log.data
                if isinstance(data, (bytes, bytearray)):
                    data = data.hex()
                if data.startswith("0x"):
                    data = data[2:]
                liq_added = int(data[0:64], 16)
                amt0_used = int(data[64:128], 16)
                amt1_used = int(data[128:192], 16)
                break

    info(f"increaseLiquidity ✓: liquidity_added={liq_added}  amount0={amt0_used}  amount1={amt1_used}")
    return True, liq_added, amt0_used, amt1_used


def decrease_liquidity(w3, wallet, private_key, token_id: int,
                       liquidity: int, gas_price: int) -> tuple[bool, int, int]:
    """Withdraw liquidity from a position. Returns (success, amount0_received, amount1_received)."""
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )
    deadline = int(time.time()) + 600
    params = (token_id, liquidity, 0, 0, deadline)  # min amounts = 0 (we'll collect right after)

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = npm.functions.decreaseLiquidity(params).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 300_000

    tx = npm.functions.decreaseLiquidity(params).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })
    receipt = send_tx(w3, tx, private_key, "decreaseLiquidity")
    if receipt is None:
        return False, 0, 0

    # decreaseLiquidity returns amounts but we'd need to call statically to see them.
    # Easier: read tokens_owed before and after collect.
    return True, 0, 0


def collect_fees(w3, wallet, private_key, token_id: int,
                 gas_price: int) -> tuple[bool, int, int]:
    """Collect all owed tokens (fees + decreased liquidity proceeds)."""
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )
    params = (token_id, wallet, MAX_UINT128, MAX_UINT128)

    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = npm.functions.collect(params).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 200_000

    tx = npm.functions.collect(params).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })
    receipt = send_tx(w3, tx, private_key, "collect")
    if receipt is None:
        return False, 0, 0

    # Parse Collect event: Collect(tokenId, recipient, amount0, amount1)
    COLLECT_SIG = "0x40d0efd1a53d60ecbf40971b9daf7dc90178c3aadc7aab1765632738fa8b8f01"
    amt0 = amt1 = 0
    for log in receipt.logs:
        if log.address.lower() == config.NPM_ADDRESS.lower() and len(log.topics) > 0:
            topic0 = log.topics[0].hex() if hasattr(log.topics[0], 'hex') else log.topics[0]
            if not topic0.startswith("0x"):
                topic0 = "0x" + topic0
            if topic0.lower() == COLLECT_SIG:
                data = log.data
                if isinstance(data, (bytes, bytearray)):
                    data = data.hex()
                if data.startswith("0x"):
                    data = data[2:]
                # data: recipient(32), amount0(32), amount1(32)
                amt0 = int(data[64:128], 16)
                amt1 = int(data[128:192], 16)
                break

    info(f"collect: amount0={amt0}  amount1={amt1}")
    return True, amt0, amt1


def burn_position(w3, wallet, private_key, token_id: int,
                  gas_price: int) -> bool:
    """Burn an empty NFT position (must have liquidity=0 and tokens_owed=0)."""
    npm = w3.eth.contract(
        address=Web3.to_checksum_address(config.NPM_ADDRESS), abi=NPM_ABI
    )
    nonce = w3.eth.get_transaction_count(wallet, "pending")
    try:
        gas_est = npm.functions.burn(token_id).estimate_gas({"from": wallet})
        gas_limit = int(gas_est * 1.5)
    except Exception:
        gas_limit = 100_000
    tx = npm.functions.burn(token_id).build_transaction({
        "from": wallet, "nonce": nonce, "gas": gas_limit,
        "gasPrice": gas_price, "chainId": config.CHAIN_ID, "value": 0,
    })
    return send_tx(w3, tx, private_key, "burn") is not None
