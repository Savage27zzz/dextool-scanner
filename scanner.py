import asyncio
from datetime import datetime, timedelta, timezone

import aiohttp

import db
from config import (
    CHAIN,
    CHAIN_MAP,
    DEXTOOLS_API_KEY,
    DEXTOOLS_BASE_URL,
    MAX_MCAP,
    MIN_LIQUIDITY,
    MIN_MCAP,
    MIN_SCORE,
    logger,
)
from honeypot import check_honeypot
from scorer import score_token

_HEADERS = {
    "X-API-Key": DEXTOOLS_API_KEY,
    "accept": "application/json",
}

_MAX_RETRIES = 3
_BACKOFF_BASE = 2


async def _api_get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict | None:
    url = f"{DEXTOOLS_BASE_URL}{path}"
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.get(url, headers=_HEADERS, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug("DexTools API OK %s", path)
                    return data
                if resp.status == 429:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Rate-limited on %s – retrying in %ds (attempt %d/%d)", path, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                body = await resp.text()
                logger.error("DexTools API %d on %s: %s", resp.status, path, body[:300])
                return None
        except asyncio.TimeoutError:
            logger.warning("Timeout on %s (attempt %d/%d)", path, attempt, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE ** attempt)
        except Exception as exc:
            logger.error("Request error on %s: %s", path, exc)
            return None
    logger.error("Exhausted retries for %s", path)
    return None


def _chain_id(chain: str | None = None) -> str:
    return CHAIN_MAP.get((chain or CHAIN).upper(), "solana")


async def _fetch_hot_pools(session: aiohttp.ClientSession, chain_id: str) -> list[dict]:
    data = await _api_get(session, f"/ranking/hotpools/{chain_id}")
    if not data:
        return []
    results = data.get("data", data.get("results", []))
    if isinstance(results, dict):
        results = results.get("results", [])
    if not isinstance(results, list):
        return []
    return results


async def _fetch_new_tokens(session: aiohttp.ClientSession, chain_id: str) -> list[dict]:
    now = datetime.now(timezone.utc)
    from_dt = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to_dt = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"sort": "creationTime", "order": "desc", "from": from_dt, "to": to_dt}
    data = await _api_get(session, f"/token/{chain_id}", params=params)
    if not data:
        return []
    results = data.get("data", data.get("results", []))
    if isinstance(results, dict):
        results = results.get("tokens", results.get("results", []))
    if not isinstance(results, list):
        return []
    return results


async def _fetch_token_details(session: aiohttp.ClientSession, chain_id: str, address: str) -> dict | None:
    data = await _api_get(session, f"/token/{chain_id}/{address}")
    if not data:
        return None
    return data.get("data", data)


async def _fetch_token_info(session: aiohttp.ClientSession, chain_id: str, address: str) -> dict | None:
    data = await _api_get(session, f"/token/{chain_id}/{address}/info")
    if not data:
        return None
    return data.get("data", data)


async def _fetch_token_price(session: aiohttp.ClientSession, chain_id: str, address: str) -> dict | None:
    data = await _api_get(session, f"/token/{chain_id}/{address}/price")
    if not data:
        return None
    return data.get("data", data)


async def _fetch_token_audit(session: aiohttp.ClientSession, chain_id: str, address: str) -> dict | None:
    data = await _api_get(session, f"/token/{chain_id}/{address}/audit")
    if not data:
        return None
    return data.get("data", data)


async def _fetch_token_pools(session: aiohttp.ClientSession, chain_id: str, address: str) -> list[dict]:
    data = await _api_get(session, f"/token/{chain_id}/{address}/pools")
    if not data:
        return []
    results = data.get("data", data.get("results", []))
    if isinstance(results, dict):
        results = results.get("results", [])
    if not isinstance(results, list):
        return []
    return results


async def _fetch_pool_info(session: aiohttp.ClientSession, chain_id: str, pool_address: str) -> dict | None:
    data = await _api_get(session, f"/pool/{chain_id}/{pool_address}")
    if not data:
        return None
    return data.get("data", data)


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _safe_int(val, default: int = 0) -> int:
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _extract_address(raw) -> str | None:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        return raw.get("address") or raw.get("id")
    return None


