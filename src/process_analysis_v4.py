from pathlib import Path
import re
import hashlib
from urllib.parse import unquote
import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
OUT = DATA / 'processed_v4'
OUT.mkdir(parents=True, exist_ok=True)


def norm(s):
    return re.sub(r'[^a-z0-9]+', '_', str(s).lower()).strip('_')


def find_col(df, aliases):
    d = {norm(c): c for c in df.columns}
    for a in aliases:
        if norm(a) in d:
            return d[norm(a)]
    for c in df.columns:
        if any(norm(a) in norm(c) for a in aliases):
            return c
    return None


def parse_tc(v):
    if pd.isna(v):
        return ('Unknown', 'Unknown', np.nan, np.nan, np.nan)
    s = str(v).strip()
    m = re.match(r'^(\d+)(?:\s*\+\s*(\d+))?$', s)
    if not m:
        cat = 'Daily' if s.lower().startswith('daily') else 'Unknown'
        return (cat, s, np.nan, np.nan, np.nan)
    initial = int(m.group(1)); inc = int(m.group(2) or 0)
    effective = initial + 40 * inc
    cat = 'Bullet' if effective < 180 else 'Blitz' if effective < 600 else 'Rapid' if effective < 1500 else 'Classical'
    return (cat, f'{initial//60}:{initial%60:02d}+{inc}' if inc else f'{initial//60}:{initial%60:02d}', initial, inc, effective)


