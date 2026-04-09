"""Smart scorer with on-chain intelligence: dev analysis, bundle detection, holder analysis, narrative filtering."""

import asyncio
import re
import time
from datetime import datetime, timezone, timedelta
from collections import Counter

import aiohttp

from config import logger, PUMPFUN_MIN_DEV_SCORE, HELIUS_API_KEY
import helius

_HELIUS_SEM = asyncio.Semaphore(3)

_dev_cache: dict[str, tuple[float, dict]] = {}
_DEV_CACHE_TTL = 300  # 5 minutes


def _cached_dev(address: str) -> dict | None:
    entry = _dev_cache.get(address)
    if entry is None:
        return None
    ts, result = entry
    if time.monotonic() - ts > _DEV_CACHE_TTL:
        _dev_cache.pop(address, None)
        return None
    return result


def _store_dev(address: str, result: dict) -> None:
    _dev_cache[address] = (time.monotonic(), result)


def _default_dev_result() -> dict:
    return {
        "tokens_created": 0,
        "tokens_alive": 0,
        "success_rate": 0.0,
        "creation_frequency": 0.0,
        "quick_sells": 0,
        "dev_score": 50,
        "flags": [],
    }


def _default_bundle_result() -> dict:
    return {
        "is_bundled": False,
        "bundle_wallets": 0,
        "bundle_percent": 0.0,
        "common_funders": [],
        "early_buyer_count": 0,
        "bundle_score": 50,
        "flags": [],
    }


def _default_holder_result() -> dict:
    return {
        "top10_percent": 0.0,
        "top_holder_percent": 0.0,
        "small_entry_count": 0,
        "recent_sellers": 0,
        "avg_hold_time_minutes": 0.0,
        "holder_score": 50,
        "flags": [],
    }


# --- Dev Wallet Analysis ---

async def analyze_dev_wallet(session: aiohttp.ClientSession, creator_address: str) -> dict:
    """
    Analyze the dev/creator wallet's history to assess trustworthiness.
    """
    cached = _cached_dev(creator_address)
    if cached is not None:
        logger.debug("Dev analysis cache hit for %s", creator_address[:12])
        return cached

    result = _default_dev_result()

    if not HELIUS_API_KEY:
        logger.warning("No Helius API key — returning neutral dev score for %s", creator_address[:12])
        return result

    try:
        async with _HELIUS_SEM:
            txns = await helius.get_wallet_transactions(session, creator_address, limit=100)
    except Exception as exc:
        logger.error("Failed to fetch dev wallet txns for %s: %s", creator_address[:12], exc)
        _store_dev(creator_address, result)
        return result

    if not txns:
        result["dev_score"] = 50
        result["flags"].append("no_history")
        _store_dev(creator_address, result)
        return result

    token_mints: list[dict] = []
    swaps: list[dict] = []
    now = datetime.now(timezone.utc)

    for tx in txns:
        tx_type = tx.get("type", "")
        tx_source = tx.get("source", "")
        ts = tx.get("timestamp", 0)

        if tx_type == "TOKEN_MINT" or tx_source == "PUMP_FUN" and tx_type in ("TOKEN_MINT", "CREATE"):
            token_mints.append(tx)
        elif tx_type == "SWAP":
            swaps.append(tx)

    result["tokens_created"] = len(token_mints)

    if token_mints:
        earliest_ts = min(t.get("timestamp", 0) for t in token_mints)
        latest_ts = max(t.get("timestamp", 0) for t in token_mints)
        if earliest_ts > 0:
            try:
                earliest_dt = datetime.fromtimestamp(earliest_ts, tz=timezone.utc)
                days_span = max(1.0, (now - earliest_dt).total_seconds() / 86400)
                result["creation_frequency"] = len(token_mints) / min(days_span, 7.0)
            except (TypeError, ValueError, OSError):
                result["creation_frequency"] = 0.0

    created_mints: set[str] = set()
    for tx in token_mints:
        for tt in tx.get("tokenTransfers", []):
            mint = tt.get("mint", "")
            if mint:
                created_mints.add(mint)

    quick_sells = 0
    if created_mints and swaps:
        creation_times: dict[str, int] = {}
        for tx in token_mints:
            ts = tx.get("timestamp", 0)
            for tt in tx.get("tokenTransfers", []):
                mint = tt.get("mint", "")
                if mint and ts > 0:
                    creation_times[mint] = ts

        for swap in swaps:
            swap_ts = swap.get("timestamp", 0)
            for tt in swap.get("tokenTransfers", []):
                mint = tt.get("mint", "")
                if mint in creation_times:
                    c_ts = creation_times[mint]
                    if 0 < swap_ts - c_ts < 600:
                        from_acct = tt.get("fromUserAccount", "")
                        if from_acct == creator_address:
                            quick_sells += 1
                            break

    result["quick_sells"] = quick_sells

    alive_count = 0
    checked_mints = list(created_mints)[:10]
    for mint in checked_mints:
        try:
            async with _HELIUS_SEM:
                supply = await helius.get_token_supply(session, mint)
            if supply and float(supply.get("uiAmount", 0)) > 0:
                alive_count += 1
        except Exception:
            pass

    result["tokens_alive"] = alive_count
    total = result["tokens_created"]
    if total > 0:
        result["success_rate"] = alive_count / min(total, len(checked_mints))
    else:
        result["success_rate"] = 0.0

    flags: list[str] = []
    if total > 10 and result["success_rate"] < 0.2:
        flags.append("rug_factory")
    if result["creation_frequency"] > 3:
        flags.append("high_frequency_deployer")
    if quick_sells > 0:
        flags.append("quick_seller")
    if total == 0:
        flags.append("unknown_dev")
    result["flags"] = flags

    result["dev_score"] = _score_dev(result)
    _store_dev(creator_address, result)
    return result


