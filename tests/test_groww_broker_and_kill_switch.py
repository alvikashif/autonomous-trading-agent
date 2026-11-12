import json
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
import threading
import pytest

from agent.broker import GrowwBroker, get_last_6am_ist, is_token_fresh, IST
from agent.config import Config, BrokerConfig
from agent.dashboard import make_handler
from agent.kill_switch import (
    get_kill_switch_status,
    is_kill_switch_active,
    reset_kill_switch,
    trigger_kill_switch,
)
from agent.models import Order, Side
from agent.risk import RiskLimits, check_orders
from agent.costs import CostConfig


def test_6am_ist_reset_calculation():
    # If time is 10:00 AM IST today, last 6 AM is 06:00 AM today
    dt_10am = datetime(2026, 11, 8, 10, 0, tzinfo=IST)
    last_6am = get_last_6am_ist(dt_10am)
    assert last_6am == datetime(2026, 11, 8, 6, 0, tzinfo=IST)

    # If time is 05:00 AM IST today, last 6 AM is 06:00 AM yesterday
    dt_5am = datetime(2026, 11, 8, 5, 0, tzinfo=IST)
    last_6am_yesterday = get_last_6am_ist(dt_5am)
    assert last_6am_yesterday == datetime(2026, 11, 7, 6, 0, tzinfo=IST)


def test_token_freshness():
    now = datetime(2026, 11, 8, 10, 0, tzinfo=IST)
    # Token created at 07:00 AM today -> Fresh
    fresh_time = datetime(2026, 11, 8, 7, 0, tzinfo=IST).isoformat()
    assert is_token_fresh(fresh_time, now=now) is True

    # Token created at 05:30 AM today -> Expired (before 6 AM reset)
    stale_time = datetime(2026, 11, 8, 5, 30, tzinfo=IST).isoformat()
    assert is_token_fresh(stale_time, now=now) is False

    # Token created yesterday -> Expired
    yesterday_time = datetime(2026, 11, 7, 15, 0, tzinfo=IST).isoformat()
    assert is_token_fresh(yesterday_time, now=now) is False


def test_groww_broker_mocked_calls(tmp_path, monkeypatch):
    cache_path = tmp_path / "token.json"

    class FakeGrowwClient:
        def __init__(self, token):
            self.token = token
            self.orders_placed = []

        def get_available_margin_details(self):
            return {
                "clear_cash": 15000.0,
                "equity_margin_details": {"cnc_balance_available": 12500.0},
            }

        def get_holdings_for_user(self):
            return {
                "payload": {
                    "holdings": [
                        {"trading_symbol": "INFY", "quantity": 10},
                        {"trading_symbol": "TCS", "quantity": 5},
                    ]
                }
            }

        def place_order(self, **kwargs):
            self.orders_placed.append(kwargs)
            return {"status": "SUCCESS", "order_id": "GROWW123"}

    # Mock get_access_token
    import growwapi
    monkeypatch.setattr(
        growwapi.GrowwAPI, "get_access_token", lambda api_key, totp: "mock_access_token_xyz"
    )

    broker = GrowwBroker(
        api_key="TEST_API_KEY",
        totp_secret="JBSWY3DPEHPK3PXP",  # valid base32 seed
        token_cache_path=cache_path,
    )

    # Monkeypatch client factory
    monkeypatch.setattr(broker, "_init_client", lambda token: setattr(broker, "_client", FakeGrowwClient(token)))

    # Authenticate
    token = broker.authenticate()
    assert token == "mock_access_token_xyz"
    assert cache_path.exists()
    assert json.loads(cache_path.read_text())["token"] == "mock_access_token_xyz"

    # Check cash
    cash = broker.cash()
    assert cash == 12500.0

    # Check positions
    pos = broker.positions()
    assert pos == {"INFY": 10, "TCS": 5}

    # Place Buy Order (LIMIT with +0.3% buffer rounded to NSE 0.05 tick size)
    order = Order("INFY", Side.BUY, 2, 1800.0)
    resp = broker.place_order(order)
    assert resp["status"] == "SUCCESS"
    placed = broker._client.orders_placed[0]
    assert placed["trading_symbol"] == "INFY"
    assert placed["quantity"] == 2
    assert placed["product"] == "CNC"
    assert placed["exchange"] == "NSE"
    assert placed["order_type"] == "LIMIT"
    assert placed["price"] == 1805.40

    # Place Sell Order (LIMIT with -0.3% buffer)
    sell_order = Order("INFY", Side.SELL, 2, 1800.0)
    broker.place_order(sell_order)
    placed_sell = broker._client.orders_placed[1]
    assert placed_sell["order_type"] == "LIMIT"
    assert placed_sell["price"] == 1794.60

    # Place Market Order explicitly
    mkt_order = Order("INFY", Side.BUY, 1, 0.0, order_type="MARKET")
    broker.place_order(mkt_order)
    placed_mkt = broker._client.orders_placed[2]
    assert placed_mkt["order_type"] == "MARKET"
    assert placed_mkt["price"] == 0.0


