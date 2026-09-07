from __future__ import annotations
import asyncio, base64
import httpx
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from .models import Candidate, PairSnapshot

DEX = "https://api.dexscreener.com"
BIRDEYE = "https://public-api.birdeye.so"

DEX_CHAIN = {
    "solana": "solana",
    "bsc": "bsc",
    "base": "base",
    "ethereum": "ethereum",
    "robinhood": "robinhood",
}
BIRDEYE_CHAIN = {
    "solana": "solana",
    "bsc": "bsc",
    "base": "base",
    "ethereum": "ethereum",
    "robinhood": "robinhood",
}

def _num(v, default=0.0):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default

def _int(v, default=0):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default

def _extract_items(payload):
    """Tolerate Birdeye response wrapper changes: data.items, data.tokens, data list, etc."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "tokens", "list", "result"):
            if isinstance(data.get(key), list):
                return data[key]
    return []

class MarketProviders:
    def __init__(self, birdeye_api_key: str = "", jupiter_api_key: str = ""):
        self.birdeye_api_key = birdeye_api_key
        self.jupiter_api_key = jupiter_api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": "MemeProspectBot/1.0"},
            follow_redirects=True,
        )

    async def close(self):
        await self.client.aclose()

    async def discover(self, chains: tuple[str, ...]) -> list[Candidate]:
        found: dict[tuple[str, str], Candidate] = {}
        if self.birdeye_api_key:
            tasks = [self._birdeye_new_listings(c) for c in chains if c in BIRDEYE_CHAIN]
            for batch in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(batch, list):
                    for x in batch:
                        found[(x.chain, x.address)] = x

        # Free fallback / supplement. These feeds are not a complete new-token universe.
        try:
            for x in await self._dex_latest_candidates():
                if x.chain in chains:
                    found.setdefault((x.chain, x.address), x)
        except Exception:
            pass
        return list(found.values())

    async def _birdeye_new_listings(self, chain: str) -> list[Candidate]:
        headers = {
            "X-API-KEY": self.birdeye_api_key,
            "x-chain": BIRDEYE_CHAIN[chain],
            "accept": "application/json",
        }
        params = {"limit": 20}
        if chain == "solana":
            params["meme_platform_enabled"] = "true"
        r = await self.client.get(f"{BIRDEYE}/defi/v2/tokens/new_listing", headers=headers, params=params)
        r.raise_for_status()
        items = _extract_items(r.json())
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            address = item.get("address") or item.get("tokenAddress") or item.get("token_address")
            if address:
                out.append(Candidate(chain=chain, address=str(address), source="birdeye_new_listing"))
        return out

    async def _dex_latest_candidates(self) -> list[Candidate]:
        urls = [
            f"{DEX}/token-profiles/latest/v1",
            f"{DEX}/token-boosts/latest/v1",
        ]
        batches = await asyncio.gather(*(self.client.get(u) for u in urls), return_exceptions=True)
        out = []
        for r in batches:
            if isinstance(r, Exception):
                continue
            r.raise_for_status()
            payload = r.json()
            if isinstance(payload, dict):
                payload = [payload]
            for item in payload or []:
                if not isinstance(item, dict):
                    continue
                chain = str(item.get("chainId") or "").lower()
                addr = item.get("tokenAddress")
                if chain in DEX_CHAIN.values() and addr:
                    out.append(Candidate(chain=chain, address=str(addr), source="dex_latest"))
        return out

    async def pair_snapshot(self, c: Candidate) -> PairSnapshot | None:
        chain = DEX_CHAIN.get(c.chain)
        if not chain:
            return None
        r = await self.client.get(f"{DEX}/token-pairs/v1/{chain}/{c.address}")
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list) or not pairs:
            return None

        # Pick the deepest pool for the candidate token.
        def liq(p):
            return _num((p.get("liquidity") or {}).get("usd"))
        p = max(pairs, key=liq)
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        candidate_is_base = str(base.get("address", "")).lower() == c.address.lower()
        token = base if candidate_is_base else quote
        txns = p.get("txns") or {}
        vol = p.get("volume") or {}
        pc = p.get("priceChange") or {}
        info = p.get("info") or {}
        socials = info.get("socials") or []
        boosts = p.get("boosts") or {}

        return PairSnapshot(
            chain=c.chain,
            token_address=c.address,
            token_name=str(token.get("name") or "Unknown"),
            token_symbol=str(token.get("symbol") or "?"),
            pair_address=str(p.get("pairAddress") or ""),
            dex_id=str(p.get("dexId") or ""),
            url=str(p.get("url") or ""),
            market_cap=_num(p.get("marketCap") or p.get("fdv")),
            fdv=_num(p.get("fdv")),
            liquidity_usd=liq(p),
            pair_created_at_ms=_int(p.get("pairCreatedAt")),
            price_usd=_num(p.get("priceUsd")),
            price_change_m5=_num(pc.get("m5")),
            price_change_h1=_num(pc.get("h1")),
            volume_m5=_num(vol.get("m5")),
            volume_h1=_num(vol.get("h1")),
            buys_m5=_int((txns.get("m5") or {}).get("buys")),
            sells_m5=_int((txns.get("m5") or {}).get("sells")),
            buys_h1=_int((txns.get("h1") or {}).get("buys")),
            sells_h1=_int((txns.get("h1") or {}).get("sells")),
            socials_count=len(socials),
            boosts_active=_int(boosts.get("active")),
            source=c.source,
            raw=p,
        )

    async def solana_wallet_balance(self, address: str, rpc_url: str = "https://api.mainnet-beta.solana.com") -> float:
        """Read the public SOL balance. This method cannot sign or send transactions."""
        if not address:
            return 0.0
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getBalance",
            "params": [address, {"commitment": "confirmed"}],
        }
        r = await self.client.post(rpc_url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        lamports = int(((data.get("result") or {}).get("value")) or 0)
        return lamports / 1_000_000_000


    async def solana_token_balance(
        self,
        owner: str,
        mint: str,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
    ) -> dict:
        """Read an SPL token balance for a public wallet. Never signs anything."""
        if not owner or not mint:
            return {"raw": 0, "ui": 0.0, "decimals": 0}

        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": "confirmed"},
            ],
        }
        r = await self.client.post(rpc_url, json=payload)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(str(data["error"]))

        raw_total = 0
        decimals = 0
        for row in ((data.get("result") or {}).get("value") or []):
            try:
                info = (
                    (((row.get("account") or {}).get("data") or {}).get("parsed") or {})
                    .get("info") or {}
                )
                amount = (info.get("tokenAmount") or {})
                raw_total += int(amount.get("amount") or 0)
                decimals = max(decimals, int(amount.get("decimals") or 0))
            except Exception:
                continue

        ui = raw_total / (10 ** decimals) if decimals >= 0 else 0.0
        return {"raw": raw_total, "ui": ui, "decimals": decimals}

    async def solana_sol_price_usd(self) -> float:
        """Approximate current SOL/USD from the deepest wrapped-SOL DEX Screener pool."""
        sol_mint = "So11111111111111111111111111111111111111112"
        r = await self.client.get(f"{DEX}/token-pairs/v1/solana/{sol_mint}")
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list) or not pairs:
            return 0.0

        def liq(p):
            return _num((p.get("liquidity") or {}).get("usd"))

        for p in sorted(pairs, key=liq, reverse=True):
            price = _num(p.get("priceUsd"))
            if price > 0:
                return price
        return 0.0

    @staticmethod
    def _account_key_text(key) -> str:
        if isinstance(key, str):
            return key
        if isinstance(key, dict):
            return str(key.get("pubkey") or "")
        return str(key or "")

    @staticmethod
    def _owned_token_raw(rows, owner: str, mint: str) -> tuple[int, int]:
        total = 0
        decimals = 0
        for row in rows or []:
            if str(row.get("owner") or "") != owner:
                continue
            if str(row.get("mint") or "") != mint:
                continue
            ui = row.get("uiTokenAmount") or {}
            total += int(ui.get("amount") or 0)
            decimals = max(decimals, int(ui.get("decimals") or 0))
        return total, decimals

    async def solana_recent_buy_fill(
        self,
        owner: str,
        mint: str,
        since_ts: int,
        rpc_url: str = "https://api.mainnet-beta.solana.com",
        limit: int = 20,
    ) -> dict | None:
        """
        Find a recent confirmed transaction where this public wallet gained the token.
        Returns an on-chain estimate of SOL spent and token amount received.
        """
        if not owner or not mint:
            return None

        sig_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getSignaturesForAddress",
            "params": [
                owner,
                {
                    "limit": int(limit),
                    "commitment": "confirmed",
                },
            ],
        }
        r = await self.client.post(rpc_url, json=sig_payload)
        r.raise_for_status()
        sig_data = r.json()
        if sig_data.get("error"):
            raise RuntimeError(str(sig_data["error"]))

        signatures = (sig_data.get("result") or [])
        for row in signatures:
            if row.get("err") is not None:
                continue
            block_time = int(row.get("blockTime") or 0)
            if block_time and block_time < int(since_ts) - 60:
                continue
            signature = str(row.get("signature") or "")
            if not signature:
                continue

            tx_payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [
                    signature,
                    {
                        "encoding": "jsonParsed",
                        "commitment": "confirmed",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            }
            txr = await self.client.post(rpc_url, json=tx_payload)
            txr.raise_for_status()
            tx = (txr.json().get("result") or {})
            if not tx:
                continue

            meta = tx.get("meta") or {}
            if meta.get("err") is not None:
                continue

            msg = ((tx.get("transaction") or {}).get("message") or {})
            keys = msg.get("accountKeys") or []
            wallet_index = None
            for i, key in enumerate(keys):
                if self._account_key_text(key) == owner:
                    wallet_index = i
                    break
            if wallet_index is None:
                continue

            pre_raw, pre_dec = self._owned_token_raw(
                meta.get("preTokenBalances"), owner, mint
            )
            post_raw, post_dec = self._owned_token_raw(
                meta.get("postTokenBalances"), owner, mint
            )
            token_delta_raw = post_raw - pre_raw
            decimals = max(pre_dec, post_dec)
            if token_delta_raw <= 0:
                continue

            pre_bal = meta.get("preBalances") or []
            post_bal = meta.get("postBalances") or []
            if wallet_index >= len(pre_bal) or wallet_index >= len(post_bal):
                continue

            fee_lamports = int(meta.get("fee") or 0)
            wallet_lamport_out = int(pre_bal[wallet_index]) - int(post_bal[wallet_index])
            # Remove the network fee from the SOL-spent estimate. ATA rent may still
            # make the estimate slightly high on a first-time token purchase.
            swap_lamports = max(0, wallet_lamport_out - fee_lamports)

            return {
                "signature": signature,
                "block_time": int(tx.get("blockTime") or block_time or 0),
                "token_delta_raw": int(token_delta_raw),
                "token_decimals": int(decimals),
                "token_ui": (
                    token_delta_raw / (10 ** decimals)
                    if decimals >= 0 else 0.0
                ),
                "sol_spent": swap_lamports / 1_000_000_000,
                "network_fee_sol": fee_lamports / 1_000_000_000,
            }

        return None


    def signer_status(self, private_key_b58: str, expected_address: str) -> dict:
        if not private_key_b58:
            return {"installed": False, "matches": False, "address": ""}
        try:
            kp = Keypair.from_base58_string(private_key_b58)
            address = str(kp.pubkey())
            return {
                "installed": True,
                "matches": bool(expected_address and address == expected_address),
                "address": address,
            }
        except Exception:
            return {"installed": True, "matches": False, "address": ""}

    def _jupiter_headers(self) -> dict:
        headers = {"accept": "application/json"}
        if self.jupiter_api_key:
            headers["x-api-key"] = self.jupiter_api_key
        return headers

    async def jupiter_swap(
        self,
        input_mint: str,
        output_mint: str,
        amount_raw: int,
        taker: str,
        private_key_b58: str,
        slippage_bps: int = 300,
    ) -> dict:
        if int(amount_raw) <= 0:
            raise RuntimeError("Swap amount must be positive.")

        signer = Keypair.from_base58_string(private_key_b58)
        if str(signer.pubkey()) != taker:
            raise RuntimeError("Signing key does not match configured wallet.")

        base_url = "https://api.jup.ag/swap/v2"
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(int(amount_raw)),
            "taker": taker,
            "slippageBps": str(int(slippage_bps)),
            # Avoid JupiterZ RFQ multi-signer flow in this Python bot.
            "excludeRouters": "jupiterz",
        }

        order_response = await self.client.get(
            f"{base_url}/order",
            params=params,
            headers=self._jupiter_headers(),
        )
        order_response.raise_for_status()
        order = order_response.json()

        transaction_b64 = str(order.get("transaction") or "")
        if not transaction_b64:
            raise RuntimeError(
                f"Jupiter order failed ({order.get('errorCode')}): "
                f"{order.get('errorMessage') or 'no executable transaction'}"
            )

        unsigned = VersionedTransaction.from_bytes(base64.b64decode(transaction_b64))
        signed = VersionedTransaction(unsigned.message, [signer])
        signed_b64 = base64.b64encode(bytes(signed)).decode("ascii")

        execute_response = await self.client.post(
            f"{base_url}/execute",
            headers={**self._jupiter_headers(), "Content-Type": "application/json"},
            json={
                "signedTransaction": signed_b64,
                "requestId": order.get("requestId"),
            },
        )
        execute_response.raise_for_status()
        result = execute_response.json()

        if str(result.get("status") or "").lower() != "success":
            raise RuntimeError(
                f"Jupiter execution failed ({result.get('code')}): "
                f"{result.get('error') or 'transaction did not confirm'}"
            )
        return {"order": order, "result": result}

    async def security(self, chain: str, address: str) -> dict | None:
        if not self.birdeye_api_key or chain not in BIRDEYE_CHAIN:
            return None
        headers = {
            "X-API-KEY": self.birdeye_api_key,
            "x-chain": BIRDEYE_CHAIN[chain],
            "accept": "application/json",
        }
        try:
            r = await self.client.get(
                f"{BIRDEYE}/defi/token_security",
                headers=headers,
                params={"address": address},
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", payload) if isinstance(payload, dict) else None
            return data if isinstance(data, dict) else None
        except Exception:
            return None
