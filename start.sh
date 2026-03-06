#!/bin/bash
# ============================================================
# Crypto Context Bot v3.0 — Script de démarrage
# Lance le bot Discord ET le serveur webhook Paddle
# ============================================================

set -e

echo "🚀 Crypto Context Bot v4.0"
echo "─────────────────────────"

# Sur Render, les variables d'env sont injectées directement — pas besoin de .env
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 non trouvé."
    exit 1
fi

# Créer les dossiers nécessaires
mkdir -p logs data

echo ""
echo "▶️  Démarrage du bot Discord..."
echo "▶️  Démarrage du serveur webhook Paddle..."
echo ""
echo "Logs : logs/bot.log et logs/webhook.log"
echo "Arrêt : Ctrl+C"
echo "─────────────────────────"

# Lancer les deux processus en parallèle
python3 bot_discord.py &
BOT_PID=$!

python3 paddle_webhook.py &
WEBHOOK_PID=$!

echo "✅ Bot Discord PID: $BOT_PID"
echo "✅ Webhook Server PID: $WEBHOOK_PID"

# Attendre et gérer Ctrl+C proprement
trap "echo ''; echo '🛑 Arrêt en cours...'; kill $BOT_PID $WEBHOOK_PID 2>/dev/null; exit 0" SIGINT SIGTERM

wait $BOT_PID $WEBHOOK_PID
