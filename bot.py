import asyncio
import signal
import sys
import datetime as dt
from datetime import datetime, timezone

import aiohttp
import base58 as b58
from mnemonic import Mnemonic
from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solders.system_program import TransferParams, transfer
from solders.transaction import Transaction
from solders.message import Message
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler

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
    PRIVATE_KEY,
    RPC_URL_SOL,
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
    OPERATOR_FEE_PCT,
    OPERATOR_FEE_ENABLED,
    MAX_OPEN_POSITIONS,
    MAX_DAILY_LOSS,
    MAX_BUY_AMOUNT,
    SELL_TIERS_RAW,
    API_ENABLED,
    API_PORT,
    logger,
)
from crypto_utils import encrypt_key, decrypt_key
from fee_collector import collect_fee
from monitor import ProfitMonitor, _format_duration
from notifier import Notifier
from honeypot import check_honeypot
from scanner import scan_all_sources, fetch_token_research
from trader import create_trader, create_user_trader, _load_solana_keypair, _get_shared_client
from whale_tracker import WhaleTracker
from config import WHALE_TRACKING_ENABLED, WHALE_CHECK_INTERVAL, WHALE_MIN_SOL, WHALE_COPY_ENABLED, WHALE_COPY_AMOUNT
from api import start_api_server, stop_api_server
from config import SNIPER_ENABLED, SNIPER_CHECK_INTERVAL, SNIPER_MIN_LIQUIDITY
from sniper import Sniper

trader = None
monitor: ProfitMonitor | None = None
notifier: Notifier | None = None
scanner_task: asyncio.Task | None = None
monitor_task: asyncio.Task | None = None
whale_tracker: WhaleTracker | None = None
whale_task: asyncio.Task | None = None
daily_report_task: asyncio.Task | None = None
is_running: bool = False
alerts_enabled: bool = False
_pending_buys: dict[str, str] = {}
api_runner = None
sniper: Sniper | None = None
sniper_task: asyncio.Task | None = None
dca_task: asyncio.Task | None = None
limit_order_task: asyncio.Task | None = None


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


async def _register_chat(update):
    chat = update.effective_chat
    if chat:
        await db.upsert_bot_chat(chat.id, chat.type, chat.title or "")


async def _get_user_trader(update):
    user_id = update.effective_user.id
    ut = await create_user_trader(user_id)
    if ut is None:
        await update.message.reply_text("No wallet found. Ask admin to run /adduser.")
        return None
    return ut


def _generate_solana_wallet() -> tuple[Keypair, str]:
    mnemo = Mnemonic("english")
    seed_phrase = mnemo.generate(strength=128)
    seed_bytes = Bip39SeedGenerator(seed_phrase).Generate()
    bip44_ctx = Bip44.FromSeed(seed_bytes, Bip44Coins.SOLANA)
    derived = bip44_ctx.Purpose().Coin().Account(0).Change(Bip44Changes.CHAIN_EXT)
    privkey_bytes = derived.PrivateKey().Raw().ToBytes()
    pubkey_bytes = derived.PublicKey().RawCompressed().ToBytes()[1:]
    kp = Keypair.from_bytes(privkey_bytes + pubkey_bytes)
    return kp, seed_phrase


async def scanner_loop():
    global is_running
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    logger.info("Scanner loop started (chain=%s, interval=%ds)", CHAIN, SCAN_INTERVAL)

    _daily_loss_notified: set[int] = set()

    while is_running:
        try:
            _daily_loss_notified.clear()
            async with aiohttp.ClientSession() as session:
                tokens = await scan_all_sources(session, CHAIN)

                for token in tokens:
                    try:
                        contract = token.get("contract_address", "")

                        if await db.is_blacklisted(contract, CHAIN.upper()):
                            logger.debug("Skipping blacklisted token %s", token.get("symbol"))
                            continue

                        await db.save_detected_token(token)

                        if alerts_enabled:
                            alert_msg = (
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                "🔍 <b>NEW LOWCAP DETECTED</b>\n"
                                "━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🪙 {token.get('name', '?')} ({token.get('symbol', '?')})\n"
                                f"📄 <code>{token.get('contract_address', '')}</code>\n"
                                f"⛓ {token.get('chain', '')}\n"
                                f"💰 MCap: ${token.get('market_cap', 0):,.0f}\n"
                                f"💧 Liq: ${token.get('liquidity', 0):,.0f}\n"
                                "━━━━━━━━━━━━━━━━━━━━━━"
                            )
                            tp = token.get("contract_address", "")[:16]
                            alert_markup = InlineKeyboardMarkup([
                                [
                                    InlineKeyboardButton("🛒 Quick Buy 0.1 SOL", callback_data=f"quickbuy:{tp}:0.1"),
                                    InlineKeyboardButton("🛒 Quick Buy 0.5 SOL", callback_data=f"quickbuy:{tp}:0.5"),
                                ]
                            ])
                            await notifier.broadcast_alert(alert_msg, reply_markup=alert_markup)

                        trading_users = await db.get_all_trading_users()
                        if not trading_users:
                            continue

                        for user_wallet in trading_users:
                            uid = user_wallet["user_id"]
                            try:
                                already = await db.is_token_already_bought(
                                    token["contract_address"], CHAIN.upper(), uid
                                )
                                if already:
                                    continue

                                # --- Risk Management Checks ---
                                # 1. Max open positions
                                open_count = await db.count_open_positions(uid)
                                user_cfg = await db.get_effective_config(uid)
                                if open_count >= user_cfg["max_positions"]:
                                    logger.debug("User %d at max positions (%d/%d) — skipping %s",
                                                 uid, open_count, user_cfg["max_positions"], token.get("symbol"))
                                    continue

                                # 2. Daily loss limit
                                if MAX_DAILY_LOSS > 0:
                                    daily_loss = await db.get_daily_realized_loss(uid)
                                    if daily_loss >= MAX_DAILY_LOSS:
                                        logger.info("User %d hit daily loss limit (%.4f/%.4f) — skipping",
                                                     uid, daily_loss, MAX_DAILY_LOSS)
                                        if uid not in _daily_loss_notified:
                                            _daily_loss_notified.add(uid)
                                            await notifier.notify_daily_loss_limit(uid, daily_loss, MAX_DAILY_LOSS, native)
                                        continue

                                user_trader = await create_user_trader(uid)
                                if user_trader is None:
                                    continue

                                buy_amount = await user_trader.get_buy_amount()
                                if buy_amount <= 0:
                                    continue

                                # 3. Max buy amount cap
                                if user_cfg["max_buy_amount"] > 0 and buy_amount > user_cfg["max_buy_amount"]:
                                    buy_amount = user_cfg["max_buy_amount"]
                                    logger.debug("Capped buy for user %d to %.4f", uid, user_cfg["max_buy_amount"])

                                # 4. Add compound funds if available
                                compound_funds = await db.get_compound_fund(uid)
                                if compound_funds > 0.01:
                                    buy_amount += compound_funds
                                    await db.deduct_compound_funds(uid, compound_funds)
                                    logger.info("User %d compound buy: +%.4f %s from fund", uid, compound_funds, native)

                                await notifier.send_to_user(
                                    uid,
                                    f"⚙️ Buying {token.get('symbol', '?')} with {buy_amount:.4f} {native}..."
                                )

                                result = await user_trader.buy_token(
                                    token["contract_address"], buy_amount
                                )
                                if result is None:
                                    await notifier.send_to_user(uid, f"❌ Buy failed for {token.get('symbol', '?')}")
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
                                    "user_id": uid,
                                }
                                await db.save_open_position(position)

                                await notifier.notify_buy_executed(
                                    symbol=token["symbol"],
                                    tokens_received=result["tokens_received"],
                                    entry_price=result["entry_price"],
                                    tx_hash=result["tx_hash"],
                                    chain=CHAIN.upper(),
                                    chat_id=uid,
                                )

                                await notifier.send_message(
                                    f"📋 User <code>{uid}</code> bought {token['symbol']} — "
                                    f"{result['amount_spent']:.4f} {native}",
                                )

                            except Exception as exc:
                                logger.error("Error buying %s for user %d: %s",
                                           token.get("symbol"), uid, exc)

                    except Exception as exc:
                        logger.error("Error processing token %s: %s", token.get("symbol"), exc)

                whitelist = await db.get_whitelist()
                for wl_item in whitelist:
                    wl_addr = wl_item["token_address"]
                    trading_users = await db.get_all_trading_users()
                    for user_wallet in trading_users:
                        uid = user_wallet["user_id"]
                        try:
                            already = await db.is_token_already_bought(wl_addr, CHAIN.upper(), uid)
                            if already:
                                continue
                            user_cfg = await db.get_effective_config(uid)
                            open_count = await db.count_open_positions(uid)
                            if open_count >= user_cfg["max_positions"]:
                                continue
                            user_trader = await create_user_trader(uid)
                            if user_trader is None:
                                continue
                            buy_amount = await user_trader.get_buy_amount()
                            if buy_amount <= 0:
                                continue
                            if user_cfg["max_buy_amount"] > 0 and buy_amount > user_cfg["max_buy_amount"]:
                                buy_amount = user_cfg["max_buy_amount"]

                            logger.info("Whitelist auto-buy: %s for user %d", wl_addr, uid)
                            result = await user_trader.buy_token(wl_addr, buy_amount)
                            if result:
                                position = {
                                    "token_address": wl_addr,
                                    "token_symbol": wl_item.get("label", wl_addr[:8]),
                                    "chain": CHAIN.upper(),
                                    "entry_price": result["entry_price"],
                                    "tokens_received": result["tokens_received"],
                                    "buy_amount_native": result["amount_spent"],
                                    "buy_tx_hash": result["tx_hash"],
                                    "pair_address": "",
                                    "entry_liquidity": 0,
                                    "user_id": uid,
                                }
                                await db.save_open_position(position)
                                await notifier.send_to_user(uid, f"\u2705 Whitelist buy: {wl_item.get('label', wl_addr[:8])} \u2014 {result['amount_spent']:.4f} {native}")
                        except Exception as exc:
                            logger.error("Whitelist buy error for %s user %d: %s", wl_addr, uid, exc)

        except Exception as exc:
            logger.error("Scanner error: %s", exc)

        await asyncio.sleep(SCAN_INTERVAL)


