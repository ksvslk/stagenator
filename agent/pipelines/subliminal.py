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

CANVAS = 1024  # SVG_SIZE in word_level_svg.js
# Roboto to match the game's render font (font-family: Roboto, Arial, sans-serif)
ROBOTO = {
    200: Path(__file__).parent.parent / "assets/fonts/Roboto-Thin.ttf",
    400: Path(__file__).parent.parent / "assets/fonts/Roboto-Regular.ttf",
    700: Path(__file__).parent.parent / "assets/fonts/Roboto-Bold.ttf",
}
PACK_ID = "fresh-drops"  # the agent's own pack (auto-created on first level)


# ------------------------------------------------------------------ design ----


def design_level(existing_words: set[str], culture: str | None = None) -> dict | None:
    """Gemini proposes word + scene prompt; code builds the letter layout.
    `culture` is an OPTIONAL soft nod (e.g. where active players are) — never a mandate."""
    culture_line = (
        f"OPTIONAL: many active players are in {culture} right now — you MAY choose a word/"
        f"scene that quietly resonates there, but ONLY if it stays a strong universal level; "
        f"never force it.\n"
        if culture
        else ""
    )
    reply = genai_client.generate_json(
        "You design levels for 'Subliminal Words' — a puzzle game where a word is hidden "
        "inside a photorealistic image; players stare until the word pops out.\n"
        f"Words already used (do NOT repeat): {random.sample(sorted(existing_words), min(200, len(existing_words)))}\n"
        + culture_line
        + "Propose ONE new level: a short, punchy English word (3-8 letters, uppercase, "
        "concrete noun or vivid concept) and a photorealistic scene prompt that thematically "
        "hints at the word without depicting it literally as text.\n"
        "The scene must contain NO people, faces, or human figures — use landscapes, objects, "
        "textures, animals, machines, or abstract environments. (People and faces both raise "
        "likeness concerns and disrupt the hidden-word illusion.)\n"
        'Reply JSON: {"word": "...", "prompt": "...", "theme": "..."}'
    )
    if not reply or not reply.get("word") or not reply.get("prompt"):
        return None
    if not isinstance(reply.get("word"), str):
        return None
    word = reply["word"].strip().upper()
    # letters-only (A-Z): the solution SVG renders one Roboto glyph per letter, so
    # digits/accents/punctuation/spaces would break rendering and the letter tiles.
    if not (word.isascii() and word.isalpha() and 3 <= len(word) <= 8):
        return None
    if word.lower() in existing_words:
        return None
    return {"word": word, "prompt": reply["prompt"], "theme": reply.get("theme", "")}


def build_layout(word: str) -> list[dict]:
    """Letter model matching word_level_svg.js sanitizeWordLettersForSolution:
    x,y normalized (0-1, center), fontSize (24-512), rotationDegrees, scaleX/scaleY
    (0.2-12), skewX/YDegrees (<=80), opacity, fontWeightValue in {200,400,700}.

    Letters spread across the canvas center so the word reads left-to-right when
    highlighted (mild variation keeps it 'hidden', not printed)."""
    n = len(word)
    margin = 0.13
    font_size = 150.0
    letters = []
    for i, ch in enumerate(word):
        x = margin + (i + 0.5) * (1 - 2 * margin) / n
        y = 0.5 + random.uniform(-0.14, 0.14)
        letters.append(
            {
                "letter": ch,
                "x": round(min(max(x, 0.02), 0.98), 4),
                "y": round(min(max(y, 0.02), 0.98), 4),
                "fontSize": font_size,
                "rotationDegrees": round(random.uniform(-18, 18), 2),
                "scaleX": round(random.uniform(0.85, 1.25), 3),
                "scaleY": round(random.uniform(1.05, 1.5), 3),
                "skewXDegrees": round(random.uniform(-10, 10), 2),
                "skewYDegrees": 0.0,
                "opacity": 1.0,
                "fontWeightValue": random.choice([400, 700, 700]),
            }
        )
    return letters


# --------------------------------------------------------------- rendering ----


def _glyph_table() -> dict:
    """Roboto-Black glyph outlines captured from the SAME opentype.js the admin
    dashboard uses (agent/assets/roboto_black_glyphs.json). Commands are stored at
    a reference size and scaled linearly by fontSize, so the emitted <path> matches
    word_level_svg.js buildCanonicalWordSolutionSvg without needing opentype/Node
    or a font renderer at runtime."""
    global _GLYPHS
    try:
        return _GLYPHS
    except NameError:
        _GLYPHS = json.loads(
            (
                Path(__file__).resolve().parent.parent
                / "assets"
                / "roboto_black_glyphs.json"
            ).read_text()
        )
        return _GLYPHS


