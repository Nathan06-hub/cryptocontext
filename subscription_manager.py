"""
Subscription Manager - Gestion des abonnements et tiers
Tiers : Free / Basic / Pro / Premium
Intégration Stripe pour les paiements
"""

import sqlite3
import logging
import os
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from enum import Enum

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'subscriptions.db')

# ==================== TIERS ====================

class Tier(Enum):
    FREE = "free"
    BASIC = "basic"
    PRO = "pro"
    PREMIUM = "premium"


@dataclass
class TierConfig:
    name: str
    price_monthly: float       # EUR
    alerts_limit: int
    watchlist_limit: int
    currencies: List[str]      # Devises supportées
    chart_access: bool
    portfolio_access: bool
    advanced_analysis: bool
    daily_digest: bool         # Résumé quotidien automatique
    priority_cache: bool       # Cache plus court = données plus fraîches
    color: int                 # Couleur Discord embed (hex)


TIER_CONFIGS: Dict[str, TierConfig] = {
    Tier.FREE.value: TierConfig(
        name="🆓 Free",
        price_monthly=0.0,
        alerts_limit=3,
        watchlist_limit=5,
        currencies=["usd"],
        chart_access=False,
        portfolio_access=False,
        advanced_analysis=False,
        daily_digest=False,
        priority_cache=False,
        color=0x95a5a6,
    ),
    Tier.BASIC.value: TierConfig(
        name="⭐ Basic",
        price_monthly=4.99,
        alerts_limit=10,
        watchlist_limit=20,
        currencies=["usd", "eur", "gbp"],
        chart_access=True,
        portfolio_access=True,
        advanced_analysis=False,
        daily_digest=False,
        priority_cache=False,
        color=0x3498db,
    ),
    Tier.PRO.value: TierConfig(
        name="💎 Pro",
        price_monthly=14.99,
        alerts_limit=50,
        watchlist_limit=100,
        currencies=["usd", "eur", "gbp", "jpy", "chf", "cad", "aud"],
        chart_access=True,
        portfolio_access=True,
        advanced_analysis=True,
        daily_digest=True,
        priority_cache=True,
        color=0x9b59b6,
    ),
    Tier.PREMIUM.value: TierConfig(
        name="👑 Premium",
        price_monthly=29.99,
        alerts_limit=999,
        watchlist_limit=999,
        currencies=["usd", "eur", "gbp", "jpy", "chf", "cad", "aud", "btc", "eth"],
        chart_access=True,
        portfolio_access=True,
        advanced_analysis=True,
        daily_digest=True,
        priority_cache=True,
        color=0xf39c12,
    ),
}

CURRENCY_SYMBOLS = {
    "usd": "$", "eur": "€", "gbp": "£", "jpy": "¥",
    "chf": "Fr", "cad": "C$", "aud": "A$", "btc": "₿", "eth": "Ξ"
}

CURRENCY_NAMES = {
    "usd": "US Dollar", "eur": "Euro", "gbp": "British Pound",
    "jpy": "Japanese Yen", "chf": "Swiss Franc", "cad": "Canadian Dollar",
    "aud": "Australian Dollar", "btc": "Bitcoin", "eth": "Ethereum"
}


# ==================== SUBSCRIPTION MANAGER ====================

