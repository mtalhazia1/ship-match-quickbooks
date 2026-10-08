import pytest

from apps.core.evaluation import run


@pytest.mark.django_db
def test_end_to_end_scores_on_synthetic_set(dataset, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # eval_reports/ goes to the temp dir
    r = run(dataset, org_slug="eval-test")
    assert r["classification"]["correct"] == r["classification"]["total"]
    assert r["field_accuracy"] >= 0.98
    assert r["grouping"]["pair_precision"] == 1.0
    assert r["grouping"]["pair_recall"] == 1.0
    assert r["error_recall"] == 1.0, r["error_detection"]
    assert r["false_alarms"] == []
    assert len(r["needs_ocr"]) == 1
