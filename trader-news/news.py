#!/usr/bin/env python3
"""News monitor for trader-news (India: NSE / BSE). Standard library only.

Watches Indian headlines and NSE corporate announcements for the stocks you
watch or hold and prints only what is new since the last check, newest first,
with how far each stock has moved today and a flag on anything that touches
your tickers. All times in IST.

    python3 news.py                                  # watchlist + plan + portfolio tickers
    python3 news.py --tickers RELIANCE TCS HDFCBANK
    python3 news.py --tickers RELIANCE --all --hours 72  # everything in the last 3 days, seen or not
    python3 news.py --no-filings                     # headlines only
    python3 news.py --market                         # plus the Economic Times markets feed
    python3 news.py --no-moves                       # skip the price line per ticker

Your tickers, when --tickers is not given: every hit in trader/watchlist.json
(trader-scanner), the symbol in trader/plan.json (trader-exit) and the
positions in trader/portfolio.json. Company names come from
trader/universe.txt (trader-scanner universe) when it is there.

Sources (all Indian / India-edition):
  - Google News, India edition (Economic Times, Moneycontrol, Mint, Business
    Standard, CNBC-TV18, BusinessLine...), one search per ticker:
    https://news.google.com/rss/search?q=<Company>+NSE&hl=en-IN&gl=IN&ceid=IN:en
  - NSE corporate announcements, from NSE's official RSS feed (results,
    board meetings, dividends, bonus/split, acquisitions, credit ratings,
    management changes). The feed is market-wide and holds roughly the last
    day of filings; yours are picked out by company name:
    https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml
  - --market: Economic Times markets RSS (Nifty, Sensex, RBI, FII/DII flows):
    https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms
  - The move: Yahoo Finance's chart for SYMBOL.NS (unofficial, can lag), the
    last price against yesterday's close.

State: trader/state/news.json remembers what you have seen (30 days), so an
item shows once. Offline: --offline DIR reads gnews-<T>.xml, nse-announcements.xml,
et-markets.xml and chart-<T>.json from DIR.

Headlines are leads, not facts: open the story (or the NSE filing) before
acting, and treat nothing here as advice.
"""

import argparse
import datetime as _dt
import email.utils
import gzip
import html
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

GNEWS = "https://news.google.com/rss/search?q=%s&hl=en-IN&gl=IN&ceid=IN:en"
ET_MARKETS = "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"
NSE_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
CHART = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=5m"
BROWSER_UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
KEEP_DAYS = 30
MOVE_PCT = 3.0  # a stock this far from yesterday's close is flagged as moving

# NSE announcement subjects that move a stock the same day
IMPORTANT_NSE = re.compile(
    r"financial result|outcome of board meeting|bonus|split|sub-division|buy ?back|dividend|"
    r"acquisition|amalgamation|merger|scheme of arrangement|demerger|resignation|appointment of (md|ceo|cfo)|"
    r"credit rating|fraud|default|insolvency|ibc|nclt|search|raid|sebi order|penalt|"
    r"pledge|open offer|delisting|qip|preferential|rights issue|fund ?rais|order (win|received)|"
    r"bagging|award of order|clarification|price movement|spurt", re.I)
KEYWORDS = [
    ("results", r"\bresults?\b|\bQ[1-4]\b|\bprofit\b|\brevenue\b|\bEBITDA\b|\bmargin\b|\bguidance\b|\bearnings\b"),
    ("analyst", r"\bupgrade[sd]?\b|\bdowngrade[sd]?\b|target price|price target|\bbuy call\b|\bsell call\b|\boutperform\b|\bunderperform\b|brokerage"),
    ("regulatory", r"\bSEBI\b|\bRBI\b|\bCCI\b|\bNCLT\b|\bED\b raid|\bincome tax\b|\bGST\b notice|\bshow[- ]cause\b|\bTRAI\b|\bDGCA\b|\bUSFDA\b"),
    ("deal", r"\bacqui(?:re|res|red|sition)\b|\bmerger\b|\bdemerger\b|\bstake\b|\bopen offer\b|\bdeal\b|\bJV\b|\border (?:win|worth)\b|\bbags?\b"),
    ("capital", r"\bdividend\b|\bbuy ?back\b|\bbonus\b|\bstock split\b|\bQIP\b|\brights issue\b|\bOFS\b|\bIPO\b|\bfund ?rais"),
    ("promoter", r"\bpromoter\b|\bpledge\b|\binsider\b|\bbulk deal\b|\bblock deal\b"),
    ("people", r"\bMD\b|\bCEO\b|\bCFO\b|\bresign|\bsteps down\b|\bappoint"),
    ("risk", r"\bdefault\b|\binsolvency\b|\bfraud\b|\bdowngrade\b|\braid\b|\bprobe\b|\bplant shut|\bstrike\b|\bfire\b"),
    ("circuit", r"\bupper circuit\b|\blower circuit\b|\bF&O ban\b|\bban period\b|\bASM\b|\bGSM\b"),
    ("macro", r"\bRBI policy\b|\brepo rate\b|\bCPI\b|\binflation\b|\bGDP\b|\bFII\b|\bFPI\b|\bDII\b|\bUnion Budget\b|\brupee\b|\bcrude\b|\bmonsoon\b|\bNifty\b|\bSensex\b"),
]


