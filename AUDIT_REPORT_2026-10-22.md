# Repository audit — Chess.com-Stats

Audit date: 2026-10-22
Audited branch: main
Audited tree SHA: 2eff0767165c467fa582d0de142d7b79d4bf6baf

## Scope and limitations
Static review of dashboard_v4.py, src/process_analysis_v4.py, src/chess_data.py, src/metrics.py, src/engine_analysis.py, src/analyze_games.py, README_V4.md, tests/test_core.py and committed aggregates. Tests were not executed locally. The full large CSV/JSON files were not downloaded, so data-distribution findings requiring full raw data validation are identified below.

## Executive summary
The highest-priority defect is opening nomenclature: the committed opening aggregate has ECO identifiers such as B12 and D02 in the opening_name column. Other top priorities are PGN termination misclassification; unclear denominators/coverage in headline statistics; a large disparity between SourceACPL and PlayerCPL that needs schema verification; and opening rankings based on raw win rate without uncertainty or opponent-strength adjustment.

## Findings

### P1 — Opening names are not canonicalized
Files: src/process_analysis_v4.py (opening_from_url, opening_name, opening_stats_v4.csv).
Opening labels are derived from ECOUrl by replacing hyphens/underscores, then filled from PGN Opening or raw source. No opening-book lookup or canonical-name/variant validation is present. The committed opening aggregate contains opening_name values B12, D02, B10, and other ECO-like identifiers.
Impact: filters, charts, rankings and reports can show codes/slugs instead of names and duplicate rows for spelling variants.
Fix: define canonical opening_name, variant_name, eco_code, naming_source and classification_confidence. Prefer validated PGN Opening tags, then a versioned opening taxonomy/position matcher, then explicit Unknown. Keep ECO code separate. Rebuild aggregates.
Acceptance: no ECO-only value in opening_name except explicit fallback; aliases normalize deterministically; tests cover missing/invalid tags, URL slugs, ECO codes and ambiguous positions.

### P1 — Imported PGN results can misstate termination cause
Files: src/chess_data.py (_result_for, _game_from_headers); dashboard_v4.py (_outcome_from_api).
_result_for derives checkmated for the losing side from Result 1-0/0-1 regardless of Termination. For PGNs won by resignation or timeout, outcome can be correct while result_type/termination is false. Dashboard API-result classification also uses substring matching and needs tests against PubAPI result values.
Fix: store outcome separately from termination. Only derive termination with explicit evidence; mark missing termination Unknown, not checkmate.
Acceptance: PGNs with resignation, timeout, abandonment, checkmate, stalemate and agreed draw have correct outcome and separate termination labels.

### P1 — Headline rates do not disclose known-result denominator
File: dashboard_v4.py (headline metrics); src/metrics.py (result_summary).
Headline rates are derived from Boolean win/draw/loss fields while Games includes all rows. Imported games with unknown outcomes use nullable booleans, so coverage and denominators are unclear.
Fix: derive rates from result_summary; show total games and known-result games (N), with Unknown separate.
Acceptance: percentages use one known-result denominator and sum to 100% up to rounding when all W/D/L are known.

### P1 — SourceACPL semantics appear inconsistent with PlayerCPL
Files: src/process_analysis_v4.py and data/processed_v4/time_control_stats_v4.csv.
SourceACPL is a direct mean of source acpl, while PlayerCPL is recomputed from selected-player move rows excluding values whose absolute CPL >= 10000. In the committed aggregate, common Bullet SourceACPL is about 1086 versus PlayerCPL about 72. This does not prove a bug without the original schema, but it requires validation before interpretation.
Fix: document source-column semantics and sample sizes; compare on the same player-side/game population with consistent sentinel treatment. Never put non-comparable SourceACPL and engine CPL on the same scale.
Acceptance: semantics verified, aggregation tests added and sources explicit in labels.

### P1 — Opening ranking is statistically fragile
File: dashboard_v4.py (opening ranking).
Best/worst opening tables sort by raw WinRate and Games, with a configurable minimum as low as five games, and explicitly do not adjust for opponent rating or statistical uncertainty.
Fix: show score rate (wins + 0.5 draws)/known results, known-result N, confidence interval or shrinkage, and opponent ratings. Keep raw W/D/L descriptive and avoid treating small samples as definitive.
Acceptance: rankings communicate uncertainty and are stable under reasonable minimum samples.

### P1 — Dataset and engine coverage are not surfaced consistently
Files: dashboard_v4.py and README_V4.md. The README documents 14,121 games in the source analysis and 14,548 in the imported archive; Stockfish coverage varies by checkpoint.
Fix: prominently show source-analysis coverage, archive coverage, known-result N and Stockfish coverage/settings, plus checkpoint timestamp. Add data-lineage metadata to generated outputs.
Acceptance: headline metrics identify their contributing sample and provenance.

