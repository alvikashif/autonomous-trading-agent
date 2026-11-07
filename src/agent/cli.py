"""Commands: screen, backtest, paper-run. No live trading exists in this tool."""
from __future__ import annotations

import argparse
from datetime import date, timedelta

import pandas as pd

from .backtest import run_backtest, window_metrics
from .config import load_config
from .data import fetch_prices, load_fundamentals, load_universe
from .paper import PaperBroker
from .portfolio import plan_orders
from .risk import check_orders
from .shariah import business_violation, screen_universe
from .strategy import target_weights


def _eligible_report(path: str) -> pd.DataFrame:
    rep = pd.read_csv(path)
    return rep[rep["compliant"]].reset_index(drop=True)


def _quality(cfg) -> pd.Series | None:
    fund = load_fundamentals(cfg.universe.fundamentals_csv)
    if "roe" not in fund.columns:
        return None
    return fund.set_index("symbol")["roe"].dropna()


def _size(cfg) -> pd.Series:
    fund = load_fundamentals(cfg.universe.fundamentals_csv)
    return fund.set_index("symbol")["market_cap"].dropna()


def cmd_screen(args, cfg) -> None:
    uni = load_universe(cfg.universe.nse_list_csv)
    fund = load_fundamentals(cfg.universe.fundamentals_csv)
    merged = uni.merge(fund.drop(columns=["industry"], errors="ignore"), on="symbol", how="left")
    rep = screen_universe(merged, cfg.shariah, cfg.universe.excluded_symbols)
    rep.to_csv(cfg.universe.screen_report_csv, index=False)
    print(f"{int(rep['compliant'].sum())} of {len(rep)} compliant -> {cfg.universe.screen_report_csv}")
    failed = rep[~rep["compliant"]]
    if not failed.empty:
        print("sample failures:")
        print(failed.head(10)[["symbol", "reasons"]].to_string(index=False))


def cmd_fetch_fundamentals(args, cfg) -> None:
    from .fundamentals import fetch_fundamentals

    uni = load_universe(cfg.universe.nse_list_csv)
    # Skip stocks the coarse NSE industry label already excludes: no point fetching them.
    keep = uni["industry"].map(lambda i: business_violation(i) is None)
    print(f"skipping {int((~keep).sum())} stocks excluded by NSE industry label")
    symbols = uni.loc[keep, "symbol"].tolist()
    if args.limit:
        symbols = symbols[: args.limit]
    if args.assume_zero_interest:
        print("NOTE: absent Interest Income rows are treated as 0. Spot-check results against filings.")
    fetch_fundamentals(
        symbols, cfg.universe.fundamentals_csv,
        assume_zero_interest=args.assume_zero_interest, max_age_days=args.max_age_days,
        refetch_missing_roe=args.refetch_missing_roe,
    )


def cmd_backtest(args, cfg) -> None:
    elig = _eligible_report(cfg.universe.screen_report_csv)
    if elig.empty:
        raise SystemExit("no compliant stocks; run `agent screen` first")
    symbols = elig["symbol"].tolist()
    prices = fetch_prices(symbols + [cfg.strategy.benchmark], args.start, cache_path=cfg.universe.prices_cache_csv)
    bench = prices[cfg.strategy.benchmark]
    stocks = prices.drop(columns=[cfg.strategy.benchmark])
    sectors = dict(zip(elig["symbol"], elig["industry"].fillna("")))
    res = run_backtest(
        stocks, bench, symbols, sectors, cfg.strategy, cfg.risk, cfg.costs,
        cfg.portfolio.monthly_contribution, cfg.portfolio.min_trade_value,
        cfg.portfolio.cash_buffer_pct, _quality(cfg),
        rebalance_months=cfg.portfolio.rebalance_months, size=_size(cfg),
        stop_loss_pct=cfg.portfolio.stop_loss_pct or None,
    )
    if cfg.portfolio.stop_loss_pct:
        print(f"stop-loss     : {cfg.portfolio.stop_loss_pct:.0%} trailing, {res.n_stops} stops triggered")

    def line(name, m):
        return f"{name:<22} CAGR {m['cagr']:>6.1%}  maxDD {m['max_drawdown']:>6.1%}  Sharpe {m['sharpe']:>5.2f}"

    print(f"contributed   : {res.total_contributed:,.0f}")
    print(f"final equity  : {res.equity.iloc[-1]:,.0f}")
    print(f"trades / costs: {res.n_trades} / {res.total_costs:,.0f}")
    print(line("strategy", res.metrics))
    print(line("EW eligible universe", res.universe_metrics))
    print(line("Nifty 50 index", res.benchmark_metrics))
    for w in args.window or []:
        s, _, e = w.partition(":")
        print(f"-- window {s or 'start'} to {e or 'end'}")
        print(line("strategy", window_metrics(res.twr, s or None, e or None)))
        print(line("EW eligible universe", window_metrics(res.universe_returns, s or None, e or None)))
        print(line("Nifty 50 index", window_metrics(res.bench_returns, s or None, e or None)))
    print("Strategy vs 'EW eligible universe' is the fair test. Both share survivorship bias,")
    print("so only the gap between them says anything about the strategy. Absolute numbers are inflated.")