class NewsError(Exception):
    pass


# ---------------------------------------------------------------- time

def ist_offset(utc=None):
    """Hours from UTC for India: always +5:30 (IST has no daylight saving)."""
    return 5.5


def to_ist(utc):
    return utc.replace(tzinfo=None) + _dt.timedelta(hours=5, minutes=30)


def ist_to_utc(local):
    return local.replace(tzinfo=None) - _dt.timedelta(hours=5, minutes=30)



def load_json(path):
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def watch_from_files(portfolio="trader/portfolio.json", watchlist="trader/watchlist.json",
                     plan="trader/plan.json"):
    """{ticker: name or None}: the scanner's hits, the planned trade and your holdings."""
    out = {}
    wl = load_json(watchlist) or {}
    for h in wl.get("hits", []):
        sym = h.get("symbol") or h.get("ticker")
        if sym:
            out[sym.upper()] = h.get("name")
    pl = load_json(plan) or {}
    if pl.get("symbol"):
        out.setdefault(pl["symbol"].upper(), pl.get("name"))
    pf = load_json(portfolio) or {}
    for p in pf.get("positions", []):
        sym = p.get("ticker") or p.get("symbol")
        if sym:
            out[sym.upper()] = p.get("name") or out.get(sym.upper())
    return out


def names_from_universe(path="trader/universe.txt"):
    out = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                if "|" in ln:
                    sym, name = ln.rstrip("\n").split("|", 1)
                    out[sym.strip().upper()] = name.strip()
    return out


# ---------------------------------------------------------------- fetching

class Fetcher(object):
    """Plain, honest requests: no cookies, no browser disguise. Sources that refuse
    scripts are simply reported as unavailable."""

    def __init__(self, offline=None, pause=0.5):
        self.offline = offline
        self.pause = pause
        self._last = 0.0

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise NewsError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        gap = time.time() - self._last
        if gap < self.pause:
            time.sleep(self.pause - gap)
        self._last = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as exc:
            raise NewsError("HTTP %s for %s" % (exc.code, url.split("?")[0]))
        except (urllib.error.URLError, OSError) as exc:
            raise NewsError("could not reach %s: %s" % (url.split("?")[0], getattr(exc, "reason", exc)))


def name_key(name):
    """'Reliance Industries Ltd.' -> 'reliance industries' (to match NSE feed titles)."""
    n = re.sub(r"[^a-z0-9& ]+", " ", html.unescape(str(name or "")).lower())
    n = re.sub(r"\b(limited|ltd|the)\b", " ", n)
    return " ".join(n.split())


def nse_names(fetcher, watch):
    """Fill missing company names from NSE's EQUITY_L.csv (one request, only if needed)."""
    if all(watch.get(t) for t in watch) or fetcher.offline:
        return
    try:
        text = fetcher.get(NSE_LIST, "EQUITY_L.csv").decode("utf-8", "replace")
    except NewsError:
        return
    for ln in text.splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) > 1 and parts[0].strip().upper() in watch and not watch[parts[0].strip().upper()]:
            watch[parts[0].strip().upper()] = parts[1].strip()


def yahoo_symbol(sym):
    """RELIANCE -> RELIANCE.NS; for BSE write RELIANCE.BO; .NS / .BO / ^index kept."""
    s = str(sym).strip().upper()
    if s.startswith("^") or s.endswith(".NS") or s.endswith(".BO"):
        return s
    return s + ".NS"


