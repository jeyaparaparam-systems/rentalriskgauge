#!/usr/bin/env python3
"""
Daily data update for the Rental Risk Gauge (www.rentalriskgauge.com).

Runs after the U.S. stock market closes on each market day (see
.github/workflows/daily-update.yml). It downloads the latest public data,
recalculates both risk scores with the published methodology, writes a short
plain-English note, and saves:

  data/latest.json   – today's inputs, signal details and note (read by index.html)
  data/history.json  – one row per update: {date, score, mfScore, inputs}

Model v5.2. Licensed sources are the main data where available (S&P Cotality
Case-Shiller, Moody's Baa, Nasdaq-100 via the FRED API; CoStar and MLS figures
entered by hand in data/manual.json), used for non-commercial purposes while
permission requests are pending. Every licensed input has a free public fallback
(U.S. Treasury, Federal Reserve, BLS, Census, FHFA, Cboe, Freddie Mac,
Realtor.com) that is always downloaded too. Set USE_LICENSED_DATA=0 to make the
headline scores use free public data only (the page has the same switch).

If any download fails, the previous value is kept and the run still succeeds.
Only the Python standard library is used.

Usage:
  python scripts/update.py            # normal run (skips weekends/holidays)
  python scripts/update.py --force    # run even on a non-market day
  python scripts/update.py --check    # recompute scores from current data, no downloads
  python scripts/update.py --verify   # exit with an error if the saved data is older than it should be
"""
import csv, io, json, math, os, sys, urllib.request, urllib.parse
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
PT = ZoneInfo("America/Los_Angeles")
FRED_KEY = os.environ.get("FRED_API_KEY", "").strip()
UA = {"User-Agent": "RentalRiskGauge/1.0 (+https://www.rentalriskgauge.com)"}

# U.S. stock market (NYSE) full-day holidays
HOLIDAYS = {
    "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    "2028-01-17", "2028-02-21", "2028-04-14", "2028-05-29", "2028-06-19", "2028-07-04",
    "2028-09-04", "2028-11-23", "2028-12-25",
}

# ---------------------------------------------------------------- scoring (must match the Methodology page)
def clamp(v): return max(0.0, min(100.0, v))
def lin(v, calm, stress): return clamp((v - calm) / (stress - calm) * 100)

def _ok(v): return isinstance(v, (int, float)) and math.isfinite(v)
def _L(v, c, st): return lin(v, c, st) if _ok(v) else None
def _mix(parts):
    t = w = 0.0
    for v, wt in parts:
        if v is not None: t += v * wt; w += wt
    return t / w if w else None
def _final(parts):
    """75% weighted average of the parts + 25% average of the two highest parts (model v3)."""
    r = [(v, w) for v, w in parts if v is not None]
    if not r: return float("nan")
    tw = sum(w for _, w in r)
    avg = sum(v * w for v, w in r) / tw
    top = sorted((v for v, _ in r), reverse=True)[:2]
    return 0.75 * avg + 0.25 * sum(top) / len(top)

# measure: (input key, calm, stress, licensed?)
M = {"vixAvg": ("vixAvg", 12, 35, 0), "mort": ("mort", 5.5, 8, 0), "mortChg": ("mortChg", -0.5, 1.5, 0), "vix": ("vix", 12, 35, 0),
     "baa": ("baa", 1.6, 3.5, 1), "cr": ("cr", 1, 3, 0), "ndx": ("ndxDD", 0, 25, 1),
     "unemp": ("unemp", 3, 7, 0), "tech": ("techJobs", 3, -5, 0), "pay": ("payroll", 2.5, -2, 0),
     "mos": ("mos", 1, 6, 1), "lpr": ("lpr", 1.46, 8.73, 0), "listYoY": ("listYoY", -20, 60, 0), "priceCut": ("priceCut", 20, 50, 0),
     "cs": ("cs", 6, -6, 1), "hp": ("hp", 6, -6, 0), "hpSJ": ("hpSJ", 6, -6, 0), "p2rSJ": ("p2rSJ", 90, 120, 0), "p2r": ("p2r", 90, 120, 1), "p2rF": ("p2rF", 90, 120, 0),
     "t10": ("t10", 3.5, 5.5, 0), "sloos": ("sloos", -20, 50, 0), "cap": ("cap", 3, 0.5, 1),
     "vac": ("vac", 3, 7, 1), "vacChg": ("vacChg", -1, 1.5, 1), "vacRel": ("vacRel", -1.5, 2, 0), "vacRelChg": ("vacRelChg", -1, 1.5, 0),
     "rent": ("rent", 6, -4, 1), "rentCPI": ("rentCPI", 5, -1, 0), "pipe": ("permIdx", 60, 150, 0)}
