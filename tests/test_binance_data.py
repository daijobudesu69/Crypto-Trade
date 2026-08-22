"""Tes perilaku pengambil data Binance.

Tiga jebakan yang ditangani binance_data.py sanggup menggagalkan sistem produksi
secara diam-diam. Diuji di sini supaya kegagalannya berisik, bukan senyap.
"""
from __future__ import annotations
import os, sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import binance_data as bd
import config_v14 as cfg

ok = True


def cek(nama: str, kondisi: bool, ket: str = "") -> None:
    global ok
    print(f"  {'PASS' if kondisi else 'GAGAL'}  {nama}" + (f"  [{ket}]" if ket else ""))
    ok = ok and kondisi


print("=== 1. Lilin yang belum tutup wajib dibuang ===")
# Job cron jalan 00:05 UTC: lilin hari ini baru berumur 5 menit. Memakainya =
# menghitung sinyal dari harga yang masih bergerak.
mid = int(pd.Timestamp("2026-08-16 12:00", tz="UTC").timestamp() * 1000)
d = bd.fetch_klines("BTCUSDT", "2026-08-14", "2026-08-16", now_ms=mid)
cek("lilin hari berjalan tidak ikut terpakai",
    d.index.max() == pd.Timestamp("2026-08-15", tz="UTC"),
    f"terakhir {d.index.max().date()}, dibuang {d.attrs['dropped_open_candles']}")

lima_menit = int(pd.Timestamp("2026-08-16 00:05", tz="UTC").timestamp() * 1000)
d2 = bd.fetch_klines("BTCUSDT", "2026-08-14", "2026-08-16", now_ms=lima_menit)
cek("jam 00:05 UTC -> hari kemarin yang dipakai, bukan hari ini",
    d2.index.max() == pd.Timestamp("2026-08-15", tz="UTC"))

print("\n=== 2. Rentang tanggal harus persis (days_listed = nomor baris) ===")
# Menarik lebih awal dari yang diminta akan menggeser days_listed dan mengubah
# kapan gate 60-hari lolos -- bug yang tidak kelihatan sampai hasilnya beda.
d3 = bd.fetch_klines("BTCUSDT", "2019-01-01", "2019-03-31")
cek("mulai persis di tanggal yang diminta", d3.index.min() == pd.Timestamp("2019-01-01", tz="UTC"))
cek("berakhir persis di tanggal yang diminta (inklusif)",
    d3.index.max() == pd.Timestamp("2019-03-31", tz="UTC"))
cek("jumlah baris = jumlah hari kalender", len(d3) == 90, f"{len(d3)} baris")

print("\n=== 3. Halaman >1000 lilin: tanpa duplikat, tanpa lubang ===")
# Batas API 1000/panggilan; sambungan halaman rawan mengulang satu lilin.
d4 = bd.fetch_klines("BTCUSDT", "2019-01-01", "2022-12-31")
n_hari = (pd.Timestamp("2022-12-31", tz="UTC") - pd.Timestamp("2019-01-01", tz="UTC")).days + 1
cek("lebih dari satu halaman tertarik", len(d4) > bd.MAX_LIMIT, f"{len(d4)} lilin")
cek("nol duplikat open_time", not d4.index.has_duplicates)
cek("jumlah baris = jumlah hari kalender", len(d4) == n_hari, f"{len(d4)} vs {n_hari}")
cek("index terurut naik", d4.index.is_monotonic_increasing)

print("\n=== 4. Lubang tanggal harus gagal keras, bukan didiamkan ===")
# Lubang berarti mom_28 diam-diam mengukur 29 hari kalender tanpa memberi tahu.
bolong = d3.drop(d3.index[30])
try:
    bd.assert_contiguous(bolong, "UJI")
    cek("lubang tanggal terdeteksi", False, "TIDAK terdeteksi")
except RuntimeError as e:
    cek("lubang tanggal terdeteksi dan dilempar", "1 hari hilang" in str(e))
bd.assert_contiguous(d3, "UJI")
cek("data utuh lolos tanpa keluhan", True)

print("\n=== 5. Kolom sesuai yang dibutuhkan features.add_features ===")
cek("kolom OHLCV lengkap", list(d3.columns) == bd.OHLCV_COLS, str(list(d3.columns)))
cek("semua bertipe float", all(str(t) == "float64" for t in d3.dtypes))
cek("index bertimezone UTC", str(d3.index.tz) == "UTC")

print("\n" + ("SEMUA TES PENGAMBIL DATA LOLOS" if ok else "ADA TES YANG GAGAL"))
raise SystemExit(0 if ok else 1)
