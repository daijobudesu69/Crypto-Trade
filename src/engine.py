"""V1.2 backtest engine — transparent daily event loop.

No-lookahead contract:
  * All signals use features computed through day t (close of t).
  * Entry fills at OPEN of day t+1.
  * Barriers (SL/TP anchored at fill) are evaluated from day t+1 (the entry day itself,
    since fill is at the open) through day t+HOLD_MAX.
  * Conservative same-day rule: if a day's range touches both SL and TP, assume SL first.
  * Gap rule: if a day OPENS beyond a barrier, fill at that open (worse for SL, better for TP).

Costs: fee_side + slip_side charged on entry and exit notionals (default 0.10% + 0.05% per side).
R-multiple: (exit/entry - 1) / sl_dist_pct  minus  round-trip cost / sl_dist_pct.
"""
from __future__ import annotations
import pandas as pd, numpy as np
from dataclasses import dataclass, field


@dataclass
class Config:
    rank_col: str = "mom_14"            # ranking variable (C1)
    top_n: int = 3                      # candidates taken per day
    use_c2: bool = True                 # trend gate
    use_c5: bool = True                 # volume gate
    use_c6: bool = True                 # extension gate
    sl_atr: float = 1.5
    tp_atr: float = 3.0
    hold_max: int = 14                  # calendar days incl. entry day
    min_history: int = 60               # days listed before eligible
    liq_floor: float = 5e6              # 20d median quote-vol (point-in-time)
    fee_side: float = 0.0010            # Binance spot taker
    slip_side: float = 0.0005
    require_mom_pos: bool = False       # optionally require mom>0 (kill-switch style)
    btc_regime_col: str | None = None   # e.g. "btc_above_sma200" to gate entries (folded into regime_cols)
    regime_cols: tuple[str, ...] = ()   # AND of date-level boolean columns in the `regime` frame (T3/T4/T5/T6)
    extra_gate_cols: tuple[str, ...] = ()  # AND of symbol-level boolean columns in `panel` (T10/T11)
    corr_cap: float | None = None       # reject a candidate if trailing corr to an open position exceeds this (T2d)
    corr_lookback: int = 60
    max_positions: int = 3
    rank_ascending: bool = False        # False = take highest rank_col
    entry_price_col: str = "next_open"  # T8 entry-timing grid: "next_open" (default) or "next_close"
    start: str = "2019-01-01"
    end: str = "2026-08-16"
    label: str = "base"


