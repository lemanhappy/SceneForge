from __future__ import annotations

from pathlib import Path
from typing import Sequence

import cv2
import numpy as np


def _read_sample_frames(samples: Sequence[dict], max_width: int = 320) -> list[np.ndarray]:
    frames = []
    for sample in samples:
        frame = cv2.imread(str(sample.get("path") or ""), cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            continue
        height, width = frame.shape[:2]
        if width > max_width:
            scale = float(max_width) / width
            frame = cv2.resize(
                frame,
                (max_width, max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        frames.append(frame)
    return frames


def extract_video_samples(
    video_path: str,
    output_dir: str,
    fractions: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0),
) -> list[dict]:
    """Extract deterministic timeline samples. Invalid videos fail open with []."""
    capture = cv2.VideoCapture(str(video_path))
    try:
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if not capture.isOpened() or frame_count <= 0:
            return []
        target = Path(output_dir)
        target.mkdir(parents=True, exist_ok=True)
        samples = []
        seen_frames = set()
        for fraction in fractions:
            normalized = max(0.0, min(1.0, float(fraction)))
            frame_index = round(normalized * (frame_count - 1))
            if frame_index in seen_frames:
                continue
            seen_frames.add(frame_index)
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue
            path = target / f"sample_{round(normalized * 100):03d}.jpg"
            if not cv2.imwrite(str(path), frame):
                continue
            samples.append({
                "fraction": normalized,
                "frame_index": frame_index,
                "time_seconds": (frame_index / fps) if fps > 0 else None,
                "path": str(path),
            })
        return samples
    finally:
        capture.release()


def analyze_sample_stability(
    samples: Sequence[dict],
    *,
    camera_locked: bool = False,
    expected_character_count: int | None = None,
) -> dict:
    """Measure clear temporal defects before asking a probabilistic critic.

    Median dense flow is dominated by the background in ordinary performance
    shots, so it distinguishes a moving actor from an unintended whole-frame
    slide. The gate intentionally requires both broad pixel change and repeated
    global displacement to avoid rejecting a focus pull or one moving subject.
    Face counts are advisory because mirrors, profiles, and occlusion make a
    classical detector unsuitable as an automatic rejection rule.
    """
    frames = _read_sample_frames(samples)

    if len(frames) < 2:
        return {
            "available": False,
            "consistent": True,
            "reason": "skipped (fewer than two readable samples)",
            "metrics": {},
            "issues": [],
            "failed": [],
        }

    pair_metrics = []
    for first, second in zip(frames, frames[1:]):
        if first.shape[:2] != second.shape[:2]:
            second = cv2.resize(second, (first.shape[1], first.shape[0]))
        first_gray = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)
        second_gray = cv2.cvtColor(second, cv2.COLOR_BGR2GRAY)
        flow = cv2.calcOpticalFlowFarneback(
            first_gray,
            second_gray,
            None,
            0.5,
            3,
            21,
            3,
            5,
            1.2,
            0,
        )
        magnitude = cv2.magnitude(flow[..., 0], flow[..., 1])
        difference = cv2.absdiff(first_gray, second_gray)
        pair_metrics.append({
            "median_flow_px": round(float(np.median(magnitude)), 4),
            "p90_flow_px": round(float(np.percentile(magnitude, 90)), 4),
            "changed_pixel_ratio": round(float(np.mean(difference >= 18)), 4),
        })

    drifting_pairs = [
        item for item in pair_metrics
        if item["median_flow_px"] >= 1.5 and item["changed_pixel_ratio"] >= 0.45
    ]
    repeated_global_drift = camera_locked and len(drifting_pairs) >= max(
        1, (len(pair_metrics) + 1) // 2
    )

    face_counts = []
    if expected_character_count is not None and expected_character_count >= 0:
        cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        detector = cv2.CascadeClassifier(cascade_path)
        if not detector.empty():
            for frame in frames:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = detector.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=5,
                    minSize=(24, 24),
                )
                face_counts.append(len(faces))

    issues = []
    failed = []
    if repeated_global_drift:
        issues.append({
            "code": "locked_camera_global_drift",
            "severity": "error",
            "message": "Repeated whole-frame displacement was detected in a locked-camera shot.",
        })
        failed.append("static_world")
    if (
        face_counts
        and expected_character_count is not None
        and sum(count > expected_character_count for count in face_counts) >= 2
    ):
        issues.append({
            "code": "possible_extra_face",
            "severity": "warning",
            "message": (
                "More frontal faces than expected appear in multiple samples; "
                "confirm with the visual critic because reflections can be valid."
            ),
        })

    metrics = {
        "pair_count": len(pair_metrics),
        "drifting_pair_count": len(drifting_pairs),
        "max_median_flow_px": max(item["median_flow_px"] for item in pair_metrics),
        "max_changed_pixel_ratio": max(item["changed_pixel_ratio"] for item in pair_metrics),
        "median_sharpness": round(float(np.median([
            cv2.Laplacian(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            for frame in frames
        ])), 4),
        "face_counts": face_counts,
        "pairs": pair_metrics,
    }
    return {
        "available": True,
        "consistent": not failed,
        "reason": issues[0]["message"] if failed else "deterministic checks passed",
        "metrics": metrics,
        "issues": issues,
        "failed": failed,
    }


def _face_crops(frame: np.ndarray) -> list[np.ndarray]:
    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return []
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    boxes = detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(24, 24),
    )
    return [frame[y:y + h, x:x + w] for x, y, w, h in boxes if w > 0 and h > 0]


