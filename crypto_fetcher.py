"""
Crypto Data Fetcher - Natif async avec aiohttp
Cache intégré | Multi-devises | Pas de run_in_executor nécessaire
"""

import asyncio
import os
import logging
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# aiohttp importé lazily pour éviter crash si absent
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    logger.warning("aiohttp not installed, falling back to requests via executor")


# ==================== CACHE ====================

class APICache:
    """Cache en mémoire avec TTL."""

    def __init__(self):
        self._store: Dict[str, Tuple] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str):
        async with self._lock:
            if key in self._store:
                value, expires_at = self._store[key]
                if time.monotonic() < expires_at:
                    return value
                del self._store[key]
        return None

    async def set(self, key: str, value, ttl: int = 60):
        async with self._lock:
            self._store[key] = (value, time.monotonic() + ttl)

    async def clear_expired(self):
        async with self._lock:
            now = time.monotonic()
            expired = [k for k, (_, exp) in self._store.items() if now >= exp]
            for k in expired:
                del self._store[k]

    def clear_all(self):
        self._store.clear()


# ==================== FETCHER ====================

class CryptoFetcher:
    """Fetcher async natif — aiohttp."""

    BASE_URL = "https://api.coingecko.com/api/v3"
    FNG_URL = "https://api.alternative.me/fng/"

    # TTL en secondes
    TTL_PRICE = 60
    TTL_PRICE_PRIORITY = 30       # Utilisateurs Pro/Premium
    TTL_MARKET = 120
    TTL_TOP = 180
    TTL_HISTORY = 300
    TTL_SYMBOL = 3600
    TTL_FNG = 300

    SUPPORTED_CURRENCIES = ["usd", "eur", "gbp", "jpy", "chf", "cad", "aud", "btc", "eth"]

    def __init__(self):
        self.cache = APICache()
        self._session: Optional[aiohttp.ClientSession] = None
        self._symbol_map = {
            'BTC': 'bitcoin', 'ETH': 'ethereum', 'BNB': 'binancecoin',
            'SOL': 'solana', 'XRP': 'ripple', 'ADA': 'cardano',
            'DOGE': 'dogecoin', 'MATIC': 'matic-network', 'DOT': 'polkadot',
            'AVAX': 'avalanche-2', 'LINK': 'chainlink', 'ATOM': 'cosmos',
            'UNI': 'uniswap', 'LTC': 'litecoin', 'BCH': 'bitcoin-cash',
            'XLM': 'stellar', 'ALGO': 'algorand', 'VET': 'vechain',
            'ICP': 'internet-computer', 'FIL': 'filecoin', 'SHIB': 'shiba-inu',
            'TRX': 'tron', 'ETC': 'ethereum-classic', 'NEAR': 'near',
            'APT': 'aptos', 'ARB': 'arbitrum', 'OP': 'optimism',
            'INJ': 'injective-protocol', 'SUI': 'sui', 'TON': 'the-open-network',
            'PEPE': 'pepe', 'FTM': 'fantom', 'SAND': 'the-sandbox',
            'MANA': 'decentraland', 'AXS': 'axie-infinity',
        }

    async def _get_session(self) -> aiohttp.ClientSession:
        """Retourne la session aiohttp, la crée si nécessaire."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            self._session = aiohttp.ClientSession(
                headers={'User-Agent': 'CryptoContextBot/3.0'},
                timeout=timeout
            )
        return self._session

    async def close(self):
        """Ferme la session aiohttp proprement."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, url: str, params: dict = None, retries: int = 3) -> Optional[dict]:
        """Requête HTTP async avec retry et gestion rate-limit."""
        session = await self._get_session()

        for attempt in range(retries):
            try:
                async with session.get(url, params=params) as response:
                    if response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', 30))
                        wait = min(retry_after, 30)
                        logger.warning(f"Rate limited. Waiting {wait}s...")
                        await asyncio.sleep(wait)
                        continue
                    if response.status == 404:
                        return None
                    response.raise_for_status()
                    return await response.json()

            except asyncio.TimeoutError:
                logger.warning(f"Timeout on attempt {attempt + 1} for {url}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except aiohttp.ClientError as e:
                logger.error(f"Request error: {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                return None

        return None

    async def _symbol_to_id(self, symbol: str) -> Optional[str]:
        """Convertit un symbole en ID CoinGecko."""
        sym = symbol.upper()

        if sym in self._symbol_map:
            return self._symbol_map[sym]

        cached = await self.cache.get(f"sym:{sym}")
        if cached:
            return cached

        data = await self._request(f"{self.BASE_URL}/search", params={'query': symbol})
        if data:
            coins = data.get('coins', [])
            # Priorité aux correspondances exactes de symbole
            for coin in coins:
                if coin.get('symbol', '').upper() == sym:
                    await self.cache.set(f"sym:{sym}", coin['id'], ttl=self.TTL_SYMBOL)
                    return coin['id']
            if coins:
                await self.cache.set(f"sym:{coins[0]['id']}", coins[0]['id'], ttl=self.TTL_SYMBOL)
                return coins[0]['id']

        return None

    # ==================== PRIX ====================

    async def get_price(self, symbol: str, currency: str = "usd", priority: bool = False) -> Optional[Dict]:
        """
        Obtenir le prix d'une crypto dans la devise choisie.
        priority=True = TTL plus court (utilisateurs Pro/Premium)
        """
        currency = currency.lower()
        cache_key = f"price:{symbol.upper()}:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        coin_id = await self._symbol_to_id(symbol)
        if not coin_id:
            return None

        data = await self._request(
            f"{self.BASE_URL}/coins/{coin_id}",
            params={
                'localization': 'false', 'tickers': 'false',
                'community_data': 'false', 'developer_data': 'false'
            }
        )
        if not data:
            return None

        md = data.get('market_data', {})

        def safe(*keys, default=0):
            v = md
            for k in keys:
                if isinstance(v, dict):
                    v = v.get(k)
                else:
                    return default
            return v if v is not None else default

        result = {
            'symbol': symbol.upper(),
            'name': data.get('name', symbol),
            'price': safe('current_price', currency),
            'price_usd': safe('current_price', 'usd'),
            'currency': currency,
            'change_24h': safe('price_change_percentage_24h'),
            'change_7d': safe('price_change_percentage_7d'),
            'change_30d': safe('price_change_percentage_30d'),
            'high_24h': safe('high_24h', currency),
            'low_24h': safe('low_24h', currency),
            'volume_24h': safe('total_volume', currency),
            'market_cap': safe('market_cap', currency),
            'rank': data.get('market_cap_rank', 0),
            'coin_id': coin_id,
            'image': data.get('image', {}).get('small', ''),
            'ath': safe('ath', currency),
            'ath_change_percentage': safe('ath_change_percentage', currency),
            'circulating_supply': safe('circulating_supply'),
        }

        ttl = self.TTL_PRICE_PRIORITY if priority else self.TTL_PRICE
        await self.cache.set(cache_key, result, ttl=ttl)
        return result

    async def get_multiple_prices(self, symbols: List[str], currency: str = "usd") -> Dict[str, Optional[Dict]]:
        """Récupère plusieurs prix en parallèle."""
        tasks = [self.get_price(sym, currency) for sym in symbols]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        results = {}
        for sym, res in zip(symbols, results_list):
            if isinstance(res, Exception):
                logger.error(f"Error fetching {sym}: {res}")
                results[sym.upper()] = None
            else:
                results[sym.upper()] = res

        return results

    async def get_simple_prices(self, symbols: List[str], currency: str = "usd") -> Dict[str, float]:
        """
        Récupère uniquement les prix (sans détails) via l'endpoint simple.
        Plus rapide, moins de données. Idéal pour watchlist et monitoring alertes.
        """
        cache_key = f"simple:{','.join(sorted(s.upper() for s in symbols))}:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        # Résoudre les IDs en parallèle
        id_tasks = [self._symbol_to_id(sym) for sym in symbols]
        ids_list = await asyncio.gather(*id_tasks, return_exceptions=True)

        sym_to_id = {}
        id_to_sym = {}
        valid_ids = []
        for sym, coin_id in zip(symbols, ids_list):
            if coin_id and not isinstance(coin_id, Exception):
                sym_to_id[sym.upper()] = coin_id
                id_to_sym[coin_id] = sym.upper()
                valid_ids.append(coin_id)

        if not valid_ids:
            return {}

        data = await self._request(
            f"{self.BASE_URL}/simple/price",
            params={
                'ids': ','.join(valid_ids),
                'vs_currencies': currency,
                'include_24hr_change': 'true',
            }
        )
        if not data:
            return {}

        result = {}
        for coin_id, price_data in data.items():
            sym = id_to_sym.get(coin_id)
            if sym:
                result[sym] = {
                    'price': price_data.get(currency, 0) or 0,
                    'change_24h': price_data.get(f'{currency}_24h_change', 0) or 0,
                    'symbol': sym,
                    'currency': currency,
                }

        await self.cache.set(cache_key, result, ttl=self.TTL_PRICE)
        return result

    # ==================== MARCHÉ ====================

    async def get_market_overview(self, currency: str = "usd") -> Optional[Dict]:
        """Vue d'ensemble du marché."""
        cache_key = f"market:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        data = await self._request(f"{self.BASE_URL}/global")
        if not data:
            return None

        gd = data.get('data', {})
        result = {
            'total_market_cap': (gd.get('total_market_cap') or {}).get(currency, 0) or 0,
            'total_volume_24h': (gd.get('total_volume') or {}).get(currency, 0) or 0,
            'btc_dominance': (gd.get('market_cap_percentage') or {}).get('btc', 0) or 0,
            'eth_dominance': (gd.get('market_cap_percentage') or {}).get('eth', 0) or 0,
            'active_cryptos': gd.get('active_cryptocurrencies', 0),
            'markets': gd.get('markets', 0),
            'market_cap_change_24h': gd.get('market_cap_change_percentage_24h_usd', 0) or 0,
            'currency': currency,
        }

        await self.cache.set(cache_key, result, ttl=self.TTL_MARKET)
        return result

    async def get_top_gainers_losers(self, limit: int = 5, currency: str = "usd") -> Tuple[List, List]:
        """Top gainers et losers."""
        cache_key = f"top:{limit}:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        data = await self._request(
            f"{self.BASE_URL}/coins/markets",
            params={
                'vs_currency': currency,
                'order': 'market_cap_desc',
                'per_page': 100, 'page': 1,
                'sparkline': 'false',
                'price_change_percentage': '24h'
            }
        )
        if not data:
            return [], []

        coins = [c for c in data if c.get('price_change_percentage_24h') is not None]

        def fmt(c):
            return {
                'symbol': c['symbol'].upper(),
                'name': c['name'],
                'price': c.get('current_price', 0) or 0,
                'change_24h': c.get('price_change_percentage_24h', 0) or 0,
                'market_cap': c.get('market_cap', 0) or 0,
                'image': c.get('image', ''),
                'currency': currency,
            }

        gainers = [fmt(c) for c in sorted(coins, key=lambda x: x.get('price_change_percentage_24h', 0), reverse=True)[:limit]]
        losers = [fmt(c) for c in sorted(coins, key=lambda x: x.get('price_change_percentage_24h', 0))[:limit]]

        result = (gainers, losers)
        await self.cache.set(cache_key, result, ttl=self.TTL_TOP)
        return result

    # ==================== HISTORIQUE ====================

    async def get_historical_data(self, symbol: str, days: int = 30, currency: str = "usd") -> Optional[List[float]]:
        """Données historiques pour indicateurs techniques."""
        cache_key = f"history:{symbol.upper()}:{days}:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        coin_id = await self._symbol_to_id(symbol)
        if not coin_id:
            return None

        data = await self._request(
            f"{self.BASE_URL}/coins/{coin_id}/market_chart",
            params={'vs_currency': currency, 'days': days, 'interval': 'daily'}
        )
        if not data:
            return None

        prices = [p[1] for p in data.get('prices', []) if p[1] is not None]
        if not prices:
            return None

        await self.cache.set(cache_key, prices, ttl=self.TTL_HISTORY)
        return prices

    async def get_ohlc_data(self, symbol: str, days: int = 14, currency: str = "usd") -> Optional[List[Dict]]:
        """Données OHLC."""
        cache_key = f"ohlc:{symbol.upper()}:{days}:{currency}"
        cached = await self.cache.get(cache_key)
        if cached:
            return cached

        coin_id = await self._symbol_to_id(symbol)
        if not coin_id:
            return None

        data = await self._request(
            f"{self.BASE_URL}/coins/{coin_id}/ohlc",
            params={'vs_currency': currency, 'days': days}
        )
        if not data:
            return None

        ohlc = [{'timestamp': d[0], 'open': d[1], 'high': d[2], 'low': d[3], 'close': d[4]}
                for d in data if len(d) == 5]

        await self.cache.set(cache_key, ohlc, ttl=self.TTL_HISTORY)
        return ohlc

    # ==================== FEAR & GREED ====================

    async def get_fear_greed_index(self) -> Optional[Dict]:
        """Indice Fear & Greed."""
        cached = await self.cache.get("fng")
        if cached:
            return cached

        try:
            session = await self._get_session()
            async with session.get(self.FNG_URL, timeout=aiohttp.ClientTimeout(total=5)) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
                fg = data.get('data', [{}])[0]
                result = {
                    'value': int(fg.get('value', 50)),
                    'classification': fg.get('value_classification', 'Neutral'),
                }
                await self.cache.set("fng", result, ttl=self.TTL_FNG)
                return result
        except Exception as e:
            logger.error(f"Fear & Greed error: {e}")
            return None

    # ==================== UTILITAIRES ====================

    async def validate_symbol(self, symbol: str) -> bool:
        """Vérifie qu'un symbole existe sur CoinGecko."""
        coin_id = await self._symbol_to_id(symbol)
        return coin_id is not None

    async def clear_cache(self):
        """Vide le cache."""
        self.cache.clear_all()
        logger.info("Cache cleared")


# Instance globale
fetcher = CryptoFetcher()
