import tempfile
import unittest
from pathlib import Path

from domain.artifacts import (
    ArtifactStatus,
    ArtifactType,
    ShotReadiness,
    compute_input_hash,
)
from infrastructure.sqlite import SQLiteArtifactRepository, SQLiteDatabase
from services.artifact_versions import ArtifactVersionService


def _insert_project(database: SQLiteDatabase, project_id: str = "project-1") -> None:
    with database.transaction(immediate=True) as connection:
        connection.execute(
            """
            INSERT INTO projects(
                project_id, legacy_session_id, working_dir, mode, title,
                stage, revision, record_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'idea', '', 'created', 0, '{}', 'now', 'now')
            """,
            (project_id, project_id, f".working_dir/{project_id}"),
        )


class InputHashTests(unittest.TestCase):
    def test_hash_is_order_independent_but_value_sensitive(self):
        left = compute_input_hash({"provider": "cloud", "params": {"b": 2, "a": 1}})
        right = compute_input_hash({"params": {"a": 1, "b": 2}, "provider": "cloud"})
        changed = compute_input_hash({"params": {"a": 1, "b": 3}, "provider": "cloud"})
        self.assertEqual(left, right)
        self.assertNotEqual(left, changed)


class SQLiteArtifactRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = SQLiteDatabase(self.root / ".sceneforge" / "sceneforge.db")
        self.repository = SQLiteArtifactRepository(self.database)
        _insert_project(self.database)

    def tearDown(self):
        self.tmp.cleanup()

    def _create(self, shot_index: int, artifact_type: ArtifactType, tag: str):
        return self.repository.create_version(
            "project-1",
            0,
            shot_index,
            artifact_type,
            input_hash=compute_input_hash(tag),
            relative_path=f"versions/shot-{shot_index}/{tag}.bin",
            inputs={"tag": compute_input_hash(tag)},
        )

    def test_single_shot_input_change_only_stales_its_downstream(self):
        shot_zero = [
            self._create(0, ArtifactType.STORYBOARD, "s0-storyboard"),
            self._create(0, ArtifactType.KEYFRAME, "s0-keyframe"),
            self._create(0, ArtifactType.VIDEO, "s0-video"),
        ]
        shot_one = [
            self._create(1, ArtifactType.STORYBOARD, "s1-storyboard"),
            self._create(1, ArtifactType.KEYFRAME, "s1-keyframe"),
            self._create(1, ArtifactType.VIDEO, "s1-video"),
        ]

        changed = self.repository.mark_inputs_changed(
            "project-1",
            0,
            0,
            ArtifactType.STORYBOARD,
            input_hash=compute_input_hash("edited storyboard"),
            reason="manual_edit",
        )

        self.assertEqual({item.artifact_id for item in changed}, {item.artifact_id for item in shot_zero})
        self.assertTrue(
            all(self.repository.get_version(item.artifact_id).status is ArtifactStatus.STALE
                for item in shot_zero)
        )
        self.assertTrue(
            all(self.repository.get_version(item.artifact_id).status is ArtifactStatus.ACTIVE
                for item in shot_one)
        )
        state = self.repository.get_shot_state("project-1", 0, 0)
        self.assertEqual(state.readiness, ShotReadiness.STALE)
        self.assertEqual(state.stale_reason, "manual_edit")

    def test_new_upstream_version_stales_only_strictly_downstream_types(self):
        first_storyboard = self._create(0, ArtifactType.STORYBOARD, "storyboard-v1")
        keyframe = self._create(0, ArtifactType.KEYFRAME, "keyframe-v1")
        video = self._create(0, ArtifactType.VIDEO, "video-v1")

        second_storyboard = self._create(0, ArtifactType.STORYBOARD, "storyboard-v2")

        self.assertEqual(second_storyboard.version, 2)
        self.assertEqual(
            self.repository.get_version(first_storyboard.artifact_id).status,
            ArtifactStatus.ARCHIVED,
        )
        self.assertEqual(self.repository.get_version(keyframe.artifact_id).status, ArtifactStatus.STALE)
        self.assertEqual(self.repository.get_version(video.artifact_id).status, ArtifactStatus.STALE)
        self.assertEqual(
            self.repository.get_version(second_storyboard.artifact_id).status,
            ArtifactStatus.ACTIVE,
        )


class ArtifactVersionServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.database = SQLiteDatabase(self.root / ".sceneforge" / "sceneforge.db")
        self.repository = SQLiteArtifactRepository(self.database)
        _insert_project(self.database)
        self.service = ArtifactVersionService(self.repository, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_version_can_be_viewed_and_rolled_back(self):
        live = self.root / ".working_dir" / "project-1" / "scene_0" / "shots" / "0" / "video.mp4"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"video-version-one")
        first = self.service.record_file(
            "project-1",
            0,
            0,
            ArtifactType.VIDEO,
            live,
            input_values={"prompt": "wait for me", "model": "cloud-v1"},
        )
        live.write_bytes(b"video-version-two")
        second = self.service.record_file(
            "project-1",
            0,
            0,
            ArtifactType.VIDEO,
            live,
            input_values={"prompt": "wait for me quietly", "model": "cloud-v1"},
        )

        self.assertEqual(self.service.resolve_version_path(first.artifact_id).read_bytes(), b"video-version-one")
        self.assertEqual(self.service.resolve_version_path(second.artifact_id).read_bytes(), b"video-version-two")

        active = self.service.rollback(first.artifact_id)

        self.assertEqual(active.artifact_id, first.artifact_id)
        self.assertEqual(active.status, ArtifactStatus.ACTIVE)
        self.assertEqual(live.read_bytes(), b"video-version-one")
        history = self.service.list_versions("project-1", 0, 0, ArtifactType.VIDEO)
        self.assertEqual([item.version for item in history], [2, 1])
        self.assertEqual(history[0].status, ArtifactStatus.ARCHIVED)
        self.assertEqual(history[1].status, ArtifactStatus.ACTIVE)

    def test_identical_keyframe_is_recorded_once(self):
        live = self.root / ".working_dir" / "project-1" / "scene_0" / "shots" / "0" / "first_frame.png"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"one-keyframe")

        first = self.service.record_file(
            "project-1", 0, 0, ArtifactType.KEYFRAME, live,
            input_values={"source": "preview"},
        )
        repeated = self.service.record_file(
            "project-1", 0, 0, ArtifactType.KEYFRAME, live,
            input_values={"source": "video-stage"},
        )

        self.assertEqual(repeated.artifact_id, first.artifact_id)
        self.assertEqual(
            len(self.service.list_versions("project-1", 0, 0, ArtifactType.KEYFRAME)),
            1,
        )
        snapshots = list((self.root / ".sceneforge" / "artifact_versions").rglob("*.png"))
        self.assertEqual(len(snapshots), 1)

    def test_changed_keyframe_creates_one_new_version(self):
        live = self.root / ".working_dir" / "project-1" / "scene_0" / "shots" / "0" / "first_frame.png"
        live.parent.mkdir(parents=True)
        live.write_bytes(b"first-keyframe")
        first = self.service.record_file(
            "project-1", 0, 0, ArtifactType.KEYFRAME, live)

        live.write_bytes(b"second-keyframe")
        second = self.service.record_file(
            "project-1", 0, 0, ArtifactType.KEYFRAME, live)

        self.assertNotEqual(second.artifact_id, first.artifact_id)
        versions = self.service.list_versions(
            "project-1", 0, 0, ArtifactType.KEYFRAME)
        self.assertEqual([item.version for item in versions], [2, 1])

    def test_external_media_root_can_be_versioned_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as external:
            external_root = Path(external)
            service = ArtifactVersionService(
                self.repository,
                self.root,
                external_roots_provider=lambda: [external_root],
            )
            live = external_root / "project-1" / "scene_0" / "shots" / "0" / "first_frame.png"
            live.parent.mkdir(parents=True)
            live.write_bytes(b"external-v1")
            first = service.record_file("project-1", 0, 0, ArtifactType.KEYFRAME, live)
            live.write_bytes(b"external-v2")
            service.record_file("project-1", 0, 0, ArtifactType.KEYFRAME, live)

            service.rollback(first.artifact_id)

            self.assertEqual(live.read_bytes(), b"external-v1")
            self.assertTrue(service.resolve_version_path(first.artifact_id).is_file())

    def test_legacy_duplicate_keyframes_are_collapsed_when_listed(self):
        version_dir = self.root / "versions" / "keyframes"
        version_dir.mkdir(parents=True)
        first_path = version_dir / "first.png"
        second_path = version_dir / "second.png"
        first_path.write_bytes(b"same-keyframe")
        second_path.write_bytes(b"same-keyframe")
        common = {
            "project_id": "project-1",
            "scene_index": 0,
            "shot_index": 0,
            "artifact_type": ArtifactType.KEYFRAME,
            "input_hash": compute_input_hash("same"),
        }
        self.repository.create_version(
            **common, relative_path="versions/keyframes/first.png")
        current = self.repository.create_version(
            **common, relative_path="versions/keyframes/second.png")

        versions = self.service.list_versions(
            "project-1", 0, 0, ArtifactType.KEYFRAME)

        self.assertEqual([item.artifact_id for item in versions], [current.artifact_id])

    def test_paths_outside_workspace_are_rejected(self):
        outside = self.root.parent / "outside-artifact.mp4"
        outside.write_bytes(b"x")
        try:
            with self.assertRaisesRegex(ValueError, "within the workspace"):
                self.service.record_file(
                    "project-1", 0, 0, ArtifactType.VIDEO, outside)
        finally:
            outside.unlink(missing_ok=True)

    def test_storyboard_rollback_replaces_only_the_selected_shot(self):
        live = self.root / ".working_dir" / "project-1" / "scene_0" / "storyboard.json"
        live.parent.mkdir(parents=True)
        first_scene = [
            {"idx": 0, "visual_desc": "old shot zero"},
            {"idx": 1, "visual_desc": "shot one"},
        ]
        import json

        live.write_text(json.dumps(first_scene, ensure_ascii=False), encoding="utf-8")
        old_zero = self.service.record_json_item(
            "project-1",
            0,
            0,
            first_scene[0],
            live_path=live,
            input_values={"shot": first_scene[0]},
        )
        edited_scene = [
            {"idx": 0, "visual_desc": "new shot zero"},
            {"idx": 1, "visual_desc": "shot one edited independently"},
        ]
        live.write_text(json.dumps(edited_scene, ensure_ascii=False), encoding="utf-8")
        self.service.record_json_item(
            "project-1",
            0,
            0,
            edited_scene[0],
            live_path=live,
            input_values={"shot": edited_scene[0]},
        )

        self.service.rollback(old_zero.artifact_id)

        rolled_back = json.loads(live.read_text(encoding="utf-8"))
        self.assertEqual(rolled_back[0]["visual_desc"], "old shot zero")
        self.assertEqual(rolled_back[1]["visual_desc"], "shot one edited independently")


