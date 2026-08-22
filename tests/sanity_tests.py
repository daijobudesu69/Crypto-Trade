"""Unit sanity tests on synthetic data (repo produksi V1.4) — run BEFORE trusting any result.

T1-T12 cover engine.py (barriers, costs, slots, gates, entry timing).
T13-T17 cover FEATURE and REGIME COLUMN construction, which sits outside engine.py and was
therefore uncovered by the original 13 tests — the gap that let KOREKSI_V1_3.md K1 through
(`btc_dd_shallow` thresholded on a full-sample median, i.e. an Aug-2026 number applied to 2019).
"""
import pandas as pd, numpy as np
from engine import Config, run

def make_panel(prices: dict[str, list], extra: dict | None = None):
    """Build a minimal panel from OHLC tuples per symbol."""
    frames = []
    for sym, rows in prices.items():
        idx = pd.date_range("2024-01-01", periods=len(rows), freq="D", tz="UTC")
        df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
        df["volume"] = 1e6; df["quote_volume"] = 1e9
        df["days_listed"] = np.arange(len(df)); df["med_qvol_20"] = 1e9
        df["atr14"] = 10.0                      # fixed ATR for predictable barriers
        df["atr_pct"] = df["atr14"] / df["close"]
        df["mom_14"] = 0.5                      # everyone signals
        df["c2_pass"] = True; df["c5_pass"] = True; df["c6_pass"] = True
        df["rv56"] = 0.03; df["ext_ma20_atr"] = 1.0; df["rs_btc_14"] = 0.0
        df["next_open"] = df["open"].shift(-1)
        df["next_close"] = df["close"].shift(-1)
        df["symbol"] = sym
        frames.append(df)
    p = pd.concat(frames).reset_index().rename(columns={"index": "date"}).set_index(["date", "symbol"]).sort_index()
    if extra:
        for k, v in extra.items():
            p[k] = v
    return p

BR = pd.DataFrame()   # no btc regime
CFG = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=1, top_n=1)

# T1: TP hit exactly. Entry open=100 (day1). SL=85, TP=130. Day2 high=131 -> exit 130, R=+2.0
rows = [(100,101,99,100), (100,105,98,102), (103,131,100,125), (120,121,119,120)]
t = run(make_panel({"AAA": rows}), BR, CFG)
assert t.iloc[0]["reason"] == "tp" and abs(t.iloc[0]["R_gross"] - 2.0) < 1e-9, t
print("T1 pass: TP exit at exact barrier, R=+2.0")

# T2: SL hit. Day2 low=84 -> exit 85, R=-1.0
rows = [(100,101,99,100), (100,105,98,102), (103,104,84,90), (90,91,89,90)]
t = run(make_panel({"AAA": rows}), BR, CFG)
assert t.iloc[0]["reason"] == "sl" and abs(t.iloc[0]["R_gross"] + 1.0) < 1e-9, t
print("T2 pass: SL exit, R=-1.0")

# T3: both barriers same day -> SL wins (conservative)
rows = [(100,101,99,100), (100,105,98,102), (103,140,80,120), (120,121,119,120)]
t = run(make_panel({"AAA": rows}), BR, CFG)
assert t.iloc[0]["reason"] == "sl", t
print("T3 pass: same-day double touch resolves to SL")

# T4: gap down through SL -> fill at open (worse than SL). Open day2 = 80 < SL 85 -> R = (80/100-1)/0.15
rows = [(100,101,99,100), (100,105,98,102), (80,82,78,80), (80,81,79,80)]
t = run(make_panel({"AAA": rows}), BR, CFG)
assert t.iloc[0]["reason"] == "sl_gap" and abs(t.iloc[0]["R_gross"] - (-0.2/0.15)) < 1e-9, t
print("T4 pass: gap through SL fills at open, R < -1")

# T5: time exit at close of day hold_max. hold_max=3 -> exit day3 close
cfg5 = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=1, hold_max=3)
rows = [(100,101,99,100)] + [(100,102,98,101)]*6
t = run(make_panel({"AAA": rows}), BR, cfg5)
assert t.iloc[0]["reason"] == "time" and t.iloc[0]["days_held"] == 3, t
print("T5 pass: time exit after hold_max days")

