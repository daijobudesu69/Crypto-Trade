"""V1.2 feature pipeline. Every indicator at row t uses data through day t ONLY.
Signals computed on day t are tradeable at the OPEN of day t+1 (no lookahead)."""
import pandas as pd, numpy as np, glob, os

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EXCLUDE = {"XAUT", "PAXG", "GRAM"}  # gold-pegged x2, suspect mapping + 46 rows


def load_all() -> dict[str, pd.DataFrame]:
    out = {}
    for f in sorted(glob.glob(f"{DATA}/*.csv")):
        sym = os.path.basename(f).replace("USDT.csv", "")
        if sym in EXCLUDE:
            continue
        df = pd.read_csv(f)
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        df = df.sort_values("date").set_index("date")
        out[sym] = df[["open", "high", "low", "close", "volume", "quote_volume"]].astype(float)
    return out


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    c, h, l, o, qv = df["close"], df["high"], df["low"], df["open"], df["quote_volume"]
    r = c.pct_change()
    # C1 momentum family
    for w in (3, 7, 14, 21, 28):
        df[f"mom_{w}"] = c.pct_change(w)
    # C2 trend
    df["sma20"] = c.rolling(20).mean()
    df["sma50"] = c.rolling(50).mean()
    df["sma200"] = c.rolling(200).mean()
    df["sma20_slope5"] = df["sma20"] - df["sma20"].shift(5)
    df["c2_pass"] = (c > df["sma20"]) & (c > df["sma50"]) & (df["sma20_slope5"] > 0)
    # C3a realized vol (56d, annualized-free daily stdev)
    df["rv56"] = r.rolling(56).std()
    df["rv28"] = r.rolling(28).std()
    df["rv84"] = r.rolling(84).std()
    # C3b ATR14 Wilder (RMA)
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    df["atr_pct"] = df["atr14"] / c
    # C5 volume participation
    df["avg_vol_3"] = qv.rolling(3).mean()
    df["avg_vol_20"] = qv.rolling(20).mean()
    df["c5_pass"] = df["avg_vol_3"] > df["avg_vol_20"]
    # C6 extension
    df["ext_ma20_atr"] = (c - df["sma20"]) / df["atr14"]
    df["c6_pass"] = df["ext_ma20_atr"] <= 3.0
    # liquidity (point-in-time)
    df["med_qvol_20"] = qv.rolling(20).median()
    # exploratory
    df["ret_1"] = r
    df["ret_3"] = c.pct_change(3)
    df["dd_from_90d_high"] = c / c.rolling(90).max() - 1.0
    df["vol_of_vol"] = df["rv28"].pct_change(14)
    df["days_listed"] = np.arange(len(df))
    return df


def expanding_median_split(s: pd.Series, min_periods: int = 365) -> pd.Series:
    """Causal replacement for `s > s.median()`.

    A full-sample `.median()` is a lookahead: the threshold for day t is computed from days that
    have not happened yet (KOREKSI_V1_3.md K1 -- this is exactly how `btc_dd_shallow` leaked
    Aug-2026 information into 2019 signals). The expanding median uses days 0..t only, so the
    threshold on day t is knowable on day t. Rows before `min_periods` are False (fail-safe:
    no threshold yet means the gate is off, never silently on).
    """
    thresh = s.expanding(min_periods=min_periods).median()
    return (s > thresh).where(thresh.notna(), False).astype(bool)


def btc_regime_columns(btc: pd.DataFrame) -> pd.DataFrame:
    """Date-level BTC regime columns derivable from the V1.2 feature set.

    Extracted from build_panel() so `sanity_tests.py` can check it for lookahead directly --
    regime-column construction sits outside engine.py and was therefore never covered by the
    original 13 engine tests (KOREKSI_V1_3.md sec 8, action 1). Every column here must be a
    function of days <= t only. See run_registry.add_btc_regime_columns() for the columns that
    additionally need mom_120 / dd_from_90d_high.
    """
    return pd.DataFrame({
        "btc_above_sma50": btc["close"] > btc["sma50"],
        "btc_above_sma200": btc["close"] > btc["sma200"],
        "btc_ret_14": btc["mom_14"],
    }, index=btc.index)


def build_panel() -> pd.DataFrame:
    """Long panel indexed by (date, symbol) with all features + next-day open for entries."""
    frames = []
    raw = load_all()
    btc = add_features(raw["BTC"].copy())
    btc_regime = btc_regime_columns(btc)
    for sym, df in raw.items():
        df = add_features(df.copy())
        df["rs_btc_14"] = df["mom_14"] - btc["mom_14"].reindex(df.index)
        df["next_open"] = df["open"].shift(-1)  # execution price for signal at close t
        df["symbol"] = sym
        frames.append(df)
    panel = pd.concat(frames).reset_index().set_index(["date", "symbol"]).sort_index()
    return panel, btc_regime


if __name__ == "__main__":
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
    os.makedirs(out_dir, exist_ok=True)
    panel, btc_regime = build_panel()
    panel.to_pickle(os.path.join(out_dir, "panel.pkl"))
    btc_regime.to_pickle(os.path.join(out_dir, "btc_regime.pkl"))
    print("panel:", panel.shape, "| symbols:", panel.index.get_level_values(1).nunique(),
          "| dates:", panel.index.get_level_values(0).nunique())
    print(panel.columns.tolist())