# each part: slots of (measures in order of preference, weight)
PARTS = {"fin": [(["mort"], .7), (["mortChg"], .3)], "stress": [(["vixAvg", "vix"], .5), (["baa", "cr"], .25), (["ndx"], .25)],
         "jobs": [(["unemp"], .5), (["tech"], .3), (["pay"], .2)], "supply": [(["mos", "lpr"], .5), (["listYoY"], .25), (["priceCut"], .25)],
         "prices": [(["cs", "hp"], .5), (["hpSJ"], .5)], "value": [(["p2r", "p2rF"], .5), (["p2rSJ"], .5)],
         "mfin": [(["t10"], .6), (["sloos"], .2), (["baa", "cr"], .2)], "cushion": [(["cap", "p2r", "p2rF"], 1)],
         "vac": [(["vac", "vacRel"], .7), (["vacChg", "vacRelChg"], .3)], "rent": [(["rent"], .5), (["rentCPI"], .5)], "pipe": [(["pipe"], 1)]}
WEIGHTS = {"sf": {"fin": 20, "stress": 15, "jobs": 25, "supply": 15, "prices": 10, "value": 15},
           "mf": {"mfin": 25, "cushion": 15, "jobs": 20, "vac": 20, "rent": 10, "pipe": 10}}
USE_LICENSED = os.environ.get("USE_LICENSED_DATA", "1").strip() != "0"

def _measure(k, i, licensed):
    f, c, st, lic = M[k]
    if lic and not licensed: return None
    v = i.get(f)
    if k == "cap":
        if not (_ok(v) and _ok(i.get("t10"))): return None
        v = v - i["t10"]
    return lin(v, c, st) if _ok(v) else None

def part_score(k, i, licensed):
    t = w = 0.0
    for alts, wt in PARTS[k]:
        for a in alts:
            v = _measure(a, i, licensed)
            if v is not None: t += v * wt; w += wt; break
    return t / w if w else None

def score(lens, i, licensed):
    return round(_final([(part_score(k, i, licensed), w) for k, w in WEIGHTS[lens].items()]), 1)

def both_scores(i):
    return ({"sf": score("sf", i, True), "mf": score("mf", i, True)},
            {"sf": score("sf", i, False), "mf": score("mf", i, False)})

def band(v): return "Low" if v < 35 else "Moderate" if v < 45 else "Elevated" if v < 65 else "High"

# ---------------------------------------------------------------- helpers
def load(name, default):
    p = os.path.join(DATA, name)
    if not os.path.exists(p): return default
    with open(p, encoding="utf-8") as f: return json.load(f)

def save(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False); f.write("\n")

def http(url, timeout=30, tries=3):
    """GET with up to 3 tries (waits 5 s, then 20 s) so a brief outage at a data source doesn't lose the day."""
    import time
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            last = e
            if attempt < tries - 1: time.sleep(5 if attempt == 0 else 20)
    raise last

def fred(series_id, start):
    """FRED observations as [(date, value)], skipping blanks."""
    if not FRED_KEY: raise RuntimeError("FRED_API_KEY not set")
    q = urllib.parse.urlencode({"series_id": series_id, "api_key": FRED_KEY, "file_type": "json",
                                "observation_start": start})
    js = json.loads(http("https://api.stlouisfed.org/fred/series/observations?" + q))
    out = []
    for o in js.get("observations", []):
        if o["value"] not in (".", ""):
            out.append((date.fromisoformat(o["date"]), float(o["value"])))
    if not out: raise RuntimeError(f"no data for {series_id}")
    return out

def mon_d(d): return f"{d.strftime('%b')} {d.day}"
def month_label(d): return d.strftime("%B %Y")
def short_month(d): return d.strftime("%b %y")
def is_market_day(d): return d.weekday() < 5 and d.isoformat() not in HOLIDAYS

def last_expected_update(now):
    """The most recent market day whose evening update should already be saved (today after 5:30 PM PT)."""
    d = now.date()
    if not (is_market_day(d) and (now.hour, now.minute) >= (17, 30)):
        d -= timedelta(days=1)
        while not is_market_day(d): d -= timedelta(days=1)
    return d
def sgn(v, n): return f"{'+' if v >= 0 else '−'}{abs(v):.{n}f}"

def yoy_series(obs, n=21):
    """Year-over-year % for the last n months of a monthly series, with sparse labels."""
    by = {d.strftime("%Y-%m"): v for d, v in obs}
    rows = []
    for d, v in obs:
        prev = by.get(f"{d.year - 1}-{d.month:02d}")
        if prev: rows.append((d, (v / prev - 1) * 100))
    rows = rows[-n:]
    out = []
    for k, (d, v) in enumerate(rows):
        label = short_month(d) if (d.month in (1, 7) or k == len(rows) - 1 or k == 0) else ""
        out.append([label, round(v, 1)])
    return out, rows[-1] if rows else None

