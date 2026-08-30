# VPS Deploy Guide

These steps assume Ubuntu 22.04 or 24.04.

## 1. Install Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

## 2. Install Bot Dependencies

```bash
cd/peprl
python3 -m venv .venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
nano .env
exit
```

Fill these values in `.env`:

```env
PERPL_API_KEY=
PERPL_API_KEY_SECRET=
DRY_RUN=false (false to run on mainnet)
```

The bot auto-discovers your exchange account ID after API key sign-in. `PERPL_ACCOUNT_ID` is optional — set it only if your wallet has multiple accounts.

To list account IDs manually:

```bash
python tools/get_account_id.py
```

Keep `DRY_RUN=true` first.

## 3. Test in Dry Run

```bash
cd/peprl
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
nano .env
```

You should see:

```text
loaded BTC market=1 mode=dry-run
market-data connected
market subscriptions: ...
[dry-run] place bid ...
[dry-run] place ask ...
```

Stop it with `Ctrl+C`.

## 4. Go Live

Only after dry-run logs look correct:

Set:

```env
DRY_RUN=false
QUOTE_SIZE=0.001
MAX_OPEN_POSITION=0.003
ALLOW_EMERGENCY_MARKET_CLOSE=false
```

Then:
```bash
cd/peprl
source venv/bin/activate
python main.py
```
## 5. Most Important :

This Bot Still Have Few Bugs So You Can Use Codex or Cursor To Solve It 
- This bot places order on the top of both side of orderbook 
- This bot closes the position instantly whenever it opens up position as taker
- This is Maker - Taker Bot (keep this in mind)

## Recommended VPS

Start simple:

- Ubuntu 22.04/24.04
- 2 vCPU
- 2 GB RAM
- Low-latency region near Perpl's infrastructure if you can test it
- No shared API keys with withdrawal permission

## Safety Checklist

- Keep API key trade-scoped only.
- Start with very small `QUOTE_SIZE`.
- Keep `ALLOW_EMERGENCY_MARKET_CLOSE=false` until you understand failed-close behavior.
- Watch logs during the first live session.
- Stop the bot if it logs repeated command rejects or depth-check failures.
