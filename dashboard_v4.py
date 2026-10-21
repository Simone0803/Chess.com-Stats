from pathlib import Path
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter, defaultdict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import unquote, urlsplit
import pandas as pd
import numpy as np
import streamlit as st
import plotly.express as px
from src.metrics import grouped_result_summary, longest_streak, rating_summary, result_summary

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'data'/'processed_v4'

st.set_page_config(page_title='Chess Analytics V4', page_icon='♟', layout='wide')

def _outcome_from_api(me_result, opp_result):
    result=str(me_result or '').lower(); opponent=str(opp_result or '').lower()
    draw_terms=('agreed','stalemate','repetition','insufficient','50move','draw','timevsinsufficient')
    loss_terms=('checkmated','resigned','timeout','abandoned','lose','lost','time')
    return 'Win' if result=='win' else 'Draw' if result in draw_terms or opponent in draw_terms else 'Loss' if result in loss_terms or opponent=='win' else 'Unknown'

def _archive_signature(date, color, result, player_rating, opponent_rating, time_control):
    if pd.isna(date): return None
    def rating(value):
        try: return str(int(value))
        except (TypeError,ValueError,OverflowError): return ''
    return (pd.Timestamp(date).date().isoformat(),str(color).lower(),str(result),
            rating(player_rating),rating(opponent_rating),str(time_control or ''))

def _api_signature(rec, username):
    w,b=rec.get('white') or {},rec.get('black') or {}; headers=rec.get('_headers') or {}
    white=str(w.get('username') or headers.get('White') or '').lower()==username.lower()
    black=str(b.get('username') or headers.get('Black') or '').lower()==username.lower()
    if not (white or black): return None
    me,op=(w,b) if white else (b,w)
    end=rec.get('end_time')
    date=pd.to_datetime(end,unit='s',utc=True,errors='coerce') if end is not None else pd.NaT
    return _archive_signature(date,'white' if white else 'black',
        _outcome_from_api(me.get('result'),op.get('result')),me.get('rating'),op.get('rating'),rec.get('time_control'))

def _phase_value(summary, phase, key):
    if not isinstance(summary,dict): return np.nan
    value=summary.get(phase)
    if not isinstance(value,dict): return np.nan
    return value.get(key) if value.get(key) is not None else np.nan

@st.cache_data(ttl=60,show_spinner=False)
def archived_rows(username, known_ids=None, known_urls=None, known_signatures=None):
    """Translate local PubAPI/PGN metadata to the dashboard's independent schema."""
    try:
        from src.chess_data import game_summary_rows, legacy_csv_game_ids_from_headers
        records=game_summary_rows()
    except Exception:
        return []
    api_signatures=Counter(sig for rec in records if (sig:=_api_signature(rec,username)) is not None)
    rows=[]
    for rec in records:
        w,b=rec.get('white') or {},rec.get('black') or {}
        headers=rec.get('_headers') or {}
        if not username:
            continue
        white=str(w.get('username') or headers.get('White') or '').lower()==username.lower()
        black=str(b.get('username') or headers.get('Black') or '').lower()==username.lower()
        if not white and not black: continue
        me,op=(w,b) if white else (b,w)
        result=str(me.get('result') or '').lower()
        opp_result=str(op.get('result') or '').lower()
        outcome=_outcome_from_api(result,opp_result)
        end=rec.get('end_time')
        date=pd.to_datetime(end,unit='s',utc=True,errors='coerce') if end is not None else pd.NaT
        start_date=headers.get('UTCDate') or headers.get('Date')
        start_clock=headers.get('UTCTime') or headers.get('StartTime') or '00:00:00'
        start_time=pd.to_datetime(f'{start_date} {start_clock}',format='%Y.%m.%d %H:%M:%S',utc=True,errors='coerce') if start_date else pd.NaT
        archive_id=str(rec.get('uuid') or str(rec.get('url') or '').rstrip('/').split('/')[-1] or rec.get('game_id') or '')
        aliases=legacy_csv_game_ids_from_headers(headers)
        known_ids=known_ids or set()
        url=str(rec.get('url') or headers.get('Site') or '').rstrip('/')
        gid=next((candidate for candidate in aliases if candidate in known_ids),None)
        if gid is None and url: gid=(known_urls or {}).get(url)
        signature=_api_signature(rec,username)
        if gid is None and signature and api_signatures[signature]==1:
            gid=(known_signatures or {}).get(signature)
        if gid is None: gid=aliases[0] if aliases else archive_id
        accuracies=rec.get('accuracies') or {}
        acc=accuracies.get('white' if white else 'black')
        tc=str(rec.get('time_control') or '')
        try:
            secs=int(tc.split('+')[0]); inc=int(tc.split('+')[1]) if '+' in tc else 0
            effective=secs+40*inc
            category='Bullet' if effective<180 else 'Blitz' if effective<600 else 'Rapid' if effective<1500 else 'Classical'
            fmt=f'{secs//60}:{secs%60:02d}+{inc}'
        except (ValueError,IndexError): category='Unknown'; fmt=tc or 'Unknown'
        if str(rec.get('time_class') or '').lower()=='daily': category='Daily'
        eco_candidate=str(headers.get('ECO') or '')
        if not re.fullmatch(r'[A-Ea-e]\d{2}',eco_candidate):
            eco_candidate=str(urlsplit(str(rec.get('eco') or '')).path.rstrip('/').split('/')[-1])
        eco=eco_candidate.upper() if re.fullmatch(r'[A-Ea-e]\d{2}',eco_candidate) else 'Unknown'
        eco_url=headers.get('ECOUrl') or rec.get('eco') or ''
        opening_slug=unquote(urlsplit(str(eco_url)).path.rstrip('/').split('/')[-1]).replace('-',' ').replace('_',' ')
        opening=str(headers.get('Opening') or rec.get('opening') or opening_slug or (f'ECO {eco}' if eco!='Unknown' else 'Unknown'))
        termination=str(headers.get('Termination') or rec.get('termination') or
                        (opp_result if result=='win' else result) or 'unknown')
        termination_lower=termination.lower()
        if 'checkmate' in termination_lower: result_type='Checkmate'
        elif 'resign' in termination_lower: result_type='Resignation'
        elif 'timeout' in termination_lower or 'time forfeit' in termination_lower: result_type='Timeout'
        elif 'abandon' in termination_lower: result_type='Abandoned'
        elif any(term in termination_lower for term in ('stalemate','repetition','insufficient','50-move','draw','agreement')): result_type='Draw'
        else: result_type=termination
        rows.append({'game_id':gid,'archive_game_id':archive_id,'date':date,'player_result':outcome,
          'player_rating':me.get('rating') or headers.get('WhiteElo' if white else 'BlackElo'),
          'opponent_rating':op.get('rating') or headers.get('BlackElo' if white else 'WhiteElo'),
          'time_class':rec.get('time_class') or category.lower(),'time_control':tc,'time_category':category,
          'time_format':fmt,'eco':eco,'opening_raw':opening,'opening_name':opening,
          'opening_family':(eco[0] if eco[:1] in 'ABCDE' else 'Unknown / Unclassified'),
          'player_color':'White' if white else 'Black','moves':np.nan,'accuracy':acc,'acpl':np.nan,
          'inaccuracies':np.nan,'mistakes':np.nan,'blunders':np.nan,'rating_diff':None,
          'player_cpl':np.nan,'player_accuracy':np.nan,'player_errors':np.nan,'player_major_errors':np.nan,
          'rating_band':'Unknown','rated':rec.get('rated'),'white':w.get('username') or headers.get('White'),
          'black':b.get('username') or headers.get('Black'),'result':headers.get('Result'),
          'game_url':url or None,'termination':termination,
          'start_time_utc':start_time,'end_time_utc':date,'source':'local archive','win':True if outcome=='Win' else False if outcome in ('Draw','Loss') else pd.NA,
          'loss':True if outcome=='Loss' else False if outcome in ('Win','Draw') else pd.NA,
          'draw':True if outcome=='Draw' else False if outcome in ('Win','Loss') else pd.NA,
          'outcome_points':1.0 if outcome=='Win' else .5 if outcome=='Draw' else 0.0 if outcome=='Loss' else pd.NA,
          'result_type':result_type,'month':date.strftime('%Y-%m') if pd.notna(date) else 'NaT',
          'day_of_week':start_time.day_name() if pd.notna(start_time) else None,
          'hour_utc':start_time.hour if pd.notna(start_time) else None})
    return rows

