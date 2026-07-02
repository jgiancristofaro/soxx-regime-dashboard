# SOXX Dashboard — Work Log

Key decisions, architecture choices, and notable changes. Entries are append-only — never edit previous entries. Each entry is dated and indexed.

---

## [001] 2026-07-02 — Gauge automation strategy

**Context:** After implementing the catalyst/basket/checklist dashboard spec, we had 8 supporting gauge rows — some pulling from `data/catalysts.json` (manually updated) and some auto-fetched. Reviewed which could be automated with free, unauthenticated data sources.

**Decisions:**

| Gauge | Action | Reason |
|---|---|---|
| P/C OI ratio (SOXX) | **Automate** | Yahoo Finance `/v7/finance/options/{ticker}` returns OI by contract; summing put_OI / call_OI gives ratio. Fetches near 4 monthly expiries. |
| Constituent correlation | **Automate** | Top-15 SOXX holdings hardcoded (`SOXX_CONSTITUENTS` in `update_data.py`). Fetch daily closes via Yahoo, compute avg pairwise 20-day correlation. |
| HMM stress probability | **Automate** | 2-state Gaussian HMM (`hmmlearn`) fit on 2-year SOXX daily returns. Stress state = higher-variance state. P(stress) for most recent observation updated daily. Uses full 2-year price history for fit, even though dashboard only displays 2026+ data. |
| Dealer net gamma (SOXX/EWY) | **Remove** | Requires computing from full options chain with dealer positioning sign convention. Free proxies (SqueezeMetrics GEX) are not API-accessible. Barchart's gamma dashboard requires session auth. No clean free endpoint. |
| DDR4 1Gx8 spot price | **Remove** | TrendForce / DRAMeXchange are behind subscription walls. No free, machine-readable endpoint. Was showing in footer — removed from display. |
| TSMC monthly revenue | **Remove** | Released once per month (~10th of following month). Could scrape TSMC IR page but low value for a daily refresh. Low signal/effort ratio to automate. |

**Files changed:** `scripts/update_data.py`, `requirements.txt` (`hmmlearn>=0.3` added), `data/catalysts.json` (`static_gauges` cleared), `index.html` (dealer gamma row removed, DDR4 footer removed, P/C OI and corr notes updated).

**Trade-offs noted:**
- P/C OI is now "near-month" (4 expiries out) rather than all-expiry aggregate. Front-month is most liquid and arguably most informative for near-term sentiment.
- Constituent correlation uses a hardcoded top-15 ticker list. If SOXX reconstitutes significantly, update `SOXX_CONSTITUENTS` in `update_data.py`.
- HMM state labels can in theory flip if the model converges differently. `random_state=42` + 500 iterations makes this unlikely. P(stress) near 1.0 is robust regardless of label assignment since the high-variance state is unambiguous in the current regime.
- Percentile ranks for P/C OI and constituent corr are dropped (were manually set). Would require storing a rolling history to compute; left as future enhancement.

---
