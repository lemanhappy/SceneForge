import logging
import re
from typing import List, Sequence, Tuple
from tenacity import retry, stop_after_attempt
from pydantic import BaseModel, Field
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain.chat_models import init_chat_model
from utils.image import image_path_to_b64

from utils.retry import after_func

system_prompt_template_select_reference_images_only_text = \
"""
[Role]
You are a professional visual creation assistant skilled in multimodal image analysis and reasoning.

[Task]
Your core task is to intelligently select the most suitable reference images from a provided set of reference image descriptions (including multiple character reference images and existing scene images from prior frames) based on the user's text description (describing the target frame), ensuring that the subsequently generated image meets the following key consistencies:
- Character Consistency: The appearance (e.g. gender, ethnicity, age, facial features, hairstyle, body shape), clothing, expression, posture, etc., of the generated character should highly match the reference image descriptions.
- Environmental Consistency: The scene of the generated image (e.g., background, lighting, atmosphere, layout) should remain coherent with the existing image descriptions from prior frames.
- Style Consistency: The visual style of the generated image (e.g., realistic, cartoon, film-like, color tone) should harmonize with the reference image descriptions.

[Input]
You will receive a text description of the target frame, along with a sequence of reference image descriptions.
- The text description of the target frame is enclosed within <FRAME_DESC> and </FRAME_DESC>.
- The sequence of reference image descriptions is enclosed within <SEQ_DESC> and </SEQ_DESC>. Each description is prefixed with its index, starting from 0.

Below is an example of the input format:
<FRAME_DESC>
[Camera 1] Shot from Alice's over-the-shoulder perspective. Alice is on the side closer to the camera, with only her shoulder appearing in the lower left corner of the frame. Bob is on the side farther from the camera, positioned slightly right of center in the frame. Bob's expression shifts from surprise to delight as he recognizes Alice.
</FRAME_DESC>

<SEQ_DESC>
Image 0: A front-view portrait of Alice.
Image 1: A front-view portrait of Bob.
Image 2: [Camera 0] Medium shot of the supermarket aisle. Alice and Bob are shown in profile facing the right side of the frame. Bob is on the right side of the frame, and Alice is on the left side. Alice, looking down and pushing a shopping cart, follows closely behind Bob and accidentally bumps into his heel.
Image 3: [Camera 1] Shot from Alice's over-the-shoulder perspective. Alice is on the side closer to the camera, with only her shoulder appearing in the lower left corner of the frame. Bob is on the side farther from the camera, positioned slightly right of center in the frame. Bob quickly turns around, and his expression shifts from neutral to surprised.
Image 4: [Camera 2] Shot from Bob's over-the-shoulder perspective. Bob is on the side closer to the camera, with only his shoulder appearing in the lower right corner of the frame. Alice is on the side farther from the camera, positioned slightly left of center in the frame. Alice looks down, then up as she prepares to apologize. Upon realizing it's someone familiar, her expression shifts to one of surprise.
</SEQ_DESC>


[Output]
You need to select up to 8 of the most relevant reference images based on the user's description and put the corresponding indices in the ref_image_indices field of the output. At the same time, you should generate a text prompt that describes the image to be created, specifying which elements in the generated image should reference which image description (and which elements within it).

{format_instructions}


[Guidelines]
- Ensure that the language of all output values (not include keys) matches that used in the frame description.
- The reference image descriptions may depict the same character from different angles, in different outfits, or in different scenes. Identify the description closest to the version described by the user
- Prioritize image descriptions with similar compositions, i.e., shots taken by the same camera.
- The images from prior frames are arranged in chronological order. Give higher priority to more recent images (those closer to the end of the sequence).
- Choose reference image descriptions that are as concise as possible and avoid including duplicate information. For example, if Image 3 depicts the facial features of Bob from the front, and Image 1 also depicts Bob's facial features from the front-view portrait, then Image 1 is redundant and should not be selected.
- When a new character appears in the frame description, prioritize selecting their portrait image description (if available) to ensure accurate depiction of their appearance. Pay attention to whether the character is facing the camera from the front, side, or back. Choose the most suitable view as the reference image for the character.
- For character portraits, you can only select at most one image from multiple views (front, side, back). Choose the most appropriate one based on the frame description. For example, when depicting a character from the side, choose the side view of the character.
- Select at most **8** optimal reference image descriptions.
"""


