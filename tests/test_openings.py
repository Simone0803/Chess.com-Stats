"""Opening nomenclature: ECO codes are never shown as names, and sources are ranked."""
import contextlib
import io
import re
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import src.process_analysis_v4 as pipeline
from src.openings import (UNKNOWN_FAMILY, family_for, load_eco_table, name_from_eco_url, normalize_eco,
                          prefer_richer_naming, repair_legacy_opening_names,
                          resolve_from_record, resolve_opening)

ECO_LIKE = r'[A-E]\d{2}'

PGN_TWO_GAMES = '''[Event "Live Chess"]
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


class CodeAndFamilyTests(unittest.TestCase):
    def test_normalize_eco_accepts_codes_and_rejects_everything_else(self):
        self.assertEqual(normalize_eco('b12'), 'B12')
        self.assertEqual(normalize_eco(' D02 '), 'D02')
        for bad in ('Z99', 'B1', 'B123', 'nan', 'Caro-Kann', None, np.nan, '', 'https://www.chess.com/openings/C50'):
            self.assertIsNone(normalize_eco(bad), bad)

    def test_family_follows_the_letter_and_unknown_is_explicit(self):
        self.assertEqual(family_for('B12'), 'B — Sicilian / Caro-Kann / Modern')
        self.assertEqual(family_for('Unknown'), 'Unknown / Unclassified')


class NameSourceTests(unittest.TestCase):
    def test_pgn_tag_wins_over_url_and_table(self):
        table = {'B12': 'Caro-Kann Defense'}
        result = resolve_opening('B12', 'Caro-Kann Defense: Advance Variation',
                                 'https://www.chess.com/openings/Other-Name', table)
        self.assertEqual(result, ('B12', 'Caro-Kann Defense: Advance Variation',
                                  'B — Sicilian / Caro-Kann / Modern', 'pgn_tag'))

    def test_table_is_used_before_the_url_slug(self):
        result = resolve_opening('C50', None, 'https://www.chess.com/openings/Italian-Game', {'C50': 'Italian Game'})
        self.assertEqual(result[1:], ('Italian Game', 'C — French / Open Games / Ruy Lopez', 'eco_table'))

    def test_url_slug_is_the_fallback_when_nothing_else_exists(self):
        result = resolve_opening('B12', None, 'https://www.chess.com/openings/Caro-Kann-Defense')
        self.assertEqual(result[1:], ('Caro Kann Defense', 'B — Sicilian / Caro-Kann / Modern', 'chesscom_url'))

    def test_eco_code_is_never_the_name(self):
        for args in [('C50', None, None), ('C50', 'C50', None), ('C50', 'nan', 'nan'), ('C50', 'Unknown', None)]:
            code, name, _, source = resolve_opening(*args)
            self.assertEqual((code, name, source), ('C50', 'Unknown', 'none'), args)
            self.assertIsNone(re.fullmatch(ECO_LIKE, name), 'ECO code leaked into opening_name')

    def test_invalid_eco_becomes_unknown_code_and_unknown_name(self):
        self.assertEqual(resolve_opening('Z99', None, None)[:2], ('Unknown', 'Unknown'))

    def test_bare_eco_code_in_the_url_field_is_not_a_url(self):
        # PGN-imported records store the ECO code in rec['eco']; it must not become a slug.
        rec = {'_headers': {'ECO': 'C50'}, 'eco': 'C50', 'opening': None}
        self.assertEqual(resolve_from_record(rec)[1:], ('Unknown', 'C — French / Open Games / Ruy Lopez', 'none'))

    def test_api_record_uses_the_eco_url_slug(self):
        rec = {'eco': 'https://www.chess.com/openings/Caro-Kann-Defense-Advance-Variation'}
        # An API record carries no ECO code, so the family is honestly unknown.
        self.assertEqual(resolve_from_record(rec)[1:3], ('Caro Kann Defense Advance Variation', UNKNOWN_FAMILY))

    def test_url_helper_requires_an_openings_path(self):
        self.assertIsNone(name_from_eco_url('https://www.chess.com/openings'))
        self.assertIsNone(name_from_eco_url('C50'))
        self.assertEqual(name_from_eco_url('https://www.chess.com/openings/Italian-Game'), 'Italian Game')


class EcoTableTests(unittest.TestCase):
    def _table(self, text, name='eco_names.tsv'):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        path = Path(td.name) / name
        path.write_text(text, encoding='utf-8')
        return path

    def test_missing_file_gives_empty_table(self):
        self.assertEqual(load_eco_table(Path('/nonexistent/eco.tsv')), {})

    def test_base_name_is_used_when_variations_extend_it(self):
        path = self._table('eco\tname\nB12\tCaro-Kann Defense\nB12\tCaro-Kann Defense: Advance Variation\n'
                           'C50\tItalian Game\nC50\tGiuoco Piano\n')
        table = load_eco_table(path)
        self.assertEqual(table['B12'], 'Caro-Kann Defense')
        self.assertNotIn('C50', table)  # two unrelated names: refuse to guess

    def test_invalid_rows_are_skipped_and_missing_columns_raise(self):
        path = self._table('eco\tname\nZ99\tBogus\nD02\tQueen’s Pawn Game\n')
        self.assertEqual(load_eco_table(path), {'D02': 'Queen’s Pawn Game'})
        bad = self._table('code,label\nB12,Caro\n', name='bad.csv')
        with self.assertRaises(ValueError):
            load_eco_table(bad)


class LegacyAndMergeTests(unittest.TestCase):
    def test_legacy_eco_names_are_labelled_unknown_and_keep_their_code(self):
        legacy = pd.DataFrame({'eco': ['B12', 'D02'], 'opening_name': ['B12', 'Caro-Kann Defense']})
        fixed = repair_legacy_opening_names(legacy)
        self.assertEqual(fixed['opening_name'].tolist(), ['Unknown', 'Caro-Kann Defense'])
        self.assertEqual(fixed['eco'].tolist(), ['B12', 'D02'])
        self.assertEqual(fixed['naming_source'].tolist(), ['legacy', 'legacy'])

    def test_archive_replaces_a_weaker_processed_name_only(self):
        processed = pd.DataFrame({'game_id': ['g1', 'g2'], 'eco': ['B12', 'B12'], 'opening_name': ['Unknown', 'Caro-Kann Defense'],
                                  'opening_family': ['B — x', 'B — x'], 'naming_source': ['none', 'pgn_tag']})
        archive = pd.DataFrame({'game_id': ['g1', 'g2'], 'eco': ['B12', 'B12'],
                                'opening_name': ['Caro-Kann Defense: Advance Variation', 'Other'],
                                'opening_family': ['B — x', 'B — x'], 'naming_source': ['pgn_tag', 'chesscom_url']})
        merged = prefer_richer_naming(processed, archive)
        self.assertEqual(merged['opening_name'].tolist(), ['Caro-Kann Defense: Advance Variation', 'Caro-Kann Defense'])
        self.assertEqual(merged['naming_source'].tolist(), ['pgn_tag', 'pgn_tag'])


class PipelineNamingTests(unittest.TestCase):
    """Run the real pipeline on a small synthetic dataset and inspect its outputs."""

    ANALYSIS_COLUMNS = ['game_id', 'date', 'player_result', 'player_rating', 'opponent_rating', 'time_class',
                        'time_control', 'eco', 'opening', 'player_color', 'moves', 'accuracy', 'acpl',
                        'inaccuracies', 'mistakes', 'blunders']

    def _run(self, with_pgn=False, table_text=None):
        saved = (pipeline.DATA, pipeline.OUT)
        self.addCleanup(lambda: setattr(pipeline, 'DATA', saved[0]) or setattr(pipeline, 'OUT', saved[1]))
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        data = Path(td.name) / 'data'
        data.mkdir()
        if with_pgn:
            (data / 'games.pgn').write_text(PGN_TWO_GAMES, encoding='utf-8')
            ids = pipeline.parse_pgn_headers(data / 'games.pgn')['game_id'].tolist() + ['g3']
        else:
            ids = ['g1', 'g2', 'g3']
        analysis = pd.DataFrame({
            'game_id': ids, 'date': ['2024.01.02', '2024.01.03', '2024.01.04'],
            'player_result': ['Win', 'Loss', 'Draw'], 'player_rating': [1200, 1200, 1200],
            'opponent_rating': [1190, 1210, 1205], 'time_class': [np.nan] * 3,
            'time_control': ['180+2', '180+2', '600'], 'eco': ['B12', 'D02', 'C50'],
            'opening': ['B12', 'D02', 'C50'],  # legacy column: holds ECO codes, never names
            'player_color': ['White', 'Black', 'White'], 'moves': [30, 30, 30],
            'accuracy': [75, 80, 78], 'acpl': [70, 60, 50], 'inaccuracies': [5, 4, 3],
            'mistakes': [3, 2, 1], 'blunders': [4, 1, 0]})
        analysis.to_csv(data / 'analysis.csv', index=False)
        moves = pd.DataFrame({'game_id': ids, 'ply': 1, 'move_number': 1,
                              'color': analysis['player_color'], 'san': 'e4', 'white': 'me', 'black': 'op',
                              'cpl': 10, 'accuracy': 90, 'classification': 'good', 'phase': 'opening',
                              'date': analysis['date'], 'player_color': analysis['player_color']})
        moves.to_csv(data / 'moves_analysis.csv', index=False)
        if table_text is not None:
            (data / 'openings').mkdir()
            (data / 'openings' / 'eco_names.tsv').write_text(table_text, encoding='utf-8')
        pipeline.DATA, pipeline.OUT = data, data / 'processed_v4'
        pipeline.OUT.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.main()
        games = pd.read_csv(pipeline.OUT / 'games_enriched_v4.csv', dtype={'eco': str})
        stats = pd.read_csv(pipeline.OUT / 'opening_stats_v4.csv', dtype={'eco': str})
        return games, stats

    def test_without_pgn_no_eco_code_is_shown_as_a_name(self):
        games, stats = self._run(with_pgn=False)
        self.assertEqual(games['opening_name'].tolist(), ['Unknown'] * 3)
        self.assertEqual(games['eco'].tolist(), ['B12', 'D02', 'C50'])
        self.assertEqual(games['naming_source'].tolist(), ['none'] * 3)
        self.assertFalse(stats['opening_name'].astype(str).str.fullmatch(ECO_LIKE).any())

    def test_with_pgn_tags_name_the_games_and_gaps_stay_unknown(self):
        games, _ = self._run(with_pgn=True)
        self.assertEqual(games['opening_name'].tolist(),
                         ['Caro-Kann Defense: Advance Variation', 'Unknown', 'Unknown'])
        self.assertEqual(games['naming_source'].tolist(), ['pgn_tag', 'none', 'none'])

    def test_table_name_is_used_only_where_the_code_is_unambiguous(self):
        games, _ = self._run(with_pgn=False, table_text='eco\tname\nC50\tItalian Game\n')
        self.assertEqual(games['opening_name'].tolist(), ['Unknown', 'Unknown', 'Italian Game'])
        self.assertEqual(games['naming_source'].tolist(), ['none', 'none', 'eco_table'])


if __name__ == '__main__':
    unittest.main()