def cmd_paper_run(args, cfg) -> None:
    elig = _eligible_report(cfg.universe.screen_report_csv)
    symbols = elig["symbol"].tolist()
    sectors = dict(zip(elig["symbol"], elig["industry"].fillna("")))
    start = (date.today() - timedelta(days=500)).isoformat()
    prices = fetch_prices(symbols + [cfg.strategy.benchmark], start, cache_path=cfg.universe.prices_cache_csv, refresh=True)
    bench = prices[cfg.strategy.benchmark]
    stocks = prices.drop(columns=[cfg.strategy.benchmark]).ffill()
    asof = stocks.index[-1]
    px = stocks.iloc[-1].dropna().to_dict()

    broker = PaperBroker(args.state, cfg.costs)
    today = str(asof.date())
    months = cfg.portfolio.rebalance_months
    if args.deposit:
        if broker.deposited_in_month(today) and not args.force_deposit:
            print(f"already deposited in {today[:7]}: skipping deposit (--force-deposit to override)")
        else:
            broker.deposit(cfg.portfolio.monthly_contribution, today)
            print(f"deposited {cfg.portfolio.monthly_contribution:,.0f} (paper)")
    equity = broker.equity(px)
    if not (broker.rebalance_due(today, months) or args.force_rebalance):
        print(f"as of {today}  equity {equity:,.0f}  cash {broker.cash():,.0f}  holdings {broker.positions()}")
        print(f"no rebalance today: next one due {broker.next_rebalance(months)} (--force-rebalance to override)")
        if args.dry_run:
            print("dry run: nothing saved")
        else:
            broker.save()
            print(f"paper state saved -> {args.state}")
        return
    targets = target_weights(stocks, bench, asof, symbols, cfg.strategy, cfg.risk.max_position_pct, _quality(cfg), _size(cfg),
                            equity=equity, held=broker.positions().keys())
    orders = plan_orders(targets, broker.positions(), px, equity, cfg.portfolio.min_trade_value, cfg.portfolio.cash_buffer_pct)
    approved, rejected = check_orders(orders, broker.cash(), broker.positions(), px, sectors, equity, cfg.risk, cfg.costs)

    print(f"as of {asof.date()}  equity {equity:,.0f}  cash {broker.cash():,.0f}  targets {sorted(targets)}")
    for o in approved:
        print(f"  APPROVE {o.side.value} {o.qty} {o.symbol} @ {o.price:.2f}")
    for o, why in rejected:
        print(f"  REJECT  {o.side.value} {o.qty} {o.symbol}: {why}")
    if args.dry_run:
        print("dry run: nothing executed, nothing saved")
        return
    for o in approved:
        broker.place_order(o, ts=today)
    broker.mark_rebalanced(today)
    broker.save()
    print(f"paper state saved -> {args.state}")


def cmd_swing_backtest(args, cfg) -> None:
    from .swing import SwingParams, run_swing

    elig = _eligible_report(cfg.universe.screen_report_csv)
    symbols = elig["symbol"].tolist()
    prices = fetch_prices(symbols, args.start, cache_path=cfg.universe.prices_cache_csv)
    p = SwingParams(target_pct=args.target, stop_pct=args.stop, max_hold_days=args.hold,
                    max_positions=args.positions, capital=args.capital)
    res = run_swing(prices, symbols, p, cfg.costs, "breakout", start=args.start)
    s = res.summary()
    print(f"rule: 20d breakout + above 50d avg, +{p.target_pct:.0%} target / -{p.stop_pct:.0%} stop / {p.max_hold_days}d max, {p.max_positions} slots, start Rs {p.capital:,.0f}")
    print(f"breakout : trades {s['n']}  win {s['win_rate']:.0%}  avg net/trade {s['avg_net_pct']:.2%}  "
          f"total net {s['total_net']:,.0f}  costs {s['total_costs']:,.0f}  final {s['final']:,.0f}")
    if not res.trades.empty:
        print(res.trades.groupby("reason").agg(n=("net", "size"), avg_net_pct=("net_pct", "mean")).round(4).to_string())
    finals, nets = [], []
    for seed in range(args.seeds):
        r = run_swing(prices, symbols, p, cfg.costs, "random", seed=seed, start=args.start).summary()
        finals.append(r["final"]); nets.append(r["avg_net_pct"])
    import numpy as np
    print(f"random   : {args.seeds} seeds, same exits  avg net/trade {np.mean(nets):.2%}  final median {np.median(finals):,.0f} "
          f"(10th-90th pct {np.percentile(finals, 10):,.0f} - {np.percentile(finals, 90):,.0f})")
    print("If breakout is not clearly above the random band, the entry signal adds nothing. Survivorship bias applies.")