system_prompt_template_select_reference_images_multimodal = \
"""
[Role]
You are a professional visual creation assistant skilled in multimodal image analysis and reasoning.

[Task]
Your core task is to intelligently select the most suitable reference images from a provided reference image library (including multiple character reference images and existing scene images from prior frames) based on the user's text description (describing the target frame), ensuring that the subsequently generated image meets the following key consistencies:
- Character Consistency: The appearance (e.g. gender, ethnicity, age, facial features, hairstyle, body shape), clothing, expression, posture, etc., of the generated character should highly match the reference images.
- Environmental Consistency: The scene of the generated image (e.g., background, lighting, atmosphere, layout) should remain coherent with the existing images from prior frames.
- Style Consistency: The visual style of the generated image (e.g., realistic, cartoon, film-like, color tone) should harmonize with the reference images and existing images.

[Input]
You will receive a text description of the target frame, along with a sequence of reference images.
- The text description of the target frame is enclosed within <FRAME_DESC> and </FRAME_DESC>.
- The sequence of reference images is enclosed within <SEQ_IMAGES> and </SEQ_IMAGES>. Each reference image is provided with a text description. The reference images are indexed starting from 0.

Below is an example of the input format:
<FRAME_DESC>
[Camera 1] Shot from Alice's over-the-shoulder perspective. <Alice> is on the side closer to the camera, with only her shoulder appearing in the lower left corner of the frame. <Bob> is on the side farther from the camera, positioned slightly right of center in the frame. <Bob>'s expression shifts from surprise to delight as he recognizes <Alice>.
</FRAME_DESC>

<SEQ_IMAGES>
Image 0: A front-view portrait of Alice.
[Image 0 here]
Image 1: A front-view portrait of Bob.
[Image 1 here]
Image 2: [Camera 0] Medium shot of the supermarket aisle. Alice and Bob are shown in profile facing the right side of the frame. Bob is on the right side of the frame, and Alice is on the left side. Alice, looking down and pushing a shopping cart, follows closely behind Bob and accidentally bumps into his heel.
[Image 2 here]
Image 3: [Camera 1] Shot from Alice's over-the-shoulder perspective. Alice is on the side closer to the camera, with only her shoulder appearing in the lower left corner of the frame. Bob is on the side farther from the camera, positioned slightly right of center in the frame. Bob is back to the camera.
[Image 3 here]
Image 4: [Camera 2] Shot from Bob's over-the-shoulder perspective. Bob is on the side closer to the camera, with only his shoulder appearing in the lower right corner of the frame. Alice is on the side farther from the camera, positioned slightly left of center in the frame. Alice looks down, then up as she prepares to apologize. Upon realizing it's someone familiar, her expression shifts to one of surprise.
</SEQ_IMAGES>

[Output]
You need to select the most relevant reference images based on the user's description and put the corresponding indices in the `ref_image_indices` field of the output. At the same time, you should generate a text prompt that describes the image to be created, specifying which elements in the generated image should reference which image (and which elements within it).

{format_instructions}


[Guidelines]
- Ensure that the language of all output values (not include keys) matches that used in the frame description.
- The reference image descriptions may depict the same character from different angles, in different outfits, or in different scenes. Identify the description closest to the version described by the user
- Prioritize image descriptions with similar compositions, i.e., shots taken by the same camera.
- The images from prior frames are arranged in chronological order. Give higher priority to more recent images (those closer to the end of the sequence).
- Choose reference image descriptions that are as concise as possible and avoid including duplicate information. For example, if Image 3 depicts the facial features of Bob from the front, and Image 1 also depicts Bob's facial features from the front-view portrait, then Image 1 is redundant and should not be selected.
- For character portraits, you can only select at most one image from multiple views (front, side, back). Choose the most appropriate one based on the frame description. For example, when depicting a character from the side, choose the side view of the character.
- Select at most **8** optimal reference image descriptions.
- The text guiding image editing should be as concise as possible.
"""


human_prompt_template_select_reference_images = \
"""
<FRAME_DESC>
{frame_description}
</FRAME_DESC>
"""




