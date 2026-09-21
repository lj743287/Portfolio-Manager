import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import screener


class MigrationTests(unittest.TestCase):
    def test_alpaca_cache_miss_never_calls_twelve_data(self):
        with patch.object(screener, 'DATA_PROVIDER', 'alpaca'), \
             patch.object(screener.CACHE, 'get', return_value=None), \
             patch.object(screener.requests, 'get') as request:
            self.assertIsNone(screener.td_daily('MISSING'))
            request.assert_not_called()

    def test_cached_bars_are_oldest_first(self):
        rows = [dict(d='2026-09-18', o=3, h=4, l=2, c=3, v=100),
                dict(d='2026-09-17', o=2, h=3, l=1, c=2, v=90)]
        frame = screener.bars_to_frame(rows)
        self.assertEqual(list(frame.close), [2, 3])

    def test_strategy_parameters_preserved(self):
        self.assertEqual(screener.P, dict(
            ADR_MIN=3.5, ADR_MAX=7.5, RUNUP_MIN=40.0, RUNUP_MAX=1000.0,
            PRICE_MIN=3.0, BASE_MIN=6, RUNUP_LB=60, MA_TOL=7.0,
            PEAK_MIN_BACK=2, PULLBACK_MIN=0.5))

    def test_empty_results_render(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(screener, 'OUT_DIR', folder), \
             patch.object(screener, 'DOCS_DIR', folder), \
             patch.object(screener, 'write_scan') as write_scan:
            screener.write_outputs([], universe_n=100)
            page = Path(folder, 'index.html').read_text()
            self.assertIn('No candidates today', page)
            self.assertIn('ADR 3.5%-7.5%', page)
            self.assertEqual(write_scan.call_args.kwargs['meta']['criteria']['runup_min'], 40.0)
