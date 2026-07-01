#!/usr/bin/env python3
"""
SOXX Regime Dashboard — daily data updater.

Two data layers:
  1. PRICE LAYER (always free, no key): SOXX daily closes from Stooq.
     Drives the realized-vol regime trigger — the risk-on / derisk decision.
  2. OPTIONS LAYER (optional, paid feed): implied vol, put/call OI, skew.
     Enriches context only. Enabled when an API token env var is present.

The script MERGES fresh data onto the existing data/regime.json so the full
2026 history (including seed options fields) is preserved. If the options
feed is unavailable, the last known options values are carried forward and
flagged stale — the price-based signal keeps updating regardless.

Run: python scripts/update_data.py
Env:
  OPTIONS_PROVIDER = "orats" | "polygon" | "none"   (default: none)
  ORATS_TOKEN      = <your ORATS Data API token>
  POLYGON_API_KEY  = <your Polygon.io key>
"""
import os, json, io, sys, datetime as dt
from urllib.request import urlopen, Request
import numpy as np
import pandas as pd

TICKER = "SOXX"
THRESHOLD = 60.0          # 20d annualized realized vol (%) that flips to DERISK
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "..", "data", "regime.json")


# --------------------------------------------------------------------------
# 1. PRICE LAYER — free, no API key
# --------------------------------------------------------------------------
def fetch_prices_yahoo(ticker=TICKER):
    """Daily closes from Yahoo Finance chart API (free, no key)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range=2y&interval=1d")
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
    data = json.loads(raw)
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({"date": pd.to_datetime(ts, unit="s").normalize(), "soxx": closes})
    df = df.dropna(subset=["soxx"]).drop_duplicates("date")
    return df.sort_values("date").reset_index(drop=True)


def fetch_prices_stooq(ticker=TICKER):
    """Fallback: daily closes from Stooq CSV (free, no key)."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode()
    if "Date" not in raw:
        raise RuntimeError(f"Stooq returned no data for {ticker}: {raw[:120]}")
    px = pd.read_csv(io.StringIO(raw))
    px["date"] = pd.to_datetime(px["Date"])
    return px[["date", "Close"]].rename(columns={"Close": "soxx"}).sort_values("date").reset_index(drop=True)


def fetch_prices(ticker=TICKER):
    """Try Yahoo first, fall back to Stooq."""
    try:
        return fetch_prices_yahoo(ticker)
    except Exception as e:
        print(f"[warn] Yahoo price feed failed ({e}); trying Stooq", file=sys.stderr)
        return fetch_prices_stooq(ticker)


def compute_regime(df):
    """Add realized-regime metrics to a DataFrame that has date, soxx."""
    df = df.sort_values("date").reset_index(drop=True)
    df["ret"] = df["soxx"].pct_change()
    df["rvol20"] = df["ret"].rolling(20).std() * np.sqrt(252) * 100
    df["bigday"] = (df["ret"].abs() >= 0.03).rolling(20).mean() * 100
    df["peak"] = df["soxx"].cummax()
    df["dd"] = (df["soxx"] / df["peak"] - 1) * 100
    df["autocorr20"] = df["ret"].rolling(20).apply(
        lambda s: pd.Series(s).autocorr(1), raw=False)
    return df


# --------------------------------------------------------------------------
# 2. OPTIONS LAYER — optional paid feed
# --------------------------------------------------------------------------
def fetch_options_orats():
    """ORATS Data API summaries. Returns dict of the latest options metrics."""
    token = os.environ.get("ORATS_TOKEN")
    if not token:
        return None
    url = f"https://api.orats.io/datav2/summaries?token={token}&ticker={TICKER}"
    data = json.loads(urlopen(Request(url, headers={"User-Agent": "py"}), timeout=30).read())
    rows = data.get("data") or []
    if not rows:
        return None
    r = rows[0]
    # ORATS field names; guard each in case the plan omits some.
    iv = r.get("iv30d") or r.get("pxAtmIv") or r.get("orIvXmon")
    c_oi, p_oi = r.get("cOi"), r.get("pOi")
    pcoi = (p_oi / c_oi) if (c_oi and p_oi) else None
    skew = r.get("slope") or r.get("skewing")
    return {"iv": _num(iv), "pcoi": _num(pcoi), "skew": _num(skew)}


