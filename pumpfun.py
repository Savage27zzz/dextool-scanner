"""Pump.fun token discovery and bonding curve monitoring."""

import asyncio
import time
from datetime import datetime, timezone

import aiohttp

from config import (
    PUMPFUN_MIN_BONDING_PCT,
    PUMPFUN_MAX_AGE_HOURS,
    logger,
)

PUMPFUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMPFUN_MIGRATION = "39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg"

DEXSCREENER_BASE = "https://api.dexscreener.com"
PUMPFUN_API_BASE = "https://frontend-api-v3.pump.fun"

_MAX_RETRIES = 3
_BACKOFF_BASE = 2


async def _pf_get(session: aiohttp.ClientSession, url: str, params: dict | None = None) -> dict | list | None:
    """GET with retry/backoff for pump.fun or DexScreener endpoints."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    return await resp.json()
                if resp.status == 429:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Rate-limited on %s – retry in %ds (attempt %d/%d)", url[:80], wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                if resp.status in (401, 403):
                    logger.debug("Auth required/forbidden on %s (%d)", url[:80], resp.status)
                    return None
                body = await resp.text()
                logger.error("HTTP %d on %s: %s", resp.status, url[:80], body[:300])
                return None
        except asyncio.TimeoutError:
            logger.warning("Timeout on %s (attempt %d/%d)", url[:80], attempt, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE ** attempt)
        except Exception as exc:
            logger.error("Request error on %s: %s", url[:80], exc)
            return None
    logger.error("Exhausted retries for %s", url[:80])
    return None


def _safe_float(val, default: float = 0.0) -> float:
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# --- Token Discovery ---

async def fetch_graduated_tokens(session: aiohttp.ClientSession, max_age_hours: float | None = None) -> list[dict]:
    """
    Find recently graduated pump.fun tokens using DexScreener.

    Strategy:
    1. Query DexScreener for latest Solana token profiles
    2. Filter for tokens that are very new (< max_age_hours old)
    3. Cross-reference with DexScreener pair data to get liquidity, volume, etc.

    This catches tokens that just migrated from pump.fun to Raydium/PumpSwap.
    """
    max_age = max_age_hours or PUMPFUN_MAX_AGE_HOURS
    now = datetime.now(timezone.utc)

    profiles = await _pf_get(session, f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    if not profiles or not isinstance(profiles, list):
        logger.warning("Failed to fetch DexScreener token profiles for graduated tokens")
        return []

    sol_tokens = [p for p in profiles if p.get("chainId") == "solana" and p.get("tokenAddress")]

    results: list[dict] = []
    batch_size = 5
    for i in range(0, len(sol_tokens), batch_size):
        batch = sol_tokens[i:i + batch_size]
        tasks = [
            _pf_get(session, f"{DEXSCREENER_BASE}/tokens/v1/solana/{t['tokenAddress']}")
            for t in batch
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        for token_profile, resp in zip(batch, responses):
            if isinstance(resp, Exception) or not resp:
                continue
            pairs = resp if isinstance(resp, list) else resp.get("pairs", []) if isinstance(resp, dict) else []
            if not pairs:
                continue

            best = max(pairs, key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")))

            created_at = best.get("pairCreatedAt")
            if not created_at:
                continue
            try:
                ct = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
                age_minutes = (now - ct).total_seconds() / 60
                age_hours = age_minutes / 60
            except (TypeError, ValueError, OSError):
                continue

            if age_hours > max_age:
                continue

            base = best.get("baseToken", {})
            liq = best.get("liquidity") or {}
            vol = best.get("volume") or {}

            results.append({
                "mint": base.get("address", token_profile["tokenAddress"]),
                "name": base.get("name", token_profile.get("name", "Unknown")),
                "symbol": base.get("symbol", token_profile.get("symbol", "???")),
                "creator": token_profile.get("creator", ""),
                "market_cap": _safe_float(best.get("marketCap") or best.get("fdv")),
                "liquidity": _safe_float(liq.get("usd")),
                "volume_24h": _safe_float(vol.get("h24")),
                "age_minutes": round(age_minutes, 1),
                "pair_address": best.get("pairAddress", ""),
                "dex_url": best.get("url", ""),
            })

        if i + batch_size < len(sol_tokens):
            await asyncio.sleep(0.5)

    logger.info("Graduated tokens found: %d (max age %.1fh)", len(results), max_age)
    return results


async def fetch_bonding_tokens(session: aiohttp.ClientSession, min_progress: int | None = None) -> list[dict]:
    """
    Find pump.fun tokens still in bonding curve that are close to graduating.

    Primary: GET https://frontend-api-v3.pump.fun/coins/latest
    Fallback: DexScreener search for very new, very low mcap Solana tokens.
    """
    min_prog = min_progress or PUMPFUN_MIN_BONDING_PCT
    results: list[dict] = []

    data = await _pf_get(session, f"{PUMPFUN_API_BASE}/coins/latest")
    if data and isinstance(data, list):
        for coin in data:
            v_sol = _safe_float(coin.get("virtual_sol_reserves"))
            v_tok = _safe_float(coin.get("virtual_token_reserves"))
            progress = calculate_bonding_progress(v_sol, v_tok) if v_sol > 0 else 0.0
            is_complete = coin.get("complete", False)

            if is_complete or progress < min_prog:
                continue

            created_ts = coin.get("created_timestamp")
            if created_ts and isinstance(created_ts, (int, float)):
                created_ts = int(created_ts)
            else:
                created_ts = int(time.time())

            results.append({
                "mint": coin.get("mint", ""),
                "name": coin.get("name", "Unknown"),
                "symbol": coin.get("symbol", "???"),
                "creator": coin.get("creator", ""),
                "bonding_progress": round(progress, 1),
                "market_cap": _safe_float(coin.get("usd_market_cap") or coin.get("market_cap")),
                "created_timestamp": created_ts,
            })

        logger.info("Bonding tokens from pump.fun API: %d (>=%d%% progress)", len(results), min_prog)
        return results

    logger.warning("pump.fun /coins/latest unavailable — falling back to DexScreener low-mcap search")

    profiles = await _pf_get(session, f"{DEXSCREENER_BASE}/token-profiles/latest/v1")
    if not profiles or not isinstance(profiles, list):
        logger.warning("DexScreener fallback also failed for bonding tokens")
        return []

    sol_tokens = [p for p in profiles if p.get("chainId") == "solana" and p.get("tokenAddress")]
    now = datetime.now(timezone.utc)

    for token_profile in sol_tokens[:30]:
        pair_data = await _pf_get(session, f"{DEXSCREENER_BASE}/tokens/v1/solana/{token_profile['tokenAddress']}")
        if not pair_data:
            continue
        pairs = pair_data if isinstance(pair_data, list) else pair_data.get("pairs", []) if isinstance(pair_data, dict) else []
        if not pairs:
            continue

        best = max(pairs, key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")))
        mcap = _safe_float(best.get("marketCap") or best.get("fdv"))

        if mcap > 69_000 or mcap <= 0:
            continue

        created_at = best.get("pairCreatedAt")
        if not created_at:
            continue
        try:
            ct = datetime.fromtimestamp(created_at / 1000, tz=timezone.utc)
            age_hours = (now - ct).total_seconds() / 3600
        except (TypeError, ValueError, OSError):
            continue

        if age_hours > 1.0:
            continue

        base = best.get("baseToken", {})
        estimated_progress = min(100.0, (mcap / 69_000) * 100)

        if estimated_progress < min_prog:
            continue

        results.append({
            "mint": base.get("address", token_profile["tokenAddress"]),
            "name": base.get("name", "Unknown"),
            "symbol": base.get("symbol", "???"),
            "creator": "",
            "bonding_progress": round(estimated_progress, 1),
            "market_cap": mcap,
            "created_timestamp": int(created_at / 1000),
        })
        await asyncio.sleep(0.3)

    logger.info("Bonding tokens from DexScreener fallback: %d (>=%d%% progress)", len(results), min_prog)
    return results


async def get_coin_details(session: aiohttp.ClientSession, mint: str) -> dict | None:
    """
    Get detailed info for a specific pump.fun coin.

    Try pump.fun API first, fall back to DexScreener pair data.
    """
    data = await _pf_get(session, f"{PUMPFUN_API_BASE}/coins/{mint}")
    if data and isinstance(data, dict) and data.get("mint"):
        v_sol = _safe_float(data.get("virtual_sol_reserves"))
        v_tok = _safe_float(data.get("virtual_token_reserves"))
        progress = calculate_bonding_progress(v_sol, v_tok) if v_sol > 0 else 0.0

        return {
            "mint": data.get("mint", mint),
            "name": data.get("name", "Unknown"),
            "symbol": data.get("symbol", "???"),
            "creator": data.get("creator", ""),
            "description": data.get("description", ""),
            "bonding_progress": round(progress, 1),
            "complete": bool(data.get("complete", False)),
            "market_cap": _safe_float(data.get("market_cap")),
            "usd_market_cap": _safe_float(data.get("usd_market_cap")),
            "created_timestamp": data.get("created_timestamp", 0),
            "twitter": data.get("twitter", ""),
            "telegram": data.get("telegram", ""),
            "website": data.get("website", ""),
            "image_uri": data.get("image_uri", ""),
        }

    logger.debug("pump.fun API unavailable for %s — trying DexScreener", mint)
    pair_data = await _pf_get(session, f"{DEXSCREENER_BASE}/tokens/v1/solana/{mint}")
    if not pair_data:
        return None
    pairs = pair_data if isinstance(pair_data, list) else pair_data.get("pairs", []) if isinstance(pair_data, dict) else []
    if not pairs:
        return None

    best = max(pairs, key=lambda p: _safe_float((p.get("liquidity") or {}).get("usd")))
    base = best.get("baseToken", {})
    info = best.get("info") or {}

    socials = {}
    for s in (info.get("socials") or []):
        socials[s.get("type", "")] = s.get("url", "")
    website_url = ""
    for w in (info.get("websites") or []):
        if w.get("url"):
            website_url = w["url"]
            break

    created_at = best.get("pairCreatedAt")
    created_ts = int(created_at / 1000) if created_at else 0

    return {
        "mint": base.get("address", mint),
        "name": base.get("name", "Unknown"),
        "symbol": base.get("symbol", "???"),
        "creator": "",
        "description": info.get("description", ""),
        "bonding_progress": 100.0,
        "complete": True,
        "market_cap": _safe_float(best.get("marketCap") or best.get("fdv")),
        "usd_market_cap": _safe_float(best.get("marketCap") or best.get("fdv")),
        "created_timestamp": created_ts,
        "twitter": socials.get("twitter", ""),
        "telegram": socials.get("telegram", ""),
        "website": website_url,
        "image_uri": info.get("imageUrl", ""),
    }


# --- Bonding Curve Analysis ---

def calculate_bonding_progress(virtual_sol_reserves: float, virtual_token_reserves: float) -> float:
    """
    Calculate bonding curve completion percentage.

    Pump.fun bonding curve completes at ~85 SOL raised / ~$69k market cap.
    The virtual reserves track progress:
    - Initial: ~30 SOL virtual reserves, ~1B token virtual reserves
    - Complete: ~115 SOL virtual reserves (85 real + 30 virtual), tokens depleted

    Formula: progress = ((virtual_sol_reserves - 30) / 85) * 100
    Clamp to 0-100%.
    """
    if virtual_sol_reserves <= 0:
        return 0.0
    sol_in_lamports = virtual_sol_reserves > 1_000_000
    if sol_in_lamports:
        virtual_sol_reserves = virtual_sol_reserves / 1e9
    progress = ((virtual_sol_reserves - 30) / 85) * 100
    return max(0.0, min(100.0, progress))


# --- Creator/Dev Wallet Extraction ---

async def get_token_creator(session: aiohttp.ClientSession, mint: str) -> str | None:
    """
    Get the creator/deployer wallet for a pump.fun token.

    Strategy:
    1. Try pump.fun coin details (has 'creator' field)
    2. Fall back to checking first transaction signatures for the mint via Helius
    """
    details = await get_coin_details(session, mint)
    if details and details.get("creator"):
        return details["creator"]

    try:
        import helius
        txns = await helius.get_wallet_transactions(session, mint, limit=5)
        if txns:
            last_tx = txns[-1] if isinstance(txns, list) else None
            if last_tx:
                native_transfers = last_tx.get("nativeTransfers", [])
                if native_transfers:
                    return native_transfers[0].get("fromUserAccount")
                fee_payer = last_tx.get("feePayer")
                if fee_payer:
                    return fee_payer
    except Exception as exc:
        logger.debug("Failed to get creator via Helius for %s: %s", mint, exc)

    return None