def infer_username_from_moves(moves):
    needed={'color','player_color','white','black'}
    if moves.empty or not needed.issubset(moves.columns): return None
    side=moves.color.astype(str).str.lower()
    player_side=moves.player_color.astype(str).str.lower()
    matches=moves.loc[side.eq(player_side) & side.isin(['white','black'])]
    if matches.empty: return None
    names=matches.white.where(matches.color.astype(str).str.lower().eq('white'),matches.black).dropna().astype(str).str.strip().str.lower()
    counts=names[names.ne('')].value_counts()
    if counts.empty or (len(counts)>1 and counts.iloc[0]==counts.iloc[1]) or counts.iloc[0]/counts.sum()<.8: return None
    return counts.index[0]

@st.cache_data
def load(name):
    p=OUT/name
    return pd.read_csv(p, low_memory=False) if p.exists() else pd.DataFrame()

@st.cache_data(show_spinner=False)
def load_engine_json(path,modified_ns,size_bytes):
    """Parse a checkpoint once per file version instead of on every UI rerun."""
    return json.loads(Path(path).read_text(encoding='utf-8'))

g=load('games_enriched_v4.csv'); m=load('moves_enriched_v4.csv'); phase=load('phase_stats_v4.csv')
source_game_ids=set(g.game_id.dropna().astype(str)) if 'game_id' in g else set()
source_game_urls={str(url).rstrip('/'):str(gid) for gid,url in zip(g.get('game_id',[]),g.get('game_url',[]))
                  if pd.notna(url) and str(url).strip()}
engine_path=ROOT/'data'/'engine_analysis.json'
engine_index_path=ROOT/'data'/'engine_analysis_index.json'
engine_games=pd.DataFrame(); engine_moves=pd.DataFrame(); engine_settings={}
engine_index_loaded=False
engine_read_path=engine_index_path if engine_index_path.exists() else engine_path
if engine_read_path.exists():
    try:
        engine_stat=engine_read_path.stat()
        engine_payload=load_engine_json(str(engine_read_path),engine_stat.st_mtime_ns,engine_stat.st_size)
        if isinstance(engine_payload,dict):
            engine_settings=engine_payload.get('settings') or {}
            engine_payload=engine_payload.get('results',[])
        engine_index_loaded=engine_read_path==engine_index_path
        engine_games=pd.DataFrame([{k:v for k,v in x.items() if k!='moves'} for x in engine_payload])
        if not engine_index_loaded:
            engine_moves=pd.DataFrame([{'game_id':next((gid for gid in x.get('legacy_game_ids',[]) if gid in source_game_ids),
                                                       source_game_urls.get(str(x.get('game_url') or '').rstrip('/'),x.get('game_id'))),
                                        'archive_game_id':x.get('game_id'),**move}
                                       for x in engine_payload for move in x.get('moves',[])])
    except (ValueError,OSError,TypeError):
        engine_games=pd.DataFrame(); engine_moves=pd.DataFrame(); engine_settings={}

with st.sidebar.expander('Local archive: PGN / public API', expanded=False):
    inferred_username=infer_username_from_moves(m)
    default_username=os.getenv('CHESSCOM_USERNAME') or inferred_username or ''
    username=st.text_input('Chess.com username',value=default_username,key='archive_username')
    if inferred_username and not os.getenv('CHESSCOM_USERNAME'):
        st.caption(f'Prefilled from your analyzed move rows ({inferred_username}); edit this field if the archive contains a different account.')
    uploaded=st.file_uploader('Import PGN',type=['pgn'])
    if st.button('Import uploaded PGN',disabled=uploaded is None or not username):
        try:
            from src.chess_data import import_pgn
            with tempfile.NamedTemporaryFile(suffix='.pgn') as tmp:
                tmp.write(uploaded.getvalue()); tmp.flush()
                n=import_pgn(Path(tmp.name),username)
            archived_rows.clear()
            st.success(f'PGN importato nell’archivio locale ({n} righe inserite/aggiornate). Ricarica la pagina per visualizzarle.')
        except Exception as exc: st.error(f'Importazione PGN non riuscita: {exc}')
    st.caption('Il caricamento dall’API è disponibile da terminale con `python3 -m src.import_games --fetch`.')
    st.caption('L’analisi del motore e le classificazioni sono post-partita; non sono calcolate durante il gioco.')

profile_stats={}
if username:
    known_urls={str(url).rstrip('/'):str(gid) for gid,url in zip(g.get('game_id',[]),g.get('game_url',[]))
                if pd.notna(url) and str(url).strip()}
    signature_groups={}
    for row in g.itertuples(index=False):
        sig=_archive_signature(getattr(row,'date',pd.NaT),getattr(row,'player_color',''),
            getattr(row,'player_result',''),getattr(row,'player_rating',None),getattr(row,'opponent_rating',None),
            getattr(row,'time_control',''))
        if sig: signature_groups.setdefault(sig,[]).append(str(row.game_id))
    known_signatures={sig:ids[0] for sig,ids in signature_groups.items() if len(ids)==1}
    rows=archived_rows(username,set(g.game_id.astype(str)),known_urls,known_signatures)
    try:
        from src.chess_data import read_player_stats
        profile_stats=read_player_stats(username)
    except Exception: profile_stats={}
    if rows:
        archive=pd.DataFrame(rows)
        if g.empty: g=archive
        else:
            archive=archive.drop_duplicates('game_id').set_index('game_id')
            for column in archive.columns:
                if column not in g: g[column]=pd.NA
                fill=g.game_id.astype(str).map(archive[column])
                g[column]=g[column].where(g[column].notna(),fill)
            known=set(g.game_id.astype(str))
            extra=archive.reset_index()
            g=pd.concat([g,extra[~extra.game_id.astype(str).isin(known)]],ignore_index=True,sort=False)
archive_to_source={str(archive_id):str(game_id) for game_id,archive_id in zip(g.get('game_id',[]),g.get('archive_game_id',[]))
                   if pd.notna(archive_id) and pd.notna(game_id)}
