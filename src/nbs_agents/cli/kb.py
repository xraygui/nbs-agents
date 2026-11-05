"""
Beamline knowledge base: short Markdown notes about hardware, problems,
calibration routines and quirks, searchable from nbs-cli.

Notes live in three folders, and the folder says who wrote a note:

- ``kb_dir`` holds human notes, kept in git with the beamline profile. nbs-cli
  never writes there, and the agent sandbox mounts it read-only. Staff write
  and approve notes into it from nbs-agents-web, and commit them with git.
- ``kb_agent_dir`` holds what agents wrote through ``nbs-cli kb``: their own
  notes, which they may edit, drafts waiting for staff review, and appendices
  to any note, which only grow.
- ``kb_review_dir`` holds staff review's record: what was kept or reviewed,
  originals of approved notes, and what was rejected. Only the web service
  writes there; the agent sandbox mounts it read-only, so an agent cannot mark
  its own note reviewed.

A note is a Markdown file with a YAML front matter block::

    ---
    title: Exit slit and resolution
    topic: hardware
    devices: [eslit, en]
    nodes: [mono]
    tags: [resolution, flux]
    aliases: [energy resolution, bandwidth]
    ---
    Body text.

``aliases`` is optional: other words people use for the subject, searched
like the title (see ``KnowledgeBase.search``).

A human note's ID is its path inside ``kb_dir`` without ``.md``
(``mono/exit-slit``). Agent notes have IDs starting with ``agent/``, drafts
and drafts ``draft/``. Drafts are not searched or listed until
staff approve them. The appendix for note ``X`` is
``kb_agent_dir/appendix/X.md``; each entry is a ``## Appended by ... at ...``
section. ``README.md`` files are not notes.

Staff review in nbs-agents-web: approving an agent note or draft writes it into
``kb_dir`` as a human note, ready to commit; rejecting moves it to
``kb_review_dir/rejected``. An agent note can also be kept as it is, and
appendix entries marked reviewed or removed.
"""

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from nbs_agents.paths import paths

AGENT_PREFIX = "agent/"
DRAFT_PREFIX = "draft/"
APPENDIX = "appendix"
REVIEW_FILE = "review.json"
ENTRY_RE = re.compile(r"^## Appended by (.*) at (\d{4}-\d\d-\d\d \d\d:\d\d)$", re.M)
LIST_KEYS = ("devices", "nodes", "tags")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
WORD_RE = re.compile(r"[a-z0-9_]+")
# Query words that say nothing about the subject; dropped so that a natural
# question ("why is i0 noisy") is not judged on "why" and "is"
STOP_WORDS = frozenset(
    "a an and are as at be by can do does for from how i if in is it its of on or "
    "the there this to was what when where which who why will with".split()
)
# Index columns: what a note is about (weighted), and its text
HEAD_WEIGHT, BODY_WEIGHT = 3.0, 1.0
SNIPPET_TOKENS = 24


def fts_term(word: str) -> str:
    """
    One query word as an FTS5 query: quoted, so no word is read as FTS5 syntax,
    and a prefix match for words of four letters or more (``reflect`` finds
    ``reflectivity``). The porter tokenizer matches plurals and endings.
    """
    return f'"{word}"*' if len(word) >= 4 else f'"{word}"'


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*$")


class KBError(Exception):
    """A knowledge-base request that cannot be done (shown as the CLI error)."""


