"""Feature settings backend: read/write the pipeline configs' production toggles
(transition, subtitle, hook, cover, AIGC label, moderation) so they're editable
from the 设置 page instead of hand-editing YAML.

Schema-driven: each field has a dotted config path + type; values are written into
every pipeline yaml (idea2video / script2video) atomically under a per-file lock.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import yaml

from utils.atomic import atomic_write_text, file_lock

# Groups of editable fields. type: bool | number | text | select | list
# Optional per-field keys: hint (helper text), unit, and for bounded numbers
# min/max/step (frontend renders a slider). Each group has a category so the
# frontend can fold the long settings list into a few collapsible sections.
_SCHEMA: List[dict] = [
    {"title": "画面文字", "category": "文案与字幕", "fields": [
        # 目标语言 / 画面比例 是「每条视频」的选项 -> 放在「创作」页随视频走，不在全局设置里。
        {"path": "language.image_text_policy", "label": "画面文字策略", "type": "select",
         "options": ["essential", "none", "off"], "default": "essential",
         "hint": "图像模型画不好文字(尤其中文易乱码)。essential=仅剧情必需处(手机/PPT/招牌)出现简短文字(语言跟随该视频的目标语言)、抑制无谓背景文字(推荐)；"
                 "none=画面完全不出现文字(最干净，文字靠字幕/钩子叠加)；off=不约束。注：即使 essential，模型仍可能糊字"},
    ]},
    {"title": "配音与镜头节奏", "category": "文案与字幕", "fields": [
        {"path": "audio.fit_shot_to_speech", "label": "按配音补齐镜头时长", "type": "bool", "default": True,
         "hint": "开启后：某镜台词比画面长时，冻结末帧把该镜补到说完再切，避免『话没说完就切到下一镜』。关闭=按定长镜头(可能截断台词)"},
        {"path": "audio.fit_tail_pad", "label": "每句留白", "type": "number", "default": 0.4,
         "unit": "秒", "min": 0.0, "max": 2.0, "step": 0.1,
         "hint": "每句台词说完后额外多停留的时间，留点呼吸感；0=说完立刻可切"},
        {"path": "audio.max_shot_extend", "label": "单镜最多补齐", "type": "number", "default": 6.0,
         "unit": "秒", "min": 0.0, "max": 15.0, "step": 0.5,
         "min_label": "不补", "max_label": "可长定格",
         "hint": "单个镜头最多冻结/补齐多少秒，防某句超长导致末帧定格太久；超出则该句仍可能被下一镜接上"},
    ]},
    # 画面比例(画幅)是「每条视频」的选项 -> 在「创作」页随视频走，不在全局设置里。
    {"title": "转场", "category": "画面", "fields": [
        {"path": "video.transition.type", "label": "转场类型", "type": "select",
         "options": ["none", "crossfade", "dissolve", "fade"], "default": "none",
         "hint": "none=硬切(默认)；crossfade/dissolve=叠化；fade=黑场过渡"},
        {"path": "video.transition.duration", "label": "转场时长", "type": "number", "default": 0.5,
         "unit": "秒", "min": 0.2, "max": 2.0, "step": 0.1,
         "hint": "越长越柔和但越拖节奏；短视频建议 0.3–0.6 秒"},
    ]},
    {"title": "字幕样式", "category": "文案与字幕", "fields": [
        # 是否烧录字幕(开/关)是「每条视频」的选项 -> 在「创作」页随视频走；这里只配样式。
        {"path": "subtitle.burn_in", "label": "烧录进画面", "type": "bool", "default": True,
         "hint": "开=字幕烧进画面(平台通用)；关=另存外挂字幕文件"},
        {"path": "subtitle.style.font_size", "label": "字号", "type": "number", "default": 42,
         "unit": "px", "hint": "720p 下 36–48 较合适"},
        {"path": "subtitle.style.primary_color", "label": "字体颜色 (名称或 #RRGGBB)", "type": "text", "default": "white"},
        {"path": "subtitle.style.outline_color", "label": "描边颜色", "type": "text", "default": "black"},
        {"path": "subtitle.style.position", "label": "位置", "type": "select",
         "options": ["bottom", "middle", "top"], "default": "bottom"},
        {"path": "subtitle.style.margin_v", "label": "垂直边距", "type": "number", "default": 30,
         "unit": "px", "hint": "距画面上/下边的留白，越大离边越远"},
    ]},
    {"title": "开场钩子", "category": "文案与字幕", "fields": [
        {"path": "video.hook.enabled", "label": "启用前3秒钩子浮层", "type": "bool", "default": False},
        {"path": "video.hook.auto", "label": "LLM 自动生成钩子文案", "type": "bool", "default": True,
         "hint": "开=据剧本自动写钩子；关=用下方手填文案"},
        {"path": "video.hook.candidates", "label": "候选数", "type": "number", "default": 3,
         "min": 1, "max": 6, "step": 1,
         "hint": "1=直接生成一条；>1=生成多条再自动选最佳(更好但更费一点)"},
        {"path": "video.hook.text", "label": "钩子文案 (留空则自动生成)", "type": "text", "default": ""},
        {"path": "video.hook.seconds", "label": "显示秒数", "type": "number", "default": 3.0,
         "unit": "秒", "min": 1.0, "max": 6.0, "step": 0.5,
         "hint": "钩子文字停留多久。短视频前 2–3 秒最关键，一般 3 秒；太长会挡住画面"},
        {"path": "video.hook.position", "label": "位置", "type": "select",
         "options": ["top", "middle", "bottom"], "default": "top"},
        {"path": "video.hook.font_size", "label": "字号", "type": "number", "default": 72, "unit": "px",
         "hint": "钩子是大字标题，要醒目。720p 下 60–80 较合适，越大越抢眼"},
        {"path": "video.hook.color", "label": "颜色", "type": "text", "default": "#FFD24A"},
    ]},
    {"title": "封面海报", "category": "画面", "fields": [
        {"path": "video.cover.enabled", "label": "导出封面缩略图", "type": "bool", "default": False},
        {"path": "video.cover.at", "label": "取帧时间", "type": "number", "default": 0.0,
         "unit": "秒", "hint": "从成片第几秒抓一帧做封面；0=首帧"},
    ]},
    {"title": "AIGC 合规标识", "category": "合规与质量", "fields": [
        {"path": "compliance.aigc_label.enabled", "label": "启用 AIGC 角标", "type": "bool", "default": True,
         "hint": "网信办《标识办法》要求 AI 生成内容显著标识，建议保持开启"},
        {"path": "compliance.aigc_label.text", "label": "标识文字", "type": "text", "default": "AI生成"},
        {"path": "compliance.aigc_label.position", "label": "位置", "type": "select",
         "options": ["bottom_right", "bottom_left", "top_right", "top_left", "bottom_center", "top_center"],
         "default": "bottom_right"},
        {"path": "compliance.aigc_label.font_size", "label": "字号", "type": "number", "default": 30, "unit": "px",
         "hint": "合规角标是小字水印，不抢戏即可。720p 下 24–32 较合适"},
    ]},
    {"title": "生成预算（镜头/场景上限）", "category": "合规与质量", "fields": [
        {"path": "generation_budget.max_total_shots", "label": "镜头总数上限", "type": "number", "default": 20,
         "min": 1, "max": 100, "step": 1,
         "hint": "全片镜头总数的硬上限。视频生成成本随镜头数上升，超过此数会在分镜脚本『通过』时被拦下并提示，需先精简或调高此值。留 0/空=不限。"
                 "注：这是上限闸门，不决定实际生成多少；想控制数量请在创作页填『期望镜头数』"},
        {"path": "generation_budget.max_scenes", "label": "场景数上限", "type": "number", "default": 3,
         "min": 1, "max": 20, "step": 1,
         "hint": "全片场景数硬上限，超过会在『通过』时被拦下。场景越多镜头越多、成本越高"},
        {"path": "generation_budget.max_shot_regenerations", "label": "单镜最多重生成次数", "type": "number", "default": 2,
         "min": 0, "max": 5, "step": 1,
         "hint": "质量自评或手动重生成单镜时，同一镜头最多可重生成多少次，防止反复重试失控"},
    ]},
    {"title": "Skill 风格市场", "category": "扩展", "fields": [
        {"path": "skills.market_url", "label": "Skill 市场地址", "type": "text", "default": "",
         "hint": "外部 skill 市场的网址(你自建的网站 / GitHub 仓库)。创作页『风格 Skill』里的『浏览市场 ↗』按钮会打开它，"
                 "用户在那下载 .md 风格文件再上传回本工具。留空则隐藏该按钮。"},
    ]},
    {"title": "内容审核", "category": "合规与质量", "fields": [
        {"path": "moderation.enabled", "label": "启用内容审核", "type": "bool", "default": False},
        {"path": "moderation.keywords", "label": "敏感词 (逗号分隔)", "type": "list", "default": [],
         "hint": "命中任一词的主题/剧本会被拦截"},
    ]},
    {"title": "质量自评（人物一致性）", "category": "合规与质量", "fields": [
        {"path": "quality.consistency.enabled", "label": "启用人物一致性自动校验+重生成", "type": "bool", "default": False},
        {"path": "quality.consistency.threshold", "label": "一致性阈值", "type": "number", "default": 0.6,
         "min": 0.0, "max": 1.0, "step": 0.05, "min_label": "宽松", "max_label": "严格",
         "hint": "越接近 1 越严格：人物形象稍有变化就判不合格并自动重生成(更稳但更慢更费)；越接近 0 越宽松，几乎不重生成。建议 0.5–0.7"},
        {"path": "quality.consistency.max_retries", "label": "每镜最多重生成次数", "type": "number", "default": 1,
         "min": 0, "max": 3, "step": 1, "hint": "判不合格后最多自动重生成几次；越大越稳但越费"},
        {"path": "quality.consistency.aesthetic_threshold", "label": "画面质量阈值", "type": "number", "default": 0.0,
         "min": 0.0, "max": 1.0, "step": 0.05, "min_label": "关闭", "max_label": "严格",
         "hint": "0=关闭此项。>0 时额外检查画面质量(花屏/畸变/多手多脸/糊字)，低于该分数的镜头会自动重生成；越接近 1 越严格(更稳但更慢更费)。建议从 0.5 起调"},
        {"path": "quality.consistency.adherence_threshold", "label": "镜头贴合度阈值", "type": "number", "default": 0.0,
         "min": 0.0, "max": 1.0, "step": 0.05, "min_label": "关闭", "max_label": "严格",
         "hint": "0=关闭此项。>0 时额外检查画面是否符合分镜描述，跑题的镜头会自动重生成；越接近 1 越严格(更稳但更慢更费)。建议从 0.5 起调"},
        {"path": "quality.consistency.temporal_threshold", "label": "镜内连贯性阈值", "type": "number", "default": 0.0,
         "min": 0.0, "max": 1.0, "step": 0.05, "min_label": "关闭", "max_label": "严格",
         "hint": "0=关闭此项。>0 时检查同一镜头首帧与尾帧是否连贯(人物/场景一致、无变形跳变)，不连贯则重生成；仅对有首尾帧的运动镜头生效。越接近 1 越严格。建议从 0.5 起调"},
    ]},
]

_BY_PATH = {f["path"]: f for g in _SCHEMA for f in g["fields"]}
# Display order of the collapsible categories in the UI.
_CATEGORY_ORDER = ["画面", "文案与字幕", "合规与质量", "扩展"]


def _get_path(data: dict, path: str, default=None):
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(data: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _coerce(field: dict, value):
    t = field["type"]
    if t == "bool":
        return bool(value)
    if t == "number":
        try:
            f = float(value)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return field.get("default", 0)
    if t == "list":
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return [s.strip() for s in str(value or "").replace("、", ",").replace("\n", ",").split(",") if s.strip()]
    return "" if value is None else str(value)


def _display(field: dict, value):
    if field["type"] == "list":
        return "、".join(value) if isinstance(value, list) else str(value or "")
    return value


class FeaturesService:
    def __init__(self, config_paths: List[str] | None = None):
        self.config_paths = [Path(p) for p in (config_paths or
                             ["configs/idea2video.yaml", "configs/script2video.yaml"])]

    def _load(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            return {}
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            return {}

    def _first(self) -> Dict[str, Any]:
        for p in self.config_paths:
            if p.exists():
                return self._load(p)
        return {}

    # Field attributes forwarded to the frontend for rendering (besides value).
    _FIELD_KEYS = ("path", "label", "type", "hint", "unit", "min", "max", "step",
                   "min_label", "max_label")

    def get(self) -> dict:
        data = self._first()
        groups = []
        for g in _SCHEMA:
            fields = []
            for f in g["fields"]:
                raw = _get_path(data, f["path"], f.get("default"))
                fields.append({**{k: f[k] for k in self._FIELD_KEYS if k in f},
                               "options": f.get("options"),
                               "value": _display(f, raw)})
            groups.append({"title": g["title"], "category": g.get("category", ""), "fields": fields})
        # Also expose the category order so the UI can render collapsible sections.
        return {"groups": groups, "categories": _CATEGORY_ORDER}

    def update(self, values: Dict[str, Any]) -> dict:
        # coerce only known paths; ignore unknown keys
        coerced = {path: _coerce(_BY_PATH[path], val) for path, val in (values or {}).items() if path in _BY_PATH}
        for path in self.config_paths:
            if not path.exists():
                continue
            with file_lock(path):
                data = self._load(path)
                for p, v in coerced.items():
                    _set_path(data, p, v)
                atomic_write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
        return self.get()
