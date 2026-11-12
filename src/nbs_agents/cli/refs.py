"""
Engineering reference scans: past runs that show a part of the beamline
working, described by people, findable from nbs-cli.

An entry points at one run in a Tiled catalog and says what it shows ("M3
pitch rocking curve after the 2026 realignment"). It records where the run is
(``tiled_uri``, ``tiled_path`` and ``uid``, so it still resolves if the catalog
settings change) and a snapshot taken when the entry was made: the start
document's essentials, every baseline reading at the start of the run, and the
run summary nbs-cli gives for it. A reference shows a past working state, never
the current one.

Entries live in three folders, like the knowledge base (``kb.py``):

- ``ref_dir`` holds approved entries, kept in git with the beamline profile.
  nbs-cli never writes there, and the agent sandbox mounts it read-only. Staff
  approve drafts into it from nbs-agents-web, and commit them with git.
- ``ref_agent_dir`` holds the drafts agents write through ``nbs-cli ref
  create``, in ``drafts/``. Agents write nothing else.
- ``ref_review_dir`` holds staff review's record: originals of approved
  drafts, rejected drafts, and staff requests for drafts. Only the web service
  writes there.

An entry is a Markdown file with a YAML front matter block::

    ---
    title: M3 pitch rocking curve
    uid: 4f1c...
    scan_id: 81234
    catalog: default
    tiled_uri: https://tiled.example.org
    tiled_path: sst/raw
    taken: '2026-10-02T14:11:05'
    plan: nbs_count
    devices: [m3_pitch, i0]
    nodes: [m3]
    tags: [alignment]
    aliases: [mirror 3 pitch]
    status: current
    created_by: assistant
    approved_by: Ana
    ---
    What the scan shows and why it is a reference.

and a generated sidecar ``ID.snapshot.json``. ``status`` is ``current``,
``superseded`` (``superseded_by`` names the newer entry) or ``retired``.
An approved entry's ID is its path in ``ref_dir`` without ``.md``; drafts have
IDs starting with ``draft/``. ``README.md`` files are not entries.
"""

import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

import numpy as np

from nbs_agents.paths import paths
from .config import DEFAULT_CATALOG
from .notes import ID_RE, NoteError, fts_rank, parse_note, query_terms, render_note, slugify

DRAFT_PREFIX = "draft/"
SNAPSHOT_SUFFIX = ".snapshot.json"
REQUESTS_FILE = "requests.json"
LIST_KEYS = ("devices", "nodes", "tags", "aliases")
STATUSES = ("current", "superseded", "retired")
# Front matter that says where the run is; set from the run, never edited
RUN_KEYS = ("uid", "scan_id", "catalog", "tiled_uri", "tiled_path", "taken", "plan")
# Longest plan argument (as JSON) the snapshot keeps in full
PLAN_ARG_CHARS = 500


class RefError(NoteError):
    """A reference-library request that cannot be done (shown as the CLI error)."""


