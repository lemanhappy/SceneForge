"""Generate a single punchy opening-hook line for short-video retention.

Used to auto-fill the first-3-seconds hook overlay when the user hasn't supplied
one. Deliberately tiny: one chat call, returns a single cleaned line.
"""

import re
from typing import List

_CN_CANDIDATES_PROMPT = (
    "你是抖音/短视频爆款钩子文案专家。基于下面的剧本/主题，写 {n} 条【风格各异】的开场钩子文案"
    "（用于视频前3秒画面文字，制造悬念或强烈反差）。每条不超过20字、口语化、有冲击力、不要书名号/引号。"
    "每行一条，前面加序号（1. 2. 3. …），只输出这 {n} 行，不要任何解释。\n\n剧本/主题：\n{src}"
)
_EN_CANDIDATES_PROMPT = (
    "You are a short-video hook copywriter. Based on the script/topic below, write {n} DISTINCT opening hook "
    "lines (overlaid on the first 3 seconds, creating suspense or contrast). Each <=12 words, punchy, no quotes. "
    "One per line, numbered (1. 2. 3. …), output only those {n} lines.\n\nScript/topic:\n{src}"
)
_CN_SELECT_PROMPT = (
    "下面是几条短视频开场钩子候选。选出最能抓住观众、提升完播率的【一条】，"
    "只回复它的序号数字，不要解释。\n\n{listing}"
)
_EN_SELECT_PROMPT = (
    "Below are candidate opening hooks for a short video. Pick the ONE most likely to grab viewers and boost "
    "completion. Reply with only its number.\n\n{listing}"
)

_CN_PROMPT = (
    "你是抖音/短视频爆款钩子文案专家。基于下面的剧本/主题，写【一句】开场钩子文案，"
    "用于视频前3秒的画面文字，目的是制造悬念或强烈反差、让观众忍不住看下去。"
    "要求：不超过20个字；口语化、有冲击力；不要书名号/引号；只输出这一句，不要任何解释。\n\n剧本/主题：\n{src}"
)
_EN_PROMPT = (
    "You are a short-video hook copywriter. Based on the script/topic below, write ONE opening hook line "
    "to overlay on the first 3 seconds. Create suspense or contrast so viewers keep watching. "
    "Max ~12 words, punchy, no quotes, output only the single line.\n\nScript/topic:\n{src}"
)


def _clean(text: str) -> str:
    text = (text or "").strip()
    # take first non-empty line; drop a leading label, then surrounding quotes/《》
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^(钩子文案?[:：]?|Hook[:：]?)\s*", "", line, flags=re.IGNORECASE).strip()
        return line.strip('“”"\'《》「」').strip()
    return ""


class HookWriter:
    def __init__(self, chat_model, extra_instruction: str = ""):
        self.chat_model = chat_model
        # Optional domain-specific hook guidance (agents.domain_packs); appended
        # to the prompt so the hook matches the vertical (短剧/解说/科普).
        self.extra_instruction = (extra_instruction or "").strip()

    def _with_domain(self, prompt: str) -> str:
        return f"{prompt}\n\n[领域要求] {self.extra_instruction}" if self.extra_instruction else prompt

    async def generate(self, source: str, chinese: bool = True) -> str:
        source = (source or "").strip()
        if not source or self.chat_model is None:
            return ""
        prompt = self._with_domain((_CN_PROMPT if chinese else _EN_PROMPT).format(src=source[:2000]))
        resp = await self.chat_model.ainvoke(prompt)
        return _clean(getattr(resp, "content", None) or str(resp))

    async def generate_candidates(self, source: str, n: int = 3, chinese: bool = True) -> List[str]:
        """Generate ``n`` distinct hook candidates in one call (best-of-N input)."""
        source = (source or "").strip()
        if not source or self.chat_model is None:
            return []
        if n <= 1:
            one = await self.generate(source, chinese)
            return [one] if one else []
        prompt = self._with_domain((_CN_CANDIDATES_PROMPT if chinese else _EN_CANDIDATES_PROMPT).format(n=n, src=source[:2000]))
        resp = await self.chat_model.ainvoke(prompt)
        text = getattr(resp, "content", None) or str(resp)
        out = []
        for raw in str(text).splitlines():
            line = re.sub(r"^\s*\d+[\.\、\)]\s*", "", raw.strip())  # strip "1." / "1、" / "1)"
            line = _clean(line)
            if line:
                out.append(line)
        return out[:n]

    async def select_best(self, candidates: List[str], chinese: bool = True) -> str:
        """Have the model pick the strongest candidate; fall back to the first."""
        if not candidates:
            return ""
        if len(candidates) == 1 or self.chat_model is None:
            return candidates[0]
        listing = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(candidates))
        try:
            resp = await self.chat_model.ainvoke((_CN_SELECT_PROMPT if chinese else _EN_SELECT_PROMPT).format(listing=listing))
            m = re.search(r"\d+", getattr(resp, "content", None) or str(resp))
            if m:
                idx = int(m.group(0)) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx]
        except Exception:
            pass
        return candidates[0]

    async def best(self, source: str, n: int = 3, chinese: bool = True) -> dict:
        """Generate N candidates and auto-select the best. Returns {chosen, candidates}."""
        candidates = await self.generate_candidates(source, n=n, chinese=chinese)
        chosen = await self.select_best(candidates, chinese=chinese) if candidates else ""
        return {"chosen": chosen, "candidates": candidates}
