import asyncio
import os
import tempfile
from types import SimpleNamespace

from interfaces.camera import Camera
from quality import build_continuity_contracts, continuity_reference_for_shot
from quality.consistency_critic import ConsistencyCritic


class _Response:
    def __init__(self, content):
        self.content = content


class _Model:
    def __init__(self, content):
        self.content = content
        self.calls = 0
        self.messages = None

    async def ainvoke(self, messages):
        self.calls += 1
        self.messages = messages
        return _Response(self.content)


class _SequenceModel(_Model):
    def __init__(self, contents):
        super().__init__(contents[0])
        self.contents = list(contents)

    async def ainvoke(self, messages):
        self.calls += 1
        self.messages = messages
        return _Response(self.contents[min(self.calls - 1, len(self.contents) - 1)])


def _image(path):
    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n")
    return path


def _shot(idx, cam_idx):
    return SimpleNamespace(
        idx=idx,
        cam_idx=cam_idx,
        visual_desc=f"shot {idx}",
        motion_desc="character moves as described",
        variation_reason="intentional coverage change",
        ff_desc=f"frame {idx}",
        ff_vis_char_idxs=[],
    )


def test_contract_distinguishes_same_camera_and_cross_camera_continuity():
    shots = [_shot(0, 0), _shot(1, 1), _shot(2, 0)]
    cameras = [
        Camera(idx=0, active_shot_idxs=[0, 2]),
        Camera(
            idx=1,
            active_shot_idxs=[1],
            parent_cam_idx=0,
            parent_shot_idx=0,
            reason="reverse angle across the same room",
        ),
    ]

    contracts = build_continuity_contracts(cameras, shots)

    assert continuity_reference_for_shot(contracts, 0) == (None, "root")
    assert continuity_reference_for_shot(contracts, 1) == (0, "cross_camera")
    assert continuity_reference_for_shot(contracts, 2) == (0, "same_camera")
    assert contracts["shots"]["1"]["camera_relation"] == "reverse angle across the same room"
    assert contracts["shots"]["1"]["lock_scene_geometry"] is False
    assert contracts["shots"]["2"]["lock_scene_geometry"] is True
    assert contracts["shots"]["1"]["prompt_preflight_status"] == "passed"
    assert contracts["shots"]["1"]["initial_state"]["camera"]["camera_idx"] == 1
    assert contracts["shots"]["1"]["final_state"]["characters"] == []
    assert contracts["shots"]["1"]["action_transitions"] == []


def test_keyframe_target_keeps_whole_shot_static_prop_state():
    from pipelines.script2video_pipeline import _frame_target_description

    shot = _shot(0, 0)
    shot.ff_desc = "The character enters through the left door."
    shot.visual_desc = "A blue lunchbox rests on the central green bench."
    shot.motion_desc = "The character notices the lunchbox and approaches it."

    target = _frame_target_description(shot, "first_frame")

    assert "blue lunchbox rests on the central green bench" in target
    assert "must already exist in the first frame" in target
    assert "Never make an object pop in" in target


def test_incomplete_frame_is_quarantined_with_cached_selector():
    from pipelines.script2video_pipeline import _is_reusable_image

    with tempfile.TemporaryDirectory() as tmp:
        frame = os.path.join(tmp, "first_frame.png")
        selector = os.path.join(tmp, "first_frame_selector_output.json")
        with open(frame, "wb") as stream:
            stream.write(b"partial provider download")
        with open(selector, "w", encoding="utf-8") as stream:
            stream.write('{"text_prompt":"stale"}')

        assert _is_reusable_image(frame) is False
        assert not os.path.exists(frame)
        assert not os.path.exists(selector)
        names = os.listdir(tmp)
        assert any(name.startswith("first_frame.png.invalid-") for name in names)
        assert any(
            name.startswith("first_frame_selector_output.json.invalid-")
            for name in names
        )


