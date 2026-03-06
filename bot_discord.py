"""
Crypto Context Bot v3.0
aiohttp natif | Multi-devises | Watchlist | Digest | Abonnements | RGPD
"""

import logging
import asyncio
import io
import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from crypto_fetcher import fetcher
from crypto_analyzer import analyzer
from alert_manager import alert_manager
from watchlist_manager import watchlist_manager
from subscription_manager import subscription_manager, TIER_CONFIGS, CURRENCY_SYMBOLS, Tier
from scheduler import CryptoScheduler
from db_migrations import run_migrations
from referral_manager import referral_manager
from health_monitor import HealthMonitor
from backup_manager import run_backup, list_backups
from i18n import t, SUPPORTED_LANGUAGES

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
BOT_NAME = os.getenv('BOT_NAME', 'CryptoBot')
PADDLE_STORE_URL = os.getenv('PADDLE_STORE_URL', 'https://buy.paddle.com')

# Mode paiement manuel (avant activation Paddle)
MANUAL_MODE       = os.getenv('MANUAL_MODE', 'true').lower() == 'true'
CONTACT_DISCORD   = os.getenv('CONTACT_DISCORD', 'ton_pseudo_discord')
CONTACT_EMAIL     = os.getenv('CONTACT_EMAIL', 'maresteph06@gmail.com')
PAYPAL_LINK       = os.getenv('PAYPAL_LINK', '')

os.makedirs('logs', exist_ok=True)
os.makedirs('data', exist_ok=True)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
scheduler      = CryptoScheduler(bot, fetcher, subscription_manager)
health_monitor = HealthMonitor(bot)


# ==================== COOLDOWN ====================

from collections import defaultdict

class CooldownBucket:
    """
    Cooldown par utilisateur et par commande.
    Tiers supérieurs = cooldown réduit (cache prioritaire = données plus fraîches).
    """
    # Cooldowns en secondes par tier
    COOLDOWNS = {
        'free':    15,
        'basic':   8,
        'pro':     4,
        'premium': 2,
    }
    # Commandes légères : cooldown fixe court quel que soit le tier
    LIGHT_COMMANDS = {'ping', 'help', 'about', 'plan', 'myalerts', 'watchlist', 'alerthistory'}
    LIGHT_COOLDOWN = 3

    def __init__(self):
        # {(user_id, command_name): last_used_timestamp}
        self._last_used: dict[tuple, float] = defaultdict(float)

    def check(self, user_id: int, command: str, tier: str) -> tuple[bool, float]:
        """
        Vérifie si la commande est disponible.
        Retourne (ok, remaining_seconds).
        """
        import time
        now = time.monotonic()
        key = (user_id, command)

        if command in self.LIGHT_COMMANDS:
            cooldown = self.LIGHT_COOLDOWN
        else:
            cooldown = self.COOLDOWNS.get(tier, self.COOLDOWNS['free'])

        elapsed = now - self._last_used[key]
        if elapsed >= cooldown:
            self._last_used[key] = now
            return True, 0.0
        return False, round(cooldown - elapsed, 1)

    def reset(self, user_id: int, command: str):
        """Reset manuel (utile après une erreur pour ne pas pénaliser l'utilisateur)."""
        self._last_used.pop((user_id, command), None)


cooldown_bucket = CooldownBucket()


