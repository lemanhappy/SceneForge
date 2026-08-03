"""Structured, performance-level storyboard planning regression tests."""

import unittest

from pydantic import ValidationError

from agents.storyboard_artist import chinese_review_field_issues, validate_chinese_review_fields
from interfaces import PerformanceBeat, ShotBriefDescription
from prompting import compile_video_prompt


class TestDetailedStoryboardModel(unittest.TestCase):
    def test_old_storyboard_entries_receive_compatible_defaults(self):
        shot = ShotBriefDescription.model_validate({
            "idx": 0,
            "is_last": True,
            "cam_idx": 0,
            "visual_desc": "A close-up.",
            "audio_desc": "",
        })

        self.assertEqual(shot.duration_sec, 5.0)
        self.assertEqual(shot.director_desc, "")
        self.assertEqual(shot.beats, [])
        self.assertEqual(shot.visual_style, [])
        self.assertEqual(shot.avoid, [])

    def test_duration_has_a_bounded_planning_range(self):
        with self.assertRaises(ValidationError):
            ShotBriefDescription(
                idx=0, is_last=True, cam_idx=0, visual_desc="x",
                audio_desc="", duration_sec=30,
            )

    def test_rejects_overlapping_or_out_of_range_beats(self):
        common = dict(idx=0, is_last=True, cam_idx=0, visual_desc="x", audio_desc="", duration_sec=5)
        with self.assertRaises(ValidationError):
            ShotBriefDescription(**common, beats=[
                PerformanceBeat(start_sec=0, end_sec=3),
                PerformanceBeat(start_sec=2, end_sec=4),
            ])
        with self.assertRaises(ValidationError):
            ShotBriefDescription(**common, beats=[PerformanceBeat(start_sec=4, end_sec=6)])

    def test_chinese_mode_rejects_english_dominant_review_fields(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            visual_desc="A wide shot of <王云宝> entering a dark office from the left.",
            director_desc="0-5秒，固定广角镜头，王云宝从左侧进入办公室。",
            audio_desc="[音效] 窗外持续雨声。",
        )

        with self.assertRaisesRegex(ValueError, "visual_desc"):
            validate_chinese_review_fields(shot)

    def test_chinese_mode_accepts_chinese_review_fields(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            visual_desc="固定广角镜头，王云宝从画面左侧进入昏暗办公室。",
            director_desc="0-5秒，王云宝走入办公室，停下并望向桌面。",
            visual_style=["电影感现实主义", "冷蓝环境光"],
            avoid=["避免人物重影", "避免静态物体移动"],
            audio_desc="[音效] 窗外持续雨声。",
        )

        validate_chinese_review_fields(shot)

    def test_chinese_mode_reports_each_english_beat_field(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            visual_desc="固定广角镜头，人物从画面左侧进入办公室。",
            director_desc="0-5秒，人物走入办公室并停下。",
            beats=[PerformanceBeat(
                start_sec=0,
                end_sec=5,
                action="The actor enters from the left.",
                performance="Controlled breathing.",
                camera="Static wide shot.",
            )],
            audio_desc="[音效] 窗外持续雨声。",
        )

        self.assertEqual(
            set(chinese_review_field_issues(shot)),
            {"beats[0].action", "beats[0].performance", "beats[0].camera"},
        )

    def test_chinese_mode_ignores_machine_tags_and_character_identifiers(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            visual_desc="固定广角镜头，<E2E林夏-20260729>从画面左侧进入。",
            director_desc="0-5秒，林夏进入室内并停下。",
            audio_desc='[Speaker] <E2E林夏-20260729>（克制）：“等我。”',
        )

        validate_chinese_review_fields(shot)

    def test_chinese_mode_rejects_mixed_english_audio_description(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            visual_desc="固定广角镜头，人物从画面左侧进入。",
            director_desc="0-5秒，人物进入室内并停下。",
            audio_desc='[Sound Effect] Heavy rain and loud door latch. [Speaker] 林夏：“等我。”',
        )

        with self.assertRaisesRegex(ValueError, "audio_desc"):
            validate_chinese_review_fields(shot)


class TestVideoPromptCompiler(unittest.TestCase):
    def test_preserves_timing_performance_style_and_avoid_constraints(self):
        shot = ShotBriefDescription(
            idx=0,
            is_last=True,
            cam_idx=0,
            duration_sec=6,
            director_desc="0-3秒，他没有移开目光。",
            visual_desc="Close-up of <Lin> facing right.",
            beats=[
                PerformanceBeat(
                    start_sec=0, end_sec=3,
                    action="He maintains eye contact.",
                    performance="His tense shoulders slowly release.",
                ),
                PerformanceBeat(
                    start_sec=3, end_sec=6,
                    camera="Slow push-in to a tighter close-up",
                    action="A single tear moves down his cheek.",
                    performance="His lips tremble once; his breathing remains controlled.",
                ),
            ],
            visual_style=["cinematic", "shallow depth of field", "soft natural light"],
            avoid=["exaggerated crying", "fast cutting", "large gestures"],
            audio_desc='[Speaker] Lin (restrained): "等我。"',
        )

        prompt = compile_video_prompt(shot)

        self.assertIn("Planned duration: 6 seconds", prompt)
        self.assertLess(prompt.index("0-3s:"), prompt.index("3-6s:"))
        self.assertIn("His lips tremble once", prompt)
        self.assertIn("shallow depth of field", prompt)
        self.assertIn("Avoid: exaggerated crying; fast cutting; large gestures", prompt)
        self.assertNotIn("等我", prompt)


if __name__ == "__main__":
    unittest.main()
