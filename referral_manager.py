"""
Referral Manager — Système de parrainage
Chaque utilisateur peut partager son code unique.
Chaque nouveau payant parrainé = 1 mois gratuit pour le parrain.
"""

import hashlib
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'subscriptions.db')

REFERRAL_REWARD_MONTHS = int(os.getenv('REFERRAL_REWARD_MONTHS', 1))
REFERRAL_DISCOUNT_PCT  = int(os.getenv('REFERRAL_DISCOUNT_PCT', 20))   # % de remise pour le filleul


class ReferralManager:

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ── Génération du code ───────────────────────────────────────────

    def generate_code(self, user_id: int) -> str:
        """Génère un code referral court et unique basé sur le user_id."""
        raw = f"cryptobot-{user_id}-referral"
        return hashlib.sha256(raw.encode()).hexdigest()[:8].upper()

    def get_or_create_code(self, user_id: int, username: str) -> str:
        """Retourne le code existant ou en crée un nouveau."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT referral_code FROM subscriptions WHERE user_id = ?", (user_id,)
                ).fetchone()

                if row and row['referral_code']:
                    return row['referral_code']

                # Créer le code
                code = self.generate_code(user_id)
                now  = datetime.now().isoformat()
                conn.execute("""
                    INSERT INTO subscriptions (user_id, username, tier, referral_code, created_at, updated_at)
                    VALUES (?, ?, 'free', ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET referral_code = excluded.referral_code
                """, (user_id, username, code, now, now))
                conn.commit()
                return code
        except Exception as e:
            logger.error(f"Error getting referral code: {e}")
            return self.generate_code(user_id)

    # ── Lookup ───────────────────────────────────────────────────────

    def get_referrer_by_code(self, code: str) -> Optional[dict]:
        """Trouve le parrain à partir de son code."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT user_id, username, tier FROM subscriptions WHERE referral_code = ?",
                    (code.upper(),)
                ).fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.error(f"Error looking up referral code: {e}")
            return None

    def get_referral_stats(self, user_id: int) -> dict:
        """Statistiques de parrainage pour un utilisateur."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT referral_months_earned FROM subscriptions WHERE user_id = ?",
                    (user_id,)
                ).fetchone()
                count = conn.execute(
                    "SELECT COUNT(*) as cnt FROM subscriptions WHERE referred_by = ?",
                    (user_id,)
                ).fetchone()

            return {
                'months_earned': row['referral_months_earned'] if row else 0,
                'total_referrals': count['cnt'] if count else 0,
            }
        except Exception as e:
            logger.error(f"Error getting referral stats: {e}")
            return {'months_earned': 0, 'total_referrals': 0}

    # ── Récompense ───────────────────────────────────────────────────

    def apply_referral(self, new_user_id: int, new_username: str, referral_code: str) -> Optional[int]:
        """
        Associe un filleul à son parrain lors d\'un premier paiement.
        Retourne le user_id du parrain si succès, None sinon.
        """
        referrer = self.get_referrer_by_code(referral_code)
        if not referrer:
            logger.warning(f"Invalid referral code: {referral_code}")
            return None

        referrer_id = referrer['user_id']
        if referrer_id == new_user_id:
            logger.warning(f"User {new_user_id} tried to use their own referral code")
            return None

        try:
            now = datetime.now().isoformat()
            with self._get_conn() as conn:
                # Vérifier que ce filleul n'a pas déjà un parrain
                existing = conn.execute(
                    "SELECT referred_by FROM subscriptions WHERE user_id = ?", (new_user_id,)
                ).fetchone()

                if existing and existing['referred_by']:
                    logger.info(f"User {new_user_id} already has a referrer")
                    return None

                # Associer le filleul
                conn.execute("""
                    INSERT INTO subscriptions (user_id, username, tier, referred_by, created_at, updated_at)
                    VALUES (?, ?, 'free', ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET referred_by = excluded.referred_by
                """, (new_user_id, new_username, referrer_id, now, now))

                # Créditer le parrain
                conn.execute("""
                    UPDATE subscriptions
                    SET referral_months_earned = referral_months_earned + ?,
                        updated_at = ?
                    WHERE user_id = ?
                """, (REFERRAL_REWARD_MONTHS, now, referrer_id))

                conn.commit()

            logger.info(f"Referral applied: {new_user_id} referred by {referrer_id} (+{REFERRAL_REWARD_MONTHS} month)")
            return referrer_id

        except Exception as e:
            logger.error(f"Error applying referral: {e}")
            return None

    def build_referral_link(self, user_id: int, username: str, tier: str = 'basic') -> str:
        """Construit le lien de parrainage complet avec le code pré-rempli."""
        import urllib.parse
        code       = self.get_or_create_code(user_id, username)
        tier_links = {
            'basic':   os.getenv('STRIPE_LINK_BASIC', ''),
            'pro':     os.getenv('STRIPE_LINK_PRO', ''),
            'premium': os.getenv('STRIPE_LINK_PREMIUM', ''),
        }
        base = tier_links.get(tier, tier_links.get('basic', ''))
        if not base:
            return f"https://votre-store.lemonsqueezy.com?checkout[custom][referral_code]={code}"

        params = urllib.parse.urlencode({
            'passthrough': f'{user_id}:{code}:{tier}'
        })
        return f"{base}?{params}"


referral_manager = ReferralManager()
