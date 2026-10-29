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


def test_cli_attach_prints_markdown_link(tmp_path):
    from nbs_agents.cli.config import CliConfig
    from nbs_agents.cli.core import Beamline, CliError
    from nbs_agents.cli.main import build_parser, run_command

    bl = Beamline(CliConfig(attachments_dir=str(tmp_path / "store")))
    src = tmp_path / "fit.png"
    src.write_bytes(PNG)
    args = build_parser().parse_args(["attach", str(src), "--alt", "S1 [fit]\nresidual", "--run", "r1"])
    out = run_command(bl, args)
    assert out["markdown"] == f"![S1 (fit) residual](att:{out['id']})"
    assert out["run_uid"] == "r1"
    assert AttachmentStore(tmp_path / "store").path(out["id"]) is not None
    with pytest.raises(CliError):
        bl.attach(str(tmp_path / "missing.png"), alt="x")
    (tmp_path / "notes.txt").write_text("x")
    with pytest.raises(CliError):
        bl.attach(str(tmp_path / "notes.txt"), alt="x")


def test_cli_attach_allowed_for_the_assistant(tmp_path):
    from nbs_agents.cli.config import CliConfig
    from nbs_agents.cli.core import Beamline

    src = tmp_path / "p.png"
    src.write_bytes(PNG)
    bl = Beamline(CliConfig(role="assistant", attachments_dir=str(tmp_path)))
    assert bl.attach(str(src), alt="p")["markdown"].startswith("![p](att:")


def test_run_attach_plots_into_the_store(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nbs_agents.cli.config import CliConfig
    from nbs_agents.cli.core import Beamline

    bl = Beamline(CliConfig(attachments_dir=str(tmp_path)))

    def fake_plot(run, path):
        open(path, "wb").write(PNG)
        return path

    monkeypatch.setattr(bl, "plot_run", fake_plot)
    run = SimpleNamespace(metadata={"start": {"uid": "r9", "scan_id": 29, "plan_name": "c_xas", "sample_name": "s1"}})
    out = bl.attach_run_plot(run)
    assert out["alt"] == "scan 29 c_xas s1" and out["run_uid"] == "r9"
    assert out["markdown"] == f"![scan 29 c_xas s1](att:{out['id']})"
