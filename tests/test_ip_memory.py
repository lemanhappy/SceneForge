import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

from characters.models import (
    CharacterAsset,
    OutfitVersion,
    ReusableAsset,
)
from agent_runtime.session_index import SessionIndex
from quality import (
    asset_change_impact,
    build_continuity_ledger,
    continuity_handoff,
    evaluate_asset_invalidations,
    load_continuity_ledger,
    regeneration_impact,
    save_continuity_ledger,
)
from pipelines.script2video_pipeline import Script2VideoPipeline
from server.artifacts_reader import build_manifest
from server.production_api import ProductionAPI
from services.production_service import ProductionService
from services.workflow_engine import WorkflowEngine


def _shot(idx, *, text="", visible=()):
    return SimpleNamespace(
        idx=idx,
        cam_idx=0,
        ff_desc=text,
        lf_desc=text,
        visual_desc=text,
        motion_desc="",
        director_desc="",
        audio_desc="",
        screen_text="",
        variation_reason="",
        ff_vis_char_idxs=list(visible),
        lf_vis_char_idxs=list(visible),
        beats=[],
    )


def _contracts():
    return {
        "version": 1,
        "shots": {
            "0": {
                "shot_idx": 0,
                "camera_idx": 0,
                "continuity_mode": "root",
                "continuity_reference_shot_idx": None,
            },
            "1": {
                "shot_idx": 1,
                "camera_idx": 1,
                "previous_shot_idx": 0,
                "continuity_mode": "cross_camera",
                "continuity_reference_shot_idx": 0,
            },
            "2": {
                "shot_idx": 2,
                "camera_idx": 1,
                "previous_shot_idx": 1,
                "continuity_mode": "same_camera",
                "continuity_reference_shot_idx": 1,
            },
        },
    }


def _preflight():
    return {
        "shots": {
            str(index): {
                "status": "passed",
                "initial_state": {"characters": [], "props": []},
                "final_state": {"characters": [], "props": []},
                "transitions": ([{"kind": "pickup"}] if index == 1 else []),
                "issues": [],
            }
            for index in range(3)
        }
    }


def test_asset_bibles_extend_legacy_models_without_breaking_defaults():
    character = CharacterAsset(
        asset_id="lead",
        display_name="Lead",
        identity_profile={"facial_features": "oval face"},
        bible={
            "personality_traits": ["restrained"],
            "voice": {"vocal_quality": "low and calm"},
        },
        outfit_versions=[OutfitVersion(
            outfit_version_id="coat",
            name="Blue coat",
            description="dark blue wool coat",
            is_default=True,
        )],
    )
    scene = ReusableAsset(
        asset_id="station",
        asset_type="scene",
        display_name="Old station",
        scene_bible={
            "spatial_layout": "entrance left, ticket window right",
            "fixed_objects": ["central bench"],
        },
    )
    legacy_prop = ReusableAsset(
        asset_id="box",
        asset_type="prop",
        display_name="Lunchbox",
    )

    assert "oval face" in character.visual_constraint()
    assert "dark blue wool coat" in character.visual_constraint()
    assert "low and calm" in character.bible_constraint()
    assert "entrance left" in scene.prompt_constraint()
    assert legacy_prop.prop_bible is not None
    assert legacy_prop.scene_bible is None


def test_continuity_ledger_tracks_asset_usage_and_state_dependencies():
    character = CharacterAsset(
        asset_id="lead",
        display_name="Lin",
        bible={"continuity_notes": "scar remains on left eyebrow"},
    )
    registry = SimpleNamespace(get=lambda asset_id: character if asset_id == "lead" else None)
    scene = ReusableAsset(
        asset_id="station",
        asset_type="scene",
        display_name="Old station",
        scene_bible={"spatial_layout": "entrance left, ticket window right"},
    )
    prop = ReusableAsset(
        asset_id="lunchbox",
        asset_type="prop",
        display_name="blue lunchbox",
        prop_bible={"ownership": "Lin", "initial_location": "central bench"},
    )
    characters = [SimpleNamespace(idx=0, identifier_in_scene="Lin")]
    shots = [
        _shot(0, text="Lin enters the old station.", visible=[0]),
        _shot(1, text="Lin picks up the blue lunchbox.", visible=[0]),
        _shot(2, text="Close-up of Lin holding it.", visible=[0]),
    ]

    ledger = build_continuity_ledger(
        shots,
        contracts=_contracts(),
        preflight_report=_preflight(),
        characters=characters,
        character_bindings={"Lin": "lead"},
        character_assets=registry,
        reusable_assets=[scene, prop],
    )

    assert ledger["summary"] == {
        "shot_count": 3,
        "tracked_character_count": 1,
        "tracked_scene_count": 1,
        "tracked_prop_count": 1,
        "state_transition_count": 1,
        "repair_suggestion_count": 0,
        "inherits_previous_state": False,
    }
    assert ledger["shots"]["1"]["prop_asset_ids"] == ["lunchbox"]
    assert ledger["shots"]["2"]["prop_asset_ids"] == []
    assert ledger["asset_usage"]["station"] == [0, 1, 2]
    assert ledger["asset_usage"]["lunchbox"] == [1]
    assert ledger["asset_bibles"]["characters"]["lead"]["bible"]["continuity_notes"]

    visual = regeneration_impact(ledger, 0, ["visual"])
    audio = regeneration_impact(ledger, 0, ["audio"])
    prop_impact = asset_change_impact(ledger, "lunchbox")
    assert [item["shot_idx"] for item in visual["affected_shots"]] == [0, 1, 2]
    assert [item["shot_idx"] for item in audio["affected_shots"]] == [0]
    assert [item["shot_idx"] for item in prop_impact["affected_shots"]] == [1]


