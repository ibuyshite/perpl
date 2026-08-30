# VPS Deploy Guide

These steps assume Ubuntu 22.04 or 24.04.

## 1. Install Python

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

## 4. Install Bot Dependencies

```bash
sudo -u peprl bash
cd /opt/peprl-mm-bot
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.mainnet.example .env
nano .env
chmod 600 .env
exit
```

Fill these values in `.env`:

```env
PERPL_API_KEY=
PERPL_API_KEY_SECRET=
DRY_RUN=true
```

The bot auto-discovers your exchange account ID after API key sign-in. `PERPL_ACCOUNT_ID` is optional — set it only if your wallet has multiple accounts.

To list account IDs manually:

```bash
python tools/get_account_id.py
```

Keep `DRY_RUN=true` first.

## 5. Test in Dry Run

```bash
sudo -u peprl bash
cd /opt/peprl-mm-bot
. .venv/bin/activate
python -u main.py
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

## 6. Install as a Service

```bash
sudo cp /opt/peprl-mm-bot/deploy/perpl-mm-bot.service /etc/systemd/system/perpl-mm-bot.service
sudo systemctl daemon-reload
sudo systemctl enable perpl-mm-bot
sudo systemctl start perpl-mm-bot
```

Watch logs:

```bash
sudo journalctl -u perpl-mm-bot -f
```

Stop/restart:

```bash
sudo systemctl stop perpl-mm-bot
sudo systemctl restart perpl-mm-bot
```

## 7. Go Live

Only after dry-run logs look correct:

```bash
sudo systemctl stop perpl-mm-bot
sudo -u peprl nano /opt/peprl-mm-bot/.env
```

Set:

```env
DRY_RUN=false
QUOTE_SIZE=0.001
MAX_OPEN_POSITION=0.003
ALLOW_EMERGENCY_MARKET_CLOSE=false
```

Then:

```bash
sudo systemctl start perpl-mm-bot
sudo journalctl -u perpl-mm-bot -f
```

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
