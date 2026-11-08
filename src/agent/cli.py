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
    from .kill_switch import is_kill_switch_active

    if is_kill_switch_active(cfg.universe.kill_switch_json):
        print("Trading halted: Kill switch is currently ACTIVE! Disengage with `agent kill-switch --reset` or via dashboard.")
        return

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


def cmd_swing_run(args, cfg) -> None:
    """Forward paper run: replay the fixed rule from swing.start_date on fresh prices, write a status file."""
    import json
    import os
    import tempfile

    from .swing import params_from_config, run_swing, swing_status

    sc = cfg.swing
    elig = _eligible_report(cfg.universe.screen_report_csv)
    symbols = elig["symbol"].tolist()
    start = (pd.Timestamp(sc.start_date) - timedelta(days=250)).date().isoformat()  # warm-up for the averages
    prices = fetch_prices(symbols + [cfg.strategy.benchmark], start, cache_path=cfg.universe.prices_cache_csv, refresh=True)
    bench = prices[cfg.strategy.benchmark]
    stocks = prices.drop(columns=[cfg.strategy.benchmark])
    if stocks.index[-1] < pd.Timestamp(sc.start_date):
        status = {"mode": "PAPER sandbox (simulated)", "start_date": sc.start_date, "capital": sc.capital,
                  "waiting": True, "holdings": [], "recent": [], "pending_buy": []}
    else:
        res = run_swing(stocks, symbols, params_from_config(sc), cfg.costs, "breakout", start=sc.start_date)
        status = swing_status(res, sc, bench)
    if args.dry_run:
        print(json.dumps(status, indent=1, default=str)[:1500])
        return
    path = cfg.universe.swing_status_json
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(status, f, default=str)
    os.replace(tmp, path)
    if status.get("waiting"):
        print(f"swing sandbox starts {sc.start_date}; no data yet")
    else:
        print(f"swing as of {status['as_of']}: equity {status['equity']:,.0f} ({status['pnl_pct']:+.1%}), "
              f"{status['trades']} closed trades, {len(status['holdings'])} open, buying next close: {status['pending_buy']}")


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

    paths = {
        "paper": cfg.universe.paper_state_json,
        "monitor": cfg.universe.monitor_status_json,
        "screen": cfg.universe.screen_report_csv,
        "prices": cfg.universe.prices_cache_csv,
        "swing": cfg.universe.swing_status_json,
        "kill_switch": cfg.universe.kill_switch_json,
    }
    serve(cfg, paths, host=args.host, port=args.port, extra_hosts=tuple(args.allow_host or ()))


def cmd_kill_switch(args, cfg) -> None:
    from .kill_switch import get_kill_switch_status, reset_kill_switch, trigger_kill_switch

    path = cfg.universe.kill_switch_json
    if args.trigger:
        reason = args.reason or "CLI manual trigger"
        res = trigger_kill_switch(reason=reason, path=path)
        print(f"KILL SWITCH ENGAGED! Reason: {res['reason']}")
    elif args.reset:
        res = reset_kill_switch(path=path)
        print("KILL SWITCH DISENGAGED. Normal operations restored.")
    else:
        st = get_kill_switch_status(path=path)
        status_str = "ACTIVE (HALTED)" if st["active"] else "INACTIVE (NORMAL)"
        print(f"Kill switch status: {status_str}")
        if st["active"]:
            print(f"  Reason: {st.get('reason')}")
            print(f"  Triggered at: {st.get('triggered_at')}")


def cmd_groww_status(args, cfg) -> None:
    from .broker import GrowwBroker

    b_cfg = cfg.broker
    api_key = b_cfg.resolved_api_key()
    totp_secret = b_cfg.resolved_totp_secret()
    if not api_key or not totp_secret:
        raise SystemExit(
            "Missing Groww credentials. Set broker.api_key & broker.totp_secret in config.yaml "
            "or export GROWW_API_KEY and GROWW_TOTP_SECRET."
        )

    print("Connecting to Groww API with automatic daily TOTP generation...")
    broker = GrowwBroker(api_key=api_key, totp_secret=totp_secret, token_cache_path=b_cfg.token_cache_path)
    broker.authenticate(force=args.force)
    print(f"Authentication verified! (Token cached at {b_cfg.token_cache_path})")
    cash = broker.cash()
    pos = broker.positions()
    print(f"Available CNC Balance: Rs {cash:,.2f}")
    print(f"Demat Holdings ({len(pos)} symbols):", pos if pos else "none")