if not engine_moves.empty:
    engine_moves['game_id']=engine_moves['archive_game_id'].astype(str).map(archive_to_source).fillna(engine_moves['game_id'])
if not engine_games.empty:
    # The engine CLI summarizes only the configured username's moves. Do not
    # overwrite the source-provided accuracy or its labels.
    eg=engine_games.copy()
    eg['archive_game_id']=eg.game_id
    if 'legacy_game_id' in eg or 'legacy_game_ids' in eg:
        def engine_display_id(row):
            aliases=row.get('legacy_game_ids')
            if isinstance(aliases,list):
                match=next((candidate for candidate in aliases if candidate in source_game_ids),None)
                if match: return match
            archive_id=str(row.get('game_id') or '')
            if archive_id in archive_to_source: return archive_to_source[archive_id]
            return source_game_urls.get(str(row.get('game_url') or '').rstrip('/'),row.get('game_id'))
        eg['game_id']=eg.apply(engine_display_id,axis=1)
    eg['engine_accuracy']=pd.to_numeric(eg.get('player_accuracy'),errors='coerce')
    phase_data=eg.get('phase_summary',pd.Series(index=eg.index,dtype='object'))
    eg['engine_opening_accuracy']=phase_data.apply(lambda value:_phase_value(value,'opening','mean_accuracy'))
    eg['engine_opening_cpl']=phase_data.apply(lambda value:_phase_value(value,'opening','mean_cpl'))
    eg['engine_opening_errors']=phase_data.apply(lambda value:_phase_value(value,'opening','errors'))
    eg['engine_opening_major_errors']=phase_data.apply(lambda value:_phase_value(value,'opening','major_errors'))
    eg['engine_lost_winning_advantage']=pd.to_numeric(eg.get('lost_winning_advantage_count'),errors='coerce')
    eg['engine_missed_mates']=pd.to_numeric(eg.get('missed_mate_opportunities_count'),errors='coerce')
    eg['engine_key_moments']=eg.get('key_moments')
    engine_count_names={'good':'engine_good_moves','inaccuracy':'engine_inaccuracies',
                        'mistake':'engine_mistakes','blunder':'engine_blunders'}
    for label,column in engine_count_names.items():
        eg[column]=eg.get('own_move_counts',pd.Series(index=eg.index,dtype='object')).apply(
            lambda counts: (counts or {}).get(label,0) if isinstance(counts,dict) else 0)
    if not g.empty:
        g=g.merge(eg[['game_id','engine_accuracy','engine_opening_accuracy','engine_opening_cpl',
                      'engine_opening_errors','engine_opening_major_errors',
                      'engine_lost_winning_advantage','engine_missed_mates','engine_key_moments',
                      *engine_count_names.values()]],
                  on='game_id',how='left')
if g.empty:
    st.title('♟ Chess Analytics V4')
    st.info('Nessuna partita elaborata. Esegui `python3 -m src.process_analysis_v4`, importa un PGN, oppure carica lo storico pubblico con `python3 -m src.import_games --username NOME --fetch`.')
    st.stop()

g['date']=pd.to_datetime(g['date'],errors='coerce',utc=True)
g['start_time_utc']=pd.to_datetime(g['start_time_utc'] if 'start_time_utc' in g else pd.Series(pd.NaT,index=g.index),errors='coerce',utc=True)
for c in ['accuracy','acpl','player_cpl','blunders','mistakes','inaccuracies','player_errors','player_major_errors','rating_diff','player_rating','opponent_rating','moves']:
    if c in g: g[c]=pd.to_numeric(g[c],errors='coerce')
try:
    timezone_name=os.getenv('CHESS_TIMEZONE','Europe/Amsterdam')
    ZoneInfo(timezone_name)
except ZoneInfoNotFoundError:
    timezone_name='UTC'
if 'start_time_utc' in g:
    g['local_start_time']=g.start_time_utc.dt.tz_convert(timezone_name)
    g['day_of_week']=g.local_start_time.dt.day_name()
    g['hour_local']=g.local_start_time.dt.hour

st.title('♟ Chess Analytics V4')
st.caption('Independent chess performance dashboard. Existing move analysis is reused; optional Stockfish runs only after completed games.')

with st.sidebar:
    st.header('Filters')
    timezone_name=st.text_input('Timezone for calendar views',value=timezone_name,help='IANA zone, for example Europe/Amsterdam')
    try: ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        st.warning('Unknown timezone; using UTC for calendar views.')
        timezone_name='UTC'
    if 'start_time_utc' in g:
        g['local_start_time']=g.start_time_utc.dt.tz_convert(timezone_name)
        g['day_of_week']=g.local_start_time.dt.day_name()
        g['hour_local']=g.local_start_time.dt.hour
    gg=g.copy()
    for label,c in [('Time category','time_category'),('Exact time control','time_format'),('Color','player_color'),('Opening family','opening_family'),('Opening','opening_name'),('ECO','eco'),('Result','player_result'),('Opponent rating band','rating_band')]:
        if c in gg:
            vals=sorted(gg[c].dropna().astype(str).unique())
            sel=st.multiselect(label,vals)
            if sel: gg=gg[gg[c].astype(str).isin(sel)]
    for label,c in [('Rated status','rated'),('Rules / variant','rules')]:
        if c in gg and gg[c].notna().any():
            vals=sorted(gg[c].dropna().astype(str).unique())
            sel=st.multiselect(label,vals)
            if sel: gg=gg[gg[c].astype(str).isin(sel)]
    opponent_search=st.text_input('Opponent contains')
    if opponent_search:
        if 'white' in gg and 'black' in gg:
            mask=gg.white.astype(str).str.contains(opponent_search,case=False,na=False)|gg.black.astype(str).str.contains(opponent_search,case=False,na=False)
            gg=gg[mask]
        elif 'opponent' in gg: gg=gg[gg.opponent.astype(str).str.contains(opponent_search,case=False,na=False)]
    period_data=gg.copy()
    if gg.date.notna().any():
        d=st.date_input('Date range',(gg.date.dropna().min().date(),gg.date.dropna().max().date()))
        if isinstance(d,tuple) and len(d)==2: gg=gg[(gg.date.dt.date>=d[0])&(gg.date.dt.date<=d[1])]
    min_games=st.number_input('Minimum games for aggregate tables',1,100,5)

g=gg
cols=st.columns(8 if 'engine_accuracy' in g and g.engine_accuracy.notna().any() else 7)
cols[0].metric('Games',f'{len(g):,}')
cols[1].metric('Win rate',f'{100*g.win.mean():.1f}%' if len(g) else '—')
cols[2].metric('Draw rate',f'{100*g.draw.mean():.1f}%' if len(g) else '—')
cols[3].metric('Loss rate',f'{100*g.loss.mean():.1f}%' if len(g) else '—')
cols[4].metric('Source accuracy',f'{g.accuracy.mean():.1f}%' if g.accuracy.notna().any() else '—')
peak=rating_summary(g)
cols[5].metric('Highest recorded rating',f"{peak['peak']:.0f}" if peak['peak'] is not None else '—')
cols[6].metric('Blunders/game',f'{g.blunders.mean():.2f}' if g.blunders.notna().any() else '—')
if len(cols)>7:
    scored_engine_games=int(g.engine_accuracy.notna().sum())
    cols[7].metric(f'Independent engine accuracy · {scored_engine_games} games',f'{g.engine_accuracy.mean():.1f}%' if scored_engine_games else '—')
    if engine_settings:
        st.caption(f"Stockfish post-game sample: {scored_engine_games} games · depth {engine_settings.get('depth')} · time limit {engine_settings.get('time_limit') or 'not set'} sec/position.")

