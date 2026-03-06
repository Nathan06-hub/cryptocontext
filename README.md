# 🤖 Crypto Context Bot v3.0

## 🆕 Nouveautés v3.0
- **aiohttp natif** — Plus de `run_in_executor`, zéro blocage asyncio
- **Multi-devises** — USD, EUR, GBP, JPY, CHF, CAD, AUD, BTC, ETH
- **Watchlist** — `/watchlist`, `/watchadd`, `/watchremove`
- **Digest quotidien** — Résumé automatique dans un canal à l'heure choisie
- **Système d'abonnements** — Free / Basic / Pro / Premium avec limites par tier
- **Stripe** — Paiements sécurisés, webhooks, gestion automatique des expirations
- **RGPD** — `/deletemydata` + `/confirmdeletion`
- **Admin** — `/adminsetplan`, `/adminstats`

## 📦 Installation

```bash
pip install -r requirements_discord.txt
cp .env.example .env
# Éditez .env avec votre token Discord et clés Stripe
python3 bot_discord.py
```

## 🎯 Commandes

| Commande | Description | Plan requis |
|----------|-------------|-------------|
| `/price BTC eur` | Prix + stats | Free |
| `/market` | Vue mondiale + Fear & Greed | Free |
| `/top` | Top gainers/losers | Free |
| `/compare BTC ETH` | Comparaison | Free |
| `/analyze BTC` | Analyse technique | Free (avancée: Pro) |
| `/chart BTC 30` | Graphique historique | Basic+ |
| `/portfolio BTC:0.5,ETH:2` | Valeur portefeuille | Basic+ |
| `/watchlist` | Voir watchlist | Free |
| `/watchadd SOL` | Ajouter à la watchlist | Free |
| `/currency eur` | Changer devise par défaut | Free/limité |
| `/alert BTC 50000` | Créer alerte prix | Free (3 max) |
| `/setdigest #canal 8` | Digest quotidien à 8h UTC | Pro+ |
| `/plan` | Voir votre plan | Free |
| `/upgrade` | Plans et tarifs | Free |
| `/deletemydata` | Supprimer vos données (RGPD) | Free |

## 💰 Tiers

| Plan | Prix | Alertes | Watchlist | Devises |
|------|------|---------|-----------|---------|
| Free | 0€ | 3 | 5 | USD |
| Basic | 4.99€/mois | 10 | 20 | USD, EUR, GBP |
| Pro | 14.99€/mois | 50 | 100 | 7 devises + digest |
| Premium | 29.99€/mois | ∞ | ∞ | 9 devises |

## 🔧 Stripe Setup

1. Créez un compte sur [stripe.com](https://stripe.com)
2. Créez des Payment Links pour chaque tier
3. Ajoutez `discord_user_id` et `discord_username` dans les métadonnées
4. Configurez un webhook pointant vers votre serveur
5. Copiez le `STRIPE_WEBHOOK_SECRET` dans `.env`

## 📁 Structure

```
crypto_bot/
├── bot_discord.py           # Bot principal
├── crypto_fetcher.py        # aiohttp + cache async
├── crypto_analyzer.py       # RSI, MACD, Bollinger, MA
├── alert_manager.py         # Alertes SQLite
├── watchlist_manager.py     # Watchlists SQLite
├── subscription_manager.py  # Abonnements + Stripe
├── scheduler.py             # Digest quotidien
├── data/
│   ├── alerts.db
│   ├── watchlists.db
│   └── subscriptions.db
├── logs/bot.log
├── .env
└── requirements_discord.txt
```
