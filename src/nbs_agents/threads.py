"""
Manager threads: one conversation per question to the assistant.

Each question a person asks the assistant's chat role (the manager) opens a
thread, a JSON value in the agent-log Redis under ``{prefix}thread:{id}``::

    {
        "id": str,
        "status": "open" | "closed",
        "seed": {"kind": "snapshot" | "entry" | "tile", ...},  # what it was asked about
        "session_id": str,        # the Claude session follow-ups resume
        "started": bool,          # whether that session exists yet
        "created_by": str, "source": str, "created_t": float, "last_t": float,
        "messages": [{"t", "kind", "text", "by"?, "source"?, "role"?, "model"?}],
        "seen": int,              # messages the manager has been shown
        "calling": bool,          # a call is running for it
        "marker": {"record": str, "t": float},  # its one line in the main record
        "summary": str or None, "closed_t": float or None,
    }

The question that opens a thread is the one line the main record gets (an
``operator`` entry carrying ``thread``); follow-ups, answers and the replies of
the roles the manager asks stay in the thread. The runner answers each new
question or reply with a call that resumes the thread's Claude session, and
closes a quiet thread with a short summary in the manager's
``notebook/threads.md``.
"""

import time
import uuid
from typing import Any, Optional

from nbs_agents.jsonstore import JsonStore

THREAD_TOPIC = "thread:"
SEED_KINDS = ("snapshot", "entry", "tile")
MESSAGE_KINDS = ("question", "answer", "reply", "note", "error")
# Messages the manager answers with a call
CALL_KINDS = ("question", "reply")


class ThreadError(ValueError):
    """A thread that does not exist, or a message it cannot take."""


def new_thread_id(t: Optional[float] = None) -> str:
    """A short, sortable id such as ``1113-2154-a3f9``."""
    return time.strftime("%m%d-%H%M", time.localtime(t)) + "-" + uuid.uuid4().hex[:4]