def test_cross_camera_scene_score_allows_viewpoint_change_but_rejects_world_drift():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            anchor = _image(os.path.join(tmp, "anchor.png"))
            frame = _image(os.path.join(tmp, "frame.png"))
            model = _Model('{"scene":0.3,"reason":"different building"}')
            critic = ConsistencyCritic(model, scene_threshold=0.65)

            verdict = await critic.score_scene(
                anchor,
                frame,
                same_camera=False,
                camera_relation="reverse angle",
            )

            assert verdict["consistent"] is False
            assert verdict["failed"] == ["scene"]
            text = str(model.messages[0].content)
            assert "Viewpoint, shot size, composition, and lens MAY change" in text
            assert "Intended camera relationship: reverse angle" in text

    asyncio.run(run())


def test_scene_score_retries_when_model_puts_repair_target_in_score_field():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            anchor = _image(os.path.join(tmp, "anchor.png"))
            frame = _image(os.path.join(tmp, "frame.png"))
            model = _SequenceModel([
                '{"scene":"current","reason":"wrong field"}',
                '{"scene":0.9,"repair_target":"none","reason":"same world"}',
            ])
            critic = ConsistencyCritic(model, scene_threshold=0.65)

            verdict = await critic.score_scene(anchor, frame, same_camera=False)

            assert verdict["consistent"] is True
            assert verdict["score"] == 0.9
            assert model.calls == 2

    asyncio.run(run())


def test_scene_score_parses_quoted_number_and_keeps_repair_target():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            anchor = _image(os.path.join(tmp, "anchor.png"))
            frame = _image(os.path.join(tmp, "frame.png"))
            model = _Model(
                '{"scene":"0.5","repair_target":"anchor","reason":"bad anchor"}'
            )
            critic = ConsistencyCritic(model, scene_threshold=0.65)

            verdict = await critic.score_scene(anchor, frame, same_camera=False)

            assert verdict["consistent"] is False
            assert verdict["score"] == 0.5
            assert verdict["repair_target"] == "anchor"

    asyncio.run(run())


def test_scene_prompt_allows_scripted_prop_state_progression():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            anchor = _image(os.path.join(tmp, "anchor.png"))
            frame = _image(os.path.join(tmp, "frame.png"))
            model = _Model('{"scene":0.9,"repair_target":"anchor"}')
            critic = ConsistencyCritic(model, scene_threshold=0.65)

            verdict = await critic.score_scene(
                anchor,
                frame,
                same_camera=False,
                anchor_description="The lunchbox rests on the bench.",
                description="He picks up the lunchbox and carries it to the window.",
            )

            prompt = str(model.messages[0].content)
            assert "scripted state progression is continuity, not teleportation" in prompt
            assert '"scene" value MUST be a JSON number' in prompt
            assert verdict["repair_target"] == "none"

    asyncio.run(run())


def test_scene_threshold_loads_from_quality_config():
    critic = ConsistencyCritic.from_config(
        {"quality": {"consistency": {"enabled": True, "scene_threshold": 0.72}}},
        object(),
    )

    assert critic.scene_threshold == 0.72
    assert critic.extra_dims_enabled is True


def test_same_camera_scene_score_allows_described_reframing():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            anchor = _image(os.path.join(tmp, "anchor.png"))
            frame = _image(os.path.join(tmp, "frame.png"))
            model = _Model('{"scene":0.9,"reason":"same set, tighter shot"}')
            critic = ConsistencyCritic(model, scene_threshold=0.65)

            verdict = await critic.score_scene(
                anchor,
                frame,
                same_camera=True,
                description="The camera slowly pushes from a wide shot to a medium shot.",
            )

            assert verdict["consistent"] is True
            text = str(model.messages[0].content)
            assert "Shot size, lens, composition" in text
            assert "do not require pixel alignment" in text

    asyncio.run(run())


def test_pipeline_flags_only_later_shot_when_same_world_continuity_fails():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            model = _Model('{"scene":0.2,"reason":"layout drift"}')
            critic = ConsistencyCritic(model, scene_threshold=0.65)
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
                consistency_critic=critic,
            )
            shots = [_shot(0, 0), _shot(1, 0)]
            for shot in shots:
                shot_dir = os.path.join(tmp, "shots", str(shot.idx))
                os.makedirs(shot_dir)
                _image(os.path.join(shot_dir, "first_frame.png"))

            failing = await pipeline._failing_shots(shots, [])

            assert [idx for idx, _verdict in failing] == [1]
            assert failing[0][1]["failed"] == ["scene"]
            assert model.calls == 1

    asyncio.run(run())


