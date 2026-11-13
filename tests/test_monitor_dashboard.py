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

        # PWA endpoints are served publicly
        code, manifest = _get(port, "/manifest.json")
        assert code == 200 and "Alvi Quantitative Terminal" in manifest
        assert "standalone" in manifest

        code, sw = _get(port, "/sw.js")
        assert code == 200 and "CACHE_NAME" in sw

        code, svg = _get(port, "/favicon.svg")
        assert code == 200 and "<svg" in svg

        req192 = urllib.request.Request(f"http://127.0.0.1:{port}/icon-192.png")
        with urllib.request.urlopen(req192) as r:
            assert r.status == 200 and r.headers.get("Content-Type") == "image/png"
            assert len(r.read()) > 500

        req512 = urllib.request.Request(f"http://127.0.0.1:{port}/icon-512.png")
        with urllib.request.urlopen(req512) as r:
            assert r.status == 200 and r.headers.get("Content-Type") == "image/png"
            assert len(r.read()) > 1000
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
        assert _get(port, "/api/summary", {"Host": "100.127.177.5:8765", "Authorization": f"Basic {good}"})[0] == 200
        assert _get(port, "/api/summary", {"Host": "100.87.146.109", "Authorization": f"Basic {good}"})[0] == 200
        assert _get(port, "/api/summary", {"Host": "captain-new.ts.net", "Authorization": f"Basic {good}"})[0] == 200
    finally:
        srv.shutdown()


def test_serve_refuses_public_bind_without_password(tmp_path, monkeypatch):
    from agent.dashboard import serve

    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        serve(Config(), {}, host="0.0.0.0")


def test_dashboard_login_page_session_and_custom_username(tmp_path):
    paths = {"paper": str(_paper(tmp_path)), "monitor": str(tmp_path / "m.json"),
             "screen": str(tmp_path / "s.csv"), "prices": str(tmp_path / "p.csv")}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(), paths, {"localhost", "127.0.0.1"}, "s3cret", username="kashif"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    try:
        # GET /login returns login page
        code, body = _get(port, "/login")
        assert code == 200 and "Alvi Quantitative Terminal" in body
        assert "Sign In to Terminal" in body

        # Unauthenticated GET / returns login page
        code, body = _get(port, "/")
        assert code == 200 and "Alvi Quantitative Terminal" in body

        # Unauthenticated GET /api/summary returns 401
        code, _ = _get(port, "/api/summary")
        assert code == 401

        # POST /api/login with wrong user returns 401
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"username": "admin", "password": "s3cret"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            assert False, "should have failed with 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # POST /api/login with wrong password returns 401
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"username": "kashif", "password": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            assert False, "should have failed with 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401

        # POST /api/login with correct credentials returns 200 and Set-Cookie
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login",
            data=json.dumps({"username": "kashif", "password": "s3cret"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            cookie_header = resp.headers.get("Set-Cookie")
            assert cookie_header is not None and "agent_session=" in cookie_header
            token_part = [p for p in cookie_header.split(";") if p.strip().startswith("agent_session=")][0]

        # Access /api/summary using session cookie
        code, body = _get(port, "/api/summary", headers={"Cookie": token_part})
        assert code == 200
        summary = json.loads(body)
        assert summary["auth_user"] == "kashif"

        # POST /api/logout clears cookie
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/logout", method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            logout_cookie = resp.headers.get("Set-Cookie")
            assert logout_cookie is not None and "Max-Age=0" in logout_cookie

        # Basic Auth: kashif:s3cret works, admin:s3cret fails
        bad_auth = base64.b64encode(b"admin:s3cret").decode()
        assert _get(port, "/api/summary", {"Authorization": f"Basic {bad_auth}"})[0] == 401
        good_auth = base64.b64encode(b"kashif:s3cret").decode()
        assert _get(port, "/api/summary", {"Authorization": f"Basic {good_auth}"})[0] == 200
    finally:
        srv.shutdown()


def test_build_summary_live_mode_excludes_dummy_paper_trades(tmp_path):
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({
        "cash": 1000.0,
        "positions": {"SHYAMMETL": 3},
        "trades": [
            {"ts": "2026-11-06", "symbol": "DUMMY1", "side": "BUY", "qty": 4, "fill_price": 200.0, "cost": 5.0},
            {"ts": "2026-11-06", "symbol": "DUMMY2", "side": "BUY", "qty": 2, "fill_price": 400.0, "cost": 5.0},
        ]
    }))
    mon = tmp_path / "mon.json"
    mon.write_text(json.dumps({
        "heartbeat": "2026-11-11T20:00:00+05:30",
        "market_open": False,
        "live_mode": True,
        "act": True,
    }))
    screen = tmp_path / "screen.csv"
    screen.write_text("symbol,compliant\nSHYAMMETL,True\n")
    prices = tmp_path / "prices.csv"
    prices.write_text("date,SHYAMMETL\n2026-11-06,1330.0\n")

    summary = build_summary(str(paper), str(mon), str(screen), str(prices), 3, broker_mode="live")
    assert summary["broker_mode"] == "live"
    assert summary["monitor"]["live_mode"] is True
    # Dummy paper trades must NOT leak into live trades list
    assert all(t.get("symbol") not in ("DUMMY1", "DUMMY2") for t in summary["trades"])


def test_build_summary_includes_holdings_charts(tmp_path):
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({"cash": 1000.0, "positions": {}, "trades": []}))
    mon = tmp_path / "mon.json"
    mon.write_text(json.dumps({}))
    screen = tmp_path / "screen.csv"
    screen.write_text("symbol,compliant\n")
    prices = tmp_path / "prices.csv"
    prices.write_text("date\n")

    summary = build_summary(str(paper), str(mon), str(screen), str(prices), 3)
    assert "charts" in summary
    assert isinstance(summary["charts"], dict)
    assert "SHYAMMETL" in summary["charts"]
    shyam = summary["charts"]["SHYAMMETL"]
    assert "meta" in shyam and "history" in shyam
    assert shyam["meta"]["buy_price"] == 1320.50
    assert shyam["meta"]["buy_date"] == "2026-11-09"
    assert shyam["meta"]["qty"] == 3
    assert len(shyam["history"]) > 30


