#!/usr/bin/env python3
"""
SOXX Regime Dashboard — daily data updater.

Two data layers:
  1. PRICE LAYER (always free, no key): SOXX daily closes from Yahoo/Stooq.
     Drives the realized-vol regime trigger — the risk-on / derisk decision.
  2. OPTIONS LAYER (optional, paid feed): implied vol, put/call OI, skew.
     Enriches context only. Enabled when an API token env var is present.

Additional auto-fetched (free):
  - Basket: SOXL, DRAM, RAM, EWY, KORU, MU — YTD + Jun 1 returns via Yahoo
  - Macro: ^MOVE (bond vol), ^TNX (10Y yield) via Yahoo
  - VIXEQ from CBOE public CSV
  - MU 20-day realized vol (confirmer for DRAM health)

Static / manually-updated (in data/catalysts.json):
  - Catalyst calendar with outcome fields
  - Dealer gamma, P/C OI percentile, HMM stress prob, constituent correlation
  - DDR4 spot price, TSMC cumulative revenue

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
CATALYST_PATH = os.path.join(HERE, "..", "data", "catalysts.json")

BASKET_TICKERS = ["SOXL", "DRAM", "RAM", "EWY", "KORU", "MU"]

# --------------------------------------------------------------------------
# 1. PRICE LAYER — free, no API key
# --------------------------------------------------------------------------
def fetch_prices_yahoo(ticker):
    """Daily closes from Yahoo Finance chart API (free, no key)."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
           f"?range=2y&interval=1d")
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
    data = json.loads(raw)
    res = data["chart"]["result"][0]
    ts = res["timestamp"]
    closes = res["indicators"]["quote"][0]["close"]
    df = pd.DataFrame({"date": pd.to_datetime(ts, unit="s").normalize(), "close": closes})
    df = df.dropna(subset=["close"]).drop_duplicates("date")
    return df.sort_values("date").reset_index(drop=True)


def fetch_prices_stooq(ticker=TICKER):
    """Fallback: daily closes from Stooq CSV (free, no key)."""
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    raw = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read().decode()
    if "Date" not in raw:
        raise RuntimeError(f"Stooq returned no data for {ticker}: {raw[:120]}")
    px = pd.read_csv(io.StringIO(raw))
    px["date"] = pd.to_datetime(px["Date"])
    return px[["date", "Close"]].rename(columns={"Close": "close"}).sort_values("date").reset_index(drop=True)


def fetch_soxx_prices():
    """Try Yahoo first, fall back to Stooq."""
    try:
        return fetch_prices_yahoo(TICKER)
    except Exception as e:
        print(f"[warn] Yahoo price feed failed ({e}); trying Stooq", file=sys.stderr)
        df = fetch_prices_stooq(TICKER)
        return df


def compute_regime(df):
    """Add realized-regime metrics to a DataFrame that has date, close."""
    df = df.sort_values("date").reset_index(drop=True)
    df = df.rename(columns={"close": "soxx"})
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
    token = os.environ.get("ORATS_TOKEN")
    if not token:
        return None
    url = f"https://api.orats.io/datav2/summaries?token={token}&ticker={TICKER}"
    data = json.loads(urlopen(Request(url, headers={"User-Agent": "py"}), timeout=30).read())
    rows = data.get("data") or []
    if not rows:
        return None
    r = rows[0]
    iv = r.get("iv30d") or r.get("pxAtmIv") or r.get("orIvXmon")
    c_oi, p_oi = r.get("cOi"), r.get("pOi")
    pcoi = (p_oi / c_oi) if (c_oi and p_oi) else None
    skew = r.get("slope") or r.get("skewing")
    return {"iv": _num(iv), "pcoi": _num(pcoi), "skew": _num(skew)}


def fetch_options_polygon():
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