class SubscriptionManager:

    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self.stripe_webhook_secret = os.getenv('STRIPE_WEBHOOK_SECRET', '')
        self._init_db()
        logger.info("SubscriptionManager initialized")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'free',
                    stripe_customer_id TEXT,
                    stripe_subscription_id TEXT,
                    expires_at TEXT,
                    preferred_currency TEXT DEFAULT 'usd',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS payment_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    stripe_payment_id TEXT,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS guild_subscriptions (
                    guild_id INTEGER PRIMARY KEY,
                    guild_name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'free',
                    owner_user_id INTEGER,
                    stripe_subscription_id TEXT,
                    expires_at TEXT,
                    digest_channel_id INTEGER,
                    digest_hour INTEGER DEFAULT 8,
                    digest_timezone TEXT DEFAULT 'UTC',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_tier ON subscriptions(tier)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sub_stripe ON subscriptions(stripe_customer_id)")

            # Table d'idempotence : évite de traiter deux fois le même event Stripe
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_stripe_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    processed_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                DELETE FROM processed_stripe_events
                WHERE processed_at < datetime('now', '-90 days')
            """)
            conn.commit()

    # ==================== GETTERS ====================

    def get_user_sub(self, user_id: int) -> dict:
        """Récupère l'abonnement d'un user (Free par défaut)."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)
                ).fetchone()

            if not row:
                return self._default_sub(user_id)

            sub = dict(row)

            # Vérifier expiration
            if sub.get('expires_at'):
                expires = datetime.fromisoformat(sub['expires_at'])
                if datetime.now() > expires:
                    self._downgrade_to_free(user_id)
                    sub['tier'] = 'free'

            return sub
        except Exception as e:
            logger.error(f"Error getting user sub: {e}")
            return self._default_sub(user_id)

    def get_tier_config(self, user_id: int) -> TierConfig:
        """Récupère la config du tier d'un utilisateur."""
        sub = self.get_user_sub(user_id)
        tier = sub.get('tier', 'free')
        return TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])

    def get_tier(self, user_id: int) -> str:
        """Récupère le tier d'un utilisateur."""
        return self.get_user_sub(user_id).get('tier', 'free')

    def get_preferred_currency(self, user_id: int) -> str:
        """Récupère la devise préférée d'un utilisateur."""
        sub = self.get_user_sub(user_id)
        return sub.get('preferred_currency', 'usd')

    def can_use_currency(self, user_id: int, currency: str) -> bool:
        """Vérifie si l'utilisateur peut utiliser une devise."""
        config = self.get_tier_config(user_id)
        return currency.lower() in config.currencies

    def check_feature(self, user_id: int, feature: str) -> bool:
        """Vérifie si l'utilisateur a accès à une feature."""
        config = self.get_tier_config(user_id)
        return getattr(config, feature, False)

    def get_guild_sub(self, guild_id: int) -> Optional[dict]:
        """Récupère l'abonnement d'un serveur."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM guild_subscriptions WHERE guild_id = ?", (guild_id,)
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting guild sub: {e}")
            return None

    def get_sub_by_stripe_customer(self, customer_id: str) -> Optional[dict]:
        """Récupère un abonnement via le Stripe customer_id."""
        if not customer_id:
            return None
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE stripe_customer_id = ?", (customer_id,)
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error getting sub by stripe customer: {e}")
            return None

    # ==================== IDEMPOTENCE STRIPE ====================

    def is_event_processed(self, event_id: str) -> bool:
        """Vérifie si un event Stripe a déjà été traité (évite les doublons)."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT 1 FROM processed_stripe_events WHERE event_id = ?", (event_id,)
                ).fetchone()
            return row is not None
        except Exception as e:
            logger.error(f"Error checking processed event: {e}")
            return False

    def mark_event_processed(self, event_id: str, event_type: str) -> bool:
        """Marque un event Stripe comme traité."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    """INSERT OR IGNORE INTO processed_stripe_events
                       (event_id, event_type, processed_at)
                       VALUES (?, ?, datetime('now'))""",
                    (event_id, event_type)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error marking event processed: {e}")
            return False

    def get_digest_guilds(self) -> List[dict]:
        """Récupère tous les serveurs avec digest activé."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT * FROM guild_subscriptions
                       WHERE digest_channel_id IS NOT NULL
                       AND (tier = 'pro' OR tier = 'premium')"""
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error getting digest guilds: {e}")
            return []

    # ==================== SETTERS ====================

    def ensure_user(self, user_id: int, username: str):
        """Crée un enregistrement Free si l'utilisateur n'existe pas encore."""
        try:
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO subscriptions
                    (user_id, username, tier, created_at, updated_at)
                    VALUES (?, ?, 'free', ?, ?)
                """, (user_id, username, now, now))
                conn.commit()
        except Exception as e:
            logger.error(f"Error ensuring user: {e}")

    def set_preferred_currency(self, user_id: int, username: str, currency: str) -> bool:
        """Définit la devise préférée d'un utilisateur."""
        try:
            self.ensure_user(user_id, username)
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE subscriptions SET preferred_currency = ?, updated_at = ? WHERE user_id = ?",
                    (currency.lower(), now, user_id)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error setting currency: {e}")
            return False

    def upgrade_user(self, user_id: int, username: str, tier: str,
                     stripe_customer_id: str = None,
                     stripe_subscription_id: str = None,
                     months: int = 1) -> bool:
        """Upgrade un utilisateur vers un tier supérieur."""
        try:
            self.ensure_user(user_id, username)
            now = datetime.now()
            expires_at = (now + timedelta(days=30 * months)).isoformat()

            with self._get_conn() as conn:
                conn.execute("""
                    UPDATE subscriptions SET
                        tier = ?, stripe_customer_id = ?, stripe_subscription_id = ?,
                        expires_at = ?, updated_at = ?
                    WHERE user_id = ?
                """, (tier, stripe_customer_id, stripe_subscription_id,
                      expires_at, now.isoformat(), user_id))
                conn.commit()

            logger.info(f"User {user_id} ({username}) upgraded to {tier} until {expires_at}")
            return True
        except Exception as e:
            logger.error(f"Error upgrading user: {e}")
            return False

    def set_guild_digest(self, guild_id: int, guild_name: str, channel_id: int,
                         hour: int = 8, timezone: str = "UTC", tier: str = "free") -> bool:
        """Configure le digest quotidien pour un serveur."""
        try:
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO guild_subscriptions
                        (guild_id, guild_name, tier, digest_channel_id, digest_hour,
                         digest_timezone, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        digest_channel_id = excluded.digest_channel_id,
                        digest_hour = excluded.digest_hour,
                        digest_timezone = excluded.digest_timezone,
                        updated_at = excluded.updated_at
                """, (guild_id, guild_name, tier, channel_id, hour, timezone, now, now))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error setting guild digest: {e}")
            return False

    def remove_guild_digest(self, guild_id: int) -> bool:
        """Désactive le digest pour un serveur."""
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE guild_subscriptions SET digest_channel_id = NULL WHERE guild_id = ?",
                    (guild_id,)
                )
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error removing guild digest: {e}")
            return False

    def _downgrade_to_free(self, user_id: int):
        """Downgrade automatique à Free à l'expiration."""
        try:
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE subscriptions SET tier = 'free', expires_at = NULL, updated_at = ? WHERE user_id = ?",
                    (now, user_id)
                )
                conn.commit()
            logger.info(f"User {user_id} downgraded to free (expired)")
        except Exception as e:
            logger.error(f"Error downgrading user: {e}")

    def _default_sub(self, user_id: int) -> dict:
        return {
            'user_id': user_id, 'username': 'unknown', 'tier': 'free',
            'preferred_currency': 'usd', 'expires_at': None,
            'stripe_customer_id': None, 'stripe_subscription_id': None,
        }

    # ==================== STRIPE ====================

    def verify_stripe_signature(self, payload: bytes, sig_header: str) -> bool:
        """Vérifie la signature d'un webhook Stripe."""
        if not self.stripe_webhook_secret:
            logger.warning("No Stripe webhook secret configured")
            return False
        try:
            parts = {k: v for k, v in (p.split('=', 1) for p in sig_header.split(','))}
            timestamp = parts.get('t', '')
            signature = parts.get('v1', '')
            signed_payload = f"{timestamp}.{payload.decode('utf-8')}"
            expected = hmac.new(
                self.stripe_webhook_secret.encode(),
                signed_payload.encode(),
                hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(expected, signature)
        except Exception as e:
            logger.error(f"Stripe signature verification error: {e}")
            return False

    def handle_stripe_event(self, event_type: str, event_data: dict) -> Optional[dict]:
        """
        Traite un événement Stripe et retourne les infos pour notifier l'utilisateur.
        À appeler depuis le webhook handler.
        """
        try:
            if event_type == 'checkout.session.completed':
                return self._handle_checkout_completed(event_data)
            elif event_type == 'customer.subscription.deleted':
                return self._handle_subscription_cancelled(event_data)
            elif event_type == 'invoice.payment_failed':
                return self._handle_payment_failed(event_data)
            elif event_type == 'customer.subscription.updated':
                return self._handle_subscription_updated(event_data)
        except Exception as e:
            logger.error(f"Error handling Stripe event {event_type}: {e}")
        return None

    def _handle_checkout_completed(self, data: dict) -> Optional[dict]:
        metadata = data.get('metadata', {})
        user_id = metadata.get('discord_user_id')
        username = metadata.get('discord_username', 'unknown')
        tier = metadata.get('tier', 'basic')

        if not user_id:
            logger.warning("Checkout completed but no discord_user_id in metadata")
            return None

        user_id = int(user_id)
        customer_id = data.get('customer')
        subscription_id = data.get('subscription')
        amount = data.get('amount_total', 0) / 100
        currency = data.get('currency', 'eur')

        self.upgrade_user(user_id, username, tier, customer_id, subscription_id)
        self._log_payment(user_id, data.get('id'), amount, currency, tier, 'completed')

        return {'action': 'upgraded', 'user_id': user_id, 'tier': tier, 'username': username}

    def _handle_subscription_cancelled(self, data: dict) -> Optional[dict]:
        customer_id = data.get('customer')
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE stripe_customer_id = ?", (customer_id,)
                ).fetchone()
            if row:
                self._downgrade_to_free(row['user_id'])
                return {'action': 'cancelled', 'user_id': row['user_id'], 'username': row['username']}
        except Exception as e:
            logger.error(f"Error handling cancellation: {e}")
        return None

    def _handle_payment_failed(self, data: dict) -> Optional[dict]:
        customer_id = data.get('customer')
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE stripe_customer_id = ?", (customer_id,)
                ).fetchone()
            if row:
                return {'action': 'payment_failed', 'user_id': row['user_id'], 'username': row['username']}
        except Exception as e:
            logger.error(f"Error handling payment failed: {e}")
        return None

    def _handle_subscription_updated(self, data: dict) -> Optional[dict]:
        customer_id = data.get('customer')
        new_tier = data.get('metadata', {}).get('tier')
        if not new_tier:
            return None
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM subscriptions WHERE stripe_customer_id = ?", (customer_id,)
                ).fetchone()
            if row:
                self.upgrade_user(row['user_id'], row['username'], new_tier, customer_id)
                return {'action': 'updated', 'user_id': row['user_id'], 'tier': new_tier}
        except Exception as e:
            logger.error(f"Error handling subscription update: {e}")
        return None

    def _log_payment(self, user_id, payment_id, amount, currency, tier, status):
        try:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO payment_history
                    (user_id, stripe_payment_id, amount, currency, tier, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (user_id, payment_id, amount, currency, tier, status, datetime.now().isoformat()))
                conn.commit()
        except Exception as e:
            logger.error(f"Error logging payment: {e}")

    # ==================== ADMIN ====================

    def admin_set_tier(self, user_id: int, username: str, tier: str, months: int = 1) -> bool:
        """Override admin pour définir manuellement un tier."""
        return self.upgrade_user(user_id, username, tier, months=months)

    def get_stats(self) -> dict:
        """Statistiques globales des abonnements."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT tier, COUNT(*) as cnt FROM subscriptions GROUP BY tier"
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) as cnt FROM subscriptions").fetchone()['cnt']
                revenue_row = conn.execute(
                    "SELECT SUM(amount) as total FROM payment_history WHERE status = 'completed'"
                ).fetchone()

            by_tier = {r['tier']: r['cnt'] for r in rows}
            return {
                'total_users': total,
                'by_tier': by_tier,
                'total_revenue': revenue_row['total'] or 0,
                'free': by_tier.get('free', 0),
                'basic': by_tier.get('basic', 0),
                'pro': by_tier.get('pro', 0),
                'premium': by_tier.get('premium', 0),
            }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}

    def delete_user_data(self, user_id: int) -> bool:
        """RGPD : supprime toutes les données d'un utilisateur."""
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
                conn.execute("DELETE FROM payment_history WHERE user_id = ?", (user_id,))
                conn.commit()
            logger.info(f"GDPR: deleted all data for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error deleting user data: {e}")
            return False


# Instance globale
subscription_manager = SubscriptionManager()
