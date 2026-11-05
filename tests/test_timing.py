import json
from datetime import datetime, timezone
from pathlib import Path

from nbs_agents.assistant.timing import analyze, format_report


def line(**msg):
    return json.dumps(msg)


def assistant(ts, *tools):
    content = [{"type": "tool_use", "id": i, "name": "Bash", "input": {"command": c}} for i, c in tools]
    return line(type="assistant", timestamp=ts, message={"content": content or [{"type": "text", "text": "x"}]})


def result(ts, *ids):
    return line(type="user", timestamp=ts, message={"content": [{"type": "tool_result", "tool_use_id": i} for i in ids]})


def test_splits_startup_model_and_tools():
    lines = [
        line(type="system", subtype="init"),
        assistant("2026-11-03T18:00:05Z", ("a", "nbs-cli state")),
        result("2026-11-03T18:00:09Z", "a"),
        line(type="system", subtype="api_retry"),
        assistant("2026-11-03T18:00:19Z", ("b", "nbs-cli runs -n 20"), ("c", "nbs-cli beam")),
        result("2026-11-03T18:00:20Z", "c"),
        result("2026-11-03T18:00:21Z", "b"),
        assistant("2026-11-03T18:00:30Z"),
        "not json",
        line(type="result", duration_ms=31000, duration_api_ms=20000, num_turns=4, total_cost_usd=0.1),
    ]
    out = analyze(lines, launched=datetime(2026, 11, 3, 18, 0, 0, tzinfo=timezone.utc))
    assert out["startup_s"] == 5
    assert out["tools_s"] == 4 + 2
    assert out["model_s"] == 10 + 9
    assert [(t["label"], t["seconds"]) for t in out["tools"]] == [
        ("nbs-cli state", 4), ("nbs-cli beam", 1), ("nbs-cli runs -n 20", 2)
    ]
    assert out["api_retries"] == 1
    assert (out["total_s"], out["api_s"], out["turns"]) == (31, 20, 4)
    report = format_report(Path("claude-x-20261103-180000.log"), out)
    assert report.splitlines()[-3].endswith("nbs-cli state")


def test_empty_log():
    out = analyze([])
    assert out["model_s"] == 0 and out["tools"] == [] and out["startup_s"] is None
