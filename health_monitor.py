"""
Health Monitor — Watchdog et métriques du bot
Détecte les déconnexions silencieuses et alerte l\'admin Discord.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Surveille la santé du bot et envoie des alertes à l\'admin Discord
    en cas de problème (déconnexion, erreurs répétées, latence élevée).
    """

    LATENCY_WARN_MS  = 500   # ms
    LATENCY_CRIT_MS  = 1000  # ms
    CHECK_INTERVAL   = 60    # secondes entre chaque vérification
    MAX_ERRORS_1H    = 20    # alerter si plus de N erreurs en 1h

    def __init__(self, bot):
        self.bot            = bot
        self.admin_user_id  = int(os.getenv('ADMIN_DISCORD_ID', 0))
        self._error_counts  = []   # timestamps des erreurs
        self._last_alert_ts = 0.0
        self._alert_cooldown = 3600  # 1h entre deux alertes du même type
        self._task: Optional[asyncio.Task] = None
        self._start_time    = time.monotonic()

    def start(self):
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("HealthMonitor started")

    def stop(self):
        if self._task:
            self._task.cancel()

    def record_error(self):
        """À appeler quand une erreur non gérée est détectée."""
        self._error_counts.append(time.monotonic())
        # Nettoyer les erreurs > 1h
        cutoff = time.monotonic() - 3600
        self._error_counts = [t for t in self._error_counts if t > cutoff]

    def get_uptime(self) -> str:
        elapsed = int(time.monotonic() - self._start_time)
        h, m = divmod(elapsed // 60, 60)
        return f"{h}h {m}m"

    def get_stats(self) -> dict:
        return {
            'latency_ms':    round(self.bot.latency * 1000),
            'guilds':        len(self.bot.guilds),
            'uptime':        self.get_uptime(),
            'errors_1h':     len(self._error_counts),
            'is_ready':      self.bot.is_ready(),
        }

    async def _send_admin_alert(self, title: str, description: str, color: int = 0xe74c3c):
        """Envoie une alerte DM à l\'admin."""
        if not self.admin_user_id:
            return
        # Cooldown : une alerte toutes les ALERT_COOLDOWN secondes max
        now = time.monotonic()
        if now - self._last_alert_ts < self._alert_cooldown:
            return
        self._last_alert_ts = now

        try:
            import discord
            admin = await self.bot.fetch_user(self.admin_user_id)
            embed = discord.Embed(
                title=f"🚨 {title}",
                description=description,
                color=color,
                timestamp=datetime.now(timezone.utc)
            )
            stats = self.get_stats()
            embed.add_field(name="Latence", value=f"{stats['latency_ms']}ms", inline=True)
            embed.add_field(name="Serveurs", value=str(stats['guilds']), inline=True)
            embed.add_field(name="Uptime", value=stats['uptime'], inline=True)
            embed.set_footer(text="Crypto Context Bot Health Monitor")
            await admin.send(embed=embed)
            logger.warning(f"Admin alert sent: {title}")
        except Exception as e:
            logger.error(f"Failed to send admin alert: {e}")

    async def _monitor_loop(self):
        await self.bot.wait_until_ready()
        logger.info("HealthMonitor loop running")

        while True:
            try:
                await asyncio.sleep(self.CHECK_INTERVAL)

                if not self.bot.is_ready():
                    await self._send_admin_alert(
                        "Bot déconnecté",
                        "Le bot Discord n\'est plus connecté (is_ready() = False)."
                    )
                    continue

                # Vérification latence
                latency_ms = round(self.bot.latency * 1000)
                if latency_ms > self.LATENCY_CRIT_MS:
                    await self._send_admin_alert(
                        "Latence critique",
                        f"Latence Discord : **{latency_ms}ms** (seuil critique : {self.LATENCY_CRIT_MS}ms)"
                    )
                elif latency_ms > self.LATENCY_WARN_MS:
                    logger.warning(f"High latency: {latency_ms}ms")

                # Vérification erreurs répétées
                errors_1h = len(self._error_counts)
                if errors_1h >= self.MAX_ERRORS_1H:
                    await self._send_admin_alert(
                        "Erreurs répétées",
                        f"**{errors_1h} erreurs** détectées au cours de la dernière heure.\n"
                        f"Consultez les logs pour plus de détails."
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"HealthMonitor loop error: {e}")
