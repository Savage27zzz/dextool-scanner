import json
from pathlib import Path

import aiosqlite

from config import logger, BASE_DIR

DB_PATH = BASE_DIR / "trading.db"

_CREATE_DETECTED_TOKENS = """
CREATE TABLE IF NOT EXISTS detected_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    contract_address TEXT NOT NULL,
    chain TEXT NOT NULL,
    market_cap REAL,
    liquidity REAL,
    price_usd REAL,
    price_native REAL,
    volume_24h REAL,
    price_change_24h REAL,
    holders INTEGER,
    buy_tax REAL,
    sell_tax REAL,
    dextools_url TEXT,
    dex_pair_url TEXT,
    deployer_wallet TEXT,
    social_links TEXT,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(contract_address, chain)
);
"""

_CREATE_OPEN_POSITIONS = """
CREATE TABLE IF NOT EXISTS open_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    entry_price REAL NOT NULL,
    tokens_received REAL NOT NULL,
    buy_amount_native REAL NOT NULL,
    buy_tx_hash TEXT NOT NULL,
    pair_address TEXT,
    peak_price REAL DEFAULT 0,
    trailing_activated INTEGER DEFAULT 0,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(token_address, chain)
);
"""

_CREATE_COMPLETED_TRADES = """
CREATE TABLE IF NOT EXISTS completed_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_address TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    chain TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    tokens_amount REAL NOT NULL,
    buy_amount_native REAL NOT NULL,
    sell_amount_native REAL NOT NULL,
    profit_usd REAL,
    roi_percent REAL NOT NULL,
    buy_tx_hash TEXT NOT NULL,
    sell_tx_hash TEXT NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    duration_seconds INTEGER
);
"""

_CREATE_ALLOWED_USERS = """
CREATE TABLE IF NOT EXISTS allowed_users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_WHALE_WALLETS = """
CREATE TABLE IF NOT EXISTS whale_wallets (
    address TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_WHALE_EVENTS = """
CREATE TABLE IF NOT EXISTS whale_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address TEXT NOT NULL,
    token_mint TEXT NOT NULL,
    token_symbol TEXT DEFAULT '',
    sol_spent REAL NOT NULL,
    tokens_received REAL NOT NULL,
    tx_signature TEXT NOT NULL UNIQUE,
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def _conn() -> aiosqlite.Connection:
    db = await aiosqlite.connect(str(DB_PATH))
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute(_CREATE_DETECTED_TOKENS)
        await db.execute(_CREATE_OPEN_POSITIONS)
        await db.execute(_CREATE_COMPLETED_TRADES)
        await db.execute(_CREATE_ALLOWED_USERS)
        await db.execute(_CREATE_WHALE_WALLETS)
        await db.execute(_CREATE_WHALE_EVENTS)
        # Migration: add trailing columns if they don't exist
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN peak_price REAL DEFAULT 0")
        except Exception:
            pass  # column already exists
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN trailing_activated INTEGER DEFAULT 0")
        except Exception:
            pass  # column already exists
        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


async def save_detected_token(token_data: dict):
    sql = """
        INSERT OR IGNORE INTO detected_tokens
            (name, symbol, contract_address, chain, market_cap, liquidity,
             price_usd, price_native, volume_24h, price_change_24h, holders,
             buy_tax, sell_tax, dextools_url, dex_pair_url, deployer_wallet, social_links)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    social = token_data.get("social_links")
    if isinstance(social, dict):
        social = json.dumps(social)
    params = (
        token_data.get("name"),
        token_data.get("symbol"),
        token_data.get("contract_address"),
        token_data.get("chain"),
        token_data.get("market_cap"),
        token_data.get("liquidity"),
        token_data.get("price_usd"),
        token_data.get("price_native"),
        token_data.get("volume_24h"),
        token_data.get("price_change_24h"),
        token_data.get("holders"),
        token_data.get("buy_tax"),
        token_data.get("sell_tax"),
        token_data.get("dextools_url"),
        token_data.get("dex_pair_url"),
        token_data.get("deployer_wallet"),
        social,
    )
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(sql, params)
        await db.commit()
    logger.debug("Saved detected token %s (%s)", token_data.get("symbol"), token_data.get("contract_address"))


async def save_open_position(position: dict):
    sql = """
        INSERT INTO open_positions
            (token_address, token_symbol, chain, entry_price, tokens_received,
             buy_amount_native, buy_tx_hash, pair_address)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        position["token_address"],
        position["token_symbol"],
        position["chain"],
        position["entry_price"],
        position["tokens_received"],
        position["buy_amount_native"],
        position["buy_tx_hash"],
        position.get("pair_address"),
    )
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(sql, params)
        await db.commit()
    logger.info("Saved open position for %s on %s", position["token_symbol"], position["chain"])


async def get_open_positions() -> list[dict]:
    sql = "SELECT * FROM open_positions ORDER BY opened_at DESC"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def close_position(token_address: str, chain: str, exit_data: dict):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM open_positions WHERE token_address = ? AND chain = ?",
            (token_address, chain),
        )
        row = await cursor.fetchone()
        if row is None:
            logger.warning("close_position: no open position for %s on %s", token_address, chain)
            return
        pos = dict(row)

        opened_at = pos["opened_at"]
        duration = exit_data.get("duration_seconds", 0)

        insert_sql = """
            INSERT INTO completed_trades
                (token_address, token_symbol, chain, entry_price, exit_price,
                 tokens_amount, buy_amount_native, sell_amount_native, profit_usd,
                 roi_percent, buy_tx_hash, sell_tx_hash, opened_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            pos["token_address"],
            pos["token_symbol"],
            pos["chain"],
            pos["entry_price"],
            exit_data["exit_price"],
            pos["tokens_received"],
            pos["buy_amount_native"],
            exit_data["sell_amount_native"],
            exit_data.get("profit_usd"),
            exit_data["roi_percent"],
            pos["buy_tx_hash"],
            exit_data["sell_tx_hash"],
            opened_at,
            duration,
        )
        await db.execute(insert_sql, params)
        await db.execute(
            "DELETE FROM open_positions WHERE token_address = ? AND chain = ?",
            (token_address, chain),
        )
        await db.commit()
    logger.info(
        "Closed position %s on %s | ROI %.2f%%",
        pos["token_symbol"],
        chain,
        exit_data["roi_percent"],
    )


async def get_trade_history(limit: int = 10) -> list[dict]:
    sql = "SELECT * FROM completed_trades ORDER BY closed_at DESC LIMIT ?"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, (limit,))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def add_allowed_user(user_id: int, username: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO allowed_users (user_id, username) VALUES (?, ?)",
            (user_id, username),
        )
        await conn.commit()
    logger.info("Added allowed user %d (%s)", user_id, username)


