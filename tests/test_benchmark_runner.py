import json

from scripts.run_benchmark import summarize_results, validate_dataset


def test_repository_benchmark_is_valid():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "sceneforge_benchmark"
    report = validate_dataset(root)
    assert report["ok"], report["errors"]
    assert report["summary"]["stories"] == 35
    assert report["summary"]["scenes"] == 104
    assert report["summary"]["shots"] == 437
    assert set(report["summary"]["types"]) == {"Type A", "Type B", "Type C"}


def test_result_summary_groups_models_and_calculates_quality(tmp_path):
    path = tmp_path / "results.jsonl"
    records = [
        {"case": "a", "provider": "demo", "model": "v1", "status": "succeeded", "identity": 0.8, "temporal": 0.6, "seconds": 20, "retries": 1, "cost": 0.2},
        {"case": "b", "provider": "demo", "model": "v1", "status": "failed", "seconds": 10, "retries": 2, "cost": 0.1},
    ]
    path.write_text("\n".join(json.dumps(item) for item in records), encoding="utf-8")

    report = summarize_results(path)

    assert report["ok"]
    model = report["models"]["demo/v1"]
    assert model["success_rate"] == 0.5
    assert model["identity"] == 0.8
    assert model["total_cost"] == 0.3


def test_invalid_result_metric_is_rejected(tmp_path):
    path = tmp_path / "results.jsonl"
    path.write_text(json.dumps({
        "case": "a", "provider": "demo", "model": "v1", "status": "succeeded", "identity": 1.5,
    }), encoding="utf-8")
    assert not summarize_results(path)["ok"]