# T6: no lookahead — entry price is NEXT day's open, not signal-day close
rows = [(100,101,99,100), (150,155,149,151), (151,152,150,151), (151,152,150,151)]
t = run(make_panel({"AAA": rows}), BR, cfg5)
assert abs(t.iloc[0]["entry_px"] - 150) < 1e-9, t
print("T6 pass: entry at t+1 open (150), not signal close (100)")

# T7: slot limit — 1 slot, 2 symbols signaling -> only 1 trade open at a time
rows = [(100,101,99,100)] + [(100,102,98,101)]*6
t = run(make_panel({"AAA": rows, "BBB": rows}), BR, cfg5)
d = t.groupby("entry_date").size()
assert d.max() == 1, t
print("T7 pass: max_positions respected")

# T8: costs reduce R. fee 0.1%+slip 0.05% per side => 0.3% RT; sl_dist=15% => 0.02R
cfg8 = Config(min_history=0, liq_floor=0, max_positions=1, hold_max=3)
rows = [(100,101,99,100), (100,105,98,102), (103,131,100,125), (120,121,119,120)]
t = run(make_panel({"AAA": rows}), BR, cfg8)
assert abs((t.iloc[0]["R_gross"] - t.iloc[0]["R_net"]) - 0.003/0.15) < 1e-9, t
print("T8 pass: cost deduction exact (0.02R at these params)")

print("ALL 8 ORIGINAL SANITY TESTS PASS\n")

# --- V1.3 additions: extra_gate_cols / regime_cols / corr_cap (engine.py generalized for T3/T4/T5/T6/T10/T11) ---

# T9: extra_gate_cols excludes symbols failing a symbol-level boolean gate, regardless of rank
rows = [(100, 101, 99, 100)] + [(100, 102, 98, 101)] * 10
p9 = make_panel({"AAA": rows, "BBB": rows})
p9["gate_ok"] = True
p9.loc[(slice(None), "BBB"), "gate_ok"] = False
cfg9 = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=2, extra_gate_cols=("gate_ok",))
t = run(p9, BR, cfg9)
assert set(t["symbol"]) == {"AAA"}, t
print("T9 pass: extra_gate_cols excludes symbols failing the symbol-level gate")

# T10: regime_cols (multiple, AND'd) blocks entries on any day a date-level condition is false
rows = [(100, 101, 99, 100)] + [(100, 102, 98, 101)] * 10
p10 = make_panel({"AAA": rows})
dates10 = p10.index.get_level_values(0).unique().sort_values()
regime10 = pd.DataFrame({"cond_a": True, "cond_b": False}, index=dates10)
regime10.loc[dates10[3]:, "cond_b"] = True
cfg10 = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=1,
               regime_cols=("cond_a", "cond_b"))
t = run(p10, regime10, cfg10)
assert t.iloc[0]["signal_date"] == dates10[3], t
print("T10 pass: regime_cols requires ALL date-level conditions true before entries")

# T10b: a NaN regime reading fails safe (excluded), not bool(nan)==True (which would wrongly pass)
regime10b = pd.DataFrame({"cond_a": [np.nan] * 3 + [True] * (len(dates10) - 3)}, index=dates10)
cfg10b = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=1, regime_cols=("cond_a",))
t = run(p10, regime10b, cfg10b)
assert t.iloc[0]["signal_date"] == dates10[3], t
print("T10b pass: NaN regime_cols reading is treated as gate-fail, not truthy")

# T11: corr_cap rejects a candidate too correlated with an open position, backfills from next-ranked
def _closes(rets, invert=False):
    out = [100.0]
    for r in rets[1:]:
        out.append(out[-1] * (1 + (-r if invert else r)))
    return out

def _rows(closes):
    out, prev = [], closes[0]
    for c in closes:
        o = prev
        out.append((o, max(o, c) * 1.001, min(o, c) * 0.999, c))
        prev = c
    return out

rets = [0.005 if i % 2 == 0 else -0.005 for i in range(25)]
p11 = make_panel({"AAA": _rows(_closes(rets)), "BBB": _rows(_closes(rets)), "CCC": _rows(_closes(rets, invert=True))})
p11["mom_14"] = np.nan
dates11 = p11.index.get_level_values(0).unique().sort_values()
p11.loc[(dates11[0], "AAA"), "mom_14"] = 0.9      # AAA alone signals day0 -> opens, occupies 1 of 2 slots
test_day = dates11[-2]                             # last day that can still produce a t+1 entry
p11.loc[(test_day, "BBB"), "mom_14"] = 0.9         # BBB outranks CCC but is perfectly correlated with AAA
p11.loc[(test_day, "CCC"), "mom_14"] = 0.5
cfg11 = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=2, hold_max=30,
               corr_cap=0.9, corr_lookback=60, rank_col="mom_14")
