import json
from pathlib import Path

from domain.providers import MediaType, ModelRequirement, ProviderCapability, QualityTier
from services.production_metrics import (
    aggregate_production_metrics,
    append_decision,
    append_generation,
    current_generation_id,
    rebuild_provider_performance,
)
from services.provider_registry import ProviderRegistry


def _generation(generation_id: str, *, attempts: int, seconds: float, actual=None):
    return {
        "generation_id": generation_id,
        "shot_index": 0,
        "status": "completed",
        "started_at": "2026-07-31T00:00:00+00:00",
        "completed_at": "2026-07-31T00:00:10+00:00",
        "route": {
            "profile_id": "balanced",
            "provider_id": "seedance",
            "model_id": "seedance-v1",
            "estimated_cost": 2.0,
        },
        "request_attempts": attempts,
        "retry_count": max(0, attempts - 1),
        "queue_seconds": 1.0,
        "generation_seconds": seconds,
        "billing": {
            "status": "provider_reported" if actual is not None else "estimate_only",
            "currency": "CNY",
            "estimated_lower_bound": 2.0,
            "estimated_upper_bound": 2.0 * attempts,
            "actual_cost": actual,
        },
    }


def test_aggregate_tracks_rework_acceptance_cost_and_quality(tmp_path: Path):
    working = tmp_path / "project"
    scene = working / "idea2video" / "scene_0"
    shot = scene / "shots" / "0"
    shot.mkdir(parents=True)
    (shot / "video.mp4").write_bytes(b"video")
    (scene / "storyboard.json").write_text('[{"idx": 0}]', encoding="utf-8")
    (scene / "quality.json").write_text(
        json.dumps({"0": {"ok": True, "score": 0.91, "failed": []}}),
        encoding="utf-8",
    )

    append_generation(scene, _generation("gen-1", attempts=2, seconds=10.0))
    append_decision(
        working,
        "regenerated",
        scene_index=0,
        shot_index=0,
        generation_id="gen-1",
        reason="character_identity",
        dimensions=["identity"],
    )
    append_generation(scene, _generation("gen-2", attempts=1, seconds=8.0, actual=1.8))
    append_decision(
        working,
        "accepted",
        scene_index=0,
        shot_index=0,
        generation_id="gen-2",
        reason="shot_video_gate_approved",
    )
    append_decision(
        working,
        "local_rework_completed",
        reason="character_identity",
        metadata={"savings_estimate": {
            "avoided_shot_count": 3,
            "estimated_generation_seconds_saved": 24.0,
            "estimated_cost_saved_lower_bound": 6.0,
            "estimated_cost_saved_upper_bound": 18.0,
            "full_rerender_cost_estimate": {"currency": "CNY"},
        }},
    )

    result = aggregate_production_metrics(working, session_id="demo")
    summary = result["summary"]

    assert current_generation_id(working, 0, 0) == "gen-2"
    assert summary["accepted_shots"] == 1
    assert summary["first_pass_rate"] == 0.0
    assert summary["quality_pass_rate"] == 1.0
    assert summary["total_reworks"] == 1
    assert summary["request_attempts"] == 3
    assert summary["cost"]["estimated_upper_bound"] == 6.0
    assert summary["cost"]["actual_total"] == 1.8
    assert summary["cost"]["actual_coverage_rate"] == 0.5
    assert summary["local_rework_savings"] == {
        "completed_batches": 1,
        "avoided_shot_count": 3,
        "estimated_generation_seconds_saved": 24.0,
        "estimated_cost_saved_lower_bound": 6.0,
        "estimated_cost_saved_upper_bound": 18.0,
        "currency": "CNY",
        "cost_estimate_coverage_count": 1,
    }
    assert result["models"][0]["acceptance_rate"] == 0.5


class _SessionIndex:
    def __init__(self, working: Path):
        self._working = working

    def list_sessions(self):
        return [{"session_id": "demo"}]

    def working_dir(self, _session_id):
        return self._working


def test_rebuild_performance_and_router_use_only_eligible_history(tmp_path: Path):
    working = tmp_path / "project"
    scene = working / "idea2video" / "scene_0"
    (scene / "shots" / "0").mkdir(parents=True)
    (scene / "storyboard.json").write_text('[{"idx": 0}]', encoding="utf-8")

    for index in range(5):
        generation_id = f"gen-{index}"
        append_generation(scene, _generation(generation_id, attempts=1, seconds=5.0))
        append_decision(
            working,
            "accepted",
            scene_index=0,
            shot_index=0,
            generation_id=generation_id,
            reason="test",
        )
    performance = rebuild_provider_performance(tmp_path, _SessionIndex(working))
    assert performance["profiles"][0]["routing_eligible"] is True

    registry = ProviderRegistry([
        ProviderCapability(
            profile_id="balanced",
            provider_id="seedance",
            model_id="seedance-v1",
            media_type=MediaType.VIDEO,
            image_to_video=True,
            estimated_cost=2.0,
            quality_tier=QualityTier.BALANCED,
        ),
        ProviderCapability(
            profile_id="other",
            provider_id="veo",
            model_id="veo-v1",
            media_type=MediaType.VIDEO,
            image_to_video=True,
            estimated_cost=1.0,
            quality_tier=QualityTier.BALANCED,
        ),
    ], workspace_root=tmp_path)
    decision = registry.route(
        ModelRequirement(media_type=MediaType.VIDEO, image_to_video=True),
        quality_tier="balanced",
    )

    assert decision.capability.profile_id == "balanced"
    assert decision.historical_performance["sample_count"] == 5
    assert decision.reason.endswith("with_historical_performance")


def test_legacy_video_is_visible_but_has_no_invented_cost(tmp_path: Path):
    working = tmp_path / "project"
    shot = working / "idea2video" / "scene_0" / "shots" / "0"
    shot.mkdir(parents=True)
    (shot / "video.mp4").write_bytes(b"legacy-video")

    result = aggregate_production_metrics(working)
    generation = result["shots"][0]["current_generation"]

    assert result["summary"]["generated_shots"] == 1
    assert generation["legacy_imported"] is True
    assert generation["request_attempts"] == 0
    assert result["summary"]["cost"]["estimated_upper_bound"] == 0.0
    assert result["summary"]["cost"]["actual_coverage_rate"] == 0.0
