"""
The beamline's staging queue: plans people build up in nbs-gui before
moving them to the QueueServer queue, kept in the beamline Redis so nbs-gui
and nbs-cli share one list.

Two kinds of JSON value, under the beamline Redis prefix::

    {prefix}staging:queue
        {"version": int, "items": [queue item dicts with "item_uid"],
         "updated": float, "updated_by": str}

    {prefix}staging:proposal:{id}
        {"id": str, "base_version": int, "status": "pending" | "accepted" |
         "rejected" | "changes_requested" | "withdrawn" | "stale",
         "ops": [{"op": "add", "item": {...}, "after_uid": uid | None | "top"},
                 {"op": "update", "item_uid": uid, "item": {...}},
                 {"op": "remove", "item_uid": uid},
                 {"op": "move", "item_uid": uid, "after_uid": uid | None | "top"}],
         "reason": str, "author": str, "thread": str or None, "created": float,
         "checks": {"estimate_s": float or None, "warnings": [str], "errors": [str]},
         "decided_by": str or None, "decided_at": float or None}

Only people write the queue (nbs-gui, or a person accepting a proposal with
``StagingStore.accept``, as the web UI does);
nbs-cli only reads it and writes proposals, which expire after
``PROPOSAL_TTL_S``. ``after_uid`` None means the end of the list, ``"top"``
the start; consecutive ``add`` ops with the same ``after_uid`` land in
their own order.
"""

import copy
import json
import time
import uuid
from typing import Any, Callable, Optional

QUEUE_KEY = "staging:queue"
PROPOSAL_TOPIC = "staging:proposal:"
PROPOSAL_TTL_S = 7 * 86400
PROPOSAL_STATUSES = ("pending", "accepted", "rejected", "changes_requested", "withdrawn", "stale")
OPS = ("add", "update", "remove", "move")
TOP = "top"


class StagingError(ValueError):
    """A malformed op, a proposal that does not exist, or a change it cannot take."""


class StaleProposal(StagingError):
    """A proposal whose items are no longer in the staging queue."""


def empty_queue() -> dict[str, Any]:
    """The staging queue before anyone has written it."""
    return {"version": 0, "items": [], "updated": None, "updated_by": None}


def check_ops(ops: Any) -> list[dict[str, Any]]:
    """
    Check the shape of a list of ops.

    Parameters
    ----------
    ops : list of dict
        Ops as in the module docstring.

    Returns
    -------
    list of dict
        The same ops.

    Raises
    ------
    StagingError
        On anything malformed, naming the op by its position.
    """
    if not isinstance(ops, list) or not ops:
        raise StagingError("ops must be a non-empty list")
    for n, op in enumerate(ops, 1):
        if not isinstance(op, dict) or op.get("op") not in OPS:
            raise StagingError(f"op {n}: 'op' must be one of {list(OPS)}")
        kind = op["op"]
        if kind in ("add", "update"):
            item = op.get("item")
            if not isinstance(item, dict) or not item.get("name"):
                raise StagingError(f"op {n} ({kind}): 'item' must be a dict with a 'name'")
            if not isinstance(item.get("args", []), list) or not isinstance(item.get("kwargs", {}), dict):
                raise StagingError(f"op {n} ({kind}): item 'args' must be a list and 'kwargs' a dict")
        if kind != "add" and not op.get("item_uid"):
            raise StagingError(f"op {n} ({kind}): 'item_uid' is required")
        if kind in ("add", "move"):
            after = op.get("after_uid")
            if after is not None and not isinstance(after, str):
                raise StagingError(f"op {n} ({kind}): 'after_uid' must be an item uid, 'top' or null")
    return ops


def referenced_uids(ops: list[dict[str, Any]]) -> list[str]:
    """Item uids the ops expect to find in the staging queue (not ones they add)."""
    out = []
    for op in ops:
        for uid in (op.get("item_uid"), op.get("after_uid")):
            if uid and uid != TOP and uid not in out:
                out.append(uid)
    return out