# --------------------------------------------------------------------------
# 3. BASKET + MACRO (free, Yahoo Finance)
# --------------------------------------------------------------------------
def _ret_pct(price_now, price_ref):
    if price_ref and price_now:
        return round((price_now / price_ref - 1) * 100, 1)
    return None


def fetch_basket():
    """Fetch basket tickers from Yahoo; compute YTD and Jun-1 returns."""
    results = []
    ytd_ref_date = pd.Timestamp("2026-01-02")
    jun1_ref_date = pd.Timestamp("2026-06-01")

    for ticker in BASKET_TICKERS:
        try:
            df = fetch_prices_yahoo(ticker)
            df = df[df["date"] >= pd.Timestamp("2026-01-01")].reset_index(drop=True)
            if len(df) == 0:
                continue
            last = df.iloc[-1]
            price = _num(last["close"])

            # YTD reference: first trading day on or after Jan 2
            ytd_row = df[df["date"] >= ytd_ref_date]
            ytd_price = _num(ytd_row.iloc[0]["close"]) if len(ytd_row) else None
            ytd_date = ytd_row.iloc[0]["date"].strftime("%Y-%m-%d") if len(ytd_row) else None

            # Jun 1 reference: last close on or before Jun 1
            jun1_row = df[df["date"] <= jun1_ref_date]
            jun1_price = _num(jun1_row.iloc[-1]["close"]) if len(jun1_row) else None
            jun1_date = jun1_row.iloc[-1]["date"].strftime("%Y-%m-%d") if len(jun1_row) else None

            # ATH in 2026
            ath_idx = df["close"].idxmax()
            ath_price = _num(df.loc[ath_idx, "close"])
            ath_date = df.loc[ath_idx, "date"].strftime("%Y-%m-%d")
            ath_dd = _ret_pct(price, ath_price)

            results.append({
                "ticker": ticker,
                "price": price,
                "as_of": last["date"].strftime("%Y-%m-%d"),
                "ytd_ret": _ret_pct(price, ytd_price),
                "ytd_start": ytd_price,
                "ytd_start_date": ytd_date,
                "jun1_ret": _ret_pct(price, jun1_price),
                "jun1_start": jun1_price,
                "jun1_start_date": jun1_date,
                "ath": ath_price,
                "ath_date": ath_date,
                "ath_dd": ath_dd,
            })
        except Exception as e:
            print(f"[warn] basket fetch failed for {ticker}: {e}", file=sys.stderr)

    return results


def fetch_macro():
    """Fetch ^MOVE, ^TNX via Yahoo."""
    result = {}
    for ticker, key in [("^MOVE", "move"), ("^TNX", "tnx")]:
        try:
            df = fetch_prices_yahoo(ticker)
            if len(df):
                result[key] = _num(df.iloc[-1]["close"])
                result[key + "_date"] = df.iloc[-1]["date"].strftime("%Y-%m-%d")
        except Exception as e:
            print(f"[warn] macro fetch failed for {ticker}: {e}", file=sys.stderr)
    return result


def fetch_vixeq():
    """Fetch VIXEQ from CBOE public CSV."""
    try:
        url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIXEQ_History.csv"
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cboe.com/us/indices/dispersion/"
        }
        raw = urlopen(Request(url, headers=headers), timeout=30).read().decode()
        df = pd.read_csv(io.StringIO(raw))
        df.columns = df.columns.str.strip()
        date_col = next(c for c in df.columns if "date" in c.lower())
        close_col = next(c for c in df.columns if "close" in c.lower())
        df[date_col] = pd.to_datetime(df[date_col])
        df = df.sort_values(date_col)
        last = df.iloc[-1]
        return {
            "vixeq": _num(last[close_col]),
            "vixeq_date": last[date_col].strftime("%Y-%m-%d")
        }
    except Exception as e:
        print(f"[warn] VIXEQ fetch failed: {e}", file=sys.stderr)
        return {}


