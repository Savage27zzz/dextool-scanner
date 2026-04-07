import json
from pathlib import Path

import aiosqlite

from config import logger, DATA_DIR

DB_PATH = DATA_DIR / "trading.db"

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
    entry_liquidity REAL DEFAULT 0,
    user_id INTEGER DEFAULT 0,
    opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(token_address, chain, user_id)
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
    duration_seconds INTEGER,
    user_id INTEGER DEFAULT 0
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

_CREATE_USER_WALLETS = """
CREATE TABLE IF NOT EXISTS user_wallets (
    user_id INTEGER PRIMARY KEY,
    public_key TEXT NOT NULL,
    encrypted_private_key TEXT NOT NULL,
    encrypted_seed_phrase TEXT NOT NULL DEFAULT '',
    auto_trade INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_BOT_CHATS = """
CREATE TABLE IF NOT EXISTS bot_chats (
    chat_id INTEGER PRIMARY KEY,
    chat_type TEXT DEFAULT 'private',
    title TEXT DEFAULT '',
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_SCAN_HISTORY = """
CREATE TABLE IF NOT EXISTS scan_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_address TEXT NOT NULL,
    chain TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    score INTEGER NOT NULL,
    score_breakdown TEXT,
    market_cap REAL,
    liquidity REAL,
    volume_24h REAL,
    price_usd REAL,
    holders INTEGER,
    buy_tax REAL,
    sell_tax REAL,
    source TEXT DEFAULT '',
    was_bought INTEGER DEFAULT 0,
    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_CREATE_FEE_LEDGER = """
CREATE TABLE IF NOT EXISTS fee_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_symbol TEXT NOT NULL,
    trade_profit_native REAL NOT NULL,
    fee_amount_native REAL NOT NULL,
    fee_percent REAL NOT NULL,
    fee_tx_hash TEXT,
    status TEXT DEFAULT 'pending',   -- 'pending', 'submitted', 'collected', 'failed'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        await db.execute(_CREATE_USER_WALLETS)
        await db.execute(_CREATE_BOT_CHATS)
        await db.execute(_CREATE_FEE_LEDGER)
        await db.execute(_CREATE_SCAN_HISTORY)
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN peak_price REAL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN trailing_activated INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN entry_liquidity REAL DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN user_id INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE completed_trades ADD COLUMN user_id INTEGER DEFAULT 0")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE user_wallets ADD COLUMN encrypted_seed_phrase TEXT DEFAULT ''")
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE open_positions ADD COLUMN tiers_completed TEXT DEFAULT '[]'")
        except Exception:
            pass
        try:
            cursor = await db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='open_positions'"
            )
            row = await cursor.fetchone()
            if row:
                create_sql = row[0] or ""
                if "user_id" not in create_sql:
                    await db.execute("ALTER TABLE open_positions RENAME TO _open_positions_old")
                    await db.execute(_CREATE_OPEN_POSITIONS)
                    await db.execute("""
                        INSERT INTO open_positions
                            (id, token_address, token_symbol, chain, entry_price,
                             tokens_received, buy_amount_native, buy_tx_hash,
                             pair_address, peak_price, trailing_activated,
                             entry_liquidity, user_id, opened_at)
                        SELECT id, token_address, token_symbol, chain, entry_price,
                               tokens_received, buy_amount_native, buy_tx_hash,
                               pair_address,
                               COALESCE(peak_price, 0),
                               COALESCE(trailing_activated, 0),
                               COALESCE(entry_liquidity, 0),
                               0,
                               opened_at
                        FROM _open_positions_old
                    """)
                    await db.execute("DROP TABLE _open_positions_old")
                    logger.info("Migrated open_positions table with user_id constraint")
        except Exception as exc:
            logger.warning("open_positions migration check: %s", exc)
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
             buy_amount_native, buy_tx_hash, pair_address, entry_liquidity, user_id, tiers_completed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        position.get("entry_liquidity", 0),
        position.get("user_id", 0),
        "[]",
    )
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(sql, params)
        await db.commit()
    logger.info("Saved open position for %s on %s (user %d)", position["token_symbol"], position["chain"], position.get("user_id", 0))


