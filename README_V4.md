# Chess Analytics V4

Independent post-game analysis dashboard built around the CSV data already in `data/`.
The original `analysis.csv` and `moves_analysis.csv` remain the primary source for
engine classifications; the application does not claim those metrics are Chess.com
official values. Data management also supports local PGN files and Chess.com's
documented, read-only PubAPI. The tool is independent and is not affiliated with
Chess.com. The client follows the [official PubAPI documentation](https://www.chess.com/news/view/published-data-api).

## Setup and launch

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements_v4.txt
python -m src.process_analysis_v4
streamlit run dashboard_v4.py
```

The provided `data/analysis.csv` and `data/moves_analysis.csv` are already available
for the dashboard. The local archive is stored in `data/chess_analytics.sqlite3` and
can be exported to PGN or JSON. This workspace has imported 14,548 unique public
Chess.com games for `effetto_magnus`. The dashboard links these games to existing
CSV analysis by PGN identity and, for 41 older header variants, by a unique match on
date, color, result, ratings and time control. Depth-8 Stockfish batch progress is
saved in `data/engine_analysis_index.json`; its result count is the current checkpoint
coverage and changes as analysis continues. The dashboard/CLI prefill a username when at least 80% of analyzed
player-side move rows agree on one account; override it with `CHESSCOM_USERNAME`,
`--username`, or the dashboard field. Set
`CHESS_ANALYTICS_DB` to use a different SQLite path, and `CHESS_TIMEZONE` (IANA
name) to set the dashboard's default calendar timezone.

## Opening names

Each game's opening is named from the best source available, in this order:
the PGN `[Opening]` tag; a local ECO table `data/openings/eco_names.tsv` (optional,
columns `eco` and `name`, used only where a code has a single base name); the
Chess.com opening URL (approximate, because a URL cannot show hyphens inside a name);
and finally the explicit label `Unknown`. The ECO code is always kept in its own
column and is never used as a name. The `naming_source` column records which source
was used.

After updating the code, run `python -m src.process_analysis_v4` again so that the
processed CSVs use this rule. Older outputs are displayed with `Unknown` until then.
The repository does not include a naming table; add one from a source whose licence
you have checked before relying on it.

## Use games already downloaded

Import an existing PGN without network access:

```bash
python -m src.import_games --username YOUR_USERNAME --pgn /path/to/games.pgn
```

When `python-chess` is installed, the importer validates and parses PGN mainlines
with that library. If it is unavailable, a standard-library fallback extracts
headers and estimates mainline ply counts but does not validate move legality or
variation structure. `python-chess` is required for Stockfish analysis.

Or upload a PGN in the dashboard's “Local archive” section. Duplicate games are
upserted by Chess.com game URL/UUID (or a deterministic PGN/header digest), and the
local archive is updated without recalculating engine analysis.

After an existing batch finishes, normalize opening, ECO code and termination tags
from already stored PGNs without using the network. This also removes the older
duplicate PGN copy from each JSON metadata blob; the PGN remains stored once in its
dedicated database column and is rejoined by full-record exports:

```bash
python -m src.import_games --backfill-pgn-metadata
```

If some games are missing, fetch all monthly archives available from the public API:

```bash
export CHESSCOM_USERNAME=YOUR_USERNAME
python -m src.import_games --fetch
```

The fetch includes the monthly archives and public player stats. Requests are serial, retried on transient failures and rate limits, include a
descriptive User-Agent, and use a 12-hour local response cache with ETag and
Last-Modified revalidation. The import uses documented public endpoints only; it
does not scrape or authenticate. Public data may omit private games and can be
served stale by the API cache. No actual API fetch is needed to use already-imported
local files.

Export:

```bash
python -m src.import_games --export-pgn data/archive-export.pgn --export-json data/archive-export.json
```

## Optional Stockfish analysis

Install a Stockfish UCI executable separately, then analyze concluded supported
games from the local SQLite archive. Standard chess, Chess960 and Odds Chess PGNs
with recorded positions are supported; other rule sets are skipped:

```bash
python -m src.analyze_games --engine /path/to/stockfish --username YOUR_USERNAME --depth 14
# Or bound runtime per position instead of using depth:
python -m src.analyze_games --engine /path/to/stockfish --username YOUR_USERNAME --time 0.25
# Resume a checkpoint with the same settings, or limit work to chosen stored IDs:
python -m src.analyze_games --engine /path/to/stockfish --username YOUR_USERNAME --depth 8 --resume --game-id GAME_ID
```

To resume this workspace's full-archive depth-8 batch, activate the local virtual
environment and use the same engine path and username recorded in the checkpoint:

```bash
python -m src.analyze_games --engine /opt/homebrew/Cellar/stockfish/19/bin/stockfish \
  --username effetto_magnus --depth 8 --resume --checkpoint-every 1000
```

`Ctrl-C` saves a checkpoint and prints a resume hint. Each completed game is also
stored in SQLite, so restarting can reuse cached games after the last JSON checkpoint.
For long archives, a larger checkpoint interval avoids rewriting the large batch JSON
too often.

The persistent SQLite engine cache is keyed by PGN, engine path, settings and
analysis version, so a repeat run reuses results. Batch JSON is checkpointed
atomically every ten games by default; `--checkpoint-every` changes that interval.
`--resume` requires a checkpoint created with the same engine path, username, depth
or time limit; use a different output file when changing settings. The database cache lets interrupted single games continue without
recalculating completed games. Each move stores the engine's best move, post-move
evaluation, mover-relative evaluation loss (CPL), operational phase and a large-swing
flag, plus the FEN before each move. Inaccuracy/mistake/blunder thresholds are
50/100/200 centipawns of loss. A lost-winning-advantage event means the player's
evaluation is at least +200 cp before their move and falls below +200 cp after it;
position labels use ±200 cp from the mover's perspective.
Material-state summaries use standard point values (pawn 1, knight/bishop 3, rook 5,
queen 9) and are reported from the configured player's perspective.
The independent move score is `100 * exp(-max(0, CPL)/100)`, averaged over the
selected player's analyzed moves that have both engine scores; missing scores stay
unknown instead of being counted as error-free. Mate scores are converted to +/-100000 cp before
calculating loss. Values with absolute CPL at or above 10000 cp remain in move-level
details and error counts but are omitted from arithmetic mean CPL. The cutoff handles
mate/forced-win sentinels in the supplied source data; extremely large genuine swings
would be excluded too, so it is a documented robust-summary rule, not a claim that
every excluded value is a forced mate. The score is exponential, not logistic, and is
not the proprietary accuracy formula of Chess.com.

Older analysis files may have stored the position after a move in the `fen_before`
field. Re-running with `--resume` repairs this display field by replaying the saved
PGNs; it preserves existing engine evaluations and does not rerun Stockfish. SQLite
cache hits are repaired the same way.

The phase rule is intentionally simple: opening through full move 10, otherwise
endgame when non-pawn material is at most 13 points (queen 9, rook 5, bishop/knight
3), else middlegame. The “exposed piece” flag only says that the just-moved piece is
on a square attacked by an opponent piece; it does not prove a tactical loss. This
tool does not label forks, pins, or all missed tactics as if those had been reliably
solved. A missed-mate flag is emitted only when the engine reports a forced mate for
the mover before their move and no longer reports it after; it is not a general
tactics detector. Analysis is only for concluded games and must not be used as
assistance in live games.

Chess960 uses python-chess's automatic UCI Chess960 handling. Odds Chess is evaluated
with the recorded PGN setup position and standard movement rules; these scores do not
model any external odds or special game-specific rules beyond that position.

For the supplied move CSV, phase labels and classifications are inherited from that
source analysis; their original phase algorithm is not present in this repository.
They are surfaced as provided, while the optional engine analysis uses the explicit
phase rule above.

## What is available and what is not

- The dashboard adds filters, rating/accuracy trends where source data exists,
  opening and phase summaries, a searchable game table, evidence-based summary
  downloads, and imported public/PGN games. Imported archive rows have no engine
  accuracy unless the source API itself supplies a per-player accuracy. Opening and
  time-control summaries show W/D/L shares over games with a known result; opening
  quality distinguishes supplied first-phase metrics from Stockfish's independently
  analyzed first ten moves. Best/worst opening rankings use the selected minimum
  sample size and are descriptive, without opponent-strength adjustment.
- The dashboard caches compact archive summaries for up to 60 seconds and reads the
  engine index once per checkpoint version. The full move-level JSON download is
  prepared only after an explicit request; detailed results remain in the local file
  and SQLite cache.
- The original CSV data is preserved. The SQLite archive is an additional source and
  can be refreshed from the public API with `python -m src.import_games --fetch`.
- The supplied CSV move analysis covers 14,121 games. Independent Stockfish coverage
  is the result count in `data/engine_analysis_index.json`, out of the 14,548 archived
  games; the current batch is partial and can be extended with the resumable command
  above. The dashboard loads detailed engine moves from
  SQLite only when a reviewed game is selected.
- PGN `ECO`, opening URL, termination and UTC start tags are parsed independently
  from the API's opening URL and end timestamp. The game explorer provides a
  move-by-move view from the supplied analysis files; an evaluation trajectory and
  best-move comparisons appear after Stockfish results are generated. It compares
  two consecutive equal-length date windows and supports direct review of each
  game; older game IDs can be found through the explorer search.
- Historical rating is only as complete as recorded per-game ratings in available
  CSV/PGN/API data; these are official recorded ratings, not a reconstructed daily
  rating history. Ratings and termination details can be missing for some PGNs.
- Opening labels/ECO use the existing source or PGN metadata. No external opening
  book is bundled, and theory exit is not reported without a licensed reference.
- The public API cannot return private information. Proprietary platform metrics,
  account-only analysis and exact premium labels are unavailable here.
- Full-depth Stockfish analysis can be expensive: runtime scales with game count,
  ply count and the configured depth/time. Start with a modest setting. API download
  and batch analysis are terminal commands, so they do not block Streamlit's UI.

## Tests

Run deterministic unit tests (the network is mocked):

```bash
python -m unittest discover -s tests -v
```
