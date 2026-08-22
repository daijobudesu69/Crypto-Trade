"""Membangun panel produksi V1.4 dari OHLCV mentah.

Kenapa berkas ini ada. `features.py` dan `engine.py` diport apa adanya dari V1.3
(V1_4_SPEC.md §2.2: "Jangan tulis ulang dari nol"). Yang TIDAK diport adalah
`run_registry.py` — itu orkestrator riset untuk 26 tes dan 44 koin, tidak ada
urusannya dengan produksi. Tapi tiga kolom yang dipakai konfigurasi produksi
justru lahir di sana:

    mom_120                  -> features.py hanya menghitung mom_3..28
    is_t7b_universe          -> di sini jadi is_production_universe
    t7_tsmom_pos             -> di sini jadi tsmom_pos
    next_open                -> lahir di features.build_panel(), yang membaca
                                44 CSV lokal; produksi membaca 2 simbol dari API

Berkas ini menyalin logika ketiga kolom itu PERSIS, tidak menafsirkan ulang.
Perbandingan baris-per-baris ada di komentar tiap kolom.

Kontrak no-lookahead: semua kolom di sini kausal (nilai hari t hanya dari hari
<= t) KECUALI `next_open`, yang memang harga eksekusi satu hari ke depan dan
diperlakukan khusus. Dijaga oleh tests/sanity_tests.py T14 dan T17.
"""
from __future__ import annotations
import pandas as pd

import config_v14 as cfg
import features


def add_production_columns(panel: pd.DataFrame) -> pd.DataFrame:
    """Tambahkan kolom yang di V1.3 lahir di run_registry.build_universe().

    `panel` adalah long panel ber-index (date, symbol) yang setiap simbolnya
    sudah lewat features.add_features().
    """
    # --- mom_120 -------------------------------------------------------------
    # run_registry.py:
    #     c_wide = panel["close"].unstack()
    #     mom120 = c_wide.pct_change(120).stack()
    # Lewat frame wide supaya tiap simbol memakai deret tanggalnya SENDIRI —
    # urutan baris panel itu date-major, bukan symbol-contiguous, jadi
    # pct_change() langsung di panel akan mencampur simbol.
    c_wide = panel["close"].unstack()
    mom_long = c_wide.pct_change(cfg.MOM_LONG_DAYS).stack()
    mom_long.index.names = ["date", "symbol"]
    panel[f"mom_{cfg.MOM_LONG_DAYS}"] = mom_long.reindex(panel.index)

    # --- next_open -----------------------------------------------------------
    # features.build_panel(): df["next_open"] = df["open"].shift(-1)
    # Harga eksekusi untuk sinyal di close hari t. SATU-SATUNYA kolom yang
    # menengok ke depan, dan tepat satu hari (sanity test T17).
    o_wide = panel["open"].unstack()
    next_open = o_wide.shift(-1).stack()
    next_open.index.names = ["date", "symbol"]
    panel["next_open"] = next_open.reindex(panel.index)

    # --- gate universe -------------------------------------------------------
    # run_registry.py: panel["is_t7b_universe"] = symbols.isin({"BTC", "ETH"})
    symbols = panel.index.get_level_values("symbol")
    panel["is_production_universe"] = symbols.isin(set(cfg.UNIVERSE))

    # --- gate momentum dua kaki ---------------------------------------------
    # run_registry.py:
    #     panel["t7_tsmom_pos"] = (panel["mom_28"] > 0) & (panel["mom_120"] > 0)
    panel["tsmom_pos"] = ((panel[f"mom_{cfg.MOM_SHORT_DAYS}"] > 0)
                          & (panel[f"mom_{cfg.MOM_LONG_DAYS}"] > 0))

    # --- rs_btc_14: DIAGNOSTIK SAJA, tidak pernah jadi keputusan -------------
    # features.build_panel(): df["rs_btc_14"] = df["mom_14"] - btc["mom_14"]
    # engine.py mencatatnya di tiap baris trade log. Disediakan di sini supaya
    # engine.py tetap bisa diport byte-identical dan skema trade log sama persis
    # dengan V1.3 — penting untuk perbandingan trade-per-trade di v1.4.2.
    # CATATAN: gate RS-vs-BTC DILARANG (§2.3, T10a p=1.4e-05, delta -0.030R).
    # Yang ada di sini hanya nilai yang dicatat, bukan filter.
    ref = cfg.UNIVERSE[0]                      # BTC — acuan relative strength
    btc_mom14 = panel.xs(ref, level="symbol")["mom_14"]
    panel["rs_btc_14"] = (panel["mom_14"]
                          - btc_mom14.reindex(panel.index.get_level_values("date")).to_numpy())
    return panel


def build(raw: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """OHLCV per simbol -> panel produksi siap dipakai engine.run().

    `raw` = {"BTC": df, "ETH": df, ...} dengan kolom
    open/high/low/close/volume/quote_volume dan index tanggal UTC ter-normalisasi.
    Simbol shadow (§2.4) boleh ikut — gate universe yang menyaringnya, sehingga
    kolom shadow tetap terhitung tanpa pernah menghasilkan trade.
    """
    frames = []
    for sym, df in raw.items():
        f = features.add_features(df.copy())
        f["symbol"] = sym
        frames.append(f)
    panel = (pd.concat(frames)
               .reset_index()
               .rename(columns={"index": "date"})
               .set_index(["date", "symbol"])
               .sort_index())
    return add_production_columns(panel)


def empty_regime(panel: pd.DataFrame) -> pd.DataFrame:
    """Frame regime kosong ber-index tanggal.

    Konfigurasi produksi tidak punya overlay (§2.1 baris 14), tapi engine.run()
    tetap menerima argumen `btc_regime`. Frame kosong = tidak ada gate level-hari
    yang pernah dievaluasi, karena cfg.REGIME_OVERLAY juga kosong.
    """
    return pd.DataFrame(index=panel.index.get_level_values("date").unique().sort_values())
