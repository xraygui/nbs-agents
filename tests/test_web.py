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


def test_recheck_asks_for_a_blind_second_look():
    bl = FakeBeamline()
    client = client_for(bl)
    uid = bl.agent_log.ensure_assistant_record()
    post = bl.agent_log.append(uid, "alert", "beam lost", source="assistant", model="sonnet")
    r = client.post("/api/assistant/recheck", json={"t": post["t"], "model": "opus"}, headers=ORIGIN)
    assert r.status_code == 200
    entry = bl.agent_log.get(uid)["log"][-1]
    assert entry["kind"] == "operator" and entry["source"] == "web"
    assert entry["recheck"] == {"t": post["t"], "model": "opus", "note": None}
    assert client.post("/api/assistant/recheck", json={"t": 1.0}, headers=ORIGIN).status_code == 404
    snap = client.get("/api/snapshot").json()
    assert snap["assistant"]["log"][0]["model"] == "sonnet"


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
    assert "nbs-cli run 81234 --catalog eng" in entry["text"]
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



# ---------------------------------------------------------------- role sidebar and manager threads

def roles_beamline(threads=True):
    from nbs_agents.cli.config import AssistantConfig, read_roles

    bl = FakeBeamline()
    bl.assistant = AssistantConfig(threads=threads, roles=read_roles({
        "manager": {"mode": "chat"}, "plotter": {"triggers": ["run"]}, "integrity": {"triggers": ["alert"]},
    }))
    return bl


def test_snapshot_has_tiles_role_records_threads_and_directives():
    from nbs_agents.directives import DirectiveStore
    from nbs_agents.threads import open_thread
    from nbs_agents.tiles import TileStore

    bl = roles_beamline()
    TileStore.for_agent_log(bl.agent_log).post("plotter", "ok", "scan 3 plotted", figure="att:0123456789abcdef")
    uid = bl.agent_log.ensure_assistant_record(role="plotter")
    bl.agent_log.append(uid, "tile", "scan 3 plotted", source="assistant:plotter")
    open_thread(bl.agent_log, "why is i0 low?", "jamie")
    DirectiveStore.for_agent_log(bl.agent_log).propose("integrity", "U6 charges; do not alert on it", {"kind": "beamtime"})
    snap = client_for(bl).get("/api/snapshot").json()
    assert snap["tiles"]["plotter"]["message"] == "scan 3 plotted"
    assert snap["roles"]["plotter"]["uid"] == uid
    assert snap["roles"]["plotter"]["log"][-1]["text"] == "scan 3 plotted"
    # The plotter's record is not the main assistant record
    assert all(e.get("source") != "assistant:plotter" for e in snap["assistant"]["log"])
    assert [t["messages"][0]["text"] for t in snap["threads"]] == ["why is i0 low?"]
    assert snap["directives"][0]["status"] == "proposed"
    assert snap["threads_on"] is True
    assert snap["errors"] == {}


