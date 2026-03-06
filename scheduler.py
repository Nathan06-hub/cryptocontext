"""
Scheduler - Notifications automatiques
Résumé marché quotidien, alertes de tendance hebdomadaire
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

logger = logging.getLogger(__name__)


class CryptoScheduler:
    """Gère les tâches planifiées du bot."""

    def __init__(self, bot, fetcher, subscription_manager):
        self.bot = bot
        self.fetcher = fetcher
        self.sub_mgr = subscription_manager
        self._digest_task = None
        self._running = False

    def start(self):
        """Démarre le scheduler."""
        if not self._running:
            self._running = True
            self._digest_task = asyncio.create_task(self._digest_loop())
            logger.info("Scheduler started")

    def stop(self):
        """Arrête le scheduler."""
        self._running = False
        if self._digest_task:
            self._digest_task.cancel()

    async def _digest_loop(self):
        """Boucle principale : vérifie toutes les minutes si un digest doit être envoyé."""
        await self.bot.wait_until_ready()
        logger.info("Digest loop running")

        while self._running:
            try:
                now_utc = datetime.now(timezone.utc)
                guilds = self.sub_mgr.get_digest_guilds()

                for guild_conf in guilds:
                    digest_hour = guild_conf.get('digest_hour', 8)
                    # Envoyer si on est à la bonne heure (UTC) et à la minute 0
                    if now_utc.hour == digest_hour and now_utc.minute == 0:
                        asyncio.create_task(
                            self._send_daily_digest(guild_conf)
                        )

            except Exception as e:
                logger.error(f"Digest loop error: {e}", exc_info=True)

            # Attendre jusqu'à la prochaine minute pile
            now = datetime.now()
            seconds_to_next_minute = 60 - now.second
            await asyncio.sleep(seconds_to_next_minute)

    async def _send_daily_digest(self, guild_conf: dict):
        """Envoie le résumé quotidien dans le canal configuré."""
        import discord

        guild_id = guild_conf['guild_id']
        channel_id = guild_conf.get('digest_channel_id')

        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            logger.warning(f"Digest channel {channel_id} not found for guild {guild_id}")
            return

        try:
            # Récupérer toutes les données en parallèle
            market_data, gainers_losers, fg = await asyncio.gather(
                self.fetcher.get_market_overview(),
                self.fetcher.get_top_gainers_losers(limit=3),
                self.fetcher.get_fear_greed_index(),
                return_exceptions=True
            )

            gainers, losers = gainers_losers if not isinstance(gainers_losers, Exception) else ([], [])

            embed = await self._build_digest_embed(market_data, gainers, losers, fg)
            await channel.send(embed=embed)
            logger.info(f"Daily digest sent to guild {guild_id} channel {channel_id}")

        except Exception as e:
            logger.error(f"Error sending digest to guild {guild_id}: {e}", exc_info=True)

    async def _build_digest_embed(self, market_data, gainers, losers, fg) -> "discord.Embed":
        """Construit l'embed du résumé quotidien."""
        import discord

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%A %d %B %Y")

        # Couleur selon tendance du marché
        market_change = 0
        if market_data and not isinstance(market_data, Exception):
            market_change = market_data.get('market_cap_change_24h', 0)

        color = discord.Color.green() if market_change >= 0 else discord.Color.red()

        embed = discord.Embed(
            title=f"📰 Résumé Crypto du Jour — {date_str}",
            description="Votre briefing quotidien des marchés crypto",
            color=color,
            timestamp=now
        )

        # Marché global
        if market_data and not isinstance(market_data, Exception):
            change = market_data.get('market_cap_change_24h', 0)
            arrow = "📈" if change >= 0 else "📉"
            sign = "+" if change >= 0 else ""

            def fmt_num(n):
                if n >= 1e12:
                    return f"${n/1e12:.2f}T"
                elif n >= 1e9:
                    return f"${n/1e9:.2f}B"
                return f"${n:,.0f}"

            embed.add_field(
                name="🌍 Marché Global",
                value=(
                    f"Cap: **{fmt_num(market_data.get('total_market_cap', 0))}**\n"
                    f"Volume: {fmt_num(market_data.get('total_volume_24h', 0))}\n"
                    f"{arrow} 24h: **{sign}{change:.2f}%**"
                ),
                inline=True
            )
            embed.add_field(
                name="📊 Dominance",
                value=(
                    f"₿ BTC: **{market_data.get('btc_dominance', 0):.1f}%**\n"
                    f"Ξ ETH: **{market_data.get('eth_dominance', 0):.1f}%**"
                ),
                inline=True
            )

        # Fear & Greed
        if fg and not isinstance(fg, Exception):
            val = fg['value']
            cls = fg['classification']
            emoji = "😱" if val < 25 else "😰" if val < 45 else "😐" if val < 55 else "😊" if val < 75 else "🤑"
            embed.add_field(
                name="Fear & Greed",
                value=f"{emoji} **{val}/100**\n{cls}",
                inline=True
            )

        # Top gainers
        if gainers:
            text = ""
            for i, c in enumerate(gainers, 1):
                text += f"{i}. **{c['symbol']}** +{c['change_24h']:.1f}%\n"
            embed.add_field(name="🚀 Top Gainers 24h", value=text, inline=True)

        # Top losers
        if losers:
            text = ""
            for i, c in enumerate(losers, 1):
                text += f"{i}. **{c['symbol']}** {c['change_24h']:.1f}%\n"
            embed.add_field(name="📉 Top Losers 24h", value=text, inline=True)

        embed.set_footer(text="CryptoBot • Digest quotidien • /setdigest pour configurer")
        return embed

    async def send_test_digest(self, channel) -> bool:
        """Envoie un digest de test immédiatement."""
        try:
            market_data, gainers_losers, fg = await asyncio.gather(
                self.fetcher.get_market_overview(),
                self.fetcher.get_top_gainers_losers(limit=3),
                self.fetcher.get_fear_greed_index(),
                return_exceptions=True
            )
            gainers, losers = gainers_losers if not isinstance(gainers_losers, Exception) else ([], [])
            embed = await self._build_digest_embed(market_data, gainers, losers, fg)
            await channel.send(content="🧪 **Digest de test :**", embed=embed)
            return True
        except Exception as e:
            logger.error(f"Error sending test digest: {e}")
            return False
