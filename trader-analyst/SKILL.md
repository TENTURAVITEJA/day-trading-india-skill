---
name: trader-analyst
description: >-
  The Earnings Analyst for Indian companies (NSE/BSE). Lines up a company's
  last reported quarters (about five, consolidated, in rupees crore) and sums
  up what changed: revenue, net profit, operating and net margins and diluted
  EPS, quarter on quarter and, where the year-ago quarter is available, year
  on year, using the Indian fiscal year (Q1 = Apr-Jun). Checks NSE's official
  board-meetings feed for a results meeting announced in the last day. Reads
  a results PDF, concall transcript or investor presentation when you paste
  one. Use when the user says "how were Reliance's results", "analyze Q2",
  "what changed this quarter", "is revenue growing", "compare margins", "sum
  up the concall", "when are the results", or before trading a stock around
  its results.
argument-hint: "[NSE symbols]"
---

# trader-analyst

Quarterly results move Indian stocks more than anything else on the calendar.
This skill lines the last quarters up and says plainly what changed. For a day
trader it answers two questions before a trade: what did the last results say,
and is a results board meeting inside this trade?

Tool in this folder (standard library Python, no keys):

```bash
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE
python3 "${CLAUDE_SKILL_DIR}/facts.py" TCS INFY --json
python3 "${CLAUDE_SKILL_DIR}/facts.py" RELIANCE --metrics trader/metrics.json
```

## What it reads, and its limits

- **Numbers: Yahoo Finance's fundamentals time series** for `SYMBOL.NS`. About
  the last five reported quarters, consolidated, in INR. This is third-party
  data, not the filing: it can lag a fresh result by a day or two, it
  sometimes skips a quarter (then YoY is blank and the tool says so), and for
  some companies (several banks) it is badly out of date (the tool warns when
  the newest quarter is older than about 135 days).
- **Results date: NSE's official board-meetings RSS feed.** It only holds
  about the last day of notices, so most of the time it has nothing for a
  given company. Then say the date is unknown and point to the company's page
  on nseindia.com; never guess a date.
- **NSE's own results pages (the filed XBRL) refuse scripts.** Do not try to
  work around that. When the filed numbers matter (exceptional items,
  segments, a bank's NIM and GNPA/NNPA), ask the user to open the results PDF
  from nseindia.com > Corporate Filings > Financial Results and paste it or
  share the file.
- Writes `trader/metrics.json` with `--metrics`.

## The rules

- **Say where each number came from** (Yahoo, or a PDF the user shared) and how old it is.
- **Compare like with like.** YoY against the same quarter last year when it
  is there; QoQ is shown, but many Indian businesses are seasonal (Q3, Oct-Dec,
  is festive season for consumer companies).
- **Indian fiscal year.** April to March: Q1 = Apr-Jun, Q2 = Jul-Sep,
  Q3 = Oct-Dec, Q4 = Jan-Mar. Q2FY27 ends 30 Sep 2026.
- **Banks and NBFCs**: revenue here is Yahoo's total revenue; what matters is
  NIM and asset quality, which are only in the results PDF.
- **Results date inside the trade.** A day trade is flat by 3:15 PM IST. If the
  board meets today, say the stock can jump at any minute.

## Steps

1. Run `facts.py` for the tickers. Relay any `note:` lines.
2. Sum up in a few lines per company: revenue and profit direction, margins,
   EPS, how fresh the data is, and the results date if known.
3. If the user is about to trade it, connect the dots.

## Safety

Education and tooling, not investment advice; this pack is not a
SEBI-registered research analyst. Third-party numbers can be wrong or stale;
check the filing on nseindia.com before acting.
