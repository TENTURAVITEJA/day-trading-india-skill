---
name: trader-timemachine
description: >-
  Time Machine. Replays every possible outcome of a trade before you risk a
  rupee: 10,000 simulated sessions plus every past time the setup fired.
  Takes the plan from trader-exit (entry, stop, target), rebuilds the rest of
  the day 10,000 times from the stock's own 5-minute bars over the last 60
  days, and re-runs the same rule on every past session. Prints the odds of
  target first, stop first, or neither by the close, where the price sits at
  the close, the average R, the history, and one honest verdict, then hands
  off to /trader-budget. Use when the user says "run the time machine",
  "replay this trade", "what are the odds", "simulate this trade", "monte
  carlo", "backtest this setup", "how often does this hit the target", "is
  this a coin flip", or after /trader-exit writes a plan, before
  /trader-budget.
argument-hint: "[path to plan.json, or TICKER]"
---

# trader-timemachine

The Time Machine sits between the plan and the money. trader-exit has set
the entry, the stop and the target. Before anything is sized or sent, this
skill asks one question: if today plays out like the stock's own last 60
days, how often does this plan hit the target first, the stop first, or
neither? It answers with 10,000 replays and with the real record of the same
rule on every past session. It is a simulation built from the stock's own
recent bars, not a prediction. Day trading loses most people money, and a
good-looking replay does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run                    # trader/plan.json, 10,000 replays, seed 19
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --write            # also save trader/timemachine.json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --until 15:15      # end each replay at your flat-by time
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --json
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" run --symbol ADANIENT --date 2026-11-11 \
    --entry 2768.50 --stop 2737.75 --target 2830.00 --entry-time "10:20 AM"
python3 "${CLAUDE_SKILL_DIR}/timemachine.py" chart --from           # ASCII fan chart of the saved run
```

Other flags: `--replays N`, `--seed N` (default 19), `--window N` (default 2
bars), `--offline FILE` (saved Kite candles `{"candles": [...]}`, `.json` or `.json.gz`),
`--now "2026-11-11 10:20"` (IST) to pin the clock.

## The backtest (the whole rule set, day by day)

`backtest.py` answers a different question from a replay: not "this one trade"
but "would these rules have made money". It runs the live code (trader-entry's
trigger with the volume and VWAP checks, trader-exit's stop and target on the
tick, the config's sizing, one trade a day, Zerodha's charges) over cached Kite
history for every symbol in `trader/universe.txt`.

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" fetch --days 730     # once: candles -> trader/cache/ (needs a Kite session)
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run                  # the rules in trader/config.json
python3 "${CLAUDE_SKILL_DIR}/backtest.py" run --stop or-low --daily-setup --max-rsi 75
python3 "${CLAUDE_SKILL_DIR}/backtest.py" grid                 # 48 variants, each split into two halves
```

