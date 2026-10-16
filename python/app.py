#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi>=0.115",
#   "uvicorn>=0.30",
#   "httpx>=0.27",
#   "python-dotenv>=1.0",
#   "python-multipart>=0.0.9",
# ]
# ///
"""Play a video file and detect ships and people in it with Jev.

The browser plays the video, grabs frames off the <video> element, and POSTs
them here. This process forwards each frame to TypeSafe's System One endpoint
as a multipart request with a bundle of Box questions (one box per question,
each gated by a companion Noul), then hands the normalized edges back for the
page to draw next to the original video.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import mimetypes
import os
import re
import shutil
import time
import uuid
import webbrowser
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

import cast as cast_builder

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

VERSION = "0.1.0"

JEV_URL = "https://api.typesafe.ai/v1/systemone"

# Box questions need Jev 1.14; jev-preview is its alias.
DEFAULT_MODEL = "jev-preview"

# Images and Box questions are opt-in behind this header.
IMAGE_HEADER = {"TypeSafe-Danger-Zone-Images": "true"}

# Box finds one target per question, and asking for "the second ship from the
# left" tends to land on the same salient vessel every time. Splitting the
# frame into vertical bands and asking for one target per band is what actually
# separates instances, so each slot is a region of the image.
BAND_LAYOUTS: dict[int, list[tuple[str, str, str]]] = {
    1: [("", "`frame`", "whole frame")],
    2: [
        ("L", "the left half of `frame`", "left half"),
        ("R", "the right half of `frame`", "right half"),
    ],
    3: [
        ("L", "the left third of `frame`", "left third"),
        ("M", "the middle third of `frame`", "middle third"),
        ("R", "the right third of `frame`", "right third"),
    ],
    4: [
        ("1", "the leftmost quarter of `frame`", "1st quarter"),
        ("2", "the second quarter from the left of `frame`", "2nd quarter"),
        ("3", "the third quarter from the left of `frame`", "3rd quarter"),
        ("4", "the rightmost quarter of `frame`", "4th quarter"),
    ],
}

# Past four, naming the slices in words stops working ("the seventh tenth")
# and vertical strips get too narrow to hold a person, so the frame is split
# into a 2-row grid described by explicit percentages instead. Ten slots on a
# 512-wide frame would be 51px strips; a 5x2 grid gives 102x192 cells.
# How far the count Choice enumerates before it says "more". This bounds the
# histogram, not the search: slots themselves are uncapped.
COUNT_MAX = 10

GRID_ROWS = 2


def grid_regions(count: int) -> list[tuple[str, str, str]]:
    """Split the frame into `count` cells over two rows.

    An odd count gives the top row the extra cell and widens the bottom row to
    span the frame, rather than leaving a hole: a 4x2 grid truncated to seven
    would never look at the bottom-right quadrant.
    """
    top = -(-count // GRID_ROWS)  # ceil
    per_row = [n for n in (top, count - top) if n]

    cells: list[tuple[str, str, str]] = []
    for row, columns in enumerate(per_row):
        y0 = round(row * 100 / len(per_row))
        y1 = round((row + 1) * 100 / len(per_row))
        for column in range(columns):
            x0 = round(column * 100 / columns)
            x1 = round((column + 1) * 100 / columns)
            cells.append((
                f"{'TB'[row]}{column + 1}",
                f"the region of `frame` between {x0}% and {x1}% of the image "
                f"width from the left edge, and between {y0}% and {y1}% of the "
                f"image height from the top edge",
                f"x {x0}\u2013{x1}% \u00b7 y {y0}\u2013{y1}%",
            ))
    return cells


# What to look for. Each target becomes a Choice (how many?) plus, per region,
# a Noul (is one of these in that region?) and a Box (where in it?).
TARGETS = {
    "ship": {
        "label": "Ship",
        "noun": "ship or boat",
        "plural": "ships or boats",
        "gloss": (
            "a ship, boat, barge, ferry, yacht or any other "
            "watercraft on or in the water"
        ),
    },
    "person": {
        "label": "Person",
        "noun": "person",
        "plural": "people",
        "gloss": (
            "a human being, whether their whole body or only part of "
            "it (head, face, torso) is visible"
        ),
    },
}

# Identity is a Choice, not a Box. Asking Box for "the person who is Worf"
# scores 0.06-0.15 and returns half the frame; locating the person by region
# and then naming them against a closed cast reaches 0.68-0.97. Each entry
# carries a visual description because names alone are much weaker.
#
# This is the fallback cast. `cast.py` writes cast.json from the video's own
# cast list, and that wins when it exists.
DEFAULT_CAST = {
    "jean_luc_picard": ("Captain Jean-Luc Picard", "Picard", "older, bald, white, red-shouldered command uniform"),
    "william_riker": ("Commander William Riker", "Riker", "tall white man with dark hair, often bearded, red-shouldered command uniform"),
    "data": ("Lieutenant Commander Data", "Data", "an android with pale golden-white skin and yellow eyes, gold-shouldered uniform"),
    "worf": ("Lieutenant Worf", "Worf", "a Klingon with a heavily ridged forehead and long dark hair, gold-shouldered uniform with a metallic sash across the chest"),
    "deanna_troi": ("Counselor Deanna Troi", "Troi", "woman with long dark curly hair, usually in a coloured jumpsuit rather than a uniform"),
    "geordi_la_forge": ("Lieutenant Geordi La Forge", "La Forge", "Black man wearing a metallic VISOR across his eyes"),
    "beverly_crusher": ("Doctor Beverly Crusher", "Dr Crusher", "white woman with red hair, blue-shouldered medical uniform"),
    "katherine_pulaski": ("Doctor Katherine Pulaski", "Dr Pulaski", "older white woman with short fair hair, blue-shouldered medical uniform"),
    "wesley_crusher": ("Wesley Crusher", "Wesley", "a teenage boy, usually in a grey-and-black cadet jumpsuit"),
    "guinan": ("Guinan", "Guinan", "Black woman wearing a very large wide-brimmed hat"),
    "miles_o_brien": ("Chief Miles O'Brien", "O'Brien", "white man with short brown hair, gold-shouldered uniform"),
}

OTHER_DESCRIPTION = (
    "Someone else, a background crew member, an alien guest character, "
    "or nobody identifiable."
)

CAST_FILE = HERE / "cast.json"

# Only one rebuild at a time; each one does a dozen rate-limited searches.
CAST_LOCK = asyncio.Lock()

# Debug cases land here, one JSON each.
RUNTIME_DIR = HERE.parent / "runtime"

# A short history of what the backend has been doing, streamed to the page.
# Cast lookups are slow and mostly invisible; this is where they show up.
EVENTS: deque[dict[str, Any]] = deque(maxlen=400)
EVENT_SEQ = itertools.count(1)
EVENT_LISTENERS: set[asyncio.Queue] = set()
EVENT_LOOP: asyncio.AbstractEventLoop | None = None


def emit(kind: str, message: str, level: str = "info", **detail: Any) -> None:
    """Record an event and push it to any page watching.

    Safe from a worker thread: cast lookups run off the loop, so the fan-out
    is scheduled back onto it rather than touching the queues directly.
    """
    event = {
        "seq": next(EVENT_SEQ),
        "at": time.time(),
        "level": level,
        "kind": kind,
        "message": message,
        "detail": detail or None,
    }
    EVENTS.append(event)

    loop = EVENT_LOOP
    if loop is None:
        return

    def fan_out() -> None:
        for queue in list(EVENT_LISTENERS):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    try:
        if asyncio.get_running_loop() is loop:
            fan_out()
            return
    except RuntimeError:
        pass
    loop.call_soon_threadsafe(fan_out)

# The last frame and the exact exchange it produced, kept so a snapshot can be
# taken after the fact: by the time you notice a bad result, the call is over.
LAST_CALL: dict[str, Any] = {}

# The last cast lookup, so a failed one is visible in a case file even though
# it produced no detection of its own.
LAST_CAST_LOOKUP: dict[str, Any] = {}


def load_cast() -> tuple[dict[str, dict[str, str]], str | None]:
    """Read cast.json if `cast.py` has written one, else the built-in cast."""
    source = None
    rows = DEFAULT_CAST

    if CAST_FILE.is_file():
        try:
            data = json.loads(CAST_FILE.read_text())
            found = data.get("characters")
            # An empty map is a real answer: this video has no cast we could
            # find. Falling back to the built-in list would put another show's
            # characters on it, which is what we are trying to stop.
            if isinstance(found, dict):
                rows = {
                    slug: (
                        info.get("formal") or info.get("name") or slug,
                        info.get("name") or info.get("formal") or slug,
                        (info.get("description") or "").rstrip("."),
                    )
                    for slug, info in found.items()
                }
                season = data.get("season")
                source = data.get("title") or "cast.json"
                if season:
                    source = f"{source} season {season}"
                if not rows:
                    source = f"none found for {source}"
        except (OSError, ValueError) as error:  # malformed file, keep the default
            print(f"WARNING: ignoring {CAST_FILE.name}: {error}")

    cast = {
        slug: {"formal": formal, "name": name, "look": look}
        for slug, (formal, name, look) in rows.items()
    }
    return cast, source


CHARACTERS: dict[str, dict[str, str]] = {}
CHARACTER_NAMES: dict[str, str] = {}
CAST_SOURCE: str | None = None


def apply_cast() -> None:
    """(Re)read cast.json into the module globals the questions are built from."""
    global CHARACTERS, CHARACTER_NAMES, CAST_SOURCE
    CHARACTERS, CAST_SOURCE = load_cast()
    CHARACTER_NAMES = {slug: entry["name"] for slug, entry in CHARACTERS.items()}
    CHARACTER_NAMES["other"] = "Unknown"


apply_cast()


def cast_criteria() -> dict[str, str]:
    """Choice options: the name, plus how they look when that is known."""
    criteria = {
        slug: (f"{entry['formal']}: {entry['look']}." if entry["look"] else f"{entry['formal']}.")
        for slug, entry in CHARACTERS.items()
    }
    criteria["other"] = OTHER_DESCRIPTION
    return criteria


def character_parts(key: str) -> tuple[str, str]:
    entry = CHARACTERS[key]
    return entry["formal"], entry["look"]


def build_character_questions() -> dict[str, Any]:
    """One Box per named character, each gated by its own Noul.

    This is the direct way to ask, and it is weaker than locating a person by
    region and naming them with a Choice: a bare "the person who is Worf"
    scores 0.06-0.15. Repeating the visual description inside the Box question
    is what makes it usable at all, so it is asked the long way here.
    """
    questions: dict[str, Any] = {}
    for key in CHARACTERS:
        formal, look = character_parts(key)
        questions[f"char_{key}_present"] = {
            "type": "noul",
            "instructions": (
                f"Is {formal} visible in `frame`? Look for {look}."
                if look else f"Is {formal} visible in `frame`?"
            ),
        }
        questions[f"char_{key}_box"] = {
            "type": "box",
            "instructions": (
                f"the person in `frame` who is {formal}: {look}"
                if look else f"the person in `frame` who is {formal}"
            ),
        }
    return questions


def parse_character_answers(answers: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in CHARACTERS:
        box = answers.get(f"char_{key}_box") or {}
        edges = box.get("box")
        if not edges:
            continue
        presence = answers.get(f"char_{key}_present") or {}
        formal, _ = character_parts(key)
        found.append({
            "kind": "character",
            "identity": None,
            "label": CHARACTER_NAMES.get(key, key),
            "region": formal,
            "index": 0,
            "box": edges,
            "confidence": box.get("confidence", 0.0),
            "presence": presence.get("noul", 0.0),
        })
    return found


CONFIG: dict[str, Any] = {
    "video_path": None,
    "video_name": None,
    "model": DEFAULT_MODEL,
    "max_ship": 10,
    "max_person": 10,
}


def build_epoch() -> float:
    """Newest mtime across the sources.

    There is no build step, so the moment the served code last changed is the
    closest honest thing to a build time — and it stays true after an edit
    instead of going stale like a baked-in constant.
    """
    sources = [Path(__file__), *STATIC.glob("*")]
    return max(path.stat().st_mtime for path in sources if path.is_file())


def count_criteria(spec: dict[str, str]) -> dict[str, str]:
    """One option per exact number, so the answer carries a distribution.

    A `none` / `one` / `several` Choice collapses everything above one into a
    single bucket. Numbering the options up to the slot ceiling means the
    probabilities say how many are actually there, and the `more` bucket is a
    signal that the slot count is too low to box them all.
    """
    criteria = {"0": f"No {spec['plural']} are visible at all."}
    for number in range(1, COUNT_MAX + 1):
        noun = spec["noun"] if number == 1 else spec["plural"]
        criteria[str(number)] = f"Exactly {number} {noun} visible."
    criteria["more"] = f"More than {COUNT_MAX} {spec['plural']} visible."
    return criteria


def bands(count: int) -> list[tuple[str, str, str]]:
    count = max(1, count)
    layout = BAND_LAYOUTS.get(count)
    return layout if layout else grid_regions(count)


def build_questions(slots: dict[str, int]) -> dict[str, Any]:
    """One request, many questions: Jev evaluates them in parallel on one frame."""
    questions: dict[str, Any] = {}

    for kind, spec in TARGETS.items():
        count = slots.get(kind, 0)
        if count < 1:
            continue

        questions[f"{kind}_count"] = {
            "type": "choice",
            "instructions": (
                f"How many {spec['plural']} are visible in the image `frame`? "
                f"Count {spec['gloss']}."
            ),
            "criteria": count_criteria(spec),
        }

        whole = count == 1
        for index, (_, region, _label) in enumerate(bands(count), start=1):
            # Box has no "not found" answer, so this Noul decides whether the
            # box beside it means anything. Both ask about the same region.
            questions[f"{kind}_{index}_present"] = {
                "type": "noul",
                "instructions": (
                    f"Is there a {spec['noun']} visible in `frame`?"
                    if whole
                    else f"Is there a {spec['noun']} whose centre lies in {region}?"
                ),
            }
            questions[f"{kind}_{index}_box"] = {
                "type": "box",
                "instructions": (
                    f"the most prominent {spec['noun']} in `frame`"
                    if whole
                    else f"the {spec['noun']} whose centre lies in {region}"
                ),
            }

            # Always asked, when there is a cast to ask against.
            if kind == "person" and CHARACTERS:
                # Same wording as the Box above, so both land on one person.
                questions[f"{kind}_{index}_who"] = {
                    "type": "choice",
                    "instructions": (
                        "The image `frame` is from Star Trek: The Next "
                        "Generation. Which character is "
                        + ("the most prominent person in `frame`" if whole
                           else f"the person whose centre lies in {region}")
                        + "?"
                    ),
                    "criteria": cast_criteria(),
                }

    # Every frame gets a Box per named character, alongside the region search.
    questions.update(build_character_questions())

    return questions


def parse_answers(
    answers: dict[str, Any],
    slots: dict[str, int],
) -> dict[str, Any]:
    """Turn Jev's answers into candidate boxes the page can filter and draw."""
    counts: dict[str, Any] = {}
    candidates: list[dict[str, Any]] = []

    for kind, spec in TARGETS.items():
        count = slots.get(kind, 0)
        if count < 1:
            continue

        answer = answers.get(f"{kind}_count") or {}
        counts[kind] = {
            "label": spec["label"],
            "choice": answer.get("choice"),
            "confidence": answer.get("confidence"),
            "probabilities": answer.get("probabilities") or {},
        }

        for index, (tag, _region, label) in enumerate(bands(count), start=1):
            box = answers.get(f"{kind}_{index}_box") or {}
            edges = box.get("box")
            if not edges:
                continue

            presence = answers.get(f"{kind}_{index}_present") or {}

            identity = None
            who = answers.get(f"{kind}_{index}_who")
            if who and who.get("choice"):
                pick = who["choice"]
                identity = {
                    "key": pick,
                    "name": CHARACTER_NAMES.get(pick, pick),
                    "probability": (who.get("probabilities") or {}).get(pick, 0.0),
                    "confidence": who.get("confidence", 0.0),
                    "runners_up": sorted(
                        ((CHARACTER_NAMES.get(k, k), v)
                         for k, v in (who.get("probabilities") or {}).items()
                         if k != pick and v >= 0.05),
                        key=lambda kv: -kv[1],
                    )[:2],
                }

            candidates.append({
                "kind": kind,
                "identity": identity,
                "label": f"{spec['label']} {tag}".strip(),
                "region": label,
                "index": index,
                "box": edges,
                "confidence": box.get("confidence", 0.0),
                "presence": presence.get("noul", 0.0),
            })

    candidates.extend(parse_character_answers(answers))

    return {"counts": counts, "candidates": candidates}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global EVENT_LOOP
    EVENT_LOOP = asyncio.get_running_loop()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0))
    emit("server", f"Started {VERSION}, model {CONFIG['model']}",
         video=CONFIG["video_name"], cast=CAST_SOURCE)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="Jev video play", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "version": VERSION,
        # Seconds since the epoch; the page formats it in the viewer's locale.
        "build_epoch": build_epoch(),
        "video_url": "/video" if CONFIG["video_path"] else None,
        "video_name": CONFIG["video_name"],
        "model": CONFIG["model"],
        "max_slots": {"ship": CONFIG["max_ship"], "person": CONFIG["max_person"]},
        "targets": {k: v["label"] for k, v in TARGETS.items()},
        "count_max": COUNT_MAX,
        "can_transcode": ffmpeg_available(),
        "cast": CHARACTER_NAMES,
        "cast_source": CAST_SOURCE,
        "has_key": bool(os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")),
    }


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


