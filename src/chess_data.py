"""Public game acquisition and reproducible local storage.

Uses only Chess.com's documented PubAPI and standard PGN imports. No login,
private endpoints, scraping, or live-game assistance is involved.
"""
from __future__ import annotations

import hashlib
import json
import os
import csv
from collections import Counter
import re
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import quote, urlsplit

import requests

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("CHESS_ANALYTICS_DB", ROOT / "data" / "chess_analytics.sqlite3"))
API_ROOT = "https://api.chess.com/pub"


def _chesscom_game_url_id(url):
    parsed = urlsplit(str(url or ""))
    parts = [part for part in parsed.path.split("/") if part]
    host = (parsed.hostname or "").lower()
    valid_host = host == "chess.com" or host.endswith(".chess.com")
    if valid_host and len(parts) >= 3 and parts[-3] == "game" and parts[-2] in ("live", "daily"):
        return parts[-1] if parts[-1] not in ("-", "?") else None
    return None


def stable_game_id(game: dict) -> str:
    """Prefer the API UUID; use a deterministic PGN/header digest otherwise."""
    if game.get("uuid") and str(game["uuid"]).strip() not in ("-", "?"):
        return str(game["uuid"]).strip()
    if game.get("url") and str(game["url"]).strip() not in ("-", "?"):
        game_url_id = _chesscom_game_url_id(game["url"])
        if game_url_id: return game_url_id
    pgn = str(game.get("pgn", ""))
    if pgn:
        return hashlib.sha256(pgn.strip().encode()).hexdigest()[:24]
    return hashlib.sha256(json.dumps(game, sort_keys=True, default=str).encode()).hexdigest()[:24]


def legacy_csv_game_id(pgn: str):
    """Reproduce the V2/V3 CSV key so imported PGNs merge with existing rows."""
    aliases = legacy_csv_game_ids(pgn)
    return aliases[0] if aliases else None


def parse_pgn_headers(pgn: str) -> dict:
    """Read PGN tag pairs without parsing movetext or requiring python-chess."""
    headers = {}
    for line in str(pgn or "").splitlines():
        match = re.match(r'^\[([^\s]+)\s+"(.*)"\]\s*$', line.strip())
        if match:
            headers[match.group(1)] = match.group(2).replace('\\"','"').replace('\\\\','\\')
        elif headers and not line.strip():
            break
    return headers


def legacy_csv_game_ids(pgn: str):
    """Return exact and normalized-site aliases for old CSV identity variants."""
    return legacy_csv_game_ids_from_headers(parse_pgn_headers(pgn))


def legacy_csv_game_ids_from_headers(headers: dict):
    """Return legacy CSV identity aliases from already-parsed PGN headers."""
    if not all(headers.get(k) for k in ("Site", "Date", "White", "Black", "Result")):
        return []
    sites=[headers.get('Site','')]
    if 'chess.com' in headers.get('Site','').lower() and headers.get('Site') != 'Chess.com':
        sites.append('Chess.com')
    aliases=[]
    for site in sites:
        values=[site]+[str(headers.get(k,'')) for k in
                       ("Date", "Round", "White", "Black", "Result", "UTCDate", "UTCTime")]
        raw="\n".join(values)
        value=hashlib.sha1(raw.encode()).hexdigest()[:16]
        if value not in aliases: aliases.append(value)
    return aliases


def infer_local_username(path: Path | None = None):
    """Infer the active account from analyzed move rows, if one dominates clearly.

    Only rows whose move color agrees with player_color contribute. Ties and
    ambiguous sources return None so callers can ask for an explicit name.
    """
    path = path or (ROOT / "data" / "moves_analysis.csv")
    if not path.exists(): return None
    counts = Counter()
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"color", "player_color", "white", "black"}
        if not required.issubset(reader.fieldnames or []): return None
        for row in reader:
            color = (row.get("color") or "").strip().lower()
            player_color = (row.get("player_color") or "").strip().lower()
            if color != player_color or color not in ("white", "black"): continue
            username = (row.get(color) or "").strip()
            if username: counts[username.lower()] += 1
    if not counts: return None
    best = counts.most_common()
    if len(best) > 1 and best[0][1] == best[1][1]: return None
    # Require a clear majority to avoid silently selecting one player from a mixed archive.
    if best[0][1] / sum(counts.values()) < 0.8: return None
    return best[0][0]