t = run(p11, BR, cfg11)
assert "CCC" in set(t["symbol"]) and "BBB" not in set(t["symbol"]), t
print("T11 pass: corr_cap rejects the correlated top-ranked candidate and backfills the slot")

# T12: entry_price_col="next_close" fills at t+1 CLOSE and does not check barriers against that
# same day's low/high (which occurred before the position existed) -- T8's entry-timing grid.
rows = [(100, 101, 99, 100), (100, 200, 50, 105), (105, 106, 104, 105), (105, 106, 104, 105),
        (105, 106, 104, 105)]
cfg12 = Config(min_history=0, liq_floor=0, fee_side=0, slip_side=0, max_positions=1, hold_max=3,
               entry_price_col="next_close")
t = run(make_panel({"AAA": rows}), BR, cfg12)
assert abs(t.iloc[0]["entry_px"] - 105) < 1e-9, t                 # filled at day1's CLOSE (105), not open (100)
assert t.iloc[0]["reason"] != "sl", t                              # day1's low=50 must NOT be checked (pre-fill)
print("T12 pass: entry_price_col='next_close' fills at close and skips that day's own barrier check")

print("\nALL 13 ENGINE SANITY TESTS PASS")

# ==========================================================================================
# V1.4.0 additions (KOREKSI_V1_3.md sec 8, action 1): no-lookahead tests for the construction
# of feature and regime columns. engine.py was already covered; column construction was not.
# ==========================================================================================
import features

# ---- the causality harness -------------------------------------------------------------
# A column is causal iff its value on day t depends only on days <= t. The operational test is
# PREFIX INVARIANCE: rebuild the column from a truncated input ending at day T; every value on
# days <= T must be identical to the full-history build. Any full-sample statistic (a .median(),
# .mean(), .quantile() taken over the whole series) breaks this immediately.

def _differs(a: pd.Series, b: pd.Series) -> np.ndarray:
    """Element-wise inequality treating NaN==NaN as equal (a warm-up NaN is not a violation)."""
    an, bn = a.to_numpy(), b.to_numpy()
    both_nan = a.isna().to_numpy() & b.isna().to_numpy()
    return (an != bn) & ~both_nan


def prefix_violations(build_fn, df: pd.DataFrame, checkpoints, cols=None) -> dict:
    """Returns {column: worst number of days whose value changed when future rows were removed}.
    An empty dict means every checked column is causal."""
    full = build_fn(df.copy())
    cols = list(full.columns) if cols is None else list(cols)
    worst = {}
    for T in checkpoints:
        pre = build_fn(df.iloc[:T].copy())
        for c in cols:
            n_bad = int(_differs(full[c].iloc[:T], pre[c]).sum())
            if n_bad:
                worst[c] = max(worst.get(c, 0), n_bad)
    return worst


# ---- synthetic BTC-like OHLCV: deterministic, trending (so a full-sample median is clearly
# ---- different from any prefix median), long enough for sma200 and a 365d expanding warm-up.
_rng = np.random.default_rng(7)
_n = 800
_ret = _rng.normal(0.0015, 0.03, _n)
_close = 100 * np.exp(np.cumsum(_ret))
SYN = pd.DataFrame({
    "open": _close * (1 + _rng.normal(0, 0.002, _n)),
    "high": _close * (1 + np.abs(_rng.normal(0, 0.012, _n))),
    "low": _close * (1 - np.abs(_rng.normal(0, 0.012, _n))),
    "close": _close,
    "volume": _rng.uniform(1e4, 1e5, _n),
    "quote_volume": _rng.uniform(1e8, 1e9, _n),
}, index=pd.date_range("2019-01-01", periods=_n, freq="D", tz="UTC"))
CHECKPOINTS = (400, 550, 700)

