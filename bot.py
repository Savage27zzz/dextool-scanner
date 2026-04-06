import asyncio
import signal
import sys
from datetime import datetime, timezone

import aiohttp
from telegram.ext import Application, CommandHandler

import db
from config import (
    BUY_PERCENT,
    CHAIN,
    DEXTOOLS_API_KEY,
    EXPLORER_TX,
    MAX_MCAP,
    MIN_LIQUIDITY,
    MIN_MCAP,
    MIN_SCORE,
    MONITOR_INTERVAL,
    NATIVE_SYMBOL,
    SCAN_INTERVAL,
    SLIPPAGE,
    STOP_LOSS,
    TAKE_PROFIT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TRAILING_DROP,
    TRAILING_ENABLED,
    ANTIRUG_ENABLED,
    ANTIRUG_MIN_LIQ,
    ANTIRUG_LIQ_DROP_PCT,
    logger,
)
from monitor import ProfitMonitor, _format_duration
from notifier import Notifier
from honeypot import check_honeypot
from scanner import scan_all_sources
from trader import create_trader
from whale_tracker import WhaleTracker
from config import WHALE_TRACKING_ENABLED, WHALE_CHECK_INTERVAL, WHALE_MIN_SOL

trader = None
monitor: ProfitMonitor | None = None
notifier: Notifier | None = None
scanner_task: asyncio.Task | None = None
monitor_task: asyncio.Task | None = None
whale_tracker: WhaleTracker | None = None
whale_task: asyncio.Task | None = None
is_running: bool = False


def _is_admin(update) -> bool:
    return update.effective_user.id == TELEGRAM_CHAT_ID


async def _is_authorized(update) -> bool:
    if _is_admin(update):
        return True
    return await db.is_user_allowed(update.effective_user.id)


async def _reject_unauthorized(update) -> bool:
    if await _is_authorized(update):
        return False
    uid = update.effective_user.id
    uname = update.effective_user.username or update.effective_user.first_name or ""
    await update.message.reply_html(
        f"🔒 <b>Access Denied</b>\n\n"
        f"Your user ID: <code>{uid}</code>\n"
        f"Ask the bot admin to run:\n"
        f"<code>/adduser {uid}</code>"
    )
    logger.warning("Unauthorized access attempt from user %d (%s)", uid, uname)
    return True


async def scanner_loop():
    global is_running
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    logger.info("Scanner loop started (chain=%s, interval=%ds)", CHAIN, SCAN_INTERVAL)

    while is_running:
        try:
            async with aiohttp.ClientSession() as session:
                tokens = await scan_all_sources(session, CHAIN)

                for token in tokens:
                    try:
                        await db.save_detected_token(token)

                        if CHAIN.upper() == "SOL":
                            buy_amount = await trader.get_buy_amount()
                        else:
                            buy_amount = await trader.get_buy_amount(CHAIN)

                        if buy_amount <= 0:
                            logger.warning("Insufficient balance to buy %s", token["symbol"])
                            continue

                        await notifier.notify_new_token(token, buy_amount, native)

                        if CHAIN.upper() == "SOL":
                            result = await trader.buy_token(token["contract_address"], buy_amount)
                        else:
                            result = await trader.buy_token(token["contract_address"], CHAIN, buy_amount)

                        if result is None:
                            logger.error("Buy failed for %s", token["symbol"])
                            await notifier.notify_error(f"Buy failed for {token['symbol']}")
                            continue

                        position = {
                            "token_address": token["contract_address"],
                            "token_symbol": token["symbol"],
                            "chain": CHAIN.upper(),
                            "entry_price": result["entry_price"],
                            "tokens_received": result["tokens_received"],
                            "buy_amount_native": result["amount_spent"],
                            "buy_tx_hash": result["tx_hash"],
                            "pair_address": token.get("pair_address", ""),
                            "entry_liquidity": token.get("liquidity", 0),
                        }
                        await db.save_open_position(position)

                        await notifier.notify_buy_executed(
                            symbol=token["symbol"],
                            tokens_received=result["tokens_received"],
                            entry_price=result["entry_price"],
                            tx_hash=result["tx_hash"],
                            chain=CHAIN.upper(),
                        )

                    except Exception as exc:
                        logger.error("Error processing token %s: %s", token.get("symbol"), exc)

        except Exception as exc:
            logger.error("Scanner error: %s", exc)

        await asyncio.sleep(SCAN_INTERVAL)


