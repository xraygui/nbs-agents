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

The References tab does the same for engineering reference scans
(``nbs_agents.cli.refs``): staff approve, edit or reject the drafts agents
write, mark entries superseded or retired, and ask the beamline assistant to
draft an entry for a run. The request goes to the assistant as an operator
question, like ``nbs-cli ask-assistant``, and stays listed until a draft for it
appears.

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

from nbs_agents.agentlog import assistant_uid, find_assistant_post, is_assistant_record
from ..assistant.attachments import AttachmentStore
from ..assistant.auth import check_token
from ..cli.core import compact_item, login_name
from ..cli.kb import KBError, KnowledgeBase
from ..cli.refs import ReferenceLibrary, RefError, catalogs, resolve_refs
from nbs_agents.constants import AGENT_UID_KEY
from ..assistant.launcher import claude_launch_parameters
from nbs_agents.queue import submit_agent_block
from ..directives import DirectiveError, DirectiveStore, decide_directive, revoke_directive
from ..beamline_info import STAGING
from ..staging import StagingError, StaleProposal, check_proposal
from ..threads import ThreadError, ThreadStore, open_thread
from ..tiles import TileError, TileStore, acknowledge

STATIC_DIR = Path(__file__).parent / "static"
# Same as the Qt agent log tab
STEERABLE_STATUSES = ("opening", "active", "finalizing")
CLAUDE_AGENT = "claude"
SOURCE = "web"
POLL_S = 1.0
STAGING_DECIDED_SHOWN_S = 86400  # decided staging proposals stay in their thread this long
# After a failed QueueServer call, skip it this long so one dead server does not stall every snapshot
QUEUE_RETRY_S = 15.0
KEEPALIVE_S = 15.0
# Closed threads shown in the manager chat: those active this recently, at most THREADS_SHOWN in all
THREADS_SHOWN_S = 86400.0
THREADS_SHOWN = 40
# Newest entries of each headless role's record in the snapshot
ROLE_ENTRIES = 20
# Most of each role's figures from today kept for flipping back on its tile
ROLE_FIGURES = 50
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


class AskBody(MessageBody):
    thread: str = ""
    entry: Optional[float] = None
    tile: str = ""
    by: str = ""


class ByBody(BaseModel):
    by: str = ""
    reason: str = ""


class DecideBody(ByBody):
    confirm: bool


class StagingDecideBody(ByBody):
    accept: bool


class StagingChangesBody(MessageBody):
    by: str = ""


class RecheckBody(BaseModel):
    t: float
    model: str = ""
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


class RefBody(StaffBody):
    title: Optional[str] = None
    body: Optional[str] = None
    devices: Optional[str] = None
    nodes: Optional[str] = None
    tags: Optional[str] = None
    aliases: Optional[str] = None
    supersedes: str = ""


class RefStatusBody(StaffBody):
    status: str
    superseded_by: str = ""


class RefRequestBody(BaseModel):
    by: str
    catalog: str = "default"
    run: str
    purpose: str


def _lists(body: BaseModel, keys=("devices", "nodes", "tags")) -> dict[str, Optional[list[str]]]:
    return {
        key: None if getattr(body, key) is None else [v.strip() for v in getattr(body, key).split(",") if v.strip()]
        for key in keys
    }


REF_KEYS = ("devices", "nodes", "tags", "aliases")


def draft_request_text(request: dict[str, Any]) -> str:
    """
    The operator question that asks the assistant to draft a reference entry.

    Parameters
    ----------
    request : dict
        From ``ReferenceLibrary.add_request``.
    """
    catalog = request["catalog"]
    return (
        f"Reference scan request {request['id']} from {request['by']}: please draft a reference entry for run "
        f"{request['run']} in catalog `{catalog}`.\n\n"
        f"What it is a reference for: {request['purpose']}\n\n"
        "Use the beamline-knowledge skill (References). Look at the run first "
        f"(`nbs-cli run {request['run']} --catalog {catalog}`), "
        "write a short description in notebook/ref-draft.md, then run "
        f"`nbs-cli ref create {request['run']} --catalog {catalog} --request {request['id']} "
        "--title \"...\" --file notebook/ref-draft.md` with the right --devices and --nodes. "
        "Answer with the draft's ID; staff review it in the web page's References tab."
    )


