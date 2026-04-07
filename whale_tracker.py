import asyncio
import json

from solders.pubkey import Pubkey
from solders.signature import Signature

import db
from config import CHAIN, WHALE_CHECK_INTERVAL, WHALE_MIN_SOL, WHALE_COPY_ENABLED, WHALE_COPY_AMOUNT, WHALE_COPY_MAX_PER_TOKEN, NATIVE_SYMBOL, logger
from trader import create_user_trader

LAMPORTS_PER_SOL = 1_000_000_000


class WhaleTracker:
    def __init__(self, solana_client, notifier):
        self.client = solana_client
        self.notifier = notifier
        self.running = False
        self.last_signatures: dict[str, str] = {}
        self._consecutive_errors = 0

    async def start(self):
        self.running = True
        if CHAIN.upper() != "SOL":
            logger.info("Whale tracking is Solana-only; current chain is %s — skipping", CHAIN)
            return
        logger.info(
            "WhaleTracker started (interval=%ds, min_sol=%.2f)",
            WHALE_CHECK_INTERVAL,
            WHALE_MIN_SOL,
        )
        while self.running:
            try:
                await self._check_wallets()
                self._consecutive_errors = 0
            except Exception as exc:
                self._consecutive_errors += 1
                logger.error("WhaleTracker error (%d consecutive): %s", self._consecutive_errors, exc)
                if self._consecutive_errors >= 10:
                    logger.warning("WhaleTracker: 10+ consecutive errors — continuing anyway")
            await asyncio.sleep(WHALE_CHECK_INTERVAL)

    async def stop(self):
        self.running = False
        logger.info("WhaleTracker stopped")

    async def _check_wallets(self):
        wallets = await db.get_whale_wallets()
        if not wallets:
            return

        for wallet_row in wallets:
            if not self.running:
                break
            address = wallet_row["address"]
            label = wallet_row.get("label", "")

            try:
                pubkey = Pubkey.from_string(address)
            except Exception:
                logger.warning("WhaleTracker: invalid wallet address %s — skipping", address)
                continue

            try:
                await self._process_wallet(pubkey, address, label)
            except Exception as exc:
                logger.error("WhaleTracker: error processing wallet %s: %s", address, exc)

            await asyncio.sleep(0.2)

    async def _process_wallet(self, pubkey: Pubkey, address: str, label: str):
        kwargs = {"limit": 10}
        last_sig = self.last_signatures.get(address)
        if last_sig:
            kwargs["until"] = Signature.from_string(last_sig)

        resp = await self.client.get_signatures_for_address(pubkey, **kwargs)
        sigs = resp.value
        if not sigs:
            return

        self.last_signatures[address] = str(sigs[0].signature)

        for sig_info in sigs:
            if not self.running:
                break
            sig_str = str(sig_info.signature)

            try:
                tx_resp = await self.client.get_transaction(
                    Signature.from_string(sig_str),
                    max_supported_transaction_version=0,
                    encoding="jsonParsed",
                )
            except Exception as exc:
                logger.error("WhaleTracker: get_transaction failed for %s: %s", sig_str, exc)
                continue

            if tx_resp.value is None:
                continue

            tx_json = tx_resp.value.to_json()
            tx_data = json.loads(tx_json)

            buy_events = self._parse_transaction(tx_data, address)
            for event in buy_events:
                await self._handle_buy_event(event, address, label, sig_str)

            await asyncio.sleep(0.2)

    def _parse_transaction(self, tx_data: dict, wallet_address: str) -> list[dict]:
        results = []
        meta = tx_data.get("meta")
        if meta is None or meta.get("err") is not None:
            return results

        pre_token = {}
        for b in (meta.get("preTokenBalances") or []):
            owner = b.get("owner")
            mint = b.get("mint")
            if owner and mint:
                amount_str = (b.get("uiTokenAmount") or {}).get("amount", "0")
                pre_token[(owner, mint)] = int(amount_str)

        post_token = {}
        for b in (meta.get("postTokenBalances") or []):
            owner = b.get("owner")
            mint = b.get("mint")
            if owner and mint:
                amount_str = (b.get("uiTokenAmount") or {}).get("amount", "0")
                decimals = (b.get("uiTokenAmount") or {}).get("decimals", 0)
                post_token[(owner, mint)] = (int(amount_str), decimals)

        pre_balances = meta.get("preBalances") or []
        post_balances = meta.get("postBalances") or []
        account_keys = []
        transaction = tx_data.get("transaction", {})
        message = transaction.get("message", {})
        for ak in (message.get("accountKeys") or []):
            if isinstance(ak, dict):
                account_keys.append(ak.get("pubkey", ""))
            else:
                account_keys.append(str(ak))

        wallet_index = None
        for i, key in enumerate(account_keys):
            if key == wallet_address:
                wallet_index = i
                break

        sol_spent = 0.0
        if wallet_index is not None and wallet_index < len(pre_balances) and wallet_index < len(post_balances):
            lamports_delta = pre_balances[wallet_index] - post_balances[wallet_index]
            sol_spent = lamports_delta / LAMPORTS_PER_SOL

        for (owner, mint), (post_amount, decimals) in post_token.items():
            if owner != wallet_address:
                continue
            pre_amount = pre_token.get((owner, mint), 0)
            if post_amount > pre_amount:
                tokens_delta = post_amount - pre_amount
                ui_tokens = tokens_delta / (10 ** decimals) if decimals > 0 else float(tokens_delta)
                results.append({
                    "token_mint": mint,
                    "tokens_received": ui_tokens,
                    "sol_spent": sol_spent,
                    "wallet": wallet_address,
                })

        return results

    async def _handle_buy_event(self, event: dict, wallet_address: str, label: str, tx_signature: str):
        if event["sol_spent"] < WHALE_MIN_SOL:
            return

        token_mint = event["token_mint"]
        watched = await db.is_token_watched(token_mint, "SOL")
        if watched is None:
            return

        is_held = watched.get("source") == "open_positions"
        token_symbol = watched.get("token_symbol") or watched.get("symbol") or token_mint[:8]

        saved = await db.save_whale_event({
            "wallet_address": wallet_address,
            "token_mint": token_mint,
            "token_symbol": token_symbol,
            "sol_spent": event["sol_spent"],
            "tokens_received": event["tokens_received"],
            "tx_signature": tx_signature,
        })
        if not saved:
            return

        logger.info(
            "Whale buy: %s spent %.4f SOL on %s (%s)",
            wallet_address[:8],
            event["sol_spent"],
            token_symbol,
            token_mint[:12],
        )

        await self.notifier.notify_whale_alert(
            wallet_address=wallet_address,
            wallet_label=label,
            token_symbol=token_symbol,
            token_mint=token_mint,
            sol_spent=event["sol_spent"],
            tokens_received=event["tokens_received"],
            tx_signature=tx_signature,
            is_held=is_held,
        )

        # Trigger copy trades
        await self._execute_copy_trades(
            token_mint=token_mint,
            token_symbol=token_symbol,
            whale_address=wallet_address,
            whale_label=label,
            sol_spent=event["sol_spent"],
        )

    async def _execute_copy_trades(self, token_mint: str, token_symbol: str, whale_address: str, whale_label: str, sol_spent: float):
        """Copy a whale buy across all auto-trading users."""
        if not WHALE_COPY_ENABLED:
            return
        if CHAIN.upper() != "SOL":
            return

        trading_users = await db.get_all_trading_users()
        if not trading_users:
            return

        native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
        buy_amount = WHALE_COPY_AMOUNT

        for user_wallet in trading_users:
            uid = user_wallet["user_id"]
            try:
                already = await db.is_token_already_bought(token_mint, CHAIN.upper(), uid)
                if already:
                    logger.debug("Copy trade skip user %d — already holds %s", uid, token_symbol)
                    continue

                try:
                    from config import MAX_OPEN_POSITIONS
                    open_count = await db.count_open_positions(uid)
                    if open_count >= MAX_OPEN_POSITIONS:
                        logger.debug("Copy trade skip user %d — max positions reached", uid)
                        continue
                except ImportError:
                    pass

                try:
                    from config import MAX_DAILY_LOSS
                    if MAX_DAILY_LOSS > 0:
                        daily_loss = await db.get_daily_realized_loss(uid)
                        if daily_loss >= MAX_DAILY_LOSS:
                            logger.debug("Copy trade skip user %d — daily loss limit", uid)
                            continue
                except (ImportError, AttributeError):
                    pass

                user_trader = await create_user_trader(uid)
                if user_trader is None:
                    continue

                balance = await user_trader.get_balance()
                if balance < buy_amount + 0.005:
                    logger.debug("Copy trade skip user %d — insufficient balance (%.4f)", uid, balance)
                    continue

                actual_buy = buy_amount
                try:
                    from config import MAX_BUY_AMOUNT
                    if MAX_BUY_AMOUNT > 0 and actual_buy > MAX_BUY_AMOUNT:
                        actual_buy = MAX_BUY_AMOUNT
                except ImportError:
                    pass

                logger.info("Copy trade: buying %s for user %d (%.4f SOL, whale=%s)",
                            token_symbol, uid, actual_buy, whale_address[:8])

                result = await user_trader.buy_token(token_mint, actual_buy)
                if result is None:
                    logger.error("Copy trade buy failed for user %d on %s", uid, token_symbol)
                    await self.notifier.send_to_user(uid, f"❌ Whale copy buy failed for {token_symbol}")
                    continue

                entry_liq = 0.0
                try:
                    import aiohttp
                    from dexscreener import get_token_liquidity
                    async with aiohttp.ClientSession() as session:
                        entry_liq = await get_token_liquidity(session, CHAIN, token_mint)
                except Exception:
                    pass

                position = {
                    "token_address": token_mint,
                    "token_symbol": token_symbol,
                    "chain": CHAIN.upper(),
                    "entry_price": result["entry_price"],
                    "tokens_received": result["tokens_received"],
                    "buy_amount_native": result["amount_spent"],
                    "buy_tx_hash": result["tx_hash"],
                    "pair_address": "",
                    "entry_liquidity": entry_liq,
                    "user_id": uid,
                }
                await db.save_open_position(position)

                short_whale = whale_address[:6] + "…" + whale_address[-4:]
                whale_lbl = f" ({whale_label})" if whale_label else ""
                await self.notifier.send_to_user(
                    uid,
                    f"🐋 <b>WHALE COPY BUY</b>\n"
                    f"Whale: <code>{short_whale}</code>{whale_lbl} spent {sol_spent:.4f} SOL\n"
                    f"Token: {token_symbol}\n"
                    f"Your buy: {result['amount_spent']:.4f} {native}\n"
                    f"Entry: {result['entry_price']:.10f} {native}",
                )

                await self.notifier.send_message(
                    f"🐋 Copy trade: user <code>{uid}</code> bought {token_symbol} — "
                    f"{result['amount_spent']:.4f} {native} (whale {short_whale})",
                )

            except Exception as exc:
                logger.error("Copy trade error for user %d on %s: %s", uid, token_symbol, exc)
