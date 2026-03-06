"""
Watchlist Manager - Gestion des watchlists crypto par utilisateur
Persistance SQLite
"""

import sqlite3
import logging
import os
from typing import List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'watchlists.db')


class WatchlistManager:

    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()
        logger.info("WatchlistManager initialized")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS watchlists (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    note TEXT,
                    UNIQUE(user_id, symbol)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wl_user ON watchlists(user_id)")
            conn.commit()

    def get_watchlist(self, user_id: int) -> List[str]:
        """Récupère la watchlist d'un utilisateur (liste de symboles)."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT symbol FROM watchlists WHERE user_id = ? ORDER BY added_at ASC",
                    (user_id,)
                ).fetchall()
            return [r['symbol'] for r in rows]
        except Exception as e:
            logger.error(f"Error getting watchlist: {e}")
            return []

    def add_symbol(self, user_id: int, symbol: str, limit: int = 5, note: str = None) -> dict:
        """
        Ajoute un symbole à la watchlist.
        Retourne {'success': bool, 'reason': str}
        """
        symbol = symbol.upper()
        try:
            current = self.get_watchlist(user_id)

            if symbol in current:
                return {'success': False, 'reason': f'**{symbol}** est déjà dans votre watchlist'}

            if len(current) >= limit:
                return {'success': False, 'reason': f'Limite atteinte ({limit} symboles). Upgradez votre plan !'}

            with self._get_conn() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO watchlists (user_id, symbol, added_at, note) VALUES (?, ?, ?, ?)",
                    (user_id, symbol, datetime.now().isoformat(), note)
                )
                conn.commit()
            return {'success': True, 'reason': f'**{symbol}** ajouté à votre watchlist'}
        except Exception as e:
            logger.error(f"Error adding symbol: {e}")
            return {'success': False, 'reason': 'Erreur interne'}

    def remove_symbol(self, user_id: int, symbol: str) -> bool:
        """Supprime un symbole de la watchlist."""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM watchlists WHERE user_id = ? AND symbol = ?",
                    (user_id, symbol.upper())
                )
                conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing symbol: {e}")
            return False

    def clear_watchlist(self, user_id: int) -> int:
        """Vide complètement la watchlist. Retourne le nb supprimé."""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM watchlists WHERE user_id = ?", (user_id,)
                )
                conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.error(f"Error clearing watchlist: {e}")
            return 0

    def count(self, user_id: int) -> int:
        """Nombre de symboles dans la watchlist."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM watchlists WHERE user_id = ?", (user_id,)
                ).fetchone()
            return row['cnt'] if row else 0
        except Exception as e:
            logger.error(f"Error counting watchlist: {e}")
            return 0

    def delete_user_data(self, user_id: int) -> bool:
        """RGPD : supprime toutes les données d'un utilisateur."""
        try:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM watchlists WHERE user_id = ?", (user_id,))
                conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error deleting user watchlist data: {e}")
            return False


# Instance globale
watchlist_manager = WatchlistManager()