@dataclass
class Entry:
    """
    One reference entry.

    Attributes
    ----------
    id : str
        Entry ID.
    meta : dict
        Front matter.
    body : str
        Description (Markdown).
    path : Path
        File holding the entry.
    """

    id: str
    meta: dict[str, Any]
    body: str
    path: Path

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.id)

    @property
    def draft(self) -> bool:
        return self.id.startswith(DRAFT_PREFIX)

    @property
    def status(self) -> str:
        return "draft" if self.draft else str(self.meta.get("status") or "current")

    @property
    def snapshot_path(self) -> Path:
        return self.path.with_name(self.path.name[:-3] + SNAPSHOT_SUFFIX)

    def listed(self, key: str) -> list[str]:
        value = self.meta.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value]

    def snapshot(self) -> Optional[dict[str, Any]]:
        """The sidecar snapshot, or None if there is none."""
        try:
            return json.loads(self.snapshot_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            return {"error": f"cannot read {self.snapshot_path.name}: {exc}"}

    def summary(self) -> dict[str, Any]:
        """Short form for lists and search hits."""
        out = {"id": self.id, "title": self.title, "status": self.status}
        for key in ("scan_id", "uid", "catalog", "taken", "plan", "superseded_by", "created_by", "approved_by",
                    "request"):
            if self.meta.get(key) not in (None, ""):
                out[key] = self.meta[key]
        for key in LIST_KEYS:
            if self.listed(key):
                out[key] = self.listed(key)
        return out

    def full(self) -> dict[str, Any]:
        """Everything, for ``ref show``."""
        out = self.summary()
        out["meta"] = self.meta
        out["body"] = self.body
        out["snapshot"] = self.snapshot()
        return out


class ReferenceLibrary:
    """
    Read approved entries and drafts, write drafts (agents), and apply staff
    review and draft requests (nbs-agents-web only).

    Parameters
    ----------
    human_dir : str or Path, optional
        Folder of approved entries; none if empty.
    agent_dir : str or Path, optional
        Folder for agent drafts; ``$NBS_REF_AGENT_DIR`` or ``paths().references`` if empty.
    review_dir : str or Path, optional
        Folder for the review record; ``$NBS_REF_REVIEW_DIR`` or
        ``references-review`` beside ``agent_dir`` if empty.
    clock : callable, optional
        Time source, for tests.
    """

    def __init__(self, human_dir=None, agent_dir=None, review_dir=None, clock=time.time) -> None:
        self.human_dir = Path(human_dir).expanduser() if human_dir else None
        agent = agent_dir or os.environ.get("NBS_REF_AGENT_DIR") or paths().references
        self.agent_dir = Path(agent).expanduser()
        review = review_dir or os.environ.get("NBS_REF_REVIEW_DIR") or self.agent_dir.parent / "references-review"
        self.review_dir = Path(review).expanduser()
        self.clock = clock

    # Reading

    def _read(self, entry_id: str, path: Path) -> Entry:
        text = path.read_text()
        try:
            meta, body = parse_note(text)
        except NoteError as exc:
            # Keep a broken entry visible, with the reason, instead of hiding every other one
            meta, body = {"error": str(exc)}, text
        return Entry(entry_id, meta, body, path)

    def _folder(self, folder: Optional[Path], prefix: str) -> list[Entry]:
        if folder is None or not folder.is_dir():
            return []
        out = []
        for path in sorted(folder.rglob("*.md")):
            rel = path.relative_to(folder).with_suffix("").as_posix()
            if path.name == "README.md" or (not prefix and rel.startswith(DRAFT_PREFIX)):
                continue
            out.append(self._read(prefix + rel, path))
        return out

    def entries(self) -> list[Entry]:
        """Every approved entry, whatever its status, in path order."""
        return self._folder(self.human_dir, "")

    def drafts(self) -> list[Entry]:
        """Agent drafts waiting for staff review."""
        return self._folder(self.agent_dir / "drafts", DRAFT_PREFIX)

    def _path(self, entry_id: str) -> Path:
        if not ID_RE.match(entry_id):
            raise RefError(f"bad reference id {entry_id!r}")
        if entry_id.startswith(DRAFT_PREFIX):
            return self.agent_dir / "drafts" / (entry_id[len(DRAFT_PREFIX):] + ".md")
        if self.human_dir is None:
            raise RefError(f"no reference {entry_id!r}: no reference folder is set (ref_dir)")
        return self.human_dir / (entry_id + ".md")

    def get(self, entry_id: str) -> Entry:
        """One entry or draft by ID."""
        path = self._path(entry_id)
        if not path.is_file():
            raise RefError(f"no reference {entry_id!r}")
        return self._read(entry_id, path)

    def select(
        self,
        entries: Optional[Iterable[Entry]] = None,
        device: Optional[str] = None,
        node: Optional[str] = None,
        tag: Optional[str] = None,
        all_statuses: bool = False,
    ) -> list[Entry]:
        """Entries matching every filter given; only current ones unless ``all_statuses``."""
        out = []
        for entry in self.entries() if entries is None else entries:
            if not all_statuses and entry.status not in ("current", "draft"):
                continue
            if device and device not in entry.listed("devices"):
                continue
            if node and node not in entry.listed("nodes"):
                continue
            if tag and tag not in entry.listed("tags"):
                continue
            out.append(entry)
        return out

    def search(self, query: str, n: int = 10, **filters: Any) -> list[dict[str, Any]]:
        """
        Rank entries by how well they match the query words (see ``notes.fts_rank``).

        Title, plan, catalog, scan ID, devices, nodes, tags and aliases count
        three times the description.

        Parameters
        ----------
        query : str
            Words to look for; empty lists every entry that passes the filters.
        n : int
            Most hits to return.
        **filters
            Passed to ``select``.

        Returns
        -------
        list of dict
            Entry summaries with ``score``, a ``snippet`` and, for partial
            matches, ``missing``.
        """
        entries = self.select(**filters)
        terms = query_terms(query)
        if not terms:
            return [dict(e.summary(), score=0, snippet=self._first_line(e)) for e in entries][:n]
        hits = []
        for i, score, snippet, missing in fts_rank([(self._head(e), e.body) for e in entries], terms)[:n]:
            hit = entries[i].summary()
            hit["score"] = score
            hit["snippet"] = snippet or self._first_line(entries[i])
            if missing:
                hit["missing"] = missing
            hits.append(hit)
        return hits

    @staticmethod
    def _head(entry: Entry) -> str:
        """What an entry is about: title, plan, catalog, scan ID and its lists."""
        return " ".join([entry.title] + [str(entry.meta.get(k) or "") for k in ("plan", "catalog", "scan_id")] + [
            v for key in LIST_KEYS for v in entry.listed(key)
        ])

    @staticmethod
    def _first_line(entry: Entry) -> str:
        lines = (ln.strip() for ln in entry.body.splitlines())
        return next((ln[:200] for ln in lines if ln and not ln.startswith("#")), "")

    def refs(self, device: Optional[str] = None, node: Optional[str] = None) -> list[dict[str, Any]]:
        """IDs, titles and scan IDs of current entries for a device or node, for ``device``/``diagnose``."""
        return [
            {"id": e.id, "title": e.title, "scan_id": e.meta.get("scan_id")}
            for e in self.select(device=device, node=node)
        ]

    # Writing drafts (agent folder only)

    def create(
        self,
        title: str,
        body: str,
        author: str,
        run: dict[str, Any],
        snapshot: dict[str, Any],
        request: str = "",
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """
        Write a draft entry and its snapshot, for staff to review.

        Parameters
        ----------
        title : str
            Entry title; also gives the ID.
        body : str
            Description (Markdown).
        author : str
            Who is writing (role and session).
        run : dict
            Where the run is: ``uid``, ``scan_id``, ``catalog``, ``tiled_uri``,
            ``tiled_path``, ``taken`` and ``plan`` (see ``run_location``).
        snapshot : dict
            The sidecar (see ``take_snapshot``).
        request : str, optional
            The staff request this draft answers.
        **lists : list of str
            ``devices``, ``nodes``, ``tags`` and ``aliases``.

        Returns
        -------
        dict
            The draft's summary.
        """
        if not title.strip() or not body.strip():
            raise RefError("a reference needs a title and a description")
        if not run.get("uid"):
            raise RefError("a reference needs the run's uid")
        meta = {"title": title.strip(), **{k: run.get(k) for k in RUN_KEYS if run.get(k) not in (None, "")}}
        for key in LIST_KEYS:
            if lists.get(key):
                meta[key] = list(lists[key])
        meta["status"] = "current"
        meta["created_by"] = author
        meta["created"] = self._stamp()
        if request:
            meta["request"] = request
        return self._new(DRAFT_PREFIX, meta, body, snapshot).summary()

    def _new(self, prefix: str, meta: dict[str, Any], body: str, snapshot: Optional[dict[str, Any]]) -> Entry:
        base = slugify(meta["title"])
        entry_id, k = prefix + base, 2
        while self._path(entry_id).exists():
            entry_id, k = f"{prefix}{base}-{k}", k + 1
        path = self._path(entry_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(meta, body))
        entry = self.get(entry_id)
        if snapshot is not None:
            entry.snapshot_path.write_text(json.dumps(snapshot, indent=1, default=_json_default))
        return entry

    # Staff review (nbs-agents-web only: the agent sandbox cannot write ref_dir or review_dir)

    @staticmethod
    def _who(by: str) -> str:
        by = " ".join((by or "").split())
        if not by:
            raise RefError("say who you are: staff review needs a name")
        return by

    def _human_prefix(self) -> str:
        if self.human_dir is None:
            raise RefError("no reference folder is set: set ref_dir to the profile's references folder")
        return ""

    def _draft(self, entry_id: str) -> Entry:
        if not entry_id.startswith(DRAFT_PREFIX):
            raise RefError(f"{entry_id} is not a draft")
        return self.get(entry_id)

    def _approved(self, entry_id: str) -> Entry:
        if entry_id.startswith(DRAFT_PREFIX):
            raise RefError(f"{entry_id} is a draft; approve it first")
        return self.get(entry_id)

    @staticmethod
    def _apply(meta: dict[str, Any], title: Optional[str], lists: dict[str, Optional[list[str]]]) -> None:
        if title is not None:
            if not title.strip():
                raise RefError("a reference needs a title")
            meta["title"] = title.strip()
        for key in LIST_KEYS:
            if lists.get(key) is not None:
                meta.pop(key, None)
                if lists[key]:
                    meta[key] = list(lists[key])

    def approve(
        self,
        entry_id: str,
        by: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        supersedes: str = "",
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """
        Turn a draft into an approved entry in ``ref_dir``, with any staff edits,
        ready to commit.

        The draft moves to ``review_dir/archive``; its snapshot moves with the
        entry. The entry keeps ``created_by`` and records ``approved_by`` and
        ``drafted_from``.

        Parameters
        ----------
        entry_id : str
            A ``draft/...`` entry.
        by : str
            The staff member's name.
        title, body : str, optional
            Replacements; ``None`` keeps the agent's.
        supersedes : str, optional
            An approved entry this one replaces; it is marked superseded.
        **lists : list of str, optional
            Replacement ``devices``, ``nodes``, ``tags`` or ``aliases``.

        Returns
        -------
        dict
            The new entry's summary.
        """
        by = self._who(by)
        draft = self._draft(entry_id)
        prefix = self._human_prefix()
        if supersedes:
            self._approved(supersedes)
        meta = dict(draft.meta)
        self._apply(meta, title, lists)
        body = draft.body if body is None else body
        if not body.strip():
            raise RefError("a reference needs a description")
        meta.update({"status": "current", "approved_by": by, "approved": self._stamp(), "drafted_from": entry_id})
        new = self._new(prefix, meta, body, None)
        if draft.snapshot_path.is_file():
            os.replace(draft.snapshot_path, new.snapshot_path)
        self._retire(draft, "archive")
        if supersedes:
            self.set_status(supersedes, by, "superseded", superseded_by=new.id)
        return self.get(new.id).summary()

    def edit(
        self, entry_id: str, by: str, title: Optional[str] = None, body: Optional[str] = None,
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """Change an approved entry's title, description or lists (never where its run is)."""
        by = self._who(by)
        entry = self._approved(entry_id)
        meta = dict(entry.meta)
        self._apply(meta, title, lists)
        body = entry.body if body is None else body
        if not body.strip():
            raise RefError("a reference needs a description")
        meta.update({"edited_by": by, "edited": self._stamp()})
        entry.path.write_text(render_note(meta, body))
        return self.get(entry_id).summary()

    def set_status(self, entry_id: str, by: str, status: str, superseded_by: str = "") -> dict[str, Any]:
        """
        Mark an approved entry current, superseded (by another approved entry) or retired.

        Parameters
        ----------
        entry_id : str
            The entry.
        by : str
            The staff member's name.
        status : str
            One of ``STATUSES``.
        superseded_by : str, optional
            The entry that replaces it; required for ``superseded``.
        """
        by = self._who(by)
        if status not in STATUSES:
            raise RefError(f"status must be one of {', '.join(STATUSES)}")
        entry = self._approved(entry_id)
        meta = dict(entry.meta)
        meta.pop("superseded_by", None)
        if status == "superseded":
            if not superseded_by:
                raise RefError("say which entry supersedes it")
            if superseded_by == entry_id:
                raise RefError("an entry cannot supersede itself")
            self._approved(superseded_by)
            meta["superseded_by"] = superseded_by
        meta.update({"status": status, "status_by": by, "status_changed": self._stamp()})
        entry.path.write_text(render_note(meta, entry.body))
        return self.get(entry_id).summary()

    def reject(self, entry_id: str, by: str, reason: str = "") -> dict[str, Any]:
        """
        Take a draft out, into ``review_dir/rejected`` with who rejected it and why.
        A staff request it answered is closed.

        Returns
        -------
        dict
            ``{"rejected": entry_id, "file": ...}``.
        """
        by = self._who(by)
        draft = self._draft(entry_id)
        path = self._retire(draft, "rejected", {"rejected_by": by, "rejected": self._stamp(),
                                                "reason": reason.strip()})
        draft.snapshot_path.unlink(missing_ok=True)
        if draft.meta.get("request"):
            self._close_request(str(draft.meta["request"]), by, f"draft {entry_id} rejected")
        return {"rejected": entry_id, "file": str(path)}

    def _retire(self, entry: Entry, folder: str, extra: Optional[dict] = None) -> Path:
        """Move a draft out into ``review_dir/<folder>``, with its snapshot if it still has one."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        target = self.review_dir / folder / f"{entry.id}-{stamp}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_note({**entry.meta, **(extra or {})}, entry.body))
        if entry.snapshot_path.is_file():
            os.replace(entry.snapshot_path, target.with_name(target.name[:-3] + SNAPSHOT_SUFFIX))
        entry.path.unlink()
        return target

    # Staff requests for drafts (written by nbs-agents-web; agents only read them)

    def _requests(self) -> list[dict[str, Any]]:
        path = self.review_dir / REQUESTS_FILE
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise RefError(f"cannot read the request record {path}: {exc}") from exc
        return data if isinstance(data, list) else []

    def _save_requests(self, requests: list[dict[str, Any]]) -> None:
        path = self.review_dir / REQUESTS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(requests, indent=1))
        os.replace(tmp, path)

    def add_request(self, by: str, catalog: str, run: str, purpose: str) -> dict[str, Any]:
        """
        Record a staff request for an agent to draft an entry.

        Parameters
        ----------
        by : str
            The staff member's name.
        catalog : str
            Catalog name.
        run : str
            UID, UID prefix or scan ID.
        purpose : str
            What the scan is a reference for.

        Returns
        -------
        dict
            The request: ``id``, ``by``, ``at``, ``catalog``, ``run``, ``purpose``.
        """
        by = self._who(by)
        run = run.strip()
        if not run or not ID_RE.match(run) or "/" in run:
            raise RefError("give the run as a UID, UID prefix or scan ID")
        if not purpose.strip():
            raise RefError("say what the scan is a reference for")
        request = {"id": "req-" + secrets.token_hex(4), "by": by, "at": self._stamp(),
                   "catalog": catalog or DEFAULT_CATALOG, "run": run, "purpose": purpose.strip()}
        self._save_requests(self._requests() + [request])
        return request

    def _close_request(self, request_id: str, by: str, reason: str) -> Optional[dict[str, Any]]:
        requests = self._requests()
        for request in requests:
            if request["id"] == request_id and not request.get("closed"):
                request.update({"closed": self._stamp(), "closed_by": by, "close_reason": reason})
                self._save_requests(requests)
                return request
        return None

    def cancel_request(self, request_id: str, by: str) -> dict[str, Any]:
        """Close a request staff no longer want answered."""
        by = self._who(by)
        closed = self._close_request(request_id, by, "cancelled")
        if closed is None:
            raise RefError(f"no open request {request_id}")
        return closed

    def pending_requests(self) -> list[dict[str, Any]]:
        """
        Requests no draft or entry answers yet, oldest first.

        A draft or entry answers a request if it names it (``request``), or if
        it is for the same catalog and its uid starts with, or scan ID equals,
        the requested run.
        """
        found = self.drafts() + self.entries()
        out = []
        for request in self._requests():
            if request.get("closed"):
                continue
            if not any(self._answers(e, request) for e in found):
                out.append(request)
        return out

    @staticmethod
    def _answers(entry: Entry, request: dict[str, Any]) -> bool:
        if entry.meta.get("request") == request["id"]:
            return True
        if str(entry.meta.get("catalog") or DEFAULT_CATALOG) != request.get("catalog", DEFAULT_CATALOG):
            return False
        run = str(request.get("run", ""))
        return bool(run) and (str(entry.meta.get("uid", "")).startswith(run) or str(entry.meta.get("scan_id")) == run)

    def review_queue(self) -> dict[str, Any]:
        """Drafts (in full) and open requests, and how many there are of both."""
        drafts = [d.full() for d in self.drafts()]
        requests = self.pending_requests()
        return {"drafts": drafts, "requests": requests, "count": len(drafts) + len(requests)}

    def _stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock()))


# ---------------------------------------------------------------- Tiled side


def catalogs(config: Any) -> dict[str, dict[str, str]]:
    """Every catalog by name: ``default`` (the install's own) and the ``[references.catalogs]`` ones."""
    out = {DEFAULT_CATALOG: {"uri": getattr(config, "tiled_uri", ""),
                             "path": (getattr(config, "tiled_path", "") or "").strip("/")}}
    out.update(getattr(config, "ref_catalogs", None) or {})
    return out


def _server(uri: str) -> tuple[str, str]:
    parts = urlsplit(uri)
    return parts.scheme.lower(), parts.netloc.lower()


def open_catalog(config: Any, uri: str, path: str, interactive: bool = False):
    """
    The Tiled container at ``uri`` and ``path``.

    On the install's own server this connects exactly as nbs-cli always does
    (``tiled_auth.connect``): the configured or agent API key, else the cached
    login. On another server no API key is sent, since a key is only good for
    the server that made it: the cached login for that server is used, if
    there is one.

    Raises
    ------
    RefError
        If the server cannot be reached, needs a login, or the path cannot be read.
    """
    from dataclasses import replace

    from nbs_agents.tiled_auth import TiledLoginRequired, _navigate, connect, login_context

    try:
        if _server(uri) == _server(config.tiled_uri):
            return connect(replace(config, tiled_path=path), interactive=interactive)
        from tiled.client import from_context

        return _navigate(from_context(login_context(uri, interactive)), path)
    except TiledLoginRequired as exc:
        raise RefError(f"{exc} (agents hold a key only for {config.tiled_uri})") from exc
    except KeyError as exc:
        raise RefError(
            f"cannot read {path!r} on {uri}: it does not exist, or this login or key may not read it. "
            "The agents' key reads whatever the user who started nbs-agents-runner can read on the "
            "install's own Tiled server, and nothing on other servers."
        ) from exc


def resolve_refs(config: Any, entries: list) -> list[dict[str, Any]]:
    """
    Check that each entry's run is still in Tiled, at the URI and path the entry saved.

    Returns
    -------
    list of dict
        One per entry: ``id``, ``resolves`` (True, False, or None when the
        catalog could not be read) and ``error`` when it does not resolve.
    """
    clients: dict[tuple[str, str], Any] = {}
    out = []
    for entry in entries:
        m = entry.meta
        where = (str(m.get("tiled_uri") or config.tiled_uri), str(m.get("tiled_path") or ""))
        result: dict[str, Any] = {"id": entry.id}
        try:
            if where not in clients:
                try:
                    clients[where] = open_catalog(config, *where)
                except Exception as exc:
                    clients[where] = exc
            client = clients[where]
            if isinstance(client, Exception):
                raise client
            if not m.get("uid"):
                raise RefError("the entry has no uid")
            try:
                client[str(m["uid"])]
                result["resolves"] = True
            except KeyError:
                result.update(resolves=False, error=f"run {m['uid']} is not in {where[1] or '/'} on {where[0]}")
        except Exception as exc:
            result.update(resolves=None, error=f"{type(exc).__name__}: {exc}")
        configured = catalogs(config).get(str(m.get("catalog") or DEFAULT_CATALOG))
        if configured and (configured["uri"], configured["path"]) != where:
            result["note"] = (f"catalog {m.get('catalog')!r} is now {configured['uri']} {configured['path']!r}; "
                              "the entry keeps where the run was saved")
        out.append(result)
    return out


def run_location(name: str, catalog: dict[str, str], run: Any) -> dict[str, Any]:
    """The front matter that says where a run is: catalog, URI, path, uid, scan ID, time, plan."""
    start = run.metadata["start"]
    taken = start.get("time")
    return {
        "uid": start["uid"],
        "scan_id": start.get("scan_id"),
        "catalog": name,
        "tiled_uri": catalog["uri"],
        "tiled_path": catalog.get("path", ""),
        "taken": datetime.fromtimestamp(taken).isoformat(timespec="seconds") if taken else None,
        "plan": start.get("plan_name"),
    }


def _json_default(obj: Any) -> Any:
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return str(obj)


def _short(value: Any) -> Any:
    """A start-document value for the snapshot: kept unless it is long as JSON."""
    text = json.dumps(value, default=_json_default)
    return value if len(text) <= PLAN_ARG_CHARS else text[:PLAN_ARG_CHARS] + "..."


def _scalar(values: Any) -> Any:
    """The first reading of a baseline key as a plain value, or a note of its shape."""
    arr = np.asarray(values)
    if arr.size == 0:
        return None
    first = arr.reshape(arr.shape[0], -1)[0] if arr.ndim else arr.reshape(1)
    if first.size != 1:
        return {"shape": list(arr.shape[1:])}
    value = first[0].item() if isinstance(first[0], np.generic) else first[0]
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


def baseline_start(run: Any) -> dict[str, Any]:
    """
    Every baseline reading at the start of a run.

    Returns
    -------
    dict
        ``{key: value}``, empty if the run has no baseline. Array readings
        are given as ``{"shape": [...]}``.
    """
    if "baseline" not in run:
        return {}
    stream = run["baseline"]
    try:
        ds = stream.read()
        values = {k: ds[k].values for k in ds.data_vars}
    except Exception:
        values = {k: stream[k].read() for k in stream}
    out = {}
    for key in sorted(values):
        try:
            out[key] = _scalar(values[key])
        except Exception:  # one odd key must not lose the rest
            out[key] = None
    return out


def scan_ranges(start: dict[str, Any], data: dict[str, Any]) -> dict[str, list]:
    """``{motor: [first, last, min, max]}`` for each scanned motor with numeric data in the primary stream."""
    out = {}
    for motor in start.get("motors") or []:
        key = motor if motor in data else next((k for k in sorted(data) if k.startswith(f"{motor}_")), None)
        if key is None:
            continue
        try:
            arr = np.asarray(data[key], dtype=float).ravel()
        except (TypeError, ValueError):
            continue
        if arr.size:
            out[motor] = [float(f"{v:.6g}") for v in (arr[0], arr[-1], np.nanmin(arr), np.nanmax(arr))]
    return out


def take_snapshot(bl: Any, run: Any) -> dict[str, Any]:
    """
    What a reference records about its run when it is made.

    Parameters
    ----------
    bl : nbs_agents.cli.core.Beamline
        Client whose ``tiled`` is the run's catalog.
    run : BlueskyRun
        The run.

    Returns
    -------
    dict
        ``start`` (plan, plan arguments, motors and their scanned range as
        ``[first, last, min, max]``, detectors, sample, edge, ...),
        ``baseline`` (every baseline reading at the start) and ``summary``
        (``nbs-cli run``'s summary, with the reducer's).
    """
    start = dict(run.metadata["start"])
    essentials: dict[str, Any] = {}
    for key in ("uid", "scan_id", "plan_name", "plan_type", "motors", "detectors", "num_points", "sample_name",
                "sample_id", "edge", "element", "purpose", "operator"):
        if start.get(key) not in (None, "", []):
            essentials[key] = start[key]
    if start.get("time"):
        essentials["time"] = datetime.fromtimestamp(start["time"]).isoformat(timespec="seconds")
    if start.get("plan_args"):
        essentials["plan_args"] = {k: _short(v) for k, v in dict(start["plan_args"]).items()}
    try:
        data = bl.run_data(run)
    except Exception:
        data = {}
    ranges = scan_ranges(start, data)
    if ranges:
        essentials["ranges"] = ranges
    return {"start": essentials, "baseline": baseline_start(run), "summary": bl.run_summary(run)}