def with_cooldown(func):
    """
    Décorateur qui applique le cooldown à une commande slash.
    Doit être appliqué APRÈS @bot.tree.command.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(interaction: discord.Interaction, *args, **kwargs):
        user_id = interaction.user.id
        command = interaction.command.name if interaction.command else 'unknown'
        tier    = subscription_manager.get_tier(user_id)

        ok, remaining = cooldown_bucket.check(user_id, command, tier)
        if not ok:
            tier_config = subscription_manager.get_tier_config(user_id)
            msg = f"⏳ Commande `/{command}` disponible dans **{remaining}s**.\n"
            # Inciter à l'upgrade si Free ou Basic
            if tier in ('free', 'basic'):
                msg += f"Les plans supérieurs ont des cooldowns réduits. `/upgrade`"
            await interaction.response.send_message(msg, ephemeral=True)
            return
        return await func(interaction, *args, **kwargs)

    return wrapper


# ==================== EVENTS ====================

@bot.event
async def on_ready():
    logger.info(f'{bot.user} is now online! ({len(bot.guilds)} servers)')
    try:
        synced = await bot.tree.sync()
        logger.info(f'Synced {len(synced)} slash commands')
    except Exception as e:
        logger.error(f'Sync error: {e}')

    monitor_alerts_loop.start()
    cleanup_loop.start()
    expiry_reminder_loop.start()
    scheduler.start()
    health_monitor.start()

    await bot.change_presence(
        activity=discord.Activity(type=discord.ActivityType.watching, name="crypto markets 📊")
    )


@bot.event
async def on_shutdown():
    await fetcher.close()
    scheduler.stop()
    health_monitor.stop()


# ==================== DECORATEURS TIERS ====================

def require_tier(*tiers: str):
    """Décorateur : vérifie que l'utilisateur a le tier requis."""
    def decorator(func):
        async def wrapper(interaction: discord.Interaction, *args, **kwargs):
            user_tier = subscription_manager.get_tier(interaction.user.id)
            if user_tier not in tiers:
                config = TIER_CONFIGS.get(tiers[0])
                tier_name = config.name if config else tiers[0]
                embed = discord.Embed(
                    title="🔒 Fonctionnalité Premium",
                    description=(
                        f"Cette commande nécessite le plan **{tier_name}**.\n\n"
                        f"Votre plan actuel : **{TIER_CONFIGS.get(user_tier, TIER_CONFIGS['free']).name}**\n\n"
                        f"👉 `/upgrade` pour voir les options"
                    ),
                    color=0xf39c12
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            return await func(interaction, *args, **kwargs)
        wrapper.__name__ = func.__name__
        return wrapper
    return decorator


# ==================== HELPERS ====================

def create_embed(title, description, color=discord.Color.blue()):
    embed = discord.Embed(title=title, description=description, color=color, timestamp=datetime.now())
    embed.set_footer(text="Crypto Context Bot v3.0")
    return embed


def format_price(price: float, currency: str = "usd") -> str:
    sym = CURRENCY_SYMBOLS.get(currency.lower(), "$")
    if price is None:
        return "N/A"
    if price >= 1000:
        return f"{sym}{price:,.2f}"
    elif price >= 1:
        return f"{sym}{price:,.4f}"
    else:
        return f"{sym}{price:.8f}"


def format_number(num: float, currency: str = "usd") -> str:
    sym = CURRENCY_SYMBOLS.get(currency.lower(), "$")
    if num is None or num == 0:
        return "N/A"
    if num >= 1_000_000_000:
        return f"{sym}{num/1e9:.2f}B"
    elif num >= 1_000_000:
        return f"{sym}{num/1e6:.2f}M"
    elif num >= 1_000:
        return f"{sym}{num/1e3:.2f}K"
    return f"{sym}{num:.2f}"


def fmt_change(change: float) -> str:
    sign = "+" if change > 0 else ""
    return f"{sign}{change:.2f}%"


def change_color(change: float) -> discord.Color:
    if change > 5:
        return discord.Color.dark_green()
    elif change > 0:
        return discord.Color.green()
    elif change < -5:
        return discord.Color.dark_red()
    return discord.Color.red() if change < 0 else discord.Color.light_grey()


def get_user_currency(user_id: int) -> str:
    """Retourne la devise préférée de l'utilisateur."""
    return subscription_manager.get_preferred_currency(user_id)


# ==================== PRIX ====================

@bot.tree.command(name="price", description="Prix actuel d'une crypto")
@app_commands.describe(
    symbol="Symbole crypto (ex: BTC, ETH)",
    currency="Devise (ex: usd, eur, gbp) — laissez vide pour votre devise par défaut"
)
@with_cooldown
async def price(interaction: discord.Interaction, symbol: str, currency: str = None):
    await interaction.response.defer()
    symbol = symbol.upper()
    user_id = interaction.user.id
    subscription_manager.ensure_user(user_id, interaction.user.name)

    currency = currency.lower() if currency else get_user_currency(user_id)

    # Vérifier accès devise
    if not subscription_manager.can_use_currency(user_id, currency):
        tier_config = subscription_manager.get_tier_config(user_id)
        supported = ", ".join(f"`{c}`" for c in tier_config.currencies)
        await interaction.followup.send(
            f"❌ La devise `{currency}` n'est pas disponible dans votre plan.\n"
            f"Devises disponibles : {supported}\n👉 `/upgrade` pour plus de devises.",
            ephemeral=True
        )
        return

    is_priority = subscription_manager.check_feature(user_id, 'priority_cache')

    try:
        data = await fetcher.get_price(symbol, currency, priority=is_priority)
        if not data:
            await interaction.followup.send(f"❌ Impossible de trouver **{symbol}**", ephemeral=True)
            return

        change = data['change_24h']
        arrow = "📈" if change > 0 else "📉"

        embed = discord.Embed(
            title=f"{arrow} {data['name']} ({symbol})",
            color=change_color(change),
            timestamp=datetime.now()
        )
        if data.get('image'):
            embed.set_thumbnail(url=data['image'])

        cur = data['currency']
        embed.add_field(name=f"💰 Prix ({cur.upper()})", value=format_price(data['price'], cur), inline=True)
        embed.add_field(name="📊 24h", value=fmt_change(change), inline=True)
        embed.add_field(name="📈 Rang", value=f"#{data['rank']}" if data.get('rank') else "N/A", inline=True)

        embed.add_field(name="🔝 Haut 24h", value=format_price(data['high_24h'], cur), inline=True)
        embed.add_field(name="🔻 Bas 24h", value=format_price(data['low_24h'], cur), inline=True)
        embed.add_field(name="💹 Volume", value=format_number(data['volume_24h'], cur), inline=True)

        embed.add_field(name="🏦 Market Cap", value=format_number(data['market_cap'], cur), inline=True)
        if data.get('change_7d'):
            embed.add_field(name="📅 7j", value=fmt_change(data['change_7d']), inline=True)
        if data.get('ath') and data['ath'] > 0:
            embed.add_field(name="🏆 ATH", value=f"{format_price(data['ath'], cur)} ({fmt_change(data.get('ath_change_percentage', 0))})", inline=True)

        tier_name = TIER_CONFIGS.get(subscription_manager.get_tier(user_id), TIER_CONFIGS['free']).name
        embed.set_footer(text=f"Plan: {tier_name} • Devise: {cur.upper()} • /currency pour changer")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/price error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


@bot.tree.command(name="market", description="Vue d'ensemble du marché crypto mondial")
@app_commands.describe(currency="Devise (laissez vide pour votre devise par défaut)")
@with_cooldown
async def market(interaction: discord.Interaction, currency: str = None):
    await interaction.response.defer()
    user_id = interaction.user.id
    currency = currency.lower() if currency else get_user_currency(user_id)

    try:
        data, fg = await asyncio.gather(
            fetcher.get_market_overview(currency),
            fetcher.get_fear_greed_index()
        )

        if not data:
            await interaction.followup.send("❌ Impossible de récupérer les données.", ephemeral=True)
            return

        change = data.get('market_cap_change_24h', 0)
        embed = discord.Embed(title="🌍 Marché Crypto Global", color=change_color(change), timestamp=datetime.now())

        embed.add_field(name=f"💰 Market Cap ({currency.upper()})", value=format_number(data['total_market_cap'], currency), inline=True)
        embed.add_field(name="📊 Volume 24h", value=format_number(data['total_volume_24h'], currency), inline=True)
        embed.add_field(name=f"{'📈' if change >= 0 else '📉'} 24h", value=fmt_change(change), inline=True)

        embed.add_field(name="₿ BTC", value=f"{data['btc_dominance']:.1f}%", inline=True)
        embed.add_field(name="Ξ ETH", value=f"{data['eth_dominance']:.1f}%", inline=True)
        embed.add_field(name="🪙 Cryptos actives", value=f"{data['active_cryptos']:,}", inline=True)

        if fg:
            val = fg['value']
            emoji = "😱" if val < 25 else "😰" if val < 45 else "😐" if val < 55 else "😊" if val < 75 else "🤑"
            embed.add_field(name="Fear & Greed", value=f"{emoji} **{val}/100** — {fg['classification']}", inline=False)

        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/market error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


@bot.tree.command(name="top", description="Top gainers et losers 24h")
@app_commands.describe(currency="Devise (laissez vide pour votre devise par défaut)")
@with_cooldown
async def top(interaction: discord.Interaction, currency: str = None):
    await interaction.response.defer()
    user_id = interaction.user.id
    currency = currency.lower() if currency else get_user_currency(user_id)

    try:
        gainers, losers = await fetcher.get_top_gainers_losers(limit=5, currency=currency)
        embed = discord.Embed(title="📊 Top Performers (24h)", color=discord.Color.blue(), timestamp=datetime.now())

        def coin_line(c):
            return f"**{c['symbol']}** — {c['name']}\n{format_price(c['price'], currency)} | {fmt_change(c['change_24h'])}\n\n"

        embed.add_field(name="🏆 Top Gainers", value="".join(coin_line(c) for c in gainers) or "N/A", inline=False)
        embed.add_field(name="📉 Top Losers", value="".join(coin_line(c) for c in losers) or "N/A", inline=False)
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/top error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


# ==================== ANALYSE ====================

@bot.tree.command(name="analyze", description="Analyse technique (RSI, MACD, Bollinger, MA)")
@app_commands.describe(symbol="Symbole crypto (ex: BTC)")
async def analyze_command(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer()
    user_id = interaction.user.id
    symbol = symbol.upper()
    has_advanced = subscription_manager.check_feature(user_id, 'advanced_analysis')

    try:
        price_data, historical = await asyncio.gather(
            fetcher.get_price(symbol, get_user_currency(user_id)),
            fetcher.get_historical_data(symbol, days=30) if has_advanced else asyncio.sleep(0)
        )

        if not price_data:
            await interaction.followup.send(f"❌ Impossible de trouver **{symbol}**", ephemeral=True)
            return

        analysis = analyzer.analyze(symbol, price_data, historical if has_advanced else None)
        signal_colors = {
            'STRONG BUY': discord.Color.dark_green(), 'BUY': discord.Color.green(),
            'NEUTRAL': discord.Color.gold(), 'SELL': discord.Color.orange(),
            'STRONG SELL': discord.Color.red()
        }

        embed = discord.Embed(
            title=f"📊 Analyse — {price_data['name']} ({symbol})",
            color=signal_colors.get(analysis['signal'], discord.Color.blue()),
            timestamp=datetime.now()
        )
        if price_data.get('image'):
            embed.set_thumbnail(url=price_data['image'])

        embed.add_field(name="🎯 Signal", value=f"{analysis['signal_emoji']} **{analysis['signal']}**", inline=True)
        embed.add_field(name="📈 Score", value=f"{analysis['score']}/100", inline=True)
        embed.add_field(name="🔄 Tendance", value=analysis['trend'], inline=True)

        cur = get_user_currency(user_id)
        embed.add_field(name="💰 Prix", value=format_price(price_data['price'], cur), inline=True)
        embed.add_field(name="📊 24h", value=fmt_change(price_data['change_24h']), inline=True)
        embed.add_field(name="📍 Position 24h", value=f"{analysis['position_in_range']:.1f}%", inline=True)

        if has_advanced and analysis.get('has_history') and analysis.get('indicators'):
            indicators_text = analyzer.get_indicator_summary(analysis)
            if indicators_text:
                embed.add_field(name="⚙️ Indicateurs techniques", value=indicators_text, inline=False)

            signals = analysis.get('signals', [])
            bullish = [msg for s, msg in signals if s == 'bullish']
            bearish = [msg for s, msg in signals if s == 'bearish']
            if bullish:
                embed.add_field(name="✅ Signaux haussiers", value="\n".join(f"• {s}" for s in bullish[:4]), inline=False)
            if bearish:
                embed.add_field(name="❌ Signaux baissiers", value="\n".join(f"• {s}" for s in bearish[:4]), inline=False)
        elif not has_advanced:
            embed.add_field(
                name="🔒 Analyse avancée",
                value="RSI, MACD, Bollinger disponibles avec le plan **Pro**.\n👉 `/upgrade`",
                inline=False
            )

        embed.set_footer(text="⚠️ Pas de conseil financier. DYOR!")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/analyze error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


# ==================== COMPARE ====================

@bot.tree.command(name="compare", description="Comparer deux cryptos côte à côte")
@app_commands.describe(symbol1="Première crypto", symbol2="Deuxième crypto")
@with_cooldown
async def compare(interaction: discord.Interaction, symbol1: str, symbol2: str):
    await interaction.response.defer()
    s1, s2 = symbol1.upper(), symbol2.upper()
    cur = get_user_currency(interaction.user.id)

    try:
        d1, d2 = await asyncio.gather(
            fetcher.get_price(s1, cur),
            fetcher.get_price(s2, cur)
        )

        if not d1:
            await interaction.followup.send(f"❌ Impossible de trouver **{s1}**", ephemeral=True); return
        if not d2:
            await interaction.followup.send(f"❌ Impossible de trouver **{s2}**", ephemeral=True); return

        embed = discord.Embed(title=f"⚖️ {s1} vs {s2}", color=discord.Color.blue(), timestamp=datetime.now())

        def add_comparison(name, v1, v2, higher_is_better=True, fmt_fn=None):
            fmt = fmt_fn or str
            try:
                n1, n2 = float(v1), float(v2)
                win1 = "✅" if (n1 >= n2 if higher_is_better else n1 <= n2) else ""
                win2 = "✅" if (n2 > n1 if higher_is_better else n2 < n1) else ""
            except:
                win1 = win2 = ""
            embed.add_field(name=f"{name}\n{s1} {win1}", value=fmt(v1) if fmt_fn else str(v1), inline=True)
            embed.add_field(name=f"\u200b\n{s2} {win2}", value=fmt(v2) if fmt_fn else str(v2), inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)

        add_comparison(f"💰 Prix ({cur.upper()})", d1['price'], d2['price'], fmt_fn=lambda v: format_price(v, cur))
        add_comparison("📊 24h", d1['change_24h'], d2['change_24h'], fmt_fn=fmt_change)
        add_comparison("🏦 Market Cap", d1.get('market_cap', 0), d2.get('market_cap', 0), fmt_fn=lambda v: format_number(v, cur))
        add_comparison("💹 Volume 24h", d1.get('volume_24h', 0), d2.get('volume_24h', 0), fmt_fn=lambda v: format_number(v, cur))

        if d1.get('price') and d2.get('price') and d2['price'] > 0:
            ratio = d1['price'] / d2['price']
            embed.add_field(name="🔄 Ratio", value=f"1 {s1} = **{ratio:.4f}** {s2}", inline=False)

        embed.set_footer(text="✅ = meilleure valeur")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/compare error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


# ==================== PORTFOLIO ====================

@bot.tree.command(name="portfolio", description="Calculer la valeur de votre portefeuille")
@app_commands.describe(holdings="Format: BTC:0.5,ETH:2,SOL:10")
@with_cooldown
async def portfolio(interaction: discord.Interaction, holdings: str):
    await interaction.response.defer()
    user_id = interaction.user.id

    if not subscription_manager.check_feature(user_id, 'portfolio_access'):
        await interaction.followup.send(
            "🔒 La commande `/portfolio` est disponible à partir du plan **Basic**.\n👉 `/upgrade`",
            ephemeral=True
        )
        return

    cur = get_user_currency(user_id)

    try:
        items = []
        for part in holdings.split(','):
            part = part.strip()
            if ':' not in part:
                await interaction.followup.send(f"❌ Format invalide : `{part}`. Utilisez `BTC:0.5`", ephemeral=True); return
            sym, qty_str = part.split(':', 1)
            try:
                qty = float(qty_str)
                if qty <= 0:
                    raise ValueError
            except ValueError:
                await interaction.followup.send(f"❌ Quantité invalide pour `{sym}`.", ephemeral=True); return
            items.append((sym.strip().upper(), qty))

        if len(items) > 15:
            await interaction.followup.send("❌ Maximum 15 cryptos.", ephemeral=True); return

        symbols = [sym for sym, _ in items]
        prices_tasks = [fetcher.get_price(sym, cur) for sym in symbols]
        prices_list = await asyncio.gather(*prices_tasks, return_exceptions=True)
        prices_data = {sym: (p if not isinstance(p, Exception) else None) for sym, p in zip(symbols, prices_list)}

        embed = discord.Embed(title="💼 Valeur du Portefeuille", color=discord.Color.gold(), timestamp=datetime.now())

        total = 0.0
        lines = []
        not_found = []

        for sym, qty in items:
            pd = prices_data.get(sym)
            if pd and pd.get('price', 0) > 0:
                value = pd['price'] * qty
                total += value
                lines.append({'symbol': sym, 'qty': qty, 'price': pd['price'], 'value': value, 'change_24h': pd.get('change_24h', 0)})
            else:
                not_found.append(sym)

        if not lines:
            await interaction.followup.send("❌ Aucune crypto trouvée.", ephemeral=True); return

        lines.sort(key=lambda x: x['value'], reverse=True)

        for item in lines:
            pct = (item['value'] / total * 100) if total > 0 else 0
            emoji = "📈" if item['change_24h'] >= 0 else "📉"
            embed.add_field(
                name=f"{item['symbol']} ({pct:.1f}%)",
                value=f"**{item['qty']:g}** × {format_price(item['price'], cur)}\n= **{format_number(item['value'], cur)}** {emoji} {fmt_change(item['change_24h'])}",
                inline=True
            )

        embed.add_field(name="─────────────────", value=f"💰 **TOTAL : {format_number(total, cur)}**", inline=False)

        if lines:
            weighted_change = sum(l['change_24h'] * (l['value'] / total) for l in lines)
            yesterday = total / (1 + weighted_change / 100)
            pnl = total - yesterday
            embed.add_field(
                name="📊 Performance 24h",
                value=f"{'📈' if weighted_change >= 0 else '📉'} {fmt_change(weighted_change)} ({'+' if pnl >= 0 else ''}{format_number(abs(pnl), cur)})",
                inline=False
            )

        if not_found:
            embed.add_field(name="⚠️ Non trouvées", value=", ".join(not_found), inline=False)

        embed.set_footer(text=f"Valeurs en {cur.upper()} • ⚠️ Pas de conseil financier")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/portfolio error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


# ==================== CHART ====================

@bot.tree.command(name="chart", description="Graphique de prix historique")
@app_commands.describe(symbol="Symbole crypto", days="Période : 7, 14 ou 30 jours")
@app_commands.choices(days=[
    app_commands.Choice(name="7 jours", value=7),
    app_commands.Choice(name="14 jours", value=14),
    app_commands.Choice(name="30 jours", value=30),
])
@with_cooldown
async def chart(interaction: discord.Interaction, symbol: str, days: int = 14):
    await interaction.response.defer()
    user_id = interaction.user.id

    if not subscription_manager.check_feature(user_id, 'chart_access'):
        await interaction.followup.send("🔒 Les graphiques sont disponibles à partir du plan **Basic**.\n👉 `/upgrade`", ephemeral=True)
        return
    if not MATPLOTLIB_AVAILABLE:
        await interaction.followup.send("❌ `matplotlib` non installé.", ephemeral=True)
        return

    symbol = symbol.upper()
    cur = get_user_currency(user_id)

    try:
        historical, price_data = await asyncio.gather(
            fetcher.get_historical_data(symbol, days, cur),
            fetcher.get_price(symbol, cur)
        )

        if not historical or len(historical) < 5:
            await interaction.followup.send(f"❌ Données insuffisantes pour **{symbol}**", ephemeral=True); return

        name = price_data['name'] if price_data else symbol
        end_date = datetime.now()
        dates = [end_date - timedelta(days=len(historical) - 1 - i) for i in range(len(historical))]

        # Calcul MAs
        def sma(data, period):
            return [sum(data[max(0,i-period+1):i+1])/min(i+1,period) if i >= period-1 else None for i in range(len(data))]

        ma7 = sma(historical, 7)
        ma14 = sma(historical, 14)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7), gridspec_kw={'height_ratios': [3, 1]})
        for ax in (fig, ax1, ax2):
            (ax.patch if hasattr(ax, 'patch') else ax).set_facecolor('#1a1a2e')
        ax1.set_facecolor('#16213e')
        ax2.set_facecolor('#16213e')

        line_color = '#00ff88' if historical[-1] >= historical[0] else '#ff4444'
        ax1.fill_between(dates, historical, alpha=0.15, color=line_color)
        ax1.plot(dates, historical, color=line_color, linewidth=2, label=symbol)

        ma7_pts = [(d, v) for d, v in zip(dates, ma7) if v]
        ma14_pts = [(d, v) for d, v in zip(dates, ma14) if v]
        if ma7_pts:
            d7, v7 = zip(*ma7_pts); ax1.plot(d7, v7, '#ffd700', 1.2, '--', label='MA7', alpha=0.8)
        if ma14_pts:
            d14, v14 = zip(*ma14_pts); ax1.plot(d14, v14, '#ff9500', 1.2, '-.', label='MA14', alpha=0.8)

        if len(historical) >= 20:
            bb_u, bb_l = [], []
            for i in range(len(historical)):
                sub = historical[max(0, i-19):i+1]
                if len(sub) >= 20:
                    m = sum(sub)/20
                    std = (sum((x-m)**2 for x in sub)/20)**0.5
                    bb_u.append(m + 2*std)
                    bb_l.append(m - 2*std)
                else:
                    bb_u.append(None)
                    bb_l.append(None)

            bb_dates = [d for d, u in zip(dates, bb_u) if u]
            bu = [u for u in bb_u if u]
            bl = [l for l in bb_l if l]
            if bb_dates:
                ax1.fill_between(bb_dates, bl, bu, alpha=0.08, color='#8888ff', label='Bollinger')

        ax1.set_title(f"{name} ({symbol}) — {days}j ({cur.upper()})", color='white', fontsize=14, pad=10)
        ax1.tick_params(colors='#aaaaaa', labelsize=8)
        ax1.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
        for spine in ax1.spines.values(): spine.set_color('#333355')
        ax1.grid(color='#222244', linestyle='--', alpha=0.5)
        ax1.legend(loc='upper left', facecolor='#1a1a2e', labelcolor='white', fontsize=8)
        sym_label = CURRENCY_SYMBOLS.get(cur, "$")
        ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"{sym_label}{x:,.0f}" if x >= 1 else f"{sym_label}{x:.4f}"))

        changes = [((historical[i]-historical[i-1])/historical[i-1]*100) if i > 0 else 0 for i in range(len(historical))]
        colors = ['#00ff88' if c >= 0 else '#ff4444' for c in changes]
        ax2.bar(dates, changes, color=colors, alpha=0.7, width=0.8)
        ax2.axhline(0, color='#555577', linewidth=0.8)
        ax2.set_ylabel('Var %', color='#aaaaaa', fontsize=8)
        ax2.tick_params(colors='#aaaaaa', labelsize=7)
        ax2.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m'))
        for spine in ax2.spines.values(): spine.set_color('#333355')
        ax2.grid(color='#222244', linestyle='--', alpha=0.3, axis='y')

        plt.tight_layout(pad=1.5)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor='#1a1a2e', edgecolor='none')
        plt.close(fig)
        buf.seek(0)

        pct = ((historical[-1]-historical[0])/historical[0])*100
        embed = discord.Embed(
            title=f"📈 {name} ({symbol}) — {days} derniers jours",
            description=f"De {format_price(historical[0], cur)} à **{format_price(historical[-1], cur)}** ({fmt_change(pct)})",
            color=change_color(pct), timestamp=datetime.now()
        )
        embed.set_image(url="attachment://chart.png")
        embed.set_footer(text=f"Courbe + MA7 + MA14 + Bollinger • Devise: {cur.upper()}")

        await interaction.followup.send(embed=embed, file=discord.File(buf, "chart.png"))

    except Exception as e:
        logger.error(f"/chart error: {e}", exc_info=True)
        await interaction.followup.send("❌ Erreur lors de la génération du graphique.", ephemeral=True)


