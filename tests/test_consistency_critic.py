"""Tests for the character-consistency critic and the pipeline auto-fix loop."""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from quality.consistency_critic import ConsistencyCritic


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        return _FakeResp(self.reply)


def _img(path):
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n")  # token bytes; critic only base64-encodes them


class TestCritic(unittest.TestCase):
    def test_parse_score(self):
        self.assertEqual(ConsistencyCritic._parse_score('{"score": 0.82, "reason":"ok"}'), 0.82)
        self.assertEqual(ConsistencyCritic._parse_score('score = 0.3'), 0.3)
        self.assertIsNone(ConsistencyCritic._parse_score('no number here'))

    def test_consistent_above_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            ref, frame = os.path.join(d, "r.png"), os.path.join(d, "f.png")
            _img(ref); _img(frame)
            c = ConsistencyCritic(_FakeModel('{"score":0.9,"reason":"same"}'), threshold=0.6)
            v = asyncio.run(c.score(ref, frame, "Hero"))
            self.assertTrue(v["consistent"]); self.assertEqual(v["score"], 0.9)

    def test_inconsistent_below_threshold(self):
        with tempfile.TemporaryDirectory() as d:
            ref, frame = os.path.join(d, "r.png"), os.path.join(d, "f.png")
            _img(ref); _img(frame)
            c = ConsistencyCritic(_FakeModel('{"score":0.2,"reason":"different face"}'), threshold=0.6)
            v = asyncio.run(c.score(ref, frame, "Hero"))
            self.assertFalse(v["consistent"])

    def test_missing_files_fail_open(self):
        c = ConsistencyCritic(_FakeModel('{"score":0.1}'), threshold=0.6)
        v = asyncio.run(c.score("/no/ref.png", "/no/frame.png", "Hero"))
        self.assertTrue(v["consistent"])  # can't judge -> don't block

    def test_unparseable_fail_open(self):
        with tempfile.TemporaryDirectory() as d:
            ref, frame = os.path.join(d, "r.png"), os.path.join(d, "f.png")
            _img(ref); _img(frame)
            c = ConsistencyCritic(_FakeModel('the model rambled with no score'), threshold=0.6)
            self.assertTrue(asyncio.run(c.score(ref, frame, "Hero"))["consistent"])

    def test_from_config(self):
        self.assertIsNone(ConsistencyCritic.from_config({}, object()))
        c = ConsistencyCritic.from_config({"quality": {"consistency": {"enabled": True, "threshold": 0.7}}}, object())
        self.assertIsNotNone(c); self.assertEqual(c.threshold, 0.7)
        # extra dimensions default off
        self.assertFalse(c.extra_dims_enabled)

    def test_from_config_extra_dims(self):
        c = ConsistencyCritic.from_config(
            {"quality": {"consistency": {"enabled": True, "aesthetic_threshold": 0.5,
                                         "adherence_threshold": 0.4}}}, object())
        self.assertEqual(c.aesthetic_threshold, 0.5)
        self.assertEqual(c.adherence_threshold, 0.4)
        self.assertTrue(c.extra_dims_enabled)

    def test_aesthetic_dim_fails_without_reference(self):
        # no reference portrait at all, but aesthetic gate is on -> still judged
        with tempfile.TemporaryDirectory() as d:
            frame = os.path.join(d, "f.png"); _img(frame)
            c = ConsistencyCritic(_FakeModel('{"aesthetic":0.2,"reason":"garbled hands"}'),
                                  threshold=0.6, aesthetic_threshold=0.5)
            v = asyncio.run(c.score("", frame, "Hero"))
            self.assertFalse(v["consistent"])
            self.assertIn("aesthetic", v["reason"])
            self.assertEqual(v["score"], 1.0)  # identity not checked -> stays 1.0

    def test_adherence_needs_description(self):
        with tempfile.TemporaryDirectory() as d:
            frame = os.path.join(d, "f.png"); _img(frame)
            model = _FakeModel('{"adherence":0.1}')
            c = ConsistencyCritic(model, threshold=0.6, adherence_threshold=0.5)
            # no description -> adherence not checked, nothing to judge -> fail open
            v = asyncio.run(c.score("", frame, "Hero", description=""))
            self.assertTrue(v["consistent"]); self.assertEqual(model.calls, 0)
            # with description -> checked and fails
            v = asyncio.run(c.score("", frame, "Hero", description="a wide shot of a temple"))
            self.assertFalse(v["consistent"])

    def test_multi_dim_all_pass(self):
        with tempfile.TemporaryDirectory() as d:
            ref, frame = os.path.join(d, "r.png"), os.path.join(d, "f.png")
            _img(ref); _img(frame)
            c = ConsistencyCritic(_FakeModel('{"score":0.9,"aesthetic":0.8,"adherence":0.7}'),
                                  threshold=0.6, aesthetic_threshold=0.5, adherence_threshold=0.5)
            v = asyncio.run(c.score(ref, frame, "Hero", description="hero smiles"))
            self.assertTrue(v["consistent"])
            self.assertEqual(v["dims"], {"score": 0.9, "aesthetic": 0.8, "adherence": 0.7})
            self.assertEqual(v["failed"], [])

    def test_temporal_needs_last_frame(self):
        with tempfile.TemporaryDirectory() as d:
            frame, last = os.path.join(d, "f.png"), os.path.join(d, "l.png")
            _img(frame); _img(last)
            model = _FakeModel('{"temporal":0.2}')
            c = ConsistencyCritic(model, threshold=0.6, temporal_threshold=0.5)
            # no last frame supplied -> temporal not checked, nothing to judge -> fail open
            v = asyncio.run(c.score("", frame, "Hero"))
            self.assertTrue(v["consistent"]); self.assertEqual(model.calls, 0)
            # last frame present -> temporal checked and fails
            v = asyncio.run(c.score("", frame, "Hero", second_frame_path=last))
            self.assertFalse(v["consistent"]); self.assertEqual(v["failed"], ["temporal"])

    def test_extra_dims_includes_temporal(self):
        c = ConsistencyCritic(object(), temporal_threshold=0.5)
        self.assertTrue(c.extra_dims_enabled)

    def test_temporal_instruction_allows_described_occlusion(self):
        text = ConsistencyCritic._temporal_instruction(
            "She carries the key, turns away, and exits through the door."
        )
        self.assertIn("naturally occluded", text)
        self.assertIn("leaving the frame with the character", text)
        self.assertIn("translate and rotate with the actor's hands", text)
        self.assertIn("continuous hand-to-counter path", text)
        self.assertIn("She carries the key", text)
        self.assertIn("duplicate instances of the same character", text)
        self.assertIn("visibly drifting static world", text)
        self.assertIn("physically plausible mirror or window reflection", text)

    def test_sequence_prompt_ignores_synchronized_reflections(self):
        critic = ConsistencyCritic(object(), threshold=0.6, aesthetic_threshold=0.6)
        message = critic._build_message(
            [("score", 0.6, '"score": identity match')],
            [],
        )
        prompt = message.content[0]["text"]
        self.assertIn("moves synchronously", prompt)
        self.assertIn("judge the primary physical subject", prompt)


