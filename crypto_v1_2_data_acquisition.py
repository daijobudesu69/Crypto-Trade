#!/usr/bin/env python3
"""
Crypto Swing Trade V1.2 — Data Acquisition Script
===================================================

Implements Steps 1-4 of `V1.2_DATA_ACQUISITION_HANDOFF.md`:
  1. Universe selection (CoinGecko top-50 by market cap, widen to top-75 if
     fewer than 25 survive; exclude stablecoins/wrapped/LST/LRT via CoinGecko
     category lookup; apply mcap >= $100M and Binance 24h quote volume >= $20M
     floors; require a live TRADING USDT spot pair on Binance).
  2. Historical daily klines from Binance's public monthly archive
     (data.binance.vision), REST API fallback for gaps and the trailing
     partial month.
  3. QA checks: duplicate open_time rows, missing calendar days (flagged,
     NEVER interpolated/forward-filled), zero-volume days after the first
     trading day.
  4. Save one CSV per symbol + a markdown inventory doc.

WHY THIS RUNS LOCALLY: this needs real outbound internet to CoinGecko and
Binance. A prior cloud sandbox session for this project confirmed those
domains are blocked there — that's why this script exists as a handoff
artifact instead of running directly in the cloud session.

USAGE
-----
    pip install requests pandas
    python crypto_v1_2_data_acquisition.py                 # full run
    python crypto_v1_2_data_acquisition.py --universe-only  # step 1 sanity check only, no download
    python crypto_v1_2_data_acquisition.py --output-dir ./my-data

Everything is a design-level default lifted straight from the handoff doc
(top-50/top-75, $100M mcap floor, $20M volume floor, 2019-01 archive start,
00:00 UTC daily boundary). See the CONFIG block below to adjust.
"""

import argparse
import io
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pandas as pd
    import requests
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with:  pip install requests pandas")
    sys.exit(1)

# ---------------------------------------------------------------------------
# CONFIG — every value here is a design-level decision from the handoff doc
# ---------------------------------------------------------------------------
COINGECKO_BASE = "https://api.coingecko.com/api/v3"
BINANCE_API_BASE = "https://api.binance.com/api/v3"
BINANCE_ARCHIVE_BASE = "https://data.binance.vision/data/spot/monthly/klines"

TOP_N_INITIAL = 50          # Step 1: primary universe rank cutoff
TOP_N_WIDE = 75             # Step 1: widen here if primary universe < MIN_UNIVERSE
FETCH_BUFFER = 150          # pull this many from CoinGecko once, so widening needs no 2nd call
MIN_UNIVERSE = 25           # doc: "do not silently ship a universe under 25 names"

MCAP_FLOOR = 100_000_000
VOLUME_FLOOR = 20_000_000

ARCHIVE_START = (2019, 1)   # doc: "or 2019-01 if listing date is unknown, whichever is later"
ARCHIVE_END_CAP = (2026, 8)  # doc's stated end month; script also self-caps to "today"

EXCLUDE_CATEGORY_KEYWORDS = [
    "stablecoin",
    "wrapped",
    "liquid staking",
    "liquid-staking",
    "liquid restaking",
    "liquid-restaking",
]
# Secondary safety net (NOT a primary filter): coin *names* containing these
# terms get flagged for manual review if category matching didn't already
# catch them. Symbol-prefix regexes are deliberately NOT used here — they
# false-positive constantly (WLD, WIF, STX, STRK, etc. all start with W/ST).
NAME_REVIEW_TERMS = ["wrapped", "staked", "restaked", "bridged", "pegged"]

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote", "ignore",
]
NUMERIC_FLOAT_COLS = ["open", "high", "low", "close", "volume",
                       "quote_volume", "taker_buy_base", "taker_buy_quote"]
NUMERIC_INT_COLS = ["open_time", "close_time", "trades"]

