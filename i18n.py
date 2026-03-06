"""
i18n — Internationalisation minimale FR / EN / ES
Ajouter une langue : dupliquer un bloc et traduire les valeurs.
"""

TRANSLATIONS = {
    'fr': {
        'price_title':        "{arrow} {name} ({symbol})",
        'price_footer':       "Plan: {tier} • Devise: {currency} • /currency pour changer",
        'alert_created':      "🔔 Alerte créée !",
        'alert_triggered':    "🔔 Alerte déclenchée !",
        'alert_reached':      "**{symbol}** a atteint votre prix cible !",
        'watchlist_empty':    "Votre watchlist est vide.\nAjoutez des cryptos avec `/watchadd BTC`",
        'plan_current':       "📋 Votre plan : {name}",
        'upgrade_title':      "⬆️ Plans d'abonnement",
        'no_data':            "❌ Impossible de récupérer les données.",
        'error_generic':      "❌ Une erreur est survenue.",
        'not_found':          "❌ Impossible de trouver **{symbol}**",
        'cooldown_msg':       "⏳ Commande `/{cmd}` disponible dans **{remaining}s**.",
        'feature_locked':     "🔒 Cette fonctionnalité nécessite le plan **{tier}**.\n👉 `/upgrade`",
        'feedback_thanks':    "✅ Merci pour votre retour ! Nous l'examinerons prochainement.",
        'convert_title':      "💱 Conversion",
        'referral_title':     "🎁 Votre programme de parrainage",
        'expiry_reminder':    "⏰ Votre abonnement {name} expire dans {days} jour(s).",
    },
    'en': {
        'price_title':        "{arrow} {name} ({symbol})",
        'price_footer':       "Plan: {tier} • Currency: {currency} • /currency to change",
        'alert_created':      "🔔 Alert created!",
        'alert_triggered':    "🔔 Alert triggered!",
        'alert_reached':      "**{symbol}** has reached your target price!",
        'watchlist_empty':    "Your watchlist is empty.\nAdd cryptos with `/watchadd BTC`",
        'plan_current':       "📋 Your plan: {name}",
        'upgrade_title':      "⬆️ Subscription Plans",
        'no_data':            "❌ Could not fetch data.",
        'error_generic':      "❌ An error occurred.",
        'not_found':          "❌ Could not find **{symbol}**",
        'cooldown_msg':       "⏳ Command `/{cmd}` available in **{remaining}s**.",
        'feature_locked':     "🔒 This feature requires the **{tier}** plan.\n👉 `/upgrade`",
        'feedback_thanks':    "✅ Thanks for your feedback! We'll review it soon.",
        'convert_title':      "💱 Conversion",
        'referral_title':     "🎁 Your referral program",
        'expiry_reminder':    "⏰ Your {name} subscription expires in {days} day(s).",
    },
    'es': {
        'price_title':        "{arrow} {name} ({symbol})",
        'price_footer':       "Plan: {tier} • Moneda: {currency} • /currency para cambiar",
        'alert_created':      "🔔 ¡Alerta creada!",
        'alert_triggered':    "🔔 ¡Alerta activada!",
        'alert_reached':      "**{symbol}** ha alcanzado su precio objetivo.",
        'watchlist_empty':    "Tu watchlist está vacía.\nAñade criptos con `/watchadd BTC`",
        'plan_current':       "📋 Tu plan: {name}",
        'upgrade_title':      "⬆️ Planes de suscripción",
        'no_data':            "❌ No se pudieron obtener los datos.",
        'error_generic':      "❌ Ocurrió un error.",
        'not_found':          "❌ No se pudo encontrar **{symbol}**",
        'cooldown_msg':       "⏳ Comando `/{cmd}` disponible en **{remaining}s**.",
        'feature_locked':     "🔒 Esta función requiere el plan **{tier}**.\n👉 `/upgrade`",
        'feedback_thanks':    "✅ ¡Gracias por tu comentario! Lo revisaremos pronto.",
        'convert_title':      "💱 Conversión",
        'referral_title':     "🎁 Tu programa de referidos",
        'expiry_reminder':    "⏰ Tu suscripción {name} vence en {days} día(s).",
    },
}

SUPPORTED_LANGUAGES = list(TRANSLATIONS.keys())
DEFAULT_LANGUAGE = 'fr'


def t(key: str, lang: str = 'fr', **kwargs) -> str:
    """
    Traduit une clé dans la langue donnée.
    Fallback automatique vers FR si la clé ou la langue est absente.
    """
    lang = lang if lang in TRANSLATIONS else DEFAULT_LANGUAGE
    text = TRANSLATIONS[lang].get(key) or TRANSLATIONS[DEFAULT_LANGUAGE].get(key, key)
    try:
        return text.format(**kwargs) if kwargs else text
    except KeyError:
        return text
