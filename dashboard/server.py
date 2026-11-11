#!/usr/bin/env python3
"""Indicator dashboard for the day-trader skills. Standard library only.

    python3 dashboard/server.py                 # http://127.0.0.1:8770 , reads ./trader
    python3 dashboard/server.py --dir path/to/trader --port 8770 --open

A local, read-only web page: the watchlist with live prices, a candlestick
chart with the opening range, VWAP and moving averages, RSI, the current
trade plan (entry, stop, target), the Time Machine's odds and the alerts.
It never places, changes or cancels an order.

Data: Zerodha Kite (live quotes and candles) when you have logged in today
with trader-broker/kite.py; otherwise Yahoo Finance (free, unofficial, can
lag). The page says which one it is using. It listens on 127.0.0.1 only.
"""

import argparse
import csv
import datetime
import importlib.util
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IST = datetime.timedelta(hours=5, minutes=30)
UA = "day-trader-skill-india/0.1 (python-urllib; personal use)"
CACHE_DIR = os.path.expanduser("~/.config/day-trader-india")
DEFAULT_SYMBOLS = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "BHARTIARTL", "ITC", "LT", "AXISBANK"]
OR_MINUTES = 15


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


kite = _load("kite", "trader-broker/kite.py")
alerts = _load("alerts", "trader-alerts/alerts.py")
scan = _load("scan", "trader-scanner/scan.py")


class ApiError(Exception):
    pass


# ---------------------------------------------------------------- small cache

_cache, _lock = {}, threading.Lock()


def cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _lock:
        _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------- data sources

def kite_ok():
    try:
        kite.access_token()
        return True
    except kite.KiteError:
        return False


_kite_gate = threading.Lock()
_kite_last = [0.0]


def kite_call(method, path, params=None):
    """Kite allows about 3 historical requests a second: space the calls out."""
    with _kite_gate:
        gap = time.time() - _kite_last[0]
        if gap < 0.4:
            time.sleep(0.4 - gap)
        _kite_last[0] = time.time()
        try:
            return kite.call(method, path, params=params)
        except kite.KiteError as e:
            raise ApiError(str(e))


def instruments():
    """{NSE symbol: {"token", "tick", "name"}} from Kite's daily instrument dump (cached per day)."""
    def fetch():
        day = (datetime.datetime.utcnow() + IST).strftime("%Y%m%d")
        path = os.path.join(CACHE_DIR, "instruments-NSE-%s.csv" % day)
        if not os.path.isfile(path):
            req = urllib.request.Request(kite.API + "/instruments/NSE", headers={
                "X-Kite-Version": "3", "User-Agent": UA,
                "Authorization": "token %s:%s" % (kite.keys()[0], kite.access_token())})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
            for old in os.listdir(CACHE_DIR):
                if old.startswith("instruments-NSE-"):
                    os.remove(os.path.join(CACHE_DIR, old))
            with open(path, "wb") as fh:
                fh.write(data)
        out = {}
        with open(path, encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("segment") == "NSE" and r.get("instrument_type") == "EQ":
                    out[r["tradingsymbol"]] = {"token": r["instrument_token"], "tick": float(r.get("tick_size") or 0.05),
                                               "name": r.get("name") or ""}
        return out
    return cached("instruments", 6 * 3600, fetch)


def plain(sym):
    s = str(sym).strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def candles_kite(sym, tf):
    ins = instruments().get(plain(sym))
    if not ins:
        raise ApiError("%s is not an NSE equity symbol Kite knows" % sym)
    now = datetime.datetime.utcnow() + IST
    days, interval = (8, "5minute") if tf == "5m" else (400, "day")
    frm = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d 09:00:00")
    data = kite_call("GET", "/instruments/historical/%s/%s" % (ins["token"], interval),
                     {"from": frm, "to": now.strftime("%Y-%m-%d %H:%M:%S")})
    out = []
    for c in (data or {}).get("candles") or []:
        ts = datetime.datetime.fromisoformat(c[0])
        local = ts.replace(tzinfo=None)            # Kite stamps are IST wall clock (+05:30)
        out.append({"dt": local, "o": c[1], "h": c[2], "l": c[3], "c": c[4], "v": c[5]})
    return out


def candles_yahoo(sym, tf):
    ysym = alerts.yahoo_symbol(sym)
    rng, interval = ("5d", "5m") if tf == "5m" else ("2y", "1d")
    last = None
    for host in ("query2", "query1"):
        url = "https://%s.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=%s" % (
            host, urllib.parse.quote(ysym), rng, interval)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                res = json.loads(r.read().decode())["chart"]["result"][0]
            break
        except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, IndexError) as e:
            last = e
    else:
        raise ApiError("Yahoo has no chart for %s (%s)" % (sym, last))
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"dt": datetime.datetime.utcfromtimestamp(ts) + IST, "o": o, "h": h, "l": l, "c": c,
                    "v": q["volume"][i] or 0})
    return out


