"""Tests for the localhost web UI."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from fakes import FakeRedis  # noqa: E402

from nbs_agents.agentlog import AgentLog, assistant_uid  # noqa: E402
from nbs_agents.cli.core import Beamline  # noqa: E402
from nbs_agents.cli.kb import KnowledgeBase  # noqa: E402
from nbs_agents.cli.refs import ReferenceLibrary  # noqa: E402
from nbs_agents.web import create_app  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}


class FakeBeamline:
    split_queue = staticmethod(Beamline.split_queue)

    def __init__(self, queue=(), running=None, read_only=False):
        self.config = SimpleNamespace(qs_user="web-user", qs_user_group="primary", read_only=read_only)
        self.agent_log = AgentLog(FakeRedis(), "bl-")
        self.manager = MagicMock()
        added = [{"item_uid": "op"}, {"item_uid": "qs"}, {"item_uid": "ca"}]
        self.manager.item_add_batch.return_value = {"success": True, "items": added}
        self.manager.queue_get.return_value = {
            "success": True,
            "items": list(queue) + added,
            "running_item": running or {},
        }
        self.manager.status.return_value = {"manager_state": "idle", "items_in_queue": len(queue)}
        self.manager.queue_start.return_value = {"success": True}
        self.beam_value = {"endstation": "es", "expected": False, "reason": "shutter closed"}

    def beam(self):
        return self.beam_value


def client_for(bl, attachments=None, kb=None, refs=None):
    app = create_app(bl, allowed_hosts=("testserver",), attachments=attachments, kb=kb, refs=refs)
    return TestClient(app, base_url="http://testserver")


def test_snapshot_splits_sessions_from_assistant_and_shows_block():
    block_item = {"item_uid": "x1", "name": "xas", "item_type": "plan", "kwargs": {"edge": "C"},
                  "meta": {"agent_uid": "u1", "agent_reason": "check carbon"}}
    stop = {"item_uid": "s", "name": "queue_stop", "item_type": "instruction"}
    close = {"item_uid": "c", "name": "close_agent", "item_type": "plan", "args": ["u1"]}
    running = {"item_uid": "r", "name": "count", "item_type": "plan", "meta": {"agent_uid": "u1"}}
    bl = FakeBeamline(queue=[block_item, stop, close], running=running)
    bl.agent_log.open("u1", "claude", goal="map carbon")
    bl.agent_log.append("u1", "note", "hello")
    bl.agent_log.ensure_assistant_record()

    snap = client_for(bl).get("/api/snapshot").json()
    assert list(snap["sessions"]) == ["u1"]
    assert snap["assistant"]["kind"] == "assistant"
    assert snap["assistant_uid"] == assistant_uid()
    assert snap["beam"]["reason"] == "shutter closed"
    assert snap["manager"]["state"] == "idle"
    assert snap["running"]["agent_uid"] == "u1"
    assert snap["blocks"]["u1"] == [
        {"item_uid": "x1", "name": "xas", "kwargs": {"edge": "C"}, "reason": "check carbon"}
    ]
    assert snap["runner_alive"] is False
    assert snap["errors"] == {}


def test_snapshot_reports_unreachable_sources():
    bl = FakeBeamline()
    bl.manager.status.side_effect = TimeoutError("no queueserver")

    def broken_beam():
        raise RuntimeError("no redis")

    bl.beam = broken_beam
    snap = client_for(bl).get("/api/snapshot").json()
    assert snap["manager"] is None
    assert "no queueserver" in snap["errors"]["queue"]
    assert "no redis" in snap["errors"]["beam"]


def test_message_and_stop_write_operator_entries():
    bl = FakeBeamline()
    bl.agent_log.open("u1", "claude", goal="g")
    client = client_for(bl)
    assert client.post("/api/agents/u1/message", json={"text": " go on "}, headers=ORIGIN).status_code == 200
    assert client.post("/api/agents/u1/stop", json={"text": "enough"}, headers=ORIGIN).status_code == 200
    record = bl.agent_log.get("u1")
    assert [(e["kind"], e["text"], e["source"]) for e in record["log"]] == [
        ("operator", "go on", "web"),
        ("stop", "enough", "web"),
    ]
    assert record["stop_requested"]["text"] == "enough"
    assert client.post("/api/agents/u1/stop", json={}, headers=ORIGIN).status_code == 409
    assert client.post("/api/agents/u1/message", json={"text": " "}, headers=ORIGIN).status_code == 400
    assert client.post("/api/agents/nope/message", json={"text": "hi"}, headers=ORIGIN).status_code == 404


def test_closed_session_and_assistant_record_are_not_steerable():
    bl = FakeBeamline()
    bl.agent_log.open("u1", "claude", status="closed")
    uid = bl.agent_log.ensure_assistant_record()
    client = client_for(bl)
    assert client.post("/api/agents/u1/message", json={"text": "hi"}, headers=ORIGIN).status_code == 409
    assert client.post(f"/api/agents/{uid}/message", json={"text": "hi"}, headers=ORIGIN).status_code == 404


def test_ask_assistant_posts_to_todays_record():
    bl = FakeBeamline()
    client = client_for(bl)
    assert client.post("/api/assistant/ask", json={"text": "how is S1?"}, headers=ORIGIN).status_code == 200
    log = bl.agent_log.get(assistant_uid())["log"]
    assert log[-1]["kind"] == "operator" and log[-1]["text"] == "how is S1?"


def test_new_agent_needs_runner_and_confirms_busy_sessions():
    bl = FakeBeamline()
    client = client_for(bl)
    body = {"goal": "Map the C K-edge", "budget": "2h"}
    res = client.post("/api/agents", json=body, headers=ORIGIN)
    assert res.status_code == 409 and "not running" in res.json()["detail"]

    bl.agent_log.heartbeat()
    assert client.post("/api/agents", json={"goal": " "}, headers=ORIGIN).status_code == 400
    res = client.post("/api/agents", json=body, headers=ORIGIN)
    assert res.json() == {"started": True, "position": 0, "item_uids": ["op", "qs", "ca"]}
    block, kwargs = bl.manager.item_add_batch.call_args
    assert block[0][0].args[1] == {"goal": "Map the C K-edge", "launch": True, "budget": {"time_s": 7200.0}}
    assert kwargs == {"user": "web-user", "user_group": "primary"}

    bl.agent_log.open("u1", "claude", goal="g")
    res = client.post("/api/agents", json=body, headers=ORIGIN)
    assert res.status_code == 409 and res.json()["detail"] == {"busy": ["u1"]}
    assert client.post("/api/agents", json={**body, "confirm_busy": True}, headers=ORIGIN).status_code == 200


def test_new_agent_refused_for_read_only_role():
    bl = FakeBeamline(read_only=True)
    bl.agent_log.heartbeat()
    res = client_for(bl).post("/api/agents", json={"goal": "g"}, headers=ORIGIN)
    assert res.status_code == 403


def test_guard_refuses_foreign_hosts_and_cross_origin_writes():
    bl = FakeBeamline()
    bl.agent_log.open("u1", "claude")
    client = client_for(bl)
    assert client.get("/api/snapshot", headers={"Host": "evil.example"}).status_code == 403
    assert client.post("/api/agents/u1/message", json={"text": "hi"}).status_code == 403
    res = client.post("/api/agents/u1/message", json={"text": "hi"}, headers={"Origin": "http://evil.example"})
    assert res.status_code == 403
    assert client.get("/").status_code == 200


def test_attachments_are_served_with_safe_headers(tmp_path):
    from nbs_agents.assistant.attachments import AttachmentStore

    store = AttachmentStore(tmp_path)
    info = store.put(b"<svg xmlns='http://www.w3.org/2000/svg'/>", "image/svg+xml", alt="plot")
    client = client_for(FakeBeamline(), attachments=store)
    res = client.get(f"/api/attachments/{info['id']}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in res.headers["content-security-policy"]
    assert client.get("/api/attachments/0000000000000000").status_code == 404
    assert client.get("/api/attachments/..%2F..%2Fsecret").status_code == 404


def test_token_is_required_and_sets_a_cookie(nbs_agents_token):
    bl = FakeBeamline()
    bl.agent_log.open("u1", "claude", goal="g")
    app = create_app(bl, allowed_hosts=("testserver",), token=nbs_agents_token)
    client = TestClient(app, base_url="http://testserver")

    page = client.get("/")
    assert page.status_code == 401 and "Sign in" in page.text
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/api/events").status_code == 401
    assert client.post("/api/agents/u1/message", json={"text": "hi"}, headers=ORIGIN).status_code == 401
    assert client.get("/?token=wrong").status_code == 401
    assert client.post("/login", data={"token": "wrong"}, headers=ORIGIN).status_code == 401

    bearer = {"Authorization": f"Bearer {nbs_agents_token}"}
    assert client.get("/api/snapshot", headers=bearer).status_code == 200

    res = client.get(f"/?token={nbs_agents_token}", follow_redirects=False)
    assert res.status_code == 303 and res.headers["location"] == "/"
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/").status_code == 200
    assert client.get("/api/snapshot").status_code == 200
    assert client.post("/api/agents/u1/message", json={"text": "hi"}, headers=ORIGIN).status_code == 200

    other = TestClient(app, base_url="http://testserver")
    res = other.post("/login", data={"token": nbs_agents_token}, headers=ORIGIN, follow_redirects=False)
    assert res.status_code == 303
    assert other.get("/api/snapshot").status_code == 200


def test_web_snapshot_shows_pending_login():
    bl = FakeBeamline()
    bl.agent_log.heartbeat()
    bl.agent_log.set_tiled_login("https://login.example/device", "ABCD-EFGH")
    snap = client_for(bl).get("/api/snapshot").json()
    assert snap["tiled_login"]["code"] == "ABCD-EFGH"
    bl.agent_log.set_tiled_login(None)
    assert client_for(bl).get("/api/snapshot").json()["tiled_login"] is None


def test_staff_review_knowledge_base(tmp_path):
    kb = KnowledgeBase(tmp_path / "knowledge", tmp_path / "agent", tmp_path / "review")
    kb.add("Slit backlash", "Approach eslit from wide.", "assistant", draft=True)
    kb.add("Wrong idea", "nonsense", "assistant")
    client = client_for(FakeBeamline(), kb=kb)
    assert client.get("/api/snapshot").json()["kb_pending"] == 2
    overview = client.get("/api/kb").json()
    assert [n["id"] for n in overview["queue"]["notes"]] == ["draft/slit-backlash", "agent/wrong-idea"]
    assert client.get("/api/kb/note", params={"id": "draft/slit-backlash"}).json()["body"].strip() == \
        "Approach eslit from wide."

    # A name is required, and refusals come back as 400
    r = client.post("/api/kb/approve", json={"id": "draft/slit-backlash", "by": " "}, headers=ORIGIN)
    assert r.status_code == 400
    r = client.post("/api/kb/approve", json={"id": "draft/slit-backlash", "by": "Ana", "body": "Always wide.",
                                             "devices": "eslit, en"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "slit-backlash"
    assert kb.get("slit-backlash").meta["devices"] == ["eslit", "en"]
    r = client.post("/api/kb/reject", json={"id": "agent/wrong-idea", "by": "Ana", "reason": "no"}, headers=ORIGIN)
    assert r.status_code == 200
    assert client.get("/api/snapshot").json()["kb_pending"] == 0

    r = client.post("/api/kb/note", json={"by": "Ana", "title": "Gate valves", "body": "Open after pumping."},
                    headers=ORIGIN)
    assert r.json()["id"] == "gate-valves"
    kb.append("gate-valves", "took 20 min", "assistant")
    kb.append("gate-valves", "wrong", "assistant")
    assert client.get("/api/kb").json()["queue"]["count"] == 2
    r = client.post("/api/kb/appendix/remove", json={"id": "gate-valves", "by": "Ana", "index": 1},
                    headers=ORIGIN)
    assert r.status_code == 200
    client.post("/api/kb/appendix/reviewed", json={"id": "gate-valves", "by": "Ana"}, headers=ORIGIN)
    assert client.get("/api/kb").json()["queue"]["count"] == 0
    # Writes from other pages are refused like every other write
    assert client.post("/api/kb/note", json={"by": "x", "title": "t", "body": "b"}).status_code == 403


def test_knowledge_base_search(tmp_path):
    kb = KnowledgeBase(tmp_path / "knowledge", tmp_path / "agent", tmp_path / "review")
    kb.write_note("Ana", "Gate valves", "Open the gate valves after pumping down.")
    kb.write_note("Ana", "Beam dumps", "The ring current reads 0.")
    client = client_for(FakeBeamline(), kb=kb)
    hits = client.get("/api/kb/search", params={"q": "valve"}).json()
    assert [h["id"] for h in hits] == ["gate-valves"]
    assert "pumping" in hits[0]["snippet"]
    assert len(client.get("/api/kb/search", params={"q": ""}).json()) == 2


def ref_draft(refs, title="M3 pitch", uid="4f1c0a7e-0000", scan_id=81234, request=""):
    run = {"uid": uid, "scan_id": scan_id, "catalog": "default", "tiled_uri": "https://t", "tiled_path": "bl/raw"}
    return refs.create(title, "Rocking curve.", "assistant", run, {"baseline": {"ring_current": 400.0}},
                       request=request, devices=["m3_pitch"])


def test_staff_review_reference_scans(tmp_path):
    refs = ReferenceLibrary(tmp_path / "references", tmp_path / "agent", tmp_path / "review")
    ref_draft(refs)
    ref_draft(refs, title="Bad one", uid="dead")
    bl = FakeBeamline()
    bl.config.tiled_uri, bl.config.tiled_path = "https://t", "bl/raw"
    client = client_for(bl, refs=refs)
    assert client.get("/api/snapshot").json()["ref_pending"] == 2
    overview = client.get("/api/refs").json()
    assert [d["id"] for d in overview["queue"]["drafts"]] == ["draft/bad-one", "draft/m3-pitch"]
    assert overview["catalogs"]["default"] == {"uri": "https://t", "path": "bl/raw"}
    entry = client.get("/api/refs/entry", params={"id": "draft/m3-pitch"}).json()
    assert entry["snapshot"]["baseline"] == {"ring_current": 400.0}
    r = client.post("/api/refs/approve", json={"id": "draft/m3-pitch", "by": " "}, headers=ORIGIN)
    assert r.status_code == 400
    r = client.post("/api/refs/approve", json={"id": "draft/m3-pitch", "by": "Ana", "body": "Staff text.",
                                               "tags": "alignment, m3"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["id"] == "m3-pitch"
    assert refs.get("m3-pitch").meta["tags"] == ["alignment", "m3"]
    assert (tmp_path / "references" / "m3-pitch.snapshot.json").is_file()
    r = client.post("/api/refs/reject", json={"id": "draft/bad-one", "by": "Ana", "reason": "no"}, headers=ORIGIN)
    assert r.status_code == 200
    assert client.get("/api/snapshot").json()["ref_pending"] == 0
    # A newer scan supersedes the entry
    ref_draft(refs, title="M3 pitch after bake", uid="beef", scan_id=90000)
    r = client.post("/api/refs/approve", json={"id": "draft/m3-pitch-after-bake", "by": "Ana",
                                               "supersedes": "m3-pitch"}, headers=ORIGIN)
    assert r.status_code == 200
    assert refs.get("m3-pitch").meta["superseded_by"] == "m3-pitch-after-bake"
    hits = client.get("/api/refs/search", params={"q": "pitch"}).json()
    assert {h["id"]: h["status"] for h in hits} == {"m3-pitch": "superseded", "m3-pitch-after-bake": "current"}
    r = client.post("/api/refs/status", json={"id": "m3-pitch", "by": "Ana", "status": "retired"}, headers=ORIGIN)
    assert r.json()["status"] == "retired"
    r = client.post("/api/refs/entry", json={"id": "m3-pitch-after-bake", "by": "Ben", "title": "M3 pitch (baked)"},
                    headers=ORIGIN)
    assert r.json()["title"] == "M3 pitch (baked)"
    assert client.post("/api/refs/reject", json={"id": "draft/x", "by": "x"}).status_code == 403


def test_request_reference_draft_asks_the_assistant(tmp_path):
    refs = ReferenceLibrary(tmp_path / "references", tmp_path / "agent", tmp_path / "review")
    bl = FakeBeamline()
    bl.config.tiled_uri, bl.config.tiled_path = "https://t", "bl/raw"
    bl.config.ref_catalogs = {"eng": {"uri": "https://t", "path": "eng/raw"}}
    client = client_for(bl, refs=refs)
    r = client.post("/api/refs/request", json={"by": "Ana", "catalog": "nope", "run": "81234", "purpose": "x"},
                    headers=ORIGIN)
    assert r.status_code == 400 and "default, eng" in r.json()["detail"]
    r = client.post("/api/refs/request", json={"by": "Ana", "catalog": "eng", "run": "81234",
                                               "purpose": "M3 alignment reference"}, headers=ORIGIN)
    assert r.status_code == 200
    request = r.json()
    entry = bl.agent_log.get(assistant_uid())["log"][-1]
    assert entry["kind"] == "operator" and entry["source"] == "web"
    assert request["id"] in entry["text"]
    assert f"nbs-cli ref create 81234 --catalog eng --request {request['id']}" in entry["text"]
    assert "M3 alignment reference" in entry["text"]
    assert client.get("/api/snapshot").json()["ref_pending"] == 1
    assert [q["id"] for q in client.get("/api/refs").json()["queue"]["requests"]] == [request["id"]]
    # The assistant's draft answers it
    run = {"uid": "4f1c", "scan_id": 81234, "catalog": "eng", "tiled_uri": "https://t", "tiled_path": "eng/raw"}
    refs.create("M3 pitch", "Rocking curve.", "assistant", run, {}, request=request["id"])
    assert client.get("/api/refs").json()["queue"]["requests"] == []
    # Cancelling one that was never answered
    r = client.post("/api/refs/request", json={"by": "Ana", "run": "5", "purpose": "y"}, headers=ORIGIN)
    other = r.json()["id"]
    r = client.post("/api/refs/request/cancel", json={"id": other, "by": "Ana"}, headers=ORIGIN)
    assert r.status_code == 200
    assert client.get("/api/refs").json()["queue"]["requests"] == []

