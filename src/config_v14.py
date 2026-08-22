"""Konfigurasi produksi V1.4 — FROZEN sejak 22 Agustus 2026.

Setiap nilai di berkas ini disalin dari V1_4_SPEC.md §2. Tidak ada yang boleh
dihitung, dioptimasi, atau ditebak saat runtime. Tidak ada TODO.

Kenapa hard-coded semua:
  Gerbang v1.4.2 mengharuskan pipa produksi menghasilkan PERSIS 298 trade dengan
  mean R 0.3182 saat diberi data historis 2019-2026. Satu konstanta yang berubah
  diam-diam membuat gerbang itu gagal tanpa sebab yang jelas — atau lebih buruk,
  lolos padahal mengukur sistem yang berbeda.

Aturan perubahan (V1_4_SPEC.md §2 dan catatan penutup):
  Perubahan apa pun setelah v1.4.2 dimulai MEMBATALKAN shadow log dan wajib
  dicatat di V1_4_SPEC.md beserta alasannya.

Referensi silang ke spec ditulis di tiap blok supaya bisa diaudit baris per baris.
"""
from __future__ import annotations

# =============================================================== identitas ===
VERSION = "1.4.1"
CONFIG_FROZEN_DATE = "2026-08-22"
SPEC_REF = "V1_4_SPEC.md §2 (frozen 22 Agt 2026)"


# ====================================================== §2.1 hard gate =======
# Sinyal batal kalau satu saja gagal.

# baris 1 — universe produksi. Pasangan USDT di Binance SPOT.
UNIVERSE: tuple[str, ...] = ("BTC", "ETH")
QUOTE_ASSET = "USDT"
SYMBOLS: tuple[str, ...] = tuple(f"{c}{QUOTE_ASSET}" for c in UNIVERSE)

# baris 2-3 — dua kaki momentum. Keduanya harus > 0. Ini SATU-SATUNYA filter
# yang benar-benar menyaring, selain universe itu sendiri (§2.5, §2.7).
MOM_SHORT_DAYS = 28
MOM_LONG_DAYS = 120

# baris 4 — riwayat minimum sebelum sebuah simbol boleh diperdagangkan.
MIN_HISTORY_DAYS = 60

# baris 5 — likuiditas. SAFETY RAIL, BUKAN FILTER: tidak pernah membatalkan
# satu sinyal pun di BTC+ETH (§2.5). Titik terburuk ETH masih 5.5x di atasnya.
# Dipertahankan untuk kondisi patologis: feed rusak, quote_volume nol, halt.
LIQ_FLOOR_USD = 5_000_000.0
LIQ_MEDIAN_WINDOW = 20

# baris 6-7 — barrier, dijangkar di harga fill.
SL_ATR_MULT = 1.5
TP_ATR_MULT = 3.0
ATR_PERIOD = 14                      # Wilder RMA, BUKAN SMA (§2.2)

# baris 8 — hold maksimum, hari kalender, termasuk hari entry.
HOLD_MAX_DAYS = 14

# baris 9 — SAFETY RAIL, BUKAN FILTER. Tidak pernah mengikat di universe 2 koin:
# plafon 2 sudah dipaksakan oleh ukuran universe + aturan "koin yang sedang
# dipegang dicoret dari kandidat". Dibuktikan di §2.7 (max_positions 2/3/9 ->
# trade log identik). Baru aktif kalau universe diperluas.
MAX_POSITIONS = 2

# baris 10 — harga eksekusi. Sinyal dihitung di close hari t, fill di OPEN t+1.
ENTRY_PRICE_COL = "next_open"

# baris 11 — risk per trade. Artinya: kalau kena SL, rugi 3% dari EKUITAS,
# bukan "beli senilai 3% ekuitas". Ukuran posisi = RISK / jarak_SL.
RISK_PER_TRADE = 0.03

# baris 11b — modal awal forward test.
ACCOUNT_START_USD = 100.00

# baris 11c — instrumen. SPOT, tanpa leverage, eksposur di-cap 100% ekuitas.
# Perp ditolak di §2.6: funding memangkas 12.22x -> 9.06x.
INSTRUMENT = "BINANCE_SPOT"
USE_LEVERAGE = False
MAX_EXPOSURE_FRAC = 1.00

