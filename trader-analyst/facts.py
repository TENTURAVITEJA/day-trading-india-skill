#!/usr/bin/env python3
"""Earnings Analyst for trader-analyst (India: NSE / BSE). Standard library only, no keys.

Lines up a company's last reported quarters and says what changed: revenue,
operating profit, EBITDA, net profit, the margins and diluted EPS, each
quarter against the one before (QoQ) and, when the year-ago quarter is in
the data, against the same quarter a year earlier (YoY). Amounts in Rs crore.

    python3 facts.py RELIANCE
    python3 facts.py TCS INFY
    python3 facts.py RELIANCE --metrics trader/metrics.json
    python3 facts.py RELIANCE --json

Sources:
  - Yahoo Finance's fundamentals time series for SYMBOL.NS (or NAME.BO):
    about the last five reported quarters, consolidated, in INR. Free and
    unofficial: it can lag a fresh result by a day or two, and it sometimes
    skips a quarter (then YoY for that row is blank).
  - NSE's official board-meetings RSS feed, for a board meeting on results
    announced in the last day or so:
    https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml

NSE's own results pages (with the filed XBRL) refuse scripts, so this tool
does not read them. For the filed numbers, the notes and the segments, open
the results PDF at nseindia.com > Corporate Filings > Financial Results.
Banks and NBFCs: "revenue" here is Yahoo's total revenue; NIM and GNPA/NNPA
are in the results PDF only.

Reported numbers via a third party, not advice.
"""

import argparse
import datetime as _dt
import gzip
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
TS = ("https://%s.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/%s"
      "?symbol=%s&type=%s&period1=%d&period2=%d")
BOARD = "https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml"
NSE_LIST = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
FIELDS = {"revenue": "quarterlyTotalRevenue", "operating": "quarterlyOperatingIncome",
          "ebitda": "quarterlyEBITDA", "np": "quarterlyNetIncomeCommonStockholders",
          "eps": "quarterlyDilutedEPS"}
CRORE = 1e7


class AnalystError(Exception):
    pass


def yahoo_symbol(sym):
    s = str(sym).strip().upper()
    if s.endswith(".NS") or s.endswith(".BO"):
        return s
    return s + ".NS"


def get(url, offline=None, name=None):
    if offline:
        path = os.path.join(offline, name)
        if not os.path.isfile(path):
            raise AnalystError("offline: no file %s" % path)
        with open(path, "rb") as fh:
            return fh.read()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        raise AnalystError("HTTP %d for %s" % (e.code, url.split("?")[0]))
    except (urllib.error.URLError, OSError) as e:
        raise AnalystError("could not reach %s: %s" % (url.split("?")[0], getattr(e, "reason", e)))


def fetch_quarters(sym, offline=None):
    ysym = yahoo_symbol(sym)
    now = int(time.time())
    last = None
    for host in ("query2", "query1"):
        url = TS % (host, urllib.parse.quote(ysym), urllib.parse.quote(ysym), ",".join(FIELDS.values()),
                    now - 3 * 366 * 86400, now)
        try:
            return parse_quarters(get(url, offline, "timeseries-%s.json" % ysym), sym)
        except AnalystError as e:
            last = e
            if offline or not re.search(r"HTTP (429|5\d\d)", str(e)):
                raise
            time.sleep(2)
    raise last


def parse_quarters(data, sym=""):
    try:
        res = json.loads(data.decode("utf-8") if isinstance(data, bytes) else data)["timeseries"]["result"]
    except (ValueError, KeyError, TypeError):
        raise AnalystError("Yahoo returned something unexpected for %s" % sym)
    rev = {v: k for k, v in FIELDS.items()}
    by_date, currency = {}, None
    for r in res or []:
        typ = ((r.get("meta") or {}).get("type") or [None])[0]
        if typ not in rev:
            continue
        for x in r.get(typ) or []:
            if not x or (x.get("reportedValue") or {}).get("raw") is None:
                continue
            try:
                d = _dt.date.fromisoformat(x["asOfDate"])
            except (KeyError, ValueError):
                continue
            by_date.setdefault(d, {"to": d})[rev[typ]] = float(x["reportedValue"]["raw"])
            currency = x.get("currencyCode") or currency
    rows = [by_date[d] for d in sorted(by_date, reverse=True)]
    if not rows:
        raise AnalystError("Yahoo has no quarterly figures for %s (check the symbol)" % sym.upper())
    for r in rows:
        r["currency"] = currency or "INR"
    return rows


