"""Opening nomenclature shared by the processing pipeline and the dashboard.

The name shown as ``opening_name`` comes from the most reliable source available,
in this order:

1. ``pgn_tag``      the PGN [Opening] tag, when it holds a real name (not an ECO code);
2. ``eco_table``    a local ECO-to-name table (optional, see ``load_eco_table``), used
                    only where one ECO code has a single base name;
3. ``chesscom_url`` the slug of the Chess.com ECOUrl. This is approximate: a slug cannot
                    tell a hyphen inside a name from a word break ("Nimzo-Indian");
4. ``none``         the explicit label ``Unknown``.

The ECO code and the opening family live in their own columns and are never used as
a name. Names are never invented: nothing outside the input data or the optional
table can appear as an opening name.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pandas as pd

UNKNOWN_NAME = 'Unknown'
UNKNOWN_FAMILY = 'Unknown / Unclassified'
FAMILY_BY_LETTER = {
    'A': 'A — Flank / English / Réti / Dutch / Benoni',
    'B': 'B — Sicilian / Caro-Kann / Modern',
    'C': 'C — French / Open Games / Ruy Lopez',
    'D': 'D — Queen’s Gambit / Slav / Grünfeld',
    'E': 'E — Indian Defences / Catalan / Nimzo-Indian / King’s Indian',
}
# Higher rank = more reliable name. Used when two sources describe the same game.
NAMING_RANK = {'pgn_tag': 4, 'eco_table': 3, 'chesscom_url': 2, 'legacy': 0, 'none': 0}
OPENING_COLUMNS = ['eco', 'opening_name', 'opening_family', 'naming_source']

_ECO = re.compile(r'[A-E]\d{2}')


def _missing(value) -> bool:
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def normalize_eco(value):
    """Return an ECO code such as 'B12', or None when the value is not one."""
    if _missing(value):
        return None
    text = str(value).strip().upper()
    return text if _ECO.fullmatch(text) else None


def family_for(eco: str) -> str:
    return FAMILY_BY_LETTER.get(eco[0], UNKNOWN_FAMILY) if eco else UNKNOWN_FAMILY


def clean_name(value):
    """Return the text as an opening name, or None when it is missing or only a code."""
    if _missing(value):
        return None
    text = str(value).strip()
    if not text or text.lower() in {'nan', 'unknown'} or normalize_eco(text):
        return None
    return text


def name_from_eco_url(value):
    """Readable name from a Chess.com opening URL, or None for anything else.

    Only full ``https://…/openings/<slug>`` URLs are accepted, so a bare ECO code
    such as 'C50' is never mistaken for a slug.
    """
    if _missing(value):
        return None
    text = str(value).strip()
    if not text.lower().startswith(('http://', 'https://')):
        return None
    parts = urlsplit(text).path.strip('/').split('/')
    if len(parts) < 2 or parts[-2] != 'openings':
        return None
    name = unquote(parts[-1]).replace('-', ' ').replace('_', ' ').strip()
    return name or None


def _is_base_of(base: str, other: str) -> bool:
    """True when ``base`` is ``other`` or a whole-word prefix of it."""
    return other == base or (other.startswith(base) and other[len(base)] in ' :,(')


def load_eco_table(path) -> dict:
    """Read {ECO code: base name} from a TSV or CSV with 'eco' and 'name' columns.

    A code gets a name only when every name listed for it extends one shortest name
    (for example 'Caro-Kann Defense' and 'Caro-Kann Defense: Advance Variation').
    Codes with unrelated names are left out rather than guessed. A missing file
    gives an empty table.
    """
    path = Path(path)
    if not path.exists():
        return {}
    sep = '\t' if path.suffix.lower() in {'.tsv', '.txt'} else ','
    table = pd.read_csv(path, sep=sep, dtype=str, keep_default_na=False)
    columns = {c.lower(): c for c in table.columns}
    if 'eco' not in columns or 'name' not in columns:
        raise ValueError(f'{path.name}: expected columns "eco" and "name"')
    names: dict[str, set] = {}
    for code, label in zip(table[columns['eco']], table[columns['name']]):
        eco, clean = normalize_eco(code), clean_name(label)
        if eco and clean:
            names.setdefault(eco, set()).add(clean)
    resolved = {}
    for eco, labels in names.items():
        base = min(labels, key=len)
        if all(_is_base_of(base, other) for other in labels):
            resolved[eco] = base
    return resolved


def resolve_opening(eco, pgn_name=None, eco_url=None, eco_table=None):
    """Return (eco, opening_name, opening_family, naming_source) for one game."""
    code = normalize_eco(eco) or UNKNOWN_NAME
    family = family_for(code)
    name = clean_name(pgn_name)
    if name:
        return code, name, family, 'pgn_tag'
    if code != UNKNOWN_NAME and eco_table and code in eco_table:
        return code, eco_table[code], family, 'eco_table'
    url_name = name_from_eco_url(eco_url)
    if url_name:
        return code, url_name, family, 'chesscom_url'
    return code, UNKNOWN_NAME, family, 'none'


def resolve_from_record(rec: dict, eco_table=None):
    """Resolve the opening of a stored game record (PubAPI or PGN import)."""
    headers = rec.get('_headers') or {}
    eco = (normalize_eco(headers.get('ECO')) or normalize_eco(rec.get('eco_code'))
           or normalize_eco(rec.get('eco')))
    eco_url = headers.get('ECOUrl') or rec.get('eco_url') or rec.get('eco')
    pgn_name = headers.get('Opening') or rec.get('opening')
    return resolve_opening(eco, pgn_name, eco_url, eco_table)


def repair_legacy_opening_names(df):
    """Show ECO codes stored as names by earlier outputs as ``Unknown``.

    Outputs written before this change can hold the ECO code in ``opening_name``.
    Until the pipeline is run again, those rows are labelled explicitly instead.
    """
    if df is None or df.empty or 'opening_name' not in df:
        return df
    out = df.copy()
    if 'naming_source' not in out:
        out['naming_source'] = 'legacy'
    out['naming_source'] = out['naming_source'].fillna('legacy')
    looks_like_code = out['opening_name'].map(lambda v: normalize_eco(v) is not None).astype(bool)
    out.loc[looks_like_code, 'opening_name'] = UNKNOWN_NAME
    out.loc[looks_like_code, 'naming_source'] = 'legacy'
    return out


def prefer_richer_naming(processed, archive):
    """Give a processed game the archive's opening fields when the archive names it better.

    Matching is by ``game_id``. Only the opening columns are replaced, and only when
    the archive's naming source ranks higher than the processed one.
    """
    if processed is None or processed.empty or archive is None or archive.empty:
        return processed
    if 'game_id' not in processed or 'game_id' not in archive:
        return processed
    out = processed.copy()
    if 'naming_source' not in out:
        out['naming_source'] = 'legacy'
    out['naming_source'] = out['naming_source'].fillna('legacy')
    best = archive.drop_duplicates('game_id', keep='first')
    by_id = {str(row['game_id']): row for row in best.to_dict('records')}
    for position, game_id in enumerate(out['game_id'].astype(str)):
        row = by_id.get(game_id)
        if row is None:
            continue
        current = NAMING_RANK.get(str(out['naming_source'].iat[position]), 0)
        if NAMING_RANK.get(str(row.get('naming_source')), 0) <= current:
            continue
        for column in OPENING_COLUMNS:
            if column in out and column in row:
                out.iloc[position, out.columns.get_loc(column)] = row[column]
    return out