REQUEST_TIMEOUT = 20
ARCHIVE_DOWNLOAD_SLEEP = 0.15   # politeness pacing between monthly zip downloads
REST_PAGE_SLEEP = 0.3           # politeness pacing between REST kline pages
COINGECKO_CATEGORY_SLEEP = 8.0  # CoinGecko free tier is rate-limit sensitive (bumped from 1.5s after
                                 # observing sustained 429s on coins/markets even with 1.5s pacing)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "crypto-v1.2-data-acquisition/1.0"})

FLAGS = []  # list of (context, message) — collected across the whole run

# Set once at startup by probe_binance_api_reachable(). When False, api.binance.com
# (exchangeInfo / ticker / klines REST) is substituted or skipped — see that function
# for why this exists in this run's network environment.
BINANCE_API_REACHABLE = True


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def flag(context, msg):
    FLAGS.append((context, msg))
    log(f"  ⚠ FLAG [{context}]: {msg}")


def probe_binance_api_reachable():
    """One fast, low-timeout check of api.binance.com before the real run.

    This network was confirmed (via curl, PowerShell Test-NetConnection, and
    direct-IP connection — all bypassing DNS) to have api.binance.com/www.binance.com
    blocked at the raw TCP level, while data.binance.vision (a different host) and
    coingecko.com are unaffected. That's consistent with an IP-range block rather
    than DNS tampering or an SNI filter, and no VPN/proxy is configured on this
    machine to route around it. Rather than let every call into api.binance.com
    burn through get_json's full 5-attempt/20s-timeout retry cycle (~2 minutes each,
    guaranteed to fail), this does one quick probe and short-circuits downstream
    callers to a CoinGecko-sourced substitute (universe) or a skip-with-flag
    (REST kline fallback) instead.
    """
    global BINANCE_API_REACHABLE
    try:
        SESSION.get(f"{BINANCE_API_BASE}/ping", timeout=6)
        BINANCE_API_REACHABLE = True
    except requests.RequestException:
        BINANCE_API_REACHABLE = False
        flag("network", "api.binance.com unreachable from this network (TCP-level block, confirmed "
                         "via direct-IP test — not DNS). Universe pair-list/volume will be sourced "
                         "from CoinGecko's exchange-tickers endpoint instead, and REST kline fallback "
                         "(gap-filling + trailing partial month) will be skipped rather than retried. "
                         "data.binance.vision archive downloads are unaffected and used normally.")
    return BINANCE_API_REACHABLE


def get_json(url, params=None, max_retries=5, backoff=2.0):
    for attempt in range(1, max_retries + 1):
        try:
            r = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                wait = backoff * attempt
                log(f"Rate limited on {url}, waiting {wait:.0f}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if attempt == max_retries:
                raise
            wait = backoff * attempt
            log(f"Request failed ({e}), retrying in {wait:.0f}s (attempt {attempt}/{max_retries})")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url} after {max_retries} attempts")


def month_range(start_ym, end_ym):
    y, m = start_ym
    ey, em = end_ym
    while (y, m) <= (ey, em):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def last_complete_month_utc():
    now = datetime.now(timezone.utc)
    first_of_this_month = now.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    return (last_month_end.year, last_month_end.month)


def add_month(y, m, n=1):
    total = (y * 12 + (m - 1)) + n
    return total // 12, total % 12 + 1


