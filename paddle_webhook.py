"""
Paddle Webhook Server — Crypto Context Bot v4.0
Gère les abonnements via Paddle Billing (API v2).

Lancement : python3 paddle_webhook.py
Tourne en parallèle du bot Discord sur le port 8080.

Événements gérés :
  - transaction.completed      → Paiement initial / renouvellement réussi
  - subscription.created       → Nouvel abonnement actif
  - subscription.updated       → Changement de plan
  - subscription.canceled      → Annulation (accès jusqu'à fin de période)
  - subscription.past_due      → Paiement en retard
  - transaction.payment_failed → Échec de paiement
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import aiohttp
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# Créer les dossiers nécessaires avant tout
import pathlib
pathlib.Path('logs').mkdir(exist_ok=True)
pathlib.Path('data').mkdir(exist_ok=True)

logger = logging.getLogger(__name__)
import os as _os
_os.makedirs('logs', exist_ok=True)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[logging.StreamHandler()]
)

# ── Config ───────────────────────────────────────────────────────────────────
PADDLE_WEBHOOK_SECRET = os.getenv('PADDLE_WEBHOOK_SECRET', '')
DISCORD_BOT_TOKEN     = os.getenv('DISCORD_BOT_TOKEN', '')
# Render injecte $PORT automatiquement — on le lit en priorité
WEBHOOK_PORT          = int(os.getenv('PORT', os.getenv('WEBHOOK_PORT', 8080)))
WEBHOOK_HOST          = os.getenv('WEBHOOK_HOST', '0.0.0.0')
PADDLE_CUSTOMER_PORTAL = os.getenv('PADDLE_CUSTOMER_PORTAL', 'https://customer.paddle.com')

# Price IDs Paddle → tier interne
# Récupérables dans Paddle Dashboard > Catalog > Prices
PADDLE_PRICE_TO_TIER = {
    os.getenv('PADDLE_PRICE_BASIC',   'pri_basic_placeholder'):   'basic',
    os.getenv('PADDLE_PRICE_PRO',     'pri_pro_placeholder'):     'pro',
    os.getenv('PADDLE_PRICE_PREMIUM', 'pri_premium_placeholder'): 'premium',
}


# ── Vérification signature Paddle ─────────────────────────────────────────────

def verify_paddle_signature(payload: bytes, sig_header: str, secret: str) -> bool:
    """
    Paddle signe les webhooks avec HMAC-SHA256.
    Header : Paddle-Signature: ts=TIMESTAMP;h1=HASH
    """
    if not secret:
        logger.warning("PADDLE_WEBHOOK_SECRET non configuré — signature non vérifiée")
        return True  # Dev uniquement

    try:
        parts = {}
        for item in sig_header.split(';'):
            if '=' in item:
                k, v = item.split('=', 1)
                parts[k.strip()] = v.strip()

        timestamp = parts.get('ts', '')
        signature = parts.get('h1', '')

        if not timestamp or not signature:
            logger.warning("Paddle signature header malformé")
            return False

        # Vérifier que le timestamp n'est pas trop vieux (5 min)
        if abs(time.time() - int(timestamp)) > 300:
            logger.warning("Paddle webhook timestamp trop vieux — possible replay attack")
            return False

        signed_payload = f"{timestamp}:{payload.decode('utf-8')}"
        expected = hmac.new(
            secret.encode('utf-8'),
            signed_payload.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    except Exception as e:
        logger.error(f"Erreur vérification signature Paddle: {e}")
        return False


# ── Extraction des métadonnées Discord ───────────────────────────────────────

def extract_discord_meta(data: dict) -> tuple:
    """
    Les métadonnées Discord sont dans data.custom_data.
    Paddle les transmet depuis le checkout via :
      checkout[custom][discord_user_id]
      checkout[custom][discord_username]
      checkout[custom][tier]
    """
    custom = data.get('custom_data') or {}

    # Fallback : chercher dans les items
    if not custom:
        items = data.get('items', [])
        for item in items:
            price = item.get('price', {})
            custom = price.get('custom_data') or {}
            if custom:
                break

    user_id_raw = custom.get('discord_user_id') or custom.get('user_id')
    username    = custom.get('discord_username') or custom.get('username', 'unknown')
    tier        = custom.get('tier', 'basic')
    customer_id = str(data.get('customer_id', '') or data.get('id', ''))

    try:
        user_id = int(user_id_raw) if user_id_raw else None
    except (ValueError, TypeError):
        user_id = None

    return user_id, username, tier, customer_id


def extract_price_tier(data: dict) -> str | None:
    """Détecte le tier depuis le price_id des items."""
    items = data.get('items', [])
    for item in items:
        price_id = item.get('price', {}).get('id', '')
        tier = PADDLE_PRICE_TO_TIER.get(price_id)
        if tier:
            return tier
    return None


def extract_amount(data: dict) -> tuple[float, str]:
    """Extrait le montant et la devise."""
    details  = data.get('details', {})
    totals   = details.get('totals', {}) or data.get('totals', {})
    total    = totals.get('total', '0')
    currency = data.get('currency_code', 'EUR').upper()
    try:
        return float(total) / 100, currency
    except (ValueError, TypeError):
        return 0.0, currency


# ── Gestion des événements Paddle ─────────────────────────────────────────────

class PaddleEventHandler:

    def __init__(self):
        from subscription_manager import subscription_manager
        self.sub_mgr = subscription_manager

    async def handle(self, event_type: str, data: dict) -> dict:
        logger.info(f"Paddle event: {event_type}")

        handlers = {
            'transaction.completed':       self._on_transaction_completed,
            'subscription.created':        self._on_subscription_created,
            'subscription.updated':        self._on_subscription_updated,
            'subscription.canceled':       self._on_subscription_cancelled,
            'subscription.past_due':       self._on_subscription_past_due,
            'transaction.payment_failed':  self._on_payment_failed,
        }

        handler = handlers.get(event_type)
        if handler:
            return await handler(data) or {}

        logger.debug(f"Événement Paddle non géré: {event_type}")
        return {}

    # ── Handlers ─────────────────────────────────────────────────────────────

    async def _on_transaction_completed(self, data: dict) -> dict:
        """
        Transaction complétée — couvre :
        - Premier paiement (subscription.created arrive juste après)
        - Renouvellements mensuels
        """
        origin = data.get('origin', '')
        sub_id = data.get('subscription_id', '')

        # Renouvellement mensuel
        if origin == 'subscription_recurring' and sub_id:
            customer_id = str(data.get('customer_id', ''))
            amount, currency = extract_amount(data)
            sub = self.sub_mgr.get_sub_by_stripe_customer(customer_id)

            if sub and sub.get('tier') != 'free':
                self.sub_mgr.upgrade_user(
                    sub['user_id'], sub['username'], sub['tier'],
                    stripe_customer_id=customer_id,
                    months=1
                )
                logger.info(f"Renouvellement OK: user {sub['user_id']} — {amount:.2f} {currency}")
                return {
                    'action': 'renewed',
                    'user_id': sub['user_id'],
                    'username': sub['username'],
                    'tier': sub['tier'],
                    'amount': amount,
                    'currency': currency,
                }
            return {}

        # Premier paiement — on attend subscription.created
        logger.info(f"transaction.completed (origin: {origin}) — attente subscription.created")
        return {}

    async def _on_subscription_created(self, data: dict) -> dict:
        """Nouvel abonnement créé et actif."""
        status = data.get('status', '')
        if status not in ('active', 'trialing'):
            logger.info(f"subscription.created avec status {status}, ignoré")
            return {}

        user_id, username, tier, customer_id = extract_discord_meta(data)
        sub_id = str(data.get('id', ''))

        if not user_id:
            # Fallback : chercher via customer_id
            if customer_id:
                sub = self.sub_mgr.get_sub_by_stripe_customer(customer_id)
                if sub:
                    user_id  = sub['user_id']
                    username = sub['username']
            if not user_id:
                logger.error("subscription.created: discord_user_id manquant")
                return {}

        # Détecter le tier via price_id si absent des custom_data
        tier = tier or extract_price_tier(data) or 'basic'

        success = self.sub_mgr.upgrade_user(
            user_id, username, tier,
            stripe_customer_id=customer_id,
            stripe_subscription_id=sub_id,
            months=1
        )

        if success:
            logger.info(f"Nouvel abonnement: user {user_id} → {tier} (sub: {sub_id})")
            return {
                'action': 'upgraded',
                'user_id': user_id,
                'username': username,
                'tier': tier,
                'amount': 0,
                'currency': 'EUR',
            }
        return {}

    async def _on_subscription_updated(self, data: dict) -> dict:
        """Changement de plan (upgrade/downgrade)."""
        customer_id = str(data.get('customer_id', ''))
        sub         = self.sub_mgr.get_sub_by_stripe_customer(customer_id)

        if not sub:
            logger.warning(f"subscription.updated: customer inconnu {customer_id}")
            return {}

        new_tier = extract_price_tier(data)
        if not new_tier:
            logger.warning("subscription.updated: price_id inconnu dans PADDLE_PRICE_TO_TIER")
            return {}

        if new_tier == sub.get('tier'):
            logger.info("subscription.updated: même tier, pas de changement")
            return {}

        self.sub_mgr.upgrade_user(
            sub['user_id'], sub['username'], new_tier,
            stripe_customer_id=customer_id,
            months=1
        )
        logger.info(f"Changement plan: user {sub['user_id']} → {new_tier}")
        return {
            'action': 'plan_changed',
            'user_id': sub['user_id'],
            'username': sub['username'],
            'tier': new_tier,
        }

    async def _on_subscription_cancelled(self, data: dict) -> dict:
        """
        Abonnement annulé.
        Paddle maintient l'accès jusqu'à scheduled_change.effective_at.
        On notifie mais on ne dégrade pas immédiatement.
        """
        customer_id = str(data.get('customer_id', ''))
        ends_at     = data.get('current_billing_period', {}).get('ends_at', '')
        sub         = self.sub_mgr.get_sub_by_stripe_customer(customer_id)

        if not sub:
            return {}

        logger.info(f"Annulation: user {sub['user_id']} — accès jusqu'à {ends_at}")
        return {
            'action': 'cancelled',
            'user_id': sub['user_id'],
            'username': sub['username'],
        }

    async def _on_subscription_past_due(self, data: dict) -> dict:
        """Abonnement en retard de paiement."""
        customer_id = str(data.get('customer_id', ''))
        sub         = self.sub_mgr.get_sub_by_stripe_customer(customer_id)

        if sub:
            logger.warning(f"Paiement en retard: user {sub['user_id']}")
            return {
                'action': 'payment_failed',
                'user_id': sub['user_id'],
                'username': sub['username'],
                'tier': sub.get('tier', 'unknown'),
            }
        return {}

    async def _on_payment_failed(self, data: dict) -> dict:
        """Échec de transaction."""
        customer_id = str(data.get('customer_id', ''))
        sub         = self.sub_mgr.get_sub_by_stripe_customer(customer_id)

        if sub:
            logger.warning(f"Échec paiement: user {sub['user_id']}")
            return {
                'action': 'payment_failed',
                'user_id': sub['user_id'],
                'username': sub['username'],
                'tier': sub.get('tier', 'unknown'),
            }
        return {}


# ── Notifications Discord ─────────────────────────────────────────────────────

class DiscordNotifier:

    BASE = "https://discord.com/api/v10"

    def __init__(self):
        self._session = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession(headers={
                'Authorization': f'Bot {DISCORD_BOT_TOKEN}',
                'Content-Type': 'application/json',
            })
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_dm_channel(self, user_id: int) -> str | None:
        session = await self._get_session()
        try:
            async with session.post(
                f"{self.BASE}/users/@me/channels",
                json={'recipient_id': str(user_id)}
            ) as resp:
                if resp.status == 200:
                    return (await resp.json()).get('id')
        except Exception as e:
            logger.error(f"Erreur ouverture DM: {e}")
        return None

    async def send_dm(self, user_id: int, embed: dict, max_retries: int = 4) -> bool:
        channel_id = await self._get_dm_channel(user_id)
        if not channel_id:
            return False

        session = await self._get_session()
        for attempt in range(max_retries):
            try:
                async with session.post(
                    f"{self.BASE}/channels/{channel_id}/messages",
                    json={'embeds': [embed]}
                ) as resp:
                    if resp.status == 200:
                        return True
                    if resp.status == 429:
                        data = await resp.json()
                        retry_after = float(data.get('retry_after', 2 ** attempt))
                        logger.warning(f"Rate-limit Discord. Retry in {retry_after:.1f}s")
                        await asyncio.sleep(retry_after)
                        continue
                    if resp.status == 403:
                        logger.info(f"DMs fermés pour {user_id}")
                        return False
                    if resp.status >= 500:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return False
            except asyncio.TimeoutError:
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Erreur DM Discord: {e}")
                return False
        return False

    async def notify(self, action_data: dict):
        if not action_data or 'user_id' not in action_data:
            return

        action   = action_data.get('action', '')
        user_id  = action_data['user_id']
        username = action_data.get('username', 'utilisateur')

        from subscription_manager import TIER_CONFIGS
        tier        = action_data.get('tier', 'free')
        tier_config = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])

        embed = None

        if action == 'upgraded':
            amount   = action_data.get('amount', 0)
            currency = action_data.get('currency', 'EUR')
            price_str = f"{amount:.2f} {currency}/mois" if amount > 0 else tier_config.name
            embed = {
                'title': f'🎉 Bienvenue sur le plan {tier_config.name} !',
                'description': (
                    f'Merci pour votre abonnement **{tier_config.name}** ({price_str}).\n\n'
                    f'Vos nouvelles fonctionnalités sont actives :\n'
                    f'• 🔔 {tier_config.alerts_limit} alertes\n'
                    f'• 👀 {tier_config.watchlist_limit} cryptos en watchlist\n'
                    f'• 💱 {len(tier_config.currencies)} devises disponibles'
                ),
                'color': tier_config.color,
                'footer': {'text': 'Crypto Context Bot • Paiement via Paddle'},
                'timestamp': _now_iso(),
            }

        elif action == 'renewed':
            amount   = action_data.get('amount', 0)
            currency = action_data.get('currency', 'EUR')
            embed = {
                'title': f'✅ Abonnement renouvelé — {tier_config.name}',
                'description': (
                    f'Votre abonnement a été renouvelé avec succès'
                    + (f' ({amount:.2f} {currency})' if amount > 0 else '') + '.\n'
                    f'Accès garanti pour un mois supplémentaire. Merci ! 🙏'
                ),
                'color': 0x2ecc71,
                'footer': {'text': 'Crypto Context Bot'},
                'timestamp': _now_iso(),
            }

        elif action == 'plan_changed':
            embed = {
                'title': f'🔄 Plan modifié → {tier_config.name}',
                'description': f'Votre plan a été mis à jour vers **{tier_config.name}**.',
                'color': tier_config.color,
                'footer': {'text': 'Crypto Context Bot'},
                'timestamp': _now_iso(),
            }

        elif action == 'cancelled':
            embed = {
                'title': '😢 Abonnement annulé',
                'description': (
                    'Votre abonnement a été annulé.\n\n'
                    'Votre accès reste actif jusqu\'à la fin de la période payée.\n'
                    'Vous pouvez vous réabonner à tout moment avec `/subscribe`.\n'
                    'Vos données sont conservées.'
                ),
                'color': 0xe67e22,
                'footer': {'text': 'Crypto Context Bot'},
                'timestamp': _now_iso(),
            }

        elif action == 'payment_failed':
            embed = {
                'title': '⚠️ Échec de paiement',
                'description': (
                    'Nous n\'avons pas pu traiter votre paiement.\n\n'
                    f'**Action requise :** Mettez à jour votre moyen de paiement :\n'
                    f'[🔗 Gérer mon abonnement]({PADDLE_CUSTOMER_PORTAL})\n\n'
                    'Sans action sous 7 jours, votre compte sera rétrogradé en **Free**.'
                ),
                'color': 0xf39c12,
                'footer': {'text': 'Crypto Context Bot'},
                'timestamp': _now_iso(),
            }

        if embed:
            sent = await self.send_dm(user_id, embed)
            logger.info(f"DM Discord → {username} ({user_id}) [{action}]: {'OK' if sent else 'FAILED'}")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── Serveur Web ───────────────────────────────────────────────────────────────

class WebhookServer:

    def __init__(self):
        self.event_handler = PaddleEventHandler()
        self.notifier      = DiscordNotifier()
        self.app           = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        self.app.router.add_post('/paddle/webhook', self.handle_paddle_webhook)
        self.app.router.add_post('/webhook',        self.handle_paddle_webhook)  # alias
        self.app.router.add_get('/health',          self.health_check)

    async def health_check(self, request: web.Request) -> web.Response:
        return web.json_response({
            'status': 'ok',
            'service': 'CryptoBot Paddle Webhook',
            'version': '4.0',
        })

    async def handle_paddle_webhook(self, request: web.Request) -> web.Response:
        payload    = await request.read()
        sig_header = request.headers.get('Paddle-Signature', '')

        # 1. Vérifier la signature
        if not verify_paddle_signature(payload, sig_header, PADDLE_WEBHOOK_SECRET):
            logger.warning(f"Signature Paddle invalide depuis {request.remote}")
            return web.Response(status=400, text="Invalid signature")

        # 2. Parser le JSON
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        event_id   = body.get('event_id', '')
        event_type = body.get('event_type', '')
        event_data = body.get('data', {})

        logger.info(f"Paddle event reçu: {event_type} (id: {event_id})")

        # 3. Idempotence
        from subscription_manager import subscription_manager as sub_mgr
        if event_id and sub_mgr.is_event_processed(event_id):
            logger.info(f"Événement déjà traité: {event_id}")
            return web.Response(status=200, text="Already processed")

        # 4. Traiter en tâche de fond
        asyncio.create_task(self._process_event(event_id, event_type, event_data))

        # Paddle exige une réponse 200 rapide
        return web.Response(status=200, text="OK")

    async def _process_event(self, event_id: str, event_type: str, event_data: dict):
        try:
            action_data = await self.event_handler.handle(event_type, event_data)
            if action_data:
                await self.notifier.notify(action_data)
            if event_id:
                from subscription_manager import subscription_manager as sub_mgr
                sub_mgr.mark_event_processed(event_id, event_type)
        except Exception as e:
            logger.error(f"Erreur traitement événement Paddle {event_type}: {e}", exc_info=True)

    async def start(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, WEBHOOK_HOST, WEBHOOK_PORT)
        await site.start()
        logger.info(f"Paddle webhook server: http://{WEBHOOK_HOST}:{WEBHOOK_PORT}")
        logger.info(f"  POST /paddle/webhook — Événements Paddle")
        logger.info(f"  GET  /health         — Health check")

    async def cleanup(self):
        await self.notifier.close()


# ── Validation & Main ─────────────────────────────────────────────────────────

def validate_env() -> bool:
    errors, warnings = [], []

    if not DISCORD_BOT_TOKEN:
        errors.append("DISCORD_BOT_TOKEN manquant")
    if not PADDLE_WEBHOOK_SECRET:
        warnings.append("PADDLE_WEBHOOK_SECRET absent — signatures non vérifiées (dangereux en prod)")
    if 'pri_basic_placeholder' in PADDLE_PRICE_TO_TIER:
        warnings.append("PADDLE_PRICE_BASIC non configuré — changements de plan non détectés")

    for w in warnings:
        logger.warning(f"  ⚠️  {w}")
    if errors:
        for e in errors:
            logger.error(f"  ❌  {e}")
        logger.error("Vérifiez votre fichier .env")
        return False

    logger.info("✅ Config Paddle OK")
    return True


async def main():
    os.makedirs('logs', exist_ok=True)

    if not validate_env():
        raise SystemExit(1)

    server = WebhookServer()
    await server.start()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Arrêt du webhook server...")
    finally:
        await server.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
