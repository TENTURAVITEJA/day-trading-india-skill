---
name: trader-entry
description: >-
  Entry Timer. Watches a setup bar by bar and tells you the moment to get in.
  Reads Zerodha Kite 1-minute or 5-minute candles, builds the opening range (the
  first 15 minutes by default) and calls the entry on the first completed bar
  that CLOSES above the range high, or above a level you give it (like a
  20-day high from the scanner), with an optional volume check. Says waiting
  and how far, triggered with the time and the price, expired, or not open
  yet, and writes trader/entry.json for /trader-exit. Long side only. It never
  places an order. Use when the user says "tell me when to get in", "watch
  ADANIENT", "has it broken out", "did it break the opening range", "entry
  signal", "time my entry", "check my watchlist", or after /trader-scanner
  and /trader-news, before /trader-exit.
argument-hint: "[TICKER] [--setup orb|level] [--interval 5m|1m]"
---

# trader-entry

The Entry Timer turns a setup into one moment: the bar that confirms it. It
reads bars, 1-minute or 5-minute, and waits for each one to finish. A price
that pokes above the high in the middle of a bar is not an entry. A bar that
CLOSES above it is. Then it says the time and the price, writes them down for
the Exit Planner, and stops. It never buys anything. Day trading loses most
people money, and a clean signal does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ADANIENT                  # 15-minute opening range, 5-minute bars
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ADANIENT --write          # also save trader/entry.json
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ADANIENT --setup level --level 2800.00
python3 "${CLAUDE_SKILL_DIR}/entry.py" check ADANIENT --interval 1m --min-volume-x 1.5
python3 "${CLAUDE_SKILL_DIR}/entry.py" watch ADANIENT --until 15:30    # one line per closed bar, stops on the trigger
python3 "${CLAUDE_SKILL_DIR}/entry.py" watchlist                   # every hit in trader/watchlist.json
```

Other flags: `--or-minutes N` (default 15), `--json`, `--offline FILE` (saved
Kite candles `{"candles": [...]}`, `.json` or `.json.gz`, or a folder of
`<TICKER>.json`), `--now "2026-11-11 10:20"` (IST) to replay a day.
`watch` with `--now` or `--offline` runs on a simulated clock and replays the
day bar by bar without waiting.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/watchlist.json` | the scanner's hits (`hits[].symbol`), for `watchlist` |
| `trader/config.json` | `no_entries_before` (09:30) and `no_entries_after` (15:30), IST. Optional |
| Zerodha Kite | today's 5-minute (or 1-minute) candles, and the live quote for the last trade time and previous close |
| `trader/entry.json` | with `check --write`, or when `watch` triggers: symbol, setup, status, time, bar, price, level, opening range |

## The rule

- **Opening range (orb).** The high and low of the first 15 minutes: the
  9:15, 9:35 and 9:40 five-minute bars, or fifteen 1-minute bars. The level
  to beat is the range high.
- **Level.** A price you give, like a 20-day high from `/trader-scanner`.
- **The trigger** is the first completed bar after the range that CLOSES
  above the level. A wick above is not a trigger. A close exactly at the
  level is not above it.
- **The time** is that bar's close time (the 10:10 bar closes at 10:15 AM).
  The price is its close, to the cent.
- **Only closed bars count.** A bar is closed when the clock passes its start
  plus 5 (or 1) minutes and Kite's last trade is at or past that moment (it
  waits a minute at most). The candle still forming is never a bar.
- **Volume (on by default).** The trigger bar's volume must be at least 1.5
  times the range bars' average (config `min_volume_x`; `--min-volume-x 0`
  turns it off).
- **VWAP (on by default).** The trigger bar must also close above the session
  VWAP, (H+L+C)/3 weighted by volume from 9:15 (config `require_above_vwap`;
  `--no-vwap` turns it off). The 9:15 bar holds the opening auction, so the
  volume bar is high: on Nov 11 1.5x left 8 of the Nifty 50 triggering.
- **Time limits.** No entries before 9:30 or after 14:45 (config), so
  there is time for the trade to work before the 15:15 flat-by and the broker's
  MIS square-off.
- **What it says.** Waiting (and how far the last close is from the level),
  triggered, expired, or no data. Before 9:15: not open yet. Before 9:45: the
  range is still forming. Weekends and NSE holidays: the market is closed.

## Steps

1. Get the symbol from the user, or run `entry.py watchlist` after
   `/trader-scanner`. Check `/trader-news` for it first.
2. Run `entry.py check SYMBOL`. Tell the user the status in one line:
   waiting and how far, triggered at what time and price, or expired.
3. To wait for it, Claude can run `entry.py watch SYMBOL --until 15:30` in
   the background, or `entry.py check SYMBOL --write` on a `/loop` every 5
   minutes. `watch` wakes just after each bar closes, prints one line per
   bar, and exits 0 on the trigger (it writes `trader/entry.json`) or 1 when
   the setup expires. Keep the computer awake while it watches: a sleeping
   laptop pauses it, and it catches up, late, when it wakes.
4. When it triggers, say the time and the price, and that nothing was placed.
   If the trigger is more than a bar old, say that too: a late entry is a
   different trade.
5. Hand off to `/trader-exit` to set the stop and the target BEFORE anything
   is bought.

## Example

Adani Enterprises (ADANIENT), Wednesday 2026-11-11, 5-minute bars, real data.
The opening range (9:15 to 9:30) is 2,728.80 to 2,746.70. At 10:17 the 10:15
bar is still open, so it waits:

```
WAITING  last close ₹2737.90 (10:10 bar), ₹8.80 under the range high ₹2746.70 (-0.32%).
The 10:15 bar is still forming (₹2768.50 so far, closes 10:20 AM); it does not count until it closes.
```

At 10:20 the bar has closed:

```
ADANIENT  Wed 2026-11-11 10:20 IST  opening-range breakout (15-minute range) on 5-minute Kite candles
Opening range 9:15-9:30: high ₹2746.70, low ₹2728.80 (3 of 3 bars).
TRIGGERED 10:20 AM at ₹2768.50: the 10:15 bar (bar 12) closed above the range high ₹2746.70 (+0.79%).
This is the moment to get in, if you take the trade. Nothing was placed.
Next: set the stop and target BEFORE you buy: /trader-exit (exit.py plan --from trader/entry.json).
```

With `--min-volume-x 1.5` the same bar still passes: it traded 2.44x the
average volume of the opening-range bars. Checked again 36 minutes later, the
tool says the trigger is stale ("a late entry is a different trade").

## Rules

- Bar by bar, on Zerodha Kite 1-minute or 5-minute candles. A candle counts
  once it has closed. Say so when a minute matters.
- A signal is information, not an order, and not a promise. Nothing here
  promises winning trades or money.
- Long side only. v2 plans longs only. If the user wants a short, say that.
- One trigger per setup per day: the first close above. Later closes do not
  reset it.
- Never call an entry early because a bar looks like it will close above.

## What it never does

It never places an order, never sizes a position, never sets the stop (that
is `/trader-exit`), never moves the level after the fact, and never counts a
bar that has not closed. It never runs `/trader-broker` from a background
watch: nobody is there to say yes.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
