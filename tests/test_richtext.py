"""Tests for rich message text."""

from nbs_agents.richtext import attachment_ids, plain_text


def test_plain_text_replaces_figures_and_keeps_markdown():
    text = "Edge at $E_0 = 284.8$ eV.\n\n![C K-edge, S1](att:0123456789abcdef) and ![](att:fedcba9876543210 \"t\")"
    assert plain_text(text) == "Edge at $E_0 = 284.8$ eV.\n\n[figure: C K-edge, S1] and [figure]"
    assert attachment_ids(text + " ![again](att:0123456789abcdef)") == ["0123456789abcdef", "fedcba9876543210"]
    assert plain_text("![remote](https://example.com/x.png)") == "![remote](https://example.com/x.png)"