def _mat_mul(m1, m2):
    return [
        m1[0] * m2[0] + m1[2] * m2[1],
        m1[1] * m2[0] + m1[3] * m2[1],
        m1[0] * m2[2] + m1[2] * m2[3],
        m1[1] * m2[2] + m1[3] * m2[3],
        m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
        m1[1] * m2[4] + m1[3] * m2[5] + m1[5],
    ]


def _letter_path_d(char: str, L: dict, actual_x: float, actual_y: float) -> str:
    """Baked outline path data for one letter — EXACT port of word_level_svg.js
    textToPath + applyTransformToPathData (matrix order: center -> skew -> scale
    -> rotate -> translate; commands serialized at 3 decimals)."""
    tbl = _glyph_table()
    g = tbl["glyphs"].get(char)
    if g is None:
        raise ValueError(f"no Roboto glyph for {char!r}")
    k = float(L["fontSize"]) / tbl["ref"]
    bb = g["bbox"]
    off_x = (bb["x1"] + bb["x2"]) / 2 * k
    off_y = (bb["y1"] + bb["y2"]) / 2 * k
    m = [1, 0, 0, 1, -off_x, -off_y]
    m = _mat_mul(
        [
            1,
            math.tan(math.radians(L["skewYDegrees"])),
            math.tan(math.radians(L["skewXDegrees"])),
            1,
            0,
            0,
        ],
        m,
    )
    m = _mat_mul([L["scaleX"], 0, 0, L["scaleY"], 0, 0], m)
    rad = math.radians(L["rotationDegrees"])
    cos, sin = math.cos(rad), math.sin(rad)
    m = _mat_mul([cos, sin, -sin, cos, 0, 0], m)
    m = _mat_mul([1, 0, 0, 1, actual_x, actual_y], m)

    def ap(px, py):
        return (m[0] * px + m[2] * py + m[4], m[1] * px + m[3] * py + m[5])

    out = []
    for c in g["commands"]:
        t = c["t"]
        if t == "Z":
            out.append("Z")
        elif t == "M":
            x, y = ap(c["x"] * k, c["y"] * k)
            out.append(f"M{x:.3f} {y:.3f}")
        elif t == "L":
            x, y = ap(c["x"] * k, c["y"] * k)
            out.append(f"L{x:.3f} {y:.3f}")
        elif t == "Q":
            x1, y1 = ap(c["x1"] * k, c["y1"] * k)
            x, y = ap(c["x"] * k, c["y"] * k)
            out.append(f"Q{x1:.3f} {y1:.3f} {x:.3f} {y:.3f}")
        elif t == "C":
            x1, y1 = ap(c["x1"] * k, c["y1"] * k)
            x2, y2 = ap(c["x2"] * k, c["y2"] * k)
            x, y = ap(c["x"] * k, c["y"] * k)
            out.append(f"C{x1:.3f} {y1:.3f} {x2:.3f} {y2:.3f} {x:.3f} {y:.3f}")
    return "".join(out)


