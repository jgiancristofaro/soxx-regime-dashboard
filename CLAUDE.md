# SOXX Regime Dashboard — Claude Instructions

## Project overview

A static, single-page dashboard that produces a **risk-on / derisk signal** for SOXX (iShares Semiconductor ETF) based on the *realized tape*, not implied/options fear. The signal is the 20-day annualized realized volatility vs. a 60% trigger level.

**Live URL:** https://jgiancristofaro.github.io/soxx-regime-dashboard/
**GitHub repo:** https://github.com/jgiancristofaro/soxx-regime-dashboard

## File map

```
index.html                   # the entire dashboard UI (static, reads data/regime.json at runtime)
data/regime.json             # all data the page renders — auto-refreshed daily by CI
data/catalysts.json          # MANUALLY maintained: catalyst events + static gauge values
scripts/update_data.py       # fetches prices (Yahoo/Stooq) + macro data, writes regime.json
requirements.txt             # Python deps for the update script
.github/workflows/update.yml # GitHub Actions: runs update_data.py daily + deploys to Pages
```

No build step. No npm. No framework. `index.html` is served directly as a static file.

## Data architecture (as of July 2026)

`regime.json` has five top-level sections, all consumed by `render()` in `index.html`:

| Section | Source | Refresh |
|---|---|---|
| `signal` | `update_data.py` (SOXX closes from Stooq) | Daily CI cron |
| `history` | `update_data.py` | Daily CI cron |
| `basket` | `update_data.py` (Yahoo Finance: SOXL, DRAM, RAM, EWY, KORU, MU) | Daily CI cron |
| `gauges` | Merged: `update_data.py` (MU RVol, MOVE, TNX, VIXEQ) + `static_gauges` from `catalysts.json` | Daily CI + manual |
| `catalysts` | `update_data.py` reads `data/catalysts.json` directly | Manual only |

### Manually maintained data (`data/catalysts.json`)

Edit this file to:
- **Update event outcomes** — set `"outcome": "bull"`, `"bear"`, or `"neutral"`, and fill `outcome_date` + `outcome_note` as events resolve
- **Update static gauges** — update `static_gauges` fields (SOXX/EWY dealer gamma, P/C OI, HMM stress, constituent correlation, DDR4 spot price, TSMC cumulative revenue) whenever you have fresh data from Barchart, TrendForce, etc.

After editing `catalysts.json`, run `python3 scripts/update_data.py` locally and commit both files.

## Architecture decisions

- **Pure static site** — `index.html` fetches `data/regime.json` at runtime via `fetch()`. All rendering is vanilla JS + Chart.js (CDN). No bundler, no React.
- **Data refresh in CI** — `update_data.py` runs on a cron (22:30 UTC weekdays, ~30 min after US close). It commits updated `data/regime.json` back to `main`, which triggers a redeploy.
- **Signal is price-only** — the risk-on/derisk decision uses only free daily closes from Stooq. Options metrics (IV, put/call, skew) are optional, controlled by `OPTIONS_PROVIDER` env var.
- **No API key needed to run** — `OPTIONS_PROVIDER=none` (default) keeps the signal fully live. Options side shows "historical" badge.

## Design system (as of July 2026)

The dashboard uses a **Minimal Dark** design:
- Background: `#07080c`, surfaces `#0d1017` / `#121720`, borders `#1c2333`
- Text: `#eef0f4` (primary), `#c9cfd8` (secondary), `#606878` (muted)
- Accent colors: blue `#3b82f6`, red `#ef4444`, green `#22c55e`, amber `#f59e0b`
- Typography: system font stack (`-apple-system`, `BlinkMacSystemFont`, `Segoe UI`, etc.)
- Signal banner: split layout — signal word + explanation on the left, price/drawdown/impl-vol stats on the right

## Signal logic (in `update_data.py`)

| Input | Source | Role |
|---|---|---|
| 20-day annualized realized vol | Daily closes (Stooq) | **Decision variable** — crosses 60% → DERISK |
| Big-day frequency (≥3% sessions in last 20d) | Daily closes | Supporting tile |
| Drawdown from running peak | Daily closes | Supporting tile |
| 20-day autocorrelation | Daily closes | Supporting tile |
| Implied vol, put/call, skew | Paid options feed (optional) | **Context only** — does not flip the signal |