# ---------------------------------------------------------------- fetchers (each returns updates or raises)
def get_vix():
    try:
        text = http("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")
        rows = [r for r in csv.DictReader(io.StringIO(text)) if r.get("CLOSE")]
        obs = [(datetime.strptime(r["DATE"], "%m/%d/%Y").date(), float(r["CLOSE"])) for r in rows]
    except Exception as e:
        print("  VIX: Cboe unavailable, trying FRED:", e)
        obs = fred("VIXCLS", (date.today() - timedelta(days=90)).isoformat())
    obs = obs[-30:]
    (d, v), (pd, pv) = obs[-1], obs[-2]
    word = "Up" if v > pv else "Down" if v < pv else "Unchanged"
    detail = (f"{word} from {pv:.2f} the day before. The long-term average is about 19.5." if word != "Unchanged"
              else "Unchanged from the day before. The long-term average is about 19.5.")
    avg = sum(x for _, x in obs[-20:]) / len(obs[-20:])
    detail += f" The score uses the 20-market-day average ({avg:.1f}), so a one-day jump moves it less."
    return {"vix": round(v, 2), "vixAvg": round(avg, 2)}, {"vix": {"iso": d.isoformat(), "date": f"{mon_d(d)} close", "detail": detail,
                                          "series": [[mon_d(x), round(y, 2)] for x, y in obs]}}, pv

def get_t10():
    obs = []
    try:
        yr = datetime.now(PT).year
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{yr}/all?type=daily_treasury_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
        rows = list(csv.DictReader(io.StringIO(http(url))))
        obs = sorted((datetime.strptime(r["Date"], "%m/%d/%Y").date(), float(r["10 Yr"])) for r in rows if r.get("10 Yr"))
        if len(obs) < 2: raise RuntimeError("too few rows")
    except Exception as e:
        print("  10-year: Treasury unavailable, trying FRED:", e)
        obs = fred("DGS10", (date.today() - timedelta(days=90)).isoformat())
    obs = obs[-30:]
    (d, v), (pd, pv) = obs[-1], obs[-2]
    if abs(v - pv) < 0.005: detail = "About the same as the day before."
    else: detail = f"{'Up' if v > pv else 'Down'} from {pv:.2f}% the day before."
    return {"t10": round(v, 2)}, {"t10": {"iso": d.isoformat(), "date": f"{mon_d(d)} close", "detail": detail,
                                          "series": [[mon_d(x), round(y, 2)] for x, y in obs]}}

def get_mortgage():
    obs = fred("MORTGAGE30US", (date.today() - timedelta(days=420)).isoformat())
    d, v = obs[-1]
    target = d - timedelta(days=364)
    yd, yv = min(obs, key=lambda o: abs((o[0] - target).days))
    chg = v - yv
    recent = obs[-26:]
    peak = max(recent, key=lambda o: o[1])
    detail = f"A year ago it was {yv:.2f}%."
    if peak[0] != d and peak[1] - v >= 0.1: detail += f" Down from {peak[1]:.2f}% in {peak[0].strftime('%B')}."
    return ({"mort": round(v, 2), "mortChg": round(chg, 2)},
            {"mort": {"iso": d.isoformat(), "date": f"Week of {mon_d(d)}", "detail": detail,
                      "series": [[mon_d(x), round(y, 2)] for x, y in recent]}})

def get_unemployment():
    """Seasonally adjusted metro unemployment rates, weighted by each metro's labor force (model v3.1)."""
    start = (date.today() - timedelta(days=500)).isoformat()
    sf, sj = dict(fred("SANF806UR", start)), dict(fred("SANJ906UR", start))
    lf_sf, lf_sj = dict(fred("SANF806LF", start)), dict(fred("SANJ906LF", start))
    common = sorted(set(sf) & set(sj) & set(lf_sf) & set(lf_sj))
    d = common[-1]
    rate = (sf[d] * lf_sf[d] + sj[d] * lf_sj[d]) / (lf_sf[d] + lf_sj[d])
    us = None
    try: us = dict(fred("UNRATE", start)).get(d)
    except Exception: pass
    detail = f"Seasonally adjusted: San Francisco–Oakland area {sf[d]:.1f}%, San Jose area {sj[d]:.1f}%, weighted by the size of each labor force."
    if us is not None: detail += f" The U.S. rate is {us:.1f}%."
    upd, label = {"unemp": round(rate, 2)}, month_label(d)
    try:
        tsf, tsj = dict(fred("SANF806INFON", start)), dict(fred("SANJ906INFON", start))
        tc = sorted(set(tsf) & set(tsj))
        td = tc[-1]; prev = date(td.year - 1, td.month, 1)
        chg = ((tsf[td] + tsj[td]) / (tsf[prev] + tsj[prev]) - 1) * 100
        upd["techJobs"] = round(chg, 2)
        detail += f" Tech jobs are {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% from a year ago."
        label = f"{month_label(d)} (unemployment, all jobs) · {td.strftime('%B')} (tech jobs)"
    except Exception as e:
        print("  Tech jobs: kept previous value", e)
    return upd, {"unemp": {"iso": d.isoformat(), "date": label, "detail": detail}}