def cmd_live_run(args, cfg) -> None:
    from .broker import GrowwBroker
    from .kill_switch import is_kill_switch_active

    if is_kill_switch_active(cfg.universe.kill_switch_json):
        raise SystemExit("Trading halted: Kill switch is currently ACTIVE! Disengage with `agent kill-switch --reset` or via dashboard.")

    b_cfg = cfg.broker
    api_key = b_cfg.resolved_api_key()
    totp_secret = b_cfg.resolved_totp_secret()
    if not api_key or not totp_secret:
        raise SystemExit(
            "Missing Groww credentials. Set broker.api_key & broker.totp_secret in config.yaml "
            "or export GROWW_API_KEY and GROWW_TOTP_SECRET."
        )

    broker = GrowwBroker(api_key=api_key, totp_secret=totp_secret, token_cache_path=b_cfg.token_cache_path)
    broker.authenticate()

    elig = _eligible_report(cfg.universe.screen_report_csv)
    symbols = elig["symbol"].tolist()
    sectors = dict(zip(elig["symbol"], elig["industry"].fillna("")))
    start = (date.today() - timedelta(days=500)).isoformat()
    prices = fetch_prices(symbols + [cfg.strategy.benchmark], start, cache_path=cfg.universe.prices_cache_csv, refresh=True)
    bench = prices[cfg.strategy.benchmark]
    stocks = prices.drop(columns=[cfg.strategy.benchmark]).ffill()
    asof = stocks.index[-1]
    px = stocks.iloc[-1].dropna().to_dict()

    live_cash = broker.cash()
    live_pos = broker.positions()
    equity = live_cash + sum(live_pos.get(s, 0) * px.get(s, 0.0) for s in live_pos)

    today = str(asof.date())
    print(f"LIVE Groww: as of {today}  equity Rs {equity:,.0f}  cash Rs {live_cash:,.0f}  holdings: {live_pos}")

    targets = target_weights(stocks, bench, asof, symbols, cfg.strategy, cfg.risk.max_position_pct, _quality(cfg), _size(cfg),
                            equity=equity, held=live_pos.keys())
    orders = plan_orders(targets, live_pos, px, equity, cfg.portfolio.min_trade_value, cfg.portfolio.cash_buffer_pct)
    approved, rejected = check_orders(orders, live_cash, live_pos, px, sectors, equity, cfg.risk, cfg.costs,
                                      kill_switch_active=is_kill_switch_active(cfg.universe.kill_switch_json))

    print(f"Orders planned: {len(approved)} approved, {len(rejected)} rejected:")
    for o in approved:
        print(f"  APPROVE {o.side.value} {o.qty} {o.symbol} @ {o.price:.2f} (Delivery CNC)")
    for o, why in rejected:
        print(f"  REJECT  {o.side.value} {o.qty} {o.symbol}: {why}")

    if not approved:
        print("No orders to execute.")
        return

    if args.dry_run:
        print("DRY RUN: No live orders placed to Groww.")
        return

    if not args.yes:
        confirm = input(f"Place {len(approved)} LIVE REAL-MONEY orders to Groww? Type 'YES' to confirm: ")
        if confirm.strip() != "YES":
            print("Aborted by user.")
            return

    for o in approved:
        print(f"Placing LIVE CNC order: {o.side.value} {o.qty} {o.symbol}...")
        resp = broker.place_order(o)
        print("  Groww Response:", resp)
    print("All live orders processed.")


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
    sr = sub.add_parser("swing-run", help="Forward PAPER run of the short-term rule; writes data/swing_status.json")
    sr.add_argument("--dry-run", action="store_true", help="print, do not write the status file")
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

    lr = sub.add_parser("live-run", help="Run one LIVE rebalance on Groww (Delivery CNC only)")
    lr.add_argument("--dry-run", action="store_true", help="calculate orders without placing live trades")
    lr.add_argument("--yes", action="store_true", help="skip confirmation prompt")

    ks = sub.add_parser("kill-switch", help="Emergency circuit breaker controls")
    ks.add_argument("--trigger", action="store_true", help="immediately engage the kill switch (halt trading)")
    ks.add_argument("--reset", action="store_true", help="disengage the kill switch and resume normal operations")
    ks.add_argument("--status", action="store_true", help="check kill switch state (default)")
    ks.add_argument("--reason", default="", help="reason for engaging the kill switch")

    gs = sub.add_parser("groww-status", help="Test Groww connection and check cash/holdings")
    gs.add_argument("--force", action="store_true", help="force regenerate a fresh TOTP access token")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    {
        "fetch-fundamentals": cmd_fetch_fundamentals,
        "screen": cmd_screen,
        "backtest": cmd_backtest,
        "paper-run": cmd_paper_run,
        "live-run": cmd_live_run,
        "swing-backtest": cmd_swing_backtest,
        "swing-run": cmd_swing_run,
        "monitor": cmd_monitor,
        "dashboard": cmd_dashboard,
        "kill-switch": cmd_kill_switch,
        "groww-status": cmd_groww_status,
    }[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
