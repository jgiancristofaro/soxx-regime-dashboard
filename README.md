# SOXX Regime Dashboard

An interactive dashboard that decides **risk-on vs. derisk** on SOXX using the *realized tape*, not what options fear. The signal is the 20-day annualized realized volatility versus a 60% trigger — a level SOXX never touched Jan–May 2026 and first crossed on **June 5, 2026**.

**Live signal · SOXX price + realized-vol / implied-vol chart · Jan–May vs. June comparison · Feb-1 counterfactuals · month-by-month regime table.**

Price data refreshes automatically every trading day (free, no key). Implied-vol / put-call / skew refresh from a paid options feed when you supply an API key; otherwise they display from the historical record and the price-based signal keeps updating.

---

## What's in here

```
index.html                 # the dashboard (static, reads data/regime.json)
data/regime.json           # the data the page renders (auto-refreshed daily)
scripts/update_data.py      # fetches prices + options, recomputes metrics
requirements.txt
.github/workflows/update.yml # daily cron: refresh data + deploy to Pages
```

---

## Deploy it (public repo + GitHub Pages) — ~5 minutes

You'll create a **public** GitHub repo (GitHub Pages is free on public repos) and let a scheduled Action rebuild it daily.

### 1. Create the repo and push

Install [Git](https://git-scm.com/) and the [GitHub CLI](https://cli.github.com/) if you don't have them, then from this folder:

```bash
cd soxx-regime-dashboard
git init -b main
git add .
git commit -m "Initial SOXX regime dashboard"

# Create a PUBLIC repo and push (opens a browser to log in the first time):
gh repo create soxx-regime-dashboard --public --source=. --remote=origin --push
```

> No `gh` CLI? Create the repo manually at github.com/new (name it `soxx-regime-dashboard`, **Public**), then:
> ```bash
> git remote add origin https://github.com/<YOUR_USERNAME>/soxx-regime-dashboard.git
> git push -u origin main
> ```

### 2. Turn on GitHub Pages

Repo → **Settings → Pages** → under *Build and deployment*, set **Source = GitHub Actions**. That's it — the included workflow deploys the site.

Your public URL will be:
```
https://<YOUR_USERNAME>.github.io/soxx-regime-dashboard/
```

### 3. First build

Repo → **Actions** tab → select *"Update regime data & deploy"* → **Run workflow**. After it finishes (~1 min), open the URL above. Thereafter it runs itself at **22:30 UTC on weekdays** (~30 min after the US close) and on every push.

---

## Live options data (optional, paid)

The **risk-on/derisk signal is fully price-based** and needs no key. To also refresh implied vol, put/call OI, and skew each day, plug in one options feed:

### Option A — ORATS (simplest for these exact metrics)

1. Get a Data API token from [orats.com](https://orats.com/).
2. Repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** tab → *New repository secret* → name `ORATS_TOKEN`, value = your token.
   - **Variables** tab → *New repository variable* → name `OPTIONS_PROVIDER`, value `orats`.

### Option B — Polygon.io

1. Get an Options API key from [polygon.io](https://polygon.io/) (options snapshot requires a paid tier).
2. Repo → **Settings → Secrets and variables → Actions**:
   - **Secrets** → `POLYGON_API_KEY` = your key.
   - **Variables** → `OPTIONS_PROVIDER` = `polygon`.

Leave `OPTIONS_PROVIDER` unset (or `none`) to run price-only. The dashboard shows an **"options: historical"** badge when the feed is off so you always know whether the implied-side numbers are live.

> Note: cross-sectional *dispersion* (across the 30 SOXX constituents) isn't part of the free/standard options endpoints and isn't auto-refreshed; it's shown from the historical analysis. The signal doesn't depend on it.

---

## Run locally

```bash
pip install -r requirements.txt
python scripts/update_data.py          # refreshes data/regime.json from Stooq
python -m http.server 8000             # then open http://localhost:8000
```

---

## How the signal works

| Layer | Source | Drives |
|---|---|---|
| **Realized tape** | free daily closes (Stooq) | the risk-on/derisk **decision** — 20d realized vol vs 60%, big-day frequency, drawdown, autocorrelation |
| **Implied metrics** | paid options feed (optional) | **context** — sizes tail-risk expectations, does *not* flip the signal |

The framework: *let implied metrics size your tail-risk expectations, but let the realized tape decide risk-on vs. derisk.*

**Caveats:** the 60% threshold was chosen in-sample (trust the dimension, not the exact number); this is n=1 on the 2026 phase change; the rule accepts moderate drawdowns (it rode through March's −12% dip) as the price of staying in trends. Not investment advice.
