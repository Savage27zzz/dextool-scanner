from __future__ import annotations

import asyncio
import base64
import json
import time

import aiohttp
import base58
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts, TokenAccountOpts
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

from config import (
    BUY_PERCENT,
    CHAIN,
    PRIVATE_KEY,
    RPC_URL_BSC,
    RPC_URL_ETH,
    RPC_URL_SOL,
    SLIPPAGE,
    logger,
)

WSOL_MINT = "So11111111111111111111111111111111111111112"

_shared_client: AsyncClient | None = None


def _get_shared_client() -> AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = AsyncClient(RPC_URL_SOL, commitment=Confirmed)
    return _shared_client

JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
JUPITER_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
JUPITER_PRICE_URL = "https://api.jup.ag/price/v2"

SOLSCAN_TX_URL = "https://solscan.io/tx/"

UNISWAP_V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
PANCAKESWAP_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
WBNB_ADDRESS = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"

ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactETHForTokens",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForETH",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
        ],
        "name": "getAmountsOut",
        "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "WETH",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "factory",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "spender", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "owner", "type": "address"},
            {"internalType": "address", "name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]

MAX_UINT256 = 2**256 - 1
SOL_FEE_RESERVE = 0.01


def _load_solana_keypair(raw: str) -> Keypair:
    stripped = raw.strip()
    if stripped.startswith("["):
        byte_array = json.loads(stripped)
        return Keypair.from_bytes(bytes(byte_array))
    return Keypair.from_base58_string(stripped)


class SolanaTrader:
    def __init__(self, keypair: Keypair | None = None):
        if keypair is not None:
            self.keypair = keypair
        else:
            self.keypair = _load_solana_keypair(PRIVATE_KEY)
        self.client = _get_shared_client()
        self.wallet = str(self.keypair.pubkey())
        logger.info("SolanaTrader initialised – wallet %s", self.wallet)

    async def get_balance(self) -> float:
        try:
            resp = await self.client.get_balance(self.keypair.pubkey())
            return resp.value / 1e9
        except Exception as exc:
            logger.error("get_balance error: %s", exc)
            return 0.0

    async def get_token_balance(self, token_mint: str) -> tuple[float, int]:
        try:
            from solders.pubkey import Pubkey

            owner = self.keypair.pubkey()
            mint_pubkey = Pubkey.from_string(token_mint)

            resp = await self.client.get_token_accounts_by_owner_json_parsed(
                owner,
                TokenAccountOpts(mint=mint_pubkey),
            )

            if resp.value:
                for acct in resp.value:
                    parsed = acct.account.data.parsed
                    info = parsed.get("info", parsed) if isinstance(parsed, dict) else parsed
                    token_amount = info.get("tokenAmount", {})
                    ui_amount = float(token_amount.get("uiAmount", 0))
                    decimals = int(token_amount.get("decimals", 0))
                    raw_amount = int(token_amount.get("amount", 0))
                    return ui_amount, decimals
            return 0.0, 0
        except Exception as exc:
            logger.error("get_token_balance error for %s: %s", token_mint, exc)
            return 0.0, 0

    async def get_buy_amount(self) -> float:
        balance = await self.get_balance()
        amount = balance * (BUY_PERCENT / 100)
        amount = max(0, amount - SOL_FEE_RESERVE)
        return amount

    async def _get_mint_decimals(self, token_mint: str) -> int:
        """Get token decimals by reading the mint account directly from chain."""
        try:
            from solders.pubkey import Pubkey
            mint_pubkey = Pubkey.from_string(token_mint)
            resp = await self.client.get_account_info(mint_pubkey)
            if resp.value and resp.value.data:
                data = bytes(resp.value.data)
                if len(data) >= 45:
                    return data[44]
            logger.warning("Could not read mint decimals for %s, defaulting to 9", token_mint)
            return 9
        except Exception as exc:
            logger.warning("_get_mint_decimals error for %s: %s – defaulting to 9", token_mint, exc)
            return 9

    async def _get_price_via_quote(self, token_mint: str) -> float:
        """Get SOL-per-token price using Jupiter Quote API."""
        try:
            test_lamports = 100_000_000  # 0.1 SOL
            async with aiohttp.ClientSession() as session:
                params = {
                    "inputMint": WSOL_MINT,
                    "outputMint": token_mint,
                    "amount": str(test_lamports),
                    "slippageBps": "100",
                }
                async with session.get(JUPITER_QUOTE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("Jupiter quote API returned %d for %s", resp.status, token_mint)
                        return 0.0
                    quote = await resp.json()
                    out_amount = int(quote.get("outAmount", 0))
                    if out_amount <= 0:
                        return 0.0

                    decimals = await self._get_mint_decimals(token_mint)

                    sol_amount = test_lamports / 1e9
                    tokens_human = out_amount / (10 ** decimals)

                    if tokens_human <= 0:
                        return 0.0

                    price = sol_amount / tokens_human
                    logger.debug("Price for %s: %.12f SOL (decimals=%d)", token_mint, price, decimals)
                    return price
        except Exception as exc:
            logger.error("_get_price_via_quote error for %s: %s", token_mint, exc)
            return 0.0

    async def _get_price_via_jupiter_api(self, token_mint: str) -> float:
        """Fallback: use Jupiter Price API v2 to get SOL-denominated price."""
        try:
            async with aiohttp.ClientSession() as session:
                params = {"ids": token_mint, "vsToken": WSOL_MINT}
                async with session.get(JUPITER_PRICE_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return 0.0
                    data = await resp.json()
                    token_data = data.get("data", {}).get(token_mint)
                    if token_data and token_data.get("price"):
                        return float(token_data["price"])
            return 0.0
        except Exception as exc:
            logger.error("Jupiter Price API fallback error for %s: %s", token_mint, exc)
            return 0.0

    async def get_token_price_via_jupiter(self, token_mint: str) -> float:
        """Return price as SOL per token (same unit as entry_price)."""
        price = await self._get_price_via_quote(token_mint)
        if price > 0:
            return price
        logger.warning("Quote-based price failed for %s, trying Price API fallback", token_mint)
        return await self._get_price_via_jupiter_api(token_mint)

    async def buy_token(self, token_mint: str, amount_sol: float) -> dict | None:
        try:
            amount_lamports = int(amount_sol * 1e9)
            slippage_bps = SLIPPAGE * 100

            async with aiohttp.ClientSession() as session:
                quote_params = {
                    "inputMint": WSOL_MINT,
                    "outputMint": token_mint,
                    "amount": str(amount_lamports),
                    "slippageBps": str(slippage_bps),
                }
                async with session.get(JUPITER_QUOTE_URL, params=quote_params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Jupiter quote failed %d: %s", resp.status, body[:300])
                        return None
                    quote = await resp.json()

                if "error" in quote:
                    logger.error("Jupiter quote error: %s", quote["error"])
                    return None

                out_amount = int(quote.get("outAmount", 0))
                logger.info("Jupiter quote: %s lamports -> %s token raw", amount_lamports, out_amount)

                swap_payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": "auto",
                }
                async with session.post(JUPITER_SWAP_URL, json=swap_payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Jupiter swap failed %d: %s", resp.status, body[:300])
                        return None
                    swap_data = await resp.json()

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                logger.error("No swapTransaction in Jupiter response")
                return None

            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])

            send_resp = await self.client.send_raw_transaction(
                bytes(signed_tx),
                opts=TxOpts(skip_preflight=True, max_retries=3),
            )

            signature = str(send_resp.value)
            logger.info("Buy tx sent: %s", signature)

            await self._confirm_transaction(signature)

            decimals = 0
            try:
                _, decimals = await self.get_token_balance(token_mint)
            except Exception:
                decimals = 9

            tokens_received = out_amount / (10**decimals) if decimals > 0 else out_amount / 1e9
            entry_price = amount_sol / tokens_received if tokens_received > 0 else 0

            logger.info(
                "Buy SUCCESS: %.4f tokens of %s at price %f SOL, sig %s",
                tokens_received, token_mint, entry_price, signature,
            )

            return {
                "tx_hash": signature,
                "tokens_received": tokens_received,
                "tokens_received_raw": out_amount,
                "entry_price": entry_price,
                "amount_spent": amount_sol,
                "decimals": decimals,
            }

        except Exception as exc:
            logger.error("buy_token error for %s: %s", token_mint, exc)
            return None

    async def sell_token(self, token_mint: str, token_amount_raw: int, decimals: int = 9) -> dict | None:
        try:
            slippage_bps = SLIPPAGE * 100
            balance_before = await self.get_balance()

            async with aiohttp.ClientSession() as session:
                quote_params = {
                    "inputMint": token_mint,
                    "outputMint": WSOL_MINT,
                    "amount": str(token_amount_raw),
                    "slippageBps": str(slippage_bps),
                }
                async with session.get(JUPITER_QUOTE_URL, params=quote_params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Jupiter sell quote failed %d: %s", resp.status, body[:300])
                        return None
                    quote = await resp.json()

                if "error" in quote:
                    logger.error("Jupiter sell quote error: %s", quote["error"])
                    return None

                out_lamports = int(quote.get("outAmount", 0))
                logger.info("Jupiter sell quote: %s raw -> %s lamports SOL", token_amount_raw, out_lamports)

                swap_payload = {
                    "quoteResponse": quote,
                    "userPublicKey": self.wallet,
                    "wrapAndUnwrapSol": True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": "auto",
                }
                async with session.post(JUPITER_SWAP_URL, json=swap_payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.error("Jupiter sell swap failed %d: %s", resp.status, body[:300])
                        return None
                    swap_data = await resp.json()

            swap_tx_b64 = swap_data.get("swapTransaction")
            if not swap_tx_b64:
                logger.error("No swapTransaction in Jupiter sell response")
                return None

            raw_tx = base64.b64decode(swap_tx_b64)
            tx = VersionedTransaction.from_bytes(raw_tx)
            signed_tx = VersionedTransaction(tx.message, [self.keypair])

            send_resp = await self.client.send_raw_transaction(
                bytes(signed_tx),
                opts=TxOpts(skip_preflight=True, max_retries=3),
            )

            signature = str(send_resp.value)
            logger.info("Sell tx sent: %s", signature)

            await self._confirm_transaction(signature)

            sol_received = out_lamports / 1e9
            tokens_human = token_amount_raw / (10**decimals)
            exit_price = sol_received / tokens_human if tokens_human > 0 else 0

            logger.info(
                "Sell SUCCESS: received %.6f SOL for %s, sig %s",
                sol_received, token_mint, signature,
            )

            return {
                "tx_hash": signature,
                "native_received": sol_received,
                "exit_price": exit_price,
            }

        except Exception as exc:
            logger.error("sell_token error for %s: %s", token_mint, exc)
            return None

    async def _confirm_transaction(self, signature: str, timeout: int = 60):
        from solders.signature import Signature

        sig = Signature.from_string(signature)
        start = time.time()
        while time.time() - start < timeout:
            resp = await self.client.get_signature_statuses([sig])
            statuses = resp.value
            if statuses and statuses[0] is not None:
                if statuses[0].err is None:
                    logger.debug("Transaction confirmed: %s", signature)
                    return
                else:
                    logger.error("Transaction error: %s – %s", signature, statuses[0].err)
                    return
            await asyncio.sleep(2)
        logger.warning("Transaction confirmation timed out: %s", signature)

    async def close(self):
        pass


async def create_user_trader(user_id: int) -> SolanaTrader | None:
    import db
    from crypto_utils import decrypt_key
    wallet_data = await db.get_user_wallet(user_id)
    if not wallet_data:
        return None
    raw_key = decrypt_key(wallet_data["encrypted_private_key"])
    keypair = Keypair.from_bytes(raw_key)
    return SolanaTrader(keypair=keypair)


class EVMTrader:
    def __init__(self):
        from web3 import Web3

        self.w3_eth = Web3(Web3.HTTPProvider(RPC_URL_ETH)) if RPC_URL_ETH else None
        self.w3_bsc = Web3(Web3.HTTPProvider(RPC_URL_BSC)) if RPC_URL_BSC else None
        active = self.w3_eth or self.w3_bsc
        if not active:
            raise EnvironmentError("No EVM RPC URL configured (RPC_URL_ETH or RPC_URL_BSC)")
        self.account = active.eth.account.from_key(PRIVATE_KEY)
        self.wallet = self.account.address
        logger.info("EVMTrader initialised – wallet %s", self.wallet)

    def get_w3(self, chain: str):
        from web3 import Web3

        if chain.upper() == "ETH":
            if not self.w3_eth:
                raise EnvironmentError("RPC_URL_ETH not configured")
            return self.w3_eth
        if not self.w3_bsc:
            raise EnvironmentError("RPC_URL_BSC not configured")
        return self.w3_bsc

    def get_router_address(self, chain: str) -> str:
        return UNISWAP_V2_ROUTER if chain.upper() == "ETH" else PANCAKESWAP_V2_ROUTER

    def get_router_contract(self, chain: str):
        w3 = self.get_w3(chain)
        addr = w3.to_checksum_address(self.get_router_address(chain))
        return w3.eth.contract(address=addr, abi=ROUTER_ABI)

    def get_wrapped_native(self, chain: str) -> str:
        return WETH_ADDRESS if chain.upper() == "ETH" else WBNB_ADDRESS

    async def get_balance(self, chain: str | None = None) -> float:
        chain = chain or CHAIN
        w3 = self.get_w3(chain)
        balance_wei = await asyncio.to_thread(w3.eth.get_balance, self.wallet)
        return float(w3.from_wei(balance_wei, "ether"))

    async def get_token_balance(self, token_address: str, chain: str | None = None) -> tuple[float, int]:
        chain = chain or CHAIN
        w3 = self.get_w3(chain)
        token = w3.eth.contract(address=w3.to_checksum_address(token_address), abi=ERC20_ABI)
        raw = await asyncio.to_thread(token.functions.balanceOf(self.wallet).call)
        decimals = await asyncio.to_thread(token.functions.decimals().call)
        return raw / (10**decimals), decimals

    async def get_buy_amount(self, chain: str | None = None) -> float:
        chain = chain or CHAIN
        balance = await self.get_balance(chain)
        return balance * (BUY_PERCENT / 100)

    async def get_token_price_via_jupiter(self, token_address: str) -> float:
        return 0.0

    async def get_token_price_onchain(self, token_address: str, chain: str) -> float:
        try:
            w3 = self.get_w3(chain)
            router = self.get_router_contract(chain)
            wrapped = w3.to_checksum_address(self.get_wrapped_native(chain))
            token_addr = w3.to_checksum_address(token_address)
            amount_in_wei = w3.to_wei(0.001, "ether")

            amounts = await asyncio.to_thread(
                router.functions.getAmountsOut(amount_in_wei, [wrapped, token_addr]).call
            )
            tokens_out = amounts[1]

            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            decimals = await asyncio.to_thread(token_contract.functions.decimals().call)

            amount_native = float(w3.from_wei(amount_in_wei, "ether"))
            amount_tokens = tokens_out / (10**decimals)

            if amount_tokens > 0:
                return amount_native / amount_tokens
            return 0.0
        except Exception as exc:
            logger.error("get_token_price_onchain error for %s: %s", token_address, exc)
            return 0.0

    async def buy_token(self, token_address: str, chain: str | None = None, amount_native: float = 0) -> dict | None:
        chain = chain or CHAIN
        try:
            w3 = self.get_w3(chain)
            router = self.get_router_contract(chain)
            wrapped = w3.to_checksum_address(self.get_wrapped_native(chain))
            token_addr = w3.to_checksum_address(token_address)
            amount_wei = w3.to_wei(amount_native, "ether")

            gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
            gas_price = int(gas_price * 1.2)

            try:
                amounts_out = await asyncio.to_thread(
                    router.functions.getAmountsOut(amount_wei, [wrapped, token_addr]).call
                )
                amount_out_min = int(amounts_out[1] * (100 - SLIPPAGE) / 100)
            except Exception:
                amount_out_min = 0

            deadline = int(time.time()) + 300
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.wallet)

            tx = router.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
                amount_out_min,
                [wrapped, token_addr],
                self.wallet,
                deadline,
            ).build_transaction({
                "from": self.wallet,
                "value": amount_wei,
                "gas": 300000,
                "gasPrice": gas_price,
                "nonce": nonce,
            })

            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            logger.info("EVM buy tx sent: %s", tx_hash_hex)

            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
            if receipt["status"] != 1:
                logger.error("EVM buy tx FAILED: %s", tx_hash_hex)
                return None

            tokens_received = 0
            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            decimals = await asyncio.to_thread(token_contract.functions.decimals().call)

            transfer_topic = w3.keccak(text="Transfer(address,address,uint256)")
            for log_entry in receipt.get("logs", []):
                if (
                    log_entry["address"].lower() == token_addr.lower()
                    and len(log_entry["topics"]) >= 3
                    and log_entry["topics"][0] == transfer_topic
                ):
                    to_addr = "0x" + log_entry["topics"][2].hex()[-40:]
                    if to_addr.lower() == self.wallet.lower():
                        tokens_received = int(log_entry["data"].hex(), 16)

            tokens_human = tokens_received / (10**decimals) if decimals else tokens_received
            entry_price = amount_native / tokens_human if tokens_human > 0 else 0

            logger.info("EVM buy SUCCESS: %.4f tokens of %s, tx %s", tokens_human, token_address, tx_hash_hex)

            return {
                "tx_hash": tx_hash_hex,
                "tokens_received": tokens_human,
                "tokens_received_raw": tokens_received,
                "entry_price": entry_price,
                "amount_spent": amount_native,
                "decimals": decimals,
            }
        except Exception as exc:
            logger.error("EVM buy_token error for %s: %s", token_address, exc)
            return None

    async def sell_token(self, token_address: str, chain: str | None = None, token_amount_raw: int = 0, decimals: int = 18) -> dict | None:
        chain = chain or CHAIN
        try:
            w3 = self.get_w3(chain)
            router = self.get_router_contract(chain)
            router_addr = w3.to_checksum_address(self.get_router_address(chain))
            wrapped = w3.to_checksum_address(self.get_wrapped_native(chain))
            token_addr = w3.to_checksum_address(token_address)

            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)

            allowance = await asyncio.to_thread(
                token_contract.functions.allowance(self.wallet, router_addr).call
            )
            if allowance < token_amount_raw:
                logger.info("Approving EVM router to spend token %s", token_address)
                gp = await asyncio.to_thread(lambda: w3.eth.gas_price)
                gp = int(gp * 1.2)
                nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.wallet)

                approve_tx = token_contract.functions.approve(
                    router_addr, MAX_UINT256
                ).build_transaction({
                    "from": self.wallet,
                    "gas": 100000,
                    "gasPrice": gp,
                    "nonce": nonce,
                })
                signed_approve = w3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
                approve_hash = await asyncio.to_thread(
                    w3.eth.send_raw_transaction, signed_approve.raw_transaction
                )
                approve_receipt = await asyncio.to_thread(
                    w3.eth.wait_for_transaction_receipt, approve_hash, timeout=120
                )
                if approve_receipt["status"] != 1:
                    logger.error("EVM approve tx FAILED for %s", token_address)
                    return None
                logger.info("EVM approve confirmed: %s", approve_hash.hex())

            gas_price = await asyncio.to_thread(lambda: w3.eth.gas_price)
            gas_price = int(gas_price * 1.2)
            nonce = await asyncio.to_thread(w3.eth.get_transaction_count, self.wallet)
            deadline = int(time.time()) + 300

            balance_before = await asyncio.to_thread(w3.eth.get_balance, self.wallet)

            tx = router.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
                token_amount_raw, 0, [token_addr, wrapped], self.wallet, deadline,
            ).build_transaction({
                "from": self.wallet,
                "gas": 350000,
                "gasPrice": gas_price,
                "nonce": nonce,
            })

            signed = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
            tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            logger.info("EVM sell tx sent: %s", tx_hash_hex)

            receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)
            if receipt["status"] != 1:
                logger.error("EVM sell tx FAILED: %s", tx_hash_hex)
                return None

            balance_after = await asyncio.to_thread(w3.eth.get_balance, self.wallet)
            gas_cost = receipt["gasUsed"] * receipt["effectiveGasPrice"]
            native_received = float(w3.from_wei(balance_after - balance_before + gas_cost, "ether"))
            tokens_human = token_amount_raw / (10**decimals)
            exit_price = native_received / tokens_human if tokens_human > 0 else 0

            logger.info("EVM sell SUCCESS: received %f native for %s, tx %s", native_received, token_address, tx_hash_hex)

            return {
                "tx_hash": tx_hash_hex,
                "native_received": native_received,
                "exit_price": exit_price,
            }
        except Exception as exc:
            logger.error("EVM sell_token error for %s: %s", token_address, exc)
            return None

    async def close(self):
        pass


def create_trader(chain: str | None = None):
    chain = (chain or CHAIN).upper()
    if chain == "SOL":
        return SolanaTrader()
    return EVMTrader()