# baris 12 — biaya. 0.10% fee + 0.05% slippage per sisi = 0.30% round-trip.
FEE_SIDE = 0.0010
SLIP_SIDE = 0.0005
COST_ROUND_TRIP = 2 * (FEE_SIDE + SLIP_SIDE)

# baris 13 — ranking. TIE-BREAK SAJA: hanya dipakai kalau BTC dan ETH sama-sama
# lolos dan slot tidak cukup. Bukan skor seleksi.
RANK_COL = "mom_28"
RANK_ASCENDING = False

# baris 14 — TIDAK ADA regime overlay. Alasan: tidak ada overlay yang terbukti
# membantu secara signifikan (V1_4_0_RESULTS.md §2) — bukan karena overlay
# terbukti merugikan. Semua t < 1.3.
REGIME_OVERLAY: tuple[str, ...] = ()

# baris 15 — gate C2/C5/C6 semuanya OFF. C2 memakai SMA20/SMA50; dimatikan di
# keluarga strategi ini. SMA20/SMA50 tetap dihitung karena dipakai kolom shadow.
USE_C2 = False
USE_C5 = False
USE_C6 = False


# ================================================== §2.8 alur keluar posisi ==
# Keputusan Dew 22 Agt 2026. Job harian hanya jalan sekali sehari dan tidak
# mengintip intraday — tanpa OCO, asumsi "exit tepat di harga SL/TP" yang dipakai
# backtest jadi tidak berdasar dan perbandingan v1.4.4 kehilangan arti.
OCO_ON_ENTRY = True                  # pasang limit(TP) + stop-limit(SL) saat masuk
TIME_EXIT_WARNING_DAY = 13           # alarm "besok tutup paksa"
TIME_EXIT_DAY = HOLD_MAX_DAYS        # tutup manual di harga berapa pun


# ==================================================== §2.4 kolom shadow ======
# Dicatat tiap hari untuk SETIAP kandidat, termasuk yang tidak jadi trade.
# TIDAK PERNAH memblokir. Tujuannya mengumpulkan data agar 6-12 bulan lagi bisa
# diuji dengan BH-FDR yang benar. Kandidat terkuat pun t < 2.0 dan merusak era
# 2022-23, karena itu status shadow.
SHADOW_SYMBOLS: tuple[str, ...] = ("SOL",)      # sinyal sama, dijalankan terpisah
SHADOW_COLUMNS: tuple[str, ...] = (
    "sol_signal", "c2_pass", "c5_pass", "macd_bull", "mom_120_strong",
    "rsi14", "ext_ma20_atr", "atr_pct", "dd_from_90d_high",
    "btc_mom_28", "btc_mom_120",
)
MOM_120_STRONG_THRESHOLD = 0.10
RSI_PERIOD = 14                      # nilai MENTAH, jangan di-threshold
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

# Diagnostik wajib (§2.6): ukuran diambil / ukuran diinginkan. Kalau size_frac < 1
# jauh lebih sering dari 14%, ada bug pelacakan ekuitas.
SIZE_FRAC_EXPECTED_BELOW_ONE = 0.14


# ======================================================= jendela & data ======
BACKTEST_START = "2019-01-01"
BACKTEST_END = "2026-08-16"
OOS_START = "2024-01-01"             # jendela "ujian jujur"
KLINE_INTERVAL = "1d"                # daily close 00:00 UTC
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
CRON_UTC = "00:05"                   # §5 v1.4.3


# ============================================ angka acuan gerbang v1.4.2 =====
# Pipa produksi diberi data historis harus mereproduksi ini PERSIS.
# Beda 1 trade = STOP (§5).
REPLAY_EXPECTED_N_TRADES = 298
REPLAY_EXPECTED_MEAN_R = 0.3182      # 4 desimal
REPLAY_EXPECTED_BY_SYMBOL = {"BTC": 145, "ETH": 153}

# Ekspektasi jujur untuk forward test — BUKAN 13.45x, BUKAN +0.3182.
# Angka headline didominasi era 2019-2021 (§3.2). T8 gagal 16.7%, jadi tidak ada
# konfigurasi yang boleh disebut robust (§3.3).
FORWARD_EXPECTED_MEAN_R = 0.151      # era 2024-2026
FORWARD_EXPECTED_TRADES_PER_MONTH = 3.82
FORWARD_LONGEST_GAP_DAYS = 299       # sistem bisa diam ~10 bulan. Itu BUKAN kerusakan.


