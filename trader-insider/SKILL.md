---
name: trader-insider
description: >-
  Big-Money Tracker for Indian stocks (replaces the original US Pelosi
  tracker). Reads NSE's published bulk-deal and block-deal files for the
  latest trading day: bulk deals (any client trading over 0.5 % of a
  company's shares in a day: big investors, funds, prop desks, promoters) and
  block deals (Rs 10 crore+ negotiated trades). Filters by stock, by client
  name (a well-known investor or fund) or by size in crore, flags deals in
  stocks you hold or watch, and shows each deal once. Use when the user says
  "who is buying", "bulk deals today", "block deals", "is any big investor
  buying X", "track <investor name>", "superstar investor moves", "promoter
  buying", "insider buying", or "run the insider tracker".
argument-hint: "[bulk|block|watch] [--symbol X] [--who NAME] [--min-cr N]"
---

# trader-insider

In India, the public record of who is moving big money in a stock is:

- **Bulk deals** (NSE, published every evening): any single client whose trades
  in a stock add up to more than 0.5 % of its listed shares that day.
- **Block deals** (NSE): single trades of at least Rs 10 crore in the special
  block-deal windows.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/insider.py" bulk                    # the latest day's bulk deals
python3 "${CLAUDE_SKILL_DIR}/insider.py" block
python3 "${CLAUDE_SKILL_DIR}/insider.py" bulk --symbol RELIANCE
python3 "${CLAUDE_SKILL_DIR}/insider.py" bulk --who "kacholia" --min-cr 5
python3 "${CLAUDE_SKILL_DIR}/insider.py" watch                   # both, new only, flags your stocks
```

Sources: `nsearchives.nseindia.com/content/equities/bulk.csv` and `block.csv`
(the latest trading day only; no history). `watch` remembers what it showed in
`trader/state/insider.json` and marks with `*` any stock in
`trader/portfolio.json`, `trader/watchlist.json` or `trader/plan.json`.

**Not covered: SEBI insider-trading (PIT) disclosures** by promoters and
directors, and pledges. NSE publishes those only on pages that refuse scripts
and its RSS feed for them is stale. Do not work around that. If the user asks
about promoter or director trades, say the tool cannot read them and point to
nseindia.com > Corporate Filings > Insider Trading (or the company's page).

## The rules

- **After the fact, and legal.** These are trades that already happened. A
  famous name buying is not a reason on its own.
- **Read the side and the size.** Say BUY or SELL, the quantity, the price and
  the value in crore.
- **Most bulk-deal names are trading firms** (prop desks, HFT, arbitrage). The
  same client buying and selling the same stock the same day is trading, not
  a view. Say so rather than calling it "big money buying".
- **SME and small-cap stocks** in bulk deals are thinly traded and easy to move.

## Steps

1. Run the command that fits the question.
2. Summarise: who, which stock, buy or sell, how much (Rs crore), and the date.
   Flag anything in the user's own stocks first.
3. Never tell the user to copy a trade. If they want to act on it, the normal
   pipeline applies: `/trader-news`, `/trader-entry`, `/trader-exit`, `/trader-risk`.

## Safety

Education and tooling, not investment advice. Public disclosures only.