def _score_dev(analysis: dict) -> int:
    """
    Score dev wallet 0-100.

    Scoring:
    - tokens_created == 0: 50 (unknown dev, neutral)
    - tokens_created 1-3 with good success rate (>50%): 80+
    - tokens_created > 10 with low success rate (<20%): 10 (rug factory)
    - creation_frequency > 3/day: heavy penalty (-30)
    - quick_sells > 0: penalty (-20 per quick sell, max -60)
    - success_rate > 70%: bonus (+20)
    """
    created = analysis.get("tokens_created", 0)
    success = analysis.get("success_rate", 0.0)
    freq = analysis.get("creation_frequency", 0.0)
    qs = analysis.get("quick_sells", 0)

    if created == 0:
        return 50

    if created <= 3 and success > 0.5:
        score = 80
    elif created <= 3:
        score = 60
    elif created <= 10:
        score = max(20, int(60 * success))
    else:
        score = max(10, int(50 * success))

    if freq > 3:
        score -= 30
    elif freq > 1:
        score -= 10

    qs_penalty = min(60, qs * 20)
    score -= qs_penalty

    if success > 0.7:
        score += 20

    return max(0, min(100, score))


# --- Bundle/Insider Detection ---

async def detect_bundles(session: aiohttp.ClientSession, mint: str, creator: str) -> dict:
    """
    Detect coordinated buying (bundles/insiders) in early token transactions.
    """
    result = _default_bundle_result()

    if not HELIUS_API_KEY:
        logger.warning("No Helius API key — returning neutral bundle score for %s", mint[:12])
        return result

    try:
        async with _HELIUS_SEM:
            txns = await helius.get_wallet_transactions(session, mint, limit=100)
    except Exception as exc:
        logger.error("Failed to fetch token txns for bundle detection %s: %s", mint[:12], exc)
        return result

    if not txns:
        return result

    sorted_txns = sorted(txns, key=lambda t: t.get("timestamp", 0))
    creation_ts = sorted_txns[0].get("timestamp", 0) if sorted_txns else 0
    cutoff_ts = creation_ts + 300  # first 5 minutes

    early_buyers: set[str] = set()
    for tx in sorted_txns:
        ts = tx.get("timestamp", 0)
        if ts > cutoff_ts:
            break
        if tx.get("type") != "SWAP":
            continue
        for tt in tx.get("tokenTransfers", []):
            to_acct = tt.get("toUserAccount", "")
            if to_acct and to_acct != creator:
                early_buyers.add(to_acct)

    early_buyers_list = list(early_buyers)[:20]
    result["early_buyer_count"] = len(early_buyers_list)

    if len(early_buyers_list) < 2:
        result["bundle_score"] = 90
        return result

    funder_map: dict[str, list[str]] = {}
    for buyer in early_buyers_list:
        try:
            async with _HELIUS_SEM:
                buyer_txns = await helius.get_wallet_transactions(session, buyer, limit=20)
        except Exception:
            continue

        if not buyer_txns:
            continue

        for tx in buyer_txns:
            for nt in tx.get("nativeTransfers", []):
                from_acct = nt.get("fromUserAccount", "")
                to_acct = nt.get("toUserAccount", "")
                amount = nt.get("amount", 0)
                if to_acct == buyer and from_acct and amount > 0 and from_acct != buyer:
                    if from_acct not in funder_map:
                        funder_map[from_acct] = []
                    if buyer not in funder_map[from_acct]:
                        funder_map[from_acct].append(buyer)
                    break

    common_funders = [f for f, buyers in funder_map.items() if len(buyers) >= 3]
    bundle_wallets_set: set[str] = set()
    for funder in common_funders:
        bundle_wallets_set.update(funder_map[funder])

    result["common_funders"] = common_funders
    result["bundle_wallets"] = len(bundle_wallets_set)
    result["is_bundled"] = len(common_funders) > 0

    if bundle_wallets_set:
        try:
            async with _HELIUS_SEM:
                holders = await helius.get_token_largest_accounts(session, mint)
            async with _HELIUS_SEM:
                supply_data = await helius.get_token_supply(session, mint)
            total_supply = float(supply_data.get("uiAmount", 0)) if supply_data else 0

            if total_supply > 0 and holders:
                bundle_amount = 0.0
                holder_addresses = {h["address"] for h in holders}
                for h in holders:
                    if h["address"] in bundle_wallets_set or h["address"] in holder_addresses:
                        ui_amount = float(h.get("uiAmount", 0) or 0)
                        if h["address"] in bundle_wallets_set:
                            bundle_amount += ui_amount
                result["bundle_percent"] = (bundle_amount / total_supply) * 100 if total_supply > 0 else 0.0
        except Exception as exc:
            logger.debug("Bundle percent calculation failed for %s: %s", mint[:12], exc)

    flags: list[str] = []
    if len(common_funders) >= 2 or result["bundle_wallets"] >= 5:
        flags.append("heavy_bundle")
    elif result["is_bundled"]:
        flags.append("insider_cluster")
    if result["bundle_percent"] > 20:
        flags.append("high_bundle_concentration")
    result["flags"] = flags

    if result["bundle_wallets"] >= 5 or result["bundle_percent"] > 30:
        result["bundle_score"] = 10
    elif result["is_bundled"] and result["bundle_percent"] > 15:
        result["bundle_score"] = 30
    elif result["is_bundled"]:
        result["bundle_score"] = 50
    else:
        result["bundle_score"] = 90

    return result


