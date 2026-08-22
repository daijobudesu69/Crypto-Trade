"""Tulis shadow log ke Google Sheets. Mode kering kalau kredensial tidak ada.

Dua sheet:
  shadow_log  satu baris per kandidat per hari — TERMASUK yang tidak jadi trade.
              Inilah data yang 6-12 bulan lagi diuji dengan BH-FDR yang benar.
  runs        satu baris per eksekusi job — untuk membuktikan cron benar-benar
              jalan tiap hari, dan membedakan "tidak ada sinyal" dari "job mati".

gspread diimpor MALAS (di dalam fungsi) supaya mode kering tetap jalan di mesin
yang tidak memasang paketnya — penting untuk tes dan untuk pengembangan lokal.

Kredensial dibaca dari environment. SHEET_ID ikut jadi secret walau bukan
kredensial: ia mengungkap posisi terbuka dan ukurannya secara real-time
(§6.1 aturan 3).
"""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone

import config_v14 as cfg

SHADOW_SHEET = "shadow_log"
RUNS_SHEET = "runs"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

SHADOW_HEADER = (["date", "symbol"] + list(cfg.SHADOW_COLUMNS)
                 + [f"mom_{cfg.MOM_SHORT_DAYS}", f"mom_{cfg.MOM_LONG_DAYS}",
                    "close", "atr14", "med_qvol_20", "days_listed", "tsmom_pos"])
RUNS_HEADER = ["run_utc", "data_through", "n_signals", "n_open_positions",
               "n_shadow_rows", "n_alarms", "status", "note"]


def credentials() -> tuple[str | None, str | None]:
    return os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"), os.environ.get("SHEET_ID")


def is_dry_run() -> bool:
    if os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False"):
        return True
    sa, sheet_id = credentials()
    return not (sa and sheet_id)


def check_sheet_id(sheet_id: str) -> str | None:
    """Kesalahan format SHEET_ID yang bisa dideteksi sebelum memanggil API.

    Google membalas kesalahan ini dengan HALAMAN HTML, bukan pesan error yang
    berguna — jadi lebih baik ditangkap di sini.
    """
    if not sheet_id:
        return "SHEET_ID kosong"
    s = sheet_id.strip()
    if s != sheet_id:
        return "SHEET_ID punya spasi/baris baru di ujung — salin ulang tanpa spasi"
    if s.startswith("http"):
        return ("SHEET_ID berisi URL lengkap. Ambil HANYA bagian di antara "
                "'/d/' dan '/edit', misal 1AbCdEf...XyZ")
    if "/" in s:
        return "SHEET_ID mengandung '/' — itu potongan URL, bukan ID-nya"
    if len(s) < 30:
        return f"SHEET_ID cuma {len(s)} karakter; ID Google Sheets biasanya ~44"
    return None


def _diagnose(err: Exception, sheet_id: str) -> str:
    """Terjemahkan kegagalan gspread jadi sebab dan tindakan yang jelas.

    Google sering membalas dengan halaman HTML lengkap saat spreadsheet tidak
    bisa dibuka. Menyalin halaman itu ke log dan ke pesan Telegram membuat
    penyebab sebenarnya tenggelam di ribuan karakter CSS.
    """
    t = str(err)
    html_page = "<!DOCTYPE html" in t or "<html" in t
    if html_page or "unable to open the file" in t or "Page Not Found" in t:
        return ("Google membalas halaman 'tidak bisa membuka file'. Dua sebab, "
                "urut dari yang paling sering:\n"
                "  1. Spreadsheet BELUM di-share ke email service account "
                "(client_email di file JSON) sebagai Editor\n"
                "  2. SHEET_ID salah — harus bagian antara '/d/' dan '/edit', "
                "bukan URL lengkap")
    if "has not been used in project" in t or "SERVICE_DISABLED" in t:
        return ("Google Sheets API belum di-enable di project Google Cloud yang "
                "memiliki service account ini.")
    if "PERMISSION_DENIED" in t or "403" in t:
        return ("Service account tidak punya izin tulis. Share spreadsheet "
                "sebagai EDITOR, bukan Viewer.")
    if "invalid_grant" in t or "JWT" in t:
        return ("Kredensial service account ditolak. Kemungkinan key sudah "
                "di-revoke, atau isi JSON kepotong saat ditempel.")
    return t[:300]


