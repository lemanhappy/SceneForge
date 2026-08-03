import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, List, Optional

from .mixer import mux_audio, mux_voiceover, probe_audio_duration
from .models import AudioMixSpec, VoiceClip
from .sfx import extract_sound_effects, resolve_sfx_file
from .tts_provider import MiniMaxTTSProvider, OpenAITTSProvider

logger = logging.getLogger(__name__)

# Reuse the env-var fallbacks the rest of the project already honours so a single
# SCENEFORGE_LLM_API_KEY / gateway base_url drives TTS too (see agent_runtime/config).
_DEFAULT_BASE_URL = "https://yunwu.ai/v1"
_DEFAULT_MODEL = "tts-1"
_DEFAULT_VOICE = "alloy"
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
_DUCKING_PROFILES = {
    "gentle": {"threshold": 0.04, "ratio": 4.0},
    "balanced": {"threshold": 0.02, "ratio": 8.0},
    "strong": {"threshold": 0.01, "ratio": 12.0},
}


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return ""


def make_provider(api_key: str, base_url: str, section: dict):
    """Build the TTS provider chosen by ``section['provider']`` (``openai`` or
    ``minimax``) with an already-resolved key + base_url. The chosen voice is
    baked into the provider, so callers leave ``VoiceoverService.voice`` as
    ``None`` and let the provider use its own default."""
    section = section or {}
    provider = (section.get("provider") or "openai").strip().lower()
    if provider == "minimax":
        return MiniMaxTTSProvider(
            api_key=api_key,
            base_url=base_url,
            model=section.get("model") or "speech-2.6-hd",
            voice_id=section.get("voice_id") or section.get("voice") or "male-qn-jingying",
            speed=float(section.get("speed", 1.0)),
            vol=float(section.get("vol", 1.0)),
            pitch=int(section.get("pitch", 0)),
            sample_rate=int(section.get("sample_rate", 32000)),
            audio_format=section.get("response_format") or "mp3",
        )
    return OpenAITTSProvider(
        api_key=api_key,
        model=section.get("model") or _DEFAULT_MODEL,
        base_url=base_url,
        voice=section.get("voice") or _DEFAULT_VOICE,
        response_format=section.get("response_format") or "mp3",
        speed=float(section.get("speed", 1.0)),
    )


def build_provider_from_section(section: dict):
    """Build a TTS provider from an ``audio.tts`` config section, falling back to
    the shared LLM gateway env vars for the key/base_url (so the CLI path works
    without duplicating secrets). Returns ``None`` when no key can be resolved."""
    section = section or {}
    api_key = section.get("api_key") or _first_env("SCENEFORGE_TTS_API_KEY", "SCENEFORGE_LLM_API_KEY", "SCENEFORGE_API_KEY")
    if not api_key:
        return None
    base_url = section.get("base_url") or _first_env("SCENEFORGE_TTS_BASE_URL", "SCENEFORGE_LLM_BASE_URL") or _DEFAULT_BASE_URL
    return make_provider(api_key, base_url, section)


def resolve_bgm_path(section: dict) -> Optional[str]:
    """Resolve a background-music file from an ``audio.bgm`` section: an explicit
    ``path``, or the first audio file in ``dir`` (sorted, deterministic).
    Returns ``None`` if nothing usable is found (BGM is then skipped)."""
    section = section or {}
    path = section.get("path")
    if path and os.path.isfile(path):
        return path
    directory = section.get("dir")
    if directory and os.path.isdir(directory):
        files = sorted(p for p in Path(directory).iterdir()
                       if p.is_file() and p.suffix.lower() in _AUDIO_EXTS)
        if files:
            return str(files[0])
    return None


