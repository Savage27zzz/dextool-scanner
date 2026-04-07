"""HTTP API server for external integrations (Discord, dashboards, webhooks)."""

import json
from aiohttp import web

import db
from config import API_KEY, API_PORT, CHAIN, NATIVE_SYMBOL, logger
from trader import create_user_trader
from honeypot import check_honeypot

routes = web.RouteTableDef()


def _check_auth(request: web.Request) -> bool:
    """Validate Bearer token from Authorization header."""
    if not API_KEY:
        return False
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip() == API_KEY
    return request.headers.get("X-API-Key", "") == API_KEY


def _require_auth(func):
    """Decorator to require API key authentication."""
    async def wrapper(request):
        if not _check_auth(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await func(request)
    return wrapper


@routes.get("/api/health")
async def health(request):
    """Health check — no auth required."""
    return web.json_response({"status": "ok"})


@routes.get("/api/status")
@_require_auth
async def status(request):
    """Bot status overview."""
    from bot import is_running
    positions = await db.get_open_positions()
    trading_users = await db.get_all_trading_users()
    return web.json_response({
        "running": is_running,
        "chain": CHAIN,
        "open_positions": len(positions),
        "active_traders": len(trading_users),
    })


@routes.get("/api/positions")
@_require_auth
async def positions(request):
    """Get all open positions. Optional ?user_id= filter."""
    user_id_param = request.query.get("user_id")
    user_id = int(user_id_param) if user_id_param else None
    positions = await db.get_open_positions(user_id=user_id)
    return web.json_response({"positions": positions})


@routes.get("/api/history")
@_require_auth
async def history(request):
    """Get completed trade history. Optional ?limit= and ?user_id= params."""
    limit = int(request.query.get("limit", "20"))
    limit = max(1, min(limit, 100))
    user_id_param = request.query.get("user_id")
    user_id = int(user_id_param) if user_id_param else None
    trades = await db.get_trade_history(limit=limit, user_id=user_id)
    return web.json_response({"trades": trades})


@routes.get("/api/stats")
@_require_auth
async def stats(request):
    """Get trade statistics. Optional ?days= and ?user_id= params."""
    days_param = request.query.get("days")
    days = int(days_param) if days_param else None
    user_id_param = request.query.get("user_id")
    user_id = int(user_id_param) if user_id_param else None
    trade_stats = await db.get_trade_stats(user_id=user_id, days=days)
    if trade_stats.get("profit_factor") == float('inf'):
        trade_stats["profit_factor"] = "infinity"
    return web.json_response({"stats": trade_stats})


@routes.get("/api/balance/{user_id}")
@_require_auth
async def balance(request):
    """Get wallet balance for a user."""
    try:
        user_id = int(request.match_info["user_id"])
    except (ValueError, KeyError):
        return web.json_response({"error": "Invalid user_id"}, status=400)

    wallet = await db.get_user_wallet(user_id)
    if not wallet:
        return web.json_response({"error": "User not found"}, status=404)

    ut = await create_user_trader(user_id)
    if ut is None:
        return web.json_response({"error": "Could not create trader"}, status=500)

    bal = await ut.get_balance()
    native = NATIVE_SYMBOL.get(CHAIN.upper(), "SOL")
    return web.json_response({
        "user_id": user_id,
        "public_key": wallet["public_key"],
        "balance": bal,
        "native": native,
        "auto_trade": bool(wallet.get("auto_trade", 1)),
    })


@routes.post("/api/buy")
@_require_auth
async def buy(request):
    """Trigger a manual buy. Body: {"token_address": "...", "amount": 0.5, "user_id": 123}"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token_address = body.get("token_address")
    amount = body.get("amount")
    user_id = body.get("user_id")

    if not token_address or not amount or not user_id:
        return web.json_response({"error": "Missing required fields: token_address, amount, user_id"}, status=400)

    try:
        amount = float(amount)
        user_id = int(user_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid amount or user_id"}, status=400)

    if amount <= 0:
        return web.json_response({"error": "Amount must be positive"}, status=400)

    wallet = await db.get_user_wallet(user_id)
    if not wallet:
        return web.json_response({"error": "User not found"}, status=404)

    already = await db.is_token_already_bought(token_address, CHAIN.upper(), user_id)
    if already:
        return web.json_response({"error": "Already holding this token"}, status=409)

    try:
        from config import MAX_OPEN_POSITIONS, MAX_DAILY_LOSS, MAX_BUY_AMOUNT
        open_count = await db.count_open_positions(user_id)
        if open_count >= MAX_OPEN_POSITIONS:
            return web.json_response({"error": f"Max positions reached ({open_count}/{MAX_OPEN_POSITIONS})"}, status=429)
        if MAX_DAILY_LOSS > 0:
            daily_loss = await db.get_daily_realized_loss(user_id)
            if daily_loss >= MAX_DAILY_LOSS:
                return web.json_response({"error": "Daily loss limit reached"}, status=429)
        if MAX_BUY_AMOUNT > 0 and amount > MAX_BUY_AMOUNT:
            amount = MAX_BUY_AMOUNT
    except (ImportError, AttributeError):
        pass

    import aiohttp
    async with aiohttp.ClientSession() as session:
        hp = await check_honeypot(session, CHAIN, token_address)
    if hp["is_honeypot"]:
        return web.json_response({
            "error": "Honeypot detected",
            "buy_tax": hp["buy_tax"],
            "sell_tax": hp["sell_tax"],
        }, status=403)

    user_trader = await create_user_trader(user_id)
    if user_trader is None:
        return web.json_response({"error": "Could not create trader"}, status=500)

    result = await user_trader.buy_token(token_address, amount)
    if result is None:
        return web.json_response({"error": "Buy failed"}, status=500)

    entry_liq = 0.0
    try:
        from dexscreener import get_token_liquidity
        async with aiohttp.ClientSession() as session:
            entry_liq = await get_token_liquidity(session, CHAIN, token_address)
    except Exception:
        pass

    position = {
        "token_address": token_address,
        "token_symbol": result.get("symbol", token_address[:8]),
        "chain": CHAIN.upper(),
        "entry_price": result["entry_price"],
        "tokens_received": result["tokens_received"],
        "buy_amount_native": result["amount_spent"],
        "buy_tx_hash": result["tx_hash"],
        "pair_address": "",
        "entry_liquidity": entry_liq,
        "user_id": user_id,
    }
    await db.save_open_position(position)

    logger.info("API buy: %s for user %d, tx=%s", token_address, user_id, result["tx_hash"])

    return web.json_response({
        "success": True,
        "tx_hash": result["tx_hash"],
        "tokens_received": result["tokens_received"],
        "entry_price": result["entry_price"],
        "amount_spent": result["amount_spent"],
    })


@routes.post("/api/webhook/alert")
@_require_auth
async def webhook_alert(request):
    """Receive external signal alerts. Body: {"token_address": "...", "signal": "buy|info", "source": "discord", "message": "..."}"""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token_address = body.get("token_address", "")
    signal = body.get("signal", "info")
    source = body.get("source", "external")
    message = body.get("message", "")

    logger.info("Webhook alert from %s: signal=%s, token=%s", source, signal, token_address)

    from bot import notifier
    if notifier:
        alert_msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📡 <b>EXTERNAL SIGNAL</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Source: {source}\n"
            f"Signal: {signal}\n"
        )
        if token_address:
            alert_msg += f"Token: <code>{token_address}</code>\n"
        if message:
            alert_msg += f"Message: {message}\n"
        alert_msg += "━━━━━━━━━━━━━━━━━━━━━━"
        await notifier.send_message(alert_msg)

    return web.json_response({"received": True, "signal": signal, "source": source})


def create_api_app() -> web.Application:
    """Create and configure the aiohttp web application."""
    app = web.Application()
    app.add_routes(routes)
    return app


async def start_api_server() -> web.AppRunner | None:
    """Start the API server as a background service. Returns the runner for cleanup."""
    from config import API_ENABLED, API_PORT
    if not API_ENABLED:
        logger.info("API server disabled (API_ENABLED=false)")
        return None

    if not API_KEY:
        logger.warning("API_KEY not set — API server will reject all authenticated requests")

    app = create_api_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    logger.info("API server started on port %d", API_PORT)
    return runner


async def stop_api_server(runner: web.AppRunner | None):
    """Stop the API server."""
    if runner:
        await runner.cleanup()
        logger.info("API server stopped")
