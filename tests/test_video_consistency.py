import asyncio
import os
import tempfile

import cv2
import numpy as np

from quality import (
    ConsistencyCritic,
    VideoConsistencyAuditor,
    analyze_prop_references,
    analyze_sample_stability,
    extract_video_samples,
    score_video_candidate,
)
from quality.video_consistency import _plausible_reference_projection


class _Response:
    content = (
        '{"identity_0":0.92,"identity_1":0.90,"identity_2":0.88,'
        '"identity_3":0.86,"identity_4":0.84,'
        '"aesthetic_0":0.9,"aesthetic_1":0.8,"aesthetic_2":0.7,'
        '"aesthetic_3":0.8,"aesthetic_4":0.9,"temporal":0.82,"reason":"stable"}'
    )


class _Model:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _Response()


def _video(path):
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (32, 24))
    assert writer.isOpened()
    for index in range(21):
        frame = np.full((24, 32, 3), index * 10, dtype=np.uint8)
        writer.write(frame)
    writer.release()


def _image(path):
    with open(path, "wb") as stream:
        stream.write(b"\x89PNG\r\n")
    return path


def _motion_video(path, *, global_drift):
    writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (160, 120))
    assert writer.isOpened()
    rng = np.random.default_rng(31)
    background = rng.integers(0, 200, size=(120, 160, 3), dtype=np.uint8)
    for index in range(21):
        if global_drift:
            transform = np.float32([[1, 0, index], [0, 1, 0]])
            frame = cv2.warpAffine(background, transform, (160, 120), borderMode=cv2.BORDER_REFLECT)
        else:
            frame = background.copy()
            x = 8 + index * 2
            cv2.rectangle(frame, (x, 48), (x + 14, 70), (255, 255, 255), -1)
        writer.write(frame)
    writer.release()


def test_extracts_first_quarters_middle_and_last_frames():
    with tempfile.TemporaryDirectory() as tmp:
        video = os.path.join(tmp, "video.mp4")
        _video(video)

        samples = extract_video_samples(video, os.path.join(tmp, "samples"))

        assert [item["frame_index"] for item in samples] == [0, 5, 10, 15, 20]
        assert all(os.path.exists(item["path"]) for item in samples)


def test_video_auditor_uses_one_multimodal_call_per_character():
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            video = os.path.join(tmp, "video.mp4")
            _video(video)
            alice = _image(os.path.join(tmp, "alice.png"))
            bob = _image(os.path.join(tmp, "bob.png"))
            model = _Model()
            critic = ConsistencyCritic(
                model,
                threshold=0.6,
                aesthetic_threshold=0.6,
                temporal_threshold=0.6,
                video_sampling_enabled=True,
            )

            verdict = await VideoConsistencyAuditor(critic).audit(
                video,
                [(alice, "Alice"), (bob, "Bob")],
                description="<Alice> and <Bob> say goodbye.",
                output_dir=os.path.join(tmp, "samples"),
            )

            assert verdict["consistent"] is True
            assert verdict["score"] == 0.84
            assert set(verdict["characters"]) == {"Alice", "Bob"}
            assert len(verdict["samples"]) == 5
            assert model.calls == 2

    asyncio.run(run())


def test_sampling_config_is_loaded_and_normalized():
    critic = ConsistencyCritic.from_config(
        {"quality": {"consistency": {
            "enabled": True,
            "video_sampling_enabled": True,
            "video_sample_fractions": [-1, 0.5, 0.5, 2],
        }}},
        object(),
    )

    assert critic.video_sampling_enabled is True
    assert critic.video_sample_fractions == (0.0, 0.5, 1.0)


def test_locked_camera_allows_a_small_moving_subject_on_static_background():
    with tempfile.TemporaryDirectory() as tmp:
        rng = np.random.default_rng(7)
        background = rng.integers(0, 180, size=(120, 160, 3), dtype=np.uint8)
        samples = []
        for index, x in enumerate((10, 30, 50)):
            frame = background.copy()
            cv2.rectangle(frame, (x, 45), (x + 16, 70), (255, 255, 255), -1)
            path = os.path.join(tmp, f"stable_{index}.png")
            assert cv2.imwrite(path, frame)
            samples.append({"path": path})

        result = analyze_sample_stability(samples, camera_locked=True)

        assert result["consistent"] is True
        assert result["failed"] == []


