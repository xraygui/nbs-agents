"""
Rich text in agent messages.

Agents write message text as Markdown with ``$...$`` / ``$$...$$`` math, kept
readable as plain text. A figure is referenced by an image link to an
attachment, ``![alt](att:ID)``; the bytes live in an attachment store, never
in the message. The web UI renders all of it; plain views (log files,
terminals, simple Qt labels) use ``plain_text`` to swap figures for
``[figure: alt]`` and leave the rest as written.
"""

import re

# ![alt](att:ID) or ![alt](att:ID "title")
ATTACHMENT_RE = re.compile(r"!\[([^\]]*)\]\(att:([0-9A-Za-z_-]+)(?:\s+\"[^\"]*\")?\)")


def attachment_ids(text: str) -> list[str]:
    """
    Attachment IDs referenced by a message, in order, without repeats.

    Parameters
    ----------
    text : str
        Message text.

    Returns
    -------
    list of str
        IDs from ``![alt](att:ID)`` links.
    """
    seen: list[str] = []
    for match in ATTACHMENT_RE.finditer(text or ""):
        if match.group(2) not in seen:
            seen.append(match.group(2))
    return seen


def plain_text(text: str) -> str:
    """
    A message as plain text: figures become ``[figure: alt]``.

    Markdown and math are left as written, since both read well raw.

    Parameters
    ----------
    text : str
        Message text.

    Returns
    -------
    str
        Text for plain views.
    """

    def figure(match):
        alt = match.group(1).strip()
        return f"[figure: {alt}]" if alt else "[figure]"

    return ATTACHMENT_RE.sub(figure, text or "")