def run(panel: pd.DataFrame, btc_regime: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Returns one row per trade."""
    p = panel
    dates = p.index.get_level_values(0).unique().sort_values()
    dates = dates[(dates >= pd.Timestamp(cfg.start, tz="UTC")) & (dates <= pd.Timestamp(cfg.end, tz="UTC"))]
    date_pos = {d: i for i, d in enumerate(dates)}

    # Wide frames for fast barrier walk
    O = p["open"].unstack();  H = p["high"].unstack();  L = p["low"].unstack();  C = p["close"].unstack()

    trades = []
    open_pos: dict[str, dict] = {}   # symbol -> position dict

    for d in dates[:-1]:                                   # last day can't produce a t+1 entry
        di = date_pos[d]
        nxt = dates[di + 1]

        # ---- 1) manage open positions on day d (barriers use day-d OHLC) ----
        for sym in list(open_pos):
            pos = open_pos[sym]
            if d < pos["entry_date"] or (pos["skip_own_day"] and d == pos["entry_date"]):
                # skip_own_day: entry filled at that day's CLOSE, so that day's low/high happened
                # BEFORE the position existed and must not be checked against barriers (T8 entry-
                # timing grid, next_close variant) -- only "next_open" fills can use their own day.
                continue
            o, h, l, c = O.at[d, sym], H.at[d, sym], L.at[d, sym], C.at[d, sym]
            if np.isnan(o):
                continue
            exit_px, reason = None, None
            if o <= pos["sl"]:
                exit_px, reason = o, "sl_gap"
            elif o >= pos["tp"]:
                exit_px, reason = o, "tp_gap"
            elif l <= pos["sl"]:
                exit_px, reason = pos["sl"], "sl"          # conservative: SL before TP
            elif h >= pos["tp"]:
                exit_px, reason = pos["tp"], "tp"
            elif (d - pos["entry_date"]).days >= cfg.hold_max - 1:
                exit_px, reason = c, "time"
            if exit_px is not None:
                gross = exit_px / pos["entry_px"] - 1.0
                rt_cost = 2 * (cfg.fee_side + cfg.slip_side)
                net = gross - rt_cost
                sl_dist = pos["sl_dist_pct"]
                trades.append(dict(symbol=sym, signal_date=pos["signal_date"], entry_date=pos["entry_date"],
                                   exit_date=d, days_held=(d - pos["entry_date"]).days + 1,
                                   entry_px=pos["entry_px"], exit_px=exit_px, reason=reason,
                                   gross_ret=gross, net_ret=net,
                                   R_gross=gross / sl_dist, R_net=net / sl_dist,
                                   sl_dist_pct=sl_dist, atr_pct=pos["atr_pct"],
                                   rank_val=pos["rank_val"], rv56=pos["rv56"],
                                   ext=pos["ext"], rs_btc=pos["rs_btc"]))
                del open_pos[sym]

        # ---- 2) generate signals from day-d close, fill at day-(d+1) open ----
        slots = cfg.max_positions - len(open_pos)
        if slots <= 0:
            continue
        try:
            day = p.xs(d, level=0)
        except KeyError:
            continue
        elig = day[(day["days_listed"] >= cfg.min_history)
                   & (day["med_qvol_20"] >= cfg.liq_floor)
                   & day["atr14"].notna() & day[cfg.rank_col].notna()
                   & day[cfg.entry_price_col].notna()]
        if cfg.use_c2:
            elig = elig[elig["c2_pass"]]
        if cfg.use_c5:
            elig = elig[elig["c5_pass"]]
        if cfg.use_c6:
            elig = elig[elig["c6_pass"]]
        if cfg.require_mom_pos:
            elig = elig[elig[cfg.rank_col] > 0]
        regime_cols = list(cfg.regime_cols) + ([cfg.btc_regime_col] if cfg.btc_regime_col else [])
        for rc in regime_cols:
            if d in btc_regime.index:
                val = btc_regime.loc[d, rc]
                if pd.isna(val) or not bool(val):   # missing/NaN regime reading fails safe (excluded)
                    elig = elig.iloc[0:0]
                    break
            else:
                elig = elig.iloc[0:0]
                break
        for gc in cfg.extra_gate_cols:
            elig = elig[elig[gc].fillna(False)]
        elig = elig[~elig.index.isin(open_pos.keys())]
        if elig.empty:
            continue
        ranked = elig.sort_values(cfg.rank_col, ascending=cfg.rank_ascending)
        taken = 0
        for sym, row in ranked.iterrows():
            if taken >= slots:
                break
            if cfg.corr_cap is not None and open_pos:
                look = dates[max(0, di - cfg.corr_lookback + 1):di + 1]
                cwin = C.loc[C.index.isin(look)]
                cand_ret = cwin[sym].pct_change().dropna() if sym in cwin.columns else pd.Series(dtype=float)
                rejected = False
                for osym in open_pos:
                    if osym not in cwin.columns:
                        continue
                    o_ret = cwin[osym].pct_change().dropna()
                    joined = pd.concat([cand_ret, o_ret], axis=1).dropna()
                    if len(joined) >= 20 and joined.iloc[:, 0].corr(joined.iloc[:, 1]) > cfg.corr_cap:
                        rejected = True
                        break
                if rejected:
                    continue
            entry_px = row[cfg.entry_price_col]
            atr = row["atr14"]
            sl = entry_px - cfg.sl_atr * atr
            tp = entry_px + cfg.tp_atr * atr
            if sl <= 0 or entry_px <= 0:
                continue
            open_pos[sym] = dict(signal_date=d, entry_date=nxt, entry_px=entry_px, sl=sl, tp=tp,
                                 sl_dist_pct=cfg.sl_atr * atr / entry_px, atr_pct=row["atr_pct"],
                                 rank_val=row[cfg.rank_col], rv56=row["rv56"],
                                 ext=row["ext_ma20_atr"], rs_btc=row["rs_btc_14"],
                                 skip_own_day=(cfg.entry_price_col == "next_close"))
            taken += 1

    # force-close remaining at final close
    last = dates[-1]
    for sym, pos in open_pos.items():
        c = C.at[last, sym]
        if np.isnan(c):
            continue
        gross = c / pos["entry_px"] - 1.0
        net = gross - 2 * (cfg.fee_side + cfg.slip_side)
        trades.append(dict(symbol=sym, signal_date=pos["signal_date"], entry_date=pos["entry_date"],
                           exit_date=last, days_held=(last - pos["entry_date"]).days + 1,
                           entry_px=pos["entry_px"], exit_px=c, reason="eod",
                           gross_ret=gross, net_ret=net,
                           R_gross=gross / pos["sl_dist_pct"], R_net=net / pos["sl_dist_pct"],
                           sl_dist_pct=pos["sl_dist_pct"], atr_pct=pos["atr_pct"],
                           rank_val=pos["rank_val"], rv56=pos["rv56"], ext=pos["ext"], rs_btc=pos["rs_btc"]))
    t = pd.DataFrame(trades)
    if not t.empty:
        t["label"] = cfg.label
    return t


def summarize(t: pd.DataFrame, label: str | None = None) -> dict:
    from scipy import stats as st
    if t.empty:
        return dict(label=label, n=0)
    r = t["R_net"]
    tt = st.ttest_1samp(r, 0.0)
    win = (r > 0).mean()
    eq = (1 + 0.03 * r).cumprod()          # 3% risk per trade, sequential approx
    peak = eq.cummax()
    return dict(label=label or t["label"].iloc[0], n=len(t), mean_R=r.mean(), median_R=r.median(),
                win_rate=win, t_stat=tt.statistic, p_val=tt.pvalue,
                mean_hold=t["days_held"].mean(),
                tp_rate=(t["reason"].isin(["tp", "tp_gap"])).mean(),
                sl_rate=(t["reason"].isin(["sl", "sl_gap"])).mean(),
                time_rate=(t["reason"] == "time").mean(),
                growth_3pct=eq.iloc[-1], maxdd_3pct=(eq / peak - 1).min())