def test_locked_camera_rejects_repeated_whole_frame_drift():
    with tempfile.TemporaryDirectory() as tmp:
        rng = np.random.default_rng(11)
        base = rng.integers(0, 255, size=(120, 160, 3), dtype=np.uint8)
        samples = []
        for index, offset in enumerate((0, 7, 14)):
            transform = np.float32([[1, 0, offset], [0, 1, 0]])
            frame = cv2.warpAffine(base, transform, (160, 120), borderMode=cv2.BORDER_REFLECT)
            path = os.path.join(tmp, f"drift_{index}.png")
            assert cv2.imwrite(path, frame)
            samples.append({"path": path})

        result = analyze_sample_stability(samples, camera_locked=True)

        assert result["consistent"] is False
        assert result["failed"] == ["static_world"]
        assert result["metrics"]["drifting_pair_count"] == 2


def test_bound_static_prop_tracker_flags_large_unplanned_displacement():
    with tempfile.TemporaryDirectory() as tmp:
        rng = np.random.default_rng(19)
        prop = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
        reference = os.path.join(tmp, "lunchbox.png")
        assert cv2.imwrite(reference, prop)
        samples = []
        for index, x in enumerate((10, 55, 95)):
            frame = np.full((140, 180, 3), 20, dtype=np.uint8)
            frame[38:102, x:x + 64] = prop
            path = os.path.join(tmp, f"prop_{index}.png")
            assert cv2.imwrite(path, frame)
            samples.append({"path": path})

        result = analyze_prop_references(
            samples,
            [(reference, "[prop] blue metal lunchbox")],
            prop_motion_allowed=False,
            camera_locked=True,
        )

        assert result["available"] is True
        assert result["assets"]["lunchbox"]["matched_sample_count"] == 3
        assert {issue["code"] for issue in result["issues"]} == {
            "possible_static_prop_drift"
        }


def test_prop_tracker_rejects_weak_and_impossible_homographies():
    plausible = np.float32([[20, 20], [80, 20], [80, 70], [20, 70]])
    assert _plausible_reference_projection(
        plausible, (100, 120, 3), inlier_matches=10
    )
    assert not _plausible_reference_projection(
        plausible, (100, 120, 3), inlier_matches=9
    )
    impossible = np.float32([[-800, -600], [900, -600], [900, 700], [-800, 700]])
    assert not _plausible_reference_projection(
        impossible, (100, 120, 3), inlier_matches=20
    )


def test_moving_camera_does_not_report_static_prop_projection_change():
    with tempfile.TemporaryDirectory() as tmp:
        rng = np.random.default_rng(23)
        prop = rng.integers(0, 255, size=(64, 64, 3), dtype=np.uint8)
        reference = os.path.join(tmp, "notebook.png")
        assert cv2.imwrite(reference, prop)
        samples = []
        for index, x in enumerate((10, 55, 95)):
            frame = np.full((140, 180, 3), 20, dtype=np.uint8)
            frame[38:102, x:x + 64] = prop
            path = os.path.join(tmp, f"moving_camera_{index}.png")
            assert cv2.imwrite(path, frame)
            samples.append({"path": path})

        result = analyze_prop_references(
            samples,
            [(reference, "[prop] burgundy notebook")],
            prop_motion_allowed=False,
            camera_locked=False,
        )

        assert result["available"] is True
        assert result["issues"] == []


def test_candidate_score_prefers_static_world_for_locked_camera():
    with tempfile.TemporaryDirectory() as tmp:
        stable = os.path.join(tmp, "stable.mp4")
        drifting = os.path.join(tmp, "drifting.mp4")
        _motion_video(stable, global_drift=False)
        _motion_video(drifting, global_drift=True)

        stable_report = score_video_candidate(
            stable,
            os.path.join(tmp, "stable_samples"),
            camera_locked=True,
        )
        drift_report = score_video_candidate(
            drifting,
            os.path.join(tmp, "drift_samples"),
            camera_locked=True,
        )

        assert stable_report["consistent"] is True
        assert drift_report["consistent"] is False
        assert stable_report["score"] > drift_report["score"]