PERMIT_AVG = 33101   # average 24-month total of permits (SF + San Jose metros), 2005–2025
def get_permits(manual):
    start = (date.today() - timedelta(days=900)).isoformat()
    a, b = dict(fred("SANF806BPPRIV", start)), dict(fred("SANJ906BPPRIV", start))
    months = sorted(set(a) & set(b))
    if len(months) < 24: raise RuntimeError("not enough permit months")
    last24 = months[-24:]
    total = sum(a[m] + b[m] for m in last24)
    idx = total / PERMIT_AVG * 100
    d = last24[-1]
    detail = (f"{total:,.0f} homes were permitted in the San Francisco and San Jose metro areas over the past 24 months, "
              f"about {abs(idx - 100):.0f}% {'below' if idx < 100 else 'above'} the 2005–2025 average pace.")
    return {"permIdx": round(idx, 1)}, {"pipe": {"iso": d.isoformat(), "date": month_label(d), "detail": detail}}

FHFA = ("ATNHPIUS41884Q", "ATNHPIUS36084Q", "ATNHPIUS41940Q")   # SF–San Mateo, Oakland–Berkeley, San Jose (U.S. government data)
def fhfa_avg(start):
    series = [dict(fred(sid, start)) for sid in FHFA]
    common = sorted(set(series[0]) & set(series[1]) & set(series[2]))
    return [(d, sum(x[d] for x in series) / 3) for d in common]

def get_home_prices():
    obs = fhfa_avg((date.today() - timedelta(days=1400)).isoformat())
    by = dict(obs); rows = []
    for d, v in obs:
        p = by.get(date(d.year - 1, d.month, 1))
        if p: rows.append((d, (v / p - 1) * 100))
    rows = rows[-9:]
    q = lambda d: f"Q{(d.month + 2) // 3} {d.strftime('%y')}"
    series = [[q(d) if k in (0, len(rows) - 1) or d.month == 4 else "", round(v, 2)] for k, (d, v) in enumerate(rows)]
    d, v = rows[-1]
    detail = ("Average of the FHFA house price indexes for the San Francisco–San Mateo, Oakland–Berkeley and San Jose areas. "
              + ("Prices are higher than a year ago." if v >= 1 else "Prices are roughly flat compared with a year ago." if v > -1 else "Prices are lower than a year ago."))
    return {"hp": round(v, 2)}, {"hp": {"iso": d.isoformat(), "date": f"Q{(d.month + 2) // 3} {d.year}", "detail": detail, "series": series}}

P2R_SJ_AVG = 0.95438   # average of (FHFA San Jose ÷ SF-area rent index), 2005–2025
def get_san_jose_prices():
    """FHFA San Jose–Sunnyvale–Santa Clara: half of the home-price and price-to-rent parts (Case-Shiller San Francisco excludes Santa Clara County)."""
    start = (date.today() - timedelta(days=900)).isoformat()
    obs = fred("ATNHPIUS41940Q", start); by = dict(obs); d, v = obs[-1]
    p = by.get(date(d.year - 1, d.month, 1))
    if not p: raise RuntimeError("no year-ago value")
    chg = (v / p - 1) * 100
    rent = dict(fred("CUURA422SEHA", start)); rd = max(rent)
    p2r = v / rent[rd] / P2R_SJ_AVG * 100
    q = f"Q{(d.month + 2) // 3} {d.year}"
    return ({"hpSJ": round(chg, 2), "p2rSJ": round(p2r, 1)},
            {"hpSJ": {"iso": d.isoformat(), "date": q, "detail": f"FHFA index for the San Jose–Sunnyvale–Santa Clara area: {'up' if chg >= 0 else 'down'} {abs(chg):.1f}% from a year ago."},
             "valueSJ": {"iso": d.isoformat(), "date": f"{q} prices · {rd.strftime('%B %Y')} rents",
                         "detail": f"San Jose-area home prices compared with rents: about {abs(p2r - 100):.0f}% {'above' if p2r >= 100 else 'below'} normal."}})