def test_questions_open_threads_and_follow_ups_join_them():
    from nbs_agents.threads import ThreadStore
    from nbs_agents.tiles import TileStore

    bl = roles_beamline()
    client = client_for(bl)
    r = client.post("/api/assistant/ask", json={"text": "how is S1?", "by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200
    thread_id = r.json()["thread"]
    line = bl.agent_log.get(assistant_uid())["log"][-1]
    assert (line["kind"], line["thread"], line["by"], line["source"]) == ("operator", thread_id, "jamie", "web")
    r = client.post("/api/assistant/ask", json={"text": "and S2?", "thread": thread_id}, headers=ORIGIN)
    assert r.status_code == 200
    thread = ThreadStore.for_agent_log(bl.agent_log).get(thread_id)
    assert [m["text"] for m in thread["messages"]] == ["how is S1?", "and S2?"]
    # Only the question is in the main record
    assert len(bl.agent_log.get(assistant_uid())["log"]) == 1
    assert client.post("/api/assistant/ask", json={"text": "x", "thread": "nope"}, headers=ORIGIN).status_code == 404
    # About a post, and about a tile
    post = bl.agent_log.append(assistant_uid(), "alert", "i0 dropped", source="assistant")
    r = client.post("/api/assistant/ask", json={"text": "why?", "entry": post["t"]}, headers=ORIGIN)
    seed = ThreadStore.for_agent_log(bl.agent_log).get(r.json()["thread"])["seed"]
    assert seed["kind"] == "entry" and seed["entry"]["text"] == "i0 dropped"
    TileStore.for_agent_log(bl.agent_log).post("plotter", "warn", "flat signal")
    r = client.post("/api/assistant/ask", json={"text": "which keys?", "tile": "plotter"}, headers=ORIGIN)
    seed = ThreadStore.for_agent_log(bl.agent_log).get(r.json()["thread"])["seed"]
    assert seed == {"kind": "tile", "role": "plotter", "tile": TileStore.for_agent_log(bl.agent_log).get("plotter")}
    assert client.post("/api/assistant/ask", json={"text": "x", "tile": "health"}, headers=ORIGIN).status_code == 404
    two = {"text": "x", "tile": "plotter", "thread": thread_id}
    assert client.post("/api/assistant/ask", json=two, headers=ORIGIN).status_code == 400


def test_threads_off_keeps_questions_in_the_record():
    bl = roles_beamline(threads=False)
    client = client_for(bl)
    r = client.post("/api/assistant/ask", json={"text": "how is S1?"}, headers=ORIGIN)
    assert r.status_code == 200 and "thread" not in r.json()
    assert "thread" not in bl.agent_log.get(assistant_uid())["log"][-1]
    assert client.post("/api/assistant/ask", json={"text": "x", "tile": "plotter"}, headers=ORIGIN).status_code == 409


def test_acknowledging_a_sticky_alert_is_logged():
    from nbs_agents.tiles import TileStore

    bl = roles_beamline()
    client = client_for(bl)
    tiles = TileStore.for_agent_log(bl.agent_log)
    assert client.post("/api/tiles/plotter/ack", json={"by": "jamie"}, headers=ORIGIN).status_code == 409
    tiles.post("integrity", "alert", "edge inverted on U6")
    tiles.post("integrity", "ok", "fine again")
    assert tiles.get("integrity")["cleared"] is True
    r = client.post("/api/tiles/integrity/ack", json={"by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200
    tile = tiles.get("integrity")
    assert (tile["severity"], tile["message"], tile["last_ack"]["by"]) == ("ok", "fine again", "jamie")
    entry = bl.agent_log.get(assistant_uid(role="integrity"))["log"][-1]
    assert (entry["kind"], entry["by"], entry["source"]) == ("ack", "jamie", "web")
    assert client.post("/api/tiles/nobody/ack", json={}, headers=ORIGIN).status_code == 404


def test_directives_are_confirmed_rejected_and_revoked_by_a_person():
    from nbs_agents.directives import DirectiveStore

    bl = roles_beamline()
    client = client_for(bl)
    store = DirectiveStore.for_agent_log(bl.agent_log)
    a = store.propose("integrity", "U6 charges; do not alert on it", {"kind": "beamtime", "until": 9e12})
    b = store.propose("plotter", "use pey", {"kind": "duration", "until": 9e12})
    r = client.post(f"/api/directives/{a['id']}/decide", json={"confirm": True, "by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200 and store.get(a["id"])["status"] == "active"
    r = client.post(f"/api/directives/{b['id']}/decide", json={"confirm": False, "by": "jamie", "reason": "no"},
                    headers=ORIGIN)
    assert r.status_code == 200 and store.get(b["id"])["status"] == "rejected"
    # Decided already
    assert client.post(f"/api/directives/{b['id']}/decide", json={"confirm": True}, headers=ORIGIN).status_code == 409
    r = client.post(f"/api/directives/{a['id']}/revoke", json={"by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200 and store.get(a["id"])["status"] == "revoked"
    texts = [e["text"] for e in bl.agent_log.get(assistant_uid())["log"] if e["kind"] == "directive"]
    assert any("jamie confirmed" in t for t in texts) and any("jamie revoked" in t for t in texts)
    snap = client.get("/api/snapshot").json()
    assert snap["directives"] == []


def test_snapshot_keeps_each_roles_figures_for_flipping_back():
    bl = roles_beamline()
    uid = bl.agent_log.ensure_assistant_record(role="plotter")
    bl.agent_log.append(uid, "tile", "scan 1", source="assistant:plotter", figure="att:00000000000000a1")
    bl.agent_log.append(uid, "tile", "no figure", source="assistant:plotter")
    bl.agent_log.append(uid, "summary", "scan 2 ![x](att:00000000000000a2)", source="assistant:plotter")
    bl.agent_log.append(uid, "tile", "scan 2", source="assistant:plotter", figure="att:00000000000000a2")
    figs = client_for(bl).get("/api/snapshot").json()["roles"]["plotter"]["figures"]
    assert [(f["figure"], f["text"]) for f in figs] == [("att:00000000000000a1", "scan 1"), ("att:00000000000000a2", "scan 2")]


class StagingBeamline(FakeBeamline):
    """A beamline whose provider offers nbs-gui's staging queue."""

    def __init__(self):
        super().__init__()
        from fakes import StaticInfo
        from nbs_agents.staging import StagingStore

        self.redis = FakeRedis()
        self.info = StaticInfo(staging=(self.redis, "bl-"))
        self._store = StagingStore(self.redis, "bl-")

    @property
    def staging(self):
        return self._store


def test_staging_proposals_show_and_are_accepted_or_rejected_by_a_person():
    import json

    bl = StagingBeamline()
    bl.redis.set("bl-staging:queue", json.dumps({"version": 2, "updated": 1.0, "updated_by": "nbs-gui:ws1:jamie", "items": [
        {"item_uid": "a", "item_type": "plan", "name": "nbs_count", "args": [], "kwargs": {"num": 1}}]}))
    store = bl.staging
    add = store.propose([{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 3}}, "after_uid": None}],
                        "three more counts", "assistant:manager")
    drop = store.propose([{"op": "remove", "item_uid": "a"}], "drop a", "assistant:manager")
    client = client_for(bl)
    snap = client.get("/api/snapshot").json()
    assert snap["staging"]["version"] == 2
    assert [i["item_uid"] for i in snap["staging"]["items"]] == ["a"]
    assert {p["id"] for p in snap["staging"]["proposals"]} == {add["id"], drop["id"]}
    assert all(p["stale"] is False for p in snap["staging"]["proposals"])
    r = client.post(f"/api/staging/{add['id']}/decide", json={"accept": True, "by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200
    queue = store.queue()
    assert queue["version"] == 3 and queue["updated_by"] == "web:jamie"
    assert [i["kwargs"]["num"] for i in queue["items"]] == [1, 3]
    r = client.post(f"/api/staging/{drop['id']}/decide", json={"accept": False, "by": "jamie"}, headers=ORIGIN)
    assert r.status_code == 200 and store.get(drop["id"])["status"] == "rejected"
    assert store.queue()["version"] == 3
    assert client.post(f"/api/staging/{drop['id']}/decide", json={"accept": True}, headers=ORIGIN).status_code == 409
    # Decided ones stay for the conversation, marked
    statuses = {p["id"]: p["status"] for p in client.get("/api/snapshot").json()["staging"]["proposals"]}
    assert statuses == {add["id"]: "accepted", drop["id"]: "rejected"}


def test_staging_proposal_modified_by_a_person_and_noted_in_its_thread():
    from nbs_agents.threads import ThreadStore, open_thread

    bl = StagingBeamline()
    checked = []

    def check_plan(name, args, kwargs):
        checked.append((name, kwargs))
        return {"errors": [] if kwargs.get("num", 0) < 100 else ["num too large"]}

    bl.check_plan = check_plan
    thread = open_thread(bl.agent_log, "stage three counts", "jamie")["thread"]
    tid = thread["id"] if isinstance(thread, dict) else thread
    store = bl.staging
    p = store.propose([{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 3}}, "after_uid": None},
                       {"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 4}}, "after_uid": None}],
                      "two counts", "assistant:manager", thread=tid)
    client = client_for(bl)
    bad = [{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 500}}, "after_uid": None}]
    r = client.post(f"/api/staging/{p['id']}/decide", json={"accept": True, "by": "jamie", "ops": bad}, headers=ORIGIN)
    assert r.status_code == 409 and "num too large" in r.json()["detail"]
    assert store.get(p["id"])["status"] == "pending"
    mine = [{"op": "add", "item": {"name": "nbs_count", "kwargs": {"num": 5}}, "after_uid": None}]
    r = client.post(f"/api/staging/{p['id']}/decide", json={"accept": True, "by": "jamie", "ops": mine}, headers=ORIGIN)
    assert r.status_code == 200
    assert [i["kwargs"]["num"] for i in store.queue()["items"]] == [5]
    stored = store.get(p["id"])
    assert stored["accepted_ops"] == mine and stored["ops"][0]["item"]["kwargs"]["num"] == 3
    notes = [m for m in ThreadStore.for_agent_log(bl.agent_log).all()[tid]["messages"] if m["kind"] == "note"]
    assert "jamie accepted with changes" in notes[-1]["text"]
    q = store.propose([{"op": "remove", "item_uid": store.queue()["items"][0]["item_uid"]}], "drop it",
                      "assistant:manager", thread=tid)
    r = client.post(f"/api/staging/{q['id']}/decide", json={"accept": False, "by": "jamie", "reason": "keep it"},
                    headers=ORIGIN)
    assert r.status_code == 200 and store.get(q["id"])["decision_reason"] == "keep it"
    notes = [m for m in ThreadStore.for_agent_log(bl.agent_log).all()[tid]["messages"] if m["kind"] == "note"]
    assert notes[-1]["text"].endswith("rejected staging proposal " + q["id"] + ": keep it")


def test_no_staging_without_a_provider():
    client = client_for(FakeBeamline())
    snap = client.get("/api/snapshot").json()
    assert snap["staging"] is None and "staging" not in snap["errors"]
    assert client.post("/api/staging/x/decide", json={"accept": True}, headers=ORIGIN).status_code == 404