def production_engine_config():
    """Bangun engine.Config produksi dari konstanta di atas.

    Dipisah dari konstanta supaya engine.py tetap generik (dipakai juga untuk
    replay v1.4.2) sementara nilai produksinya cuma hidup di satu tempat.
    """
    from engine import Config
    return Config(
        rank_col=RANK_COL,
        rank_ascending=RANK_ASCENDING,
        use_c2=USE_C2, use_c5=USE_C5, use_c6=USE_C6,
        sl_atr=SL_ATR_MULT, tp_atr=TP_ATR_MULT,
        hold_max=HOLD_MAX_DAYS,
        min_history=MIN_HISTORY_DAYS,
        liq_floor=LIQ_FLOOR_USD,
        fee_side=FEE_SIDE, slip_side=SLIP_SIDE,
        regime_cols=REGIME_OVERLAY,
        extra_gate_cols=("is_production_universe", "tsmom_pos"),
        max_positions=MAX_POSITIONS,
        entry_price_col=ENTRY_PRICE_COL,
        corr_cap=None,
        start=BACKTEST_START, end=BACKTEST_END,
        label="v14_production",
    )


def position_size_frac(entry_price: float, atr14: float) -> tuple[float, float]:
    """Ukuran posisi sebagai fraksi ekuitas.

    Returns (ukuran_diinginkan, ukuran_dipakai). Keduanya dicatat di shadow log
    sebagai size_frac = dipakai / diinginkan (§2.6).

    Jarak SL = SL_ATR_MULT * ATR14 / entry. Ukuran = RISK / jarak_SL, di-cap
    MAX_EXPOSURE_FRAC karena spot tanpa leverage. Saat ekuitas bebas tidak cukup:
    ambil sisa yang ada — JANGAN lewati sinyal, JANGAN naikkan risk (§2.6).
    """
    if entry_price <= 0 or atr14 <= 0:
        raise ValueError(f"entry_price={entry_price} atr14={atr14} tidak valid")
    sl_dist_frac = SL_ATR_MULT * atr14 / entry_price
    wanted = RISK_PER_TRADE / sl_dist_frac
    return wanted, min(wanted, MAX_EXPOSURE_FRAC)


def barriers(entry_price: float, atr14: float) -> tuple[float, float]:
    """(stop_loss, take_profit) dijangkar di harga fill."""
    return (entry_price - SL_ATR_MULT * atr14,
            entry_price + TP_ATR_MULT * atr14)


def validate() -> None:
    """Cek konsistensi internal. Dipanggil saat import supaya config yang rusak
    gagal keras di detik pertama, bukan diam-diam menghasilkan sinyal salah."""
    assert UNIVERSE == ("BTC", "ETH"), "universe produksi frozen di BTC+ETH (§2.1 baris 1)"
    assert MOM_SHORT_DAYS < MOM_LONG_DAYS, "momentum pendek harus < panjang"
    assert TP_ATR_MULT == 2 * SL_ATR_MULT, "rasio TP:SL frozen 2:1 (1.5/3.0, §2.3)"
    assert 0 < RISK_PER_TRADE <= 0.03, "risk/trade dibatasi 3% oleh aturan project"
    assert MAX_POSITIONS >= len(UNIVERSE), (
        "max_positions tidak boleh lebih kecil dari ukuran universe — kalau lebih "
        "kecil ia berubah dari safety rail jadi filter aktif dan §2.7 batal")
    assert REGIME_OVERLAY == (), "TIDAK ADA regime overlay (§2.1 baris 14)"
    assert not (USE_C2 or USE_C5 or USE_C6), "C2/C5/C6 semuanya OFF (§2.1 baris 15)"
    assert USE_LEVERAGE is False and MAX_EXPOSURE_FRAC == 1.00, "spot, tanpa leverage (§2.6)"
    assert ENTRY_PRICE_COL == "next_open", "fill di OPEN t+1, tanpa pengecualian (§2.2)"
    assert TIME_EXIT_WARNING_DAY < TIME_EXIT_DAY, "alarm harus datang sebelum tutup paksa"
    assert abs(COST_ROUND_TRIP - 0.0030) < 1e-12, "biaya round-trip frozen 0.30% (§2.1 baris 12)"
    assert set(SHADOW_COLUMNS).isdisjoint({"btc_dd_shallow"}), (
        "btc_dd_shallow DILARANG — lookahead K1 (§2.3)")


validate()