def fetch_options_polygon():
    """Polygon.io options snapshot. Aggregates ATM IV and put/call OI."""
    key = os.environ.get("POLYGON_API_KEY")
    if not key:
        return None
    url = (f"https://api.polygon.io/v3/snapshot/options/{TICKER}"
           f"?limit=250&apiKey={key}")
    data = json.loads(urlopen(Request(url, headers={"User-Agent": "py"}), timeout=30).read())
    results = data.get("results") or []
    if not results:
        return None
    ivs, call_oi, put_oi = [], 0, 0
    for c in results:
        det = c.get("details", {})
        oi = c.get("open_interest") or 0
        iv = c.get("implied_volatility")
        if det.get("contract_type") == "call":
            call_oi += oi
        elif det.get("contract_type") == "put":
            put_oi += oi
        if iv:
            ivs.append(iv)
    iv = (float(np.median(ivs)) * 100) if ivs else None
    pcoi = (put_oi / call_oi) if call_oi else None
    return {"iv": _num(iv), "pcoi": _num(pcoi), "skew": None}


def fetch_options():
    provider = os.environ.get("OPTIONS_PROVIDER", "none").lower()
    try:
        if provider == "orats":
            return fetch_options_orats()
        if provider == "polygon":
            return fetch_options_polygon()
    except Exception as e:
        print(f"[warn] options feed '{provider}' failed: {e}", file=sys.stderr)
    return None


def _num(v):
    try:
        return None if v is None else round(float(v), 4)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 3. MERGE, DERIVE, WRITE
# --------------------------------------------------------------------------
def cum(rets):
    return float((np.prod(1 + np.asarray(rets)) - 1) * 100)


def load_existing():
    with open(DATA_PATH) as f:
        return json.load(f)


def build_series(price_df, existing_series, latest_options):
    """Merge price metrics with existing per-day options fields; append today's."""
    prev = {row["date"]: row for row in existing_series}
    out = []
    for _, r in price_df.iterrows():
        d = r["date"].strftime("%Y-%m-%d")
        old = prev.get(d, {})
        row = {
            "date": d,
            "soxx": _num(r["soxx"]),
            "ret": _num(r["ret"]),
            "rvol20": _num(r["rvol20"]),
            "bigday": _num(r["bigday"]),
            "dd": _num(r["dd"]),
            "autocorr20": _num(r["autocorr20"]),
            # options fields: keep historical seed values; only the newest row
            # gets a fresh options reading if the feed provided one.
            "iv": old.get("iv"),
            "pcoi": old.get("pcoi"),
            "skew": old.get("skew"),
            "iv_pct": old.get("iv_pct"),
        }
        row["rv_minus_iv"] = (_num(row["rvol20"] - row["iv"])
                              if (row["rvol20"] is not None and row["iv"] is not None) else None)
        row["risk_off"] = bool(r["rvol20"] > THRESHOLD) if pd.notna(r["rvol20"]) else False
        out.append(row)
    # apply fresh options reading to the last row
    if latest_options and out:
        last = out[-1]
        for k in ("iv", "pcoi", "skew"):
            if latest_options.get(k) is not None:
                last[k] = latest_options[k]
        if last["rvol20"] is not None and last["iv"] is not None:
            last["rv_minus_iv"] = _num(last["rvol20"] - last["iv"])
    return out


