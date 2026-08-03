from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from infrastructure.sqlite.database import SQLiteDatabase


@dataclass(frozen=True, slots=True)
class ImportReport:
    source_path: str
    source_sha256: str
    project_count: int
    review_count: int
    active_session_id: str
    errors: tuple[str, ...] = field(default_factory=tuple)
    imported: bool = False
    already_imported: bool = False


class LegacySessionImporter:
    """Explicit, idempotent importer for the legacy ``sessions.json`` file."""

    def __init__(self, database: SQLiteDatabase) -> None:
        self.database = database

    def scan(self, source: str | Path) -> ImportReport:
        path = Path(source).resolve()
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        errors: list[str] = []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return ImportReport(str(path), digest, 0, 0, "", (f"invalid JSON: {exc}",))
        if not isinstance(payload, dict):
            return ImportReport(str(path), digest, 0, 0, "", ("top-level value must be an object",))
        sessions = payload.get("sessions", {})
        if not isinstance(sessions, dict):
            return ImportReport(str(path), digest, 0, 0, "", ("sessions must be an object",))
        review_count = 0
        for session_id, record in sessions.items():
            if not str(session_id).strip():
                errors.append("session id cannot be empty")
                continue
            if not isinstance(record, dict):
                errors.append(f"session {session_id!r} must be an object")
                continue
            if not str(record.get("working_dir") or "").strip():
                errors.append(f"session {session_id!r} is missing working_dir")
            reviews = record.get("review_tasks", []) or []
            if not isinstance(reviews, list) or any(not isinstance(item, dict) for item in reviews):
                errors.append(f"session {session_id!r} has invalid review_tasks")
            else:
                review_count += len(reviews)
        active = str(payload.get("active_session_id") or "")
        if active and active not in sessions:
            errors.append("active_session_id does not reference an existing session")
        return ImportReport(
            source_path=str(path),
            source_sha256=digest,
            project_count=len(sessions),
            review_count=review_count,
            active_session_id=active,
            errors=tuple(errors),
        )

    def import_file(
        self,
        source: str | Path,
        *,
        conflict_strategy: str = "fail",
    ) -> ImportReport:
        report = self.scan(source)
        if report.errors:
            raise ValueError("legacy session import validation failed: " + "; ".join(report.errors))
        strategy = str(conflict_strategy).strip().lower()
        if strategy not in {"fail", "skip", "replace"}:
            raise ValueError("conflict_strategy must be one of: fail, skip, replace")
        path = Path(report.source_path)
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        sessions: dict[str, dict[str, Any]] = payload.get("sessions", {})
        self.database.migrate()
        with self.database.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT imported_projects, imported_reviews FROM legacy_imports WHERE source_sha256 = ?",
                (report.source_sha256,),
            ).fetchone()
            if previous is not None:
                return ImportReport(
                    **{
                        **_report_values(report),
                        "project_count": int(previous["imported_projects"]),
                        "review_count": int(previous["imported_reviews"]),
                        "already_imported": True,
                    }
                )

            existing_ids = {
                row["project_id"]
                for row in connection.execute(
                    "SELECT project_id FROM projects WHERE project_id IN ({})".format(
                        ",".join("?" for _ in sessions)
                    ),
                    tuple(sessions),
                ).fetchall()
            } if sessions else set()
            if existing_ids and strategy == "fail":
                conflicts = ", ".join(sorted(existing_ids))
                raise ValueError(f"legacy import conflicts with existing projects: {conflicts}")

            imported_at = _utc_now()
            imported_projects = 0
            imported_reviews = 0
            for session_id, original in sessions.items():
                if session_id in existing_ids and strategy == "skip":
                    continue
                record = dict(original)
                record.setdefault("session_id", session_id)
                created_at = str(record.get("created_at") or imported_at)
                updated_at = str(record.get("updated_at") or created_at)
                connection.execute(
                    """
                    INSERT INTO projects(
                        project_id, legacy_session_id, working_dir, mode, title,
                        stage, revision, record_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    ON CONFLICT(project_id) DO UPDATE SET
                        legacy_session_id = excluded.legacy_session_id,
                        working_dir = excluded.working_dir,
                        mode = excluded.mode,
                        title = excluded.title,
                        stage = excluded.stage,
                        record_json = excluded.record_json,
                        updated_at = excluded.updated_at,
                        revision = projects.revision + 1
                    """,
                    (
                        session_id,
                        session_id,
                        str(record["working_dir"]),
                        str(record.get("mode") or "idea"),
                        str(record.get("idea") or record.get("user_requirement") or "")[:200],
                        str(record.get("stage") or "created"),
                        _dump_json(record),
                        created_at,
                        updated_at,
                    ),
                )
                imported_projects += 1
                connection.execute("DELETE FROM reviews WHERE project_id = ?", (session_id,))
                for ordinal, review in enumerate(record.get("review_tasks", []) or [], start=1):
                    review_id = str(review.get("review_id") or f"legacy_review_{ordinal}")
                    connection.execute(
                        """
                        INSERT INTO reviews(
                            project_id, review_id, stage, status, summary,
                            artifact_version, artifact_refs_json, created_at, resolved_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            session_id,
                            review_id,
                            str(review.get("stage") or ""),
                            str(review.get("status") or "pending"),
                            str(review.get("summary") or ""),
                            str(review.get("artifact_version") or "v1"),
                            _dump_json(review.get("artifact_refs", []) or []),
                            str(review.get("created_at") or created_at),
                            (str(review["resolved_at"]) if review.get("resolved_at") else None),
                        ),
                    )
                    imported_reviews += 1

            connection.execute(
                """
                INSERT INTO app_state(key, value_json, updated_at)
                VALUES ('active_session_id', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (_dump_json(report.active_session_id), imported_at),
            )
            connection.execute(
                """
                INSERT INTO legacy_imports(
                    source_sha256, source_path, imported_projects, imported_reviews, imported_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    report.source_sha256,
                    report.source_path,
                    imported_projects,
                    imported_reviews,
                    imported_at,
                ),
            )
        return ImportReport(**{**_report_values(report), "imported": True})


def _report_values(report: ImportReport) -> dict[str, Any]:
    return {
        "source_path": report.source_path,
        "source_sha256": report.source_sha256,
        "project_count": report.project_count,
        "review_count": report.review_count,
        "active_session_id": report.active_session_id,
        "errors": report.errors,
        "imported": report.imported,
        "already_imported": report.already_imported,
    }


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
