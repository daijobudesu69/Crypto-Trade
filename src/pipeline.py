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


def open_positions_on(trades: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """Posisi yang terbuka pada tanggal `day` (entry <= day <= exit)."""
    if trades.empty:
        return trades
    return trades[(trades["entry_date"] <= day) & (trades["exit_date"] >= day)]


def entries_signalled_on(trades: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    """Trade yang sinyalnya lahir di close tanggal `day` (fill di open day+1).

    Inilah yang dikirim ke Telegram di v1.4.3.
    """
    if trades.empty:
        return trades
    return trades[trades["signal_date"] == day]


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