async def remove_allowed_user(user_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        cursor = await conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        await conn.commit()
        removed = cursor.rowcount > 0
    if removed:
        logger.info("Removed allowed user %d", user_id)
    return removed


async def get_allowed_users() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT * FROM allowed_users ORDER BY added_at")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def is_user_allowed(user_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM allowed_users WHERE user_id = ? LIMIT 1", (user_id,)
        )
        return await cursor.fetchone() is not None


async def update_peak_price(token_address: str, chain: str, peak_price: float, trailing_activated: bool):
    """Update the peak price and trailing activation status for an open position."""
    sql = """
        UPDATE open_positions 
        SET peak_price = ?, trailing_activated = ?
        WHERE token_address = ? AND chain = ?
    """
    async with aiosqlite.connect(str(DB_PATH)) as db_conn:
        await db_conn.execute(sql, (peak_price, int(trailing_activated), token_address, chain))
        await db_conn.commit()


async def add_whale_wallet(address: str, label: str = "") -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        try:
            await db.execute(
                "INSERT INTO whale_wallets (address, label) VALUES (?, ?)",
                (address, label),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_whale_wallet(address: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM whale_wallets WHERE address = ?", (address,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_whale_wallets() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM whale_wallets ORDER BY added_at")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def save_whale_event(event: dict):
    sql = """
        INSERT OR IGNORE INTO whale_events
            (wallet_address, token_mint, token_symbol, sol_spent, tokens_received, tx_signature)
        VALUES (?, ?, ?, ?, ?, ?)
    """
    params = (
        event["wallet_address"],
        event["token_mint"],
        event.get("token_symbol", ""),
        event["sol_spent"],
        event["tokens_received"],
        event["tx_signature"],
    )
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(sql, params)
        await db.commit()


async def get_whale_events(limit: int = 10) -> list[dict]:
    sql = "SELECT * FROM whale_events ORDER BY detected_at DESC LIMIT ?"
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, (limit,))
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def is_token_watched(token_mint: str, chain: str = "SOL") -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM open_positions WHERE token_address = ? AND chain = ? LIMIT 1",
            (token_mint, chain),
        )
        row = await cursor.fetchone()
        if row:
            return {"source": "open_positions", **dict(row)}
        cursor = await db.execute(
            "SELECT * FROM detected_tokens WHERE contract_address = ? AND chain = ? LIMIT 1",
            (token_mint, chain),
        )
        row = await cursor.fetchone()
        if row:
            return {"source": "detected_tokens", **dict(row)}
    return None


async def is_token_already_bought(contract_address: str, chain: str) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur1 = await db.execute(
            "SELECT 1 FROM open_positions WHERE token_address = ? AND chain = ? LIMIT 1",
            (contract_address, chain),
        )
        if await cur1.fetchone():
            return True
        cur2 = await db.execute(
            "SELECT 1 FROM completed_trades WHERE token_address = ? AND chain = ? LIMIT 1",
            (contract_address, chain),
        )
        if await cur2.fetchone():
            return True
    return False