@app.get("/stream")
async def stream() -> StreamingResponse:
    """Transcode the current file on the fly for browsers that cannot play it.

    Matroska, AC-3 and friends are common in rips and unplayable in a browser;
    ffmpeg re-wraps to fragmented MP4 as it goes. The output has no index, so
    the page cannot seek a stream, only play it.
    """
    path: Path | None = CONFIG["video_path"]
    if path is None:
        raise HTTPException(status_code=404, detail="No file to stream.")
    if not ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg is not installed.")

    emit("stream", f"Transcoding {path.name} with ffmpeg", level="warn")
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", str(path),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )

    async def chunks():
        try:
            while True:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            # The page navigating away should not leave ffmpeg running.
            if process.returncode is None:
                process.kill()
                await process.wait()

    return StreamingResponse(chunks(), media_type="video/mp4", headers={
        "cache-control": "no-store",
    })


@app.get("/video")
async def video(request: Request) -> Response:
    """Serve the video file with Range support, so the page can seek and scrub."""
    path: Path | None = CONFIG["video_path"]
    if path is None:
        raise HTTPException(status_code=404, detail="No video file was passed on the command line.")

    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    start, end, status = 0, size - 1, 200

    raw_range = request.headers.get("range", "")
    if raw_range.startswith("bytes="):
        first, _, last = raw_range.removeprefix("bytes=").split(",")[0].strip().partition("-")
        try:
            if first:
                start = int(first)
                if last:
                    end = min(int(last), size - 1)
            elif last:
                start = max(0, size - int(last))
        except ValueError:
            start, end = 0, size - 1
        else:
            if start > end or start >= size:
                return Response(
                    status_code=416,
                    headers={"content-range": f"bytes */{size}"},
                )
            status = 206

    length = end - start + 1
    headers = {"accept-ranges": "bytes", "content-length": str(length)}
    if status == 206:
        headers["content-range"] = f"bytes {start}-{end}/{size}"

    def chunks():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(chunks(), status_code=status, headers=headers, media_type=media_type)


