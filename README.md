# Polymarket Wallet Copy-Trading Bot

Monitors target wallets on Polymarket and mirrors their trades automatically.

## Setup

1. **Python 3.11+** required.

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment:
   ```bash
   cp .env.example .env
   # Edit .env and fill in all values
   ```

4. Add wallets to copy in `wallets.json`:
   ```json
   ["0xWALLET_ADDRESS_1", "0xWALLET_ADDRESS_2"]
   ```

5. Run in dry-run mode first (default):
   ```bash
   python main.py
   ```

6. When ready to go live, set `DRY_RUN=false` in `.env`.

## Environment Variables

See `.env.example` for all required variables and their descriptions.

## Safety

- Always test with `DRY_RUN=true` before going live.
- `MAX_POSITION_SIZE_USDC` and `MAX_PORTFOLIO_EXPOSURE_USDC` act as hard caps.
- All trades and signals are logged to `bot.db` for review.
