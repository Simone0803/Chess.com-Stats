import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests

from src.chess_data import (ChessComClient, connect, export_json, export_pgn,
                            game_rows, game_summary_rows, infer_local_username, read_player_stats,
                            save_games, save_player_stats, stable_game_id, legacy_csv_game_id,
                            parse_pgn_headers)
from src.metrics import (classify_cpl, comparable_cpl, longest_streak, move_accuracy, mover_cpl,
                         grouped_result_summary, monthly_summary, prepare_filters, rating_summary,
                         result_summary)
from src.process_analysis_v4 import parse_tc


class MetricTests(unittest.TestCase):
    def setUp(self):
        self.games = pd.DataFrame({
            'game_id':['a','b','c','d'],
            'date':pd.to_datetime(['2024-01-01','2024-01-02','2024-01-03','2024-02-01'],utc=True),
            'player_result':['Win','Win','Draw','Loss'],
            'player_rating':[1200,1210,1205,1190],
            'player_color':['White','Black','White','Black'],
            'time_category':['Blitz','Bullet','Blitz','Rapid'],
            'rating_band':['1200–1399']*4,
            'rated':[True,True,False,True],
        })

    def test_result_percentages_and_empty(self):
        x=result_summary(self.games)
        self.assertEqual((x['games'],x['wins'],x['draws'],x['losses']),(4,2,1,1))
        self.assertEqual(x['win_pct'],50)
        self.assertIsNone(result_summary(pd.DataFrame({'player_result':[]}))['win_pct'])

    def test_grouped_results_include_draws_and_exclude_unknown_denominators(self):
        games=pd.DataFrame({'game_id':['a','b','c','d','d'],
                            'opening_name':['A','A','A','B','B'],
                            'player_result':['Win','Draw','Loss','Unknown','Win']})
        grouped=grouped_result_summary(games,'opening_name').set_index('opening_name')
        self.assertEqual(grouped.loc['A','Games'],3)
        self.assertEqual(grouped.loc['A','KnownResults'],3)
        self.assertAlmostEqual(grouped.loc['A','WinRate'],1/3)
        self.assertAlmostEqual(grouped.loc['A','DrawRate'],1/3)
        self.assertAlmostEqual(grouped.loc['A','LossRate'],1/3)
        self.assertEqual(grouped.loc['B','Games'],1)
        self.assertEqual(grouped.loc['B','KnownResults'],0)
        self.assertTrue(pd.isna(grouped.loc['B','WinRate']))

    def test_rating_peak_and_streak(self):
        self.assertEqual(rating_summary(self.games)['peak'],1210)
        self.assertEqual(rating_summary(self.games)['peak_date'],'2024-01-02 00:00:00+00:00')
        self.assertEqual(longest_streak(self.games),{'Win':2,'Draw':1,'Loss':1})

    def test_rating_summary_uses_chronology_and_first_peak_on_unsorted_rows(self):
        games=pd.DataFrame({'date':['2024-01-03','2024-01-01','2024-01-02'],
                            'player_rating':[1300,1200,1300]})
        summary=rating_summary(games)
        self.assertEqual(summary['peak'],1300)
        self.assertEqual(summary['peak_date'],'2024-01-02')
        self.assertEqual(summary['change'],100)

    def test_filter_by_time_color_and_period(self):
        chosen=prepare_filters(self.games,start=pd.Timestamp('2024-01-02').date(),
          end=pd.Timestamp('2024-01-31').date(),color='White',time_class='Blitz',rated=False)
        self.assertEqual(chosen.game_id.tolist(),['c'])
        self.assertEqual(prepare_filters(self.games,opponent_band='missing').shape[0],0)

    def test_engine_metric_sign_threshold_mate_cases(self):
        # White drops from +2.0 to +0.5; Black drops from -2.0 to -0.5.
        self.assertEqual(mover_cpl(200,50,True),150)
        self.assertEqual(mover_cpl(-200,-50,False),150)
        self.assertEqual(mover_cpl(-300,-100,True),0)
        self.assertIsNone(mover_cpl(None,100,False))
        self.assertEqual(mover_cpl(100000,-100000,True),200000)
        self.assertEqual([classify_cpl(n) for n in (0,50,100,200)],
                         ['good','inaccuracy','mistake','blunder'])
        self.assertEqual(move_accuracy(0),100)
        self.assertEqual(move_accuracy(-2),100)
        self.assertIsNone(move_accuracy(None))
        self.assertEqual(classify_cpl(None),'unknown')
        self.assertGreater(move_accuracy(100),move_accuracy(300))
        self.assertEqual(comparable_cpl(9999),9999)
        self.assertIsNone(comparable_cpl(10000))

    def test_month_aggregation_matches_game_rows(self):
        x=self.games.copy(); x['win']=x.player_result.eq('Win'); x['accuracy']=[80,75,90,60]
        summary=monthly_summary(x).set_index('month')
        self.assertEqual(summary.loc['2024-01','games'],3)
        self.assertAlmostEqual(summary.loc['2024-01','win'],2/3)

    def test_time_control_parse(self):
        category,fmt,initial,inc,effective=parse_tc('60+1')
        self.assertEqual((category,fmt,initial,inc,effective),('Bullet','1:00+1',60,1,100))


