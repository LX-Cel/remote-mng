"""Host diagnostics cannot turn arbitrary update failures into CI success."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def smoke_module(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location("source_smoke_fixture", scripts / "smoke_source_update.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def blocked_record(tmp_path):
    return {"state": "blocked", "events": [{"state": "preparing"}, {"state": "blocked"}],
            "source_prepared": {"work": str(tmp_path / "prepared")},
            "error": {"code": "update_blocked", "details": {"installation_changed": False,
                      "blockers": [{"code": "independent_process_unavailable", "winerror": 5}]}}}


def test_process_block_requires_exact_pre_mutation_failure(smoke_module, blocked_record, tmp_path):
    assert smoke_module.assert_process_block(blocked_record, tmp_path)["winerror"] == 5


@pytest.mark.parametrize("changed", ["failed", "other_blocker", "phase", "gate", "activation", "recovery"])
def test_process_block_rejects_other_failures_or_mutation(smoke_module, blocked_record, tmp_path, changed):
    if changed == "failed":
        blocked_record["state"] = "failed"
    elif changed == "other_blocker":
        blocked_record["error"]["details"]["blockers"][0]["code"] = "skill_conflict"
    elif changed == "phase":
        blocked_record["events"].insert(0, {"state": "quiescing"})
    elif changed == "gate":
        (tmp_path / "runtime").mkdir()
        (tmp_path / "runtime/update-switch.json").write_text("{}")
    elif changed == "activation":
        (tmp_path / "prepared").mkdir()
        (tmp_path / "prepared/source-activation.json").write_text("{}")
    else:
        blocked_record["recovery"] = {"state": "restored"}
    with pytest.raises(AssertionError):
        smoke_module.assert_process_block(blocked_record, tmp_path)


def test_default_ci_requires_full_success_and_clean_cleanup(smoke_module):
    assert smoke_module.accepted_report({"state": "passed"})
    assert not smoke_module.accepted_report({"state": "passed_blocked_safely"})
    assert smoke_module.accepted_report({"state": "passed_blocked_safely"}, True)
    assert not smoke_module.accepted_report({"state": "passed"}, True)
    assert not smoke_module.accepted_report({"state": "failed"}, True)
    assert not smoke_module.accepted_report({"state": "passed_blocked_safely", "cleanup": {"error": "not cleaned"}}, True)
