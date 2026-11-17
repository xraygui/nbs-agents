"""
Directives: standing instructions to an assistant role that a person confirmed.

A reply to the manager is sometimes an instruction for a role ("U6 is known to
charge, stop alerting on it"). The manager proposes it as a directive; only a
person confirms or rejects it (``nbs-cli directive confirm``, or the web UI).
Confirmed directives go into the role's prompt and its ``directives.md`` until
they expire. Each is a JSON value in the agent-log Redis under
``{prefix}directive:{id}``::

    {
        "id": str,
        "role": str,
        "text": str,
        "status": "proposed" | "active" | "rejected" | "expired" | "revoked",
        "proposed_t": float, "proposed_by": str, "thread": str or None,
        "expires": {"kind": "beamtime" | "date" | "duration", "until": float,
                    "proposal": any},  # beamtime: also ends when the proposal changes
        "decided": {"by", "t", "source", "reason"} or None,  # confirm or reject
        "ended": {"t", "by"?, "why"} or None,                # expired or revoked
    }

Every change is logged in today's main assistant record as a ``directive``
entry, and a confirmation, revocation or expiry also in the role's record.
"""

import datetime
import json
import re
import time
import uuid
from typing import Any, Optional

from nbs_agents.jsonstore import JsonStore

DIRECTIVE_TOPIC = "directive:"
DIRECTIVE_STATUSES = ("proposed", "active", "rejected", "expired", "revoked")
# How long "beamtime" lasts at most when the proposal does not change first
BEAMTIME_S = 3 * 86400.0
DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([mhd])$")
UNITS = {"m": 60.0, "h": 3600.0, "d": 86400.0}


class DirectiveError(ValueError):
    """A directive that does not exist, or a change it cannot take."""


def parse_expiry(text: str, now: float, proposal: Any = None, beamtime_s: float = BEAMTIME_S) -> dict[str, Any]:
    """
    When a directive ends.

    Parameters
    ----------
    text : str
        ``beamtime`` (until the proposal changes, at most ``beamtime_s``), a
        duration such as ``12h`` or ``3d``, or a date ``YYYY-MM-DD`` (to the
        end of that local day) or ``YYYY-MM-DDTHH:MM``.
    now : float
        Epoch seconds.
    proposal : any, optional
        The beamline's current proposal metadata, for ``beamtime``.
    beamtime_s : float, optional
        Longest a beamtime directive lasts.

    Returns
    -------
    dict
        ``{"kind", "until", "text"}``, plus ``proposal`` for ``beamtime``.

    Raises
    ------
    DirectiveError
        On anything else, or a time already past.
    """
    text = (text or "beamtime").strip()
    if text == "beamtime":
        return {"kind": "beamtime", "until": now + beamtime_s, "proposal": proposal, "text": text}
    match = DURATION_RE.match(text)
    if match:
        until = now + float(match.group(1)) * UNITS[match.group(2)]
        return {"kind": "duration", "until": until, "text": text}
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise DirectiveError(f"--expires takes beamtime, a duration (12h, 3d) or a date (YYYY-MM-DD), not {text!r}")
    if len(text) == 10:
        when = when + datetime.timedelta(days=1)
    until = when.timestamp()
    if until <= now:
        raise DirectiveError(f"--expires {text} is already past")
    return {"kind": "date", "until": until, "text": text}


def expired(directive: dict[str, Any], now: float, proposal: Any = None) -> Optional[str]:
    """
    Why an active directive has ended, or None while it holds.

    Parameters
    ----------
    directive : dict
        The directive.
    now : float
        Epoch seconds.
    proposal : any, optional
        The beamline's current proposal metadata; a beamtime directive ends
        when it differs from the one recorded (when both are known).
    """
    expires = directive.get("expires") or {}
    if now >= (expires.get("until") or float("inf")):
        return f"its time ran out ({expires.get('text')})"
    if expires.get("kind") == "beamtime" and expires.get("proposal") is not None and proposal is not None:
        if json.dumps(proposal, sort_keys=True, default=str) != json.dumps(expires["proposal"], sort_keys=True, default=str):
            return "the beamtime ended (the proposal changed)"
    return None


def directives_text(directives: list[dict[str, Any]]) -> str:
    """Markdown listing a role's active directives (its ``directives.md``)."""
    if not directives:
        return "No directives."
    lines = ["Directives a person confirmed. Follow them until they expire.", ""]
    for d in directives:
        until = time.strftime("%Y-%m-%d %H:%M", time.localtime((d.get("expires") or {}).get("until") or 0))
        by = (d.get("decided") or {}).get("by")
        lines.append(f"- {d['text']} ({d['id']}; confirmed by {by}; until {(d.get('expires') or {}).get('text')}, "
                     f"at the latest {until})")
    return "\n".join(lines)


