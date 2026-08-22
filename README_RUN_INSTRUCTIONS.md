# Running the V1.2 Data Acquisition Script

## Why this exists

The handoff doc (`V1.2_DATA_ACQUISITION_HANDOFF.md`) needs to hit CoinGecko and
Binance for real market data. This Cowork session's sandbox sits behind a
network allowlist that blocks both domains (confirmed directly — `curl` came
back `403 blocked-by-allowlist` on `api.coingecko.com`, `api.binance.com`,
`data.binance.vision`, even a control request to `google.com`). So the script
was written here, verified offline against mocked API responses, and handed
to you to run somewhere with real internet.

## 1. Install dependencies

```bash
pip install requests pandas
```

## 2. Sanity-check the universe first (recommended)

The full run can take a while (roughly 30–50 symbols × ~90 monthly downloads
each, plus REST fallback). Before committing to that, do a fast check of
just Step 1 (universe selection):

```bash
python crypto_v1_2_data_acquisition.py --universe-only
```

This prints the final symbol list, market caps, 24h volumes, and any flags
(widened-to-top-75, name-review terms, symbol collisions) — no files written,
no historical data pulled. If something in the universe looks wrong (a
ticker that shouldn't be there, a widen-to-75 trigger you didn't expect),
that's the moment to look into it before the long pull starts.

## 3. Run the full acquisition

```bash
python crypto_v1_2_data_acquisition.py
```

Optional: `--output-dir <path>` to change where CSVs + inventory land
(default: `./crypto-v1.2-data/`).

Progress prints per symbol as it goes (month-by-month archive pulls, then any
REST fallback, then QA flags). Expect it to take anywhere from several
minutes to an hour+ depending on connection speed and universe size — it's
mostly small file downloads from Binance's archive plus some paced REST calls.

## 4. What you get

- `crypto-v1.2-data/{SYMBOL}USDT.csv` — one file per symbol, standard Binance
  kline columns (`open_time, open, high, low, close, volume, close_time,
  quote_volume, trades, taker_buy_base, taker_buy_quote, ignore`).
- `crypto-v1.2-data/VERSION_1_2_DATA_INVENTORY.md` — per-symbol row counts,
  date ranges, and every QA flag raised (duplicates removed, gaps found and
  left unfilled, zero-volume days after listing, widen-to-75 trigger, name-
  review flags, symbol collisions, anything that failed to download).
- A console summary at the end: final universe, total rows, dropped symbols
  and why, elapsed time.

Move `VERSION_1_2_DATA_INVENTORY.md` into the "Trade with Claude" project as
`Ver. Crypto Trade/VERSION_1_2_DATA_INVENTORY.md` per the handoff doc's Step 4.

## 5. Read every flag before trusting the data

Per the handoff doc's own instruction ("flag it and ask rather than
guessing"), the script never silently patches problems:

- **Missing calendar days** are reported, never interpolated or
  forward-filled.
- **Universe under 25 names** (even after widening to top-75) ships anyway,
  but flagged loudly — that's a "stop and look" signal, not a pass/fail.
- **Symbol collisions** (two CoinGecko coins mapping to the same Binance
  pair) keep the higher-market-cap one and flag the loser for manual
  verification.
- **Name-review flags**: any coin whose name contains "wrapped/staked/
  restaked/bridged/pegged" that wasn't already caught by the CoinGecko
  category exclusion gets included but flagged — check these by hand, since
  category tagging on CoinGecko isn't perfectly complete.

## 6. Decision point (handoff doc Step 5)

Once you have the CSVs, choose:

- **(a) Continue in this same local session** into building the vectorbt
  indicator/gate/rank pipeline — you already have real internet here if
  anything needs re-pulling.
- **(b) Hand the CSVs + inventory back** to the cloud session to pick up the
  pipeline build from `VERSION_1_2_BACKTEST_KICKOFF.md`.

## Note on the test/verification files

`_test_mock_run.py` and `_test_output/` in this same folder are the offline
verification harness I used to exercise the script's logic against fake
CoinGecko/Binance responses (since the real APIs aren't reachable from this
session either). They're not part of the deliverable — safe to ignore or
delete; I wasn't able to remove them myself since file deletion in this
folder needs your explicit approval.