async def _enrich_token(session: aiohttp.ClientSession, chain_id: str, address: str, chain: str) -> dict | None:
    details, price_data, audit, pools = await asyncio.gather(
        _fetch_token_details(session, chain_id, address),
        _fetch_token_price(session, chain_id, address),
        _fetch_token_audit(session, chain_id, address),
        _fetch_token_pools(session, chain_id, address),
        return_exceptions=True,
    )

    if isinstance(details, Exception) or details is None:
        logger.debug("No details for %s", address)
        return None
    if isinstance(price_data, Exception):
        price_data = None
    if isinstance(audit, Exception):
        audit = None
    if isinstance(pools, Exception):
        pools = []

    name = details.get("name") or details.get("symbol", "Unknown")
    symbol = details.get("symbol", "???")

    price_usd = _safe_float(price_data.get("price") if price_data else None)
    price_native = _safe_float(price_data.get("priceChain") if price_data else None)
    volume_24h = _safe_float(price_data.get("volume24h") if price_data else None)
    variation_24h = _safe_float(price_data.get("variation24h") if price_data else None)

    if audit and isinstance(audit, dict):
        is_honeypot = audit.get("isHoneypot", False)
        buy_tax = _safe_float(audit.get("buyTax"))
        sell_tax = _safe_float(audit.get("sellTax"))
        if is_honeypot or sell_tax > 50:
            logger.info("Skipping honeypot token %s (%s) — sell_tax=%.1f%%", symbol, address, sell_tax)
            return None
    else:
        hp = await check_honeypot(session, chain, address)
        if hp["is_honeypot"]:
            logger.info("Skipping honeypot token %s (%s) — fallback check", symbol, address)
            return None
        buy_tax = hp["buy_tax"]
        sell_tax = hp["sell_tax"]

    pair_address = ""
    liquidity = 0.0
    dex_pair_url = ""

    if pools and isinstance(pools, list) and len(pools) > 0:
        first_pool = pools[0]
        pair_addr_raw = first_pool.get("address") or _extract_address(first_pool)
        pair_address = pair_addr_raw or ""
        liq = first_pool.get("liquidity")
        if isinstance(liq, dict):
            liquidity = _safe_float(liq.get("usd", liq.get("total")))
        else:
            liquidity = _safe_float(liq)
        if pair_address:
            dex_pair_url = f"https://www.dextools.io/app/en/{chain_id}/pair-explorer/{pair_address}"

            pool_detail = await _fetch_pool_info(session, chain_id, pair_address)
            if pool_detail and isinstance(pool_detail, dict):
                pool_liq = pool_detail.get("liquidity")
                if isinstance(pool_liq, dict):
                    liquidity = max(liquidity, _safe_float(pool_liq.get("usd", pool_liq.get("total"))))
                elif pool_liq is not None:
                    liquidity = max(liquidity, _safe_float(pool_liq))

    total_supply = _safe_float(details.get("totalSupply"))
    mcap_raw = details.get("mcap") or details.get("marketCap")
    if mcap_raw is not None:
        market_cap = _safe_float(mcap_raw)
    elif price_usd > 0 and total_supply > 0:
        market_cap = price_usd * total_supply
    else:
        market_cap = 0.0

    holders = _safe_int(details.get("holders"))

    creation_time = details.get("creationTime") or details.get("createdAt")
    if creation_time:
        try:
            if isinstance(creation_time, str):
                ct = datetime.fromisoformat(creation_time.replace("Z", "+00:00"))
            else:
                ct = datetime.fromtimestamp(creation_time, tz=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - ct).total_seconds() / 3600
            if age_hours > 24:
                logger.debug("Skipping %s – age %.1fh > 24h", symbol, age_hours)
                return None
        except Exception:
            pass

    if market_cap > 0 and (market_cap < MIN_MCAP or market_cap > MAX_MCAP):
        logger.debug("Skipping %s – mcap $%.0f outside range", symbol, market_cap)
        return None

    if liquidity < MIN_LIQUIDITY:
        logger.debug("Skipping %s – liquidity $%.0f < min $%d", symbol, liquidity, MIN_LIQUIDITY)
        return None

    already = await db.is_token_already_bought(address, chain)
    if already:
        logger.debug("Skipping %s – already bought", symbol)
        return None

    info_data = await _fetch_token_info(session, chain_id, address)
    social_links = {}
    deployer_wallet = ""
    if info_data and isinstance(info_data, dict):
        social_links = {
            "website": info_data.get("website", ""),
            "twitter": info_data.get("twitter", ""),
            "telegram": info_data.get("telegram", ""),
            "discord": info_data.get("discord", ""),
        }
        social_links = {k: v for k, v in social_links.items() if v}
        deployer_wallet = info_data.get("owner", info_data.get("deployer", ""))

    dextools_url = dex_pair_url or f"https://www.dextools.io/app/en/{chain_id}/pair-explorer/{address}"

    return {
        "name": name,
        "symbol": symbol,
        "contract_address": address,
        "chain": chain,
        "chain_id": chain_id,
        "market_cap": market_cap,
        "liquidity": liquidity,
        "price_usd": price_usd,
        "price_native": price_native,
        "volume_24h": volume_24h,
        "price_change_24h": variation_24h,
        "holders": holders,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "dextools_url": dextools_url,
        "dex_pair_url": dex_pair_url,
        "deployer_wallet": deployer_wallet,
        "social_links": social_links,
        "pair_address": pair_address,
    }