class RefImageIndicesAndTextPrompt(BaseModel):
    ref_image_indices: List[int] = Field(
        description="Indices of reference images selected from the provided images. For example, [0, 2, 5] means selecting the first, third, and sixth images. The indices should be 0-based.",
        examples=[
            [1, 3]
        ]
    )
    text_prompt: str = Field(
        description="Text description to guide the image generation. You need to describe the image to be generated, specifying which elements in the generated image should reference which image (and which elements within it). For example, 'Create an image following the given description: \nThe man is standing in the landscape. The man should reference Image 0. The landscape should reference Image 1.' Here, the index of the reference image should refer to its position in the ref_image_indices list, not the sequence number in the provided image list. Refer to the reference image must be in the format of Image N. Do not use any other word except Image.",
        examples=[
            "Create an image based on the following guidance: \n Make modifications based on Image 1: Bob's body turns to face the camera, while all other elements remain unchanged. Bob's appearance should refer to Image 0.",
            "Create an image following the given description: \nThe man is standing in the landscape. The man should reference Image 0. The landscape should reference Image 1."
        ]
    )



class ReferenceImageSelector:
    def __init__(
        self,
        chat_model,
    ):

        self.chat_model = chat_model


    @retry(
        stop=stop_after_attempt(3),
        after=after_func,
    )
    async def select_reference_images_and_generate_prompt(
        self,
        available_image_path_and_text_pairs: List[Tuple[str, str]],
        frame_description: str,
        pinned_reference_paths: Sequence[str] = (),
        continuity_reference_paths: Sequence[str] = (),
        world_reference_paths: Sequence[str] = (),
    ):
        character_pairs = pin_visible_character_references(
            available_image_path_and_text_pairs, frame_description
        )
        continuity_pairs = pin_reference_paths(
            available_image_path_and_text_pairs, continuity_reference_paths
        )
        world_pairs = pin_reference_paths(
            available_image_path_and_text_pairs, world_reference_paths
        )
        explicit_pairs = pin_reference_paths(
            available_image_path_and_text_pairs, pinned_reference_paths
        )
        # Composition continuity is the strongest constraint, followed by visible
        # character identity and explicitly bound reusable assets.  The LLM may
        # fill the remaining slots, but cannot evict these required references.
        pinned_pairs = [
            *continuity_pairs,
            *world_pairs,
            *character_pairs.values(),
            *explicit_pairs,
        ]
        filtered_image_path_and_text_pairs = available_image_path_and_text_pairs

        # 1. filter images using text-only model
        if len(available_image_path_and_text_pairs) >= 8:
            human_content = []
            for idx, (_, text) in enumerate(available_image_path_and_text_pairs):
                human_content.append({
                    "type": "text",
                    "text": f"Image {idx}: {text}"
                })
            human_content.append({
                "type": "text",
                "text": human_prompt_template_select_reference_images.format(frame_description=frame_description)
            })
            parser = PydanticOutputParser(pydantic_object=RefImageIndicesAndTextPrompt)

            messages = [
                SystemMessage(content=system_prompt_template_select_reference_images_only_text.format(format_instructions=parser.get_format_instructions())),
                HumanMessage(content=human_content)
            ]

            chain = self.chat_model | parser

            try:
                ref = await chain.ainvoke(messages)
                filtered_image_path_and_text_pairs = merge_reference_pairs(
                    pinned_pairs,
                    select_pairs_by_indices(available_image_path_and_text_pairs, ref.ref_image_indices),
                )
                logging.info(f"Filtered image idx:{ref.ref_image_indices}")
                
            except Exception as e:
                logging.error(f"Error get image prompt: \n{e}")
                raise e

        # 2. filter images using multimodal model
        human_content = []
        for idx, (image_path, text) in enumerate(filtered_image_path_and_text_pairs):
            human_content.append({
                "type": "text",
                "text": f"Image {idx}: {text}"
            })
            human_content.append({
                "type": "image_url",
                "image_url": {"url": image_path_to_b64(image_path)}
            })
        human_content.append({
            "type": "text",
            "text": human_prompt_template_select_reference_images.format(frame_description=frame_description)
        })

        parser = PydanticOutputParser(pydantic_object=RefImageIndicesAndTextPrompt)

        messages = [
            SystemMessage(content=system_prompt_template_select_reference_images_multimodal.format(format_instructions=parser.get_format_instructions())),
            HumanMessage(content=human_content)
        ]

        chain = self.chat_model | parser

        try:
            response = await chain.ainvoke(messages)
            model_pairs = select_pairs_by_indices(filtered_image_path_and_text_pairs, response.ref_image_indices)
            reference_image_path_and_text_pairs = merge_reference_pairs(pinned_pairs, model_pairs)
            text_prompt = remap_reference_prompt(
                response.text_prompt,
                model_pairs,
                reference_image_path_and_text_pairs,
                source_pairs=filtered_image_path_and_text_pairs,
                source_indices=response.ref_image_indices,
            )
            for character, pair in character_pairs.items():
                index = _path_index(reference_image_path_and_text_pairs, pair[0])
                if index is not None:
                    text_prompt += (
                        f"\nKeep <{character}>'s identity and appearance strictly "
                        f"consistent with Image {index}."
                    )
            continuity_paths = {str(path) for path in continuity_reference_paths}
            for pair in continuity_pairs:
                index = _path_index(reference_image_path_and_text_pairs, pair[0])
                if index is not None:
                    text_prompt += (
                        f"\nImage {index} is the locked camera-and-scene anchor. "
                        "Preserve its camera side and axis, spatial projection, architecture, "
                        "doors, windows, fixed furniture, major lighting sources, and background "
                        "layout. Allow shot size, lens, composition, or camera movement to change "
                        "only when explicitly required by the target frame; such changes must still "
                        "preserve the same scene-world topology."
                    )
            for pair in world_pairs:
                index = _path_index(reference_image_path_and_text_pairs, pair[0])
                if index is not None:
                    text_prompt += (
                        f"\nImage {index} is a same-world reference from another camera. "
                        "The viewpoint, shot size, composition, and lens may change, but preserve "
                        "the location identity, spatial topology, architecture, materials, time, "
                        "weather, motivated lighting, and screen direction."
                    )
            for pair in explicit_pairs:
                if str(pair[0]) in continuity_paths:
                    continue
                index = _path_index(reference_image_path_and_text_pairs, pair[0])
                if index is not None:
                    text_prompt += "\n" + reusable_asset_reference_instruction(
                        index, pair[1]
                    )
            text_prompt += (
                "\n\n[Mandatory target frame]\n"
                + str(frame_description or "").strip()
                + "\n[/Mandatory target frame]"
                "\nThe mandatory target frame above is authoritative for every movable object's "
                "current location, support surface, held/placed/open/closed state, count, and "
                "ownership. Reusable asset images define appearance and identity only; they "
                "must never override the target frame's spatial placement or action state."
            )
            return {
                "reference_image_path_and_text_pairs": reference_image_path_and_text_pairs,
                "text_prompt": text_prompt,
            }

        except Exception as e:
            logging.error(f"Error get image prompt: \n{e}")
            raise e




