import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer

import pytest

from agent.config import Config
from agent.costs import CostConfig
from agent.dashboard import average_costs, build_summary, make_handler
from agent.monitor import IST, check_positions, market_open, run_once


def test_market_hours_ist():
    assert market_open(datetime(2026, 11, 6, 10, 0, tzinfo=IST))        # Friday
    assert not market_open(datetime(2026, 11, 6, 15, 30, tzinfo=IST))   # closes 15:30
    assert not market_open(datetime(2026, 11, 6, 9, 14, tzinfo=IST))
    assert not market_open(datetime(2026, 11, 7, 11, 0, tzinfo=IST))    # Saturday


def test_check_positions_stop_alert_and_daily_loss():
    peaks = {}
    ev = check_positions({"A": 10}, {"A": 100.0}, {"A": 100.0}, peaks, 0.2, 0.1, 0.03)
    assert ev == [] and peaks["A"] == 100.0
    ev = check_positions({"A": 10}, {"A": 88.0}, {"A": 100.0}, peaks, 0.2, 0.1, 0.5)
    assert [e.kind for e in ev] == ["ALERT_DROP"]                        # 12% below peak, stop is 20%
    ev = check_positions({"A": 10}, {"A": 79.0}, {"A": 100.0}, peaks, 0.2, 0.1, 0.5)
    assert [e.kind for e in ev] == ["STOP_HIT"]
    ev = check_positions({"A": 10}, {"A": 96.0}, {"A": 100.0}, {"A": 96.0}, 0.0, 0.0, 0.03)
    assert [e.kind for e in ev] == ["DAILY_LOSS"]
    ev = check_positions({"A": 10}, {}, {}, {}, 0.2, 0.1, 0.03)
    assert [e.kind for e in ev] == ["NO_QUOTE"]


def _paper(tmp_path, qty=10):
    p = tmp_path / "paper.json"
    p.write_text(json.dumps({"cash": 100.0, "positions": {"A": qty}, "total_costs": 5.0,
                             "deposits": [{"date": "2026-11-06", "amount": 1100.0}], "last_rebalance": "2026-11-06",
                             "trades": [{"ts": "2026-11-06", "symbol": "A", "side": "BUY", "qty": qty,
                                         "fill_price": 100.0, "cost": 10.0}]}))
    return p


def test_run_once_stop_sells_on_paper_only_when_acting(tmp_path):
    paper, status = _paper(tmp_path), tmp_path / "mon.json"
    now = datetime(2026, 11, 6, 11, 0, tzinfo=IST)
    quotes = lambda syms: ({"A": 70.0}, {"A": 100.0})
    st = run_once(str(paper), str(status), CostConfig(), 0.2, 0.1, 0.9, act=False, now=now, quote_fn=quotes)
    assert json.loads(paper.read_text())["positions"] == {"A": 10}       # alert-only: untouched
    # peak was initialised at 70 on first sight, so drop 0; seed a higher peak and act
    status.write_text(json.dumps({"peaks": {"A": 100.0}}))
    st = run_once(str(paper), str(status), CostConfig(), 0.2, 0.1, 0.9, act=True, now=now, quote_fn=quotes)
    assert json.loads(paper.read_text())["positions"] == {}
    assert any(a["kind"] == "STOP_SOLD" for a in st["alerts"])


def test_run_once_closed_market_makes_no_quote_calls(tmp_path):
    called = []
    run_once(str(_paper(tmp_path)), str(tmp_path / "m.json"), CostConfig(), 0.2, 0.1, 0.03, act=True,
             now=datetime(2026, 11, 7, 12, 0, tzinfo=IST), quote_fn=lambda s: called.append(s) or ({}, {}))
    assert called == []


def test_average_costs_after_partial_sell():
    t = [{"symbol": "A", "side": "BUY", "qty": 10, "fill_price": 100.0, "cost": 10.0},
         {"symbol": "A", "side": "SELL", "qty": 5, "fill_price": 120.0, "cost": 5.0}]
    assert average_costs(t)["A"] == pytest.approx(101.0)                  # (1000+10)/10, unchanged by selling


def test_summary_math(tmp_path):
    paper = _paper(tmp_path)
    mon = tmp_path / "mon.json"
    mon.write_text(json.dumps({"quotes": {"A": 110.0}, "peaks": {"A": 120.0}, "heartbeat": "x"}))
    s = build_summary(str(paper), str(mon), str(tmp_path / "none.csv"), str(tmp_path / "none2.csv"), 3)
    assert s["equity"] == pytest.approx(100.0 + 1100.0)                   # cash + 10 * 110
    assert s["pnl"] == pytest.approx(100.0) and s["next_rebalance"] == "2027-02-06"
    h = s["holdings"][0]
    assert h["price_source"] == "live" and h["from_peak_pct"] == pytest.approx(110 / 120 - 1)


def _serve(tmp_path, password=None, hosts=("localhost", "127.0.0.1")):
    paths = {"paper": str(_paper(tmp_path)), "monitor": str(tmp_path / "m.json"),
             "screen": str(tmp_path / "s.csv"), "prices": str(tmp_path / "p.csv")}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(), paths, set(hosts), password))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _get(port, path="/", headers=None, method="GET"):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_dashboard_serves_summary_and_is_read_only(tmp_path):
    srv, port = _serve(tmp_path)
    try:
        code, body = _get(port, "/api/summary")
        assert code == 200 and json.loads(body)["holdings"][0]["symbol"] == "A"
        assert _get(port)[0] == 200
        assert _get(port, "/nope")[0] == 404
        assert _get(port, "/api/summary", method="POST")[0] == 405
    finally:
        srv.shutdown()


def test_dashboard_rejects_foreign_host_and_enforces_password(tmp_path):
    srv, port = _serve(tmp_path, password="s3cret")
    try:
        assert _get(port, "/api/summary", {"Host": "evil.example"})[0] == 403          # DNS-rebinding guard
        assert _get(port, "/api/summary")[0] == 401                                      # no credentials
        bad = base64.b64encode(b"u:wrong").decode()
        assert _get(port, "/api/summary", {"Authorization": f"Basic {bad}"})[0] == 401
        good = base64.b64encode(b"u:s3cret").decode()
        assert _get(port, "/api/summary", {"Authorization": f"Basic {good}"})[0] == 200
    finally:
        srv.shutdown()


def test_serve_refuses_public_bind_without_password(tmp_path, monkeypatch):
    from agent.dashboard import serve

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        serve(Config(), {}, host="0.0.0.0")
