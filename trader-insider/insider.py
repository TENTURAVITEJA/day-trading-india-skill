#!/usr/bin/env python3
"""Big-Money Tracker for trader-insider (India: NSE). Standard library only.

India's public "who is buying" record, from NSE's published files:

  bulk     NSE bulk deals: any client trading more than 0.5 % of a company's
           shares in one day (big investors, funds and promoters show up here).
           https://nsearchives.nseindia.com/content/equities/bulk.csv
  block    NSE block deals: negotiated trades of Rs 10 crore or more in the
           block window. https://nsearchives.nseindia.com/content/equities/block.csv

Both files hold the latest trading day and are published in the evening.

    python3 insider.py bulk                        # the latest day's bulk deals
    python3 insider.py block
    python3 insider.py bulk --symbol RELIANCE
    python3 insider.py bulk --who "jhunjhunwala"   # filter by client name
    python3 insider.py bulk --min-cr 10            # only deals of Rs 10 crore or more
    python3 insider.py watch                       # both, only what is new, flags your stocks
    python3 insider.py bulk --offline DIR          # reads DIR/bulk.csv etc.

SEBI insider-trading (PIT) disclosures by promoters and directors are NOT read:
NSE publishes them only on pages that refuse scripts, and its RSS feed for
them is stale. Read them by hand at
https://www.nseindia.com/companies-listing/corporate-filings-insider-trading

`watch` remembers what it has shown in trader/state/insider.json and flags any
deal in a stock from trader/portfolio.json, trader/watchlist.json or
trader/plan.json. Exit 0 when something was found, 1 when not.

Disclosures are public, after-the-fact and legal. A big name buying is not a
reason on its own to buy.
"""

import argparse
import csv
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import urllib.error
import urllib.request

ARCH = "https://nsearchives.nseindia.com/content/equities/"
UA = "day-trader-skill-india/2.0 (python-urllib; personal use)"
KEEP_DAYS = 60


class InsiderError(Exception):
    pass


def ist_now():
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) + _dt.timedelta(hours=5, minutes=30)


class Fetcher(object):
    def __init__(self, offline=None):
        self.offline = offline

    def get(self, url, name):
        if self.offline:
            path = os.path.join(self.offline, name)
            if not os.path.isfile(path):
                raise InsiderError("offline: no file %s" % path)
            with open(path, "rb") as fh:
                return fh.read()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except urllib.error.HTTPError as e:
            raise InsiderError("NSE answered HTTP %d for %s" % (e.code, url))
        except (urllib.error.URLError, OSError) as e:
            raise InsiderError("could not reach %s: %s" % (url, getattr(e, "reason", e)))


def _num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except ValueError:
        return None


def parse_deals(data, kind):
    """NSE bulk.csv / block.csv -> rows."""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    rdr = csv.reader(io.StringIO(text.replace("\r", "")))
    hdr, out = None, []
    for rec in rdr:
        if not rec or not any(c.strip() for c in rec):
            continue
        if hdr is None:
            hdr = [h.strip().lower() for h in rec]
            if "symbol" not in hdr:
                raise InsiderError("the %s file is not NSE's deals csv (header %s)" % (kind, ",".join(hdr)[:60]))
            continue
        r = dict(zip(hdr, [c.strip() for c in rec]))
        price_key = next((k for k in r if k.startswith("trade price")), None)
        qty_key = next((k for k in r if k.startswith("quantity")), None)
        qty, price = _num(r.get(qty_key, "")), _num(r.get(price_key, ""))
        out.append({"kind": kind, "date": r.get("date", ""), "symbol": r.get("symbol", "").upper(),
                    "name": r.get("security name", ""), "who": r.get("client name", ""),
                    "side": r.get("buy/sell", "").upper(), "qty": qty, "price": price,
                    "value_cr": round(qty * price / 1e7, 2) if qty and price else None,
                    "remarks": r.get("remarks", "")})
    return out