# --- Top Holder Analysis ---

async def analyze_holders(session: aiohttp.ClientSession, mint: str) -> dict:
    """
    Analyze top 10 token holders to assess distribution and risk.
    """
    result = _default_holder_result()

    if not HELIUS_API_KEY:
        logger.warning("No Helius API key — returning neutral holder score for %s", mint[:12])
        return result

    try:
        async with _HELIUS_SEM:
            holders = await helius.get_token_largest_accounts(session, mint)
        async with _HELIUS_SEM:
            supply_data = await helius.get_token_supply(session, mint)
    except Exception as exc:
        logger.error("Failed to fetch holder data for %s: %s", mint[:12], exc)
        return result

    if not holders or not supply_data:
        return result

    total_supply = float(supply_data.get("uiAmount", 0) or 0)
    if total_supply <= 0:
        return result

    top10 = holders[:10]
    top10_total = sum(float(h.get("uiAmount", 0) or 0) for h in top10)
    result["top10_percent"] = round((top10_total / total_supply) * 100, 2)

    if top10:
        top1_amount = float(top10[0].get("uiAmount", 0) or 0)
        result["top_holder_percent"] = round((top1_amount / total_supply) * 100, 2)

    recent_sellers = 0
    small_entry = 0
    hold_times: list[float] = []
    now = datetime.now(timezone.utc)

    for holder in top10[:5]:
        addr = holder.get("address", "")
        if not addr:
            continue
        try:
            async with _HELIUS_SEM:
                h_txns = await helius.get_wallet_transactions(session, addr, limit=20)
        except Exception:
            continue
        if not h_txns:
            continue

        has_recent_sell = False
        earliest_buy_ts = 0
        entry_sol = 0.0

        for tx in h_txns:
            tx_type = tx.get("type", "")
            ts = tx.get("timestamp", 0)

            if tx_type == "SWAP":
                for tt in tx.get("tokenTransfers", []):
                    if tt.get("mint") == mint:
                        from_acct = tt.get("fromUserAccount", "")
                        to_acct = tt.get("toUserAccount", "")

                        if to_acct == addr:
                            if earliest_buy_ts == 0 or ts < earliest_buy_ts:
                                earliest_buy_ts = ts
                            for nt in tx.get("nativeTransfers", []):
                                if nt.get("fromUserAccount") == addr:
                                    entry_sol += abs(nt.get("amount", 0)) / 1e9

                        elif from_acct == addr:
                            if ts > 0:
                                try:
                                    tx_time = datetime.fromtimestamp(ts, tz=timezone.utc)
                                    if (now - tx_time).total_seconds() < 3600:
                                        has_recent_sell = True
                                except (TypeError, ValueError, OSError):
                                    pass

        if has_recent_sell:
            recent_sellers += 1
        if 0 < entry_sol < 0.1:
            small_entry += 1
        if earliest_buy_ts > 0:
            try:
                buy_dt = datetime.fromtimestamp(earliest_buy_ts, tz=timezone.utc)
                hold_minutes = (now - buy_dt).total_seconds() / 60
                hold_times.append(hold_minutes)
            except (TypeError, ValueError, OSError):
                pass

    result["recent_sellers"] = recent_sellers
    result["small_entry_count"] = small_entry
    result["avg_hold_time_minutes"] = round(sum(hold_times) / len(hold_times), 1) if hold_times else 0.0

    flags: list[str] = []
    if result["top_holder_percent"] > 20:
        flags.append("whale_dominant")
    if result["top10_percent"] > 60:
        flags.append("concentrated_supply")
    if small_entry >= 3:
        flags.append("many_small_entries")
    if recent_sellers >= 2:
        flags.append("recent_dumps")
    result["flags"] = flags

    score = 80
    if result["top_holder_percent"] > 30:
        score -= 30
    elif result["top_holder_percent"] > 20:
        score -= 20
    elif result["top_holder_percent"] > 10:
        score -= 10

    if result["top10_percent"] > 70:
        score -= 20
    elif result["top10_percent"] > 50:
        score -= 10

    if recent_sellers >= 2:
        score -= 15
    elif recent_sellers >= 1:
        score -= 5

    if small_entry >= 3:
        score -= 10

    result["holder_score"] = max(0, min(100, score))
    return result


