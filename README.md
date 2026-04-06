# DexTool Scanner — Solana Trading Bot

Automated Telegram trading bot that scans DexTools for newly launched low-cap tokens on **Solana** (with optional ETH/BSC support), auto-buys qualifying tokens via Jupiter aggregator, monitors open positions, and takes profit at a configurable ROI target.

## Features

- **Multi-chain support** — Solana (primary), Ethereum, BSC
- **DexTools API v2 integration** — scans hot pools and new token listings
- **Jupiter V6 swaps** — best-route execution on Solana via Jupiter aggregator
- **Uniswap V2 / PancakeSwap V2** — DEX routing for EVM chains
- **Configurable filters** — market cap range, minimum liquidity, honeypot detection
- **Auto take-profit** — monitors positions and sells at target ROI
- **Telegram interface** — real-time notifications and bot commands
- **SQLite persistence** — tracks detected tokens, open positions, and completed trades
- **Rotating log files** — `trading.log` with automatic rotation (5 MB × 3 backups)
- **Graceful shutdown** — handles SIGINT/SIGTERM cleanly

## Prerequisites

- Python 3.11+
- A funded Solana wallet (or ETH/BSC wallet if using EVM chains)
- Telegram bot token (from [@BotFather](https://t.me/BotFather))
- DexTools API key ([developer.dextools.io](https://developer.dextools.io))
- Solana RPC endpoint (public, or Helius/QuickNode for better reliability)

## Installation

```bash
git clone https://github.com/Savage27zz/dextool-scanner.git
cd dextool-scanner
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your keys and configuration
```

## Configuration

Edit `.env` with your values:

| Variable | Description | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token from BotFather | *required* |
| `TELEGRAM_CHAT_ID` | Your Telegram chat/group ID | *required* |
| `PRIVATE_KEY` | Base58 Solana private key (or JSON byte array) | *required* |
| `RPC_URL_SOL` | Solana RPC endpoint | `https://api.mainnet-beta.solana.com` |
| `RPC_URL_ETH` | Ethereum RPC (optional) | — |
| `RPC_URL_BSC` | BSC RPC (optional) | — |
| `DEXTOOLS_API_KEY` | DexTools API key | *required* |
| `DEXTOOLS_PLAN` | DexTools plan tier (`trial`, `standard`, etc.) | `trial` |
| `CHAIN` | Active chain: `SOL`, `ETH`, or `BSC` | `SOL` |
| `BUY_PERCENT` | % of wallet balance to use per trade | `50` |
| `TAKE_PROFIT` | ROI % target to trigger sell | `20` |
| `SLIPPAGE` | Slippage tolerance % | `15` |
| `MIN_LIQUIDITY` | Minimum pool liquidity in USD | `5000` |
| `MIN_MCAP` | Minimum market cap in USD | `10000` |
| `MAX_MCAP` | Maximum market cap in USD | `500000` |
| `SCAN_INTERVAL` | Seconds between DexTools scans | `60` |
| `MONITOR_INTERVAL` | Seconds between position checks | `30` |
| `MIN_SCORE` | Minimum safety score (0-100) for auto-buy | `40` |

## Usage

```bash
python bot.py
```

The bot will connect to Telegram and send a startup message. Use commands to control it.

## Bot Commands

**Anyone** (including unapproved users):
| Command | Description |
|---|---|
| `/help` | Show bot info and available commands |

**Authorized users** (admin + approved friends):
| Command | Description |
|---|---|
| `/status` | Show open positions with live ROI |
| `/balance` | Show current wallet balance |
| `/history` | Show last 10 completed trades |
| `/config` | Display current configuration |

**Admin only** (`TELEGRAM_CHAT_ID` owner):
| Command | Description |
|---|---|
| `/start` | Start scanning and auto-trading |
| `/stop` | Stop scanning (bot stays responsive) |
| `/buy <address> [amount]` | Manually buy a token. Amount in SOL/ETH/BNB (default: configured %) |
| `/sell <address> [percent]` | Manually sell a token. Percent 1-100 (default: 100%) |
| `/portfolio` | Full portfolio overview: wallet balance, positions value, PnL breakdown, win rate |
| `/adduser <id>` | Grant a friend read-only access |
| `/removeuser <id>` | Revoke a user's access |
| `/users` | List all authorized users |

### Sharing with friends

Your friends can message the bot directly on Telegram — no GitHub or setup needed. When they send any command, the bot shows them their user ID. You then run `/adduser <their_id>` to grant them read-only access to positions, balance, history, and config.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Telegram    │◄───►│    bot.py     │────►│  notifier.py │
│   (User)      │     │  (commands)   │     │  (messages)  │
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                   ┌────────┴────────┐
                   ▼                 ▼
            ┌────────────┐   ┌─────────────┐
            │ scanner.py  │   │ monitor.py   │
            │ (DexTools   │   │ (profit      │
            │  API scan)  │   │  tracking)   │
            └──────┬─────┘   └──────┬──────┘
                   │                 │
                   ▼                 ▼
            ┌────────────────────────────┐
            │        trader.py            │
            │  ┌──────────┐ ┌──────────┐ │
            │  │ Solana    │ │  EVM     │ │
            │  │ Trader    │ │  Trader  │ │
            │  │ (Jupiter) │ │ (Uni/PS) │ │
            │  └──────────┘ └──────────┘ │
            └──────────────┬─────────────┘
                           │
                    ┌──────┴──────┐
                    │    db.py     │
                    │  (SQLite)    │
                    └─────────────┘

config.py ─── loaded by all modules (env vars + logger)
```

**Scan → Filter → Buy → Monitor → Sell** loop:

1. `scanner.py` queries DexTools API every 60s for hot pools and new tokens
2. Filters by market cap, liquidity, age, and honeypot status
3. `trader.py` executes a buy (Jupiter swap on Solana, or Uniswap/PancakeSwap on EVM)
4. `monitor.py` checks positions every 30s using Jupiter Price API (or on-chain quotes for EVM)
5. When ROI hits the take-profit target, executes a sell and logs the trade

## File Overview

| File | Purpose |
|---|---|
| `config.py` | Loads `.env`, validates config, sets up rotating logger |
| `db.py` | SQLite async layer — detected tokens, positions, trade history |
| `scanner.py` | DexTools API v2 — scan, enrich, filter new tokens |
| `trader.py` | `SolanaTrader` (Jupiter V6) + `EVMTrader` (web3.py) |
| `monitor.py` | Background profit-monitoring loop |
| `notifier.py` | Telegram message formatting and sending |
| `bot.py` | Entry point — Telegram bot commands, scanner/monitor orchestration |

## Disclaimer

**This software is for educational purposes only.** Trading cryptocurrencies involves substantial risk of loss. This bot trades real funds automatically. Use at your own risk. The authors are not responsible for any financial losses. Never trade with funds you cannot afford to lose. Always test with small amounts first.