def growth(now, then):
    if now is None or then is None or then == 0:
        return None
    return 100.0 * (now - then) / abs(then)


def enrich(rows):
    by = {r["to"]: r for r in rows}
    for r in rows:
        ya = next((by[d] for d in by if abs((r["to"] - d).days - 365) <= 10), None)
        qa = next((by[d] for d in by if 80 <= (r["to"] - d).days <= 100), None)
        for k in ("revenue", "np"):
            r[k + "_yoy"] = growth(r.get(k), ya and ya.get(k))
            r[k + "_qoq"] = growth(r.get(k), qa and qa.get(k))
        if r.get("revenue"):
            for k, m in (("operating", "op_margin"), ("ebitda", "ebitda_margin"), ("np", "net_margin")):
                if r.get(k) is not None:
                    r[m] = 100.0 * r[k] / r["revenue"]
    return rows


def name_key(name):
    n = re.sub(r"[^a-z0-9& ]+", " ", html.unescape(str(name or "")).lower())
    n = re.sub(r"\b(limited|ltd|the)\b", " ", n)
    return " ".join(n.split())


def company_names(symbols, offline=None, universe="trader/universe.txt"):
    out = {}
    if os.path.isfile(universe):
        with open(universe, encoding="utf-8") as fh:
            for ln in fh:
                if "|" in ln:
                    s, n = ln.rstrip("\n").split("|", 1)
                    out[s.strip().upper()] = n.strip()
    if any(s not in out for s in symbols):
        try:
            for ln in get(NSE_LIST, offline, "EQUITY_L.csv").decode("utf-8", "replace").splitlines()[1:]:
                p = ln.split(",")
                if len(p) > 1:
                    out.setdefault(p[0].strip().upper(), p[1].strip())
        except AnalystError:
            pass
    return out


def board_meetings(offline=None):
    """NSE board-meetings RSS -> {name_key: (meeting date, purpose)} for results meetings."""
    out = {}
    try:
        root = ET.fromstring(get(BOARD, offline, "Board_Meetings.xml"))
    except (AnalystError, ET.ParseError):
        return None
    for it in root.iter("item"):
        title = " ".join((it.findtext("title") or "").split())
        desc = html.unescape(" ".join((it.findtext("description") or "").split()))
        m = re.search(r"Meeting Date:\s*(\d{2}-[A-Za-z]{3}-\d{4})", desc)
        if not m or not re.search(r"result", desc, re.I):
            continue
        try:
            d = _dt.datetime.strptime(m.group(1), "%d-%b-%Y").date()
        except ValueError:
            continue
        out[name_key(title)] = (d, desc.split("|")[0].strip())
    return out


def qlabel(d):
    fy = d.year + (1 if d.month > 3 else 0)
    return "Q%sFY%02d" % ({6: 1, 9: 2, 12: 3, 3: 4}.get(d.month, "?"), fy % 100)


def cr(x, unit=CRORE):
    return "" if x is None else "{:,.0f}".format(x / unit)


def pct(x, sign=True):
    return "" if x is None else (("%+.1f%%" if sign else "%.1f%%") % x)