def _client():
    """Buka spreadsheet. Diimpor malas — lihat docstring modul."""
    import gspread
    from google.oauth2.service_account import Credentials

    sa_raw, sheet_id = credentials()
    salah = check_sheet_id(sheet_id or "")
    if salah:
        raise RuntimeError(salah)
    try:
        info = json.loads(sa_raw)
    except json.JSONDecodeError as e:
        # jangan pernah cetak sa_raw: itu kunci privat penuh
        raise RuntimeError(
            "GOOGLE_SERVICE_ACCOUNT_JSON bukan JSON yang sah. Tempelkan ISI file "
            f"JSON-nya secara utuh, bukan path-nya. ({e.msg})") from None
    email = info.get("client_email", "(client_email tidak ada di JSON)")
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    try:
        return gspread.authorize(creds).open_by_key(sheet_id)
    except Exception as e:
        raise RuntimeError(
            f"tidak bisa membuka spreadsheet.\n{_diagnose(e, sheet_id)}\n"
            f"  Email yang harus di-share: {email}\n"
            f"  SHEET_ID dipakai: {sheet_id[:8]}...{sheet_id[-4:]} "
            f"({len(sheet_id)} karakter)") from None


# shadow_log tumbuh 3 baris/hari (BTC + ETH + SOL). Default gspread 1000 baris
# habis dalam ~11 bulan, dan forward test ini dirancang berjalan bertahun-tahun.
# Kegagalannya akan muncul jauh di kemudian hari, dalam bentuk baris yang diam-
# diam tidak tertulis -- persis jenis kegagalan senyap yang paling mahal di sini.
# 20.000 baris cukup untuk ~18 tahun dan tidak memakan kuota apa pun kalau kosong.
INITIAL_ROWS = 20_000


def _worksheet(book, title: str, header: list[str]):
    """Ambil worksheet, buat kalau belum ada, dan pastikan barisnya berjudul."""
    try:
        ws = book.worksheet(title)
    except Exception:
        ws = book.add_worksheet(title=title, rows=INITIAL_ROWS, cols=max(len(header), 26))
        ws.append_row(header, value_input_option="RAW")
        return ws
    if not ws.row_values(1):
        ws.append_row(header, value_input_option="RAW")
    return ws


def _rows_to_lists(rows: list[dict], header: list[str]) -> list[list]:
    out = []
    for r in rows:
        out.append(["" if r.get(k) is None else r.get(k) for k in header])
    return out


def append_shadow(rows: list[dict]) -> bool:
    """Tambahkan baris shadow log. True kalau berhasil (atau tercetak saat kering)."""
    if not rows:
        return True
    data = _rows_to_lists(rows, SHADOW_HEADER)
    if is_dry_run():
        print(f"--- GOOGLE SHEETS '{SHADOW_SHEET}' (MODE KERING, tidak ditulis) ---")
        print("  " + " | ".join(SHADOW_HEADER))
        for r in data:
            print("  " + " | ".join("" if v == "" else
                                    (f"{v:.6g}" if isinstance(v, float) else str(v))
                                    for v in r))
        print("-" * 70)
        return True
    book = _client()
    ws = _worksheet(book, SHADOW_SHEET, SHADOW_HEADER)
    ws.append_rows(data, value_input_option="RAW")
    return True


def append_run(status: str, data_through: str, n_signals: int, n_open: int,
               n_shadow: int, n_alarms: int, note: str = "") -> bool:
    """Catat satu eksekusi job. Inilah bukti cron hidup — tanpa ini, 'tidak ada
    sinyal' dan 'job mati' terlihat sama persis di Sheets."""
    row = [datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), data_through,
           n_signals, n_open, n_shadow, n_alarms, status, note]
    if is_dry_run():
        print(f"--- GOOGLE SHEETS '{RUNS_SHEET}' (MODE KERING, tidak ditulis) ---")
        print("  " + " | ".join(RUNS_HEADER))
        print("  " + " | ".join(str(v) for v in row))
        print("-" * 70)
        return True
    book = _client()
    ws = _worksheet(book, RUNS_SHEET, RUNS_HEADER)
    ws.append_row(row, value_input_option="RAW")
    return True
