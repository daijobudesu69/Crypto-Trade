"""Smoke test kesetiaan port v1.4.1.

BUKAN gerbang v1.4.2. Gerbang itu menjalankan pipa produksi PENUH (fetch Binance
-> panel -> sinyal) di atas data historis. Yang diuji di sini lebih sempit tapi
sudah cukup untuk menangkap port yang rusak sejak dini: kode yang diport, dijalankan
lewat config_v14, harus menghasilkan trade log yang sama persis dengan V1.3.

Acuan (V1_4_SPEC.md §3, dan config_v14.REPLAY_EXPECTED_*):
    n = 298 | mean R = 0.3182 | BTC 145, ETH 153

Sumber data: CSV V1.3 di ../v1.3/data/. Berkas ini di-skip otomatis kalau folder
itu tidak ada, supaya repo publik tetap bisa dites tanpa membawa data apa pun
(*.csv masuk .gitignore, §6.1 baris 2).
"""
from __future__ import annotations
import os, sys, glob

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import config_v14 as cfg
import engine
import panel_v14

V13_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "v1.3", "data")


def load_v13_csv(symbols) -> dict[str, pd.DataFrame]:
    out = {}
    for sym in symbols:
        path = os.path.join(V13_DATA, f"{sym}{cfg.QUOTE_ASSET}.csv")
        if not os.path.exists(path):
            return {}
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        df = df.sort_values("date").set_index("date")
        out[sym] = df[["open", "high", "low", "close", "volume", "quote_volume"]].astype(float)
    return out


def main() -> int:
    raw = load_v13_csv(cfg.UNIVERSE)
    if not raw:
        print(f"SKIP: data V1.3 tidak ada di {V13_DATA} — test ini butuh CSV historis")
        return 0

    panel = panel_v14.build(raw)
    regime = panel_v14.empty_regime(panel)
    trades = engine.run(panel, regime, cfg.production_engine_config())

    n, mean_r = len(trades), trades["R_net"].mean()
    by_sym = trades["symbol"].value_counts().to_dict()
    print(f"  n trade   : {n}   (acuan {cfg.REPLAY_EXPECTED_N_TRADES})")
    print(f"  mean R    : {mean_r:.4f}   (acuan {cfg.REPLAY_EXPECTED_MEAN_R})")
    print(f"  per simbol: {by_sym}   (acuan {cfg.REPLAY_EXPECTED_BY_SYMBOL})")

    ok = True
    if n != cfg.REPLAY_EXPECTED_N_TRADES:
        print(f"  GAGAL: jumlah trade {n} != {cfg.REPLAY_EXPECTED_N_TRADES}"); ok = False
    if round(mean_r, 4) != cfg.REPLAY_EXPECTED_MEAN_R:
        print(f"  GAGAL: mean R {mean_r:.4f} != {cfg.REPLAY_EXPECTED_MEAN_R}"); ok = False
    for sym, want in cfg.REPLAY_EXPECTED_BY_SYMBOL.items():
        if by_sym.get(sym) != want:
            print(f"  GAGAL: {sym} {by_sym.get(sym)} != {want}"); ok = False

    # kontrak posisi (§2.7): satu simbol tidak boleh punya dua posisi bersamaan
    for sym, g in trades.groupby("symbol"):
        g = g.sort_values("entry_date")
        overlap = int((g["entry_date"].shift(-1) <= g["exit_date"]).sum())
        if overlap:
            print(f"  GAGAL: {sym} punya {overlap} posisi tumpang-tindih"); ok = False

    # barrier & sizing dari config, dicek pada trade pertama
    t0 = trades.sort_values("entry_date").iloc[0]
    atr_implied = t0["sl_dist_pct"] * t0["entry_px"] / cfg.SL_ATR_MULT
    sl, tp = cfg.barriers(t0["entry_px"], atr_implied)
    wanted, used = cfg.position_size_frac(t0["entry_px"], atr_implied)
    assert abs((t0["entry_px"] - sl) / t0["entry_px"] - t0["sl_dist_pct"]) < 1e-12
    assert abs(tp - t0["entry_px"] - 2 * (t0["entry_px"] - sl)) < 1e-9, "TP:SL harus 2:1"
    assert 0 < used <= cfg.MAX_EXPOSURE_FRAC
    print(f"  barrier/sizing konsisten (contoh: ukuran diinginkan {wanted:.1%}, dipakai {used:.1%})")

    print("\n  PORT SETIA" if ok else "\n  PORT RUSAK")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
