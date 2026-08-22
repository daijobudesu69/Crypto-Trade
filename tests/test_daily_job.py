"""Tes job harian: bentuk pesan, mode kering, dan pengaman kredensial.

Yang diuji di sini adalah hal-hal yang kalau salah tidak akan terlihat sampai
uang sungguhan bergerak — atau sampai kredensial bocor.
"""
from __future__ import annotations
import os, sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import config_v14 as cfg
import notify
import sheets

ok = True


def cek(nama: str, kondisi: bool, ket: str = "") -> None:
    global ok
    print(f"  {'PASS' if kondisi else 'GAGAL'}  {nama}" + (f"  [{ket}]" if ket else ""))
    ok = ok and kondisi


# Fixture DIHITUNG dari config, tidak diketik tangan. Angka yang diketik dari
# tampilan sudah terlanjur dibulatkan dan membuat tes gagal karena selisih 0.01
# pada fixture-nya sendiri, bukan pada kodenya.
_PX, _ATR = 78338.03, 2124.27
_SL, _TP = cfg.barriers(_PX, _ATR)
_WANT, _USED = cfg.position_size_frac(_PX, _ATR)
TRADE = {"symbol": "BTC", "signal_date": "2026-08-21", "entry_date": "2026-08-22",
         "entry_px": _PX, "atr14": _ATR,
         "oco_stop_loss": _SL, "oco_take_profit": _TP,
         "size_frac_wanted": _WANT, "size_frac_used": _USED, "size_frac": _USED / _WANT,
         "hold_warning_date": "2026-09-03", "hold_force_exit_date": "2026-09-04"}

print("=== 1. Pesan masuk WAJIB memuat harga OCO (gerbang v1.4.3) ===")
m = notify.entry_message(TRADE)
cek("memuat harga Take Profit", f"{_TP:,.2f}" in m, f"{_TP:,.2f}")
cek("memuat harga Stop Loss", f"{_SL:,.2f}" in m, f"{_SL:,.2f}")
cek("memuat harga entry", f"{_PX:,.2f}" in m)
cek("memuat ukuran posisi", f"{100*_USED:,.1f}" in m)
cek("memuat tanggal tutup paksa", "2026-09-04" in m)
cek("menyebut OCO secara eksplisit", "OCO" in m)
cek("menyatakan ini forward test tanpa modal", "tanpa modal" in m.lower())

print("\n=== 2. Barrier di pesan berasal dari config, bukan angka lepas ===")
cek("SL = entry - 1.5xATR", abs(_SL - (_PX - cfg.SL_ATR_MULT * _ATR)) < 1e-9, f"{_SL:.2f}")
cek("TP = entry + 3.0xATR", abs(_TP - (_PX + cfg.TP_ATR_MULT * _ATR)) < 1e-9, f"{_TP:.2f}")
cek("jarak TP tepat 2x jarak SL", abs((_TP - _PX) - 2 * (_PX - _SL)) < 1e-9)
cek("ukuran = risk / jarak SL",
    abs(_WANT - cfg.RISK_PER_TRADE / (cfg.SL_ATR_MULT * _ATR / _PX)) < 1e-12, f"{_USED:.4f}")
cek("ukuran tidak pernah lewat 100% ekuitas (spot, tanpa leverage)",
    cfg.position_size_frac(100.0, 0.5)[1] <= cfg.MAX_EXPOSURE_FRAC)

print("\n=== 3. Alarm hari ke-13 ===")
POS = dict(TRADE, days_held=13, entry_date="2026-08-22")
a = notify.hold_alarm_message(POS)
cek("menyebut hari ke-13", "hari ke-13" in a)
cek("menyuruh tutup besok", "tutup paksa" in a.lower())
cek("mengingatkan membatalkan OCO", "batalkan" in a.lower())

print("\n=== 4. Heartbeat tetap terkirim walau tidak ada apa-apa ===")
h = notify.heartbeat_message({
    "run_date": "2026-08-22", "run_time": "00:05", "data_through": "2026-08-21",
    "open_positions": [], "n_signals": 0, "n_shadow_rows": 3,
    "last_signal_date": "2025-10-26", "days_since_last_signal": 299})
cek("menyatakan posisi terbuka tidak ada", "tidak ada" in h)
cek("melaporkan sinyal nol", "Sinyal hari ini: <b>0</b>" in h)
cek("menenangkan saat sepi panjang", "bukan berarti rusak" in h,
    "penting: 299 hari sepi itu normal secara historis")

print("\n=== 5. Mode kering aktif kalau kredensial tidak ada ===")
simpan = {k: os.environ.pop(k, None) for k in
          ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "GOOGLE_SERVICE_ACCOUNT_JSON",
           "SHEET_ID", "DRY_RUN")}
try:
    cek("Telegram kering tanpa kredensial", notify.is_dry_run())
    cek("Sheets kering tanpa kredensial", sheets.is_dry_run())
    os.environ["TELEGRAM_BOT_TOKEN"] = "x"
    cek("token saja belum cukup (chat id masih kosong)", notify.is_dry_run())
    os.environ["TELEGRAM_CHAT_ID"] = "y"
    cek("token + chat id -> mode basah", not notify.is_dry_run())
    os.environ["DRY_RUN"] = "1"
    cek("DRY_RUN=1 memaksa kering walau kredensial lengkap", notify.is_dry_run())
finally:
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DRY_RUN"):
        os.environ.pop(k, None)
    for k, v in simpan.items():
        if v is not None:
            os.environ[k] = v

print("\n=== 6. Kredensial tidak boleh bocor ke log ===")
import inspect
src_notify = inspect.getsource(notify)
src_sheets = inspect.getsource(sheets)
cek("notify tidak pernah mencetak isi respons mentah",
    "print(r.text" not in src_notify and "print(r.content" not in src_notify)
cek("notify tidak pernah mencetak token atau URL berisi token",
    "print(token" not in src_notify and "print(API.format" not in src_notify)
cek("sheets tidak pernah mencetak isi service account",
    "print(sa_raw" not in src_sheets and "{sa_raw}" not in src_sheets)
cek("header shadow_log cocok dengan kolom §2.4",
    all(c in sheets.SHADOW_HEADER for c in cfg.SHADOW_COLUMNS),
    f"{len(sheets.SHADOW_HEADER)} kolom")

print("\n=== 7. Workflow: tidak ada pull_request_target (§6.1 aturan 1) ===")
wf_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows")
bad = []
for f in os.listdir(wf_dir):
    if f.endswith((".yml", ".yaml")):
        txt = open(os.path.join(wf_dir, f), encoding="utf-8").read()
        # abaikan baris komentar yang justru MELARANGnya
        aktif = [l for l in txt.splitlines()
                 if "pull_request_target" in l and not l.strip().startswith("#")]
        if aktif:
            bad.append(f)
cek("tidak ada workflow memakai pull_request_target", not bad, str(bad))

print("\n" + ("SEMUA TES JOB HARIAN LOLOS" if ok else "ADA TES YANG GAGAL"))
raise SystemExit(0 if ok else 1)
