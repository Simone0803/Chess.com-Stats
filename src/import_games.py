"""Import local PGNs or fetch public Chess.com archive data into SQLite."""
import argparse
from pathlib import Path

from .chess_data import (ChessComClient, DB_PATH, export_json, export_pgn,
                         backfill_pgn_metadata, import_pgn, infer_local_username,
                         save_games, save_player_stats)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", default=None, help="Username Chess.com (oppure CHESSCOM_USERNAME)")
    parser.add_argument("--contact", default="", help="Contatto incluso nel User-Agent API")
    parser.add_argument("--pgn", type=Path, help="Importa un PGN locale")
    parser.add_argument("--fetch", action="store_true", help="Scarica gli archivi pubblici mancanti/aggiornati")
    parser.add_argument("--backfill-pgn-metadata", action="store_true",
                        help="Completa ECO, apertura e conclusione dai tag PGN già archiviati; non usa la rete")
    parser.add_argument("--export-pgn", type=Path, help="Esporta il database locale in PGN")
    parser.add_argument("--export-json", type=Path, help="Esporta il database locale in JSON")
    parser.add_argument("--database", type=Path, default=DB_PATH)
    args = parser.parse_args(argv)
    import os
    username = (args.username or os.getenv("CHESSCOM_USERNAME", "") or infer_local_username() or "").strip()
    if (args.fetch or args.pgn) and not username:
        parser.error("specifica --username oppure CHESSCOM_USERNAME")
    if (args.fetch or args.pgn) and username and not args.username and not os.getenv("CHESSCOM_USERNAME"):
        print(f"Username inferito dai dati locali: {username}")
    if args.pgn:
        print(f"PGN: importate/aggiornate {import_pgn(args.pgn, username, args.database)} partite")
    if args.backfill_pgn_metadata:
        print(f"Metadati PGN: aggiornate {backfill_pgn_metadata(args.database)} partite")
    if args.fetch:
        client=ChessComClient(username, args.contact, db_path=args.database)
        games = list(client.games())
        print(f"PubAPI: archiviate/aggiornate {save_games(games, username, db_path=args.database)} partite")
        stats=client.player_stats()
        save_player_stats(username,stats,args.database)
        print('PubAPI: statistiche di profilo salvate')
    if args.export_pgn:
        print(f"PGN: esportate {export_pgn(args.export_pgn, args.database)} partite")
    if args.export_json:
        print(f"JSON: esportate {export_json(args.export_json, args.database)} partite")
    if not any((args.pgn, args.fetch, args.backfill_pgn_metadata, args.export_pgn, args.export_json)):
        parser.print_help()


if __name__ == "__main__":
    main()