def check_proposal(proposal: dict[str, Any], queue: dict[str, Any]) -> dict[str, Any]:
    """
    Whether a proposal still fits the staging queue.

    Parameters
    ----------
    proposal : dict
        The proposal.
    queue : dict
        The staging queue now.

    Returns
    -------
    dict
        ``stale`` (an item it refers to is gone, so it cannot apply),
        ``changed`` (the list changed since it was made; it may still apply)
        and ``missing`` uids.
    """
    present = {item.get("item_uid") for item in queue.get("items", [])}
    missing = [uid for uid in referenced_uids(proposal.get("ops", [])) if uid not in present]
    return {
        "stale": bool(missing),
        "changed": proposal.get("base_version") != queue.get("version", 0),
        "missing": missing,
    }


def _position(items: list[dict[str, Any]], after: Optional[str]) -> int:
    if after is None:
        return len(items)
    if after == TOP:
        return 0
    for n, item in enumerate(items):
        if item.get("item_uid") == after:
            return n + 1
    raise StaleProposal(f"item {after} is not in the staging queue")


def _index(items: list[dict[str, Any]], uid: str) -> int:
    for n, item in enumerate(items):
        if item.get("item_uid") == uid:
            return n
    raise StaleProposal(f"item {uid} is not in the staging queue")


def apply_ops(items: list[dict[str, Any]], ops: list[dict[str, Any]],
              new_uid: Callable[[], str] = lambda: str(uuid.uuid4())) -> list[dict[str, Any]]:
    """
    The staging list after ops, for a person accepting a proposal.

    Parameters
    ----------
    items : list of dict
        The staging queue's items now; not changed.
    ops : list of dict
        Ops as in the module docstring, applied in order.
    new_uid : callable, optional
        Makes the ``item_uid`` of each added item.

    Returns
    -------
    list of dict
        New list of new item dicts.

    Raises
    ------
    StaleProposal
        If an op refers to an item that is not there.
    StagingError
        On a malformed op.
    """
    check_ops(ops)
    out = copy.deepcopy(items)
    last_add = None  # (after_uid, uid) of the add just before, so a run of adds keeps its order
    for op in ops:
        kind = op["op"]
        if kind == "add":
            item = copy.deepcopy(op["item"])
            item.setdefault("item_type", "plan")
            item.setdefault("args", [])
            item.setdefault("kwargs", {})
            item["item_uid"] = new_uid()
            after = op.get("after_uid")
            if last_add is not None and last_add[0] == after and after is not None:
                after = last_add[1]
            out.insert(_position(out, after), item)
            last_add = (op.get("after_uid"), item["item_uid"])
            continue
        last_add = None
        if kind == "update":
            n = _index(out, op["item_uid"])
            item = copy.deepcopy(op["item"])
            item.setdefault("item_type", out[n].get("item_type", "plan"))
            item["item_uid"] = op["item_uid"]
            out[n] = item
        elif kind == "remove":
            out.pop(_index(out, op["item_uid"]))
        else:
            if op.get("after_uid") == op["item_uid"]:
                raise StagingError("an item cannot move after itself")
            item = out.pop(_index(out, op["item_uid"]))
            out.insert(_position(out, op.get("after_uid")), item)
    return out


