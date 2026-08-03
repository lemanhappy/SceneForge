"""Per-session cost *estimate* from produced artifacts.

Exact token/billing isn't tracked end-to-end, so this counts what was actually
generated (video clips, image frames, TTS lines, an LLM planning flat) and
multiplies by configurable unit prices. It's a transparent estimate for the
"成本" panel, not an invoice — every number is labelled as such.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from .quality_profiles import get_quality_profile

_DEFAULT_PRICES = {
    "video_clip": 3.0,   # per generated shot clip (Seedance) — the dominant cost
    "image": 0.1,        # per generated frame/portrait
    "tts_line": 0.03,    # per synthesized voiceover line
    "llm_flat": 0.3,     # flat per session for script/storyboard planning
}


class CostEstimator:
    def __init__(self, unit_prices: Dict[str, float] | None = None, currency: str = "¥",
                 quality_multipliers: Dict[str, float] | None = None):
        self.unit_prices = {**_DEFAULT_PRICES, **(unit_prices or {})}
        self.currency = currency
        self.quality_multipliers = {"economy": 0.7, "balanced": 1.0, "quality": 1.6,
                                    **(quality_multipliers or {})}

    @classmethod
    def from_config(cls, config: dict) -> "CostEstimator":
        section = (config or {}).get("cost") or {}
        return cls(unit_prices=section.get("unit_prices") or {}, currency=section.get("currency", "¥"),
                   quality_multipliers=section.get("quality_multipliers") or {})

    def _counts(self, working_dir: str) -> Dict[str, int]:
        root = Path(working_dir)
        if not root.is_dir():
            return {"video_clips": 0, "images": 0, "tts_lines": 0, "scenes": 0}
        video_clips = sum(1 for _ in root.rglob("shots/*/video.mp4"))
        images = sum(1 for _ in root.rglob("*.png"))
        tts_lines = sum(1 for _ in root.rglob("audio/line_*.mp3"))
        scenes = sum(1 for p in root.glob("scene_*") if p.is_dir()) or 1
        return {"video_clips": video_clips, "images": images, "tts_lines": tts_lines, "scenes": scenes}

    def estimate_plan(self, scenes: int, shots: int, quality_tier: str = "balanced") -> Dict[str, Any]:
        """Forward cost estimate from a storyboard's scene/shot counts, shown at
        the review gate *before* the expensive video stage runs. Candidate counts
        come from the same quality profile used by the render pipeline."""
        p = self.unit_prices
        profile = get_quality_profile(quality_tier)
        tier = profile.tier.value
        multiplier = max(0.0, float(self.quality_multipliers.get(tier, 1.0)))
        breakdown = {
            "video_clips": round(
                shots * profile.video_candidates * p["video_clip"] * multiplier,
                2,
            ),
            "images": round(
                shots * 2 * profile.image_candidates * p["image"] * multiplier,
                2,
            ),
            "llm_planning": round(max(1, scenes) * p["llm_flat"], 2),
        }
        total = round(sum(breakdown.values()), 2)
        uncertainty = max(0.1, total * 0.15)
        return {"currency": self.currency, "scenes": scenes, "shots": shots,
                "quality_tier": tier, "quality_multiplier": multiplier,
                "request_counts": {
                    "image_candidates_per_frame": profile.image_candidates,
                    "video_candidates_per_shot": profile.video_candidates,
                    "planned_image_generations": shots * 2 * profile.image_candidates,
                    "planned_video_generations": shots * profile.video_candidates,
                },
                "breakdown": breakdown, "estimated_total": total,
                "estimated_min": round(max(0.0, total - uncertainty), 2),
                "estimated_max": round(total + uncertainty, 2),
                "kind": "plan"}

    def plan_summary(self, scenes: int, shots: int, quality_tier: str = "balanced") -> str:
        """One-line human plan for a review-gate message."""
        est = self.estimate_plan(scenes, shots, quality_tier)
        return (f"📋 生产计划：{scenes} 场景 / {shots} 镜，预计成本 {est['currency']}{est['estimated_min']}"
                f"-{est['currency']}{est['estimated_max']}"
                f"（下一步「通过」将开始视频生成——最贵的一步）。")

    def estimate(self, working_dir: str) -> Dict[str, Any]:
        counts = self._counts(working_dir)
        p = self.unit_prices
        lines = {
            "video_clips": round(counts["video_clips"] * p["video_clip"], 2),
            "images": round(counts["images"] * p["image"], 2),
            "tts_lines": round(counts["tts_lines"] * p["tts_line"], 2),
            "llm_planning": round(counts["scenes"] * p["llm_flat"], 2),
        }
        total = round(sum(lines.values()), 2)
        return {
            "currency": self.currency,
            "counts": counts,
            "unit_prices": p,
            "breakdown": lines,
            "estimated_total": total,
            "note": "估算值：按已生成产物数量×单价推算，非实际账单。视频生成为主要成本。",
        }