def write_case(note: str, view: Any, thresholds: Any) -> Path:
    """Write one self-contained debug case: runtime/YYYYMMDD-HHMMSS-video.json.

    The frame travels base64 inside the file, and the stored request inlines it
    as a `data_base64` asset, so the `request` object can be POSTed to Jev
    exactly as it stands. One file is enough to reproduce the call; nothing
    from this app is needed.
    """
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(LAST_CALL["taken_at"]))
    label = re.sub(r"[^a-z0-9]+", "-", Path(LAST_CALL["video_name"] or "frame").stem.lower()).strip("-")
    path = RUNTIME_DIR / f"{stamp}-{label[:60] or 'frame'}.json"

    frame_b64 = base64.b64encode(LAST_CALL["frame"]).decode("ascii")

    # The same request, with the image inlined instead of sent as a file part.
    sent = LAST_CALL["request"]
    reference = next(iter(sent.get("assets") or {}), None)
    replayable = json.loads(json.dumps(sent))
    if reference:
        replayable["assets"][reference] = {
            "type": "image", "format": "jpg", "data_base64": frame_b64,
        }

    case = {
        "note": note,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(LAST_CALL["taken_at"])),
        "reproduce": {
            "how": (
                "POST the `request` object below to the endpoint with an "
                "Authorization bearer token and the images header. It already "
                "contains the frame, so nothing else is needed. For example: "
                "jq '.request' THIS_FILE | curl -s $endpoint "
                "-H \"Authorization: Bearer $JEV_API_KEY\" "
                "-H 'TypeSafe-Danger-Zone-Images: true' "
                "-H 'Content-Type: application/json' --data-binary @-"
            ),
            "endpoint": JEV_URL,
            "headers": {"TypeSafe-Danger-Zone-Images": "true"},
            "sent_as": "multipart/form-data (the frame was a file part; it is inlined here)",
        },
        "app": {
            "version": VERSION,
            "build_epoch": build_epoch(),
            "model_requested": LAST_CALL["model"],
            "model_answered": LAST_CALL["response"].get("model"),
        },
        "video": {
            "name": LAST_CALL["video_name"],
            "served_from_command_line": CONFIG["video_name"],
            "timestamp_seconds": LAST_CALL["timestamp"],
        },
        "settings": {
            "slots": LAST_CALL["slots"],
            "questions": len(sent.get("questions") or {}),
            "thresholds": thresholds,
        },
        # The cast as it was when this call went out.
        "cast": {
            "source": LAST_CALL["cast_source"],
            "characters": {
                slug: {"formal": e["formal"], "name": e["name"], "look": e["look"]}
                for slug, e in LAST_CALL["cast"].items()
            },
            "without_description": [
                e["formal"] for e in LAST_CALL["cast"].values() if not e["look"]
            ],
        },
        "result": {
            "latency_ms": LAST_CALL["latency_ms"],
            "usage": LAST_CALL["response"].get("usage"),
            "counts": LAST_CALL["parsed"]["counts"],
            "candidates": LAST_CALL["parsed"]["candidates"],
        },
        "as_drawn": view,
        "cast_lookup": dict(LAST_CAST_LOOKUP) or None,
        "request": replayable,
        "response": LAST_CALL["response"],
        "frame": {
            "media_type": "image/jpeg",
            "bytes": len(LAST_CALL["frame"]),
            "base64": frame_b64,
        },
    }

    path.write_text(json.dumps(case, indent=2, default=str) + "\n")
    return path


