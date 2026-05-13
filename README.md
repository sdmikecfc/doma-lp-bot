# Doma LP Bot

Automated concentrated liquidity provider for Uniswap V3-style pools on Doma. Mints positions, rebalances when price drifts, compounds earned fees, adapts range to realized volatility.

## What it does

- Mints a concentrated LP position around current price
- Monitors position every 2 seconds
- Rebalances when price approaches range edge (configurable trigger)
- Compounds earned fees back into position every 6h
- Adapts range tightness to realized volatility (more capital efficient)
- Optional HTTP API for dashboard integration

## What it earns

Concentrated LP fees on swap activity. APR depends on:
- Pool fee tier (0.05% pools typically out-earn 0.30% pools by 5×+)
- Pool's daily volume
- Competition (number of other concentrated LPs at the same tick)
- Your range tightness (tighter = higher concentration multiplier)

Realistic range for active Doma pools: **50% — 500% APR** at $100-$1000 capital.

## Risks

- **Impermanent loss** — when price moves, you end up holding the worse side
- **Swap costs** — every rebalance costs 2 swap fees + slippage
- **Dead pools** — if volume drops to zero, you earn nothing while paying gas
- **Slippage on big positions** — too big = your own swaps move the pool

## Quick start

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
nano .env   # fill in MNEMONIC, LP_POOL_ADDRESS, LP_TOKEN_ADDRESS, etc.

# 3. Verify Permit2 setup (does 2 one-time approvals)
python3 verify_permit2.py

# 4. Dry run first
python3 dry_run.py

# 5. Live run
python3 lp_bot.py
```

## Where to find pool/token addresses

1. Visit https://doma.xyz/explore
2. Click the pool you want to LP in
3. Copy the pool address + the non-USDC token address
4. Note the fee tier (0.01%, 0.05%, 0.30%, or 1%)
5. Look up tick spacing based on fee tier (see `.env.example`)

## Key scripts

- `lp_bot.py` — main loop
- `dry_run.py` — preview what bot would do without sending txs
- `exit_position.py` — burn position, return all funds to wallet
- `topup_position.py` — add wallet funds to existing position
- `recover_position.py` — handles broken/partial states
- `verify_permit2.py` — one-time approval setup
- `check_balance.py` — print wallet + position state

## Recommended first run

1. Start with $50-100 capital to learn the bot's behavior
2. Set `DRY_RUN=true` first
3. Run `dry_run.py` and read the output carefully
4. Set `DRY_RUN=false` and run `lp_bot.py` manually
5. Watch the first mint + first compound cycle
6. Once comfortable, run under supervisor for 24/7 operation

## Production setup

See `INSTALL.md` for:
- Generating a dedicated wallet
- Setting up supervisor for auto-restart
- Monitoring + emergency exit procedures

## License

MIT. Use at your own risk. The bot performs real transactions that move real money.