def test_groww_broker_auto_reauth_on_401(tmp_path, monkeypatch):
    cache_path = tmp_path / "token.json"

    call_count = {"margin": 0, "auth": 0}

    class FlakyGrowwClient:
        def __init__(self, token):
            self.token = token

        def get_available_margin_details(self):
            call_count["margin"] += 1
            if call_count["margin"] == 1:
                from growwapi.groww.exceptions import GrowwAPIAuthenticationException
                raise GrowwAPIAuthenticationException()
            return {"clear_cash": 8000.0}

    import growwapi
    def fake_get_token(api_key, totp):
        call_count["auth"] += 1
        return f"token_v{call_count['auth']}"

    monkeypatch.setattr(growwapi.GrowwAPI, "get_access_token", fake_get_token)

    broker = GrowwBroker(
        api_key="TEST_API_KEY",
        totp_secret="JBSWY3DPEHPK3PXP",
        token_cache_path=cache_path,
    )
    monkeypatch.setattr(broker, "_init_client", lambda token: setattr(broker, "_client", FlakyGrowwClient(token)))

    cash = broker.cash()
    assert cash == 8000.0
    assert call_count["margin"] == 2
    assert call_count["auth"] >= 1


def test_kill_switch_lifecycle(tmp_path):
    ks_path = tmp_path / "kill_switch.json"
    assert is_kill_switch_active(ks_path) is False

    st = trigger_kill_switch("High market volatility", path=ks_path)
    assert st["active"] is True
    assert is_kill_switch_active(ks_path) is True
    assert get_kill_switch_status(ks_path)["reason"] == "High market volatility"

    reset_kill_switch(path=ks_path)
    assert is_kill_switch_active(ks_path) is False
    assert get_kill_switch_status(ks_path)["active"] is False


def test_risk_vetoes_orders_when_kill_switch_active():
    orders = [Order("INFY", Side.BUY, 5, 1500.0)]
    approved, rejected = check_orders(
        orders=orders,
        cash=10000.0,
        positions={},
        prices={"INFY": 1500.0},
        sectors={"INFY": "IT"},
        equity=10000.0,
        limits=RiskLimits(),
        cost_cfg=CostConfig(),
        kill_switch_active=True,
    )
    assert len(approved) == 0
    assert len(rejected) == 1
    assert "kill switch is active" in rejected[0][1]


def test_dashboard_kill_switch_endpoints(tmp_path):
    paper = tmp_path / "paper.json"
    paper.write_text(json.dumps({"cash": 1000.0, "positions": {}}))
    ks_path = tmp_path / "kill_switch.json"

    paths = {
        "paper": str(paper),
        "monitor": str(tmp_path / "m.json"),
        "screen": str(tmp_path / "s.csv"),
        "prices": str(tmp_path / "p.csv"),
        "kill_switch": str(ks_path),
    }

    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Config(), paths, {"localhost", "127.0.0.1"}, None))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    try:
        # Check initial summary
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary") as r:
            data = json.loads(r.read().decode())
            assert data["kill_switch"]["active"] is False

        # POST /api/kill-switch/trigger
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/kill-switch/trigger", data=b"", method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res["active"] is True

        # Check summary reflects active kill switch
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary") as r:
            data = json.loads(r.read().decode())
            assert data["kill_switch"]["active"] is True

        # POST /api/kill-switch/reset
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/kill-switch/reset", data=b"", method="POST")
        with urllib.request.urlopen(req) as r:
            assert r.status == 200
            res = json.loads(r.read().decode())
            assert res["active"] is False

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/summary") as r:
            data = json.loads(r.read().decode())
            assert data["kill_switch"]["active"] is False
    finally:
        srv.shutdown()