# ---------------------------------------------------------------------------
# Step 1a — exclusion set (stablecoins / wrapped / LST / LRT) via CoinGecko categories
# ---------------------------------------------------------------------------
def build_exclusion_set():
    log("Fetching CoinGecko category list to find stablecoin/wrapped/LST/LRT categories...")
    categories = get_json(f"{COINGECKO_BASE}/coins/categories/list")
    matched = [c for c in categories if any(kw in c["name"].lower() for kw in EXCLUDE_CATEGORY_KEYWORDS)]

    if not matched:
        flag("universe", "No matching exclusion categories found on CoinGecko — "
                          "exclusion list may be incomplete this run.")
    else:
        log(f"Matched {len(matched)} exclusion categories: {[c['name'] for c in matched]}")

    excluded_ids = set()
    for cat in matched:
        cat_id = cat["category_id"]
        try:
            coins = get_json(
                f"{COINGECKO_BASE}/coins/markets",
                params={"vs_currency": "usd", "category": cat_id, "per_page": 250, "order": "market_cap_desc"},
                max_retries=6, backoff=6.0,
            )
            for c in coins:
                excluded_ids.add(c["id"])
            time.sleep(COINGECKO_CATEGORY_SLEEP)
        except Exception as e:
            flag("universe", f"Could not fetch category '{cat_id}' ({e}) — some exclusions may be missing.")

    log(f"Total excluded coin IDs from category lookup: {len(excluded_ids)}")
    return excluded_ids


# ---------------------------------------------------------------------------
# Step 1b/c — CoinGecko top coins + Binance universe data
# ---------------------------------------------------------------------------
def fetch_top_coins(n):
    log(f"Fetching top {n} coins by market cap from CoinGecko...")
    return get_json(
        f"{COINGECKO_BASE}/coins/markets",
        params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": n, "page": 1, "sparkline": "false"},
        max_retries=6, backoff=6.0,
    )


def fetch_binance_universe_data_via_coingecko():
    """Substitute for fetch_binance_universe_data() when api.binance.com is unreachable.

    CoinGecko's /exchanges/binance/tickers endpoint already aggregates Binance's live
    pair list and 24h volume (converted_volume.usd), so it stands in for Binance's own
    exchangeInfo + ticker/24hr. See probe_binance_api_reachable() for why this is needed.
    """
    log("Sourcing Binance USDT pair list + 24h volume from CoinGecko's exchange-tickers "
        "endpoint (api.binance.com unreachable — see network flag above)...")
    trading_usdt_pairs = set()
    quote_volume = {}
    page = 1
    while True:
        data = get_json(
            f"{COINGECKO_BASE}/exchanges/binance/tickers",
            params={"page": page},
            max_retries=6, backoff=6.0,
        )
        tickers = data.get("tickers", [])
        if not tickers:
            break
        for t in tickers:
            if t.get("target") != "USDT" or t.get("is_stale") or t.get("is_anomaly"):
                continue
            base = (t.get("base") or "").upper()
            vol_usd = (t.get("converted_volume") or {}).get("usd")
            if not base or vol_usd is None:
                continue
            symbol = f"{base}USDT"
            trading_usdt_pairs.add(symbol)
            quote_volume[symbol] = max(quote_volume.get(symbol, 0.0), float(vol_usd))
        page += 1
        time.sleep(COINGECKO_CATEGORY_SLEEP)

    log(f"CoinGecko reports {len(trading_usdt_pairs)} active Binance USDT pairs.")
    return trading_usdt_pairs, quote_volume


def fetch_binance_universe_data():
    if not BINANCE_API_REACHABLE:
        return fetch_binance_universe_data_via_coingecko()

    log("Fetching Binance exchangeInfo (symbol list)...")
    info = get_json(f"{BINANCE_API_BASE}/exchangeInfo")
    trading_usdt_pairs = {
        s["symbol"] for s in info["symbols"]
        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"
    }
    log(f"Binance has {len(trading_usdt_pairs)} active USDT spot pairs.")

    log("Fetching Binance 24hr ticker (volume data)...")
    tickers = get_json(f"{BINANCE_API_BASE}/ticker/24hr")
    quote_volume = {t["symbol"]: float(t["quoteVolume"]) for t in tickers if t["symbol"] in trading_usdt_pairs}
    return trading_usdt_pairs, quote_volume