class ChessComClient:
    def __init__(self, username: str, contact: str = "", session=None,
                 timeout: float = 20, retries: int = 4, sleep=time.sleep, db_path: Path = DB_PATH):
        name = username.strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]+", name):
            raise ValueError("Username Chess.com non valido")
        self.username = name
        self.timeout, self.retries, self.sleep = timeout, retries, sleep
        self.session = session or requests.Session()
        self.db_path = db_path
        contact_part = f"; contact: {contact}" if contact else ""
        self.session.headers.update({"User-Agent": f"ChessArchiveAnalytics/1.0 (username: {name}{contact_part})",
                                     "Accept": "application/json"})

    def get(self, url: str):
        # PubAPI responses are cached locally for 12h and conditionally revalidated
        # afterwards. This both avoids needless work and follows the API's cache model.
        cache_path = self.db_path
        cached = None
        if cache_path.exists():
            with connect(cache_path) as conn:
                cached = conn.execute("SELECT body, etag, modified, fetched_at FROM api_cache WHERE url=?", (url,)).fetchone()
        if cached:
            fetched = datetime.fromisoformat(cached[3])
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if age < 12 * 3600:
                return _JsonResponse(cached[0])
        request_headers = {}
        if cached:
            if cached[1]: request_headers["If-None-Match"] = cached[1]
            if cached[2]: request_headers["If-Modified-Since"] = cached[2]
        last = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, headers=request_headers)
                if response.status_code == 304:
                    if cached:
                        with connect(cache_path) as conn:
                            conn.execute("UPDATE api_cache SET fetched_at=? WHERE url=?", (datetime.now(timezone.utc).isoformat(), url))
                        return _JsonResponse(cached[0])
                    return None
                if response.status_code == 429 or response.status_code >= 500:
                    if attempt < self.retries:
                        retry_after = response.headers.get("Retry-After")
                        delay = None
                        if retry_after:
                            try:
                                delay = max(0.0, float(retry_after))
                            except ValueError:
                                try:
                                    from email.utils import parsedate_to_datetime
                                    delay = max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                                except (TypeError, ValueError, OverflowError):
                                    delay = None
                        self.sleep(delay if delay is not None else min(2 ** attempt, 30))
                        continue
                response.raise_for_status()
                with connect(cache_path) as conn:
                    conn.execute("INSERT INTO api_cache(url,body,etag,modified,fetched_at) VALUES(?,?,?,?,?) "
                                 "ON CONFLICT(url) DO UPDATE SET body=excluded.body,etag=excluded.etag,modified=excluded.modified,fetched_at=excluded.fetched_at",
                                 (url, response.text, response.headers.get("ETag"), response.headers.get("Last-Modified"), datetime.now(timezone.utc).isoformat()))
                return response
            except requests.RequestException as exc:
                last = exc
                if attempt < self.retries:
                    self.sleep(min(2 ** attempt, 30))
        if last:
            raise last
        response.raise_for_status()

    def games(self) -> Iterable[dict]:
        archives_url = f"{API_ROOT}/player/{quote(self.username)}/games/archives"
        response = self.get(archives_url)
        archives = (response.json() if response is not None else {}).get("archives", [])
        for archive_url in archives:
            response = self.get(archive_url)
            if response is None:
                continue
            payload = response.json()
            for game in payload.get("games", []):
                game = dict(game)
                game["_archive"] = archive_url
                yield game

    def player_stats(self):
        response = self.get(f"{API_ROOT}/player/{quote(self.username)}/stats")
        return response.json() if response is not None else {}


