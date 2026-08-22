"""GERBANG v1.4.2 — replay 2019-2026.

V1_4_SPEC.md §5: "pipeline produksi diberi data historis, output dibanding
trade-per-trade dengan hasil backtest. n = 298 persis; mean R = 0.3182 sampai
4 desimal. Beda 1 trade = STOP."

Dijalankan dalam dua lapis supaya kalau ada beda, penyebabnya langsung terisolasi
dan tidak perlu ditebak:

  LAPIS A — pipa produksi + DATA CSV V1.3
            Data dibuat identik, jadi beda apa pun murni kesalahan KODE.
            Toleransi: NOL.

  LAPIS B — pipa produksi + DATA REST API Binance (jalur produksi sesungguhnya)
            Kode sudah terbukti benar di lapis A, jadi beda apa pun di sini
            murni berasal dari DATA.

Memisahkan keduanya penting: kalau hanya lapis B yang dijalankan, satu bug kode
dan satu selisih data bisa saling menutupi dan gerbangnya lolos padahal salah.
"""
from __future__ import annotations
import os, sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import config_v14 as cfg
import binance_data
import pipeline

V13_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "v1.3", "data")
KEY = ["symbol", "signal_date", "entry_date"]
CMP_COLS = ["exit_date", "entry_px", "exit_px", "reason", "R_net", "days_held"]


def load_csv(symbols) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in symbols:
        p = os.path.join(V13_DATA, f"{sym}{cfg.QUOTE_ASSET}.csv")
        if not os.path.exists(p):
            return {}
        df = pd.read_csv(p)
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        df = df.sort_values("date").set_index("date")
        out[sym] = df[binance_data.OHLCV_COLS].astype(float)
    return out


def compare(actual: pd.DataFrame, ref: pd.DataFrame, label: str) -> bool:
    """Bandingkan trade-per-trade. True kalau identik."""
    a = actual.sort_values(KEY).reset_index(drop=True)
    r = ref.sort_values(KEY).reset_index(drop=True)
    print(f"\n  --- {label} ---")
    print(f"  n trade : {len(a)}  vs acuan {len(r)}")
    print(f"  mean R  : {a['R_net'].mean():.4f}  vs acuan {r['R_net'].mean():.4f}")

    ka = set(map(tuple, a[KEY].astype(str).values))
    kr = set(map(tuple, r[KEY].astype(str).values))
    only_a, only_r = ka - kr, kr - ka
    if only_a or only_r:
        print(f"  BEDA HIMPUNAN TRADE: hanya-baru {len(only_a)}, hanya-acuan {len(only_r)}")
        for k in sorted(only_a)[:5]:
            print(f"     hanya di hasil baru : {k}")
        for k in sorted(only_r)[:5]:
            print(f"     hanya di acuan      : {k}")
        return False

    m = a.merge(r, on=KEY, suffixes=("_baru", "_acuan"))
    beda = []
    for c in CMP_COLS:
        x, y = m[f"{c}_baru"], m[f"{c}_acuan"]
        if x.dtype.kind in "fc":
            d = (x - y).abs()
            n = int((d > 1e-9).sum())
            if n:
                i = int(d.idxmax())
                beda.append(f"{c}: {n} baris beda, terbesar {d.max():.6g} pada "
                            f"{m.loc[i,'symbol']} {m.loc[i,'entry_date'].date()} "
                            f"({x[i]!r} vs {y[i]!r})")
        else:
            n = int((x.astype(str) != y.astype(str)).sum())
            if n:
                beda.append(f"{c}: {n} baris beda")
    if beda:
        print(f"  himpunan trade SAMA, tapi {len(beda)} kolom berbeda:")
        for b in beda:
            print(f"     {b}")
        return False
    print("  IDENTIK trade-per-trade")
    return True


def check_gate(trades: pd.DataFrame, label: str) -> bool:
    n, mr = len(trades), round(trades["R_net"].mean(), 4)
    by = trades["symbol"].value_counts().to_dict()
    ok = (n == cfg.REPLAY_EXPECTED_N_TRADES and mr == cfg.REPLAY_EXPECTED_MEAN_R
          and all(by.get(k) == v for k, v in cfg.REPLAY_EXPECTED_BY_SYMBOL.items()))
    print(f"  {label}: n={n} (acuan {cfg.REPLAY_EXPECTED_N_TRADES}) | "
          f"mean R={mr} (acuan {cfg.REPLAY_EXPECTED_MEAN_R}) | {by} -> "
          f"{'LOLOS' if ok else 'GAGAL'}")
    return ok


def main() -> int:
    syms = tuple(cfg.UNIVERSE) + tuple(cfg.SHADOW_SYMBOLS)
    fails = []

    print("=" * 74)
    print("LAPIS A — pipa produksi + data CSV V1.3 (isolasi kesalahan KODE)")
    print("=" * 74)
    csv_raw = load_csv(syms)
    if not csv_raw:
        print(f"  SKIP: data V1.3 tidak ada di {V13_DATA}")
        return 0
    ref = pipeline.build_trades(csv_raw)
    if not check_gate(ref, "  lapis A"):
        fails.append("lapis A gagal gerbang -> ADA BUG KODE")

    print("\n" + "=" * 74)
    print("LAPIS B — pipa produksi + REST API Binance (jalur produksi sungguhan)")
    print("=" * 74)
    api_raw = binance_data.load(syms)
    live = pipeline.build_trades(api_raw)
    gate_b = check_gate(live, "  lapis B")
    same = compare(live, ref, "lapis B vs lapis A (beda = murni DATA)")
    if not gate_b or not same:
        print("\n  Diagnosa selisih data:")
        for sym in syms:
            c, a = csv_raw[sym], api_raw[sym]
            common = c.index.intersection(a.index)
            for col in ["open", "high", "low", "close"]:
                d = (a.loc[common, col] - c.loc[common, col]).abs()
                n = int((d > 1e-9).sum())
                if n:
                    worst = d.idxmax()
                    print(f"     {sym}.{col}: {n} hari beda, terbesar {d.max():.6g} "
                          f"pada {worst.date()}")
        if not gate_b:
            fails.append("lapis B gagal gerbang")

    print("\n" + "=" * 74)
    print("HASIL GERBANG v1.4.2")
    print("=" * 74)
    if fails:
        for f in fails:
            print("   GAGAL:", f)
        return 1
    print("   LOLOS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