async def cmd_help(update, context):
    await _register_chat(update)
    is_admin = _is_admin(update)
    is_auth = await _is_authorized(update)

    lines = [
        "🤖 <b>DexTool Scanner Bot</b>\n",
        "Scans for new low-cap tokens, auto-buys for each user's personal wallet, and takes profit automatically.\n",
    ]

    if is_auth:
        lines.append("<b>Commands:</b>")
        lines.append("/help — Show this message")
        lines.append("/wallet — Your wallet address & balance")
        lines.append("/status — Open positions with live ROI")
        lines.append("/balance — Wallet balance")
        lines.append("/history — Last 10 completed trades")
        lines.append("/portfolio — Full portfolio overview with PnL")
        lines.append("/buy &lt;address&gt; [amount] — Manual buy")
        lines.append("/sell &lt;address&gt; [percent] — Manual sell")
        lines.append("/info &lt;address&gt; — Token research &amp; safety report")
        lines.append("/snipe &lt;address&gt; [amount] — Snipe a token on pool creation")
        lines.append("/autotrade on|off — Toggle auto-trading")
        lines.append("/withdraw &lt;amount&gt; &lt;address&gt; — Withdraw SOL")
        lines.append("/export — Export wallet credentials (DM only)")
        lines.append("/sellall — Panic sell all open positions")
        lines.append("/stats — Detailed trading performance analytics")
        lines.append("/pnl [days] — P&amp;L summary (default: today)")
        lines.append("/lowcaps [count] — Show recent detected tokens")
        lines.append("")
        lines.append("<b>⚙️ Settings:</b>")
        lines.append("/mysettings — View/edit your personal trading settings")
        lines.append("/compound — Auto-compound settings &amp; fund")
        lines.append("/config — Current global bot configuration")
        if is_admin:
            lines.append("\n<b>Admin only:</b>")
            lines.append("/start — Start scanning and trading")
            lines.append("/stop — Pause scanning and trading")
            lines.append("/adduser &lt;user_id&gt; — Grant access + generate wallet")
            lines.append("/removeuser &lt;user_id&gt; — Revoke access + delete wallet")
            lines.append("/users — List all users with wallet info")
            lines.append("/chats — List active bot chats")
            lines.append("/status all — All users' positions")
            lines.append("/portfolio all — All users' portfolio")
            lines.append("/addwhale &lt;address&gt; [label] — Track a whale wallet")
            lines.append("/removewhale &lt;address&gt; — Stop tracking a whale wallet")
            lines.append("/whales — List tracked whales &amp; recent events")
            lines.append("/copytrade — Whale copy trading status")
            lines.append("/fees — Fee revenue stats")
            lines.append("/stats all — All users' combined stats")
            lines.append("/alerts on|off — Toggle lowcap alert broadcasting")
            lines.append("/backtest [days] — Replay scoring strategy against history")
            lines.append("/blacklist — Manage token blacklist")
            lines.append("/whitelist — Manage token whitelist")
            lines.append("")
            lines.append("<b>📊 Trading:</b>")
            lines.append("/dca — Dollar cost averaging (split buys)")
            lines.append("/limit — Limit orders (buy/sell at price)")
            lines.append("/orders — View all active orders")
            from config import API_ENABLED, API_PORT
            lines.append(f"\nAPI: {'Enabled on port ' + str(API_PORT) if API_ENABLED else 'Disabled'}")
            lines.append(f"Sniper: {'Enabled' if SNIPER_ENABLED else 'Disabled'}")
    else:
        uid = update.effective_user.id
        lines.append(f"Your user ID: <code>{uid}</code>")
        lines.append(f"Ask the admin to run: <code>/adduser {uid}</code>")

    if is_auth:
        keyboard = [
            [
                InlineKeyboardButton("📊 Status", callback_data="status_refresh"),
                InlineKeyboardButton("💰 Balance", callback_data="balance_refresh"),
                InlineKeyboardButton("👛 Wallet", callback_data="wallet_refresh"),
            ],
            [
                InlineKeyboardButton("📜 History", callback_data="history_page:0"),
                InlineKeyboardButton("💼 Portfolio", callback_data="portfolio_refresh"),
                InlineKeyboardButton("⚙️ Config", callback_data="config_show"),
            ],
        ]
        await update.message.reply_html(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        await update.message.reply_html("\n".join(lines))


async def cmd_start(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    global is_running, scanner_task, monitor_task, whale_task, daily_report_task, sniper_task, dca_task, limit_order_task

    if is_running:
        await update.message.reply_text("Bot is already running.")
        return

    is_running = True
    scanner_task = asyncio.create_task(scanner_loop())
    monitor_task = asyncio.create_task(monitor.start())
    if whale_tracker:
        whale_task = asyncio.create_task(whale_tracker.start())
    if sniper and SNIPER_ENABLED:
        sniper_task = asyncio.create_task(sniper.start())
    daily_report_task = asyncio.create_task(daily_report_loop())
    dca_task = asyncio.create_task(dca_loop())
    limit_order_task = asyncio.create_task(limit_order_loop())

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    trading_users = await db.get_all_trading_users()

    msg = (
        "🚀 <b>Bot Started</b>\n\n"
        f"Chain: {CHAIN}\n"
        f"Admin balance: {balance:.4f} {native}\n"
        f"Active traders: {len(trading_users)}\n"
        f"Buy: {BUY_PERCENT}% | TP: {TAKE_PROFIT}% | Slippage: {SLIPPAGE}%\n"
        f"Scan every {SCAN_INTERVAL}s | Monitor every {MONITOR_INTERVAL}s\n"
        f"MCap: ${MIN_MCAP:,}–${MAX_MCAP:,} | Min Liq: ${MIN_LIQUIDITY:,}"
    )
    await update.message.reply_html(msg)
    logger.info("Bot started by user %s", update.effective_user.id)


async def cmd_stop(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    global is_running, scanner_task, monitor_task, whale_task, daily_report_task, sniper_task, dca_task, limit_order_task

    if not is_running:
        await update.message.reply_text("Bot is not running.")
        return

    is_running = False
    if monitor:
        await monitor.stop()
    if whale_tracker:
        await whale_tracker.stop()
    if sniper:
        await sniper.stop()
    if scanner_task and not scanner_task.done():
        scanner_task.cancel()
    if monitor_task and not monitor_task.done():
        monitor_task.cancel()
    if whale_task and not whale_task.done():
        whale_task.cancel()
    if sniper_task and not sniper_task.done():
        sniper_task.cancel()
    if daily_report_task and not daily_report_task.done():
        daily_report_task.cancel()
    if dca_task and not dca_task.done():
        dca_task.cancel()
    if limit_order_task and not limit_order_task.done():
        limit_order_task.cancel()

    scanner_task = None
    monitor_task = None
    whale_task = None
    sniper_task = None
    daily_report_task = None
    dca_task = None
    limit_order_task = None

    await update.message.reply_html("🛑 <b>Bot Stopped</b>\nScanning and trading paused. Bot still responds to commands.")
    logger.info("Bot stopped by user %s", update.effective_user.id)


def _format_stats_message(stats_all, stats_7d, stats_30d, native, title):
    def _sign(v):
        return f"+{v:.4f}" if v >= 0 else f"{v:.4f}"

    msg_parts = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"<b>{title}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        "<b>All Time</b>",
        f"   Trades: {stats_all['total_trades']} ({stats_all['winning_trades']}W / {stats_all['losing_trades']}L)",
        f"   Win Rate: {stats_all['win_rate']:.1f}%",
        f"   Avg ROI: {stats_all['avg_roi']:+.2f}%",
        f"   Total PnL: {_sign(stats_all['total_pnl_native'])} {native}",
        f"   Total Invested: {stats_all['total_invested']:.4f} {native}",
        f"   Profit Factor: {stats_all['profit_factor']:.2f}" if stats_all['profit_factor'] != float('inf') else "   Profit Factor: ∞ (no losses)",
    ]

    if stats_all["best_trade"]:
        bt = stats_all["best_trade"]
        pnl = bt["sell_amount_native"] - bt["buy_amount_native"]
        msg_parts.append(f"   🏆 Best: {bt['token_symbol']} ({bt['roi_percent']:+.1f}% / {_sign(pnl)} {native})")
    if stats_all["worst_trade"]:
        wt = stats_all["worst_trade"]
        pnl = wt["sell_amount_native"] - wt["buy_amount_native"]
        msg_parts.append(f"   💀 Worst: {wt['token_symbol']} ({wt['roi_percent']:+.1f}% / {_sign(pnl)} {native})")

    avg_dur = int(stats_all.get("avg_duration_seconds", 0))
    msg_parts.append(f"   ⏱ Avg Duration: {_format_duration(avg_dur)}")

    if stats_7d["total_trades"] > 0:
        msg_parts.extend([
            "",
            "<b>Last 7 Days</b>",
            f"   Trades: {stats_7d['total_trades']} | Win Rate: {stats_7d['win_rate']:.1f}%",
            f"   PnL: {_sign(stats_7d['total_pnl_native'])} {native} | Avg ROI: {stats_7d['avg_roi']:+.2f}%",
        ])

    if stats_30d["total_trades"] > 0:
        msg_parts.extend([
            "",
            "<b>Last 30 Days</b>",
            f"   Trades: {stats_30d['total_trades']} | Win Rate: {stats_30d['win_rate']:.1f}%",
            f"   PnL: {_sign(stats_30d['total_pnl_native'])} {native} | Avg ROI: {stats_30d['avg_roi']:+.2f}%",
        ])

    daily = stats_all.get("daily_pnl", [])
    if daily:
        msg_parts.extend(["", "<b>Daily PnL (Last 7 Days)</b>"])
        for d in daily:
            pnl = d["pnl"]
            icon = "🟢" if pnl >= 0 else "🔴"
            msg_parts.append(f"   {icon} {d['day']}: {_sign(pnl)} {native} ({d['trades']} trades, {d['wins']}W)")

    return "\n".join(msg_parts)


async def cmd_stats(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    is_admin = _is_admin(update)
    show_all = is_admin and context.args and context.args[0].lower() == "all"
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    target_user = None if show_all else user_id

    stats_all = await db.get_trade_stats(user_id=target_user)
    stats_7d = await db.get_trade_stats(user_id=target_user, days=7)
    stats_30d = await db.get_trade_stats(user_id=target_user, days=30)

    if stats_all["total_trades"] == 0:
        await update.message.reply_text("No completed trades to analyze.")
        return

    title = "📊 ALL USERS STATS" if show_all else "📊 YOUR TRADING STATS"
    msg = _format_stats_message(stats_all, stats_7d, stats_30d, native, title)

    keyboard = [[InlineKeyboardButton("🔄 Refresh Stats", callback_data="stats_refresh")]]
    await update.message.reply_html(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def daily_report_loop():
    global is_running
    logger.info("Daily PnL loop started")

    while is_running:
        try:
            now = datetime.now(timezone.utc)
            tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (tomorrow - now).total_seconds()

            elapsed = 0
            while elapsed < wait_seconds and is_running:
                await asyncio.sleep(min(60, wait_seconds - elapsed))
                elapsed += 60

            if not is_running:
                break

            await _send_daily_reports()

        except Exception as exc:
            logger.error("Daily PnL loop error: %s", exc)
            await asyncio.sleep(3600)


async def _send_daily_reports():
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    trading_users = await db.get_all_trading_users()

    for user_wallet in trading_users:
        uid = user_wallet["user_id"]
        try:
            report = await db.get_daily_pnl_report(uid)
            if report is None:
                continue

            balance = 0.0
            try:
                user_trader = await create_user_trader(uid)
                if user_trader:
                    balance = await user_trader.get_balance()
            except Exception:
                pass

            lines = [
                "━━━━━━━━━━━━━━━━━━━━━━",
                "📊 <b>DAILY PnL REPORT</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"📅 {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                "",
            ]

            if report["trades_today"] > 0:
                pnl_emoji = "🟢" if report["net_pnl"] >= 0 else "🔴"
                lines.append(f"<b>Today's Activity:</b>")
                lines.append(f"  Trades closed: {report['trades_today']}")
                lines.append(f"  Wins/Losses: {report['wins']}/{report['losses']}")
                lines.append(f"  Win rate: {report['win_rate']:.0f}%")
                lines.append(f"  {pnl_emoji} Net P&L: {report['net_pnl']:+.6f} {native}")
                if report["best_trade"]:
                    lines.append(f"  🏆 Best: {report['best_trade']['symbol']} ({report['best_trade']['roi']:+.1f}%)")
                if report["worst_trade"]:
                    lines.append(f"  💀 Worst: {report['worst_trade']['symbol']} ({report['worst_trade']['roi']:+.1f}%)")
                lines.append("")
            else:
                lines.append("No trades closed today.\n")

            lines.append(f"<b>Open Positions:</b> {report['open_count']}")
            if report["open_count"] > 0:
                lines.append(f"  Total invested: {report['total_invested']:.4f} {native}")

            lines.append(f"\n💰 Wallet balance: {balance:.6f} {native}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")

            await notifier.send_to_user(uid, "\n".join(lines))

        except Exception as exc:
            logger.error("Daily report error for user %d: %s", uid, exc)


async def cmd_wallet(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        await update.message.reply_text("No wallet found. Ask admin to run /adduser.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    ut = await create_user_trader(user_id)
    balance = await ut.get_balance() if ut else 0.0
    auto_trade = "✅ Enabled" if wallet_data.get("auto_trade", 1) else "❌ Disabled"
    at_state = "off" if wallet_data.get("auto_trade", 1) else "on"
    at_label = "⚙️ AutoTrade Off" if wallet_data.get("auto_trade", 1) else "⚙️ AutoTrade On"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
            InlineKeyboardButton(at_label, callback_data=f"autotrade:{at_state}"),
        ],
        [
            InlineKeyboardButton("💸 Withdraw Instructions", callback_data="withdraw_prompt"),
            InlineKeyboardButton("🔑 Export (DM only)", callback_data="export_prompt"),
        ],
    ]

    await update.message.reply_html(
        f"👛 <b>Your Wallet</b>\n"
        f"Address: <code>{wallet_data['public_key']}</code>\n"
        f"Balance: {balance:.6f} {native}\n"
        f"Auto-Trade: {auto_trade}\n\n"
        f"Send {native} to the address above to fund your trading wallet.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_autotrade(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        await update.message.reply_text("No wallet found.")
        return

    if not context.args:
        current = "on" if wallet_data.get("auto_trade", 1) else "off"
        await update.message.reply_html(f"Usage: <code>/autotrade on|off</code>\nCurrent: {current}")
        return

    arg = context.args[0].lower()
    if arg in ("on", "yes", "1", "true"):
        await db.set_auto_trade(user_id, True)
        await update.message.reply_text("✅ Auto-trading enabled.")
    elif arg in ("off", "no", "0", "false"):
        await db.set_auto_trade(user_id, False)
        await update.message.reply_text("❌ Auto-trading paused.")
    else:
        await update.message.reply_html("Usage: <code>/autotrade on|off</code>")


async def cmd_withdraw(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if not context.args or len(context.args) < 2:
        await update.message.reply_html(
            "Usage: <code>/withdraw &lt;amount&gt; &lt;destination_address&gt;</code>\n"
            "Example: <code>/withdraw 0.5 ABC...XYZ</code>"
        )
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            await update.message.reply_text("Amount must be positive.")
            return
    except ValueError:
        await update.message.reply_text("Invalid amount.")
        return

    dest_str = context.args[1].strip()
    try:
        dest_pubkey = Pubkey.from_string(dest_str)
    except Exception:
        await update.message.reply_text("Invalid Solana address.")
        return

    user_id = update.effective_user.id
    ut = await create_user_trader(user_id)
    if ut is None:
        await update.message.reply_text("No wallet found.")
        return

    balance = await ut.get_balance()
    if amount > balance - 0.005:
        await update.message.reply_html(
            f"Insufficient balance. You have {balance:.6f} SOL (need ~0.005 for fees)."
        )
        return

    await update.message.reply_html(f"🔄 Sending {amount:.6f} SOL to <code>{dest_str}</code>...")

    try:
        lamports = int(amount * 1e9)
        client = _get_shared_client()
        recent = await client.get_latest_blockhash()
        blockhash = recent.value.blockhash

        ix = transfer(TransferParams(
            from_pubkey=ut.keypair.pubkey(),
            to_pubkey=dest_pubkey,
            lamports=lamports,
        ))
        msg = Message.new_with_blockhash([ix], ut.keypair.pubkey(), blockhash)
        tx = Transaction.new_unsigned(msg)
        tx.sign([ut.keypair], blockhash)

        resp = await client.send_raw_transaction(
            bytes(tx),
            opts={"skip_preflight": True, "max_retries": 3},
        )
        sig = str(resp.value)

        tx_url = EXPLORER_TX.get("SOL", "https://solscan.io/tx/{}").format(sig)
        short = sig[:10] + "…" + sig[-6:]
        await update.message.reply_html(
            f"✅ <b>Withdrawal Sent</b>\n"
            f"Amount: {amount:.6f} SOL\n"
            f"To: <code>{dest_str}</code>\n"
            f'TX: <a href="{tx_url}">{short}</a>'
        )
    except Exception as exc:
        logger.error("Withdraw error for user %d: %s", user_id, exc)
        await update.message.reply_html(f"❌ Withdrawal failed: <code>{str(exc)[:200]}</code>")


async def cmd_export(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if update.message.chat.type != "private":
        await update.message.reply_text("⚠️ For security, please DM me directly with /export")
        return

    user_id = update.effective_user.id
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        await update.message.reply_text("No wallet found.")
        return

    public_key = wallet_data["public_key"]

    raw_key = decrypt_key(wallet_data["encrypted_private_key"])
    privkey_b58 = b58.b58encode(raw_key).decode()

    enc_seed = wallet_data.get("encrypted_seed_phrase", "")
    if enc_seed:
        seed_phrase = decrypt_key(enc_seed).decode()
    else:
        seed_phrase = None

    seed_display = seed_phrase if seed_phrase else "N/A (imported wallet)"

    await update.message.reply_html(
        "🔑 <b>YOUR WALLET CREDENTIALS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "⚠️ NEVER share these with anyone!\n"
        "⚠️ Delete this message after saving!\n\n"
        f"👛 Address:\n<code>{public_key}</code>\n\n"
        f"🌱 Seed Phrase (12 words):\n<code>{seed_display}</code>\n\n"
        f"🔐 Private Key (base58):\n<code>{privkey_b58}</code>\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Import into Phantom or Solflare using either the seed phrase or private key."
    )


async def cmd_status(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    is_admin = _is_admin(update)

    show_all = is_admin and context.args and context.args[0].lower() == "all"

    if show_all:
        positions = await monitor.get_positions_with_roi()
    else:
        positions = await monitor.get_positions_with_roi(user_id=user_id)

    if not positions:
        await update.message.reply_text("No open positions.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if show_all:
        by_user: dict[int, list] = {}
        for p in positions:
            uid = p.get("user_id", 0)
            by_user.setdefault(uid, []).append(p)

        lines = ["📊 <b>All Open Positions</b>\n"]
        for uid, user_positions in by_user.items():
            uw = await db.get_user_wallet(uid)
            addr = uw["public_key"][:8] + "…" + uw["public_key"][-4:] if uw else "unknown"
            lines.append(f"\n👤 User <code>{uid}</code> ({addr})")
            for p in user_positions:
                roi = p.get("roi", 0)
                arrow = "🟢" if roi >= 0 else "🔴"
                lines.append(
                    f"  {arrow} <b>{p['token_symbol']}</b> ROI: {roi:+.2f}% | "
                    f"Spent: {p['buy_amount_native']:.4f} {native}"
                )
    else:
        lines = ["📊 <b>Open Positions</b>\n"]
        keyboard = []
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
            tp = p["token_address"][:16]
            keyboard.append([
                InlineKeyboardButton(f"💰 Sell 25% {p['token_symbol']}", callback_data=f"sell:{tp}:25"),
                InlineKeyboardButton(f"💰 Sell 50%", callback_data=f"sell:{tp}:50"),
                InlineKeyboardButton(f"💰 Sell 100%", callback_data=f"sell:{tp}:100"),
            ])
        keyboard.append([InlineKeyboardButton("🔄 Refresh Positions", callback_data="status_refresh")])

    await update.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard) if not show_all else None,
    )


async def cmd_balance(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    ut = await create_user_trader(user_id)
    if ut is None:
        await update.message.reply_text("No wallet found.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    balance = await ut.get_balance()
    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="balance_refresh"),
            InlineKeyboardButton("👛 Wallet Details", callback_data="wallet_refresh"),
        ]
    ]
    await update.message.reply_html(
        f"💰 <b>Wallet Balance</b>\n{balance:.6f} {native} ({CHAIN})",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_history(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    all_trades = await db.get_trade_history(limit=100, user_id=user_id)

    if not all_trades:
        await update.message.reply_text("No completed trades.")
        return

    page_size = 5
    total = len(all_trades)
    total_pages = max(1, (total + page_size - 1) // page_size)
    trades = all_trades[:page_size]

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = [f"📜 <b>Trade History</b> (page 1/{total_pages})\n"]
    for t in trades:
        roi = t.get("roi_percent", 0)
        arrow = "🟢" if roi >= 0 else "🔴"
        dur = _format_duration(t.get("duration_seconds", 0))
        lines.append(
            f"{arrow} <b>{t['token_symbol']}</b> | ROI: {roi:+.2f}%\n"
            f"   Buy: {t['buy_amount_native']:.4f} → Sell: {t['sell_amount_native']:.4f} {native}\n"
            f"   Duration: {dur}\n"
        )

    keyboard_row = []
    keyboard_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="noop"))
    keyboard_row.append(InlineKeyboardButton(f"Page 1/{total_pages}", callback_data="noop"))
    if total_pages > 1:
        keyboard_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"history_page:{page_size}"))
    else:
        keyboard_row.append(InlineKeyboardButton("➡️ Next", callback_data="noop"))

    await update.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup([keyboard_row]),
    )


async def cmd_config(update, context):
    await _register_chat(update)
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
        f"Whale Copy Trade: {'Enabled' if WHALE_COPY_ENABLED else 'Disabled'}\n"
        f"Copy Amount: {WHALE_COPY_AMOUNT} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}\n"
        f"Anti-Rug: {'Enabled' if ANTIRUG_ENABLED else 'Disabled'}\n"
        f"Anti-Rug Min Liquidity: ${ANTIRUG_MIN_LIQ:,}\n"
        f"Anti-Rug Drop Threshold: {ANTIRUG_LIQ_DROP_PCT}%\n"
        f"Operator Fee: {OPERATOR_FEE_PCT}% ({'Enabled' if OPERATOR_FEE_ENABLED else 'Disabled'})"
        f"\nAlert Broadcast: {'Enabled' if alerts_enabled else 'Disabled'}"
        f"\nSell Tiers: {SELL_TIERS_RAW if SELL_TIERS_RAW else 'None (full sell at TP)'}"
        f"\n\n<b>Risk Management</b>\n"
        f"Max Positions: {MAX_OPEN_POSITIONS} per user\n"
        f"Max Daily Loss: {MAX_DAILY_LOSS} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}\n"
        f"Max Buy Amount: {MAX_BUY_AMOUNT} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}"
        f"\n\n<b>External API</b>\n"
        f"API Server: {'Enabled (port ' + str(API_PORT) + ')' if API_ENABLED else 'Disabled'}"
        f"\nSniper: {'ON' if SNIPER_ENABLED else 'OFF'} | Check: {SNIPER_CHECK_INTERVAL}s | Min Liq: ${SNIPER_MIN_LIQUIDITY:,}\n"
    )
    await update.message.reply_html(msg)


_SETTINGS_VALIDATION = {
    "min_score":     (int,   0,      100),
    "stop_loss":     (int,  -100,    0),
    "take_profit":   (int,   1,      1000),
    "buy_percent":   (int,   1,      100),
    "trailing_drop": (int,   1,      100),
    "slippage":      (int,   1,      100),
    "max_positions": (int,   1,      20),
    "max_buy_amount":(float, 0.001,  100),
}


async def cmd_mysettings(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    uid = update.effective_user.id
    args = context.args or []

    if len(args) == 1 and args[0].lower() == "reset":
        deleted = await db.delete_user_settings(uid)
        if deleted:
            await update.message.reply_html("✅ All your settings have been reset to global defaults.")
        else:
            await update.message.reply_html("ℹ️ You have no custom settings to reset.")
        return

    if len(args) >= 2:
        key = args[0].lower()
        raw_value = args[1]
        if key not in _SETTINGS_VALIDATION:
            valid_keys = ", ".join(sorted(_SETTINGS_VALIDATION))
            await update.message.reply_html(f"❌ Unknown setting <code>{key}</code>.\nValid keys: <code>{valid_keys}</code>")
            return
        cast, lo, hi = _SETTINGS_VALIDATION[key]
        try:
            value = cast(raw_value)
        except (ValueError, TypeError):
            await update.message.reply_html(f"❌ Invalid value. Expected {cast.__name__} between {lo} and {hi}.")
            return
        if value < lo or value > hi:
            await update.message.reply_html(f"❌ Value out of range. <code>{key}</code> must be between {lo} and {hi}.")
            return
        await db.upsert_user_setting(uid, key, value)
        await update.message.reply_html(f"✅ <code>{key}</code> set to <b>{value}</b>")
        return

    if args:
        await update.message.reply_html(
            "Usage:\n"
            "<code>/mysettings</code> — view current settings\n"
            "<code>/mysettings &lt;key&gt; &lt;value&gt;</code> — set a value\n"
            "<code>/mysettings reset</code> — reset all to defaults"
        )
        return

    cfg = await db.get_effective_config(uid)
    user_row = await db.get_user_settings(uid)

    from config import (
        MIN_SCORE, STOP_LOSS, TAKE_PROFIT, BUY_PERCENT,
        TRAILING_DROP, SLIPPAGE, MAX_OPEN_POSITIONS, MAX_BUY_AMOUNT,
    )
    global_defaults = {
        "min_score": MIN_SCORE,
        "stop_loss": STOP_LOSS,
        "take_profit": TAKE_PROFIT,
        "buy_percent": BUY_PERCENT,
        "trailing_drop": TRAILING_DROP,
        "slippage": SLIPPAGE,
        "max_positions": MAX_OPEN_POSITIONS,
        "max_buy_amount": MAX_BUY_AMOUNT,
    }

    labels = {
        "min_score": "Min Score",
        "stop_loss": "Stop Loss",
        "take_profit": "Take Profit",
        "buy_percent": "Buy %",
        "trailing_drop": "Trailing Drop",
        "slippage": "Slippage",
        "max_positions": "Max Positions",
        "max_buy_amount": "Max Buy Amount",
    }
    suffixes = {
        "stop_loss": "%", "take_profit": "%", "buy_percent": "%",
        "trailing_drop": "%", "slippage": "%",
    }

    lines = ["⚙️ <b>Your Settings</b>", "─────────────────"]
    for key, label in labels.items():
        val = cfg[key]
        suffix = suffixes.get(key, "")
        is_custom = user_row is not None and user_row.get(key) is not None
        if is_custom:
            lines.append(f"{label}: <b>{val}{suffix}</b> ✏️  (global: {global_defaults[key]}{suffix})")
        else:
            lines.append(f"{label}: {val}{suffix}  <i>(global default)</i>")

    lines.append("")
    lines.append("<b>Usage:</b> <code>/mysettings &lt;key&gt; &lt;value&gt;</code>")
    lines.append("<b>Example:</b> <code>/mysettings min_score 70</code>")
    lines.append("<b>Reset all:</b> <code>/mysettings reset</code>")

    await update.message.reply_html("\n".join(lines))


async def cmd_buy(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "Usage: <code>/buy &lt;token_address&gt; [amount]</code>\n"
            "Example: <code>/buy So1abc...xyz 0.5</code>\n"
            "If amount is omitted, uses configured BUY_PERCENT% of balance."
        )
        return

    user_id = update.effective_user.id
    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await update.message.reply_text("No wallet found.")
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
        buy_amount = await user_trader.get_buy_amount()

    if buy_amount <= 0:
        await update.message.reply_text(f"Insufficient {native} balance.")
        return

    already = await db.is_token_already_bought(token_address, CHAIN.upper(), user_id)
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

    # Risk management checks for manual buys
    open_count = await db.count_open_positions(user_id)
    if open_count >= MAX_OPEN_POSITIONS:
        await update.message.reply_text(f"⚠️ Max positions reached ({open_count}/{MAX_OPEN_POSITIONS}). Sell a position first.")
        return

    if MAX_DAILY_LOSS > 0:
        daily_loss = await db.get_daily_realized_loss(user_id)
        if daily_loss >= MAX_DAILY_LOSS:
            native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            await update.message.reply_html(
                f"⚠️ Daily loss limit reached ({daily_loss:.4f}/{MAX_DAILY_LOSS} {native}). Trading paused until tomorrow."
            )
            return

    if MAX_BUY_AMOUNT > 0 and buy_amount > MAX_BUY_AMOUNT:
        buy_amount = MAX_BUY_AMOUNT

    tp = token_address[:16]
    _pending_buys[f"{user_id}:{tp}"] = token_address
    keyboard = [
        [
            InlineKeyboardButton("✅ Confirm Buy", callback_data=f"buy_confirm:{tp}:{buy_amount}"),
            InlineKeyboardButton("❌ Cancel", callback_data="buy_cancel"),
        ]
    ]

    await update.message.reply_html(
        f"⚙️ <b>Manual Buy</b>\n"
        f"Token: <code>{token_address}</code>\n"
        f"Amount: {buy_amount:.4f} {native}\n\n"
        f"Confirm or cancel below.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_sell(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "Usage: <code>/sell &lt;token_address&gt; [percent]</code>\n"
            "Example: <code>/sell So1abc...xyz 50</code> (sell 50%)\n"
            "If percent is omitted, sells 100% of holdings."
        )
        return

    user_id = update.effective_user.id
    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await update.message.reply_text("No wallet found.")
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

    ui_balance, decimals = await user_trader.get_token_balance(token_address)

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

    result = await user_trader.sell_token(token_address, sell_raw, decimals)

    if result is None:
        await update.message.reply_html("\u274c <b>Sell failed.</b> Check logs for details.")
        logger.error("Manual sell failed for %s (user %d)", token_address, user_id)
        return

    if sell_percent == 100:
        positions = await db.get_open_positions(user_id=user_id)
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
                await db.close_position(pos["token_address"], CHAIN.upper(), exit_data, user_id=user_id)

                try:
                    profit_native = result["native_received"] - pos["buy_amount_native"]
                    if profit_native > 0:
                        admin_wallet_data = await db.get_user_wallet(TELEGRAM_CHAT_ID)
                        if admin_wallet_data:
                            fee_result = await collect_fee(
                                user_id=user_id,
                                token_symbol=pos["token_symbol"],
                                profit_native=profit_native,
                                admin_public_key=admin_wallet_data["public_key"],
                            )
                            if fee_result and fee_result.get("tx_hash"):
                                await update.message.reply_html(
                                    f"💰 Operator fee: {fee_result['fee_amount']:.6f} SOL "
                                    f"({fee_result['fee_pct']:.1f}% of profit)"
                                )
                                await notifier.send_message(
                                    f"💰 Fee collected: {fee_result['fee_amount']:.6f} SOL from user {user_id} "
                                    f"({fee_result['fee_pct']:.1f}% of {profit_native:.6f} SOL profit on {pos['token_symbol']})"
                                )
                except Exception as fee_exc:
                    logger.error("Fee collection failed during manual sell: %s", fee_exc)

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

    if not _is_admin(update):
        await notifier.send_message(
            f"📋 User <code>{user_id}</code> manual sell {token_address[:8]}… — "
            f"received {result['native_received']:.4f} {native}",
        )

    logger.info("Manual sell executed: %s (%d%%), user=%d, tx=%s", token_address, sell_percent, user_id, result["tx_hash"])


async def cmd_info(update, context):
    """Token research: /info <token_address>"""
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if not context.args or len(context.args) < 1:
        await update.message.reply_html(
            "Usage: <code>/info &lt;token_address&gt;</code>\n"
            "Example: <code>/info EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v</code>"
        )
        return

    token_address = context.args[0].strip()
    await update.message.reply_text("\U0001f50d Researching token...")

    async with aiohttp.ClientSession() as session:
        data = await fetch_token_research(session, CHAIN, token_address)

    if data is None:
        await update.message.reply_text("\u274c Could not find token data. Check the address and try again.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    hp_icon = "\U0001f6ab HONEYPOT" if data["is_honeypot"] else "\u2705 Safe" if data["honeypot_checked"] else "\u26a0\ufe0f Unknown"

    pc = data["price_change_24h"]
    pc_icon = "\U0001f7e2" if pc >= 0 else "\U0001f534"
    pc_str = f"{pc_icon} {pc:+.2f}%" if pc != 0 else "\u2014"

    socials = data.get("social_links", {})
    social_parts = []
    social_icons = {"website": "\U0001f310", "twitter": "\U0001f426", "telegram": "\U0001f4ac", "discord": "\U0001f3ae"}
    for key, icon in social_icons.items():
        url = socials.get(key, "")
        if url:
            social_parts.append(f'<a href="{url}">{icon} {key.title()}</a>')
    social_str = " | ".join(social_parts) if social_parts else "None found"

    txns = data.get("txns_24h", {})
    buys = txns.get("buys", 0)
    sells = txns.get("sells", 0)

    chain_slug = {"SOL": "solana", "ETH": "ether", "BSC": "bsc"}.get(CHAIN.upper(), "solana")
    dextools_url = f"https://www.dextools.io/app/en/{chain_slug}/pair-explorer/{data.get('pair_address') or token_address}"
    ds_url = data.get("dexscreener_url") or f"https://dexscreener.com/{chain_slug}/{token_address}"

    msg = (
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f52c <b>TOKEN INFO</b>\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001fa99 <b>{data['name']}</b> ({data['symbol']})\n"
        f"\U0001f4c4 <code>{token_address}</code>\n"
        f"\u26d3 {CHAIN.upper()} | Age: {data['age_str']}\n\n"
        f"\U0001f4b0 <b>Price:</b> ${data['price_usd']:.8g} ({data['price_native']:.10f} {native})\n"
        f"\U0001f4ca <b>MCap:</b> ${data['market_cap']:,.0f}\n"
        f"\U0001f4a7 <b>Liquidity:</b> ${data['liquidity']:,.0f}\n"
        f"\U0001f4c8 <b>Volume 24h:</b> ${data['volume_24h']:,.0f}\n"
        f"\U0001f4c9 <b>24h Change:</b> {pc_str}\n"
        f"\U0001f504 <b>Txns 24h:</b> {buys} buys / {sells} sells\n"
    )

    if data["holders"] > 0:
        msg += f"\U0001f465 <b>Holders:</b> {data['holders']:,}\n"

    msg += (
        f"\n\U0001f6e1 <b>Safety:</b> {hp_icon}\n"
        f"\U0001f4b8 <b>Tax:</b> Buy {data['buy_tax']:.1f}% / Sell {data['sell_tax']:.1f}%\n"
        f"\u2b50 <b>Score:</b> {data['score_bar']}\n"
        f"\n\U0001f517 {social_str}\n"
        f'\n<a href="{dextools_url}">\U0001f4ca DexTools</a> | <a href="{ds_url}">\U0001f4ca DexScreener</a>\n'
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
    )

    tp = token_address[:16]
    keyboard = [
        [
            InlineKeyboardButton("\U0001f6d2 Buy 0.1 SOL", callback_data=f"quickbuy:{tp}:0.1"),
            InlineKeyboardButton("\U0001f6d2 Buy 0.5 SOL", callback_data=f"quickbuy:{tp}:0.5"),
        ],
        [
            InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"info_refresh:{token_address}"),
        ],
    ]

    await update.message.reply_html(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )


async def cmd_snipe(update, context):
    """Sniper: /snipe <address> [amount] | /snipe list | /snipe remove <address>"""
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    if not SNIPER_ENABLED:
        await update.message.reply_text("⚠️ Sniper mode is disabled. Set SNIPER_ENABLED=true to enable.")
        return

    user_id = update.effective_user.id
    args = context.args or []
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if not args:
        await update.message.reply_html(
            "🎯 <b>Sniper Mode</b>\n\n"
            "Usage:\n"
            "<code>/snipe &lt;token_address&gt; [amount]</code> — Add to snipe list\n"
            "<code>/snipe list</code> — Show your snipe targets\n"
            "<code>/snipe remove &lt;token_address&gt;</code> — Remove from list\n\n"
            f"Min liquidity: ${SNIPER_MIN_LIQUIDITY:,}\n"
            f"Check interval: {SNIPER_CHECK_INTERVAL}s"
        )
        return

    subcmd = args[0].lower()

    if subcmd == "list":
        targets = await db.get_user_snipe_targets(user_id)
        if not targets:
            await update.message.reply_text("🎯 No active snipe targets.")
            return
        lines = ["🎯 <b>Your Snipe Targets</b>\n"]
        for i, t in enumerate(targets, 1):
            amt = f"{t['amount']:.4f} {native}" if t["amount"] > 0 else "auto"
            lines.append(f"{i}. <code>{t['token_address']}</code>\n   Amount: {amt} | Added: {t['added_at']}")
        await update.message.reply_html("\n".join(lines))
        return

    if subcmd == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /snipe remove <token_address>")
            return
        addr = args[1].strip()
        removed = await db.remove_snipe_target(addr, user_id)
        if removed:
            await update.message.reply_text(f"✅ Removed from snipe list.")
        else:
            await update.message.reply_text(f"❌ Not found in your snipe list.")
        return

    token_address = args[0].strip()
    amount = 0.0
    if len(args) >= 2:
        try:
            amount = float(args[1])
            if amount < 0:
                await update.message.reply_text("Amount must be positive.")
                return
        except ValueError:
            await update.message.reply_text("Invalid amount.")
            return

    already = await db.is_token_already_bought(token_address, CHAIN.upper(), user_id)
    if already:
        await update.message.reply_text("Already holding this token.")
        return

    added = await db.add_snipe_target(token_address, user_id, amount)
    if not added:
        await update.message.reply_text("Already in your snipe list.")
        return

    amt_str = f"{amount:.4f} {native}" if amount > 0 else f"auto ({BUY_PERCENT}% of balance)"
    await update.message.reply_html(
        f"🎯 <b>Snipe Target Added</b>\n\n"
        f"Token: <code>{token_address}</code>\n"
        f"Amount: {amt_str}\n"
        f"Min Liquidity: ${SNIPER_MIN_LIQUIDITY:,}\n\n"
        f"The bot will auto-buy when a pool appears with sufficient liquidity.\n"
        f"Checking every {SNIPER_CHECK_INTERVAL} seconds."
    )


async def cmd_portfolio(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    is_admin = _is_admin(update)
    show_all = is_admin and context.args and context.args[0].lower() == "all"

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if show_all:
        trading_users = await db.get_all_trading_users()
        all_users = [u["user_id"] for u in trading_users]
        if TELEGRAM_CHAT_ID not in all_users:
            all_users.insert(0, TELEGRAM_CHAT_ID)

        msg_parts = [
            "━━━━━━━━━━━━━━━━━━━━━━",
            "💼 <b>ALL USERS PORTFOLIO</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]

        grand_total = 0.0

        for uid in all_users:
            ut = await create_user_trader(uid)
            if ut is None:
                continue
            bal = await ut.get_balance()
            positions = await monitor.get_positions_with_roi(user_id=uid)
            pos_value = sum(
                (p.get("current_price", 0) * p.get("tokens_received", 0)) if p.get("current_price", 0) > 0 else p.get("buy_amount_native", 0)
                for p in positions
            )
            total = bal + pos_value
            grand_total += total

            uw = await db.get_user_wallet(uid)
            addr = uw["public_key"][:8] + "…" + uw["public_key"][-4:] if uw else "?"

            msg_parts.append(
                f"\n👤 <code>{uid}</code> ({addr})\n"
                f"   💰 Balance: {bal:.4f} {native}\n"
                f"   📦 Positions: {len(positions)} ({pos_value:.4f} {native})\n"
                f"   📊 Total: {total:.4f} {native}"
            )

        msg_parts.append(f"\n━━━━━━━━━━━━━━━━━━━━━━")
        msg_parts.append(f"💎 Grand Total: {grand_total:.4f} {native}")

        await update.message.reply_html("\n".join(msg_parts))
        return

    ut = await create_user_trader(user_id)
    if ut is None:
        await update.message.reply_text("No wallet found.")
        return

    wallet_balance = await ut.get_balance()
    positions = await monitor.get_positions_with_roi(user_id=user_id)

    total_invested = 0.0
    total_current_value = 0.0

    position_lines = []
    for p in positions:
        invested = p.get("buy_amount_native", 0)
        tokens = p.get("tokens_received", 0)
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

    trades = await db.get_trade_history(limit=100, user_id=user_id)
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

    keyboard = [[InlineKeyboardButton("🔄 Refresh Portfolio", callback_data="portfolio_refresh")]]
    await update.message.reply_html(
        "\n".join(msg_parts),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_adduser(update, context):
    await _register_chat(update)
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

    existing_wallet = await db.get_user_wallet(user_id)
    if existing_wallet:
        await update.message.reply_html(
            f"✅ User <code>{user_id}</code> granted access.\n"
            f"👛 Wallet already exists: <code>{existing_wallet['public_key']}</code>"
        )
        return

    kp, seed_phrase = _generate_solana_wallet()
    public_key = str(kp.pubkey())
    encrypted_pk = encrypt_key(bytes(kp))
    encrypted_seed = encrypt_key(seed_phrase.encode())
    await db.save_user_wallet(user_id, public_key, encrypted_pk, encrypted_seed)

    await update.message.reply_html(
        f"✅ User <code>{user_id}</code> granted access.\n"
        f"👛 Wallet generated: <code>{public_key}</code>\n"
        f"They should fund this address with SOL to start trading."
    )


async def cmd_removeuser(update, context):
    await _register_chat(update)
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

    force = len(context.args) > 1 and context.args[1].lower() == "force"

    open_positions = await db.get_open_positions(user_id=user_id)
    ut = await create_user_trader(user_id)
    balance = await ut.get_balance() if ut else 0.0

    if (open_positions or balance > 0.001) and not force:
        warn_parts = []
        if open_positions:
            warn_parts.append(f"{len(open_positions)} open position(s)")
        if balance > 0.001:
            warn_parts.append(f"{balance:.4f} SOL balance")
        await update.message.reply_html(
            f"⚠️ User <code>{user_id}</code> has {' and '.join(warn_parts)}.\n"
            f"Funds will be <b>PERMANENTLY</b> lost.\n"
            f"To confirm: <code>/removeuser {user_id} force</code>"
        )
        return

    removed = await db.remove_allowed_user(user_id)
    await db.delete_user_wallet(user_id)

    if removed:
        msg = f"🚫 User <code>{user_id}</code> access revoked."
        warn_parts = []
        if open_positions:
            warn_parts.append(f"{len(open_positions)} open position(s)")
        if balance > 0.001:
            warn_parts.append(f"{balance:.4f} SOL balance")
        if warn_parts:
            msg += f"\n⚠️ User had {' and '.join(warn_parts)} (now inaccessible)."
        await update.message.reply_html(msg)
    else:
        await update.message.reply_html(f"User <code>{user_id}</code> was not in the list.")


async def cmd_users(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    users = await db.get_allowed_users()
    if not users:
        await update.message.reply_text("No authorized users (besides admin).")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = ["👥 <b>Authorized Users</b>\n"]
    for u in users:
        uid = u["user_id"]
        name = u.get("username") or "—"
        uw = await db.get_user_wallet(uid)
        if uw:
            addr = uw["public_key"]
            auto = "✅" if uw.get("auto_trade", 1) else "❌"
            ut = await create_user_trader(uid)
            bal = await ut.get_balance() if ut else 0.0
            positions = await db.get_open_positions(user_id=uid)
            lines.append(
                f"• <code>{uid}</code> ({name}) — {auto} auto-trade\n"
                f"   👛 <code>{addr}</code>\n"
                f"   💰 {bal:.4f} {native} | 📦 {len(positions)} position(s)"
            )
        else:
            lines.append(f"• <code>{uid}</code> ({name}) — no wallet")

    await update.message.reply_html("\n".join(lines))


async def cmd_chats(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    chats = await db.get_all_bot_chats()
    if not chats:
        await update.message.reply_text("No active chats.")
        return

    lines = ["📢 <b>Active Chats</b>\n"]
    for c in chats:
        cid = c["chat_id"]
        ctype = c.get("chat_type", "private")
        title = c.get("title", "")
        if ctype == "private":
            lines.append(f"• DM: {title or 'user'} (<code>{cid}</code>)")
        else:
            lines.append(f"• {ctype.capitalize()}: \"{title}\" (<code>{cid}</code>)")

    await update.message.reply_html("\n".join(lines))


async def cmd_addwhale(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    if not context.args:
        await update.message.reply_html("Usage: <code>/addwhale &lt;address&gt; [label]</code>")
        return

    address = context.args[0].strip()

    try:
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
    await _register_chat(update)
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
    await _register_chat(update)
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


async def cmd_copytrade(update, context):
    """Toggle or show copy trading status."""
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    status = "✅ Enabled" if WHALE_COPY_ENABLED else "❌ Disabled"
    msg = (
        "🐋 <b>Whale Copy Trading</b>\n\n"
        f"Status: {status}\n"
        f"Copy Amount: {WHALE_COPY_AMOUNT} {native}\n"
        f"Chain: {CHAIN}\n\n"
        "Configure via environment variables:\n"
        "<code>WHALE_COPY_ENABLED=true</code>\n"
        "<code>WHALE_COPY_AMOUNT=0.1</code>\n"
        "<code>WHALE_COPY_MAX_PER_TOKEN=1</code>"
    )
    await update.message.reply_html(msg)


async def cmd_fees(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    stats = await db.get_fee_stats()
    recent = await db.get_fee_history(limit=10)

    msg_parts = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "💰 <b>FEE REVENUE</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"Fee Rate: {OPERATOR_FEE_PCT}%",
        f"Status: {'✅ Enabled' if OPERATOR_FEE_ENABLED else '❌ Disabled'}",
        "",
        "<b>Totals</b>",
        f"   Collected: {stats.get('total_collected', 0):.6f} SOL",
        f"   Pending: {stats.get('total_pending', 0):.6f} SOL",
        f"   Failed: {stats.get('total_failed', 0):.6f} SOL",
        f"   Total Trades: {stats.get('count', 0)}",
    ]

    if recent:
        msg_parts.append("")
        msg_parts.append("<b>Recent Fees</b>")
        for f in recent:
            status_icon = "✅" if f["status"] == "collected" else "❌" if f["status"] == "failed" else "⏳"
            msg_parts.append(
                f"  {status_icon} {f['token_symbol']} — {f['fee_amount_native']:.6f} SOL "
                f"(user {f['user_id']})"
            )

    await update.message.reply_html("\n".join(msg_parts))


async def post_init(application):
    global trader, monitor, notifier, whale_tracker, alerts_enabled, sniper

    await db.init_db()

    from config import ALERT_BROADCAST, RPC_URLS_SOL
    alerts_enabled = ALERT_BROADCAST
    logger.info("Configured %d Solana RPC endpoint(s)", len(RPC_URLS_SOL))

    trader = create_trader(CHAIN)
    notifier = Notifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    monitor = ProfitMonitor(trader, notifier)

    if CHAIN.upper() == "SOL" and WHALE_TRACKING_ENABLED:
        whale_tracker = WhaleTracker(trader.client, notifier)

    if SNIPER_ENABLED:
        sniper = Sniper(notifier)

    admin_wallet = await db.get_user_wallet(TELEGRAM_CHAT_ID)
    if not admin_wallet:
        admin_kp = _load_solana_keypair(PRIVATE_KEY)
        encrypted_admin = encrypt_key(bytes(admin_kp))
        await db.save_user_wallet(TELEGRAM_CHAT_ID, str(admin_kp.pubkey()), encrypted_admin, "")

    await db.migrate_legacy_positions(TELEGRAM_CHAT_ID)

    await db.upsert_bot_chat(TELEGRAM_CHAT_ID, "private", "admin")

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    if CHAIN.upper() == "SOL":
        balance = await trader.get_balance()
    else:
        balance = await trader.get_balance(CHAIN)

    trading_users = await db.get_all_trading_users()

    global api_runner
    api_runner = await start_api_server()

    logger.info("Bot initialised – chain=%s, balance=%.6f %s, traders=%d", CHAIN, balance, native, len(trading_users))
    scanner_mode = "DexTools + DexScreener" if DEXTOOLS_API_KEY else "DexScreener only (free)"
    await notifier.send_message(
        f"🤖 <b>DexTool Scanner Online</b>\n"
        f"Chain: {CHAIN} | Balance: {balance:.4f} {native}\n"
        f"Active traders: {len(trading_users)}\n"
        f"Scanner: {scanner_mode}\n"
        f"Send /start to begin scanning."
    )


async def shutdown(application):
    global is_running, api_runner, dca_task, limit_order_task, daily_report_task
    is_running = False
    await stop_api_server(api_runner)
    api_runner = None
    if whale_tracker:
        await whale_tracker.stop()
    if monitor:
        await monitor.stop()
    if dca_task and not dca_task.done():
        dca_task.cancel()
    if limit_order_task and not limit_order_task.done():
        limit_order_task.cancel()
    if daily_report_task and not daily_report_task.done():
        daily_report_task.cancel()
    dca_task = None
    limit_order_task = None
    daily_report_task = None
    if trader:
        await trader.close()
    logger.info("Shutdown complete")


# ---------------------------------------------------------------------------
# Inline-keyboard callback handler + sub-handlers
# ---------------------------------------------------------------------------

async def _handle_sell_callback(query, user_id, token_prefix, percent):
    positions = await db.get_open_positions(user_id=user_id)
    pos = None
    for p in positions:
        if p["token_address"][:16] == token_prefix:
            pos = p
            break
    if pos is None:
        await query.edit_message_text("Position not found or already closed.")
        return

    token_address = pos["token_address"]
    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await query.edit_message_text("No wallet found.")
        return

    ui_balance, decimals = await user_trader.get_token_balance(token_address)
    if ui_balance <= 0:
        await query.edit_message_text("Zero token balance.")
        return

    sell_fraction = percent / 100.0
    sell_ui = ui_balance * sell_fraction
    sell_raw = int(sell_ui * (10 ** decimals)) if decimals > 0 else int(sell_ui * 1e9)

    if sell_raw <= 0:
        await query.edit_message_text("Amount too small to sell.")
        return

    await query.edit_message_text(f"🔄 Selling {percent}% of {pos['token_symbol']}...")

    result = await user_trader.sell_token(token_address, sell_raw, decimals)
    if result is None:
        await query.edit_message_text(f"❌ Sell failed for {pos['token_symbol']}.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if percent == 100:
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
        await db.close_position(pos["token_address"], CHAIN.upper(), exit_data, user_id=user_id)

        try:
            profit_native = result["native_received"] - pos["buy_amount_native"]
            if profit_native > 0:
                admin_wallet_data = await db.get_user_wallet(TELEGRAM_CHAT_ID)
                if admin_wallet_data:
                    fee_result = await collect_fee(
                        user_id=user_id,
                        token_symbol=pos["token_symbol"],
                        profit_native=profit_native,
                        admin_public_key=admin_wallet_data["public_key"],
                    )
                    if fee_result and fee_result.get("tx_hash"):
                        await notifier.send_message(
                            f"💰 Fee collected: {fee_result['fee_amount']:.6f} SOL from user {user_id} "
                            f"({fee_result['fee_pct']:.1f}% of {profit_native:.6f} SOL profit on {pos['token_symbol']})"
                        )
        except Exception as fee_exc:
            logger.error("Fee collection failed during inline sell: %s", fee_exc)

    tx_url = EXPLORER_TX.get(CHAIN.upper(), EXPLORER_TX["SOL"]).format(result["tx_hash"])
    short_hash = result["tx_hash"][:10] + "…" + result["tx_hash"][-6:] if len(result["tx_hash"]) > 20 else result["tx_hash"]
    await query.edit_message_text(
        f"✅ Sold {percent}% of {pos['token_symbol']}\n"
        f"Received: {result['native_received']:.6f} {native}\n"
        f'TX: <a href="{tx_url}">{short_hash}</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    await notifier.send_message(
        f"📋 User <code>{user_id}</code> inline sell {pos['token_symbol']} ({percent}%) — "
        f"received {result['native_received']:.4f} {native}",
    )
    logger.info("Inline sell: %s (%d%%), user=%d, tx=%s", token_address, percent, user_id, result["tx_hash"])


async def _handle_buy_confirm_callback(query, user_id, token_prefix, amount):
    cache_key = f"{user_id}:{token_prefix}"
    token_address = _pending_buys.pop(cache_key, None)

    if token_address is None:
        import aiosqlite
        from db import DB_PATH
        async with aiosqlite.connect(str(DB_PATH)) as conn:
            conn.row_factory = aiosqlite.Row
            cur = await conn.execute(
                "SELECT contract_address FROM detected_tokens WHERE contract_address LIKE ? ORDER BY detected_at DESC LIMIT 1",
                (token_prefix + "%",),
            )
            row = await cur.fetchone()
            if row:
                token_address = row["contract_address"]
    if token_address is None:
        positions = await db.get_open_positions(user_id=user_id)
        for p in positions:
            if p["token_address"][:16] == token_prefix:
                token_address = p["token_address"]
                break

    if token_address is None:
        await query.edit_message_text("Token not found. Please use /buy with the full address.")
        return

    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await query.edit_message_text("No wallet found.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    # Risk management checks for inline buy confirm
    open_count = await db.count_open_positions(user_id)
    if open_count >= MAX_OPEN_POSITIONS:
        await query.edit_message_text(f"⚠️ Max positions reached ({open_count}/{MAX_OPEN_POSITIONS}). Sell a position first.")
        return

    if MAX_DAILY_LOSS > 0:
        daily_loss = await db.get_daily_realized_loss(user_id)
        if daily_loss >= MAX_DAILY_LOSS:
            await query.edit_message_text(
                f"⚠️ Daily loss limit reached ({daily_loss:.4f}/{MAX_DAILY_LOSS} {native}). Trading paused until tomorrow."
            )
            return

    if MAX_BUY_AMOUNT > 0 and amount > MAX_BUY_AMOUNT:
        amount = MAX_BUY_AMOUNT

    await query.edit_message_text(
        f"🔄 Executing buy of {amount:.4f} {native}...",
    )

    result = await user_trader.buy_token(token_address, amount)
    if result is None:
        await query.edit_message_text("❌ Buy failed. Check logs for details.")
        logger.error("Inline buy failed for %s (user %d)", token_address, user_id)
        return

    symbol = result.get("symbol", token_address[:8])

    entry_liq = 0.0
    try:
        from dexscreener import get_token_liquidity
        async with aiohttp.ClientSession() as liq_session:
            entry_liq = await get_token_liquidity(liq_session, CHAIN, token_address)
    except Exception:
        pass

    position = {
        "token_address": token_address,
        "token_symbol": symbol,
        "chain": CHAIN.upper(),
        "entry_price": result["entry_price"],
        "tokens_received": result["tokens_received"],
        "buy_amount_native": result["amount_spent"],
        "buy_tx_hash": result["tx_hash"],
        "pair_address": "",
        "entry_liquidity": entry_liq,
        "user_id": user_id,
    }
    await db.save_open_position(position)

    tx_url = EXPLORER_TX.get(CHAIN.upper(), EXPLORER_TX["SOL"]).format(result["tx_hash"])
    short_hash = result["tx_hash"][:10] + "…" + result["tx_hash"][-6:] if len(result["tx_hash"]) > 20 else result["tx_hash"]
    await query.edit_message_text(
        f"✅ <b>Buy Executed</b>\n"
        f"Token: {symbol} | Amount: {result['tokens_received']:.4f}\n"
        f"Entry Price: {result['entry_price']:.10f}\n"
        f'TX: <a href="{tx_url}">{short_hash}</a>',
        parse_mode="HTML",
        disable_web_page_preview=True,
    )

    is_admin_user = user_id == TELEGRAM_CHAT_ID
    if not is_admin_user:
        await notifier.send_message(
            f"📋 User <code>{user_id}</code> inline buy {symbol} — "
            f"{result['amount_spent']:.4f} {native}",
        )
    logger.info("Inline buy: %s, user=%d, tx=%s", token_address, user_id, result["tx_hash"])


async def _handle_quickbuy_callback(query, user_id, token_prefix, amount):
    is_allowed = user_id == TELEGRAM_CHAT_ID or await db.is_user_allowed(user_id)
    if not is_allowed:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    import aiosqlite
    from db import DB_PATH
    token_address = None
    symbol = "?"
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT contract_address, symbol FROM detected_tokens WHERE contract_address LIKE ? ORDER BY detected_at DESC LIMIT 1",
            (token_prefix + "%",),
        )
        row = await cur.fetchone()
        if row:
            token_address = row["contract_address"]
            symbol = row["symbol"] or "?"

    if token_address is None:
        await query.answer("Token no longer available.", show_alert=True)
        return

    already = await db.is_token_already_bought(token_address, CHAIN.upper(), user_id)
    if already:
        await query.answer("Already holding this token.", show_alert=True)
        return

    # Risk management checks for quick buy
    open_count = await db.count_open_positions(user_id)
    if open_count >= MAX_OPEN_POSITIONS:
        await query.answer(f"Max positions reached ({open_count}/{MAX_OPEN_POSITIONS}). Sell first.", show_alert=True)
        return

    if MAX_DAILY_LOSS > 0:
        daily_loss = await db.get_daily_realized_loss(user_id)
        if daily_loss >= MAX_DAILY_LOSS:
            native_sym = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            await query.answer(f"Daily loss limit reached ({daily_loss:.4f}/{MAX_DAILY_LOSS} {native_sym}).", show_alert=True)
            return

    if MAX_BUY_AMOUNT > 0 and amount > MAX_BUY_AMOUNT:
        amount = MAX_BUY_AMOUNT

    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await query.answer("No wallet found.", show_alert=True)
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    await query.answer(f"Buying {amount} {native} of {symbol}...")

    result = await user_trader.buy_token(token_address, amount)
    if result is None:
        await notifier.send_to_user(user_id, f"❌ Quick buy failed for {symbol}")
        return

    entry_liq = 0.0
    try:
        from dexscreener import get_token_liquidity
        async with aiohttp.ClientSession() as liq_session:
            entry_liq = await get_token_liquidity(liq_session, CHAIN, token_address)
    except Exception:
        pass

    position = {
        "token_address": token_address,
        "token_symbol": symbol,
        "chain": CHAIN.upper(),
        "entry_price": result["entry_price"],
        "tokens_received": result["tokens_received"],
        "buy_amount_native": result["amount_spent"],
        "buy_tx_hash": result["tx_hash"],
        "pair_address": "",
        "entry_liquidity": entry_liq,
        "user_id": user_id,
    }
    await db.save_open_position(position)

    await notifier.notify_buy_executed(
        symbol=symbol,
        tokens_received=result["tokens_received"],
        entry_price=result["entry_price"],
        tx_hash=result["tx_hash"],
        chain=CHAIN.upper(),
        chat_id=user_id,
    )
    await notifier.send_message(
        f"📋 User <code>{user_id}</code> quick buy {symbol} — "
        f"{result['amount_spent']:.4f} {native}",
    )
    logger.info("Quick buy: %s, user=%d, tx=%s", token_address, user_id, result["tx_hash"])


async def _handle_status_refresh(query, user_id):
    positions = await monitor.get_positions_with_roi(user_id=user_id)
    if not positions:
        await query.edit_message_text("No open positions.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = ["📊 <b>Open Positions</b>\n"]
    keyboard = []
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
        tp = p["token_address"][:16]
        keyboard.append([
            InlineKeyboardButton(f"💰 Sell 25% {p['token_symbol']}", callback_data=f"sell:{tp}:25"),
            InlineKeyboardButton(f"💰 Sell 50%", callback_data=f"sell:{tp}:50"),
            InlineKeyboardButton(f"💰 Sell 100%", callback_data=f"sell:{tp}:100"),
        ])
    keyboard.append([InlineKeyboardButton("🔄 Refresh Positions", callback_data="status_refresh")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_wallet_refresh(query, user_id):
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        await query.edit_message_text("No wallet found.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    ut = await create_user_trader(user_id)
    balance = await ut.get_balance() if ut else 0.0
    auto_trade = "✅ Enabled" if wallet_data.get("auto_trade", 1) else "❌ Disabled"
    at_state = "off" if wallet_data.get("auto_trade", 1) else "on"
    at_label = "⚙️ AutoTrade Off" if wallet_data.get("auto_trade", 1) else "⚙️ AutoTrade On"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
            InlineKeyboardButton(at_label, callback_data=f"autotrade:{at_state}"),
        ],
        [
            InlineKeyboardButton("💸 Withdraw Instructions", callback_data="withdraw_prompt"),
            InlineKeyboardButton("🔑 Export (DM only)", callback_data="export_prompt"),
        ],
    ]

    await query.edit_message_text(
        f"👛 <b>Your Wallet</b>\n"
        f"Address: <code>{wallet_data['public_key']}</code>\n"
        f"Balance: {balance:.6f} {native}\n"
        f"Auto-Trade: {auto_trade}\n\n"
        f"Send {native} to the address above to fund your trading wallet.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_balance_refresh(query, user_id):
    ut = await create_user_trader(user_id)
    if ut is None:
        await query.edit_message_text("No wallet found.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    balance = await ut.get_balance()
    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="balance_refresh"),
            InlineKeyboardButton("👛 Wallet Details", callback_data="wallet_refresh"),
        ]
    ]
    await query.edit_message_text(
        f"💰 <b>Wallet Balance</b>\n{balance:.6f} {native} ({CHAIN})",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_portfolio_refresh(query, user_id):
    ut = await create_user_trader(user_id)
    if ut is None:
        await query.edit_message_text("No wallet found.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    wallet_balance = await ut.get_balance()
    positions = await monitor.get_positions_with_roi(user_id=user_id)

    total_invested = 0.0
    total_current_value = 0.0
    position_lines = []
    for p in positions:
        invested = p.get("buy_amount_native", 0)
        tokens = p.get("tokens_received", 0)
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
            arrow = "⚠️"
        elif roi >= 0:
            arrow = "🟢"
        else:
            arrow = "🔴"
        line = (
            f"{arrow} <b>{symbol}</b>\n"
            f"   Invested: {invested:.4f} {native}\n"
            f"   Value: {current_value:.4f} {native} ({pnl_sign}{pnl:.4f})\n"
            f"   ROI: {roi:+.2f}%"
        )
        if not price_ok:
            line += " ⚠️ price unavailable"
        position_lines.append(line)

    trades = await db.get_trade_history(limit=100, user_id=user_id)
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
        "━━━━━━━━━━━━━━━━━━━━━━",
        "💼 <b>PORTFOLIO OVERVIEW</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"💰 Wallet: {wallet_balance:.4f} {native}",
        f"📦 In Positions: {total_current_value:.4f} {native}",
        f"📊 Total Value: {total_portfolio:.4f} {native}",
        "",
        "<b>PnL Summary</b>",
        f"   Unrealized: {unrealized_sign}{unrealized_pnl:.4f} {native}",
        f"   Realized: {realized_sign}{realized_pnl:.4f} {native}",
        f"   Overall: {overall_sign}{overall_pnl:.4f} {native}",
        "",
        "<b>Stats</b>",
        f"   Open Positions: {len(positions)}",
        f"   Completed Trades: {total_trades}",
        f"   Win Rate: {win_rate:.1f}%",
    ]
    if position_lines:
        msg_parts.append("")
        msg_parts.append("━━━━━━━━━━━━━━━━━━━━━━")
        msg_parts.append("📋 <b>OPEN POSITIONS</b>")
        msg_parts.append("━━━━━━━━━━━━━━━━━━━━━━")
        for line in position_lines:
            msg_parts.append(line)
    else:
        msg_parts.append("")
        msg_parts.append("No open positions.")

    keyboard = [[InlineKeyboardButton("🔄 Refresh Portfolio", callback_data="portfolio_refresh")]]
    await query.edit_message_text(
        "\n".join(msg_parts),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_history_page(query, user_id, offset):
    all_trades = await db.get_trade_history(limit=100, user_id=user_id)
    if not all_trades:
        await query.edit_message_text("No completed trades.")
        return

    page_size = 5
    total = len(all_trades)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = offset // page_size + 1
    trades = all_trades[offset: offset + page_size]

    if not trades:
        await query.answer("No more trades.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = [f"📜 <b>Trade History</b> (page {current_page}/{total_pages})\n"]
    for t in trades:
        roi = t.get("roi_percent", 0)
        arrow = "🟢" if roi >= 0 else "🔴"
        dur = _format_duration(t.get("duration_seconds", 0))
        lines.append(
            f"{arrow} <b>{t['token_symbol']}</b> | ROI: {roi:+.2f}%\n"
            f"   Buy: {t['buy_amount_native']:.4f} → Sell: {t['sell_amount_native']:.4f} {native}\n"
            f"   Duration: {dur}\n"
        )

    keyboard_row = []
    if offset > 0:
        keyboard_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"history_page:{offset - page_size}"))
    else:
        keyboard_row.append(InlineKeyboardButton("⬅️ Prev", callback_data="noop"))
    keyboard_row.append(InlineKeyboardButton(f"Page {current_page}/{total_pages}", callback_data="noop"))
    if offset + page_size < total:
        keyboard_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"history_page:{offset + page_size}"))
    else:
        keyboard_row.append(InlineKeyboardButton("➡️ Next", callback_data="noop"))

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([keyboard_row]),
    )


async def _handle_autotrade_callback(query, user_id, state):
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        await query.edit_message_text("No wallet found.")
        return

    enable = state == "on"
    await db.set_auto_trade(user_id, enable)
    label = "enabled" if enable else "paused"
    icon = "✅" if enable else "❌"
    await query.answer(f"{icon} Auto-trading {label}.")

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    ut = await create_user_trader(user_id)
    balance = await ut.get_balance() if ut else 0.0
    auto_trade = "✅ Enabled" if enable else "❌ Disabled"
    at_state = "off" if enable else "on"
    at_label = "⚙️ AutoTrade Off" if enable else "⚙️ AutoTrade On"

    keyboard = [
        [
            InlineKeyboardButton("🔄 Refresh", callback_data="wallet_refresh"),
            InlineKeyboardButton(at_label, callback_data=f"autotrade:{at_state}"),
        ],
        [
            InlineKeyboardButton("💸 Withdraw Instructions", callback_data="withdraw_prompt"),
            InlineKeyboardButton("🔑 Export (DM only)", callback_data="export_prompt"),
        ],
    ]

    await query.edit_message_text(
        f"👛 <b>Your Wallet</b>\n"
        f"Address: <code>{wallet_data['public_key']}</code>\n"
        f"Balance: {balance:.6f} {native}\n"
        f"Auto-Trade: {auto_trade}\n\n"
        f"Send {native} to the address above to fund your trading wallet.",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    user_id = update.effective_user.id

    is_allowed = user_id == TELEGRAM_CHAT_ID or await db.is_user_allowed(user_id)
    if not is_allowed:
        await query.answer("⛔ Not authorized.", show_alert=True)
        return

    try:
        if data == "noop":
            return

        elif data == "status_refresh":
            await _handle_status_refresh(query, user_id)

        elif data.startswith("sell:"):
            parts = data.split(":")
            if len(parts) == 3:
                token_prefix = parts[1]
                percent = int(parts[2])
                await _handle_sell_callback(query, user_id, token_prefix, percent)

        elif data.startswith("buy_confirm:"):
            parts = data.split(":")
            if len(parts) == 3:
                token_prefix = parts[1]
                amount = float(parts[2])
                await _handle_buy_confirm_callback(query, user_id, token_prefix, amount)

        elif data == "buy_cancel":
            await query.edit_message_text("Buy cancelled.")

        elif data == "wallet_refresh":
            await _handle_wallet_refresh(query, user_id)

        elif data.startswith("autotrade:"):
            state = data.split(":")[1]
            await _handle_autotrade_callback(query, user_id, state)

        elif data == "withdraw_prompt":
            await query.edit_message_text(
                "💸 <b>Withdraw Instructions</b>\n\n"
                "Use: <code>/withdraw &lt;amount&gt; &lt;destination_address&gt;</code>\n"
                "Example: <code>/withdraw 0.5 ABC...XYZ</code>",
                parse_mode="HTML",
            )

        elif data == "export_prompt":
            await query.answer("Please DM me /export for security", show_alert=True)

        elif data == "portfolio_refresh":
            await _handle_portfolio_refresh(query, user_id)

        elif data.startswith("history_page:"):
            offset = int(data.split(":")[1])
            await _handle_history_page(query, user_id, offset)

        elif data == "balance_refresh":
            await _handle_balance_refresh(query, user_id)

        elif data.startswith("pos_detail:"):
            token_prefix = data.split(":")[1]
            positions = await monitor.get_positions_with_roi(user_id=user_id)
            pos = None
            for p in positions:
                if p["token_address"][:16] == token_prefix:
                    pos = p
                    break
            if pos is None:
                await query.edit_message_text("Position not found.")
                return
            native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            roi = pos.get("roi", 0)
            arrow = "🟢" if roi >= 0 else "🔴"
            tp = pos["token_address"][:16]
            keyboard = [
                [
                    InlineKeyboardButton("💰 Sell 25%", callback_data=f"sell:{tp}:25"),
                    InlineKeyboardButton("💰 Sell 50%", callback_data=f"sell:{tp}:50"),
                    InlineKeyboardButton("💰 Sell 100%", callback_data=f"sell:{tp}:100"),
                ],
                [InlineKeyboardButton("🔙 Back to Positions", callback_data="status_refresh")],
            ]
            await query.edit_message_text(
                f"{arrow} <b>{pos['token_symbol']}</b>\n"
                f"Address: <code>{pos['token_address']}</code>\n"
                f"ROI: {roi:+.2f}%\n"
                f"Entry: {pos['entry_price']:.10f} {native}\n"
                f"Current: {pos.get('current_price', 0):.10f} {native}\n"
                f"Tokens: {pos['tokens_received']:.4f}\n"
                f"Invested: {pos['buy_amount_native']:.4f} {native}",
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data.startswith("info_refresh:"):
            token_address = data.split(":", 1)[1]
            await query.edit_message_text("\U0001f50d Refreshing...")
            async with aiohttp.ClientSession() as session:
                info_data = await fetch_token_research(session, CHAIN, token_address)
            if info_data is None:
                await query.edit_message_text("\u274c Could not refresh token data.")
                return
            native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            hp_icon = "\U0001f6ab HONEYPOT" if info_data["is_honeypot"] else "\u2705 Safe" if info_data["honeypot_checked"] else "\u26a0\ufe0f Unknown"
            pc = info_data["price_change_24h"]
            pc_icon = "\U0001f7e2" if pc >= 0 else "\U0001f534"
            pc_str = f"{pc_icon} {pc:+.2f}%" if pc != 0 else "\u2014"
            socials = info_data.get("social_links", {})
            social_parts = []
            social_icons = {"website": "\U0001f310", "twitter": "\U0001f426", "telegram": "\U0001f4ac", "discord": "\U0001f3ae"}
            for key, icon in social_icons.items():
                url = socials.get(key, "")
                if url:
                    social_parts.append(f'<a href="{url}">{icon} {key.title()}</a>')
            social_str = " | ".join(social_parts) if social_parts else "None found"
            txns = info_data.get("txns_24h", {})
            buys_count = txns.get("buys", 0)
            sells_count = txns.get("sells", 0)
            chain_slug = {"SOL": "solana", "ETH": "ether", "BSC": "bsc"}.get(CHAIN.upper(), "solana")
            dextools_url = f"https://www.dextools.io/app/en/{chain_slug}/pair-explorer/{info_data.get('pair_address') or token_address}"
            ds_url = info_data.get("dexscreener_url") or f"https://dexscreener.com/{chain_slug}/{token_address}"
            msg = (
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001f52c <b>TOKEN INFO</b>\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
                f"\U0001fa99 <b>{info_data['name']}</b> ({info_data['symbol']})\n"
                f"\U0001f4c4 <code>{token_address}</code>\n"
                f"\u26d3 {CHAIN.upper()} | Age: {info_data['age_str']}\n\n"
                f"\U0001f4b0 <b>Price:</b> ${info_data['price_usd']:.8g} ({info_data['price_native']:.10f} {native})\n"
                f"\U0001f4ca <b>MCap:</b> ${info_data['market_cap']:,.0f}\n"
                f"\U0001f4a7 <b>Liquidity:</b> ${info_data['liquidity']:,.0f}\n"
                f"\U0001f4c8 <b>Volume 24h:</b> ${info_data['volume_24h']:,.0f}\n"
                f"\U0001f4c9 <b>24h Change:</b> {pc_str}\n"
                f"\U0001f504 <b>Txns 24h:</b> {buys_count} buys / {sells_count} sells\n"
            )
            if info_data["holders"] > 0:
                msg += f"\U0001f465 <b>Holders:</b> {info_data['holders']:,}\n"
            msg += (
                f"\n\U0001f6e1 <b>Safety:</b> {hp_icon}\n"
                f"\U0001f4b8 <b>Tax:</b> Buy {info_data['buy_tax']:.1f}% / Sell {info_data['sell_tax']:.1f}%\n"
                f"\u2b50 <b>Score:</b> {info_data['score_bar']}\n"
                f"\n\U0001f517 {social_str}\n"
                f'\n<a href="{dextools_url}">\U0001f4ca DexTools</a> | <a href="{ds_url}">\U0001f4ca DexScreener</a>\n'
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
            )
            tp = token_address[:16]
            keyboard = [
                [
                    InlineKeyboardButton("\U0001f6d2 Buy 0.1 SOL", callback_data=f"quickbuy:{tp}:0.1"),
                    InlineKeyboardButton("\U0001f6d2 Buy 0.5 SOL", callback_data=f"quickbuy:{tp}:0.5"),
                ],
                [
                    InlineKeyboardButton("\U0001f504 Refresh", callback_data=f"info_refresh:{token_address}"),
                ],
            ]
            await query.edit_message_text(
                msg, parse_mode="HTML", disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data.startswith("quickbuy:"):
            parts = data.split(":")
            if len(parts) == 3:
                token_prefix = parts[1]
                amount = float(parts[2])
                await _handle_quickbuy_callback(query, user_id, token_prefix, amount)

        elif data == "stats_refresh":
            native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            stats_all = await db.get_trade_stats(user_id=user_id)
            stats_7d = await db.get_trade_stats(user_id=user_id, days=7)
            stats_30d = await db.get_trade_stats(user_id=user_id, days=30)

            if stats_all["total_trades"] == 0:
                await query.edit_message_text("No completed trades to analyze.")
                return

            msg = _format_stats_message(stats_all, stats_7d, stats_30d, native, "📊 YOUR TRADING STATS")
            keyboard = [[InlineKeyboardButton("🔄 Refresh Stats", callback_data="stats_refresh")]]
            await query.edit_message_text(
                msg,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "lowcaps_refresh":
            recent = await db.get_recent_detected_tokens(limit=10)
            if not recent:
                await query.edit_message_text("No tokens detected yet.")
                return

            native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
            lines = [
                "━━━━━━━━━━━━━━━━━━━━━━",
                f"🔍 <b>RECENT LOWCAPS</b> (last {len(recent)})",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            keyboard = []
            for t in recent:
                lines.append(
                    f"\n🪙 <b>{t.get('name', '?')}</b> ({t.get('symbol', '?')})\n"
                    f"   📄 <code>{t.get('contract_address', '')}</code>\n"
                    f"   💰 MCap: ${t.get('market_cap', 0):,.0f} | 💧 Liq: ${t.get('liquidity', 0):,.0f}\n"
                    f"   📈 Vol: ${t.get('volume_24h', 0):,.0f} | 🧾 Tax: {t.get('buy_tax', 0):.1f}%/{t.get('sell_tax', 0):.1f}%\n"
                    f"   🕐 {t.get('detected_at', '?')}"
                )
                tp = t.get("contract_address", "")[:16]
                keyboard.append([
                    InlineKeyboardButton(f"🛒 Buy 0.1 SOL {t.get('symbol', '?')}", callback_data=f"quickbuy:{tp}:0.1"),
                    InlineKeyboardButton(f"🛒 Buy 0.5 SOL", callback_data=f"quickbuy:{tp}:0.5"),
                ])
            keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="lowcaps_refresh")])

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        elif data == "config_show":
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
                f"Monitor Interval: {MONITOR_INTERVAL}s"
                f"\nWhale Copy Trade: {'Enabled' if WHALE_COPY_ENABLED else 'Disabled'}"
                f"\nCopy Amount: {WHALE_COPY_AMOUNT} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}"
                f"\nAlert Broadcast: {'Enabled' if alerts_enabled else 'Disabled'}"
                f"\nSell Tiers: {SELL_TIERS_RAW if SELL_TIERS_RAW else 'None (full sell at TP)'}"
                f"\n\n<b>Risk Management</b>\n"
                f"Max Positions: {MAX_OPEN_POSITIONS} per user\n"
                f"Max Daily Loss: {MAX_DAILY_LOSS} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}\n"
                f"Max Buy Amount: {MAX_BUY_AMOUNT} {NATIVE_SYMBOL.get(CHAIN.upper(), 'SOL')}"
                f"\n\n<b>External API</b>\n"
                f"API Server: {'Enabled (port ' + str(API_PORT) + ')' if API_ENABLED else 'Disabled'}"
            )
            await query.edit_message_text(msg, parse_mode="HTML")

        else:
            logger.warning("Unknown callback data: %s", data)

    except Exception as exc:
        logger.error("Callback error (data=%s, user=%d): %s", data, user_id, exc)
        try:
            await query.edit_message_text(f"❌ Error: {str(exc)[:200]}")
        except Exception:
            pass


async def cmd_alerts(update, context):
    """Toggle lowcap alert broadcasting."""
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    global alerts_enabled

    if not context.args:
        status = "✅ ON" if alerts_enabled else "❌ OFF"
        await update.message.reply_html(
            f"📢 <b>Lowcap Alert Broadcast</b>\n\n"
            f"Status: {status}\n\n"
            f"Usage: <code>/alerts on</code> or <code>/alerts off</code>\n\n"
            f"When OFF, the scanner still runs and auto-trades — it just doesn't spam token details to chat.\n"
            f"Use <code>/lowcaps</code> to see recent detections on demand."
        )
        return

    arg = context.args[0].lower()
    if arg in ("on", "yes", "1", "true"):
        alerts_enabled = True
        await update.message.reply_html("📢 Lowcap alerts <b>enabled</b>. New detections will be broadcast to all chats.")
    elif arg in ("off", "no", "0", "false"):
        alerts_enabled = False
        await update.message.reply_html("🔇 Lowcap alerts <b>disabled</b>. Scanner still runs silently. Use /lowcaps to check manually.")
    else:
        await update.message.reply_html("Usage: <code>/alerts on|off</code>")


async def cmd_lowcaps(update, context):
    """Show recently detected lowcap tokens on demand."""
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    limit = 10
    if context.args:
        try:
            limit = int(context.args[0])
            limit = max(1, min(limit, 25))
        except ValueError:
            pass

    recent = await db.get_recent_detected_tokens(limit=limit)

    if not recent:
        await update.message.reply_text("No tokens detected yet. Start the scanner with /start.")
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🔍 <b>RECENT LOWCAPS</b> (last {len(recent)})",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    keyboard = []
    for t in recent:
        score = t.get("score", "?")
        lines.append(
            f"\n🪙 <b>{t.get('name', '?')}</b> ({t.get('symbol', '?')})\n"
            f"   📄 <code>{t.get('contract_address', '')}</code>\n"
            f"   💰 MCap: ${t.get('market_cap', 0):,.0f} | 💧 Liq: ${t.get('liquidity', 0):,.0f}\n"
            f"   📈 Vol: ${t.get('volume_24h', 0):,.0f} | 🧾 Tax: {t.get('buy_tax', 0):.1f}%/{t.get('sell_tax', 0):.1f}%\n"
            f"   🕐 {t.get('detected_at', '?')}"
        )
        tp = t.get("contract_address", "")[:16]
        keyboard.append([
            InlineKeyboardButton(f"🛒 Buy 0.1 SOL {t.get('symbol', '?')}", callback_data=f"quickbuy:{tp}:0.1"),
            InlineKeyboardButton(f"🛒 Buy 0.5 SOL", callback_data=f"quickbuy:{tp}:0.5"),
        ])

    keyboard.append([InlineKeyboardButton("🔄 Refresh", callback_data="lowcaps_refresh")])

    await update.message.reply_html(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_backtest(update, context):
    """Replay scoring strategy against historical scan data."""
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("Admin only.")
        return

    days = 7
    if context.args:
        try:
            days = int(context.args[0])
            days = max(1, min(days, 90))
        except ValueError:
            pass

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    data = await db.get_backtest_data(days=days)
    summary = data["summary"]

    if not summary or summary.get("total_scanned", 0) == 0:
        await update.message.reply_text(f"No scan history for the last {days} days. Start the bot and scan some tokens first.")
        return

    msg_parts = [
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        f"\U0001f9ea <b>BACKTEST \u2014 Last {days} Days</b>",
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501",
        "",
        "<b>Scan Summary</b>",
        f"   Total Scanned: {summary['total_scanned']}",
        f"   Bought: {summary['total_bought'] or 0}",
        f"   Avg Score: {summary['avg_score']:.1f}",
        f"   Score Range: {summary['min_score']} \u2013 {summary['max_score']}",
        f"   Current MIN_SCORE: {MIN_SCORE}",
    ]

    ranges = data.get("score_ranges", [])
    if ranges:
        msg_parts.extend(["", "<b>Score Distribution</b>"])
        for r in ranges:
            bar_len = min(int(r["total_scanned"] / max(summary["total_scanned"], 1) * 20), 20)
            bar = "\u2588" * bar_len
            msg_parts.append(f"   {r['score_range']}: {r['total_scanned']} tokens {bar}")

    sims = data.get("simulations", [])
    if sims:
        msg_parts.extend(["", "<b>MIN_SCORE Simulations</b>"])
        msg_parts.append("   Score | Would Buy | Traded | Win% | Avg ROI")
        for s in sims:
            marker = " \u25c0" if s["threshold"] == MIN_SCORE else ""
            win_rate = f"{s['win_rate']:.0f}%" if s['traded'] > 0 else "N/A"
            avg_roi = f"{s['avg_roi']:+.1f}%" if s['traded'] > 0 else "N/A"
            msg_parts.append(
                f"   \u2265{s['threshold']:3d}  |  {s['would_buy']:5d}    |  {s['traded']:4d}   | {win_rate:>4s} | {avg_roi:>7s}{marker}"
            )

    outcomes = data.get("trade_outcomes", [])
    if outcomes:
        msg_parts.extend(["", f"<b>Trade Outcomes by Score (last {days}d)</b>"])
        for o in outcomes[:15]:
            icon = "\U0001f7e2" if o["roi_percent"] > 0 else "\U0001f534"
            pnl = o["sell_amount_native"] - o["buy_amount_native"]
            sign = "+" if pnl >= 0 else ""
            msg_parts.append(f"   {icon} [{o['score']}] {o['symbol']}: {o['roi_percent']:+.1f}% ({sign}{pnl:.4f} {native})")

    await update.message.reply_html("\n".join(msg_parts))


async def cmd_blacklist(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("\U0001f512 Admin only.")
        return

    if not context.args:
        items = await db.get_blacklist()
        if not items:
            await update.message.reply_text("Blacklist is empty.")
            return
        lines = ["\U0001f6ab <b>Token Blacklist</b>\n"]
        for item in items:
            lines.append(f"\u2022 <code>{item['token_address'][:12]}\u2026</code> {item.get('reason', '')}")
        await update.message.reply_html("\n".join(lines))
        return

    if context.args[0].lower() == "remove" and len(context.args) >= 2:
        removed = await db.remove_from_blacklist(context.args[1], CHAIN.upper())
        msg = "\u2705 Removed from blacklist." if removed else "Not found in blacklist."
        await update.message.reply_text(msg)
        return

    token_addr = context.args[0]
    reason = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    added = await db.add_to_blacklist(token_addr, CHAIN.upper(), reason, update.effective_user.id)
    msg = "\U0001f6ab Added to blacklist." if added else "Already blacklisted."
    await update.message.reply_text(msg)


async def cmd_whitelist(update, context):
    await _register_chat(update)
    if not _is_admin(update):
        await update.message.reply_text("\U0001f512 Admin only.")
        return

    if not context.args:
        items = await db.get_whitelist()
        if not items:
            await update.message.reply_text("Whitelist is empty.")
            return
        lines = ["\u2705 <b>Token Whitelist</b>\n"]
        for item in items:
            lines.append(f"\u2022 <code>{item['token_address'][:12]}\u2026</code> {item.get('label', '')}")
        await update.message.reply_html("\n".join(lines))
        return

    if context.args[0].lower() == "remove" and len(context.args) >= 2:
        removed = await db.remove_from_whitelist(context.args[1], CHAIN.upper())
        msg = "\u2705 Removed from whitelist." if removed else "Not found in whitelist."
        await update.message.reply_text(msg)
        return

    token_addr = context.args[0]
    label = " ".join(context.args[1:]) if len(context.args) > 1 else ""
    added = await db.add_to_whitelist(token_addr, CHAIN.upper(), label, update.effective_user.id)
    msg = "\u2705 Added to whitelist." if added else "Already whitelisted."
    await update.message.reply_text(msg)


async def cmd_sellall(update, context):
    """Dump all open positions immediately."""
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await update.message.reply_text("No wallet found.")
        return

    positions = await db.get_open_positions(user_id=user_id)
    if not positions:
        await update.message.reply_text("No open positions to sell.")
        return

    if not context.args or context.args[0].lower() != "confirm":
        await update.message.reply_html(
            f"\u26a0\ufe0f <b>PANIC SELL</b>\n\n"
            f"This will sell ALL {len(positions)} open position(s) at market price.\n\n"
            f"To confirm: <code>/sellall confirm</code>"
        )
        return

    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    await update.message.reply_html(f"\U0001f534 <b>Selling all {len(positions)} positions...</b>")

    sold = 0
    failed = 0
    total_received = 0.0

    for pos in positions:
        try:
            token_address = pos["token_address"]
            chain = pos["chain"]
            symbol = pos["token_symbol"]

            if chain.upper() == "SOL":
                ui_balance, decimals = await user_trader.get_token_balance(token_address)
                tokens_raw = int(ui_balance * (10**decimals)) if decimals > 0 else int(ui_balance * 1e9)
            else:
                continue

            if tokens_raw <= 0:
                exit_data = {
                    "exit_price": 0, "sell_amount_native": 0,
                    "profit_usd": None, "roi_percent": -100,
                    "sell_tx_hash": "zero_balance", "duration_seconds": 0,
                }
                await db.close_position(token_address, chain, exit_data, user_id=user_id)
                sold += 1
                continue

            result = await user_trader.sell_token(token_address, tokens_raw, decimals)
            if result:
                entry_price = pos["entry_price"]
                roi = ((result["exit_price"] - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                exit_data = {
                    "exit_price": result["exit_price"],
                    "sell_amount_native": result["native_received"],
                    "profit_usd": None,
                    "roi_percent": roi,
                    "sell_tx_hash": result["tx_hash"],
                    "duration_seconds": 0,
                }
                await db.close_position(token_address, chain, exit_data, user_id=user_id)
                total_received += result["native_received"]
                sold += 1
            else:
                failed += 1
        except Exception as exc:
            logger.error("Sellall error for %s: %s", pos.get("token_symbol"), exc)
            failed += 1

    await update.message.reply_html(
        f"\U0001f534 <b>Panic Sell Complete</b>\n"
        f"Sold: {sold}/{len(positions)}\n"
        f"Failed: {failed}\n"
        f"Total received: {total_received:.6f} {native}"
    )


# ---------------------------------------------------------------------------
# DCA background loop
# ---------------------------------------------------------------------------

async def dca_loop():
    global is_running
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    logger.info("DCA loop started")

    while is_running:
        try:
            due_orders = await db.get_active_dca_orders()
            for order in due_orders:
                if not is_running:
                    break
                try:
                    uid = order["user_id"]
                    token_addr = order["token_address"]
                    amount = order["amount_per_buy"]

                    user_trader = await create_user_trader(uid)
                    if user_trader is None:
                        continue

                    balance = await user_trader.get_balance()
                    if balance < amount + 0.005:
                        await notifier.send_to_user(uid, f"⚠️ DCA #{order['id']}: insufficient balance ({balance:.4f} {native})")
                        continue

                    logger.info("DCA buy #%d: %s for user %d (%.4f %s, split %d/%d)",
                                order["id"], token_addr[:12], uid, amount, native,
                                order["splits_done"] + 1, order["splits_total"])

                    result = await user_trader.buy_token(token_addr, amount)
                    if result:
                        existing = await db.is_token_already_bought(token_addr, CHAIN.upper(), uid)
                        if not existing:
                            position = {
                                "token_address": token_addr,
                                "token_symbol": order.get("token_symbol", token_addr[:8]),
                                "chain": CHAIN.upper(),
                                "entry_price": result["entry_price"],
                                "tokens_received": result["tokens_received"],
                                "buy_amount_native": result["amount_spent"],
                                "buy_tx_hash": result["tx_hash"],
                                "pair_address": "",
                                "entry_liquidity": 0,
                                "user_id": uid,
                            }
                            await db.save_open_position(position)

                        await db.advance_dca_order(order["id"])

                        remaining = order["splits_total"] - order["splits_done"] - 1
                        await notifier.send_to_user(
                            uid,
                            f"📊 DCA #{order['id']} buy {order['splits_done']+1}/{order['splits_total']}: "
                            f"{amount:.4f} {native} → {order.get('token_symbol', token_addr[:8])}\n"
                            f"{'✅ DCA complete!' if remaining == 0 else f'{remaining} buys remaining'}"
                        )
                    else:
                        await notifier.send_to_user(uid, f"❌ DCA #{order['id']} buy failed. Will retry next interval.")

                except Exception as exc:
                    logger.error("DCA order #%d error: %s", order.get("id", 0), exc)

        except Exception as exc:
            logger.error("DCA loop error: %s", exc)

        await asyncio.sleep(30)


# ---------------------------------------------------------------------------
# Limit order background loop
# ---------------------------------------------------------------------------

async def limit_order_loop():
    global is_running
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    logger.info("Limit order loop started")

    while is_running:
        try:
            orders = await db.get_active_limit_orders()
            for order in orders:
                if not is_running:
                    break
                try:
                    token_addr = order["token_address"]
                    uid = order["user_id"]

                    current_price = await trader.get_token_price_via_jupiter(token_addr)
                    if current_price <= 0:
                        continue

                    target = order["target_price"]
                    triggered = False

                    if order["side"] == "buy" and current_price <= target:
                        triggered = True
                    elif order["side"] == "sell" and current_price >= target:
                        triggered = True

                    if not triggered:
                        continue

                    user_trader = await create_user_trader(uid)
                    if user_trader is None:
                        continue

                    if order["side"] == "buy":
                        amount = order["amount"]
                        result = await user_trader.buy_token(token_addr, amount)
                        if result:
                            existing = await db.is_token_already_bought(token_addr, CHAIN.upper(), uid)
                            if not existing:
                                position = {
                                    "token_address": token_addr,
                                    "token_symbol": order.get("token_symbol", token_addr[:8]),
                                    "chain": CHAIN.upper(),
                                    "entry_price": result["entry_price"],
                                    "tokens_received": result["tokens_received"],
                                    "buy_amount_native": result["amount_spent"],
                                    "buy_tx_hash": result["tx_hash"],
                                    "pair_address": "",
                                    "entry_liquidity": 0,
                                    "user_id": uid,
                                }
                                await db.save_open_position(position)

                            await db.fill_limit_order(order["id"], result["tx_hash"])
                            await notifier.send_to_user(
                                uid,
                                f"📋 Limit BUY filled! #{order['id']}\n"
                                f"{order.get('token_symbol', token_addr[:8])} @ {current_price:.10f} {native}\n"
                                f"Spent: {amount:.4f} {native}"
                            )

                    elif order["side"] == "sell":
                        sell_pct = order["amount"]
                        ui_balance, decimals = await user_trader.get_token_balance(token_addr)
                        if ui_balance <= 0:
                            continue
                        sell_ui = ui_balance * (sell_pct / 100)
                        sell_raw = int(sell_ui * (10 ** decimals)) if decimals > 0 else int(sell_ui * 1e9)

                        if sell_raw <= 0:
                            continue

                        result = await user_trader.sell_token(token_addr, sell_raw, decimals)
                        if result:
                            await db.fill_limit_order(order["id"], result["tx_hash"])

                            if sell_pct == 100:
                                positions = await db.get_open_positions(user_id=uid)
                                for pos in positions:
                                    if pos["token_address"].lower() == token_addr.lower():
                                        entry_price = pos["entry_price"]
                                        roi = ((result["exit_price"] - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                                        exit_data = {
                                            "exit_price": result["exit_price"],
                                            "sell_amount_native": result["native_received"],
                                            "profit_usd": None,
                                            "roi_percent": roi,
                                            "sell_tx_hash": result["tx_hash"],
                                            "duration_seconds": 0,
                                        }
                                        await db.close_position(token_addr, CHAIN.upper(), exit_data, user_id=uid)
                                        break

                            await notifier.send_to_user(
                                uid,
                                f"📋 Limit SELL filled! #{order['id']}\n"
                                f"{order.get('token_symbol', token_addr[:8])} @ {current_price:.10f} {native}\n"
                                f"Received: {result['native_received']:.6f} {native}"
                            )

                except Exception as exc:
                    logger.error("Limit order #%d error: %s", order.get("id", 0), exc)

        except Exception as exc:
            logger.error("Limit order loop error: %s", exc)

        await asyncio.sleep(15)


# ---------------------------------------------------------------------------
# /pnl command
# ---------------------------------------------------------------------------

async def cmd_pnl(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    days = 1
    if context.args:
        try:
            days = int(context.args[0])
            days = max(1, min(days, 30))
        except ValueError:
            pass

    report = await db.get_pnl_report(user_id, days=days)
    if report is None:
        await update.message.reply_text("Could not generate report.")
        return

    balance = 0.0
    try:
        user_trader = await create_user_trader(user_id)
        if user_trader:
            balance = await user_trader.get_balance()
    except Exception:
        pass

    period = "Today" if days == 1 else f"Last {days} days"
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "📊 <b>PnL REPORT</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📅 {period}",
        "",
    ]

    if report["trades"] > 0:
        pnl_emoji = "🟢" if report["net_pnl"] >= 0 else "🔴"
        lines.append("<b>Activity:</b>")
        lines.append(f"  Trades closed: {report['trades']}")
        lines.append(f"  Wins/Losses: {report['wins']}/{report['losses']}")
        lines.append(f"  Win rate: {report['win_rate']:.0f}%")
        lines.append(f"  {pnl_emoji} Net P&L: {report['net_pnl']:+.6f} {native}")
        if report["best_trade"]:
            lines.append(f"  🏆 Best: {report['best_trade']['symbol']} ({report['best_trade']['roi']:+.1f}%)")
        if report["worst_trade"]:
            lines.append(f"  💀 Worst: {report['worst_trade']['symbol']} ({report['worst_trade']['roi']:+.1f}%)")
        lines.append("")
    else:
        lines.append("No trades closed in this period.\n")

    lines.append(f"<b>Open Positions:</b> {report['open_count']}")
    if report["open_count"] > 0:
        lines.append(f"  Total invested: {report['total_invested']:.4f} {native}")

    lines.append(f"\n💰 Wallet balance: {balance:.6f} {native}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    await update.message.reply_html("\n".join(lines))


# ---------------------------------------------------------------------------
# /compound command
# ---------------------------------------------------------------------------

async def cmd_compound(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    user_cfg = await db.get_effective_config(user_id)

    if not context.args:
        fund = await db.get_compound_fund(user_id)
        status = "🟢 ON" if user_cfg.get("compound_enabled") else "🔴 OFF"
        await update.message.reply_html(
            f"🔄 <b>Auto-Compound</b>\n\n"
            f"Status: {status}\n"
            f"Reinvest: {user_cfg.get('compound_percent', 50)}% of profits\n"
            f"Fund balance: {fund:.6f} {native}\n\n"
            f"<code>/compound on</code> — enable\n"
            f"<code>/compound off</code> — disable\n"
            f"<code>/compound percent 75</code> — set reinvest %\n"
            f"<code>/compound withdraw</code> — withdraw fund to wallet"
        )
        return

    subcmd = context.args[0].lower()

    if subcmd == "on":
        await db.upsert_user_setting(user_id, "compound_enabled", 1)
        await update.message.reply_text("🟢 Auto-compound enabled.")
    elif subcmd == "off":
        await db.upsert_user_setting(user_id, "compound_enabled", 0)
        await update.message.reply_text("🔴 Auto-compound disabled.")
    elif subcmd == "percent" and len(context.args) >= 2:
        try:
            pct = int(context.args[1])
            if 1 <= pct <= 100:
                await db.upsert_user_setting(user_id, "compound_percent", pct)
                await update.message.reply_text(f"✅ Compound percent set to {pct}%.")
            else:
                await update.message.reply_text("Must be 1-100.")
        except ValueError:
            await update.message.reply_text("Invalid number.")
    elif subcmd == "withdraw":
        fund = await db.get_compound_fund(user_id)
        if fund < 0.001:
            await update.message.reply_text("Compound fund is empty.")
        else:
            await db.deduct_compound_funds(user_id, fund)
            await update.message.reply_html(
                f"✅ Withdrew {fund:.6f} {native} from compound fund to your wallet balance."
            )
    else:
        await update.message.reply_text("Unknown subcommand. Use on, off, percent, or withdraw.")


# ---------------------------------------------------------------------------
# /dca command
# ---------------------------------------------------------------------------

async def cmd_dca(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if not context.args:
        await update.message.reply_html(
            "📊 <b>DCA Mode</b>\n\n"
            "Usage: <code>/dca &lt;token&gt; &lt;total_sol&gt; &lt;splits&gt; &lt;interval_min&gt;</code>\n"
            "Example: <code>/dca EPjF...1v 0.3 3 5</code>\n"
            "(Buy 0.3 SOL in 3 orders, 5min apart)\n\n"
            "<code>/dca list</code> — view active orders\n"
            "<code>/dca cancel &lt;id&gt;</code> — cancel order"
        )
        return

    if context.args[0].lower() == "list":
        orders = await db.get_user_dca_orders(user_id)
        if not orders:
            await update.message.reply_text("No active DCA orders.")
            return
        lines = ["📊 <b>Active DCA Orders</b>\n"]
        for o in orders:
            lines.append(
                f"#{o['id']} {o['token_symbol']} — "
                f"{o['splits_done']}/{o['splits_total']} buys "
                f"({o['amount_per_buy']:.4f} {native} each, "
                f"every {o['interval_seconds']//60}min)"
            )
        await update.message.reply_html("\n".join(lines))
        return

    if context.args[0].lower() == "cancel":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /dca cancel <order_id>")
            return
        order_id = int(context.args[1])
        cancelled = await db.cancel_dca_order(order_id, user_id)
        await update.message.reply_text("✅ DCA order cancelled." if cancelled else "❌ Order not found or not yours.")
        return

    if len(context.args) < 4:
        await update.message.reply_text("Usage: /dca <token> <total_sol> <splits> <interval_min>")
        return

    token_address = context.args[0]
    try:
        total_amount = float(context.args[1])
        splits = int(context.args[2])
        interval_min = int(context.args[3])
    except ValueError:
        await update.message.reply_text("Invalid numbers. Check your input.")
        return

    if total_amount <= 0 or splits < 2 or splits > 20 or interval_min < 1 or interval_min > 1440:
        await update.message.reply_text("Limits: amount > 0, splits 2-20, interval 1-1440 min")
        return

    amount_per = total_amount / splits

    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        await update.message.reply_text("No wallet found.")
        return

    order_id = await db.create_dca_order(
        user_id=user_id,
        token_address=token_address,
        token_symbol=token_address[:8],
        chain=CHAIN.upper(),
        total_amount=total_amount,
        splits=splits,
        interval_seconds=interval_min * 60,
    )

    if order_id:
        await update.message.reply_html(
            f"📊 <b>DCA Order Created</b> (#{order_id})\n"
            f"Token: <code>{token_address}</code>\n"
            f"Total: {total_amount:.4f} {native}\n"
            f"Splits: {splits} × {amount_per:.4f} {native}\n"
            f"Interval: every {interval_min} min\n"
            f"First buy starts immediately."
        )
    else:
        await update.message.reply_text("❌ Failed to create DCA order.")


# ---------------------------------------------------------------------------
# /limit command
# ---------------------------------------------------------------------------

async def cmd_limit(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return

    user_id = update.effective_user.id
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if not context.args:
        await update.message.reply_html(
            "📋 <b>Limit Orders</b>\n\n"
            "Buy: <code>/limit buy &lt;token&gt; &lt;sol&gt; &lt;price&gt;</code>\n"
            "Sell: <code>/limit sell &lt;token&gt; &lt;percent&gt; &lt;price&gt;</code>\n"
            "List: <code>/limit list</code>\n"
            "Cancel: <code>/limit cancel &lt;id&gt;</code>\n\n"
            "Price is in SOL per token."
        )
        return

    subcmd = context.args[0].lower()

    if subcmd == "list":
        orders = await db.get_user_limit_orders(user_id)
        if not orders:
            await update.message.reply_text("No active limit orders.")
            return
        lines = ["📋 <b>Active Limit Orders</b>\n"]
        for o in orders:
            side_emoji = "🟢" if o["side"] == "buy" else "🔴"
            amount_str = f"{o['amount']:.4f} {native}" if o["side"] == "buy" else f"{o['amount']:.0f}%"
            lines.append(
                f"{side_emoji} #{o['id']} {o['side'].upper()} {o.get('token_symbol', o['token_address'][:8])} "
                f"— {amount_str} @ {o['target_price']:.10f} {native}"
            )
        await update.message.reply_html("\n".join(lines))
        return

    if subcmd == "cancel":
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /limit cancel <order_id>")
            return
        order_id = int(context.args[1])
        cancelled = await db.cancel_limit_order(order_id, user_id)
        await update.message.reply_text("✅ Limit order cancelled." if cancelled else "❌ Order not found or not yours.")
        return

    if subcmd in ("buy", "sell"):
        if len(context.args) < 4:
            await update.message.reply_text(f"Usage: /limit {subcmd} <token> <amount> <price>")
            return

        token_address = context.args[1]
        try:
            amount = float(context.args[2])
            target_price = float(context.args[3])
        except ValueError:
            await update.message.reply_text("Invalid numbers.")
            return

        if amount <= 0 or target_price <= 0:
            await update.message.reply_text("Amount and price must be positive.")
            return

        if subcmd == "sell" and (amount < 1 or amount > 100):
            await update.message.reply_text("Sell percent must be 1-100.")
            return

        order_id = await db.create_limit_order(
            user_id=user_id,
            token_address=token_address,
            token_symbol=token_address[:8],
            chain=CHAIN.upper(),
            side=subcmd,
            amount=amount,
            target_price=target_price,
        )

        if order_id:
            side_label = f"Buy {amount:.4f} {native}" if subcmd == "buy" else f"Sell {amount:.0f}%"
            await update.message.reply_html(
                f"📋 <b>Limit Order Created</b> (#{order_id})\n"
                f"Side: {subcmd.upper()}\n"
                f"Token: <code>{token_address}</code>\n"
                f"{side_label} @ {target_price:.10f} {native}/token"
            )
        else:
            await update.message.reply_text("❌ Failed to create limit order.")
        return

    await update.message.reply_text("Unknown subcommand. Use buy, sell, list, or cancel.")


# ---------------------------------------------------------------------------
# /orders command — unified view
# ---------------------------------------------------------------------------

async def cmd_orders(update, context):
    await _register_chat(update)
    if await _reject_unauthorized(update):
        return
    user_id = update.effective_user.id
    dca_orders = await db.get_user_dca_orders(user_id)
    limit_orders = await db.get_user_limit_orders(user_id)

    if not dca_orders and not limit_orders:
        await update.message.reply_text("No active orders.")
        return

    lines = ["📋 <b>Your Active Orders</b>\n"]
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")

    if dca_orders:
        lines.append("<b>DCA Orders:</b>")
        for o in dca_orders:
            lines.append(f"  📊 #{o['id']} {o.get('token_symbol', '?')} — {o['splits_done']}/{o['splits_total']} ({o['amount_per_buy']:.4f} {native}/buy)")

    if limit_orders:
        lines.append("\n<b>Limit Orders:</b>")
        for o in limit_orders:
            side_emoji = "🟢" if o["side"] == "buy" else "🔴"
            lines.append(f"  {side_emoji} #{o['id']} {o['side'].upper()} {o.get('token_symbol', '?')} @ {o['target_price']:.10f}")

    await update.message.reply_html("\n".join(lines))


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
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("autotrade", cmd_autotrade))
    app.add_handler(CommandHandler("withdraw", cmd_withdraw))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("mysettings", cmd_mysettings))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("sell", cmd_sell))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("snipe", cmd_snipe))
    app.add_handler(CommandHandler("portfolio", cmd_portfolio))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("addwhale", cmd_addwhale))
    app.add_handler(CommandHandler("removewhale", cmd_removewhale))
    app.add_handler(CommandHandler("whales", cmd_whales))
    app.add_handler(CommandHandler("copytrade", cmd_copytrade))
    app.add_handler(CommandHandler("fees", cmd_fees))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("lowcaps", cmd_lowcaps))
    app.add_handler(CommandHandler("backtest", cmd_backtest))
    app.add_handler(CommandHandler("blacklist", cmd_blacklist))
    app.add_handler(CommandHandler("whitelist", cmd_whitelist))
    app.add_handler(CommandHandler("sellall", cmd_sellall))
    app.add_handler(CommandHandler("dca", cmd_dca))
    app.add_handler(CommandHandler("limit", cmd_limit))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("compound", cmd_compound))
    app.add_handler(CallbackQueryHandler(handle_callback))

    def _handle_signal(signum, frame):
        logger.info("Received signal %s – shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("Polling for Telegram updates …")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