class WorkflowArtifactInvalidationTests(unittest.TestCase):
    def test_manual_edit_keeps_unrelated_shot_files_and_versions_active(self):
        import json

        from agent_runtime.session_factory import create_session_index
        from interfaces import ShotBriefDescription
        from services.workflow_engine import WorkflowEngine

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = create_session_index(root, auto_import_legacy=False)
            engine = WorkflowEngine(index, root)
            sid = index.create(idea="two shots")["session_id"]
            index.update_stage(sid, "storyboard_review_pending", "review")
            scene = index.working_dir(sid) / "idea2video" / "scene_0"
            scene.mkdir(parents=True, exist_ok=True)
            shots = [
                ShotBriefDescription(
                    idx=0, cam_idx=0, is_last=False, visual_desc="shot zero", audio_desc=""
                ).model_dump(),
                ShotBriefDescription(
                    idx=1, cam_idx=1, is_last=True, visual_desc="shot one", audio_desc=""
                ).model_dump(),
            ]
            storyboard = scene / "storyboard.json"
            storyboard.write_text(json.dumps(shots, ensure_ascii=False), encoding="utf-8")
            (scene / "camera_tree.json").write_text("[]", encoding="utf-8")
            for shot_index in (0, 1):
                shot_dir = scene / "shots" / str(shot_index)
                shot_dir.mkdir(parents=True)
                (shot_dir / "first_frame.png").write_bytes(f"frame-{shot_index}".encode())
                (shot_dir / "video.mp4").write_bytes(f"video-{shot_index}".encode())
                engine.artifact_versions.record_file(
                    sid, 0, shot_index, ArtifactType.KEYFRAME,
                    shot_dir / "first_frame.png", input_values={"shot": shot_index})
                engine.artifact_versions.record_file(
                    sid, 0, shot_index, ArtifactType.VIDEO,
                    shot_dir / "video.mp4", input_values={"shot": shot_index})

            edited = [dict(shots[0], visual_desc="shot zero edited"), shots[1]]
            result = engine.edit_storyboard(
                sid, [{"scene_index": 0, "shots": edited}])

            self.assertTrue(result["ok"])
            self.assertFalse((scene / "shots" / "0").exists())
            self.assertTrue((scene / "shots" / "1" / "video.mp4").exists())
            shot_one_video = engine.artifact_versions.list_versions(
                sid, 0, 1, ArtifactType.VIDEO)[0]
            self.assertEqual(shot_one_video.status, ArtifactStatus.ACTIVE)

    def test_single_shot_regeneration_records_dependency_versions(self):
        import json

        from agent_runtime.session_factory import create_session_index
        from services import JobRunner, ProductionService
        from services.workflow_engine import WorkflowEngine

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = create_session_index(root, auto_import_legacy=False)
            engine = WorkflowEngine(index, root)
            sid = index.create(idea="dependent shots")["session_id"]
            scene = index.working_dir(sid) / "idea2video" / "scene_0"
            scene.mkdir(parents=True, exist_ok=True)
            (scene / "camera_tree.json").write_text(
                json.dumps([{"idx": 0, "active_shot_idxs": [0, 1]}]), encoding="utf-8")
            for shot_index in (0, 1):
                shot_dir = scene / "shots" / str(shot_index)
                shot_dir.mkdir(parents=True)
                (shot_dir / "shot_description.json").write_text(
                    json.dumps({"idx": shot_index, "prompt": "old"}), encoding="utf-8")
                (shot_dir / "first_frame.png").write_bytes(f"old-frame-{shot_index}".encode())
                (shot_dir / "video.mp4").write_bytes(f"old-video-{shot_index}".encode())
                engine.artifact_versions.record_file(
                    sid, 0, shot_index, "keyframe", shot_dir / "first_frame.png",
                    input_values={"generation": "old"})
                engine.artifact_versions.record_file(
                    sid, 0, shot_index, "video", shot_dir / "video.mp4",
                    input_values={"generation": "old"})
                (shot_dir / "first_frame.png").write_bytes(f"new-frame-{shot_index}".encode())
                (shot_dir / "video.mp4").write_bytes(f"new-video-{shot_index}".encode())

            service = ProductionService(engine, JobRunner())
            service._record_regenerated_versions(
                {"session_id": sid, "scene_index": 0, "shot_idx": 0, "keep_description": True},
                {"scene_index": 0},
            )

            for shot_index in (0, 1):
                keyframes = engine.artifact_versions.list_versions(
                    sid, 0, shot_index, ArtifactType.KEYFRAME)
                videos = engine.artifact_versions.list_versions(
                    sid, 0, shot_index, ArtifactType.VIDEO)
                self.assertEqual([item.version for item in keyframes], [2, 1])
                self.assertEqual([item.version for item in videos], [2, 1])
                self.assertEqual(keyframes[0].status, ArtifactStatus.ACTIVE)
                self.assertEqual(videos[0].status, ArtifactStatus.ACTIVE)
