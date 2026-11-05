"""
FastAPI app behind ``nbs-agents-web``.

The page shows the same things as the nbs-gui "Agents Log" and "Assistant"
tabs: agent sessions from the shared agent log, their conversation and
activity, steering (message, request stop), launching a Claude session, the
assistant's daily feed, beam status and the queue. One snapshot holds all of
it; ``/api/events`` streams it to the page whenever it changes.

The Knowledge tab is where staff review what agents wrote to the knowledge
base (approve, edit and approve, keep, reject) and write or edit human notes.
Approved and written notes go into ``kb_dir`` (the profile's knowledge folder)
for staff to commit. Agents get ``kb_dir`` and the review record read-only.

Writes go to the same places the Qt tabs write: operator entries in the agent
log, and ``submit_agent_block`` on the QueueServer for a new agent.

With a ``token`` (``nbs-agents-web`` always passes one), every request needs
the user's nbs-agents token, Jupyter style: open the ``/?token=...`` URL the
server prints, or paste the token into the sign-in page, and a cookie keeps
the browser signed in. Scripts can send ``Authorization: Bearer <token>``.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from nbs_agents.agentlog import assistant_uid, is_assistant_record
from ..assistant.attachments import AttachmentStore
from ..assistant.auth import check_token
from ..cli.core import compact_item
from ..cli.kb import KBError, KnowledgeBase
from nbs_agents.constants import AGENT_UID_KEY
from ..assistant.launcher import claude_launch_parameters
from nbs_agents.queue import submit_agent_block

STATIC_DIR = Path(__file__).parent / "static"
# Same as the Qt agent log tab
STEERABLE_STATUSES = ("opening", "active", "finalizing")
CLAUDE_AGENT = "claude"
SOURCE = "web"
POLL_S = 1.0
# After a failed QueueServer call, skip it this long so one dead server does not stall every snapshot
QUEUE_RETRY_S = 15.0
KEEPALIVE_S = 15.0
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
COOKIE = "nbs_agents_token"
LOGIN_PAGE = """<!doctype html><meta charset="utf-8"><title>nbs-agents sign in</title>
<style>body{font:15px system-ui,sans-serif;max-width:32em;margin:15vh auto;padding:0 16px}
input{width:100%;box-sizing:border-box;font:inherit;padding:6px;margin:8px 0}</style>
<h1>nbs-agents</h1>
<p>Open the link nbs-agents-web printed when it started, or paste the token from
the <code>token</code> file in your nbs-agents config folder.</p>
<form method="post" action="/login"><input name="token" type="password" autofocus
placeholder="token"><button>Sign in</button></form>{error}
"""


class MessageBody(BaseModel):
    text: str = ""


class NewAgentBody(BaseModel):
    goal: str
    budget: str = ""
    start: bool = True
    confirm_busy: bool = False


class StaffBody(BaseModel):
    by: str
    id: str = ""


class StaffNoteBody(StaffBody):
    title: Optional[str] = None
    body: Optional[str] = None
    topic: Optional[str] = None
    devices: Optional[str] = None
    nodes: Optional[str] = None
    tags: Optional[str] = None


class RejectBody(StaffBody):
    reason: str = ""
    index: Optional[int] = None


def _lists(body: StaffNoteBody) -> dict[str, Optional[list[str]]]:
    return {
        key: None if getattr(body, key) is None else [v.strip() for v in getattr(body, key).split(",") if v.strip()]
        for key in ("devices", "nodes", "tags")
    }


class WebState:
    """
    Read the beamline for the page and apply the operator's actions.

    Parameters
    ----------
    beamline : nbs_agents.cli.core.Beamline
        nbs-cli client; its agent log, manager and ``beam()`` are used.
    kb : KnowledgeBase, optional
        Knowledge base; from the beamline's config if omitted.
    """

    def __init__(self, beamline: Any, kb: Optional[KnowledgeBase] = None) -> None:
        self.beamline = beamline
        config = beamline.config
        self.kb = kb or KnowledgeBase(getattr(config, "kb_dir", ""), getattr(config, "kb_agent_dir", ""),
                                      getattr(config, "kb_review_dir", ""))
        self._queue_error: Optional[str] = None
        self._queue_retry_at = 0.0

    @property
    def log(self):
        return self.beamline.agent_log

    # ------------------------------------------------------------------ reads

    def snapshot(self) -> dict[str, Any]:
        """
        Everything the page shows, in one JSON-ready dict.

        Each source is read on its own, so a missing QueueServer or beam map
        only blanks its section and adds a message to ``errors``.

        Returns
        -------
        dict
            ``time``, ``sessions`` (agent records keyed by UID, without the
            assistant's), ``assistant`` (today's assistant record or None),
            ``runner_alive``, ``tiled_login`` (a pending login link), ``beam``,
            ``manager``, ``running``, ``blocks``
            (each steerable session's queued items), ``read_only``,
            ``kb_pending`` (knowledge-base items waiting for review) and
            ``errors``.
        """
        errors: dict[str, str] = {}
        out: dict[str, Any] = {
            "time": time.time(),
            "sessions": {},
            "assistant": None,
            "assistant_uid": assistant_uid(),
            "runner_alive": None,
            "tiled_login": None,
            "beam": None,
            "manager": None,
            "running": None,
            "blocks": {},
            "read_only": bool(getattr(self.beamline.config, "read_only", False)),
            "drivers": bool(getattr(self.beamline.config, "drivers", True)),
            "kb_pending": None,
        }
        try:
            out["kb_pending"] = self.kb.review_queue()["count"]
        except Exception as exc:
            errors["kb"] = str(exc)
        try:
            records = self.log.records()
        except Exception as exc:
            errors["agent_log"] = str(exc)
            records = {}
        for uid, record in records.items():
            if not isinstance(record, dict):
                continue
            if is_assistant_record(record, uid):
                if uid == out["assistant_uid"]:
                    out["assistant"] = record
            else:
                out["sessions"][uid] = record
        try:
            out["runner_alive"] = self.log.runner_alive()
            out["tiled_login"] = self.log.tiled_login() if out["runner_alive"] else None
        except Exception as exc:
            errors["runner"] = str(exc)
        try:
            out["beam"] = self.beamline.beam()
        except Exception as exc:
            errors["beam"] = str(exc)
        if self._queue_error and time.monotonic() < self._queue_retry_at:
            errors["queue"] = self._queue_error
            out["errors"] = errors
            return out
        try:
            manager = self.beamline.manager
            status = manager.status()
            queue = manager.queue_get()
            out["manager"] = {
                "state": status.get("manager_state"),
                "re_state": status.get("re_state"),
                "queue_stop_pending": status.get("queue_stop_pending"),
                "items_in_queue": status.get("items_in_queue"),
                "environment_open": status.get("worker_environment_exists"),
            }
            running = queue.get("running_item") or {}
            if running:
                out["running"] = compact_item(running)
                out["running"]["agent_uid"] = (running.get("meta") or {}).get(AGENT_UID_KEY)
            items = queue.get("items", [])
            for uid, record in out["sessions"].items():
                if record.get("status") in STEERABLE_STATUSES:
                    split = self.beamline.split_queue(items, uid)
                    out["blocks"][uid] = [compact_item(i) for i in split["block"]]
            self._queue_error = None
        except Exception as exc:
            errors["queue"] = self._queue_error = str(exc)
            self._queue_retry_at = time.monotonic() + QUEUE_RETRY_S
        out["errors"] = errors
        return out

    # ----------------------------------------------------------------- writes

    def _steerable(self, uid: str) -> dict[str, Any]:
        record = self.log.get(uid)
        if record is None or is_assistant_record(record, uid):
            raise HTTPException(404, f"no agent session {uid}")
        if record.get("status") not in STEERABLE_STATUSES:
            raise HTTPException(409, f"session {uid[:8]} is {record.get('status')}")
        return record

    def message(self, uid: str, text: str) -> dict[str, Any]:
        """Leave an operator message for a running session."""
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty message")
        self._steerable(uid)
        return self.log.message(uid, text, source=SOURCE)

    def request_stop(self, uid: str, text: str = "") -> dict[str, Any]:
        """Ask a running session to stop adding work and finalize."""
        record = self._steerable(uid)
        if record.get("stop_requested"):
            raise HTTPException(409, "stop already requested")
        return self.log.request_stop(uid, text.strip(), source=SOURCE)

    def ask_assistant(self, text: str) -> dict[str, Any]:
        """Post a question to today's assistant record."""
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty question")
        uid = self.log.ensure_assistant_record()
        return self.log.message(uid, text, source=SOURCE)

    def kb_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run a knowledge-base method, turning its refusals into HTTP 400."""
        try:
            return getattr(self.kb, method)(*args, **kwargs)
        except KBError as exc:
            raise HTTPException(400, str(exc))

    def new_agent(self, goal: str, budget: str = "", start: bool = True, confirm_busy: bool = False) -> dict:
        """
        Queue a Claude agent session that nbs-agents-runner will launch.

        Parameters
        ----------
        goal : str
            The session's goal.
        budget : str, optional
            Time budget such as ``"90m"``.
        start : bool, optional
            Start the queue if it is idle and the block is at its head.
        confirm_busy : bool, optional
            Queue even if another Claude session is still running.

        Returns
        -------
        dict
            ``submit_agent_block`` result, minus the raw items, plus ``item_uids``.
        """
        if not getattr(self.beamline.config, "drivers", True):
            raise HTTPException(403, "Claude agents are off on this beamline ([nbs_cli] drivers = false)")
        if getattr(self.beamline.config, "read_only", False):
            raise HTTPException(403, "this server runs with the read-only assistant role")
        try:
            parameters = claude_launch_parameters(goal, budget)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if self.log.runner_alive() is False:
            raise HTTPException(409, "nbs-agents-runner is not running; it launches the agent")
        busy = [
            uid
            for uid, r in self.log.records().items()
            if r.get("agent_name") == CLAUDE_AGENT
            and r.get("status") in STEERABLE_STATUSES
            and not is_assistant_record(r, uid)
        ]
        if busy and not confirm_busy:
            raise HTTPException(409, {"busy": busy})
        config = self.beamline.config
        user = {"user": config.qs_user, "user_group": config.qs_user_group}
        try:
            out = submit_agent_block(self.beamline.manager, CLAUDE_AGENT, parameters, start=start, **user)
        except Exception as exc:
            raise HTTPException(502, str(exc))
        return {
            "started": out["started"],
            "position": out["position"],
            "item_uids": [i.get("item_uid") for i in out["items"]],
        }


def _host_name(netloc: str) -> str:
    host = urlsplit(f"//{netloc}").hostname or ""
    return host.strip("[]")


def create_app(
    beamline: Any,
    allowed_hosts: Optional[tuple] = None,
    poll_s: float = POLL_S,
    attachments: Optional[AttachmentStore] = None,
    token: Optional[str] = None,
    kb: Optional[KnowledgeBase] = None,
) -> FastAPI:
    """
    Build the web app.

    Parameters
    ----------
    beamline : nbs_agents.cli.core.Beamline
        nbs-cli client.
    allowed_hosts : tuple of str, optional
        Host names the page may be reached by; localhost names by default.
        Requests naming another host are refused, which blocks DNS rebinding.
    poll_s : float, optional
        Seconds between snapshots on the event stream.
    attachments : AttachmentStore, optional
        Where ``att:ID`` figures in messages are served from; the default store
        if omitted.
    token : str, optional
        The user's login token. If given, every request must carry it (cookie
        or bearer header); without one the page is open to anyone who can reach it.
    kb : KnowledgeBase, optional
        Knowledge base for the Knowledge tab; from the beamline's config if omitted.

    Returns
    -------
    FastAPI
        The app.
    """
    state = WebState(beamline, kb)
    store = attachments or AttachmentStore()
    hosts = set(allowed_hosts or LOCAL_HOSTS)
    app = FastAPI(title="nbs-agents web", docs_url=None, redoc_url=None)
    app.state.web = state
    cache: dict[str, Any] = {"t": 0.0, "snap": None}
    lock = asyncio.Lock()

    async def current(max_age: float) -> dict[str, Any]:
        async with lock:
            if cache["snap"] is None or time.monotonic() - cache["t"] >= max_age:
                cache["snap"] = await run_in_threadpool(state.snapshot)
                cache["t"] = time.monotonic()
            return cache["snap"]

    def invalidate():
        cache["t"] = 0.0

    def signed_in(request: Request) -> bool:
        if token is None:
            return True
        bearer = request.headers.get("authorization", "")
        if bearer.lower().startswith("bearer ") and check_token(token, bearer[7:].strip()):
            return True
        return check_token(token, request.cookies.get(COOKIE))

    def sign_in() -> RedirectResponse:
        # Redirect so the token leaves the address bar; the cookie is only for this page
        response = RedirectResponse("/", status_code=303)
        if token is not None:
            response.set_cookie(COOKIE, token, httponly=True, samesite="strict", path="/")
        return response

    def login_page(error: str = "", status: int = 401) -> HTMLResponse:
        return HTMLResponse(
            LOGIN_PAGE.replace("{error}", f"<p style='color:#b00'>{error}</p>" if error else ""),
            status_code=status,
            headers={"Cache-Control": "no-store"},
        )

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Only localhost pages may read or write, and writes must come from this page
        if _host_name(request.headers.get("host", "")) not in hosts:
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if not origin or urlsplit(origin).netloc != request.headers.get("host"):
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        # Only the user holding the nbs-agents token may see or steer agents
        if request.url.path not in ("/", "/login") and not signed_in(request):
            return JSONResponse({"detail": "sign in with your nbs-agents token"}, status_code=401)
        return await call_next(request)

    @app.get("/")
    async def index(request: Request):
        sent = request.query_params.get("token")
        if sent is not None and token is not None:
            return sign_in() if check_token(token, sent) else login_page("That token is not right.")
        if not signed_in(request):
            return login_page()
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.post("/login")
    async def login(request: Request):
        form = parse_qs((await request.body()).decode(errors="replace"))
        sent = (form.get("token") or [""])[0].strip()
        if token is None or check_token(token, sent):
            return sign_in()
        return login_page("That token is not right.")

    @app.get("/api/attachments/{att_id}")
    async def attachment(att_id: str):
        found = store.path(att_id)
        if found is None:
            raise HTTPException(404, "no such attachment")
        path, mime = found
        # Figures are shown with <img>, but an SVG opened directly must not run scripts
        headers = {
            "Cache-Control": "max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
        }
        return FileResponse(path, media_type=mime, headers=headers)

    @app.get("/api/snapshot")
    async def snapshot():
        return await current(0)

    @app.get("/api/events")
    async def events(request: Request):
        async def stream():
            last, last_sent = None, time.monotonic()
            while not await request.is_disconnected():
                snap = await current(poll_s)
                text = json.dumps(snap, default=str)
                if text != last:
                    last, last_sent = text, time.monotonic()
                    yield f"data: {text}\n\n"
                elif time.monotonic() - last_sent > KEEPALIVE_S:
                    last_sent = time.monotonic()
                    yield ": keepalive\n\n"
                await asyncio.sleep(poll_s)

        headers = {"Cache-Control": "no-store", "X-Accel-Buffering": "no"}
        return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)

    async def write(func, *args, **kwargs):
        out = await run_in_threadpool(func, *args, **kwargs)
        invalidate()
        return out

    @app.post("/api/agents/{uid}/message")
    async def message(uid: str, body: MessageBody):
        return await write(state.message, uid, body.text)

    @app.post("/api/agents/{uid}/stop")
    async def stop(uid: str, body: MessageBody):
        return await write(state.request_stop, uid, body.text)

    @app.post("/api/agents")
    async def new_agent(body: NewAgentBody):
        return await write(state.new_agent, body.goal, body.budget, body.start, body.confirm_busy)

    @app.post("/api/assistant/ask")
    async def ask(body: MessageBody):
        return await write(state.ask_assistant, body.text)

    # Knowledge base. Note IDs contain "/", so they travel in the body or query.

    @app.get("/api/kb")
    async def kb_overview():
        def read():
            return {
                "queue": state.kb_call("review_queue"),
                "notes": [n.summary() for n in state.kb_call("notes")],
            }
        return await run_in_threadpool(read)

    @app.get("/api/kb/note")
    async def kb_note(id: str):
        def read():
            out = state.kb_call("get", id).full()
            out["entries"] = state.kb_call("entries", id)
            return out
        return await run_in_threadpool(read)

    @app.post("/api/kb/note")
    async def kb_write_note(body: StaffNoteBody):
        return await write(state.kb_call, "write_note", body.by, body.title or "", body.body or "",
                           topic=body.topic or "", note_id=body.id or None, **_lists(body))

    @app.post("/api/kb/approve")
    async def kb_approve(body: StaffNoteBody):
        return await write(state.kb_call, "approve", body.id, body.by, title=body.title, body=body.body,
                           topic=body.topic, **_lists(body))

    @app.post("/api/kb/keep")
    async def kb_keep(body: StaffBody):
        return await write(state.kb_call, "keep", body.id, body.by)

    @app.post("/api/kb/reject")
    async def kb_reject(body: RejectBody):
        return await write(state.kb_call, "reject", body.id, body.by, body.reason)

    @app.post("/api/kb/appendix/reviewed")
    async def kb_appendix_reviewed(body: StaffBody):
        return await write(state.kb_call, "review_appendix", body.id, body.by)

    @app.post("/api/kb/appendix/remove")
    async def kb_appendix_remove(body: RejectBody):
        if body.index is None:
            raise HTTPException(400, "which entry? give index")
        return await write(state.kb_call, "remove_entry", body.id, body.index, body.by, body.reason)

    return app
