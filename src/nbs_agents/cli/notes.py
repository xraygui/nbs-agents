"""
Markdown files with YAML front matter, and full-text search over them.

Shared by the knowledge base (``kb.py``) and the reference scan library
(``refs.py``): both keep one Markdown file per item, with an ID taken from its
path, and rank items by query words with SQLite FTS5.
"""

import re
import sqlite3
from typing import Any

import yaml

FRONT_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
WORD_RE = re.compile(r"[a-z0-9_]+")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*(/[A-Za-z0-9][A-Za-z0-9_.-]*)*$")
# Query words that say nothing about the subject; dropped so that a natural
# question ("why is i0 noisy") is not judged on "why" and "is"
STOP_WORDS = frozenset(
    "a an and are as at be by can do does for from how i if in is it its of on or "
    "the there this to was what when where which who why will with".split()
)
# Index columns: what an item is about (weighted), and its text
HEAD_WEIGHT, BODY_WEIGHT = 3.0, 1.0
SNIPPET_TOKENS = 24


class NoteError(Exception):
    """A request on note files that cannot be done (shown as the CLI error)."""


def fts_term(word: str) -> str:
    """
    One query word as an FTS5 query: quoted, so no word is read as FTS5 syntax,
    and a prefix match for words of four letters or more (``reflect`` finds
    ``reflectivity``). The porter tokenizer matches plurals and endings.
    """
    return f'"{word}"*' if len(word) >= 4 else f'"{word}"'


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

    Raises
    ------
    NoteError
        If the front matter is not a YAML mapping.
    """
    match = FRONT_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise NoteError(f"bad front matter: {exc}") from exc
    if not isinstance(meta, dict):
        raise NoteError("front matter must be a mapping")
    return meta, match.group(2)


def render_note(meta: dict[str, Any], body: str) -> str:
    """Front matter block plus body, as written to a file."""
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None).strip()
    return f"---\n{front}\n---\n{body.rstrip()}\n"


def slugify(text: str) -> str:
    """An ID segment from a title: lowercase words joined by ``-``."""
    slug = "-".join(WORD_RE.findall(text.lower()))[:60].strip("-")
    return slug or "note"


def query_terms(query: str) -> list[str]:
    """The words of a query to search for: common words dropped, unless that leaves none."""
    words = list(dict.fromkeys(WORD_RE.findall(query.lower())))
    return [w for w in words if w not in STOP_WORDS] or words


def fts_rank(docs: list[tuple[str, str]], terms: list[str]) -> list[tuple[int, float, str, list[str]]]:
    """
    Rank documents by query terms with SQLite FTS5.

    Documents containing more of the terms come first, then BM25 relevance
    (rare words count more than common ones); the head counts three times the
    body. The index is built in memory on every call, so there is no index
    file to keep up to date.

    Parameters
    ----------
    docs : list of (str, str)
        Each document's head (what it is about) and body.
    terms : list of str
        Words from ``query_terms``; must not be empty.

    Returns
    -------
    list of (int, float, str, list of str)
        For each matching document, best first: its index in ``docs``, its
        score (higher is better), a body snippet, and the terms it lacks.
    """
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE kb USING fts5(head, body, tokenize='porter unicode61')")
        db.executemany("INSERT INTO kb (rowid, head, body) VALUES (?, ?, ?)",
                       [(i, head, body) for i, (head, body) in enumerate(docs)])
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
        missing = [t for t in terms if t not in found.get(rowid, ())]
        hits.append((len(missing), rank, rowid, " ".join(snippet.split()), missing))
    hits.sort(key=lambda h: h[:2])
    return [(rowid, round(-rank, 3), snippet, missing) for _, rank, rowid, snippet, missing in hits]