def unseen(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """The thread's messages the manager has not been shown and should answer."""
    return [m for m in (thread.get("messages") or [])[thread.get("seen") or 0:] if m.get("kind") in CALL_KINDS]


def seed_text(seed: dict[str, Any]) -> str:
    """One paragraph saying what a thread was asked about."""
    kind = seed.get("kind")
    if kind == "entry":
        entry = seed.get("entry") or {}
        when = time.strftime("%H:%M:%S", time.localtime(entry.get("t") or 0))
        runs = f" (runs: {' '.join(entry['run_uids'])})" if entry.get("run_uids") else ""
        return (f"It is about the {entry.get('kind')} entry of {seed.get('record')} at {when}{runs}:\n\n"
                f"{(entry.get('text') or '').strip()}")
    if kind == "tile":
        tile = seed.get("tile") or {}
        runs = f" (runs: {' '.join(tile['run_uids'])})" if tile.get("run_uids") else ""
        cleared = " (since cleared)" if tile.get("cleared") else ""
        return (f"It is about the {seed.get('role')} role's tile, {tile.get('severity')}{cleared}{runs}:\n\n"
                f"{(tile.get('message') or '').strip()}")
    return "It was asked from the top-level box, about nothing in particular: start from the snapshot."


class ThreadStore(JsonStore):
    """Read and write manager threads in the agent-log Redis."""

    topic = THREAD_TOPIC

    def create(
        self,
        question: str,
        by: str,
        source: str = "operator",
        seed: Optional[dict[str, Any]] = None,
        marker: Optional[dict[str, Any]] = None,
        t: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Open a thread with its first question.

        Parameters
        ----------
        question : str
            The question.
        by : str
            Who asked.
        source : str, optional
            Where it was asked (``nbs-cli``, ``web``, ...).
        seed : dict, optional
            What it is about: ``{"kind": "entry", "record", "entry"}``,
            ``{"kind": "tile", "role", "tile"}``; a snapshot seed if omitted.
        marker : dict, optional
            ``{"record", "t"}`` of its line in the main record.
        t : float, optional
            When; now if omitted.

        Returns
        -------
        dict
            The thread as written.
        """
        if not (question or "").strip():
            raise ThreadError("empty question")
        seed = dict(seed or {"kind": "snapshot"})
        if seed.get("kind") not in SEED_KINDS:
            raise ThreadError(f"unknown seed {seed.get('kind')!r}; use one of {list(SEED_KINDS)}")
        now = t or time.time()
        thread_id = new_thread_id(now)
        thread = {
            "id": thread_id, "status": "open", "seed": seed, "session_id": str(uuid.uuid4()), "started": False,
            "created_by": by, "source": source, "created_t": now, "last_t": now,
            "messages": [{"t": now, "kind": "question", "text": question.strip(), "by": by, "source": source}],
            "seen": 0, "calling": False, "marker": marker, "summary": None, "closed_t": None,
        }
        return self._modify(thread_id, lambda current: thread)

    def add(self, thread_id: str, kind: str, text: str, t: Optional[float] = None, **fields: Any) -> dict[str, Any]:
        """
        Add a message to a thread. A question or reply reopens a closed thread.

        Returns
        -------
        dict
            The message.

        Raises
        ------
        ThreadError
            If there is no such thread, or on an unknown kind or empty text.
        """
        if kind not in MESSAGE_KINDS:
            raise ThreadError(f"unknown message kind {kind!r}; use one of {list(MESSAGE_KINDS)}")
        if not (text or "").strip():
            raise ThreadError("empty message")
        message = {"t": t or time.time(), "kind": kind, "text": text.strip(),
                   **{k: v for k, v in fields.items() if v is not None}}

        def update(thread):
            if thread is None:
                raise ThreadError(f"no thread {thread_id}")
            thread.setdefault("messages", []).append(message)
            thread["last_t"] = message["t"]
            if kind in CALL_KINDS and thread.get("status") == "closed":
                thread.update(status="open", closed_t=None)
            return thread

        self._modify(thread_id, update)
        return message

    def ask(self, thread_id: str, text: str, by: str, source: str = "operator") -> dict[str, Any]:
        """A follow-up question in a thread (see ``add``)."""
        return self.add(thread_id, "question", text, by=by, source=source)

    def update(self, thread_id: str, **fields: Any) -> dict[str, Any]:
        """Set top-level fields of an existing thread."""

        def update(thread):
            if thread is None:
                raise ThreadError(f"no thread {thread_id}")
            thread.update(fields)
            return thread

        return self._modify(thread_id, update)

    def mark_seen(self, thread_id: str, n: int, **fields: Any) -> dict[str, Any]:
        """Note that the manager has been shown the first ``n`` messages."""
        return self.update(thread_id, seen=n, **fields)

    def open_threads(self) -> list[dict[str, Any]]:
        """Threads not closed, oldest first."""
        return sorted((t for t in self.all().values() if t.get("status") != "closed"),
                      key=lambda t: t.get("created_t") or 0)

    def listing(self, since: Optional[float] = None) -> list[dict[str, Any]]:
        """
        Every thread (created or active since ``since``), newest first,
        without its messages.
        """
        out = []
        for thread in self.all().values():
            if since is not None and (thread.get("last_t") or 0) < since:
                continue
            messages = thread.get("messages") or []
            out.append({
                "id": thread["id"], "status": thread.get("status"), "created_by": thread.get("created_by"),
                "created_t": thread.get("created_t"), "last_t": thread.get("last_t"),
                "question": messages[0]["text"] if messages else None, "messages": len(messages),
                "seed": (thread.get("seed") or {}).get("kind"), "summary": thread.get("summary"),
                "calling": thread.get("calling"),
            })
        return sorted(out, key=lambda t: t.get("created_t") or 0, reverse=True)

    def prune(self, older_than: float) -> int:
        """Delete closed threads last active before ``older_than``; how many."""
        n = 0
        for thread_id, thread in self.all().items():
            if thread.get("status") == "closed" and (thread.get("last_t") or 0) < older_than:
                n += self.delete(thread_id)
        return n


def open_thread(
    agent_log: Any,
    question: str,
    by: str,
    source: str = "operator",
    seed: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    Open a manager thread for a person's question and put its one line in
    today's main assistant record: an ``operator`` entry carrying ``thread``,
    which the runner does not treat as a question of its own.

    Parameters
    ----------
    agent_log : AgentLog
        The agent log; its Redis holds the threads.
    question : str
        The question.
    by : str
        Who asked.
    source : str, optional
        Where it was asked.
    seed : dict, optional
        What it is about (see ``ThreadStore.create``).

    Returns
    -------
    dict
        ``thread`` and ``entry`` (the line in the main record).
    """
    store = ThreadStore.for_agent_log(agent_log)
    thread = store.create(question, by, source=source, seed=seed)
    uid = agent_log.ensure_assistant_record()
    entry = agent_log.append(uid, "operator", question.strip(), source=source, by=by, thread=thread["id"])
    thread = store.update(thread["id"], marker={"record": uid, "t": entry["t"]})
    return {"thread": thread, "entry": entry}
