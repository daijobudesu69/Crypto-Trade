"""Kolom shadow (V1_4_SPEC.md §2.4) — dicatat tiap hari, TIDAK PERNAH memblokir.

Kenapa modul terpisah, bukan ditambahkan ke features.py:

  1. features.py diport byte-identical dari V1.3 dan harus tetap begitu — gerbang
     v1.4.2 bersandar padanya.
  2. Pemisahan fisik membuat sifat "tidak pernah memblokir" jadi jelas secara
     struktural, bukan sekadar janji di komentar. Tidak ada satu pun fungsi di
     sini yang dipanggil dari jalur keputusan; `pipeline.build_trades()` tidak
     mengimpor modul ini sama sekali.

Kenapa dicatat kalau tidak dipakai: kandidat terkuat (C2, C2+C5, C2+MACD,
C2+mom120>10%) mencapai OOS mean R +0.256 sampai +0.377, TAPI semuanya t < 2.0
dari sweep 35 konfigurasi tanpa koreksi multiple-testing, dan semuanya merusak
era 2022-23. Jadi statusnya shadow: kumpulkan data dulu, uji nanti dengan BH-FDR
yang benar setelah ada >=300 trade baru.

PERINGATAN: mempromosikan kolom di sini menjadi gate tanpa koreksi
multiple-testing adalah persis kesalahan yang membuat V1.1-V1.3 harus diulang
tiga kali.
"""
from __future__ import annotations
import pandas as pd

import config_v14 as cfg


def rsi_wilder(close: pd.Series, period: int = cfg.RSI_PERIOD) -> pd.Series:
    """RSI Wilder. NILAI MENTAH — jangan di-threshold (§2.4).

    Gate RSI sudah diuji dan gagal: OOS +0.067 / +0.144 / +0.164 untuk ambang
    <70 / <80 / 50-75, semuanya di bawah baseline tanpa gate (+0.151).
    """
    d = close.diff()
    gain = d.clip(lower=0.0)
    loss = (-d).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100.0 - 100.0 / (1.0 + rs)
    return rsi.where(avg_loss != 0, 100.0)      # avg_loss==0 -> RSI 100, bukan NaN


def macd_bull(close: pd.Series) -> pd.Series:
    """EMA12 - EMA26 > EMA9(EMA12 - EMA26)."""
    ema_f = close.ewm(span=cfg.MACD_FAST, adjust=False).mean()
    ema_s = close.ewm(span=cfg.MACD_SLOW, adjust=False).mean()
    macd = ema_f - ema_s
    signal = macd.ewm(span=cfg.MACD_SIGNAL, adjust=False).mean()
    return macd > signal


def add_shadow_columns(panel: pd.DataFrame) -> pd.DataFrame:
    """Tambahkan kolom §2.4 yang belum dihitung features.add_features().

    Sudah ada dari features.py: c2_pass, c5_pass, ext_ma20_atr, atr_pct,
    dd_from_90d_high. Yang ditambahkan di sini: rsi14, macd_bull,
    mom_120_strong, btc_mom_28, btc_mom_120, sol_signal.
    """
    p = panel.copy()
    close_wide = p["close"].unstack()

    rsi = close_wide.apply(rsi_wilder).stack()
    rsi.index.names = ["date", "symbol"]
    p["rsi14"] = rsi.reindex(p.index)

    mb = close_wide.apply(macd_bull).stack()
    mb.index.names = ["date", "symbol"]
    p["macd_bull"] = mb.reindex(p.index).fillna(False).astype(bool)

    p["mom_120_strong"] = p[f"mom_{cfg.MOM_LONG_DAYS}"] > cfg.MOM_120_STRONG_THRESHOLD

    # Konteks level-BTC. BUKAN gate — regime overlay TIDAK ADA (§2.1 baris 14).
    ref = cfg.UNIVERSE[0]
    dates = p.index.get_level_values("date")
    for col in (f"mom_{cfg.MOM_SHORT_DAYS}", f"mom_{cfg.MOM_LONG_DAYS}"):
        btc_series = p.xs(ref, level="symbol")[col]
        p[f"btc_{col}"] = btc_series.reindex(dates).to_numpy()

    # sol_signal: sinyal produksi yang sama, dijalankan pada simbol shadow.
    # Full-sample +0.348 terlihat aditif, TAPI era 2024-26 justru -0.079 dan SOL
    # membawa survivorship bias yang tidak dimiliki BTC/ETH (turun ~96% saat FTX
    # kolaps). Karena itu shadow, bukan universe produksi (KOREKSI_V1_3.md §5).
    p["sol_signal"] = False
    for s in cfg.SHADOW_SYMBOLS:
        if s in set(p.index.get_level_values("symbol")):
            mask = p.index.get_level_values("symbol") == s
            p.loc[mask, "sol_signal"] = p.loc[mask, "tsmom_pos"].to_numpy()
    return p


def row_for(panel: pd.DataFrame, symbol: str, day: pd.Timestamp) -> dict:
    """Satu baris shadow log untuk satu simbol pada satu tanggal.

    Dipanggil untuk SETIAP kandidat, termasuk yang tidak jadi trade — itulah
    gunanya shadow log.
    """
    try:
        r = panel.loc[(day, symbol)]
    except KeyError:
        return {}
    out = {"date": day.date().isoformat(), "symbol": symbol}
    for c in cfg.SHADOW_COLUMNS:
        v = r.get(c)
        if v is None or (hasattr(v, "__len__") and not isinstance(v, str)):
            v = None
        out[c] = None if v is None or pd.isna(v) else (
            bool(v) if isinstance(v, (bool,)) else float(v))
    # konteks tambahan yang bukan kolom shadow tapi perlu untuk audit
    for c in (f"mom_{cfg.MOM_SHORT_DAYS}", f"mom_{cfg.MOM_LONG_DAYS}", "close", "atr14",
              "med_qvol_20", "days_listed", "tsmom_pos"):
        v = r.get(c)
        out[c] = None if v is None or pd.isna(v) else (
            bool(v) if isinstance(v, bool) else float(v))
    return out
