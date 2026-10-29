"""Tests for rich message text and the attachment store."""

import pytest

from nbs_agents.attachments import AttachmentStore
from nbs_agents.richtext import attachment_ids, plain_text

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_plain_text_replaces_figures_and_keeps_markdown():
    text = "Edge at $E_0 = 284.8$ eV.\n\n![C K-edge, S1](att:0123456789abcdef) and ![](att:fedcba9876543210 \"t\")"
    assert plain_text(text) == "Edge at $E_0 = 284.8$ eV.\n\n[figure: C K-edge, S1] and [figure]"
    assert attachment_ids(text + " ![again](att:0123456789abcdef)") == ["0123456789abcdef", "fedcba9876543210"]
    assert plain_text("![remote](https://example.com/x.png)") == "![remote](https://example.com/x.png)"


def test_store_round_trip(tmp_path):
    store = AttachmentStore(tmp_path)
    src = tmp_path / "plot.png"
    src.write_bytes(PNG)
    info = store.put(src, alt="C K-edge", run_uid="r1")
    assert info["mime"] == "image/png" and info["alt"] == "C K-edge" and info["run_uid"] == "r1"
    assert store.put(PNG, "image/png")["id"] == info["id"]
    path, mime = store.path(info["id"])
    assert path.read_bytes() == PNG and mime == "image/png"
    assert store.info("../etc/passwd") is None
    assert store.path("0" * 16) is None
    with pytest.raises(ValueError):
        store.put(b"<html>", "text/html")