# --- Narrative Uniqueness ---

OVERUSED_NARRATIVES = [
    "pepe", "doge", "shib", "inu", "elon", "trump", "biden",
    "cat", "dog", "baby", "safe", "moon", "rocket", "gem",
    "ai", "gpt", "bot", "agent",
]

_recent_names: list[str] = []
_MAX_RECENT = 500


def check_narrative_uniqueness(name: str, symbol: str, description: str = "") -> dict:
    """
    Check if the token's narrative/theme is original or a copycat.
    """
    global _recent_names

    combined = f"{name} {symbol} {description}".lower()
    matched: list[str] = []
    for narrative in OVERUSED_NARRATIVES:
        if narrative in combined:
            matched.append(narrative)

    similar_recent: list[str] = []
    name_lower = f"{name} {symbol}".lower()
    for recent in _recent_names:
        if not recent or not name_lower:
            continue
        if name_lower in recent or recent in name_lower:
            similar_recent.append(recent)
            continue
        name_words = set(name_lower.split())
        recent_words = set(recent.split())
        overlap = name_words & recent_words - {"the", "a", "of", "to", "in"}
        if len(overlap) >= 2:
            similar_recent.append(recent)

    score = 100
    score -= len(matched) * 15
    score -= len(similar_recent) * 10

    if len(matched) >= 3:
        score -= 20

    score = max(0, min(100, score))

    _recent_names.append(name_lower)
    if len(_recent_names) > _MAX_RECENT:
        _recent_names = _recent_names[-_MAX_RECENT:]

    return {
        "is_unique": score >= 60,
        "matched_narratives": matched,
        "similar_recent": similar_recent[:5],
        "narrative_score": score,
    }


# --- Composite Smart Score ---

CRITICAL_FLAGS = {"rug_factory", "heavy_bundle", "honeypot"}


