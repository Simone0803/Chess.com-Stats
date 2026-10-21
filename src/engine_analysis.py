"""Optional post-game Stockfish analysis with a persistent, reproducible cache."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from .chess_data import DB_PATH, connect
from .metrics import classify_cpl, comparable_cpl, move_accuracy, mover_cpl

ENGINE_ANALYSIS_VERSION = 2


def missed_mate_opportunity(mate_before_white, mate_after_white, mover_white: bool) -> bool:
    """Flag only a forced mate for the mover that disappears after their move."""
    if mate_before_white is None: return False
    before_for_mover = mate_before_white > 0 if mover_white else mate_before_white < 0
    if not before_for_mover: return False
    after_for_mover = (mate_after_white is not None and
                       (mate_after_white > 0 if mover_white else mate_after_white < 0))
    return not after_for_mover


def _material_points(board, color_white: bool) -> int:
    """Count board material with pawn=1, knight/bishop=3, rook=5, queen=9."""
    import chess
    values={chess.PAWN:1,chess.KNIGHT:3,chess.BISHOP:3,chess.ROOK:5,chess.QUEEN:9}
    return sum(len(board.pieces(piece,color_white))*value for piece,value in values.items())


def normalize_fen_before(pgn: str, result: dict) -> bool:
    """Repair move FENs from the PGN mainline; return whether any row changed.

    Earlier analysis files accidentally stored the position after each move in
    ``fen_before``. Replaying the PGN repairs that display field without rerunning
    the engine or changing any evaluation.
    """
    try:
        import chess.pgn
        game=chess.pgn.read_game(io.StringIO(pgn))
    except (ImportError,ValueError,TypeError):
        return False
    moves=result.get('moves')
    if game is None or not isinstance(moves,list): return False
    rows_by_ply={}
    for index,row in enumerate(moves,1):
        if not isinstance(row,dict): continue
        try: ply=int(row.get('ply',index))
        except (TypeError,ValueError): continue
        rows_by_ply[ply]=row
    changed=False
    board=game.board()
    for ply,move in enumerate(game.mainline_moves(),1):
        row=rows_by_ply.get(ply)
        if row is not None:
            fen=board.fen()
            if row.get('fen_before') != fen:
                row['fen_before']=fen
                changed=True
        board.push(move)
    return changed


def analyze_pgn(pgn: str, engine_path: str, depth: int = 12, time_limit: float | None = None,
                db_path: Path = DB_PATH, engine_factory=None, username: str | None = None):
    """Analyze each own move after the game has ended.

    CPL is the loss in centipawns for the mover relative to their pre-move engine
    evaluation. Labels are thresholds (50/100/200 cp), not Chess.com labels.
    Accuracy is an independent, exponential move score: 100*exp(-CPL/100), averaged
    over the player's plies. Mate scores are capped to +/-100000 cp.
    """
    try:
        import chess
        import chess.engine
        import chess.pgn
    except ImportError as exc:
        raise RuntimeError("Installa python-chess per l'analisi Stockfish") from exc
    game = chess.pgn.read_game(io.StringIO(pgn))
    if game is None: raise ValueError("PGN vuoto o non valido")
    if game.headers.get("Result", "*") not in {"1-0","0-1","1/2-1/2"}:
        raise ValueError("L'analisi è consentita solo dopo la partita: il PGN non contiene un risultato finale valido")
    if game.errors:
        raise ValueError(f"PGN non valido; analisi interrotta per evitare di valutare una partita parziale: {game.errors[0]}")
    key = hashlib.sha256((f"v{ENGINE_ANALYSIS_VERSION}|" + pgn +
                          f"|{engine_path}|{depth}|{time_limit}|{username or ''}").encode()).hexdigest()
    conn = connect(db_path)
    cached = conn.execute("SELECT result_json FROM engine_cache WHERE cache_key=?", (key,)).fetchone()
    if cached:
        result=json.loads(cached[0])
        if normalize_fen_before(pgn,result):
            conn.execute("UPDATE engine_cache SET result_json=? WHERE cache_key=?",
                          (json.dumps(result,ensure_ascii=False),key))
            conn.commit()
        conn.close()
        return result
    limit = chess.engine.Limit(time=time_limit) if time_limit else chess.engine.Limit(depth=depth)
    factory = engine_factory or chess.engine.SimpleEngine.popen_uci
    rows, board = [], game.board()
    player_white=(game.headers.get('White','').lower()==username.lower()) if username is not None else None
    def phase_for(pos, fullmove):
        nonpawn = sum(len(pos.pieces(piece, color)) * value for color in (chess.WHITE, chess.BLACK)
                      for piece, value in ((chess.QUEEN, 9), (chess.ROOK, 5),
                                           (chess.BISHOP, 3), (chess.KNIGHT, 3)))
        if fullmove <= 10: return "opening"
        return "endgame" if nonpawn <= 13 else "middlegame"

    try:
        with factory(engine_path) as engine:
            for ply, move in enumerate(game.mainline_moves(), 1):
                mover = board.turn
                before_info = engine.analyse(board, limit)
                before_white_score = before_info["score"].pov(chess.WHITE)
                before_white = before_white_score.score(mate_score=100000)
                mate_before_white = before_white_score.mate()
                before = before_white if mover == chess.WHITE else -before_white if before_white is not None else None
                san = board.san(move)
                best_move = before_info.get("pv", [None])[0]
                best_san = board.san(best_move) if best_move is not None and best_move in board.legal_moves else None
                phase = phase_for(board, (ply + 1) // 2)
                fen_before = board.fen()
                material_white=_material_points(board,chess.WHITE)-_material_points(board,chess.BLACK)
                perspective_white=mover if player_white is None else player_white
                player_material=material_white if perspective_white else -material_white
                board.push(move)
                after_info = engine.analyse(board, limit)
                after_white_score = after_info["score"].pov(chess.WHITE)
                after_white = after_white_score.score(mate_score=100000)
                mate_after_white = after_white_score.mate()
                after = after_white if mover == chess.WHITE else -after_white if after_white is not None else None
                loss = mover_cpl(before_white, after_white, mover == chess.WHITE)
                label = classify_cpl(loss)
                own_name = game.headers.get("White" if mover else "Black", "").lower()
                is_player = username is None or own_name == username.lower()
                # Heuristic only: the moved piece now occupies an enemy-attacked
                # square. This does not establish that it is tactically lost.
                exposed = bool(board.piece_at(move.to_square) and
                               board.is_attacked_by(board.turn, move.to_square))
                category = "unknown" if before is None else "winning" if before >= 200 else "losing" if before <= -200 else "roughly_equal"
                missed_mate = missed_mate_opportunity(mate_before_white,mate_after_white,mover == chess.WHITE)
                lost_winning_advantage = bool(is_player and before is not None and after is not None and before >= 200 and after < 200)
                rows.append({"ply": ply, "move_number": (ply + 1) // 2, "color": "White" if mover else "Black",
                             "san": san, "best_move": best_san, "phase": phase, "player_move": is_player,
                             "fen_before": fen_before,
                             "player_material_balance": player_material,
                             "player_in_material_disadvantage": player_material < 0,
                             "eval_before_cp_mover": before, "eval_after_cp_mover": after,
                             "eval_before_cp_white": before_white, "eval_after_cp_white": after_white,
                             "mate_before_white": mate_before_white, "mate_after_white": mate_after_white,
                             "missed_mate_opportunity": missed_mate,
                             "lost_winning_advantage": lost_winning_advantage,
                             "position_before": category,
                             "cpl": loss, "classification": label,
                             "cpl_for_average": comparable_cpl(loss),
                             "accuracy": move_accuracy(loss),
                             "exposed_piece_heuristic": exposed,
                             "large_swing": loss >= 100})
    except Exception:
        conn.close()
        raise
    own = [row for row in rows if row["player_move"]]
    rated_own_scores=[r["accuracy"] for r in own if r["accuracy"] is not None]
    key_moments=sorted((row for row in own if row['cpl_for_average'] is not None),
                       key=lambda row:row['cpl_for_average'],reverse=True)[:5]
    result = {"cache_key": key, "analysis_version": ENGINE_ANALYSIS_VERSION,
              "depth": depth, "time_limit": time_limit,
              "moves": rows, "analyzed_player": username,
              "player_accuracy": sum(rated_own_scores) / len(rated_own_scores) if rated_own_scores else None,
              "lost_winning_advantage_count": sum(r['lost_winning_advantage'] for r in own),
              "missed_mate_opportunities_count": sum(r['missed_mate_opportunity'] for r in own),
              "key_moments": " | ".join(f"{r['move_number']}{'.' if r['color']=='White' else '...'} {r['san']} ({r['classification']}, CPL {r['cpl']})" for r in key_moments),
              "position_thresholds_cp": {"winning": 200, "losing": -200},
              "material_values_points": {"pawn": 1, "knight": 3, "bishop": 3, "rook": 5, "queen": 9},
              "own_move_counts": {label: sum(r["classification"] == label for r in own)
                                  for label in ("good", "inaccuracy", "mistake", "blunder", "unknown")},
              "classification_thresholds_cp": {"inaccuracy": 50, "mistake": 100, "blunder": 200},
              "accuracy_formula": "mean(100 * exp(-max(0,CPL)/100)) on analyzed player moves",
              "tactical_detection": "exposed piece heuristic only: moved piece lands on a square attacked by the opponent; not proof of a tactical loss"}
    conn.execute("INSERT INTO engine_cache VALUES (?,?)", (key, json.dumps(result)))
    conn.commit(); conn.close()
    return result