tabs=st.tabs(['Overview','Time controls','Openings','Errors & tactics','Phases','Progress','Context','Game explorer','Improvement report'])

with tabs[0]:
    st.subheader('Results and distributions')
    a,b,c=st.columns(3)
    a.metric('Average opponent rating',f"{pd.to_numeric(g.opponent_rating,errors='coerce').mean():.0f}" if g.opponent_rating.notna().any() else '—')
    a.metric('Rating change in selected period',f"{peak['change']:+.0f}" if peak['change'] is not None else '—')
    b.metric('Highest rating date',str(peak['peak_date'])[:10] if peak['peak_date'] else '—')
    streak=longest_streak(g)
    c.metric('Longest win streak',streak['Win'])
    st.caption(f"Longest streaks: {streak['Win']} wins · {streak['Draw']} draws · {streak['Loss']} losses. Ratings are recorded per-game values, not reconstructed daily history.")
    if profile_stats:
        profile_best=[]
        for key,value in profile_stats.items():
            if key.startswith('chess_') and isinstance(value,dict) and isinstance(value.get('best'),dict):
                best=value['best']
                if best.get('rating') is not None:
                    profile_best.append({'time_class':key.removeprefix('chess_'),'best_rating':best.get('rating'),
                                         'date':pd.to_datetime(best.get('date'),unit='s',utc=True,errors='coerce'),
                                         'game':best.get('game')})
        if profile_best:
            st.write('Official best ratings from the cached public profile stats endpoint')
            st.dataframe(pd.DataFrame(profile_best),width='stretch',hide_index=True)
    if g.opponent_rating.notna().any():
        st.dataframe(g.groupby('player_result').agg(Games=('game_id','nunique'),AverageOpponentRating=('opponent_rating','mean')).reset_index(),width='stretch',hide_index=True)
        best=g[(g.player_result=='Win') & g.opponent_rating.notna()].sort_values('opponent_rating',ascending=False).head(10)
        if not best.empty:
            st.write('Highest-rated opponents defeated in the filtered games')
            best_cols=[c for c in ['date','white','black','opponent_rating','player_color','opening_name','game_url'] if c in best]
            st.dataframe(best[best_cols],width='stretch',hide_index=True)
    color_agg={'SourceAccuracy':('accuracy','mean'),'PlayerCPL':('player_cpl','mean')}
    by_color=grouped_result_summary(g,'player_color')
    color_quality=g.groupby('player_color',dropna=False,observed=True).agg(**color_agg).reset_index()
    by_color=by_color.merge(color_quality,on='player_color',how='left')
    if 'engine_accuracy' in g:
        engine_quality=g.groupby('player_color',dropna=False,observed=True).agg(
            EngineAccuracy=('engine_accuracy','mean'),EngineGames=('engine_accuracy','count')).reset_index()
        by_color=by_color.merge(engine_quality,on='player_color',how='left')
    st.subheader('White and Black performance')
    st.dataframe(by_color,width='stretch',hide_index=True)
    a,b=st.columns(2)
    with a:
        r=g.player_result.value_counts().rename_axis('Result').reset_index(name='Games')
        st.plotly_chart(px.bar(r,x='Result',y='Games'),width='stretch')
    with b:
        rr=g.result_type.value_counts().rename_axis('Termination').reset_index(name='Games')
        st.plotly_chart(px.bar(rr,x='Termination',y='Games'),width='stretch')
    a,b=st.columns(2)
    with a: st.plotly_chart(px.histogram(g,x='accuracy',nbins=30,title='Game accuracy'),width='stretch')
    with b: st.plotly_chart(px.histogram(g,x='player_cpl',nbins=30,title='Average own-move CPL (mate sentinels excluded)'),width='stretch')
    st.info('Accuracy and move classifications here are based on the existing analysis files; they are not reconstructed as exact Chess.com metrics. Average own-move CPL excludes extreme ±10,000 cp mate/forced-win sentinels; raw source ACPL is kept in the game table.')

with tabs[1]:
    st.subheader('Time-control performance')
    time_agg={'Games':('game_id','nunique'),'WinRate':('win','mean'),'Accuracy':('accuracy','mean'),'SourceACPL':('source_acpl','mean'),'PlayerCPL':('player_cpl','mean'),'Blunders':('blunders','mean'),'Mistakes':('mistakes','mean')}
    if 'engine_accuracy' in g:
        time_agg['EngineAccuracy']=('engine_accuracy','mean'); time_agg['EngineGames']=('engine_accuracy','count')
    time_keys=['time_category','time_format']
    time_results=grouped_result_summary(g,time_keys)
    time_quality=g.groupby(time_keys,dropna=False,observed=True).agg(**{
        name:spec for name,spec in time_agg.items() if name not in {'Games','WinRate'}
    }).reset_index()
    x=time_results.merge(time_quality,on=time_keys,how='left')
    x=x[x.Games>=min_games].sort_values('Games',ascending=False)
    st.dataframe(x,width='stretch',hide_index=True)
    st.plotly_chart(px.scatter(x,x='PlayerCPL',y='WinRate',size='Games',hover_name='time_format',color='time_category',title='Player CPL vs win rate'),width='stretch')