async def scan_for_new_tokens(session: aiohttp.ClientSession, chain: str | None = None) -> list[dict]:
    chain = (chain or CHAIN).upper()
    chain_id = _chain_id(chain)
    logger.info("Scanning for new tokens on %s (%s) …", chain, chain_id)

    seen_addresses: set[str] = set()
    candidates: list[str] = []

    hot_pools = await _fetch_hot_pools(session, chain_id)
    for pool in hot_pools:
        main_token = pool.get("mainToken") or pool.get("token")
        addr = _extract_address(main_token)
        if addr and addr.lower() not in seen_addresses:
            seen_addresses.add(addr.lower())
            candidates.append(addr)

    new_tokens = await _fetch_new_tokens(session, chain_id)
    for tok in new_tokens:
        addr = _extract_address(tok) or tok.get("address")
        if addr and addr.lower() not in seen_addresses:
            seen_addresses.add(addr.lower())
            candidates.append(addr)

    logger.info("Found %d unique candidate tokens to evaluate", len(candidates))

    qualifying: list[dict] = []
    batch_size = 5
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        tasks = [_enrich_token(session, chain_id, addr, chain) for addr in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Enrichment error: %s", result)
                continue
            if result is not None:
                qualifying.append(result)
        if i + batch_size < len(candidates):
            await asyncio.sleep(0.5)

    logger.info("Scan complete – %d qualifying tokens found", len(qualifying))
    return qualifying


from dexscreener import scan_dexscreener


async def scan_all_sources(session: aiohttp.ClientSession, chain: str | None = None) -> list[dict]:
    """
    Scan both DexTools and DexScreener, merge and deduplicate results.
    DexTools is primary; DexScreener is fallback/supplement.
    """
    chain = (chain or CHAIN).upper()

    if DEXTOOLS_API_KEY:
        dextools_results, dexscreener_results = await asyncio.gather(
            scan_for_new_tokens(session, chain),
            scan_dexscreener(session, chain),
            return_exceptions=True,
        )
        if isinstance(dextools_results, Exception):
            logger.error("DexTools scanner failed: %s", dextools_results)
            dextools_results = []
    else:
        logger.info("DexTools API key not configured \u2014 using DexScreener only")
        dextools_results = []
        dexscreener_results = await scan_dexscreener(session, chain)

    if isinstance(dexscreener_results, Exception):
        logger.error("DexScreener scanner failed: %s", dexscreener_results)
        dexscreener_results = []

    seen_addresses: set[str] = set()
    merged: list[dict] = []

    for token in dextools_results:
        addr = token.get("contract_address", "").lower()
        if addr and addr not in seen_addresses:
            seen_addresses.add(addr)
            token["source"] = token.get("source", "dextools")
            merged.append(token)

    for token in dexscreener_results:
        addr = token.get("contract_address", "").lower()
        if addr and addr not in seen_addresses:
            seen_addresses.add(addr)
            merged.append(token)

    logger.info(
        "Combined scan: %d from DexTools + %d from DexScreener = %d merged (%d unique)",
        len(dextools_results), len(dexscreener_results), len(merged), len(seen_addresses),
    )

    # Score all tokens
    scored = [score_token(t) for t in merged]

    # Filter by minimum score
    qualified = [t for t in scored if t.get("score", 0) >= MIN_SCORE]

    # Sort by score descending (best first)
    qualified.sort(key=lambda t: t.get("score", 0), reverse=True)

    logger.info(
        "Scoring: %d scored, %d passed MIN_SCORE=%d (rejected %d)",
        len(scored), len(qualified), MIN_SCORE, len(scored) - len(qualified),
    )

    bought_addresses = {t.get("contract_address", "").lower() for t in qualified}
    try:
        await db.save_scan_history_batch(scored, bought_addresses=bought_addresses)
    except Exception as exc:
        logger.error("Failed to save scan history: %s", exc)

    return qualified
