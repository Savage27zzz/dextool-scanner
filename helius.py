"""Helius API client for Solana on-chain intelligence."""

import asyncio

import aiohttp

from config import HELIUS_API_KEY, HELIUS_RPC_URL, logger

HELIUS_API_BASE = "https://api.helius.xyz"

_MAX_RETRIES = 3
_BACKOFF_BASE = 2


async def _helius_get(session: aiohttp.ClientSession, path: str, params: dict | None = None) -> dict | list | None:
    """GET request to Helius REST API with api-key param."""
    if not HELIUS_API_KEY:
        logger.warning("HELIUS_API_KEY not configured — skipping Helius GET %s", path)
        return None
    url = f"{HELIUS_API_BASE}{path}"
    p = dict(params) if params else {}
    p["api-key"] = HELIUS_API_KEY
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.get(url, params=p, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.debug("Helius GET OK %s", path)
                    return data
                if resp.status == 429:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Helius rate-limited on %s – retry in %ds (attempt %d/%d)", path, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                body = await resp.text()
                logger.error("Helius %d on %s: %s", resp.status, path, body[:300])
                return None
        except asyncio.TimeoutError:
            logger.warning("Helius timeout on %s (attempt %d/%d)", path, attempt, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE ** attempt)
        except Exception as exc:
            logger.error("Helius request error on %s: %s", path, exc)
            return None
    logger.error("Exhausted retries for Helius GET %s", path)
    return None


async def _helius_rpc(session: aiohttp.ClientSession, method: str, params: list) -> dict | None:
    """JSON-RPC call to Helius enhanced RPC endpoint."""
    if not HELIUS_RPC_URL:
        logger.warning("HELIUS_RPC_URL not configured — skipping RPC %s", method)
        return None
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with session.post(
                HELIUS_RPC_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "error" in data and data["error"]:
                        logger.error("Helius RPC error on %s: %s", method, data["error"])
                        return None
                    logger.debug("Helius RPC OK %s", method)
                    return data.get("result")
                if resp.status == 429:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning("Helius RPC rate-limited on %s – retry in %ds (attempt %d/%d)", method, wait, attempt, _MAX_RETRIES)
                    await asyncio.sleep(wait)
                    continue
                body = await resp.text()
                logger.error("Helius RPC %d on %s: %s", resp.status, method, body[:300])
                return None
        except asyncio.TimeoutError:
            logger.warning("Helius RPC timeout on %s (attempt %d/%d)", method, attempt, _MAX_RETRIES)
            await asyncio.sleep(_BACKOFF_BASE ** attempt)
        except Exception as exc:
            logger.error("Helius RPC request error on %s: %s", method, exc)
            return None
    logger.error("Exhausted retries for Helius RPC %s", method)
    return None


# --- Transaction History ---

async def get_wallet_transactions(session: aiohttp.ClientSession, address: str, limit: int = 100) -> list[dict]:
    """
    Get parsed transaction history for a wallet using Helius Enhanced Transactions API.

    Endpoint: GET https://api.helius.xyz/v0/addresses/{address}/transactions?api-key={key}&limit={limit}

    Returns list of enhanced transaction objects with fields like:
    - type: "SWAP", "TRANSFER", "TOKEN_MINT", "UNKNOWN", etc.
    - source: "JUPITER", "RAYDIUM", "PUMP_FUN", etc.
    - timestamp, fee, signature
    - tokenTransfers: [{mint, fromUserAccount, toUserAccount, tokenAmount, ...}]
    - nativeTransfers: [{fromUserAccount, toUserAccount, amount}]
    - description: human-readable description
    """
    data = await _helius_get(session, f"/v0/addresses/{address}/transactions", {"limit": limit})
    if not data or not isinstance(data, list):
        return []
    return data


async def parse_transactions(session: aiohttp.ClientSession, signatures: list[str]) -> list[dict]:
    """
    Parse raw transaction signatures into enriched format.

    Endpoint: POST https://api.helius.xyz/v0/transactions?api-key={key}
    Body: {"transactions": [sig1, sig2, ...]}

    Max 100 signatures per request.
    """
    if not HELIUS_API_KEY:
        logger.warning("HELIUS_API_KEY not configured — skipping parse_transactions")
        return []
    if not signatures:
        return []
    url = f"{HELIUS_API_BASE}/v0/transactions"
    params = {"api-key": HELIUS_API_KEY}
    results: list[dict] = []
    for i in range(0, len(signatures), 100):
        batch = signatures[i:i + 100]
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                async with session.post(
                    url,
                    params=params,
                    json={"transactions": batch},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            results.extend(data)
                        logger.debug("Helius parse_transactions OK (%d sigs)", len(batch))
                        break
                    if resp.status == 429:
                        wait = _BACKOFF_BASE ** attempt
                        logger.warning("Helius parse rate-limited – retry in %ds (attempt %d/%d)", wait, attempt, _MAX_RETRIES)
                        await asyncio.sleep(wait)
                        continue
                    body = await resp.text()
                    logger.error("Helius parse_transactions %d: %s", resp.status, body[:300])
                    break
            except asyncio.TimeoutError:
                logger.warning("Helius parse_transactions timeout (attempt %d/%d)", attempt, _MAX_RETRIES)
                await asyncio.sleep(_BACKOFF_BASE ** attempt)
            except Exception as exc:
                logger.error("Helius parse_transactions error: %s", exc)
                break
    return results


# --- Token Holders (via standard RPC) ---

async def get_token_largest_accounts(session: aiohttp.ClientSession, mint: str) -> list[dict]:
    """
    Get largest token accounts for a mint using Solana RPC via Helius.

    Uses standard RPC method: getTokenLargestAccounts
    Returns list of {address, amount, decimals, uiAmount} for top holders.
    """
    result = await _helius_rpc(session, "getTokenLargestAccounts", [mint])
    if not result:
        return []
    accounts = result.get("value", [])
    holders = []
    for acct in accounts:
        holders.append({
            "address": acct.get("address", ""),
            "amount": acct.get("amount", "0"),
            "decimals": acct.get("decimals", 0),
            "uiAmount": acct.get("uiAmount", 0.0) or acct.get("uiAmountString", 0.0),
        })
    return holders


async def get_token_supply(session: aiohttp.ClientSession, mint: str) -> dict | None:
    """
    Get token supply info.

    Uses RPC method: getTokenSupply
    Returns {amount, decimals, uiAmount}
    """
    result = await _helius_rpc(session, "getTokenSupply", [mint])
    if not result:
        return None
    value = result.get("value", {})
    return {
        "amount": value.get("amount", "0"),
        "decimals": value.get("decimals", 0),
        "uiAmount": value.get("uiAmount", 0.0) or float(value.get("uiAmountString", "0")),
    }


# --- Account Info ---

async def get_account_info(session: aiohttp.ClientSession, address: str) -> dict | None:
    """
    Get account info via Helius RPC.
    Standard getAccountInfo call.
    """
    result = await _helius_rpc(session, "getAccountInfo", [address, {"encoding": "jsonParsed"}])
    return result