def yours(paths):
    syms = set()
    for p in paths:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
        except ValueError:
            continue
        for x in d.get("positions", []) + d.get("hits", []):
            s = x.get("ticker") or x.get("symbol")
            if s:
                syms.add(s.upper().replace(".NS", ""))
        if d.get("symbol"):
            syms.add(d["symbol"].upper().replace(".NS", ""))
    return syms


def render(rows, mine, title):
    out = [title]
    if not rows:
        out.append("  nothing.")
    for r in rows:
        flag = "*" if r["symbol"] in mine else " "
        out.append("%s %-11s %-12s %-5s %-38s %12s @ %-9s %s" % (
            flag, r["date"][:11], r["symbol"], r["side"][:5], r["who"][:38],
            "{:,.0f}".format(r["qty"]) if r["qty"] else "", "%.2f" % r["price"] if r["price"] else "",
            ("Rs %s cr" % r["value_cr"]) if r["value_cr"] is not None else ""))
    return "\n".join(out)


def fp(r):
    return hashlib.sha1(json.dumps([r["kind"], r["date"], r["symbol"], r["who"], r["side"], r["qty"]],
                                   sort_keys=True).encode()).hexdigest()[:16]


def fetch_kind(f, kind, a):
    return parse_deals(f.get(ARCH + kind + ".csv", kind + ".csv"), kind)


def filt(rows, a):
    if a.symbol:
        rows = [r for r in rows if r["symbol"] == a.symbol.upper()]
    if a.who:
        rx = re.compile(re.escape(a.who), re.I)
        rows = [r for r in rows if rx.search(r["who"])]
    if a.min_cr:
        rows = [r for r in rows if (r["value_cr"] or 0) >= a.min_cr]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Big-money tracker (trader-insider): NSE bulk and block deals.")
    ap.add_argument("cmd", choices=["bulk", "block", "watch"])
    ap.add_argument("--symbol")
    ap.add_argument("--who", help="filter by client / person name (case-insensitive)")
    ap.add_argument("--min-cr", type=float, default=0, help="only deals worth at least this many Rs crore")
    ap.add_argument("--state", default="trader/state/insider.json")
    ap.add_argument("--offline", metavar="DIR")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    f = Fetcher(offline=a.offline)
    mine = yours(["trader/portfolio.json", "trader/watchlist.json", "trader/plan.json"])
    kinds = ["bulk", "block"] if a.cmd == "watch" else [a.cmd]
    allrows, notes = [], []
    for k in kinds:
        try:
            allrows += filt(fetch_kind(f, k, a), a)
        except InsiderError as e:
            notes.append("%s: %s" % (k, e))
    if a.cmd == "watch":
        state = {}
        if os.path.isfile(a.state):
            with open(a.state, encoding="utf-8") as fh:
                state = json.load(fh)
        seen = state.setdefault("seen", {})
        stamp = ist_now().isoformat(timespec="seconds")
        new = [r for r in allrows if fp(r) not in seen]
        for r in allrows:
            seen.setdefault(fp(r), stamp)
        cutoff = (ist_now() - _dt.timedelta(days=KEEP_DAYS)).isoformat()
        state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
        d = os.path.dirname(a.state)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.state, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        allrows = new
    if a.json:
        print(json.dumps({"rows": allrows, "yours": sorted(mine), "notes": notes}, indent=2))
    else:
        for k in kinds:
            rs = [r for r in allrows if r["kind"] == k]
            print(render(rs, mine, {"bulk": "NSE bulk deals", "block": "NSE block deals"}[k]
                         + (" (new since last check)" if a.cmd == "watch" else "") + ":"))
            print()
        if any(r["symbol"] in mine for r in allrows):
            print("* = a stock you hold, plan or watch.")
        for n in notes:
            print("note: " + n)
        print("Public, after-the-fact disclosures. Not a signal on their own, not advice.")
    return 0 if allrows else 1


if __name__ == "__main__":
    sys.exit(main())