# T13: the harness itself -- it must FIRE on a known leak and stay SILENT on the causal fix.
# Without this positive control a broken detector would pass every column vacuously.
def _leaky(d):
    x = d["close"] / d["close"].rolling(90).max() - 1.0
    return pd.DataFrame({"shallow": x > x.median()}, index=d.index)          # full-sample median

def _causal(d):
    x = d["close"] / d["close"].rolling(90).max() - 1.0
    return pd.DataFrame({"shallow": features.expanding_median_split(x, 365)}, index=d.index)

v_leak = prefix_violations(_leaky, SYN, CHECKPOINTS)
v_ok = prefix_violations(_causal, SYN, CHECKPOINTS)
assert v_leak, "harness FAILED to detect a full-sample-median lookahead"
assert not v_ok, f"harness produced a false positive on the causal build: {v_ok}"
print("T13 pass: prefix-causality harness detects the full-sample median leak "
      f"({v_leak['shallow']} days flipped) and clears expanding_median_split")

# T14: every column produced by features.add_features() is causal.
v = prefix_violations(features.add_features, SYN, CHECKPOINTS)
assert not v, f"features.add_features has lookahead columns: {v}"
print(f"T14 pass: all {len(features.add_features(SYN.copy()).columns)} features.add_features "
      "columns are prefix-causal")

# T15: features.btc_regime_columns() -- the date-level regime frame -- is causal.
v = prefix_violations(lambda d: features.btc_regime_columns(features.add_features(d)),
                      SYN, CHECKPOINTS)
assert not v, f"features.btc_regime_columns has lookahead: {v}"
print("T15 pass: features.btc_regime_columns (btc_above_sma50/sma200, btc_ret_14) is prefix-causal")

# T16: konfigurasi produksi tidak boleh memuat kolom terlarang sebagai gate.
# Menggantikan regression-lock V1.3 atas `btc_dd_shallow` (KOREKSI_V1_3.md K1):
# kolom itu tidak ada sama sekali di repo ini, jadi yang dijaga di sini adalah
# pintu masuknya -- daftar larangan V1_4_SPEC.md §2.3 tidak boleh bocor jadi gate.
import config_v14 as cfg

TERLARANG = {
    "btc_dd_shallow",        # lookahead K1
    "btc_above_sma200",      # terbukti merusak (6.25x vs 13.45x)
    "btc_above_sma50",       # belum terbukti membantu (t=1.25)
    "rs_btc_14",             # T10a p=1.4e-05, delta -0.030R -- diagnostik saja
    "rsi14", "ext_ma20_atr", # shadow, jangan di-threshold
    "funding_rate", "macro_t4a_dxy_riskon", "etf_t5a_btc_smoothed_ok",
}
c = cfg.production_engine_config()
gates = set(c.regime_cols) | set(c.extra_gate_cols) | ({c.btc_regime_col} if c.btc_regime_col else set())
bocor = gates & TERLARANG
assert not bocor, f"kolom terlarang dipakai sebagai gate: {bocor}"
assert c.regime_cols == (), "produksi TIDAK punya regime overlay (spec 2.1 baris 14)"
assert not (c.use_c2 or c.use_c5 or c.use_c6), "C2/C5/C6 harus OFF"
assert c.corr_cap is None, "corr_cap tidak dipakai di produksi"
assert c.entry_price_col == "next_open", "fill di OPEN t+1"
assert c.sl_atr == 1.5 and c.tp_atr == 3.0 and c.hold_max == 14
assert c.max_positions >= len(cfg.UNIVERSE)
print(f"T16 pass: gate produksi = {sorted(gates)}; nol kolom terlarang; "
      f"config_v14.validate() lolos")

# T17: the one column that is forward-looking BY DESIGN is forward by exactly one day, and it is
# kept out of the causal feature set. next_open is the execution price for a signal at close t
# (engine test T6), built in build_panel, never in add_features.
_no = SYN["open"].shift(-1)
assert (_no.iloc[:-1].to_numpy() == SYN["open"].iloc[1:].to_numpy()).all()
assert "next_open" not in features.add_features(SYN.copy()).columns, \
    "next_open must stay in build_panel (execution price), not mixed into the causal features"
print("T17 pass: next_open is exactly open[t+1] and is kept out of the causal feature set")

print()
print("ALL 18 SANITY TESTS PASS  (13 engine + 4 kausalitas fitur + 1 integritas config)")