async def get_open_positions(user_id: int | None = None) -> list[dict]:
    if user_id is not None:
        sql = "SELECT * FROM open_positions WHERE user_id = ? ORDER BY opened_at DESC"
        params: tuple = (user_id,)
    else:
        sql = "SELECT * FROM open_positions ORDER BY opened_at DESC"
        params = ()
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def close_position(token_address: str, chain: str, exit_data: dict, user_id: int = 0):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM open_positions WHERE token_address = ? AND chain = ? AND user_id = ?",
            (token_address, chain, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            logger.warning("close_position: no open position for %s on %s user %d", token_address, chain, user_id)
            return
        pos = dict(row)

        opened_at = pos["opened_at"]
        duration = exit_data.get("duration_seconds", 0)

        insert_sql = """
            INSERT INTO completed_trades
                (token_address, token_symbol, chain, entry_price, exit_price,
                 tokens_amount, buy_amount_native, sell_amount_native, profit_usd,
                 roi_percent, buy_tx_hash, sell_tx_hash, opened_at, duration_seconds, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            user_id,
        )
        await db.execute(insert_sql, params)
        await db.execute(
            "DELETE FROM open_positions WHERE token_address = ? AND chain = ? AND user_id = ?",
            (token_address, chain, user_id),
        )
        await db.commit()
    logger.info(
        "Closed position %s on %s user %d | ROI %.2f%%",
        pos["token_symbol"],
        chain,
        user_id,
        exit_data["roi_percent"],
    )


async def update_tiers_completed(token_address: str, chain: str, tiers_completed: list[int], user_id: int = 0):
    sql = """
        UPDATE open_positions
        SET tiers_completed = ?
        WHERE token_address = ? AND chain = ? AND user_id = ?
    """
    async with aiosqlite.connect(str(DB_PATH)) as db_conn:
        await db_conn.execute(sql, (json.dumps(tiers_completed), token_address, chain, user_id))
        await db_conn.commit()


async def record_partial_sell(token_address: str, chain: str, user_id: int, sell_fraction: float, exit_data: dict):
    """Record a partial sell: log to completed_trades, reduce open_position amounts proportionally."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM open_positions WHERE token_address = ? AND chain = ? AND user_id = ?",
            (token_address, chain, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        pos = dict(row)

        sold_tokens = pos["tokens_received"] * sell_fraction
        sold_buy_native = pos["buy_amount_native"] * sell_fraction
        remaining_tokens = pos["tokens_received"] - sold_tokens
        remaining_buy_native = pos["buy_amount_native"] - sold_buy_native

        opened_at = pos["opened_at"]
        duration = exit_data.get("duration_seconds", 0)

        insert_sql = """
            INSERT INTO completed_trades
                (token_address, token_symbol, chain, entry_price, exit_price,
                 tokens_amount, buy_amount_native, sell_amount_native, profit_usd,
                 roi_percent, buy_tx_hash, sell_tx_hash, opened_at, duration_seconds, user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            pos["token_address"],
            pos["token_symbol"],
            pos["chain"],
            pos["entry_price"],
            exit_data["exit_price"],
            sold_tokens,
            sold_buy_native,
            exit_data["sell_amount_native"],
            exit_data.get("profit_usd"),
            exit_data["roi_percent"],
            pos["buy_tx_hash"],
            exit_data["sell_tx_hash"],
            opened_at,
            duration,
            user_id,
        )
        await db.execute(insert_sql, params)

        await db.execute(
            "UPDATE open_positions SET tokens_received = ?, buy_amount_native = ? WHERE token_address = ? AND chain = ? AND user_id = ?",
            (remaining_tokens, remaining_buy_native, token_address, chain, user_id),
        )
        await db.commit()
    logger.info(
        "Partial sell %.0f%% of %s for user %d | ROI %.2f%%",
        sell_fraction * 100, pos["token_symbol"], user_id, exit_data["roi_percent"],
    )


async def get_trade_history(limit: int = 10, user_id: int | None = None) -> list[dict]:
    if user_id is not None:
        sql = "SELECT * FROM completed_trades WHERE user_id = ? ORDER BY closed_at DESC LIMIT ?"
        params: tuple = (user_id, limit)
    else:
        sql = "SELECT * FROM completed_trades ORDER BY closed_at DESC LIMIT ?"
        params = (limit,)
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(sql, params)
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


async def update_peak_price(token_address: str, chain: str, peak_price: float, trailing_activated: bool, user_id: int = 0):
    sql = """
        UPDATE open_positions 
        SET peak_price = ?, trailing_activated = ?
        WHERE token_address = ? AND chain = ? AND user_id = ?
    """
    async with aiosqlite.connect(str(DB_PATH)) as db_conn:
        await db_conn.execute(sql, (peak_price, int(trailing_activated), token_address, chain, user_id))
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


async def save_whale_event(event: dict) -> bool:
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
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.rowcount > 0


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


async def is_token_already_bought(contract_address: str, chain: str, user_id: int = 0) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cur1 = await db.execute(
            "SELECT 1 FROM open_positions WHERE token_address = ? AND chain = ? AND user_id = ? LIMIT 1",
            (contract_address, chain, user_id),
        )
        if await cur1.fetchone():
            return True
        cur2 = await db.execute(
            "SELECT 1 FROM completed_trades WHERE token_address = ? AND chain = ? AND user_id = ? LIMIT 1",
            (contract_address, chain, user_id),
        )
        if await cur2.fetchone():
            return True
    return False


async def save_user_wallet(user_id: int, public_key: str, encrypted_private_key: str, encrypted_seed_phrase: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR REPLACE INTO user_wallets (user_id, public_key, encrypted_private_key, encrypted_seed_phrase) VALUES (?, ?, ?, ?)",
            (user_id, public_key, encrypted_private_key, encrypted_seed_phrase),
        )
        await db.commit()
    logger.info("Saved wallet for user %d (pubkey %s)", user_id, public_key[:16] + "...")


async def get_user_wallet(user_id: int) -> dict | None:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_wallets WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
    return dict(row) if row else None


async def get_all_trading_users() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM user_wallets WHERE auto_trade = 1"
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def set_auto_trade(user_id: int, enabled: bool):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE user_wallets SET auto_trade = ? WHERE user_id = ?",
            (1 if enabled else 0, user_id),
        )
        await db.commit()
    logger.info("Set auto_trade=%s for user %d", enabled, user_id)


async def delete_user_wallet(user_id: int) -> bool:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "DELETE FROM user_wallets WHERE user_id = ?", (user_id,)
        )
        await db.commit()
        removed = cursor.rowcount > 0
    if removed:
        logger.info("Deleted wallet for user %d", user_id)
    return removed


async def migrate_legacy_positions(admin_user_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "UPDATE open_positions SET user_id = ? WHERE user_id = 0",
            (admin_user_id,),
        )
        await db.execute(
            "UPDATE completed_trades SET user_id = ? WHERE user_id = 0",
            (admin_user_id,),
        )
        await db.commit()
    logger.info("Migrated legacy positions/trades to admin user %d", admin_user_id)


async def upsert_bot_chat(chat_id: int, chat_type: str = "private", title: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(
            "INSERT OR REPLACE INTO bot_chats (chat_id, chat_type, title) VALUES (?, ?, ?)",
            (chat_id, chat_type, title),
        )
        await db.commit()


async def remove_bot_chat(chat_id: int):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute("DELETE FROM bot_chats WHERE chat_id = ?", (chat_id,))
        await db.commit()
    logger.info("Removed bot chat %d", chat_id)


async def get_all_bot_chats() -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM bot_chats ORDER BY added_at")
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def record_fee(
    user_id: int,
    token_symbol: str,
    trade_profit: float,
    fee_amount: float,
    fee_pct: float,
    tx_hash: str = "",
    status: str = "pending",
) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "INSERT INTO fee_ledger (user_id, token_symbol, trade_profit_native, "
            "fee_amount_native, fee_percent, fee_tx_hash, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, token_symbol, trade_profit, fee_amount, fee_pct, tx_hash, status),
        )
        await db.commit()
        row_id = cursor.lastrowid
    logger.debug("Recorded fee entry #%d for user %d (%s)", row_id, user_id, token_symbol)
    return row_id


async def update_fee_status(fee_id: int, status: str, tx_hash: str = ""):
    async with aiosqlite.connect(str(DB_PATH)) as db:
        if tx_hash:
            await db.execute(
                "UPDATE fee_ledger SET status = ?, fee_tx_hash = ? WHERE id = ?",
                (status, tx_hash, fee_id),
            )
        else:
            await db.execute(
                "UPDATE fee_ledger SET status = ? WHERE id = ?",
                (status, fee_id),
            )
        await db.commit()
    logger.debug("Updated fee #%d status=%s", fee_id, status)


async def get_fee_stats() -> dict:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT "
            "  COALESCE(SUM(CASE WHEN status='collected' THEN fee_amount_native ELSE 0 END), 0) AS total_collected, "
            "  COALESCE(SUM(CASE WHEN status='pending' OR status='submitted' THEN fee_amount_native ELSE 0 END), 0) AS total_pending, "
            "  COALESCE(SUM(CASE WHEN status='failed' THEN fee_amount_native ELSE 0 END), 0) AS total_failed, "
            "  COUNT(*) AS count "
            "FROM fee_ledger"
        )
        row = await cursor.fetchone()
    if row is None:
        return {"total_collected": 0, "total_pending": 0, "total_failed": 0, "count": 0}
    return dict(row)


async def count_open_positions(user_id: int) -> int:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM open_positions WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
    return row[0] if row else 0


async def get_daily_realized_loss(user_id: int) -> float:
    """Return total realized loss (as a positive number) for today's trades."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        cursor = await db.execute(
            """
            SELECT COALESCE(SUM(buy_amount_native - sell_amount_native), 0)
            FROM completed_trades
            WHERE user_id = ?
              AND DATE(closed_at) = DATE('now')
              AND sell_amount_native < buy_amount_native
            """,
            (user_id,),
        )
        row = await cursor.fetchone()
    return row[0] if row and row[0] else 0.0


async def get_fee_history(limit: int = 20) -> list[dict]:
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM fee_ledger ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_trade_stats(user_id: int | None = None, days: int | None = None) -> dict:
    """
    Get comprehensive trade statistics.
    If user_id is None, returns stats for all users.
    If days is set, only includes trades from the last N days.
    """
    conditions = []
    params = []

    if user_id is not None:
        conditions.append("user_id = ?")
        params.append(user_id)
    if days is not None:
        conditions.append("closed_at >= datetime('now', ?)")
        params.append(f"-{days} days")

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(f"""
            SELECT
                COUNT(*) as total_trades,
                COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as winning_trades,
                COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN 1 ELSE 0 END), 0) as losing_trades,
                COALESCE(AVG(roi_percent), 0) as avg_roi,
                COALESCE(MAX(roi_percent), 0) as best_roi,
                COALESCE(MIN(roi_percent), 0) as worst_roi,
                COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as total_pnl_native,
                COALESCE(SUM(CASE WHEN roi_percent > 0 THEN sell_amount_native - buy_amount_native ELSE 0 END), 0) as total_profit,
                COALESCE(SUM(CASE WHEN roi_percent <= 0 THEN sell_amount_native - buy_amount_native ELSE 0 END), 0) as total_loss,
                COALESCE(SUM(buy_amount_native), 0) as total_invested,
                COALESCE(AVG(duration_seconds), 0) as avg_duration_seconds
            FROM completed_trades {where}
        """, params)
        stats = dict(await cursor.fetchone())

        cursor = await db.execute(f"""
            SELECT token_symbol, roi_percent, buy_amount_native, sell_amount_native
            FROM completed_trades {where}
            ORDER BY roi_percent DESC LIMIT 1
        """, params)
        best = await cursor.fetchone()
        stats["best_trade"] = dict(best) if best else None

        cursor = await db.execute(f"""
            SELECT token_symbol, roi_percent, buy_amount_native, sell_amount_native
            FROM completed_trades {where}
            ORDER BY roi_percent ASC LIMIT 1
        """, params)
        worst = await cursor.fetchone()
        stats["worst_trade"] = dict(worst) if worst else None

        daily_conditions = ["closed_at >= datetime('now', '-7 days')"]
        daily_params = []
        if user_id is not None:
            daily_conditions.insert(0, "user_id = ?")
            daily_params.append(user_id)
        daily_where = f"WHERE {' AND '.join(daily_conditions)}"

        cursor = await db.execute(f"""
            SELECT
                DATE(closed_at) as day,
                COUNT(*) as trades,
                COALESCE(SUM(sell_amount_native - buy_amount_native), 0) as pnl,
                COALESCE(SUM(CASE WHEN roi_percent > 0 THEN 1 ELSE 0 END), 0) as wins
            FROM completed_trades
            {daily_where}
            GROUP BY DATE(closed_at)
            ORDER BY day DESC
        """, daily_params)
        stats["daily_pnl"] = [dict(r) for r in await cursor.fetchall()]

    total = stats["total_trades"]
    stats["win_rate"] = (stats["winning_trades"] / total * 100) if total > 0 else 0
    stats["profit_factor"] = abs(stats["total_profit"] / stats["total_loss"]) if stats["total_loss"] != 0 else float('inf') if stats["total_profit"] > 0 else 0

    return stats


async def save_scan_history(token: dict, was_bought: bool = False):
    """Log a scanned token to history for backtesting."""
    sql = """
        INSERT INTO scan_history
            (contract_address, chain, symbol, name, score, score_breakdown,
             market_cap, liquidity, volume_24h, price_usd, holders,
             buy_tax, sell_tax, source, was_bought)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    breakdown = token.get("score_breakdown")
    if isinstance(breakdown, dict):
        breakdown = json.dumps(breakdown)
    params = (
        token.get("contract_address", ""),
        token.get("chain", ""),
        token.get("symbol", ""),
        token.get("name", ""),
        token.get("score", 0),
        breakdown,
        token.get("market_cap", 0),
        token.get("liquidity", 0),
        token.get("volume_24h", 0),
        token.get("price_usd", 0),
        token.get("holders", 0),
        token.get("buy_tax", 0),
        token.get("sell_tax", 0),
        token.get("source", ""),
        1 if was_bought else 0,
    )
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.execute(sql, params)
        await db.commit()


async def save_scan_history_batch(tokens: list[dict], bought_addresses: set[str] | None = None):
    """Log a batch of scanned tokens to history."""
    if not tokens:
        return
    bought = bought_addresses or set()
    sql = """
        INSERT INTO scan_history
            (contract_address, chain, symbol, name, score, score_breakdown,
             market_cap, liquidity, volume_24h, price_usd, holders,
             buy_tax, sell_tax, source, was_bought)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = []
    for t in tokens:
        breakdown = t.get("score_breakdown")
        if isinstance(breakdown, dict):
            breakdown = json.dumps(breakdown)
        addr = t.get("contract_address", "").lower()
        rows.append((
            t.get("contract_address", ""),
            t.get("chain", ""),
            t.get("symbol", ""),
            t.get("name", ""),
            t.get("score", 0),
            breakdown,
            t.get("market_cap", 0),
            t.get("liquidity", 0),
            t.get("volume_24h", 0),
            t.get("price_usd", 0),
            t.get("holders", 0),
            t.get("buy_tax", 0),
            t.get("sell_tax", 0),
            t.get("source", ""),
            1 if addr in bought else 0,
        ))
    async with aiosqlite.connect(str(DB_PATH)) as db:
        await db.executemany(sql, rows)
        await db.commit()
    logger.debug("Saved %d tokens to scan_history", len(rows))


async def get_backtest_data(days: int = 7) -> dict:
    """Get scan history data for backtesting, cross-referenced with actual trade outcomes."""
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute("""
            SELECT
                CASE
                    WHEN score >= 80 THEN '80-100'
                    WHEN score >= 60 THEN '60-79'
                    WHEN score >= 40 THEN '40-59'
                    WHEN score >= 20 THEN '20-39'
                    ELSE '0-19'
                END as score_range,
                COUNT(*) as total_scanned,
                SUM(was_bought) as total_bought
            FROM scan_history
            WHERE scanned_at >= datetime('now', ?)
            GROUP BY score_range
            ORDER BY score_range DESC
        """, (f"-{days} days",))
        score_ranges = [dict(r) for r in await cursor.fetchall()]

        cursor = await db.execute("""
            SELECT
                COUNT(*) as total_scanned,
                SUM(was_bought) as total_bought,
                AVG(score) as avg_score,
                MIN(score) as min_score,
                MAX(score) as max_score
            FROM scan_history
            WHERE scanned_at >= datetime('now', ?)
        """, (f"-{days} days",))
        summary = dict(await cursor.fetchone())

        cursor = await db.execute("""
            SELECT ds.score, ds.symbol, ct.roi_percent,
                   ct.buy_amount_native, ct.sell_amount_native
            FROM (
                SELECT contract_address, chain, score, symbol,
                       ROW_NUMBER() OVER (PARTITION BY LOWER(contract_address), chain
                                          ORDER BY scanned_at DESC) as rn
                FROM scan_history
                WHERE scanned_at >= datetime('now', ?)
            ) ds
            INNER JOIN completed_trades ct ON LOWER(ds.contract_address) = LOWER(ct.token_address)
                AND ds.chain = ct.chain
            WHERE ds.rn = 1
            ORDER BY ds.score DESC
        """, (f"-{days} days",))
        trade_outcomes = [dict(r) for r in await cursor.fetchall()]

        thresholds = [20, 30, 40, 50, 60, 70, 80]
        simulations = []
        for threshold in thresholds:
            cursor = await db.execute("""
                SELECT COUNT(*) as would_buy
                FROM scan_history
                WHERE scanned_at >= datetime('now', ?)
                  AND score >= ?
            """, (f"-{days} days", threshold))
            row = await cursor.fetchone()
            would_buy = row[0] if row else 0

            cursor = await db.execute("""
                SELECT
                    COUNT(*) as traded,
                    COALESCE(SUM(CASE WHEN ct.roi_percent > 0 THEN 1 ELSE 0 END), 0) as wins,
                    COALESCE(AVG(ct.roi_percent), 0) as avg_roi
                FROM (
                    SELECT contract_address, chain, score,
                           ROW_NUMBER() OVER (PARTITION BY LOWER(contract_address), chain
                                              ORDER BY scanned_at DESC) as rn
                    FROM scan_history
                    WHERE scanned_at >= datetime('now', ?)
                      AND score >= ?
                ) ds
                INNER JOIN completed_trades ct ON LOWER(ds.contract_address) = LOWER(ct.token_address)
                    AND ds.chain = ct.chain
                WHERE ds.rn = 1
            """, (f"-{days} days", threshold))
            outcome = dict(await cursor.fetchone())

            simulations.append({
                "threshold": threshold,
                "would_buy": would_buy,
                "traded": outcome["traded"],
                "wins": outcome["wins"],
                "avg_roi": outcome["avg_roi"],
                "win_rate": (outcome["wins"] / outcome["traded"] * 100) if outcome["traded"] > 0 else 0,
            })

    return {
        "summary": summary,
        "score_ranges": score_ranges,
        "trade_outcomes": trade_outcomes,
        "simulations": simulations,
    }
