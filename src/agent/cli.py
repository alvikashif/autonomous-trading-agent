"""Commands: screen, backtest, paper-run. No live trading exists in this tool."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path

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

        # AI Take-Profit Evaluation: Ask AI Brain if any holding hitting target should be trailed higher
        if cfg.ai.resolved_api_key():
            from .ai_brain import evaluate_take_profit, get_symbol_technicals, load_ai_signals, save_ai_signals

            ai_sig = load_ai_signals(cfg.universe.ai_signals_json)
            evals = ai_sig.get("exit_evaluations", {})
            for h in status.get("holdings", []):
                if h.get("pending") == "target":
                    sym = h["symbol"]
                    print(f"Holding {sym} hit profit target (+{h.get('pnl_pct', 0)*100:.1f}%). Asking AI Brain whether to take profit or trail...")
                    tech = get_symbol_technicals(sym, stocks)
                    decision = evaluate_take_profit(h, tech, cfg.ai)
                    evals[sym] = decision
                    print(f"  AI Decision for {sym}: {decision['decision']} ({decision.get('reason')})")
                    if decision["decision"] == "HOLD_AND_TRAIL":
                        h["pending"] = f"trail (+{decision.get('revised_target_pct', 0.12):.0%} target, +{decision.get('trailing_stop_pct', 0.05):.0%} stop)"
            ai_sig["exit_evaluations"] = evals
            save_ai_signals(ai_sig, cfg.universe.ai_signals_json)
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

    broker = None
    live_mode = getattr(args, "live", False)
    if live_mode:
        from .broker import GrowwBroker
        api_key = cfg.broker.resolved_api_key()
        totp = cfg.broker.resolved_totp_secret()
        if api_key and totp:
            broker = GrowwBroker(api_key=api_key, totp_secret=totp, token_cache_path=cfg.broker.token_cache_path)
            try:
                broker.authenticate()
            except Exception as e:
                print(f"Warning: Groww authentication failed in monitor: {e}")

    kwargs = dict(
        paper_path=cfg.universe.paper_state_json,
        status_path=cfg.universe.monitor_status_json,
        cost_cfg=cfg.costs,
        stop_pct=cfg.portfolio.stop_loss_pct,
        alert_pct=cfg.portfolio.alert_drop_pct,
        daily_loss_limit_pct=cfg.risk.daily_loss_limit_pct,
        act=not args.alert_only,
        ai_signals_path=cfg.universe.ai_signals_json,
        broker=broker,
        ai_cfg=cfg.ai,
        live_mode=live_mode,
        kill_switch_path=cfg.universe.kill_switch_json,
    )
    if args.once:
        st = run_once(**kwargs)
        print(f"{st['heartbeat']} market_open={st['market_open']} alerts={len(st['alerts'])} live={st.get('live_mode')} ai_monitored={st.get('ai_monitored')}")
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
        "ai_signals": cfg.universe.ai_signals_json,
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


def cmd_ai_status(args, cfg) -> None:
    from .ai_brain import load_ai_signals

    ai = cfg.ai
    provider = ai.resolved_provider()
    model = ai.resolved_model()
    key = ai.resolved_api_key()
    masked_key = (key[:6] + "..." + key[-4:]) if len(key) > 10 else ("Configured" if key else "MISSING")

    print("=== AI Trading Brain Status ===")
    print(f"Provider (ENV/Config) : {provider}")
    print(f"Model (ENV/Config)    : {model}")
    print(f"API Key               : {masked_key}")
    print(f"Min Confidence        : {ai.resolved_min_confidence()} / 10.0")
    print(f"Max Allocation        : Rs {ai.resolved_max_allocation():,.0f}")
    print(f"Max Positions         : {ai.resolved_max_positions()}")

    signals = load_ai_signals(cfg.universe.ai_signals_json)
    if not signals:
        print("\nLatest AI Signal      : No scans run yet (run `agent ai-scan`)")
    else:
        print(f"\nLatest Scan Time      : {signals.get('scan_timestamp', 'unknown')}")
        print(f"Market Regime         : {signals.get('market_regime', 'unknown').upper()}")
        print(f"Verdict               : {signals.get('verdict', 'unknown')}")
        print(f"Summary               : {signals.get('decision_summary', 'none')}")
        sel = signals.get("selected_trade")
        if sel:
            print(f"Selected Stock        : {sel['symbol']} ({sel['action']})")
            print(f"Entry Price           : Rs {sel['entry_price']:.2f}")
            print(f"Target (+8%)          : Rs {sel['target_price']:.2f}")
            print(f"Stop Loss (-4%)       : Rs {sel['stop_loss']:.2f}")
            print(f"Conviction Score      : {sel['confidence_score']:.1f} / 10.0")
            print(f"Thesis                : {sel['thesis']}")

    # Print API Budget Tracker summary
    try:
        from .api_tracker import load_api_usage
        usage = load_api_usage()
        budget = usage.get("budget_limit_usd", 5.0)
        spent = usage.get("total_spent_usd", 0.0)
        rem = usage.get("remaining_usd", budget)
        print(f"\nAI API Budget ($5 Limit): ${spent:.4f} spent / ${rem:.4f} remaining ({usage.get('total_calls', 0)} calls)")
    except Exception:
        pass


def cmd_api_budget(args, cfg) -> None:
    from .api_tracker import load_api_usage

    usage = load_api_usage()
    budget = usage.get("budget_limit_usd", 5.0)
    spent = usage.get("total_spent_usd", 0.0)
    rem = usage.get("remaining_usd", budget)
    calls = usage.get("total_calls", 0)
    pct_used = (spent / budget * 100) if budget > 0 else 0

    print("=== AI API Budget & Usage Tracker ($5.00 Prepaid Limit) ===")
    print(f"Total Budget    : ${budget:.2f}")
    print(f"Total Spent     : ${spent:.4f} ({pct_used:.1f}%)")
    print(f"Remaining Funds : ${rem:.4f} ({100 - pct_used:.1f}%)")
    print(f"Total API Calls : {calls}")
    print("\nBreakdown by Provider:")
    providers = usage.get("providers", {})
    if not providers:
        print("  (No API calls recorded yet)")
    for prov, data in providers.items():
        print(f"  • {prov.upper():<10}: {data['calls']} calls | {data['input_tokens']:,} in | {data['output_tokens']:,} out | ${data['cost_usd']:.4f}")

    recent = usage.get("recent_calls", [])
    if recent:
        print("\nRecent API Invocations:")
        for c in recent[:5]:
            print(f"  [{c['ts']}] {c['provider'].upper()} ({c['model']}): {c['input_tokens']} in, {c['output_tokens']} out => ${c['cost_usd']:.5f}")


def cmd_ai_scan(args, cfg) -> None:
    from .ai_brain import (
        build_analysis_prompt,
        clean_json_response,
        extract_technical_candidates,
        query_llm,
        save_ai_signals,
        validate_ai_decision,
    )

    ai = cfg.ai
    provider = ai.resolved_provider()
    model = ai.resolved_model()
    api_key = ai.resolved_api_key()

    print(f"Scanning for AI swing opportunities using {provider.upper()} ({model})...")

    elig = _eligible_report(cfg.universe.screen_report_csv)
    if elig.empty:
        raise SystemExit("No compliant stocks found. Run `agent screen` first.")
    compliant_symbols = set(elig["symbol"])

    prices_path = cfg.universe.prices_cache_csv
    if not Path(prices_path).is_file():
        raise SystemExit(f"Prices cache not found at {prices_path}. Run `agent backtest` or update cache first.")
    prices_df = pd.read_csv(prices_path, index_col=0, parse_dates=True)

    fund_path = cfg.universe.fundamentals_csv
    fund_df = load_fundamentals(fund_path) if Path(fund_path).is_file() else pd.DataFrame()

    candidates = extract_technical_candidates(
        prices_df,
        compliant_symbols,
        fund_df,
        max_candidates=10,
        max_price=ai.resolved_max_allocation(),
    )

    if not candidates:
        print("No breakout candidates found within price and technical criteria.")
        return

    print(f"Found {len(candidates)} pre-screened breakout candidates (max Rs {ai.resolved_max_allocation():,.0f}):")
    for c in candidates:
        print(f"  {c['symbol']:<12} Rs {c['current_price']:<8.2f} (20d ret: +{c['ret_20d_pct']}%, RSI: {c['rsi_14']})")

    if args.dry_run:
        print("\nDRY RUN: Prompt prepared. Skipping actual LLM API call.")
        return

    if not api_key:
        raise SystemExit(
            f"ERROR: Missing API key for {provider.upper()}. "
            f"Please set {provider.upper()}_API_KEY in your ENV or CapRover configuration."
        )

    prompt = build_analysis_prompt(
        candidates=candidates,
        capital=ai.resolved_max_allocation(),
        min_confidence=ai.resolved_min_confidence(),
        target_pct=ai.target_pct,
        stop_pct=ai.stop_pct,
    )

    print(f"\nQuerying {provider} ({model})...")
    raw_response = query_llm(prompt, provider, model, api_key)
    from .ai_brain import get_last_used_model, get_last_used_provider
    actual_prov = get_last_used_provider() or provider
    actual_model = get_last_used_model() or model
    if actual_prov != provider:
        print(f"Notice: Failover engaged. Analysis completed by {actual_prov.upper()} ({actual_model}).")
    parsed = clean_json_response(raw_response)
    validated = validate_ai_decision(parsed, compliant_symbols, min_confidence=ai.resolved_min_confidence())
    validated["provider"] = actual_prov
    validated["model"] = actual_model

    save_ai_signals(validated, cfg.universe.ai_signals_json)
    print(f"Analysis saved to {cfg.universe.ai_signals_json}")
    print("\n--- AI Brain Decision ---")
    print(f"Verdict         : {validated.get('verdict')}")
    print(f"Market Regime   : {validated.get('market_regime', '').upper()}")
    print(f"Summary         : {validated.get('decision_summary')}")
    sel = validated.get("selected_trade")
    if sel:
        print(f"Selected Stock  : {sel['symbol']}")
        print(f"Entry Price     : Rs {sel['entry_price']:.2f}")
        print(f"Target (+8%)    : Rs {sel['target_price']:.2f}")
        print(f"Stop Loss (-4%) : Rs {sel['stop_loss']:.2f}")
        print(f"Conviction      : {sel['confidence_score']:.1f} / 10.0")
        print(f"Thesis          : {sel['thesis']}")
        print(f"Risks           : {', '.join(sel.get('risks', []))}")


def cmd_ai_trade(args, cfg) -> None:
    from .ai_brain import compute_order_size, load_ai_signals
    from .broker import GrowwBroker
    from .kill_switch import is_kill_switch_active
    from .models import Order, Side

    # Emergency Kill Switch Check
    if is_kill_switch_active(cfg.universe.kill_switch_json):
        raise SystemExit("EMERGENCY KILL SWITCH IS ACTIVE! All trading orders are blocked.")

    signals = load_ai_signals(cfg.universe.ai_signals_json)
    if not signals:
        raise SystemExit("No AI signals found. Run `agent ai-scan` first.")

    verdict = signals.get("verdict")
    selected = signals.get("selected_trade")
    if verdict != "EXECUTE" or not selected:
        print(f"AI Decision was '{verdict}': {signals.get('decision_summary', 'No trade selected')}")
        return

    symbol = selected["symbol"]
    entry_px = float(selected["entry_price"])
    confidence = float(selected.get("confidence_score", 0.0))
    min_conf = cfg.ai.resolved_min_confidence()

    if confidence < min_conf:
        print(f"Aborting: Conviction {confidence:.1f} is below minimum threshold {min_conf:.1f}.")
        return

    max_alloc = cfg.ai.resolved_max_allocation()
    broker_mode = "live" if args.live else "paper"

    print(f"=== AI Trade Execution [{broker_mode.upper()} MODE] ===")
    print(f"Stock        : {symbol}")
    print(f"Entry Target : Rs {entry_px:.2f}")
    print(f"Profit Target: Rs {selected['target_price']:.2f} (+8%)")
    print(f"Stop Loss    : Rs {selected['stop_loss']:.2f} (-4%)")
    print(f"Conviction   : {confidence:.1f} / 10.0")

    if not args.live:
        sizing = compute_order_size(selected, max_alloc)
        qty = sizing["qty"]
        trade_val = sizing["trade_value"]
        print(f"Position Size: {qty} shares @ Rs {entry_px:.2f} = Rs {trade_val:,.2f}")
        print("PAPER MODE: Trade registered in simulation state.")
        return

    # LIVE Groww Execution
    b_cfg = cfg.broker
    api_key = b_cfg.resolved_api_key()
    totp_secret = b_cfg.resolved_totp_secret()
    if not api_key or not totp_secret:
        raise SystemExit("Missing Groww credentials for live order execution.")

    broker = GrowwBroker(api_key=api_key, totp_secret=totp_secret, token_cache_path=b_cfg.token_cache_path)
    broker.authenticate()
    live_cash = broker.cash()
    print(f"Available Groww Balance: Rs {live_cash:,.2f}")

    sizing = compute_order_size(selected, max_alloc, cash_balance=live_cash)
    qty = sizing["qty"]
    trade_val = sizing["trade_value"]

    if qty <= 0:
        raise SystemExit(
            f"Insufficient funds or share price too high: 1 share is Rs {entry_px:.2f}, balance is Rs {live_cash:.2f}"
        )

    print(f"Order: BUY {qty} shares of {symbol} (Est: Rs {trade_val:,.2f}, CNC Delivery)")

    if args.dry_run:
        print("DRY RUN: No live order placed.")
        return

    if not args.yes:
        confirm = input(f"Place LIVE REAL-MONEY order for {qty} {symbol} (Rs {trade_val:,.2f}) on Groww? Type 'YES': ")
        if confirm.strip() != "YES":
            print("Aborted by user.")
            return

    order = Order(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        price=entry_px,
    )
    print(f"Submitting LIVE CNC Delivery order to Groww: BUY {qty} {symbol}...")
    resp = broker.place_order(order)
    print("Groww Response:", resp)
    print("Order execution completed successfully.")
    try:
        from .notify import dispatch_notification
        dispatch_notification(
            event_type="buy",
            title=f"LIVE BUY: {qty} {symbol}",
            message=f"Groww CNC order submitted for {qty} shares of {symbol} @ Rs {entry_px:,.2f} (Total: Rs {trade_val:,.2f})",
            details={"symbol": symbol, "qty": qty, "price": entry_px, "trade_val": trade_val, "mode": "LIVE"},
            path=cfg.universe.notifications_json,
        )
    except Exception as e:
        print(f"Notification dispatch error: {e}")


def cmd_ai_eval_exit(args, cfg) -> None:
    from .ai_brain import evaluate_take_profit, get_symbol_technicals, load_ai_signals, save_ai_signals

    sym = args.symbol.upper()
    prices_path = cfg.universe.prices_cache_csv
    if not Path(prices_path).is_file():
        raise SystemExit(f"Prices cache not found at {prices_path}.")
    prices_df = pd.read_csv(prices_path, index_col=0, parse_dates=True)

    tech = get_symbol_technicals(sym, prices_df)
    if not tech:
        raise SystemExit(f"No technical data found for symbol '{sym}'.")

    cur_px = args.current_price or tech["current_price"]
    entry_px = args.entry_price or (cur_px / 1.08)  # default assumed target reached
    gain_pct = (cur_px / entry_px - 1)

    holding = {
        "symbol": sym,
        "entry_price": entry_px,
        "current_price": cur_px,
        "gain_pct": gain_pct,
        "days_held": args.days_held or 6,
    }

    print(f"=== AI Take-Profit Maximizer Evaluation for {sym} ===")
    print(f"Entry Price   : Rs {entry_px:.2f}")
    print(f"Current Price : Rs {cur_px:.2f} (+{gain_pct*100:.1f}%)")
    print(f"14-day RSI    : {tech['rsi_14']:.1f}")
    print(f"Querying {cfg.ai.resolved_provider().upper()} ({cfg.ai.resolved_model()})...")

    decision = evaluate_take_profit(holding, tech, cfg.ai)
    print("\n--- AI Brain Take-Profit Recommendation ---")
    print(f"Decision      : {decision['decision']}")
    print(f"Confidence    : {decision.get('confidence_score', '–')}/10")
    print(f"Reason        : {decision.get('reason')}")
    if decision["decision"] == "HOLD_AND_TRAIL":
        print(f"Revised Target: Rs {decision.get('revised_target_price', 0):.2f} (+{decision.get('revised_target_pct', 0)*100:.0f}%)")
        print(f"Trailing Stop : Rs {decision.get('trailing_stop_price', 0):.2f} (+{decision.get('trailing_stop_pct', 0)*100:.0f}% locked)")

    ai_sig = load_ai_signals(cfg.universe.ai_signals_json)
    ai_sig.setdefault("exit_evaluations", {})[sym] = decision
    save_ai_signals(ai_sig, cfg.universe.ai_signals_json)
    print(f"\nDecision saved to {cfg.universe.ai_signals_json} and active on dashboard.")


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
    mo = sub.add_parser("monitor", help="Watch holdings during NSE hours (paper/live), alert / enforce stop / take profit")
    mo.add_argument("--once", action="store_true", help="single check then exit")
    mo.add_argument("--alert-only", action="store_true", help="never sell, even if a stop is configured")
    mo.add_argument("--live", action="store_true", help="monitor and execute mid-day exits on live Groww account")
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

    ais = sub.add_parser("ai-status", help="Check AI Brain configuration and latest signal")

    aic = sub.add_parser("ai-scan", help="Scan universe with AI Brain (Anthropic/DeepSeek/OpenAI/Gemini)")
    aic.add_argument("--dry-run", action="store_true", help="extract candidates and format prompt without calling AI API")

    ait = sub.add_parser("ai-trade", help="Execute trade based on latest AI Brain recommendation")
    ait.add_argument("--live", action="store_true", help="execute LIVE real-money order on Groww (Delivery CNC)")
    ait.add_argument("--dry-run", action="store_true", help="simulate order calculations without sending to Groww")
    ait.add_argument("--yes", action="store_true", help="skip confirmation prompt in live mode")

    aie = sub.add_parser("ai-eval-exit", help="Ask AI Brain whether to take profit or trail higher on a winning trade")
    aie.add_argument("--symbol", required=True, help="stock symbol to evaluate (e.g. SHYAMMETL)")
    aie.add_argument("--entry-price", type=float, help="original entry price (optional)")
    aie.add_argument("--current-price", type=float, help="current market price (optional)")
    aie.add_argument("--days-held", type=int, help="days held so far (optional)")

    sub.add_parser("api-budget", help="Check AI API token usage and remaining $5 prepaid budget")

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
        "ai-status": cmd_ai_status,
        "ai-scan": cmd_ai_scan,
        "ai-trade": cmd_ai_trade,
        "ai-eval-exit": cmd_ai_eval_exit,
        "api-budget": cmd_api_budget,
    }[args.cmd](args, cfg)


if __name__ == "__main__":
    main()