async def cmd_help(update, context):
    is_admin = _is_admin(update)
    is_auth = await _is_authorized(update)

    lines = [
        "🤖 <b>DexTool Scanner Bot</b>\n",
        "Scans DexTools for new low-cap tokens on Solana, auto-buys qualifying tokens, and takes profit automatically.\n",
    ]

    if is_auth:
        lines.append("<b>Commands:</b>")
        lines.append("/help — Show this message")
        lines.append("/status — Open positions with live ROI")
        lines.append("/balance — Wallet balance")
        lines.append("/history — Last 10 completed trades")
        lines.append("/config — Current bot configuration")
        lines.append("/buy &lt;address&gt; [amount] — Manual buy")
        lines.append("/sell &lt;address&gt; [percent] — Manual sell")
        lines.append("/portfolio — Full portfolio overview with PnL")
        if is_admin:
            lines.append("\n<b>Admin only:</b>")
            lines.append("/start — Start scanning and trading")
            lines.append("/stop — Pause scanning and trading")
            lines.append("/adduser &lt;user_id&gt; — Grant access")
            lines.append("/removeuser &lt;user_id&gt; — Revoke access")
            lines.append("/users — List authorized users")
            lines.append("/addwhale &lt;address&gt; [label] — Track a whale wallet")
            lines.append("/removewhale &lt;address&gt; — Stop tracking a whale wallet")
            lines.append("/whales — List tracked whales &amp; recent events")
    else:
        uid = update.effective_user.id
        lines.append(f"Your user ID: <code>{uid}</code>")
        lines.append(f"Ask the admin to run: <code>/adduser {uid}</code>")

    await update.message.reply_html("\n".join(lines))