with tabs[2]:
    st.subheader('Opening repertoire and performance')
    opening_keys=['opening_family','eco','opening_name']
    opening_results=grouped_result_summary(g,opening_keys)
    opening_agg={'Accuracy':('accuracy','mean'),'SourceACPL':('source_acpl','mean'),
                 'PlayerCPL':('player_cpl','mean'),'Blunders':('blunders','mean'),
                 'AverageOpponentRating':('opponent_rating','mean')}
    for column,label in [('player_accuracy_opening','SourceOpeningAccuracy'),
                         ('player_cpl_opening','SourceOpeningCPL'),
                         ('player_errors_opening','SourceOpeningErrors'),
                         ('player_major_errors_opening','SourceOpeningMajorErrors')]:
        if column in g: opening_agg[label]=(column,'mean')
    for column,label in [('engine_opening_accuracy','EngineOpeningAccuracy'),
                         ('engine_opening_cpl','EngineOpeningCPL'),
                         ('engine_opening_errors','EngineOpeningErrors'),
                         ('engine_opening_major_errors','EngineOpeningMajorErrors')]:
        if column in g: opening_agg[label]=(column,'mean')
    opening_quality=g.groupby(opening_keys,dropna=False,observed=True).agg(**opening_agg).reset_index()
    x=opening_results.merge(opening_quality,on=opening_keys,how='left')
    x=x[x.Games>=min_games].sort_values('Games',ascending=False)
    st.caption('Win/draw/loss rates use only games with a known result. “SourceOpening…” fields come from the supplied move analysis; “EngineOpening…” fields use the independent Stockfish opening phase when available.')
    st.dataframe(x,width='stretch',hide_index=True)
    eligible=x.dropna(subset=['WinRate']).copy()
    if not eligible.empty:
        rank_cols=[c for c in ['opening_name','eco','Games','WinRate','DrawRate','LossRate',
                               'AverageOpponentRating','SourceOpeningAccuracy','EngineOpeningAccuracy']
                   if c in eligible]
        best_openings=eligible.sort_values(['WinRate','Games'],ascending=[False,False]).head(10)
        worst_openings=eligible.sort_values(['WinRate','Games'],ascending=[True,False]).head(10)
        st.caption(f'Best/worst observed results require at least {min_games} games and are descriptive; no adjustment for opponent rating or statistical uncertainty is applied.')
        left,right=st.columns(2)
        with left:
            st.write('Highest observed win rate')
            st.dataframe(best_openings[rank_cols],width='stretch',hide_index=True)
        with right:
            st.write('Lowest observed win rate')
            st.dataframe(worst_openings[rank_cols],width='stretch',hide_index=True)
    st.plotly_chart(px.bar(x.head(30),x='Games',y='opening_name',orientation='h',title='Most played openings'),width='stretch')
    st.plotly_chart(px.scatter(x,x='PlayerCPL',y='WinRate',size='Games',hover_name='opening_name',hover_data=['eco','opening_family'],title='Opening performance profile'),width='stretch')
    if 'player_color' in g:
        color_keys=['opening_name','player_color']
        oc=grouped_result_summary(g,color_keys)
        oc_quality=g.groupby(color_keys,dropna=False,observed=True).agg(Accuracy=('accuracy','mean')).reset_index()
        oc=oc.merge(oc_quality,on=color_keys,how='left')
        oc=oc[oc.Games>=min_games]
        st.plotly_chart(px.bar(oc,x='opening_name',y='WinRate',color='player_color',barmode='group',title='Opening results by colour'),width='stretch')
    if 'date' in g and g.date.notna().any():
        top_openings=g.opening_name.value_counts().head(10).index
        trend=g[g.opening_name.isin(top_openings)].copy()
        trend['month']=pd.to_datetime(trend.date,utc=True,errors='coerce').dt.strftime('%Y-%m')
        trend=trend.dropna(subset=['month'])
        trend=grouped_result_summary(trend,['month','opening_name','player_color'])
        trend=trend.melt(id_vars=['month','opening_name','player_color','Games'],
                         value_vars=['WinRate','DrawRate','LossRate'],var_name='Result',value_name='Rate')
        if not trend.empty:
            st.plotly_chart(px.line(trend,x='month',y='Rate',color='opening_name',line_dash='player_color',
                                    facet_row='Result',markers=True,title='Monthly win/draw/loss rates in the ten most-played openings'),width='stretch')

with tabs[3]:
    st.subheader('Error profile')
    e=pd.DataFrame({'Type':['Inaccuracy','Mistake','Blunder'],'Average per game':[g.inaccuracies.mean(),g.mistakes.mean(),g.blunders.mean()]})
    st.plotly_chart(px.bar(e,x='Type',y='Average per game'),width='stretch')
    if not m.empty:
        mm=m[m.game_id.astype(str).isin(g.game_id.astype(str)) & m.is_player_move.astype(bool)]
        cls=mm.classification.value_counts().rename_axis('Classification').reset_index(name='Moves')
        st.plotly_chart(px.bar(cls,x='Classification',y='Moves',title='Your analyzed move classifications'),width='stretch')
        st.subheader('Largest individual mistakes')
        top=mm.sort_values('cpl',ascending=False).head(50)
        show=[c for c in ['game_id','move_number','color','san','cpl','accuracy','classification','phase','date','opening'] if c in top]
        st.dataframe(top[show],width='stretch',hide_index=True)
    if not engine_moves.empty:
        own_engine=engine_moves[engine_moves.get('player_move',False).astype(bool) & engine_moves.game_id.astype(str).isin(g.game_id.astype(str))]
        if not own_engine.empty:
            st.subheader('Post-game engine analysis')
            st.caption('Independent CPL thresholds: inaccuracy ≥50, mistake ≥100, blunder ≥200 cp. Exposed-piece flags are a heuristic.')
            st.plotly_chart(px.histogram(own_engine,x='classification',color='phase',title='Engine move classifications by phase'),width='stretch')
            own_engine=own_engine.copy()
            if 'player_material_balance' in own_engine and 'missed_mate_opportunity' in own_engine:
                material_balance=pd.to_numeric(own_engine.player_material_balance,errors='coerce')
                own_engine['material_state']=np.select(
                    [material_balance.lt(0),material_balance.gt(0)],
                    ['Material disadvantage','Material advantage'],default='Equal material')
                own_engine['major_error']=own_engine.classification.isin(['mistake','blunder'])
                material_view=own_engine.groupby('material_state').agg(
                    Moves=('ply','size'),MeanCPL=('cpl_for_average','mean'),MajorErrors=('major_error','sum'),
                    MissedMateOpportunities=('missed_mate_opportunity','sum')).reset_index()
                material_view['MajorErrorRate']=material_view.MajorErrors/material_view.Moves
                st.write('Your move quality by material balance (piece values: pawn 1, minor piece 3, rook 5, queen 9)')
                st.dataframe(material_view,width='stretch',hide_index=True)
            else:
                st.caption('This saved engine analysis predates material tracking. Re-run the CLI to refresh the cached analysis.')
            engine_show=[c for c in ['game_id','move_number','color','san','best_move','phase','eval_before_cp_mover','eval_after_cp_mover','cpl','classification','exposed_piece_heuristic','large_swing'] if c in own_engine]
            st.dataframe(own_engine.sort_values('cpl',ascending=False).head(100)[engine_show],width='stretch',hide_index=True)
    elif not engine_games.empty:
        summaries=engine_games[engine_games.game_id.astype(str).isin(g.game_id.astype(str))]
        class_totals=Counter()
        phase_totals=defaultdict(lambda:{'Moves':0,'Errors':0,'MajorErrors':0,'CPLTotal':0.0,'CPLMoves':0})
        material_totals=defaultdict(lambda:{'Moves':0,'MajorErrors':0,'CPLTotal':0.0,'CPLMoves':0})
        for row in summaries.itertuples(index=False):
            counts=getattr(row,'own_move_counts',None)
            if isinstance(counts,dict): class_totals.update(counts)
            phase_summary=getattr(row,'phase_summary',None)
            if isinstance(phase_summary,dict):
                for name,values in phase_summary.items():
                    target=phase_totals[name];target['Moves']+=values.get('moves',0);target['Errors']+=values.get('errors',0);target['MajorErrors']+=values.get('major_errors',0)
                    if values.get('mean_cpl') is not None:
                        target['CPLTotal']+=values['mean_cpl']*values.get('moves',0);target['CPLMoves']+=values.get('moves',0)
            material_summary=getattr(row,'material_summary',None)
            if isinstance(material_summary,dict):
                for name,values in material_summary.items():
                    target=material_totals[name];target['Moves']+=values.get('moves',0);target['MajorErrors']+=values.get('major_errors',0)
                    if values.get('mean_cpl') is not None:
                        target['CPLTotal']+=values['mean_cpl']*values.get('moves',0);target['CPLMoves']+=values.get('moves',0)
        if class_totals:
            st.subheader('Post-game engine analysis')
            st.caption('Aggregates are read from the compact index; individual move records are loaded only when you open one game.')
            st.plotly_chart(px.bar(pd.DataFrame({'Classification':list(class_totals),'Moves':list(class_totals.values())}),x='Classification',y='Moves',title='Engine classifications in analyzed games'),width='stretch')
        if phase_totals:
            phase_view=pd.DataFrame([{'Phase':name,'Moves':v['Moves'],'Errors':v['Errors'],'MajorErrors':v['MajorErrors'],
                'MeanCPL':v['CPLTotal']/v['CPLMoves'] if v['CPLMoves'] else np.nan} for name,v in phase_totals.items()])
            st.dataframe(phase_view,width='stretch',hide_index=True)
        if material_totals:
            material_view=pd.DataFrame([{'Material state':name,'Moves':v['Moves'],'MajorErrors':v['MajorErrors'],
                'MeanCPL':v['CPLTotal']/v['CPLMoves'] if v['CPLMoves'] else np.nan} for name,v in material_totals.items()])
            st.dataframe(material_view,width='stretch',hide_index=True)

