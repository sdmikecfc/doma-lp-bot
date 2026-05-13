"""
config.py — loads LP bot settings from .env
"""
import os
from dotenv import load_dotenv
from web3 import Web3

load_dotenv()


def _addr(val: str) -> str:
    return Web3.to_checksum_address(val.strip())


def _float(key: str, default: float) -> float:
    return float(os.getenv(key, str(default)))


def _int(key: str, default: int) -> int:
    return int(os.getenv(key, str(default)))


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes")


# ── Wallet ────────────────────────────────────────────────────────────────
MNEMONIC               = os.getenv("MNEMONIC", "").strip()
PRIVATE_KEY            = os.getenv("PRIVATE_KEY", "").strip()
MNEMONIC_ACCOUNT_INDEX = _int("MNEMONIC_ACCOUNT_INDEX", 0)

if not MNEMONIC and not PRIVATE_KEY:
    raise ValueError("Set either MNEMONIC or PRIVATE_KEY in your .env file")

if PRIVATE_KEY and not PRIVATE_KEY.startswith("0x"):
    PRIVATE_KEY = "0x" + PRIVATE_KEY


# ── Network ──────────────────────────────────────────────────────────────
RPC_URL    = os.getenv("RPC_URL",    "https://doma.drpc.org")
RPC_BACKUP = os.getenv("RPC_BACKUP", "https://rpc.doma.xyz")
CHAIN_ID   = _int("CHAIN_ID", 97477)


# ── Doma V3 contracts ────────────────────────────────────────────────────
USDCE_ADDRESS    = _addr(os.getenv("USDCE_ADDRESS",    "0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477"))
UNIVERSAL_ROUTER = _addr(os.getenv("UNIVERSAL_ROUTER", "0x5089863E97196773038f98459262D866f2281f58"))
NPM_ADDRESS      = _addr(os.getenv("NPM_ADDRESS",      "0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0"))
V3_FACTORY       = _addr(os.getenv("V3_FACTORY",       "0x2e50b586d5bcD04cb6125E028A6a669f7f3cF1C2"))


# ── LP target (REQUIRED — set in .env) ───────────────────────────────────
LP_POOL_ADDRESS  = _addr(os.getenv("LP_POOL_ADDRESS",  "0x0000000000000000000000000000000000000000"))
LP_TOKEN_ADDRESS = _addr(os.getenv("LP_TOKEN_ADDRESS", "0x0000000000000000000000000000000000000000"))
LP_TOKEN_SYMBOL  = os.getenv("LP_TOKEN_SYMBOL", "YOURTOKEN")
LP_FEE_TIER      = _int("LP_FEE_TIER", 500)

if LP_POOL_ADDRESS == "0x0000000000000000000000000000000000000000":
    raise ValueError(
        "LP_POOL_ADDRESS not set in .env. Find your pool address at "
        "https://doma.xyz/explore and set it in your .env file before running."
    )
if LP_TOKEN_ADDRESS == "0x0000000000000000000000000000000000000000":
    raise ValueError(
        "LP_TOKEN_ADDRESS not set in .env. This should be the non-USDC.e "
        "token address from your pool."
    )


# ── Strategy ─────────────────────────────────────────────────────────────
LP_TOTAL_USD               = _float("LP_TOTAL_USD",               150.0)
LP_RANGE_PCT               = _float("LP_RANGE_PCT",               0.005)
LP_REBALANCE_TRIGGER_PCT   = _float("LP_REBALANCE_TRIGGER_PCT",   0.25)
LP_MIN_FEES_TO_COLLECT_USD = _float("LP_MIN_FEES_TO_COLLECT_USD", 0.50)
LP_SWAP_MAX_SLIPPAGE_PCT   = _float("LP_SWAP_MAX_SLIPPAGE_PCT",   0.005)
LP_EMERGENCY_SLIPPAGE_PCT  = _float("LP_EMERGENCY_SLIPPAGE_PCT",  0.02)
LP_MIN_24H_VOLUME_USD      = _float("LP_MIN_24H_VOLUME_USD",      500.0)
LP_LOOP_INTERVAL_SEC       = _int("LP_LOOP_INTERVAL_SEC", 2)

# Periodic compound: collect fees + swap to balance + increaseLiquidity
# Re-deploys earned fees back into the active position rather than letting
# them sit idle in the wallet. Runs every LP_COMPOUND_INTERVAL_SEC.
LP_COMPOUND_INTERVAL_SEC = _int("LP_COMPOUND_INTERVAL_SEC", 21600)  # default 6h
LP_COMPOUND_MIN_USD      = _float("LP_COMPOUND_MIN_USD", 0.50)      # skip if total idle < this


# ── Token constants ──────────────────────────────────────────────────────
USDCE_DECIMALS = _int("USDCE_DECIMALS", 6)
TOKEN_DECIMALS = _int("TOKEN_DECIMALS", 6)   # 6 for SOFTWARE.ai, 18 for WETH, etc.


# ── Tick math constants for fee tier 500 (0.05%) ─────────────────────────
TICK_SPACING = 10
MIN_TICK = -887272
MAX_TICK =  887272


# ── Mode ─────────────────────────────────────────────────────────────────
DRY_RUN = _bool("DRY_RUN", True)