@app.post("/api/snapshot")
async def api_snapshot(
    note: str = Form(""),
    view: str = Form("[]"),
    thresholds: str = Form("{}"),
    video_name: str = Form(""),
) -> JSONResponse:
    if not LAST_CALL:
        raise HTTPException(status_code=400, detail="Nothing to snapshot yet. Run a detection first.")

    # Snapshotting captures the last detection, which is not necessarily this
    # video: a failed cast lookup leaves the player on a new file with no
    # detection of its own, and the case would quietly describe the old one.
    stored = LAST_CALL.get("video_name") or ""
    if video_name and stored and video_name != stored:
        emit("snapshot",
             f"Refused: no detection for {video_name!r} (last was {stored!r})",
             level="warn")
        raise HTTPException(
            status_code=409,
            detail=(
                f"No detection has run for {video_name!r}. The last one was "
                f"{stored!r}, and snapshotting now would describe that instead. "
                "Run a detection on this video first."
            ),
        )

    def decode(raw: str, fallback: Any) -> Any:
        try:
            return json.loads(raw)
        except ValueError:
            return fallback

    path = await asyncio.to_thread(
        write_case, note.strip(), decode(view, []), decode(thresholds, {}),
    )
    emit("snapshot", f"Wrote runtime/{path.name}", note=note.strip() or None)
    return JSONResponse({
        "path": str(path),
        "name": path.name,
        "url": f"/runtime/{path.name}",
        "bytes": path.stat().st_size,
    })