with tabs[4]:
    st.subheader('Opening / middlegame / endgame')
    if not phase.empty:
        pp=phase[phase.game_id.astype(str).isin(g.game_id.astype(str))]
        x=pp.groupby('phase',observed=True).agg(Moves=('Moves','sum'),CPL=('CPL','mean'),Accuracy=('Accuracy','mean'),Errors=('Errors','sum'),MajorErrors=('MajorErrors','sum')).reset_index()
        st.dataframe(x,width='stretch',hide_index=True)
        a,b=st.columns(2)
        with a: st.plotly_chart(px.bar(x,x='phase',y='CPL',title='Player CPL by phase'),width='stretch')
        with b: st.plotly_chart(px.bar(x,x='phase',y='MajorErrors',title='Mistakes + blunders by phase'),width='stretch')

with tabs[5]:
    st.subheader('Progress over time')
    q=g.dropna(subset=['date']).copy(); q['month']=q.date.dt.strftime('%Y-%m')
    x=q.groupby('month').agg(Games=('game_id','nunique'),WinRate=('win','mean'),Accuracy=('accuracy','mean'),SourceACPL=('source_acpl','mean'),PlayerCPL=('player_cpl','mean'),Blunders=('blunders','mean')).reset_index()
    st.plotly_chart(px.line(x,x='month',y='Accuracy',markers=True,title='Accuracy over time'),width='stretch')
    st.plotly_chart(px.line(x,x='month',y='PlayerCPL',markers=True,title='Player CPL over time'),width='stretch')
    st.plotly_chart(px.line(x,x='month',y='WinRate',markers=True,title='Win rate over time'),width='stretch')
    q['week']=q.date.dt.strftime('%G-W%V')
    weekly=q.groupby('week').agg(Games=('game_id','nunique'),WinRate=('win','mean'),Accuracy=('accuracy','mean'),PlayerCPL=('player_cpl','mean')).reset_index()
    st.plotly_chart(px.line(weekly,x='week',y='WinRate',markers=True,title='Weekly results'),width='stretch')
    if 'engine_accuracy' in q and q.engine_accuracy.notna().any():
        st.plotly_chart(px.line(q.dropna(subset=['engine_accuracy']),x='date',y='engine_accuracy',markers=True,title='Independent engine accuracy per analyzed game'),width='stretch')
    if 'player_rating' in q and q.player_rating.notna().any():
        st.plotly_chart(px.line(q.sort_values('date'),x='date',y='player_rating',markers=True,title='Official player rating recorded per game'),width='stretch')
        st.caption('Il rating è il valore registrato per ogni partita; la piattaforma pubblica qui non fornisce necessariamente lo storico giornaliero completo.')
    if not period_data.dropna(subset=['date']).empty:
        st.subheader('Compare equal-length periods')
        period_latest=pd.to_datetime(period_data.date,utc=True,errors='coerce').max().date()
        period_days=st.number_input('Days per comparison period',7,365,30,key='period_days')
        recent_end=st.date_input('End of recent period',period_latest,key='period_recent_end')
        recent_start=recent_end-pd.Timedelta(days=int(period_days)-1)
        previous_end=recent_start-pd.Timedelta(days=1)
        previous_start=previous_end-pd.Timedelta(days=int(period_days)-1)
        period_dates=pd.to_datetime(period_data.date,utc=True,errors='coerce').dt.date
        period_rows=[]
        for label,start_date,end_date in [('Previous',previous_start,previous_end),('Recent',recent_start,recent_end)]:
            part=period_data[(period_dates>=start_date)&(period_dates<=end_date)]
            summary=result_summary(part)
            period_rows.append({'Period':label,'Start':start_date,'End':end_date,'Games':summary['games'],
                'Wins':summary['wins'],'Draws':summary['draws'],'Losses':summary['losses'],
                'WinRatePct':summary['win_pct'],'Accuracy':part.accuracy.mean(),
                'PlayerCPL':part.player_cpl.mean(),'BlundersPerGame':part.blunders.mean()})
        st.dataframe(pd.DataFrame(period_rows),width='stretch',hide_index=True)
        st.caption('The comparison uses the selected color, cadenza, rating-band and opponent filters, and can span beyond the dashboard’s current date range. Dates use the stored UTC game date. These are descriptive comparisons, not causal effects.')

with tabs[6]:
    st.subheader('Context: opponent strength and playing time')
    a,b=st.columns(2)
    with a:
        rating_agg={'Games':('game_id','size'),'WinRate':('win','mean'),'Accuracy':('accuracy','mean'),'PlayerCPL':('player_cpl','mean')}
        if 'engine_accuracy' in g:
            rating_agg['EngineAccuracy']=('engine_accuracy','mean'); rating_agg['EngineGames']=('engine_accuracy','count')
        x=g.groupby('rating_band',observed=True).agg(**rating_agg).reset_index()
        st.plotly_chart(px.bar(x,x='rating_band',y='WinRate',title='Win rate by opponent rating band'),width='stretch')
    with b:
        calendar_results=grouped_result_summary(g,'day_of_week')
        calendar_quality=g.groupby('day_of_week',dropna=False,observed=True).agg(Accuracy=('accuracy','mean')).reset_index()
        x=calendar_results.merge(calendar_quality,on='day_of_week',how='left')
        day_order=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        x['day_of_week']=pd.Categorical(x.day_of_week,categories=day_order,ordered=True)
        x=x.sort_values('day_of_week')
        st.plotly_chart(px.bar(x,x='day_of_week',y='WinRate',title='Win rate by weekday'),width='stretch')
        st.dataframe(x,width='stretch',hide_index=True)
    if g.start_time_utc.notna().any():
        hour_col='hour_local' if 'hour_local' in g else 'hour_utc'
        if hour_col not in g: g[hour_col]=g.start_time_utc.dt.hour
        timed=g.dropna(subset=['start_time_utc'])
        x=grouped_result_summary(timed,hour_col)
        hour_quality=timed.groupby(hour_col,dropna=False,observed=True).agg(Accuracy=('accuracy','mean')).reset_index()
        x=x.merge(hour_quality,on=hour_col,how='left')
        if 'engine_accuracy' in timed:
            engine_quality=timed.groupby(hour_col,dropna=False,observed=True).agg(
                EngineAccuracy=('engine_accuracy','mean'),EngineGames=('engine_accuracy','count')).reset_index()
            x=x.merge(engine_quality,on=hour_col,how='left')
        x=x.sort_values(hour_col)
        st.dataframe(x,width='stretch',hide_index=True)
        st.plotly_chart(px.line(x,x=hour_col,y='WinRate',markers=True,title=f'Win rate by local start hour ({timezone_name})'),width='stretch')
    st.caption(f'Calendar views convert trusted PGN timestamps from UTC to {timezone_name}. Missing timestamps are omitted.')
    if 'local_start_time' in g and g.local_start_time.notna().any():
        sessions=g.dropna(subset=['local_start_time']).sort_values('local_start_time').copy()
        sessions['new_session']=sessions.local_start_time.diff().gt(pd.Timedelta(minutes=30)).fillna(True)
        sessions['session_id']=sessions.new_session.cumsum()
        sessions['game_in_session']=sessions.groupby('session_id').cumcount()+1
        session_view=sessions.groupby('game_in_session').agg(Games=('game_id','size'),WinRate=('win','mean'),Accuracy=('accuracy','mean')).reset_index()
        st.subheader('Results as a play session progresses')
        st.dataframe(session_view,width='stretch',hide_index=True)
        st.caption('Sessions are reconstructed as timestamp gaps over 30 minutes. These descriptive associations do not show that session length causes performance changes.')

