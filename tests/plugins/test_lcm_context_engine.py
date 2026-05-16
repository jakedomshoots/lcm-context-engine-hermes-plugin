import json
import os

from plugins.context_engine import load_context_engine
from plugins.context_engine.lcm.engine import LCMContextEngine


def test_lcm_engine_discovery_and_name():
    engine = load_context_engine("lcm")
    assert engine is not None
    assert isinstance(engine, LCMContextEngine)
    assert engine.name == "lcm"


def test_lcm_ingest_and_grep_roundtrip():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("test-session")

    messages = [
        {"role": "user", "content": "alpha bedtime fox"},
        {"role": "assistant", "content": "gentle moonlight story"},
        {"role": "user", "content": "beta bedtime bunny"},
    ]
    inserted = engine.ingest_messages("test-session", messages)
    assert inserted == 3

    inserted_again = engine.ingest_messages("test-session", messages)
    assert inserted_again == 0

    raw = engine.handle_tool_call("lcm_grep", {"query": "bedtime", "limit": 5})
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["count"] >= 2
    assert payload["search_mode"] in {"fts5", "like"}
    snippets = "\n".join(hit["snippet"] for hit in payload["hits"])
    assert "bedtime" in snippets

    first_id = payload["hits"][0]["id"]
    describe_raw = engine.handle_tool_call("lcm_describe", {"id": first_id})
    described = json.loads(describe_raw)
    assert described["ok"] is True
    assert described["message"]["id"] == first_id


def test_lcm_tool_schemas_include_grep_and_describe():
    engine = LCMContextEngine()
    names = {s["name"] for s in engine.get_tool_schemas()}
    assert "lcm_grep" in names
    assert "lcm_describe" in names
    assert "lcm_expand" in names
    assert "lcm_stats" in names
    assert "lcm_health" in names


def test_lcm_tool_call_ingests_live_messages_snapshot():
    engine = LCMContextEngine()
    engine.on_session_start("live-snapshot")

    _ = engine.handle_tool_call(
        "lcm_grep",
        {"query": "orion"},
        messages=[{"role": "user", "content": "orion bedtime prompt"}],
    )
    described = json.loads(engine.handle_tool_call("lcm_describe", {"query": "orion"}))
    assert described["ok"] is True
    assert "orion" in described["message"]["content"]


def test_lcm_expand_returns_context_window():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("expand-session")
    msgs = [
        {"role": "user", "content": "star one"},
        {"role": "assistant", "content": "star two"},
        {"role": "user", "content": "anchor planet"},
        {"role": "assistant", "content": "star four"},
        {"role": "user", "content": "star five"},
    ]
    engine.ingest_messages("expand-session", msgs)

    grep = json.loads(engine.handle_tool_call("lcm_grep", {"query": "anchor"}))
    anchor_id = grep["hits"][0]["id"]
    expanded = json.loads(engine.handle_tool_call("lcm_expand", {"id": anchor_id, "before": 1, "after": 1}))

    assert expanded["ok"] is True
    assert expanded["anchor_id"] == anchor_id
    assert expanded["count"] >= 3
    assert any(item["is_anchor"] for item in expanded["window"])


def test_lcm_invalid_id_errors_cleanly():
    engine = LCMContextEngine()
    engine.on_session_start("invalid-id")

    bad_describe = json.loads(engine.handle_tool_call("lcm_describe", {"id": "abc"}))
    assert "error" in bad_describe

    bad_expand = json.loads(engine.handle_tool_call("lcm_expand", {"id": "abc"}))
    assert "error" in bad_expand


def test_lcm_stats_empty_session_reports_zero():
    engine = LCMContextEngine()
    engine.on_session_start("empty-session")
    stats = json.loads(engine.handle_tool_call("lcm_stats", {}))
    assert stats["ok"] is True
    assert stats["messages"] == 0
    assert stats["summaries"] == 0
    assert stats["max_summary_depth"] == 0