def relevant(title, ticker, name):
    """A headline is kept only if it names the company: the ticker (TCS), or the
    leading word(s) of its name with their capitals (Reliance, not "reliance on")."""
    keys = [ticker[:-3] if ticker.endswith((".NS", ".BO")) else ticker]
    words = re.sub(r"\b(Limited|Ltd\.?|The)\b", " ", html.unescape(name or "")).split()
    if words:
        if len(words[0]) >= 4:
            keys.append(words[0])
        if len(words) > 1:
            keys.append(" ".join(words[:2]))
    return any(re.search(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(k), title) for k in keys if k)


def gnews_query(ticker, name):
    import urllib.parse
    base = re.sub(r"\b(limited|ltd\.?)\s*$", "", name or ticker, flags=re.I).strip()
    return urllib.parse.quote_plus('%s share when:2d' % base)


# ---------------------------------------------------------------- parsing

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _text(el, name):
    for c in el.iter():
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_time(value):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> aware UTC datetime, or None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        d = None
    if d is None:
        try:
            d = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def parse_rss(data, ticker):
    """Yahoo headline RSS -> items."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("RSS for %s is not valid XML: %s" % (ticker, exc))
    out = []
    for it in root.iter("item"):
        title = html.unescape(_text(it, "title"))
        link = _text(it, "link")
        guid = _text(it, "guid") or link
        src = html.unescape(_text(it, "source")) or "Google News"
        if src != "Google News" and title.endswith(" - " + src):
            title = title[:-len(src) - 3]
        out.append({"id": "rss:" + guid, "source": src[:22], "kind": "headline",
                    "ticker": ticker, "title": title, "summary": re.sub(r"<[^>]+>", " ", _text(it, "description")),
                    "link": link, "time": parse_time(_text(it, "pubDate"))})
    return out


def parse_nse_rss(data, watch):
    """NSE's official announcements RSS (market-wide, today's filings) -> items for
    the companies in `watch`, matched by company name. pubDate is IST."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise NewsError("NSE announcements feed is not valid XML: %s" % exc)
    by_name = {name_key(n): t for t, n in watch.items() if n}
    out = []
    for it in root.iter("item"):
        company = " ".join(_text(it, "title").split())
        t = by_name.get(name_key(company))
        if not t:
            continue
        desc = html.unescape(" ".join(_text(it, "description").split()))
        body, _, subject = desc.partition("|SUBJECT:")
        subject = subject.strip() or "announcement"
        raw, when = _text(it, "pubDate"), None
        for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M"):
            try:
                when = (_dt.datetime.strptime(raw, fmt) - _dt.timedelta(hours=5, minutes=30)).replace(
                    tzinfo=_dt.timezone.utc)
                break
            except ValueError:
                continue
        link = _text(it, "link")
        out.append({"id": "nse:" + (link or (company + raw)), "source": "NSE filing", "kind": "filing",
                    "form": subject, "ticker": t, "title": "%s (NSE filing): %s" % (company, subject),
                    "summary": body.strip()[:300], "important": bool(IMPORTANT_NSE.search(desc)),
                    "link": link, "time": when})
    return out


