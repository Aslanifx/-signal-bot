#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram zone-alert bot  (1H zone formation + 5m first entry into the zone)

Modes (python bot.py <mode>):
  run        normal run (default) - called every 5 minutes by GitHub Actions
  selftest   offline logic test (no internet needed)
  test       send a demo message + demo chart to your Telegram channel
  chatid     print the chat id of your channel/group (for the secret)
  backtest   run the setup on the last ~month of real data, send 3 sample charts
"""
import io
import json
import os
import sys
import time
import traceback
import datetime as dt

import requests

try:  # optional: Chrome-like connection, helps when Yahoo blocks plain scripts
    from curl_cffi import requests as cffi_requests
except Exception:  # pragma: no cover
    cffi_requests = None

# =====================================================================
#                              SETTINGS
# =====================================================================
SWING_N = 1                  # 1 = swing of 3 candles (middle one is the extreme). 2 = 5 candles (fewer signals)
LOOKBACK_BARS = 150          # how far back (1H candles) to look for swings
MAX_SWEEP_TO_MSS_BARS = 48   # max 1H candles between the liquidity hunt and the structure break
CHART_CANDLES = 70           # candles shown on the chart
MAX_SIGNAL_AGE_H = 3         # a zone signal older than this (e.g. after an outage) is not sent
ZONE_MAX_AGE_H = 96          # a zone that was never touched is dropped after this many hours
FAIL_ALERT_RUNS = 12         # data down for this many runs in a row -> warning in channel
MAX_CATCHUP = 8              # max missed 1H candles processed in one run

H1 = 3600
CLOSE_MARGIN = 90            # seconds to wait after a candle ends before trusting it
GATE_SECONDS = 115 * 60      # skip the 1H download if the next candle can't have closed yet
RLM = "\u200f"               # keeps Persian lines right-to-left in Telegram
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))

# name shown, Yahoo tickers (tried in order), decimals, optional Kraken pair (crypto fallback)
SYMBOLS = [
    {"name": "NAS100", "yahoo": ["NQ=F"],               "dec": 2, "kraken": None},
    {"name": "XAUUSD", "yahoo": ["XAUUSD=X", "GC=F"],   "dec": 2, "kraken": None},
    {"name": "US30",   "yahoo": ["YM=F"],               "dec": 2, "kraken": None},
    {"name": "SPX500", "yahoo": ["ES=F"],               "dec": 2, "kraken": None},
    {"name": "GER40",  "yahoo": ["^GDAXI"],             "dec": 2, "kraken": None},
    {"name": "EURUSD", "yahoo": ["EURUSD=X"],           "dec": 5, "kraken": None},
    {"name": "USDJPY", "yahoo": ["JPY=X"],              "dec": 3, "kraken": None},
    {"name": "GBPUSD", "yahoo": ["GBPUSD=X"],           "dec": 5, "kraken": None},
    {"name": "BTC",    "yahoo": ["BTC-USD"],            "dec": 2, "kraken": "XBTUSD"},
    {"name": "ETH",    "yahoo": ["ETH-USD"],            "dec": 2, "kraken": "ETHUSD"},
]

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
}
STATS = {"ok": 0, "fail": 0}


def log(*a):
    print(*a, flush=True)


def now_ts():
    return time.time()


# =====================================================================
#                                DATA
# =====================================================================
def http_get_json(url, params=None, timeout=20):
    """GET with retries; alternates between a Chrome-like client and plain requests."""
    clients = []
    if cffi_requests is not None:
        clients.append("cffi")
    clients.append("requests")
    last = "unknown error"
    for attempt in range(4):
        kind = clients[attempt % len(clients)]
        try:
            if kind == "cffi":
                r = cffi_requests.get(url, params=params, headers=UA,
                                      timeout=timeout, impersonate="chrome")
            else:
                r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = "HTTP %s" % r.status_code
        except Exception as e:  # network error, bad json, ...
            last = repr(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(last)


def _clean(rows):
    """rows: list of (t,o,h,l,c) -> sorted unique valid candles."""
    out = {}
    for t, o, h, l, c in rows:
        if None in (t, o, h, l, c):
            continue
        o, h, l, c = float(o), float(h), float(l), float(c)
        if min(o, h, l, c) <= 0:
            continue
        h = max(h, o, c)
        l = min(l, o, c)
        out[int(t)] = {"t": int(t), "o": o, "h": h, "l": l, "c": c}
    return [out[k] for k in sorted(out)]


def yahoo_fetch(ticker, interval, rng):
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        try:
            url = "https://%s/v8/finance/chart/%s" % (host, ticker)
            js = http_get_json(url, {"interval": interval, "range": rng,
                                     "includePrePost": "false"})
            res = js["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            rows = list(zip(ts, q.get("open", []), q.get("high", []),
                            q.get("low", []), q.get("close", [])))
            return _clean(rows)
        except Exception as e:
            last = e
    raise RuntimeError("yahoo %s: %s" % (ticker, last))


def kraken_fetch(pair, minutes):
    js = http_get_json("https://api.kraken.com/0/public/OHLC",
                       {"pair": pair, "interval": minutes})
    if js.get("error"):
        raise RuntimeError("kraken: %s" % js["error"])
    res = js["result"]
    key = [k for k in res if k != "last"][0]
    rows = [(r[0], r[1], r[2], r[3], r[4]) for r in res[key]]
    return _clean(rows)


def fetch_candles(sym, interval, rng):
    """interval: '60m' or '5m'. Returns (candles, source)."""
    need = 30 if interval == "60m" else 1
    errs = []
    for tk in sym["yahoo"]:
        try:
            c = yahoo_fetch(tk, interval, rng)
            if len(c) >= need:
                STATS["ok"] += 1
                return c, tk
            errs.append("%s: only %d candles" % (tk, len(c)))
        except Exception as e:
            errs.append(str(e))
    if sym.get("kraken"):
        try:
            c = kraken_fetch(sym["kraken"], 60 if interval == "60m" else 5)
            if len(c) >= need:
                STATS["ok"] += 1
                return c, "kraken"
            errs.append("kraken: only %d candles" % len(c))
        except Exception as e:
            errs.append(str(e))
    STATS["fail"] += 1
    raise RuntimeError("%s data failed -> %s" % (sym["name"], " | ".join(errs)))


# =====================================================================
#                       SETUP LOGIC  (1H zone)
# =====================================================================
def _is_sh(c, i):
    n = SWING_N
    if i < n or i + n >= len(c):
        return False
    v = c[i]["h"]
    for k in range(1, n + 1):
        if not (v > c[i - k]["h"] and v > c[i + k]["h"]):
            return False
    return True


def _is_sl(c, i):
    n = SWING_N
    if i < n or i + n >= len(c):
        return False
    v = c[i]["l"]
    for k in range(1, n + 1):
        if not (v < c[i - k]["l"] and v < c[i + k]["l"]):
            return False
    return True


def detect_sell(c):
    """
    Looks ONLY at the last candle of list c (the just-closed 1H candle, index j).
    SELL zone =
      1) a swing high gets hunted: a candle's wick goes above it but that candle
         does NOT close above it (first touch of that swing high),
      2) no candle closes above that swing high afterwards,
      3) the most recent swing low before the hunt candle is the structure low L,
      4) candle j is the FIRST candle that touches L after the hunt, and its
         CLOSE (body) is below L  -> MSS.
    Zone = from L (broken swing low) up to the highest wick of the hunt.
    """
    j = len(c) - 1
    if j < 6:
        return None
    best = None
    first = max(SWING_N, j - LOOKBACK_BARS)
    for s in range(first, j - SWING_N - 1):
        if not _is_sh(c, s):
            continue
        sh = c[s]["h"]
        h = None
        for k in range(s + SWING_N + 1, j):          # first candle that takes the swing high
            if c[k]["h"] > sh:
                h = k
                break
        if h is None:
            continue
        if j - h > MAX_SWEEP_TO_MSS_BARS:
            continue
        if c[h]["c"] > sh:                           # closed above -> not a wick hunt
            continue
        if any(c[k]["c"] > sh for k in range(h + 1, j)):
            continue
        li = None                                    # structure low = last swing low before the hunt
        for i in range(h - SWING_N, max(SWING_N, h - LOOKBACK_BARS) - 1, -1):
            if _is_sl(c, i):
                li = i
                break
        if li is None:
            continue
        lv = c[li]["l"]
        if any(c[k]["l"] <= lv for k in range(li + 1, j)):   # must still be untouched
            continue
        if not (c[j]["c"] < lv):                     # first touch must CLOSE below with the body
            continue
        ext = max(c[k]["h"] for k in range(h, j + 1))
        cand = {"dir": "SELL", "top": ext, "bottom": lv, "swept": sh,
                "swing_i": s, "sweep_i": h, "struct_i": li, "mss_i": j}
        if best is None or h > best["sweep_i"]:
            best = cand
    return best


def _mirror(c):
    return [{"t": x["t"], "o": -x["o"], "h": -x["l"], "l": -x["h"], "c": -x["c"]} for x in c]


def detect_buy(c):
    """BUY is the exact mirror image of SELL (prices multiplied by -1)."""
    r = detect_sell(_mirror(c))
    if not r:
        return None
    return {"dir": "BUY", "top": -r["bottom"], "bottom": -r["top"], "swept": -r["swept"],
            "swing_i": r["swing_i"], "sweep_i": r["sweep_i"],
            "struct_i": r["struct_i"], "mss_i": r["mss_i"]}


def find_touch(zone, c5):
    """First 5m candle (including the one still forming) that touches the zone edge."""
    for x in c5:
        if x["t"] < zone["t0"]:
            continue
        if zone["dir"] == "BUY":
            if x["l"] <= zone["top"]:
                return x
        else:
            if x["h"] >= zone["bottom"]:
                return x
    return None


# =====================================================================
#                              CHART
# =====================================================================
def tehran(ts, fmt="%Y/%m/%d  %H:%M"):
    return dt.datetime.fromtimestamp(ts, TEHRAN).strftime(fmt)


def fmt(v, dec):
    return ("%." + str(dec) + "f") % v


def render_chart(name, d, sub, dec):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    j = len(sub) - 1
    start = min(j - CHART_CANDLES + 1, d["struct_i"] - 10)
    start = max(0, start, j - 120)
    view = sub[start:]
    n = len(view)
    buy = d["dir"] == "BUY"
    col = "#1e8449" if buy else "#c0392b"
    bg = "#e8e8e8"

    ymin = min(min(x["l"] for x in view), d["bottom"])
    ymax = max(max(x["h"] for x in view), d["top"])
    pad = (ymax - ymin) * 0.07
    tiny = (ymax - ymin) * 0.002

    fig, ax = plt.subplots(figsize=(9, 5.6), dpi=130)
    fig.patch.set_facecolor(bg)
    ax.set_facecolor(bg)
    for i, x in enumerate(view):
        ax.vlines(i, x["l"], x["h"], color="#222222", linewidth=1.0, zorder=2)
        hi, lo = max(x["o"], x["c"]), min(x["o"], x["c"])
        face = "#ffffff" if x["c"] >= x["o"] else "#4a4a4a"
        ax.add_patch(Rectangle((i - 0.32, lo), 0.64, max(hi - lo, tiny), facecolor=face,
                               edgecolor="#222222", linewidth=0.9, zorder=3))
    x0 = max(0, d["struct_i"] - start - 1)
    xr = n - 1 + 7
    ax.hlines([d["top"], d["bottom"]], x0, xr, colors=col, linewidth=1.7, zorder=4)
    ax.fill_between([x0, xr], [d["bottom"]] * 2, [d["top"]] * 2, color=col, alpha=0.10, zorder=1)
    ax.text(xr - 0.2, d["top"], fmt(d["top"], dec), color=col, fontsize=9, va="bottom", ha="right")
    ax.text(xr - 0.2, d["bottom"], fmt(d["bottom"], dec), color=col, fontsize=9, va="top", ha="right")
    ax.set_xlim(-1, xr + 1)
    ax.set_ylim(ymin - pad, ymax + pad)
    nt = min(5, max(2, n // 14))
    ticks = sorted(set(int(round(k * (n - 1) / float(nt - 1))) for k in range(nt)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([tehran(view[k]["t"], "%m/%d %H:%M") for k in ticks], fontsize=8, color="#555555")
    lbls = ax.get_xticklabels()
    if lbls:
        lbls[0].set_ha("left")
        lbls[-1].set_ha("right")
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelsize=8, colors="#555555", length=0)
    ax.tick_params(axis="x", length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    fig.text(0.02, 0.955, "%s  |  1H  |  %s ZONE" % (name, "BUY" if buy else "SELL"),
             fontsize=13, fontweight="bold", color=col, ha="left", va="center")
    fig.text(0.98, 0.955, "Tehran time", fontsize=8, color="#777777", ha="right", va="center")
    fig.subplots_adjust(left=0.02, right=0.93, top=0.92, bottom=0.08)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=bg)
    plt.close(fig)
    return buf.getvalue()


# =====================================================================
#                             TELEGRAM
# =====================================================================
def _creds():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID secret is missing")
    return token, chat


def tg_call(method, data=None, files=None):
    token, _ = _creds()
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    last = "unknown"
    for attempt in range(4):
        try:
            r = requests.post(url, data=data, files=files, timeout=60)
            try:
                js = r.json()
            except Exception:
                js = {}
            if js.get("ok"):
                return js["result"]
            last = "HTTP %s %s" % (r.status_code, js.get("description", r.text[:200]))
            if r.status_code == 429:
                time.sleep(int(js.get("parameters", {}).get("retry_after", 3)) + 1)
                continue
            if 400 <= r.status_code < 500:
                break                                   # wrong token/chat etc: retry is useless
        except Exception as e:
            last = repr(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("telegram %s failed: %s" % (method, last))


def send_text(text, reply_to=None):
    _, chat = _creds()
    data = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
        data["allow_sending_without_reply"] = "true"
    return tg_call("sendMessage", data)["message_id"]


def send_photo(png, caption, reply_to=None):
    _, chat = _creds()
    data = {"chat_id": chat, "caption": caption[:1000]}
    if reply_to:
        data["reply_to_message_id"] = str(reply_to)
        data["allow_sending_without_reply"] = "true"
    return tg_call("sendPhoto", data, {"photo": ("zone.png", png, "image/png")})["message_id"]


def zone_id(name, d, t_close):
    return "%s-%s-%s" % (name, "B" if d["dir"] == "BUY" else "S", tehran(t_close, "%m%d-%H%M"))


def caption_zone(name, d, zid, t_close, dec, prefix=""):
    buy = d["dir"] == "BUY"
    lines = [
        "%s ناحیه معاملاتی %s تشکیل شد" % ("🟢" if buy else "🔴", "خرید" if buy else "فروش"),
        "📌 نماد: #%s  #%s" % (name, "BUY" if buy else "SELL"),
        "⏱ تایم‌فریم: ۱ ساعته",
        "🔺 سقف ناحیه: %s" % fmt(d["top"], dec),
        "🔻 کف ناحیه: %s" % fmt(d["bottom"], dec),
        "🕒 کلوز کندل: %s (وقت تهران)" % tehran(t_close),
        "🔖 کد ناحیه: %s" % zid,
        "⏳ منتظر اولین ورود قیمت به ناحیه ...",
    ]
    if prefix:
        lines.insert(0, prefix)
    return "\n".join(RLM + x for x in lines)


def text_entry(name, z, touch, dec):
    buy = z["dir"] == "BUY"
    edge = z["top"] if buy else z["bottom"]
    lines = [
        "🔔 قیمت برای اولین بار وارد ناحیه %s شد" % ("خرید" if buy else "فروش"),
        "📌 نماد: #%s  #%s" % (z["sym"], "BUY" if buy else "SELL"),
        "📍 لبه %s ناحیه لمس شد: %s" % ("بالایی" if buy else "پایینی", fmt(edge, dec)),
        "🕒 زمان: %s (وقت تهران)" % tehran(touch["t"]),
        "🔖 کد ناحیه: %s" % z["id"],
        "👀 برو پشت چارت برای ورود ۱ دقیقه",
    ]
    return "\n".join(RLM + x for x in lines)


# =====================================================================
#                               STATE
# =====================================================================
def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("last_1h", {})
    s.setdefault("zones", [])
    s.setdefault("fail_runs", 0)
    s.setdefault("down_notified", False)
    s.setdefault("hb_ts", 0)
    return s


def save_state(s):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STATE_PATH)


# =====================================================================
#                           MAIN PROCESSING
# =====================================================================
def sym_by_name(name):
    for s in SYMBOLS:
        if s["name"] == name:
            return s
    return None


def process_1h(sym, state, now):
    name = sym["name"]
    last = state["last_1h"].get(name)
    if last and now - last < GATE_SECONDS:
        return
    candles, src = fetch_candles(sym, "60m", "1mo")
    closed = [x for x in candles if x["t"] + H1 + CLOSE_MARGIN <= now]
    if len(closed) < 30:
        raise RuntimeError("%s: not enough closed candles" % name)
    if last is None:                                    # first run: just remember where we are
        state["last_1h"][name] = closed[-1]["t"]
        log("[%s] first run, start from %s (%s)" % (name, tehran(closed[-1]["t"]), src))
        return
    new = [i for i, x in enumerate(closed) if x["t"] > last]
    if not new:
        return
    if len(new) > MAX_CATCHUP:
        log("[%s] %d missed candles, only the last %d are checked" % (name, len(new), MAX_CATCHUP))
        new = new[-MAX_CATCHUP:]
    for j in new:
        sub = closed[:j + 1]
        t_close = sub[-1]["t"] + H1
        if now - t_close <= MAX_SIGNAL_AGE_H * 3600:
            for d in (detect_sell(sub), detect_buy(sub)):
                if d:
                    send_zone(sym, state, d, sub, t_close)
        else:
            log("[%s] candle %s is too old, skipped" % (name, tehran(t_close)))
        state["last_1h"][name] = sub[-1]["t"]
        save_state(state)


def send_zone(sym, state, d, sub, t_close, prefix=""):
    name, dec = sym["name"], sym["dec"]
    zid = zone_id(name, d, t_close)
    if any(z["id"] == zid for z in state["zones"]):
        return
    png = render_chart(name, d, sub, dec)
    mid = send_photo(png, caption_zone(name, d, zid, t_close, dec, prefix))
    state["zones"].append({"id": zid, "sym": name, "dir": d["dir"], "top": d["top"],
                           "bottom": d["bottom"], "t0": int(t_close), "status": "active",
                           "msg_id": mid})
    save_state(state)
    log("[%s] ZONE %s sent" % (name, zid))


def process_5m(sym, state, now):
    name, dec = sym["name"], sym["dec"]
    zs = [z for z in state["zones"] if z["sym"] == name and z["status"] == "active"]
    for z in zs:
        if now - z["t0"] > ZONE_MAX_AGE_H * 3600:
            z["status"] = "expired"
            log("[%s] zone %s expired" % (name, z["id"]))
    zs = [z for z in zs if z["status"] == "active"]
    if not zs:
        return
    c5, _src = fetch_candles(sym, "5m", "5d")
    for z in zs:
        x = find_touch(z, c5)
        if x:
            send_text(text_entry(name, z, x, dec), reply_to=z.get("msg_id"))
            z["status"] = "used"
            z["touch_t"] = x["t"]
            save_state(state)
            log("[%s] ENTRY alert for %s" % (name, z["id"]))


def run():
    state = load_state()
    now = now_ts()
    STATS["ok"] = STATS["fail"] = 0
    errors = 0
    for sym in SYMBOLS:
        for fn in (process_1h, process_5m):
            try:
                fn(sym, state, now)
            except Exception:
                errors += 1
                log("[%s] ERROR in %s:" % (sym["name"], fn.__name__))
                traceback.print_exc()
        save_state(state)

    # data health: warn once if nothing can be downloaded for a long time
    if STATS["ok"] + STATS["fail"] > 0:
        if STATS["ok"] == 0:
            state["fail_runs"] += 1
        else:
            if state["down_notified"]:
                try:
                    send_text(RLM + "✅ اتصال دیتا دوباره برقرار شد، ربات عادی کار می‌کند.")
                    state["down_notified"] = False
                except Exception:
                    traceback.print_exc()
            state["fail_runs"] = 0
        if state["fail_runs"] >= FAIL_ALERT_RUNS and not state["down_notified"]:
            try:
                send_text(RLM + "⚠️ هشدار: ربات چند بار پشت سر هم نتوانست دیتای بازار را بگیرد. "
                          "تا وصل شدن دوباره ممکن است آلارم‌ها دیر برسند.")
                state["down_notified"] = True
            except Exception:
                traceback.print_exc()

    # weekly heartbeat commit keeps GitHub from pausing the schedule
    if now - state.get("hb_ts", 0) > 6 * 86400:
        state["hb_ts"] = int(now)

    # clean old finished zones
    state["zones"] = [z for z in state["zones"]
                      if z["status"] == "active" or now - z["t0"] < 15 * 86400]
    save_state(state)
    log("done. data ok=%d fail=%d errors=%d active_zones=%d" % (
        STATS["ok"], STATS["fail"], errors,
        len([z for z in state["zones"] if z["status"] == "active"])))


# =====================================================================
#                       EXTRA MODES (test / chatid / backtest)
# =====================================================================
def _mk(i, o, h, l, c, base):
    return {"t": base + i * H1, "o": float(o), "h": float(h), "l": float(l), "c": float(c)}


SELL_PATTERN = [
    (100, 101, 99, 100.5), (100.5, 102, 100, 101.8), (101.8, 104, 101.5, 103.5),
    (103.5, 103.8, 102, 102.5), (102.5, 103.6, 101.0, 103.2), (103.2, 103.7, 101.8, 102.0),
    (102.0, 104.6, 102.0, 103.0), (103.0, 103.5, 102.2, 102.6), (102.6, 102.9, 99.8, 100.2),
]


def pattern_candles(rows, base, scale=1.0, mirror_at=None):
    out = []
    for i, (o, h, l, c) in enumerate(rows):
        if mirror_at is not None:
            o, h, l, c = mirror_at - o, mirror_at - l, mirror_at - h, mirror_at - c
        out.append(_mk(i, o * scale, h * scale, l * scale, c * scale, base))
    return out


def demo_zone(direction):
    base = int(now_ts() // H1) * H1 - 9 * H1
    if direction == "SELL":
        sub = pattern_candles(SELL_PATTERN, base, scale=210.0)
        d = detect_sell(sub)
    else:
        sub = pattern_candles(SELL_PATTERN, base, scale=210.0, mirror_at=200.0)
        d = detect_buy(sub)
    return sub, d


def mode_test():
    send_text(RLM + "✅ تست اتصال: ربات به کانال وصل است.")
    sub, d = demo_zone("SELL")
    t_close = sub[-1]["t"] + H1
    zid = "TEST-S-0000-0000"
    png = render_chart("NAS100", d, sub, 2)
    mid = send_photo(png, caption_zone("NAS100", d, zid, t_close, 2, "🧪 این فقط نمونه آزمایشی است"))
    z = {"id": zid, "sym": "NAS100", "dir": "SELL", "bottom": d["bottom"], "top": d["top"]}
    send_text(text_entry("NAS100", z, {"t": t_close + 600}, 2), reply_to=mid)
    log("test messages sent")


def mode_chatid():
    res = tg_call("getUpdates", {"limit": 100, "timeout": 0})
    seen = {}
    for u in res:
        for key in ("message", "channel_post", "edited_message", "edited_channel_post",
                    "my_chat_member", "chat_member"):
            obj = u.get(key)
            if obj and obj.get("chat"):
                ch = obj["chat"]
                seen[ch["id"]] = "%s | %s | @%s" % (ch.get("type"), ch.get("title") or ch.get("first_name"),
                                                    ch.get("username", "-"))
    if not seen:
        log("NOTHING FOUND. Send a new message in your channel, then run this again within a few minutes.")
    for cid, info in seen.items():
        log("CHAT ID = %s    (%s)" % (cid, info))


def mode_backtest():
    found = []
    for sym in SYMBOLS:
        try:
            candles, src = fetch_candles(sym, "60m", "1mo")
        except Exception as e:
            log("[%s] no data: %s" % (sym["name"], e))
            continue
        closed = candles[:-1]
        cnt = 0
        for j in range(40, len(closed)):
            sub = closed[:j + 1]
            for d in (detect_sell(sub), detect_buy(sub)):
                if d:
                    cnt += 1
                    found.append((sub[-1]["t"], sym, d, sub))
                    log("  %s %s zone  %s .. %s  (candle close %s)" % (
                        sym["name"], d["dir"], fmt(d["bottom"], sym["dec"]), fmt(d["top"], sym["dec"]),
                        tehran(sub[-1]["t"] + H1)))
        log("[%s] %d candles (%s) -> %d zone signals" % (sym["name"], len(closed), src, cnt))
    found.sort(key=lambda r: r[0])
    log("TOTAL zone signals: %d" % len(found))
    if not found:
        send_text(RLM + "🧪 بک‌تست: در داده‌ی یک ماه اخیر هیچ ناحیه‌ای پیدا نشد.")
        return
    send_text(RLM + "🧪 بک‌تست یک ماه اخیر: %d ناحیه پیدا شد. ۳ نمونه‌ی آخر برای مقایسه با ستاپ خودت:" % len(found))
    for t, sym, d, sub in found[-3:]:
        t_close = sub[-1]["t"] + H1
        png = render_chart(sym["name"], d, sub, sym["dec"])
        send_photo(png, caption_zone(sym["name"], d, zone_id(sym["name"], d, t_close), t_close,
                                     sym["dec"], "🧪 نمونه بک‌تست (قدیمی)"))


# =====================================================================
#                      OFFLINE SELF TEST (no internet)
# =====================================================================
def selftest():
    base = 1_700_000_000 - (1_700_000_000 % H1)

    def build(rows, mirror_at=None):
        return pattern_candles(rows, base, mirror_at=mirror_at)

    # 1) SELL positive
    c = build(SELL_PATTERN)
    d = detect_sell(c)
    assert d and d["dir"] == "SELL", "sell not detected"
    assert abs(d["top"] - 104.6) < 1e-9 and abs(d["bottom"] - 101.0) < 1e-9, "sell zone levels wrong"
    assert detect_buy(c) is None, "buy must not appear on sell pattern"
    # 2) BUY positive (mirror)
    cb = build(SELL_PATTERN, mirror_at=200.0)
    db = detect_buy(cb)
    assert db and db["dir"] == "BUY", "buy not detected"
    assert abs(db["top"] - 99.0) < 1e-9 and abs(db["bottom"] - 95.4) < 1e-9, "buy zone levels wrong"
    assert detect_sell(cb) is None, "sell must not appear on buy pattern"
    # 3) wick only through the swing low, then closes above -> no signal, and later break is NOT valid either
    rows = list(SELL_PATTERN)
    rows[8] = (102.6, 102.9, 99.8, 101.5)
    assert detect_sell(build(rows)) is None, "wick-only break must not signal"
    rows.append((101.5, 101.8, 99.0, 99.5))
    assert detect_sell(build(rows)) is None, "second-candle break must not signal"
    # 4) hunt candle closes ABOVE the swing high -> not a hunt
    rows = list(SELL_PATTERN)
    rows[6] = (102.0, 104.6, 102.0, 104.3)
    assert detect_sell(build(rows)) is None, "close above swing high must not signal"
    # 5) a later candle closes above the swing high before the break -> invalid
    rows = list(SELL_PATTERN[:8]) + [(102.6, 104.5, 102.5, 104.3), (104.3, 104.4, 99.0, 99.5)]
    assert detect_sell(build(rows)) is None, "close above swing high after hunt must invalidate"
    # 6) break candle must be strictly after the hunt candle
    rows = list(SELL_PATTERN[:6]) + [(102.0, 104.6, 99.8, 100.2)]
    assert detect_sell(build(rows)) is None, "hunt and break in the same candle must not signal"
    # 7) zone touch logic
    zone = {"dir": "SELL", "top": 104.6, "bottom": 101.0, "t0": base + 9 * H1}
    c5 = [{"t": base + 9 * H1 - 300, "o": 0, "h": 200, "l": 0, "c": 0},      # before zone -> ignored
          {"t": base + 9 * H1, "o": 100, "h": 100.8, "l": 99.9, "c": 100.5},
          {"t": base + 9 * H1 + 300, "o": 100.5, "h": 101.2, "l": 100.4, "c": 101.0}]
    t = find_touch(zone, c5)
    assert t and t["t"] == base + 9 * H1 + 300, "sell touch wrong"
    zone = {"dir": "BUY", "top": 99.0, "bottom": 95.4, "t0": base + 9 * H1}
    c5 = [{"t": base + 9 * H1, "o": 100, "h": 100.1, "l": 99.5, "c": 99.8},
          {"t": base + 9 * H1 + 300, "o": 99.8, "h": 99.9, "l": 98.9, "c": 99.0}]
    t = find_touch(zone, c5)
    assert t and t["t"] == base + 9 * H1 + 300, "buy touch wrong"
    zone = {"dir": "BUY", "top": 99.0, "bottom": 95.4, "t0": base + 9 * H1}
    assert find_touch(zone, c5[:1]) is None, "no touch must give None"
    log("SELFTEST OK")


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "run").strip().lower()
    try:
        if mode == "selftest":
            selftest()
        elif mode == "test":
            mode_test()
        elif mode == "chatid":
            mode_chatid()
        elif mode == "backtest":
            mode_backtest()
        else:
            run()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
