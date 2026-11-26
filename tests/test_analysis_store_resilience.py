import json
from pathlib import Path

from core.analysis_store import AnalysisStore


def test_analysis_store_recovers_from_backup_when_primary_corrupt(tmp_path):
    store_path = tmp_path / "news_analysis.json"
    backup_path = Path(str(store_path) + ".bak")

    store_path.write_text('{"broken": ', encoding='utf-8')

    payload = {
        "_meta": {"version": 2},
        "by_url": {
            "https://example.com/a": {
                "timestamp": "2026-02-08T00:00:00+00:00",
                "model": "test",
                "analysis": {"k": "v"},
                "analysis_type": "deep",
                "url": "https://example.com/a",
            }
        },
        "by_article": {},
        "narratives": {},
    }
    backup_path.write_text(json.dumps(payload), encoding='utf-8')

    store = AnalysisStore(str(store_path))
    rec = store.get_analysis("https://example.com/a")

    assert rec is not None
    assert rec["analysis"]["k"] == "v"

    # Primary should be repaired to valid JSON after recovery.
    repaired = json.loads(store_path.read_text(encoding='utf-8'))
    assert repaired["by_url"]["https://example.com/a"]["analysis"]["k"] == "v"

    quarantine = list(tmp_path.glob("news_analysis.json.corrupt.*.json"))
    assert quarantine


def test_analysis_store_isolation_from_external_mutation(tmp_path):
    store_path = tmp_path / "news_analysis.json"
    store = AnalysisStore(str(store_path))

    source = {"nested": {"score": 1}}
    store.save_analysis("https://example.com/b", source, "test-model")

    source["nested"]["score"] = 99

    rec1 = store.get_analysis("https://example.com/b")
    assert rec1["analysis"]["nested"]["score"] == 1

    rec1["analysis"]["nested"]["score"] = 55
    rec2 = store.get_analysis("https://example.com/b")
    assert rec2["analysis"]["nested"]["score"] == 1

    assert Path(str(store_path) + ".bak").exists()


def test_analysis_store_recovers_when_primary_has_trailing_garbage(tmp_path):
    store_path = tmp_path / "news_analysis.json"
    payload = {
        "_meta": {"version": 2},
        "by_url": {
            "https://example.com/c": {
                "timestamp": "2026-02-08T00:00:00+00:00",
                "model": "test",
                "analysis": {"k": "v2"},
                "analysis_type": "deep",
                "url": "https://example.com/c",
            }
        },
        "by_article": {},
        "narratives": {},
    }
    raw = json.dumps(payload) + "\n{broken trailing"
    store_path.write_text(raw, encoding="utf-8")

    store = AnalysisStore(str(store_path))
    rec = store.get_analysis("https://example.com/c")
    assert rec is not None
    assert rec["analysis"]["k"] == "v2"

    repaired = json.loads(store_path.read_text(encoding="utf-8"))
    assert repaired["by_url"]["https://example.com/c"]["analysis"]["k"] == "v2"
