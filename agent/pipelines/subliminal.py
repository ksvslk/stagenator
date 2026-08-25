"""Subliminal Words level pipeline.

One letter-LAYOUT drives everything (single source of truth):
  layout -> mask PNG   (ControlNet input for the Runpod ComfyUI worker)
  layout -> solution SVG (strict word_level_svg.js contract: 1024 canvas,
            per-letter x/y normalized, scale/rotation/skew/opacity, weight in {200,400,700})
  layout -> the answer word itself

Flow: design (Gemini word+scene, dedup vs existing) -> render mask (PIL)
   -> Runpod generate (difficulty = ControlNet strength) -> QA (Gemini vision)
   -> submit exactly like the admin dashboard (Storage + packs tx + counters).
"""

import base64
import io
import json
import logging
import math
import random
import time
from pathlib import Path

from google.cloud import firestore, storage

from agent import config, state
from agent.tools import genai_client, runpod

log = logging.getLogger("stagenator.subliminal")

CANVAS = 1024
FONTS = {
    200: Path(__file__).parent.parent / "assets/fonts/DejaVuSans-ExtraLight.ttf",
    400: Path(__file__).parent.parent / "assets/fonts/DejaVuSans.ttf",
    700: Path(__file__).parent.parent / "assets/fonts/DejaVuSans-Bold.ttf",
}
PACK_ID = "fresh-drops"  # the agent's own pack (auto-created on first level)


# ------------------------------------------------------------------ design ----

def design_level(existing_words: set[str]) -> dict | None:
    """Gemini proposes word + scene prompt; code builds the letter layout."""
    reply = genai_client.generate_json(
        "You design levels for 'Subliminal Words' — a puzzle game where a word is hidden "
        "inside a photorealistic image; players stare until the word pops out.\n"
        f"Words already used (do NOT repeat): {sorted(existing_words)[:200]}\n"
        "Propose ONE new level: a short, punchy English word (3-7 letters, uppercase, "
        "concrete noun or vivid concept) and a photorealistic scene prompt that thematically "
        "hints at the word without depicting it literally as text.\n"
        'Reply JSON: {"word": "...", "prompt": "...", "theme": "..."}'
    )
    if not reply or not reply.get("word") or not reply.get("prompt"):
        return None
    word = reply["word"].strip().upper()
    if not (3 <= len(word) <= 8) or word.lower() in existing_words:
        return None
    return {"word": word, "prompt": reply["prompt"], "theme": reply.get("theme", "")}


def build_layout(word: str) -> list[dict]:
    """Spread the word's letters across the canvas with mild, readable chaos.

    Every field matches the word_level_svg.js contract ranges.
    """
    n = len(word)
    letters = []
    margin = 0.12
    for i, ch in enumerate(word):
        x = margin + (i + 0.5) * (1 - 2 * margin) / n + random.uniform(-0.02, 0.02)
        y = 0.5 + random.uniform(-0.18, 0.18)
        letters.append(
            {
                "letter": ch,
                "x": round(min(max(x, 0.0), 1.0), 4),
                "y": round(min(max(y, 0.0), 1.0), 4),
                "scale": round(random.uniform(2.2, 3.4), 3),  # of base font size
                "rotation": round(random.uniform(-24, 24), 2),
                "skewX": round(random.uniform(-8, 8), 2),
                "opacity": 1.0,
                "weight": random.choice([400, 700, 700]),
            }
        )
    return letters


# --------------------------------------------------------------- rendering ----

BASE_FONT_SIZE = 96  # scaled per letter; stays inside the 24..512 contract range


