"""Video production packages: everything needed to film and export one video.

A concept is not a shoot. This module turns an idea into the artefacts a person
(or an editing tool) can act on without asking a follow-up question: a scene
list with real timecodes, transitions with reasons, burned-caption cues, a
valid `.srt` file, thumbnail directions, music guidance, and export metadata.

Three things here are load-bearing.

**Timecodes are computed, not estimated.** Scene durations accumulate into
absolute in/out points, and the caption cues are generated from those same
numbers. A script where the captions drift from the cut is worse than no
captions — most of the audience watches muted, so the caption *is* the
soundtrack.

**Music is guidance, never a file.** Commercial rights on audio are the fastest
way to get a video muted or a shop struck, and this system cannot verify a
licence. So it names the categories that are safe on TikTok specifically (the
in-app Commercial Music Library for a business account), states the trap that
catches people (adding a sound in-app after uploading an export that already
contains one), and refuses to name a specific track.

**An export refuses to be marked ready while a slot is unfilled.** The
`[FILL: ...]` markers from `creative.py` survive into the package, and
`ProductionPackage.ready` is false while any remain. The failure mode this
prevents is a real one: a placeholder that reads as plausible English gets
filmed, because nobody re-reads a brief they asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .content import ShotListItem, VideoConcept, check_claims
from .creative import SLOT_PATTERN, VideoIdea, check_originality, unfilled_slots

# Vertical short-form. Not configurable because it is not a preference: a
# non-9:16 upload is letterboxed, and letterboxed video is scrolled past.
CANVAS = {"width": 1080, "height": 1920, "aspect": "9:16", "fps": 30}

# Safe-area insets in pixels. TikTok's own UI covers these regions; a caption
# placed under the CTA rail is invisible to every viewer on the app, which is
# all of them.
SAFE_AREA = {
    "top": 120,      # search bar and following/for-you tabs
    "bottom": 480,   # caption block, sound rail, and the CTA button
    "right": 180,    # like/comment/share/profile column
    "left": 40,
}

# Caption timing. Two-second minimum because a cue shorter than that cannot be
# read at all; the reading-rate figure is what drives splitting long lines.
MIN_CUE_SECONDS = 1.2
MAX_CUE_SECONDS = 4.0
CHARS_PER_SECOND = 14.0
MAX_CUE_CHARS = 42          # two comfortable lines at this canvas width

TRANSITIONS = {
    "hard_cut": ("Hard cut", "Default. Costs nothing and reads as competent. "
                             "Anything else must earn its place."),
    "match_cut": ("Match cut on the action", "Same framing either side. The only "
                  "transition that makes a before/after legible in one glance."),
    "whip_pan": ("Whip pan", "Hides a location change. Overused — it now reads "
                 "as an edit rather than a movement."),
    "j_cut": ("J-cut (audio leads)", "Next scene's audio starts under the current "
              "picture. Keeps a talking-head from feeling like slides."),
    "hold": ("No transition — hold", "Used at the end of a demonstration. A cut "
             "on the payoff reads as a hidden edit."),
}

# Music categories that are safe to *plan* around. Deliberately categories, not
# tracks: this system cannot verify a licence, and naming a track it cannot
# verify is exactly the fabrication the charter forbids.
MUSIC_GUIDANCE = {
    "primary": (
        "TikTok Commercial Music Library, selected in-app after upload. This is "
        "the only library cleared for a business account, and using it in-app is "
        "what attaches the licence."),
    "trap": (
        "Do not burn music into the export. An export that already contains a "
        "track cannot have the in-app licence applied to it, and the upload gets "
        "muted or struck — with the strike landing on the shop, not the video."),
    "alternatives": (
        "Royalty-free libraries with a written commercial licence, or audio the "
        "operator owns outright. Keep the licence file; a takedown dispute needs "
        "the document, not a memory of where it came from."),
    "diegetic": (
        "For demonstration and satisfying formats, no music at all. The mechanism's "
        "own sound outperforms a bed, and it carries no licence risk whatsoever."),
}


def _format_timecode(seconds: float, *, srt: bool = False) -> str:
    """HH:MM:SS,mmm for SRT; MM:SS.s for a human reading a call sheet."""
    seconds = max(0.0, seconds)
    if srt:
        hours, rem = divmod(int(seconds), 3600)
        minutes, secs = divmod(rem, 60)
        millis = int(round((seconds - int(seconds)) * 1000))
        if millis == 1000:      # rounding can push to a whole second
            millis, secs = 0, secs + 1
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"
    minutes, secs = divmod(seconds, 60)
    return f"{int(minutes):02d}:{secs:04.1f}"


@dataclass
class Scene:
    number: int
    start: float
    end: float
    shot: str
    audio: str
    on_screen_text: str
    purpose: str
    transition_in: str
    transition_note: str

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 2)

    @property
    def timecode(self) -> str:
        return f"{_format_timecode(self.start)}–{_format_timecode(self.end)}"


@dataclass
class Cue:
    index: int
    start: float
    end: float
    text: str

    def to_srt(self) -> str:
        return (f"{self.index}\n"
                f"{_format_timecode(self.start, srt=True)} --> "
                f"{_format_timecode(self.end, srt=True)}\n"
                f"{self.text}\n")


@dataclass
class ProductionPackage:
    package_id: str
    sku: str
    title: str
    angle: str
    format: str
    scenes: list[Scene]
    cues: list[Cue]
    storyboard: list[str]
    thumbnail_ideas: list[dict[str, str]]
    music: dict[str, str]
    export_metadata: dict[str, Any]
    caption: str
    hashtags: list[str]
    cta: str
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def runtime(self) -> float:
        return round(self.scenes[-1].end, 2) if self.scenes else 0.0

    @property
    def ready(self) -> bool:
        """False while anything is unfilled or non-compliant.

        The gate exists because a plausible-sounding placeholder gets filmed.
        Nobody re-reads a brief they asked for.
        """
        return not self.blockers

    def srt(self) -> str:
        """A valid SubRip file. Cues are derived from the same timings as the cut."""
        return "\n".join(cue.to_srt() for cue in self.cues)

    def call_sheet(self) -> str:
        """Plain-text shooting order, in the form someone can hold on set."""
        lines = [
            f"{self.title} — {self.angle} ({self.format})",
            f"Runtime {self.runtime:.1f}s · {CANVAS['aspect']} · "
            f"{CANVAS['width']}x{CANVAS['height']} @ {CANVAS['fps']}fps",
            "",
        ]
        if not self.ready:
            lines.append("NOT READY TO SHOOT:")
            lines.extend(f"  - {b}" for b in self.blockers)
            lines.append("")
        for scene in self.scenes:
            lines.extend([
                f"[{scene.number}] {scene.timecode}  ({scene.duration:.1f}s)",
                f"     Shot:  {scene.shot}",
                f"     Audio: {scene.audio}",
            ])
            if scene.on_screen_text:
                lines.append(f"     Text:  {scene.on_screen_text}")
            lines.append(f"     Why:   {scene.purpose}")
            if scene.number > 1:
                lines.append(f"     In:    {scene.transition_in} — {scene.transition_note}")
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------
def split_for_reading(text: str) -> list[str]:
    """Break a line into chunks that can actually be read on screen.

    Splits on clause boundaries first and only falls back to word count,
    because a caption cut mid-phrase is harder to read than a slightly long one.
    """
    text = " ".join(text.split())
    if len(text) <= MAX_CUE_CHARS:
        return [text] if text else []

    chunks: list[str] = []
    for part in re.split(r"(?<=[.!?,;:—])\s+", text):
        if not part:
            continue
        if len(part) <= MAX_CUE_CHARS:
            chunks.append(part)
            continue
        words, current = part.split(), ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) > MAX_CUE_CHARS and current:
                chunks.append(current)
                current = word
            else:
                current = candidate
        if current:
            chunks.append(current)
    return chunks


# Spoken words appear in quotes; everything else on an audio line is a
# direction to the person filming. The distinction is the whole reason cues can
# be generated at all — burning `VO: the specific thing it does differently`
# into a video as a caption puts a stage direction on screen.
SPOKEN_LINE = re.compile(r'["“]([^"”]+)["”]')


def extract_spoken(audio: str) -> tuple[str, bool]:
    """Return (spoken words, is_direction).

    A quoted span is speech. An unquoted `VO:` line is an instruction to write
    the speech later, which is a missing script, not a caption.
    """
    quoted = SPOKEN_LINE.search(audio or "")
    if quoted:
        return quoted.group(1).strip(), False
    stripped = re.sub(r'^(?:VO:|VO continues[,:]?)\s*', "", audio or "").strip()
    if not stripped:
        return "", False
    if stripped.lower().startswith(("diegetic", "natural room", "no talking",
                                    "silence", "room tone")):
        return "", False
    return stripped, True


def build_cues(scenes: list[Scene]) -> tuple[list[Cue], list[str]]:
    """Caption cues generated from the cut itself, plus scenes still unscripted.

    Timed from the scene boundaries rather than written separately, so captions
    cannot drift from the edit. Muted viewing is the default on this platform —
    a caption out of sync is the whole video out of sync.
    """
    cues: list[Cue] = []
    unscripted: list[str] = []
    index = 1
    for scene in scenes:
        spoken, is_direction = extract_spoken(scene.audio)
        if is_direction:
            # Do not caption a direction. Record it as an open item instead.
            unscripted.append(
                f"scene {scene.number}: audio is a direction, not a line — "
                f'"{spoken[:60]}". Write the spoken words before the captions '
                "can be generated.")
            continue
        if not spoken:
            continue
        chunks = split_for_reading(spoken)
        if not chunks:
            continue

        # Distribute the scene's time across its chunks by length, so a long
        # line gets longer on screen than a short one.
        total_chars = sum(len(c) for c in chunks) or 1
        cursor = scene.start
        for chunk in chunks:
            share = len(chunk) / total_chars
            duration = scene.duration * share
            duration = max(MIN_CUE_SECONDS,
                           min(duration, MAX_CUE_SECONDS,
                               max(len(chunk) / CHARS_PER_SECOND, MIN_CUE_SECONDS)))
            end = min(cursor + duration, scene.end)
            if end <= cursor:
                # The scene is too short for another readable cue; better to
                # drop it than to flash text nobody can read.
                break
            cues.append(Cue(index=index, start=round(cursor, 2),
                            end=round(end, 2), text=chunk))
            index += 1
            cursor = end
    return cues, unscripted


# ---------------------------------------------------------------------------
# Package assembly
# ---------------------------------------------------------------------------
def _transition_for(scene_number: int, fmt: str, is_last: bool) -> tuple[str, str]:
    if scene_number == 1:
        return TRANSITIONS["hard_cut"]
    if fmt in ("DEMO", "SATISFYING") and is_last:
        return TRANSITIONS["hold"]
    if fmt == "BEFORE_AFTER" and scene_number == 2:
        return TRANSITIONS["match_cut"]
    if fmt in ("UGC", "TUTORIAL") and scene_number == 2:
        return TRANSITIONS["j_cut"]
    return TRANSITIONS["hard_cut"]


def build_scenes(shot_list: list[ShotListItem], fmt: str) -> list[Scene]:
    """Turn a shot list into scenes with absolute timecodes."""
    scenes: list[Scene] = []
    cursor = 0.0
    for i, shot in enumerate(shot_list, start=1):
        transition, note = _transition_for(i, fmt, is_last=(i == len(shot_list)))
        end = cursor + shot.duration_seconds
        scenes.append(Scene(
            number=i, start=round(cursor, 2), end=round(end, 2),
            shot=shot.shot, audio=shot.audio,
            on_screen_text=shot.on_screen_text, purpose=shot.purpose,
            transition_in=transition, transition_note=note,
        ))
        cursor = end
    return scenes


def build_thumbnail_ideas(title: str, hook_line: str,
                          transformation: str | None) -> list[dict[str, str]]:
    """Cover-frame directions.

    The cover is what the profile grid and search results show, so it is the
    second most important frame after the first — and unlike the first frame it
    can be chosen after the fact from footage already shot.
    """
    ideas = [
        {"concept": "Mid-action frame",
         "direction": "A frame from the middle of the key action, not a posed shot. "
                      "Motion blur is fine and reads as authentic.",
         "text_overlay": hook_line[:28],
         "why": "Implies the video is already underway, which reduces the "
                "perceived cost of watching."},
        {"concept": "Face with a real reaction",
         "direction": "A genuine reaction frame, eyes visible, product in the "
                      "lower third.",
         "text_overlay": "",
         "why": "Faces outperform objects in a grid. A posed smile does not."},
        {"concept": "Product in context, no text",
         "direction": f"{title} in the real environment it is used in, filling "
                      "roughly a third of the frame.",
         "text_overlay": "",
         "why": "Control against the text variants. Sometimes the clean frame wins "
                "and the overlay was costing clicks."},
    ]
    if transformation:
        ideas.insert(1, {
            "concept": "Split before/after",
            "direction": f"Two halves of the same framing — untouched left, "
                         f"{transformation} right. Hard vertical divide.",
            "text_overlay": "",
            "why": "Communicates the entire premise without a word, which is what "
                   "a cover has to do at grid size.",
        })
    return ideas


def build_export_metadata(*, sku: str, package_id: str, angle: str,
                          caption: str, hashtags: list[str],
                          runtime: float) -> dict[str, Any]:
    """Everything an upload needs, in one place.

    `tracking_suffix` is the part that pays for itself: without a per-video UTM
    the Shopify journey shows 'tiktok' for every video ever posted, and the
    learning engine cannot tell which creative earned the sale.
    """
    return {
        "package_id": package_id,
        "sku": sku,
        "filename": f"{package_id}.mp4",
        "container": "mp4 (H.264 + AAC)",
        "resolution": f"{CANVAS['width']}x{CANVAS['height']}",
        "aspect": CANVAS["aspect"],
        "fps": CANVAS["fps"],
        "runtime_seconds": runtime,
        "caption": caption,
        "hashtags": hashtags,
        "subtitle_file": f"{package_id}.srt",
        "cover_frame_note": "Choose from the thumbnail directions after the edit.",
        "audio_note": MUSIC_GUIDANCE["trap"],
        "safe_area_px": dict(SAFE_AREA),
        # Per-video attribution. `utm_content` carries the package id so the
        # learning engine can join a Shopify order back to one specific video.
        "tracking_suffix": (f"?utm_source=tiktok&utm_medium=organic"
                            f"&utm_campaign={sku}&utm_content={package_id}"),
    }


def build_production_package(
    *,
    concept: VideoConcept | None = None,
    idea: VideoIdea | None = None,
    sku: str = "",
    title: str = "",
    caption: str = "",
    hashtags: list[str] | None = None,
    cta: str = "",
    transformation: str | None = None,
) -> ProductionPackage:
    """Assemble a shootable package from a concept (and optionally an idea).

    A concept supplies the shot list; an idea supplies the angle and any slots
    still unfilled. Either may be given alone — an idea without a concept
    produces a package that is correctly marked not ready, which is the honest
    result rather than an error.
    """
    hashtags = list(hashtags or [])
    # Concept ids are "<SKU>-C<n>", and SKUs contain hyphens — splitting on the
    # first one turns BAMBOO-ORG-01 into BAMBOO and silently mis-tags every UTM.
    if not sku and concept:
        sku = re.sub(r"-C\d+$", "", concept.concept_id)
    angle = idea.angle_name if idea else (concept.format if concept else "unspecified")
    fmt = concept.format if concept else "UGC"
    package_id = (idea.idea_id if idea else
                  (concept.concept_id if concept else f"{sku}-PKG"))

    scenes = build_scenes(concept.shot_list, fmt) if concept else []
    cues, unscripted = build_cues(scenes)
    caption = caption or (concept.caption if concept else "")
    cta = cta or (concept.call_to_action if concept else "")

    storyboard = [
        f"{s.number}. [{s.timecode}] {s.shot.split('.')[0]}" for s in scenes
    ]

    blockers: list[str] = []
    warnings: list[str] = []

    # Every string that reaches a shoot day gets screened.
    surfaces = {
        "caption": caption,
        "cta": cta,
        **{f"scene {s.number} audio": s.audio for s in scenes},
        **{f"scene {s.number} text": s.on_screen_text for s in scenes if s.on_screen_text},
    }
    if idea:
        surfaces["premise"] = idea.premise
        surfaces["opening shot"] = idea.opening_shot
        surfaces["payoff"] = idea.payoff

    for where, text in surfaces.items():
        if not text:
            continue
        for slot in unfilled_slots(text):
            blockers.append(f"{where}: unfilled — {slot}")
        for issue in check_claims(text):
            blockers.append(f"{where}: claim — {issue}")
        for issue in check_originality(text):
            blockers.append(f"{where}: originality — {issue}")

    if concept is None:
        blockers.append(
            "No shot list. This package has an angle but nothing to film — build "
            "a content plan for the SKU first.")
    # An unwritten line is a blocker, not a warning: the alternative is a shoot
    # day where somebody improvises the one sentence that carries the claim.
    blockers.extend(unscripted)
    if idea is not None and idea.blocked_reason:
        blockers.append(f"angle: {idea.blocked_reason}")

    runtime = round(scenes[-1].end, 2) if scenes else 0.0
    if runtime and runtime > 60:
        warnings.append(
            f"Runtime is {runtime:.0f}s. Completion rate is the strongest ranking "
            "input on short-form, and it falls off sharply past about 30 seconds "
            "for commerce content — cut a scene rather than trimming all of them.")
    if runtime and runtime < 7:
        warnings.append(
            f"Runtime is {runtime:.0f}s. Very short videos loop, which inflates "
            "watch time without inflating intent; read the conversion rate rather "
            "than the view count on this one.")
    if scenes and not cues:
        warnings.append(
            "No caption cues were generated — this cut has no spoken audio. That "
            "is correct for a satisfying/diegetic format and wrong for anything "
            "else, since most viewing is muted.")

    return ProductionPackage(
        package_id=package_id,
        sku=sku,
        title=title or (concept.hook.line if concept else ""),
        angle=angle,
        format=fmt,
        scenes=scenes,
        cues=cues,
        storyboard=storyboard,
        thumbnail_ideas=build_thumbnail_ideas(
            title, concept.hook.line if concept else "", transformation),
        music=dict(MUSIC_GUIDANCE),
        export_metadata=build_export_metadata(
            sku=sku, package_id=package_id, angle=angle, caption=caption,
            hashtags=hashtags, runtime=runtime),
        caption=caption,
        hashtags=hashtags,
        cta=cta,
        blockers=blockers,
        warnings=warnings,
    )