def test_pipeline_keeps_single_child_anchor_suspicion_advisory():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            model = _Model(
                '{"scene":0.2,"repair_target":"anchor",'
                '"reason":"anchor puts the lunchbox on the floor; child correctly keeps it on the bench"}'
            )
            critic = ConsistencyCritic(model, scene_threshold=0.65)
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
                consistency_critic=critic,
            )
            shots = [_shot(0, 0), _shot(1, 0)]
            shots[0].ff_desc = "The lunchbox rests on the central bench."
            shots[1].ff_desc = "Close-up of the same lunchbox on the bench."
            for shot in shots:
                shot_dir = os.path.join(tmp, "shots", str(shot.idx))
                os.makedirs(shot_dir)
                _image(os.path.join(shot_dir, "first_frame.png"))

            failing = await pipeline._failing_shots(shots, [])

            assert failing == []
            import json
            with open(os.path.join(tmp, "quality.json"), encoding="utf-8") as stream:
                quality = json.load(stream)
            advisory = quality["1"]["checks"]["scene_anchor_advisory"]
            assert advisory["advisory"] is True
            assert advisory["failed"] == []
            prompt = str(model.messages[0].content)
            assert "Anchor shot intent for Image A" in prompt
            assert "support surface" in prompt

    asyncio.run(run())


def test_pipeline_repairs_anchor_after_two_independent_camera_failures():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            model = _Model(
                '{"scene":0.2,"repair_target":"anchor","reason":"bad root layout"}'
            )
            critic = ConsistencyCritic(model, scene_threshold=0.65)
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
                consistency_critic=critic,
            )
            shots = [_shot(0, 0), _shot(1, 1), _shot(2, 2)]
            cameras = [
                Camera(idx=0, active_shot_idxs=[0]),
                Camera(idx=1, active_shot_idxs=[1], parent_cam_idx=0, parent_shot_idx=0),
                Camera(idx=2, active_shot_idxs=[2], parent_cam_idx=0, parent_shot_idx=0),
            ]
            with open(os.path.join(tmp, "camera_tree.json"), "w", encoding="utf-8") as stream:
                import json
                json.dump([camera.model_dump(mode="json") for camera in cameras], stream)
            for shot in shots:
                shot_dir = os.path.join(tmp, "shots", str(shot.idx))
                os.makedirs(shot_dir)
                _image(os.path.join(shot_dir, "first_frame.png"))

            failing = await pipeline._failing_shots(shots, [])

            assert [idx for idx, _verdict in failing] == [0]
            assert failing[0][1]["failed"] == ["scene"]
            assert "scene_anchor_for_shot_1" in failing[0][1]["checks"]
            assert "scene_anchor_for_shot_2" in failing[0][1]["checks"]

    asyncio.run(run())


def test_keyframe_preview_builds_child_camera_from_parent_world_reference():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
            )
            cameras = [
                Camera(idx=0, active_shot_idxs=[0]),
                Camera(idx=1, active_shot_idxs=[1], parent_cam_idx=0, parent_shot_idx=0),
            ]
            shots = [_shot(0, 0), _shot(1, 1)]
            calls = []

            async def generate(**kwargs):
                calls.append((kwargs["camera"].idx, kwargs.get("world_reference_pair")))

            pipeline.generate_frames_for_single_camera = generate
            await pipeline._generate_preview_camera_frames(
                camera_tree=cameras,
                shot_descriptions=shots,
                characters=[],
                character_portraits_registry={},
                priority_shot_idxs=[],
            )

            assert [camera_idx for camera_idx, _pair in calls] == [0, 1]
            assert calls[0][1] is None
            assert calls[1][1][0].endswith(os.path.join("shots", "0", "first_frame.png"))
            assert calls[1][1][1].startswith("frame 0")
            assert "[Object-state rule]" in calls[1][1][1]

    asyncio.run(run())