@dataclass
class Note:
    """
    One note and its appendix.

    Attributes
    ----------
    id : str
        Note ID.
    written_by : str
        ``"human"`` (kb_dir) or ``"agent"`` (agent notes and drafts), from the
        folder the note is in.
    meta : dict
        Front matter.
    body : str
        Markdown body without the front matter.
    appendix : str
        Agent-appended sections, or ``""``.
    path : Path
        File holding the note.
    """

    id: str
    written_by: str
    meta: dict[str, Any]
    body: str
    appendix: str = ""
    path: Optional[Path] = None

    @property
    def title(self) -> str:
        return str(self.meta.get("title") or self.id)

    def listed(self, key: str) -> list[str]:
        value = self.meta.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v) for v in value]

    def summary(self) -> dict[str, Any]:
        """Short form for lists and search hits."""
        out = {"id": self.id, "title": self.title, "written_by": self.written_by}
        if self.id.startswith(DRAFT_PREFIX):
            out["draft"] = True
        for key in ("author", "approved_by", "topic"):
            if self.meta.get(key):
                out[key] = self.meta[key]
        for key in LIST_KEYS:
            if self.listed(key):
                out[key] = self.listed(key)
        if self.appendix:
            out["appended"] = self.appendix.count("\n## Appended by ")
        return out

    def full(self) -> dict[str, Any]:
        """Everything, for ``kb show``."""
        out = self.summary()
        out["meta"] = self.meta
        out["body"] = self.body
        if self.appendix:
            out["appendix"] = self.appendix
        return out