with tabs[7]:
    st.subheader('Game explorer')
    search=st.text_input('Search player / opening / game id')
    ge=g.copy()
    if search:
        mask=pd.Series(False,index=ge.index)
        for c in ['game_id','archive_game_id','game_url','white','black','opening_name','eco','termination']:
            if c in ge: mask |= ge[c].astype(str).str.contains(search,case=False,na=False)
        ge=ge[mask]
    ge=ge.sort_values('date',ascending=False)
    show=[c for c in ['date','game_id','archive_game_id','white','black','player_result','player_color','player_rating','opponent_rating','rating_diff','time_format','eco','opening_name','accuracy','source_acpl','player_cpl','mate_score_sentinels','blunders','mistakes','inaccuracies','key_moments','engine_key_moments','engine_lost_winning_advantage','engine_missed_mates','game_url'] if c in ge]
    st.dataframe(ge[show],width='stretch',hide_index=True)
    if not ge.empty:
        game_ids=ge.game_id.dropna().astype(str).drop_duplicates().tolist()
        if not search and len(game_ids)>500:
            game_ids=game_ids[:500]
            st.caption('La revisione diretta propone le 500 partite più recenti; cerca qui sopra un avversario o un ID per selezionare partite meno recenti.')
        game_lookup=ge.drop_duplicates('game_id').set_index(ge.drop_duplicates('game_id').game_id.astype(str))
        def game_label(game_id):
            row=game_lookup.loc[game_id]
            white=row.get('white'); black=row.get('black')
            white='White' if pd.isna(white) else str(white)
            black='Black' if pd.isna(black) else str(black)
            date=pd.to_datetime(row.get('date'),utc=True,errors='coerce')
            day=date.strftime('%Y-%m-%d') if pd.notna(date) else 'Unknown date'
            return f'{day} · {white} – {black} · {row.get("player_result", "Unknown")}'
        selected_id=st.selectbox('Review one game move by move',game_ids,format_func=game_label,key='review_game_id')
        selected=ge[ge.game_id.astype(str)==selected_id].iloc[0]
        match_engine=engine_moves[engine_moves.game_id.astype(str)==selected_id].sort_values('ply') if not engine_moves.empty else pd.DataFrame()
        if match_engine.empty and not engine_games.empty:
            engine_match=engine_games[engine_games.game_id.astype(str)==selected_id]
            # API archive IDs can remain distinct from source/legacy IDs. Match
            # on the selected row's archive ID as a stable fallback, even when
            # a legacy alias is unavailable or ambiguous.
            selected_archive_id=selected.get('archive_game_id')
            if engine_match.empty and pd.notna(selected_archive_id) and 'game_id' in engine_games:
                engine_match=engine_games[engine_games.game_id.astype(str)==str(selected_archive_id)]
            if not engine_match.empty and engine_match.iloc[0].get('cache_key'):
                try:
                    from src.chess_data import connect
                    with connect() as conn:
                        cached=conn.execute('SELECT result_json FROM engine_cache WHERE cache_key=?',
                                             (engine_match.iloc[0].cache_key,)).fetchone()
                    if cached:
                        detail=json.loads(cached[0])
                        match_engine=pd.DataFrame(detail.get('moves',[]))
                        if not match_engine.empty:
                            match_engine['game_id']=selected_id
                            match_engine['archive_game_id']=engine_match.iloc[0].get('archive_game_id')
                            match_engine=match_engine.sort_values('ply')
                except (OSError,ValueError,TypeError,sqlite3.Error):
                    match_engine=pd.DataFrame()
        if not match_engine.empty:
            white_player=str(selected.get('player_color','')).lower()=='white'
            sign=1 if white_player else -1
            match_engine=match_engine.copy()
            raw_eval=pd.to_numeric(match_engine.eval_after_cp_white,errors='coerce')*sign
            match_engine['eval_player_cp']=raw_eval
            match_engine['eval_plot_cp']=raw_eval.clip(-1000,1000)
            st.subheader('Post-game engine evaluation')
            st.caption('Valutazioni dal tuo punto di vista; il grafico è limitato a ±10 pedoni per mantenerlo leggibile. La tabella conserva i punteggi interi e i casi di matto.')
            fig=px.line(match_engine,x='ply',y='eval_plot_cp',markers=True,hover_data=['move_number','color','san','eval_player_cp','classification'],
                        labels={'ply':'Ply','eval_plot_cp':'Evaluation (cp, clipped)','eval_player_cp':'Evaluation (cp)'})
            fig.add_hline(y=0,line_dash='dash',line_color='gray')
            st.plotly_chart(fig,width='stretch')
            detail_cols=[c for c in ['ply','move_number','color','san','best_move','phase','position_before','eval_before_cp_mover','eval_after_cp_mover','player_material_balance','player_in_material_disadvantage','cpl','classification','lost_winning_advantage','missed_mate_opportunity','exposed_piece_heuristic','fen_before'] if c in match_engine]
            st.dataframe(match_engine[detail_cols],width='stretch',hide_index=True)
            st.caption('Le classi di posizione sono dal punto di vista del giocatore che muove (vantaggiosa ≥+200 cp, svantaggiosa ≤−200 cp). “Lost winning advantage” segnala solo il passaggio da ≥+200 cp a <+200 cp dopo una tua mossa; CPL è la perdita stimata per chi ha mosso.')
        else:
            source_game=m[m.game_id.astype(str)==selected_id].sort_values('ply') if not m.empty else pd.DataFrame()
            if not source_game.empty:
                st.subheader('Move-by-move source analysis')
                st.caption('Questi CPL e classi provengono dai file di analisi locali; non contengono la valutazione della posizione necessaria per tracciare il vantaggio Stockfish.')
                detail_cols=[c for c in ['ply','move_number','color','san','phase','cpl','accuracy','classification','is_player_move','mate_score_sentinel'] if c in source_game]
                st.dataframe(source_game[detail_cols],width='stretch',hide_index=True)
            else:
                st.info('Nessun dato mossa disponibile per questa partita. Importa un PGN o calcola un’analisi motore post-partita.')