def test_keyframe_preview_prefers_bound_scene_over_parent_frame_pixels():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
            )
            pipeline.global_reference_images = [
                (os.path.join(tmp, "office.png"), "[scene] fixed office topology")
            ]
            cameras = [
                Camera(idx=0, active_shot_idxs=[0]),
                Camera(idx=1, active_shot_idxs=[1], parent_cam_idx=0, parent_shot_idx=0),
            ]
            shots = [_shot(0, 0), _shot(1, 1)]
            calls = []

            async def generate(**kwargs):
                calls.append((kwargs["camera"].idx, kwargs.get("world_reference_pair")))

            pipeline.generate_frames_for_single_camera = generate
            await pipeline._generate_preview_camera_frames(
                camera_tree=cameras,
                shot_descriptions=shots,
                characters=[],
                character_portraits_registry={},
                priority_shot_idxs=[],
            )

            assert [camera_idx for camera_idx, _pair in calls] == [0, 1]
            assert calls[0][1] is None
            assert calls[1][1] is None

    asyncio.run(run())


def test_keyframe_preview_can_target_one_shot_with_existing_camera_anchor():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
            )
            cameras = [Camera(idx=0, active_shot_idxs=[0, 1])]
            shots = [_shot(0, 0), _shot(1, 0)]
            anchor_dir = os.path.join(tmp, "shots", "0")
            os.makedirs(anchor_dir)
            _image(os.path.join(anchor_dir, "first_frame.png"))
            calls = []

            async def generate(**kwargs):
                calls.append(kwargs)

            pipeline.generate_frames_for_single_camera = generate
            await pipeline._generate_preview_camera_frames(
                camera_tree=cameras,
                shot_descriptions=shots,
                characters=[],
                character_portraits_registry={},
                priority_shot_idxs=[],
                target_shot_idxs=[1],
            )

            assert len(calls) == 1
            assert calls[0]["camera"].active_shot_idxs == [1]
            assert calls[0]["camera"].parent_shot_idx is None
            assert calls[0]["world_reference_pair"][0].endswith(
                os.path.join("shots", "0", "first_frame.png"))
            assert calls[0]["first_frames_only"] is True

    asyncio.run(run())


def test_failed_keyframe_regeneration_restores_previous_frame():
    async def run():
        from pipelines.script2video_pipeline import Script2VideoPipeline

        with tempfile.TemporaryDirectory() as tmp:
            pipeline = Script2VideoPipeline(
                chat_model=object(),
                image_generator=object(),
                video_generator=object(),
                working_dir=tmp,
            )
            shot = _shot(0, 0)
            camera = Camera(idx=0, active_shot_idxs=[0])
            shot_dir = os.path.join(tmp, "shots", "0")
            os.makedirs(shot_dir)
            frame_path = os.path.join(shot_dir, "first_frame.png")
            with open(frame_path, "wb") as stream:
                stream.write(b"previous-frame")

            async def design(**_kwargs):
                return [shot]

            async def decompose(**_kwargs):
                return [shot]

            async def construct(**_kwargs):
                return [camera]

            async def fail_render(**_kwargs):
                raise RuntimeError("image provider failed")

            pipeline.design_storyboard = design
            pipeline.decompose_visual_descriptions = decompose
            pipeline.construct_camera_tree = construct
            pipeline._write_prompt_preflight = lambda _shots: {}
            pipeline._write_continuity_contracts = lambda _tree, _shots: {}
            pipeline._generate_preview_camera_frames = fail_render

            try:
                await pipeline.generate_keyframes(
                    script="测试",
                    user_requirement="",
                    style="",
                    characters=[],
                    character_portraits_registry={},
                    shot_indexes=[0],
                    force=True,
                )
            except RuntimeError as exc:
                assert str(exc) == "image provider failed"
            else:
                raise AssertionError("regeneration failure was not propagated")

            with open(frame_path, "rb") as stream:
                assert stream.read() == b"previous-frame"
            assert not os.path.exists(frame_path + ".before-regenerate")

    asyncio.run(run())
