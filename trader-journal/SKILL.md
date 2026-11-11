---
name: trader-journal
description: >-
  Trade Journal. Records every signal, taken or skipped, with the checks that
  passed (trigger, reward:risk, Time Machine, risk check, news) and scores it
  afterwards in R after Zerodha's charges, from Kite 5-minute candles. Skipped
  signals are scored too, so you can see what a skip cost or saved. Reports
  taken vs skipped, your actual paper or live fills, each check's pass rate
  and rule breaks (a trade taken with a failed or missing check). Use when the
  user says "log this trade", "I took it", "I skipped it", "I passed on that
  one", "journal", "resolve today's trades", "how did today go", "how did my
  week go", "weekly review", "did I follow my rules", or after /trader-broker
  records a fill and after the 3:15 PM square-off.
---

# trader-journal

The journal is the memory of the paper test. A week of trades proves nothing
about an edge; what it does show is whether the rules were followed, and what
the skipped signals would have done. Log EVERY signal that triggers, not only
the trades taken.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/journal.py" log --taken --news clear
python3 "${CLAUDE_SKILL_DIR}/journal.py" log --skip "time machine leaned to the stop"
python3 "${CLAUDE_SKILL_DIR}/journal.py" log --skip "results today" --news flagged --symbol TCS
python3 "${CLAUDE_SKILL_DIR}/journal.py" resolve        # after flat_by (15:15)
python3 "${CLAUDE_SKILL_DIR}/journal.py" report         # or: report --since 2026-11-12
python3 "${CLAUDE_SKILL_DIR}/journal.py" list
```

Flags: `--dir trader` (the folder), `--now "2026-11-11 15:20"` (IST),
`--note "..."`, `resolve --all` (score everything again), `report --json`.

## What it reads and writes

| file | what |
| --- | --- |
| `trader/entry.json` | the trigger: symbol, date, time, price, opening range |
| `trader/plan.json` | stop, target, reward:risk, flat by (when trader-exit ran for this signal) |
| `trader/timemachine.json` | odds and average R after charges (when it ran for this plan) |
| `trader/state/risk.json` | PASS or BLOCK for this symbol today |
| `trader/state/broker.json` | your recorded fills (paper or live), for the actual result |
| `trader/config.json` | limits for the share count, `min_reward_risk`, `flat_by` |
| Zerodha Kite candles | `resolve` only: that day's 5-minute candles |
| `trader/journal.json` | written: one record per signal (date + symbol) |

A file about another symbol or another day is ignored, and that check shows
as "not run". Logging the same signal again replaces its record.

## The four checks

| check | passes when |
| --- | --- |
| trigger | `entry.json` says triggered |
| reward risk | the plan's reward:risk is at least `min_reward_risk` |
| time machine | average R after charges is above 0, and the history's too when it fired |
| risk | trader-risk said PASS for this symbol today |

News is what the user says (`--news clear` or `flagged`); the code does not
check it. A trade taken with any check failed or not run is a **rule break**
and is said so at once.

## Scoring ("by the rules")

`resolve` plays each plan forward on the day's 5-minute candles: the fill is
the next bar's open after the trigger bar closes (plus 0.02 % slippage), the
stop is checked before the target, a bar that opens under the stop sells at
that open, and anything still open sells at the close of the last bar by
`flat_by`. Shares come from the config's limits; charges are Zerodha's
intraday charges; R is net rupees over shares times the risk a share. A
skipped signal without a plan is scored with the default plan (opening-range
midpoint stop, `min_reward_risk` as the target R). For a taken trade the
recorded fills give the actual result next to it.

## Steps

1. When a signal triggers and the user decides: `log --taken` or
   `log --skip "why"`. Ask for the reason if they skip without one. Read the
   Checks line back, and the rule break if there is one.
2. After 3:15 PM: `resolve`. Say each result as printed. If a taken trade has
   no recorded fill, say so and offer to record it with trader-broker.
3. On "how did the week go": `report`. Say the counts, the two averages
   (taken and skipped), the actual net, the rule breaks, and the small-sample
   line exactly as printed.

## Rules

- Never call a few trades proof of anything. Under 30 scored signals the
  report says the sample is too small; repeat that.
- "By the rules" is a simulation with assumed fills, not what the account
  did. The recorded fills are what the account did. Keep the two apart.
- Skips count. A signal that triggered and was not logged is a hole in the
  record: when one comes up in the chat, offer to log it.
- Never place, size or check an order here.

## Safety

- Education and tooling, not investment advice; not a SEBI-registered adviser.
- Paper trading by default (`"paper": true`). Going live is the user's own decision, in their own config.
- Nothing in this pack places an order except trader-broker, with the user's explicit yes.