def build_solution_svg(layout: list[dict], word: str) -> str:
    """Canonical port of buildCanonicalWordSolutionSvg (word_level_svg.js): one
    Roboto-Black outline <path> per letter with the transform baked into the
    coordinates (no live transform attr), id = uppercase letter with a duplicate
    count suffix (A, A2, ...). Byte-for-byte aligned with the admin dashboard."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        f'<svg width="{CANVAS}" height="{CANVAS}" viewBox="0 0 {CANVAS} {CANVAS}" '
        f'xmlns="http://www.w3.org/2000/svg">',
    ]
    counts: dict[str, int] = {}
    for L in layout:
        base = str(L["letter"]).strip().upper()
        counts[base] = counts.get(base, 0) + 1
        lid = base if counts[base] == 1 else f"{base}{counts[base]}"
        ax = L["x"] * CANVAS
        ay = L["y"] * CANVAS
        d = _letter_path_d(base, L, ax, ay)
        esc = (
            base.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )
        lines.append(
            f'<path d="{d}" id="{lid}" style="fill:#000000" aria-label="{esc}" '
            f'data-x="{ax:.2f}" data-y="{ay:.2f}" '
            f'data-font-size="{L["fontSize"]:.2f}" '
            f'data-font-weight="{L["fontWeightValue"]}" '
            f'data-rotation="{L["rotationDegrees"]:.2f}" '
            f'data-scale-x="{L["scaleX"]:.3f}" '
            f'data-scale-y="{L["scaleY"]:.3f}" '
            f'data-skew-x="{L["skewXDegrees"]:.2f}" '
            f'data-skew-y="{L["skewYDegrees"]:.2f}" '
            f'data-opacity="{L["opacity"]:.3f}" '
            f'data-font="Roboto" />'
        )
    lines.append("</svg>")
    return "\n".join(lines)


BASE_FONT_SIZE = 150  # kept for eval import compatibility


def render_mask(
    layout: list[dict], solution_svg: str | None = None, paint: bool = False
) -> bytes:
    """ControlNet mask: the letter layout rasterized to 1024x1024 (black letters on
    white) with the same transform math as the solution SVG (translate->rotate->
    skew->scale, centered), so the hidden word lands where the solution highlights.
    NOTE: rendered from `layout` with PIL fonts, not by rasterizing the SVG — small
    weight differences vs the Roboto-Black solution glyphs are acceptable for a mask."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (CANVAS, CANVAS), 255)
    for L in layout:
        size = int(L["fontSize"])
        font = ImageFont.truetype(
            str(ROBOTO.get(L["fontWeightValue"], ROBOTO[400])), size
        )
        # render glyph centered on its own tile
        pad = size * 3
        tile = Image.new("L", (pad, pad), 0)
        d = ImageDraw.Draw(tile)
        bbox = d.textbbox((0, 0), L["letter"], font=font)
        gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        d.text(
            ((pad - gw) / 2 - bbox[0], (pad - gh) / 2 - bbox[1]),
            L["letter"],
            font=font,
            fill=255,
        )
        # apply skewX + non-uniform scale via affine, then rotate
        sx, sy = L["scaleX"], L["scaleY"]
        skew = math.tan(math.radians(L["skewXDegrees"]))
        cx = cy = pad / 2
        # affine around center: [scaleX, skew*scaleY ; 0, scaleY]
        a, b = sx, skew * sy
        c, e = 0.0, sy
        inv_det = 1.0 / (a * e - b * c)
        ia, ib = e * inv_det, -b * inv_det
        ic, ie = -c * inv_det, a * inv_det
        tx = cx - (ia * cx + ib * cy)
        ty = cy - (ic * cx + ie * cy)
        tile = tile.transform(
            (pad, pad), Image.AFFINE, (ia, ib, tx, ic, ie, ty), resample=Image.BICUBIC
        )
        tile = tile.rotate(
            -L["rotationDegrees"], resample=Image.BICUBIC, center=(cx, cy)
        )
        px, py = int(L["x"] * CANVAS), int(L["y"] * CANVAS)
        img.paste(0, (px - pad // 2, py - pad // 2), mask=tile)
    if paint:
        _add_paint_strokes(img, layout)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def _add_paint_strokes(img, layout: list[dict], n: int | None = None) -> None:
    """Agent equivalent of the admin dashboard's paint mode: a few freehand brush
    strokes swept across the word band and baked into the ControlNet mask, so the
    generated puzzle carries extra obscuring marks and the hidden word is harder to
    pick out. The reveal SVG stays clean letters — strokes live only in the mask."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    xs = [L["x"] * CANVAS for L in layout]
    x0, x1 = min(xs), max(xs)
    ymid = (sum(L["y"] for L in layout) / len(layout)) * CANVAS
    for _ in range(n if n is not None else random.randint(2, 4)):
        width = random.randint(8, 34)  # brush size (admin range 1-50)
        sx = random.uniform(x0 - 80, x0 + 120)
        ex = random.uniform(x1 - 120, x1 + 80)
        steps = random.randint(4, 7)
        pts = [
            (sx + (ex - sx) * (i / (steps - 1)), ymid + random.uniform(-140, 140))
            for i in range(steps)
        ]
        d.line(pts, fill=0, width=width, joint="curve")


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
    bucket = storage.Client(project="subliminal-words").bucket(
        "subliminal-words.firebasestorage.app"
    )
    ts = int(time.time() * 1000)

    pack_ref = gdb.collection("packs").document(PACK_ID)
    pack = pack_ref.get()
    first_level = not pack.exists
    if first_level:
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
    category_ref = gdb.collection("categories").document(
        pack.to_dict().get("categoryId", "words")
    )

    transaction = gdb.transaction()

    @firestore.transactional
    def _tx(tx):
        # Firestore requires ALL reads before ANY write.
        if level_ref.get(transaction=tx).exists:
            raise RuntimeError(f"level {level_id} already exists in {PACK_ID}")
        category_exists = category_ref.get(transaction=tx).exists
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
                # first level of a brand-new pack ships silent (owner eyeballs it in-game)
                "notifyOnPublish": meta.get("notify", not first_level),
                "createdBy": "stagenator",
                "theme": meta.get("theme", ""),
                "createdAt": firestore.SERVER_TIMESTAMP,
                "modifiedAt": firestore.SERVER_TIMESTAMP,
            },
        )
        tx.update(pack_ref, {"totalLevels": firestore.Increment(1)})
        if category_exists:
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


def _self_validate(level_id: str, word: str, svg: str) -> None:
    """Post-publish check: the delivered solution SVG must be canonical and have
    exactly one positioned <path> per letter. On failure, disable the level and
    raise (so the task retries -> re-ships a good one)."""
    problems = []
    if 'data-font="Roboto"' not in svg or "data-x=" not in svg:
        problems.append("solution SVG not in canonical format")
    n_path = svg.count("<path ")
    if n_path != len(word):
        problems.append(f"solution has {n_path} letters, word has {len(word)}")
    # id of the first letter is its uppercase character (dup letters get A2, A3 ...)
    if word and f'id="{word[0].upper()}"' not in svg:
        problems.append(f"missing id {word[0].upper()}")
    if problems:
        from google.cloud import firestore as _fs

        gdb = state.game_db("subliminal-words")
        pack_ref = gdb.collection("packs").document(PACK_ID)
        pack_ref.collection("levels").document(str(level_id)).update(
            {"isEnabled": False, "selfHealReason": "; ".join(problems)}
        )
        # keep the counter consistent with live levels (B4): the disabled level no longer counts
        pack_ref.update({"totalLevels": _fs.Increment(-1)})
        state.critical(
            f"Subliminal level {level_id} ({word}) failed self-validation, "
            f"disabled: {problems}",
            game="subliminal-words",
        )
        raise RuntimeError(f"self-validation failed: {problems}")


def run(task: dict) -> dict:
    payload = task.get("payload", {})
    used = existing_words()
    culture = payload.get("culture")

    design = None
    for _ in range(3):
        design = design_level(used, culture)
        if design:
            break
    if not design:
        raise RuntimeError("could not design a novel level (word dedup exhausted)")

    layout = build_layout(design["word"])
    svg = build_solution_svg(layout, design["word"])
    # "Paint mode" on some levels (agent-equivalent of the admin paint tool): brush
    # strokes baked into the mask to make the hidden word harder to guess.
    paint = payload.get("paint")
    paint = (random.random() < 0.35) if paint is None else bool(paint)
    mask_png = render_mask(layout, svg, paint=paint)
    # Difficulty = ControlNet strength (higher = word more visible = easier). Keep it
    # STABLE: only a small ±0.1 variance around the 1.0 baseline for variety — no big swings.
    difficulty = round(1.0 + random.uniform(-0.10, 0.10), 3)

    if config.DRY_RUN:
        return {
            "dry_run": True,
            "word": design["word"],
            "prompt": design["prompt"],
            "difficulty": difficulty,
            "mode": "paint" if paint else "standard",
            "mask_bytes": len(mask_png),
        }

    def _ship():
        puzzle_png = runpod.generate_puzzle(
            design["prompt"], difficulty, base64.b64encode(mask_png).decode()
        )
        qa = qa_puzzle(puzzle_png, design["word"])
        if not qa.get("pass"):
            raise RuntimeError(
                f"QA rejected puzzle for {design['word']}: {json.dumps(qa)[:200]}"
            )

        result = submit_level(design["word"], puzzle_png, svg, meta=design | {"qa": qa})
        _self_validate(result["level"], design["word"], svg)

        from agent.tools import preview

        media = {
            "puzzle": preview.upload(
                puzzle_png, f"{design['word']}_puzzle.png", "image/png"
            ),
            "mask": preview.upload(mask_png, f"{design['word']}_mask.png", "image/png"),
            "solution_svg": preview.upload(
                svg.encode(), f"{design['word']}_solution.svg", "image/svg+xml"
            ),
        }
        return {
            **result,
            "qa": qa.get("visibility"),
            "media": media,
            "design": {
                "prompt": design["prompt"],
                "theme": design.get("theme"),
                "difficulty": difficulty,
            },
        }

    # once(): a crash after publish must not re-generate (Runpod cost) and re-ship a 2nd level.
    return state.once(task.get("id"), "ship", _ship)