def render(sym, rows, meeting, n):
    cur = rows[0].get("currency", "INR")
    unit = CRORE if cur == "INR" else 1e6
    out = ["%s, reported quarters (%s, consolidated, via Yahoo Finance; Indian FY runs April to March)"
           % (sym, "Rs crore" if cur == "INR" else "%s million" % cur)]
    if cur != "INR":
        out.append("note: Yahoo carries this company's figures in %s (it also reports in %s), not rupees; "
                   "growth and margins are unaffected, EPS is in %s." % (cur, cur, cur))
    out.append("%-8s %-10s %10s %8s %8s %10s %8s %8s %7s %7s %7s" % (
        "quarter", "ended", "revenue", "YoY", "QoQ", "net profit", "YoY", "QoQ", "OPM", "NPM", "EPS"))
    for r in rows[:n]:
        out.append("%-8s %-10s %10s %8s %8s %10s %8s %8s %7s %7s %7s" % (
            qlabel(r["to"]), r["to"].isoformat(), cr(r.get("revenue"), unit), pct(r.get("revenue_yoy")),
            pct(r.get("revenue_qoq")), cr(r.get("np"), unit), pct(r.get("np_yoy")), pct(r.get("np_qoq")),
            pct(r.get("op_margin"), False), pct(r.get("net_margin"), False),
            "" if r.get("eps") is None else "%.2f" % r["eps"]))
    ends = [r["to"] for r in rows]
    gaps = [qlabel(a) for a, b in zip(ends, ends[1:]) if (a - b).days > 100]
    if gaps:
        out.append("note: Yahoo is missing the quarter before %s, so some YoY / QoQ cells are blank."
                   % ", ".join(gaps))
    age = (_dt.date.today() - rows[0]["to"]).days
    if age > 135:
        out.append("note: the newest quarter here ended %d days ago; a newer result may be out. Check nseindia.com." % age)
    if meeting:
        out.append("Board meeting on results: %s (%s; NSE board-meetings feed)." % (meeting[0].isoformat(), meeting[1][:90]))
    elif meeting is None:
        out.append("Next results date: NSE's board-meetings feed could not be read.")
    else:
        out.append("Next results date: nothing for this company in NSE's board-meetings feed (it only holds the "
                   "last day or so of notices). Check the company's page on nseindia.com.")
    return "\n".join(out)


def jsonable(x):
    if isinstance(x, (_dt.date, _dt.datetime)):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    return x


def main(argv=None):
    ap = argparse.ArgumentParser(description="Earnings Analyst (trader-analyst): reported quarters for NSE/BSE companies.")
    ap.add_argument("tickers", nargs="+", help="NSE symbols (RELIANCE) or NAME.BO for BSE")
    ap.add_argument("--quarters", type=int, default=5)
    ap.add_argument("--metrics", metavar="FILE", help="merge a row per ticker into this JSON")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    syms = [t.upper().replace(".NS", "") for t in a.tickers]
    names = company_names(syms, a.offline)
    meetings = board_meetings(a.offline)
    results, failed = {}, 0
    for k, sym in enumerate(syms):
        if k and not a.offline:
            time.sleep(0.5)
        try:
            rows = enrich(fetch_quarters(sym, a.offline))
        except AnalystError as e:
            print("%s: %s" % (sym, e), file=sys.stderr)
            failed += 1
            continue
        meeting = None if meetings is None else (meetings.get(name_key(names.get(sym, ""))) or False)
        results[sym] = {"quarters": rows[:a.quarters], "board_meeting": meeting or None}
        if not a.json:
            print(render(sym, rows, meeting, a.quarters))
            print()
    if a.json:
        print(json.dumps(jsonable(results), indent=2))
    if a.metrics and results:
        doc = {}
        if os.path.isfile(a.metrics):
            with open(a.metrics, encoding="utf-8") as fh:
                doc = json.load(fh)
        for sym, r in results.items():
            q = r["quarters"][0]
            doc[sym] = jsonable({"currency": q.get("currency", "INR"), "latest_quarter": q["to"], "revenue_cr": q.get("revenue") and round(q["revenue"] / CRORE, 1),
                                 "net_profit_cr": q.get("np") and round(q["np"] / CRORE, 1),
                                 "revenue_yoy_pct": q.get("revenue_yoy"), "revenue_qoq_pct": q.get("revenue_qoq"),
                                 "np_yoy_pct": q.get("np_yoy"), "np_qoq_pct": q.get("np_qoq"),
                                 "op_margin_pct": q.get("op_margin"), "net_margin_pct": q.get("net_margin"),
                                 "eps": q.get("eps"), "board_meeting": r["board_meeting"], "source": "Yahoo Finance"})
        d = os.path.dirname(a.metrics)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.metrics, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
            fh.write("\n")
        if not a.json:
            print("Wrote %s." % a.metrics)
    if not a.json:
        print("Third-party data, not the filing itself and not advice. For the filed numbers open the results PDF on nseindia.com.")
    return 0 if results and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