def candles(sym, tf):
    if kite_ok() and not plain(sym).endswith(".BO"):
        return candles_kite(sym, tf), "kite"
    return candles_yahoo(sym, tf), "yahoo"


# ---------------------------------------------------------------- indicators

def ema(vals, n):
    out, k, e = [], 2.0 / (n + 1), None
    for i, v in enumerate(vals):
        if i < n - 1:
            out.append(None)
        elif i == n - 1:
            e = sum(vals[:n]) / n
            out.append(e)
        else:
            e = v * k + e * (1 - k)
            out.append(e)
    return out


def sma(vals, n):
    out, s = [], 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def rsi_series(closes, n=14):
    """Wilder's RSI for every bar (None until there are n changes)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    losses = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(closes)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def tstamp(dt):
    """IST wall clock as a fake-UTC epoch, so the chart's axis reads in IST."""
    return int((dt - datetime.datetime(1970, 1, 1)).total_seconds())


def r2(x):
    return None if x is None else round(x, 2)


def chart(sym, tf):
    bars, source = candles(sym, tf)
    if not bars:
        raise ApiError("no bars for %s" % sym)
    closes = [b["c"] for b in bars]
    rsi = rsi_series(closes)
    out = {"symbol": plain(sym), "tf": tf, "source": source}
    if tf == "5m":
        e20 = ema(closes, 20)
        day = bars[-1]["dt"].date()
        idx = [i for i, b in enumerate(bars) if b["dt"].date() == day]
        prev = [b for b in bars if b["dt"].date() < day]
        pv = cum = 0.0
        vwap = {}
        for i in idx:
            b = bars[i]
            pv += (b["h"] + b["l"] + b["c"]) / 3.0 * b["v"]
            cum += b["v"]
            vwap[i] = pv / cum if cum else None
        open_dt = datetime.datetime.combine(day, datetime.time(9, 15))
        rng = [bars[i] for i in idx if open_dt <= bars[i]["dt"] < open_dt + datetime.timedelta(minutes=OR_MINUTES)]
        o_r = None
        if len(rng) == OR_MINUTES // 5:
            o_r = {"high": r2(max(b["h"] for b in rng)), "low": r2(min(b["l"] for b in rng)),
                   "from": tstamp(rng[0]["dt"]), "until": tstamp(open_dt + datetime.timedelta(minutes=OR_MINUTES)),
                   "minutes": OR_MINUTES}
        today = [bars[i] for i in idx]
        last = bars[-1]
        out.update({
            "session": day.isoformat(),
            "candles": [{"time": tstamp(bars[i]["dt"]), "open": bars[i]["o"], "high": bars[i]["h"],
                         "low": bars[i]["l"], "close": bars[i]["c"]} for i in idx],
            "volume": [{"time": tstamp(bars[i]["dt"]), "value": bars[i]["v"], "up": bars[i]["c"] >= bars[i]["o"]} for i in idx],
            "lines": {"vwap": [{"time": tstamp(bars[i]["dt"]), "value": r2(vwap[i])} for i in idx if vwap[i]],
                      "ema20": [{"time": tstamp(bars[i]["dt"]), "value": r2(e20[i])} for i in idx if e20[i]]},
            "rsi": [{"time": tstamp(bars[i]["dt"]), "value": r2(rsi[i])} for i in idx if rsi[i] is not None],
            "opening_range": o_r,
            "stats": {"last": r2(last["c"]), "prev_close": r2(prev[-1]["c"]) if prev else None,
                      "day_high": r2(max(b["h"] for b in today)), "day_low": r2(min(b["l"] for b in today)),
                      "volume": sum(b["v"] for b in today), "vwap": r2(vwap[idx[-1]]), "ema20": r2(e20[-1]),
                      "rsi": r2(rsi[-1]), "last_bar": last["dt"].strftime("%H:%M")},
        })
    else:
        s20, s50 = sma(closes, 20), sma(closes, 50)
        keep = range(max(0, len(bars) - 130), len(bars))
        hi20 = max(closes[-21:-1]) if len(closes) > 21 else None
        try:
            setups = scan.find_setups(closes)
        except Exception:
            setups = []
        out.update({
            "candles": [{"time": bars[i]["dt"].strftime("%Y-%m-%d"), "open": bars[i]["o"], "high": bars[i]["h"],
                         "low": bars[i]["l"], "close": bars[i]["c"]} for i in keep],
            "volume": [{"time": bars[i]["dt"].strftime("%Y-%m-%d"), "value": bars[i]["v"],
                        "up": bars[i]["c"] >= bars[i]["o"]} for i in keep],
            "lines": {"sma20": [{"time": bars[i]["dt"].strftime("%Y-%m-%d"), "value": r2(s20[i])} for i in keep if s20[i]],
                      "sma50": [{"time": bars[i]["dt"].strftime("%Y-%m-%d"), "value": r2(s50[i])} for i in keep if s50[i]]},
            "rsi": [{"time": bars[i]["dt"].strftime("%Y-%m-%d"), "value": r2(rsi[i])} for i in keep if rsi[i] is not None],
            "high20": r2(hi20),
            "setups": _setup_names(setups),
            "stats": {"last": r2(closes[-1]), "prev_close": r2(closes[-2]) if len(closes) > 1 else None,
                      "sma20": r2(s20[-1]), "sma50": r2(s50[-1]), "rsi": r2(rsi[-1]), "high20": r2(hi20),
                      "last_bar": bars[-1]["dt"].strftime("%d %b")},
        })
    return out