### P2 — Date filter uses stored UTC date despite configurable timezone
File: dashboard_v4.py (timezone conversion and date range filter).
Calendar labels are converted to a chosen timezone, while the date filter uses g.date.dt.date. For games around midnight, local date and filtered records may disagree.
Fix: define filter semantics; recommended local-date filter on start_time_utc converted to selected timezone.
Acceptance: tests cover midnight and daylight-saving boundaries.

### P2 — Heuristic archive/source/engine linking needs match-quality reporting
File: dashboard_v4.py (source IDs, URLs and signature mapping).
Matching attempts IDs, URLs and a signature (date, color, result, ratings, time control), with fallback IDs if no confident match exists. Ambiguous records can cause metrics to attach to the wrong game.
Fix: record link_method, link_confidence and candidate count; don't join engine data through ambiguous identifiers; expose matched/unmatched/ambiguous counters.
Acceptance: synthetic collisions do not silently merge and mappings are auditable.

### P2 — Archive exceptions can silently hide data
File: dashboard_v4.py (archived_rows and profile stats loading).
archived_rows catches all exceptions and returns [], while profile stats errors are also ignored.
Fix: preserve app availability but show an actionable warning and log exception type; distinguish empty archive from a failed load.
Acceptance: failure is visible in diagnostic UI/logs.

### P2 — Stockfish metrics can be mixed across settings
Files: src/engine_analysis.py and README_V4.md.
The independent accuracy is an exponential function of CPL, not Chess.com's proprietary metric, and depends on engine settings. Combining runs with different depths/time limits may confound comparisons.
Fix: store/validate engine path/version, depth/time and analysis version per game; only aggregate comparable settings or stratify them.
Acceptance: metric is explicitly independent; coverage/settings are visible and mixed settings aren't silently aggregated.

### P2 — Skipped game analyses lack a durable failure ledger
File: src/analyze_games.py.
Per-game exceptions increment a skipped count and print to terminal, but failure cause isn't persisted per game.
Fix: persist status keyed by game ID (completed/skipped/failed, reason, settings and timestamp).
Acceptance: after resume, users can inspect unsupported/failed games without terminal logs.

### P2 — Generated files and bytecode are tracked
The repository tree contains .DS_Store, __pycache__/*.pyc, graphify-out/cache artifacts and a 34 MB data/engine_analysis_index.json.
Fix: add ignore rules for bytecode/editor files and decide which reproducible outputs belong in version control. Prefer small fixtures for tests unless large snapshots are intentional.
Acceptance: fresh clone works without tracked local caches/bytecode and artifact policy is documented.

### P2 — Test suite doesn't visibly cover critical dashboard transforms
File: tests/test_core.py.
Existing tests cover helper metrics, metadata, IDs, cache and resume behavior. No obvious regression tests cover opening canonicalization, termination inference, headline result coverage, timezone filtering or ambiguous game linking.
Fix: extract pure transforms from Streamlit UI and add fixtures for these cases.
Acceptance: every P1 finding has a reproducing test before its fix merges.

### P3 — Dashboard information architecture is dense and mixed-language
File: dashboard_v4.py.
Nine tabs combine overlapping statistics and English metric labels with Italian explanations. Final report is plain text and has limited provenance/coverage detail.
Fix: add an executive overview, consistent language, metric glossary, expandable technical details and structured exports.

### P3 — Report construction is brittle
File: dashboard_v4.py (summary report construction).
The report string uses a conditional expression within implicit string concatenation; output fields depend on whether engine accuracy is present and missing values are not uniformly handled.
Fix: build report sections incrementally; format missing values as N/A and always include source/Stockfish coverage.
Acceptance: stable report schema with or without engine results.

## Recommended implementation order
1. Add characterization tests; verify original ACPL schema.
2. Normalize opening metadata, add provenance fields, regenerate aggregates and improve opening UI/rankings.
3. Correct outcome/termination separation; fix W/D/L denominator and coverage panel.
4. Add confident linking and persisted failure statuses.
5. Align local-date filtering and metric/source labels.
6. Clean generated artifacts, improve report generation and simplify dashboard.

## Confidence
High confidence: ECO-like values in opening_name; implementation derives labels from tags/URL/raw values; PGN termination inference; raw win-rate ranking; tracked generated bytecode and large checkpoint.
Needs data/schema validation: why SourceACPL is so high and actual error rate of heuristic ID fallback. Code was not modified and tests were not executed in this audit.
