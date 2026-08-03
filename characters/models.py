from pydantic import BaseModel, Field, model_validator
from typing import Dict, List, Literal, Optional, Union


class IdentityProfile(BaseModel):
    facial_features: str = ""
    hairstyle: str = ""
    body_features: str = ""
    age_range: str = ""
    signature_features: List[str] = Field(default_factory=list)
    forbidden_changes: List[str] = Field(default_factory=list)

    def prompt_constraint(self) -> str:
        stable = [
            self.facial_features,
            self.hairstyle,
            self.body_features,
            self.age_range,
            *self.signature_features,
        ]
        parts = [str(item).strip() for item in stable if str(item).strip()]
        forbidden = [str(item).strip() for item in self.forbidden_changes if str(item).strip()]
        if forbidden:
            parts.append("Never change: " + "; ".join(forbidden))
        return ". ".join(parts)


class VoiceProfile(BaseModel):
    provider_voice_id: Optional[str] = None
    vocal_quality: str = ""
    speaking_style: str = ""
    accent: str = ""
    language: str = ""
    forbidden_changes: List[str] = Field(default_factory=list)

    def prompt_constraint(self) -> str:
        parts = [
            f"voice id: {self.provider_voice_id}" if self.provider_voice_id else "",
            f"vocal quality: {self.vocal_quality}" if self.vocal_quality.strip() else "",
            f"speaking style: {self.speaking_style}" if self.speaking_style.strip() else "",
            f"accent: {self.accent}" if self.accent.strip() else "",
            f"language: {self.language}" if self.language.strip() else "",
        ]
        forbidden = [str(item).strip() for item in self.forbidden_changes if str(item).strip()]
        if forbidden:
            parts.append("Never change voice traits: " + "; ".join(forbidden))
        return ". ".join(part for part in parts if part)


class CharacterBible(BaseModel):
    personality_traits: List[str] = Field(default_factory=list)
    behavioral_notes: str = ""
    continuity_notes: str = ""
    voice: VoiceProfile = Field(default_factory=VoiceProfile)

    def prompt_constraint(self) -> str:
        parts = []
        traits = [str(item).strip() for item in self.personality_traits if str(item).strip()]
        if traits:
            parts.append("personality: " + ", ".join(traits))
        if self.behavioral_notes.strip():
            parts.append("behavior: " + self.behavioral_notes.strip())
        if self.continuity_notes.strip():
            parts.append("continuity: " + self.continuity_notes.strip())
        voice = self.voice.prompt_constraint()
        if voice:
            parts.append(voice)
        return ". ".join(parts)


class SceneBible(BaseModel):
    spatial_layout: str = ""
    fixed_objects: List[str] = Field(default_factory=list)
    lighting: str = ""
    time_of_day: str = ""
    weather: str = ""
    color_palette: List[str] = Field(default_factory=list)
    forbidden_changes: List[str] = Field(default_factory=list)

    def prompt_constraint(self) -> str:
        fields = (
            ("spatial layout", self.spatial_layout),
            ("fixed objects", ", ".join(self.fixed_objects)),
            ("lighting", self.lighting),
            ("time of day", self.time_of_day),
            ("weather", self.weather),
            ("color palette", ", ".join(self.color_palette)),
            ("never change", "; ".join(self.forbidden_changes)),
        )
        return ". ".join(f"{label}: {value.strip()}" for label, value in fields if value.strip())


class PropBible(BaseModel):
    appearance: str = ""
    materials: List[str] = Field(default_factory=list)
    colors: List[str] = Field(default_factory=list)
    ownership: str = ""
    initial_location: str = ""
    condition: str = ""
    forbidden_changes: List[str] = Field(default_factory=list)

    def prompt_constraint(self) -> str:
        fields = (
            ("appearance", self.appearance),
            ("materials", ", ".join(self.materials)),
            ("colors", ", ".join(self.colors)),
            ("owner", self.ownership),
            ("initial location", self.initial_location),
            ("condition", self.condition),
            ("never change", "; ".join(self.forbidden_changes)),
        )
        return ". ".join(f"{label}: {value.strip()}" for label, value in fields if value.strip())


class ReferenceSet(BaseModel):
    reference_set_id: str
    name: str = "Default"
    outfit_version_id: Optional[str] = None
    images: Dict[str, str] = Field(default_factory=dict)
    expressions: Dict[str, str] = Field(default_factory=dict)
    is_default: bool = False

    def all_images(self) -> Dict[str, str]:
        return {**self.images, **{f"expression_{key}": value for key, value in self.expressions.items()}}


class OutfitVersion(BaseModel):
    outfit_version_id: str
    name: str
    description: str = ""
    reference_set_ids: List[str] = Field(default_factory=list)
    is_default: bool = False


class LoRABinding(BaseModel):
    kind: Literal["lora"] = "lora"
    binding_id: str
    enabled: bool = False
    provider: Optional[str] = None
    base_model: str = ""
    model_path: str = ""
    trigger_words: List[str] = Field(default_factory=list)
    weight: float = Field(default=0.8, ge=0.0, le=2.0)


class ProviderCharacterBinding(BaseModel):
    kind: Literal["provider_character_id"] = "provider_character_id"
    binding_id: str
    enabled: bool = True
    provider: str
    character_id: str
    model_id: Optional[str] = None


class ThreeDModelBinding(BaseModel):
    kind: Literal["three_d_model"] = "three_d_model"
    binding_id: str
    enabled: bool = False
    model_path: str
    rig: Optional[str] = None


RenderBinding = Union[LoRABinding, ProviderCharacterBinding, ThreeDModelBinding]


