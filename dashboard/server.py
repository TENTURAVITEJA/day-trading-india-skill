#!/usr/bin/env python3
"""Indicator dashboard for the day-trader skills. Standard library only.

    python3 dashboard/server.py                 # http://127.0.0.1:8770 , reads ./trader
    python3 dashboard/server.py --dir path/to/trader --port 8770 --open

A local, read-only web page: the watchlist with live prices, a candlestick
chart with the opening range, VWAP and moving averages, RSI, the current
trade plan (entry, stop, target), the Time Machine's odds and the alerts.
It never places, changes or cancels an order.

Data: Zerodha Kite Connect only (live quotes and historical candles) through
trader-broker/kite.py. Log in once each trading day:
    python3 trader-broker/kite.py login
Without a session the page says so and shows no prices. It listens on 127.0.0.1 only.
"""

import argparse
import datetime
import importlib.util
import io
import json
import os
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IST = datetime.timedelta(hours=5, minutes=30)
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


_kite_gate = threading.Lock()   # kite.candles() spaces its own calls; one caller at a time keeps that true


def need_kite():
    try:
        kite.access_token()
    except kite.KiteError as e:
        raise ApiError("%s (the dashboard reads Zerodha Kite only)" % e)


def plain(sym):
    s = str(sym).strip().upper()
    return s[:-3] if s.endswith(".NS") else s


def candles(sym, tf):
    need_kite()
    days, interval = (8, "5m") if tf == "5m" else (400, "1d")
    with _kite_gate:
        try:
            return kite.candles(plain(sym), interval, days=days), "kite"
        except kite.KiteError as e:
            raise ApiError(str(e))


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
            setups = scan.find_setups(closes)[0]
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
    need_kite()
    try:
        data = kite.quotes(symbols)
    except kite.KiteError as e:
        raise ApiError(str(e))
    out = {}
    for s in symbols:
        d = data.get(s)
        if not d:
            continue
        out[s] = {"last": d.get("last"), "prev_close": d.get("prev_close"), "high": d.get("high"),
                  "low": d.get("low"), "volume": d.get("volume"), "time": str(d.get("time") or "")[11:19]}
        if out[s]["last"] and out[s]["prev_close"]:
            out[s]["pct"] = round(100.0 * (out[s]["last"] / out[s]["prev_close"] - 1), 2)
    return {"source": "kite", "quotes": out}


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
            "source": "kite" if ok else None}


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
    print("Dashboard: %s   (reading %s; data from Zerodha Kite%s)" % (url, Handler.folder, "" if kite_ok() else
          ": NOT LOGGED IN, run  python3 trader-broker/kite.py login"))
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