# ---------------------------------------------------------------------------
# Step 1d — build the final universe
# ---------------------------------------------------------------------------
def build_universe(excluded_ids):
    buffer_coins = fetch_top_coins(FETCH_BUFFER)
    trading_pairs, quote_volume = fetch_binance_universe_data()

    def try_build(rank_limit):
        universe = []
        seen_binance_symbols = {}
        local_flags = []

        for coin in buffer_coins[:rank_limit]:
            cid = coin["id"]
            symbol = (coin.get("symbol") or "").upper()
            mcap = coin.get("market_cap") or 0
            name = coin.get("name") or ""

            if cid in excluded_ids:
                continue

            name_lower = name.lower()
            if any(term in name_lower for term in NAME_REVIEW_TERMS):
                local_flags.append((
                    symbol or cid,
                    f"name '{name}' contains a staking/wrapping term but wasn't caught by the "
                    f"category exclusion list — included pending manual verification.",
                ))

            if mcap < MCAP_FLOOR:
                continue
            if not symbol:
                continue

            binance_symbol = f"{symbol}USDT"
            if binance_symbol not in trading_pairs:
                continue

            vol = quote_volume.get(binance_symbol, 0.0)
            if vol < VOLUME_FLOOR:
                continue

            if binance_symbol in seen_binance_symbols:
                prev_cid = seen_binance_symbols[binance_symbol]
                local_flags.append((
                    binance_symbol,
                    f"symbol collision: both '{prev_cid}' and '{cid}' map to {binance_symbol} — "
                    f"kept the higher-market-cap one ('{prev_cid}'), verify manually.",
                ))
                continue

            seen_binance_symbols[binance_symbol] = cid
            universe.append({
                "coingecko_id": cid,
                "symbol": symbol,
                "name": name,
                "binance_symbol": binance_symbol,
                "market_cap": mcap,
                "binance_24h_quote_volume": vol,
                "coingecko_rank": coin.get("market_cap_rank"),
            })

        return universe, local_flags

    universe, local_flags = try_build(TOP_N_INITIAL)
    widened = False

    if len(universe) < MIN_UNIVERSE:
        flag("universe", f"Only {len(universe)} names survived top-{TOP_N_INITIAL} filtering "
                          f"(< {MIN_UNIVERSE} floor). Widening to top-{TOP_N_WIDE} by market cap "
                          f"per handoff-doc instructions.")
        universe, local_flags = try_build(TOP_N_WIDE)
        widened = True
        if len(universe) < MIN_UNIVERSE:
            flag("universe", f"STILL only {len(universe)} names after widening to top-{TOP_N_WIDE}. "
                              f"Shipping under the {MIN_UNIVERSE}-name floor per doc instructions "
                              f"('flag it and report why' rather than abort) — manual review needed.")

    for ctx, msg in local_flags:
        flag(ctx, msg)

    return universe, widened


# ---------------------------------------------------------------------------
# Step 2 — historical daily klines (monthly archive + REST fallback)
# ---------------------------------------------------------------------------
def download_monthly_klines(binance_symbol, year, month, max_retries=3):
    url = f"{BINANCE_ARCHIVE_BASE}/{binance_symbol}/1d/{binance_symbol}-1d-{year:04d}-{month:02d}.zip"
    for attempt in range(1, max_retries + 1):
        try:
            r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            if attempt == max_retries:
                flag(binance_symbol, f"Archive download failed for {year:04d}-{month:02d} "
                                      f"after {max_retries} attempts: {e}")
                return None
            time.sleep(2 * attempt)
            continue

        if r.status_code == 404:
            return None  # not published: before listing, gap, or too-recent month
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == max_retries:
                flag(binance_symbol, f"Archive server error {r.status_code} for {year:04d}-{month:02d} "
                                      f"after {max_retries} attempts — will try REST fallback.")
                return None
            time.sleep(2 * attempt)
            continue
        if r.status_code != 200:
            flag(binance_symbol, f"Unexpected HTTP {r.status_code} for {year:04d}-{month:02d}, skipping.")
            return None

        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                csv_name = zf.namelist()[0]
                with zf.open(csv_name) as f:
                    df = pd.read_csv(f, header=None, names=KLINE_COLUMNS)
        except zipfile.BadZipFile:
            flag(binance_symbol, f"Corrupt zip for {year:04d}-{month:02d} — will try REST fallback.")
            return None

        # Some Binance archive vintages ship a literal header row inside the CSV;
        # detect it (non-numeric open_time) and strip it rather than assume a format.
        if not pd.api.types.is_numeric_dtype(df["open_time"]):
            df = df[pd.to_numeric(df["open_time"], errors="coerce").notna()].reset_index(drop=True)
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce").astype("int64")
        return df

    return None