def parse_note(text: str) -> tuple[dict[str, Any], str]:
    """
    Split a note into front matter and body.

    Parameters
    ----------
    text : str
        File contents.

    Returns
    -------
    tuple of (dict, str)
        Front matter (empty without a block) and body.
    """
    match = FRONT_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise KBError(f"bad front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise KBError("front matter must be a mapping")
    return meta, match.group(2)


def render_note(meta: dict[str, Any], body: str) -> str:
    """Front matter block plus body, as written to a file."""
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
    return f"---\n{front}\n---\n{body.rstrip()}\n"


def slugify(text: str) -> str:
    """A note-ID segment from a title: lowercase words joined by ``-``."""
    slug = "-".join(WORD_RE.findall(text.lower()))[:60].strip("-")
    return slug or "note"


class KnowledgeBase:
    """
    Read human and agent notes, write agent notes, drafts and appendices, and
    apply staff review (nbs-agents-web only).

    Parameters
    ----------
    human_dir : str or Path, optional
        Folder of human notes; none if empty.
    agent_dir : str or Path, optional
        Folder for agent writes; ``$NBS_KB_AGENT_DIR`` or ``paths().kb_agent`` if empty.
    review_dir : str or Path, optional
        Folder for the review record; ``$NBS_KB_REVIEW_DIR`` or ``kb-review``
        beside ``agent_dir`` if empty.
    clock : callable, optional
        Time source, for tests.
    """

    def __init__(self, human_dir=None, agent_dir=None, review_dir=None, clock=time.time) -> None:
        self.human_dir = Path(human_dir).expanduser() if human_dir else None
        agent = agent_dir or os.environ.get("NBS_KB_AGENT_DIR") or paths().kb_agent
        self.agent_dir = Path(agent).expanduser()
        review = review_dir or os.environ.get("NBS_KB_REVIEW_DIR") or self.agent_dir.parent / "kb-review"
        self.review_dir = Path(review).expanduser()
        self.clock = clock

    # Reading

    def _read(self, note_id: str, path: Path, written_by: str) -> Note:
        text = path.read_text()
        try:
            meta, body = parse_note(text)
        except KBError as exc:
            # Keep a broken note visible, with the reason, instead of hiding every other note
            meta, body = {"error": str(exc)}, text
        appendix_path = self._appendix_path(note_id)
        appendix = appendix_path.read_text() if appendix_path.is_file() else ""
        return Note(note_id, written_by, meta, body, appendix, path)

    def notes(self) -> list[Note]:
        """All notes but drafts: human, then agent, each folder in path order."""
        out = []
        reserved = (AGENT_PREFIX, DRAFT_PREFIX, APPENDIX + "/")
        if self.human_dir is not None and self.human_dir.is_dir():
            for path in sorted(self.human_dir.rglob("*.md")):
                rel = path.relative_to(self.human_dir).with_suffix("").as_posix()
                if path.name == "README.md" or rel.startswith(reserved):
                    continue
                out.append(self._read(rel, path, "human"))
        out += self._folder(self.agent_dir / "notes", AGENT_PREFIX, "agent")
        return out

    def drafts(self) -> list[Note]:
        """Agent drafts waiting for staff review."""
        return self._folder(self.agent_dir / "drafts", DRAFT_PREFIX, "agent")

    def _folder(self, folder: Path, prefix: str, written_by: str) -> list[Note]:
        if not folder.is_dir():
            return []
        return [
            self._read(prefix + path.relative_to(folder).with_suffix("").as_posix(), path, written_by)
            for path in sorted(folder.rglob("*.md"))
        ]

    def _path(self, note_id: str) -> tuple[Path, str]:
        if not ID_RE.match(note_id):
            raise KBError(f"bad note id {note_id!r}")
        for prefix, folder, written_by in (
            (AGENT_PREFIX, self.agent_dir / "notes", "agent"),
            (DRAFT_PREFIX, self.agent_dir / "drafts", "agent"),
        ):
            if note_id.startswith(prefix):
                return folder / (note_id[len(prefix):] + ".md"), written_by
        if self.human_dir is None:
            raise KBError(f"no note {note_id!r}: no human notes folder is set (kb_dir)")
        return self.human_dir / (note_id + ".md"), "human"

    def _appendix_path(self, note_id: str) -> Path:
        return self.agent_dir / APPENDIX / (note_id + ".md")

    def get(self, note_id: str) -> Note:
        """One note by ID."""
        path, written_by = self._path(note_id)
        if not path.is_file():
            raise KBError(f"no note {note_id!r}")
        return self._read(note_id, path, written_by)

    def select(
        self,
        notes: Optional[Iterable[Note]] = None,
        device: Optional[str] = None,
        node: Optional[str] = None,
        topic: Optional[str] = None,
        written_by: Optional[str] = None,
    ) -> list[Note]:
        """Notes matching every filter given."""
        out = []
        for note in self.notes() if notes is None else notes:
            if device and device not in note.listed("devices"):
                continue
            if node and node not in note.listed("nodes"):
                continue
            if topic and str(note.meta.get("topic", "")) != topic:
                continue
            if written_by and note.written_by != written_by:
                continue
            out.append(note)
        return out

    def search(self, query: str, n: int = 10, **filters: Any) -> list[dict[str, Any]]:
        """
        Rank notes by how well they match the query words, with SQLite FTS5.

        Notes containing more of the query words come first, then BM25 relevance
        (rare words count more than common ones); title, topic, devices, nodes,
        tags and aliases count three times the body and appendix. Words match
        across plurals and endings (porter stemmer), and words of four letters
        or more match as prefixes. Common words ("why", "is") are ignored. A note
        that misses some words is still listed, after the full matches, with the
        words it lacks under ``missing``.

        The index is built in memory on every call from the note files, so there
        is no index file to keep up to date.

        Parameters
        ----------
        query : str
            Words to look for; empty lists every note that passes the filters.
        n : int
            Most hits to return.
        **filters
            Passed to ``select``.

        Returns
        -------
        list of dict
            Note summaries with ``score`` (higher is better), a ``snippet`` and,
            for partial matches, ``missing``.
        """
        notes = self.select(**filters)
        words = list(dict.fromkeys(WORD_RE.findall(query.lower())))
        terms = [w for w in words if w not in STOP_WORDS] or words
        if not terms:
            return [dict(note.summary(), score=0, snippet=self._first_line(note)) for note in notes][:n]

        db = sqlite3.connect(":memory:")
        try:
            db.execute("CREATE VIRTUAL TABLE kb USING fts5(head, body, tokenize='porter unicode61')")
            db.executemany("INSERT INTO kb (rowid, head, body) VALUES (?, ?, ?)", [
                (i, self._head(note), note.body + "\n" + note.appendix) for i, note in enumerate(notes)
            ])
            found: dict[int, list[str]] = {}
            for term in terms:
                for (rowid,) in db.execute("SELECT rowid FROM kb WHERE kb MATCH ?", (fts_term(term),)):
                    found.setdefault(rowid, []).append(term)
            rows = db.execute(
                f"SELECT rowid, bm25(kb, {HEAD_WEIGHT}, {BODY_WEIGHT}), "
                f"snippet(kb, 1, '', '', '...', {SNIPPET_TOKENS}) FROM kb WHERE kb MATCH ?",
                (" OR ".join(fts_term(t) for t in terms),),
            ).fetchall()
        finally:
            db.close()

        hits = []
        for rowid, rank, snippet in rows:
            note = notes[rowid]
            hit = note.summary()
            hit["score"] = round(-rank, 3)
            hit["snippet"] = " ".join(snippet.split()) or self._first_line(note)
            missing = [t for t in terms if t not in found.get(rowid, ())]
            if missing:
                hit["missing"] = missing
            hits.append((len(missing), rank, hit))
        hits.sort(key=lambda h: h[:2])
        return [hit for _, _, hit in hits[:n]]

    @staticmethod
    def _head(note: Note) -> str:
        """What a note is about: title, topic, devices, nodes, tags and aliases."""
        return " ".join([note.title, str(note.meta.get("topic", ""))] + [
            v for key in LIST_KEYS + ("aliases",) for v in note.listed(key)
        ])

    @staticmethod
    def _first_line(note: Note) -> str:
        lines = (ln.strip() for ln in (note.body + "\n" + note.appendix).splitlines())
        return next((ln[:200] for ln in lines if ln and not ln.startswith("#")), "")

    def index(self, **filters: Any) -> dict[str, Any]:
        """
        What the knowledge base covers: every topic, device, node and tag with
        the notes that use it, and every note's title.

        Parameters
        ----------
        **filters
            Passed to ``select``.

        Returns
        -------
        dict
            ``notes`` (ID, title, written_by), ``counts`` per written_by, and
            ``topics``, ``devices``, ``nodes``, ``tags``, each mapping a value
            to the IDs of the notes that use it, most used first.
        """
        notes = self.select(**filters)
        out: dict[str, Any] = {
            "notes": [{"id": n.id, "title": n.title, "written_by": n.written_by} for n in notes],
            "counts": {},
        }
        for note in notes:
            out["counts"][note.written_by] = out["counts"].get(note.written_by, 0) + 1
        for key, plural in (("topic", "topics"), *((k, k) for k in LIST_KEYS)):
            used: dict[str, list[str]] = {}
            for note in notes:
                values = [str(note.meta["topic"])] if key == "topic" and note.meta.get("topic") else (
                    note.listed(key) if key != "topic" else [])
                for value in values:
                    used.setdefault(value, []).append(note.id)
            out[plural] = dict(sorted(used.items(), key=lambda kv: (-len(kv[1]), kv[0])))
        return out

    def refs(self, device: Optional[str] = None, node: Optional[str] = None) -> list[dict[str, str]]:
        """IDs and titles of notes tagged with a device or node, for ``device``/``diagnose``."""
        return [
            {"id": n.id, "title": n.title, "written_by": n.written_by}
            for n in self.select(device=device, node=node)
        ]

    # Writing (agent folder only)

    def add(
        self, title: str, body: str, author: str, topic: str = "", draft: bool = False, **lists: list[str]
    ) -> dict[str, Any]:
        """
        Write a new agent note, or a draft for staff to review.

        Parameters
        ----------
        title : str
            Note title; also gives the ID.
        body : str
            Markdown body.
        author : str
            Who is writing (role and session).
        topic : str
            Optional topic.
        draft : bool
            Write a draft (``draft/...``), which stays out of search until staff
            approve it, instead of a live agent note.
        **lists : list of str
            ``devices``, ``nodes`` and ``tags``.

        Returns
        -------
        dict
            The new note's summary.
        """
        return self._new(DRAFT_PREFIX if draft else AGENT_PREFIX, title, body, {"author": author}, topic, lists)

    def _new(self, prefix: str, title: str, body: str, meta: dict, topic: str, lists: dict) -> dict[str, Any]:
        if not title.strip() or not body.strip():
            raise KBError("a note needs a title and a body")
        base = slugify(title)
        note_id, k = prefix + base, 2
        while self._path(note_id)[0].exists():
            note_id, k = f"{prefix}{base}-{k}", k + 1
        meta = {"title": title.strip(), **meta, "created": self._stamp()}
        if topic:
            meta["topic"] = topic
        for key in LIST_KEYS:
            if lists.get(key):
                meta[key] = list(lists[key])
        path = self._path(note_id)[0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_note(meta, body))
        return self.get(note_id).summary()

    def edit(
        self,
        note_id: str,
        author: str,
        body: Optional[str] = None,
        title: Optional[str] = None,
        topic: Optional[str] = None,
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """
        Change an agent note or draft. Human notes are refused: append to them instead.

        Parameters
        ----------
        note_id : str
            An ``agent/...`` or ``draft/...`` note.
        author : str
            Who is editing; recorded as ``edited_by``.
        body, title, topic : str, optional
            Replacements; ``None`` keeps the current value.
        **lists : list of str, optional
            Replacement ``devices``, ``nodes`` or ``tags``.

        Returns
        -------
        dict
            The note's summary.
        """
        note = self.get(note_id)
        if note.written_by != "agent":
            raise KBError(f"{note_id} is a human note; agents may only append to it (kb append)")
        meta = dict(note.meta)
        if title is not None:
            meta["title"] = title.strip()
        if topic is not None:
            meta["topic"] = topic
        for key in LIST_KEYS:
            if lists.get(key) is not None:
                meta[key] = list(lists[key])
        meta["edited_by"] = author
        meta["edited"] = self._stamp()
        note.path.write_text(render_note(meta, note.body if body is None else body))
        return self.get(note_id).summary()

    def append(self, note_id: str, text: str, author: str) -> dict[str, Any]:
        """
        Append a dated, attributed section to any note's appendix.

        Parameters
        ----------
        note_id : str
            Human or agent note.
        text : str
            Markdown to add.
        author : str
            Who is appending.

        Returns
        -------
        dict
            The note's summary.
        """
        if not text.strip():
            raise KBError("nothing to append")
        self.get(note_id)
        path = self._appendix_path(note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(f"\n## Appended by {author} at {self._stamp()}\n\n{text.strip()}\n")
        return self.get(note_id).summary()

    # Staff review (nbs-agents-web only: the agent sandbox cannot write kb_dir or review_dir)

    def _review(self) -> dict[str, Any]:
        path = self.review_dir / REVIEW_FILE
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as exc:
            raise KBError(f"cannot read the review record {path}: {exc}") from exc
        data.setdefault("notes", {})
        data.setdefault("appendix", {})
        return data

    def _save_review(self, data: dict[str, Any]) -> None:
        path = self.review_dir / REVIEW_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, path)

    @staticmethod
    def _who(by: str) -> str:
        by = " ".join((by or "").split())
        if not by:
            raise KBError("say who you are: staff review needs a name")
        return by

    def entries(self, note_id: str) -> list[dict[str, Any]]:
        """A note's appendix as a list of ``{index, author, at, text}``."""
        text = self.get(note_id).appendix
        found = list(ENTRY_RE.finditer(text))
        return [
            {
                "index": i,
                "author": m.group(1),
                "at": m.group(2),
                "text": text[m.end():found[i + 1].start() if i + 1 < len(found) else len(text)].strip(),
            }
            for i, m in enumerate(found)
        ]

    def review_queue(self) -> dict[str, Any]:
        """
        What staff have not reviewed yet.

        Returns
        -------
        dict
            ``notes``: drafts, then agent notes that are new or changed since
            they were kept, each in full. ``appendices``: notes with unreviewed
            appendix entries, as ``{id, title, written_by, entries}``.
            ``count``: notes plus entries waiting.
        """
        review = self._review()
        notes = []
        for note in self.drafts() + self._folder(self.agent_dir / "notes", AGENT_PREFIX, "agent"):
            kept = review["notes"].get(note.id)
            if not kept or kept.get("mtime") != note.path.stat().st_mtime_ns:
                notes.append(note.full())
        appendices, count = [], len(notes)
        for note in self.notes():
            if not note.appendix:
                continue
            seen = review["appendix"].get(note.id, {}).get("entries", 0)
            waiting = self.entries(note.id)[seen:]
            if waiting:
                appendices.append({"id": note.id, "title": note.title, "written_by": note.written_by,
                                   "entries": waiting})
                count += len(waiting)
        return {"notes": notes, "appendices": appendices, "count": count}

    def write_note(
        self,
        by: str,
        title: str,
        body: str,
        topic: str = "",
        note_id: Optional[str] = None,
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """
        Write a new human note into ``kb_dir``, or change one (``note_id`` given).

        Parameters
        ----------
        by : str
            The staff member's name.
        title, body, topic : str
            Note content.
        note_id : str, optional
            A human note to replace the content of.
        **lists : list of str
            ``devices``, ``nodes`` and ``tags``.

        Returns
        -------
        dict
            The note's summary.
        """
        by = self._who(by)
        if note_id is None:
            return self._new(self._human_prefix(), title, body, {"author": by}, topic, lists)
        if note_id.startswith((AGENT_PREFIX, DRAFT_PREFIX)):
            raise KBError(f"{note_id} is an agent note; approve it to make it a human note")
        if not title.strip() or not body.strip():
            raise KBError("a note needs a title and a body")
        note = self.get(note_id)
        meta = {**note.meta, "title": title.strip(), "edited_by": by, "edited": self._stamp()}
        meta.pop("topic", None)
        if topic:
            meta["topic"] = topic
        for key in LIST_KEYS:
            meta.pop(key, None)
            if lists.get(key):
                meta[key] = list(lists[key])
        note.path.write_text(render_note(meta, body))
        return self.get(note_id).summary()

    def approve(
        self,
        note_id: str,
        by: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        topic: Optional[str] = None,
        **lists: Optional[list[str]],
    ) -> dict[str, Any]:
        """
        Turn an agent note or draft into a human note in ``kb_dir``, with any
        staff edits, ready to commit.

        The original moves to ``review_dir/archive`` and its appendix follows the
        note. The new note keeps the agent as ``author`` and records
        ``approved_by`` and ``drafted_from``.

        Parameters
        ----------
        note_id : str
            An ``agent/...`` or ``draft/...`` note.
        by : str
            The staff member's name.
        title, body, topic : str, optional
            Replacements; ``None`` keeps the agent's.
        **lists : list of str, optional
            Replacement ``devices``, ``nodes`` or ``tags``.

        Returns
        -------
        dict
            The new note's summary.
        """
        by = self._who(by)
        note = self._agent_note(note_id)
        meta = {k: v for k, v in note.meta.items() if k in ("author", "topic", *LIST_KEYS)}
        meta.update({"approved_by": by, "approved": self._stamp(), "drafted_from": note_id})
        if topic is not None:
            meta.pop("topic", None)
            if topic:
                meta["topic"] = topic
        for key in LIST_KEYS:
            if lists.get(key) is not None:
                meta.pop(key, None)
                if lists[key]:
                    meta[key] = list(lists[key])
        topic_value = meta.pop("topic", "")
        new = self._new(self._human_prefix(), note.title if title is None else title,
                        note.body if body is None else body, meta, topic_value,
                        {key: meta.pop(key, None) for key in LIST_KEYS})
        self._retire(note, "archive")
        old_appendix = self._appendix_path(note_id)
        if old_appendix.is_file():
            target = self._appendix_path(new["id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old_appendix, target)
        review = self._review()
        review["notes"].pop(note_id, None)
        if note_id in review["appendix"]:
            review["appendix"][new["id"]] = review["appendix"].pop(note_id)
        self._save_review(review)
        return self.get(new["id"]).summary()

    def reject(self, note_id: str, by: str, reason: str = "") -> dict[str, Any]:
        """
        Take an agent note or draft out of the knowledge base, into
        ``review_dir/rejected`` with who rejected it and why.

        Returns
        -------
        dict
            ``{"rejected": note_id, "file": ...}``.
        """
        by = self._who(by)
        note = self._agent_note(note_id)
        path = self._retire(note, "rejected", {"rejected_by": by, "rejected": self._stamp(),
                                               "reason": reason.strip()})
        self._appendix_path(note_id).unlink(missing_ok=True)
        review = self._review()
        review["notes"].pop(note_id, None)
        review["appendix"].pop(note_id, None)
        self._save_review(review)
        return {"rejected": note_id, "file": str(path)}

    def keep(self, note_id: str, by: str) -> dict[str, Any]:
        """
        Leave an agent note as it is and take it off the review list until an
        agent changes it.
        """
        by = self._who(by)
        note = self._agent_note(note_id)
        if note_id.startswith(DRAFT_PREFIX):
            raise KBError("a draft is approved or rejected, not kept")
        review = self._review()
        review["notes"][note_id] = {"by": by, "at": self._stamp(), "mtime": note.path.stat().st_mtime_ns}
        self._save_review(review)
        return note.summary()

    def review_appendix(self, note_id: str, by: str) -> dict[str, Any]:
        """Mark every appendix entry a note has now as reviewed."""
        by = self._who(by)
        n = len(self.entries(note_id))
        review = self._review()
        review["appendix"][note_id] = {"entries": n, "by": by, "at": self._stamp()}
        self._save_review(review)
        return self.get(note_id).summary()

    def remove_entry(self, note_id: str, index: int, by: str, reason: str = "") -> dict[str, Any]:
        """
        Remove one appendix entry, keeping a copy in ``review_dir/rejected``.

        Parameters
        ----------
        note_id : str
            The note the entry is appended to.
        index : int
            Entry position, from ``entries``.
        by, reason : str
            Who removed it and why.
        """
        by = self._who(by)
        path = self._appendix_path(note_id)
        text = path.read_text() if path.is_file() else ""
        found = list(ENTRY_RE.finditer(text))
        if not 0 <= index < len(found):
            raise KBError(f"{note_id} has no appendix entry {index}")
        start = found[index].start()
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        removed = text[start:end]
        kept = (text[:start] + text[end:]).rstrip()
        if kept.strip():
            path.write_text(kept + "\n")
        else:
            path.unlink()
        log = self.review_dir / "rejected" / "appendix" / (note_id + ".md")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a") as fh:
            fh.write(f"\n<!-- removed by {by} at {self._stamp()}: {reason.strip()} -->\n{removed.strip()}\n")
        review = self._review()
        seen = review["appendix"].get(note_id)
        if seen and index < seen.get("entries", 0):
            seen["entries"] -= 1
            self._save_review(review)
        return self.get(note_id).summary()

    def _human_prefix(self) -> str:
        if self.human_dir is None:
            raise KBError("no human notes folder is set: set kb_dir to the profile's knowledge folder")
        return ""

    def _agent_note(self, note_id: str) -> Note:
        if not note_id.startswith((AGENT_PREFIX, DRAFT_PREFIX)):
            raise KBError(f"{note_id} is not an agent note or draft")
        return self.get(note_id)

    def _retire(self, note: Note, folder: str, extra: Optional[dict] = None) -> Path:
        """Move an agent note or draft out of the knowledge base into ``review_dir/<folder>``."""
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        target = self.review_dir / folder / f"{note.id}-{stamp}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = {**note.meta, **(extra or {})}
        text = render_note(meta, note.body)
        if note.appendix and folder == "rejected":
            text += "\n" + note.appendix.strip() + "\n"
        target.write_text(text)
        note.path.unlink()
        return target

    def _stamp(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock()))