with tabs[8]:
    st.subheader('Evidence-based practice priorities')
    notes=[]
    for c,label in [('player_major_errors','Mistakes and blunders'),('blunders','Blunders'),('mistakes','Mistakes'),('inaccuracies','Inaccuracies')]:
        if c in g and g[c].notna().any(): notes.append((label,float(g[c].mean())))
    if 'player_cpl' in g and g.player_cpl.notna().any(): notes.append(('Average own-move CPL',float(g.player_cpl.mean())))
    if 'engine_accuracy' in g and g.engine_accuracy.notna().any():
        engine_coverage=int(g.engine_accuracy.notna().sum())
        notes.append((f'Independent engine accuracy ({engine_coverage} analyzed games)',float(g.engine_accuracy.mean())))
    if 'accuracy' in g and g.accuracy.notna().any(): notes.append(('Mean dataset accuracy (source metric)',float(g.accuracy.mean())))
    if not notes:
        st.info('Le partite importate dall’archivio PubAPI/PGN contengono metadati e mosse, ma non una revisione Stockfish. Aggiungi analisi motore post-partita per ottenere priorità basate sugli errori.')
    else:
        for label,value in notes:
            st.write(f'• **{label}:** {value:.1f} in media per partita; verifica nel filtro e nell’esploratore prima di trarre conclusioni.')
        st.caption('Percentuali, centipawn ed errori per partita hanno scale diverse: queste misure sono descrittive e non vengono ordinate tra loro.')
    if not engine_moves.empty:
        own=engine_moves[engine_moves.get('player_move',False).astype(bool) & engine_moves.game_id.astype(str).isin(g.game_id.astype(str))].copy()
        if not own.empty:
            own['major_error']=own.classification.isin(['mistake','blunder'])
            if 'cpl_for_average' not in own:
                own['cpl_for_average']=pd.to_numeric(own.cpl,errors='coerce').where(pd.to_numeric(own.cpl,errors='coerce').abs()<10000)
            phase_rank=own.groupby('phase').agg(Moves=('ply','size'),MajorErrors=('major_error','sum'),MeanCPL=('cpl_for_average','mean')).reset_index()
            phase_rank['MajorErrorRate']=phase_rank.MajorErrors/phase_rank.Moves
            phase_rank=phase_rank.sort_values(['MajorErrorRate','Moves'],ascending=[False,False])
            worst=phase_rank.iloc[0]
            st.write(f"**Practice priority:** review {worst.phase} positions first ({int(worst.MajorErrors)} mistakes/blunders in {int(worst.Moves)} analyzed moves; mean loss {worst.MeanCPL:.0f} cp). This is a descriptive signal from the analyzed sample, not a causal diagnosis.")
            evidence=own[own.phase==worst.phase].sort_values('cpl',ascending=False).head(5)
            st.dataframe(evidence[[c for c in ['game_id','move_number','san','best_move','cpl','classification'] if c in evidence]],width='stretch',hide_index=True)
    elif not m.empty:
        own=m[m.game_id.astype(str).isin(g.game_id.astype(str)) & m.is_player_move.astype(bool)].copy()
        if not own.empty:
            phase_rank=own.groupby('phase').agg(Moves=('ply','size'),Errors=('is_error','sum'),MeanCPL=('cpl','mean')).reset_index()
            phase_rank['ErrorRate']=phase_rank.Errors/phase_rank.Moves
            if phase_rank.ErrorRate.notna().any():
                worst=phase_rank.sort_values(['ErrorRate','Moves'],ascending=[False,False]).iloc[0]
                st.write(f"**Practice priority:** review {worst.phase} moves first ({int(worst.Errors)} classified errors among {int(worst.Moves)} analyzed moves; mean CPL {worst.MeanCPL:.0f}). Evidence uses the existing analysis file's move labels.")
                examples=own[own.phase==worst.phase].sort_values('cpl',ascending=False).head(5)
                st.dataframe(examples[[c for c in ['game_id','move_number','san','cpl','classification'] if c in examples]],width='stretch',hide_index=True)
    if 'opening_name' in g:
        open_stats=g.groupby(['player_color','opening_name'],dropna=False).agg(Games=('game_id','nunique'),WinRate=('win','mean'),Accuracy=('accuracy','mean'),OpponentRating=('opponent_rating','mean')).reset_index()
        eligible=open_stats[open_stats.Games>=min_games].sort_values(['WinRate','Games'],ascending=[False,False])
        st.write('Aperture con almeno il numero minimo di osservazioni selezionato:')
        st.dataframe(eligible,width='stretch',hide_index=True)
    csv=g.to_csv(index=False).encode('utf-8')
    st.download_button('Export filtered games CSV',csv,'chess_games_filtered.csv','text/csv')
    rs=result_summary(g); streak=longest_streak(g)
    report=(f"Independent Chess Analytics report\nGames in filtered set: {len(g)}\n"
            f"Wins/draws/losses: {rs['wins']}/{rs['draws']}/{rs['losses']}\n"
            f"Win/draw/loss percentages: {rs['win_pct'] or 0:.1f}/{rs['draw_pct'] or 0:.1f}/{rs['loss_pct'] or 0:.1f}\n"
            f"Mean source accuracy: {g.accuracy.mean():.1f}\n"
            f"Mean independent engine accuracy ({int(g.engine_accuracy.notna().sum())} analyzed games): {g.engine_accuracy.mean():.1f}\n" if 'engine_accuracy' in g and g.engine_accuracy.notna().any() else
            f"Independent Chess Analytics report\nGames in filtered set: {len(g)}\n"
            f"Wins/draws/losses: {rs['wins']}/{rs['draws']}/{rs['losses']}\n"
            f"Win/draw/loss percentages: {rs['win_pct'] or 0:.1f}/{rs['draw_pct'] or 0:.1f}/{rs['loss_pct'] or 0:.1f}\n"
            f"Mean source accuracy: {g.accuracy.mean():.1f}\n")
    report+=f"Longest win/draw/loss streaks: {streak['Win']}/{streak['Draw']}/{streak['Loss']}\n"
    report+='Accuracy and classifications depend on existing analysis data or the documented independent engine metric; neither claims to reproduce proprietary platform scoring.'
    st.download_button('Export summary report',report,'chess_report.txt','text/plain')
    if engine_path.exists():
        st.caption(f'Detailed engine JSON is stored locally ({engine_path.stat().st_size/1024/1024:.0f} MiB). Preparing a browser download is optional and can use substantial memory.')
        export_key='engine_analysis_download_bytes'
        if st.button('Prepare full engine analysis download',key='prepare_engine_analysis_download'):
            try:
                with st.spinner('Preparing the full move-level export…'):
                    st.session_state[export_key]=engine_path.read_bytes()
            except OSError as exc:
                st.error(f'Export preparation failed: {exc}')
        if export_key in st.session_state:
            st.download_button('Download full engine analysis JSON',st.session_state[export_key],
                               'engine_analysis.json','application/json',key='download_engine_analysis')
            if st.button('Release prepared export from memory',key='release_engine_analysis_download'):
                del st.session_state[export_key]