def test_asset_snapshot_change_invalidates_direct_users_and_real_descendants_only():
    scene = ReusableAsset(
        asset_id="station",
        asset_type="scene",
        display_name="Old station",
        scene_bible={"lighting": "warm tungsten"},
    )
    prop = ReusableAsset(
        asset_id="lunchbox",
        asset_type="prop",
        display_name="blue lunchbox",
        prop_bible={"condition": "clean"},
    )
    shots = [
        _shot(0, text="Empty old station."),
        _shot(1, text="Lin picks up the blue lunchbox."),
        _shot(2, text="Close-up of Lin after the pickup."),
    ]
    ledger = build_continuity_ledger(
        shots,
        contracts=_contracts(),
        preflight_report=_preflight(),
        reusable_assets=[scene, prop],
    )
    updated_prop = prop.model_copy(deep=True)
    updated_prop.prop_bible.condition = "scratched lid"

    evaluation = evaluate_asset_invalidations(
        ledger,
        reusable_assets=[scene, updated_prop],
    )

    assert evaluation["status"] == "stale"
    assert evaluation["summary"]["direct_stale_shot_count"] == 1
    assert evaluation["summary"]["stale_shot_idxs"] == [1, 2]
    assert evaluation["changed_assets"][0]["asset_id"] == "lunchbox"
    assert evaluation["shots"]["1"]["invalidations"][0]["direct"] is True
    assert evaluation["shots"]["2"]["invalidations"][0]["reason"] == "continuity_dependency"
    assert "0" not in evaluation["shots"]


def test_cross_episode_handoff_is_recorded_and_surfaces_prop_repair_advice():
    source = {
        "version": 2,
        "shots": {
            "4": {
                "shot_idx": 4,
                "character_asset_ids": ["lead"],
                "prop_asset_ids": ["lunchbox"],
                "scene_asset_ids": ["station"],
                "final_state": {
                    "characters": [{"character_idx": 0, "holding": ["lunchbox"]}],
                    "props": [{
                        "prop_id": "lunchbox",
                        "label": "blue lunchbox",
                        "holder_character_idx": 0,
                        "support": None,
                    }],
                },
            }
        },
    }
    ledger = build_continuity_ledger(
        [_shot(0, text="A new episode begins.")],
        contracts={"shots": {"0": {"shot_idx": 0, "continuity_mode": "root"}}},
        preflight_report={"shots": {"0": {
            "status": "passed",
            "initial_state": {"characters": [], "props": []},
            "final_state": {"characters": [], "props": []},
            "transitions": [],
            "issues": [],
        }}},
        inherited_ledger=source,
        inheritance_source={"source_session_id": "episode-1", "source_scene_index": 2},
    )

    handoff = continuity_handoff(source)
    assert handoff["source_shot_idx"] == 4
    assert ledger["inheritance"]["source_session_id"] == "episode-1"
    assert ledger["shots"]["0"]["inherited_from"]["source_scene_index"] == 2
    assert ledger["shots"]["0"]["repair_suggestions"][0]["code"] == "inherited_prop_state:lunchbox"
    assert ledger["summary"]["inherits_previous_state"] is True


