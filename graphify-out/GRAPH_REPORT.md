# Graph Report - chesscom_diamond_analytics_v4  (2026-10-21)

## Corpus Check
- cluster-only mode — file stats not available

## Summary
- 154 nodes · 400 edges · 6 communities
- Extraction: 98% EXTRACTED · 2% INFERRED · 0% AMBIGUOUS · INFERRED: 8 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- chess_data.py
- dashboard_v4.py
- engine_analysis.py
- StorageAndNetworkTests
- test_core.py
- analyze_games.py

## God Nodes (most connected - your core abstractions)
1. `connect()` - 18 edges
2. `analyze_pgn()` - 16 edges
3. `StorageAndNetworkTests` - 14 edges
4. `import_pgn()` - 13 edges
5. `ChessComClient` - 12 edges
6. `save_games()` - 12 edges
7. `main()` - 11 edges
8. `main()` - 11 edges
9. `game_rows()` - 10 edges
10. `infer_local_username()` - 10 edges

## Surprising Connections (you probably didn't know these)
- `StorageAndNetworkTests` --uses--> `ChessComClient`  [INFERRED]
  tests/test_core.py → src/chess_data.py
- `archived_rows()` --calls--> `game_rows()`  [EXTRACTED]
  dashboard_v4.py → src/chess_data.py
- `archived_rows()` --calls--> `legacy_csv_game_ids()`  [EXTRACTED]
  dashboard_v4.py → src/chess_data.py
- `archived_rows()` --calls--> `parse_pgn_headers()`  [EXTRACTED]
  dashboard_v4.py → src/chess_data.py
- `main()` --calls--> `ChessComClient`  [EXTRACTED]
  src/import_games.py → src/chess_data.py

## Import Cycles
- None detected.

## Communities (6 total, 0 thin omitted)

### Community 0 - "chess_data.py"
Cohesion: 0.13
Nodes (18): _chesscom_game_url_id(), connect(), _count_mainline_plies(), export_json(), export_pgn(), _game_from_headers(), game_rows(), import_pgn() (+10 more)

### Community 1 - "dashboard_v4.py"
Cohesion: 0.11
Nodes (12): _api_signature(), _archive_signature(), archived_rows(), game_label(), load(), _outcome_from_api(), parse_pgn_headers(), find_col() (+4 more)

### Community 2 - "engine_analysis.py"
Cohesion: 0.14
Nodes (7): analyze_pgn(), _material_points(), missed_mate_opportunity(), classify_cpl(), comparable_cpl(), move_accuracy(), mover_cpl()

### Community 3 - "StorageAndNetworkTests"
Cohesion: 0.15
Nodes (5): ChessComClient, _JsonResponse, FakeResponse, FakeSession, StorageAndNetworkTests

### Community 4 - "test_core.py"
Cohesion: 0.16
Nodes (7): longest_streak(), monthly_summary(), prepare_filters(), rating_summary(), result_summary(), EngineCliTests, MetricTests

### Community 5 - "analyze_games.py"
Cohesion: 0.18
Nodes (7): _atomic_json(), _enrich_summary(), main(), _mean(), _write_checkpoint(), legacy_csv_game_id(), legacy_csv_game_ids()

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `analyze_pgn()` connect `engine_analysis.py` to `chess_data.py`, `analyze_games.py`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Should `chess_data.py` be split into smaller, more focused modules?**
  _Cohesion score 0.12912912912912913 - nodes in this community are weakly interconnected._
- **Why does `connect()` connect `chess_data.py` to `dashboard_v4.py`, `engine_analysis.py`, `StorageAndNetworkTests`, `test_core.py`, `analyze_games.py`?**
  _High betweenness centrality (0.046) - this node is a cross-community bridge._
- **Should `dashboard_v4.py` be split into smaller, more focused modules?**
  _Cohesion score 0.1051693404634581 - nodes in this community are weakly interconnected._
- **Why does `ChessComClient` connect `StorageAndNetworkTests` to `chess_data.py`, `test_core.py`?**
  _High betweenness centrality (0.043) - this node is a cross-community bridge._
- **Should `engine_analysis.py` be split into smaller, more focused modules?**
  _Cohesion score 0.14 - nodes in this community are weakly interconnected._