The 60% threshold was chosen in-sample (the realized tape first crossed it on June 5, 2026). Trust the dimension, not the exact number.

## Deployment

Pushes to `main` automatically trigger the GitHub Actions workflow, which:
1. Runs `update_data.py` to refresh `data/regime.json`
2. Commits the updated data back to `main`
3. Deploys the entire repo to GitHub Pages

To manually trigger a data refresh: **Actions tab → "Update regime data & deploy" → Run workflow**.

## Optional: live options data

Set repo secrets/variables (Settings → Secrets and variables → Actions):

| Provider | Secret | Variable |
|---|---|---|
| ORATS | `ORATS_TOKEN` | `OPTIONS_PROVIDER=orats` |
| Polygon.io | `POLYGON_API_KEY` | `OPTIONS_PROVIDER=polygon` |

Leave `OPTIONS_PROVIDER` unset to run price-only.

## Local dev

```bash
pip install -r requirements.txt
python scripts/update_data.py          # refresh data/regime.json
python3 -m http.server 8000            # open http://localhost:8000
```

Note: on Windows, `python` may point to Python 2. Use `python3` explicitly.

## Work log — key decisions

All significant architectural choices, data source decisions, and notable changes are documented in **`WORKLOG.md`**.

Rules for WORKLOG.md:
- Entries are **append-only** — never edit a previous entry, only add new ones at the bottom
- Each entry is dated (`YYYY-MM-DD`) and sequentially indexed (`[001]`, `[002]`, ...)
- Consult it before making decisions that could conflict with prior choices
- Add a new entry whenever you: change a data source, add/remove a gauge or section, change the signal logic, or make any non-obvious architectural trade-off

## Commit hygiene — save progress frequently

**Always commit after any meaningful change**, even mid-session. Sessions can crash, connections drop, and work in flight can be lost. The rule:

- After every logical unit of work (a feature, a visual tweak, a data fix), run:
  ```bash
  git add -p          # stage specific hunks, not everything blindly
  git commit -m "..."
  git push
  ```
- Use clear, specific commit messages. Bad: `"updates"`. Good: `"Fix drawdown tile color threshold"` or `"Add autocorrelation row to comparison table"`.
- If a session is going long, commit every 20–30 minutes regardless of whether the change feels "done".
- Never leave a session with uncommitted work that would be painful to redo.
- Before starting any significant edit, confirm the working tree is clean (`git status`). If it's dirty from a prior interrupted session, commit or stash before proceeding.
- The GitHub Actions workflow triggers on every push to `main`, so each commit also redeploys the live site automatically.

## Dashboard sections (as of July 2026)

1. **Signal banner** — DERISK/RISK-ON word + sub-text (left), SOXX price / drawdown / impl-vol stats (right)
2. **Metric tiles** — RVol20, big-day count, drawdown, autocorrelation
3. **Basket tracker** (`#basketGrid`) — 6 cards: SOXL, DRAM, RAM, EWY, KORU, MU with YTD%, Jun1%, ATH drawdown%
4. **All-clear checklist** (`#checklist`) — 5 conditions: RVol20 <51%, variance premium positive, MOVE <97, P/C OI capitulation, HMM calm
5. **Supporting gauges** (`#gaugeRows`) — 8 rows: MU RVol, MOVE, TNX, VIXEQ, dealer gamma (SOXX/EWY/DRAM), P/C OI, constituent corr, HMM stress
6. **Catalyst timeline** (`#catalystList`) — Tier 1–4 events Jul–Sep 2026; outcome badges (bull/bear/neutral/upcoming); next-catalyst banner
7. **Charts** — SOXX price history + vol regime chart
8. **Footer** — DDR4 spot price + date | next catalyst countdown

JS render functions in `index.html`:
- `renderBasket(basket)` — line ~515
- `renderChecklist(s, g)` — line ~543 (calls `renderGauges(g)` at end)
- `renderGauges(g)` — line ~611
- `renderCatalysts(events)` — line ~635

## Known caveats

- The 60% RVol threshold was set in-sample — this is n=1 on the 2026 phase change.
- The rule accepted March's −12% drawdown (realized vol stayed below 60%); that's by design, not a bug.
- Cross-sectional dispersion across SOXX constituents is shown from historical analysis only — no standard free endpoint covers it.