def test_workflow_resolves_previous_episode_ledger_and_last_frame_reference():
    with tempfile.TemporaryDirectory() as tmp:
        index = SessionIndex(tmp)
        source = index.create(idea="第一集", session_id="episode-1")
        scene = index.working_dir(source["session_id"]) / "idea2video" / "scene_2"
        shot_dir = scene / "shots" / "4"
        shot_dir.mkdir(parents=True)
        (shot_dir / "last_frame.png").write_bytes(b"frame")
        save_continuity_ledger(scene / "continuity_ledger.json", {
            "version": 2,
            "shots": {"4": {
                "shot_idx": 4,
                "character_asset_ids": ["lead"],
                "prop_asset_ids": [],
                "scene_asset_ids": ["station"],
                "final_state": {"characters": [], "props": []},
            }},
        })
        target = index.create(
            idea="第二集",
            session_id="episode-2",
            continuity_source_session_id=source["session_id"],
        )
        engine = WorkflowEngine(index, tmp)

        inherited, metadata = engine._continuity_source(target, 0)
        reference = engine._continuity_reference_pair(target, 0)
        brief = engine._continuity_inheritance_brief(target)

        assert inherited["shots"]["4"]["shot_idx"] == 4
        assert metadata["source_scene_index"] == 2
        assert reference[0].endswith("last_frame.png")
        assert "上一集连续性状态" in brief
        assert "lead" in brief


def test_workflow_builds_read_only_handoff_from_legacy_contracts():
    with tempfile.TemporaryDirectory() as tmp:
        index = SessionIndex(tmp)
        source = index.create(
            idea="旧第一集",
            session_id="legacy-episode",
            character_asset_ids=["lead"],
            scene_asset_ids=["station"],
        )
        scene = index.working_dir(source["session_id"]) / "idea2video" / "scene_0"
        scene.mkdir(parents=True)
        (scene / "continuity_contracts.json").write_text(
            '{"version":1,"shots":{"2":{"shot_idx":2,"continuity_reference_shot_idx":1,'
            '"initial_state":{"props":[]},"final_state":{"props":[]},"action_transitions":[]}}}',
            encoding="utf-8",
        )
        target = index.create(
            idea="第二集",
            session_id="episode-2",
            continuity_source_session_id=source["session_id"],
        )
        engine = WorkflowEngine(index, tmp)

        inherited, metadata = engine._continuity_source(target, 0)

        assert inherited["legacy_source"] == "continuity_contracts"
        assert inherited["shots"]["2"]["character_asset_ids"] == ["lead"]
        assert inherited["shots"]["2"]["scene_asset_ids"] == ["station"]
        assert metadata["source_kind"] == "previous_episode"


def test_minimum_reference_set_keeps_scene_and_only_mentioned_props():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = {}
        for name in ("station", "lunchbox", "key", "style"):
            path = root / f"{name}.png"
            path.write_bytes(b"image")
            paths[name] = str(path)
        scene = ReusableAsset(
            asset_id="station",
            asset_type="scene",
            display_name="Old station",
            assets={"reference": paths["station"]},
        )
        lunchbox = ReusableAsset(
            asset_id="lunchbox",
            asset_type="prop",
            display_name="blue lunchbox",
            assets={"reference": paths["lunchbox"]},
        )
        key = ReusableAsset(
            asset_id="brass_key",
            asset_type="prop",
            display_name="brass key",
            assets={"reference": paths["key"]},
        )
        pipeline = object.__new__(Script2VideoPipeline)
        pipeline.reusable_assets = [scene, lunchbox, key]
        pipeline.global_reference_images = [
            (paths["station"], "[scene] station"),
            (paths["lunchbox"], "[prop] lunchbox"),
            (paths["key"], "[prop] key"),
            (paths["style"], "project style reference"),
        ]

        selected = pipeline._minimum_reusable_references(
            _shot(0, text="Lin picks up the blue lunchbox.")
        )

        assert [path for path, _text in selected] == [
            paths["station"], paths["lunchbox"], paths["style"]
        ]