class FakeResponse:
    def __init__(self, payload, status=200, headers=None):
        self.payload=payload; self.status_code=status; self.headers=headers or {}
        self.text=json.dumps(payload)
    def json(self): return self.payload
    def raise_for_status(self):
        if self.status_code>=400: raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, responses): self.responses=list(responses); self.headers={}; self.calls=[]
    def get(self,url,timeout,headers=None):
        self.calls.append((url,headers or {}))
        response=self.responses.pop(0)
        return response


class StorageAndNetworkTests(unittest.TestCase):
    def test_legacy_game_id_matches_existing_processor(self):
        pgn='''[Event "test"]\n[Site "https://www.chess.com/game/live/123"]\n[Date "2024.01.02"]\n[Round "-"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n[UTCDate "2024.01.02"]\n[UTCTime "12:00:00"]\n\n1. e4 e5 1-0\n'''
        raw='\n'.join(['https://www.chess.com/game/live/123','2024.01.02','-','me','opponent','1-0','2024.01.02','12:00:00'])
        self.assertEqual(legacy_csv_game_id(pgn),hashlib.sha1(raw.encode()).hexdigest()[:16])
        self.assertIsNone(legacy_csv_game_id('[Event "missing ids"]\n\n*'))

    def test_pgn_headers_preserve_opening_time_and_termination(self):
        pgn='''[Event "Live Chess"]\n[UTCDate "2024.02.03"]\n[UTCTime "14:05:09"]\n[ECO "B01"]\n[ECOUrl "https://www.chess.com/openings/Scandinavian-Defense"]\n[Termination "Player won by timeout"]\n[Note "escaped \\\"quote\\\""]\n\n1. e4 e5 1-0\n'''
        headers=parse_pgn_headers(pgn)
        self.assertEqual(headers['ECO'],'B01')
        self.assertEqual(headers['UTCTime'],'14:05:09')
        self.assertEqual(headers['Termination'],'Player won by timeout')
        self.assertEqual(headers['Note'],'escaped "quote"')
        from src.chess_data import _game_from_headers
        daily=_game_from_headers({'TimeControl':'1/86400'},'[Event "Daily"]',0)
        self.assertEqual(daily['time_class'],'daily')
        self.assertFalse(_game_from_headers({'Event':'Unrated Blitz'},'',0)['rated'])
        self.assertTrue(_game_from_headers({'Event':'Rated Blitz'},'',0)['rated'])
        self.assertFalse(_game_from_headers({'Rated':'unrated','Event':'Live Chess'},'',0)['rated'])
        self.assertIsNone(_game_from_headers({'Event':'Live Chess'},'',0)['rated'])

    def test_missed_forced_mate_is_side_aware_and_conservative(self):
        from src.engine_analysis import missed_mate_opportunity
        self.assertTrue(missed_mate_opportunity(5,None,True))
        self.assertTrue(missed_mate_opportunity(-5,None,False))
        self.assertFalse(missed_mate_opportunity(-5,None,True))
        self.assertFalse(missed_mate_opportunity(5,3,True))

    def test_material_balance_uses_standard_piece_values(self):
        try:
            import chess
        except ImportError:
            self.skipTest('python-chess is optional for local PGN ingestion')
        from src.engine_analysis import _material_points
        board=chess.Board()
        self.assertEqual(_material_points(board,chess.WHITE)-_material_points(board,chess.BLACK),0)
        for san in ('e4','d5','exd5'):
            board.push_san(san)
        self.assertEqual(_material_points(board,chess.WHITE)-_material_points(board,chess.BLACK),1)

    def test_username_inference_requires_clear_active_side_majority(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'moves.csv'
            p.write_text('color,player_color,white,black\nWhite,White,me,opp1\nBlack,Black,opp2,me\nWhite,White,me,opp3\n',encoding='utf-8')
            self.assertEqual(infer_local_username(p),'me')
            p.write_text('color,player_color,white,black\nWhite,White,me,opp1\nBlack,Black,other,opp2\n',encoding='utf-8')
            self.assertIsNone(infer_local_username(p))

    def test_stable_id_and_upsert_deduplicate(self):
        game={'uuid':'stable-1','pgn':'pgn','white':{'username':'me'},'black':{}}
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'games.sqlite3'
            self.assertEqual(stable_game_id(game),'stable-1')
            self.assertEqual(save_games([game],'me',db_path=db),1)
            self.assertEqual(save_games([game],'me',db_path=db),0)
            changed={**game,'white':{'username':'me','rating':1400}}
            self.assertEqual(save_games([changed],'me',db_path=db),1)
            self.assertEqual(len(game_rows(db)),1)
            self.assertEqual(game_rows(db)[0]['white']['rating'],1400)
            revised_pgn={**changed,'pgn':'revised pgn'}
            self.assertEqual(save_games([revised_pgn],'me',db_path=db),1)
            self.assertEqual(game_rows(db)[0]['pgn'],'revised pgn')

    def test_api_game_persists_opening_and_termination_from_pgn_headers(self):
        pgn='''[Event "Live Chess"]\n[Site "Chess.com"]\n[ECO "B01"]\n[Opening "Scandinavian Defense"]\n[Termination "Black won by resignation"]\n\n1. e4 d5 1-0\n'''
        game={'uuid':'metadata-1','pgn':pgn,'white':{'username':'me'},'black':{'username':'opponent'}}
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'games.sqlite3'
            save_games([game],'me',db_path=db)
            with connect(db) as conn:
                eco,opening=conn.execute('SELECT eco,opening FROM games WHERE game_id=?',('metadata-1',)).fetchone()
            record=game_rows(db)[0]
            self.assertEqual((eco,opening),('B01','Scandinavian Defense'))
            self.assertEqual(record['opening'],'Scandinavian Defense')
            self.assertEqual(record['eco_code'],'B01')
            self.assertEqual(record['termination'],'Black won by resignation')

    def test_backfill_pgn_metadata_repairs_existing_api_rows_without_network(self):
        from src.chess_data import backfill_pgn_metadata
        pgn='''[Event "Live Chess"]\n[ECO "B01"]\n[Opening "Scandinavian Defense"]\n[Termination "White won by timeout"]\n\n1. e4 d5 1-0\n'''
        game={'uuid':'legacy-metadata','pgn':pgn,'white':{'username':'me'},'black':{'username':'opponent'}}
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'games.sqlite3'
            save_games([game],'me',db_path=db)
            legacy_metadata={'uuid':'legacy-metadata','pgn':pgn}
            with connect(db) as conn:
                conn.execute('UPDATE games SET eco=NULL,opening=NULL,metadata_json=? WHERE game_id=?',
                             (json.dumps(legacy_metadata),'legacy-metadata'))
            self.assertEqual(backfill_pgn_metadata(db),1)
            self.assertEqual(backfill_pgn_metadata(db),0)
            with connect(db) as conn:
                eco,opening=conn.execute('SELECT eco,opening FROM games WHERE game_id=?',('legacy-metadata',)).fetchone()
                metadata=json.loads(conn.execute('SELECT metadata_json FROM games WHERE game_id=?',
                                                 ('legacy-metadata',)).fetchone()[0])
            record=game_rows(db)[0]
            self.assertEqual((eco,opening),('B01','Scandinavian Defense'))
            self.assertEqual(record['eco_code'],'B01')
            self.assertEqual(record['termination'],'White won by timeout')
            self.assertNotIn('pgn',metadata)

    def test_archive_exports_are_reproducible(self):
        game={'uuid':'export-1','url':'https://www.chess.com/game/live/export-1','pgn':'[Event "x"]\n\n1. e4 *',
              'white':{'username':'me'},'black':{'username':'other'}}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); db=root/'games.sqlite3'; js=root/'games.json'; pgn=root/'games.pgn'
            save_games([game],'me',db_path=db)
            self.assertEqual(export_json(js,db),1)
            self.assertEqual(export_pgn(pgn,db),1)
            self.assertEqual(json.loads(js.read_text())[0]['uuid'],'export-1')
            self.assertIn('1. e4',json.loads(js.read_text())[0]['pgn'])
            self.assertIn('1. e4',pgn.read_text())
            self.assertIn('pgn',game_rows(db)[0])
            self.assertNotIn('pgn',game_summary_rows(db)[0])
            with connect(db) as conn:
                metadata=json.loads(conn.execute('SELECT metadata_json FROM games').fetchone()[0])
            self.assertNotIn('pgn',metadata)

    def test_api_retry_cache_and_archive_list(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'cache.sqlite3'
            session=FakeSession([FakeResponse({},429,{'Retry-After':'0'}),
                                 FakeResponse({'archives':['https://api.chess.com/pub/player/me/games/2024/01']}),
                                 FakeResponse({'games':[]})])
            client=ChessComClient('Me',session=session,retries=1,sleep=lambda _:None,db_path=db)
            self.assertEqual(list(client.games()),[])
            self.assertEqual(len(session.calls),3)
            # Both successful endpoints now come from the local TTL cache.
            list(client.games())
            self.assertEqual(len(session.calls),3)

    def test_api_304_revalidates_cached_json(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'cache.sqlite3'; url='https://api.chess.com/pub/test'
            session=FakeSession([FakeResponse({'ok':True},headers={'ETag':'"etag-1"'}),FakeResponse({},304)])
            client=ChessComClient('me',session=session,retries=0,db_path=db)
            self.assertEqual(client.get(url).json(),{'ok':True})
            with connect(db) as conn:
                conn.execute('UPDATE api_cache SET fetched_at=? WHERE url=?',('2000-01-01T00:00:00+00:00',url))
            self.assertEqual(client.get(url).json(),{'ok':True})
            self.assertEqual(session.calls[-1][1].get('If-None-Match'),'"etag-1"')

    def test_player_stats_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            db=Path(td)/'games.sqlite3'; expected={'chess_blitz':{'best':{'rating':1800,'date':123}}}
            session=FakeSession([FakeResponse(expected)])
            client=ChessComClient('Me',session=session,retries=0,db_path=db)
            stats=client.player_stats()
            save_player_stats('Me',stats,db)
            self.assertEqual(read_player_stats('me',db),expected)

    def test_missing_metadata_and_pgn_import(self):
        from src.chess_data import import_pgn
        pgn='''[Event "rated blitz game"]\n[Site "https://www.chess.com/game/live/12345"]\n[Date "2024.01.02"]\n[UTCDate "2024.01.02"]\n[UTCTime "12:30:00"]\n[Round "-"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n[WhiteElo "1200"]\n[BlackElo "1190"]\n[TimeControl "180+2"]\n\n1. e4 {comment} (1. d4 d5) e5 2. Nf3 Nc6 1-0\n'''
        incomplete='''[Event "unknown"]\n[Site "?"]\n[White "me"]\n[Black "opponent2"]\n\n1. d4 d5 2. c4 *\n'''
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'game.pgn'; p.write_text(pgn+'\n'+incomplete)
            db=Path(td)/'games.sqlite3'
            self.assertEqual(import_pgn(p,'me',db),2)
            self.assertEqual(import_pgn(p,'me',db),0)
            row=next(x for x in game_rows(db) if str(x.get('url') or '').endswith('12345'))
            self.assertEqual(row['white']['result'],'win')
            self.assertEqual(row['time_control'],'180+2')
            self.assertEqual(row['moves'],4)
            self.assertEqual(row['time_class'],'blitz')
            self.assertEqual(len(game_rows(db)),2)

    def test_generic_chesscom_site_uses_each_pgn_link_for_identity(self):
        from src.chess_data import import_pgn
        games=[]
        for game_id,move in [('111','e4'),('222','d4')]:
            games.append(f'''[Event "Live Chess"]\n[Site "Chess.com"]\n[Link "https://www.chess.com/game/live/{game_id}"]\n[Date "2024.01.02"]\n[White "me"]\n[Black "opponent"]\n[Result "*"]\n\n1. {move} *\n''')
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'games.pgn'; p.write_text('\n'.join(games),encoding='utf-8')
            db=Path(td)/'games.sqlite3'
            self.assertEqual(import_pgn(p,'me',db),2)
            with connect(db) as conn:
                self.assertEqual({row[0] for row in conn.execute('SELECT game_id FROM games')},{'111','222'})
            self.assertEqual({x['url'] for x in game_rows(db)},
                             {'https://www.chess.com/game/live/111','https://www.chess.com/game/live/222'})


class EngineCliTests(unittest.TestCase):
    def test_engine_refuses_unfinished_or_malformed_games_before_startup(self):
        from src.engine_analysis import analyze_pgn
        unfinished='''[Event "Unfinished"]\n[White "me"]\n[Black "opponent"]\n[Result "*"]\n\n1. e4 *\n'''
        malformed='''[Event "Malformed"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n\n1. e5 1-0\n'''
        with tempfile.TemporaryDirectory() as td:
            for pgn,message in ((unfinished,'risultato finale valido'),(malformed,'PGN non valido')):
                with self.subTest(message=message), self.assertRaisesRegex(ValueError,message):
                    analyze_pgn(pgn,'fake-engine',db_path=Path(td)/'games.sqlite3',
                                engine_factory=lambda _:self.fail('engine must not start'))

    def test_engine_move_fen_points_to_the_position_before_the_move(self):
        import chess
        import chess.engine
        from src.engine_analysis import analyze_pgn
        pgn='''[Event "FEN test"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n\n1. e4 1-0\n'''
        class FakeEngine:
            def __init__(self): self.positions=[]
            def __enter__(self): return self
            def __exit__(self,*args): return False
            def analyse(self,board,limit):
                self.positions.append(board.fen())
                pv=[chess.Move.from_uci('e2e4')] if board.turn == chess.WHITE else []
                return {'score':chess.engine.PovScore(chess.engine.Cp(0),chess.WHITE),'pv':pv}
        with tempfile.TemporaryDirectory() as td:
            engine=FakeEngine()
            db=Path(td)/'games.sqlite3'
            result=analyze_pgn(pgn,'fake-engine',db_path=db,
                               engine_factory=lambda _:engine,username='me')
            self.assertEqual(result['moves'][0]['fen_before'],engine.positions[0])
            self.assertNotEqual(result['moves'][0]['fen_before'],engine.positions[1])
            cached=result.copy(); cached['moves']=[dict(result['moves'][0],fen_before=engine.positions[1])]
            with connect(db) as conn:
                conn.execute('UPDATE engine_cache SET result_json=? WHERE cache_key=?',
                             (json.dumps(cached),result['cache_key']))
            reused=analyze_pgn(pgn,'fake-engine',db_path=db,
                               engine_factory=lambda _:self.fail('cache hit must not start engine'),username='me')
            self.assertEqual(reused['moves'][0]['fen_before'],engine.positions[0])
            with connect(db) as conn:
                stored=json.loads(conn.execute('SELECT result_json FROM engine_cache WHERE cache_key=?',
                                               (result['cache_key'],)).fetchone()[0])
            self.assertEqual(stored['moves'][0]['fen_before'],engine.positions[0])

    def test_resume_keeps_prior_results_and_analyzes_selected_game(self):
        import chess
        from src.analyze_games import main, _write_checkpoint
        from src.chess_data import save_games
        from src.engine_analysis import ENGINE_ANALYSIS_VERSION
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); db=root/'games.sqlite3'; output=root/'engine.json'
            pgn='''[Event "CLI"]\n[Site "Chess.com"]\n[Link "https://www.chess.com/game/live/one"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n\n1. e4 1-0\n'''
            pgn2=pgn.replace('/one','/two').replace('"one"','"two"')
            save_games([{'uuid':'one','url':'https://www.chess.com/game/live/one','pgn':pgn,
                         'rules':'chess','white':{'username':'me'},'black':{'username':'opponent'}},
                        {'uuid':'two','url':'https://www.chess.com/game/live/two','pgn':pgn2,
                         'rules':'chess960','white':{'username':'me'},'black':{'username':'opponent'}}],'me',db_path=db)
            settings={'engine':str(Path('/usr/bin/stockfish').resolve()),'username':'me','depth':8,
                      'time_limit':None,'analysis_version':ENGINE_ANALYSIS_VERSION}
            post_move=chess.Board(); post_move.push_san('e4')
            old_move={'ply':1,'move_number':1,'color':'White','san':'e4','fen_before':post_move.fen()}
            _write_checkpoint(output,settings,{'one':{'game_id':'one','moves':[old_move],'cache_key':'old'}})
            with connect(db) as conn:
                conn.execute('INSERT INTO engine_cache VALUES(?,?)',
                             ('old',json.dumps({'game_id':'one','moves':[old_move]})))
            with patch('src.analyze_games.analyze_pgn',return_value={'moves':[],'player_accuracy':80,'cache_key':'new'}) as run:
                main(['--engine','/usr/bin/stockfish','--username','me','--depth','8','--database',str(db),
                      '--output',str(output),'--resume','--game-id','two','--checkpoint-every','1'])
            run.assert_called_once()
            saved=json.loads(output.read_text())
            self.assertEqual(saved['schema_version'],1)
            self.assertEqual({x['game_id'] for x in saved['results']},{'one','two'})
            self.assertEqual({x['game_id']:x['rules'] for x in saved['results']},
                             {'one':'chess','two':'chess960'})
            repaired=next(x for x in saved['results'] if x['game_id']=='one')
            self.assertEqual(repaired['moves'][0]['fen_before'],chess.Board().fen())
            with connect(db) as conn:
                repaired_cache=json.loads(conn.execute('SELECT result_json FROM engine_cache WHERE cache_key=?',
                                                       ('old',)).fetchone()[0])
            self.assertEqual(repaired_cache['moves'][0]['fen_before'],chess.Board().fen())

    def test_keyboard_interrupt_saves_checkpoint_without_traceback(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from src.analyze_games import main, _write_checkpoint
        from src.chess_data import save_games
        from src.engine_analysis import ENGINE_ANALYSIS_VERSION
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); db=root/'games.sqlite3'; output=root/'engine.json'
            pgn='''[Event "CLI"]\n[Site "Chess.com"]\n[Link "https://www.chess.com/game/live/two"]\n[White "me"]\n[Black "opponent"]\n[Result "1-0"]\n\n1. e4 1-0\n'''
            save_games([{'uuid':'two','url':'https://www.chess.com/game/live/two','pgn':pgn,
                         'rules':'chess','white':{'username':'me'},'black':{'username':'opponent'}}],
                       'me',db_path=db)
            settings={'engine':str(Path('/usr/bin/stockfish').resolve()),'username':'me','depth':8,
                      'time_limit':None,'analysis_version':ENGINE_ANALYSIS_VERSION}
            _write_checkpoint(output,settings,{'one':{'game_id':'one','moves':[],'cache_key':'old'}})
            captured=StringIO()
            with patch('src.analyze_games.analyze_pgn',side_effect=KeyboardInterrupt), redirect_stdout(captured):
                main(['--engine','/usr/bin/stockfish','--username','me','--depth','8','--database',str(db),
                      '--output',str(output),'--resume','--game-id','two'])
            saved=json.loads(output.read_text())
            self.assertEqual({x['game_id'] for x in saved['results']},{'one'})
            self.assertIn('checkpoint salvato',captured.getvalue())
            self.assertIn('Riprendi con --resume',captured.getvalue())


if __name__=='__main__': unittest.main()