def get_rent_and_vacancy():
    start = (date.today() - timedelta(days=900)).isoformat()
    r = fred("CUURA422SEHA", start); by = dict(r)
    d = max(k for k in by if date(k.year - 1, k.month, 1) in by)
    rent = (by[d] / by[date(d.year - 1, d.month, 1)] - 1) * 100
    upd = {"rentCPI": round(rent, 2)}
    sig = {"rentCPI": {"iso": d.isoformat(), "date": month_label(d),
                    "detail": "Rent paid by tenants in the San Francisco–Oakland area (consumer price index for rent). It covers all leases, so it moves more slowly than asking rents."}}
    try:
        v = fred("CARVAC", (date.today() - timedelta(days=1500)).isoformat())
        (d1, v1), (d0, v0) = v[-1], v[-2]
        upd.update({"vacRel": round(v1 - CA_VAC_AVG, 2), "vacRelChg": round(v1 - v0, 2)})
        sig["vacRel"] = {"iso": d1.isoformat(), "date": f"{d1.year} (yearly)",
                      "detail": f"California rental vacancy from the U.S. Census Bureau was {v1:.1f}%, about {abs(v1 - CA_VAC_AVG):.1f} points {'below' if v1 < CA_VAC_AVG else 'above'} its 2005–2025 average of {CA_VAC_AVG:.1f}%. It is statewide and yearly, so it changes slowly."}
    except Exception as e:
        print("  Vacancy: kept previous value", e)
    return upd, sig

def get_listings(manual=None):
    start = (date.today() - timedelta(days=1200)).isoformat()
    obs = fred("ACTLISCOU6085", start)
    series, last = yoy_series(obs)
    d, v = last
    act = dict(obs)
    upd = {"listYoY": round(v, 1)}
    parts = []
    try:
        pen = dict(fred("PENLISCOU6085", start)); pd_ = max(k for k in pen if k in act)
        upd["lpr"] = round(act[pd_] / pen[pd_], 2)
        parts.append(f"about {act[pd_] / pen[pd_]:.1f} active listings for every home under contract")
    except Exception as e:
        print("  Pending listings: kept previous value", e)
    parts.append(f"{abs(v):.0f}% {'more' if v >= 0 else 'fewer'} listings than a year ago")
    try:
        pc = dict(fred("PRIREDCOU6085", start)); pd_ = max(k for k in pc if k in act)
        share = pc[pd_] / act[pd_] * 100
        upd["priceCut"] = round(share, 1)
        parts.append(f"{share:.0f}% of listings with a price cut")
    except Exception as e:
        print("  Price cuts: kept previous value", e)
    detail = "Santa Clara County, from Realtor.com: " + ", ".join(parts[:-1]) + (", and " if len(parts) > 1 else "") + parts[-1] + "."
    return upd, {"supply": {"iso": d.isoformat(), "date": month_label(d), "detail": detail, "series": series}}

def get_credit():
    """High-grade corporate bond spread: U.S. Treasury HQM 10-year corporate yield minus the 10-year Treasury (both monthly, public domain)."""
    start = (date.today() - timedelta(days=500)).isoformat()
    h, g = dict(fred("HQMCB10YR", start)), dict(fred("GS10", start))
    d = max(k for k in h if k in g)
    v = h[d] - g[d]
    word = "near the low end of the past 20 years: lenders are comfortable" if v < 1.3 else "about normal" if v < 2.2 else "wide: lenders are cautious"
    return {"cr": round(v, 2)}, {"credit": {"iso": d.isoformat(), "date": month_label(d),
        "detail": f"High-grade company bonds paid {h[d]:.2f}%, or {v:.2f} percentage points more than the 10-year Treasury that month. That is {word}."}}

def get_payroll():
    start = (date.today() - timedelta(days=500)).isoformat()
    a, b = dict(fred("SANF806NAN", start)), dict(fred("SANJ906NAN", start))
    d = sorted(set(a) & set(b))[-1]; p = date(d.year - 1, d.month, 1)
    return {"payroll": round(((a[d] + b[d]) / (a[p] + b[p]) - 1) * 100, 2)}, {}

P2R_AVG = 0.92152   # average of (FHFA 3-area average ÷ SF-area rent index), 2005–2025
def get_price_to_rent():
    start = (date.today() - timedelta(days=900)).isoformat()
    px = fhfa_avg(start); d, v = px[-1]
    rent = dict(fred("CUURA422SEHA", start)); rd = max(rent)
    val = v / rent[rd] / P2R_AVG * 100
    if not 40 <= val <= 200: raise RuntimeError(f"price-to-rent {val:.1f} looks wrong")
    word = (f"about {val - 100:.0f}% above" if val >= 101 else f"about {100 - val:.0f}% below" if val <= 99 else "close to")
    return {"p2rF": round(val, 1)}, {"valueF": {"iso": d.isoformat(), "date": f"Q{(d.month + 2) // 3} {d.year} prices · {rd.strftime('%B %Y')} rents",
        "detail": f"Home prices compared with San Francisco-area rents are {word} their 2005–2025 average."}}

def get_sloos():
    obs = fred("SUBLPDRCSMLGNQ", (date.today() - timedelta(days=800)).isoformat()); d, v = obs[-1]
    words = (f"a net {abs(v):.0f}% of large banks said they eased" if v < 0 else f"a net {v:.0f}% of large banks said they tightened" if v > 0
             else "large banks said they left unchanged")
    return {"sloos": round(v, 1)}, {"sloos": {"iso": d.isoformat(), "date": f"{d.strftime('%B %Y')} survey",
        "detail": f"In the Federal Reserve's survey of bank loan officers, {words} their standards for apartment-building loans."}}