def connect(path: Path = DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS games (
        game_id TEXT PRIMARY KEY, username TEXT, source TEXT NOT NULL,
        fetched_at TEXT NOT NULL, end_time INTEGER, rated INTEGER,
        time_class TEXT, time_control TEXT, rules TEXT, url TEXT,
        white TEXT, black TEXT, white_rating INTEGER, black_rating INTEGER,
        white_result TEXT, black_result TEXT, eco TEXT, opening TEXT,
        pgn TEXT, metadata_json TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS games_end_time ON games(end_time)")
    conn.execute("CREATE TABLE IF NOT EXISTS api_cache (url TEXT PRIMARY KEY, body TEXT NOT NULL, etag TEXT, modified TEXT, fetched_at TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_cache (cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS player_stats (username TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, stats_json TEXT NOT NULL)")
    return conn


def save_games(games: Iterable[dict], username: str, source: str = "pubapi", db_path: Path = DB_PATH) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for game in games:
        white, black = game.get("white") or {}, game.get("black") or {}
        headers=game.get("_headers") or parse_pgn_headers(game.get("pgn", ""))
        metadata=dict(game)
        # PGN is already stored in its own indexed column. Keep it out of the
        # JSON metadata blob to avoid doubling archive size; game_rows/export_json
        # join it back when callers request complete game records.
        metadata.pop("pgn",None)
        if headers:
            if not metadata.get("_headers"): metadata["_headers"]=headers
            for field,tag in (("opening","Opening"),("termination","Termination")):
                if not metadata.get(field) and headers.get(tag): metadata[field]=headers[tag]
            if not metadata.get("eco") and headers.get("ECO"): metadata["eco"]=headers["ECO"]
            if not metadata.get("eco_code") and headers.get("ECO"): metadata["eco_code"]=headers["ECO"]
        rows.append((stable_game_id(game), username.lower(), source, now, game.get("end_time"),
                     int(bool(game.get("rated"))) if game.get("rated") is not None else None,
                     game.get("time_class"), game.get("time_control"), game.get("rules"), game.get("url"),
                     white.get("username"), black.get("username"), white.get("rating"), black.get("rating"),
                     white.get("result"), black.get("result"), game.get("eco") or headers.get("ECO"),
                     game.get("opening") or headers.get("Opening"), game.get("pgn"),
                     json.dumps(metadata, ensure_ascii=False, sort_keys=True)))
    with connect(db_path) as conn:
        before = conn.total_changes
        conn.executemany("""INSERT INTO games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(game_id) DO UPDATE SET
          username=excluded.username, source=excluded.source, fetched_at=excluded.fetched_at,
          end_time=excluded.end_time, rated=excluded.rated, time_class=excluded.time_class,
          time_control=excluded.time_control, rules=excluded.rules, url=excluded.url,
          white=excluded.white, black=excluded.black, white_rating=excluded.white_rating,
          black_rating=excluded.black_rating, white_result=excluded.white_result,
          black_result=excluded.black_result, eco=excluded.eco, opening=excluded.opening,
          pgn=excluded.pgn, metadata_json=excluded.metadata_json
          WHERE games.metadata_json != excluded.metadata_json
             OR games.pgn IS NOT excluded.pgn""", rows)
        return conn.total_changes - before


def import_pgn(path: Path, username: str, db_path: Path = DB_PATH) -> int:
    """Import PGNs; validate moves with python-chess when installed.

    Without python-chess, a conservative header/movetext fallback preserves records
    and metadata but does not validate legality or variation structure.
    """
    try:
        import chess.pgn
    except ImportError:
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            parsed_games = (_simple_pgn_records(stream))
            games = [_game_from_headers(headers, pgn, _count_mainline_plies(pgn))
                     for headers, pgn in parsed_games]
    else:
        games = []
        with path.open("r", encoding="utf-8-sig", errors="replace") as stream:
            while (parsed := chess.pgn.read_game(stream)) is not None:
                games.append(_game_from_headers(dict(parsed.headers), str(parsed),
                                                 sum(1 for _ in parsed.mainline_moves())))
    return save_games(games, username, "pgn", db_path)


def _game_from_headers(h, pgn, move_count):
    time_control = h.get("TimeControl", "")
    if re.fullmatch(r"\d+/\d+", time_control.strip()):
        time_class = h.get("TimeClass") or "daily"
    else:
        try:
            initial, _, increment = time_control.partition("+")
            start_seconds, increment = int(initial), int(increment or 0)
            effective = start_seconds + 40 * increment
            time_class = h.get("TimeClass") or ("bullet" if effective < 180 else "blitz" if effective < 600 else "rapid" if effective < 1500 else "classical")
        except (TypeError, ValueError):
            time_class = h.get("TimeClass")
    link = h.get("Link")
    if not _chesscom_game_url_id(link):
        site = h.get("Site")
        link = site if _chesscom_game_url_id(site) else None
    rated_tag=str(h.get("Rated") or "").strip().lower()
    if rated_tag in {"rated","true","yes","1"}:
        rated=True
    elif rated_tag in {"unrated","false","no","0"}:
        rated=False
    else:
        event=str(h.get("Event", "")).lower()
        if re.search(r"\bunrated\b", event): rated=False
        elif re.search(r"\brated\b", event): rated=True
        else: rated=None
    return {"pgn": pgn, "url": link, "end_time": _pgn_epoch(h),
            "time_control": h.get("TimeControl"), "time_class": time_class,
            "rules": h.get("Variant", "chess").lower(),
            "white": {"username": h.get("White"), "rating": _int_or_none(h.get("WhiteElo")),
                      "result": _result_for(h.get("Result"), True)},
            "black": {"username": h.get("Black"), "rating": _int_or_none(h.get("BlackElo")),
                      "result": _result_for(h.get("Result"), False)},
            "rated": rated,
            "eco": h.get("ECO"), "eco_url": h.get("ECOUrl"),
            "opening": h.get("Opening"), "termination": h.get("Termination"),
            "moves": move_count, "_headers": dict(h)}


def _simple_pgn_records(stream):
    """Yield header maps and record text using PGN's header-block boundaries."""
    headers, lines, in_movetext = {}, [], False
    tag = re.compile(r'^\[([^\s]+)\s+"(.*)"\]\s*$')
    for raw in stream:
        line = raw.rstrip("\r\n")
        match = tag.match(line)
        if match:
            if in_movetext and headers:
                yield headers, "\n".join(lines).strip()
                headers, lines, in_movetext = {}, [], False
            key, value = match.groups()
            headers[key] = value.replace('\\"', '"').replace('\\\\', '\\')
            lines.append(line)
        elif headers:
            if not line.strip() and not in_movetext:
                in_movetext = True
            lines.append(line)
    if headers:
        yield headers, "\n".join(lines).strip()


def _count_mainline_plies(pgn):
    text = re.sub(r"(?m)^\[[^\n]*\]\s*$", " ", pgn)
    text = re.sub(r"\{[^}]*\}|;[^\n]*", " ", text)
    # Remove nested parenthesized variations without mistaking them for mainline.
    result, depth = [], 0
    for char in text:
        if char == "(": depth += 1
        elif char == ")" and depth: depth -= 1
        elif depth == 0: result.append(char)
    tokens = "".join(result).split()
    skip_results = {"1-0", "0-1", "1/2-1/2", "*"}
    moves = [token for token in tokens
             if token not in skip_results and not re.fullmatch(r"\d+\.(?:\.\.)?", token)
             and not token.startswith("$") and not token.startswith("[%")]
    return len(moves)


def _int_or_none(value):
    try: return int(value)
    except (TypeError, ValueError): return None


def _result_for(result, white):
    if result == "1/2-1/2": return "agreed"
    if result == "1-0": return "win" if white else "checkmated"
    if result == "0-1": return "checkmated" if white else "win"
    return "unknown"


def _pgn_epoch(headers):
    try:
        dt = datetime.strptime(headers.get("UTCDate", headers.get("Date")) + " " +
                               headers.get("UTCTime", "00:00:00"), "%Y.%m.%d %H:%M:%S")
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError): return None


def _atomic_text_export(path: Path, write_content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,temp_name=tempfile.mkstemp(prefix=f".{path.name}.",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="") as stream:
            count=write_content(stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temp_name,path)
        return count
    except BaseException:
        try: os.unlink(temp_name)
        except FileNotFoundError: pass
        raise


def export_json(path: Path, db_path: Path = DB_PATH):
    """Stream a complete JSON archive to an atomic replacement file."""
    def write(stream):
        count=0
        stream.write("[\n")
        with connect(db_path) as conn:
            for metadata_json,pgn in conn.execute(
                    "SELECT metadata_json,pgn FROM games ORDER BY end_time,game_id"):
                if count: stream.write(",\n")
                record=json.loads(metadata_json)
                if pgn is not None: record["pgn"]=pgn
                json.dump(record,stream,ensure_ascii=False,indent=2)
                count+=1
        stream.write("\n]\n")
        return count
    return _atomic_text_export(path,write)


def export_pgn(path: Path, db_path: Path = DB_PATH):
    """Stream all stored PGNs in stable order to an atomic replacement file."""
    def write(stream):
        count=0
        with connect(db_path) as conn:
            for (pgn,) in conn.execute(
                    "SELECT pgn FROM games WHERE pgn IS NOT NULL ORDER BY end_time,game_id"):
                if count: stream.write("\n\n")
                stream.write(pgn)
                count+=1
        if count: stream.write("\n")
        return count
    return _atomic_text_export(path,write)


def game_rows(db_path: Path = DB_PATH):
    with connect(db_path) as conn:
        rows=conn.execute("SELECT metadata_json,pgn FROM games ORDER BY end_time, game_id").fetchall()
    records=[]
    for metadata_json,pgn in rows:
        record=json.loads(metadata_json)
        if pgn is not None: record["pgn"]=pgn
        records.append(record)
    return records


def game_summary_rows(db_path: Path = DB_PATH):
    """Read lightweight archive rows without moving full PGNs into Python memory."""
    if not db_path.exists(): return []
    uri=db_path.resolve().as_uri()+"?mode=ro"
    conn=sqlite3.connect(uri,uri=True,timeout=5)
    try:
        try:
            rows=conn.execute("""SELECT json_remove(metadata_json,'$.pgn'),
                CASE WHEN json_type(metadata_json,'$._headers') IS NULL THEN pgn END
                FROM games ORDER BY end_time,game_id""").fetchall()
        except sqlite3.DatabaseError:
            # Older SQLite builds without JSON1 retain compatibility; this
            # fallback is intentionally slower but still returns small rows.
            rows=conn.execute("SELECT metadata_json,pgn FROM games ORDER BY end_time,game_id").fetchall()
    finally:
        conn.close()
    records=[]
    for metadata_json,pgn_fallback in rows:
        try: record=json.loads(metadata_json)
        except (TypeError,ValueError): continue
        record.pop("pgn",None)
        if not record.get("_headers") and pgn_fallback:
            headers=parse_pgn_headers(pgn_fallback)
            if headers: record["_headers"]=headers
        records.append(record)
    return records


def backfill_pgn_metadata(db_path: Path = DB_PATH) -> int:
    """Normalize PGN tags and remove duplicate PGN copies from metadata JSON."""
    updates=[]
    with connect(db_path) as conn:
        rows=conn.execute("SELECT game_id,pgn,eco,opening,metadata_json FROM games WHERE pgn IS NOT NULL").fetchall()
        for game_id,pgn,stored_eco,stored_opening,metadata_json in rows:
            try: metadata=json.loads(metadata_json)
            except (TypeError,ValueError): metadata={}
            headers=metadata.get('_headers') or parse_pgn_headers(pgn)
            opening=stored_opening or metadata.get('opening') or headers.get('Opening')
            eco=stored_eco or metadata.get('eco') or headers.get('ECO')
            termination=metadata.get('termination') or headers.get('Termination')
            changed=metadata.pop('pgn',None) is not None
            if opening and not metadata.get('opening'):
                metadata['opening']=opening; changed=True
            if eco and not metadata.get('eco'):
                metadata['eco']=eco; changed=True
            if headers.get('ECO') and not metadata.get('eco_code'):
                metadata['eco_code']=headers['ECO']; changed=True
            if termination and not metadata.get('termination'):
                metadata['termination']=termination; changed=True
            if headers and not metadata.get('_headers'):
                metadata['_headers']=headers; changed=True
            if opening!=stored_opening or eco!=stored_eco or changed:
                updates.append((eco,opening,json.dumps(metadata,ensure_ascii=False,sort_keys=True),game_id))
        if updates:
            conn.executemany("""UPDATE games SET
                eco=CASE WHEN eco IS NULL OR eco='' THEN ? ELSE eco END,
                opening=CASE WHEN opening IS NULL OR opening='' THEN ? ELSE opening END,
                metadata_json=? WHERE game_id=?""",updates)
    return len(updates)


def save_player_stats(username: str, stats: dict, db_path: Path = DB_PATH):
    with connect(db_path) as conn:
        conn.execute("INSERT INTO player_stats VALUES(?,?,?) ON CONFLICT(username) DO UPDATE SET fetched_at=excluded.fetched_at, stats_json=excluded.stats_json",
                     (username.lower(), datetime.now(timezone.utc).isoformat(), json.dumps(stats,ensure_ascii=False,sort_keys=True)))


def read_player_stats(username: str, db_path: Path = DB_PATH):
    if not db_path.exists(): return {}
    with connect(db_path) as conn:
        row=conn.execute("SELECT stats_json FROM player_stats WHERE username=?",(username.lower(),)).fetchone()
    return json.loads(row[0]) if row else {}


class _JsonResponse:
    """Small adapter so cached JSON behaves like requests.Response at call sites."""
    def __init__(self, text): self.text, self.status_code, self.headers = text, 200, {}
    def json(self): return json.loads(self.text)