class StagingStore:
    """
    The staging queue and its proposals in the beamline Redis.

    Parameters
    ----------
    redis_client : redis.Redis
        Connected client for the beamline Redis.
    prefix : str, optional
        Beamline Redis prefix (nbs-gui's ``[settings.redis.info] prefix``).
    """

    def __init__(self, redis_client: Any, prefix: str = "") -> None:
        self.redis = redis_client
        self.prefix = prefix

    @property
    def queue_key(self) -> str:
        return f"{self.prefix}{QUEUE_KEY}"

    def _proposal_key(self, proposal_id: str) -> str:
        return f"{self.prefix}{PROPOSAL_TOPIC}{proposal_id}"

    def queue(self) -> dict[str, Any]:
        """The staging queue: ``version``, ``items``, ``updated``, ``updated_by``."""
        raw = self.redis.get(self.queue_key)
        if not raw:
            return empty_queue()
        data = json.loads(raw)
        out = empty_queue()
        out.update({k: data[k] for k in out if k in data})
        out["items"] = list(out["items"] or [])
        return out

    def get(self, proposal_id: str) -> Optional[dict[str, Any]]:
        """One proposal, or None."""
        raw = self.redis.get(self._proposal_key(proposal_id))
        return json.loads(raw) if raw else None

    def proposals(self, statuses: Optional[tuple] = ("pending",)) -> list[dict[str, Any]]:
        """
        Proposals with these statuses (every one with None), oldest first.
        """
        prefix = f"{self.prefix}{PROPOSAL_TOPIC}"
        out = []
        for key in self.redis.scan_iter(match=f"{prefix}*"):
            key = key.decode() if isinstance(key, bytes) else key
            raw = self.redis.get(key)
            if not raw:
                continue
            proposal = json.loads(raw)
            if statuses is None or proposal.get("status") in statuses:
                out.append(proposal)
        return sorted(out, key=lambda p: p.get("created") or 0)

    def _write(self, proposal: dict[str, Any], now: float) -> None:
        # Keep the expiry counted from creation across status changes
        left = int((proposal.get("created") or now) + PROPOSAL_TTL_S - now)
        self.redis.set(self._proposal_key(proposal["id"]), json.dumps(proposal, default=str), ex=max(left, 1))

    def propose(self, ops: list[dict[str, Any]], reason: str, author: str, *, thread: Optional[str] = None,
                checks: Optional[dict[str, Any]] = None, base_version: Optional[int] = None,
                t: Optional[float] = None) -> dict[str, Any]:
        """
        Store a pending proposal.

        Parameters
        ----------
        ops : list of dict
            What to change.
        reason : str
            Why, for the person deciding.
        author : str
            Who proposes it.
        thread : str, optional
            The assistant thread it came from.
        checks : dict, optional
            Validation results.
        base_version : int, optional
            The staging queue version the ops were made on; the current one if omitted.
        t : float, optional
            Time, for tests.

        Returns
        -------
        dict
            The proposal.
        """
        check_ops(ops)
        if not reason or not reason.strip():
            raise StagingError("a reason is required")
        now = time.time() if t is None else t
        if base_version is None:
            base_version = self.queue()["version"]
        proposal = {
            "id": uuid.uuid4().hex[:12],
            "base_version": base_version,
            "status": "pending",
            "ops": ops,
            "reason": reason.strip(),
            "author": author,
            "thread": thread,
            "created": now,
            "checks": checks or {"estimate_s": None, "warnings": [], "errors": []},
            "decided_by": None,
            "decided_at": None,
        }
        self._write(proposal, now)
        return proposal

    def set_status(self, proposal_id: str, status: str, by: str, t: Optional[float] = None,
                   **fields: Any) -> dict[str, Any]:
        """
        Decide or end a pending proposal.

        Parameters
        ----------
        proposal_id : str
            The proposal.
        status : str
            ``accepted``, ``rejected``, ``withdrawn`` or ``stale``.
        by : str
            Who decided.
        t : float, optional
            Time, for tests.
        **fields
            More fields to store, such as ``decision_reason``.

        Returns
        -------
        dict
            The proposal as stored.

        Raises
        ------
        StagingError
            If it does not exist or is no longer pending.
        """
        if status not in PROPOSAL_STATUSES or status == "pending":
            raise StagingError(f"status must be one of {list(PROPOSAL_STATUSES[1:])}")
        proposal = self.get(proposal_id)
        if proposal is None:
            raise StagingError(f"no staging proposal {proposal_id}")
        if proposal.get("status") != "pending":
            raise StagingError(f"proposal {proposal_id} is already {proposal.get('status')}")
        now = time.time() if t is None else t
        proposal.update(fields, status=status, decided_by=by, decided_at=now)
        self._write(proposal, now)
        return proposal

    def accept(self, proposal_id: str, by: str, t: Optional[float] = None, tries: int = 3) -> dict[str, Any]:
        """
        A person accepts a pending proposal: apply its ops to the staging queue.

        The ops apply to the queue as it is now, even if it changed since the
        proposal was made, as long as every item they refer to is still there.
        The queue write is checked against its version, as nbs-gui's are.

        Parameters
        ----------
        proposal_id : str
            The proposal.
        by : str
            Who accepts; recorded as the queue's ``updated_by`` and the
            proposal's ``decided_by``.
        t : float, optional
            Time, for tests.
        tries : int, optional
            Attempts when another writer changes the queue meanwhile.

        Returns
        -------
        dict
            ``proposal`` (accepted) and ``queue`` (the new staging queue).

        Raises
        ------
        StaleProposal
            If an item it refers to is gone; the proposal is marked ``stale``.
        StagingError
            If it does not exist, is not pending, or the queue kept changing.
        """
        from redis.exceptions import WatchError

        proposal = self.get(proposal_id)
        if proposal is None:
            raise StagingError(f"no staging proposal {proposal_id}")
        if proposal.get("status") != "pending":
            raise StagingError(f"proposal {proposal_id} is already {proposal.get('status')}")
        for _ in range(tries):
            now = time.time() if t is None else t
            with self.redis.pipeline() as pipe:
                try:
                    pipe.watch(self.queue_key)
                    raw = pipe.get(self.queue_key)
                    queue = empty_queue()
                    if raw:
                        queue.update({k: v for k, v in json.loads(raw).items() if k in queue})
                    fit = check_proposal(proposal, queue)
                    if fit["stale"]:
                        pipe.reset()
                        self.set_status(proposal_id, "stale", by, t=now)
                        raise StaleProposal(
                            f"proposal {proposal_id} refers to items no longer staged: {fit['missing']}"
                        )
                    new = {
                        "version": int(queue["version"] or 0) + 1,
                        "items": apply_ops(list(queue["items"] or []), proposal["ops"]),
                        "updated": now,
                        "updated_by": by,
                    }
                    pipe.multi()
                    pipe.set(self.queue_key, json.dumps(new, default=str))
                    pipe.execute()
                except WatchError:
                    continue
            return {"proposal": self.set_status(proposal_id, "accepted", by, t=now), "queue": new}
        raise StagingError("the staging queue kept changing; try again")

    def reject(self, proposal_id: str, by: str, t: Optional[float] = None, reason: str = "") -> dict[str, Any]:
        """A person rejects a pending proposal, saying why if they like; the staging queue is not touched."""
        extra = {"decision_reason": reason.strip()} if reason and reason.strip() else {}
        return self.set_status(proposal_id, "rejected", by, t=t, **extra)

    def request_changes(self, proposal_id: str, by: str, text: str, t: Optional[float] = None) -> dict[str, Any]:
        """A person asks the proposer to change a pending proposal; it is closed and a new one should follow."""
        if not text or not text.strip():
            raise StagingError("say what should change")
        return self.set_status(proposal_id, "changes_requested", by, t=t, decision_reason=text.strip())

    def withdraw(self, proposal_id: str, by: str, t: Optional[float] = None) -> dict[str, Any]:
        """Withdraw one's own pending proposal."""
        proposal = self.get(proposal_id)
        if proposal is None:
            raise StagingError(f"no staging proposal {proposal_id}")
        if proposal.get("author") != by:
            raise StagingError(f"proposal {proposal_id} is {proposal.get('author')}'s, not {by}'s")
        return self.set_status(proposal_id, "withdrawn", by, t=t)
