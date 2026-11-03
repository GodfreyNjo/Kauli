"""Kauli's remote MCP endpoint (POST /mcp) against a throwaway database.
Uses the same isolated fixture as the REST API tests: no real DB, providers or
network, and every call authenticates with a real API key."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.test_public_api import KEY, api, wav_bytes  # noqa: E402,F401  (fixtures)

AUTH = {"Authorization": f"Bearer {KEY}"}


def rpc(api, method, params=None, rid=1, headers=AUTH):
    msg = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        msg["params"] = params
    return api.client.post("/mcp", json=msg, headers=headers)


def call(api, tool, **args):
    r = rpc(api, "tools/call", {"name": tool, "arguments": args})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def fake_youtube(api, wav_bytes):
    def fake(url, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / "video.wav"
        path.write_bytes(wav_bytes)
        return path, "Some YouTube video", "abc123XYZ00"
    api.app._download_youtube = fake
    return fake


def test_initialize_works_without_a_key_and_advertises_tools(api):
    r = rpc(api, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "1"}}, headers={})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "tools" in result["capabilities"] and result["serverInfo"]["name"] == "kauli"


def test_an_unknown_protocol_version_gets_the_latest_we_support(api):
    r = rpc(api, "initialize", {"protocolVersion": "1999-01-01"}, headers={})
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"


def test_tools_need_a_key_and_a_wrong_key_is_401(api):
    r = rpc(api, "tools/list", headers={})
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer")
    r = rpc(api, "tools/list", headers={"Authorization": "Bearer kauli_live_nope"})
    assert r.status_code == 401


def test_tools_list_names_and_schemas(api):
    tools = rpc(api, "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == {"kauli_list_languages", "kauli_create_order", "kauli_get_order",
                                          "kauli_list_orders", "kauli_get_transcript"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object" and t["description"]
    read_only = {t["name"]: t["annotations"]["readOnlyHint"] for t in tools}
    assert read_only["kauli_create_order"] is False and read_only["kauli_get_order"] is True


def test_notifications_get_202_and_unknown_methods_an_error(api):
    r = api.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=AUTH)
    assert r.status_code == 202 and r.content == b""
    assert rpc(api, "nonsense/method").json()["error"]["code"] == -32601
    assert rpc(api, "ping", headers={}).json()["result"] == {}


def test_batches_and_bad_bodies(api):
    r = api.client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}], headers=AUTH)
    assert [x["id"] for x in r.json()] == [1, 2]
    assert api.client.post("/mcp", content=b"not json", headers=AUTH).status_code == 400
    assert api.client.post("/mcp", json=[], headers=AUTH).status_code == 400


def test_get_is_not_supported(api):
    assert api.client.get("/mcp").status_code == 405


def test_list_languages_tool(api):
    langs = {x["code"] for x in payload(call(api, "kauli_list_languages"))["languages"]}
    assert {"sw", "en", "ki", "fr"} <= langs


def test_create_order_from_a_link_then_read_it_back(api, fake_youtube):
    res = call(api, "kauli_create_order", url="https://www.youtube.com/watch?v=abc123XYZ00", source_lang="en")
    assert res["isError"] is False
    order = payload(res)
    assert order["review_tier"] == "ai" and order["service_level"] == "transcribe"
    assert api.submitted == [order["id"]]
    again = payload(call(api, "kauli_get_order", order_id=order["id"]))
    assert again["id"] == order["id"]
    listed = payload(call(api, "kauli_list_orders", limit=5))["orders"]
    assert [o["id"] for o in listed] == [order["id"]]


def test_an_order_needing_payment_is_not_an_error_but_carries_the_link(api, fake_youtube):
    res = call(api, "kauli_create_order", url="https://youtu.be/abc123XYZ00", source_lang="en", review_tier="human")
    assert res["isError"] is False
    body = payload(res)
    assert body["payment_required"] is True and body["payment_url"].endswith("/pay")
    assert api.submitted == []


def test_rule_violations_come_back_as_tool_errors(api, fake_youtube):
    res = call(api, "kauli_create_order", url="https://youtu.be/abc123XYZ00", source_lang="fr", review_tier="human")
    assert res["isError"] is True and "Instant AI" in payload(res)["error"]
    assert call(api, "kauli_create_order", url="", source_lang="en")["isError"] is True
    assert api.db.list_orders_for_client("u1") == []


def test_unknown_or_foreign_orders_are_not_found(api):
    assert call(api, "kauli_get_order", order_id="deadbeef00")["isError"] is True
    assert call(api, "kauli_get_transcript", order_id="deadbeef00")["isError"] is True
    r = rpc(api, "tools/call", {"name": "kauli_made_up", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


def test_transcript_is_refused_until_a_plan_is_chosen_and_for_unpaid_orders(api, fake_youtube):
    free = payload(call(api, "kauli_create_order", url="https://youtu.be/abc123XYZ00", source_lang="en"))
    assert "isn't ready" in call(api, "kauli_get_transcript", order_id=free["id"])["content"][0]["text"]
    api.db.update_order_status(free["id"], "ready_for_delivery")
    res = call(api, "kauli_get_transcript", order_id=free["id"])
    assert res["isError"] is True and "Subscribe to the Free plan" in res["content"][0]["text"]
    held = payload(call(api, "kauli_create_order", url="https://youtu.be/abc123XYZ00", source_lang="en",
                        review_tier="human"))
    assert "waiting for payment" in call(api, "kauli_get_transcript", order_id=held["id"])["content"][0]["text"]


def test_a_paid_finished_order_returns_its_subtitle_text(api, fake_youtube):
    order = payload(call(api, "kauli_create_order", url="https://youtu.be/abc123XYZ00", source_lang="en"))
    row = api.db.get_order(order["id"])
    api.db.set_plan_chosen("u1")
    api.db.set_order_free_preview(order["id"], False)
    api.db.update_order_status(order["id"], "ready_for_delivery")
    outdir = Path(row["outdir"])
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "transcript_en.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHello from Kauli.\n", encoding="utf-8")
    res = call(api, "kauli_get_transcript", order_id=order["id"], format="srt")
    assert res["isError"] is False and "Hello from Kauli." in res["content"][0]["text"]
    assert call(api, "kauli_get_transcript", order_id=order["id"], format="vtt")["isError"] is True
    assert call(api, "kauli_get_transcript", order_id=order["id"], format="pdf")["isError"] is True
