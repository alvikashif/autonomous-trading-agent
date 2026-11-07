# Shariah trading agent (NSE, paper-first)

Delivery-only. No margin/MTF, no shorting, no F&O. No live broker is implemented:
`GrowwBroker` is a deliberate stub. Everything runs on paper until you decide otherwise.

## Setup
```
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

## Data you must supply (in `data/`)
1. `nifty500_shariah.csv`: Nifty500 Shariah constituents from niftyindices.com (needs Symbol, Industry columns).
2. `fundamentals.csv`: now auto-fetched by `agent fetch-fundamentals` (Yahoo Finance). Manual format in `fundamentals_template.csv`.
   Yahoo data is free but imperfect: statement currency is converted to market currency, anything missing stays blank
   (stock fails the screen), and an absent interest-income row counts as missing unless you pass `--assume-zero-interest`.
   Spot-check a few stocks against filings. Re-run quarterly (rows younger than 90 days are skipped).

## Commands
```
agent fetch-fundamentals --limit 10   # test on a few first; full run is slow (~0.5s+ per stock)
agent screen                      # writes data/screen_report.csv (fails closed on missing data)
agent backtest --start 2019-01-01 # costs, slippage, monthly Rs 5000 contributions
agent paper-run --dry-run         # show what a monthly rebalance would do
agent paper-run --deposit         # run monthly: deposits once per calendar month, trades only when a
                                  # quarterly rebalance is due (state tracks deposits and last rebalance);
                                  # --force-deposit / --force-rebalance override the guards
```

## Default strategy (chosen from backtests)
Equal-weight the 20 largest Shariah-eligible stocks by market cap, rebalance quarterly,
monthly contributions wait as cash between rebalances. Backtest since 2019 (survivorship-biased, compare only):
size20 quarterly 20.1%/yr, max drawdown -20%, costs about Rs 6.5k; momentum variants cost Rs 50-85k and
were no better on risk-adjusted return than the plain eligible universe. Switch with `strategy.mode: momentum`.
Size ranking uses today's market caps (look-ahead in backtests).

Small accounts: `scale_top_n` makes N grow with the portfolio (one extra name per Rs 1500, never below 5 names,
max `top_n`) and only picks stocks whose share price fits a slot. Held names are kept while still in the top by size,
so adding money adds names instead of churning. Rs 5k buys about 5 cheap-per-share large caps; a full 20 needs about Rs 30k
and the priciest names (e.g. Maruti) only enter once slots are large enough.
Backtest with scaling: 21.9%/yr, max drawdown -24%, costs about Rs 7.8k (same bias caveats).

## Monitor and dashboard (paper)
`agent monitor` watches holdings during NSE hours and alerts on big drops (15% below peak by default); a
trailing stop-loss is optional (`portfolio.stop_loss_pct`, default 0 = off). Backtests: tight stops (10-15%)
cut returns badly (10.8% and 16.8% a year vs 21.9% without), 20-25% were roughly neutral with ~6x the costs and no
smaller worst drop, so it stays off. `agent dashboard` is a read-only page on http://127.0.0.1:8765.
Remote access and 24/7 setup: see REMOTE_ACCESS.md.

## Layout
- `shariah.py` screens; `strategy.py` momentum+quality+trend; `portfolio.py` weights to orders;
  `risk.py` veto layer; `paper.py` simulated broker; `backtest.py`; `costs.py` Indian delivery charges.

## Known limits
- Backtest has survivorship and look-ahead bias (today's Shariah list and ROE). Upper bound only.
- Daily-loss kill switch exists in `risk.py` but `paper-run` does not feed it yet.
- Cost schedule is approximate; verify against Groww's current charges.
- Trend filter can move to cash and realise taxable gains; set `use_trend_filter: false` to disable.
- Shariah ratios are a mechanical filter, not a ruling. Confirm with your scholar.

## Before going live
Paper-trade 1-3 months, confirm Groww API terms (static IP, limits, token refresh),
implement `GrowwBroker` forcing product=CNC, start with a tiny amount.
