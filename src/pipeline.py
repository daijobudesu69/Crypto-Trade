"""Pipa produksi: data mentah -> panel -> trade.

Satu jalur kode dipakai dua-duanya:
  * replay v1.4.2 — diberi 2019-2026 sekaligus, hasilnya dibanding backtest V1.3
  * cron v1.4.3   — diberi jendela sampai kemarin, diambil sinyal hari ini saja

Kalau kedua pemakaian itu lewat jalur berbeda, gerbang v1.4.2 tidak membuktikan
apa pun tentang yang jalan tiap hari. Karena itu `build_trades()` di bawah adalah
SATU-SATUNYA tempat sinyal lahir.
"""
from __future__ import annotations
import pandas as pd

import config_v14 as cfg
import engine
import panel_v14


def build_trades(raw: dict[str, pd.DataFrame], start: str | None = None,
                 end: str | None = None) -> pd.DataFrame:
    """OHLCV mentah -> trade log lengkap, memakai config produksi frozen."""
    panel = panel_v14.build(raw)
    regime = panel_v14.empty_regime(panel)
    c = cfg.production_engine_config()
    if start:
        c.start = start
    if end:
        c.end = end
    return engine.run(panel, regime, c)


def still_open(trades: pd.DataFrame) -> pd.DataFrame:
    """Posisi yang MASIH TERBUKA di ujung data.

    engine.run() menutup paksa semua posisi yang masih hidup di tanggal terakhir
    dan menandainya reason="eod". Jadi di backtest itu artinya "ditutup di akhir
    jendela uji", tapi di job harian artinya "posisi ini sebenarnya masih
    terbuka sekarang". Harga dan R pada baris itu adalah tanda-mati-sementara,
    bukan hasil sungguhan.
    """
    if trades.empty:
        return trades
    return trades[trades["reason"] == "eod"]


def signals_for_day(panel: pd.DataFrame, day: pd.Timestamp, open_symbols,
                    n_slots: int, require_entry_price: bool = True) -> pd.DataFrame:
    """Kandidat masuk yang lahir di close tanggal `day`, terurut peringkat.

    KENAPA LOGIKA INI DITULIS ULANG DI SINI, bukan dipanggil dari engine.py:

    engine.py diport byte-identical (§2.2) dan filter kelayakannya tertanam di
    dalam loop harian, tidak bisa dipanggil sendirian. Mengekstraknya berarti
    mengubah engine.py dan membatalkan bukti gerbang v1.4.2.

    Dan job harian TIDAK BISA memakai engine apa adanya untuk sinyal hari ini:
    engine hanya mengiterasi `dates[:-1]` karena butuh baris hari berikutnya
    untuk harga eksekusi. Di produksi, "hari berikutnya" itu HARI INI dan
    lilinnya baru saja dibuka — belum ada di data lilin-tertutup.

    Duplikasi ini dijaga oleh tests/test_signal_equivalence.py, yang menuntut
    fungsi ini menghasilkan entri yang PERSIS SAMA dengan yang benar-benar
    diambil engine, untuk setiap hari 2019-2026. Kalau salah satu bergeser,
    tes itu gagal.

    `require_entry_price=False` dipakai di jalur produksi: harga eksekusi
    (open hari ini) diambil dari lilin berjalan, bukan dari kolom next_open.
    """
    try:
        rows = panel.xs(day, level="date")
    except KeyError:
        return panel.iloc[0:0]

    elig = rows[(rows["days_listed"] >= cfg.MIN_HISTORY_DAYS)
                & (rows["med_qvol_20"] >= cfg.LIQ_FLOOR_USD)
                & rows["atr14"].notna()
                & rows[cfg.RANK_COL].notna()]
    if require_entry_price:
        elig = elig[elig[cfg.ENTRY_PRICE_COL].notna()]
    for gc in ("is_production_universe", "tsmom_pos"):
        elig = elig[elig[gc].fillna(False).astype(bool)]
    elig = elig[~elig.index.isin(set(open_symbols))]
    if elig.empty or n_slots <= 0:
        return elig.iloc[0:0]
    return elig.sort_values(cfg.RANK_COL, ascending=cfg.RANK_ASCENDING).head(n_slots)


def with_execution_plan(trades: pd.DataFrame) -> pd.DataFrame:
    """Tambahkan kolom yang dibutuhkan manusia untuk mengeksekusi manual:
    harga SL/TP, ukuran posisi, dan tanggal alarm hold (§2.8).

    Barrier dihitung ulang dari config, bukan diambil dari engine, supaya
    angka yang dikirim ke Telegram terbukti berasal dari konstanta frozen.
    """
    if trades.empty:
        return trades
    t = trades.copy()
    # engine menyimpan sl_dist_pct = sl_atr * atr / entry -> balikkan jadi atr
    atr = t["sl_dist_pct"] * t["entry_px"] / cfg.SL_ATR_MULT
    t["atr14_at_signal"] = atr
    t["oco_stop_loss"] = t["entry_px"] - cfg.SL_ATR_MULT * atr
    t["oco_take_profit"] = t["entry_px"] + cfg.TP_ATR_MULT * atr
    sizes = [cfg.position_size_frac(e, a) for e, a in zip(t["entry_px"], atr)]
    t["size_frac_wanted"] = [w for w, _ in sizes]
    t["size_frac_used"] = [u for _, u in sizes]
    t["size_frac"] = t["size_frac_used"] / t["size_frac_wanted"]     # diagnostik §2.6
    t["notional_usd_at_start"] = t["size_frac_used"] * cfg.ACCOUNT_START_USD
    t["hold_warning_date"] = t["entry_date"] + pd.Timedelta(days=cfg.TIME_EXIT_WARNING_DAY - 1)
    t["hold_force_exit_date"] = t["entry_date"] + pd.Timedelta(days=cfg.TIME_EXIT_DAY - 1)
    return t