def cmd_monitor(args, cfg) -> None:
    from .monitor import run_forever, run_once

    kwargs = dict(
        paper_path=cfg.universe.paper_state_json, status_path=cfg.universe.monitor_status_json,
        cost_cfg=cfg.costs, stop_pct=cfg.portfolio.stop_loss_pct, alert_pct=cfg.portfolio.alert_drop_pct,
        daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct, act=not args.alert_only,
    )
    if args.once:
        st = run_once(**kwargs)
        print(f"{st['heartbeat']} market_open={st['market_open']} alerts={len(st['alerts'])}")
    else:
        run_forever(**kwargs)


def cmd_dashboard(args, cfg) -> None:
    from .dashboard import serve

    paths = {"paper": cfg.universe.paper_state_json, "monitor": cfg.universe.monitor_status_json,
             "screen": cfg.universe.screen_report_csv, "prices": cfg.universe.prices_cache_csv}
    serve(cfg, paths, host=args.host, port=args.port, extra_hosts=tuple(args.allow_host or ()))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="agent")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-fundamentals", help="Download fundamentals from Yahoo Finance")
    f.add_argument("--limit", type=int, default=0, help="only the first N symbols (testing)")
    f.add_argument("--max-age-days", type=int, default=90, help="skip rows fetched more recently than this")
    f.add_argument("--assume-zero-interest", action="store_true",
                   help="treat an ABSENT interest-income row as 0 (default: leave missing, stock fails screen)")
    f.add_argument("--refetch-missing-roe", action="store_true",
                   help="re-fetch fresh rows that have no ROE (picks up the computed-ROE fallback)")
    sub.add_parser("screen", help="Shariah-screen the universe")
    b = sub.add_parser("backtest", help="Backtest the strategy")
    b.add_argument("--start", default="2019-01-01")
    b.add_argument("--window", action="append", help="extra report window START:END, e.g. 2023-01-01:  (repeatable)")
    sw = sub.add_parser("swing-backtest", help="Backtest the short-term paper rule vs a random-entry baseline")
    sw.add_argument("--start", default="2019-01-01")
    sw.add_argument("--target", type=float, default=0.08)
    sw.add_argument("--stop", type=float, default=0.04)
    sw.add_argument("--hold", type=int, default=10)
    sw.add_argument("--positions", type=int, default=2)
    sw.add_argument("--capital", type=float, default=5000.0)
    sw.add_argument("--seeds", type=int, default=20)
    mo = sub.add_parser("monitor", help="Watch holdings during NSE hours (paper), alert / enforce stop")
    mo.add_argument("--once", action="store_true", help="single check then exit")
    mo.add_argument("--alert-only", action="store_true", help="never sell, even if a stop is configured")
    da = sub.add_parser("dashboard", help="Read-only local dashboard")
    da.add_argument("--host", default="127.0.0.1")
    da.add_argument("--port", type=int, default=8765)
    da.add_argument("--allow-host", action="append", help="extra Host header name to accept (e.g. a VPN hostname)")
    r = sub.add_parser("paper-run", help="Run one paper rebalance")
    r.add_argument("--state", default="data/paper_state.json")
    r.add_argument("--deposit", action="store_true",
                   help="add the monthly contribution (once per calendar month); trades only when a rebalance is due")
    r.add_argument("--force-deposit", action="store_true", help="deposit even if already done this month")
    r.add_argument("--force-rebalance", action="store_true", help="trade even if the quarterly rebalance is not due")
    r.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    {
        "fetch-fundamentals": cmd_fetch_fundamentals,
        "screen": cmd_screen,
        "backtest": cmd_backtest,
        "paper-run": cmd_paper_run,
        "swing-backtest": cmd_swing_backtest,
        "monitor": cmd_monitor,
        "dashboard": cmd_dashboard,
    }[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
