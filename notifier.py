from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import Forbidden, BadRequest

from config import EXPLORER_TX, logger
from scorer import format_score_bar

_MAX_MSG_LEN = 4096


class Notifier:
    def __init__(self, bot_token: str, chat_id: int):
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

    async def send_message(self, text: str, chat_id: int | None = None, reply_markup=None):
        target = chat_id if chat_id is not None else self.chat_id
        try:
            chunks = _split_message(text)
            for i, chunk in enumerate(chunks):
                kwargs = {
                    "chat_id": target,
                    "text": chunk,
                    "parse_mode": ParseMode.HTML,
                    "disable_web_page_preview": True,
                }
                if reply_markup and i == len(chunks) - 1:
                    kwargs["reply_markup"] = reply_markup
                await self.bot.send_message(**kwargs)
        except Exception as exc:
            logger.error("Failed to send Telegram message to %s: %s", target, exc)

    async def send_to_user(self, user_id: int, text: str, reply_markup=None):
        await self.send_message(text, chat_id=user_id, reply_markup=reply_markup)

    async def broadcast_alert(self, text: str, reply_markup=None):
        import db as _db
        chats = await _db.get_all_bot_chats()
        for chat in chats:
            cid = chat["chat_id"]
            try:
                chunks = _split_message(text)
                for i, chunk in enumerate(chunks):
                    kwargs = {
                        "chat_id": cid,
                        "text": chunk,
                        "parse_mode": ParseMode.HTML,
                        "disable_web_page_preview": True,
                    }
                    if reply_markup and i == len(chunks) - 1:
                        kwargs["reply_markup"] = reply_markup
                    await self.bot.send_message(**kwargs)
            except (Forbidden, BadRequest) as exc:
                logger.warning("Removing unreachable chat %d: %s", cid, exc)
                await _db.remove_bot_chat(cid)
            except Exception as exc:
                logger.error("Failed to broadcast to chat %d: %s", cid, exc)

    async def notify_new_token(self, token: dict, buy_amount: float, native_symbol: str, chat_id: int | None = None):
        mcap = _fmt_usd(token.get("market_cap", 0))
        liquidity = _fmt_usd(token.get("liquidity", 0))
        price = _fmt_price(token.get("price_usd", 0))
        volume = _fmt_usd(token.get("volume_24h", 0))
        source = token.get("source", "dextools")

        if source == "dexscreener":
            holders_str = "N/A"
        else:
            holders_str = _fmt_int(token.get("holders", 0))

        links = f"🔗 DexTools: {_esc(token.get('dextools_url', ''))}\n"
        if source == "dexscreener":
            links += f"🔗 DexScreener: {_esc(token.get('dex_pair_url', ''))}\n"
        links += f"📡 Source: {_esc(source)}"

        score = token.get("score", 0)
        score_bar = format_score_bar(score)
        socials_str = _fmt_socials(token.get("social_links", {}))

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 <b>NEW LOWCAP DETECTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Name: {_esc(token.get('name', '?'))} ({_esc(token.get('symbol', '?'))})\n"
            f"📄 Contract: <code>{_esc(token.get('contract_address', ''))}</code>\n"
            f"⛓ Chain: {_esc(token.get('chain', ''))}\n"
            f"💰 Market Cap: {mcap}\n"
            f"💧 Liquidity: {liquidity}\n"
            f"📈 Price: {price}\n"
            f"📊 24h Volume: {volume}\n"
            f"👥 Holders: {holders_str}\n"
            f"🧾 Buy Tax: {token.get('buy_tax', 0):.1f}% | Sell Tax: {token.get('sell_tax', 0):.1f}%\n"
            f"{links}\n"
            + (f"🔗 Socials: {socials_str}\n" if socials_str else "")
            + f"🛡️ Safety Score: {score_bar}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚙️ Action: Buying with {buy_amount:.4f} {native_symbol}"
        )
        await self.send_message(msg, chat_id=chat_id)

    async def notify_buy_executed(
        self, symbol: str, tokens_received: float, entry_price: float, tx_hash: str, chain: str,
        chat_id: int | None = None,
    ):
        link = _tx_link(tx_hash, chain)
        msg = (
            "✅ <b>BUY EXECUTED</b>\n"
            f"Token: {_esc(symbol)} | Amount: {_fmt_tokens(tokens_received)}\n"
            f"Entry Price: {_fmt_price(entry_price)}\n"
            f"TX: {link}"
        )
        await self.send_message(msg, chat_id=chat_id)

    async def notify_take_profit(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        roi: float,
        profit_usd: float,
        duration: str,
        tx_hash: str,
        chain: str,
        chat_id: int | None = None,
    ):
        link = _tx_link(tx_hash, chain)
        msg = (
            f"💸 <b>PROFIT TAKEN — +{roi:.2f}%</b>\n"
            f"Token: {_esc(symbol)}\n"
            f"Entry: {_fmt_price(entry_price)} → Exit: {_fmt_price(exit_price)}\n"
            f"Profit: +{_fmt_price(profit_usd)}\n"
            f"Duration: {duration}\n"
            f"TX: {link}"
        )
        await self.send_message(msg, chat_id=chat_id)

    async def notify_stop_loss(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        roi: float,
        loss_native: float,
        duration: str,
        tx_hash: str,
        chain: str,
        chat_id: int | None = None,
    ):
        link = _tx_link(tx_hash, chain)
        native = {"SOL": "SOL", "ETH": "ETH", "BSC": "BNB"}.get(chain.upper(), "SOL")
        msg = (
            f"\U0001f6d1 <b>STOP-LOSS TRIGGERED — {roi:.2f}%</b>\n"
            f"Token: {_esc(symbol)}\n"
            f"Entry: {_fmt_price(entry_price)} → Exit: {_fmt_price(exit_price)}\n"
            f"Loss: {loss_native:.4f} {native}\n"
            f"Duration: {duration}\n"
            f"TX: {link}"
        )
        await self.send_message(msg, chat_id=chat_id)

    async def notify_rug_pull(
        self,
        symbol: str,
        entry_price: float,
        exit_price: float,
        roi: float,
        loss_native: float,
        duration: str,
        tx_hash: str,
        chain: str,
        reason: str,
        chat_id: int | None = None,
    ):
        link = _tx_link(tx_hash, chain)
        native = {"SOL": "SOL", "ETH": "ETH", "BSC": "BNB"}.get(chain.upper(), "SOL")
        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "\U0001f6a8 <b>RUG PULL — EMERGENCY SELL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"\U0001fa99 Token: {_esc(symbol)}\n"
            f"⚠️ Reason: {_esc(reason)}\n"
            f"Entry: {_fmt_price(entry_price)} → Exit: {_fmt_price(exit_price)}\n"
            f"ROI: {roi:.2f}%\n"
            f"Loss: {loss_native:.4f} {native}\n"
            f"Duration: {duration}\n"
            f"TX: {link}\n"
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(msg, chat_id=chat_id)

    async def notify_error(self, error_msg: str, chat_id: int | None = None):
        msg = f"⚠️ <b>ERROR</b>\n<code>{_esc(error_msg)}</code>"
        await self.send_message(msg, chat_id=chat_id)

    async def notify_whale_alert(
        self,
        wallet_address: str,
        wallet_label: str,
        token_symbol: str,
        token_mint: str,
        sol_spent: float,
        tokens_received: float,
        tx_signature: str,
        is_held: bool,
    ):
        solscan_url = f"https://solscan.io/tx/{tx_signature}"
        short_wallet = wallet_address[:6] + "…" + wallet_address[-4:]
        short_tx = tx_signature[:10] + "…" + tx_signature[-6:]
        label_str = f" ({_esc(wallet_label)})" if wallet_label else ""
        held_str = "🟢 YOU HOLD THIS TOKEN" if is_held else "👀 Token in watchlist"

        msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🐋 <b>WHALE BUY DETECTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👛 Wallet: <code>{short_wallet}</code>{label_str}\n"
            f"🪙 Token: {_esc(token_symbol)}\n"
            f"📄 Mint: <code>{_esc(token_mint)}</code>\n"
            f"💰 SOL Spent: {sol_spent:.4f} SOL\n"
            f"📦 Received: {_fmt_tokens(tokens_received)} tokens\n"
            f"📍 {held_str}\n"
            f'TX: <a href="{solscan_url}">{short_tx}</a>\n'
            "━━━━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(msg)


