"""Membuktikan pipeline.signals_for_day() setara dengan engine.run().

Job harian tidak bisa memakai engine untuk sinyal HARI INI: engine mengiterasi
`dates[:-1]` karena butuh baris hari berikutnya sebagai harga eksekusi, dan di
produksi "hari berikutnya" itu hari ini yang lilinnya baru dibuka.

Jadi ada dua implementasi seleksi kandidat. Duplikasi itu utang, dan tes ini
bunganya: untuk SETIAP hari 2019-2026, entri yang dipilih signals_for_day()
harus sama persis dengan yang benar-benar diambil engine.

Kalau tes ini gagal, jangan tambal salah satunya sampai cocok — cari tahu mana
yang benar dulu. Yang dipakai backtest adalah engine.
"""
from __future__ import annotations
import os, sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import config_v14 as cfg
import engine
import panel_v14
import pipeline
import binance_data

V13 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "v1.3", "data")


def load_csv(symbols):
    out = {}
    for s in symbols:
        p = os.path.join(V13, f"{s}{cfg.QUOTE_ASSET}.csv")
        if not os.path.exists(p):
            return {}
        df = pd.read_csv(p)
        df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.normalize()
        out[s] = (df.sort_values("date").set_index("date")[binance_data.OHLCV_COLS]
                    .astype(float))
    return out


def main() -> int:
    syms = tuple(cfg.UNIVERSE) + tuple(cfg.SHADOW_SYMBOLS)
    raw = load_csv(syms)
    if not raw:
        print(f"  SKIP: data V1.3 tidak ada di {V13}")
        return 0

    panel = panel_v14.build(raw)
    regime = panel_v14.empty_regime(panel)
    trades = engine.run(panel, regime, cfg.production_engine_config())
    print(f"  acuan engine: {len(trades)} trade")

    # Susun ulang keadaan hari-demi-hari persis seperti engine, lalu tanya
    # signals_for_day() apa yang akan dia pilih.
    dates = panel.index.get_level_values("date").unique().sort_values()
    dates = dates[(dates >= pd.Timestamp(cfg.BACKTEST_START, tz="UTC"))
                  & (dates <= pd.Timestamp(cfg.BACKTEST_END, tz="UTC"))]

    by_signal_day = {d: set(g["symbol"]) for d, g in trades.groupby("signal_date")}
    # posisi terbuka pada awal hari d = entry_date <= d < exit_date
    ent = trades[["symbol", "entry_date", "exit_date"]].copy()

    beda, n_dicek = [], 0
    for d in dates[:-1]:
        open_syms = set(ent.loc[(ent["entry_date"] <= d) & (ent["exit_date"] > d), "symbol"])
        # engine mengurus posisi lebih dulu, jadi yang exit HARI ITU sudah bebas
        n_slots = cfg.MAX_POSITIONS - len(open_syms)
        got = set(pipeline.signals_for_day(panel, d, open_syms, n_slots).index)
        want = by_signal_day.get(d, set())
        n_dicek += 1
        if got != want:
            beda.append((d, sorted(want), sorted(got), sorted(open_syms)))

    print(f"  hari diperiksa: {n_dicek}")
    print(f"  hari berbeda  : {len(beda)}")
    for d, want, got, op in beda[:10]:
        print(f"     {d.date()}  engine={want}  signals_for_day={got}  terbuka={op}")
    if beda:
        print("\n  GAGAL: dua implementasi seleksi sudah melenceng")
        return 1
    print("\n  SETARA: signals_for_day() == engine untuk setiap hari")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
