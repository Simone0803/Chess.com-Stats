"""Regression tests for pandas 3 compatibility in the processing pipeline.

Pandas 3 stores missing strings as NaN (not the text 'nan'). These tests pin the
behaviour that used to depend on that difference.
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from src.openings import name_from_eco_url
from src.process_analysis_v4 import parse_pgn_headers, player_move_mask


MIXED_PGN = '''[Event "Live Chess"]
[Site "https://www.chess.com/game/live/1"]
[Date "2024.01.02"]
[UTCDate "2024.01.02"]
[UTCTime "10:00:00"]
[White "me"]
[Black "op"]
[Result "1-0"]
[WhiteElo "1200"]
[BlackElo "1190"]
[ECO "B12"]
[ECOUrl "https://www.chess.com/openings/Caro-Kann-Defense-Advance-Variation"]
[Opening "Caro-Kann Defense: Advance Variation"]
[Termination "me won by resignation"]

1. e4 c6 1-0

[Event "Live Chess"]
[Site "https://www.chess.com/game/live/2"]
[Date "2024.01.03"]
[UTCDate "2024.01.03"]
[UTCTime "10:05:00"]
[White "op"]
[Black "me"]
[Result "1/2-1/2"]
[WhiteElo "1190"]
[BlackElo "1200"]
[ECO "C50"]
[Termination "Game drawn by agreement"]

1. e4 e5 1/2-1/2
'''


class OpeningNameFromUrlTests(unittest.TestCase):
    def test_missing_values_give_none_not_text(self):
        for missing in (np.nan, None, pd.NA, float('nan')):
            self.assertIsNone(name_from_eco_url(missing))

    def test_empty_and_literal_nan_strings_give_none(self):
        self.assertIsNone(name_from_eco_url(''))
        self.assertIsNone(name_from_eco_url('nan'))
        self.assertIsNone(name_from_eco_url('https://www.chess.com/openings/'))

    def test_slug_becomes_readable_name(self):
        self.assertEqual(name_from_eco_url(
            'https://www.chess.com/openings/Caro-Kann_Defense%3A_Advance'), 'Caro Kann Defense: Advance')


class PlayerMoveMaskTests(unittest.TestCase):
    def test_missing_colour_is_never_the_player_move(self):
        color = pd.Series([np.nan, 'White', 'Black', np.nan], dtype=object)
        player = pd.Series([np.nan, 'White', 'White', 'White'], dtype=object)
        self.assertEqual(player_move_mask(color, player).tolist(), [False, True, False, False])

    def test_is_case_insensitive_and_returns_plain_bool(self):
        color = pd.Series(['WHITE', 'black'], dtype=object)
        player = pd.Series(['white', 'white'], dtype=object)
        mask = player_move_mask(color, player)
        self.assertEqual(mask.tolist(), [True, False])
        self.assertEqual(mask.dtype, bool)


class ParsePgnHeadersPandas3Tests(unittest.TestCase):
    def test_game_without_ecourl_does_not_crash_and_stays_missing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'games.pgn'
            path.write_text(MIXED_PGN, encoding='utf-8')
            parsed = parse_pgn_headers(path).set_index('ECO')
        self.assertEqual(parsed.loc['B12', 'eco_url'],
                         'https://www.chess.com/openings/Caro-Kann-Defense-Advance-Variation')
        self.assertTrue(pd.isna(parsed.loc['C50', 'eco_url']))
        self.assertTrue(pd.isna(parsed.loc['C50', 'opening_pgn']))


if __name__ == '__main__':
    unittest.main()