async def smart_score_token(
    session: aiohttp.ClientSession,
    mint: str,
    name: str,
    symbol: str,
    creator: str,
    description: str = "",
    base_token_data: dict | None = None,
) -> dict:
    """
    Run full smart scoring pipeline on a token.

    Runs dev analysis, bundle detection, holder analysis, and narrative check
    IN PARALLEL for speed, then combines into a composite score.
    """
    if not HELIUS_API_KEY:
        logger.warning("Helius API key not configured — falling back to base scoring only for %s", symbol)
        base_score = 50
        if base_token_data:
            try:
                from scorer import score_token
                scored = score_token(dict(base_token_data))
                base_score = scored.get("score", 50)
            except Exception:
                pass
        narrative_result = check_narrative_uniqueness(name, symbol, description)
        return {
            "smart_score": base_score,
            "dev_analysis": _default_dev_result(),
            "bundle_analysis": _default_bundle_result(),
            "holder_analysis": _default_holder_result(),
            "narrative_analysis": narrative_result,
            "flags": ["no_helius_key"],
            "recommendation": "WATCH" if base_score >= 50 else "SKIP",
            "score_breakdown": {
                "dev": 15,
                "bundles": 12,
                "holders": 12,
                "narrative": int(narrative_result["narrative_score"] * 0.1),
                "base": int(base_score * 0.1),
            },
        }

    narrative_result = check_narrative_uniqueness(name, symbol, description)

    tasks = []
    if creator:
        tasks.append(analyze_dev_wallet(session, creator))
    else:
        async def _neutral_dev():
            return {**_default_dev_result(), "flags": ["unknown_dev"]}
        tasks.append(_neutral_dev())
    tasks.append(detect_bundles(session, mint, creator or ""))
    tasks.append(analyze_holders(session, mint))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    if isinstance(results[0], Exception):
        logger.error("Dev analysis failed for %s: %s", mint[:12], results[0])
        dev_result = _default_dev_result()
    else:
        dev_result = results[0]

    if isinstance(results[1], Exception):
        logger.error("Bundle detection failed for %s: %s", mint[:12], results[1])
        bundle_result = _default_bundle_result()
    else:
        bundle_result = results[1]

    if isinstance(results[2], Exception):
        logger.error("Holder analysis failed for %s: %s", mint[:12], results[2])
        holder_result = _default_holder_result()
    else:
        holder_result = results[2]

    dev_pts = int(dev_result.get("dev_score", 50) * 0.30)
    bundle_pts = int(bundle_result.get("bundle_score", 50) * 0.25)
    holder_pts = int(holder_result.get("holder_score", 50) * 0.25)
    narrative_pts = int(narrative_result.get("narrative_score", 50) * 0.10)

    base_pts = 5
    if base_token_data:
        liq = base_token_data.get("liquidity", 0)
        vol = base_token_data.get("volume_24h", 0)
        if liq >= 50_000:
            base_pts = 10
        elif liq >= 20_000:
            base_pts = 8
        elif liq >= 10_000:
            base_pts = 6
        if vol >= 50_000:
            base_pts = min(10, base_pts + 2)

    smart_score = dev_pts + bundle_pts + holder_pts + narrative_pts + base_pts
    smart_score = max(0, min(100, smart_score))

    all_flags = list(set(
        dev_result.get("flags", [])
        + bundle_result.get("flags", [])
        + holder_result.get("flags", [])
    ))

    has_critical = bool(CRITICAL_FLAGS & set(all_flags))

    if smart_score >= 70 and not has_critical:
        recommendation = "BUY"
    elif smart_score >= 50 and not has_critical:
        recommendation = "WATCH"
    else:
        recommendation = "SKIP"

    logger.info(
        "Smart score %s (%s): %d/100 [dev=%d bun=%d hold=%d nar=%d base=%d] → %s | flags=%s",
        symbol, mint[:12], smart_score, dev_pts, bundle_pts, holder_pts,
        narrative_pts, base_pts, recommendation, all_flags,
    )

    return {
        "smart_score": smart_score,
        "dev_analysis": dev_result,
        "bundle_analysis": bundle_result,
        "holder_analysis": holder_result,
        "narrative_analysis": narrative_result,
        "flags": all_flags,
        "recommendation": recommendation,
        "score_breakdown": {
            "dev": dev_pts,
            "bundles": bundle_pts,
            "holders": holder_pts,
            "narrative": narrative_pts,
            "base": base_pts,
        },
    }
