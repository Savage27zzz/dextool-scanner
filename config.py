import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def _env(key: str, default=None, cast=None, required=False):
    val = os.getenv(key, default)
    if required and val is None:
        raise EnvironmentError(f"Missing required env variable: {key}")
    if val is not None and cast is not None:
        try:
            val = cast(val)
        except (ValueError, TypeError) as exc:
            raise EnvironmentError(f"Invalid value for {key}: {exc}") from exc
    return val


TELEGRAM_BOT_TOKEN: str = _env("TELEGRAM_BOT_TOKEN", required=True)
TELEGRAM_CHAT_ID: int = _env("TELEGRAM_CHAT_ID", cast=int, required=True)
PRIVATE_KEY: str = _env("PRIVATE_KEY", required=True)
ENCRYPTION_KEY: str = _env("ENCRYPTION_KEY", required=True)

RPC_URL_SOL: str = _env("RPC_URL_SOL", default="https://api.mainnet-beta.solana.com")
RPC_URLS_SOL: list[str] = [u.strip() for u in RPC_URL_SOL.split(",") if u.strip()]
RPC_URL_ETH: str = _env("RPC_URL_ETH", default="")
RPC_URL_BSC: str = _env("RPC_URL_BSC", default="")

DEXTOOLS_API_KEY: str = _env("DEXTOOLS_API_KEY", default="")
DEXTOOLS_PLAN: str = _env("DEXTOOLS_PLAN", default="trial")

CHAIN: str = _env("CHAIN", default="SOL")
BUY_PERCENT: int = _env("BUY_PERCENT", default="50", cast=int)
TAKE_PROFIT: int = _env("TAKE_PROFIT", default="20", cast=int)
STOP_LOSS: int = _env("STOP_LOSS", default="-30", cast=int)
TRAILING_ENABLED: bool = _env("TRAILING_ENABLED", default="true", cast=lambda v: v.lower() in ("true", "1", "yes"))
TRAILING_DROP: int = _env("TRAILING_DROP", default="10", cast=int)
SLIPPAGE: int = _env("SLIPPAGE", default="15", cast=int)

MIN_LIQUIDITY: int = _env("MIN_LIQUIDITY", default="10000", cast=int)
MAX_MCAP: int = _env("MAX_MCAP", default="500000", cast=int)
MIN_MCAP: int = _env("MIN_MCAP", default="10000", cast=int)

SCAN_INTERVAL: int = _env("SCAN_INTERVAL", default="60", cast=int)
MONITOR_INTERVAL: int = _env("MONITOR_INTERVAL", default="30", cast=int)

MIN_SCORE: int = _env("MIN_SCORE", default="60", cast=int)

WHALE_TRACKING_ENABLED: bool = _env("WHALE_TRACKING_ENABLED", default="true", cast=lambda v: v.lower() in ("true", "1", "yes"))
WHALE_CHECK_INTERVAL: int = _env("WHALE_CHECK_INTERVAL", default="45", cast=int)
WHALE_MIN_SOL: float = _env("WHALE_MIN_SOL", default="1.0", cast=float)
WHALE_COPY_ENABLED: bool = _env("WHALE_COPY_ENABLED", default="false", cast=lambda v: v.lower() in ("true", "1", "yes"))
WHALE_COPY_AMOUNT: float = _env("WHALE_COPY_AMOUNT", default="0.1", cast=float)
WHALE_COPY_MAX_PER_TOKEN: int = _env("WHALE_COPY_MAX_PER_TOKEN", default="1", cast=int)

ANTIRUG_ENABLED: bool = _env("ANTIRUG_ENABLED", default="true", cast=lambda v: v.lower() in ("true", "1", "yes"))
ANTIRUG_MIN_LIQ: int = _env("ANTIRUG_MIN_LIQ", default="1000", cast=int)
ANTIRUG_LIQ_DROP_PCT: int = _env("ANTIRUG_LIQ_DROP_PCT", default="70", cast=int)

OPERATOR_FEE_PCT: float = _env("OPERATOR_FEE_PCT", default="5", cast=float)
OPERATOR_FEE_ENABLED: bool = _env("OPERATOR_FEE_ENABLED", default="true", cast=lambda v: v.lower() in ("true", "1", "yes"))

MAX_OPEN_POSITIONS: int = _env("MAX_OPEN_POSITIONS", default="3", cast=int)
MAX_DAILY_LOSS: float = _env("MAX_DAILY_LOSS", default="2.0", cast=float)  # in native token (SOL/ETH/BNB)
MAX_BUY_AMOUNT: float = _env("MAX_BUY_AMOUNT", default="1.0", cast=float)  # max per single buy in native token

COMPOUND_ENABLED: bool = _env("COMPOUND_ENABLED", default="false", cast=lambda v: v.lower() in ("true", "1", "yes"))
COMPOUND_PERCENT: int = _env("COMPOUND_PERCENT", default="50", cast=int)

API_ENABLED: bool = _env("API_ENABLED", default="false", cast=lambda v: v.lower() in ("true", "1", "yes"))
API_PORT: int = _env("API_PORT", default="8080", cast=int)
API_KEY: str = _env("API_KEY", default="")

ALERT_BROADCAST: bool = _env("ALERT_BROADCAST", default="false", cast=lambda v: v.lower() in ("true", "1", "yes"))

SNIPER_ENABLED: bool = _env("SNIPER_ENABLED", default="false", cast=lambda v: v.lower() in ("true", "1", "yes"))
SNIPER_CHECK_INTERVAL: int = _env("SNIPER_CHECK_INTERVAL", default="10", cast=int)
SNIPER_MIN_LIQUIDITY: int = _env("SNIPER_MIN_LIQUIDITY", default="1000", cast=int)

SELL_TIERS_RAW: str = _env("SELL_TIERS", default="")


def _parse_sell_tiers(raw: str) -> list[tuple[float, float]]:
    """Parse 'ROI:PERCENT,ROI:PERCENT' into sorted list of (roi_threshold, sell_percent)."""
    if not raw.strip():
        return []
    tiers = []
    for part in raw.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        roi_str, pct_str = part.split(":", 1)
        try:
            roi = float(roi_str)
            pct = float(pct_str)
            if 0 < pct <= 100 and roi > 0:
                tiers.append((roi, pct))
        except ValueError:
            continue
    tiers.sort(key=lambda t: t[0])
    return tiers


SELL_TIERS: list[tuple[float, float]] = _parse_sell_tiers(SELL_TIERS_RAW)

DEXTOOLS_BASE_URL = f"https://public-api.dextools.io/{DEXTOOLS_PLAN}/v2"

CHAIN_MAP = {
    "SOL": "solana",
    "ETH": "ether",
    "BSC": "bsc",
}

DS_CHAIN_MAP = {
    "SOL": "solana",
    "ETH": "ethereum",
    "BSC": "bsc",
}

NATIVE_SYMBOL = {
    "SOL": "SOL",
    "ETH": "ETH",
    "BSC": "BNB",
}

EXPLORER_TX = {
    "SOL": "https://solscan.io/tx/{}",
    "ETH": "https://etherscan.io/tx/{}",
    "BSC": "https://bscscan.com/tx/{}",
}


def _build_logger() -> logging.Logger:
    log = logging.getLogger("dextool_scanner")
    log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        DATA_DIR / "trading.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    log.addHandler(fh)
    log.addHandler(ch)
    return log


logger = _build_logger()