def select_pairs_by_indices(pairs, indices):
    """Index into pairs with LLM-emitted indices, rejecting out-of-range values.

    Negative indices would silently select the wrong image via Python indexing.
    """
    invalid = [i for i in indices if i < 0 or i >= len(pairs)]
    if invalid:
        raise ValueError(f"ref_image_indices out of range: {invalid} (have {len(pairs)} images)")
    return [pairs[i] for i in indices]


def visible_character_names(frame_description: str) -> List[str]:
    names = []
    seen = set()
    for raw in re.findall(r"<([^<>]+)>", str(frame_description or "")):
        name = raw.strip()
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            names.append(name)
    return names


def pin_visible_character_references(pairs, frame_description: str):
    """Choose one deterministic portrait per visible character before LLM filtering."""
    result = {}
    description = str(frame_description or "").casefold()
    preferred = _preferred_portrait_view(description)
    for name in visible_character_names(frame_description):
        candidates = [
            pair for pair in pairs
            if name.casefold() in str(pair[1] or "").casefold()
            and "portrait" in str(pair[1] or "").casefold()
        ]
        if not candidates:
            continue
        result[name] = min(
            candidates,
            key=lambda pair: _portrait_rank(str(pair[1] or "").casefold(), preferred),
        )
    return result


def pin_reference_paths(pairs, paths: Sequence[str]):
    """Resolve caller-required paths to their source pairs in stable order."""
    by_path = {str(pair[0]): pair for pair in pairs}
    result = []
    seen = set()
    for path in paths or ():
        key = str(path)
        pair = by_path.get(key)
        if pair is None or key in seen:
            continue
        seen.add(key)
        result.append(pair)
    return result