SETUP_LABELS = {"breakout_20d": "20-day closing-high breakout", "bull_flag": "bull flag",
                "reclaim_ma20": "reclaimed 20-day average", "oversold": "oversold (RSI under 30)",
                "oversold_bounce": "oversold bounce", "orb": "opening-range breakout"}


def _setup_names(setups):
    out = []
    for s in setups or []:
        key = s if isinstance(s, str) else (s.get("setup") or s.get("name") or "")
        if key:
            out.append({"key": key, "label": SETUP_LABELS.get(key, key.replace("_", " "))})
    return out


def daily_summary(sym):
    def work():
        c = chart(sym, "1d")
        return {"symbol": c["symbol"], "rsi": c["stats"]["rsi"], "setups": c["setups"], "sma20": c["stats"]["sma20"],
                "source": c["source"]}
    return cached(("daily", plain(sym)), 900, work)


def quotes(symbols):
    symbols = [plain(s) for s in symbols if s][:60]
    out, source = {}, "yahoo"
    if kite_ok():
        source = "kite"
        ins = ["BSE:" + s[:-3] if s.endswith(".BO") else "NSE:" + s for s in symbols]
        data = kite_call("GET", "/quote", {"i": ins})
        for s, i in zip(symbols, ins):
            d = (data or {}).get(i)
            if not d:
                continue
            o = d.get("ohlc") or {}
            out[s] = {"last": d.get("last_price"), "prev_close": o.get("close"), "high": o.get("high"),
                      "low": o.get("low"), "volume": d.get("volume"), "time": str(d.get("last_trade_time") or "")[11:19]}
    else:
        for s in symbols:
            try:
                q = alerts.parse_yahoo(alerts.fetch_yahoo(s), s)
                out[s] = {"last": q.get("price"), "prev_close": q.get("prev_close"), "high": None, "low": None,
                          "volume": None, "time": ""}
            except Exception:
                continue
    for q in out.values():
        if q.get("last") and q.get("prev_close"):
            q["pct"] = round(100.0 * (q["last"] / q["prev_close"] - 1), 2)
    return {"source": source, "quotes": out}


# ---------------------------------------------------------------- trader/ files