def _esc(text) -> str:
    if text is None:
        return ""
    s = str(text)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tx_link(tx_hash: str, chain: str) -> str:
    base = EXPLORER_TX.get(chain.upper(), EXPLORER_TX["SOL"])
    url = base.format(tx_hash)
    short = tx_hash[:10] + "…" + tx_hash[-6:] if len(tx_hash) > 20 else tx_hash
    return f'<a href="{url}">{short}</a>'


def _fmt_usd(val) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "$0"
    if v >= 1_000_000:
        return f"${v:,.0f}"
    if v >= 1_000:
        return f"${v:,.0f}"
    return f"${v:,.2f}"


def _fmt_price(val) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "$0"
    if v == 0:
        return "$0"
    if v >= 1:
        return f"${v:,.4f}"
    if v >= 0.0001:
        return f"${v:.6f}"
    return f"${v:.10f}"


def _fmt_int(val) -> str:
    try:
        return f"{int(val):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_tokens(val) -> str:
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "0"
    if v >= 1_000_000:
        return f"{v:,.2f}"
    if v >= 1:
        return f"{v:,.4f}"
    return f"{v:.8f}"


def _fmt_socials(social_links: dict) -> str:
    if not social_links or not isinstance(social_links, dict):
        return ""
    icons = {
        "website": "🌐",
        "twitter": "🐦",
        "telegram": "💬",
        "discord": "🎮",
    }
    parts = []
    for key, url in social_links.items():
        if url:
            icon = icons.get(key, "🔗")
            label = key.capitalize()
            parts.append(f'{icon} <a href="{_esc(url)}">{label}</a>')
    if not parts:
        return ""
    return " | ".join(parts)


def _split_message(text: str) -> list[str]:
    if len(text) <= _MAX_MSG_LEN:
        return [text]
    chunks = []
    while text:
        if len(text) <= _MAX_MSG_LEN:
            chunks.append(text)
            break
        split_at = text.rfind("\n", 0, _MAX_MSG_LEN)
        if split_at == -1:
            split_at = _MAX_MSG_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks
