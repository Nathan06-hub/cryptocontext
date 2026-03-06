"""
Migrations SQLite — Crypto Context Bot
Système de migrations versionnées pour faire évoluer le schéma sans perdre les données.

Usage :
    from db_migrations import run_migrations
    run_migrations()   # À appeler au démarrage, avant tout accès DB
"""

import sqlite3
import logging
import os
from typing import List, Tuple, Callable

logger = logging.getLogger(__name__)

# Chemins des bases
DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
DB_PATHS = {
    'subscriptions': os.path.join(DATA_DIR, 'subscriptions.db'),
    'alerts':        os.path.join(DATA_DIR, 'alerts.db'),
    'watchlists':    os.path.join(DATA_DIR, 'watchlists.db'),
}


def _get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection):
    """Crée la table de versioning si elle n\'existe pas."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS _schema_migrations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            version     INTEGER NOT NULL UNIQUE,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL DEFAULT (datetime('now')),
            checksum    TEXT
        )
    """)
    conn.commit()


def _get_applied_versions(conn: sqlite3.Connection) -> set:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM _schema_migrations").fetchall()
    return {r['version'] for r in rows}


def _apply_migration(conn: sqlite3.Connection, version: int, name: str, sql_or_fn):
    """Applique une migration dans une transaction."""
    try:
        conn.execute("BEGIN")
        if callable(sql_or_fn):
            sql_or_fn(conn)
        else:
            conn.executescript(sql_or_fn)
        conn.execute(
            "INSERT INTO _schema_migrations (version, name) VALUES (?, ?)",
            (version, name)
        )
        conn.execute("COMMIT")
        logger.info(f"Migration v{version} '{name}' applied")
    except Exception as e:
        conn.execute("ROLLBACK")
        logger.error(f"Migration v{version} '{name}' FAILED: {e}")
        raise


# ==================== MIGRATIONS PAR BASE ====================

# Format : (version, nom, sql_ou_callable)
# Ne jamais modifier une migration existante — ajouter une nouvelle à la suite

SUBSCRIPTIONS_MIGRATIONS: List[Tuple] = [
    (1, "initial_schema", """
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
        );
        CREATE TABLE IF NOT EXISTS payment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            stripe_payment_id TEXT,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            tier TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
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
        );
        CREATE INDEX IF NOT EXISTS idx_sub_tier ON subscriptions(tier);
        CREATE INDEX IF NOT EXISTS idx_sub_stripe ON subscriptions(stripe_customer_id);
    """),
    (2, "add_processed_stripe_events", """
        CREATE TABLE IF NOT EXISTS processed_stripe_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            processed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """),
    (3, "add_referral_columns", """
        ALTER TABLE subscriptions ADD COLUMN referral_code TEXT;
        ALTER TABLE subscriptions ADD COLUMN referred_by INTEGER;
        ALTER TABLE subscriptions ADD COLUMN referral_months_earned INTEGER DEFAULT 0;
        CREATE INDEX IF NOT EXISTS idx_sub_referral ON subscriptions(referral_code);
    """),
    (4, "add_notification_preferences", """
        ALTER TABLE subscriptions ADD COLUMN notify_expiry INTEGER DEFAULT 1;
        ALTER TABLE subscriptions ADD COLUMN notify_renewals INTEGER DEFAULT 1;
        ALTER TABLE subscriptions ADD COLUMN preferred_language TEXT DEFAULT 'fr';
    """),
]

ALERTS_MIGRATIONS: List[Tuple] = [
    (1, "initial_schema", """
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            symbol TEXT NOT NULL,
            target_price REAL NOT NULL,
            current_price_at_creation REAL,
            alert_type TEXT NOT NULL DEFAULT 'above',
            created_at TEXT NOT NULL,
            triggered INTEGER DEFAULT 0,
            triggered_at TEXT,
            channel_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS alert_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            symbol TEXT NOT NULL,
            target_price REAL NOT NULL,
            triggered_price REAL,
            alert_type TEXT NOT NULL,
            triggered_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id);
        CREATE INDEX IF NOT EXISTS idx_alerts_triggered ON alerts(triggered);
    """),
    (2, "add_alert_notes", """
        ALTER TABLE alerts ADD COLUMN note TEXT;
        ALTER TABLE alerts ADD COLUMN repeat INTEGER DEFAULT 0;
    """),
]

WATCHLISTS_MIGRATIONS: List[Tuple] = [
    (1, "initial_schema", """
        CREATE TABLE IF NOT EXISTS watchlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            added_at TEXT NOT NULL,
            note TEXT,
            UNIQUE(user_id, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_wl_user ON watchlists(user_id);
    """),
]


# ==================== RUNNER ====================

def run_migrations(db_key: str = None):
    """
    Applique toutes les migrations en attente pour une ou toutes les bases.
    db_key : 'subscriptions', 'alerts', 'watchlists' ou None (toutes)
    """
    os.makedirs(DATA_DIR, exist_ok=True)

    migration_sets = {
        'subscriptions': (DB_PATHS['subscriptions'], SUBSCRIPTIONS_MIGRATIONS),
        'alerts':        (DB_PATHS['alerts'],        ALERTS_MIGRATIONS),
        'watchlists':    (DB_PATHS['watchlists'],    WATCHLISTS_MIGRATIONS),
    }

    targets = {db_key: migration_sets[db_key]} if db_key else migration_sets

    for db_name, (db_path, migrations) in targets.items():
        try:
            conn = _get_conn(db_path)
            applied = _get_applied_versions(conn)
            pending = [(v, n, s) for v, n, s in migrations if v not in applied]

            if not pending:
                logger.debug(f"DB '{db_name}': already up to date (v{max(applied, default=0)})")
                conn.close()
                continue

            logger.info(f"DB '{db_name}': applying {len(pending)} migration(s)...")
            for version, name, sql_or_fn in pending:
                _apply_migration(conn, version, name, sql_or_fn)

            conn.close()
            logger.info(f"DB '{db_name}': now at v{max(v for v, _, _ in migrations)}")

        except Exception as e:
            logger.error(f"Migration error on DB '{db_name}': {e}", exc_info=True)
            raise


def get_schema_versions() -> dict:
    """Retourne la version de schéma actuelle de chaque base."""
    versions = {}
    for db_name, db_path in DB_PATHS.items():
        if not os.path.exists(db_path):
            versions[db_name] = 0
            continue
        try:
            conn = _get_conn(db_path)
            _ensure_migrations_table(conn)
            row = conn.execute(
                "SELECT MAX(version) as v FROM _schema_migrations"
            ).fetchone()
            versions[db_name] = row['v'] or 0
            conn.close()
        except Exception:
            versions[db_name] = -1
    return versions


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    logger.info("Running all migrations...")
    run_migrations()
    versions = get_schema_versions()
    for db, v in versions.items():
        print(f"  {db}: v{v}")
    print("Done.")