def get_insurance():
    obs = fred("CUUR0000SEHD", (date.today() - timedelta(days=500)).isoformat())
    by = dict(obs); d, v = obs[-1]; p = by.get(date(d.year - 1, d.month, 1))
    if not p: raise RuntimeError("no year-ago value")
    return {"ins": round((v / p - 1) * 100, 1)}, {"ins": {"iso": d.isoformat(), "date": month_label(d),
        "detail": "U.S. consumer price index for tenants' and household insurance."}}

# ---------------------------------------------------------------- licensed sources (non-commercial use, permission pending)
def get_baa():
    obs = fred("BAA10Y", (date.today() - timedelta(days=40)).isoformat()); d, v = obs[-1]
    word = "near the low end of the past 20 years: lenders are comfortable" if v < 1.9 else "about normal" if v < 2.6 else "wide: lenders are cautious"
    return {"baa": round(v, 2)}, {"baa": {"iso": d.isoformat(), "date": f"{mon_d(d)} close",
        "detail": f"Baa-rated company bonds paid {v:.2f} percentage points more than the 10-year Treasury (Moody's, via FRED). That is {word}."}}

def get_ndx():
    obs = fred("NASDAQ100", (date.today() - timedelta(days=380)).isoformat())
    d, v = obs[-1]; hd, hv = max(obs[-260:], key=lambda o: o[1])
    dd = max(0.0, (1 - v / hv) * 100)
    return {"ndxDD": round(dd, 1)}, {"ndx": {"iso": d.isoformat(), "date": f"{mon_d(d)} close",
        "detail": f"The Nasdaq-100 closed at {v:,.2f}, {dd:.1f}% below its 12-month high of {hv:,.2f} on {mon_d(hd)}." if dd >= 0.1 else f"The Nasdaq-100 closed at a 12-month high of {v:,.2f}."}}

P2R_CS_AVG = 0.60721   # average of (Case-Shiller SF ÷ SF-area rent index), 2005–2025
def get_case_shiller():
    start = (date.today() - timedelta(days=1000)).isoformat()
    obs = fred("SFXRSA", start)
    series, last = yoy_series(obs, 20)
    d, v = last
    rent = dict(fred("CUURA422SEHA", start)); rd = d if d in rent else max(rent)
    p2r = obs[-1][1] / rent[rd] / P2R_CS_AVG * 100
    word = (f"about {p2r - 100:.0f}% above" if p2r >= 101 else f"about {100 - p2r:.0f}% below" if p2r <= 99 else "close to")
    return ({"cs": round(v, 2), "p2r": round(p2r, 1)},
            {"cs": {"iso": d.isoformat(), "date": month_label(d), "series": series,
                    "detail": "S&P Cotality Case-Shiller San Francisco home price index (seasonally adjusted). " + ("Prices are higher than a year ago." if v >= 1 else "Prices are roughly flat compared with a year ago." if v > -1 else "Prices are lower than a year ago.")},
             "value": {"iso": d.isoformat(), "date": month_label(d),
                       "detail": f"Case-Shiller San Francisco home prices compared with San Francisco-area rents are {word} their 2005–2025 average."}})

def get_manual_licensed(manual):
    """CoStar (cap rate, vacancy, asking rents) and MLS months of supply, entered by hand from published reports."""
    lic = manual.get("licensed") or {}
    upd = {k: lic[k] for k in ("cap", "vac", "vacChg", "rent", "mos") if _ok(lic.get(k))}
    if not upd: raise RuntimeError("no hand-entered licensed figures")
    return upd, lic.get("signals", {})

CA_VAC_AVG = 5.096   # California rental vacancy, 2005–2025 average