@app.post("/api/response")
async def api_response() -> JSONResponse:
    """Save just Jev's raw answer for the last frame.

    Lighter than a full case: no frame, no settings, no cast — the response as
    it came back, for when you only want the numbers.
    """
    if not LAST_CALL:
        raise HTTPException(status_code=400, detail="Nothing to save yet. Run a detection first.")

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(LAST_CALL["taken_at"]))
    label = re.sub(r"[^a-z0-9]+", "-", Path(LAST_CALL["video_name"] or "frame").stem.lower()).strip("-")
    path = RUNTIME_DIR / f"{stamp}-{label[:60] or 'frame'}-response.json"
    path.write_text(json.dumps(LAST_CALL["response"], indent=2) + "\n")

    emit("snapshot", f"Wrote runtime/{path.name}")
    return JSONResponse({"path": str(path), "name": path.name, "bytes": path.stat().st_size})


@app.get("/runtime/{name}")
async def runtime_file(name: str) -> FileResponse:
    path = (RUNTIME_DIR / name).resolve()
    if path.parent != RUNTIME_DIR.resolve() or not path.is_file():
        raise HTTPException(status_code=404, detail="No such case file.")
    return FileResponse(path, filename=name, media_type="application/json")


def clear_cast(title: str, season: int | None) -> None:
    """Record that this video has no cast, and persist that.

    Writing the empty result matters as much as clearing memory: leaving the
    previous show's cast.json behind would bring it back on the next start.
    """
    CAST_FILE.write_text(json.dumps({
        "title": title, "season": season, "source_page": None, "characters": {},
    }, indent=2) + "\n")
    apply_cast()
    emit("cast", f"Cast cleared — nothing found for {title!r}", level="warn")


