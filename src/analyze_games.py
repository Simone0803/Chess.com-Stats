"""Run resumable, post-game Stockfish analysis over stored PGNs."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path

from .chess_data import DB_PATH, connect, infer_local_username, legacy_csv_game_ids
from .engine_analysis import ENGINE_ANALYSIS_VERSION, analyze_pgn, normalize_fen_before

ENGINE_SUPPORTED_RULES={"", "chess", "standard", "chess960", "oddschess"}


def _enrich_summary(result: dict):
    """Add compact phase/material summaries so the dashboard needn't load all plies."""
    moves=result.get('moves') or []
    own=[move for move in moves if move.get('player_move')]
    if 'phase_summary' not in result:
        phase_groups=defaultdict(list)
        for move in own: phase_groups[move.get('phase','unknown')].append(move)
        result['phase_summary']={phase:{
            'moves':len(group),
            'mean_cpl':_mean([m.get('cpl_for_average') for m in group]),
            'mean_accuracy':_mean([m.get('accuracy') for m in group]),
            'errors':sum(m.get('classification') in ('inaccuracy','mistake','blunder') for m in group),
            'major_errors':sum(m.get('classification') in ('mistake','blunder') for m in group),
            'missed_mates':sum(bool(m.get('missed_mate_opportunity')) for m in group)}
            for phase,group in phase_groups.items()}
    if 'material_summary' not in result:
        material_groups=defaultdict(list)
        for move in own:
            balance=move.get('player_material_balance')
            state='unknown' if balance is None else 'disadvantage' if balance<0 else 'advantage' if balance>0 else 'equal'
            material_groups[state].append(move)
        result['material_summary']={state:{
            'moves':len(group),
            'mean_cpl':_mean([m.get('cpl_for_average') for m in group]),
            'major_errors':sum(m.get('classification') in ('mistake','blunder') for m in group),
            'missed_mates':sum(bool(m.get('missed_mate_opportunity')) for m in group)}
            for state,group in material_groups.items()}


def _mean(values):
    observed=[float(value) for value in values if value is not None]
    return sum(observed)/len(observed) if observed else None


def _atomic_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,tmp_name=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp_name,path)
    except BaseException:
        try: os.unlink(tmp_name)
        except FileNotFoundError: pass
        raise


def _write_checkpoint(path: Path, settings: dict, results: dict[str, dict]):
    for result in results.values(): _enrich_summary(result)
    ordered=sorted(results.values(),key=lambda item:(item.get('game_id',''),item.get('cache_key','')))
    payload={"schema_version":1,"settings":settings,"results":ordered}
    index_path=path.with_name(f'{path.stem}_index{path.suffix}')
    index={"schema_version":1,"settings":settings,
           "results":[{key:value for key,value in item.items() if key!='moves'} for item in ordered]}
    _atomic_json(index_path,index)
    _atomic_json(path,payload)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--engine', required=True, help='Path to Stockfish UCI executable')
    p.add_argument('--username', default=os.getenv('CHESSCOM_USERNAME'), help='Player whose move metrics to summarize')
    p.add_argument('--depth', type=int, default=12)
    p.add_argument('--time', type=float, default=None, help='Seconds per position (overrides depth)')
    p.add_argument('--database', type=Path, default=DB_PATH)
    p.add_argument('--output', type=Path, default=Path('data/engine_analysis.json'))
    p.add_argument('--game-id', action='append', default=[], help='Analyze only this stored game ID; can be repeated')
    p.add_argument('--limit', type=int, default=None, help='Analyze at most this many selected games')
    p.add_argument('--resume', action='store_true', help='Resume an output file made with the same engine settings')
    p.add_argument('--checkpoint-every', type=int, default=10, help='Atomically save progress after N games')
    args = p.parse_args(argv)
    if args.depth < 1 or (args.time is not None and args.time <= 0): p.error('--depth must be positive and --time must be greater than zero')
    if args.limit is not None and args.limit < 1: p.error('--limit must be positive')
    if args.checkpoint_every < 1: p.error('--checkpoint-every must be positive')
    if not args.username:
        args.username=infer_local_username()
        if args.username: print(f"Username inferito dai dati locali: {args.username}")
    if not args.username:
        p.error('specifica --username oppure imposta CHESSCOM_USERNAME per selezionare le mosse del giocatore')
    args.username=args.username.strip().lower()
    settings={"engine":str(Path(args.engine).expanduser().resolve()),"username":args.username,
              "depth":args.depth,"time_limit":args.time,"analysis_version":ENGINE_ANALYSIS_VERSION}
    results={}
    if args.resume and args.output.exists():
        previous=json.loads(args.output.read_text(encoding='utf-8'))
        if not isinstance(previous,dict) or previous.get('schema_version')!=1:
            p.error('Il file esistente non è riprendibile; scegli un nuovo --output o rimuovi --resume')
        if previous.get('settings')!=settings:
            p.error('Le impostazioni non corrispondono al checkpoint; usa le stesse o scegli un nuovo --output')
        results={str(item['game_id']):item for item in previous.get('results',[]) if item.get('game_id')}
        with connect(args.database) as conn:
            stored_games={game_id:(rules,pgn) for game_id,rules,pgn in
                          conn.execute('SELECT game_id,rules,pgn FROM games')}
        cache_repairs=[]
        for game_id,item in results.items():
            stored_rules,stored_pgn=stored_games.get(game_id,('chess',None))
            item.setdefault('rules',(stored_rules or 'chess').lower())
            if stored_pgn and normalize_fen_before(stored_pgn,item) and item.get('cache_key'):
                cache_repairs.append((json.dumps(item,ensure_ascii=False),item['cache_key']))
        if cache_repairs:
            with connect(args.database) as conn:
                conn.executemany('UPDATE engine_cache SET result_json=? WHERE cache_key=?',cache_repairs)
    with connect(args.database) as conn:
        rows=conn.execute('SELECT game_id,pgn,rules,url FROM games WHERE pgn IS NOT NULL ORDER BY end_time,game_id').fetchall()
    requested=set(args.game_id)
    if requested:
        rows=[row for row in rows if row[0] in requested]
        missing=requested-{row[0] for row in rows}
        if missing: p.error(f"game-id non presenti nel database: {', '.join(sorted(missing))}")
    if args.limit is not None: rows=rows[:args.limit]
    if args.resume:
        rows=[row for row in rows if row[0] not in results]
    skipped=0
    interrupted=False
    try:
        for index,(game_id,pgn,rules,game_url) in enumerate(rows,1):
            normalized_rules=(rules or '').lower()
            if normalized_rules not in ENGINE_SUPPORTED_RULES:
                skipped+=1
            else:
                try:
                    analysis=analyze_pgn(pgn,settings['engine'],args.depth,args.time,args.database,username=args.username)
                    results[game_id]={'game_id':game_id,'game_url':game_url,'rules':normalized_rules or 'chess',
                                      'legacy_game_ids':legacy_csv_game_ids(pgn),**analysis}
                    print(f"Analizzata {game_id}: {len(analysis['moves'])} mosse")
                except Exception as exc:
                    skipped+=1
                    print(f"Saltata {game_id}: {exc}")
            if index % args.checkpoint_every==0:
                _write_checkpoint(args.output,settings,results)
    except KeyboardInterrupt:
        interrupted=True
    finally:
        _write_checkpoint(args.output,settings,results)
    if interrupted:
        print(f"Interrotto; checkpoint salvato con {len(results)} partite in {args.output}. Riprendi con --resume.")
        return
    print(f"Completate {len(results)} partite; saltate in questo batch {skipped}; risultati in {args.output}")


if __name__ == '__main__': main()
