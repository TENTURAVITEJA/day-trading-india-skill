---
name: trader-news
description: >-
  News Monitor for Indian stocks (NSE/BSE). Tracks breaking news on every
  stock you watch and flags what moves it: Indian business headlines per
  ticker (Google News India edition: Economic Times, Moneycontrol, Mint,
  Business Standard, CNBC-TV18, BusinessLine...), each company's NSE
  corporate announcements (results, board meetings, dividends, bonus/split,
  acquisitions, order wins, credit ratings, management changes, SEBI orders),
  the Economic Times markets feed (Nifty, Sensex, RBI, FII/DII), and how far
  each stock is from yesterday's close in rupees. Shows only what is new since
  the last check, newest first, in IST, tags each item (results, analyst,
  regulatory, deal, capital, promoter, circuit, macro...) and never repeats
  one. Use when the user says "any news on Reliance", "why is this stock
  moving", "what's the news on my watchlist", "morning briefing", "pre-market
  news", "monitor the news", "did anything break", "check NSE announcements",
  or after /trader-scanner finds setups.
argument-hint: "[NSE symbols]"
---

# trader-news

A chart can look perfect and still be about to gap on a results date, a SEBI
order, a block deal or a promoter pledge. Before a setup becomes a trade,
this skill reads Indian sources only, for every stock on the watchlist, and
tells you only what is new. It also prints how far each stock has moved today,
so a headline and a move sit side by side.

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/news.py"                               # watchlist + plan + portfolio
python3 "${CLAUDE_SKILL_DIR}/news.py" --tickers RELIANCE TCS --hours 12
python3 "${CLAUDE_SKILL_DIR}/news.py" --market                      # plus ET markets: Nifty, RBI, FII/DII
python3 "${CLAUDE_SKILL_DIR}/news.py" --no-filings --json
```

## What it reads and writes

| file / feed | what |
| --- | --- |
| `--tickers`, else `trader/watchlist.json` | the scanner's hits (trader-scanner) |
| `trader/plan.json` | the trade you are planning (trader-exit) |
| `trader/portfolio.json` | what you hold (trader-broker records fills there) |
| `trader/universe.txt` | company names for better headline searches (trader-scanner universe) |
| Google News, India edition | `news.google.com/rss/search?q=<Company> share when:2d&hl=en-IN&gl=IN&ceid=IN:en`; a headline is kept only if it names the company or its symbol |
| NSE corporate announcements | NSE's official RSS feed, `nsearchives.nseindia.com/content/RSS/Online_announcements.xml`: market-wide, roughly the last day of filings, matched to your stocks by company name |
| Economic Times markets RSS | with `--market` |
| Zerodha Kite `/quote` (via `trader-broker/kite.py`) | the last price against yesterday's close, the whole watchlist in one request |
| `trader/state/news.json` | what has been shown (kept 30 days) |

## The rules

- **Use Indian sources for Indian stocks.** If you search the web yourself to
  read a story, prefer the exchange filing (nseindia.com / bseindia.com) and
  Indian business press (Economic Times, Moneycontrol, Mint, Business Standard,
  BusinessLine, CNBC-TV18, NDTV Profit). Do not pull US tickers or US news
  into an Indian stock's brief unless it is the direct cause (for example a
  US client of an IT company).
- **Never disguise the tool as a browser** or work around a site that refuses
  scripts (NSE's main site does). Use the official feeds, or tell the user to
  open the page.
- **Headlines are leads, not facts.** Open the story or the NSE filing and read
  it before telling the user something happened. Quote the source and link it.
  Never summarise a paywalled story you could not read: say so.
- **Filings beat headlines.** If an NSE announcement and a headline disagree,
  the filing is what the company told the exchange.
- **Say what it touches.** Every item lists the tickers it touches and its tags.
  Filings marked `!` are the ones that move a stock the same day: financial
  results, outcome of board meeting, dividend / bonus / split / buyback,
  acquisition or merger, resignation of MD/CEO/CFO, credit rating, pledge,
  open offer, QIP / preferential issue, order wins, SEBI orders, insolvency,
  and NSE's "clarification on price movement" letters.
- **India-specific red flags to call out:** a stock in the F&O ban period,
  under ASM/GSM surveillance, moved to trade-for-trade (BE series: no
  intraday), hitting its upper or lower circuit, results due today, or a
  record date for a dividend/bonus/split (the price adjusts on the ex-date).
- **MOVING** marks a stock 3 % or more from yesterday's close (`--move-pct`).
  A move with no headline is worth saying out loud too.
- **No predictions.** Report what was published and how far the price is from
  yesterday's close. Never say what the stock will do next.

## Steps

1. Run the check: `python3 "${CLAUDE_SKILL_DIR}/news.py"`. Exit code 1 means
   nothing new. `--all` shows items already seen, `--json` gives everything
   machine readable, `--dry-run` leaves the state alone. The NSE feed only holds
   about a day of filings: for anything older, open the stock's page on
   nseindia.com (its filing pages refuse scripts, so read them by hand).
2. Read the items that matter: every `!` filing, anything tagged results,
   regulatory, promoter, circuit or risk, and every MOVING ticker first.
3. Write a short brief, per ticker: what happened, the source and link, the
   move in rupees and percent, and one line on why it matters to the setup
   (for example "results are tomorrow after market hours: a day trade is flat
   by 3:15 PM, so it is not exposed").
4. If a story changes the picture, say so before `/trader-entry` and
   `/trader-exit`. Never act on a headline alone.

During the session: `/loop 15m /trader-news`, or a scheduled task each
morning before 9:00 AM IST (pre-open runs 9:00 to 9:08).

## What it never does

- Never invents or embellishes a story, a quote or a number.
- Never trades on news, and never tells the user to buy or sell because of a headline or a "tip".
- Never treats Telegram/WhatsApp tips or social-media "buy calls" as news (SEBI warns about these).
- Never hammers a server: one request per feed per ticker, with a pause between calls.

## Safety

Education and tooling, not investment advice; this pack is not a
SEBI-registered investment adviser or research analyst. This skill only reads
and reports. Nothing places an order except `/trader-broker`, after
`/trader-risk` passes and the user types an explicit yes for that order.
