---
name: trader-scanner
description: >-
  Chart Scanner. Scans thousands of charts for setups: opening-range
  breakouts, flags, oversold bounces, on Indian stocks only. Builds the
  list of NSE equities (EQ series, intraday allowed) from NSE's official
  EQUITY_L.csv or a Nifty 50/100/200/500 constituents list,
  runs a pre-market scan of daily closes (20-day closing-high breakout, bull
  flag, reclaimed the 20-day average, oversold, oversold bounce) and an
  intraday opening-range breakout scan on 5-minute bars, then writes
  trader/watchlist.json for trader-news and trader-entry. Free Yahoo data
  (SYMBOL.NS),
  bar by bar. Use when the user says "scan the market", "find me setups",
  "what is breaking out", "opening range breakouts", "scan for bull flags",
  "oversold stocks", "pre-market scan", "build my watchlist", "what should I
  look at today", or at the start of a trading day before /trader-news.
argument-hint: "[universe | daily | orb] [--at HH:MM]"
---

# trader-scanner

The Chart Scanner is the first step of a day trade. It reads thousands of
free charts and lists the ones that match a rule. That is all it does. A scan
is a list of charts that match a rule, not a list of stocks to buy, and a
setup on a chart is not a prediction. Most people who day trade lose money;
the scanner does not change that. It only saves you from flipping through
5,000 charts by hand.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/scan.py" universe                       # -> trader/universe.txt
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily                          # before the open -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" daily --symbols RELIANCE,TCS,HDFCBANK  # or --watchlist FILE, --limit 200
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb                            # after 9:50 AM IST -> trader/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --at 10:15                 # cut the clock at 10:15 AM IST
python3 "${CLAUDE_SKILL_DIR}/scan.py" orb --watchlist trader/watchlist.json --json
```

Flags: `--min-price 5` and `--min-volume` (daily 1,000,000, orb 0),
`--or-minutes 15`, `--max-confirm 200` (orb stage 2),
`--now "2026-10-19 10:15"` (IST) to pin the clock, `--offline PATH`
to read saved Yahoo files, `--no-write`, `--show N`, `--json`. Exit code 0
when the scan found something, 1 when it found nothing or could not run (the
reason is one plain line).

## What it reads and writes

| file | what |
| --- | --- |
| `trader/universe.txt` | written by `universe`: one `SYMBOL\|Name` a line, read by `daily` and `orb` |
| `trader/watchlist.json` | written by `daily` and `orb`: `mode`, `as_of`, `universe`, `scanned`, `failed`, `hits[]`. trader-news and trader-entry read `hits[].symbol` |
| `trader/config.json` | read by `orb`: `no_entries_after` (default 15:30) |

## The universe

`universe` downloads NSE's list of listed equities
(`nsearchives.nseindia.com/content/equities/EQUITY_L.csv`, about 2,000+
rows) and keeps only the **EQ** series: BE/BZ (trade-for-trade) and SME
series cannot be traded intraday. `--list nifty500` (or nifty50, nifty100,
nifty200) uses that index's constituents instead: fewer, more liquid stocks
and a much faster scan. Every chart is read from Yahoo as `SYMBOL.NS`; for the BSE
listing write the symbol with `.BO`, like `RELIANCE.BO`.

Liquidity floors for India: `--min-price` ₹50 and `--min-volume` 200,000
shares a day by default. Penny stocks and thin SME counters are easy to move
and often sit at their circuit limits: keep the floors unless the user asks.

## The daily scan (before the open)

Yahoo's spark endpoint returns closes for 20 symbols a request, so the full NSE list (~2,000
symbols) takes about 100 requests, roughly a minute with the polite pause
(0.3 s); the Nifty 500 takes 25. Liquidity comes from the spark meta: last price at
least `--min-price` and last session's volume at least `--min-volume`. Run it
before the open or after the close: during the session the volume is the
day's so far, and today's daily bar is left out until 3:30 PM.

The setups are computed from daily closes only (spark has no highs or lows),
so a closing-high breakout here is not the same as an intraday-high breakout:

| setup | the rule on closes |
| --- | --- |
| `breakout_20d` | the last close is above every close of the 20 sessions before it |
| `bull_flag` | up more than 10 % over the 10 sessions before the last 5, then the last 5 closes inside a 5 % range |
| `reclaim_ma20` | the close before was under its 20-day average, the last close is above it |
| `oversold` | RSI 14 (Wilder) under 30 |
| `oversold_bounce` | RSI 14 was under 30 yesterday and today closed up |

A closing-high breakout fires more often than an intraday-high one (a stock can
close at a 20-day closing high without taking out its 20-day intraday high). Hits are
listed most traded first (price times volume).

## The opening-range breakout scan (during the session)

The opening range is the first `--or-minutes` (15) of the session: the 9:15,
9:35 and 9:40 five-minute bars. A hit is the first completed 5-minute bar
to CLOSE above the range high, at that bar's close time.

1. Stage 1, every symbol: spark closes of today's 5-minute bars, 20 a
   request. A chart goes on only if a completed bar after the range closed at
   or above the highest close of the range. That is a necessary condition
   (the range high is at least every range close), so no breakout is lost.
2. Stage 2, each candidate: the v8 chart with real highs and lows, one
   request each. Range high = the highest high of the range bars, range low =
   the lowest low. On a strong morning most charts are candidates, so stage 2
   checks at most `--max-confirm` (200) of them, the most traded first (price
   times volume so far), and prints PARTIAL with the count it skipped.
   `--max-confirm 0` checks them all.

Timing, live on Tue Oct 20 2026 (a strong morning): 100 liquid names at
10:01 AM took 35 seconds (5 spark requests, 70 v8 charts, 51 of 91 usable
charts broke out). The whole universe at 10:03 AM took about 4 minutes (298
spark requests, then 200 v8 charts): 2,448 usable charts, 1,782 candidates,
the 200 most traded checked, 119 of those broke out. For a fast answer, scan
the daily watchlist or a list of names you trade (`--watchlist`, `--symbols`).

A chart is usable only when every 5-minute bar from 9:15 to the cutoff is
there; a missing bar means a halt or a thin stock. Null closes are dropped,
a bar off the 5-minute grid (the live tick, like 10:58:07) is dropped, and a
bar counts only once it is complete (start + 5 minutes). Before 9:35 AM, on a
weekend or NSE holiday, after the close, or after `no_entries_after` (an expired
setup) it prints one plain line and writes nothing.

## Data and limits

- Yahoo's chart and spark endpoints are free, unofficial and can lag. Bars are
  5-minute bars, bar by bar, never tick by tick. query2 first, query1 on a 429
  or 5xx, one retry after a 5-second wait on a 429.
- Partial results are said as partial: "Fetched 480 of 500 charts (20
  failed or had no data)". Never round a partial scan up to "the whole market".
- NSE trading holidays for 2026 (NSE circular). Add 2027 to the code when NSE publishes it (usually in December).

## Steps

1. Once a week or so: `scan.py universe`.
2. Before the open: `scan.py daily`. Tell the user the counts (fetched,
   liquid, scanned, hits) and the top few by setup.
3. After 9:35 AM IST (9:45 to 10:15 is typical): `scan.py orb`. Tell the user how
   many usable charts broke their range and when, and that a later hit is
   closer to the price now.
4. The user picks one chart. Hand it on: `/trader-news TICKER` (what is moving
   it), then `/trader-entry TICKER` (the entry bar by bar). Never pick for them.

## Example

```
python3 scan.py universe --list nifty500
python3 scan.py daily                       # before 9:15 AM IST
python3 scan.py orb --at 09:45              # first 15-minute range = 9:15 to 9:30
```

The opening range is the 9:15, 9:20 and 9:25 five-minute bars. The first
completed bar to close above the range high is the hit, at its close time.
The user picks one chart and Claude hands it to `/trader-news SYMBOL`, then
`/trader-entry SYMBOL`.

India notes to say out loud when they apply: a stock at its upper circuit
cannot be bought at any price until it opens; a stock in the F&O ban period or
under ASM/GSM surveillance moves differently; results days gap.

## Rules

- Say what was scanned: how many symbols, how many charts came back, how many
  were usable, and the time of the data.
- Say "matched a rule", never "a buy", "a winner" or "a good trade".
- Never print a P&L, a return or a profit figure.
- A hit is stale once the price has moved on. Re-run before acting on it.

## What it never does

It never places, sizes or checks an order, never says which stock to buy,
never calls data real time, and never promises that a setup works.

## Safety

- Education and tooling, not investment advice; not a SEBI-registered adviser. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Kite passwords, TOTP and API secrets never go in the chat; the user logs in on zerodha.com.