class TestPipelineAutofix(unittest.IsolatedAsyncioTestCase):
    """Exercise the pipeline's _failing_shots + _verify_and_autofix wiring with a
    stub critic and a stubbed regenerate_shot (no real rendering)."""

    def _pipeline(self, tmp, critic, max_retries=1):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        p = Script2VideoPipeline(chat_model=object(), image_generator=object(), video_generator=object(),
                                 working_dir=tmp, consistency_critic=critic, consistency_max_retries=max_retries)
        return p

    async def test_failing_shots_uses_fixed_reference(self):
        with tempfile.TemporaryDirectory() as d:
            ref = os.path.join(d, "front.png"); _img(ref)
            # asset registry stub binding 'Hero' -> asset with front ref
            asset = SimpleNamespace(assets={"front": ref})
            registry = SimpleNamespace(get=lambda aid: asset)
            critic = ConsistencyCritic(_FakeModel('{"score":0.1}'), threshold=0.6)  # always inconsistent
            p = self._pipeline(d, critic)
            p.character_bindings = {"Hero": "hero"}
            p.asset_registry = registry
            # one shot, visible char idx 0, with a first_frame on disk
            shot = SimpleNamespace(idx=0, ff_vis_char_idxs=[0])
            os.makedirs(os.path.join(d, "shots", "0"))
            _img(os.path.join(d, "shots", "0", "first_frame.png"))
            chars = [SimpleNamespace(identifier_in_scene="Hero")]
            failing = await p._failing_shots([shot], chars)
            self.assertEqual([i for i, _v in failing], [0])

    async def test_autofix_regenerates_failing_then_stops(self):
        with tempfile.TemporaryDirectory() as d:
            ref = os.path.join(d, "front.png"); _img(ref)
            asset = SimpleNamespace(assets={"front": ref})
            critic = ConsistencyCritic(_FakeModel('{"score":0.1}'), threshold=0.6)
            p = self._pipeline(d, critic, max_retries=2)
            p.character_bindings = {"Hero": "hero"}
            p.asset_registry = SimpleNamespace(get=lambda aid: asset)
            shot = SimpleNamespace(idx=0, ff_vis_char_idxs=[0])
            os.makedirs(os.path.join(d, "shots", "0"))
            _img(os.path.join(d, "shots", "0", "first_frame.png"))
            chars = [SimpleNamespace(identifier_in_scene="Hero")]
            calls = []
            async def fake_regen(shot_idx, *a, **k):
                calls.append(shot_idx); return "final.mp4"
            p.regenerate_shot = fake_regen
            await p._verify_and_autofix("script", "req", "style", chars, [shot], "final.mp4", None)
            # max_retries=2 -> regenerate attempted on each round it stays failing
            self.assertEqual(calls, [0, 0])

    async def test_temporal_only_failure_rerenders_clip_without_rebuilding_keyframes(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._pipeline(d, object(), max_retries=1)
            verdicts = [[(0, {"failed": ["temporal"], "reason": "duplicate actor"})], []]

            async def failing(*args, **kwargs):
                return verdicts.pop(0)

            clip_calls = []

            async def rerender_clip(shot_idx, shot_descriptions, progress=None):
                clip_calls.append((shot_idx, shot_descriptions))
                return "clip-fixed.mp4"

            async def rebuild_shot(*args, **kwargs):
                self.fail("temporal-only failure rebuilt keyframes")

            shot_descriptions = [SimpleNamespace(idx=0)]
            p._failing_shots = failing
            p.regenerate_video_clip = rerender_clip
            p.regenerate_shot = rebuild_shot

            result = await p._verify_and_autofix(
                "script", "req", "style", [], shot_descriptions, "old.mp4", None
            )

            self.assertEqual(result, "clip-fixed.mp4")
            self.assertEqual(clip_calls, [(0, shot_descriptions)])

    async def test_sampled_motion_failure_preserves_keyframes(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._pipeline(d, object(), max_retries=1)
            verdicts = [[(0, {
                "failed": ["adherence", "temporal"],
                "reason": "motion drift",
                "samples": [{"path": "sample.jpg"}],
            })], []]

            async def failing(*args, **kwargs):
                return verdicts.pop(0)

            clip_calls = []

            async def rerender_clip(shot_idx, shot_descriptions, progress=None):
                clip_calls.append(shot_idx)
                return "clip-fixed.mp4"

            async def rebuild_shot(*args, **kwargs):
                self.fail("sampled motion failure rebuilt approved keyframes")

            p._failing_shots = failing
            p.regenerate_video_clip = rerender_clip
            p.regenerate_shot = rebuild_shot
            result = await p._verify_and_autofix(
                "script", "req", "style", [], [SimpleNamespace(idx=0)], "old.mp4", None
            )

            self.assertEqual(result, "clip-fixed.mp4")
            self.assertEqual(clip_calls, [0])

    async def test_finalization_repairs_before_audio_and_subtitles(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._pipeline(d, object())
            p.voiceover_service = object()
            p.subtitle_service = object()
            events = []

            async def verify(*args, **kwargs):
                events.append("verify")
                return "repaired.mp4"

            def audio(path, *args, **kwargs):
                events.append(("audio", path))
                return "audio.mp4", object()

            def subtitles(path, *args, **kwargs):
                events.append(("subtitles", path))
                return "subtitled.mp4"

            p._verify_and_autofix = verify
            p._maybe_postprocess_audio = audio
            p._maybe_burn_subtitles = subtitles

            result = await p._finalize_video(
                "script", "req", "style", [], [], "raw.mp4"
            )

            self.assertEqual(result, "subtitled.mp4")
            self.assertEqual(
                events,
                ["verify", ("audio", "repaired.mp4"), ("subtitles", "audio.mp4")],
            )

    async def test_nested_consistency_render_skips_postprocessing(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._pipeline(d, object())
            p._verifying = True
            p.voiceover_service = object()
            p.subtitle_service = object()
            p._maybe_postprocess_audio = lambda *a, **k: self.fail("audio ran")
            p._maybe_burn_subtitles = lambda *a, **k: self.fail("subtitles ran")

            result = await p._finalize_video(
                "script", "req", "style", [], [], "raw.mp4"
            )

            self.assertEqual(result, "raw.mp4")

    async def test_failing_shots_scores_referenceless_when_extra_dims_on(self):
        # a shot with NO bound character is still judged when aesthetic gate is on
        with tempfile.TemporaryDirectory() as d:
            critic = ConsistencyCritic(_FakeModel('{"aesthetic":0.1}'), threshold=0.6, aesthetic_threshold=0.5)
            p = self._pipeline(d, critic)
            p.character_bindings = {}
            p.asset_registry = None
            shot = SimpleNamespace(idx=0, ff_vis_char_idxs=[], ff_desc="a temple at dawn")
            os.makedirs(os.path.join(d, "shots", "0"))
            _img(os.path.join(d, "shots", "0", "first_frame.png"))
            failing = await p._failing_shots([shot], [])
            self.assertEqual([i for i, _v in failing], [0])

    async def test_directed_regeneration_records_and_consumes_hint(self):
        with tempfile.TemporaryDirectory() as d:
            ref = os.path.join(d, "front.png"); _img(ref)
            asset = SimpleNamespace(assets={"front": ref})
            critic = ConsistencyCritic(_FakeModel('{"score":0.1,"reason":"different face"}'), threshold=0.6)
            p = self._pipeline(d, critic, max_retries=1)
            p.character_bindings = {"Hero": "hero"}
            p.asset_registry = SimpleNamespace(get=lambda aid: asset)
            shot = SimpleNamespace(idx=0, ff_vis_char_idxs=[0])
            os.makedirs(os.path.join(d, "shots", "0"))
            _img(os.path.join(d, "shots", "0", "first_frame.png"))
            chars = [SimpleNamespace(identifier_in_scene="Hero")]
            async def fake_regen(shot_idx, *a, **k):
                return "final.mp4"
            p.regenerate_shot = fake_regen
            await p._verify_and_autofix("s", "r", "st", chars, [shot], "final.mp4", None)
            # the failing shot got a corrective instruction queued for its re-render,
            # carrying the critic's reason so the regeneration is targeted
            self.assertIn(0, p._shot_corrections)
            self.assertIn("REJECTED", p._shot_corrections[0])
            self.assertIn("different face", p._shot_corrections[0])

    async def test_correction_hint_is_dimension_scoped(self):
        from pipelines.script2video_pipeline import Script2VideoPipeline
        # only aesthetic failed -> hint should mention the aesthetic fix, not the others
        hint = Script2VideoPipeline._correction_hint(
            {"reason": "aesthetic 0.20 below required 0.50", "failed": ["aesthetic"]})
        self.assertIn("high-quality frame", hint)
        self.assertNotIn("reference portrait", hint)
        # empty failed list -> full checklist fallback
        full = Script2VideoPipeline._correction_hint({"reason": "x", "failed": []})
        self.assertIn("reference portrait", full)
        self.assertIn("high-quality frame", full)


if __name__ == "__main__":
    unittest.main()