WARN_COUNTIES = ("san francisco", "san mateo", "santa clara", "alameda", "contra costa")
def get_warn():
    """California WARN notices for the five core Bay Area counties, last 30 days (needs openpyxl)."""
    import openpyxl
    req = urllib.request.Request("https://edd.ca.gov/siteassets/files/jobs_and_training/warn/warn_report1.xlsx", headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r: blob = r.read()
    wb = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=True)
    cutoff = datetime.now(PT).date() - timedelta(days=30)
    notices = []
    for ws in wb.worksheets:
        rows = ws.iter_rows(values_only=True); hdr = None
        for row in rows:
            cells = [str(c or "").replace("\n", " ").strip().lower() for c in row]
            if hdr is None:
                if any("company" in c for c in cells) and any("county" in c for c in cells): hdr = cells
                continue
            def col(word):
                for k, h in enumerate(hdr):
                    if word in h: return row[k]
            county = str(col("county") or "").lower()
            if not any(c in county for c in WARN_COUNTIES): continue
            when = col("received") or col("notice")
            if isinstance(when, datetime): when = when.date()
            elif isinstance(when, str):
                try: when = datetime.strptime(when.strip()[:10], "%m/%d/%Y").date()
                except ValueError: continue
            if not isinstance(when, date) or when < cutoff: continue
            try: n = int(float(col("employees") or 0))
            except (TypeError, ValueError): n = 0
            notices.append((str(col("company") or "").strip(), n))
    by = {}
    for c, n in notices: by[c] = by.get(c, 0) + n
    top = sorted(by.items(), key=lambda kv: -kv[1])[:4]
    total = sum(by.values())
    today = datetime.now(PT).date()
    return {"workers": total, "notices": len(notices), "period": "Last 30 days",
            "date": f"{today.strftime('%b')} {today.day}, {today.year}", "iso": today.isoformat(),
            "top": [{"company": c, "workers": n} for c, n in top],
            "detail": ("No large layoff notices were filed in the last 30 days." if total == 0 else
                       f"{total:,} workers are covered by layoff notices filed in the last 30 days.")}

# ---------------------------------------------------------------- note
def write_note(inp, prev_inp, sf, mf, prev):
    parts = []
    dv = inp["vix"] - prev_inp.get("vix", inp["vix"])
    if dv >= 2: parts.append(f"Stock markets were more nervous today, and the VIX fear index rose to {inp['vix']:.1f}.")
    elif dv <= -2: parts.append(f"Stock markets calmed down today, and the VIX fear index fell to {inp['vix']:.1f}.")
    else: parts.append(f"Stock markets were fairly steady today, with the VIX fear index at {inp['vix']:.1f}.")
    cost = "so borrowing remains expensive" if inp["mort"] >= 6.5 or inp["t10"] >= 4.5 else "keeping borrowing costs manageable"
    parts.append(f"Mortgage rates are {inp['mort']:.2f}% and the 10-year Treasury yield closed at {inp['t10']:.2f}%, {cost}.")
    if prev:
        d1, d2 = sf - prev.get("score", sf), mf - prev.get("mfScore", mf)
        def mv(d): return "unchanged" if abs(d) < 0.5 else (f"up {d:.1f}" if d > 0 else f"down {abs(d):.1f}")
        parts.append(f"The single-family homes & condos score is {sf:.0f} ({band(sf)}, {mv(d1)}) and the apartment buildings (5+ units) score is {mf:.0f} ({band(mf)}, {mv(d2)}).")
    else:
        parts.append(f"The single-family homes & condos score is {sf:.0f} ({band(sf)}) and the apartment buildings (5+ units) score is {mf:.0f} ({band(mf)}).")
    return " ".join(parts)

