# Doma LP Bot — Complete Setup Guide

A step-by-step operations manual for deploying the Doma LP Bot: an automated concentrated-liquidity provider for Uniswap V3-style pools on the Doma chain. The bot mints positions, monitors price, rebalances when price drifts, and compounds earned fees back into your position 24/7.

This guide assumes basic command-line familiarity (you can `ssh`, run `cd`, edit files in `nano`) and that you understand you are entrusting **real on-chain capital** to a piece of software. Every step in this guide matters. Skipping one can cost real money.

> 🔐 **Read this first.** The bot signs transactions from a wallet whose seed phrase lives on your server in plaintext. Treat the server like a hot wallet. Use a fresh, dedicated mnemonic — **never** your main wallet. If your server is compromised, anyone with the seed phrase can move every token in that wallet.

---

## Table of contents

1. [What the bot does](#1-what-the-bot-does)
2. [How LP fees actually work (so the rest of the guide makes sense)](#2-how-lp-fees-actually-work-so-the-rest-of-the-guide-makes-sense)
3. [Risks — read these before you proceed](#3-risks--read-these-before-you-proceed)
4. [Prerequisites](#4-prerequisites)
5. [Pick a pool worth LPing in](#5-pick-a-pool-worth-lping-in)
6. [Generate a dedicated wallet](#6-generate-a-dedicated-wallet)
7. [Find your pool info on doma.xyz](#7-find-your-pool-info-on-domaxyz)
8. [Clone the repo and install dependencies](#8-clone-the-repo-and-install-dependencies)
9. [Configure `.env`](#9-configure-env)
10. [One-time Permit2 setup](#10-one-time-permit2-setup)
11. [Fund the wallet](#11-fund-the-wallet)
12. [Dry run](#12-dry-run)
13. [First live mint](#13-first-live-mint)
14. [Verify the position on-chain](#14-verify-the-position-on-chain)
15. [Production setup with supervisor](#15-production-setup-with-supervisor)
16. [Monitoring and tuning](#16-monitoring-and-tuning)
17. [Compound mechanics](#17-compound-mechanics)
18. [Rebalance mechanics](#18-rebalance-mechanics)
19. [Adaptive range, in plain language](#19-adaptive-range-in-plain-language)
20. [Adding capital to an existing position](#20-adding-capital-to-an-existing-position)
21. [Exiting cleanly](#21-exiting-cleanly)
22. [Emergency recovery](#22-emergency-recovery)
23. [Stopping the bot](#23-stopping-the-bot)
24. [Optional: dashboard API](#24-optional-dashboard-api)
25. [Troubleshooting](#25-troubleshooting)
26. [Appendix A: math walkthroughs](#appendix-a-math-walkthroughs)
27. [Appendix B: full `.env` reference](#appendix-b-full-env-reference)
28. [Appendix C: capital-scaling cheat sheet](#appendix-c-capital-scaling-cheat-sheet)

---

## 1. What the bot does

In one paragraph: the bot mints a Uniswap V3-style concentrated liquidity position around the current price of a Doma pool, then watches that pool every couple of seconds. When price drifts close to the edge of the range, the bot burns the position, rebalances both sides via a swap, and re-mints around the new price. Every six hours it harvests trading fees the position has earned, swaps them into the right token ratio, and adds them back as more liquidity ("compounding"). The result is a position that stays roughly centered on the live market and grows itself from its own yield.

In more detail:

1. **Mints a concentrated V3 LP position** around current price in a USDC.e-paired pool on Doma.
2. **Monitors price every 2 seconds** via `slot0` reads on the pool contract.
3. **Rebalances when price drifts** to a configurable proximity-to-edge threshold (default: 50% of the way to the edge).
4. **Compounds earned fees every 6 hours** back into the position (`collect` → swap to balance → `increaseLiquidity`).
5. **Adapts range tightness** based on the pool's realized 24-hour volatility (Phase 1 adaptive ranging — more on this later).
6. **Exposes an HTTP API** for dashboard integration (optional, off by default).

The bot does not chase narratives, doesn't try to time the market, doesn't predict price direction. It earns by being present in the right tick range when swaps happen. The only "smart" thing it does is keep the position centered and let fees compound.

### What it does **not** do

- It does not provide liquidity to V2/constant-product pools.
- It does not buy or sell speculatively. The only swaps it performs are the minimum needed to keep your position balanced.
- It does not bridge funds for you. USDC.e and ETH must already be on Doma chain in the bot's wallet.
- It does not try to outperform IL with hedges, options, or external strategy. If the token tanks, your position holds more of the tanked token. That is how V3 LP works.

---

## 2. How LP fees actually work (so the rest of the guide makes sense)

If you've LPed before on Uniswap V3, skip this section. If this is your first concentrated-LP rodeo, read it carefully — the configuration choices later will only make sense once this clicks.

A Uniswap V3 pool is split into discrete **ticks**. Each tick represents a specific price. When you LP in V3, you don't deploy liquidity across the entire price curve (that's V2). You deploy it across a **range** of ticks. The narrower the range, the higher your **concentration multiplier** — the more fees you earn *per dollar of capital deployed, per swap*, **as long as price stays inside your range**.

The catch:

- If price moves outside your range, your liquidity is **inactive** — you earn zero fees until price comes back, or until you rebalance.
- Inside the range, your position is automatically being "swept" between the two tokens as swaps happen. When price rises, your position holds more of the cheaper token. When price falls, the opposite. This is how impermanent loss happens.
- Every time you rebalance, you pay two costs: (1) the swap fee to rebalance the two sides into the right ratio for the new tick, and (2) the network gas. On Doma both are tiny but non-zero.

The economic question is simple: **do the fees earned exceed the rebalance costs + impermanent loss?**

In a high-volume, low-volatility pool: yes, easily.
In a low-volume, high-volatility pool: usually no — you churn yourself to death.
In a high-volume, high-volatility pool: depends on how tight your range is. Too tight = constant rebalance. Too wide = low concentration multiplier = low fees.

The bot's job is to keep you in a range that's tight enough to earn well and wide enough not to thrash. The **adaptive range** feature (Section 19) tunes this automatically based on what the pool has actually done in the last 24 hours.

---

## 3. Risks — read these before you proceed

⚠️ **Impermanent loss (IL).** When the price of the non-USDC.e token moves significantly, your LP position ends up holding more of the worse-performing side. If the token doubles, you'll have less of it than if you'd just held both tokens in a wallet. If the token gets cut in half, your position holds more of it (now-cheaper) than it started with. IL is real, predictable, and gets worse with bigger price moves. The bot's compounded fee income offsets IL — sometimes fully, sometimes not.

⚠️ **Rebalance churn in volatile pools.** A tight range in a violently moving pool can rebalance every few minutes. Every rebalance costs swap fees and gas. In an extreme case, your rebalance costs can exceed your earned fees and you'll bleed equity. The adaptive range feature mitigates this but cannot eliminate it. If a pool is going up and down 5% per hour and the LP fee is 0.05%, no concentrated range will beat the volatility — you've picked a bad pool.

⚠️ **Pool dies / volume drops.** If your pool stops trading (token rugs, attention moves elsewhere, narrative dies), your bot keeps holding the position earning $0 while ETH gas slowly drains from poll loop overhead (very small, but non-zero). The bot will not auto-exit a dead pool — you have to make that call and run `exit_position.py`.

⚠️ **Slippage on big positions.** When your position becomes a meaningful fraction of total pool liquidity (above ~10% of pool TVL), your own rebalance swaps move the pool noticeably, eating into profit. For most users this never happens. For users LPing in small pools with $2,000+, it can.

⚠️ **Smart contract risk.** Uniswap V3 contracts are battle-tested over many years and many billions of dollars. The Doma deployment of the Universal Router and NonfungiblePositionManager (NPM) is a fork of the same code. The risk is small but not zero. Don't deploy life-changing money.

⚠️ **Operational risk.** A bug in the bot, an RPC outage, a server crash at the wrong moment, or a misconfigured `.env` can cause partial state (e.g. tokens stuck on the router, position with `liquidity=0` but `tokensOwed>0`). The bot includes a `recover_position.py` for this, but you need to notice the problem first.

⚠️ **Custodial risk.** Your mnemonic sits in a `.env` file on your server in plaintext. If anyone gains shell access to your server, they get your wallet. Use a fresh mnemonic. Use SSH keys (no password auth). Use a firewall. Use `fail2ban`. Don't store your main wallet seed anywhere near this machine.

If any of those make you uncomfortable, **stop now**. This is not a casino-bot — it's a piece of automation that handles capital. The downside is real even if it's bounded.

---

## 4. Prerequisites

### Hardware / environment

You need a machine that's online 24/7. The bot polls the pool every 2 seconds and rebalances on demand — a laptop that goes to sleep won't work.

The standard setup:

- **A Linux VPS.** A $6/month DigitalOcean droplet (1 vCPU, 1 GB RAM, Ubuntu 22.04 or 24.04) is plenty. Hetzner, Linode, Vultr, AWS Lightsail also work. Choose a region close to wherever the Doma RPC is hosted to shave a few ms off RPC latency, but it barely matters in practice.
- **A static IPv4 address** on that VPS. Most providers give you one by default.
- **SSH access.** Ideally with an SSH key — disable password auth after setup.

If you absolutely must run this on a personal machine: a Mac mini or always-on Linux desktop is fine. Don't run it on a laptop you close at night.

### Software on the server

- **Ubuntu 22.04 / 24.04** (or any modern Debian-family Linux). The supervisor configs assume Linux. macOS works for development but switching the supervisor configs to launchd is left as an exercise.
- **Python 3.10 or newer**, with `pip` and `venv`.
- **git** for cloning the repo.
- **supervisor** (`apt install supervisor`) for keeping the bot running across reboots and crashes.
- **A text editor** — `nano` works fine if you don't have a preference.

Quick install on a fresh Ubuntu droplet:

```bash
apt update
apt install -y python3 python3-pip python3-venv git supervisor curl ufw
```

### Wallet prerequisites

- A **brand new EVM wallet** generated specifically for this bot. Never reuse a wallet that holds other assets.
- USDC.e and a small amount of ETH on **Doma chain** in that wallet. (More on funding in Section 11.)

### Knowledge prerequisites

You need to be comfortable enough with the command line to:

- SSH into a server.
- Edit a file with `nano` or `vim`.
- Read log output and notice when something looks wrong.
- Run `ls`, `cd`, `cat`, `tail`, `grep`.

If any of that is unfamiliar, spend 30 minutes with a basic Linux tutorial first. Trying to learn the shell while a bot is moving money for you is a bad combination.

---

## 5. Pick a pool worth LPing in

This is the single most important decision in the whole exercise. Pool choice matters more than range tightness, more than capital size, more than gas price. A great strategy on a dead pool earns nothing. A mediocre strategy on a healthy pool earns plenty.

### Good signs in a pool

- **Pool TVL > $20K.** Below this, your own position dominates liquidity and you eat your own slippage on rebalances.
- **24h volume > $10K.** Volume is the fee generator. No swaps, no fees.
- **Volume/TVL ratio > 0.5.** This means the pool's capital is being turned over actively. A pool with $1M TVL but $5K daily volume is dormant capital — your share of the fees is tiny.
- **Fee tier 0.05% (500).** The sweet spot for most volatile-but-active tokens on Doma. High enough to generate meaningful fee income, low enough to attract traders away from competing routes.
- **Token has sustained activity over 7+ days.** Look at the 7-day price/volume chart. A pool that did $200K volume yesterday but $0 the day before is probably not what you think it is.

### Red flags — avoid these pools

- **Pool TVL < $5K.** Effectively a dead pool. Even if a few swaps happen, the absolute fee amount per swap is small and the position math gets weird at this scale.
- **24h volume < $1K.** Not enough flow to cover rebalance costs over time.
- **Token launched < 24 hours ago.** Volatility is too high. New listings often move 50% in a day. Any tight range gets blown out within hours, every rebalance bleeds.
- **0.30% (3000) or 1% (10000) pool with low volume.** The higher the fee tier, the more it relies on a steady flow of high-priority traders to make sense. A 0.30% pool doing $2K/day will rebalance you to death.
- **One whale LP at the active tick.** If you see a single concentrated position with 80%+ of the active liquidity, you will be massively diluted as soon as you mint. Look for fragmented liquidity.

### Worked example: evaluating a pool

Let's walk through an example using the kind of pool you'll encounter on Doma. Open https://doma.xyz/explore, then click into a pool that looks promising.

Suppose the pool reads as follows:

| Metric | Value |
|---|---|
| Pair | USDC.e / SOFTWARE.ai |
| Fee tier | 0.05% (500) |
| Pool TVL | $52,000 |
| 24h Volume | $204,000 |
| 7-day average volume | $185,000 |
| Number of concentrated LPs | 12 |

Quick read:

- TVL > $20K ✅
- Volume > $10K ✅
- Vol/TVL = 204k / 52k = 3.9 — *very* healthy flow ✅
- 0.05% fee tier ✅
- 7-day volume confirms it's not a one-day spike ✅
- 12 LPs means fragmented liquidity, you won't dominate ✅

If you LP $500 at a ±0.5% range, your position is approximately 1% of pool TVL. That's safely below the slippage-on-rebalance threshold. Daily fee revenue back-of-envelope: pool earns $204K × 0.05% = $102/day in fees. Your share at 1% of TVL (heavily concentrated, so a multiplier kicks in — call it 3× effective) ≈ $3/day. Annualized: ~$1,100 on $500 = ~220% APR. **Before** IL.

That's roughly the realistic upper end for a $500 position in an active Doma pool. Most days will be less. Some days will be more.

### Where to find pool data on doma.xyz

1. Go to **https://doma.xyz/explore**.
2. Sort by 24h volume descending.
3. For each pool that looks interesting, click in.
4. Note: pool address, token address (the non-USDC.e side), fee tier, current price, TVL, 24h volume.
5. Scroll down to the "Top LPs" section to see whether liquidity is fragmented (good) or dominated by one address (bad).

Keep a short list of 2-3 candidate pools before you commit. You'll plug one into the `.env` later.

---

## 6. Generate a dedicated wallet

This wallet will:

- Hold the USDC.e and ETH the bot deploys.
- Own the LP position NFT.
- Sign every transaction the bot sends.

It will **not**:

- Be your main wallet.
- Be a wallet that holds anything else of value.

### Why a fresh wallet matters

The mnemonic sits in plaintext in `/root/doma-automation/lp-bot/.env` on your server. If your server is ever compromised (weak SSH password, an exposed service, a supply-chain attack on a package, anything), the attacker gets the mnemonic. With the mnemonic they get every token the wallet has ever touched on every chain. If that wallet has ever held a serious bag, those assets are now theirs.

Using a fresh, single-purpose wallet limits the blast radius to whatever's currently in the bot. If you keep the LP bot to $100-$500 of working capital, that's your maximum loss from a server compromise.

### How to generate one

The cleanest way: use MetaMask, Rabby, or any well-known wallet to generate a new wallet. Specifically:

1. Open MetaMask (or your wallet of choice).
2. Click your account avatar → **Add account or hardware wallet** → **Create new account**.
3. The wallet generates a new account inside your existing seed phrase by default — that's **not what we want**. We want a brand-new seed phrase.
4. In MetaMask, log out completely and choose **Create a new wallet**, then **Save** the new seed phrase. Or use a fresh browser profile.

Better option: use a tool that gives you the mnemonic in a clean way.

```bash
# Install eth-account if you don't have it
pip install eth-account

# Generate a fresh mnemonic
python3 -c "
from eth_account import Account
Account.enable_unaudited_hdwallet_features()
acct, mnemonic = Account.create_with_mnemonic()
print('Address:', acct.address)
print('Mnemonic:', mnemonic)
"
```

**Output looks like:**

```
Address: 0xAbCdEf...123
Mnemonic: brick fault arrow obey grit ... twelve words ...
```

Write the mnemonic down on paper. Put it in a sealed envelope. Do not photograph it, do not email it to yourself, do not save it in a password manager that syncs to the cloud — that defeats the point of a dedicated air-gapped backup.

Then save the address in a notes file — you'll need to send USDC.e and ETH to it shortly.

> 🔐 **Reminder:** if you accidentally generate this on your laptop and your laptop is online, someone *could* in principle exfiltrate the mnemonic. The safest version is to generate it on the server you'll run the bot on, write the mnemonic down, and never copy it back off the server again.

---

## 7. Find your pool info on doma.xyz

Once you've picked a pool, you need six pieces of information from https://doma.xyz/explore. These all go into your `.env`:

| Field | Where to find | Example |
|---|---|---|
| **Pool address** | Top of pool page on doma.xyz | `0xa1b2c3...` |
| **Token address** | The non-USDC.e token's page | `0x9e8d7c...` |
| **Token symbol** | Display only | `SOFTWARE.ai` |
| **Fee tier (raw)** | Pool page header | `0.05%` |
| **Fee tier (config value)** | Multiply by 10,000 | `500` |
| **Tick spacing** | Maps from fee tier (see table below) | `10` |
| **Token decimals** | Token's contract page | Usually `6` on Doma launchpad tokens; `18` for WETH-style tokens |

### Fee tier → tick spacing table

These are Uniswap V3 standard, applied verbatim by Doma:

| Fee tier (raw) | Config value | Tick spacing |
|---|---|---|
| 0.01% | `100` | `1` |
| 0.05% | `500` | `10` |
| 0.30% | `3000` | `60` |
| 1.00% | `10000` | `200` |

If you get the tick spacing wrong, **the mint will revert on-chain** because the ticks you propose won't align to the pool's grid. Don't guess — match the table above to the fee tier exactly.

### How to find the token address

On the pool page, you'll see two tokens listed (token0 and token1). One of them is USDC.e (the address `0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477` — that's hard-coded in `.env.example`). The **other** one is your `LP_TOKEN_ADDRESS`. Copy the full hex address with the `0x` prefix.

### How to find token decimals

Most Doma launchpad tokens use **6 decimals** (matching USDC). Some use **18** (matching ETH/WETH). To check definitively, the token's page on doma.xyz typically shows decimals near the contract metadata. If it doesn't, paste the token address into a block explorer and look at the `decimals()` read function on the contract.

If you get decimals wrong, the bot will mis-size your position by a factor of 10¹² in one direction or another. Get this right.

---

## 8. Clone the repo and install dependencies

SSH into your server. Choose a working directory. The supervisor configs assume `/root/doma-automation/`, so I'll use that — if you use something else, you'll need to edit the supervisor configs to match.

```bash
ssh root@your-server-ip
mkdir -p /root/doma-automation
cd /root/doma-automation
```

Clone the bot:

```bash
git clone <doma-lp-bot-repo-url> lp-bot
cd lp-bot
```

(The repo URL comes from wherever you got the bot — your Doma operator, the community Discord, or wherever this guide was distributed.)

Create a Python virtual environment so the bot's deps don't conflict with system Python:

```bash
cd /root/doma-automation
python3 -m venv venv
source venv/bin/activate
```

Install the bot's Python dependencies:

```bash
cd /root/doma-automation/lp-bot
pip install -r requirements.txt
```

You should see `web3`, `eth-account`, `eth-abi`, `python-dotenv`, `fastapi`, and `uvicorn` (and their transitive deps) install without errors.

Verify the install:

```bash
python3 -c "import web3, eth_account; print('OK')"
```

You should see `OK`. If you see an import error, re-check the venv is activated (`which python3` should print a path inside `/root/doma-automation/venv/`).

---

## 9. Configure `.env`

This is the heart of the setup. Take your time and double-check every field.

Copy the template:

```bash
cd /root/doma-automation/lp-bot
cp .env.example .env
nano .env
```

What you'll see is roughly the file below. Walk through each section.

### Wallet section

```ini
# Either MNEMONIC or PRIVATE_KEY. Prefer MNEMONIC.
MNEMONIC=brick fault arrow obey grit ... twelve words ...
MNEMONIC_ACCOUNT_INDEX=0
PRIVATE_KEY=
```

- Paste the mnemonic you generated in Section 6.
- Leave `MNEMONIC_ACCOUNT_INDEX=0` unless you specifically want a non-default derivation path.
- Leave `PRIVATE_KEY` blank if you're using a mnemonic.

### Network section

```ini
RPC_URL=https://doma.drpc.org
RPC_BACKUP=https://rpc.doma.xyz
CHAIN_ID=97477
```

Leave these alone. They're correct.

### Doma V3 contract addresses

```ini
USDCE_ADDRESS=0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477
UNIVERSAL_ROUTER=0x5089863E97196773038f98459262D866f2281f58
NPM_ADDRESS=0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0
V3_FACTORY=0x2e50b586d5bcD04cb6125E028A6a669f7f3cF1C2
```

Leave these alone. They are the canonical Doma deployments.

### Pool section — **fill in your values**

```ini
LP_POOL_ADDRESS=0x... your pool address from Section 7 ...
LP_TOKEN_ADDRESS=0x... the non-USDC.e token address ...
LP_TOKEN_SYMBOL=SOFTWARE.ai
LP_FEE_TIER=500
TICK_SPACING=10
USDCE_DECIMALS=6
TOKEN_DECIMALS=6
```

- `LP_POOL_ADDRESS`: from the pool page on doma.xyz.
- `LP_TOKEN_ADDRESS`: the non-USDC.e token in the pair.
- `LP_TOKEN_SYMBOL`: cosmetic. Used in log output. Pick whatever the token's display ticker is.
- `LP_FEE_TIER`: 500 for a 0.05% pool. Other tiers per the table in Section 7.
- `TICK_SPACING`: matches the fee tier per the same table.
- `TOKEN_DECIMALS`: 6 for most Doma launchpad tokens, 18 for WETH-style. **Verify.**

### Strategy section — the dials that matter

```ini
LP_TOTAL_USD=100

LP_RANGE_PCT=0.010
LP_REBALANCE_TRIGGER_PCT=0.50

LP_MIN_FEES_TO_COLLECT_USD=0.50

LP_SWAP_MAX_SLIPPAGE_PCT=0.005
LP_EMERGENCY_SLIPPAGE_PCT=0.02

LP_MIN_24H_VOLUME_USD=500

LP_LOOP_INTERVAL_SEC=2

LP_COMPOUND_INTERVAL_SEC=21600
LP_COMPOUND_MIN_USD=0.50
```

Walking through these:

- **`LP_TOTAL_USD`** — Total dollar value to deploy in the position. The bot splits this 50/50 between USDC.e and the token at mint, then re-balances to the optimal V3 ratio. Set this to roughly the dollar value of USDC.e you'll fund the wallet with. For your first run, **start at $50–$100**. Scale up *after* you've watched at least one rebalance and one compound complete successfully.

- **`LP_RANGE_PCT`** — Half-width of the price range, as a fraction. `0.010` means ±1% around the current price. `0.005` means ±0.5%. This value is used only when adaptive range is disabled, or as the starting range until enough data has been collected. Default of 0.010 is sensible.

- **`LP_REBALANCE_TRIGGER_PCT`** — When price approaches the edge of the range, what fraction of the way to the edge triggers a rebalance? `0.50` means rebalance when price has crossed 50% of the half-range. Lower numbers = more rebalancing = more fees paid in swap costs, less risk of falling out of range. `0.50` is a good default.

- **`LP_MIN_FEES_TO_COLLECT_USD`** — Don't bother harvesting fees if accrued value is below this. `0.50` is the default — keeps small dust from triggering a compound cycle.

- **`LP_SWAP_MAX_SLIPPAGE_PCT`** — Slippage tolerance on normal swaps (compounds + rebalance prep). `0.005` = 0.5%. Tighter than this and slippage-protection reverts become common; looser than this and you get sandwich-attacked.

- **`LP_EMERGENCY_SLIPPAGE_PCT`** — Looser slippage tolerance used only on exit operations where the priority is "get out at any reasonable price."

- **`LP_MIN_24H_VOLUME_USD`** — If the bot's volume estimate for the pool drops below this, it logs a warning. It does not auto-exit. Useful as a heads-up that the pool is going dormant.

- **`LP_LOOP_INTERVAL_SEC`** — How often to poll the pool. 2 seconds is the default. Lower = more responsive to price moves; higher = less RPC load. Don't go below 1.

- **`LP_COMPOUND_INTERVAL_SEC`** — Seconds between compound attempts. 21600 = 6 hours. Compounds are throttled both by this interval and by `LP_COMPOUND_MIN_USD`.

- **`LP_COMPOUND_MIN_USD`** — Minimum fee value to actually compound (vs. waiting another cycle).

### Adaptive range section

```ini
ADAPTIVE_RANGE_ENABLED=true
ADAPTIVE_RANGE_LOOKBACK_HRS=24
ADAPTIVE_RANGE_MULTIPLIER=1.5
LP_RANGE_MIN=0.005
LP_RANGE_MAX=0.05
LP_RANGE_DEFAULT=0.010
```

Recommend leaving on. The bot starts at `LP_RANGE_DEFAULT` (±1%) and after 10+ price snapshots, recomputes the range based on the realized 24-hour high-low spread. `LP_RANGE_MIN` and `LP_RANGE_MAX` are floor and ceiling. Section 19 walks through the math.

### API section

```ini
LP_API_KEY=
```

Leave blank for now. Set it later if you want to wire up a dashboard (Section 24).

### Mode

```ini
DRY_RUN=true
```

**Leave this `true` until the dry-run section confirms the config is sane**, then flip it to `false` for the first live run.

Save the file (`Ctrl+O`, `Enter`, `Ctrl+X` if you're in nano).

### Sanity check the file

```bash
cat .env | grep -E "^(MNEMONIC|LP_POOL|LP_TOKEN|LP_FEE|TICK|TOKEN_DEC|LP_TOTAL|DRY)"
```

You should see your values printed. If `LP_POOL_ADDRESS` looks wrong, fix it before going further.

Set restrictive permissions so other users on the box can't read your mnemonic:

```bash
chmod 600 .env
```

---

## 10. One-time Permit2 setup

V3 on Doma uses the **Universal Router** for swaps, which in turn uses **Permit2** for allowance management. Before the bot can swap on your behalf, you need to give two approvals — one for USDC.e, one for your LP token. These are one-time, on-chain transactions.

The script that does this:

```bash
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 verify_permit2.py
```

It will:

1. Connect to Doma RPC.
2. Check whether the wallet has already approved Permit2 for USDC.e and the LP token. If yes, exit cleanly.
3. If no, send two `approve(spender=Permit2, amount=MaxUint256)` transactions.
4. Then send two Permit2 `approve()` calls authorizing the Universal Router.

You should see output along the lines of:

```
[permit2] checking USDC.e ERC20 allowance to Permit2... need to approve
[permit2] approving USDC.e -> Permit2 (MaxUint256)
[permit2] tx 0xabc...123 submitted
[permit2] tx mined in block 1234567 (status=1)
[permit2] checking TOKEN ERC20 allowance to Permit2... need to approve
... (same for the token side)
[permit2] checking Permit2 allowance for USDC.e -> UniversalRouter... need to approve
[permit2] permit2.approve(...) submitted
... (same for the token side)
[permit2] all approvals complete
```

**What this costs:** four small transactions, each costing maybe 0.00001 ETH on Doma. You need at least ~0.0005 ETH in the wallet before running this script.

**Wait — do I need ETH before this step?** Yes. That's why funding (Section 11) splits across two parts: you should send enough ETH **first** to run Permit2 setup, then add USDC.e and more ETH after. Or send everything at once and run Permit2 immediately. Either way, the wallet needs ETH to gas these approvals.

If the script aborts saying it can't pay gas, top up ETH (Section 11) and re-run.

### Re-running is safe

`verify_permit2.py` checks allowance state before sending anything. If you run it twice, the second run says "all approvals complete" and exits without sending transactions. So if you're unsure, just re-run it.

### What if I want to LP in a different pool later?

You'll need to run `verify_permit2.py` again with the *new* `LP_TOKEN_ADDRESS` in `.env`, because the approvals are per-token. The USDC.e approval stays valid; only the new token needs approving.

---

## 11. Fund the wallet

The bot needs two assets on Doma chain in its wallet:

1. **USDC.e** — half of the capital you intend to deploy. The bot splits USDC.e and the LP token to the optimal ratio at mint time. If you only have USDC.e, the bot will swap some of it to the token. You don't need to pre-balance.

2. **ETH** — for gas. ~0.001 ETH lasts for thousands of operations on Doma. Don't overdo it; the wallet doesn't need to be capital-heavy on ETH.

### Recommended initial funding

For a first-run $100 deployment:

| Asset | Amount | Why |
|---|---|---|
| USDC.e | ~$100 | The bot will split to optimal ratio at mint |
| ETH | ~0.002 | Permit2 + first mint + a few rebalances |

For a $500 deployment, USDC.e = $500, ETH = ~0.003.

### How to send funds

1. From wherever you hold USDC.e on Doma (your main wallet, a bridge, an exchange that supports Doma withdrawals), send to the bot's wallet address.
2. Same for ETH on Doma.

Double-check you're sending on **Doma chain** (chain ID 97477) and not some other chain. Sending Ethereum mainnet USDC to a Doma address means losing those funds — they won't show up.

### Verify the wallet balance

The bot ships with a `check_balance.py` that reads the wallet and reports balances:

```bash
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 check_balance.py
```

Expected output:

```
Wallet: 0xYourAddress...
  ETH:        0.00200000
  USDC.e:     100.000000
  SOFTWARE.ai: 0.000000
Position NFTs: none
```

If you don't see your funds, wait a minute and try again (RPC indexing lag). If still missing, check the block explorer with your wallet address to confirm the funds arrived.

### Refilling later

You'll occasionally need to top up ETH — the bot drains gas slowly. Every couple of weeks, check `check_balance.py` and refill ETH if it's below 0.001.

USDC.e funding is one-shot for a given position — if you want to add more capital later, you do that through `topup_position.py` (Section 20), not by re-funding USDC.e and running `lp_bot.py` from scratch.

---

## 12. Dry run

Before sending a real mint, run the bot in dry-run mode. The script `dry_run.py` does exactly what `lp_bot.py` would do for the first mint — reads the pool, computes the target range, calculates how to split USDC.e + token, **but does not broadcast any transactions**.

```bash
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 dry_run.py
```

Expected output:

```
[dry_run] connected to Doma (chain 97477)
[dry_run] wallet: 0xYourAddress
[dry_run] balances: 100.0000 USDC.e, 2.0e-3 ETH, 0.0 SOFTWARE.ai
[dry_run] pool: 0xPoolAddress
[dry_run] current price: 1 USDC.e = 12.4521 SOFTWARE.ai
[dry_run] current tick: 81450
[dry_run] computed range: ±1.000% → ticks [81350, 81550]
[dry_run] target deposit: 100.00 USD
[dry_run] optimal split for current tick:
            USDC.e: 47.83
            SOFTWARE.ai: 651.4 (~$52.17)
[dry_run] swap needed: 52.17 USDC.e → SOFTWARE.ai
[dry_run] estimated swap output (after 0.5% slippage):  648.1 SOFTWARE.ai
[dry_run] post-swap balances would be:
            USDC.e: 47.83
            SOFTWARE.ai: 648.1
[dry_run] mint preview:
            tickLower: 81350
            tickUpper: 81550
            amount0_desired: 47.83 USDC.e
            amount1_desired: 648.1 SOFTWARE.ai
[dry_run] gas estimate: ~0.00012 ETH
[dry_run] === DRY RUN COMPLETE: NO TRANSACTIONS SENT ===
```

### What to look at

- **Pool address** — matches `.env`?
- **Current price** — matches what doma.xyz shows for this pool?
- **Computed range ticks** — both divisible by your tick spacing?
- **Optimal split** — sanity-check: roughly half USDC.e, half token, in dollar terms?
- **Swap needed** — looks plausible?
- **Gas estimate** — reasonable (single-digit thousandths of ETH)?

### Common dry-run errors

**"pool address does not match factory deployment"** — wrong pool address, or fee tier mismatched.

**"token decimals mismatch"** — `TOKEN_DECIMALS` in `.env` doesn't match the actual token contract.

**"insufficient USDC.e balance: have X, need Y"** — fund more USDC.e, or lower `LP_TOTAL_USD`.

**"current tick X not divisible by spacing Y"** — never seen, but if it ever happens, your `TICK_SPACING` value is wrong.

If anything looks off, **stop and fix it before going live**. The dry-run is your last line of defense against a misconfigured `.env`.

---

## 13. First live mint

You've configured `.env`, run Permit2, funded the wallet, and dry-run looks correct. Now flip the switch.

Edit `.env`:

```ini
DRY_RUN=false
```

Save. Then run the bot **manually**, not under supervisor yet — you want to watch the first mint live:

```bash
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 lp_bot.py
```

You'll see something like:

```
[lp_bot] starting (chain=97477, wallet=0xYourAddr...)
[lp_bot] pool: 0xPoolAddr
[lp_bot] no existing position found
[lp_bot] minting new position
[lp_bot] target range: ±1.00% → ticks [81350, 81550]
[lp_bot] swapping 52.17 USDC.e -> SOFTWARE.ai
[lp_bot] swap tx 0xabc...123 mined (block 1234567)
[lp_bot] received 648.32 SOFTWARE.ai (slippage 0.13%)
[lp_bot] minting position
[lp_bot] mint tx 0xdef...456 mined (block 1234568)
[lp_bot] position NFT id: 4827
[lp_bot] liquidity: 1.234567e+15
[lp_bot] used: 47.81 USDC.e, 645.9 SOFTWARE.ai
[lp_bot] refunded to wallet: 0.02 USDC.e, 2.4 SOFTWARE.ai
[lp_bot] entering monitor loop, interval=2s
[lp_bot] price 12.4521 → in range, 47.3% to upper edge
[lp_bot] price 12.4498 → in range, 47.8% to upper edge
...
```

### What you're looking at

- **`swap tx` mined** — the bot swapped USDC.e for the token to hit the right ratio.
- **`mint tx` mined** — the position is now live on-chain.
- **`position NFT id`** — that's your position. Note it down. You can search this ID on doma.xyz or any block explorer.
- **`refunded to wallet`** — V3 mints often have leftover dust because the optimal amount is computed off the current sqrt-price. The bot sends the refund back to your wallet automatically.
- **monitor loop** — the bot is now polling the pool every 2 seconds.

### If the mint fails

If you see a revert ("execution reverted: STF" or similar), the most common causes are:

1. **Slippage on the prep swap** — price moved too much between the swap quote and execution. The bot retries automatically; if it still fails, raise `LP_SWAP_MAX_SLIPPAGE_PCT` to 0.01 temporarily and retry.
2. **Tick range stale** — price moved outside the proposed range before the mint landed. Re-run the bot; it'll recompute around the new price.
3. **Allowance** — you skipped or mis-ran Permit2 setup. Run `verify_permit2.py` again.

### Let it run for a few minutes

Watch the log for at least 5 minutes. You should see:

- The position stays "in range" (price within your ±1% band).
- The percentage-to-edge metric moves around but doesn't approach the rebalance trigger.
- No errors.

Once you're happy, press `Ctrl+C` to stop. The position stays alive on-chain regardless — stopping the bot doesn't burn your position, it just stops monitoring.

Now move to Section 14 to verify the position from a second source, then to Section 15 to run the bot as a long-lived process.

---

## 14. Verify the position on-chain

Trust but verify. After the first mint, confirm independently that the NFT exists and represents what you expect.

### From the command line

```bash
python3 check_balance.py
```

Should now show your position:

```
Wallet: 0xYourAddress
  ETH:        0.00187...
  USDC.e:     0.02
  SOFTWARE.ai: 2.4

Position NFTs (1):
  #4827  ticks=[81350, 81550]  liquidity=1.234567e+15
         USDC.e ≈ 47.81  SOFTWARE.ai ≈ 645.9
         uncollected fees: 0.00 / 0.0
```

### From doma.xyz

1. Go to https://doma.xyz/explore and find your pool.
2. Look for a "My positions" or wallet panel.
3. Connect (read-only is fine) with the bot's wallet address.
4. Your position should appear with the same NFT ID, tick range, and liquidity numbers as the CLI.

### From a block explorer

If Doma has a block explorer (most chains do), paste the bot's wallet address. You should see:

- The mint transaction.
- The NFT (ERC-721 from the NPM contract) now owned by the wallet.

If all three sources agree, you're set.

---

## 15. Production setup with supervisor

Now wire the bot into supervisor so it survives reboots and auto-restarts on crashes.

### Install supervisor (if not already)

```bash
apt install -y supervisor
systemctl enable supervisor
systemctl start supervisor
```

### Install the bot's supervisor config

The repo includes `lp_bot.supervisor.conf`. Copy it to supervisor's conf directory:

```bash
cp /root/doma-automation/lp-bot/lp_bot.supervisor.conf /etc/supervisor/conf.d/lp_bot.conf
```

Open it and confirm the paths match your install:

```bash
nano /etc/supervisor/conf.d/lp_bot.conf
```

It should look roughly like:

```ini
[program:lp_bot]
command=/root/doma-automation/venv/bin/python3 /root/doma-automation/lp-bot/lp_bot.py
directory=/root/doma-automation/lp-bot
autostart=true
autorestart=true
stderr_logfile=/var/log/lp_bot.err.log
stdout_logfile=/var/log/lp_bot.out.log
user=root
stopsignal=INT
stopwaitsecs=10
```

If your install path differs, update the `command` and `directory` lines.

### Tell supervisor about it

```bash
supervisorctl reread
supervisorctl update
supervisorctl start lp_bot
```

### Check status

```bash
supervisorctl status lp_bot
```

Expected:

```
lp_bot    RUNNING   pid 4567, uptime 0:00:08
```

### Tail the logs

```bash
tail -f /var/log/lp_bot.out.log
```

You should see the bot's monitor-loop heartbeat every couple of seconds, the same kind of output you saw running manually.

To exit `tail`, press `Ctrl+C`. The bot keeps running — `tail` only watches the file.

### Restarting / stopping

```bash
supervisorctl restart lp_bot
supervisorctl stop lp_bot
supervisorctl start lp_bot
```

Important: `stop lp_bot` does **not** exit the position. It just stops the monitoring process. The LP position keeps existing on-chain unmonitored. If price drifts out of range while the bot is stopped, you simply earn $0 until you start it again or until you manually exit (Section 21).

### Survives reboots?

Test it. Reboot the server:

```bash
reboot
```

SSH back in once it's up. Run:

```bash
supervisorctl status lp_bot
```

If it's `RUNNING`, the autostart works. If not, check `/var/log/lp_bot.err.log` for the failure reason — usually a missing venv path or a misconfigured `.env`.

---

## 16. Monitoring and tuning

The bot is running. Now what?

### Daily routine

A reasonable check-in is once or twice a day:

```bash
# 1. Is the bot still running?
supervisorctl status lp_bot

# 2. What's it been doing?
tail -50 /var/log/lp_bot.out.log

# 3. What's the position look like right now?
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 check_balance.py
```

You're looking for:

- **Bot is RUNNING** in supervisorctl.
- **No repeated error messages** in the log (occasional `nonce too low` is fine — see troubleshooting).
- **Position has positive uncollected fees** — they should grow until the next compound cycle, then drop near zero, then grow again.

### When to tune

Watch the bot for at least 24 hours before tuning anything. Adaptive range will adjust automatically as it learns the pool's volatility.

If after a few days you observe:

- **Constant rebalances (more than once an hour)** — raise `LP_REBALANCE_TRIGGER_PCT` (closer to 1.0 means rebalance less aggressively) or widen `LP_RANGE_MAX`.
- **Position frequently out of range** — widen the range floor (`LP_RANGE_MIN`).
- **No fee accumulation despite pool volume** — your range is too narrow relative to where price actually trades. Disable adaptive temporarily and set a wider static range.
- **Big swings in equity** — IL is hurting you. Either accept it as the cost of being in this pool, or move to a less volatile pool.

### Reading the log

A healthy log looks like:

```
[lp_bot] price 12.4521 → in range, 47.3% to upper edge
[lp_bot] price 12.4498 → in range, 47.8% to upper edge
[lp_bot] price 12.4612 → in range, 49.1% to upper edge
[lp_bot] ... (continues every 2s)
[lp_bot] compound check: fees 0.34 USDC.e + 4.2 SOFTWARE.ai (~$0.68)
[lp_bot] above threshold, starting compound cycle
[lp_bot] collect tx 0x... mined
[lp_bot] rebalancing fee tokens
[lp_bot] swap tx 0x... mined
[lp_bot] increaseLiquidity tx 0x... mined
[lp_bot] compounded $0.66 added back to position
[lp_bot] price 12.4598 → in range, 48.2% to upper edge
...
```

A worrying log has repeated:

```
[lp_bot] ERROR: ...
[lp_bot] retrying...
[lp_bot] ERROR: ...
[lp_bot] retrying...
```

If you see a stuck retry loop, see Section 22 (recovery) and Section 25 (troubleshooting).

### Disk and log rotation

The bot's stdout log can grow large over time. Set up logrotate:

```bash
cat > /etc/logrotate.d/lp_bot <<'EOF'
/var/log/lp_bot.out.log /var/log/lp_bot.err.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
EOF
```

This keeps 14 days of rotated logs.

---

## 17. Compound mechanics

The bot compounds earned fees back into the position every 6 hours by default (`LP_COMPOUND_INTERVAL_SEC=21600`). What it actually does:

1. **Read uncollected fees.** Query the NPM for `tokensOwed0` and `tokensOwed1`.
2. **Convert to USD value.** Multiply by current price.
3. **Compare to threshold.** If below `LP_COMPOUND_MIN_USD` (default $0.50), skip and wait for the next cycle.
4. **Collect.** Call `NPM.collect()` to pull both tokens to the wallet.
5. **Re-balance.** Compute the ratio needed to add liquidity in the current tick. Swap one side into the other on the Universal Router until ratios match.
6. **`increaseLiquidity`.** Add the rebalanced tokens back into the same position NFT.

Total cost per compound: one swap + one `increaseLiquidity` call. On Doma, this is approximately 0.00005 ETH in gas.

### When compounding is *worth it*

Roughly: if your accrued fees in dollar terms are at least 10× the gas cost, compounding adds equity. The default `LP_COMPOUND_MIN_USD=0.50` is well above that threshold given Doma's gas economics.

If you have a very small position (e.g. $50 in a slow pool), you might find that 6 hours doesn't produce $0.50 in fees and the bot just keeps skipping the compound. That's fine — fees keep accruing on the NFT and will eventually trigger.

If you want more aggressive compounding on a small position, lower `LP_COMPOUND_MIN_USD` to 0.20 or so. Below that, gas starts to eat the gains.

### What compound mechanics looks like in the log

```
[compound] 6h elapsed since last compound, checking fees
[compound] fees: 0.42 USDC.e + 5.1 SOFTWARE.ai (~$0.83)
[compound] above threshold, executing
[compound] calling NPM.collect()
[compound] collect tx 0xaaa...111 mined
[compound] received 0.42 USDC.e + 5.1 SOFTWARE.ai to wallet
[compound] optimal ratio at current tick: USDC.e/TOKEN = 0.082
[compound] swapping 0.18 USDC.e -> SOFTWARE.ai
[compound] swap tx 0xbbb...222 mined
[compound] post-swap: 0.24 USDC.e + 7.4 SOFTWARE.ai
[compound] calling NPM.increaseLiquidity()
[compound] increase tx 0xccc...333 mined
[compound] added liquidity = 1.456e+13 to position 4827
[compound] new total liquidity = 1.249e+15 (+1.2%)
[compound] cycle complete
```

The new liquidity number tells you the position is growing.

### What if compound fails mid-cycle?

The compound cycle is split into discrete on-chain calls. If `collect` succeeds but the swap fails, the bot has tokens sitting in the wallet — it will retry the swap on the next loop. If the swap succeeds but `increaseLiquidity` fails, same story.

Worst case: tokens are stranded on the **Universal Router** (a known edge case with V3 swap routers). The bot ships `recover_router_tokens.py` for exactly this — see Section 22.

---

## 18. Rebalance mechanics

When price approaches the edge of the position's range, the bot rebalances:

1. **Detect trigger.** The monitor loop computes "proximity to edge" — how far price has moved from the position center toward one edge. If proximity ≥ `LP_REBALANCE_TRIGGER_PCT` (default 0.50), rebalance.
2. **Compute new range.** Use current adaptive range (Section 19) to size the new range around the new mid-price.
3. **Decrease liquidity to zero.** Call `decreaseLiquidity` to extract all tokens from the position.
4. **Collect fees.** Same `collect()` call as the compound flow.
5. **Burn the empty NFT.** Optional but tidy; the bot keeps the NFT in some configurations.
6. **Re-balance tokens.** Swap whichever side is over-allocated into the other.
7. **Mint a new position.** Same flow as the initial mint.

Total cost: one `decreaseLiquidity` + one `collect` + one swap + one mint = roughly 0.00015 ETH on Doma.

### What rebalance looks like in the log

```
[monitor] price 12.6043 → 92.3% to upper edge
[monitor] trigger reached (>50%), starting rebalance
[rebalance] target range: ±0.75% → ticks [82240, 82420] (adaptive)
[rebalance] decreaseLiquidity tx 0x... mined
[rebalance] withdrew 21.3 USDC.e + 738.5 SOFTWARE.ai
[rebalance] collect tx 0x... mined  (fees: 0.05 USDC.e + 1.2 SOFTWARE.ai)
[rebalance] optimal ratio for new tick: USDC.e/TOKEN = 0.063
[rebalance] swapping 388 SOFTWARE.ai -> USDC.e
[rebalance] swap tx 0x... mined
[rebalance] post-swap: 26.1 USDC.e + 414 SOFTWARE.ai
[rebalance] minting new position
[rebalance] mint tx 0x... mined
[rebalance] new position NFT id: 4901, liquidity 1.31e+15
[rebalance] cycle complete (cost: 0.00018 ETH gas + 0.012% swap slippage)
```

### Rebalance frequency expectations

- **Stable pool, tight range**: 0–2 rebalances per day.
- **Active pool, default range**: 2–6 per day.
- **Volatile pool, default range**: 10+ per day.

If you're seeing more than 10 per day on a $100 position, you're net-losing to swap costs. Widen the range or pick a less volatile pool.

### "Why did my rebalance pick a weird range?"

The adaptive ranger recomputes range at every rebalance. If the previous 24h was unusually quiet, it may tighten further. If the previous 24h was unusually volatile, it may widen.

You can override by setting `ADAPTIVE_RANGE_ENABLED=false` and using `LP_RANGE_PCT` as a static range.

---

## 19. Adaptive range, in plain language

The bot's adaptive range feature watches the pool's realized 24-hour high-low spread and continuously tunes your range size to match.

### The math, simply

Every loop, the bot stores the current price in memory. After `ADAPTIVE_RANGE_LOOKBACK_HRS` (default 24) of snapshots accumulated (or once it has at least 10 samples, whichever is first), it computes:

- `realized_range = (max_price - min_price) / mid_price`
- `target_range = clamp(realized_range × ADAPTIVE_RANGE_MULTIPLIER / 2, LP_RANGE_MIN, LP_RANGE_MAX)`

So if the pool moved between $12.20 and $12.80 in the last 24 hours (mid $12.50, realized range ~4.8%), the bot targets:

`target = clamp(4.8% × 1.5 / 2, 0.5%, 5%) = clamp(3.6%, 0.5%, 5%) = 3.6%`

It will use ±3.6% as the new range on the next rebalance.

If the pool only moved 0.4% in the last 24 hours:

`target = clamp(0.4% × 1.5 / 2, 0.5%, 5%) = clamp(0.3%, 0.5%, 5%) = 0.5%`

It picks the floor (0.5%) — meaningfully tighter, but not below the configured minimum.

### Why × 1.5?

You want the range to be a bit *wider* than realized vol so you don't bounce out of range every time price wiggles. 1.5× realized range is empirically a good "stay in range most of the time, but not too wide to dilute fees" balance. You can tune `ADAPTIVE_RANGE_MULTIPLIER` if you want more aggressive (lower) or more conservative (higher).

### Why divided by 2?

Range is expressed as half-width (±X% around center). Realized range is full-width (max − min). So half-width = full-width / 2.

### When does it actually adjust?

Adaptive range is recomputed at every **rebalance event** — not continuously. So the bot doesn't burn your position just because the math says the range should be a hair wider. It waits until price triggers a natural rebalance, then mints the new position at the adjusted range.

### The floor and ceiling

- `LP_RANGE_MIN=0.005` — never tighter than ±0.5%. Below this, gas costs as a fraction of position size start dominating.
- `LP_RANGE_MAX=0.05` — never wider than ±5%. Above this, concentration multiplier drops too low to be worthwhile.

You can adjust both. For very stable pools (a USDC.e/USDT-style pair), drop `LP_RANGE_MIN` to 0.001. For very volatile tokens, raise `LP_RANGE_MAX` to 0.10 or more.

---

## 20. Adding capital to an existing position

You watched the bot run for a week and you're happy with it. You want to add more USDC.e to the same position. Don't kill the bot and re-mint — use `topup_position.py`.

### Flow

1. Fund the bot's wallet with additional USDC.e.
2. Run `topup_position.py`. It:
   - Reads the existing position NFT.
   - Determines the optimal USDC.e/token split for the current tick.
   - Swaps a portion of new USDC.e to the token to hit that ratio.
   - Calls `increaseLiquidity` on the existing NFT.

### Step-by-step

Send USDC.e to the bot's wallet (any amount). Then:

```bash
# Stop the bot momentarily so it doesn't fight you on RPC
supervisorctl stop lp_bot

cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 topup_position.py
```

Expected output:

```
[topup] wallet: 0xYourAddress
[topup] balances: 200.0 USDC.e, 0.001 ETH
[topup] existing position: NFT #4827, ticks=[81350, 81550]
[topup] current tick: 81442 (in range)
[topup] optimal split for tick: USDC.e/TOKEN = 0.078
[topup] swapping 92.3 USDC.e -> SOFTWARE.ai
[topup] swap tx 0x... mined
[topup] received 1156 SOFTWARE.ai
[topup] increaseLiquidity tx 0x... mined
[topup] added 7.45e+14 liquidity to position 4827
[topup] new total liquidity: 1.98e+15
[topup] complete
```

Then restart the bot:

```bash
supervisorctl start lp_bot
```

The bot will pick up the now-larger position and continue monitoring.

### Important caveats

- The topup only works if the **position is still in range**. If price has drifted outside, the topup will fail because there's no balanced split possible. Run a rebalance first (by starting the bot and letting it trigger, or by manually exiting and re-minting via `lp_bot.py`).
- Update `LP_TOTAL_USD` in `.env` after a top-up so future log output is accurate. The bot uses it as a display reference, not a strict cap.

---

## 21. Exiting cleanly

Time to get out — pool dying, you need the capital elsewhere, taking profits. The clean way is `exit_position.py`.

### What it does

1. Reads the existing position NFT.
2. Calls `decreaseLiquidity` to extract 100% of the liquidity.
3. Calls `collect` to pull both tokens (principal + accrued fees) to the wallet.
4. Burns the NFT (the position is now empty and useless; cleanup).
5. Optionally swaps the LP token side back to USDC.e (controlled by a flag in the script).

After it runs, your wallet holds USDC.e + the LP token (or all USDC.e if you swap on exit). The bot's loop, if running, will see no position and idle.

### Step-by-step

```bash
# Stop the bot first
supervisorctl stop lp_bot

cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 exit_position.py
```

Expected output:

```
[exit] wallet: 0xYourAddress
[exit] existing position: NFT #4827, liquidity 1.98e+15
[exit] decreaseLiquidity tx 0x... mined
[exit] withdrew 95.1 USDC.e + 1289 SOFTWARE.ai
[exit] collect tx 0x... mined
[exit] collected 0.83 USDC.e + 12.4 SOFTWARE.ai in fees
[exit] burn tx 0x... mined
[exit] position 4827 burned
[exit] wallet now holds: 95.93 USDC.e + 1301.4 SOFTWARE.ai
[exit] complete
```

If you want everything returned to USDC.e, run a swap script or do it manually via doma.xyz. The bot ships an exit-to-USDC-only mode but check the script flags before relying on it.

### After exiting

```bash
python3 check_balance.py
```

Confirm no positions remain. Then withdraw funds from the bot's wallet to your main wallet on Doma.

You can also leave the bot's wallet funded and re-deploy into a different pool by editing `.env`'s `LP_POOL_ADDRESS` / `LP_TOKEN_ADDRESS` / `LP_FEE_TIER` / `TICK_SPACING` / `TOKEN_DECIMALS` fields, re-running `verify_permit2.py` (for the new token), then `lp_bot.py`.

---

## 22. Emergency recovery

Sometimes things go sideways. The bot ships two recovery scripts.

### `recover_position.py`

Use when a partial state happened: e.g., the position has `liquidity=0` (rebalance interrupted) but `tokensOwed > 0` (fees not collected), or a fresh mint half-completed.

```bash
supervisorctl stop lp_bot
cd /root/doma-automation/lp-bot
source ../venv/bin/activate
python3 recover_position.py
```

The script:

1. Detects the broken state by reading the NFT.
2. Collects any tokens owed.
3. Decreases liquidity to zero if not already.
4. Optionally burns the NFT.
5. Leaves you with funds in the wallet.

After running, either re-mint with `lp_bot.py` or exit and reassess.

### `recover_router_tokens.py`

Edge case: a swap completed but the bot crashed before consuming the output, leaving tokens stranded on the **Universal Router** contract. This is rare but worth knowing about. The script sweeps any tokens the router has approved back to you.

```bash
supervisorctl stop lp_bot
python3 recover_router_tokens.py
```

If there's nothing to recover, it exits silently.

### Manual recovery

If the recovery scripts can't fix the state, you can always exit manually using a wallet UI:

1. Open Doma's NPM contract (`0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0`) in a contract-call interface like a block explorer's "Write Contract" tab.
2. Connect with the bot's wallet (export the private key from the mnemonic, import to MetaMask, only do this if you understand the security implications).
3. Call `decreaseLiquidity` with `liquidity` = current liquidity, `tokenId` = your NFT.
4. Call `collect` to pull tokens.

This is a last resort. Try `recover_position.py` first.

---

## 23. Stopping the bot

When you want the bot to stop monitoring:

```bash
supervisorctl stop lp_bot
```

That's all. The position stays on-chain unchanged. You earn no fees while stopped because rebalance and compound don't happen, but the position still passively earns whatever swaps cross your range.

To resume:

```bash
supervisorctl start lp_bot
```

To stop permanently and free the resources:

```bash
supervisorctl stop lp_bot
rm /etc/supervisor/conf.d/lp_bot.conf
supervisorctl reread
supervisorctl update
```

(Optionally exit the position first with `exit_position.py`. Don't leave an unmonitored position open in a pool you've abandoned — it's free fees for whoever's monitoring against you.)

---

## 24. Optional: dashboard API

If you want to view your position from a web dashboard rather than SSHing in to read logs, the repo includes a tiny FastAPI service that exposes:

- `GET /healthz` — uptime check (no auth).
- `GET /api/lp/summary` — current position state, balances, fees (auth required).
- `GET /api/lp/logs` — tail of the bot's stdout log (auth required).

It runs as a separate supervisor process on port 5002.

### Deploy

Generate an API key and add it to `.env`:

```bash
echo "LP_API_KEY=$(openssl rand -hex 32)" >> /root/doma-automation/lp-bot/.env
```

Optionally set a log path override:

```bash
echo "LP_LOG_PATH=/var/log/lp_bot.out.log" >> /root/doma-automation/lp-bot/.env
```

Wire up the supervisor config:

```bash
cp /root/doma-automation/lp-bot/api.supervisor.conf /etc/supervisor/conf.d/lp_api.conf
supervisorctl reread
supervisorctl update
supervisorctl start lp_api
```

Confirm it's running:

```bash
supervisorctl status lp_api
# lp_api    RUNNING   pid 4123, uptime 0:00:05
```

Smoke test:

```bash
# Health (no auth)
curl http://localhost:5002/healthz

# Summary (auth required)
curl -H "X-API-Key: $(grep ^LP_API_KEY .env | cut -d= -f2)" \
     http://localhost:5002/api/lp/summary
```

### Firewall

If you want to access the API from outside the server, open the port:

```bash
ufw allow 5002/tcp
```

Then reach it from your dashboard at `http://your-server-ip:5002/api/lp/summary` with the `X-API-Key` header set.

### Security note

The API serves on port 5002, unencrypted HTTP. Anyone who knows your IP and the API key can read your position. **Use a strong API key** (32+ hex bytes from `openssl rand -hex 32`). Do not expose the API to the open internet without HTTPS — put it behind nginx + Let's Encrypt or use Cloudflare. If you only want the API consumed by a specific dashboard host (e.g. a Vercel app), restrict it via `ufw` to that host's outbound IP range.

---

## 25. Troubleshooting

### "nonce too low" errors

Look like:

```
[lp_bot] ERROR: nonce too low
[lp_bot] retrying...
```

**What's happening:** the RPC's view of your wallet's nonce was stale at the moment you broadcast a tx. The bot fetches the latest nonce and retries automatically. **You can ignore these as long as they're occasional** (a few a day). If they're happening every loop, your RPC is sick — switch to `RPC_BACKUP` in `.env` and restart the bot.

### Position shows `liquidity=0` but `tokensOwed > 0`

A rebalance got interrupted halfway (e.g. the bot crashed between `decreaseLiquidity` and the new mint). Run `recover_position.py` (Section 22), then re-start the bot. It will mint a fresh position.

### Stale RPC balance after a swap

If you manually query the wallet balance right after a swap completes, you might see the pre-swap value because the RPC hasn't caught up. The bot itself parses transaction receipts to compute post-swap balances, so it doesn't have this problem. You only see it if you're running `check_balance.py` in a tight loop. Wait 5 seconds and try again.

### Compound says "no fees" but you know there were

The bot's compound min threshold (`LP_COMPOUND_MIN_USD=0.50`) suppresses tiny compounds. Wait longer for fees to accrue, or lower the threshold.

```ini
LP_COMPOUND_MIN_USD=0.20
```

Below 0.10, gas costs start eating most of the compound's value. Don't go lower than that.

### Position out of range for hours

The adaptive range will widen on the next rebalance, but the bot only rebalances when price moves toward the edge — by definition, if price is stationary *outside* the range, the bot never triggers. Two options:

1. **Wait.** Eventually price re-enters and the bot resumes normal operation.
2. **Manually exit + re-mint.** Run `exit_position.py`, then `lp_bot.py`. The new mint will be centered on the current price.

If this keeps happening, the pool is volatile enough that the lower bound on the adaptive range is too tight. Raise `LP_RANGE_MIN` to 0.01 or 0.02.

### "Insufficient ETH for gas"

The bot ran out of ETH. Fund the wallet with another 0.001-0.002 ETH and the bot will pick up automatically on the next loop.

### 18-decimal token (WETH-style)

If you're LPing in a pool where the non-USDC.e side has 18 decimals (e.g. WETH), make sure:

```ini
TOKEN_DECIMALS=18
TICK_SPACING=...   # still depends on fee tier
```

The bot handles 6 vs 18 cleanly — but only if you tell it the right decimals.

### Bot keeps restarting under supervisor

Symptoms:

```
supervisorctl status lp_bot
lp_bot    BACKOFF   Exited too quickly (process log may have details)
```

Read the error log:

```bash
tail -100 /var/log/lp_bot.err.log
```

Common causes:

- `.env` missing or unreadable (check `chmod` and `chown`).
- Python venv path wrong in supervisor config.
- A required `.env` field empty (e.g. `LP_POOL_ADDRESS=`).
- The Permit2 setup hasn't been run (the bot bails on first transaction).

Fix the underlying issue and `supervisorctl start lp_bot` again.

### `LP_TOKEN_ADDRESS` is mistakenly the USDC.e side

This produces a really confusing error trail because the bot treats USDC.e as if it were the token and vice versa. Symptoms include impossible price quotes (price = 0.0000001) and tick computations that overflow. Re-check `.env`: `LP_TOKEN_ADDRESS` must be the **non-USDC.e** side of the pair.

### "execution reverted: STF" or "Too little received"

Slippage check failed on a swap. The bot retries automatically with a fresh quote. If it persists, raise `LP_SWAP_MAX_SLIPPAGE_PCT` from 0.005 to 0.01 temporarily. Don't leave it above 0.01 — that opens you up to MEV.

### RPC timeouts / connection refused

```
[lp_bot] ERROR: connection error to RPC
```

Switch to the backup RPC:

```ini
RPC_URL=https://rpc.doma.xyz
RPC_BACKUP=https://doma.drpc.org
```

(Swap the two.) Restart the bot. If both are down, the chain itself is having an issue — wait for it to recover.

---

## Appendix A: math walkthroughs

For users who want to understand what the numbers in the log actually mean.

### Concentration multiplier

In V3, a position spread across a tighter range earns more per swap per dollar of capital, proportional to the inverse square root of the price range width.

Approximate concentration multiplier vs ±X% range:

| Range | Multiplier vs V2 (full-range) |
|---|---|
| ±50% (≈ V2) | 1× |
| ±10% | ~7× |
| ±5% | ~13× |
| ±2% | ~30× |
| ±1% | ~60× |
| ±0.5% | ~120× |
| ±0.1% | ~580× |

So a position at ±0.5% earns roughly 120× the fees that the same capital would earn in V2 — *if price stays in range the whole time*. Out of range = 0 fees, regardless of multiplier.

### Worked APR example

Pool: USDC.e/SOFTWARE.ai 0.05%
TVL: $52,000
24h volume: $200,000
Active LPs: 12 concentrated, mostly ±2-5% ranges
Your deployment: $500 at ±0.5%

Step 1: pool's daily fee income.

`$200,000 × 0.05% = $100/day`

Step 2: estimate active liquidity at the current tick.

Roughly half the LPs have ranges that include the current price. Call active liquidity ~$25K.

Step 3: your share of active liquidity, adjusted for concentration multiplier.

Your raw share: $500 / $25,000 = 2%.

Your concentration multiplier at ±0.5% vs the ~±2.5% average for other LPs:

`120 / 25 ≈ 4.8×`

Your effective share: 2% × 4.8 ≈ 9.6%.

Step 4: your daily fees.

`$100 × 9.6% ≈ $9.60/day` while in range.

Adjust for time-in-range. If price stays in your ±0.5% band ~70% of the time:

`$9.60 × 0.7 ≈ $6.70/day`

Step 5: annualize.

`$6.70 × 365 = $2,445/year on $500 capital → 489% APR (gross, before IL)`

These numbers are illustrative. In practice the multiplier is fuzzier, time-in-range varies, and IL eats some of the upside. But the order of magnitude — high-double to triple-digit APR on a healthy small pool — is realistic.

### Impermanent loss math

If the non-USDC.e token's price moves from P₀ to P₁, with price ratio `r = P₁ / P₀`, a V2 (full-range) LP suffers IL:

`IL = 2·√r / (1+r) − 1`

| Price move | IL |
|---|---|
| 1.25× | -0.6% |
| 1.5× | -2.0% |
| 2× | -5.7% |
| 3× | -13.4% |
| 5× | -25.5% |

For V3 concentrated positions, IL within the range follows the same curve. The difference is that once you're outside the range, your position is 100% on one side — so the IL is "frozen" at whatever it was at the edge, but you also stop earning fees.

For a ±1% range, the maximum IL while in-range is about 0.005%. As soon as price exits the range, your position holds 100% of the worse-performing token (relative to a 50/50 hold) — you've effectively sold the winner and held the loser.

### Time-in-range vs range width tradeoff

Wider range = higher time-in-range = lower concentration multiplier. Tighter range = lower time-in-range = higher multiplier. The sweet spot depends entirely on pool volatility.

Rule of thumb: pick the range that gives you ~70–80% time-in-range over a 24-hour window. The adaptive ranger targets exactly this when its multiplier is set near 1.5.

---

## Appendix B: full `.env` reference

The complete annotated configuration:

```ini
# ─── Doma LP Bot Configuration ───

# ─── Wallet (REQUIRED) ────────────────────────────────────────────────
# Use a NEW dedicated wallet — never reuse your main wallet.
# Either set MNEMONIC (recommended) or raw PRIVATE_KEY.
MNEMONIC=
MNEMONIC_ACCOUNT_INDEX=0
PRIVATE_KEY=

# ─── Network ──────────────────────────────────────────────────────────
RPC_URL=https://doma.drpc.org
RPC_BACKUP=https://rpc.doma.xyz
CHAIN_ID=97477

# ─── Doma V3 contracts (already filled, leave as-is) ──────────────────
USDCE_ADDRESS=0x31EEf89D5215C305304a2fA5376a1f1b6C5dc477
UNIVERSAL_ROUTER=0x5089863E97196773038f98459262D866f2281f58
NPM_ADDRESS=0xce126ca6aceBBDCe95D7b8A3Ce637951640811E0
V3_FACTORY=0x2e50b586d5bcD04cb6125E028A6a669f7f3cF1C2

# ─── LP target pool (REQUIRED) ────────────────────────────────────────
# Find these on https://doma.xyz/explore — click the pool you want to LP.
LP_POOL_ADDRESS=
LP_TOKEN_ADDRESS=
LP_TOKEN_SYMBOL=YOURTOKEN

# Pool fee tier (100=0.01%, 500=0.05%, 3000=0.30%, 10000=1.00%)
LP_FEE_TIER=500

# Tick spacing — depends on fee tier:
#   fee 100  → tickSpacing 1
#   fee 500  → tickSpacing 10
#   fee 3000 → tickSpacing 60
#   fee 10000→ tickSpacing 200
TICK_SPACING=10

# Token decimals
USDCE_DECIMALS=6
TOKEN_DECIMALS=6

# ─── Strategy ─────────────────────────────────────────────────────────
LP_TOTAL_USD=100

# Static range (used only if ADAPTIVE_RANGE_ENABLED=false)
# Expressed as fraction: 0.005 = ±0.5%, 0.020 = ±2%
LP_RANGE_PCT=0.010

# Rebalance when position is this fraction of the way to range edge
# 0.50 = rebalance at 50% to edge. Lower = more rebalancing.
LP_REBALANCE_TRIGGER_PCT=0.50

# Don't compound fees until accrued > this amount
LP_MIN_FEES_TO_COLLECT_USD=0.50

# Slippage tolerance on swaps during rebalance/compound
LP_SWAP_MAX_SLIPPAGE_PCT=0.005
LP_EMERGENCY_SLIPPAGE_PCT=0.02

# Don't enter pools with low 24h volume (USD) — warning only
LP_MIN_24H_VOLUME_USD=500

# How often the bot polls the pool
LP_LOOP_INTERVAL_SEC=2

# How often to compound earned fees back into position
LP_COMPOUND_INTERVAL_SEC=21600   # 6 hours
LP_COMPOUND_MIN_USD=0.50

# ─── Adaptive range (recommended) ─────────────────────────────────────
# Bot tightens/widens range based on observed price volatility.
ADAPTIVE_RANGE_ENABLED=true
ADAPTIVE_RANGE_LOOKBACK_HRS=24
ADAPTIVE_RANGE_MULTIPLIER=1.5
LP_RANGE_MIN=0.005      # never tighter than ±0.5%
LP_RANGE_MAX=0.05       # never wider than ±5%
LP_RANGE_DEFAULT=0.010  # used until 10+ price snapshots collected

# ─── API server (optional, for dashboard integration) ─────────────────
# Generate random: openssl rand -hex 32
LP_API_KEY=
LP_LOG_PATH=/var/log/lp_bot.out.log

# ─── Mode ─────────────────────────────────────────────────────────────
# Set DRY_RUN=true for testing
DRY_RUN=true
```

---

## Appendix C: capital-scaling cheat sheet

How to think about deploying different amounts of capital:

### $50–$100 — starter

- **Range:** tight (±0.5% to ±1%) works because rebalance costs in absolute dollars are tiny.
- **Pool TVL:** any pool with >$10K TVL is safe; you won't move the pool.
- **Rebalance frequency:** can absorb many per day economically.
- **Goal:** learn the bot. Watch the first compound and first rebalance. Confirm log output is healthy.

### $200–$500 — comfortable

- **Range:** medium (±0.5% to ±1.5%) typically optimal.
- **Pool TVL:** prefer >$30K to keep your share below 2%.
- **Rebalance frequency:** every few hours is fine.
- **Goal:** real fee income. Should comfortably out-earn IL on a healthy pool.

### $500–$2,000 — meaningful

- **Range:** ±0.5% to ±2%.
- **Pool TVL:** target >$50K so you're below 4% of pool.
- **Rebalance frequency:** want adaptive range to settle into a wider band so you're not paying per-rebalance costs as often.
- **Goal:** material yield. Worth thinking about diversifying across 2 pools.

### $2,000–$10,000 — large for retail

- **Range:** ±1% to ±3%.
- **Pool TVL:** must be >$100K. Your position becoming >10% of pool TVL means slippage on your own rebalances starts mattering.
- **Rebalance frequency:** keep wide to minimize churn.
- **Goal:** split across 2-3 pools if any one would exceed 8% of pool TVL.

### $10,000+ — institutional

- **Above 10% of pool TVL** in any single pool: you are now the price-setter on rebalances. Split across multiple pools.
- Consider running multiple bot instances, one per pool, each with its own wallet.
- Monitor closely; large positions get noticed by MEV bots, and your rebalances become predictable targets.

---

## Closing checklist

Before you walk away and let the bot run for a week, confirm every item:

- [ ] Dedicated wallet generated, mnemonic written down on paper, stored safely.
- [ ] `.env` filled in: mnemonic, pool address, token address, fee tier, tick spacing, token decimals.
- [ ] `.env` permissions set to `600`.
- [ ] Wallet funded with USDC.e (for capital) + ETH (for gas).
- [ ] `verify_permit2.py` run successfully — all 4 approvals complete.
- [ ] `dry_run.py` output checked, all values look reasonable.
- [ ] `DRY_RUN=false` flipped.
- [ ] First mint run manually, log watched live, no errors.
- [ ] Position verified on doma.xyz and in `check_balance.py`.
- [ ] Supervisor config installed and `lp_bot` shows RUNNING.
- [ ] Logrotate config in place.
- [ ] (Optional) API server up and reachable from your dashboard.
- [ ] Reboot-tested — bot auto-restarts on server reboot.

If any item is unchecked, finish that step before you leave the bot unattended.

---

## Safety reminders, one more time

🔐 **Dedicated wallet only.** Never put your main wallet's mnemonic in `.env`.

💸 **LP earns fees and suffers IL.** Net return depends on price stability + fees. In a calm, active pool: positive. In a chaotic, low-volume pool: probably negative.

🧪 **Always dry-run first** after any config change, not just on initial setup. If you change the pool, change the range floor, change the fee tier — dry-run.

📉 **Start small.** $50–$100 first. Scale only after you've watched the bot rebalance and compound at least once and seen the logs look clean.

⏰ **Monitor.** Even with adaptive range and recovery scripts, the bot is not a "set and forget for a year" tool. Check it daily for the first week and at least weekly thereafter.

🚨 **Emergency exit:** `python3 exit_position.py` returns all funds to your wallet. Memorize the command. If something feels wrong, exit first, debug second.

---

*This guide is provided as-is. The Doma LP bot is open-source software that executes on-chain transactions with your funds. You are responsible for your own configuration, capital allocation, and risk management. Use at your own risk.*
