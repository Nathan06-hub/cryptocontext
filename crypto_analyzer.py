"""
Crypto Analyzer - Analyse technique complète
RSI, Moyennes Mobiles (MA7/14/30), MACD, Bollinger Bands
"""

import logging
import math
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TechnicalIndicators:
    """Calcul des indicateurs techniques."""

    @staticmethod
    def sma(prices: List[float], period: int) -> Optional[float]:
        """Simple Moving Average."""
        if len(prices) < period:
            return None
        return sum(prices[-period:]) / period

    @staticmethod
    def ema(prices: List[float], period: int) -> Optional[float]:
        """Exponential Moving Average."""
        if len(prices) < period:
            return None
        k = 2 / (period + 1)
        ema_val = sum(prices[:period]) / period
        for price in prices[period:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val

    @staticmethod
    def ema_series(prices: List[float], period: int) -> List[float]:
        """Série complète d'EMA pour le MACD."""
        if len(prices) < period:
            return []
        k = 2 / (period + 1)
        ema_val = sum(prices[:period]) / period
        result = [ema_val]
        for price in prices[period:]:
            ema_val = price * k + ema_val * (1 - k)
            result.append(ema_val)
        return result

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> Optional[float]:
        """
        Relative Strength Index.
        > 70 = surachat (overbought)
        < 30 = survente (oversold)
        """
        if len(prices) < period + 1:
            return None

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 2)

    @staticmethod
    def macd(prices: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[Dict]:
        """
        MACD (Moving Average Convergence Divergence).
        Retourne : macd_line, signal_line, histogram
        """
        if len(prices) < slow + signal:
            return None

        ema_fast = TechnicalIndicators.ema_series(prices, fast)
        ema_slow = TechnicalIndicators.ema_series(prices, slow)

        # Aligner les deux séries (ema_slow est plus courte)
        diff = len(ema_fast) - len(ema_slow)
        ema_fast_aligned = ema_fast[diff:]

        macd_line = [f - s for f, s in zip(ema_fast_aligned, ema_slow)]

        if len(macd_line) < signal:
            return None

        k = 2 / (signal + 1)
        signal_val = sum(macd_line[:signal]) / signal
        for val in macd_line[signal:]:
            signal_val = val * k + signal_val * (1 - k)

        macd_val = macd_line[-1]
        histogram = macd_val - signal_val

        return {
            'macd': round(macd_val, 6),
            'signal': round(signal_val, 6),
            'histogram': round(histogram, 6),
            'bullish_cross': macd_val > signal_val,
        }

    @staticmethod
    def bollinger_bands(prices: List[float], period: int = 20, num_std: float = 2.0) -> Optional[Dict]:
        """
        Bollinger Bands.
        Retourne upper, middle (SMA), lower et %B (position dans les bandes)
        """
        if len(prices) < period:
            return None

        recent = prices[-period:]
        middle = sum(recent) / period
        variance = sum((p - middle) ** 2 for p in recent) / period
        std = math.sqrt(variance)

        upper = middle + num_std * std
        lower = middle - num_std * std
        current = prices[-1]

        # %B : 0 = bande basse, 1 = bande haute
        if upper != lower:
            percent_b = (current - lower) / (upper - lower)
        else:
            percent_b = 0.5

        # Bandwidth : mesure de la volatilité
        bandwidth = (upper - lower) / middle * 100 if middle != 0 else 0

        return {
            'upper': round(upper, 6),
            'middle': round(middle, 6),
            'lower': round(lower, 6),
            'percent_b': round(percent_b, 4),
            'bandwidth': round(bandwidth, 2),
            'above_upper': current > upper,
            'below_lower': current < lower,
        }

    @staticmethod
    def stochastic_rsi(prices: List[float], rsi_period: int = 14, stoch_period: int = 14) -> Optional[Dict]:
        """
        Stochastic RSI - sensibilité accrue du RSI classique.
        > 80 = surachat, < 20 = survente
        """
        if len(prices) < rsi_period + stoch_period + 1:
            return None

        # Calculer série de RSI
        rsi_series = []
        for i in range(rsi_period, len(prices) + 1):
            rsi_val = TechnicalIndicators.rsi(prices[:i], rsi_period)
            if rsi_val is not None:
                rsi_series.append(rsi_val)

        if len(rsi_series) < stoch_period:
            return None

        recent_rsi = rsi_series[-stoch_period:]
        min_rsi = min(recent_rsi)
        max_rsi = max(recent_rsi)

        if max_rsi == min_rsi:
            stoch_rsi = 50.0
        else:
            stoch_rsi = (rsi_series[-1] - min_rsi) / (max_rsi - min_rsi) * 100

        return {
            'value': round(stoch_rsi, 2),
            'overbought': stoch_rsi > 80,
            'oversold': stoch_rsi < 20,
        }


class CryptoAnalyzer:
    """Analyse technique complète d'une crypto."""

    def analyze(self, symbol: str, price_data: Dict, historical_prices: Optional[List[float]] = None) -> Dict:
        """
        Analyse complète combinant indicateurs techniques.

        Args:
            symbol: Symbole crypto
            price_data: Données de prix actuelles
            historical_prices: Liste de prix historiques (optionnel, enrichit l'analyse)

        Returns:
            Dict complet avec tous les indicateurs et un signal global
        """
        try:
            price = price_data.get('price', 0)
            change_24h = price_data.get('change_24h', 0)
            high_24h = price_data.get('high_24h', 0)
            low_24h = price_data.get('low_24h', 0)

            result = {
                'symbol': symbol,
                'price': price,
                'has_history': False,
                'indicators': {},
                'signals': [],
                'signal': 'NEUTRAL',
                'signal_emoji': '🟡',
                'score': 50,
                'trend': 'NEUTRAL',
                'position_in_range': 50.0,
                'summary': '',
            }

            # Position dans le range 24h
            if high_24h > 0 and low_24h > 0 and high_24h != low_24h:
                position = ((price - low_24h) / (high_24h - low_24h)) * 100
                result['position_in_range'] = round(position, 1)
            else:
                position = 50.0

            # ---- Analyse de base (toujours disponible) ----
            base_score = 50
            base_signals = []

            # Signal change 24h
            if change_24h > 8:
                base_score += 20
                base_signals.append(('bullish', f'Forte hausse 24h: +{change_24h:.1f}%'))
            elif change_24h > 3:
                base_score += 10
                base_signals.append(('bullish', f'Hausse 24h: +{change_24h:.1f}%'))
            elif change_24h < -8:
                base_score -= 20
                base_signals.append(('bearish', f'Forte baisse 24h: {change_24h:.1f}%'))
            elif change_24h < -3:
                base_score -= 10
                base_signals.append(('bearish', f'Baisse 24h: {change_24h:.1f}%'))

            # Position dans range 24h
            if position > 80:
                base_score += 10
                base_signals.append(('bullish', f'Prix proche du plus haut 24h ({position:.0f}%)'))
            elif position < 20:
                base_score -= 10
                base_signals.append(('bearish', f'Prix proche du plus bas 24h ({position:.0f}%)'))

            # Tendance hebdomadaire si disponible
            change_7d = price_data.get('change_7d', 0)
            if change_7d:
                if change_7d > 10:
                    base_score += 8
                    base_signals.append(('bullish', f'Tendance haussière 7j: +{change_7d:.1f}%'))
                elif change_7d < -10:
                    base_score -= 8
                    base_signals.append(('bearish', f'Tendance baissière 7j: {change_7d:.1f}%'))

            result['score'] = base_score
            result['signals'] = base_signals

            # ---- Analyse technique avancée (si historique disponible) ----
            if historical_prices and len(historical_prices) >= 15:
                result['has_history'] = True
                indicators = {}
                adv_score = base_score
                adv_signals = list(base_signals)

                # --- Moyennes Mobiles ---
                ma7 = TechnicalIndicators.sma(historical_prices, 7)
                ma14 = TechnicalIndicators.sma(historical_prices, 14)
                ma30 = TechnicalIndicators.sma(historical_prices, min(30, len(historical_prices)))
                ema12 = TechnicalIndicators.ema(historical_prices, min(12, len(historical_prices)))
                ema26 = TechnicalIndicators.ema(historical_prices, min(26, len(historical_prices)))

                indicators['ma7'] = round(ma7, 6) if ma7 else None
                indicators['ma14'] = round(ma14, 6) if ma14 else None
                indicators['ma30'] = round(ma30, 6) if ma30 else None

                if ma7 and ma14:
                    if price > ma7 > ma14:
                        adv_score += 12
                        adv_signals.append(('bullish', f'Prix > MA7 ({self._fmt_price(ma7)}) > MA14 ({self._fmt_price(ma14)}) ✅'))
                    elif price < ma7 < ma14:
                        adv_score -= 12
                        adv_signals.append(('bearish', f'Prix < MA7 ({self._fmt_price(ma7)}) < MA14 ({self._fmt_price(ma14)}) ❌'))
                    elif ma7 > ma14:
                        adv_score += 6
                        adv_signals.append(('bullish', f'MA7 > MA14 (golden trend)'))
                    else:
                        adv_score -= 6
                        adv_signals.append(('bearish', f'MA7 < MA14 (death trend)'))

                if ma30:
                    if price > ma30:
                        adv_score += 8
                        adv_signals.append(('bullish', f'Prix au-dessus de la MA30 ({self._fmt_price(ma30)})'))
                    else:
                        adv_score -= 8
                        adv_signals.append(('bearish', f'Prix sous la MA30 ({self._fmt_price(ma30)})'))

                # --- RSI ---
                rsi_val = TechnicalIndicators.rsi(historical_prices, min(14, len(historical_prices) - 1))
                if rsi_val is not None:
                    indicators['rsi'] = rsi_val
                    if rsi_val >= 70:
                        adv_score -= 15
                        adv_signals.append(('bearish', f'RSI surachat ({rsi_val:.0f} ≥ 70) — attention retournement'))
                    elif rsi_val >= 60:
                        adv_score -= 5
                        adv_signals.append(('neutral', f'RSI haussier ({rsi_val:.0f})'))
                    elif rsi_val <= 30:
                        adv_score += 15
                        adv_signals.append(('bullish', f'RSI survente ({rsi_val:.0f} ≤ 30) — rebond possible'))
                    elif rsi_val <= 40:
                        adv_score += 5
                        adv_signals.append(('neutral', f'RSI baissier ({rsi_val:.0f})'))
                    else:
                        adv_signals.append(('neutral', f'RSI neutre ({rsi_val:.0f})'))

                # --- MACD ---
                macd_data = TechnicalIndicators.macd(historical_prices)
                if macd_data:
                    indicators['macd'] = macd_data
                    if macd_data['bullish_cross']:
                        adv_score += 10
                        adv_signals.append(('bullish', f'MACD haussier (hist: {macd_data["histogram"]:+.4f})'))
                    else:
                        adv_score -= 10
                        adv_signals.append(('bearish', f'MACD baissier (hist: {macd_data["histogram"]:+.4f})'))

                # --- Bollinger Bands ---
                bb_data = TechnicalIndicators.bollinger_bands(historical_prices)
                if bb_data:
                    indicators['bollinger'] = bb_data
                    pct_b = bb_data['percent_b']

                    if bb_data['below_lower']:
                        adv_score += 12
                        adv_signals.append(('bullish', f'Prix sous la bande basse Bollinger (%B={pct_b:.2f}) — rebond attendu'))
                    elif bb_data['above_upper']:
                        adv_score -= 12
                        adv_signals.append(('bearish', f'Prix au-dessus bande haute Bollinger (%B={pct_b:.2f}) — surachat'))
                    elif pct_b > 0.7:
                        adv_score -= 5
                        adv_signals.append(('neutral', f'Prix proche bande haute Bollinger (%B={pct_b:.2f})'))
                    elif pct_b < 0.3:
                        adv_score += 5
                        adv_signals.append(('neutral', f'Prix proche bande basse Bollinger (%B={pct_b:.2f})'))

                    # Squeeze : faible volatilité = explosion à venir
                    if bb_data['bandwidth'] < 5:
                        adv_signals.append(('neutral', f'Bollinger Squeeze ({bb_data["bandwidth"]:.1f}%) — mouvement fort imminent'))

                result['indicators'] = indicators
                result['score'] = max(0, min(100, adv_score))
                result['signals'] = adv_signals

            # ---- Signal global ----
            score = result['score']
            if score >= 75:
                result['signal'] = 'STRONG BUY'
                result['signal_emoji'] = '🟢'
                result['trend'] = 'BULLISH'
            elif score >= 60:
                result['signal'] = 'BUY'
                result['signal_emoji'] = '🟢'
                result['trend'] = 'BULLISH'
            elif score >= 45:
                result['signal'] = 'NEUTRAL'
                result['signal_emoji'] = '🟡'
                result['trend'] = 'NEUTRAL'
            elif score >= 30:
                result['signal'] = 'SELL'
                result['signal_emoji'] = '🔴'
                result['trend'] = 'BEARISH'
            else:
                result['signal'] = 'STRONG SELL'
                result['signal_emoji'] = '🔴'
                result['trend'] = 'BEARISH'

            # Résumé textuel
            bullish_count = sum(1 for s, _ in result['signals'] if s == 'bullish')
            bearish_count = sum(1 for s, _ in result['signals'] if s == 'bearish')
            result['summary'] = f"{bullish_count} signal(s) haussier(s), {bearish_count} signal(s) baissier(s)"

            return result

        except Exception as e:
            logger.error(f"Error analyzing {symbol}: {e}", exc_info=True)
            return {
                'symbol': symbol,
                'signal': 'NEUTRAL',
                'signal_emoji': '🟡',
                'score': 50,
                'trend': 'NEUTRAL',
                'position_in_range': 50.0,
                'has_history': False,
                'indicators': {},
                'signals': [],
                'summary': 'Erreur lors de l\'analyse'
            }

    def _fmt_price(self, price: float) -> str:
        """Formater un prix selon sa valeur."""
        if price >= 1000:
            return f"${price:,.0f}"
        elif price >= 1:
            return f"${price:,.2f}"
        else:
            return f"${price:.6f}"

    def get_indicator_summary(self, analysis: Dict) -> str:
        """Générer un résumé lisible des indicateurs."""
        lines = []
        indicators = analysis.get('indicators', {})

        rsi = indicators.get('rsi')
        if rsi is not None:
            if rsi >= 70:
                lines.append(f"📊 RSI: {rsi:.0f} ⚠️ Surachat")
            elif rsi <= 30:
                lines.append(f"📊 RSI: {rsi:.0f} 💡 Survente")
            else:
                lines.append(f"📊 RSI: {rsi:.0f}")

        macd = indicators.get('macd')
        if macd:
            arrow = "↑" if macd['bullish_cross'] else "↓"
            lines.append(f"📈 MACD: {arrow} {macd['histogram']:+.4f}")

        bb = indicators.get('bollinger')
        if bb:
            lines.append(f"🎯 Bollinger %B: {bb['percent_b']:.2f} (BW: {bb['bandwidth']:.1f}%)")

        ma7 = indicators.get('ma7')
        ma14 = indicators.get('ma14')
        if ma7 and ma14:
            lines.append(f"📉 MA7: {self._fmt_price(ma7)} | MA14: {self._fmt_price(ma14)}")

        return "\n".join(lines) if lines else "Données insuffisantes"


# Instance globale
analyzer = CryptoAnalyzer()