def render_mask(layout: list[dict]) -> bytes:
    """Rasterize the layout: black letters on white, 1024x1024 (ControlNet input)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (CANVAS, CANVAS), 255)
    for letter in layout:
        size = int(BASE_FONT_SIZE * letter["scale"])
        font = ImageFont.truetype(str(FONTS[letter["weight"]]), size)
        # draw each letter on its own layer so rotation is per-letter
        tile = Image.new("L", (size * 2, size * 2), 0)
        d = ImageDraw.Draw(tile)
        d.text((size * 0.5, size * 0.35), letter["letter"], font=font, fill=255)
        tile = tile.rotate(-letter["rotation"], resample=Image.BICUBIC, expand=False)
        cx, cy = int(letter["x"] * CANVAS), int(letter["y"] * CANVAS)
        img.paste(0, (cx - size, cy - size), mask=tile)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def build_solution_svg(layout: list[dict], word: str) -> str:
    """Serialize the same layout as the game's solution SVG (word-levels contract)."""
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}" '
        f'width="{CANVAS}" height="{CANVAS}" data-answer="{word}" data-generator="stagenator">'
    ]
    for letter in layout:
        x, y = letter["x"] * CANVAS, letter["y"] * CANVAS
        size = BASE_FONT_SIZE * letter["scale"]
        transform = (
            f"translate({x:.1f} {y:.1f}) rotate({letter['rotation']:.1f}) "
            f"skewX({letter['skewX']:.1f})"
        )
        parts.append(
            f'<text x="0" y="0" text-anchor="middle" dominant-baseline="middle" '
            f'font-family="DejaVu Sans, sans-serif" font-size="{size:.0f}" '
            f'font-weight="{letter["weight"]}" opacity="{letter["opacity"]}" '
            f'transform="{transform}">{letter["letter"]}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------- QA + dedup ----

def existing_words() -> set[str]:
    """All level names in all packs (level name == the hidden word, per dashboard)."""
    gdb = state.game_db("subliminal-words")
    words: set[str] = set()
    for pack in gdb.collection("packs").stream():
        for level in pack.reference.collection("levels").stream():
            name = (level.to_dict() or {}).get("name", "")
            if name:
                words.add(name.strip().lower())
    return words


def qa_puzzle(puzzle_png: bytes, word: str) -> dict:
    """Gemini vision check: is the word findable-but-hidden, image quality ok?"""
    reply = genai_client.generate_json_with_image(
        f"This is a 'hidden word' puzzle image. The hidden word is {word!r}.\n"
        "Judge it: 1) is the image photorealistic and appealing? 2) is the word "
        "present but SUBTLE (not printed like text, not totally invisible)?\n"
        'Reply JSON: {"pass": true/false, "visibility": "invisible|subtle|obvious", "note": "..."}',
        puzzle_png,
    )
    return reply or {"pass": False, "note": "QA call failed"}


# ------------------------------------------------------------------ submit ----

def submit_level(word: str, puzzle_png: bytes, solution_svg: str, meta: dict) -> dict:
    """Replicates LevelFormPage.jsx: Storage uploads + packs transaction."""
    gdb = state.game_db("subliminal-words")
    bucket = storage.Client(project="subliminal-words").bucket("subliminal-words.firebasestorage.app")
    ts = int(time.time() * 1000)

    pack_ref = gdb.collection("packs").document(PACK_ID)
    pack = pack_ref.get()
    if not pack.exists:
        pack_ref.set(
            {
                "categoryId": "words",
                "name": "Fresh Drops",
                "description": "New levels, served daily by Stagenator",
                "totalLevels": 0,
                "isEnabled": True,
                "order": ts,
                "createdAt": firestore.SERVER_TIMESTAMP,
                "modifiedAt": firestore.SERVER_TIMESTAMP,
            }
        )
        pack = pack_ref.get()

    level_nr = (pack.to_dict().get("totalLevels") or 0) + 1
    level_id = str(level_nr)
    base = f"packs/{PACK_ID}/levels/{level_id}"

    puzzle_path = f"{base}/puzzle_{ts}.webp"
    _upload_webp(bucket, puzzle_path, puzzle_png)
    thumb_path = f"{base}/puzzle_thumb_{ts}.webp"
    _upload_webp(bucket, thumb_path, puzzle_png, size=256)
    svg_path = f"{base}/solution_{ts}.svg"
    bucket.blob(svg_path).upload_from_string(solution_svg, content_type="image/svg+xml")

    level_ref = pack_ref.collection("levels").document(level_id)
    category_ref = gdb.collection("categories").document(pack.to_dict().get("categoryId", "words"))

    transaction = gdb.transaction()

    @firestore.transactional
    def _tx(tx):
        if level_ref.get(transaction=tx).exists:
            raise RuntimeError(f"level {level_id} already exists in {PACK_ID}")
        tx.set(
            level_ref,
            {
                "nr": level_nr,
                "name": word,
                "puzzlePath": puzzle_path,
                "puzzleThumbnailPath": thumb_path,
                "solutionPath": svg_path,
                "hintType": "highlight",
                "hints": {},
                "isEnabled": True,
                "notifyOnPublish": meta.get("notify", True),
                "createdBy": "stagenator",
                "theme": meta.get("theme", ""),
                "createdAt": firestore.SERVER_TIMESTAMP,
                "modifiedAt": firestore.SERVER_TIMESTAMP,
            },
        )
        tx.update(pack_ref, {"totalLevels": firestore.Increment(1)})
        if category_ref.get(transaction=tx).exists:
            tx.update(category_ref, {"totalLevels": firestore.Increment(1)})

    _tx(transaction)
    return {"pack": PACK_ID, "level": level_id, "word": word, "puzzle": puzzle_path}


def _upload_webp(bucket, path: str, png_bytes: bytes, size: int | None = None) -> None:
    from PIL import Image

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if size:
        img.thumbnail((size, size))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=82)
    bucket.blob(path).upload_from_string(buf.getvalue(), content_type="image/webp")


# ------------------------------------------------------------------- runner ----

def run(task: dict) -> dict:
    payload = task.get("payload", {})
    used = existing_words()

    design = None
    for _ in range(3):
        design = design_level(used)
        if design:
            break
    if not design:
        raise RuntimeError("could not design a novel level (word dedup exhausted)")

    layout = build_layout(design["word"])
    mask_png = render_mask(layout)
    difficulty = float(payload.get("difficulty") or 1.0)

    if config.DRY_RUN:
        return {"dry_run": True, "word": design["word"], "prompt": design["prompt"],
                "difficulty": difficulty, "mask_bytes": len(mask_png)}

    puzzle_png = runpod.generate_puzzle(
        design["prompt"], difficulty, base64.b64encode(mask_png).decode()
    )
    qa = qa_puzzle(puzzle_png, design["word"])
    if not qa.get("pass"):
        raise RuntimeError(f"QA rejected puzzle for {design['word']}: {json.dumps(qa)[:200]}")

    svg = build_solution_svg(layout, design["word"])
    result = submit_level(design["word"], puzzle_png, svg, meta=design | {"qa": qa})

    from agent.tools import preview

    media = {
        "puzzle": preview.upload(puzzle_png, f"{design['word']}_puzzle.png", "image/png"),
        "mask": preview.upload(mask_png, f"{design['word']}_mask.png", "image/png"),
        "solution_svg": preview.upload(svg.encode(), f"{design['word']}_solution.svg", "image/svg+xml"),
    }
    return {**result, "qa": qa.get("visibility"), "media": media,
            "design": {"prompt": design["prompt"], "theme": design.get("theme"), "difficulty": difficulty}}
