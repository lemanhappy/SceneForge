# Ensure aiohttp-based tools (Seedance/Veo/Omni video generators) can verify
# TLS against a known CA bundle. On Windows the default SSL context often cannot
# find the issuer cert, causing CERTIFICATE_VERIFY_FAILED. httpx-based clients
# (chat, nanobanana image) already use certifi; raw aiohttp does not. Setting
# SSL_CERT_FILE here (before any ClientSession connects) fixes them centrally.
import os as _os
try:
    import certifi as _certifi
    _os.environ.setdefault("SSL_CERT_FILE", _certifi.where())
    _os.environ.setdefault("REQUESTS_CA_BUNDLE", _certifi.where())
except Exception:
    pass

# rendering abstraction
from .protocols import ImageGenerator, VideoGenerator
from .render_backend import RenderBackend
from .video_capabilities import (
    VideoCapabilities,
    VideoDurationPlan,
    get_video_capabilities,
    plan_video_duration,
    storyboard_duration_instruction,
)

# image generators
from .image_generator_doubao_seedream_yunwu_api import ImageGeneratorDoubaoSeedreamYunwuAPI
from .image_generator_nanobanana_google_api import ImageGeneratorNanobananaGoogleAPI
from .image_generator_nanobanana_yunwu_api import ImageGeneratorNanobananaYunwuAPI

# reranker for rag
from .reranker_bge_silicon_api import RerankerBgeSiliconapi

# video generators
from .video_generator_doubao_seedance_yunwu_api import VideoGeneratorDoubaoSeedanceYunwuAPI
from .video_generator_omni_yunwu_api import VideoGeneratorOmniYunwuAPI, VideoGeneratorOminiYunwuAPI
from .video_generator_openrouter_api import VideoGeneratorOpenRouterAPI
from .video_generator_veo_google_api import VideoGeneratorVeoGoogleAPI
from .video_generator_veo_yunwu_api import VideoGeneratorVeoYunwuAPI


__all__ = [
    "ImageGenerator",
    "VideoGenerator",
    "RenderBackend",
    "VideoCapabilities",
    "VideoDurationPlan",
    "get_video_capabilities",
    "plan_video_duration",
    "storyboard_duration_instruction",
    "ImageGeneratorDoubaoSeedreamYunwuAPI",
    "ImageGeneratorNanobananaGoogleAPI",
    "ImageGeneratorNanobananaYunwuAPI",
    "RerankerBgeSiliconapi",
    "VideoGeneratorDoubaoSeedanceYunwuAPI",
    "VideoGeneratorOmniYunwuAPI",
    "VideoGeneratorOminiYunwuAPI",
    "VideoGeneratorOpenRouterAPI",
    "VideoGeneratorVeoGoogleAPI",
    "VideoGeneratorVeoYunwuAPI",
]