def _face_signature(crop: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
    if crop is None or crop.size == 0:
        return None
    normalized = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
    gray = cv2.equalizeHist(cv2.cvtColor(normalized, cv2.COLOR_BGR2GRAY))
    dct = cv2.dct(gray.astype(np.float32) / 255.0)[:16, :16].reshape(-1)
    dct[0] = 0
    norm = float(np.linalg.norm(dct))
    if norm <= 1e-6:
        return None
    dct /= norm
    hsv = cv2.cvtColor(normalized, cv2.COLOR_BGR2HSV)
    histogram = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    cv2.normalize(histogram, histogram)
    return dct, histogram


def _face_signature_similarity(
    first: tuple[np.ndarray, np.ndarray],
    second: tuple[np.ndarray, np.ndarray],
) -> float:
    dct_score = max(0.0, min(1.0, (float(np.dot(first[0], second[0])) + 1.0) / 2.0))
    histogram_score = max(
        0.0,
        min(1.0, (float(cv2.compareHist(first[1], second[1], cv2.HISTCMP_CORREL)) + 1.0) / 2.0),
    )
    return 0.7 * dct_score + 0.3 * histogram_score


def analyze_reference_faces(
    samples: Sequence[dict],
    references: Sequence[tuple[str, str]],
) -> dict:
    """Return a lightweight, advisory identity signal with no model download.

    This is deliberately not called a face embedding: it combines normalized DCT
    structure and color histograms. It is useful for candidate ranking, while the
    configured visual critic remains authoritative for identity decisions.
    """
    frames = _read_sample_frames(samples, max_width=640)
    frame_signatures = []
    for frame in frames:
        frame_signatures.append([
            signature
            for crop in _face_crops(frame)
            if (signature := _face_signature(crop)) is not None
        ])

    subjects = {}
    issues = []
    for reference_path, name in references:
        reference = cv2.imread(str(reference_path), cv2.IMREAD_COLOR)
        if reference is None:
            continue
        crops = _face_crops(reference)
        if not crops:
            continue
        reference_signature = _face_signature(max(crops, key=lambda item: item.shape[0] * item.shape[1]))
        if reference_signature is None:
            continue
        similarities = []
        for signatures in frame_signatures:
            if signatures:
                similarities.append(max(
                    _face_signature_similarity(reference_signature, signature)
                    for signature in signatures
                ))
        if not similarities:
            continue
        median = float(np.median(similarities))
        subjects[str(name)] = {
            "comparable_sample_count": len(similarities),
            "median_similarity": round(median, 4),
            "minimum_similarity": round(min(similarities), 4),
        }
        if len(similarities) >= 2 and median < 0.42:
            issues.append({
                "code": "possible_identity_drift",
                "severity": "warning",
                "subject": str(name),
                "message": f"Local face signature for {name} changed substantially across samples.",
            })
    return {
        "available": bool(subjects),
        "subjects": subjects,
        "issues": issues,
    }


def _is_prop_reference(description: str) -> bool:
    lowered = str(description or "").lower()
    return lowered.startswith("[prop]") or "道具模型" in lowered or "prop model" in lowered


def _plausible_reference_projection(
    projected: np.ndarray,
    frame_shape: Sequence[int],
    *,
    inlier_matches: int,
) -> bool:
    """Reject weak or impossible homographies before they become drift signals."""
    if inlier_matches < 10:
        return False
    points = np.asarray(projected, dtype=np.float32).reshape(-1, 2)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        return False
    height, width = int(frame_shape[0]), int(frame_shape[1])
    if height <= 0 or width <= 0 or not cv2.isContourConvex(points):
        return False
    center = points.mean(axis=0)
    if not (0 <= center[0] <= width and 0 <= center[1] <= height):
        return False
    area_ratio = abs(float(cv2.contourArea(points))) / float(height * width)
    if area_ratio <= 0.00005 or area_ratio > 0.85:
        return False
    sides = np.linalg.norm(points - np.roll(points, -1, axis=0), axis=1)
    if float(np.min(sides)) < 2.0 or float(np.max(sides) / np.min(sides)) > 20.0:
        return False
    margin_x, margin_y = width * 0.25, height * 0.25
    return bool(
        np.all(points[:, 0] >= -margin_x)
        and np.all(points[:, 0] <= width + margin_x)
        and np.all(points[:, 1] >= -margin_y)
        and np.all(points[:, 1] <= height + margin_y)
    )


def _reference_track(reference_path: str, frames: Sequence[np.ndarray]) -> list[dict]:
    reference = cv2.imread(str(reference_path), cv2.IMREAD_GRAYSCALE)
    if reference is None or reference.size == 0:
        return []
    orb = cv2.ORB_create(
        nfeatures=900,
        edgeThreshold=8,
        patchSize=21,
        fastThreshold=10,
    )
    reference_points, reference_descriptors = orb.detectAndCompute(reference, None)
    if reference_descriptors is None or len(reference_points) < 10:
        return []
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    corners = np.float32([
        [0, 0],
        [reference.shape[1] - 1, 0],
        [reference.shape[1] - 1, reference.shape[0] - 1],
        [0, reference.shape[0] - 1],
    ]).reshape(-1, 1, 2)
    tracks = []
    for index, frame in enumerate(frames):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        frame_points, frame_descriptors = orb.detectAndCompute(gray, None)
        if frame_descriptors is None or len(frame_points) < 10:
            continue
        pairs = matcher.knnMatch(reference_descriptors, frame_descriptors, k=2)
        good = [first for first, second in pairs if first.distance < 0.75 * second.distance]
        if len(good) < 8:
            continue
        source = np.float32([reference_points[item.queryIdx].pt for item in good]).reshape(-1, 1, 2)
        target = np.float32([frame_points[item.trainIdx].pt for item in good]).reshape(-1, 1, 2)
        homography, mask = cv2.findHomography(source, target, cv2.RANSAC, 4.0)
        if homography is None or mask is None:
            continue
        inlier_matches = int(mask.sum())
        projected = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
        if not _plausible_reference_projection(
            projected,
            frame.shape,
            inlier_matches=inlier_matches,
        ):
            continue
        center = projected.mean(axis=0)
        area = abs(float(cv2.contourArea(projected.astype(np.float32))))
        tracks.append({
            "sample_index": index,
            "center_x": round(float(center[0] / frame.shape[1]), 4),
            "center_y": round(float(center[1] / frame.shape[0]), 4),
            "area_ratio": round(float(area / (frame.shape[0] * frame.shape[1])), 5),
            "inlier_matches": inlier_matches,
        })
    return tracks


def analyze_prop_references(
    samples: Sequence[dict],
    asset_references: Sequence[tuple[str, str]],
    *,
    prop_motion_allowed: bool = False,
    camera_locked: bool = True,
) -> dict:
    """Track textured bound props across samples when local features permit it."""
    frames = _read_sample_frames(samples, max_width=640)
    assets = {}
    issues = []
    for path, description in asset_references:
        if not _is_prop_reference(description):
            continue
        tracks = _reference_track(path, frames)
        if not tracks:
            continue
        centers = np.array([[item["center_x"], item["center_y"]] for item in tracks])
        origin = centers[0]
        max_displacement = float(np.max(np.linalg.norm(centers - origin, axis=1)))
        areas = [item["area_ratio"] for item in tracks if item["area_ratio"] > 0]
        scale_ratio = (max(areas) / min(areas)) if areas else 1.0
        key = Path(path).stem
        assets[key] = {
            "description": description,
            "matched_sample_count": len(tracks),
            "max_normalized_displacement": round(max_displacement, 4),
            "max_scale_ratio": round(scale_ratio, 4),
            "tracks": tracks,
        }
        if len(tracks) >= 3 and camera_locked and not prop_motion_allowed and (
            max_displacement > 0.08 or scale_ratio > 1.55
        ):
            issues.append({
                "code": "possible_static_prop_drift",
                "severity": "warning",
                "asset": key,
                "message": "A bound prop that has no planned motion changes position or scale across samples.",
            })
    return {"available": bool(assets), "assets": assets, "issues": issues}


def score_video_candidate(
    video_path: str,
    output_dir: str,
    *,
    camera_locked: bool = False,
    expected_character_count: int | None = None,
    character_references: Sequence[tuple[str, str]] = (),
    asset_references: Sequence[tuple[str, str]] = (),
    prop_motion_allowed: bool = False,
) -> dict:
    samples = extract_video_samples(video_path, output_dir)
    stability = analyze_sample_stability(
        samples,
        camera_locked=camera_locked,
        expected_character_count=expected_character_count,
    )
    identity = analyze_reference_faces(samples, character_references)
    props = analyze_prop_references(
        samples,
        asset_references,
        prop_motion_allowed=prop_motion_allowed,
        camera_locked=camera_locked,
    )
    warning_count = sum(
        len(item.get("issues") or []) for item in (stability, identity, props)
    )
    metrics = stability.get("metrics") or {}
    sharpness = float(metrics.get("median_sharpness") or 0.0)
    score = 0.5 + min(0.3, sharpness / 1000.0)
    if not stability.get("available"):
        score -= 0.2
    if not stability.get("consistent", True):
        score -= 0.6
    score -= min(0.2, warning_count * 0.04)
    if camera_locked:
        score -= min(0.15, float(metrics.get("max_median_flow_px") or 0.0) / 30.0)
    return {
        "path": str(video_path),
        "score": round(max(0.0, min(1.0, score)), 4),
        "consistent": bool(stability.get("consistent", True)),
        "warning_count": warning_count,
        "stability": stability,
        "identity_signal": identity,
        "prop_signal": props,
        "samples": samples,
    }


class VideoConsistencyAuditor:
    def __init__(self, critic, fractions: Sequence[float] | None = None) -> None:
        self.critic = critic
        self.fractions = tuple(fractions or critic.video_sample_fractions)

    async def audit(
        self,
        video_path: str,
        references: Sequence[tuple[str, str]],
        *,
        description: str,
        output_dir: str,
        camera_locked: bool = False,
        expected_character_count: int | None = None,
        asset_references: Sequence[tuple[str, str]] = (),
        prop_motion_allowed: bool = False,
    ) -> dict:
        samples = extract_video_samples(video_path, output_dir, self.fractions)
        if not samples:
            return {
                "score": 1.0,
                "consistent": True,
                "reason": "skipped (video sampling unavailable)",
                "dims": {},
                "failed": [],
                "failed_characters": [],
                "characters": {},
                "samples": [],
                "deterministic": {"available": False, "consistent": True, "issues": [], "failed": []},
                "identity_signal": {"available": False, "subjects": {}, "issues": []},
                "prop_signal": {"available": False, "assets": {}, "issues": []},
            }
        deterministic = analyze_sample_stability(
            samples,
            camera_locked=camera_locked,
            expected_character_count=expected_character_count,
        )
        identity_signal = analyze_reference_faces(samples, references)
        prop_signal = analyze_prop_references(
            samples,
            asset_references,
            prop_motion_allowed=prop_motion_allowed,
            camera_locked=camera_locked,
        )
        subjects = list(references) or [("", "scene")]
        character_verdicts = {}
        for reference_path, name in subjects:
            character_verdicts[name] = await self.critic.score_sequence(
                reference_path,
                samples,
                name=name,
                description=description,
            )
        result = _aggregate(character_verdicts, samples)
        result["deterministic"] = deterministic
        result["identity_signal"] = identity_signal
        result["prop_signal"] = prop_signal
        if not deterministic.get("consistent", True):
            result["consistent"] = False
            result["failed"] = sorted(set(result.get("failed") or []) | set(deterministic.get("failed") or []))
            reason = str(result.get("reason") or "").strip()
            result["reason"] = "; ".join(
                value for value in (reason, deterministic.get("reason")) if value
            )
        return result


def _aggregate(character_verdicts: dict, samples: list[dict]) -> dict:
    verdicts = list(character_verdicts.values())
    failed_characters = [name for name, verdict in character_verdicts.items()
                         if not verdict.get("consistent", True)]
    failed = sorted({item for verdict in verdicts for item in (verdict.get("failed") or [])})
    dim_keys = {key for verdict in verdicts for key in (verdict.get("dims") or {})}
    dims = {
        key: min(verdict["dims"][key] for verdict in verdicts if key in verdict.get("dims", {}))
        for key in dim_keys
    }
    return {
        "score": min((float(verdict.get("score", 1.0)) for verdict in verdicts), default=1.0),
        "consistent": not failed_characters,
        "reason": "; ".join(
            f"{name}: {verdict.get('reason', '')}" for name, verdict in character_verdicts.items()
            if not verdict.get("consistent", True)
        ),
        "dims": dims,
        "failed": failed,
        "failed_characters": failed_characters,
        "characters": character_verdicts,
        "samples": samples,
    }