def read_json(folder, name):
    try:
        with open(os.path.join(folder, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def watchlist(folder):
    wl = read_json(folder, "watchlist.json") or {}
    rows, seen = [], set()

    def add(sym, **kw):
        s = plain(sym)
        if s and s not in seen:
            seen.add(s)
            rows.append(dict(symbol=s, **kw))
    plan = read_json(folder, "plan.json") or {}
    if plan.get("symbol"):
        add(plan["symbol"], tags=[{"key": "plan", "label": "trade plan"}])
    pf = read_json(folder, "portfolio.json") or {}
    for p in pf.get("positions", []):
        if (p.get("shares") or p.get("qty") or 0) and (p.get("ticker") or p.get("symbol")):
            add(p.get("ticker") or p.get("symbol"), tags=[{"key": "held", "label": "holding"}])
    for h in wl.get("hits", []):
        if h.get("symbol"):
            extra = {}
            if h.get("at"):
                extra["note"] = "broke out %s, %+.2f%% over the range" % (h["at"], h.get("pct_over") or 0)
            add(h["symbol"], tags=_setup_names(h.get("setups")), **extra)
    scanned = bool(rows)
    if not rows:
        for s in DEFAULT_SYMBOLS:
            add(s, tags=[])
    return {"rows": rows[:40], "from_scan": scanned, "mode": wl.get("mode"), "as_of": wl.get("as_of")}


def trade(folder):
    plan = read_json(folder, "plan.json")
    tm = read_json(folder, "timemachine.json")
    today = (datetime.datetime.utcnow() + IST).date().isoformat()
    out = {"today": today, "plan": None, "odds": None, "order": None, "alerts": [], "entry": None}
    if plan:
        out["plan"] = {k: plan.get(k) for k in ("symbol", "date", "setup", "entry", "entry_time", "stop", "stop_label",
                                                "target", "target_label", "risk_per_share", "reward_risk", "flat_by")}
        out["plan"]["stale"] = plan.get("date") != today
    if tm and plan and (tm.get("plan") or {}).get("symbol") == plan.get("symbol") \
            and (tm.get("plan") or {}).get("date") == plan.get("date"):
        mc, hist = tm.get("mc") or {}, tm.get("history") or {}
        out["odds"] = {"p_target": mc.get("p_target"), "p_stop": mc.get("p_stop"), "p_neither": mc.get("p_neither"),
                       "p_green": mc.get("p_green"), "median_close": mc.get("median_close"),
                       "p5_close": mc.get("p5_close"), "p95_close": mc.get("p95_close"), "avg_R": mc.get("avg_R"),
                       "until": mc.get("until"), "replays": mc.get("replays"), "sessions": mc.get("pool_sessions"),
                       "history": {k: hist.get(k) for k in ("triggered", "target", "stop", "close", "avg_R")}}
    orders = (read_json(folder, "orders.json") or {}).get("orders") or []
    if orders:
        o = orders[0]
        out["order"] = {"symbol": o.get("symbol"), "side": o.get("side"), "qty": o.get("qty"), "type": o.get("type"),
                        "max_loss": (o.get("plan") or {}).get("max_loss")}
    al = read_json(folder, "alerts.json") or {}
    for r in (al.get("rules") if isinstance(al, dict) else al) or []:
        if not r.get("date") or r.get("date") == today:
            out["alerts"].append({k: r.get(k) for k in ("id", "symbol", "type", "price", "at", "pct", "label", "note")})
    e = read_json(folder, "entry.json")
    if e and e.get("date") == today:
        out["entry"] = {k: e.get(k) for k in ("symbol", "status", "time", "price", "level", "reason")}
    return out


def status():
    st = alerts.market_status(alerts.parse_now(None))
    sess = kite.load_session() or {}
    ok = kite_ok()
    return {"market": {"open": st["open"], "reason": st["reason"], "now": st["now_ist"], "weekday": st["weekday"],
                       "next_open": st.get("next_open"), "closes": st.get("closes")},
            "kite": {"connected": ok, "user": sess.get("user_name") if ok else None},
            "source": "kite" if ok else "yahoo"}


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    folder = "trader"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as fh:
                    return self._send(200, fh.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self._send(200, status())
            if u.path == "/api/watchlist":
                return self._send(200, watchlist(self.folder))
            if u.path == "/api/trade":
                return self._send(200, trade(self.folder))
            if u.path == "/api/quotes":
                return self._send(200, quotes((q.get("symbols") or [""])[0].split(",")))
            if u.path == "/api/daily":
                return self._send(200, daily_summary(_sym(q)))
            if u.path == "/api/chart":
                tf = (q.get("tf") or ["5m"])[0]
                if tf not in ("5m", "1d"):
                    raise ApiError("tf must be 5m or 1d")
                sym = _sym(q)
                return self._send(200, cached(("chart", sym, tf), 20 if tf == "5m" else 600, lambda: chart(sym, tf)))
            return self._send(404, {"error": "not found"})
        except ApiError as e:
            return self._send(502, {"error": str(e)})
        except Exception as e:   # keep the page alive; show the reason
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def log_message(self, *a):
        pass


def _sym(q):
    s = (q.get("symbol") or [""])[0].strip().upper()
    if not s or len(s) > 24 or not all(ch.isalnum() or ch in "&-." for ch in s):
        raise ApiError("give a symbol like RELIANCE")
    return plain(s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Indicator dashboard (read-only) for the day-trader skills.")
    ap.add_argument("--dir", default="trader", help="the trader/ folder to read (default ./trader)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--open", action="store_true", help="open the page in your browser")
    a = ap.parse_args(argv)
    Handler.folder = os.path.abspath(a.dir)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print("dashboard: cannot listen on 127.0.0.1:%d (%s)" % (a.port, e), file=sys.stderr)
        return 1
    url = "http://127.0.0.1:%d/" % a.port
    print("Dashboard: %s   (reading %s; data from %s)" % (url, Handler.folder, "Zerodha Kite" if kite_ok() else
          "Yahoo: run trader-broker/kite.py login for live Zerodha data"))
    print("Ctrl+C to stop.")
    sys.stdout.flush()
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