@app.get("/api/events")
async def api_events(since: int = 0) -> JSONResponse:
    return JSONResponse({"events": [e for e in EVENTS if e["seq"] > since]})


@app.get("/api/events/stream")
async def api_events_stream() -> StreamingResponse:
    """Server-sent events: the recent history, then everything as it happens."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    EVENT_LISTENERS.add(queue)

    async def pump():
        try:
            for event in list(EVENTS)[-80:]:
                yield f"data: {json.dumps(event)}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20)
                except TimeoutError:
                    yield ": keep-alive\n\n"   # keeps proxies from closing us
                    continue
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            EVENT_LISTENERS.discard(queue)

    return StreamingResponse(pump(), media_type="text/event-stream", headers={
        "cache-control": "no-store",
        "x-accel-buffering": "no",
    })


@app.post("/api/cast")
async def api_cast(request: Request, name: str = Form(...)) -> JSONResponse:
    """Point the cast at another video.

    cast.json is read once at startup, so opening a different file in the page
    would otherwise keep asking about the previous show's characters.
    """
    title, season, year = cast_builder.parse_filename(name)
    wanted = f"{title} season {season}" if season else title
    LAST_CAST_LOOKUP.update({
        "video": name, "title": title, "season": season, "year": year,
        "status": "running", "detail": None, "at": time.time(),
    })
    emit("cast", f"Lookup requested for {title!r}", season=season, year=year, video=name)

    async with CAST_LOCK:
        # Already the right show (perhaps from a previous load): just re-read
        # the file, which also picks up a cast.py run made while we were up.
        apply_cast()
        if CAST_SOURCE and CAST_SOURCE.lower() == wanted.lower():
            LAST_CAST_LOOKUP.update({"status": "unchanged", "detail": None})
            emit("cast", f"Already loaded: {CAST_SOURCE}", characters=len(CHARACTERS))
            return JSONResponse({
                "status": "unchanged",
                "source": CAST_SOURCE,
                "characters": CHARACTER_NAMES,
            })

        key = cast_builder.brave_key()
        if not key:
            LAST_CAST_LOOKUP.update({"status": "skipped", "detail": "No BRAVE_SEARCH_API_KEY."})
            emit("cast", "No BRAVE_SEARCH_API_KEY; cast cleared", level="warn")
            clear_cast(title, season)
            return JSONResponse(
                {"status": "skipped",
                 "detail": "No BRAVE_SEARCH_API_KEY, so there is no cast for this video.",
                 "source": CAST_SOURCE, "characters": CHARACTER_NAMES},
                status_code=200,
            )

        try:
            started_at = time.perf_counter()
            payload = await asyncio.to_thread(
                cast_builder.build_cast, name, key=key,
                log=lambda line: emit("cast", str(line).strip()) if str(line).strip() else None,
            )
        except (LookupError, RuntimeError, httpx.HTTPError) as error:
            LAST_CAST_LOOKUP.update({"status": "failed", "detail": str(error)[:300]})
            emit("cast", f"Lookup failed: {error}", level="error")
            clear_cast(title, season)
            return JSONResponse(
                {"status": "failed", "detail": str(error)[:300],
                 "source": CAST_SOURCE, "characters": CHARACTER_NAMES},
                status_code=200,
            )

        CAST_FILE.write_text(json.dumps(payload, indent=2) + "\n")
        apply_cast()
        LAST_CAST_LOOKUP.update({"status": "rebuilt", "detail": None})
        described = sum(1 for e in CHARACTERS.values() if e["look"])
        emit("cast", f"Loaded {CAST_SOURCE}: {len(CHARACTERS)} characters, "
                     f"{described} described",
             seconds=round(time.perf_counter() - started_at, 1),
             page=payload.get("source_page"))

    return JSONResponse({
        "status": "rebuilt",
        "source": CAST_SOURCE,
        "characters": CHARACTER_NAMES,
    })


@app.post("/api/detect")
async def detect(
    request: Request,
    frame: UploadFile,
    model: str = Form(DEFAULT_MODEL),
    max_ship: int = Form(3),
    max_person: int = Form(3),
    timestamp: float = Form(0.0),
    video_name: str = Form(""),
) -> JSONResponse:
    """Send one frame to Jev and return the boxes it found."""
    api_key = os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="Set JEV_API_KEY in .env or the environment.")

    payload = await frame.read()
    if not payload:
        raise HTTPException(status_code=400, detail="Empty frame.")

    slots = {
        "ship": max(0, max_ship),
        "person": max(0, max_person),
    }
    questions = build_questions(slots)
    if not questions:
        raise HTTPException(status_code=400, detail="Nothing to look for.")

    # The reference has to be a string that cannot show up by accident, so use
    # a fresh UUID per request, as the docs recommend.
    ref = str(uuid.uuid4())
    body = {
        "model": model,
        "state": {"frame": ref},
        "assets": {ref: {"type": "image", "part": "frame-image"}},
        "questions": questions,
    }

    client: httpx.AsyncClient = request.app.state.http
    started = time.perf_counter()

    last_error: tuple[int, str] | None = None
    for attempt in range(2):
        try:
            response = await client.post(
                JEV_URL,
                headers={"Authorization": f"Bearer {api_key}", **IMAGE_HEADER},
                files={
                    "request": (None, json.dumps(body), "application/json"),
                    "frame-image": ("frame.jpg", payload, "image/jpeg"),
                },
            )
        except httpx.HTTPError as exc:
            last_error = (502, f"Could not reach Jev: {exc}")
            break

        if response.status_code == 429 and attempt == 0:
            # Respect retry-after once, then give up and let the page back off.
            try:
                delay = float(response.headers.get("retry-after", "1"))
            except ValueError:
                delay = 1.0
            await asyncio.sleep(min(delay, 5.0))
            last_error = (429, response.text)
            continue

        if response.status_code != 200:
            last_error = (response.status_code, response.text)
            break

        latency_ms = round((time.perf_counter() - started) * 1000)
        data = response.json()
        parsed = parse_answers(data.get("answers") or {}, slots)

        LAST_CALL.update({
            # Frozen here, not read at snapshot time: by then the page may have
            # loaded another video and rebuilt the cast.
            "video_name": video_name or CONFIG["video_name"],
            "cast_source": CAST_SOURCE,
            "cast": {
                slug: dict(entry) for slug, entry in CHARACTERS.items()
            },
            "frame": payload,
            "request": body,
            "response": data,
            "timestamp": timestamp,
            "latency_ms": latency_ms,
            "model": model,
            "slots": slots,
            "parsed": parsed,
            "taken_at": time.time(),
        })

        usage = data.get("usage") or {}
        counts = {k: v.get("choice") for k, v in parsed["counts"].items()}
        emit("detect",
             f"Frame at {timestamp:.1f}s — {len(questions)} questions, "
             f"{latency_ms} ms, {usage.get('input_tokens', 0)} tokens",
             counts=counts, candidates=len(parsed["candidates"]),
             model=data.get("model"), slots=slots)

        return JSONResponse({
            "model": data.get("model"),
            "usage": data.get("usage") or {},
            "latency_ms": latency_ms,
            "timestamp": timestamp,
            "questions": len(questions),
            "raw": data,
            **parsed,
        })

    status, detail = last_error or (502, "Jev request failed.")
    emit("detect", f"Jev request failed ({status})", level="error", detail=detail[:200])
    return JSONResponse(
        {"error": "Jev request failed", "status": status, "detail": detail[:2000]},
        status_code=502,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", nargs="?", help="Video file to play. Omit to pick one in the page.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Jev model (default {DEFAULT_MODEL}).")
    parser.add_argument("--max-ships", type=int, default=10, help="Frame regions searched for ships (default 10).")
    parser.add_argument("--max-people", type=int, default=10, help="Frame regions searched for people (default 10).")
    parser.add_argument("--open", action="store_true", help="Open the page in your browser.")
    parser.add_argument("--version", action="version", version=f"jev video play {VERSION}")
    args = parser.parse_args()

    # python/.env wins, then the shared one beside it at the project root.
    load_dotenv(HERE / ".env")
    load_dotenv(HERE.parent / ".env")
    load_dotenv()

    if args.video:
        path = Path(args.video).expanduser().resolve()
        if not path.is_file():
            parser.error(f"No such video file: {path}")
        CONFIG["video_path"] = path
        CONFIG["video_name"] = path.name

    CONFIG["model"] = args.model
    CONFIG["max_ship"] = max(0, args.max_ships)
    CONFIG["max_person"] = max(0, args.max_people)

    if not (os.environ.get("JEV_API_KEY") or os.environ.get("TYPESAFE_API_KEY")):
        print("WARNING: no JEV_API_KEY found. Detection calls will fail.")

    url = f"http://{args.host}:{args.port}/"
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(build_epoch()))
    print(f"\n  Jev video play {VERSION} (built {stamp}) -> {url}")
    print(f"  model: {CONFIG['model']}")
    print(f"  video: {CONFIG['video_name'] or '(choose one in the page)'}\n")

    if args.open:
        webbrowser.open(url)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