def fetch_mu_rvol():
    """Fetch MU prices and compute 20-day realized vol."""
    try:
        df = fetch_prices_yahoo("MU")
        df = df[df["date"] >= pd.Timestamp("2026-01-01")].reset_index(drop=True)
        df["ret"] = df["close"].pct_change()
        df["rvol20"] = df["ret"].rolling(20).std() * np.sqrt(252) * 100
        last = df.iloc[-1]
        return {
            "mu_rvol20": round(float(last["rvol20"]), 1) if pd.notna(last["rvol20"]) else None,
            "mu_price": _num(last["close"]),
            "mu_date": last["date"].strftime("%Y-%m-%d"),
        }
    except Exception as e:
        print(f"[warn] MU vol fetch failed: {e}", file=sys.stderr)
        return {}


def load_catalysts():
    """Load catalyst calendar and static gauges from data/catalysts.json."""
    try:
        with open(CATALYST_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] catalysts.json load failed: {e}", file=sys.stderr)
        return {"events": [], "static_gauges": {}}


# --------------------------------------------------------------------------
# 4. MERGE, DERIVE, WRITE
# --------------------------------------------------------------------------
def _num(v):
    try:
        return None if v is None else round(float(v), 4)
    except (TypeError, ValueError):
        return None


def cum(rets):
    return float((np.prod(1 + np.asarray(rets)) - 1) * 100)


def load_existing():
    with open(DATA_PATH) as f:
        return json.load(f)


def build_series(price_df, existing_series, latest_options):
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
            "iv": old.get("iv"),
            "pcoi": old.get("pcoi"),
            "skew": old.get("skew"),
            "iv_pct": old.get("iv_pct"),
        }
        row["rv_minus_iv"] = (_num(row["rvol20"] - row["iv"])
                              if (row["rvol20"] is not None and row["iv"] is not None) else None)
        row["risk_off"] = bool(r["rvol20"] > THRESHOLD) if pd.notna(r["rvol20"]) else False
        out.append(row)
    if latest_options and out:
        last = out[-1]
        for k in ("iv", "pcoi", "skew"):
            if latest_options.get(k) is not None:
                last[k] = latest_options[k]
        if last["rvol20"] is not None and last["iv"] is not None:
            last["rv_minus_iv"] = _num(last["rvol20"] - last["iv"])
    return out


def build_payload(series, options_live, basket, macro, vixeq_data, mu_data, catalysts_data):
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

    # Build live gauges: merge static (from catalysts.json) with auto-fetched
    static_gauges = catalysts_data.get("static_gauges", {})
    gauges = {
        **static_gauges,
        **mu_data,
        **macro_data_merge(macro, vixeq_data),
    }

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
        "basket": basket,
        "gauges": gauges,
        "catalysts": catalysts_data.get("events", []),
        "series": clean_series,
    }


def macro_data_merge(macro, vixeq_data):
    return {**macro, **vixeq_data}


def main():
    existing = load_existing()
    try:
        # Core SOXX price + regime metrics
        px = fetch_soxx_prices()
        px = compute_regime(px)
        px = px[px["date"] >= pd.Timestamp("2026-01-01")].reset_index(drop=True)
        options_live = fetch_options()
        series = build_series(px, existing.get("series", []), options_live)

        # Basket + macro + catalyst data
        basket = fetch_basket()
        macro = fetch_macro()
        vixeq_data = fetch_vixeq()
        mu_data = fetch_mu_rvol()
        catalysts_data = load_catalysts()

        payload = build_payload(series, options_live, basket, macro, vixeq_data, mu_data, catalysts_data)

        with open(DATA_PATH, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"[ok] updated {DATA_PATH}: {len(series)} rows, "
              f"signal={payload['signal']['state']} rvol={payload['signal']['rvol20']} "
              f"basket={len(basket)} tickers options_live={bool(options_live)}")
    except Exception as e:
        print(f"[error] update failed, leaving existing data unchanged: {e}", file=sys.stderr)
        import traceback; traceback.print_exc(file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