def fetch_klines_rest(binance_symbol, start_ms, end_ms):
    if not BINANCE_API_REACHABLE:
        # api.binance.com is unreachable from this network (see probe_binance_api_reachable).
        # Skip the attempt outright rather than burn ~2 minutes per call on a guaranteed
        # connection timeout; the caller already flags this range as an unfilled gap.
        return None

    all_rows = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": binance_symbol, "interval": "1d", "startTime": cursor,
                   "endTime": end_ms, "limit": 1000}
        try:
            batch = get_json(f"{BINANCE_API_BASE}/klines", params=params)
        except Exception as e:
            flag(binance_symbol, f"REST kline fetch failed for range starting {cursor}: {e}")
            break
        if not batch:
            break
        all_rows.extend(batch)
        last_open_time = batch[-1][0]
        if last_open_time <= cursor:
            break  # guard against an infinite loop
        cursor = last_open_time + 24 * 60 * 60 * 1000
        if len(batch) < 1000:
            break
        time.sleep(REST_PAGE_SLEEP)

    if not all_rows:
        return None
    return pd.DataFrame(all_rows, columns=KLINE_COLUMNS)


def normalize_dtypes(df):
    for c in NUMERIC_FLOAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in NUMERIC_INT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


def pull_symbol_history(binance_symbol, archive_start, archive_end):
    frames = []
    missing_months = []
    first_seen = None

    for y, m in month_range(archive_start, archive_end):
        df = download_monthly_klines(binance_symbol, y, m)
        if df is None:
            if first_seen is not None:
                missing_months.append((y, m))
            continue  # before first_seen: expected (not yet listed), no flag
        first_seen = first_seen or (y, m)
        frames.append(df)
        time.sleep(ARCHIVE_DOWNLOAD_SLEEP)

    # REST fallback for months that 404'd/failed *after* listing began
    for (y, m) in missing_months:
        month_start = datetime(y, m, 1, tzinfo=timezone.utc)
        ny, nm = add_month(y, m)
        month_end = datetime(ny, nm, 1, tzinfo=timezone.utc)
        df = fetch_klines_rest(binance_symbol, int(month_start.timestamp() * 1000),
                                int(month_end.timestamp() * 1000))
        if df is not None:
            frames.append(df)
        else:
            flag(binance_symbol, f"No data (archive or REST) for {y:04d}-{m:02d} — "
                                  f"real gap, left unfilled per QA policy.")

    # REST fallback for the trailing partial period after the last complete archive month
    ay, am = add_month(*archive_end)
    trailing_start = datetime(ay, am, 1, tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if trailing_start < now:
        df = fetch_klines_rest(binance_symbol, int(trailing_start.timestamp() * 1000),
                                int(now.timestamp() * 1000))
        if df is not None:
            frames.append(df)

    if not frames:
        return None

    full = pd.concat(frames, ignore_index=True)
    full = normalize_dtypes(full)
    return full


# ---------------------------------------------------------------------------
# Step 3 — QA
# ---------------------------------------------------------------------------
def qa_and_clean(symbol, df):
    local_flags = []
    before = len(df)
    df = df.drop_duplicates(subset="open_time", keep="first")
    dupes_removed = before - len(df)
    if dupes_removed:
        local_flags.append(f"{dupes_removed} duplicate open_time row(s) removed")

    df = df.sort_values("open_time").reset_index(drop=True)
    df["date"] = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.date

    full_range = pd.date_range(df["date"].min(), df["date"].max(), freq="D").date
    have = set(df["date"])
    missing_days = sorted(set(full_range) - have)
    if missing_days:
        sample = missing_days[:10]
        suffix = "..." if len(missing_days) > 10 else ""
        local_flags.append(f"{len(missing_days)} missing calendar day(s) in range — "
                            f"NOT interpolated/forward-filled. Sample: {sample}{suffix}")

    first_date = df["date"].min()
    zero_vol_after_start = df[(df["volume"] == 0) & (df["date"] != first_date)]
    if len(zero_vol_after_start):
        local_flags.append(f"{len(zero_vol_after_start)} zero-volume day(s) after the first trading "
                            f"day — possible placeholder/stub bars, flagged (not removed).")

    for msg in local_flags:
        flag(symbol, msg)

    meta = {
        "row_count": len(df),
        "first_date": str(df["date"].min()) if len(df) else None,
        "last_date": str(df["date"].max()) if len(df) else None,
        "flags": local_flags,
    }
    return df, meta


# ---------------------------------------------------------------------------
# Step 4 — output
# ---------------------------------------------------------------------------
def write_inventory_md(path, rows, dropped, widened, elapsed):
    lines = [
        "# V1.2 Data Inventory — Crypto Swing Trade",
        "",
        f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Universe size:** {len(rows)} symbols" +
        (" (widened to top-75 by market cap — top-50 alone yielded fewer than 25 survivors)"
         if widened else " (top-50 by market cap)"),
        f"**Total rows pulled:** {sum(r['row_count'] for r in rows):,}",
        f"**Elapsed time:** {elapsed / 60:.1f} min",
        "",
        "## Per-symbol inventory",
        "",
        "| Symbol | CoinGecko ID | First date | Last date | Rows | Market cap | "
        "Binance 24h quote vol | QA flags |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        flags_str = "; ".join(r["flags"]) if r["flags"] else "—"
        lines.append(
            f"| {r['symbol']} | {r['coingecko_id']} | {r['first_date']} | {r['last_date']} | "
            f"{r['row_count']} | ${r['market_cap']:,.0f} | ${r['binance_24h_quote_volume']:,.0f} | "
            f"{flags_str} |"
        )
    lines.append("")

    if dropped:
        lines.append("## Dropped symbols (no usable data)")
        lines.append("")
        for sym, reason in dropped:
            lines.append(f"- **{sym}**: {reason}")
        lines.append("")

    if FLAGS:
        lines.append("## All flags raised during acquisition")
        lines.append("")
        for ctx, msg in FLAGS:
            lines.append(f"- **[{ctx}]** {msg}")
        lines.append("")

    lines += [
        "## Notes",
        "",
        "- Snapshot convention: 00:00 UTC daily boundary (Binance native kline boundary).",
        "- Universe is a **current snapshot**, not point-in-time — survivorship bias applies "
        "(see V1.1 §7 / V1.2 kickoff §1).",
        "- Gaps in daily continuity were flagged, not interpolated or forward-filled.",
        "- Move this file into the \"Trade with Claude\" project as "
        "`Ver. Crypto Trade/VERSION_1_2_DATA_INVENTORY.md` per the handoff doc.",
        "",
    ]

    path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Inventory written to {path}")


def print_final_report(output_dir, inventory_path, rows, dropped, elapsed):
    print("\n" + "=" * 70)
    print("DATA ACQUISITION COMPLETE")
    print("=" * 70)
    print(f"Final universe: {len(rows)} symbols")
    print(", ".join(r["symbol"] for r in rows))
    print(f"Total rows pulled: {sum(r['row_count'] for r in rows):,}")
    print(f"Elapsed: {elapsed / 60:.1f} min")
    if dropped:
        print(f"\nDropped ({len(dropped)}): " + ", ".join(f"{s} ({r})" for s, r in dropped))
    if FLAGS:
        print(f"\n{len(FLAGS)} flag(s) raised — see {inventory_path} for the full list.")
    print(f"\nCSV files saved in: {output_dir.resolve()}")
    print(f"Inventory doc: {inventory_path.resolve()}")
    print("\nNext step per handoff doc — choose one:")
    print("  (a) Continue straight into building the vectorbt indicator/gate/rank pipeline")
    print("      in this same on-computer session.")
    print("  (b) Stop here, move the CSVs + inventory doc into the project folder, and hand")
    print("      back to the cloud session to pick up the pipeline build from")
    print("      VERSION_1_2_BACKTEST_KICKOFF.md.")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="Crypto Swing Trade V1.2 — Data Acquisition")
    p.add_argument("--universe-only", action="store_true",
                    help="Run Step 1 (universe selection) only, print the result, and exit. "
                         "Useful as a quick sanity check before committing to the full, "
                         "potentially hours-long historical pull.")
    p.add_argument("--output-dir", default="crypto-v1.2-data",
                    help="Directory to save per-symbol CSVs + inventory doc "
                         "(default: ./crypto-v1.2-data)")
    return p.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    inventory_path = output_dir / "VERSION_1_2_DATA_INVENTORY.md"

    t_start = time.time()

    log("Probing api.binance.com reachability...")
    if probe_binance_api_reachable():
        log("api.binance.com is reachable — using it directly.")
    else:
        log("api.binance.com is NOT reachable from this network — using CoinGecko-sourced "
            "substitute for universe data, and skipping REST kline fallback (see flag above).")

    log("=== STEP 1: Universe selection ===")
    excluded_ids = build_exclusion_set()
    universe, widened = build_universe(excluded_ids)

    if not universe:
        log("FATAL: universe is empty after filtering. Aborting before any data pull.")
        sys.exit(1)

    log(f"Final universe: {len(universe)} symbols" + (" (widened to top-75)" if widened else " (top-50)"))
    for u in universe:
        log(f"  {u['binance_symbol']:12s} mcap=${u['market_cap']:,.0f}  "
            f"24h_vol=${u['binance_24h_quote_volume']:,.0f}  rank={u['coingecko_rank']}")

    if args.universe_only:
        log("\n--universe-only set: stopping after Step 1 (no download, no files written).")
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    archive_end = min(ARCHIVE_END_CAP, last_complete_month_utc())

    log("=== STEP 2 & 3: Historical pull + QA per symbol ===")
    inventory_rows = []
    dropped_symbols = []

    for u in universe:
        sym = u["binance_symbol"]
        log(f"Pulling {sym}...")
        try:
            raw = pull_symbol_history(sym, ARCHIVE_START, archive_end)
        except Exception as e:
            flag(sym, f"Hard failure during pull: {e}")
            dropped_symbols.append((sym, str(e)))
            continue

        if raw is None or raw.empty:
            flag(sym, "No historical data returned at all — dropped from final output.")
            dropped_symbols.append((sym, "no data returned"))
            continue

        clean, meta = qa_and_clean(sym, raw)
        out_path = output_dir / f"{sym}.csv"
        clean[KLINE_COLUMNS].to_csv(out_path, index=False)

        inventory_rows.append({
            "symbol": sym,
            "coingecko_id": u["coingecko_id"],
            "name": u["name"],
            "row_count": meta["row_count"],
            "first_date": meta["first_date"],
            "last_date": meta["last_date"],
            "market_cap": u["market_cap"],
            "binance_24h_quote_volume": u["binance_24h_quote_volume"],
            "flags": meta["flags"],
        })
        log(f"  -> {meta['row_count']} rows, {meta['first_date']} to {meta['last_date']}")

    elapsed = time.time() - t_start

    log("=== STEP 4: Writing inventory ===")
    write_inventory_md(inventory_path, inventory_rows, dropped_symbols, widened, elapsed)

    log("=== STEP 5: Summary ===")
    print_final_report(output_dir, inventory_path, inventory_rows, dropped_symbols, elapsed)


if __name__ == "__main__":
    main()
