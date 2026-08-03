"""Generation budget guardrails (design §17).

Enforced where it matters most: before the expensive video stage, cap scenes and
total shots (video cost scales with shots). Global concurrency is enforced by
JobRunner. Empty/unset limits mean "no limit".
"""

from __future__ import annotations

from typing import Optional, Tuple


class BudgetGuard:
    def __init__(self, max_scenes: Optional[int] = None, max_total_shots: Optional[int] = None,
                 max_concurrent_generations: Optional[int] = None,
                 max_shot_regenerations: Optional[int] = None):
        self.max_scenes = max_scenes
        self.max_total_shots = max_total_shots
        self.max_concurrent_generations = max_concurrent_generations
        self.max_shot_regenerations = max_shot_regenerations

    @classmethod
    def from_config(cls, config: dict) -> "BudgetGuard":
        budget = (config or {}).get("generation_budget") or {}
        global_rl = ((config or {}).get("rate_limits") or {}).get("global") or {}
        return cls(
            max_scenes=budget.get("max_scenes"),
            max_total_shots=budget.get("max_total_shots"),
            max_concurrent_generations=global_rl.get("max_concurrent_generations"),
            max_shot_regenerations=budget.get("max_shot_regenerations"),
        )

    def check_render(self, scenes: int, total_shots: int) -> Tuple[bool, str]:
        """Gate the video stage on scene/shot counts. Returns (ok, message)."""
        if self.max_scenes and scenes > self.max_scenes:
            return False, f"场景数 {scenes} 超过上限 {self.max_scenes}，请先精简场景再进入视频生成。"
        if self.max_total_shots and total_shots > self.max_total_shots:
            return False, (f"镜头总数 {total_shots} 超过上限 {self.max_total_shots}（视频生成成本随镜头数上升）。"
                           f"请先减少镜头：回复『修改：减到 {self.max_total_shots} 个镜头以内』。")
        return True, ""