def parse_chart(data, ticker):
    """Yahoo chart JSON -> {"ticker", "price", "prev_close", "pct", "time"} or raise."""
    try:
        doc = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)
        meta = doc["chart"]["result"][0]["meta"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise NewsError("chart for %s is not usable" % ticker)
    price = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not price or not prev:
        raise NewsError("chart for %s has no price" % ticker)
    when = meta.get("regularMarketTime")
    return {"ticker": ticker, "price": round(float(price), 2), "prev_close": round(float(prev), 2),
            "pct": round((float(price) / float(prev) - 1) * 100, 2),
            "time": _dt.datetime.fromtimestamp(when, _dt.timezone.utc) if when else None}


def collect_moves(fetcher, watch, errors=None):
    errors = [] if errors is None else errors
    out = []
    for t in sorted(watch):
        name = "chart-%s.json" % t
        try:
            try:
                data = fetcher.get(CHART % ("query2", yahoo_symbol(t)), name)
            except NewsError as exc:
                if fetcher.offline or not re.search(r"HTTP (429|5\d\d)", str(exc)):
                    raise
                data = fetcher.get(CHART % ("query1", yahoo_symbol(t)), name)
            out.append(parse_chart(data, t))
        except NewsError as exc:
            if not (fetcher.offline and "no file" in str(exc)):
                errors.append("price for %s: %s" % (t, exc))
    return out


def mention_patterns(watch):
    pats = {}
    for t, name in watch.items():
        alts = [r"\(%s\)" % re.escape(t), r"\b(?:NSE|BSE)\s*:\s*%s\b" % re.escape(t),
                r"\b%s\b" % re.escape(t)]
        if name and len(name) >= 4:
            alts.append(r"\b%s\b" % re.escape(name).replace(r"\ ", r"[\s-]"))
        pats[t] = re.compile("|".join(alts))
    return pats


def tag(items, watch):
    """Which of your tickers each item touches, and why it matters."""
    pats = mention_patterns(watch)
    for it in items:
        hay = "%s %s" % (it["title"], it.get("summary") or "")
        touches = {it["ticker"]} if it["ticker"] in watch else set()
        for t, rx in pats.items():
            if rx.search(hay):
                touches.add(t)
        it["touches"] = sorted(touches)
        it["tags"] = [label for label, rx in KEYWORDS if re.search(rx, hay, re.I)]
        if it["kind"] == "filing" and re.search(r"financial result", it.get("form", ""), re.I) \
                and "results" not in it["tags"]:
            it["tags"].insert(0, "results")
    return items


def dedupe(items):
    """One item per story: same id, or same link, or same title the same day."""
    seen, out = {}, []
    for it in items:
        keys = [it["id"], it["link"],
                "%s|%s" % (re.sub(r"\W+", " ", it["title"].lower()).strip(),
                           it["time"].date() if it["time"] else "")]
        hit = next((seen[k] for k in keys if k and k in seen), None)
        if hit is not None:
            for t in it.get("touches", []):
                if t not in hit["touches"]:
                    hit["touches"].append(t)
            hit["touches"].sort()
            continue
        for k in keys:
            if k:
                seen[k] = it
        out.append(it)
    return out


def fingerprint(it):
    return hashlib.sha1(it["id"].encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------- the check

def collect(fetcher, watch, state, filings=True, market=False, errors=None):
    errors = [] if errors is None else errors
    items = []
    nse_names(fetcher, watch)
    for t in sorted(watch):
        try:
            got = parse_rss(fetcher.get(GNEWS % gnews_query(t, watch.get(t)), "gnews-%s.xml" % t), t)
            items += [it for it in got if relevant(it["title"], t, watch.get(t))]
        except NewsError as exc:
            errors.append("headlines for %s: %s" % (t, exc))
    if filings:
        nse_names(fetcher, watch)
        unnamed = sorted(t for t in watch if not watch[t])
        if unnamed:
            errors.append("NSE filings skipped for %s: no company name (run trader-scanner universe)"
                          % ", ".join(unnamed))
        try:
            items += parse_nse_rss(fetcher.get(NSE_RSS, "nse-announcements.xml"), watch)
        except NewsError as exc:
            errors.append("NSE filings: %s" % exc)
    if market:
        try:
            kept = 0
            for it in parse_rss(fetcher.get(ET_MARKETS, "et-markets.xml"), "MARKET"):
                # ET's feed is padded with auto-generated pages: skip them, keep the 12 newest real stories
                if re.search(r"Share Price Live Updates|Stock Price Live|/slideshow/|/liveblog/",
                             it["title"] + " " + it["link"]):
                    continue
                it["source"] = "Economic Times"
                items.append(it)
                kept += 1
                if kept >= 12:
                    break
        except NewsError as exc:
            errors.append("ET markets feed: %s" % exc)
    return items


def select(items, state, now, hours=48, show_all=False):
    """Items within the window; only unseen ones unless show_all."""
    seen = state.setdefault("seen", {})
    cutoff = now - _dt.timedelta(hours=hours)
    ahead = now + _dt.timedelta(minutes=10)  # a pinned --now: nothing from after it
    out = []
    for it in items:
        if it["time"] is not None and (it["time"] < cutoff or it["time"] > ahead):
            continue
        fp = fingerprint(it)
        it["new"] = fp not in seen
        if it["new"] or show_all:
            out.append(it)
    out.sort(key=lambda i: (i["time"] or now), reverse=True)
    return out


def remember(items, state, now):
    seen = state.setdefault("seen", {})
    stamp = now.isoformat()
    for it in items:
        seen.setdefault(fingerprint(it), stamp)
    cutoff = (now - _dt.timedelta(days=KEEP_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_check"] = stamp


def render_moves(moves, move_pct=MOVE_PCT):
    out = []
    for m in moves:
        flag = "MOVING" if abs(m["pct"]) >= move_pct else ""
        when = (" at %s IST" % to_ist(m["time"]).strftime("%H:%M")) if m.get("time") else ""
        out.append("%-10s ₹%s %+.2f%% vs yesterday's close ₹%s%s  %s" % (
            m["ticker"], "{:,.2f}".format(m["price"]), m["pct"], "{:,.2f}".format(m["prev_close"]),
            when, flag))
    return out


def render(items, errors, now, moves=None, move_pct=MOVE_PCT):
    out = render_moves(moves or [], move_pct)
    if out:
        out.append("")
    if not items:
        out.append("Nothing new.")
    for it in items:
        when = to_ist(it["time"]).strftime("%Y-%m-%d %H:%M IST") if it["time"] else "time unknown"
        mark = "!" if it.get("important") else " "
        who = ",".join(it["touches"]) or it["ticker"]
        out.append("%s %s  %-11s %-15s %s" % (mark, when, who, it["source"], it["title"]))
        extra = []
        if it["tags"]:
            extra.append("tags: " + ", ".join(it["tags"]))
        extra.append(it["link"])
        out.append("      " + "  ".join(extra))
    for e in errors:
        out.append("note: %s" % e)
    out.append("Checked %s. Headlines are leads, not facts; open the story before acting. "
               "Not financial advice." % to_ist(now).strftime("%Y-%m-%d %H:%M IST"))
    return "\n".join(out) + "\n"


def _json_ready(items):
    out = []
    for it in items:
        d = dict(it)
        d["time"] = it["time"].isoformat() if it["time"] else None
        out.append(d)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tickers", nargs="*", help="NSE symbols (RELIANCE; RELIANCE.BO for the BSE listing); default: watchlist + plan + portfolio")
    ap.add_argument("--portfolio", default="trader/portfolio.json")
    ap.add_argument("--watchlist", default="trader/watchlist.json")
    ap.add_argument("--plan", default="trader/plan.json")
    ap.add_argument("--universe", default="trader/universe.txt", help="SYMBOL|Name lines, for company names")
    ap.add_argument("--state", default="trader/state/news.json")
    ap.add_argument("--hours", type=float, default=48, help="ignore items older than this")
    ap.add_argument("--all", action="store_true", help="show seen items too")
    ap.add_argument("--no-filings", action="store_true", help="headlines only, skip NSE announcements")
    ap.add_argument("--market", action="store_true", help="add the Economic Times markets feed")
    ap.add_argument("--no-moves", action="store_true", help="skip the price line per ticker")
    ap.add_argument("--move-pct", type=float, default=MOVE_PCT, help="flag a move this big (percent)")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--now", help="ISO time, for reproducible runs")
    ap.add_argument("--dry-run", action="store_true", help="do not update the state file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    known = watch_from_files(args.portfolio, args.watchlist, args.plan)
    if args.tickers:
        watch = {t.upper(): known.get(t.upper()) for t in args.tickers}
    else:
        watch = known
    if not watch:
        print("news: no tickers. Pass --tickers, or run /trader-scanner first (it writes "
              "trader/watchlist.json).", file=sys.stderr)
        return 2
    names = names_from_universe(args.universe)
    for t in watch:
        watch[t] = watch[t] or names.get(t)
    now = parse_time(args.now) if args.now else _dt.datetime.now(_dt.timezone.utc)
    state = load_json(args.state) or {}
    errors = []
    fetcher = Fetcher(offline=args.offline)
    items = collect(fetcher, watch, state, filings=not args.no_filings, market=args.market, errors=errors)
    items = dedupe(tag(items, watch))
    shown = select(items, state, now, hours=args.hours, show_all=args.all)
    moves = [] if args.no_moves else collect_moves(fetcher, watch, errors)
    if args.json:
        mv = [dict(m, time=m["time"].isoformat() if m.get("time") else None,
                   moving=abs(m["pct"]) >= args.move_pct) for m in moves]
        print(json.dumps({"checked": now.isoformat(), "moves": mv, "items": _json_ready(shown),
                          "notes": errors}, indent=2))
    else:
        sys.stdout.write(render(shown, errors, now, moves, args.move_pct))
    if not args.dry_run:
        remember(items, state, now)
        folder = os.path.dirname(args.state)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
            fh.write("\n")
    return 0 if any(i["new"] for i in shown) else 1


if __name__ == "__main__":
    sys.exit(main())