def reusable_asset_kind(description: str) -> str:
    normalized = str(description or "").lstrip().casefold()
    if normalized.startswith("[prop]"):
        return "prop"
    if normalized.startswith("[scene]"):
        return "scene"
    return "asset"


def reusable_asset_reference_instruction(index: int, description: str) -> str:
    """Keep canonical asset identity without copying a catalog image's framing."""
    kind = reusable_asset_kind(description)
    prefix = f"Image {index} is an explicitly bound reusable asset."
    if kind == "prop":
        return (
            f"{prefix} It is a canonical appearance-only prop reference, not a shot or "
            "composition reference. Ignore its studio background, isolation, crop, camera "
            "angle, and apparent image scale. Only when the mandatory target frame includes "
            "this prop, render exactly one physically plausible instance with the target's "
            "specified location, orientation, support surface, and ordinary real-world size. "
            "Never paste it into the foreground, enlarge it, stand it upright, or make it float "
            "unless the target frame explicitly requires that composition. Keep only its identity, "
            "shape, materials, colors, and distinctive details consistent."
        )
    if kind == "scene":
        return (
            f"{prefix} It is a world-layout reference, not a camera-composition template. "
            "Preserve location identity, spatial topology, architecture, materials, fixed furniture, "
            "weather, and motivated lighting. The mandatory target frame is authoritative for camera "
            "position, lens, shot size, composition, actor placement, and temporary object state; do "
            "not copy those transient details from this reference."
        )
    return (
        f"{prefix} Keep its identity, shape, materials, layout, and colors consistent whenever visible, "
        "but let the mandatory target frame control placement, scale, state, and composition."
    )


def merge_reference_pairs(pinned_pairs, selected_pairs, limit: int = 8):
    result = []
    seen = set()
    required = list(pinned_pairs.values()) if hasattr(pinned_pairs, "values") else list(pinned_pairs)
    for pair in [*required, *selected_pairs]:
        key = str(pair[0])
        if key in seen:
            continue
        seen.add(key)
        result.append(pair)
        if len(result) >= limit:
            break
    return result


def remap_reference_prompt(
    prompt,
    old_pairs,
    new_pairs,
    source_pairs=None,
    source_indices=None,
):
    """Remap image references after pinning/reordering selected pairs.

    The structured-output contract asks the model to number only the selected
    images, but providers occasionally keep the index from the original source
    list (for example selecting ``[0, 3]`` and then writing ``Image 3``).  When
    the optional source context is present, recover that reference instead of
    leaving a dangling image number in the generation prompt.
    """
    mapping = {
        old_index: _path_index(new_pairs, pair[0])
        for old_index, pair in enumerate(old_pairs)
    }
    selected_source_indices = set(source_indices or [])

    def replace(match):
        old_index = int(match.group(1))
        new_index = mapping.get(old_index)
        if (
            new_index is None
            and source_pairs is not None
            and old_index in selected_source_indices
            and 0 <= old_index < len(source_pairs)
        ):
            new_index = _path_index(new_pairs, source_pairs[old_index][0])
        return match.group(0) if new_index is None else f"Image {new_index}"

    return re.sub(r"\bImage\s+(\d+)\b", replace, str(prompt or ""))


def _preferred_portrait_view(description: str) -> str:
    if re.search(r"\b(back|rear)\b|背面|背对|后背", description):
        return "back"
    if re.search(r"\b(side|profile)\b|侧面|侧脸|侧身", description):
        return "side"
    return "front"


def _portrait_rank(description: str, preferred: str) -> tuple[int, int]:
    if f"{preferred} view" in description or f"{preferred}-view" in description:
        return 0, len(description)
    if "front view" in description or "front-view" in description:
        return 1, len(description)
    return 2, len(description)


def _path_index(pairs, path):
    return next((index for index, pair in enumerate(pairs) if str(pair[0]) == str(path)), None)
