"""Visual QA critic for generated shots.

Originally a character-consistency check (does the frame show the same person as
the fixed reference portrait?). Now a small multi-dimensional gate that can also
score, on the SAME VLM call:

  * identity   — same individual as the reference portrait (needs a reference)
  * aesthetic  — frame quality: sharp, well-composed, no distortion/garbled
                 faces/limbs/text, no glitches (needs only the frame)
  * adherence  — does the frame match the intended shot description? (needs the
                 description; no reference required)
  * temporal   — are the shot's first and last frame coherent (same character &
                 scene, no morphing/jarring jump)? (needs the last frame; a cheap
                 proxy for in-shot motion coherence)

Each dimension has its own threshold; a dimension with threshold 0 is disabled,
so the default config (identity only) behaves exactly as before. The combined
verdict fails if ANY enabled-and-parsed dimension scores below its threshold,
and the ``reason`` lists which dimensions failed — that string is fed back into
the shot's re-render prompt for a targeted (rather than blind) regeneration.

Fails OPEN (treats as passing) on any error / missing input / unparseable reply
so a flaky critic never blocks production.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)


class ConsistencyCritic:
    def __init__(self, chat_model: Any, threshold: float = 0.6,
                 aesthetic_threshold: float = 0.0, adherence_threshold: float = 0.0,
                 temporal_threshold: float = 0.0,
                 scene_threshold: float = 0.0,
                 video_sampling_enabled: bool = False,
                 video_sample_fractions: Sequence[float] = (0.0, 0.25, 0.5, 0.75, 1.0)):
        self.chat_model = chat_model
        self.threshold = float(threshold)
        # 0 (or less) disables the extra dimension; >0 sets the pass bar.
        self.aesthetic_threshold = max(0.0, float(aesthetic_threshold or 0.0))
        self.adherence_threshold = max(0.0, float(adherence_threshold or 0.0))
        self.temporal_threshold = max(0.0, float(temporal_threshold or 0.0))
        self.scene_threshold = max(0.0, float(scene_threshold or 0.0))
        self.video_sampling_enabled = bool(video_sampling_enabled)
        fractions = [max(0.0, min(1.0, float(value))) for value in video_sample_fractions]
        self.video_sample_fractions = tuple(dict.fromkeys(fractions)) or (0.0, 0.5, 1.0)

    @property
    def extra_dims_enabled(self) -> bool:
        """True if any non-identity dimension is on — these don't need a
        reference portrait, so the pipeline must also score reference-less shots."""
        return (self.aesthetic_threshold > 0 or self.adherence_threshold > 0
                or self.temporal_threshold > 0 or self.scene_threshold > 0)

    @classmethod
    def from_config(cls, config: dict, chat_model: Any) -> Optional["ConsistencyCritic"]:
        section = ((config or {}).get("quality") or {}).get("consistency") or {}
        if not section.get("enabled"):
            return None
        return cls(
            chat_model=chat_model,
            threshold=float(section.get("threshold", 0.6)),
            aesthetic_threshold=float(section.get("aesthetic_threshold", 0.0) or 0.0),
            adherence_threshold=float(section.get("adherence_threshold", 0.0) or 0.0),
            temporal_threshold=float(section.get("temporal_threshold", 0.0) or 0.0),
            scene_threshold=float(section.get("scene_threshold", 0.0) or 0.0),
            video_sampling_enabled=bool(section.get("video_sampling_enabled", False)),
            video_sample_fractions=section.get(
                "video_sample_fractions", (0.0, 0.25, 0.5, 0.75, 1.0)
            ),
        )

    @staticmethod
    def _data_uri(path: str) -> str:
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            return f"data:{mime};base64," + base64.b64encode(f.read()).decode("ascii")

    @staticmethod
    def _parse_field(text: str, name: str) -> Optional[float]:
        """Pull a 0..1 number for ``name`` out of the (possibly chatty) reply."""
        m = re.search(r'"?' + re.escape(name) + r'"?\s*[:=]\s*["\']?([0-9]*\.?[0-9]+)',
                      text or "", re.IGNORECASE)
        if not m:
            return None
        try:
            return max(0.0, min(1.0, float(m.group(1))))
        except ValueError:
            return None

    @classmethod
    def _parse_score(cls, text: str) -> Optional[float]:
        # Back-compat alias: identity similarity is reported under "score".
        return cls._parse_field(text, "score")

    @staticmethod
    def _parse_choice(text: str, name: str, choices: Sequence[str]) -> Optional[str]:
        pattern = r'"?' + re.escape(name) + r'"?\s*[:=]\s*["\']?([a-z_]+)'
        match = re.search(pattern, text or "", re.IGNORECASE)
        if not match:
            return None
        value = match.group(1).lower()
        return value if value in set(choices) else None

    async def _invoke_critic(self, message, label: str):
        """Retry one transient critic failure before falling open.

        Critic requests share the same flaky upstream as generation. A single
        connection timeout should not silently approve a visibly broken anchor.
        """
        from utils.retry import retry_async

        return await retry_async(
            lambda: self.chat_model.ainvoke([message]),
            attempts=2,
            base_wait=1.0,
            label=label,
        )

    @staticmethod
    def _temporal_instruction(description: str = "") -> str:
        intended = str(description or "").strip()[:400]
        guidance = (
            '"temporal": continuity across the SAME shot; no unexplained identity '
            "morphing, outfit change, jarring jump, or object popping. An object being "
            "picked up, carried, naturally occluded by the body, or leaving the frame with "
            "the character is NOT object popping when that action is expected by the shot. "
            "A handled prop may translate and rotate with the actor's hands and may change its "
            "projected size/orientation as the camera tracks; that physically continuous motion is "
            "not static-object drift. Judge whether it remains the same single prop and follows a "
            "continuous hand-to-counter path. "
            "Penalize duplicate instances of the same character, double exposure, ghost trails, "
            "or a second copy entering while the original remains visible. "
            "A single physically plausible mirror or window reflection that stays synchronized "
            "with its source character is not a duplicate person. Penalize it only when the copy "
            "has no reflecting surface, separates from the source, or moves independently. "
            "For a fixed/locked "
            "camera, also penalize any unexplained sliding, scaling, breathing, melting, or morphing "
            "of architecture, furniture, clocks, counters, or props that the action does not move. "
            "A clearly duplicated person or visibly drifting static world must score below 0.4."
        )
        if intended:
            guidance += " Judge expected motion and occlusion against this shot description: " + intended
        return guidance

    def _build_message(self, checks: list, images: list):
        """``images`` is an ordered list of (caption, path) to attach."""
        from langchain_core.messages import HumanMessage

        asks = [a for _k, _thr, a in checks]
        prompt = (
            "You are a strict visual QA critic for an AI-generated video shot. "
            "Score each requested dimension from 0 to 1 (1 = perfect). "
            "A single mirror/window reflection that is physically attached to a visible reflecting "
            "surface and moves synchronously with the real subject is not an extra character, extra "
            "face, identity drift, or an aesthetic defect; judge the primary physical subject. "
            "Reply with ONLY compact JSON containing the requested keys plus a short \"reason\". "
            "Provide: { " + ", ".join(asks) + ', "reason": "<short, name the worst problem>" }.'
        )
        content: list = [{"type": "text", "text": prompt}]
        for caption, path in images:
            content += [{"type": "text", "text": caption},
                        {"type": "image_url", "image_url": {"url": self._data_uri(path)}}]
        return HumanMessage(content=content)

    async def score(self, reference_path: str, frame_path: str,
                    name: str = "the character", description: str = "",
                    second_frame_path: str = "") -> dict:
        """Return ``{score, consistent, reason, dims, failed}``.

        ``score`` is the identity similarity (1.0 when identity isn't checked, for
        back-compat). ``dims`` maps each parsed dimension to its 0..1 value;
        ``failed`` lists the dimension keys that fell below their threshold.
        Missing model/frame, no enabled dimension, or an unparseable reply all
        fail OPEN so production never stalls.
        """
        has_ref = bool(reference_path) and os.path.exists(reference_path)
        has_last = bool(second_frame_path) and os.path.exists(second_frame_path)
        check_identity = has_ref
        check_aesthetic = self.aesthetic_threshold > 0
        check_adherence = self.adherence_threshold > 0 and bool(description)
        check_temporal = self.temporal_threshold > 0 and has_last

        if self.chat_model is None or not frame_path or not os.path.exists(frame_path) \
                or not (check_identity or check_aesthetic or check_adherence or check_temporal):
            return {"score": 1.0, "consistent": True, "reason": "skipped (no model/frame/dims)", "dims": {}, "failed": []}

        # Images are referenced by fixed captions: A=reference, B=frame, C=last frame.
        images: list = []
        if check_identity:
            images.append(("Reference portrait (Image A):", reference_path))
        images.append(("Frame to judge (Image B):", frame_path))
        if check_temporal:
            images.append(("The shot's final frame (Image C):", second_frame_path))

        # (json_key, threshold, prompt fragment)
        checks: list = []
        if check_identity:
            checks.append(("score", self.threshold,
                           f'"score": identity match to {name} in Image A '
                           "(same face, hairstyle, age, signature clothing; ignore pose, camera angle, lighting, background)"))
        if check_aesthetic:
            checks.append(("aesthetic", self.aesthetic_threshold,
                           '"aesthetic": quality of Image B (sharp and well-composed; no distortion, no garbled or '
                           "extra faces/hands/limbs, no warped text, no glitches or artifacts)"))
        if check_adherence:
            checks.append(("adherence", self.adherence_threshold,
                           '"adherence": how well Image B matches the intended shot description: '
                           + description.strip()[:300]))
        if check_temporal:
            checks.append(("temporal", self.temporal_threshold,
                           self._temporal_instruction(description)))

        try:
            resp = await self._invoke_critic(
                self._build_message(checks, images),
                "shot consistency critic",
            )
            text = getattr(resp, "content", None) or str(resp)
            if isinstance(text, list):  # some providers return content as parts
                text = " ".join(str(p.get("text", p) if isinstance(p, dict) else p) for p in text)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Consistency critic call failed (fail-open): %s", exc)
            return {"score": 1.0, "consistent": True, "reason": f"critic error: {exc}", "dims": {}, "failed": []}

        dims: dict = {}
        failed: list = []
        failures: list = []
        for key, thr, _ask in checks:
            val = self._parse_field(text, key)
            if val is None:
                continue  # unparseable dimension -> fail open for that dimension
            dims[key] = val
            if val < thr:
                failed.append(key)
                failures.append(f"{key} {val:.2f} below required {thr:.2f}")

        identity = dims.get("score")
        reason = str(text)[:200]
        if failures:
            reason = "; ".join(failures) + " | " + reason
        return {
            "score": identity if identity is not None else 1.0,
            "consistent": not failed,
            "reason": reason,
            "dims": dims,
            "failed": failed,
        }

    async def score_scene(
        self,
        anchor_path: str,
        frame_path: str,
        *,
        description: str = "",
        second_frame_path: str = "",
        same_camera: bool = True,
        camera_relation: str = "",
        anchor_description: str = "",
    ) -> dict:
        """Judge scene continuity while allowing intentional multi-camera coverage."""
        has_anchor = bool(anchor_path) and os.path.exists(anchor_path)
        has_frame = bool(frame_path) and os.path.exists(frame_path)
        has_last = bool(second_frame_path) and os.path.exists(second_frame_path)
        if self.chat_model is None or self.scene_threshold <= 0 or not has_anchor or not has_frame:
            return {
                "score": 1.0,
                "consistent": True,
                "reason": "skipped (no model/scene anchor/frame)",
                "dims": {},
                "failed": [],
                "repair_target": "none",
            }

        intended = str(description or "").strip()[:500]
        if same_camera:
            instruction = (
                '"scene": SAME-CAMERA continuity with Image A. Preserve the camera side and '
                "axis, coherent spatial projection, architecture, doors, windows, fixed furniture, "
                "major light sources, time, weather, and world layout. Shot size, lens, composition, "
                "pan, tilt, dolly, or zoom MAY change when explicitly described; do not require "
                "pixel alignment after an intended reframe or camera move."
            )
        else:
            instruction = (
                '"scene": SAME-WORLD continuity across an intentional camera change from '
                "Image A to Images B and C. Viewpoint, shot size, composition, and lens MAY "
                "change. Preserve the identity and relative topology of the location, fixed "
                "architecture, recognizable doors/windows/furniture, materials, time, weather, "
                "lighting motivation, and screen direction. Do not demand pixel alignment or "
                "the same visible object count when the new angle naturally hides elements."
            )
        instruction += (
            " Ignore character pose/expression and allow movable props or environmental "
            "changes explicitly required by the shot. A prop may correctly move from its support "
            "surface in Image A to a character's hand in Images B/C when the sequential shot "
            "intents describe picking it up, carrying it, or placing it elsewhere. That causal, "
            "scripted state progression is continuity, not teleportation. Only report object "
            "popping when no depicted or intended action explains the state change. Use the worst "
            "continuity across B and C."
        )
        relation = str(camera_relation or "").strip()[:300]
        if relation:
            instruction += " Intended camera relationship: " + relation
        anchor_intended = str(anchor_description or "").strip()[:500]
        if anchor_intended:
            instruction += " Anchor shot intent for Image A: " + anchor_intended
        if intended:
            instruction += " Current shot intent for Images B/C: " + intended
        instruction += (
            ', "repair_target": return "anchor" when Image A violates its own intent '
            'while B/C correctly follow the current intent, return "current" when B/C are '
            'the source of the continuity error, otherwise return "none". Pay special '
            'attention to movable-prop location, support surface, held/placed state, count, '
            "and ownership; do not blame a correct child shot for an incorrect anchor."
        )
        instruction += (
            ' The "scene" value MUST be a JSON number between 0 and 1. '
            '"repair_target" MUST be a separate JSON string. Example response: '
            '{"scene":0.92,"repair_target":"none","reason":"same location and valid prop progression"}.'
        )
        checks = [("scene", self.scene_threshold, instruction)]
        images = [
            ("Same-camera scene anchor (Image A):", anchor_path),
            ("Current shot first frame (Image B):", frame_path),
        ]
        if has_last:
            images.append(("Current shot final frame (Image C):", second_frame_path))

        message = self._build_message(checks, images)
        text = ""
        value = None
        misplaced_target = None
        try:
            for schema_attempt in range(2):
                response = await self._invoke_critic(
                    message,
                    "scene continuity critic",
                )
                text = getattr(response, "content", None) or str(response)
                if isinstance(text, list):
                    text = " ".join(
                        str(part.get("text", part) if isinstance(part, dict) else part)
                        for part in text
                    )
                value = self._parse_field(str(text), "scene")
                misplaced_target = self._parse_choice(
                    str(text), "scene", ("anchor", "current", "none")
                )
                if value is not None:
                    break
                if schema_attempt == 0:
                    logger.warning(
                        "Scene continuity critic returned an invalid scene score; retrying: %s",
                        str(text)[:200],
                    )
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Scene continuity critic call failed (fail-open): %s", exc)
            return {
                "score": 1.0,
                "consistent": True,
                "reason": f"critic error: {exc}",
                "dims": {},
                "failed": [],
                "repair_target": "none",
            }

        if value is None:
            if misplaced_target in ("anchor", "current"):
                value = 0.0
            else:
                return {
                    "score": 1.0,
                    "consistent": True,
                    "reason": str(text)[:200],
                    "dims": {},
                    "failed": [],
                    "repair_target": "none",
                }
        failed = ["scene"] if value < self.scene_threshold else []
        repair_target = self._parse_choice(
            str(text), "repair_target", ("anchor", "current", "none")
        )
        if repair_target is None and misplaced_target in ("anchor", "current"):
            repair_target = misplaced_target
        if repair_target is None:
            repair_target = "current" if failed else "none"
        if not failed:
            repair_target = "none"
        reason = str(text)[:200]
        if failed:
            reason = f"scene {value:.2f} below required {self.scene_threshold:.2f} | {reason}"
        return {
            "score": value,
            "consistent": not failed,
            "reason": reason,
            "dims": {"scene": value},
            "failed": failed,
            "repair_target": repair_target,
        }

    async def score_sequence(
        self,
        reference_path: str,
        samples: Sequence[dict],
        *,
        name: str = "the character",
        description: str = "",
    ) -> dict:
        """Judge all sampled moments of one character in one multimodal request."""
        usable = [sample for sample in samples if os.path.exists(str(sample.get("path") or ""))]
        has_ref = bool(reference_path) and os.path.exists(reference_path)
        if self.chat_model is None or not usable:
            return _open_sequence("skipped (no model/video samples)", usable)

        checks = []
        if has_ref:
            for index, sample in enumerate(usable):
                checks.append((
                    f"identity_{index}",
                    self.threshold,
                    f'"identity_{index}": identity match of the primary physical {name} in Sample '
                    f'{index} to Image A; ignore any physically plausible synchronized reflection',
                ))
        if self.aesthetic_threshold > 0:
            for index, sample in enumerate(usable):
                checks.append((
                    f"aesthetic_{index}",
                    self.aesthetic_threshold,
                    f'"aesthetic_{index}": visual quality of Sample {index}; do not count a '
                    "physically plausible synchronized reflection as an extra face or artifact",
                ))
        if self.adherence_threshold > 0 and description:
            checks.append((
                "adherence",
                self.adherence_threshold,
                '"adherence": how well all samples match this shot: ' + description.strip()[:300],
            ))
        if self.temporal_threshold > 0 and len(usable) > 1:
            checks.append((
                "temporal",
                self.temporal_threshold,
                self._temporal_instruction(description),
            ))
        if not checks:
            return _open_sequence("skipped (no enabled dimensions)", usable)

        images = []
        if has_ref:
            images.append(("Reference portrait (Image A):", reference_path))
        for index, sample in enumerate(usable):
            percent = round(float(sample.get("fraction", 0.0)) * 100)
            images.append((f"Video Sample {index} ({percent}%):", sample["path"]))
        try:
            response = await self._invoke_critic(
                self._build_message(checks, images),
                "video consistency critic",
            )
            text = getattr(response, "content", None) or str(response)
            if isinstance(text, list):
                text = " ".join(str(part.get("text", part) if isinstance(part, dict) else part)
                                for part in text)
        except Exception as exc:  # pragma: no cover - network dependent
            logger.warning("Video consistency critic call failed (fail-open): %s", exc)
            return _open_sequence(f"critic error: {exc}", usable)

        parsed = {key: self._parse_field(str(text), key) for key, _threshold, _ask in checks}
        identity_values = [value for key, value in parsed.items()
                           if key.startswith("identity_") and value is not None]
        aesthetic_values = [value for key, value in parsed.items()
                            if key.startswith("aesthetic_") and value is not None]
        dims = {}
        failed = []
        if identity_values:
            dims["score"] = min(identity_values)
            if dims["score"] < self.threshold:
                failed.append("score")
        if aesthetic_values:
            dims["aesthetic"] = min(aesthetic_values)
            if dims["aesthetic"] < self.aesthetic_threshold:
                failed.append("aesthetic")
        for key, threshold in (("adherence", self.adherence_threshold),
                               ("temporal", self.temporal_threshold)):
            value = parsed.get(key)
            if value is not None:
                dims[key] = value
                if value < threshold:
                    failed.append(key)
        sample_results = []
        for index, sample in enumerate(usable):
            sample_results.append({
                **sample,
                "identity": parsed.get(f"identity_{index}"),
                "aesthetic": parsed.get(f"aesthetic_{index}"),
            })
        return {
            "score": dims.get("score", 1.0),
            "consistent": not failed,
            "reason": str(text)[:300],
            "dims": dims,
            "failed": failed,
            "samples": sample_results,
        }


def _open_sequence(reason: str, samples: Sequence[dict]) -> dict:
    return {
        "score": 1.0,
        "consistent": True,
        "reason": reason,
        "dims": {},
        "failed": [],
        "samples": list(samples),
    }