# ---------------------------------------------------------------- main
def main():
    force, check = "--force" in sys.argv, "--check" in sys.argv
    now = datetime.now(PT); today = now.date()
    latest = load("latest.json", {})
    history = load("history.json", [])
    manual = load("manual.json", {})
    if "--verify" in sys.argv:
        # Freshness check used by the workflow after each run: fail loudly if the saved data is behind.
        exp = last_expected_update(now).isoformat(); have = latest.get("asOf", "")
        if have < exp:
            print(f"::error::Saved data is from {have or 'nothing'}, but data for {exp} should exist by now."); return 2
        print(f"Data is current (asOf {have}, expected {exp})."); return 0
    if not check and not force and not is_market_day(today):
        print(f"{today}: market closed – no update."); return 0
    if not check and not force and latest.get("asOf") == today.isoformat():
        print(f"{today}: already updated earlier today – nothing to do."); return 0

    inputs = dict(latest.get("inputs", {}))
    signals = dict(latest.get("signals", {}))
    prev_inputs = dict(inputs)

    changed, ok_sources = [], set()
    if not check:
        jobs = [("VIX", get_vix), ("10-year Treasury yield", get_t10), ("Mortgage rate", get_mortgage),
                ("Unemployment", get_unemployment), ("Home prices (FHFA)", get_home_prices), ("San Jose home prices (FHFA)", get_san_jose_prices),
                ("Listings", get_listings), ("Corporate bond spread", get_credit),
                ("All jobs", get_payroll), ("Price-to-rent", get_price_to_rent), ("Bank lending survey", get_sloos),
                ("Rent and vacancy", get_rent_and_vacancy), ("Insurance costs", get_insurance),
                ("Building permits", lambda: get_permits(manual)),
                ("Baa spread (licensed)", get_baa), ("Nasdaq-100 (licensed)", get_ndx),
                ("Case-Shiller (licensed)", get_case_shiller), ("CoStar and MLS (hand-entered)", lambda: get_manual_licensed(manual))]
        for name, fn in jobs:
            try:
                res = fn()
                upd, sig = res[0], res[1]
                for k, v in upd.items():
                    if inputs.get(k) != v: changed.append(k)
                    inputs[k] = v
                for k, v in sig.items(): signals[k] = {**signals.get(k, {}), **v}
                print(f"  {name}: ok {upd}")
                ok_sources.add(name)
            except Exception as e:
                print(f"  {name}: kept previous value ({e})")
        if not ({"VIX", "10-year Treasury yield"} & ok_sources):
            print("::error::Could not download today's VIX or 10-year Treasury yield. Nothing was saved, so the site keeps "
                  "showing the last good update and its date.")
            return 1

    # sanity checks: refuse obviously broken numbers
    limits = {"vix": (5, 150), "mort": (1, 20), "t10": (0, 20), "unemp": (0, 40), "cs": (-60, 60), "listYoY": (-90, 500),
              "techJobs": (-40, 40), "priceCut": (0, 100), "payroll": (-30, 30), "cr": (-1, 10), "p2r": (40, 200),
              "sloos": (-100, 100), "ins": (-30, 50), "permIdx": (5, 400), "lpr": (0.2, 40), "rent": (-30, 30), "vac": (0, 30),
              "hp": (-60, 60), "hpSJ": (-60, 60), "p2rSJ": (40, 200), "p2rF": (40, 200), "rentCPI": (-30, 30), "vacRel": (-10, 15), "vacRelChg": (-10, 10),
              "baa": (0, 10), "ndxDD": (0, 90), "mos": (0.1, 30), "cap": (1, 15), "vacChg": (-10, 10)}
    for k, (lo, hi) in limits.items():
        if k in inputs and not (lo <= inputs[k] <= hi):
            print(f"  {k}={inputs[k]} outside {lo}–{hi}; keeping previous {prev_inputs.get(k)}")
            inputs[k] = prev_inputs.get(k)

    need = ["vix", "mort", "mortChg", "unemp", "t10"]
    optional = ["vixAvg", "hpSJ", "p2rSJ", "techJobs", "payroll", "lpr", "listYoY", "priceCut", "hp", "cr", "p2rF", "sloos", "vacRel", "vacRelChg", "rentCPI", "permIdx", "ins",
                "baa", "ndxDD", "mos", "cs", "p2r", "cap", "vac", "vacChg", "rent"]   # missing ones are re-weighted
    missing = [k for k in need if not isinstance(inputs.get(k), (int, float))]
    keep = need + [k for k in optional if isinstance(inputs.get(k), (int, float))]
    if missing: print("Missing inputs, aborting:", missing); return 1

    warn = latest.get("warn")
    if not check:
        try: warn = get_warn(); print(f"  WARN: ok {warn['workers']} workers in {warn['notices']} notices")
        except Exception as e: print("  WARN: kept previous", e)
    lic, pub = both_scores(inputs)
    sf, mf = (lic if USE_LICENSED else pub)["sf"], (lic if USE_LICENSED else pub)["mf"]
    if check:
        print(f"Single-family {sf} ({band(sf)}), multifamily {mf} ({band(mf)}); licensed {lic}; public {pub}"); return 0

    stamp = today.isoformat()
    prev = next((h for h in sorted(history, key=lambda h: h["date"], reverse=True) if h["date"] < stamp), None)
    if prev and not USE_LICENSED and _ok(prev.get("pubScore")): prev = {**prev, "score": prev["pubScore"], "mfScore": prev.get("pubMf")}
    latest = {"asOf": stamp, "updatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "note": write_note(inputs, prev["inputs"] if prev else prev_inputs, sf, mf, prev),
              "model": "v5.2", "licensedData": USE_LICENSED, "inputs": {k: inputs[k] for k in keep}, "signals": signals,
              "score": lic["sf"], "mfScore": lic["mf"], "pubScore": pub["sf"], "pubMf": pub["mf"]}
    if warn: latest["warn"] = warn
    history = [h for h in history if h.get("date") != stamp] + [{"date": stamp, "score": lic["sf"], "mfScore": lic["mf"],
                                                                 "pubScore": pub["sf"], "pubMf": pub["mf"], "model": "v5.2",
                                                                 "inputs": {k: inputs[k] for k in keep}}]
    history.sort(key=lambda h: h["date"])
    save("latest.json", latest); save("history.json", history)
    if prev:
        for name, now_v, was in (("single-family", sf, prev.get("score")), ("multifamily", mf, prev.get("mfScore"))):
            if isinstance(was, (int, float)) and abs(now_v - was) >= 12:
                print(f"::warning::{name} score moved {now_v - was:+.1f} points in one update; check the inputs for a bad download.")
    flag = ""
    if prev and (band(sf) != band(prev["score"]) or band(mf) != band(prev.get("mfScore", mf))): flag = "BAND CHANGE: "
    print(f"{flag}{stamp}: single-family {sf} ({band(sf)}), multifamily {mf} ({band(mf)}); changed: {', '.join(changed) or 'none'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