def test_lcm_env_config_overrides(tmp_path):
    db_path = tmp_path / "custom-lcm.db"
    old = {
        "HERMES_LCM_DB_PATH": os.environ.get("HERMES_LCM_DB_PATH"),
        "HERMES_LCM_THRESHOLD_PERCENT": os.environ.get("HERMES_LCM_THRESHOLD_PERCENT"),
        "HERMES_LCM_HEAD_KEEP": os.environ.get("HERMES_LCM_HEAD_KEEP"),
        "HERMES_LCM_TAIL_KEEP": os.environ.get("HERMES_LCM_TAIL_KEEP"),
    }
    try:
        os.environ["HERMES_LCM_DB_PATH"] = str(db_path)
        os.environ["HERMES_LCM_THRESHOLD_PERCENT"] = "0.8"
        os.environ["HERMES_LCM_HEAD_KEEP"] = "3"
        os.environ["HERMES_LCM_TAIL_KEEP"] = "10"

        engine = LCMContextEngine(context_length=1000)
        assert str(engine._db_path) == str(db_path)
        assert engine.threshold_tokens == 800
        assert engine._head_keep == 3
        assert engine._tail_keep == 10
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_lcm_summary_node_written_on_compress():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("summary-session")

    msgs = [{"role": "user", "content": f"line {i}"} for i in range(50)]
    out = engine.compress(msgs)
    assert len(out) < len(msgs)

    stats = json.loads(engine.handle_tool_call("lcm_stats", {}))
    assert stats["ok"] is True
    assert stats["session_id"] == "summary-session"
    assert stats["messages"] >= 50
    assert stats["summaries"] >= 1
    assert stats["latest_summary_id"] is not None
    assert stats["latest_summary_depth"] == 0

    status = engine.get_status()
    assert status["session_id"] == "summary-session"
    assert status["messages"] >= 50
    assert status["summaries"] >= 1


def test_lcm_compress_preserves_tail_and_inserts_marker():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("compress-session")

    msgs = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    out = engine.compress(msgs, focus_topic="sleep")

    assert len(out) < len(msgs)
    marker = "\n".join(str(m.get("content", "")) for m in out if m.get("role") == "system")
    assert "Summary node:" in marker
    assert any("[LCM]" in (m.get("content") or "") for m in out)
    assert out[-1]["content"] == "m39"


def test_lcm_summary_nodes_build_parent_chain_and_depth_increments():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("dag-session")

    msgs_a = [{"role": "user", "content": f"a{i}"} for i in range(50)]
    msgs_b = [{"role": "assistant", "content": f"b{i}"} for i in range(50)]

    _ = engine.compress(msgs_a)
    _ = engine.compress(msgs_b)

    stats = json.loads(engine.handle_tool_call("lcm_stats", {}))
    assert stats["summaries"] >= 2
    assert stats["max_summary_depth"] >= 1
    assert stats["latest_summary_parent_id"] is not None


def test_lcm_status_includes_fts_and_depth_fields():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("status-session")
    _ = engine.compress([{"role": "user", "content": f"s{i}"} for i in range(40)])

    status = engine.get_status()
    assert "fts_enabled" in status
    assert "max_summary_depth" in status


def test_lcm_health_reports_operational_snapshot():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("health-session")
    _ = engine.ingest_messages("health-session", [{"role": "user", "content": "hello moon"}])

    health = json.loads(engine.handle_tool_call("lcm_health", {}))
    assert health["ok"] is True
    assert health["status"] == "healthy"
    assert health["db_size_bytes"] >= 0
    assert "journal_mode" in health


def test_lcm_parameter_validation_errors_cleanly():
    engine = LCMContextEngine(context_length=1000, threshold_percent=0.5)
    engine.on_session_start("param-errors")

    bad_limit = json.loads(engine.handle_tool_call("lcm_grep", {"query": "x", "limit": "bad"}))
    assert bad_limit["error"] == "limit must be an integer"

    bad_before = json.loads(engine.handle_tool_call("lcm_expand", {"query": "x", "before": "bad"}))
    assert bad_before["error"] == "before must be an integer"