async def cmd_start(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    global is_running, scanner_task, monitor_task, whale_task

    if is_running:
        await update.message.reply_text("Bot is already running.")
        return

    is_running = True
    scanner_task = asyncio.create_task(scanner_loop())
    monitor_task = asyncio.create_task(monitor.start())
    if whale_tracker:
        whale_task = asyncio.create_task(whale_tracker.start())

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    msg = (
        "🚀 <b>Bot Started</b>\n\n"
        f"Chain: {CHAIN}\n"
        f"Wallet balance: {balance:.4f} {native}\n"
        f"Buy: {BUY_PERCENT}% | TP: {TAKE_PROFIT}% | Slippage: {SLIPPAGE}%\n"
        f"Scan every {SCAN_INTERVAL}s | Monitor every {MONITOR_INTERVAL}s\n"
        f"MCap: ${MIN_MCAP:,}–${MAX_MCAP:,} | Min Liq: ${MIN_LIQUIDITY:,}\n"
        f"Manual: /buy &lt;address&gt; [amount] | /sell &lt;address&gt; [percent]"
    )
    await update.message.reply_html(msg)
    logger.info("Bot started by user %s", update.effective_user.id)


async def cmd_stop(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    global is_running, scanner_task, monitor_task, whale_task

    if not is_running:
        await update.message.reply_text("Bot is not running.")
        return

    is_running = False
    if monitor:
        await monitor.stop()
    if whale_tracker:
        await whale_tracker.stop()
    if scanner_task and not scanner_task.done():
        scanner_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    if whale_task and not whale_task.done():
        whale_task.cancel()

    scanner_task = None
    monitor_task = None
    whale_task = None

    await update.message.reply_html("🛑 <b>Bot Stopped</b>\nScanning and trading paused. Bot still responds to commands.")
    logger.info("Bot stopped by user %s", update.effective_user.id)


async def cmd_status(update, context):
    if await _reject_unauthorized(update):
        return

    positions = await monitor.get_positions_with_roi()

    if not positions:
        await update.message.reply_text("No open positions.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = ["📊 <b>Open Positions</b>\n"]
    for p in positions:
        roi = p.get("roi", 0)
        arrow = "🟢" if roi >= 0 else "🔴"
        lines.append(
            f"{arrow} <b>{p['token_symbol']}</b> | ROI: {roi:+.2f}%\n"
            f"   Entry: {p['entry_price']:.10f} {native}\n"
            f"   Current: {p.get('current_price', 0):.10f} {native}\n"
            f"   Amount: {p['tokens_received']:.4f} | Spent: {p['buy_amount_native']:.4f} {native}\n"
        )
        if p.get("trailing_activated"):
            lines.append(f"   📈 Trailing active | Peak: {p.get('peak_price', 0):.10f} {native}")

    await update.message.reply_html("\n".join(lines))


async def cmd_balance(update, context):
    if await _reject_unauthorized(update):
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    await update.message.reply_html(f"💰 <b>Wallet Balance</b>\n{balance:.6f} {native} ({CHAIN})")


async def cmd_history(update, context):
    if await _reject_unauthorized(update):
        return

    trades = await db.get_trade_history(limit=10)

    if not trades:
        await update.message.reply_text("No completed trades.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = ["📜 <b>Trade History</b> (last 10)\n"]
    for t in trades:
        roi = t.get("roi_percent", 0)
        arrow = "🟢" if roi >= 0 else "🔴"
        dur = _format_duration(t.get("duration_seconds", 0))
        lines.append(
            f"{arrow} <b>{t['token_symbol']}</b> | ROI: {roi:+.2f}%\n"
            f"   Buy: {t['buy_amount_native']:.4f} → Sell: {t['sell_amount_native']:.4f} {native}\n"
            f"   Duration: {dur}\n"
        )

    await update.message.reply_html("\n".join(lines))


async def cmd_config(update, context):
    if await _reject_unauthorized(update):
        return

    msg = (
        "⚙️ <b>Configuration</b>\n\n"
        f"Chain: {CHAIN}\n"
        f"Scanner Mode: {'DexTools + DexScreener' if DEXTOOLS_API_KEY else 'DexScreener only'}\n"
        f"Buy Percent: {BUY_PERCENT}%\n"
        f"Take Profit: {TAKE_PROFIT}%\n"
        f"Stop Loss: {STOP_LOSS}%\n"
        f"Trailing TP: {'Enabled' if TRAILING_ENABLED else 'Disabled'}\n"
        f"Trailing Drop: {TRAILING_DROP}%\n"
        f"Slippage: {SLIPPAGE}%\n"
        f"Min Liquidity: ${MIN_LIQUIDITY:,}\n"
        f"Market Cap Range: ${MIN_MCAP:,} – ${MAX_MCAP:,}\n"
        f"Min Safety Score: {MIN_SCORE}/100\n"
        f"Scan Interval: {SCAN_INTERVAL}s\n"
        f"Monitor Interval: {MONITOR_INTERVAL}s\n"
        f"Whale Tracking: {'Enabled' if WHALE_TRACKING_ENABLED else 'Disabled'}\n"
        f"Whale Check Interval: {WHALE_CHECK_INTERVAL}s\n"
        f"Whale Min SOL: {WHALE_MIN_SOL} SOL\n"
        f"Anti-Rug: {'Enabled' if ANTIRUG_ENABLED else 'Disabled'}\n"
        f"Anti-Rug Min Liquidity: ${ANTIRUG_MIN_LIQ:,}\n"
        f"Anti-Rug Drop Threshold: {ANTIRUG_LIQ_DROP_PCT}%"
    )
    await update.message.reply_html(msg)


async def cmd_buy(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "Usage: <code>/buy &lt;token_address&gt; [amount]</code>\n"
            "Example: <code>/buy So1abc...xyz 0.5</code>\n"
            "If amount is omitted, uses configured BUY_PERCENT% of balance."
        )
        return

    token_address = context.args[0].strip()
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if len(context.args) >= 2:
        try:
            buy_amount = float(context.args[1])
            if buy_amount <= 0:
                await update.message.reply_text("Amount must be positive.")
                return
        except ValueError:
            await update.message.reply_text("Invalid amount. Must be a number.")
            return
    else:
        if CHAIN.upper() == "SOL":
            buy_amount = await trader.get_buy_amount()
        else:
            buy_amount = await trader.get_buy_amount(CHAIN)

    if buy_amount <= 0:
        await update.message.reply_text(f"Insufficient {native} balance.")
        return

    already = await db.is_token_already_bought(token_address, CHAIN.upper())
    if already:
        await update.message.reply_text("Already holding a position in this token.")
        return

    async with aiohttp.ClientSession() as hp_session:
        hp = await check_honeypot(hp_session, CHAIN, token_address)
    if hp["is_honeypot"]:
        await update.message.reply_html(
            "\U0001f6ab <b>Honeypot Detected</b>\n\n"
            f"Token <code>{token_address}</code> flagged as honeypot.\n"
            f"Buy Tax: {hp['buy_tax']:.1f}% | Sell Tax: {hp['sell_tax']:.1f}%\n"
            "Buy cancelled for your safety."
        )
        logger.warning("Manual buy blocked — honeypot: %s", token_address)
        return

    await update.message.reply_html(
        f"\U0001f504 <b>Manual Buy</b>\n"
        f"Token: <code>{token_address}</code>\n"
        f"Amount: {buy_amount:.4f} {native}\n"
        f"Executing..."
    )

    if CHAIN.upper() == "SOL":
        result = await trader.buy_token(token_address, buy_amount)
    else:
        result = await trader.buy_token(token_address, CHAIN, buy_amount)

    if result is None:
        await update.message.reply_html("\u274c <b>Buy failed.</b> Check logs for details.")
        logger.error("Manual buy failed for %s", token_address)
        return

    symbol = token_address[:8]

    entry_liq = 0.0
    try:
        from dexscreener import get_token_liquidity
        async with aiohttp.ClientSession() as liq_session:
            entry_liq = await get_token_liquidity(liq_session, CHAIN, token_address)
    except Exception:
        pass

    position = {
        "token_address": token_address,
        "token_symbol": result.get("symbol", symbol),
        "chain": CHAIN.upper(),
        "entry_price": result["entry_price"],
        "tokens_received": result["tokens_received"],
        "buy_amount_native": result["amount_spent"],
        "buy_tx_hash": result["tx_hash"],
        "pair_address": "",
        "entry_liquidity": entry_liq,
    }
    await db.save_open_position(position)

    await notifier.notify_buy_executed(
        symbol=symbol,
        tokens_received=result["tokens_received"],
        entry_price=result["entry_price"],
        tx_hash=result["tx_hash"],
        chain=CHAIN.upper(),
    )

    logger.info("Manual buy executed: %s, tx=%s", token_address, result["tx_hash"])


async def cmd_sell(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "Usage: <code>/sell &lt;token_address&gt; [percent]</code>\n"
            "Example: <code>/sell So1abc...xyz 50</code> (sell 50%)\n"
            "If percent is omitted, sells 100% of holdings."
        )
        return

    token_address = context.args[0].strip()
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    sell_percent = 100
    if len(context.args) >= 2:
        try:
            sell_percent = float(context.args[1])
            if sell_percent <= 0 or sell_percent > 100:
                await update.message.reply_text("Percent must be between 1 and 100.")
                return
        except ValueError:
            await update.message.reply_text("Invalid percent. Must be a number.")
            return

    if CHAIN.upper() == "SOL":
        ui_balance, decimals = await trader.get_token_balance(token_address)
    else:
        ui_balance, decimals = await trader.get_token_balance(token_address, CHAIN)

    if ui_balance <= 0:
        await update.message.reply_text("No tokens to sell \u2014 zero balance.")
        return

    sell_ui = ui_balance * (sell_percent / 100)
    if decimals > 0:
        sell_raw = int(sell_ui * (10 ** decimals))
    else:
        sell_raw = int(sell_ui * 1e9)

    if sell_raw <= 0:
        await update.message.reply_text("Amount too small to sell.")
        return

    await update.message.reply_html(
        f"\U0001f504 <b>Manual Sell</b>\n"
        f"Token: <code>{token_address}</code>\n"
        f"Selling: {sell_percent}% ({sell_ui:.4f} tokens)\n"
        f"Executing..."
    )

    if CHAIN.upper() == "SOL":
        result = await trader.sell_token(token_address, sell_raw, decimals)
    else:
        result = await trader.sell_token(token_address, CHAIN, sell_raw, decimals)

    if result is None:
        await update.message.reply_html("\u274c <b>Sell failed.</b> Check logs for details.")
        logger.error("Manual sell failed for %s", token_address)
        return

    if sell_percent == 100:
        positions = await db.get_open_positions()
        for pos in positions:
            if pos["token_address"].lower() == token_address.lower() and pos["chain"] == CHAIN.upper():
                entry_price = pos["entry_price"]
                roi = ((result["exit_price"] - entry_price) / entry_price) * 100 if entry_price > 0 else 0

                opened_at = pos.get("opened_at", "")
                duration_seconds = 0
                if opened_at:
                    try:
                        if isinstance(opened_at, str):
                            ot = datetime.fromisoformat(opened_at).replace(tzinfo=timezone.utc)
                        else:
                            ot = opened_at
                        duration_seconds = int((datetime.now(timezone.utc) - ot).total_seconds())
                    except Exception:
                        pass

                exit_data = {
                    "exit_price": result["exit_price"],
                    "sell_amount_native": result["native_received"],
                    "profit_usd": None,
                    "roi_percent": roi,
                    "sell_tx_hash": result["tx_hash"],
                    "duration_seconds": duration_seconds,
                }
                await db.close_position(pos["token_address"], CHAIN.upper(), exit_data)
                break

    tx_url = EXPLORER_TX.get(CHAIN.upper(), EXPLORER_TX["SOL"]).format(result["tx_hash"])
    short_hash = result["tx_hash"][:10] + "\u2026" + result["tx_hash"][-6:] if len(result["tx_hash"]) > 20 else result["tx_hash"]

    await update.message.reply_html(
        f"\u2705 <b>Sell Executed</b>\n"
        f"Token: <code>{token_address[:16]}...</code>\n"
        f"Sold: {sell_percent}% ({sell_ui:.4f} tokens)\n"
        f"Received: {result['native_received']:.6f} {native}\n"
        f'TX: <a href="{tx_url}">{short_hash}</a>'
    )

    logger.info("Manual sell executed: %s (%d%%), tx=%s", token_address, sell_percent, result["tx_hash"])


async def cmd_portfolio(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if CHAIN.upper() == "SOL":
        wallet_balance = await trader.get_balance()
    else:
        wallet_balance = await trader.get_balance(CHAIN)

    positions = await monitor.get_positions_with_roi()

    total_invested = 0.0
    total_current_value = 0.0

    position_lines = []
    for p in positions:
        invested = p.get("buy_amount_native", 0)
        tokens = p.get("tokens_received", 0)
        entry = p.get("entry_price", 0)
        current = p.get("current_price", 0)
        roi = p.get("roi", 0)
        symbol = p.get("token_symbol", "???")

        total_invested += invested

        if current > 0:
            current_value = current * tokens
            price_ok = True
        else:
            current_value = invested
            price_ok = False

        total_current_value += current_value

        pnl = current_value - invested
        pnl_sign = "+" if pnl >= 0 else ""

        if not price_ok:
            arrow = "\u26a0\ufe0f"
        elif roi >= 0:
            arrow = "\U0001f7e2"
        else:
            arrow = "\U0001f534"

        line = (
            f"{arrow} <b>{symbol}</b>\n"
            f"   Invested: {invested:.4f} {native}\n"
            f"   Value: {current_value:.4f} {native} ({pnl_sign}{pnl:.4f})\n"
            f"   ROI: {roi:+.2f}%"
        )
        if not price_ok:
            line += " \u26a0\ufe0f price unavailable"
        position_lines.append(line)

    trades = await db.get_trade_history(limit=100)
    realized_pnl = 0.0
    total_trades = len(trades)
    winning_trades = 0
    for t in trades:
        buy_native = t.get("buy_amount_native", 0)
        sell_native = t.get("sell_amount_native", 0)
        realized_pnl += (sell_native - buy_native)
        if t.get("roi_percent", 0) > 0:
            winning_trades += 1

    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    unrealized_pnl = total_current_value - total_invested
    overall_pnl = unrealized_pnl + realized_pnl
    overall_sign = "+" if overall_pnl >= 0 else ""
    unrealized_sign = "+" if unrealized_pnl >= 0 else ""
    realized_sign = "+" if realized_pnl >= 0 else ""

    total_portfolio = wallet_balance + total_current_value

    msg_parts = [
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "\U0001f4bc <b>PORTFOLIO OVERVIEW</b>",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        f"",
        f"\U0001f4b0 Wallet: {wallet_balance:.4f} {native}",
        f"\U0001f4e6 In Positions: {total_current_value:.4f} {native}",
        f"\U0001f4ca Total Value: {total_portfolio:.4f} {native}",
        f"",
        f"<b>PnL Summary</b>",
        f"   Unrealized: {unrealized_sign}{unrealized_pnl:.4f} {native}",
        f"   Realized: {realized_sign}{realized_pnl:.4f} {native}",
        f"   Overall: {overall_sign}{overall_pnl:.4f} {native}",
        f"",
        f"<b>Stats</b>",
        f"   Open Positions: {len(positions)}",
        f"   Completed Trades: {total_trades}",
        f"   Win Rate: {win_rate:.1f}%",
    ]

    if position_lines:
        msg_parts.append("")
        msg_parts.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
        msg_parts.append("\U0001f4cb <b>OPEN POSITIONS</b>")
        msg_parts.append("\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501")
        for line in position_lines:
            msg_parts.append(line)
    else:
        msg_parts.append("")
        msg_parts.append("No open positions.")

    await update.message.reply_html("\n".join(msg_parts))


async def cmd_adduser(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_html("Usage: <code>/adduser &lt;user_id&gt;</code>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID. Must be a number.")
        return

    username = context.args[1] if len(context.args) > 1 else ""
    await db.add_allowed_user(user_id, username)
    await update.message.reply_html(f"✅ User <code>{user_id}</code> has been granted access.")


async def cmd_removeuser(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_html("Usage: <code>/removeuser &lt;user_id&gt;</code>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Invalid user ID. Must be a number.")
        return

    removed = await db.remove_allowed_user(user_id)
    if removed:
        await update.message.reply_html(f"🚫 User <code>{user_id}</code> access revoked.")
    else:
        await update.message.reply_html(f"User <code>{user_id}</code> was not in the list.")


async def cmd_users(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    users = await db.get_allowed_users()
    if not users:
        await update.message.reply_text("No authorized users (besides admin).")
        return

    lines = ["👥 <b>Authorized Users</b>\n"]
    for u in users:
        name = u.get("username") or "—"
        lines.append(f"• <code>{u['user_id']}</code> ({name}) — added {u.get('added_at', '?')}")

    await update.message.reply_html("\n".join(lines))


async def cmd_addwhale(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_html("Usage: <code>/addwhale &lt;address&gt; [label]</code>")
        return

    address = context.args[0].strip()

    try:
        import base58 as b58
        decoded = b58.b58decode(address)
        if len(decoded) != 32:
            raise ValueError("not 32 bytes")
    except Exception:
        await update.message.reply_html(
            f"❌ Invalid Solana address.\n<code>{address}</code> is not a valid base58-encoded 32-byte pubkey."
        )
        return

    label = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    added = await db.add_whale_wallet(address, label)
    if added:
        short = address[:6] + "…" + address[-4:]
        lbl = f" ({label})" if label else ""
        await update.message.reply_html(f"🐋 Whale wallet added: <code>{short}</code>{lbl}")
    else:
        await update.message.reply_html("Wallet already tracked.")


async def cmd_removewhale(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_html("Usage: <code>/removewhale &lt;address&gt;</code>")
        return

    address = context.args[0].strip()
    removed = await db.remove_whale_wallet(address)
    if removed:
        short = address[:6] + "…" + address[-4:]
        await update.message.reply_html(f"🗑 Whale wallet removed: <code>{short}</code>")
    else:
        await update.message.reply_html("Wallet not found in tracking list.")


async def cmd_whales(update, context):
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    wallets = await db.get_whale_wallets()
    events = await db.get_whale_events(limit=5)

    lines = ["🐋 <b>Tracked Whale Wallets</b>\n"]
    if wallets:
        for w in wallets:
            addr = w["address"]
            short = addr[:6] + "…" + addr[-4:]
            lbl = f" ({w['label']})" if w.get("label") else ""
            lines.append(f"• <code>{short}</code>{lbl} — added {w.get('added_at', '?')}")
    else:
        lines.append("No wallets tracked. Use /addwhale to add one.")

    lines.append("\n<b>Recent Whale Events</b>\n")
    if events:
        for e in events:
            short_wallet = e["wallet_address"][:6] + "…" + e["wallet_address"][-4:]
            lines.append(
                f"• {short_wallet} bought <b>{e.get('token_symbol', '?')}</b> — "
                f"{e['sol_spent']:.4f} SOL — {e.get('detected_at', '?')}"
            )
    else:
        lines.append("No whale events recorded yet.")

    await update.message.reply_html("\n".join(lines))


async def post_init(application):
    global trader, monitor, notifier, whale_tracker

    await db.init_db()

    trader = create_trader(CHAIN)
    notifier = Notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    monitor = ProfitMonitor(trader, notifier)

    if CHAIN.upper() == "SOL" and WHALE_TRACKING_ENABLED:
        whale_tracker = WhaleTracker(trader.client, notifier)

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    logger.info("Bot initialised – chain=%s, balance=%.6f %s", CHAIN, balance, native)
    scanner_mode = "DexTools + DexScreener" if DEXTOOLS_API_KEY else "DexScreener only (free)"
    await notifier.send_message(
        f"🤖 <b>DexTool Scanner Online</b>\n"
        f"Chain: {CHAIN} | Balance: {balance:.4f} {native}\n"
        f"Scanner: {scanner_mode}\n"
        f"Send /start to begin scanning."
    )


async def shutdown(application):
    global is_running
    is_running = False
    if whale_tracker:
        await whale_tracker.stop()
    if monitor:
        await monitor.stop()
    if trader:
        await trader.close()
    logger.info("Shutdown complete")


def main():
    logger.info("Starting DexTool Scanner Bot …")

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(shutdown)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("addwhale", cmd_addwhale))
    app.add_handler(CommandHandler("removewhale", cmd_removewhale))
    app.add_handler(CommandHandler("whales", cmd_whales))

    def _handle_signal(signum, frame):
        logger.info("Received signal %s – shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Polling for Telegram updates …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