It takes the earliest trigger of the day, fills at the next bar's open plus
slippage, checks the stop before the target, sells at `flat_by`, and reports
trades, win rate, average R after charges with its standard error and t,
net rupees, the worst drawdown and both halves. Say the verdict line as it is
printed. A negative backtest means do not trade those rules live; a positive
one is still the past with assumed fills. When a grid was run, say how many
variants were tried: the best of many is partly luck.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/plan.json` | the plan from trader-exit: symbol, date, entry, entry_time / entry_bar, stop, stop_rule, target, target_r, opening range |
| Zerodha Kite candles | one historical request: 60 days of 5-minute candles |
| `trader/timemachine.json` | with `--write`: `{plan, mc, history}` plus the verdict. `mc` holds the odds, percentiles, the fan-chart bands, 160 sample paths and a 24-bin histogram of the close |

## How it works

1. **The pool.** It keeps only FULL sessions before the plan's date: all 78
   five-minute bars, 9:15 to 4:00 IST. Half days, gaps and today
   are left out. Every bar becomes four ratios to the bar before it:
   open, high, low and close over the previous close.
2. **One replay.** Start at the entry price. For each bar left in the day,
   pick a past bar from the same time of day (plus or minus 2 bars) at
   random, and scale it to the current price. Bar by bar, it checks the stop
   first, then the target. A bar that touches both counts as the stop.
3. **10,000 replays** give four odds. Target first. Stop first. Neither,
   which means the trade was still open at `--until` (default 3:15 PM, the
   flat-by time). Green, which means target
   first or above the entry at `--until`.
   R is +1 at a 1R target, -1 at the stop, and otherwise the distance from
   the entry at `--until` divided by the risk. A gap through the stop still
   counts as -1R here. A real fill can be worse.
   **Charges come off.** Zerodha's intraday round-trip charges (brokerage,
   STT, exchange, SEBI, stamp, GST) for the share count your config allows
   (`--qty` overrides) are turned into R and taken off every outcome. Average
   R, the history's average R and the verdict's break-even share are all
   after charges. With small orders they can cost 0.2R a trade: say so.
4. **The history.** The plan's own rule (the same opening range, stop rule
   and target R) runs again on every one of those past sessions. That is
   what really happened, not a simulation.
5. **The verdict** is one line from the numbers. It looks only at the
   replays that hit one side, and asks how often the target came first. A
   bigger target needs to win less often to break even: 1 in (1 + R). That
   is 50 % at 1R, 33 % at 2R, 25 % at 3R. Less than 5 points from break-even:
   "Close to a coin flip" at 1R, "Close to break-even" at any other R. 5 to
   15 points: "Leans toward the target" or "Leans toward the stop". 15 or
   more: "Strongly toward" one of them. So a 2R plan with target first
   24.1 % and stop first 50.5 % is close to break-even (it needs 33 % of
   the 74.6 % that hit a side, and got 32 %), not a loser.
6. **Below the verdict**, when it applies: a line when more than 40 % of the
   replays hit neither (the price at `--until` decides those), and the
   average R when its sign goes against the lean. Those can only point the
   other way because of the replays that hit neither.

**The seed.** The draws come from Python's `random.Random(seed)`, seed 19 by
default. Same plan, same data, same seed: the same numbers, on any machine.
Say the seed with every result so the user can repeat it.

**5-minute bars only.** The replay pool is 60 days of 5-minute sessions;
1-minute bars would make a different, noisier pool. It refuses, in one
plain line, when there are fewer than 20 full sessions, when the entry bar is
not in the data (wrong date, not closed yet, a future day), or when the plan
does not make sense (a stop above a buy's entry, an entry off the 5-minute
grid). A live in-progress bar is never used.

## Steps

1. Make sure `trader/plan.json` is today's plan from `/trader-exit`. If there
   is none, run that first, or pass the plan as flags.
2. Run `timemachine.py run --write`. Add `--until 15:15` when the plan has a
   flat-by time, so "neither" means still open at the time you would get out.
3. Tell the user, in this order: the four odds, the median and the 5 to 95
   range of the price at the close, the average R, the history line, then the
   verdict line and any line under it exactly as printed. Say the seed.
4. If the verdict is a coin flip or close to break-even, say so plainly. Do
   not dress it up. If the history and the replays disagree, say that too.
5. Say once that this is built from the last 60 days and is not a prediction.
   Earnings, news and halts can do things those 60 days never did. Check
   `/trader-news` for the stock before going on.
6. If the user still wants the trade, hand off to `/trader-budget` to size it.

## Example

ADANIENT, Wednesday 2026-11-11 (real data). The 15-minute opening range is
2,728.80 to 2,746.70. The 10:15 bar closes above it: entry 2,768.50 at 10:20 AM
(bar 12), stop 2,737.75 (the range midpoint), target 2,830.00 (2R, risk 30.75
a share).

```
10,000 replays of the rest of the session, 10:20 AM to 3:15 PM (59 five-minute bars), seed 19.
Each bar is drawn whole from ADANIENT's own 5-min bars at the same time of day (+-2 bars) over its last 58 full sessions (2026-08-14 to 2026-11-09).
  target first   5.4 %
  stop first     43.0 %
  neither        51.6 %   still open at 3:15 PM
  green          40.1 %   target first, or above the entry at 3:15 PM
  Price at 3:15 PM: median 2763.29, 90 % of replays between 2703.85 and 2821.67.
  Average R: -0.141
History: the same rule on each of those 58 real sessions fired 33 times: 6 target, 16 stop, 11 neither. Average R +0.096.
Strongly toward the stop: at 2R the target has to come first in 33 % of the replays that hit one side; it did in 11 %.
```

At 2R the target has to come first in a third of the replays that hit one
side; it did in about one in nine. The stop sits only 1.1 % under the entry,
inside the stock's normal 5-minute noise, while the target is 2.2 % away.
That is the honest answer for this plan, and it is what the user should hear
before `/trader-budget` sizes 16 shares. (The real history, 6 targets to 16
stops, is small and should not be read as an edge.)

## Rules

- Every number comes from the run. Never round a coin flip up into an edge.
- The verdict line and the lines under it are printed by the tool. Repeat
  them, do not rewrite them. Never judge a 2R or 3R plan by target first vs
  stop first alone: the verdict already weighs the target's R.
- Simulated odds are not predictions and not promises. Say it once, plainly.
- Data is Zerodha Kite's 5-minute candles. Bars are 5-minute bars, checked
  bar by bar, never tick by tick and never real time.
- Always say the seed, so any run can be repeated.
- One plan per run. A changed entry, stop, target or date is a new run.

## What it never does

It never places an order, never sizes a position, never changes the plan,
and never calls a result a sure thing. It never prints a P&L or a profit.
It never uses a bar that has not closed yet, and never mixes today's bars
into the pool.

## Safety

- Education and tooling, not financial advice. A PASS is a rules check, not a recommendation.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, and only when all three hold: trader-risk
  passed that exact order today, the user typed an explicit yes for that one order in the chat, and the
  user clicked Allow once on Claude's own card because the order tool is set to "Needs approval"
  (Customize > Connectors > tool permissions). Never set an order tool to "Always allow".
- No options, margin, short selling or leverage unless the config allows it and the user confirms.
- Broker keys live in environment variables or the Claude Desktop config, never in the chat.