class WebState:
    """
    Read the beamline for the page and apply the operator's actions.

    Parameters
    ----------
    beamline : nbs_agents.cli.core.Beamline
        nbs-cli client; its agent log, manager and ``beam()`` are used.
    kb : KnowledgeBase, optional
        Knowledge base; from the beamline's config if omitted.
    refs : ReferenceLibrary, optional
        Reference scan library; from the beamline's config if omitted.
    """

    def __init__(self, beamline: Any, kb: Optional[KnowledgeBase] = None,
                 refs: Optional[ReferenceLibrary] = None) -> None:
        self.beamline = beamline
        config = beamline.config
        self.kb = kb or KnowledgeBase(getattr(config, "kb_dir", ""), getattr(config, "kb_agent_dir", ""),
                                      getattr(config, "kb_review_dir", ""))
        self.refs = refs or ReferenceLibrary(getattr(config, "ref_dir", ""), getattr(config, "ref_agent_dir", ""),
                                             getattr(config, "ref_review_dir", ""))
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
            ``kb_pending`` (knowledge-base items waiting for review),
            ``ref_pending`` (reference drafts and open draft requests),
            ``tiles`` (each assistant role's tile), ``roles`` (each
            headless role's record for today: status, heartbeat, calls, its
            newest entries and ``figures``, its tile posts with a figure), ``threads`` (the manager's open threads and
            those closed in the last day, with their messages, newest first),
            ``directives`` (proposed and active), ``threads_on``,
            ``staging`` (nbs-gui's shared staging queue, its pending
            proposals and those decided in the last day, None when the
            beamline has none) and ``errors``.
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
            "ref_pending": None,
            "tiles": {},
            "roles": {},
            "threads": [],
            "directives": [],
            "threads_on": self.threads_on(),
            "staging": None,
        }
        try:
            out["kb_pending"] = self.kb.review_queue()["count"]
        except Exception as exc:
            errors["kb"] = str(exc)
        try:
            out["ref_pending"] = self.refs.review_queue()["count"]
        except Exception as exc:
            errors["refs"] = str(exc)
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
                elif record.get("role") and uid == assistant_uid(role=record["role"]):
                    out["roles"][record["role"]] = {
                        k: record.get(k) for k in ("status", "heartbeat", "calls", "last_call", "models", "role_config")
                    }
                    out["roles"][record["role"]]["uid"] = uid
                    out["roles"][record["role"]]["log"] = (record.get("log") or [])[-ROLE_ENTRIES:]
                    out["roles"][record["role"]]["figures"] = [
                        {k: e.get(k) for k in ("t", "figure", "text", "severity", "run_uids")}
                        for e in record.get("log") or [] if e.get("kind") == "tile" and e.get("figure")
                    ][-ROLE_FIGURES:]
            else:
                out["sessions"][uid] = record
        self._assistant_parts(out, errors)
        try:
            out["staging"] = self.staging()
        except Exception as exc:
            errors["staging"] = str(exc)
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

    def _assistant_parts(self, out: dict[str, Any], errors: dict[str, str]) -> None:
        """Tiles, threads and directives, each read on its own."""
        try:
            out["tiles"] = TileStore.for_agent_log(self.log).all()
        except Exception as exc:
            errors["tiles"] = str(exc)
        try:
            store = ThreadStore.for_agent_log(self.log)
            since = time.time() - THREADS_SHOWN_S
            threads = [t for t in store.all().values()
                       if t.get("status") != "closed" or (t.get("last_t") or 0) >= since]
            threads.sort(key=lambda t: t.get("created_t") or 0, reverse=True)
            out["threads"] = threads[:THREADS_SHOWN]
        except Exception as exc:
            errors["threads"] = str(exc)
        try:
            out["directives"] = DirectiveStore.for_agent_log(self.log).listing(statuses=("proposed", "active"))
        except Exception as exc:
            errors["directives"] = str(exc)

    # ----------------------------------------------------------------- assistant

    def _assistant_config(self) -> Any:
        """The beamline's ``[assistant]`` settings, or None if this client has none."""
        return getattr(self.beamline, "assistant", None)

    def threads_on(self) -> bool:
        """Whether questions open manager threads (``[assistant] threads``)."""
        config = self._assistant_config()
        return bool(config is not None and getattr(config, "threads", False))

    def _chat_role(self) -> Optional[str]:
        config = self._assistant_config()
        try:
            return config.chat_role().name if config is not None else None
        except Exception:
            return None

    def _chat_roles(self) -> tuple[Optional[str], set]:
        """The chat role's name and every configured role's name."""
        config = self._assistant_config()
        try:
            roles = config.resolved_roles() if config is not None else {}
        except Exception:
            roles = {}
        chat = next((r.name for r in roles.values() if r.chat), None)
        return chat, set(roles)

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

    def ask_assistant(self, text: str, thread: str = "", entry: Optional[float] = None, tile: str = "",
                      by: str = "") -> dict[str, Any]:
        """
        Ask the assistant (its chat role, the manager) a question.

        With ``[assistant] threads`` on, a new question opens a thread, seeded
        with one of today's assistant posts (``entry``, its time) or a role's
        tile (``tile``); ``thread`` adds a follow-up to an open thread. With
        threads off the question goes into today's main record, as before.

        Returns
        -------
        dict
            ``thread`` and the main record's ``entry`` for a new thread, the
            thread's new ``message`` for a follow-up, or the record entry
            itself with threads off.
        """
        text = text.strip()
        if not text:
            raise HTTPException(400, "empty question")
        if sum(bool(x) for x in (thread, entry is not None, tile)) > 1:
            raise HTTPException(400, "a question is a follow-up, about an entry or about a tile, not several")
        by = by.strip() or login_name()
        if thread:
            try:
                return {"thread": thread, "message": ThreadStore.for_agent_log(self.log).ask(thread, text, by, source=SOURCE)}
            except ThreadError as exc:
                raise HTTPException(404, str(exc))
        uid = self.log.ensure_assistant_record()
        if not self.threads_on():
            if entry is not None or tile:
                raise HTTPException(409, "asking about an entry or a tile opens a thread, and [assistant] threads is off")
            return self.log.message(uid, text, source=SOURCE)
        seed = None
        if entry is not None:
            try:
                post = find_assistant_post(self.log.get(uid) or {}, entry)
            except ValueError as exc:
                raise HTTPException(404, str(exc))
            seed = {"kind": "entry", "record": uid, "entry": post}
        elif tile:
            current = TileStore.for_agent_log(self.log).get(tile)
            if current is None:
                raise HTTPException(404, f"no tile for role {tile!r}")
            seed = {"kind": "tile", "role": tile, "tile": current}
        out = open_thread(self.log, text, by, source=SOURCE, seed=seed)
        return {"thread": out["thread"]["id"], "entry": out["entry"]}

    def acknowledge(self, role: str, by: str = "") -> dict[str, Any]:
        """A person acknowledges a role's sticky alert; it is logged in the role's record."""
        chat, roles = self._chat_roles()
        store = TileStore.for_agent_log(self.log)
        if role not in roles and store.get(role) is None:
            raise HTTPException(404, f"no role {role!r}")
        try:
            return acknowledge(self.log, role, by.strip() or login_name(), source=SOURCE, chat=role == chat)
        except TileError as exc:
            raise HTTPException(409, str(exc))

    def decide_directive(self, directive_id: str, confirm: bool, by: str = "", reason: str = "") -> dict[str, Any]:
        """A person confirms or rejects a directive the manager proposed."""
        try:
            return decide_directive(self.log, directive_id, confirm, by.strip() or login_name(), source=SOURCE,
                                    reason=reason.strip(), chat_role=self._chat_role())
        except DirectiveError as exc:
            raise HTTPException(409, str(exc))

    def revoke_directive(self, directive_id: str, by: str = "", reason: str = "") -> dict[str, Any]:
        """A person ends an active directive before it expires."""
        try:
            return revoke_directive(self.log, directive_id, by.strip() or login_name(), source=SOURCE,
                                    reason=reason.strip(), chat_role=self._chat_role())
        except DirectiveError as exc:
            raise HTTPException(409, str(exc))

    # ------------------------------------------------------------------ staging

    def _staging_store(self) -> Any:
        """The shared staging queue, or None when this beamline has none."""
        if not hasattr(type(self.beamline), "staging"):
            return None
        info = self.beamline.info
        if STAGING not in info.capabilities or info.staging_redis() is None:
            return None
        return self.beamline.staging

    def staging(self) -> Optional[dict[str, Any]]:
        """
        nbs-gui's staging queue and its proposals, for the conversation.

        Returns
        -------
        dict or None
            ``version``, ``updated``, ``updated_by``, ``items`` (compact) and
            ``proposals``: pending ones (each with ``stale``, ``changed`` and
            ``missing``) and those decided in the last day, oldest first;
            None when the beamline has no staging queue.
        """
        store = self._staging_store()
        if store is None:
            return None
        queue = store.queue()
        since = time.time() - STAGING_DECIDED_SHOWN_S
        proposals = []
        for proposal in store.proposals(statuses=None):
            if proposal.get("status") == "pending":
                proposals.append({**proposal, **check_proposal(proposal, queue)})
            elif (proposal.get("decided_at") or 0) >= since:
                proposals.append(proposal)
        return {
            "version": queue["version"],
            "updated": queue["updated"],
            "updated_by": queue["updated_by"],
            "items": [compact_item(i) for i in queue["items"]],
            "proposals": proposals,
        }

    def _staging_note(self, proposal: dict[str, Any], text: str) -> None:
        """Tell the thread that proposed it what the person did, so the manager sees it next time."""
        if not proposal.get("thread"):
            return
        try:
            ThreadStore.for_agent_log(self.log).add(proposal["thread"], "note", text, staging=proposal["id"])
        except Exception:
            pass

    def decide_staging(self, proposal_id: str, accept: bool, by: str = "", reason: str = "") -> dict[str, Any]:
        """A person accepts a staging proposal (applying it to the staging queue) or rejects it."""
        store = self._staging_store()
        if store is None:
            raise HTTPException(404, "this beamline has no shared staging queue")
        name = by.strip() or login_name()
        who = f"{SOURCE}:{name}"
        try:
            if accept:
                out = store.accept(proposal_id, who)
                text = f"{name} accepted staging proposal {proposal_id}; the staging queue is now version {out['queue']['version']}."
            else:
                out = {"proposal": store.reject(proposal_id, who, reason=reason)}
                text = f"{name} rejected staging proposal {proposal_id}" + (f": {reason.strip()}" if reason.strip() else ".")
        except StaleProposal as exc:
            raise HTTPException(409, f"{exc}; it is marked stale")
        except StagingError as exc:
            raise HTTPException(409, str(exc))
        self._staging_note(out["proposal"], text)
        return out

    def request_staging_changes(self, proposal_id: str, text: str, by: str = "") -> dict[str, Any]:
        """
        A person asks the manager to change a staging proposal, as a follow-up
        in the thread it came from (or a new thread): the proposal is closed as
        ``changes_requested`` and the manager is expected to propose again.

        Returns
        -------
        dict
            ``proposal`` and what ``ask_assistant`` returned.
        """
        store = self._staging_store()
        if store is None:
            raise HTTPException(404, "this beamline has no shared staging queue")
        text = text.strip()
        if not text:
            raise HTTPException(400, "say what should change")
        proposal = store.get(proposal_id)
        if proposal is None or proposal.get("status") != "pending":
            raise HTTPException(409, f"staging proposal {proposal_id} is not pending")
        thread = proposal.get("thread") or ""
        if thread:
            current = ThreadStore.for_agent_log(self.log).all().get(thread)
            if not current or current.get("status") == "closed":
                thread = ""
        name = by.strip() or login_name()
        out = self.ask_assistant(f"Please change staging proposal {proposal_id}: {text}", thread=thread, by=name)
        try:
            out["proposal"] = store.request_changes(proposal_id, f"{SOURCE}:{name}", text)
        except StagingError as exc:
            raise HTTPException(409, str(exc))
        return out

    def recheck(self, t: float, model: str = "", text: str = "") -> dict[str, Any]:
        """Ask the assistant for a blind second look at its post at time ``t``."""
        uid = self.log.ensure_assistant_record()
        try:
            return self.log.request_recheck(uid, t, model=model.strip() or None, text=text, source=SOURCE)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    def kb_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run a knowledge-base method, turning its refusals into HTTP 400."""
        try:
            return getattr(self.kb, method)(*args, **kwargs)
        except KBError as exc:
            raise HTTPException(400, str(exc))

    def ref_call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Run a reference-library method, turning its refusals into HTTP 400."""
        try:
            return getattr(self.refs, method)(*args, **kwargs)
        except RefError as exc:
            raise HTTPException(400, str(exc))

    def request_ref_draft(self, by: str, catalog: str, run: str, purpose: str) -> dict[str, Any]:
        """
        Record a staff request for a reference draft and ask the assistant for it.

        The question goes into today's assistant record, as ``ask_assistant``
        and ``nbs-cli ask-assistant`` do, and the runner's assistant answers it.

        Returns
        -------
        dict
            The request, with the assistant entry under ``message``.
        """
        known = catalogs(self.beamline.config)
        if catalog not in known:
            raise HTTPException(400, f"no catalog {catalog!r}; have {', '.join(known)}")
        request = self.ref_call("add_request", by, catalog, run, purpose)
        try:
            request["message"] = self.ask_assistant(draft_request_text(request))
        except Exception as exc:
            # Keep the record honest: a request the assistant never got is not pending
            self.refs.cancel_request(request["id"], request["by"])
            raise HTTPException(502, f"could not reach the assistant: {exc}")
        return request

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
    refs: Optional[ReferenceLibrary] = None,
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
    refs : ReferenceLibrary, optional
        Reference library for the References tab; from the beamline's config if omitted.

    Returns
    -------
    FastAPI
        The app.
    """
    state = WebState(beamline, kb, refs)
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
    async def ask(body: AskBody):
        return await write(state.ask_assistant, body.text, body.thread, body.entry, body.tile, body.by)

    @app.post("/api/tiles/{role}/ack")
    async def ack(role: str, body: ByBody):
        return await write(state.acknowledge, role, body.by)

    @app.post("/api/directives/{directive_id}/decide")
    async def decide(directive_id: str, body: DecideBody):
        return await write(state.decide_directive, directive_id, body.confirm, body.by, body.reason)

    @app.post("/api/directives/{directive_id}/revoke")
    async def revoke(directive_id: str, body: ByBody):
        return await write(state.revoke_directive, directive_id, body.by, body.reason)

    @app.post("/api/staging/{proposal_id}/decide")
    async def decide_staging(proposal_id: str, body: StagingDecideBody):
        return await write(state.decide_staging, proposal_id, body.accept, body.by, body.reason)

    @app.post("/api/staging/{proposal_id}/changes")
    async def staging_changes(proposal_id: str, body: StagingChangesBody):
        return await write(state.request_staging_changes, proposal_id, body.text, body.by)

    @app.post("/api/assistant/recheck")
    async def recheck(body: RecheckBody):
        return await write(state.recheck, body.t, body.model, body.text)

    # Knowledge base. Note IDs contain "/", so they travel in the body or query.

    @app.get("/api/kb")
    async def kb_overview():
        def read():
            return {
                "queue": state.kb_call("review_queue"),
                "notes": [n.summary() for n in state.kb_call("notes")],
            }
        return await run_in_threadpool(read)

    @app.get("/api/kb/search")
    async def kb_search(q: str = "", n: int = 50):
        return await run_in_threadpool(state.kb_call, "search", q, n=n)

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

    # Reference scans. IDs contain "/", so they travel in the body or query.

    @app.get("/api/refs")
    async def refs_overview():
        def read():
            return {
                "queue": state.ref_call("review_queue"),
                "entries": [e.summary() for e in state.ref_call("entries")],
                "catalogs": catalogs(beamline.config),
            }
        return await run_in_threadpool(read)

    @app.get("/api/refs/search")
    async def refs_search(q: str = "", n: int = 50, all: bool = True):
        return await run_in_threadpool(state.ref_call, "search", q, n=n, all_statuses=all)

    @app.get("/api/refs/entry")
    async def refs_entry(id: str):
        return await run_in_threadpool(lambda: state.ref_call("get", id).full())

    @app.get("/api/refs/resolve")
    async def refs_resolve(id: str):
        def check():
            return resolve_refs(beamline.config, [state.ref_call("get", id)])[0]
        return await run_in_threadpool(check)

    @app.post("/api/refs/approve")
    async def refs_approve(body: RefBody):
        return await write(state.ref_call, "approve", body.id, body.by, title=body.title, body=body.body,
                           supersedes=body.supersedes, **_lists(body, REF_KEYS))

    @app.post("/api/refs/entry")
    async def refs_edit(body: RefBody):
        return await write(state.ref_call, "edit", body.id, body.by, title=body.title, body=body.body,
                           **_lists(body, REF_KEYS))

    @app.post("/api/refs/reject")
    async def refs_reject(body: RejectBody):
        return await write(state.ref_call, "reject", body.id, body.by, body.reason)

    @app.post("/api/refs/status")
    async def refs_status(body: RefStatusBody):
        return await write(state.ref_call, "set_status", body.id, body.by, body.status, body.superseded_by)

    @app.post("/api/refs/request")
    async def refs_request(body: RefRequestBody):
        return await write(state.request_ref_draft, body.by, body.catalog, body.run, body.purpose)

    @app.post("/api/refs/request/cancel")
    async def refs_request_cancel(body: StaffBody):
        return await write(state.ref_call, "cancel_request", body.id, body.by)

    return app
