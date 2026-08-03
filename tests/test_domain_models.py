import unittest

from domain.projects import ProjectRecord
from domain.providers import (
    ExecutionMode,
    MediaType,
    ModelRequirement,
    ProviderCapability,
    ResumeStrategy,
)


class ProjectRecordTests(unittest.TestCase):
    def test_legacy_round_trip_preserves_unknown_fields(self):
        project = ProjectRecord.from_mapping(
            {"session_id": "p1", "working_dir": ".working_dir/p1", "domain": "costume", "stage": "created"}
        )
        value = project.to_legacy_dict()
        self.assertEqual(value["session_id"], "p1")
        self.assertEqual(value["domain"], "costume")


class ProviderCapabilityTests(unittest.TestCase):
    def test_capability_filters_hard_requirements(self):
        capability = ProviderCapability(
            provider_id="seedance",
            transport_id="yunwu",
            model_id="seedance-v1",
            media_type=MediaType.VIDEO,
            image_to_video=True,
            multi_reference=True,
            supported_aspect_ratios=("16:9", "9:16"),
            supported_durations=(5, 10),
            max_reference_count=3,
            execution_mode=ExecutionMode.ASYNC,
            resume_strategy=ResumeStrategy.REMOTE_TASK,
        )
        self.assertTrue(
            capability.supports(
                ModelRequirement(
                    media_type=MediaType.VIDEO,
                    image_to_video=True,
                    multi_reference=True,
                    aspect_ratio="9:16",
                    duration=5,
                    reference_count=2,
                )
            )
        )
        self.assertFalse(
            capability.supports(
                ModelRequirement(media_type=MediaType.VIDEO, image_to_video=True, duration=8, reference_count=1)
            )
        )
        self.assertFalse(
            capability.supports(
                ModelRequirement(media_type=MediaType.VIDEO, lora=True)
            )
        )