def build_payload(series, options_live):
    df = pd.DataFrame(series)
    df["date"] = pd.to_datetime(df["date"])
    df["mon"] = df["date"].dt.strftime("%Y-%m")
    last = df.iloc[-1]
    off = df[df["rvol20"] > THRESHOLD]
    first = off.iloc[0] if len(off) else None
    signal = "DERISK" if (last["rvol20"] and last["rvol20"] > THRESHOLD) else "RISK-ON"

    months = []
    for m, g in df.groupby("mon"):
        rv = g["rvol20"].dropna()
        months.append({
            "month": m,
            "rvol_min": round(float(rv.min()), 0) if len(rv) else None,
            "rvol_max": round(float(rv.max()), 0) if len(rv) else None,
            "max_dd": round(float(g["dd"].min()), 1),
            "bigday_end": round(float(g["bigday"].dropna().iloc[-1]), 0) if g["bigday"].notna().any() else None,
            "ret": round(cum(g["ret"].dropna().values), 1),
            "regime": "DERISK" if (g["rvol20"] > THRESHOLD).any() else "RISK-ON",
        })

    # Counterfactuals from Feb 1 (needs iv_pct history; skip if absent)
    feb = df[df["date"] >= pd.Timestamp("2026-02-01")].copy()
    cf = {}
    if len(feb):
        feb["rv_flat"] = (feb["rvol20"].shift(1) > THRESHOLD).fillna(False)
        cf["hold"] = round(cum(feb["ret"].fillna(0).values), 1)
        cf["rv_rule"] = round(cum(np.where(feb["rv_flat"], 0.0, feb["ret"].fillna(0))), 1)
        cf["rv_flat_days"] = int(feb["rv_flat"].sum())
        if feb["iv_pct"].notna().any():
            feb["iv_flat"] = (feb["iv_pct"].shift(1) >= 90).fillna(False)
            cf["iv_rule"] = round(cum(np.where(feb["iv_flat"], 0.0, feb["ret"].fillna(0))), 1)
            cf["iv_flat_days"] = int(feb["iv_flat"].sum())

    clean_series = []
    for row in df.assign(date=df["date"].dt.strftime("%Y-%m-%d")).to_dict("records"):
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, float) and np.isnan(v):
                clean_row[k] = None
            else:
                clean_row[k] = v
        clean_series.append(clean_row)

    return {
        "meta": {
            "ticker": TICKER,
            "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "threshold": THRESHOLD,
            "data_source": "price feed (Yahoo/Stooq)" + (" + live options" if options_live else " (options: historical)"),
            "options_live": bool(options_live),
        },
        "signal": {
            "state": signal,
            "as_of": last["date"].strftime("%Y-%m-%d"),
            "soxx": round(float(last["soxx"]), 2),
            "rvol20": round(float(last["rvol20"]), 1) if pd.notna(last["rvol20"]) else None,
            "iv": round(float(last["iv"]), 1) if pd.notna(last["iv"]) else None,
            "bigday": round(float(last["bigday"]), 0) if pd.notna(last["bigday"]) else None,
            "autocorr20": round(float(last["autocorr20"]), 2) if pd.notna(last["autocorr20"]) else None,
            "drawdown": round(float(last["dd"]), 1) if pd.notna(last["dd"]) else None,
            "variance_premium": round(float(last["rvol20"] - last["iv"]), 1) if (pd.notna(last["rvol20"]) and pd.notna(last["iv"])) else None,
            "first_trigger_date": first["date"].strftime("%Y-%m-%d") if first is not None else None,
            "first_trigger_price": round(float(first["soxx"]), 2) if first is not None else None,
        },
        "counterfactuals": cf,
        "months": months,
        "series": clean_series,
    }


def main():
    existing = load_existing()
    try:
        px = fetch_prices()
        px = compute_regime(px)
        # keep only 2026+ to match the analysis window
        px = px[px["date"] >= pd.Timestamp("2026-01-01")].reset_index(drop=True)
        options_live = fetch_options()
        series = build_series(px, existing["series"], options_live)
        payload = build_payload(series, options_live)
        with open(DATA_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[ok] updated {DATA_PATH}: {len(series)} rows, "
              f"signal={payload['signal']['state']} rvol={payload['signal']['rvol20']} "
              f"options_live={bool(options_live)}")
    except Exception as e:
        print(f"[error] update failed, leaving existing data.json unchanged: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