def test_ledger_persistence_manifest_and_service_impact():
    ledger = build_continuity_ledger(
        [_shot(0), _shot(1)],
        contracts={
            "shots": {
                "0": {"shot_idx": 0, "continuity_mode": "root"},
                "1": {
                    "shot_idx": 1,
                    "continuity_mode": "same_camera",
                    "continuity_reference_shot_idx": 0,
                },
            }
        },
        preflight_report={"shots": {}},
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        scene = root / "idea2video" / "scene_0"
        (scene / "shots" / "0").mkdir(parents=True)
        (scene / "shots" / "1").mkdir(parents=True)
        save_continuity_ledger(scene / "continuity_ledger.json", ledger)

        loaded = load_continuity_ledger(scene / "continuity_ledger.json")
        manifest = build_manifest(root)
        service = object.__new__(ProductionService)
        service.engine = SimpleNamespace(
            session_index=SimpleNamespace(
                working_dir=lambda _session_id: root,
                get=lambda session_id: {"session_id": session_id},
            ),
            selected_video_route=lambda _session: {
                "profile_id": "seedance-balanced",
                "provider_id": "seedance",
                "model_id": "seedance-v1",
                "estimated_cost": 2.0,
            },
            _effective_config=lambda _session: {
                "generation": {"video_candidates": 2, "render_retries": 3},
                "cost": {"currency": "CNY"},
            },
        )
        impact = service.regeneration_impact("session", 0, scene_index=0, dimensions=["visual"])
        local_impact = service.regeneration_impact(
            "session", 1, scene_index=0, dimensions=["audio"]
        )
        batch = service.batch_regeneration_impact(
            "session",
            [{"scene_index": 0, "shot_idx": 0}, {"scene_index": 0, "shot_idx": 1}],
            dimensions=["visual"],
            locked_dimensions=["identity", "composition", "invalid"],
        )

        assert loaded["summary"]["shot_count"] == 2
        assert manifest["scenes"][0]["shots"][1]["continuity"]["depends_on_shot_idxs"] == [0]
        assert manifest["scenes"][0]["continuity_summary"]["shot_count"] == 2
        assert impact["source"] == "continuity_ledger"
        assert impact["scene_index"] == 0
        assert [item["shot_idx"] for item in impact["affected_shots"]] == [0, 1]
        assert impact["cost_estimate"]["estimated_lower_bound"] == 8.0
        assert impact["cost_estimate"]["estimated_upper_bound"] == 24.0
        assert local_impact["affected_count"] == 1
        assert local_impact["savings_estimate"]["full_rerender_shot_count"] == 2
        assert local_impact["savings_estimate"]["avoided_shot_count"] == 1
        assert local_impact["savings_estimate"]["shot_savings_rate"] == 0.5
        assert local_impact["savings_estimate"]["estimated_cost_saved_upper_bound"] == 12.0
        assert batch["execution_roots"] == [{"scene_index": 0, "shot_idx": 0}]
        assert batch["affected_count"] == 2
        assert batch["locked_dimensions"] == ["composition", "identity"]


def test_production_api_exposes_regeneration_impact():
    session_index = SimpleNamespace(
        get=lambda session_id: {"session_id": session_id},
        working_dir=lambda _session_id: Path("."),
    )
    service = SimpleNamespace(
        regeneration_impact=lambda session_id, shot_idx, scene_index=None, dimensions=None: {
            "session_id": session_id,
            "shot_idx": int(shot_idx),
            "scene_index": int(scene_index),
            "dimensions": dimensions,
            "affected_count": 1,
            "affected_shots": [{"shot_idx": int(shot_idx), "reasons": ["direct_edit"]}],
        },
        batch_regeneration_impact=lambda session_id, shots, dimensions=None, locked_dimensions=None: {
            "session_id": session_id,
            "requested_shots": shots,
            "affected_shots": shots,
            "requested_count": len(shots),
            "affected_count": len(shots),
            "locked_dimensions": locked_dimensions,
        },
        continuity_status=lambda session_id, scene_index=None: {
            "session_id": session_id,
            "status": "current",
            "summary": {"scene_count": 1, "changed_asset_count": 0, "stale_shot_count": 0},
            "scenes": [{"scene_index": scene_index or 0}],
        },
    )
    api = ProductionAPI(session_index, service, adapters=SimpleNamespace())

    status, body = asyncio.run(api.handle(
        "POST",
        "/api/production/session/regeneration-impact",
        {"scene_index": 2, "shot_idx": 4, "dimensions": ["visual"]},
    ))

    assert status == 200
    assert body["session_id"] == "session"
    assert body["scene_index"] == 2
    assert body["affected_shots"][0]["shot_idx"] == 4

    status, preview = asyncio.run(api.handle(
        "POST",
        "/api/production/session/regeneration-preview",
        {
            "shots": [{"scene_index": 2, "shot_idx": 4}],
            "dimensions": ["visual"],
            "locked_dimensions": ["identity"],
        },
    ))
    assert status == 200
    assert preview["requested_count"] == 1
    assert preview["locked_dimensions"] == ["identity"]

    status, continuity = asyncio.run(api.handle(
        "GET", "/api/production/session/continuity-status?scene_index=2"
    ))
    assert status == 200
    assert continuity["scenes"][0]["scene_index"] == 2
