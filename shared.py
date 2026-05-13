"""
shared.py — common helpers: web3 connection, wallet, logging, ABIs.
"""
import sys
from datetime import datetime

from eth_account import Account
from web3 import Web3

import config


# ── Logging ──────────────────────────────────────────────────────────────
def log(level: str, msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{ts}  {level:<8} {msg}", flush=True)

def info(msg):  log("INFO",  msg)
def warn(msg):  log("WARN",  msg)
def err(msg):   log("ERROR", msg)
def debug(msg): log("DEBUG", msg)


# ── Connection ───────────────────────────────────────────────────────────
def connect() -> Web3:
    w3 = Web3(Web3.HTTPProvider(config.RPC_URL))
    if not w3.is_connected():
        warn(f"Primary RPC failed, trying backup: {config.RPC_BACKUP}")
        w3 = Web3(Web3.HTTPProvider(config.RPC_BACKUP))
    if not w3.is_connected():
        err("Both RPC endpoints unreachable")
        sys.exit(1)
    info(f"Connected to RPC: chain_id={w3.eth.chain_id}")
    return w3


def load_wallet():
    if config.MNEMONIC:
        Account.enable_unaudited_hdwallet_features()
        acct = Account.from_mnemonic(
            config.MNEMONIC,
            account_path=f"m/44'/60'/0'/0/{config.MNEMONIC_ACCOUNT_INDEX}",
        )
    else:
        acct = Account.from_key(config.PRIVATE_KEY)
    return acct.address, acct.key.hex()


# ── ABIs ─────────────────────────────────────────────────────────────────

ERC20_ABI = [
    {"name": "balanceOf",  "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "decimals",   "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint8"}]},
    {"name": "symbol",     "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "string"}]},
    {"name": "allowance",  "type": "function", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "approve",    "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"name": "transfer",   "type": "function", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96",   "type": "uint160"},
         {"name": "tick",           "type": "int24"},
         {"name": "observationIndex",           "type": "uint16"},
         {"name": "observationCardinality",     "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol",    "type": "uint8"},
         {"name": "unlocked",       "type": "bool"},
     ]},
    {"name": "token0", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "token1", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "fee",    "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint24"}]},
    {"name": "liquidity", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint128"}]},
    {"name": "tickSpacing", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "int24"}]},

    # Used by compute_uncollected_fees() in pool_state.py — V3 fee accounting
    {"name": "feeGrowthGlobal0X128", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "feeGrowthGlobal1X128", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "ticks", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tick", "type": "int24"}],
     "outputs": [
         {"name": "liquidityGross",                  "type": "uint128"},
         {"name": "liquidityNet",                    "type": "int128"},
         {"name": "feeGrowthOutside0X128",           "type": "uint256"},
         {"name": "feeGrowthOutside1X128",           "type": "uint256"},
         {"name": "tickCumulativeOutside",           "type": "int56"},
         {"name": "secondsPerLiquidityOutsideX128",  "type": "uint160"},
         {"name": "secondsOutside",                  "type": "uint32"},
         {"name": "initialized",                     "type": "bool"},
     ]},
]

ROUTER_ABI = [
    {"name": "execute", "type": "function", "stateMutability": "payable",
     "inputs": [
         {"name": "commands", "type": "bytes"},
         {"name": "inputs",   "type": "bytes[]"},
         {"name": "deadline", "type": "uint256"},
     ],
     "outputs": []},
]

NPM_ABI = [
    {"name": "mint", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "token0",         "type": "address"},
         {"name": "token1",         "type": "address"},
         {"name": "fee",            "type": "uint24"},
         {"name": "tickLower",      "type": "int24"},
         {"name": "tickUpper",      "type": "int24"},
         {"name": "amount0Desired", "type": "uint256"},
         {"name": "amount1Desired", "type": "uint256"},
         {"name": "amount0Min",     "type": "uint256"},
         {"name": "amount1Min",     "type": "uint256"},
         {"name": "recipient",      "type": "address"},
         {"name": "deadline",       "type": "uint256"},
     ]}],
     "outputs": [
         {"name": "tokenId",   "type": "uint256"},
         {"name": "liquidity", "type": "uint128"},
         {"name": "amount0",   "type": "uint256"},
         {"name": "amount1",   "type": "uint256"},
     ]},

    {"name": "decreaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId",    "type": "uint256"},
         {"name": "liquidity",  "type": "uint128"},
         {"name": "amount0Min", "type": "uint256"},
         {"name": "amount1Min", "type": "uint256"},
         {"name": "deadline",   "type": "uint256"},
     ]}],
     "outputs": [
         {"name": "amount0", "type": "uint256"},
         {"name": "amount1", "type": "uint256"},
     ]},

    {"name": "increaseLiquidity", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId",        "type": "uint256"},
         {"name": "amount0Desired", "type": "uint256"},
         {"name": "amount1Desired", "type": "uint256"},
         {"name": "amount0Min",     "type": "uint256"},
         {"name": "amount1Min",     "type": "uint256"},
         {"name": "deadline",       "type": "uint256"},
     ]}],
     "outputs": [
         {"name": "liquidity", "type": "uint128"},
         {"name": "amount0",   "type": "uint256"},
         {"name": "amount1",   "type": "uint256"},
     ]},

    {"name": "collect", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "params", "type": "tuple", "components": [
         {"name": "tokenId",    "type": "uint256"},
         {"name": "recipient",  "type": "address"},
         {"name": "amount0Max", "type": "uint128"},
         {"name": "amount1Max", "type": "uint128"},
     ]}],
     "outputs": [
         {"name": "amount0", "type": "uint256"},
         {"name": "amount1", "type": "uint256"},
     ]},

    {"name": "burn", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": []},

    {"name": "positions", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "tokenId", "type": "uint256"}],
     "outputs": [
         {"name": "nonce",                     "type": "uint96"},
         {"name": "operator",                  "type": "address"},
         {"name": "token0",                    "type": "address"},
         {"name": "token1",                    "type": "address"},
         {"name": "fee",                       "type": "uint24"},
         {"name": "tickLower",                 "type": "int24"},
         {"name": "tickUpper",                 "type": "int24"},
         {"name": "liquidity",                 "type": "uint128"},
         {"name": "feeGrowthInside0LastX128",  "type": "uint256"},
         {"name": "feeGrowthInside1LastX128",  "type": "uint256"},
         {"name": "tokensOwed0",               "type": "uint128"},
         {"name": "tokensOwed1",               "type": "uint128"},
     ]},
]


MAX_UINT128 = 2 ** 128 - 1
MAX_UINT256 = 2 ** 256 - 1