class VoiceoverService:
    """Audio post-production facade: TTS voiceover + sound effects + background
    music + loudness normalization, assembled in one ffmpeg pass.

    Fully optional and degrades to the original video on any failure. Named
    ``VoiceoverService`` for backward compatibility with existing wiring; its
    responsibility is the whole added-audio mix.
    """

    def __init__(self, provider=None, voice: Optional[str] = None,
                 mix_with_original: bool = False, enabled: bool = True,
                 bgm_path: Optional[str] = None, bgm_volume: float = 0.2,
                 bgm_ducking: bool = False, bgm_ducking_strength: str = "balanced",
                 bgm_duck_attack_ms: float = 20.0, bgm_duck_release_ms: float = 350.0,
                 sfx_library: Optional[str] = None, sfx_volume: float = 0.8,
                 loudnorm: bool = True, loudnorm_i: float = -16.0,
                 loudnorm_tp: float = -1.5, loudnorm_lra: float = 11.0,
                 fit_shot_to_speech: bool = True, fit_tail_pad: float = 0.4,
                 max_shot_extend: float = 6.0):
        self.provider = provider
        self.voice = voice
        self.mix_with_original = mix_with_original
        self.enabled = enabled
        self.bgm_path = bgm_path
        self.bgm_volume = bgm_volume
        self.bgm_ducking = bool(bgm_ducking)
        self.bgm_ducking_strength = (
            bgm_ducking_strength if bgm_ducking_strength in _DUCKING_PROFILES else "balanced"
        )
        self.bgm_duck_attack_ms = max(0.01, float(bgm_duck_attack_ms))
        self.bgm_duck_release_ms = max(1.0, float(bgm_duck_release_ms))
        self.sfx_library = sfx_library
        self.sfx_volume = sfx_volume
        self.loudnorm = loudnorm
        self.loudnorm_i = loudnorm_i
        self.loudnorm_tp = loudnorm_tp
        self.loudnorm_lra = loudnorm_lra
        # Extend a shot whose spoken audio outlasts its video clip (freeze the last
        # frame) so the cut doesn't fall mid-sentence. tail_pad = extra breath after
        # the line; max_shot_extend caps how long a single shot may freeze.
        self.fit_shot_to_speech = fit_shot_to_speech
        self.fit_tail_pad = max(0.0, float(fit_tail_pad))
        self.max_shot_extend = max(0.0, float(max_shot_extend))

    @classmethod
    def from_config(cls, config: dict) -> Optional["VoiceoverService"]:
        audio = (config or {}).get("audio") or {}
        tts = audio.get("tts") or {}
        bgm = audio.get("bgm") or {}
        sfx = audio.get("sfx") or {}
        loud = audio.get("loudnorm") or {}
        ducking = bgm.get("ducking") or {}
        if not isinstance(ducking, dict):
            ducking = {"enabled": bool(ducking)}
        tts_on, bgm_on, sfx_on = bool(tts.get("enabled")), bool(bgm.get("enabled")), bool(sfx.get("enabled"))
        if not (tts_on or bgm_on or sfx_on):
            return None
        return cls(
            provider=build_provider_from_section(tts) if tts_on else None,
            voice=None,  # the chosen voice is baked into the provider
            mix_with_original=bool(tts.get("mix_with_original", False)),
            enabled=True,
            bgm_path=resolve_bgm_path(bgm) if bgm_on else None,
            bgm_volume=float(bgm.get("volume", 0.2)),
            bgm_ducking=bool(ducking.get("enabled", False)),
            bgm_ducking_strength=str(ducking.get("strength") or "balanced"),
            bgm_duck_attack_ms=float(ducking.get("attack_ms", 20.0)),
            bgm_duck_release_ms=float(ducking.get("release_ms", 350.0)),
            sfx_library=(sfx.get("library") if sfx_on else None),
            sfx_volume=float(sfx.get("volume", 0.8)),
            loudnorm=bool(loud.get("enabled", True)),
            loudnorm_i=float(loud.get("i", -16.0)),
            loudnorm_tp=float(loud.get("tp", -1.5)),
            loudnorm_lra=float(loud.get("lra", 11.0)),
            fit_shot_to_speech=bool(audio.get("fit_shot_to_speech", True)),
            fit_tail_pad=float(audio.get("fit_tail_pad", 0.4)),
            max_shot_extend=float(audio.get("max_shot_extend", 6.0)),
        )

    @staticmethod
    def _compute_fit_targets(shot_idxs, orig_durations, spoken_by_shot, tail_pad, max_extend):
        """Pure timing math: target duration per shot = clamp(spoken+tail) into
        [orig, orig+max_extend]. Returns (targets, need_fit)."""
        targets, need_fit = [], False
        for idx, od in zip(shot_idxs, orig_durations):
            od = max(0.0, float(od))
            spoken = max(0.0, float(spoken_by_shot.get(idx, 0.0)))
            target = od
            if spoken > 0:
                want = spoken + tail_pad
                target = min(max(od, want), od + max_extend)
            if target - od > 0.05:
                need_fit = True
            targets.append(round(target, 3))
        return targets, need_fit

    def _maybe_fit_video(self, video_path, shot_descriptions, video_paths, voiceover_clips,
                         working_dir, transition, duration_provider, runner):
        """Return ``(shot_starts, fitted_path_or_None)``. When a shot's spoken audio
        outlasts its clip, freeze-extend it and re-concatenate; shot_starts then use
        the padded durations. Any failure falls back to the original timing."""
        from subtitles.timeline import probe_duration
        dp = duration_provider or probe_duration
        shot_descriptions = shot_descriptions or []
        video_paths = video_paths or []
        idxs = [getattr(sd, "idx", None) for sd in shot_descriptions]
        orig = [max(0.0, float(dp(p))) for p in video_paths]

        def starts_from(durs):
            s, cur = {}, 0.0
            for i, d in zip(idxs, durs):
                s[i] = round(cur, 3)
                cur += d
            return s

        if not (self.fit_shot_to_speech and voiceover_clips and idxs and len(orig) == len(idxs)):
            return starts_from(orig), None
        spoken: dict = {}
        for c in voiceover_clips:
            spoken[c.shot_idx] = spoken.get(c.shot_idx, 0.0) + (c.duration or 0.0)
        targets, need = self._compute_fit_targets(idxs, orig, spoken, self.fit_tail_pad, self.max_shot_extend)
        if not need:
            return starts_from(orig), None
        try:
            fitted = self._build_fitted_video(video_paths, orig, targets, working_dir, transition, runner)
        except Exception as exc:  # pragma: no cover - ffmpeg/moviepy dependent
            logger.warning("fit-to-speech failed (using unpadded video): %s", exc)
            fitted = None
        return (starts_from(targets), fitted) if fitted else (starts_from(orig), None)

    def _build_fitted_video(self, video_paths, orig, targets, working_dir, transition, runner):
        """Freeze-extend each shot's last frame to its target duration, then
        re-concatenate (audio stripped; voiceover replaces it). Returns the fitted
        video path, or None if ffmpeg is unavailable / a segment fails."""
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return None
        seg_dir = os.path.join(working_dir, "audio", "_fit")
        os.makedirs(seg_dir, exist_ok=True)
        seg_paths = []
        for i, (src, od, tg) in enumerate(zip(video_paths, orig, targets)):
            extra = max(0.0, tg - od)
            seg = os.path.join(seg_dir, f"seg_{i:03d}.mp4")
            vf = (f"tpad=stop_mode=clone:stop_duration={extra:.3f}" if extra > 0.05 else "null")
            cmd = [ffmpeg, "-y", "-i", src, "-an", "-vf", vf,
                   "-c:v", "libx264", "-preset", "medium", "-pix_fmt", "yuv420p", seg]
            proc = runner(cmd, capture_output=True)
            if getattr(proc, "returncode", 1) != 0 or not os.path.exists(seg):
                return None
            seg_paths.append(seg)
        out = os.path.join(working_dir, "final_video_fitted.mp4")
        from utils.video import concatenate_video_files
        concatenate_video_files(seg_paths, out, transition=transition)
        return out

    def synthesize_track(self, track, out_dir: str,
                         audio_duration_provider: Optional[Callable[[str], float]] = None) -> List[VoiceClip]:
        """Render each line of ``track`` to an audio file in ``out_dir`` and
        measure its real duration. Lines that fail to synthesize are skipped
        (not fatal)."""
        clips: List[VoiceClip] = []
        if self.provider is None or track is None:
            return clips
        if audio_duration_provider is None:
            audio_duration_provider = probe_audio_duration
        os.makedirs(out_dir, exist_ok=True)
        for i, line in enumerate(track.lines):
            out_path = os.path.join(out_dir, f"line_{i:03d}.mp3")
            rendered = self.provider.synthesize(line.text, out_path, voice=self.voice)
            if not rendered:
                return []
            try:
                duration = max(0.0, float(audio_duration_provider(rendered)))
            except Exception:  # pragma: no cover - backend dependent
                duration = 0.0
            if duration <= 0:
                logger.warning("Discarding voiceover track because line %d is not valid audio", i)
                return []
            clips.append(VoiceClip(path=rendered, start=line.start, shot_idx=line.shot_idx,
                                   text=line.text, duration=duration))
        return clips

    def _shot_starts(self, shot_descriptions, video_paths,
                     duration_provider: Optional[Callable[[str], float]] = None) -> dict:
        """Map shot_idx -> absolute start second (cumulative video durations)."""
        if duration_provider is None:
            from subtitles.timeline import probe_duration
            duration_provider = probe_duration
        starts: dict = {}
        cursor = 0.0
        for shot, video_path in zip(shot_descriptions or [], video_paths or []):
            starts[getattr(shot, "idx", None)] = round(cursor, 3)
            cursor += max(0.0, float(duration_provider(video_path)))
        return starts

    def retime_track(self, track, clips: List[VoiceClip], shot_starts: dict):
        """Rebuild a SubtitleTrack so each line spans its real TTS duration,
        anchored at its shot's start and laid sequentially within the shot. Keeps
        voiceover and subtitles in lockstep. ``track`` and ``clips`` are parallel
        (same order); both are updated to the new timing."""
        from subtitles.models import SubtitleTrack
        new_lines = []
        cursor_by_shot: dict = {}
        for line, clip in zip(track.lines, clips):
            base = shot_starts.get(line.shot_idx, 0.0)
            offset = cursor_by_shot.get(line.shot_idx, 0.0)
            start = round(base + offset, 3)
            end = round(start + clip.duration, 3)
            cursor_by_shot[line.shot_idx] = offset + clip.duration
            new_lines.append(line.model_copy(update={"start": start, "end": end}))
            clip.start = start
        return SubtitleTrack(lines=new_lines)

    def resolve_sfx_clips(self, shot_descriptions, video_paths,
                          duration_provider: Optional[Callable[[str], float]] = None) -> List[VoiceClip]:
        """Extract ``[Sound Effect]`` markers per shot, resolve each to a file in
        the sfx library, and place it at its shot's start time. Effects without a
        matching asset are silently skipped."""
        if not self.sfx_library or not shot_descriptions or not video_paths:
            return []
        if duration_provider is None:
            from subtitles.timeline import probe_duration
            duration_provider = probe_duration
        clips: List[VoiceClip] = []
        cursor = 0.0
        for shot, video_path in zip(shot_descriptions, video_paths):
            for text in extract_sound_effects(shot):
                resolved = resolve_sfx_file(text, self.sfx_library)
                if resolved:
                    clips.append(VoiceClip(path=resolved, start=round(cursor, 3),
                                           shot_idx=getattr(shot, "idx", None), text=text))
            cursor += max(0.0, float(duration_provider(video_path)))
        return clips

    def render_audio(self, video_path: str, track, shot_descriptions=None, video_paths=None,
                     working_dir: str = ".",
                     runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                     duration_provider: Optional[Callable[[str], float]] = None,
                     audio_duration_provider: Optional[Callable[[str], float]] = None,
                     transition=None):
        """Synthesize voiceover, resolve sfx, add the BGM bed, normalize loudness,
        and mux it all onto ``video_path`` in one pass.

        Returns ``(processed_video_or_None, effective_track)``. When voiceover is
        synthesized, ``effective_track`` is ``track`` retimed to the real TTS
        durations (so subtitles can reuse the exact same timing); otherwise it is
        the unchanged ``track``.
        """
        effective_track = track
        if not self.enabled:
            return None, effective_track

        voiceover_clips = self.synthesize_track(track, os.path.join(working_dir, "audio"),
                                                audio_duration_provider)
        if voiceover_clips and track is not None:
            # Fit each shot to its spoken length (freeze-extend short shots) so a cut
            # never lands mid-sentence; falls back to the unpadded video on failure.
            shot_starts, fitted = self._maybe_fit_video(
                video_path, shot_descriptions, video_paths, voiceover_clips,
                working_dir, transition, duration_provider, runner)
            if fitted:
                video_path = fitted
            effective_track = self.retime_track(track, voiceover_clips, shot_starts)

        sfx_clips = self.resolve_sfx_clips(shot_descriptions, video_paths, duration_provider)
        ducking_profile = _DUCKING_PROFILES[self.bgm_ducking_strength]
        spec = AudioMixSpec(
            voiceover=voiceover_clips,
            sfx=sfx_clips,
            bgm_path=self.bgm_path,
            bgm_volume=self.bgm_volume,
            bgm_ducking=self.bgm_ducking,
            bgm_duck_threshold=ducking_profile["threshold"],
            bgm_duck_ratio=ducking_profile["ratio"],
            bgm_duck_attack_ms=self.bgm_duck_attack_ms,
            bgm_duck_release_ms=self.bgm_duck_release_ms,
            sfx_volume=self.sfx_volume,
            loudnorm=self.loudnorm,
            loudnorm_i=self.loudnorm_i,
            loudnorm_tp=self.loudnorm_tp,
            loudnorm_lra=self.loudnorm_lra,
            mix_with_original=self.mix_with_original,
        )
        if not spec.has_added_audio:
            return None, effective_track
        output_path = os.path.join(working_dir, "final_video_audio.mp4")
        return mux_audio(
            video_path,
            spec,
            output_path,
            runner=runner,
            duration_provider=duration_provider,
        ), effective_track

    def add_audio(self, video_path: str, track, shot_descriptions=None, video_paths=None,
                  working_dir: str = ".",
                  runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                  duration_provider: Optional[Callable[[str], float]] = None) -> Optional[str]:
        """Backward-compatible wrapper around :meth:`render_audio` that returns
        only the processed video path."""
        processed, _ = self.render_audio(video_path, track, shot_descriptions=shot_descriptions,
                                         video_paths=video_paths, working_dir=working_dir,
                                         runner=runner, duration_provider=duration_provider)
        return processed

    def add_voiceover(self, video_path: str, track, working_dir: str,
                      runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                      audio_duration_provider: Optional[Callable[[str], float]] = None) -> Optional[str]:
        """Voiceover-only mux (no bgm/sfx/loudnorm). Kept for direct use; the
        pipeline uses :meth:`add_audio` for the full mix."""
        if not self.enabled or self.provider is None or track is None or len(track) == 0:
            return None
        clips = self.synthesize_track(track, os.path.join(working_dir, "audio"), audio_duration_provider)
        if not clips:
            return None
        output_path = os.path.join(working_dir, "final_video_voiced.mp4")
        return mux_voiceover(video_path, clips, output_path,
                             mix_with_original=self.mix_with_original, runner=runner)