# ==================== WATCHLIST ====================

@bot.tree.command(name="watchlist", description="Voir votre watchlist de cryptos")
async def watchlist_view(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    cur = get_user_currency(user_id)
    subscription_manager.ensure_user(user_id, interaction.user.name)

    symbols = watchlist_manager.get_watchlist(user_id)
    tier_config = subscription_manager.get_tier_config(user_id)

    if not symbols:
        embed = create_embed(
            "👀 Votre Watchlist",
            f"Votre watchlist est vide.\nAjoutez des cryptos avec `/watchadd BTC`\nLimite: **{tier_config.watchlist_limit}** symboles ({tier_config.name})",
            discord.Color.blue()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    # Récupérer tous les prix en parallèle
    prices_tasks = [fetcher.get_price(sym, cur) for sym in symbols]
    prices_list = await asyncio.gather(*prices_tasks, return_exceptions=True)

    embed = discord.Embed(
        title="👀 Votre Watchlist",
        description=f"{len(symbols)}/{tier_config.watchlist_limit} cryptos • Devise: {cur.upper()}",
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )

    for sym, pd in zip(symbols, prices_list):
        if isinstance(pd, Exception) or not pd:
            embed.add_field(name=sym, value="❌ Données indisponibles", inline=True)
            continue

        change = pd.get('change_24h', 0)
        arrow = "📈" if change >= 0 else "📉"
        embed.add_field(
            name=f"{arrow} {sym}",
            value=f"**{format_price(pd['price'], cur)}**\n{fmt_change(change)}",
            inline=True
        )

    embed.set_footer(text="/watchadd <sym> • /watchremove <sym>")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="watchadd", description="Ajouter une crypto à votre watchlist")
@app_commands.describe(symbol="Symbole à ajouter (ex: BTC)")
async def watchlist_add(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    subscription_manager.ensure_user(user_id, interaction.user.name)

    tier_config = subscription_manager.get_tier_config(user_id)
    symbol = symbol.upper()

    # Valider que le symbole existe
    if not await fetcher.validate_symbol(symbol):
        await interaction.followup.send(f"❌ Symbole **{symbol}** introuvable.", ephemeral=True)
        return

    result = watchlist_manager.add_symbol(user_id, symbol, limit=tier_config.watchlist_limit)

    color = discord.Color.green() if result['success'] else discord.Color.orange()
    embed = create_embed("👀 Watchlist", result['reason'], color)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="watchremove", description="Retirer une crypto de votre watchlist")
@app_commands.describe(symbol="Symbole à retirer")
async def watchlist_remove(interaction: discord.Interaction, symbol: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    success = watchlist_manager.remove_symbol(user_id, symbol.upper())

    if success:
        embed = create_embed("✅ Watchlist", f"**{symbol.upper()}** retiré de votre watchlist.", discord.Color.green())
    else:
        embed = create_embed("❌ Watchlist", f"**{symbol.upper()}** n'est pas dans votre watchlist.", discord.Color.red())

    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================== DEVISE ====================

@bot.tree.command(name="currency", description="Définir votre devise par défaut")
@app_commands.describe(currency="Devise à utiliser (ex: eur, gbp, usd)")
async def currency_cmd(interaction: discord.Interaction, currency: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    currency = currency.lower()
    subscription_manager.ensure_user(user_id, interaction.user.name)

    if not subscription_manager.can_use_currency(user_id, currency):
        tier_config = subscription_manager.get_tier_config(user_id)
        supported = ", ".join(f"`{c}`" for c in tier_config.currencies)
        await interaction.followup.send(
            f"❌ La devise `{currency}` n'est pas disponible dans votre plan **{tier_config.name}**.\n"
            f"Devises disponibles : {supported}\n👉 `/upgrade` pour plus de devises.",
            ephemeral=True
        )
        return

    success = subscription_manager.set_preferred_currency(user_id, interaction.user.name, currency)
    if success:
        sym = CURRENCY_SYMBOLS.get(currency, currency.upper())
        embed = create_embed("✅ Devise mise à jour", f"Votre devise par défaut est maintenant **{currency.upper()}** ({sym})", discord.Color.green())
    else:
        embed = create_embed("❌ Erreur", "Impossible de mettre à jour la devise.", discord.Color.red())

    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================== ALERTES ====================

@bot.tree.command(name="alert", description="Créer une alerte de prix")
@app_commands.describe(symbol="Symbole crypto", price="Prix cible", channel="Canal de notification (optionnel)")
async def alert_cmd(interaction: discord.Interaction, symbol: str, price: float, channel: discord.TextChannel = None):
    await interaction.response.defer()
    user_id = interaction.user.id
    username = interaction.user.name
    symbol = symbol.upper()
    subscription_manager.ensure_user(user_id, username)

    tier_config = subscription_manager.get_tier_config(user_id)
    count = alert_manager.count_user_alerts(user_id)

    if count >= tier_config.alerts_limit:
        embed = create_embed(
            "⚠️ Limite d'alertes atteinte",
            f"Limite de **{tier_config.alerts_limit}** alertes pour votre plan **{tier_config.name}**.\n"
            f"👉 `/upgrade` pour augmenter cette limite",
            discord.Color.orange()
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    cur = get_user_currency(user_id)
    price_data = await fetcher.get_price(symbol, cur)
    if not price_data:
        await interaction.followup.send(f"❌ Impossible de trouver **{symbol}**", ephemeral=True)
        return

    current_price = price_data['price']
    channel_id = channel.id if channel else None
    alert_obj = alert_manager.add_alert(user_id, username, symbol, price, current_price, channel_id)

    if not alert_obj:
        await interaction.followup.send("❌ Erreur lors de la création.", ephemeral=True)
        return

    direction = "atteint ou dépasse" if alert_obj.alert_type == "above" else "tombe à ou en dessous de"
    arrow = "📈" if alert_obj.alert_type == "above" else "📉"
    pct_diff = abs((price - current_price) / current_price * 100)

    embed = discord.Embed(title="🔔 Alerte créée !", color=discord.Color.green())
    embed.add_field(name="💰 Prix actuel", value=format_price(current_price, cur), inline=True)
    embed.add_field(name=f"🎯 Cible {arrow}", value=format_price(price, cur), inline=True)
    embed.add_field(name="📏 Distance", value=f"{pct_diff:.1f}%", inline=True)
    embed.add_field(name="📬 Notification", value=channel.mention if channel else "DM privé", inline=True)
    embed.add_field(name="📊 Alertes", value=f"{count+1}/{tier_config.alerts_limit}", inline=True)
    embed.set_footer(text=f"ID: {alert_obj.alert_id}")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="myalerts", description="Voir vos alertes actives")
async def myalerts(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    cur = get_user_currency(user_id)
    alerts = alert_manager.get_user_alerts(user_id)

    if not alerts:
        embed = create_embed("🔔 Vos alertes", "Aucune alerte active.\n`/alert BTC 50000`", discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    tier_config = subscription_manager.get_tier_config(user_id)
    embed = discord.Embed(
        title="🔔 Vos alertes actives",
        description=f"{len(alerts)}/{tier_config.alerts_limit} alertes",
        color=discord.Color.blue(), timestamp=datetime.now()
    )

    symbols = list(set(a.symbol for a in alerts))
    prices_list = await asyncio.gather(*[fetcher.get_price(s, cur) for s in symbols], return_exceptions=True)
    prices_map = {sym: (p if not isinstance(p, Exception) else None) for sym, p in zip(symbols, prices_list)}

    for a in alerts:
        arrow = "📈" if a.alert_type == "above" else "📉"
        pd = prices_map.get(a.symbol)
        current = pd.get('price') if pd else None
        lines = [f"Cible: **{format_price(a.target_price, cur)}** ({'≥' if a.alert_type == 'above' else '≤'})"]
        if current:
            pct = (a.target_price - current) / current * 100
            lines.append(f"Actuel: {format_price(current, cur)} ({pct:+.1f}%)")
        lines.append(f"ID: `{a.alert_id}`")
        embed.add_field(name=f"{arrow} {a.symbol}", value="\n".join(lines), inline=True)

    embed.set_footer(text="/removealert <ID> pour supprimer")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="removealert", description="Supprimer une alerte")
@app_commands.describe(alert_id="ID de l'alerte")
async def removealert(interaction: discord.Interaction, alert_id: str):
    await interaction.response.defer(ephemeral=True)
    try:
        aid = int(alert_id)
    except ValueError:
        await interaction.followup.send("❌ ID invalide.", ephemeral=True); return

    success = alert_manager.remove_alert(interaction.user.id, aid)
    color = discord.Color.green() if success else discord.Color.red()
    msg = f"Alerte `{alert_id}` supprimée." if success else f"Alerte `{alert_id}` introuvable."
    await interaction.followup.send(embed=create_embed("🔔 Alertes", msg, color), ephemeral=True)


@bot.tree.command(name="alerthistory", description="Historique de vos alertes déclenchées")
async def alerthistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    cur = get_user_currency(interaction.user.id)
    history = alert_manager.get_user_history(interaction.user.id, limit=10)

    if not history:
        embed = create_embed("📜 Historique", "Aucune alerte déclenchée.", discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True); return

    embed = discord.Embed(title="📜 Historique des alertes", color=discord.Color.blue(), timestamp=datetime.now())
    for h in history:
        arrow = "📈" if h['alert_type'] == 'above' else "📉"
        date = h.get('triggered_at', '')[:10]
        embed.add_field(
            name=f"{arrow} {h['symbol']} — {date}",
            value=f"Cible: {format_price(h['target_price'], cur)} | Déclenché: {format_price(h['triggered_price'], cur)}",
            inline=False
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================== DIGEST ====================

@bot.tree.command(name="setdigest", description="Configurer le résumé quotidien automatique (Pro/Premium)")
@app_commands.describe(
    channel="Canal où envoyer le résumé",
    hour="Heure d'envoi (0-23, UTC)"
)
async def setdigest(interaction: discord.Interaction, channel: discord.TextChannel, hour: int = 8):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("❌ Vous devez avoir la permission **Gérer le serveur**.", ephemeral=True)
        return

    user_id = interaction.user.id
    if not subscription_manager.check_feature(user_id, 'daily_digest'):
        await interaction.followup.send(
            "🔒 Le digest quotidien est disponible à partir du plan **Pro**.\n👉 `/upgrade`",
            ephemeral=True
        )
        return

    if not 0 <= hour <= 23:
        await interaction.followup.send("❌ L'heure doit être entre 0 et 23 (UTC).", ephemeral=True)
        return

    guild = interaction.guild
    tier = subscription_manager.get_tier(user_id)
    success = subscription_manager.set_guild_digest(guild.id, guild.name, channel.id, hour=hour, tier=tier)

    if success:
        embed = discord.Embed(
            title="✅ Digest configuré !",
            description=f"Le résumé quotidien sera envoyé dans {channel.mention} à **{hour:02d}:00 UTC**.",
            color=discord.Color.green()
        )
        embed.add_field(name="💡 Test", value="Utilisez `/testdigest` pour tester maintenant", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send("❌ Erreur lors de la configuration.", ephemeral=True)


@bot.tree.command(name="testdigest", description="Envoyer un digest de test immédiatement")
async def testdigest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("❌ Permission insuffisante.", ephemeral=True)
        return

    guild_sub = subscription_manager.get_guild_sub(interaction.guild.id)
    if not guild_sub or not guild_sub.get('digest_channel_id'):
        await interaction.followup.send("❌ Aucun digest configuré. Utilisez `/setdigest` d'abord.", ephemeral=True)
        return

    channel = bot.get_channel(guild_sub['digest_channel_id'])
    if not channel:
        await interaction.followup.send("❌ Canal introuvable.", ephemeral=True)
        return

    success = await scheduler.send_test_digest(channel)
    if success:
        await interaction.followup.send(f"✅ Digest de test envoyé dans {channel.mention} !", ephemeral=True)
    else:
        await interaction.followup.send("❌ Erreur lors de l'envoi.", ephemeral=True)


@bot.tree.command(name="stopdigest", description="Désactiver le digest quotidien")
async def stopdigest(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("❌ Permission insuffisante.", ephemeral=True)
        return

    success = subscription_manager.remove_guild_digest(interaction.guild.id)
    msg = "Digest quotidien désactivé." if success else "Aucun digest à désactiver."
    await interaction.followup.send(embed=create_embed("📰 Digest", msg, discord.Color.orange()), ephemeral=True)


# ==================== ABONNEMENTS ====================

@bot.tree.command(name="plan", description="Voir votre plan actuel et ses limites")
async def plan_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    subscription_manager.ensure_user(user_id, interaction.user.name)
    sub = subscription_manager.get_user_sub(user_id)
    tier = sub.get('tier', 'free')
    config = TIER_CONFIGS[tier]

    embed = discord.Embed(
        title=f"📋 Votre plan : {config.name}",
        color=config.color,
        timestamp=datetime.now()
    )

    def check(val): return "✅" if val else "❌"

    embed.add_field(
        name="📊 Limites",
        value=(
            f"🔔 Alertes: **{config.alerts_limit}**\n"
            f"👀 Watchlist: **{config.watchlist_limit}** cryptos\n"
            f"💱 Devises: {', '.join(f'`{c}`' for c in config.currencies)}"
        ),
        inline=True
    )
    embed.add_field(
        name="🔑 Fonctionnalités",
        value=(
            f"{check(config.chart_access)} Graphiques\n"
            f"{check(config.portfolio_access)} Portfolio\n"
            f"{check(config.advanced_analysis)} Analyse avancée\n"
            f"{check(config.daily_digest)} Digest quotidien\n"
            f"{check(config.priority_cache)} Cache prioritaire"
        ),
        inline=True
    )

    if sub.get('expires_at'):
        expires = datetime.fromisoformat(sub['expires_at'])
        remaining = (expires - datetime.now()).days
        embed.add_field(name="⏰ Expiration", value=f"Dans **{remaining}** jours ({expires.strftime('%d/%m/%Y')})", inline=False)

    if tier == 'free':
        embed.add_field(name="⬆️ Upgrade", value="👉 `/upgrade` pour voir les plans payants", inline=False)

    embed.set_footer(text=f"User ID: {user_id}")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="upgrade", description="Voir les plans d'abonnement et obtenir votre lien de paiement")
async def upgrade_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id      = interaction.user.id
    username     = interaction.user.name
    current_tier = subscription_manager.get_tier(user_id)
    subscription_manager.ensure_user(user_id, username)

    embed = discord.Embed(
        title="⬆️ Plans d'abonnement",
        description=(
            "Vos liens sont **personnalisés** — votre compte Discord est reconnu "
            "automatiquement après le paiement. Aucune action manuelle requise."
        ),
        color=discord.Color.gold(),
        timestamp=datetime.now()
    )

    def ls_link(tier_key: str) -> str:
        """Génère le lien Paddle avec les custom_data Discord pré-remplies."""
        import urllib.parse
        tier_links = {
            'basic':   os.getenv('PADDLE_LINK_BASIC',   PADDLE_STORE_URL),
            'pro':     os.getenv('PADDLE_LINK_PRO',     PADDLE_STORE_URL),
            'premium': os.getenv('PADDLE_LINK_PREMIUM', PADDLE_STORE_URL),
        }
        base = tier_links.get(tier_key, PADDLE_STORE_URL)
        # Paddle Payment Links acceptent checkout[custom][...] en query string
        params = urllib.parse.urlencode({
            'passthrough': f'{user_id}:{username}:{tier_key}'
        })
        return f"{base}?{params}"

    for tier_key, config in TIER_CONFIGS.items():
        if tier_key == 'free':
            continue

        is_current = (tier_key == current_tier)
        badge = " ← **Plan actuel**" if is_current else ""

        features = []
        if config.chart_access:      features.append("📈 Graphiques")
        if config.portfolio_access:  features.append("💼 Portfolio")
        if config.advanced_analysis: features.append("🔬 Analyse avancée (RSI, MACD…)")
        if config.daily_digest:      features.append("📰 Digest quotidien")
        if config.priority_cache:    features.append("⚡ Données en temps quasi-réel")

        if is_current:
            sub = subscription_manager.get_user_sub(user_id)
            if sub.get('expires_at'):
                from datetime import datetime as dt
                expires   = dt.fromisoformat(sub['expires_at'])
                remaining = (expires - dt.now()).days
                badge += f"\n⏰ Expire dans **{remaining}** jours"
            cta = "✅ Votre plan actuel"
        else:
            link = ls_link(tier_key)
            cta  = f"[💳 S'abonner — {config.price_monthly:.2f}€/mois]({link})"

        embed.add_field(
            name=f"{config.name}{badge}",
            value=(
                f"**{config.price_monthly:.2f}€/mois**\n"
                f"🔔 {config.alerts_limit} alertes | 👀 {config.watchlist_limit} watchlist\n"
                f"💱 {', '.join(config.currencies[:3])}{'…' if len(config.currencies) > 3 else ''}\n"
                + "\n".join(f"  {f}" for f in features) + f"\n\n{cta}"
            ),
            inline=False
        )

    embed.add_field(
        name="ℹ️ Comment ça marche ?",
        value=(
            "1. Cliquez sur le lien du plan voulu\n"
            "2. Payez par carte, PayPal ou autre méthode locale\n"
            "3. Votre plan Discord est activé **automatiquement** en quelques secondes\n"
            "4. Vous recevez une confirmation en DM\n\n"
            "🔒 Paiement 100% sécurisé par Paddle • Annulation à tout moment"
        ),
        inline=False
    )

    if MANUAL_MODE:
        embed.description = (
            "✋ **Activation manuelle** — Contactez-nous pour souscrire.\n"
            "Votre plan est activé manuellement après confirmation du paiement."
        )
        embed.add_field(
            name="📩 Comment souscrire ?",
            value=(
                f"1. Choisissez votre plan ci-dessus\n"
                f"2. Contactez-nous :\n"
                f"   • Discord : **{CONTACT_DISCORD}**\n"
                f"   • Email : **{CONTACT_EMAIL}**\n"
                f"3. Envoyez le paiement via PayPal\n"
                f"4. Votre plan est activé sous **24h**\n\n"
                f"💬 Réponse garantie sous 24h"
            ),
            inline=False
        )
    embed.set_footer(text="Questions ? /feedback • Activation sous 24h en mode manuel")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="subscribe", description="Souscrire à un plan CryptoContextBot")
@app_commands.describe(plan="Le plan auquel vous souhaitez souscrire")
@app_commands.choices(plan=[
    app_commands.Choice(name="⭐ Basic — 4.99€/mois",   value="basic"),
    app_commands.Choice(name="💎 Pro — 14.99€/mois",    value="pro"),
    app_commands.Choice(name="👑 Premium — 29.99€/mois", value="premium"),
])
async def subscribe_cmd(interaction: discord.Interaction, plan: str):
    await interaction.response.defer(ephemeral=True)
    user_id  = interaction.user.id
    username = interaction.user.name
    subscription_manager.ensure_user(user_id, username)

    current_tier = subscription_manager.get_tier(user_id)
    config       = TIER_CONFIGS.get(plan, TIER_CONFIGS['basic'])

    if current_tier == plan:
        sub = subscription_manager.get_user_sub(user_id)
        expires_str = ""
        if sub.get('expires_at'):
            from datetime import datetime as dt
            expires   = dt.fromisoformat(sub['expires_at'])
            remaining = (expires - dt.now()).days
            expires_str = f"\n⏰ Expire dans **{remaining}** jours."
        await interaction.followup.send(
            f"✅ Vous êtes déjà sur le plan **{config.name}**.{expires_str}\n"
            f"Pour changer de plan, utilisez `/upgrade`.",
            ephemeral=True
        )
        return

    # Paddle : les custom_data se passent en query string
    import urllib.parse
    tier_links = {
        'basic':   os.getenv('PADDLE_LINK_BASIC',   PADDLE_STORE_URL),
        'pro':     os.getenv('PADDLE_LINK_PRO',     PADDLE_STORE_URL),
        'premium': os.getenv('PADDLE_LINK_PREMIUM', PADDLE_STORE_URL),
    }
    base_link   = tier_links.get(plan, PADDLE_STORE_URL)
    params      = urllib.parse.urlencode({
        'passthrough': f'{user_id}:{username}:{plan}'
    })
    payment_url = f"{base_link}?{params}"

    embed = discord.Embed(
        title=f"💳 Votre lien de paiement — {config.name}",
        description=(
            f"Ce lien est **personnel** et pré-configuré pour votre compte Discord.\n"
            f"Après le paiement, votre plan est activé automatiquement.\n\n"
            f"[🔗 Accéder au paiement sécurisé]({payment_url})"
        ),
        color=config.color,
        timestamp=datetime.now()
    )
    embed.add_field(
        name="📋 Ce que vous obtenez",
        value=(
            f"🔔 **{config.alerts_limit}** alertes de prix\n"
            f"👀 **{config.watchlist_limit}** cryptos en watchlist\n"
            f"💱 {len(config.currencies)} devises : {', '.join(config.currencies)}\n"
            + ("📈 Graphiques historiques\n"         if config.chart_access      else "")
            + ("🔬 Analyse RSI, MACD, Bollinger\n"  if config.advanced_analysis else "")
            + ("📰 Digest quotidien automatique\n"   if config.daily_digest      else "")
        ),
        inline=True
    )
    embed.add_field(
        name="💰 Tarif",
        value=f"**{config.price_monthly:.2f}€/mois**\nAnnulation à tout moment",
        inline=True
    )
    if MANUAL_MODE:
        # Mode manuel : remplacer le lien de paiement par les infos de contact
        embed.description = (
            f"Plan sélectionné : **{TIER_CONFIGS.get(plan, TIER_CONFIGS['basic']).name}** "
            f"— **{TIER_CONFIGS.get(plan, TIER_CONFIGS['basic']).price_monthly:.2f}€/mois**\n\n"
            f"Contactez-nous pour finaliser votre abonnement :"
        )
        embed.add_field(
            name="📩 Étapes",
            value=(
                f"1️⃣ Contactez-nous sur Discord : **{CONTACT_DISCORD}**\n"
                f"   ou par email : **{CONTACT_EMAIL}**\n\n"
                f"2️⃣ Indiquez : votre pseudo Discord + le plan choisi\n\n"
                f"3️⃣ Envoyez le paiement via PayPal\n"
                + (f"   [💳 Lien PayPal]({PAYPAL_LINK})\n" if PAYPAL_LINK else "   (lien PayPal fourni sur demande)\n")
                + f"\n4️⃣ Votre plan est activé sous **24h** ✅"
            ),
            inline=False
        )
        embed.add_field(
            name="ℹ️ Info",
            value="Paiement sécurisé via PayPal • Annulation à tout moment",
            inline=False
        )
    else:
        embed.add_field(
            name="🔒 Sécurité",
            value="Paiement traité par **Paddle**\nDisponible depuis tous les pays\nNous ne stockons aucune donnée bancaire",
            inline=False
        )
    embed.set_footer(text="CryptoContextBot • Réponse sous 24h")
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="cancelsubscription", description="Annuler votre abonnement (accès conservé jusqu'à expiration)")
async def cancel_subscription(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    sub     = subscription_manager.get_user_sub(user_id)
    tier    = sub.get('tier', 'free')

    if tier == 'free':
        await interaction.followup.send("ℹ️ Vous êtes sur le plan gratuit, rien à annuler.", ephemeral=True)
        return

    config     = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
    portal_url = os.getenv('PADDLE_CUSTOMER_PORTAL', PADDLE_STORE_URL)

    embed = discord.Embed(
        title="❌ Annuler votre abonnement",
        description=(
            f"Gérez ou annulez votre abonnement **{config.name}** depuis le portail Paddle :\n\n"
            f"[🔗 Accéder au portail]({portal_url})\n\n"
            f"**Après annulation :**\n"
            f"• Votre accès {config.name} reste actif jusqu'à la fin de la période payée\n"
            f"• Vous passez ensuite automatiquement en plan Free\n"
            f"• Vos données (watchlist, alertes) sont conservées"
        ),
        color=discord.Color.orange()
    )
    if sub.get('expires_at'):
        from datetime import datetime as dt
        expires   = dt.fromisoformat(sub['expires_at'])
        remaining = (expires - dt.now()).days
        embed.add_field(
            name="⏰ Accès actuel",
            value=f"Expire le **{expires.strftime('%d/%m/%Y')}** (dans {remaining} jours)",
            inline=False
        )
    embed.set_footer(text="Besoin d'aide ? Utilisez /feedback")
    await interaction.followup.send(embed=embed, ephemeral=True)




# ==================== REFERRAL ====================

@bot.tree.command(name="referral", description="Votre lien de parrainage — gagnez 1 mois gratuit par filleul")
async def referral_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id  = interaction.user.id
    username = interaction.user.name
    subscription_manager.ensure_user(user_id, username)

    code  = referral_manager.get_or_create_code(user_id, username)
    stats = referral_manager.get_referral_stats(user_id)
    link  = referral_manager.build_referral_link(user_id, username, tier='basic')

    embed = discord.Embed(
        title="🎁 Votre programme de parrainage",
        description=(
            f"Partagez votre lien — pour chaque ami qui s'abonne, "
            f"vous gagnez **1 mois gratuit** !"
        ),
        color=discord.Color.green(),
        timestamp=datetime.now()
    )
    embed.add_field(name="🔑 Votre code", value=f"`{code}`", inline=True)
    embed.add_field(name="👥 Filleuls", value=f"**{stats['total_referrals']}**", inline=True)
    embed.add_field(name="🎉 Mois gagnés", value=f"**{stats['months_earned']}**", inline=True)
    embed.add_field(
        name="🔗 Votre lien de parrainage",
        value=f"[Partager ce lien]({link})\n*(pré-configuré pour le plan Basic)*",
        inline=False
    )
    embed.add_field(
        name="ℹ️ Comment ça marche",
        value=(
            "1. Partagez votre lien ou code\n"
            "2. Votre filleul s'abonne via votre lien\n"
            "3. Vous recevez **1 mois gratuit** automatiquement\n"
            "4. Votre filleul reçoit **20% de réduction** sur son premier mois"
        ),
        inline=False
    )
    embed.set_footer(text="Aucune limite de parrainages !")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ==================== RGPD ====================

@bot.tree.command(name="deletemydata", description="Supprimer toutes vos données (RGPD)")
async def deletemydata(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    embed = discord.Embed(
        title="⚠️ Suppression de vos données",
        description=(
            "Cette action est **irréversible** et supprimera :\n"
            "• Votre abonnement et préférences\n"
            "• Toutes vos alertes actives\n"
            "• Votre watchlist\n"
            "• Votre historique de paiement\n\n"
            "Répondez avec `/confirmdeletion` pour confirmer."
        ),
        color=discord.Color.red()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="confirmdeletion", description="Confirmer la suppression de toutes vos données")
async def confirmdeletion(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id

    # Supprimer dans tous les managers
    sub_ok = subscription_manager.delete_user_data(user_id)
    wl_ok = watchlist_manager.delete_user_data(user_id)
    # Supprimer les alertes
    alerts = alert_manager.get_user_alerts(user_id)
    for a in alerts:
        alert_manager.remove_alert(user_id, a.alert_id)

    embed = create_embed(
        "✅ Données supprimées",
        "Toutes vos données ont été supprimées conformément au RGPD.\nMerci d'avoir utilisé CryptoBot.",
        discord.Color.green()
    )
    await interaction.followup.send(embed=embed, ephemeral=True)




# ==================== FEEDBACK ====================

@bot.tree.command(name="feedback", description="Envoyer un retour ou signaler un bug")
@app_commands.describe(
    category="Type de feedback",
    message="Votre message (max 500 caractères)"
)
@app_commands.choices(category=[
    app_commands.Choice(name="💡 Suggestion", value="suggestion"),
    app_commands.Choice(name="🐛 Bug", value="bug"),
    app_commands.Choice(name="⭐ Avis positif", value="positive"),
    app_commands.Choice(name="❓ Question", value="question"),
])
async def feedback_cmd(interaction: discord.Interaction, category: str, message: str):
    await interaction.response.defer(ephemeral=True)

    if len(message) > 500:
        await interaction.followup.send("❌ Message trop long (max 500 caractères).", ephemeral=True)
        return

    feedback_channel_id = int(os.getenv('FEEDBACK_CHANNEL_ID', 0))
    if not feedback_channel_id:
        await interaction.followup.send(
            "⚠️ Le système de feedback n'est pas encore configuré. Merci quand même !",
            ephemeral=True
        )
        return

    channel = bot.get_channel(feedback_channel_id)
    if not channel:
        await interaction.followup.send("⚠️ Canal de feedback introuvable.", ephemeral=True)
        return

    icons = {"suggestion": "💡", "bug": "🐛", "positive": "⭐", "question": "❓"}
    user_id = interaction.user.id
    tier    = subscription_manager.get_tier(user_id)
    config  = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])

    embed = discord.Embed(
        title=f"{icons.get(category, '📝')} Feedback — {category.title()}",
        description=message,
        color=discord.Color.blue(),
        timestamp=datetime.now()
    )
    embed.add_field(name="👤 Utilisateur", value=f"{interaction.user.name} (`{user_id}`)", inline=True)
    embed.add_field(name="📋 Plan", value=config.name, inline=True)
    if interaction.guild:
        embed.add_field(name="🌐 Serveur", value=interaction.guild.name, inline=True)
    embed.set_footer(text=f"Discord ID: {user_id}")

    try:
        await channel.send(embed=embed)
        await interaction.followup.send(
            "✅ Merci pour votre retour ! Nous l'avons bien reçu et l'examinerons prochainement.",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"Failed to send feedback: {e}")
        await interaction.followup.send("❌ Erreur lors de l'envoi. Réessayez plus tard.", ephemeral=True)


# ==================== CONVERT ====================

@bot.tree.command(name="convert", description="Convertir une crypto dans une autre devise ou crypto")
@app_commands.describe(
    amount="Montant à convertir",
    from_symbol="Crypto ou devise source (ex: BTC, ETH)",
    to_symbol="Crypto ou devise cible (ex: EUR, USD, ETH)"
)
@with_cooldown
async def convert_cmd(interaction: discord.Interaction, amount: float, from_symbol: str, to_symbol: str):
    await interaction.response.defer()

    from_symbol = from_symbol.upper()
    to_symbol   = to_symbol.upper()
    user_id     = interaction.user.id

    FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'CHF', 'CAD', 'AUD'}

    try:
        # Cas 1 : crypto → fiat
        if to_symbol in FIAT_CURRENCIES:
            cur  = to_symbol.lower()
            data = await fetcher.get_price(from_symbol, cur)
            if not data:
                await interaction.followup.send(f"❌ Crypto **{from_symbol}** introuvable.", ephemeral=True)
                return
            result = amount * data['price']
            sym_out = CURRENCY_SYMBOLS.get(cur, cur)
            embed = discord.Embed(
                title="💱 Conversion",
                description=f"**{amount:g} {from_symbol}** = **{sym_out}{result:,.2f} {to_symbol}**",
                color=discord.Color.blue(), timestamp=datetime.now()
            )
            embed.add_field(name="Taux", value=f"1 {from_symbol} = {format_price(data['price'], cur)}", inline=True)
            embed.add_field(name="24h", value=fmt_change(data['change_24h']), inline=True)

        # Cas 2 : fiat → crypto
        elif from_symbol in FIAT_CURRENCIES:
            cur  = from_symbol.lower()
            data = await fetcher.get_price(to_symbol, cur)
            if not data:
                await interaction.followup.send(f"❌ Crypto **{to_symbol}** introuvable.", ephemeral=True)
                return
            if data['price'] == 0:
                await interaction.followup.send("❌ Prix invalide.", ephemeral=True)
                return
            result = amount / data['price']
            embed = discord.Embed(
                title="💱 Conversion",
                description=f"**{CURRENCY_SYMBOLS.get(cur,'')}{amount:g} {from_symbol}** = **{result:.8f} {to_symbol}**",
                color=discord.Color.blue(), timestamp=datetime.now()
            )
            embed.add_field(name="Taux", value=f"1 {to_symbol} = {format_price(data['price'], cur)}", inline=True)

        # Cas 3 : crypto → crypto (via USD)
        else:
            data_from, data_to = await asyncio.gather(
                fetcher.get_price(from_symbol, 'usd'),
                fetcher.get_price(to_symbol, 'usd')
            )
            if not data_from:
                await interaction.followup.send(f"❌ **{from_symbol}** introuvable.", ephemeral=True); return
            if not data_to:
                await interaction.followup.send(f"❌ **{to_symbol}** introuvable.", ephemeral=True); return
            if data_to['price'] == 0:
                await interaction.followup.send("❌ Prix invalide.", ephemeral=True); return

            usd_value = amount * data_from['price']
            result    = usd_value / data_to['price']
            embed = discord.Embed(
                title="💱 Conversion",
                description=f"**{amount:g} {from_symbol}** = **{result:.8f} {to_symbol}**",
                color=discord.Color.blue(), timestamp=datetime.now()
            )
            embed.add_field(name=f"{from_symbol} en USD", value=format_price(data_from['price'], 'usd'), inline=True)
            embed.add_field(name=f"{to_symbol} en USD", value=format_price(data_to['price'], 'usd'), inline=True)
            embed.add_field(name="Valeur USD", value=format_price(usd_value, 'usd'), inline=True)

        embed.set_footer(text="⚠️ Valeur indicative — cours en temps réel sur les exchanges")
        await interaction.followup.send(embed=embed)

    except Exception as e:
        logger.error(f"/convert error: {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)

# ==================== MONITORING ALERTES ====================

async def _send_with_retry(coro_fn, max_retries: int = 4, label: str = "message"):
    """
    Exécute une coroutine d'envoi Discord avec retry exponentiel sur rate-limit (429).
    Gère aussi les erreurs serveur Discord (5xx).
    """
    for attempt in range(max_retries):
        try:
            await coro_fn()
            return True
        except discord.HTTPException as e:
            if e.status == 429:
                # Rate-limit : Discord nous dit combien attendre
                retry_after = getattr(e, 'retry_after', 2 ** attempt) or 2 ** attempt
                logger.warning(
                    f"Rate-limited sending {label} "
                    f"(attempt {attempt + 1}/{max_retries}). "
                    f"Retry in {retry_after:.1f}s"
                )
                await asyncio.sleep(retry_after)
            elif e.status == 403:
                logger.info(f"Forbidden sending {label} (DMs closed or missing perms)")
                return False
            elif e.status >= 500:
                wait = 2 ** attempt
                logger.warning(f"Discord server error {e.status} on {label}. Retry in {wait}s")
                await asyncio.sleep(wait)
            else:
                logger.error(f"Discord HTTP error {e.status} on {label}: {e.text}")
                return False
        except discord.Forbidden:
            logger.info(f"Forbidden sending {label} (DMs closed)")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending {label}: {e}")
            return False
    logger.error(f"Failed to send {label} after {max_retries} attempts")
    return False


async def _notify_alert(alert_obj, embed: discord.Embed) -> bool:
    """
    Envoie la notification d'alerte à l'utilisateur.
    Priorité : canal configuré → DM → canal public du premier serveur commun.
    """
    # 1. Canal configuré par l'utilisateur
    if alert_obj.channel_id:
        channel = bot.get_channel(alert_obj.channel_id)
        if channel:
            sent = await _send_with_retry(
                lambda: channel.send(f"<@{alert_obj.user_id}>", embed=embed),
                label=f"alert to channel {alert_obj.channel_id}"
            )
            if sent:
                return True
            logger.warning(f"Canal {alert_obj.channel_id} inaccessible, fallback DM")

    # 2. DM privé
    try:
        user = await bot.fetch_user(alert_obj.user_id)
        sent = await _send_with_retry(
            lambda: user.send(embed=embed),
            label=f"DM to user {alert_obj.user_id}"
        )
        if sent:
            return True
    except discord.NotFound:
        logger.warning(f"User {alert_obj.user_id} not found")

    # 3. Fallback : canal public du premier serveur commun avec le bot
    for guild in bot.guilds:
        member = guild.get_member(alert_obj.user_id)
        if not member:
            continue
        # Chercher le premier canal où le bot peut écrire
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if perms.send_messages and perms.embed_links:
                sent = await _send_with_retry(
                    lambda c=channel: c.send(
                        f"<@{alert_obj.user_id}> *(DM indisponible)*",
                        embed=embed
                    ),
                    label=f"fallback channel {channel.id}"
                )
                if sent:
                    logger.info(f"Alert for {alert_obj.user_id} sent via fallback channel {channel.id}")
                    return True
                break

    logger.error(f"Could not deliver alert {alert_obj.alert_id} to user {alert_obj.user_id} by any means")
    return False


@tasks.loop(seconds=60)
async def monitor_alerts_loop():
    try:
        active_alerts = alert_manager.get_all_active_alerts()
        if not active_alerts:
            return

        symbols = list(set(a.symbol for a in active_alerts))
        prices_data = await fetcher.get_simple_prices(symbols)

        triggered = []
        for alert_obj in active_alerts:
            pd = prices_data.get(alert_obj.symbol)
            if not pd:
                continue
            current_price = pd['price']
            if alert_manager.check_alert(alert_obj, current_price):
                triggered.append((alert_obj, current_price))

        if not triggered:
            return

        logger.info(f"{len(triggered)} alert(s) triggered this cycle")

        for alert_obj, current_price in triggered:
            arrow = "📈" if alert_obj.alert_type == "above" else "📉"
            cur = subscription_manager.get_preferred_currency(alert_obj.user_id)

            embed = discord.Embed(
                title="🔔 Alerte déclenchée !",
                description=f"**{alert_obj.symbol}** a atteint votre prix cible !",
                color=discord.Color.gold(), timestamp=datetime.now()
            )
            embed.add_field(name="🎯 Prix cible", value=f"{arrow} {format_price(alert_obj.target_price, cur)}", inline=True)
            embed.add_field(name="💰 Prix actuel", value=format_price(current_price, cur), inline=True)
            diff = ((current_price - alert_obj.target_price) / alert_obj.target_price) * 100
            embed.add_field(name="📏 Écart", value=fmt_change(diff), inline=True)
            embed.set_footer(text=f"ID: {alert_obj.alert_id}")

            await _notify_alert(alert_obj, embed)
            alert_manager.mark_triggered(alert_obj, current_price)

            # Petit délai entre chaque notification pour éviter le rate-limit global
            await asyncio.sleep(0.5)

    except Exception as e:
        logger.error(f"monitor_alerts error: {e}", exc_info=True)


@monitor_alerts_loop.before_loop
async def before_monitor():
    await bot.wait_until_ready()




@tasks.loop(hours=24)
async def expiry_reminder_loop():
    """
    Vérifie chaque jour les abonnements qui vont expirer et envoie des rappels
    à 7 jours, 3 jours et 1 jour avant la date d'expiration.
    """
    try:
        thresholds = [7, 3, 1]  # jours avant expiration

        with __import__('sqlite3').connect('data/subscriptions.db') as conn:
            conn.row_factory = __import__('sqlite3').Row
            rows = conn.execute(
                """SELECT user_id, username, tier, expires_at
                   FROM subscriptions
                   WHERE tier != 'free' AND expires_at IS NOT NULL"""
            ).fetchall()

        now = datetime.now()
        for row in rows:
            try:
                expires = datetime.fromisoformat(row['expires_at'])
                remaining_days = (expires - now).days

                if remaining_days not in thresholds:
                    continue

                user_id  = row['user_id']
                tier     = row['tier']
                config   = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
                portal   = os.getenv('PADDLE_CUSTOMER_PORTAL', '')

                if remaining_days == 1:
                    urgency = "🚨 Dernier jour !"
                    color   = discord.Color.red()
                elif remaining_days == 3:
                    urgency = "⚠️ Plus que 3 jours"
                    color   = discord.Color.orange()
                else:
                    urgency = "📅 Rappel d'expiration"
                    color   = discord.Color.yellow()

                embed = discord.Embed(
                    title=f"⏰ {urgency} — Plan {config.name}",
                    description=(
                        f"Votre abonnement **{config.name}** expire dans **{remaining_days} jour(s)** "
                        f"({expires.strftime('%d/%m/%Y')}).\n\n"
                        f"Renouvelez maintenant pour garder accès à toutes vos fonctionnalités."
                    ),
                    color=color,
                    timestamp=datetime.now()
                )
                if portal:
                    embed.add_field(
                        name="🔄 Renouveler",
                        value=f"[Gérer mon abonnement]({portal})",
                        inline=False
                    )
                embed.add_field(
                    name="⚠️ Après expiration",
                    value="Retour automatique au plan **Free** (alertes, watchlist et données conservées)",
                    inline=False
                )
                embed.set_footer(text="Crypto Context Bot • Désactivez ces rappels avec /notifications")

                try:
                    user = await bot.fetch_user(user_id)
                    await _send_with_retry(
                        lambda u=user, e=embed: u.send(embed=e),
                        label=f"expiry reminder to {user_id}"
                    )
                    logger.info(f"Expiry reminder sent to {row['username']} ({user_id}) — {remaining_days}d remaining")
                except discord.NotFound:
                    logger.warning(f"User {user_id} not found for expiry reminder")
                except Exception as e:
                    logger.error(f"Error sending expiry reminder to {user_id}: {e}")

                await asyncio.sleep(1)  # Éviter le rate-limit

            except Exception as e:
                logger.error(f"Error processing expiry for user {row['user_id']}: {e}")

    except Exception as e:
        logger.error(f"expiry_reminder_loop error: {e}", exc_info=True)


@expiry_reminder_loop.before_loop
async def before_expiry_reminder():
    await bot.wait_until_ready()

@tasks.loop(hours=1)
async def cleanup_loop():
    await fetcher.cache.clear_expired()
    alert_manager.clean_old_alerts(days=30)
    # Backup automatique toutes les heures
    try:
        result = await run_backup()
        logger.info(f"Auto-backup: {len(result['files'])} files, {result['size_mb']} MB")
    except Exception as e:
        logger.error(f"Auto-backup failed: {e}")
        health_monitor.record_error()
    logger.info("Cleanup done")


@cleanup_loop.before_loop
async def before_cleanup():
    await bot.wait_until_ready()




# ==================== LANGUE ====================

@bot.tree.command(name="language", description="Changer la langue du bot (fr / en / es)")
@app_commands.describe(lang="Langue souhaitée")
@app_commands.choices(lang=[
    app_commands.Choice(name="🇫🇷 Français", value="fr"),
    app_commands.Choice(name="🇬🇧 English",  value="en"),
    app_commands.Choice(name="🇪🇸 Español",  value="es"),
])
async def language_cmd(interaction: discord.Interaction, lang: str):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    subscription_manager.ensure_user(user_id, interaction.user.name)

    # Stocker la langue dans la colonne preferred_language (migration 4)
    try:
        import sqlite3
        with sqlite3.connect('data/subscriptions.db') as conn:
            conn.execute(
                "UPDATE subscriptions SET preferred_language = ? WHERE user_id = ?",
                (lang, user_id)
            )
            conn.commit()
        flags = {'fr': '🇫🇷', 'en': '🇬🇧', 'es': '🇪🇸'}
        await interaction.followup.send(
            f"{flags.get(lang, '🌍')} Langue définie sur **{lang.upper()}**. "
            f"Les prochaines réponses seront en {lang}.",
            ephemeral=True
        )
    except Exception as e:
        logger.error(f"/language error: {e}")
        await interaction.followup.send("❌ Erreur lors du changement de langue.", ephemeral=True)


# ==================== ADMIN BACKUP ====================

@bot.tree.command(name="adminbackup", description="[Admin] Lancer un backup des bases de données")
async def admin_backup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        await interaction.followup.send("❌ Réservé au propriétaire du bot.", ephemeral=True)
        return

    await interaction.followup.send("⏳ Backup en cours...", ephemeral=True)
    result = await run_backup()

    embed = discord.Embed(
        title="💾 Backup terminé",
        color=discord.Color.green() if result['files'] else discord.Color.red(),
        timestamp=datetime.now()
    )
    embed.add_field(name="📁 Fichiers", value=str(len(result['files'])), inline=True)
    embed.add_field(name="📦 Taille", value=f"{result['size_mb']} MB", inline=True)
    embed.add_field(name="☁️ S3", value="✅" if result.get('s3') else "⏭️ Skipped", inline=True)
    embed.add_field(name="🕐 Timestamp", value=result.get('timestamp', 'N/A'), inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="adminbackuplist", description="[Admin] Lister les backups disponibles")
async def admin_backup_list(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        await interaction.followup.send("❌ Réservé au propriétaire du bot.", ephemeral=True)
        return

    backups = list_backups()
    if not backups:
        await interaction.followup.send("Aucun backup local disponible.", ephemeral=True)
        return

    embed = discord.Embed(title="💾 Backups disponibles", color=discord.Color.blue())
    for b in backups[:10]:
        embed.add_field(
            name=b['timestamp'],
            value=f"{', '.join(b['files'])} • {b['size_mb']} MB",
            inline=False
        )
    await interaction.followup.send(embed=embed, ephemeral=True)

# ==================== ADMIN ====================

@bot.tree.command(name="adminsetplan", description="[Admin] Définir le plan d'un utilisateur manuellement")
@app_commands.describe(user="Utilisateur Discord", tier="Plan à attribuer", months="Durée en mois")
@app_commands.choices(tier=[
    app_commands.Choice(name="Free", value="free"),
    app_commands.Choice(name="Basic", value="basic"),
    app_commands.Choice(name="Pro", value="pro"),
    app_commands.Choice(name="Premium", value="premium"),
])
async def admin_set_plan(interaction: discord.Interaction, user: discord.User, tier: str, months: int = 1):
    await interaction.response.defer(ephemeral=True)

    # Vérifier que l'appelant est admin (propriétaire du bot ou admin serveur)
    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        await interaction.followup.send("❌ Commande réservée au propriétaire du bot.", ephemeral=True)
        return

    success = subscription_manager.admin_set_tier(user.id, user.name, tier, months)
    if success:
        config = TIER_CONFIGS[tier]
        embed = create_embed(
            "✅ Plan mis à jour",
            f"**{user.name}** → {config.name} pour {months} mois",
            discord.Color.green()
        )
    else:
        embed = create_embed("❌ Erreur", "Impossible de mettre à jour le plan.", discord.Color.red())

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="adminstats", description="[Admin] Statistiques globales du bot")
async def admin_stats(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    app_info = await bot.application_info()
    if interaction.user.id != app_info.owner.id:
        await interaction.followup.send("❌ Commande réservée au propriétaire du bot.", ephemeral=True)
        return

    sub_stats = subscription_manager.get_stats()
    alert_stats = alert_manager.get_stats()

    embed = discord.Embed(title="📊 Statistiques Globales", color=discord.Color.blue(), timestamp=datetime.now())

    embed.add_field(
        name="👥 Utilisateurs",
        value=(
            f"Total: **{sub_stats.get('total_users', 0)}**\n"
            f"Free: {sub_stats.get('free', 0)} | Basic: {sub_stats.get('basic', 0)}\n"
            f"Pro: {sub_stats.get('pro', 0)} | Premium: {sub_stats.get('premium', 0)}"
        ),
        inline=True
    )
    embed.add_field(
        name="💰 Revenus",
        value=f"Total: **{sub_stats.get('total_revenue', 0):.2f}€**",
        inline=True
    )
    embed.add_field(
        name="🔔 Alertes",
        value=(
            f"Actives: **{alert_stats.get('active_alerts', 0)}**\n"
            f"Déclenchées: {alert_stats.get('total_triggered', 0)}"
        ),
        inline=True
    )
    embed.add_field(name="🌐 Serveurs", value=f"**{len(bot.guilds)}**", inline=True)

    # Métriques de santé
    health = health_monitor.get_stats()
    embed.add_field(
        name="🏥 Santé du bot",
        value=(
            f"Latence: **{health['latency_ms']}ms**\n"
            f"Uptime: **{health['uptime']}**\n"
            f"Erreurs/1h: **{health['errors_1h']}**"
        ),
        inline=True
    )

    # Derniers backups
    backups = list_backups()
    last_backup = backups[0]['timestamp'] if backups else "Aucun"
    embed.add_field(name="💾 Dernier backup", value=last_backup, inline=True)

    await interaction.followup.send(embed=embed, ephemeral=True)


# ==================== UTILITAIRES ====================

@bot.tree.command(name="help", description="Toutes les commandes disponibles")
async def help_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    tier    = subscription_manager.get_tier(user_id)
    config  = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
    is_admin = False
    try:
        app_info = await bot.application_info()
        is_admin = interaction.user.id == app_info.owner.id
    except Exception:
        pass

    embed = discord.Embed(
        title="📚 Crypto Context Bot — Commandes",
        description=(
            f"Plan actuel : **{config.name}** • `/upgrade` pour changer\n"
            f"Toutes les commandes sont en `/slash`. Utilisez `/` dans Discord pour les autocompleter."
        ),
        color=config.color
    )

    # ── Marché ──────────────────────────────────────────────────────
    embed.add_field(
        name="📊 Marché & Prix",
        value=(
            "`/price <symbol>` — Prix temps réel + variations\n"
            "`/market <symbol>` — Données complètes (cap, volume, rank…)\n"
            "`/top` — Top 10 cryptos du moment\n"
            "`/compare <s1> <s2>` — Comparaison côte à côte\n"
            "`/convert <amount> <from> <to>` — Convertisseur crypto/fiat"
        ),
        inline=False
    )

    # ── Analyse ──────────────────────────────────────────────────────
    embed.add_field(
        name="📈 Analyse Technique",
        value=(
            "`/analyze <symbol>` — RSI, MACD, Bollinger, signal 🔒Pro\n"
            "`/chart <symbol>` — Graphique historique 🔒Basic"
        ),
        inline=False
    )

    # ── Portfolio ────────────────────────────────────────────────────
    embed.add_field(
        name="💼 Portfolio",
        value="`/portfolio <holdings>` — Valeur de votre portefeuille (ex: `BTC:0.5,ETH:2`) 🔒Basic",
        inline=False
    )

    # ── Watchlist ────────────────────────────────────────────────────
    embed.add_field(
        name="👀 Watchlist",
        value=(
            "`/watchlist` — Voir vos cryptos suivies avec cours en direct\n"
            "`/watchadd <symbol>` — Ajouter une crypto\n"
            "`/watchremove <symbol>` — Retirer une crypto"
        ),
        inline=False
    )

    # ── Alertes ──────────────────────────────────────────────────────
    embed.add_field(
        name="🔔 Alertes de Prix",
        value=(
            "`/alert <symbol> <prix> <above|below>` — Créer une alerte\n"
            "`/myalerts` — Voir vos alertes actives\n"
            "`/removealert <id>` — Supprimer une alerte\n"
            "`/alerthistory` — Historique des alertes déclenchées"
        ),
        inline=False
    )

    # ── Digest ───────────────────────────────────────────────────────
    embed.add_field(
        name="📰 Digest Quotidien",
        value=(
            "`/setdigest <canal> <heure>` — Configurer le résumé quotidien 🔒Pro\n"
            "`/testdigest` — Envoyer un digest de test\n"
            "`/stopdigest` — Désactiver le digest"
        ),
        inline=False
    )

    # ── Compte & Abonnement ──────────────────────────────────────────
    embed.add_field(
        name="⚙️ Compte & Abonnement",
        value=(
            "`/plan` — Voir votre plan actuel et ses limites\n"
            "`/upgrade` — Voir tous les plans avec leurs fonctionnalités\n"
            "`/subscribe <plan>` — Obtenir votre lien de paiement personnalisé\n"
            "`/cancelsubscription` — Annuler via le portail Stripe\n"
            "`/currency <devise>` — Changer votre devise par défaut\n"
            "`/language <lang>` — Changer la langue (fr/en/es)\n"
            "`/referral` — Votre code parrainage (1 mois gratuit/filleul)\n"
            "`/feedback <type> <message>` — Envoyer un retour ou signaler un bug"
        ),
        inline=False
    )

    # ── RGPD ─────────────────────────────────────────────────────────
    embed.add_field(
        name="🔒 Confidentialité (RGPD)",
        value=(
            "`/deletemydata` — Voir ce qui sera supprimé\n"
            "`/confirmdeletion` — Supprimer toutes vos données"
        ),
        inline=False
    )

    # ── Admin (visible uniquement pour le owner) ─────────────────────
    if is_admin:
        embed.add_field(
            name="🛠️ Administration (owner only)",
            value=(
                "`/adminsetplan <user> <tier> <mois>` — Définir un plan manuellement\n"
                "`/adminstats` — Statistiques globales + santé du bot\n"
                "`/adminbackup` — Lancer un backup immédiat\n"
                "`/adminbackuplist` — Lister les backups disponibles"
            ),
            inline=False
        )

    # ── Légende ──────────────────────────────────────────────────────
    embed.add_field(
        name="ℹ️ Légende & Infos",
        value=(
            "`/ping` — Latence du bot\n"
            "`/about` — Stack technique et statistiques\n"
            "`/help` — Cette aide\n\n"
            "🔒 = Fonctionnalité réservée à un plan payant"
        ),
        inline=False
    )

    embed.set_footer(text="Crypto Context Bot v4.0 • Paiements via Paddle • /upgrade pour débloquer")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="about", description="À propos du bot")
async def about(interaction: discord.Interaction):
    sub_stats = subscription_manager.get_stats()
    alert_stats = alert_manager.get_stats()

    embed = discord.Embed(title="🤖 Crypto Context Bot v3.0", color=discord.Color.green())
    embed.add_field(
        name="🚀 Stack technique",
        value="• aiohttp (async natif)\n• SQLite (alertes + abonnements)\n• Paddle (paiements)\n• CoinGecko API",
        inline=True
    )
    embed.add_field(
        name="📈 Stats",
        value=f"• {len(bot.guilds)} serveurs\n• {sub_stats.get('total_users', 0)} utilisateurs\n• {alert_stats.get('active_alerts', 0)} alertes actives",
        inline=True
    )
    embed.set_footer(text="v3.0 — Multi-devises | Watchlist | Digest | Monétisation")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="ping", description="Latence du bot")
async def ping(interaction: discord.Interaction):
    latency = round(bot.latency * 1000)
    color = discord.Color.green() if latency < 100 else discord.Color.orange() if latency < 200 else discord.Color.red()
    await interaction.response.send_message(embed=create_embed("🏓 Pong!", f"Latence: **{latency}ms**", color))


# ==================== MAIN ====================

def validate_env() -> bool:
    """
    Valide les variables d'environnement au démarrage.
    Plante proprement avec un message clair si une variable critique est manquante.
    """
    errors = []
    warnings = []

    # ── Variables obligatoires ──────────────────────────────────────
    if not os.getenv('DISCORD_BOT_TOKEN'):
        errors.append("DISCORD_BOT_TOKEN manquant — le bot ne peut pas démarrer sans token Discord")

    # ── Variables Paddle — warnings si absentes (bot fonctionne sans) ──
    if not os.getenv('PADDLE_WEBHOOK_SECRET'):
        warnings.append("PADDLE_WEBHOOK_SECRET absent — les webhooks Paddle ne seront pas vérifiés")
    if not os.getenv('PADDLE_LINK_BASIC') or os.getenv('PADDLE_LINK_BASIC', '').startswith('https://buy.paddle.com/buy/votre'):
        warnings.append("PADDLE_LINK_BASIC non configuré — /subscribe basic affichera un lien placeholder")
    if not os.getenv('PADDLE_LINK_PRO') or os.getenv('PADDLE_LINK_PRO', '').startswith('https://buy.paddle.com/buy/votre'):
        warnings.append("PADDLE_LINK_PRO non configuré — /subscribe pro affichera un lien placeholder")
    if not os.getenv('PADDLE_LINK_PREMIUM') or os.getenv('PADDLE_LINK_PREMIUM', '').startswith('https://buy.paddle.com/buy/votre'):
        warnings.append("PADDLE_LINK_PREMIUM non configuré — /subscribe premium affichera un lien placeholder")

    # ── Affichage ───────────────────────────────────────────────────
    if warnings:
        logger.warning("⚠️  Configuration incomplète (non bloquant) :")
        for w in warnings:
            logger.warning(f"   • {w}")

    if errors:
        logger.error("❌ Variables d'environnement manquantes (bloquant) :")
        for e in errors:
            logger.error(f"   • {e}")
        logger.error("")
        logger.error("   👉 Copiez .env.example vers .env et remplissez les valeurs requises")
        return False

    logger.info("✅ Configuration validée")
    return True


def main():
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        def log_message(self, format, *args):
            pass

    def run_health_server():
        port = int(os.getenv('PORT', 8080))
        server = HTTPServer(('0.0.0.0', port), HealthHandler)
        server.serve_forever()

    threading.Thread(target=run_health_server, daemon=True).start()

    run_migrations()  # Toujours avant tout accès DB
    if not validate_env():
        raise SystemExit(1)
    logger.info("Démarrage Crypto Context Bot v3.0...")
    try:
        bot.run(os.getenv('DISCORD_BOT_TOKEN'))
    except Exception as e:
        logger.error(f"Erreur fatale: {e}", exc_info=True)


if __name__ == '__main__':
    main()
