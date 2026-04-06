import asyncio
import signal
import sys

import aiohttp
from telegram.ext import Application, CommandHandler

import db
from config import (
    BUY_PERCENT,
    CHAIN,
    MAX_MCAP,
    MIN_LIQUIDITY,
    MIN_MCAP,
    MONITOR_INTERVAL,
    NATIVE_SYMBOL,
    SCAN_INTERVAL,
    SLIPPAGE,
    TAKE_PROFIT,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    logger,
)
from monitor import ProfitMonitor, _format_duration
from notifier import Notifier
from scanner import scan_for_new_tokens
from trader import create_trader

trader = None
monitor: ProfitMonitor | None = None
notifier: Notifier | None = None
scanner_task: asyncio.Task | None = None
monitor_task: asyncio.Task | None = None
is_running: bool = False


async def scanner_loop():
    global is_running
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    logger.info("Scanner loop started (chain=%s, interval=%ds)", CHAIN, SCAN_INTERVAL)

    while is_running:
        try:
            async with aiohttp.ClientSession() as session:
                tokens = await scan_for_new_tokens(session, CHAIN)

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


async def cmd_start(update, context):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return

    global is_running, scanner_task, monitor_task

    if is_running:
        await update.message.reply_text("Bot is already running.")
        return

    is_running = True
    scanner_task = asyncio.create_task(scanner_loop())
    monitor_task = asyncio.create_task(monitor.start())

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
        f"MCap: ${MIN_MCAP:,}–${MAX_MCAP:,} | Min Liq: ${MIN_LIQUIDITY:,}"
    )
    await update.message.reply_html(msg)
    logger.info("Bot started by user %s", update.effective_user.id)


async def cmd_stop(update, context):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return

    global is_running, scanner_task, monitor_task

    if not is_running:
        await update.message.reply_text("Bot is not running.")
        return

    is_running = False
    if monitor:
        await monitor.stop()
    if scanner_task and not scanner_task.done():
        scanner_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()

    scanner_task = None
    monitor_task = None

    await update.message.reply_html("🛑 <b>Bot Stopped</b>\nScanning and trading paused. Bot still responds to commands.")
    logger.info("Bot stopped by user %s", update.effective_user.id)


async def cmd_status(update, context):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
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

    await update.message.reply_html("\n".join(lines))


async def cmd_balance(update, context):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    await update.message.reply_html(f"💰 <b>Wallet Balance</b>\n{balance:.6f} {native} ({CHAIN})")


async def cmd_history(update, context):
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
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
    if update.effective_chat.id != TELEGRAM_CHAT_ID:
        await update.message.reply_text("Unauthorized.")
        return

    msg = (
        "⚙️ <b>Configuration</b>\n\n"
        f"Chain: {CHAIN}\n"
        f"Buy Percent: {BUY_PERCENT}%\n"
        f"Take Profit: {TAKE_PROFIT}%\n"
        f"Slippage: {SLIPPAGE}%\n"
        f"Min Liquidity: ${MIN_LIQUIDITY:,}\n"
        f"Market Cap Range: ${MIN_MCAP:,} – ${MAX_MCAP:,}\n"
        f"Scan Interval: {SCAN_INTERVAL}s\n"
        f"Monitor Interval: {MONITOR_INTERVAL}s"
    )
    await update.message.reply_html(msg)


async def post_init(application):
    global trader, monitor, notifier

    await db.init_db()

    trader = create_trader(CHAIN)
    notifier = Notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    monitor = ProfitMonitor(trader, notifier)

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    logger.info("Bot initialised – chain=%s, balance=%.6f %s", CHAIN, balance, native)
    await notifier.send_message(
        f"🤖 <b>DexTool Scanner Online</b>\n"
        f"Chain: {CHAIN} | Balance: {balance:.4f} {native}\n"
        f"Send /start to begin scanning."
    )


async def shutdown(application):
    global is_running
    is_running = False
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

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("config", cmd_config))

    def _handle_signal(signum, frame):
        logger.info("Received signal %s – shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Polling for Telegram updates …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
