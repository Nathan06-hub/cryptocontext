"""
Backup Manager — Sauvegarde automatique des bases SQLite
Local par défaut, S3-compatible optionnel (boto3).
"""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

DATA_DIR   = Path(os.path.dirname(__file__)) / 'data'
BACKUP_DIR = Path(os.path.dirname(__file__)) / 'backups'

# Garder les N derniers backups locaux
MAX_LOCAL_BACKUPS = int(os.getenv('MAX_LOCAL_BACKUPS', 24))

# S3 (optionnel)
S3_BUCKET     = os.getenv('S3_BACKUP_BUCKET', '')
S3_PREFIX     = os.getenv('S3_BACKUP_PREFIX', 'cryptobot/backups')
AWS_ACCESS_KEY = os.getenv('AWS_ACCESS_KEY_ID', '')
AWS_SECRET_KEY = os.getenv('AWS_SECRET_ACCESS_KEY', '')
AWS_REGION     = os.getenv('AWS_REGION', 'eu-west-3')


async def run_backup() -> dict:
    """
    Effectue un backup de toutes les bases SQLite.
    Retourne un résumé {'files': [...], 's3': bool, 'size_mb': float}
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp  = datetime.now().strftime('%Y%m%d_%H%M%S')
    db_files   = list(DATA_DIR.glob('*.db'))
    backed_up  = []
    total_size = 0

    if not db_files:
        logger.warning("No .db files found to backup")
        return {'files': [], 's3': False, 'size_mb': 0}

    # Backup local (copie simple — SQLite est safe en lecture concurrente avec WAL)
    backup_subdir = BACKUP_DIR / timestamp
    backup_subdir.mkdir(parents=True, exist_ok=True)

    for db_path in db_files:
        dest = backup_subdir / db_path.name
        shutil.copy2(db_path, dest)
        size = dest.stat().st_size
        total_size += size
        backed_up.append(str(dest))
        logger.info(f"Backed up {db_path.name} ({size / 1024:.1f} KB)")

    # Nettoyage des vieux backups locaux
    _cleanup_old_backups()

    # Upload S3 optionnel
    s3_ok = False
    if S3_BUCKET and AWS_ACCESS_KEY:
        s3_ok = await asyncio.get_event_loop().run_in_executor(
            None, _upload_to_s3, backup_subdir, timestamp
        )

    size_mb = total_size / (1024 * 1024)
    logger.info(f"Backup complete: {len(backed_up)} files, {size_mb:.2f} MB, S3={s3_ok}")

    return {'files': backed_up, 's3': s3_ok, 'size_mb': round(size_mb, 2), 'timestamp': timestamp}


def _cleanup_old_backups():
    """Supprime les backups locaux au-delà de MAX_LOCAL_BACKUPS."""
    subdirs = sorted(BACKUP_DIR.glob('*'), key=lambda p: p.stat().st_mtime)
    while len(subdirs) > MAX_LOCAL_BACKUPS:
        oldest = subdirs.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        logger.info(f"Deleted old backup: {oldest.name}")


def _upload_to_s3(backup_dir: Path, timestamp: str) -> bool:
    """Upload vers S3 (synchrone, à appeler dans un executor)."""
    try:
        import boto3
        s3 = boto3.client(
            's3',
            aws_access_key_id=AWS_ACCESS_KEY,
            aws_secret_access_key=AWS_SECRET_KEY,
            region_name=AWS_REGION
        )
        for f in backup_dir.glob('*.db'):
            key = f"{S3_PREFIX}/{timestamp}/{f.name}"
            s3.upload_file(str(f), S3_BUCKET, key)
            logger.info(f"Uploaded to s3://{S3_BUCKET}/{key}")
        return True
    except ImportError:
        logger.warning("boto3 not installed — S3 backup skipped")
        return False
    except Exception as e:
        logger.error(f"S3 upload error: {e}")
        return False


def list_backups() -> List[dict]:
    """Liste tous les backups disponibles localement."""
    if not BACKUP_DIR.exists():
        return []
    result = []
    for subdir in sorted(BACKUP_DIR.glob('*'), reverse=True):
        if subdir.is_dir():
            files = list(subdir.glob('*.db'))
            total = sum(f.stat().st_size for f in files)
            result.append({
                'timestamp': subdir.name,
                'files':     [f.name for f in files],
                'size_mb':   round(total / (1024 * 1024), 2),
            })
    return result
