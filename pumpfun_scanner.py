"""Pump.fun-focused token scanner with smart scoring and dip-buy logic."""

import asyncio
import time
from datetime import datetime, timezone

import aiohttp

import db
from config import (
    CHAIN,
    PUMPFUN_ENABLED,
    PUMPFUN_SCAN_INTERVAL,
    PUMPFUN_MIN_BONDING_PCT,
    PUMPFUN_MAX_AGE_HOURS,
    PUMPFUN_MIN_DEV_SCORE,
    PUMPFUN_DIP_BUY_PCT,
    MIN_LIQUIDITY,
    MIN_MCAP,
    MAX_MCAP,
    logger,
)
from pumpfun import fetch_graduated_tokens, fetch_bonding_tokens, get_token_creator
from smart_scorer import smart_score_token
from honeypot import check_honeypot
from dexscreener import get_token_liquidity

_price_tracker: dict[str, dict] = {}
_MAX_TRACKED = 200


async def scan_pumpfun_tokens(session: aiohttp.ClientSession) -> list[dict]:
    """
    Main pump.fun scan function. Discovers and scores tokens.

    Pipeline:
    1. Fetch recently graduated tokens (post-migration, 30min-2hr old)
    2. Fetch tokens near bonding completion (pre-migration, >75% bonded)
    3. For each candidate:
       a. Check if already bought or blacklisted
       b. Apply basic filters (liquidity, mcap)
       c. Run smart_score_token() for deep analysis
       d. Filter by PUMPFUN_MIN_DEV_SCORE
       e. Track price for dip-buy logic
    4. Return qualified tokens sorted by smart_score descending

    For pre-migration tokens (not yet graduated):
    - DON'T buy immediately — add to watchlist/price tracker
    - Monitor until they graduate, then evaluate

    For post-migration tokens:
    - Apply full scoring pipeline
    - If score >= PUMPFUN_MIN_DEV_SCORE, check dip-buy conditions
    """
    try:
        graduated, bonding = await asyncio.gather(
            fetch_graduated_tokens(session, PUMPFUN_MAX_AGE_HOURS),
            fetch_bonding_tokens(session, PUMPFUN_MIN_BONDING_PCT),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.error("Pump.fun discovery failed: %s", exc)
        return []

    if isinstance(graduated, Exception):
        logger.error("Graduated token fetch failed: %s", graduated)
        graduated = []
    if isinstance(bonding, Exception):
        logger.error("Bonding token fetch failed: %s", bonding)
        bonding = []

    for bt in bonding:
        mint = bt.get("mint", "")
        if not mint:
            continue
        try:
            wl_token = {
                "mint_address": mint,
                "name": bt.get("name", "Unknown"),
                "symbol": bt.get("symbol", "???"),
                "creator": bt.get("creator", ""),
                "smart_score": 0,
                "initial_price": 0,
                "peak_price": 0,
                "current_price": 0,
                "bonding_progress": bt.get("bonding_progress", 0),
                "is_graduated": 0,
            }
            await db.save_to_watchlist(wl_token)
            logger.debug(
                "Pre-migration token %s (%.1f%% bonded) added to watchlist",
                bt.get("symbol"), bt.get("bonding_progress", 0),
            )
        except Exception as exc:
            logger.debug("Failed to save bonding token %s to watchlist: %s", mint, exc)

    qualified: list[dict] = []

    batch_size = 3
    for i in range(0, len(graduated), batch_size):
        batch = graduated[i : i + batch_size]
        tasks = [_evaluate_graduated_token(session, tok) for tok in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Token evaluation error: %s", result)
                continue
            if result is not None:
                qualified.append(result)
        if i + batch_size < len(graduated):
            await asyncio.sleep(0.5)

    qualified.sort(key=lambda t: t.get("smart_score", 0), reverse=True)
    logger.info(
        "Pump.fun scan complete: %d graduated, %d bonding, %d qualified",
        len(graduated), len(bonding), len(qualified),
    )
    return qualified


async def _evaluate_graduated_token(
    session: aiohttp.ClientSession, tok: dict
) -> dict | None:
    mint = tok.get("mint", "")
    symbol = tok.get("symbol", "???")
    name = tok.get("name", "Unknown")

    if not mint:
        return None

    try:
        if await db.is_blacklisted(mint, "SOL"):
            logger.debug("Skipping blacklisted pump.fun token %s", symbol)
            return None
        if await db.is_token_already_bought(mint, "SOL"):
            logger.debug("Skipping already-bought pump.fun token %s", symbol)
            return None
    except Exception:
        pass

    liquidity = tok.get("liquidity", 0)
    market_cap = tok.get("market_cap", 0)

    if liquidity < MIN_LIQUIDITY:
        logger.debug("Pump.fun skip %s — liquidity $%.0f < min $%d", symbol, liquidity, MIN_LIQUIDITY)
        return None
    if market_cap > 0 and (market_cap < MIN_MCAP or market_cap > MAX_MCAP):
        logger.debug("Pump.fun skip %s — mcap $%.0f outside range", symbol, market_cap)
        return None

    try:
        hp_result = await check_honeypot(session, "SOL", mint)
        if hp_result.get("is_honeypot"):
            logger.info("Pump.fun skip %s — honeypot detected", symbol)
            return None
    except Exception as exc:
        logger.debug("Honeypot check failed for %s: %s", symbol, exc)
        hp_result = {}

    creator = tok.get("creator", "")
    if not creator:
        try:
            creator = await get_token_creator(session, mint) or ""
        except Exception:
            creator = ""

    base_token_data = {
        "liquidity": liquidity,
        "market_cap": market_cap,
        "volume_24h": tok.get("volume_24h", 0),
    }

    try:
        score_result = await smart_score_token(
            session,
            mint=mint,
            name=name,
            symbol=symbol,
            creator=creator,
            description="",
            base_token_data=base_token_data,
        )
    except Exception as exc:
        logger.error("Smart scoring failed for %s: %s", symbol, exc)
        return None

    smart_score = score_result.get("smart_score", 0)
    recommendation = score_result.get("recommendation", "SKIP")

    if smart_score < PUMPFUN_MIN_DEV_SCORE:
        logger.debug(
            "Pump.fun skip %s — smart_score %d < min %d",
            symbol, smart_score, PUMPFUN_MIN_DEV_SCORE,
        )
        return None

    price_usd = tok.get("price_usd", 0)
    if price_usd <= 0 and market_cap > 0:
        price_usd = market_cap / 1_000_000_000

    _update_price_tracker(mint, price_usd, smart_score)

    pair_address = tok.get("pair_address", "")
    dex_url = tok.get("dex_url", "")
    chain_slug = "solana"
    dextools_url = f"https://www.dextools.io/app/en/{chain_slug}/pair-explorer/{pair_address}" if pair_address else ""
    ds_url = dex_url or f"https://dexscreener.com/{chain_slug}/{mint}"

    token_out = {
        "name": name,
        "symbol": symbol,
        "contract_address": mint,
        "chain": "SOL",
        "chain_id": "solana",
        "market_cap": market_cap,
        "liquidity": liquidity,
        "price_usd": price_usd,
        "price_native": 0,
        "volume_24h": tok.get("volume_24h", 0),
        "price_change_24h": 0,
        "holders": 0,
        "buy_tax": hp_result.get("buy_tax", 0),
        "sell_tax": hp_result.get("sell_tax", 0),
        "dextools_url": dextools_url,
        "dex_pair_url": ds_url,
        "deployer_wallet": creator,
        "social_links": {},
        "pair_address": pair_address,
        "source": "pumpfun",
        "is_mintable": hp_result.get("is_mintable", False),
        "is_proxy": hp_result.get("is_proxy", False),
        "owner_change_balance": hp_result.get("owner_change_balance", False),
        "can_take_back_ownership": hp_result.get("can_take_back_ownership", False),
        "top_holder_percent": hp_result.get("top_holder_percent", 0),
        "holder_count": hp_result.get("holder_count", 0),
        "lp_locked": hp_result.get("lp_locked", False),
        "checked": hp_result.get("checked", False),
        "goplus_checked": hp_result.get("goplus_checked", False),
        "smart_score": smart_score,
        "score": smart_score,
        "recommendation": recommendation,
        "score_breakdown": score_result.get("score_breakdown", {}),
        "flags": score_result.get("flags", []),
        "dev_analysis": score_result.get("dev_analysis", {}),
        "bundle_analysis": score_result.get("bundle_analysis", {}),
        "holder_analysis": score_result.get("holder_analysis", {}),
        "age_minutes": tok.get("age_minutes", 0),
    }

    try:
        wl_token = {
            "mint_address": mint,
            "name": name,
            "symbol": symbol,
            "creator": creator,
            "smart_score": smart_score,
            "initial_price": price_usd,
            "peak_price": price_usd,
            "current_price": price_usd,
            "bonding_progress": 100.0,
            "is_graduated": 1,
        }
        await db.save_to_watchlist(wl_token)
    except Exception:
        pass

    try:
        await db.cache_dev_analysis(creator, score_result.get("dev_analysis", {}))
    except Exception:
        pass

    return token_out


async def check_dip_buys(session: aiohttp.ClientSession) -> list[dict]:
    """
    Check tracked tokens for dip-buy opportunities.

    Logic:
    1. For each tracked token in _price_tracker:
       a. Fetch current price via DexScreener or Jupiter
       b. Compare to initial_price
       c. If price dropped >= PUMPFUN_DIP_BUY_PCT from initial AND
          the token was previously scored well (smart_score >= 60):
          → Add to buy list
       d. If token is > 4 hours old, remove from tracker
    2. Return list of tokens to buy at the dip

    This implements the "buy at 20% dip" strategy.
    """
    _cleanup_tracker()

    if not _price_tracker:
        return []

    dip_buy_list: list[dict] = []
    mints = list(_price_tracker.keys())

    batch_size = 5
    for i in range(0, len(mints), batch_size):
        batch = mints[i : i + batch_size]
        tasks = [get_token_liquidity(session, "SOL", m) for m in batch]
        liq_results = await asyncio.gather(*tasks, return_exceptions=True)

        for mint, liq_result in zip(batch, liq_results):
            entry = _price_tracker.get(mint)
            if entry is None or entry.get("dip_bought"):
                continue

            initial_price = entry.get("initial_price", 0)
            smart_score = entry.get("smart_score", 0)

            if initial_price <= 0 or smart_score < 60:
                continue

            try:
                from dexscreener import _fetch_token_pairs, _safe_float
                pairs = await _fetch_token_pairs(session, "solana", mint)
                if not pairs:
                    continue
                best = max(pairs, key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")))
                current_price = _safe_float(best.get("priceUsd"))
            except Exception:
                continue

            if current_price <= 0:
                continue

            entry["current_price"] = current_price
            if current_price > entry.get("peak_price", 0):
                entry["peak_price"] = current_price

            dip_pct = ((initial_price - current_price) / initial_price) * 100
            if dip_pct >= PUMPFUN_DIP_BUY_PCT:
                try:
                    if await db.is_token_already_bought(mint, "SOL"):
                        continue
                    if await db.is_blacklisted(mint, "SOL"):
                        continue
                except Exception:
                    pass

                liq = best.get("liquidity") or {}
                liquidity = _safe_float(liq.get("usd"))
                market_cap = _safe_float(best.get("marketCap") or best.get("fdv"))
                base = best.get("baseToken", {})
                vol = best.get("volume") or {}

                pair_address = best.get("pairAddress", "")

                dip_token = {
                    "name": entry.get("name", base.get("name", "Unknown")),
                    "symbol": entry.get("symbol", base.get("symbol", "???")),
                    "contract_address": mint,
                    "chain": "SOL",
                    "chain_id": "solana",
                    "market_cap": market_cap,
                    "liquidity": liquidity,
                    "price_usd": current_price,
                    "price_native": 0,
                    "volume_24h": _safe_float(vol.get("h24")),
                    "price_change_24h": 0,
                    "holders": 0,
                    "buy_tax": 0,
                    "sell_tax": 0,
                    "dextools_url": f"https://www.dextools.io/app/en/solana/pair-explorer/{pair_address}" if pair_address else "",
                    "dex_pair_url": best.get("url", f"https://dexscreener.com/solana/{mint}"),
                    "deployer_wallet": entry.get("creator", ""),
                    "social_links": {},
                    "pair_address": pair_address,
                    "source": "pumpfun_dip",
                    "smart_score": smart_score,
                    "score": smart_score,
                    "recommendation": "BUY",
                    "score_breakdown": entry.get("score_breakdown", {}),
                    "flags": entry.get("flags", []),
                    "dip_percent": round(dip_pct, 1),
                    "initial_price": initial_price,
                    "is_mintable": False,
                    "is_proxy": False,
                    "owner_change_balance": False,
                    "can_take_back_ownership": False,
                    "top_holder_percent": 0,
                    "holder_count": 0,
                    "lp_locked": False,
                    "checked": False,
                    "goplus_checked": False,
                }

                dip_buy_list.append(dip_token)
                entry["dip_bought"] = True

                try:
                    await db.mark_watchlist_dip_bought(mint)
                except Exception:
                    pass

                logger.info(
                    "Dip-buy opportunity: %s — %.1f%% dip (initial $%.8g → $%.8g), score %d",
                    entry.get("symbol", mint[:12]),
                    dip_pct,
                    initial_price,
                    current_price,
                    smart_score,
                )

        if i + batch_size < len(mints):
            await asyncio.sleep(0.3)

    if dip_buy_list:
        logger.info("Dip-buy check: %d opportunities found", len(dip_buy_list))
    return dip_buy_list


def _update_price_tracker(mint: str, current_price: float, smart_score: int):
    """
    Update the price tracker for dip-buy monitoring.

    - If token not tracked: add it with initial_price = current_price
    - If tracked: update peak_price if current > peak
    - Evict oldest entries if tracker is full
    """
    if mint in _price_tracker:
        entry = _price_tracker[mint]
        if current_price > entry.get("peak_price", 0):
            entry["peak_price"] = current_price
        entry["current_price"] = current_price
        if smart_score > entry.get("smart_score", 0):
            entry["smart_score"] = smart_score
        return

    if len(_price_tracker) >= _MAX_TRACKED:
        oldest_key = min(
            _price_tracker,
            key=lambda k: _price_tracker[k].get("first_seen", float("inf")),
        )
        _price_tracker.pop(oldest_key, None)

    _price_tracker[mint] = {
        "initial_price": current_price,
        "peak_price": current_price,
        "current_price": current_price,
        "first_seen": time.monotonic(),
        "smart_score": smart_score,
        "dip_bought": False,
    }


def _cleanup_tracker():
    """Remove tokens older than 4 hours from the tracker."""
    cutoff = time.monotonic() - (4 * 3600)
    expired = [k for k, v in _price_tracker.items() if v.get("first_seen", 0) < cutoff]
    for k in expired:
        _price_tracker.pop(k, None)
    if expired:
        logger.debug("Cleaned up %d expired tokens from price tracker", len(expired))
