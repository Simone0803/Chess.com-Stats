"""Independent, auditable aggregations over the locally available game rows."""
from __future__ import annotations

import pandas as pd
import math


def mover_cpl(before_cp_white, after_cp_white, mover_white: bool) -> int | None:
    """Nonnegative centipawn loss from mover POV; missing engine scores stay unknown."""
    if before_cp_white is None or after_cp_white is None: return None
    try:
        if math.isnan(float(before_cp_white)) or math.isnan(float(after_cp_white)): return None
    except (TypeError, ValueError): return None
    sign = 1 if mover_white else -1
    return max(0, int(sign * before_cp_white) - int(sign * after_cp_white))


def classify_cpl(cpl: float) -> str:
    if cpl is None or pd.isna(cpl): return "unknown"
    if cpl >= 200: return "blunder"
    if cpl >= 100: return "mistake"
    if cpl >= 50: return "inaccuracy"
    return "good"


def move_accuracy(cpl: float) -> float | None:
    """Independent bounded score. 0 CPL=100; high loss approaches zero."""
    if cpl is None or pd.isna(cpl): return None
    return 100.0 * math.exp(-max(0.0, float(cpl)) / 100.0)


def comparable_cpl(cpl):
    """Return arithmetic CPL unless a mate/forced-win sentinel (absolute >=10k cp)."""
    if cpl is None: return None
    try:
        if math.isnan(float(cpl)): return float("nan")
    except (TypeError, ValueError): return None
    return None if abs(float(cpl)) >= 10000 else float(cpl)


def result_summary(games: pd.DataFrame) -> dict:
    result = games.get("player_result", pd.Series(dtype="object")).astype("string").str.title()
    counts = result.value_counts()
    n = int(sum(counts.get(label, 0) for label in ("Win", "Draw", "Loss")))
    return {"games": int(len(games)), "rated_results": n,
            "wins": int(counts.get("Win", 0)), "draws": int(counts.get("Draw", 0)),
            "losses": int(counts.get("Loss", 0)),
            "win_pct": 100 * counts.get("Win", 0) / n if n else None,
            "draw_pct": 100 * counts.get("Draw", 0) / n if n else None,
            "loss_pct": 100 * counts.get("Loss", 0) / n if n else None}


def grouped_result_summary(games: pd.DataFrame, by) -> pd.DataFrame:
    """Return per-group W/D/L counts and rates over known game results only."""
    keys=[by] if isinstance(by,str) else list(by)
    if games.empty or not keys or any(key not in games for key in keys):
        return pd.DataFrame(columns=[*keys,"Games","KnownResults","Wins","Draws","Losses",
                                     "WinRate","DrawRate","LossRate"])
    x=games.copy()
    if "game_id" in x:
        x=x.drop_duplicates("game_id",keep="first")
    x["_result"]=x.get("player_result",pd.Series(index=x.index,dtype="object")).astype("string").str.title()
    for label in ("Win","Draw","Loss"):
        x[f"_{label.lower()}"]=x["_result"].eq(label).fillna(False).astype(int)
    x["_known"]=x["_result"].isin(("Win","Draw","Loss")).astype(int)
    x["_game"]=1
    grouped=x.groupby(keys,dropna=False,observed=True).agg(
        Games=("_game","sum"),KnownResults=("_known","sum"),
        Wins=("_win","sum"),Draws=("_draw","sum"),Losses=("_loss","sum")).reset_index()
    denominator=grouped.KnownResults.where(grouped.KnownResults.ne(0))
    for label,count_column in (("Win","Wins"),("Draw","Draws"),("Loss","Losses")):
        grouped[f"{label}Rate"]=grouped[count_column] / denominator
    return grouped


def rating_summary(games: pd.DataFrame) -> dict:
    if "player_rating" not in games:
        return {"peak": None, "peak_date": None, "change": None}
    x = games.copy()
    x["player_rating"] = pd.to_numeric(x.player_rating, errors="coerce")
    x = x.dropna(subset=["player_rating"])
    if x.empty: return {"peak": None, "peak_date": None, "change": None}
    if "date" in x:
        x["_date_sort"] = pd.to_datetime(x["date"], utc=True, errors="coerce")
        x = x.sort_values("_date_sort", kind="stable", na_position="last").reset_index(drop=True)
    peak_idx = x.player_rating.idxmax()
    return {"peak": float(x.loc[peak_idx, "player_rating"]),
            "peak_date": str(x.loc[peak_idx, "date"]) if "date" in x and pd.notna(x.loc[peak_idx, "date"]) else None,
            "change": float(x.player_rating.iloc[-1] - x.player_rating.iloc[0])}


def longest_streak(games: pd.DataFrame) -> dict:
    if "player_result" not in games: return {"Win": 0, "Draw": 0, "Loss": 0}
    x = games.copy()
    if "date" in x: x = x.sort_values("date", kind="stable")
    results = x.player_result.astype("string").str.title()
    best = {k: 0 for k in ("Win", "Draw", "Loss")}
    for value in best:
        run = 0
        for result in results:
            run = run + 1 if result == value else 0
            best[value] = max(best[value], run)
    return best


def prepare_filters(games: pd.DataFrame, start=None, end=None, color=None,
                    time_class=None, rated=None, opponent_band=None, timezone_name="UTC"):
    """Filters date inclusive and converts trusted UTC timestamps for local time.

    Missing columns/values are retained unless a corresponding filter is supplied.
    """
    x = games.copy()
    if "date" in x:
        dates = pd.to_datetime(x.date, utc=True, errors="coerce")
        if start is not None: x = x.loc[dates.dt.date >= start]
        if end is not None:
            dates = pd.to_datetime(x.date, utc=True, errors="coerce")
            x = x.loc[dates.dt.date <= end]
    if color and "player_color" in x: x = x[x.player_color.astype(str).str.lower() == color.lower()]
    if time_class and "time_category" in x: x = x[x.time_category == time_class]
    if rated is not None and "rated" in x: x = x[x.rated.astype("boolean") == rated]
    if opponent_band and "rating_band" in x: x = x[x.rating_band == opponent_band]
    if "start_time_utc" in x:
        x["local_start_time"] = pd.to_datetime(x.start_time_utc, utc=True, errors="coerce").dt.tz_convert(timezone_name)
    return x


def monthly_summary(games: pd.DataFrame) -> pd.DataFrame:
    if games.empty or "date" not in games: return pd.DataFrame()
    x = games.copy()
    x["date"] = pd.to_datetime(x.date, utc=True, errors="coerce")
    x = x.dropna(subset=["date"])
    if x.empty: return pd.DataFrame()
    x["month"] = x.date.dt.strftime("%Y-%m")
    agg = {"game_id": "nunique"}
    for col in ("win", "draw", "loss", "accuracy", "acpl", "player_rating", "player_cpl"):
        if col in x: agg[col] = "mean" if col != "game_id" else "nunique"
    return x.groupby("month").agg(agg).rename(columns={"game_id": "games"}).reset_index()

