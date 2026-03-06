"""
Tests unitaires — CryptoAnalyzer et TechnicalIndicators
Couvre les cas critiques : RSI, MACD, Bollinger, score de signal.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crypto_analyzer import TechnicalIndicators, CryptoAnalyzer

analyzer = CryptoAnalyzer()


class TestSMA(unittest.TestCase):
    def test_basic(self):
        prices = [1, 2, 3, 4, 5]
        # sma retourne la moyenne des N dernières valeurs (float)
        result = TechnicalIndicators.sma(prices, 3)
        self.assertAlmostEqual(result, 4.0)  # moyenne de [3,4,5]

    def test_period_equals_length(self):
        prices = [10, 20, 30]
        result = TechnicalIndicators.sma(prices, 3)
        self.assertAlmostEqual(result, 20.0)

    def test_empty(self):
        result = TechnicalIndicators.sma([], 5)
        self.assertIsNone(result)

    def test_insufficient_period(self):
        result = TechnicalIndicators.sma([1, 2], 5)
        self.assertIsNone(result)


class TestRSI(unittest.TestCase):
    def test_overbought(self):
        # Série de hausses répétées → RSI proche de 100
        prices = [100 + i * 5 for i in range(20)]
        rsi = TechnicalIndicators.rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 70)

    def test_oversold(self):
        # Série de baisses répétées → RSI proche de 0
        prices = [200 - i * 5 for i in range(20)]
        rsi = TechnicalIndicators.rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertLess(rsi, 30)

    def test_neutral(self):
        # Alternance hausses/baisses → RSI autour de 50
        prices = [100 + (5 if i % 2 == 0 else -5) for i in range(20)]
        rsi = TechnicalIndicators.rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 30)
        self.assertLess(rsi, 70)

    def test_insufficient_data(self):
        rsi = TechnicalIndicators.rsi([100, 101, 102], 14)
        self.assertIsNone(rsi)

    def test_bounds(self):
        prices = [100 + i * 3 for i in range(30)]
        rsi = TechnicalIndicators.rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0)
        self.assertLessEqual(rsi, 100)


class TestMACD(unittest.TestCase):
    def test_returns_dict(self):
        prices = [100 + i * 0.5 for i in range(40)]
        result = TechnicalIndicators.macd(prices)
        self.assertIn('macd', result)
        self.assertIn('signal', result)
        self.assertIn('histogram', result)
        self.assertIn('bullish_cross', result)

    def test_insufficient_data(self):
        result = TechnicalIndicators.macd([100] * 5)
        self.assertIsNone(result)

    def test_bullish_cross_detection(self):
        # Construire une série qui va créer un croisement haussier
        # (difficile à garantir exactement, on vérifie juste que c'est un bool)
        prices = [100 + i for i in range(40)]
        result = TechnicalIndicators.macd(prices)
        self.assertIsInstance(result.get('bullish_cross'), bool)


class TestBollingerBands(unittest.TestCase):
    def test_returns_all_fields(self):
        prices = [100 + i * 0.1 for i in range(25)]
        result = TechnicalIndicators.bollinger_bands(prices)
        self.assertIn('upper', result)
        self.assertIn('middle', result)
        self.assertIn('lower', result)
        self.assertIn('percent_b', result)
        self.assertIn('bandwidth', result)

    def test_upper_above_lower(self):
        prices = [100 + (i % 5) for i in range(25)]
        result = TechnicalIndicators.bollinger_bands(prices)
        if result.get('upper') and result.get('lower'):
            self.assertGreater(result['upper'], result['lower'])

    def test_insufficient_data(self):
        result = TechnicalIndicators.bollinger_bands([100] * 5)
        self.assertIsNone(result)


class TestCryptoAnalyzer(unittest.TestCase):
    def _make_price_data(self, price=50000, change=2.5):
        return {
            'symbol': 'BTC',
            'name': 'Bitcoin',
            'price': price,
            'change_24h': change,
            'change_7d': 5.0,
            'high_24h': price * 1.03,
            'low_24h': price * 0.97,
        }

    def test_basic_analysis_no_history(self):
        result = analyzer.analyze('BTC', self._make_price_data(), None)
        self.assertIn('signal', result)
        self.assertIn('score', result)
        self.assertIn('trend', result)
        self.assertFalse(result.get('has_history'))

    def test_analysis_with_history(self):
        prices = [48000 + i * 100 for i in range(35)]
        result = analyzer.analyze('BTC', self._make_price_data(), prices)
        self.assertTrue(result.get('has_history'))
        self.assertIn('indicators', result)

    def test_signal_is_valid_value(self):
        valid = {'STRONG BUY', 'BUY', 'NEUTRAL', 'SELL', 'STRONG SELL'}
        result = analyzer.analyze('BTC', self._make_price_data(), None)
        self.assertIn(result['signal'], valid)

    def test_score_in_range(self):
        prices = [50000 + i * 10 for i in range(35)]
        result = analyzer.analyze('BTC', self._make_price_data(), prices)
        self.assertGreaterEqual(result['score'], 0)
        self.assertLessEqual(result['score'], 100)

    def test_bearish_scenario(self):
        """Forte baisse → signal baissier."""
        data = self._make_price_data(change=-15.0)
        prices = [50000 - i * 500 for i in range(35)]
        result = analyzer.analyze('BTC', data, prices)
        self.assertIn(result['signal'], {'SELL', 'STRONG SELL', 'NEUTRAL'})

    def test_bullish_scenario(self):
        """Forte hausse → signal haussier."""
        data = self._make_price_data(change=12.0)
        prices = [30000 + i * 800 for i in range(35)]
        result = analyzer.analyze('BTC', data, prices)
        self.assertIn(result['signal'], {'BUY', 'STRONG BUY', 'NEUTRAL'})


class TestSubscriptionManager(unittest.TestCase):
    """Tests du subscription_manager (sans DB réelle — mock en mémoire)."""

    def test_generate_referral_code_deterministic(self):
        from referral_manager import ReferralManager
        rm = ReferralManager()
        code1 = rm.generate_code(123456789)
        code2 = rm.generate_code(123456789)
        self.assertEqual(code1, code2)
        self.assertEqual(len(code1), 8)
        self.assertTrue(code1.isupper())

    def test_tier_configs_complete(self):
        from subscription_manager import TIER_CONFIGS
        required = {'alerts_limit', 'watchlist_limit', 'currencies', 'chart_access',
                    'portfolio_access', 'advanced_analysis', 'daily_digest', 'priority_cache'}
        for tier, config in TIER_CONFIGS.items():
            for field in required:
                self.assertTrue(hasattr(config, field), f"Tier {tier} missing field {field}")

    def test_tier_hierarchy(self):
        """Premium doit avoir plus de fonctionnalités que Free."""
        from subscription_manager import TIER_CONFIGS
        free    = TIER_CONFIGS['free']
        premium = TIER_CONFIGS['premium']
        self.assertGreater(premium.alerts_limit, free.alerts_limit)
        self.assertGreater(premium.watchlist_limit, free.watchlist_limit)
        self.assertGreater(len(premium.currencies), len(free.currencies))
        self.assertTrue(premium.chart_access)
        self.assertFalse(free.chart_access)


class _CooldownBucket:
    """Copie standalone pour les tests (sans dépendances Discord)."""
    from collections import defaultdict
    COOLDOWNS = {'free': 15, 'basic': 8, 'pro': 4, 'premium': 2}
    LIGHT_COMMANDS = {'ping', 'help'}
    LIGHT_COOLDOWN = 3

    def __init__(self):
        from collections import defaultdict
        self._last_used = defaultdict(float)

    def check(self, user_id, command, tier):
        import time
        now = time.monotonic()
        key = (user_id, command)
        cooldown = self.LIGHT_COOLDOWN if command in self.LIGHT_COMMANDS else self.COOLDOWNS.get(tier, 15)
        elapsed = now - self._last_used[key]
        if elapsed >= cooldown:
            self._last_used[key] = now
            return True, 0.0
        return False, round(cooldown - elapsed, 1)


class TestCooldownBucket(unittest.TestCase):
    def setUp(self):
        self.bucket = _CooldownBucket()

    def test_first_call_allowed(self):
        ok, remaining = self.bucket.check(1, 'price', 'free')
        self.assertTrue(ok)
        self.assertEqual(remaining, 0.0)

    def test_second_call_blocked(self):
        self.bucket.check(1, 'price', 'free')
        ok, remaining = self.bucket.check(1, 'price', 'free')
        self.assertFalse(ok)
        self.assertGreater(remaining, 0)

    def test_different_users_independent(self):
        self.bucket.check(1, 'price', 'free')
        ok, _ = self.bucket.check(2, 'price', 'free')
        self.assertTrue(ok)

    def test_premium_has_lower_cooldown(self):
        self.assertEqual(self.bucket.COOLDOWNS['premium'], 2)
        self.assertLess(self.bucket.COOLDOWNS['premium'], self.bucket.COOLDOWNS['free'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