class CharacterAsset(BaseModel):
    """A fixed character asset (IP) that can be reused across videos.

    ``type`` is retained for old YAML files. New records keep references,
    outfits, and render bindings side-by-side, so LoRA is an optional binding
    instead of a mutually-exclusive character type.
    """

    asset_id: str = Field(description="Unique id used to bind this asset to a scene character.")
    display_name: str = Field(description="Human-facing name, e.g. 林老师.")
    aliases: List[str] = Field(default_factory=list, description="Alternate names used for matching.")
    type: Literal[
        "reference_images",
        "lora",
        "three_d_model",
        "provider_character_id",
    ] = "reference_images"
    description: str = Field(default="", description="Appearance description injected as a constraint.")
    visual_prompt: Optional[str] = Field(default=None, description="Optional English visual prompt for generators.")
    # view name (front/side/back/smile/...) -> image path (resolved to absolute by the registry)
    assets: Dict[str, str] = Field(default_factory=dict)
    provider: Optional[str] = None
    model_id: Optional[str] = None
    trigger_words: List[str] = Field(default_factory=list)
    identity_profile: IdentityProfile = Field(default_factory=IdentityProfile)
    bible: CharacterBible = Field(default_factory=CharacterBible)
    reference_sets: List[ReferenceSet] = Field(default_factory=list)
    outfit_versions: List[OutfitVersion] = Field(default_factory=list)
    render_bindings: List[RenderBinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _bridge_legacy_assets(self):
        if self.assets and not self.reference_sets:
            self.reference_sets = [
                ReferenceSet(
                    reference_set_id="default",
                    name="Default",
                    images=dict(self.assets),
                    is_default=True,
                )
            ]
        elif self.reference_sets and not self.assets:
            selected = self.default_reference_set()
            if selected is not None:
                self.assets = dict(selected.images)

        # Convert old single-mode LoRA metadata into a disabled professional
        # binding without removing any reference images on the same character.
        if self.type == "lora" and not any(item.kind == "lora" for item in self.render_bindings):
            self.render_bindings.append(
                LoRABinding(
                    binding_id="legacy_lora",
                    enabled=False,
                    provider=self.provider,
                    base_model=self.model_id or "",
                    trigger_words=list(self.trigger_words),
                )
            )
        return self

    def default_reference_set(self, outfit_version_id: str | None = None) -> ReferenceSet | None:
        candidates = self.reference_sets
        if outfit_version_id:
            matched = [item for item in candidates if item.outfit_version_id == outfit_version_id]
            if matched:
                candidates = matched
        return next((item for item in candidates if item.is_default), None) or (candidates[0] if candidates else None)

    def enabled_render_bindings(self, kind: str | None = None) -> List[RenderBinding]:
        return [
            item for item in self.render_bindings
            if item.enabled and (kind is None or item.kind == kind)
        ]

    def reference_images(self, outfit_version_id: str | None = None) -> Dict[str, str]:
        reference_set = self.default_reference_set(outfit_version_id)
        if reference_set is not None:
            images = reference_set.all_images()
            if images:
                return images
        return dict(self.assets)

    def identity_constraint(self) -> str:
        return self.identity_profile.prompt_constraint()

    def visual_constraint(self) -> str:
        parts = [self.identity_constraint()]
        outfit = next((item for item in self.outfit_versions if item.is_default), None)
        if outfit is None and self.outfit_versions:
            outfit = self.outfit_versions[0]
        if outfit is not None and outfit.description.strip():
            parts.append("outfit: " + outfit.description.strip())
        if self.bible.continuity_notes.strip():
            parts.append("continuity: " + self.bible.continuity_notes.strip())
        return ". ".join(part for part in parts if part)

    def bible_constraint(self) -> str:
        return ". ".join(
            part for part in (self.visual_constraint(), self.bible.prompt_constraint()) if part
        )


class ReusableAsset(BaseModel):
    """Reusable non-character visual model, such as a prop or environment."""

    asset_id: str
    asset_type: Literal["prop", "scene"]
    display_name: str
    aliases: List[str] = Field(default_factory=list)
    description: str = ""
    visual_prompt: str = ""
    negative_prompt: str = ""
    consistency_notes: str = ""
    tags: List[str] = Field(default_factory=list)
    assets: Dict[str, str] = Field(default_factory=dict)
    scene_bible: Optional[SceneBible] = None
    prop_bible: Optional[PropBible] = None

    @model_validator(mode="after")
    def _initialize_bible(self):
        if self.asset_type == "scene" and self.scene_bible is None:
            self.scene_bible = SceneBible()
        if self.asset_type == "prop" and self.prop_bible is None:
            self.prop_bible = PropBible()
        return self

    def bible_constraint(self) -> str:
        if self.asset_type == "scene" and self.scene_bible is not None:
            return self.scene_bible.prompt_constraint()
        if self.asset_type == "prop" and self.prop_bible is not None:
            return self.prop_bible.prompt_constraint()
        return ""

    def prompt_constraint(self) -> str:
        noun = "道具" if self.asset_type == "prop" else "场景"
        positive = self.visual_prompt.strip() or self.description.strip()
        parts = [f"{noun}模型「{self.display_name}」"]
        if positive:
            parts.append(f"固定外观：{positive}")
        if self.consistency_notes.strip():
            parts.append(f"一致性要求：{self.consistency_notes.strip()}")
        bible = self.bible_constraint()
        if bible:
            parts.append(f"IP bible: {bible}")
        if self.negative_prompt.strip():
            parts.append(f"禁止变化：{self.negative_prompt.strip()}")
        return "；".join(parts)