def parse_pgn_headers(path):
    """Read only PGN headers; no engine/chess parsing is performed."""
    if not path.exists():
        return pd.DataFrame()
    rows = []
    headers = {}
    in_headers = False
    with path.open('r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            line = line.rstrip('\n')
            if line.startswith('[Event '):
                if headers:
                    rows.append(headers)
                headers = {}
                in_headers = True
            if in_headers and line.startswith('['):
                m = re.match(r'^\[([^ ]+)\s+"(.*)"\]$', line)
                if m:
                    headers[m.group(1)] = m.group(2)
            elif in_headers and not line.strip():
                in_headers = False
        if headers:
            rows.append(headers)
    p = pd.DataFrame(rows)
    if p.empty:
        return p
    # V2/V3 generated game_id as SHA1(first 16 hex chars) of stable PGN headers.
    # Reproduce that key so PGN metadata joins correctly without re-running Stockfish.
    def make_game_id(row):
        raw = '\n'.join(str(row.get(k, '')) for k in ['Site','Date','Round','White','Black','Result','UTCDate','UTCTime'])
        return hashlib.sha1(raw.encode()).hexdigest()[:16]
    p['game_id'] = p.apply(make_game_id, axis=1)
    p['eco_url'] = p.get('ECOUrl', pd.Series(index=p.index, dtype='object'))
    p['opening_pgn'] = p.get('Opening', pd.Series(index=p.index, dtype='object'))
    p['opening_from_url'] = p['eco_url'].astype(str).str.rsplit('/', n=1).str[-1]
    p['opening_from_url'] = p['opening_from_url'].apply(lambda x: unquote(x).replace('-', ' ').replace('_', ' ') if x and x != 'nan' else pd.NA)
    p['utc_time'] = pd.to_datetime(p.get('UTCDate', pd.Series(index=p.index)) .astype(str) + ' ' + p.get('UTCTime', pd.Series(index=p.index)).astype(str), errors='coerce', utc=True)
    p['termination'] = p.get('Termination', pd.Series(index=p.index, dtype='object'))
    p['variant'] = p.get('Variant', pd.Series(index=p.index, dtype='object')).fillna('chess')
    p['rated'] = p.get('Rated', pd.Series(index=p.index, dtype='object')).map({'rated':True,'unrated':False})
    p['link'] = p.get('Link', pd.Series(index=p.index, dtype='object'))
    keep = ['game_id','White','Black','Result','WhiteElo','BlackElo','TimeControl','ECO','opening_from_url','opening_pgn','eco_url','utc_time','termination','link','CurrentPosition','variant','rated']
    return p[[c for c in keep if c in p.columns]].drop_duplicates('game_id')


def main():
    gp = DATA / 'analysis.csv'
    mp = DATA / 'moves_analysis.csv'
    pp = DATA / 'games.pgn'
    if not gp.exists() or not mp.exists():
        raise FileNotFoundError('Need data/analysis.csv and data/moves_analysis.csv')

    g0 = pd.read_csv(gp, low_memory=False)
    m0 = pd.read_csv(mp, low_memory=False)

    # analysis.csv uses player_* names; preserve the real schema explicitly.
    g = pd.DataFrame(index=g0.index)
    mapping = {
        'game_id':['game_id'], 'date':['date'], 'player_result':['player_result','result'],
        'player_rating':['player_rating'], 'opponent_rating':['opponent_rating'],
        'time_class':['time_class'], 'time_control':['time_control'], 'eco':['eco'],
        'opening_raw':['opening'], 'player_color':['player_color','color'], 'moves':['moves'],
        'accuracy':['accuracy'], 'acpl':['acpl','cpl'], 'inaccuracies':['inaccuracies'],
        'mistakes':['mistakes'], 'blunders':['blunders']
    }
    for k, aliases in mapping.items():
        c = find_col(g0, aliases)
        g[k] = g0[c] if c else pd.NA

    g['game_id'] = g['game_id'].astype(str)
    g['date'] = pd.to_datetime(g['date'], errors='coerce', utc=True)
    for c in ['player_rating','opponent_rating','moves','accuracy','acpl','inaccuracies','mistakes','blunders']:
        g[c] = pd.to_numeric(g[c], errors='coerce')
    g['source_acpl'] = g['acpl']
    g['rating_diff'] = g['player_rating'] - g['opponent_rating']
    g['rating_band'] = pd.cut(g['opponent_rating'], [-np.inf,1000,1200,1400,1600,1800,2000,2200,np.inf], labels=['<1000','1000–1199','1200–1399','1400–1599','1600–1799','1800–1999','2000–2199','2200+'])
    g[['time_category','time_format','initial_seconds','increment_seconds','effective_seconds']] = pd.DataFrame(g['time_control'].apply(parse_tc).tolist(), index=g.index)
    official_class=g['time_class'].astype('string').str.lower().map({'bullet':'Bullet','blitz':'Blitz','rapid':'Rapid','daily':'Daily','classical':'Classical'})
    g['time_category']=official_class.fillna(g['time_category'])
    g['eco'] = g['eco'].fillna('Unknown').astype(str).str.upper().str.extract(r'([A-E][0-9]{2})', expand=False).fillna('Unknown')
    g['opening_family'] = g['eco'].str[0].map({
        'A':'A — Flank / English / Réti / Dutch / Benoni',
        'B':'B — Sicilian / Caro-Kann / Modern',
        'C':'C — French / Open Games / Ruy Lopez',
        'D':'D — Queen’s Gambit / Slav / Grünfeld',
        'E':'E — Indian Defences / Catalan / Nimzo-Indian / King’s Indian'
    }).fillna('Unknown / Unclassified')

    # Move-level source.
    m = pd.DataFrame(index=m0.index)
    for k, aliases in {
        'game_id':['game_id'], 'ply':['ply'], 'move_number':['move_number','move'], 'color':['color'],
        'san':['san'], 'white':['white'], 'black':['black'], 'cpl':['cpl'], 'accuracy':['accuracy'], 'classification':['classification'],
        'phase':['phase'], 'date':['date'], 'player_color':['player_color']
    }.items():
        c = find_col(m0, aliases)
        m[k] = m0[c] if c else pd.NA
    m['game_id'] = m['game_id'].astype(str)
    for c in ['ply','move_number','cpl','accuracy']:
        m[c] = pd.to_numeric(m[c], errors='coerce')
    # The source engine encodes forced-mate/forced-win evaluations around 90k-200k cp.
    # Preserve those raw rows and classifications, but exclude extreme scores from
    # arithmetic centipawn averages where they are not comparable to ordinary CPL.
    m['mate_score_sentinel'] = m['cpl'].abs().ge(10000)
    m['cpl_for_average'] = m['cpl'].where(~m['mate_score_sentinel'])
    m['classification'] = m['classification'].astype(str).str.lower()
    m['phase'] = m['phase'].astype(str).str.lower()
    m['is_player_move'] = m['color'].astype(str).str.lower().eq(m['player_color'].astype(str).str.lower())
    m['is_error'] = m['classification'].isin(['inaccuracy','mistake','blunder'])
    m['is_major_error'] = m['classification'].isin(['mistake','blunder'])

    # Merge PGN metadata: start time, termination, link, names, exact ECO URL.
    p = parse_pgn_headers(pp)
    if not p.empty:
        g = g.merge(p, on='game_id', how='left', suffixes=('','_pgn'))
        g['white'] = g['White']
        g['black'] = g['Black']
        g['result'] = g['Result']
        g['game_url'] = g['link']
        g['opening_name'] = g['opening_from_url']
        g['start_time_utc'] = g['utc_time']
        g['termination'] = g['termination'].astype('string')
        g['rules'] = g['variant'].fillna('chess').astype(str).str.lower()
        g['white_rating'] = pd.to_numeric(g['WhiteElo'], errors='coerce')
        g['black_rating'] = pd.to_numeric(g['BlackElo'], errors='coerce')
        g['day_of_week'] = g['start_time_utc'].dt.day_name()
        g['hour_utc'] = g['start_time_utc'].dt.hour
        g['opening_name'] = g['opening_name'].fillna(g['opening_pgn']).fillna(g['opening_raw'])
    else:
        g['white']=pd.NA; g['black']=pd.NA; g['result']=pd.NA; g['game_url']=pd.NA; g['opening_name']=g['opening_raw']; g['start_time_utc']=pd.NaT; g['termination']=pd.NA; g['white_rating']=pd.NA; g['black_rating']=pd.NA; g['day_of_week']=pd.NA; g['hour_utc']=pd.NA; g['rules']=pd.NA; g['rated']=pd.NA

    # Player-side move statistics. These are computed from already analysed moves.
    pm = m[m['is_player_move']].copy()
    if not pm.empty:
        def count_class(x): return x.eq(x.name).sum()
        counts = pm.pivot_table(index='game_id', columns='classification', values='ply', aggfunc='count', fill_value=0).reset_index()
        counts.columns = ['game_id'] + [f'moves_{str(c)}' for c in counts.columns[1:]]
        phase_stats = pm.groupby(['game_id','phase'], observed=True).agg(
            player_moves=('ply','size'), player_cpl=('cpl_for_average','mean'), player_accuracy=('accuracy','mean'),
            player_errors=('is_error','sum'), player_major_errors=('is_major_error','sum'),
            mate_score_sentinels=('mate_score_sentinel','sum')
        ).reset_index()
        phase_wide = phase_stats.pivot(index='game_id', columns='phase', values=['player_moves','player_cpl','player_accuracy','player_errors','player_major_errors'])
        phase_wide.columns = [f'{a}_{b}' for a,b in phase_wide.columns]
        phase_wide = phase_wide.reset_index()
        player_agg = pm.groupby('game_id').agg(
            player_moves=('ply','size'), player_cpl=('cpl_for_average','mean'), player_accuracy=('accuracy','mean'),
            player_errors=('is_error','sum'), player_major_errors=('is_major_error','sum'),
            mate_score_sentinels=('mate_score_sentinel','sum')
        ).reset_index()
        # Key moments: top 5 player moves by CPL.
        key = pm.sort_values(['game_id','cpl'], ascending=[True,False]).groupby('game_id').head(5)
        key_rows = []
        for gid, gg in key.groupby('game_id'):
            parts=[]
            for _,r in gg.iterrows():
                parts.append(f"{int(r.move_number)}{'.' if str(r.color).lower()=='white' else '...'} {r.san} ({r.classification}, CPL {r.cpl:.0f})")
            key_rows.append((gid,' | '.join(parts)))
        key_df = pd.DataFrame(key_rows, columns=['game_id','key_moments'])
        g = g.merge(player_agg, on='game_id', how='left').merge(counts, on='game_id', how='left').merge(phase_wide, on='game_id', how='left').merge(key_df, on='game_id', how='left')
    else:
        g['player_moves']=np.nan; g['player_cpl']=np.nan; g['player_accuracy']=np.nan; g['player_errors']=np.nan; g['player_major_errors']=np.nan
        g['key_moments']=pd.NA

    # Derived performance labels; deliberately not presented as Chess.com labels.
    g['outcome_points'] = g['player_result'].map({'Win':1.0,'Draw':0.5,'Loss':0.0})
    g['win'] = g['player_result'].eq('Win')
    g['loss'] = g['player_result'].eq('Loss')
    g['draw'] = g['player_result'].eq('Draw')
    g['result_type'] = g['termination'].astype(str).str.lower().str.extract(r'(checkmate|resign|time|abandon|stalemate|repetition|agreement|insufficient|50-move|draw)', expand=False).fillna('Other')
    g['month'] = g['date'].dt.strftime('%Y-%m')

    # Save core tables.
    g.to_csv(OUT/'games_enriched_v4.csv', index=False)
    m.to_csv(OUT/'moves_enriched_v4.csv', index=False)
    if not pm.empty:
        phase_stats.to_csv(OUT/'player_phase_stats_v4.csv', index=False)
        # One row per game + phase summary, useful for drilldowns.
        pm.groupby(['game_id','phase'], observed=True).agg(
            Moves=('ply','size'), CPL=('cpl_for_average','mean'), Accuracy=('accuracy','mean'), Errors=('is_error','sum'), MajorErrors=('is_major_error','sum'), MateScoreSentinels=('mate_score_sentinel','sum')
        ).reset_index().to_csv(OUT/'phase_stats_v4.csv', index=False)

    # Reusable aggregate tables.
    def summary(keys):
        return g.groupby(keys, dropna=False).agg(
            Games=('game_id','nunique'), WinRate=('win','mean'), DrawRate=('draw','mean'), Accuracy=('accuracy','mean'),
            PlayerCPL=('player_cpl','mean'), SourceACPL=('source_acpl','mean'), Blunders=('blunders','mean'), Mistakes=('mistakes','mean'), Inaccuracies=('inaccuracies','mean'),
            RatingDiff=('rating_diff','mean')
        ).reset_index().sort_values('Games', ascending=False)
    summary(['time_category','time_format']).to_csv(OUT/'time_control_stats_v4.csv', index=False)
    summary(['opening_family','eco','opening_name']).to_csv(OUT/'opening_stats_v4.csv', index=False)
    summary(['player_color']).to_csv(OUT/'color_stats_v4.csv', index=False)
    summary(['rating_band']).to_csv(OUT/'opponent_rating_stats_v4.csv', index=False)
    summary(['day_of_week']).to_csv(OUT/'weekday_stats_v4.csv', index=False)

    month = g.dropna(subset=['date']).groupby('month').agg(Games=('game_id','nunique'), WinRate=('win','mean'), Accuracy=('accuracy','mean'), SourceACPL=('source_acpl','mean'), PlayerCPL=('player_cpl','mean'), Blunders=('blunders','mean')).reset_index()
    month.to_csv(OUT/'monthly_stats_v4.csv', index=False)

    print(f'Processed {len(g):,} games and {len(m):,} analyzed moves.')
    print(f'Output: {OUT}')
    print('No Stockfish analysis was run.')


if __name__ == '__main__':
    main()