class DirectiveStore(JsonStore):
    """Read and write directives in the agent-log Redis."""

    topic = DIRECTIVE_TOPIC

    def propose(
        self,
        role: str,
        text: str,
        expires: dict[str, Any],
        by: str = "assistant",
        thread: Optional[str] = None,
        t: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Propose a directive for a role; it waits for a person.

        Returns
        -------
        dict
            The directive as written.
        """
        if not (text or "").strip():
            raise DirectiveError("directive text is required")
        directive_id = "d-" + uuid.uuid4().hex[:6]
        directive = {
            "id": directive_id, "role": role, "text": text.strip(), "status": "proposed",
            "proposed_t": t or time.time(), "proposed_by": by, "thread": thread,
            "expires": expires, "decided": None, "ended": None,
        }
        return self._modify(directive_id, lambda current: directive)

    def decide(self, directive_id: str, confirm: bool, by: str, source: str = "operator",
               reason: str = "", t: Optional[float] = None) -> dict[str, Any]:
        """
        A person confirms (``active``) or rejects a proposed directive.

        Raises
        ------
        DirectiveError
            If there is no such directive or it is not waiting for a decision.
        """

        def update(directive):
            if directive is None:
                raise DirectiveError(f"no directive {directive_id}")
            if directive.get("status") != "proposed":
                raise DirectiveError(f"directive {directive_id} is {directive.get('status')}, not proposed")
            directive["status"] = "active" if confirm else "rejected"
            directive["decided"] = {"by": by, "t": t or time.time(), "source": source, "reason": reason or None}
            return directive

        return self._modify(directive_id, update)

    def end(self, directive_id: str, status: str, why: str, by: Optional[str] = None,
            t: Optional[float] = None) -> dict[str, Any]:
        """
        End an active directive: ``expired`` (the runner) or ``revoked`` (a person).

        Raises
        ------
        DirectiveError
            If there is no such directive or it is not active.
        """

        def update(directive):
            if directive is None:
                raise DirectiveError(f"no directive {directive_id}")
            if directive.get("status") != "active":
                raise DirectiveError(f"directive {directive_id} is {directive.get('status')}, not active")
            directive["status"] = status
            directive["ended"] = {"t": t or time.time(), "why": why, **({"by": by} if by else {})}
            return directive

        return self._modify(directive_id, update)

    def listing(self, role: Optional[str] = None, statuses: Optional[tuple] = None) -> list[dict[str, Any]]:
        """Directives (of one role, in some statuses), oldest first."""
        out = [d for d in self.all().values()
               if (role is None or d.get("role") == role) and (statuses is None or d.get("status") in statuses)]
        return sorted(out, key=lambda d: d.get("proposed_t") or 0)

    def active(self, role: str) -> list[dict[str, Any]]:
        """A role's active directives, oldest first."""
        return self.listing(role, ("active",))


def log_directive(agent_log: Any, directive: dict[str, Any], text: str, source: str, role_record: bool = False,
                  chat_role: Optional[str] = None, **fields: Any) -> dict[str, Any]:
    """
    Log a change to a directive as a ``directive`` entry in today's main
    assistant record and, with ``role_record``, in its role's record too.

    Parameters
    ----------
    agent_log : AgentLog
        The agent log.
    directive : dict
        The directive after the change.
    text : str
        What happened.
    source : str
        Who or what made the change.
    role_record : bool, optional
        Also log it in the role's record.
    chat_role : str, optional
        The chat role's name, whose record is the main one.

    Returns
    -------
    dict
        The main record's entry.
    """
    refs = {"directive": directive["id"], "role": directive["role"], "status": directive["status"], **fields}
    uid = agent_log.ensure_assistant_record()
    entry = agent_log.append(uid, "directive", text, source=source, **refs)
    if role_record and directive["role"] != chat_role:
        role_uid = agent_log.ensure_assistant_record(role=directive["role"])
        agent_log.append(role_uid, "directive", text, source=source, **refs)
    return entry


def _thread_note(agent_log: Any, directive: dict[str, Any], text: str) -> None:
    """Tell the thread that proposed a directive what became of it."""
    if not directive.get("thread"):
        return
    from nbs_agents.threads import ThreadError, ThreadStore

    try:
        ThreadStore.for_agent_log(agent_log).add(directive["thread"], "note", text, directive=directive["id"])
    except ThreadError:
        pass


def decide_directive(agent_log: Any, directive_id: str, confirm: bool, by: str, source: str = "operator",
                     reason: str = "", chat_role: Optional[str] = None) -> dict[str, Any]:
    """
    A person confirms or rejects a proposed directive (nbs-cli or the web
    UI); it is logged, and a confirmed one also in its role's record.

    Parameters
    ----------
    agent_log : AgentLog
        The agent log.
    directive_id : str
        The directive.
    confirm : bool
        Confirm (it becomes ``active``) or reject it.
    by : str
        The person.
    source : str, optional
        Where it came from (``nbs-cli``, ``web``).
    reason : str, optional
        Why, for a rejection.
    chat_role : str, optional
        The chat role's name, whose record is the main one.

    Returns
    -------
    dict
        ``directive`` (as written) and ``entry`` (the main record's).

    Raises
    ------
    DirectiveError
        If there is no such directive or it is not proposed.
    """
    store = DirectiveStore.for_agent_log(agent_log)
    directive = store.decide(directive_id, confirm, by, source=source, reason=reason)
    what = "confirmed" if confirm else "rejected"
    text = f"{by} {what} directive {directive_id} for {directive['role']}: {directive['text']}"
    if reason:
        text += f" (reason: {reason})"
    entry = log_directive(agent_log, directive, text, source=source, role_record=confirm, chat_role=chat_role, by=by)
    _thread_note(agent_log, directive, text)
    return {"directive": directive, "entry": entry}


def revoke_directive(agent_log: Any, directive_id: str, by: str, source: str = "operator", reason: str = "",
                     chat_role: Optional[str] = None) -> dict[str, Any]:
    """
    A person ends an active directive before it expires; logged as in
    ``decide_directive``.
    """
    store = DirectiveStore.for_agent_log(agent_log)
    directive = store.end(directive_id, "revoked", reason or "revoked", by=by)
    text = f"{by} revoked directive {directive_id} for {directive['role']}: {directive['text']}"
    if reason:
        text += f" (reason: {reason})"
    entry = log_directive(agent_log, directive, text, source=source, role_record=True, chat_role=chat_role, by=by)
    _thread_note(agent_log, directive, text)
    return {"directive": directive, "entry": entry}
