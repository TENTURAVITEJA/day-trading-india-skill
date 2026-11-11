# Day Trader Skill — India (NSE / BSE)

Eleven Claude skills for intraday trading on Indian markets: scan NSE charts,
track Indian news and NSE filings, time every entry and exit, replay every
outcome before risking a rupee, size in rupees, and paper trade by default.

Plain Python, standard library only, no API keys.

> **Status: in development.** We are building the product first. Trading is
> paper-only for now. See the roadmap below.

## Roadmap

| phase | trading | status |
| --- | --- | --- |
| 1. Product | Paper mode: tickets and simulated fills recorded locally, real market data | **now** |
| 2. Open-source engine | Plug in an open-source trading / paper-trading engine for execution and fills (to be chosen) | planned |
| 3. Zerodha | Live orders through Zerodha Kite Connect (own API key, self-hosted Kite MCP server) | later |

The broker layer is deliberately thin: `trader-budget` writes one order to
`trader/orders.json`, `trader-risk` passes or blocks it, and `trader-broker`
turns it into a ticket. Swapping the execution engine means changing only
`trader-broker`. The Zerodha ticket format is already there for phase 3 and
is never called in paper mode.

## Install

```bash
cp -r trader-* ~/.claude/skills/
```

Needs Python 3.8 or newer. Then, in your project:

```bash
mkdir -p trader && cp trader-budget/config.example.json trader/config.json   # set your budget and limits
```

On macOS, if network calls fail with `CERTIFICATE_VERIFY_FAILED`, your Python
has no root certificates: run `Install Certificates.command` from the Python
folder in Applications, or `export SSL_CERT_FILE=/etc/ssl/cert.pem`.

## The eleven

| command | job |
| --- | --- |
| `/trader-scanner` | Scans NSE charts: opening-range breakouts, flags, oversold bounces |
| `/trader-news` | Indian headlines, NSE announcements and rupee moves for your stocks |
| `/trader-entry` | Watches a setup bar by bar and tells you the moment to get in |
| `/trader-exit` | Sets the stop and the target before you enter |
| `/trader-timemachine` | Replays the rest of the session 10,000 times from the stock's own bars |
| `/trader-budget` | Sizes in whole shares to your budget and ₹ max loss, with charges |
| `/trader-risk` | Checks every order against your ₹ limits and NSE/BSE rules |
| `/trader-broker` | Order tickets, paper by default, never without your yes |
| `/trader-alerts` | Stop, target, flat-by 3:15 PM, NSE opening bell |
| `/trader-analyst` | Reported quarters: revenue, profit, margins, EPS, YoY / QoQ |
| `/trader-insider` | NSE bulk deals and block deals |

```
scanner -> news -> entry -> exit -> timemachine -> budget -> risk -> broker   (one trade)
alerts: stop, target, flat by 3:15 PM                                          (while it is open)
news / analyst / insider                                                        (on a schedule)
```

Everything lands in a `trader/` folder in your project, and each skill reads
what the last one wrote.

## Example (real data, 11 Nov 2026)

1. `/trader-scanner`: Adani Enterprises (ADANIENT) closes above its 15-minute
   opening range (2,728.80 to 2,746.70).
2. `/trader-entry`: triggered 10:20 AM at ₹2,768.50.
3. `/trader-exit`: stop ₹2,737.75 (range midpoint), target ₹2,830.00 (2R), flat by 3:15 PM.
4. `/trader-timemachine`: 10,000 replays from the stock's last 58 sessions:
   target first 5.4 %, stop first 43.0 %, neither 51.6 %. Verdict: strongly
   toward the stop.
5. `/trader-budget`: ₹1,00,000 budget, ₹500 max loss: 16 shares (₹44,296),
   max loss ₹492 plus about ₹47 of charges.
6. `/trader-risk`: PASS. `/trader-broker`: the ticket, then your yes; in paper
   mode a simulated fill is recorded.
7. `/trader-alerts`: watches the stop, the target and 3:15 PM.

## Tools

```bash
python3 trader-scanner/scan.py universe --list nifty500     # NSE stock list
python3 trader-scanner/scan.py daily                        # pre-market setups
python3 trader-scanner/scan.py orb                          # opening-range breakouts so far
python3 trader-news/news.py --tickers RELIANCE --market     # headlines, NSE filings, today's move
python3 trader-entry/entry.py check ADANIENT --write        # waiting / triggered / expired
python3 trader-exit/exit.py plan --stop or-mid --target 2R --write
python3 trader-timemachine/timemachine.py run --write
python3 trader-budget/size.py --write
python3 trader-risk/check.py
python3 trader-broker/ticket.py show
python3 trader-alerts/alerts.py add --from-plan
python3 trader-alerts/alerts.py check
python3 trader-analyst/facts.py RELIANCE
python3 trader-insider/insider.py bulk
```

## Data sources

| what | source |
| --- | --- |
| Prices, quotes, candles (1-min to daily) | Zerodha Kite Connect API through `trader-broker/kite.py` (log in once a day: `python3 trader-broker/kite.py login`). `SYMBOL` = NSE, `SYMBOL.BO` = BSE |
| Quarterly figures (trader-analyst) | Yahoo Finance fundamentals: Kite has no fundamentals API |
| Stock lists, announcements, board meetings, bulk / block deals | NSE's published files and RSS feeds (`nsearchives.nseindia.com`) |
| Headlines | Google News India edition, Economic Times markets feed |

NSE's main website refuses scripts, so the tools do not read it: no filed
results XBRL and no insider (SEBI PIT) disclosures.

## Known limits

- **Paper only for now.** No live execution engine is wired up yet.
- **Market hours**: NSE/BSE 9:15 AM to 3:30 PM IST. Only the 2026 trading
  holidays are built in; add 2027 to `NSE_HOLIDAYS` in each tool when NSE
  publishes them.
- **Kite session**: the access token ends around 6 AM IST; every price tool
  stops with a plain "run kite.py login" until you log in again.
- **Kite rate limit**: about 3 historical requests a second, one symbol per
  request, so a Nifty 500 scan takes about 3 minutes and the whole NSE list
  about 12.
- **The Time Machine's session is 9:15 to 15:15** (the flat-by time).
- **Tick sizes**: stops and targets are rounded to the paisa, not to NSE's
  tick size.
- **BSE**: `SYMBOL.BO` works for prices and charts; the stock list, filings
  and deals are NSE only.
- **Long side only**, cash-market equities only: no F&O, no short selling.
- **No automated test suite yet.**

## Fine print

- **Not investment advice.** This is software that reads public data and does
  arithmetic. It is not a SEBI-registered investment adviser or research
  analyst. Most individual intraday traders lose money.
- **No trade without your yes.** Only `/trader-broker` can lead to an order,
  and only after `/trader-risk` passes that exact order and you type yes.
- **A stop is a plan, not a guarantee.** In a fast market or at a circuit
  limit it can fill well past its price, or not at all.
- **The Time Machine is a simulation, not a prediction.**
- **Taxes**: intraday equity profit is speculative business income in India;
  nothing here is tax advice.

## License

MIT. See `LICENSE`.
