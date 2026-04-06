"""Honeypot detection via DexTools audit API with GoPlus Security fallback."""

import aiohttp

from config import CHAIN_MAP, DEXTOOLS_API_KEY, DEXTOOLS_BASE_URL, logger

_DT_HEADERS = {
    "X-API-Key": DEXTOOLS_API_KEY,
    "accept": "application/json",
}

_GOPLUS_CHAIN_IDS = {
    "SOL": "solana",
    "ETH": "1",
    "BSC": "56",
}

_HIGH_TAX_THRESHOLD = 50.0

_RESULT_UNKNOWN = {
    "is_honeypot": False,
    "buy_tax": 0.0,
    "sell_tax": 0.0,
    "checked": False,
}


def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


async def _check_dextools(session: aiohttp.ClientSession, chain: str, address: str) -> dict:
    chain_id = CHAIN_MAP.get(chain.upper(), "solana")
    url = f"{DEXTOOLS_BASE_URL}/token/{chain_id}/{address}/audit"
    try:
        async with session.get(
            url, headers=_DT_HEADERS, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                return dict(_RESULT_UNKNOWN)
            data = await resp.json()
    except Exception as exc:
        logger.debug("DexTools audit error for %s: %s", address, exc)
        return dict(_RESULT_UNKNOWN)

    audit = data.get("data", data)
    if not audit or not isinstance(audit, dict):
        return dict(_RESULT_UNKNOWN)

    is_hp = bool(audit.get("isHoneypot", False))
    buy_tax = _safe_float(audit.get("buyTax"))
    sell_tax = _safe_float(audit.get("sellTax"))

    if sell_tax > _HIGH_TAX_THRESHOLD:
        is_hp = True

    return {"is_honeypot": is_hp, "buy_tax": buy_tax, "sell_tax": sell_tax, "checked": True}


async def _check_goplus(session: aiohttp.ClientSession, chain: str, address: str) -> dict:
    gp_chain = _GOPLUS_CHAIN_IDS.get(chain.upper())
    if not gp_chain:
        return dict(_RESULT_UNKNOWN)

    if gp_chain == "solana":
        url = (
            "https://api.gopluslabs.com/api/v1/solana/token_security"
            f"?contract_addresses={address}"
        )
    else:
        url = (
            f"https://api.gopluslabs.com/api/v1/token_security/{gp_chain}"
            f"?contract_addresses={address}"
        )

    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return dict(_RESULT_UNKNOWN)
            data = await resp.json()
    except Exception as exc:
        logger.debug("GoPlus check error for %s: %s", address, exc)
        return dict(_RESULT_UNKNOWN)

    result = data.get("result", {})
    token_data = result.get(address.lower()) or result.get(address) or {}
    if not token_data:
        return dict(_RESULT_UNKNOWN)

    is_hp = str(token_data.get("is_honeypot", "0")) == "1"
    buy_tax = _safe_float(token_data.get("buy_tax")) * 100
    sell_tax = _safe_float(token_data.get("sell_tax")) * 100

    if str(token_data.get("cannot_sell_all", "0")) == "1":
        is_hp = True
    if sell_tax > _HIGH_TAX_THRESHOLD:
        is_hp = True

    return {"is_honeypot": is_hp, "buy_tax": buy_tax, "sell_tax": sell_tax, "checked": True}


async def check_honeypot(session: aiohttp.ClientSession, chain: str, address: str) -> dict:
    """
    Check if a token is a honeypot.  DexTools audit first, GoPlus fallback.

    Returns dict:
        is_honeypot (bool) — True if the token is a honeypot or has >50% sell tax
        buy_tax     (float) — buy tax percentage (0–100)
        sell_tax    (float) — sell tax percentage (0–100)
        checked     (bool)  — True if at least one API returned data
    """
    if DEXTOOLS_API_KEY:
        result = await _check_dextools(session, chain, address)
        if result["checked"]:
            if result["is_honeypot"]:
                logger.warning(
                    "HONEYPOT (DexTools): %s on %s — buy=%.1f%% sell=%.1f%%",
                    address, chain, result["buy_tax"], result["sell_tax"],
                )
            return result

    result = await _check_goplus(session, chain, address)
    if result["checked"]:
        if result["is_honeypot"]:
            logger.warning(
                "HONEYPOT (GoPlus): %s on %s — buy=%.1f%% sell=%.1f%%",
                address, chain, result["buy_tax"], result["sell_tax"],
            )
        return result

    logger.debug("Honeypot check inconclusive for %s on %s — no audit data", address, chain)
    return dict(_RESULT_UNKNOWN)
