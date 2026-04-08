import asyncio
from datetime import datetime, timezone

import aiohttp
import json

import db
from config import CHAIN, MONITOR_INTERVAL, NATIVE_SYMBOL, STOP_LOSS, TAKE_PROFIT, TRAILING_DROP, TRAILING_ENABLED, ANTIRUG_ENABLED, ANTIRUG_MIN_LIQ, ANTIRUG_LIQ_DROP_PCT, TELEGRAM_CHAT_ID, SELL_TIERS, logger
from db import get_effective_config
from fee_collector import collect_fee
from dexscreener import get_token_liquidity
from trader import create_user_trader, SolanaTrader


class ProfitMonitor:
    def __init__(self, trader, notifier):
        self.trader = trader
        self.notifier = notifier
        self.running = False

    async def start(self):
        self.running = True
        logger.info(
            "ProfitMonitor started (interval=%ds, TP=%d%%, SL=%d%%, anti-rug=%s)",
            MONITOR_INTERVAL, TAKE_PROFIT, STOP_LOSS, "ON" if ANTIRUG_ENABLED else "OFF",
        )
        while self.running:
            try:
                await self.check_positions()
            except Exception as exc:
                logger.error("Monitor error: %s", exc)
            await asyncio.sleep(MONITOR_INTERVAL)

    async def stop(self):
        self.running = False
        logger.info("ProfitMonitor stopped")

    async def _get_current_price(self, token_address: str, chain: str) -> float:
        if chain.upper() == "SOL":
            return await self.trader.get_token_price_via_jupiter(token_address)
        return await self.trader.get_token_price_onchain(token_address, chain)

    async def _execute_sell_and_close(self, pos: dict, roi: float, reason: str, user_trader: SolanaTrader | None = None, user_id: int = 0) -> dict | None:
        trader_to_use = user_trader or self.trader
        token_address = pos["token_address"]
        chain = pos["chain"]
        symbol = pos["token_symbol"]

        if chain.upper() == "SOL":
            ui_balance, decimals = await trader_to_use.get_token_balance(token_address)
            tokens_raw = int(ui_balance * (10**decimals)) if decimals > 0 else int(ui_balance * 1e9)
            if tokens_raw <= 0:
                logger.warning("Zero balance for %s – skipping %s sell", symbol, reason)
                return None
            sell_result = await trader_to_use.sell_token(token_address, tokens_raw, decimals)
        else:
            ui_balance, decimals = await trader_to_use.get_token_balance(token_address, chain)
            tokens_raw = int(ui_balance * (10**decimals))
            if tokens_raw <= 0:
                logger.warning("Zero balance for %s – skipping %s sell", symbol, reason)
                return None
            sell_result = await trader_to_use.sell_token(token_address, chain, tokens_raw, decimals)

        if sell_result is None:
            logger.error("%s sell failed for %s", reason, symbol)
            await self.notifier.notify_error(f"{reason} sell failed for {symbol} ({token_address})")
            return None

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
            "exit_price": sell_result["exit_price"],
            "sell_amount_native": sell_result["native_received"],
            "profit_usd": None,
            "roi_percent": roi,
            "sell_tx_hash": sell_result["tx_hash"],
            "duration_seconds": duration_seconds,
        }

        await db.close_position(token_address, chain, exit_data, user_id=user_id)
        sell_result["duration_seconds"] = duration_seconds
        return sell_result

    async def _execute_partial_sell(self, pos: dict, sell_fraction: float, roi: float, tier_label: str, user_trader: SolanaTrader | None = None, user_id: int = 0) -> dict | None:
        """Sell a fraction (0.0-1.0) of a position. Returns sell_result or None."""
        trader_to_use = user_trader or self.trader
        token_address = pos["token_address"]
        chain = pos["chain"]
        symbol = pos["token_symbol"]

        if chain.upper() == "SOL":
            ui_balance, decimals = await trader_to_use.get_token_balance(token_address)
            sell_ui = ui_balance * sell_fraction
            tokens_raw = int(sell_ui * (10**decimals)) if decimals > 0 else int(sell_ui * 1e9)
            if tokens_raw <= 0:
                logger.warning("Zero partial balance for %s — skipping %s", symbol, tier_label)
                return None
            sell_result = await trader_to_use.sell_token(token_address, tokens_raw, decimals)
        else:
            ui_balance, decimals = await trader_to_use.get_token_balance(token_address, chain)
            sell_ui = ui_balance * sell_fraction
            tokens_raw = int(sell_ui * (10**decimals))
            if tokens_raw <= 0:
                logger.warning("Zero partial balance for %s — skipping %s", symbol, tier_label)
                return None
            sell_result = await trader_to_use.sell_token(token_address, chain, tokens_raw, decimals)

        if sell_result is None:
            logger.error("%s partial sell failed for %s", tier_label, symbol)
            return None

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
            "exit_price": sell_result["exit_price"],
            "sell_amount_native": sell_result["native_received"],
            "profit_usd": None,
            "roi_percent": roi,
            "sell_tx_hash": sell_result["tx_hash"],
            "duration_seconds": duration_seconds,
        }

        await db.record_partial_sell(token_address, chain, user_id, sell_fraction, exit_data)
        sell_result["duration_seconds"] = duration_seconds
        return sell_result

    async def _admin_summary(self, user_id: int, reason: str, symbol: str, roi: float, native_amount: float):
        if user_id == TELEGRAM_CHAT_ID or user_id == 0:
            return
        native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
        sign = "+" if native_amount >= 0 else ""
        await self.notifier.send_message(
            f"📋 User <code>{user_id}</code> {reason} {symbol} — "
            f"ROI {roi:+.2f}% | {sign}{native_amount:.4f} {native}",
        )

    async def _try_collect_fee(self, user_id: int, symbol: str, profit_native: float, notify_chat_id: int | None):
        try:
            admin_wallet = await db.get_user_wallet(TELEGRAM_CHAT_ID)
            if not admin_wallet:
                return
            fee_result = await collect_fee(
                user_id=user_id,
                token_symbol=symbol,
                profit_native=profit_native,
                admin_public_key=admin_wallet["public_key"],
            )
            if fee_result and fee_result.get("tx_hash"):
                await self.notifier.send_to_user(
                    user_id,
                    f"💰 Operator fee: {fee_result['fee_amount']:.6f} SOL "
                    f"({fee_result['fee_pct']:.1f}% of {profit_native:.6f} SOL profit)",
                )
                await self.notifier.send_message(
                    f"💰 Fee collected: {fee_result['fee_amount']:.6f} SOL from user {user_id} "
                    f"({fee_result['fee_pct']:.1f}% of {profit_native:.6f} SOL profit on {symbol})",
                )
        except Exception as exc:
            logger.error("Fee collection error for user %d: %s", user_id, exc)

    async def _try_compound(self, user_id: int, profit_native: float):
        try:
            user_cfg = await db.get_effective_config(user_id)
            if not user_cfg.get("compound_enabled"):
                return
            compound_pct = user_cfg.get("compound_percent", 50)
            compound_amount = (profit_native * compound_pct) / 100
            if compound_amount < 0.0001:
                return
            await db.add_compound_funds(user_id, compound_amount)
            logger.info(
                "Compounded %.6f SOL for user %d (%d%% of %.6f profit)",
                compound_amount, user_id, compound_pct, profit_native,
            )
        except Exception as exc:
            logger.error("Compound error for user %d: %s", user_id, exc)

    async def check_positions(self):
        positions = await db.get_open_positions()
        if not positions:
            return

        logger.debug("Checking %d open positions", len(positions))

        _trader_cache: dict[int, SolanaTrader] = {}

        for pos in positions:
            token_address = pos["token_address"]
            chain = pos["chain"]
            entry_price = pos["entry_price"]
            symbol = pos["token_symbol"]
            user_id = pos.get("user_id", 0)

            if user_id not in _trader_cache:
                if user_id == 0:
                    _trader_cache[user_id] = self.trader
                else:
                    ut = await create_user_trader(user_id)
                    if ut is None:
                        logger.warning("No wallet for user %d, skipping position %s", user_id, symbol)
                        continue
                    _trader_cache[user_id] = ut

            user_trader = _trader_cache[user_id]
            notify_chat_id = user_id if user_id != 0 else None
            user_cfg = await get_effective_config(user_id)

            try:
                if ANTIRUG_ENABLED:
                    rug_detected = False
                    rug_reason = ""
                    try:
                        async with aiohttp.ClientSession() as session:
                            current_liq = await get_token_liquidity(session, chain, token_address)

                        entry_liq = pos.get("entry_liquidity", 0) or 0

                        if current_liq > 0 and current_liq < ANTIRUG_MIN_LIQ:
                            rug_detected = True
                            rug_reason = f"Liquidity ${current_liq:,.0f} below ${ANTIRUG_MIN_LIQ:,} floor"
                        elif entry_liq > 0 and current_liq > 0:
                            drop_pct = ((entry_liq - current_liq) / entry_liq) * 100
                            if drop_pct >= ANTIRUG_LIQ_DROP_PCT:
                                rug_detected = True
                                rug_reason = f"Liquidity dropped {drop_pct:.0f}% (${entry_liq:,.0f} → ${current_liq:,.0f})"

                    except Exception as exc:
                        logger.error("Anti-rug check failed for %s: %s", symbol, exc)

                    if rug_detected:
                        logger.warning("RUG DETECTED for %s: %s", symbol, rug_reason)
                        try:
                            current_price = await self._get_current_price(token_address, chain)
                            roi = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                        except Exception:
                            roi = -100

                        sell_result = await self._execute_sell_and_close(pos, roi, "Anti-rug", user_trader=user_trader, user_id=user_id)
                        if sell_result:
                            native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                            duration_str = _format_duration(sell_result["duration_seconds"])
                            loss_native = sell_result["native_received"] - pos["buy_amount_native"]
                            await self.notifier.notify_rug_pull(
                                symbol=symbol,
                                entry_price=entry_price,
                                exit_price=sell_result["exit_price"],
                                roi=round(roi, 2),
                                loss_native=loss_native,
                                duration=duration_str,
                                tx_hash=sell_result["tx_hash"],
                                chain=chain,
                                reason=rug_reason,
                                chat_id=notify_chat_id,
                            )
                            await self._admin_summary(user_id, "Anti-rug sell", symbol, round(roi, 2), loss_native)
                            if loss_native > 0:
                                await self._try_collect_fee(user_id, symbol, loss_native, notify_chat_id)
                                await self._try_compound(user_id, loss_native)
                        continue

                current_price = await self._get_current_price(token_address, chain)
                if current_price <= 0:
                    logger.warning("Could not get price for %s — skipping TP/SL check", symbol)
                    continue

                roi = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
                logger.debug("%s ROI: %.2f%% (entry=%.10f, current=%.10f)", symbol, roi, entry_price, current_price)

                # --- Tiered Sells ---
                if SELL_TIERS:
                    tiers_completed_raw = pos.get("tiers_completed", "[]")
                    try:
                        tiers_completed = json.loads(tiers_completed_raw) if isinstance(tiers_completed_raw, str) else []
                    except (json.JSONDecodeError, TypeError):
                        tiers_completed = []

                    tiers_changed = False
                    for tier_idx, (tier_roi, tier_pct) in enumerate(SELL_TIERS):
                        if tier_idx in tiers_completed:
                            continue
                        if roi >= tier_roi:
                            sell_fraction = tier_pct / 100.0
                            tier_label = f"Tier-{tier_idx+1} ({tier_roi}%/{tier_pct}%)"
                            logger.info("%s triggered for %s — ROI %.2f%%, selling %.0f%%", tier_label, symbol, roi, tier_pct)

                            sell_result = await self._execute_partial_sell(
                                pos, sell_fraction, roi, tier_label,
                                user_trader=user_trader, user_id=user_id,
                            )
                            if sell_result:
                                tiers_completed.append(tier_idx)
                                tiers_changed = True
                                native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                                duration_str = _format_duration(sell_result["duration_seconds"])
                                profit_native = sell_result["native_received"] - (pos["buy_amount_native"] * sell_fraction)

                                await self.notifier.notify_tier_sell(
                                    symbol=symbol,
                                    tier_label=tier_label,
                                    sell_percent=tier_pct,
                                    roi=round(roi, 2),
                                    native_received=sell_result["native_received"],
                                    tx_hash=sell_result["tx_hash"],
                                    chain=chain,
                                    chat_id=notify_chat_id,
                                )
                                await self._admin_summary(user_id, f"{tier_label} sell", symbol, round(roi, 2), profit_native)
                                if profit_native > 0:
                                    await self._try_collect_fee(user_id, symbol, profit_native, notify_chat_id)
                                    await self._try_compound(user_id, profit_native)

                    if tiers_changed:
                        await db.update_tiers_completed(token_address, chain, tiers_completed, user_id=user_id)

                sold = False

                if TRAILING_ENABLED:
                    trailing_activated = bool(pos.get("trailing_activated", 0))
                    peak_price = pos.get("peak_price", 0) or 0

                    if roi >= user_cfg["take_profit"] and not trailing_activated:
                        trailing_activated = True
                        peak_price = current_price
                        await db.update_peak_price(token_address, chain, peak_price, True, user_id=user_id)
                        logger.info(
                            "Trailing activated for %s – ROI %.2f%%, peak=%.10f",
                            symbol, roi, peak_price,
                        )
                        native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                        await self.notifier.send_message(
                            f"📈 <b>Trailing TP activated</b> for {symbol}\n"
                            f"ROI: {roi:+.2f}% | Peak: {peak_price:.10f} {native}\n"
                            f"Will sell on {user_cfg['trailing_drop']}% drop from peak.",
                            chat_id=notify_chat_id,
                        )

                    elif trailing_activated:
                        if current_price > peak_price:
                            peak_price = current_price
                            await db.update_peak_price(token_address, chain, peak_price, True, user_id=user_id)
                            logger.debug("New peak for %s: %.10f", symbol, peak_price)
                        else:
                            drop_from_peak = ((peak_price - current_price) / peak_price) * 100 if peak_price > 0 else 0
                            if drop_from_peak >= user_cfg["trailing_drop"]:
                                logger.info(
                                    "Trailing sell for %s – dropped %.2f%% from peak %.10f",
                                    symbol, drop_from_peak, peak_price,
                                )
                                sell_result = await self._execute_sell_and_close(pos, roi, "Trailing-TP", user_trader=user_trader, user_id=user_id)
                                if sell_result:
                                    sold = True
                                    native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                                    duration_str = _format_duration(sell_result["duration_seconds"])
                                    profit_native = sell_result["native_received"] - pos["buy_amount_native"]
                                    await self.notifier.notify_take_profit(
                                        symbol=symbol,
                                        entry_price=entry_price,
                                        exit_price=sell_result["exit_price"],
                                        roi=round(roi, 2),
                                        profit_usd=profit_native,
                                        duration=duration_str,
                                        tx_hash=sell_result["tx_hash"],
                                        chain=chain,
                                        chat_id=notify_chat_id,
                                    )
                                    await self._admin_summary(user_id, "Trailing-TP sell", symbol, round(roi, 2), profit_native)
                                    if profit_native > 0:
                                        await self._try_collect_fee(user_id, symbol, profit_native, notify_chat_id)
                                        await self._try_compound(user_id, profit_native)

                else:
                    if roi >= user_cfg["take_profit"]:
                        logger.info("TP hit for %s – ROI %.2f%% >= %d%%", symbol, roi, user_cfg["take_profit"])
                        sell_result = await self._execute_sell_and_close(pos, roi, "Take-profit", user_trader=user_trader, user_id=user_id)
                        if sell_result:
                            sold = True
                            native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                            duration_str = _format_duration(sell_result["duration_seconds"])
                            profit_native = sell_result["native_received"] - pos["buy_amount_native"]
                            await self.notifier.notify_take_profit(
                                symbol=symbol,
                                entry_price=entry_price,
                                exit_price=sell_result["exit_price"],
                                roi=round(roi, 2),
                                profit_usd=profit_native,
                                duration=duration_str,
                                tx_hash=sell_result["tx_hash"],
                                chain=chain,
                                chat_id=notify_chat_id,
                            )
                            await self._admin_summary(user_id, "Take-profit sell", symbol, round(roi, 2), profit_native)
                            if profit_native > 0:
                                await self._try_collect_fee(user_id, symbol, profit_native, notify_chat_id)
                                await self._try_compound(user_id, profit_native)

                if not sold and user_cfg["stop_loss"] < 0 and roi <= user_cfg["stop_loss"]:
                    logger.info("SL hit for %s – ROI %.2f%% <= %d%%", symbol, roi, user_cfg["stop_loss"])
                    sell_result = await self._execute_sell_and_close(pos, roi, "Stop-loss", user_trader=user_trader, user_id=user_id)
                    if sell_result:
                        native = NATIVE_SYMBOL.get(chain.upper(), "SOL")
                        duration_str = _format_duration(sell_result["duration_seconds"])
                        loss_native = sell_result["native_received"] - pos["buy_amount_native"]
                        await self.notifier.notify_stop_loss(
                            symbol=symbol,
                            entry_price=entry_price,
                            exit_price=sell_result["exit_price"],
                            roi=round(roi, 2),
                            loss_native=loss_native,
                            duration=duration_str,
                            tx_hash=sell_result["tx_hash"],
                            chain=chain,
                            chat_id=notify_chat_id,
                        )
                        await self._admin_summary(user_id, "Stop-loss sell", symbol, round(roi, 2), loss_native)
                        if loss_native > 0:
                            await self._try_collect_fee(user_id, symbol, loss_native, notify_chat_id)
                            await self._try_compound(user_id, loss_native)

            except Exception as exc:
                logger.error("Error checking position %s: %s", symbol, exc)

    async def get_positions_with_roi(self, user_id: int | None = None) -> list[dict]:
        positions = await db.get_open_positions(user_id=user_id)
        enriched = []
        for pos in positions:
            token_address = pos["token_address"]
            chain = pos["chain"]
            entry_price = pos["entry_price"]
            try:
                current_price = await self._get_current_price(token_address, chain)
                roi = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
            except Exception:
                current_price = 0
                roi = 0
            enriched.append({
                **pos,
                "current_price": current_price,
                "roi": round(roi, 2),
            })
        return enriched


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
