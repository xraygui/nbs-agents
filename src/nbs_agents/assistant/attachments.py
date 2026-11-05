"""
Figures and other files that agent messages refer to as ``att:ID``.

The store is a plain folder: ``{id}{ext}`` holds the bytes and ``{id}.json``
the metadata. IDs are content hashes, so attaching the same file twice gives
the same ID. The agent log only holds IDs, so the store can move (for example
into Tiled) without changing messages.
"""

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional, Union

from nbs_agents.paths import paths

# Types the web UI shows inline; anything else is refused
MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
EXTENSIONS = {mime: ext for ext, mime in reversed(list(MIME_TYPES.items()))}
ID_RE = re.compile(r"^[0-9a-f]{16}$")
MAX_BYTES = 20 * 1024 * 1024


def default_root() -> Path:
    """The store folder: ``$NBS_ATTACHMENTS_DIR`` or ``paths().attachments``."""
    return Path(os.environ.get("NBS_ATTACHMENTS_DIR") or paths().attachments).expanduser()


class AttachmentStore:
    """
    Folder of attachments addressed by ID.

    Parameters
    ----------
    root : str or Path, optional
        Folder to use; ``default_root()`` if omitted. Created on first write.
    """

    def __init__(self, root: Optional[Union[str, Path]] = None) -> None:
        self.root = Path(root).expanduser() if root else default_root()

    def put(
        self,
        data: Union[bytes, str, Path],
        mime: Optional[str] = None,
        alt: str = "",
        **meta: Any,
    ) -> dict[str, Any]:
        """
        Store a file and return its metadata.

        Parameters
        ----------
        data : bytes, str or Path
            File contents, or a path to read.
        mime : str, optional
            Media type; taken from the path's extension if omitted.
        alt : str, optional
            Short description, used as alt text and in plain views.
        **meta
            Extra fields such as ``run_uid`` or ``agent_uid``.

        Returns
        -------
        dict
            ``id``, ``mime``, ``alt``, ``size``, ``created`` and ``meta`` fields.

        Raises
        ------
        ValueError
            For an unsupported type or a file over ``MAX_BYTES``.
        """
        if not isinstance(data, bytes):
            path = Path(data)
            mime = mime or MIME_TYPES.get(path.suffix.lower())
            data = path.read_bytes()
        if mime not in EXTENSIONS:
            raise ValueError(f"unsupported attachment type {mime!r}; use one of {sorted(EXTENSIONS)}")
        if len(data) > MAX_BYTES:
            raise ValueError(f"attachment is {len(data)} bytes; the limit is {MAX_BYTES}")
        att_id = hashlib.sha256(data).hexdigest()[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        body = self.root / f"{att_id}{EXTENSIONS[mime]}"
        if not body.exists():
            tmp = body.with_suffix(body.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(body)
        info = {"id": att_id, "mime": mime, "alt": alt, "size": len(data), "created": time.time(), **meta}
        (self.root / f"{att_id}.json").write_text(json.dumps(info, default=str))
        return info

    def info(self, att_id: str) -> Optional[dict[str, Any]]:
        """
        Metadata for an attachment.

        Parameters
        ----------
        att_id : str
            Attachment ID.

        Returns
        -------
        dict or None
            Metadata, or ``None`` for an unknown or malformed ID.
        """
        if not ID_RE.match(att_id or ""):
            return None
        try:
            return json.loads((self.root / f"{att_id}.json").read_text())
        except (OSError, ValueError):
            return None

    def path(self, att_id: str) -> Optional[tuple[Path, str]]:
        """
        Where an attachment's bytes are.

        Parameters
        ----------
        att_id : str
            Attachment ID.

        Returns
        -------
        tuple of (Path, str) or None
            File path and media type, or ``None`` if missing.
        """
        info = self.info(att_id)
        if not info or info.get("mime") not in EXTENSIONS:
            return None
        body = self.root / f"{att_id}{EXTENSIONS[info['mime']]}"
        return (body, info["mime"]) if body.is_file() else None
