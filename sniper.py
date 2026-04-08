"""Sniper mode — fast-poll DexScreener for new pools and auto-buy."""

import asyncio

import aiohttp

import db
from config import (
    CHAIN,
    NATIVE_SYMBOL,
    SNIPER_CHECK_INTERVAL,
    SNIPER_MIN_LIQUIDITY,
    MAX_BUY_AMOUNT,
    MAX_OPEN_POSITIONS,
    logger,
)
from dexscreener import _fetch_token_pairs, _safe_float
from honeypot import check_honeypot
from trader import create_user_trader


class Sniper:
    def __init__(self, notifier):
        self.notifier = notifier
        self.running = False

    async def start(self):
        self.running = True
        logger.info(
            "Sniper started (interval=%ds, min_liq=$%d)",
            SNIPER_CHECK_INTERVAL, SNIPER_MIN_LIQUIDITY,
        )
        while self.running:
            try:
                await self._check_targets()
            except Exception as exc:
                logger.error("Sniper error: %s", exc)
            await asyncio.sleep(SNIPER_CHECK_INTERVAL)

    async def stop(self):
        self.running = False
        logger.info("Sniper stopped")

    async def _check_targets(self):
        targets = await db.get_active_snipe_targets()
        if not targets:
            return

        # Group by token_address to avoid duplicate API calls
        token_users: dict[str, list[dict]] = {}
        for t in targets:
            addr = t["token_address"]
            if addr not in token_users:
                token_users[addr] = []
            token_users[addr].append(t)

        ds_chain_id = {"SOL": "solana", "ETH": "ethereum", "BSC": "bsc"}.get(CHAIN.upper(), "solana")

        async with aiohttp.ClientSession() as session:
            for token_address, users in token_users.items():
                try:
                    pairs = await _fetch_token_pairs(session, ds_chain_id, token_address)
                    if not pairs:
                        continue

                    best_pair = max(
                        pairs,
                        key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")),
                    )
                    liq = _safe_float((best_pair.get("liquidity") or {}).get("usd"))

                    if liq < SNIPER_MIN_LIQUIDITY:
                        logger.debug(
                            "Snipe target %s — pool found but liq $%.0f < $%d",
                            token_address[:12], liq, SNIPER_MIN_LIQUIDITY,
                        )
                        continue

                    # Pool has sufficient liquidity — honeypot check
                    hp = await check_honeypot(session, CHAIN, token_address)
                    if hp["is_honeypot"]:
                        logger.warning("Snipe target %s is honeypot — removing all targets", token_address[:12])
                        for u in users:
                            await db.remove_snipe_target(token_address, u["user_id"])
                            await self.notifier.send_to_user(
                                u["user_id"],
                                f"🚫 Snipe cancelled for <code>{token_address}</code> — honeypot detected "
                                f"(buy tax {hp['buy_tax']:.1f}%, sell tax {hp['sell_tax']:.1f}%)",
                            )
                        continue

                    symbol = (best_pair.get("baseToken") or {}).get("symbol", "???")
                    logger.info(
                        "🎯 SNIPE TRIGGERED: %s (%s) — liq $%.0f",
                        symbol, token_address[:12], liq,
                    )

                    # Execute buy for each user
                    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
                    for target in users:
                        uid = target["user_id"]
                        try:
                            open_count = await db.count_open_positions(uid)
                            if open_count >= MAX_OPEN_POSITIONS:
                                logger.info("Snipe skip user %d — max positions", uid)
                                continue

                            already = await db.is_token_already_bought(token_address, CHAIN.upper(), uid)
                            if already:
                                await db.mark_snipe_filled(token_address, uid, "already_held")
                                continue

                            user_trader = await create_user_trader(uid)
                            if user_trader is None:
                                continue

                            buy_amount = target["amount"]
                            if buy_amount <= 0:
                                buy_amount = await user_trader.get_buy_amount()
                            if MAX_BUY_AMOUNT > 0 and buy_amount > MAX_BUY_AMOUNT:
                                buy_amount = MAX_BUY_AMOUNT
                            if buy_amount <= 0:
                                await self.notifier.send_to_user(uid, f"⚠️ Snipe failed for {symbol} — insufficient balance")
                                continue

                            await self.notifier.send_to_user(
                                uid,
                                f"🎯 <b>SNIPE EXECUTING</b>\n"
                                f"Token: {symbol} (<code>{token_address}</code>)\n"
                                f"Liquidity: ${liq:,.0f}\n"
                                f"Amount: {buy_amount:.4f} {native}",
                            )

                            result = await user_trader.buy_token(token_address, buy_amount)
                            if result is None:
                                await self.notifier.send_to_user(uid, f"❌ Snipe buy failed for {symbol}")
                                continue

                            position = {
                                "token_address": token_address,
                                "token_symbol": symbol,
                                "chain": CHAIN.upper(),
                                "entry_price": result["entry_price"],
                                "tokens_received": result["tokens_received"],
                                "buy_amount_native": result["amount_spent"],
                                "buy_tx_hash": result["tx_hash"],
                                "pair_address": best_pair.get("pairAddress", ""),
                                "entry_liquidity": liq,
                                "user_id": uid,
                            }
                            await db.save_open_position(position)
                            await db.mark_snipe_filled(token_address, uid, result["tx_hash"])

                            from config import EXPLORER_TX
                            tx_url = EXPLORER_TX.get(CHAIN.upper(), "https://solscan.io/tx/{}").format(result["tx_hash"])
                            await self.notifier.send_to_user(
                                uid,
                                f"🎯 <b>SNIPE SUCCESS</b>\n"
                                f"Token: {symbol}\n"
                                f"Tokens: {result['tokens_received']:.4f}\n"
                                f"Entry: {result['entry_price']:.10f} {native}\n"
                                f"Spent: {result['amount_spent']:.4f} {native}\n"
                                f'<a href="{tx_url}">🔗 Transaction</a>',
                            )

                            await self.notifier.send_message(
                                f"🎯 Snipe: User <code>{uid}</code> bought {symbol} — "
                                f"{result['amount_spent']:.4f} {native}",
                            )

                        except Exception as exc:
                            logger.error("Snipe buy error for user %d on %s: %s", uid, token_address[:12], exc)

                except Exception as exc:
                    logger.error("Snipe check error for %s: %s", token_address[:12], exc)
