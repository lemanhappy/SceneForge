import asyncio
import os
import tempfile
from types import SimpleNamespace

from agents.reference_image_selector import (
    merge_reference_pairs,
    pin_reference_paths,
    pin_visible_character_references,
    remap_reference_prompt,
    visible_character_names,
)
from quality.consistency_critic import ConsistencyCritic


class _Response:
    content = '{"score": 0.9, "reason": "ok"}'


class _Model:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Response()


def _image(path):
    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n")
    return path


def test_visible_characters_are_pinned_with_matching_view():
    pairs = [
        ("alice-front.png", "A front view portrait of Alice."),
        ("alice-side.png", "A side view portrait of Alice."),
        ("bob-front.png", "A front view portrait of Bob."),
        ("scene.png", "A previous scene with Alice and Bob."),
    ]
    frame = "A side profile of <Alice> while <Bob> watches. <Alice> turns away."

    pinned = pin_visible_character_references(pairs, frame)
    merged = merge_reference_pairs(pinned, [("scene.png", pairs[-1][1])])
    prompt = remap_reference_prompt("Use Image 0 for the scene.", [("scene.png", pairs[-1][1])], merged)

    assert visible_character_names(frame) == ["Alice", "Bob"]
    assert pinned["Alice"][0] == "alice-side.png"
    assert pinned["Bob"][0] == "bob-front.png"
    assert [path for path, _ in merged] == ["alice-side.png", "bob-front.png", "scene.png"]
    assert "Image 2" in prompt


def test_reference_prompt_recovers_original_source_index():
    source = [
        ("character.png", "character"),
        ("unused-a.png", "unused"),
        ("unused-b.png", "unused"),
        ("scene.png", "scene"),
    ]
    selected = [source[0], source[3]]

    prompt = remap_reference_prompt(
        "Keep the face from Image 0 and lighting from Image 3.",
        selected,
        selected,
        source_pairs=source,
        source_indices=[0, 3],
    )

    assert prompt == "Keep the face from Image 0 and lighting from Image 1."


def test_camera_anchor_and_bound_assets_are_pinned_before_model_choices():
    pairs = [
        ("anchor.png", "camera anchor"),
        ("hero.png", "A front view portrait of Hero."),
        ("prop.png", "bound prop"),
        ("optional.png", "optional reference"),
    ]
    anchors = pin_reference_paths(pairs, ["anchor.png"])
    assets = pin_reference_paths(pairs, ["prop.png"])
    characters = pin_visible_character_references(pairs, "<Hero> enters the room")

    merged = merge_reference_pairs(
        [*anchors, *characters.values(), *assets],
        [("optional.png", "optional reference")],
    )

    assert [path for path, _text in merged] == [
        "anchor.png", "hero.png", "prop.png", "optional.png"
    ]


def test_pipeline_scores_each_visible_bound_character():
    asyncio.run(_pipeline_scores_each_visible_bound_character())


async def _pipeline_scores_each_visible_bound_character():
    from pipelines.script2video_pipeline import Script2VideoPipeline

    with tempfile.TemporaryDirectory() as tmp:
        alice_ref = _image(os.path.join(tmp, "alice.png"))
        bob_ref = _image(os.path.join(tmp, "bob.png"))
        assets = {
            "alice": SimpleNamespace(assets={"front": alice_ref}),
            "bob": SimpleNamespace(assets={"front": bob_ref}),
        }
        model = _Model()
        pipeline = Script2VideoPipeline(
            chat_model=object(),
            image_generator=object(),
            video_generator=object(),
            working_dir=tmp,
            consistency_critic=ConsistencyCritic(model, threshold=0.6),
        )
        pipeline.character_bindings = {"Alice": "alice", "Bob": "bob"}
        pipeline.asset_registry = SimpleNamespace(get=lambda asset_id: assets[asset_id])
        shot_dir = os.path.join(tmp, "shots", "0")
        os.makedirs(shot_dir)
        _image(os.path.join(shot_dir, "first_frame.png"))
        shot = SimpleNamespace(
            idx=0,
            ff_vis_char_idxs=[0, 1],
            ff_desc="<Alice> faces <Bob>.",
        )
        characters = [
            SimpleNamespace(identifier_in_scene="Alice"),
            SimpleNamespace(identifier_in_scene="Bob"),
        ]

        failing = await pipeline._failing_shots([shot], characters)

        assert failing == []
        assert model.calls == 2
        assert pipeline._references_for_shot(shot, characters) == [
            (alice_ref, "Alice"),
            (bob_ref, "Bob"),
        ]
