"""
Alert Manager - Gestionnaire d'alertes avec persistance SQLite
Survit aux redémarrages du bot
"""

import sqlite3
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'data', 'alerts.db')


@dataclass
class Alert:
    """Représente une alerte de prix."""
    alert_id: int
    user_id: int
    username: str
    symbol: str
    target_price: float
    current_price_at_creation: float
    alert_type: str          # 'above' ou 'below'
    created_at: str
    triggered: bool = False
    triggered_at: Optional[str] = None
    channel_id: Optional[int] = None   # Canal Discord (optionnel)


class AlertManager:
    """Gestion des alertes avec SQLite."""

    MAX_ALERTS_FREE = 5
    MAX_ALERTS_PREMIUM = 20  # Pour usage futur

    def __init__(self):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        self._init_db()
        logger.info(f"AlertManager initialized. DB: {DB_PATH}")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        """Créer les tables si elles n'existent pas."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    current_price_at_creation REAL NOT NULL,
                    alert_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    triggered INTEGER NOT NULL DEFAULT 0,
                    triggered_at TEXT,
                    channel_id INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON alerts(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_triggered ON alerts(triggered)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_symbol ON alerts(symbol)")

            # Table historique des alertes déclenchées
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    triggered_price REAL NOT NULL,
                    alert_type TEXT NOT NULL,
                    triggered_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def _row_to_alert(self, row: sqlite3.Row) -> Alert:
        return Alert(
            alert_id=row['id'],
            user_id=row['user_id'],
            username=row['username'],
            symbol=row['symbol'],
            target_price=row['target_price'],
            current_price_at_creation=row['current_price_at_creation'],
            alert_type=row['alert_type'],
            created_at=row['created_at'],
            triggered=bool(row['triggered']),
            triggered_at=row['triggered_at'],
            channel_id=row['channel_id'],
        )

    # ==================== CRUD ====================

    def add_alert(
        self,
        user_id: int,
        username: str,
        symbol: str,
        target_price: float,
        current_price: float,
        channel_id: Optional[int] = None,
    ) -> Optional[Alert]:
        """Créer une nouvelle alerte."""
        alert_type = 'above' if target_price > current_price else 'below'
        created_at = datetime.now().isoformat()

        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    """INSERT INTO alerts
                       (user_id, username, symbol, target_price, current_price_at_creation,
                        alert_type, created_at, triggered, channel_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
                    (user_id, username, symbol.upper(), target_price, current_price,
                     alert_type, created_at, channel_id)
                )
                conn.commit()
                alert_id = cursor.lastrowid

            logger.info(f"Alert created: ID={alert_id} {username} → {symbol} {alert_type} ${target_price}")
            return Alert(
                alert_id=alert_id,
                user_id=user_id,
                username=username,
                symbol=symbol.upper(),
                target_price=target_price,
                current_price_at_creation=current_price,
                alert_type=alert_type,
                created_at=created_at,
                channel_id=channel_id,
            )
        except Exception as e:
            logger.error(f"Error adding alert: {e}")
            return None

    def remove_alert(self, user_id: int, alert_id: int) -> bool:
        """Supprimer une alerte (seulement si elle appartient à l'utilisateur)."""
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM alerts WHERE id = ? AND user_id = ?",
                    (alert_id, user_id)
                )
                conn.commit()
                return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"Error removing alert: {e}")
            return False

    def get_user_alerts(self, user_id: int, include_triggered: bool = False) -> List[Alert]:
        """Récupérer les alertes actives d'un utilisateur."""
        try:
            with self._get_conn() as conn:
                if include_triggered:
                    rows = conn.execute(
                        "SELECT * FROM alerts WHERE user_id = ? ORDER BY created_at DESC",
                        (user_id,)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM alerts WHERE user_id = ? AND triggered = 0 ORDER BY created_at DESC",
                        (user_id,)
                    ).fetchall()
                return [self._row_to_alert(r) for r in rows]
        except Exception as e:
            logger.error(f"Error getting user alerts: {e}")
            return []

    def get_all_active_alerts(self) -> List[Alert]:
        """Récupérer toutes les alertes non déclenchées."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM alerts WHERE triggered = 0 ORDER BY symbol"
                ).fetchall()
                return [self._row_to_alert(r) for r in rows]
        except Exception as e:
            logger.error(f"Error getting all alerts: {e}")
            return []

    def count_user_alerts(self, user_id: int) -> int:
        """Compter les alertes actives d'un utilisateur."""
        try:
            with self._get_conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) as cnt FROM alerts WHERE user_id = ? AND triggered = 0",
                    (user_id,)
                ).fetchone()
                return row['cnt'] if row else 0
        except Exception as e:
            logger.error(f"Error counting alerts: {e}")
            return 0

    def mark_triggered(self, alert: Alert, triggered_price: float):
        """Marquer une alerte comme déclenchée et l'archiver."""
        triggered_at = datetime.now().isoformat()
        try:
            with self._get_conn() as conn:
                conn.execute(
                    "UPDATE alerts SET triggered = 1, triggered_at = ? WHERE id = ?",
                    (triggered_at, alert.alert_id)
                )
                conn.execute(
                    """INSERT INTO alert_history
                       (alert_id, user_id, username, symbol, target_price, triggered_price, alert_type, triggered_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (alert.alert_id, alert.user_id, alert.username, alert.symbol,
                     alert.target_price, triggered_price, alert.alert_type, triggered_at)
                )
                conn.commit()
            logger.info(f"Alert {alert.alert_id} triggered at ${triggered_price}")
        except Exception as e:
            logger.error(f"Error marking alert triggered: {e}")

    def get_user_history(self, user_id: int, limit: int = 10) -> List[dict]:
        """Historique des alertes déclenchées d'un utilisateur."""
        try:
            with self._get_conn() as conn:
                rows = conn.execute(
                    """SELECT * FROM alert_history WHERE user_id = ?
                       ORDER BY triggered_at DESC LIMIT ?""",
                    (user_id, limit)
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"Error getting history: {e}")
            return []

    # ==================== VÉRIFICATION ====================

    def check_alert(self, alert: Alert, current_price: float) -> bool:
        """
        Vérifier si une alerte doit être déclenchée.
        Retourne True si le seuil est atteint.
        """
        if alert.alert_type == 'above':
            return current_price >= alert.target_price
        else:
            return current_price <= alert.target_price

    # ==================== MAINTENANCE ====================

    def clean_old_alerts(self, days: int = 30):
        """Supprimer les alertes déclenchées de plus de X jours."""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        try:
            with self._get_conn() as conn:
                cursor = conn.execute(
                    "DELETE FROM alerts WHERE triggered = 1 AND triggered_at < ?",
                    (cutoff,)
                )
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Cleaned {cursor.rowcount} old triggered alerts")
        except Exception as e:
            logger.error(f"Error cleaning old alerts: {e}")

    def get_stats(self) -> dict:
        """Statistiques générales des alertes."""
        try:
            with self._get_conn() as conn:
                active = conn.execute("SELECT COUNT(*) as cnt FROM alerts WHERE triggered = 0").fetchone()['cnt']
                total = conn.execute("SELECT COUNT(*) as cnt FROM alerts").fetchone()['cnt']
                triggered = conn.execute("SELECT COUNT(*) as cnt FROM alert_history").fetchone()['cnt']
                users = conn.execute("SELECT COUNT(DISTINCT user_id) as cnt FROM alerts WHERE triggered = 0").fetchone()['cnt']
                return {
                    'active_alerts': active,
                    'total_created': total,
                    'total_triggered': triggered,
                    'active_users': users
                }
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return {}

    # Propriété pour compatibilité
    @property
    def max_alerts_free(self) -> int:
        return self.MAX_ALERTS_FREE


# Instance globale
alert_manager = AlertManager()
