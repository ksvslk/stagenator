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
    # The scene need not relate to the word: often it hints thematically, but sometimes
    # it is DELIBERATELY unrelated and the word hides purely through placement — a fun,
    # surprising, usually-harder level. Decide here so the mix is even across levels.
    scene_rule = (
        "a photorealistic scene prompt that thematically hints at the word without "
        "depicting it literally as text"
        if random.random() < 0.6
        else "a photorealistic scene prompt DELIBERATELY UNRELATED to the word — any "
        "vivid, concrete real-world scene of your choosing; the word will hide inside it "
        "purely through placement, which makes a surprising level"
    )
    reply = genai_client.generate_json(
        "You design levels for 'Subliminal Words' — a puzzle game where a word is hidden "
        "inside a photorealistic image; players stare until the word pops out.\n"
        f"Words already used (do NOT repeat): {random.sample(sorted(existing_words), min(200, len(existing_words)))}\n"
        + culture_line
        + "Propose ONE new level: a short, punchy English word (3-8 letters, uppercase, "
        "concrete noun or vivid concept) and " + scene_rule + ".\n"
        "STRONGLY prefer SHORT words (3-5 letters) so the hidden letters render large and bold.\n"
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

    Letters are placed IN ORDER along a PATH (a curved/diagonal quadratic Bézier) that
    spans a large part of the 1024 canvas, and they are SPREAD along its whole length —
    so different levels arrange the word in very different ways, using the entire canvas
    rather than a compact block. Each letter follows the local path direction (kept
    upright) with per-letter rotation, size, scale and skew jitter. Spacing along the
    path keeps the letters' bounding circles apart, so they never overlap."""
    n = len(word)
    # ControlNet floor, raised — but capped per word length so a long word still fits
    # the canvas (an 8-letter word can't have every glyph as big as a 3-letter one)
    MIN_FONT = min(215.0, 820.0 / n)
    MAX_FONT = 470.0  # near the 512 cap — a letter this size dominates the canvas
    base = random.uniform(175, 255)  # bigger default letter size
    if random.random() < 0.22:  # some puzzles are GIANT: letters dominate the canvas
        base *= random.uniform(1.3, 1.6)

    def _roll_fs() -> float:
        # big by default; ~2 in 5 letters go even bigger for strong contrast. Floored at
        # MIN_FONT here (so packing respects it) and clamped to MAX_FONT.
        r = random.uniform(0.95, 1.3)
        if random.random() < 0.35:
            r *= random.uniform(1.8, 3.2)  # SOME letters tower over the rest
        return min(MAX_FONT, max(MIN_FONT, base * r))

    props = [
        {
            "letter": ch,
            "fs": _roll_fs(),  # clamped to [MIN_FONT, MAX_FONT] below
            "sx": random.uniform(1.0, 1.45),  # never narrower than normal -> not thin
            "sy": random.uniform(1.0, 1.55),
            "rotj": random.uniform(-22, 22),
            "skx": random.uniform(-12, 12),
            "sky": random.uniform(-5, 5),
            "fw": 700,  # always bold -> thick strokes for ControlNet
        }
        for ch in word
    ]

    def _radius(p: dict) -> float:
        return 0.5 * math.hypot(p["fs"] * 0.72 * p["sx"], p["fs"] * 0.82 * p["sy"])

    radii = [_radius(p) for p in props]

    # lay the letters IN ORDER along a curved baseline in nominal coords (centred on 0);
    # gaps scale with letter size for consistent, generous spacing
    gaps = [random.uniform(0.12, 0.5) * (radii[i] + radii[i + 1]) / 2 for i in range(n - 1)]
    pos1d, cur = [radii[0]], radii[0]
    for i in range(1, n):
        cur += radii[i - 1] + gaps[i - 1] + radii[i]
        pos1d.append(cur)
    length = (pos1d[-1] + radii[-1]) or 1.0
    phi = random.uniform(0, 2 * math.pi)
    cphi, sphi = math.cos(phi), math.sin(phi)
    bow = random.uniform(-0.6, 0.6)  # 0 = straight line, ±0.6 = strong arc
    pts, tang = [], []
    for i in range(n):
        o = pos1d[i] - length / 2  # signed offset along the baseline
        t = (o + length / 2) / length
        off = bow * length * 0.5 * math.sin(t * math.pi)  # perpendicular bow, 0 at the ends
        pts.append((o * cphi + off * (-sphi), o * sphi + off * cphi))
        slope = bow * math.pi * 0.5 * math.cos(t * math.pi)  # d(off)/d(o) -> local tangent
        tang.append(math.degrees(phi) + math.degrees(math.atan(slope)))

    # SCALE the whole arrangement to fill the canvas -> short words get big letters,
    # long words stay legible, and the word always uses the space
    minx = min(pts[i][0] - radii[i] for i in range(n))
    maxx = max(pts[i][0] + radii[i] for i in range(n))
    miny = min(pts[i][1] - radii[i] for i in range(n))
    maxy = max(pts[i][1] + radii[i] for i in range(n))
    w, h = max(maxx - minx, 1.0), max(maxy - miny, 1.0)
    margin = 0.03 * CANVAS
    fill = random.uniform(0.85, 1.0)  # fill most/all of the canvas -> big letters
    scale = (CANVAS - 2 * margin) * fill / max(w, h)
    bcx, bcy = (minx + maxx) / 2, (miny + maxy) / 2
    sw, sh = w * scale, h * scale
    tx = (
        random.uniform(margin + sw / 2, CANVAS - margin - sw / 2)
        if sw < CANVAS - 2 * margin
        else CANVAS / 2
    )
    ty = (
        random.uniform(margin + sh / 2, CANVAS - margin - sh / 2)
        if sh < CANVAS - 2 * margin
        else CANVAS / 2
    )

    letters = []
    for i, p in enumerate(props):
        fs = min(MAX_FONT, max(MIN_FONT, p["fs"] * scale))  # guaranteed >= MIN
        x = tx + (pts[i][0] - bcx) * scale
        y = ty + (pts[i][1] - bcy) * scale
        rot = tang[i] + p["rotj"]
        while rot > 90:  # follow the path but keep glyphs upright, not inverted
            rot -= 180
        while rot < -90:
            rot += 180
        letters.append(
            {
                "letter": p["letter"],
                "x": round(min(max(x / CANVAS, 0.02), 0.98), 4),
                "y": round(min(max(y / CANVAS, 0.02), 0.98), 4),
                "fontSize": round(fs, 1),
                "rotationDegrees": round(rot, 2),
                "scaleX": round(p["sx"], 3),
                "scaleY": round(p["sy"], 3),
                "skewXDegrees": round(p["skx"], 2),
                "skewYDegrees": round(p["sky"], 2),
                "opacity": 1.0,
                "fontWeightValue": p["fw"],
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
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    img = Image.new("L", (CANVAS, CANVAS), 255)
    # per-level boldness: often none, sometimes SOME letters go extra chunky (mask only)
    bold_frac = random.choice([0.0, 0.0, 0.0, 0.3, 0.55])
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
        # SOME letters go extra-bold (mask only) for a chunkier ControlNet signal
        if random.random() < bold_frac:
            tile = tile.filter(ImageFilter.MaxFilter(2 * max(1, int(size * 0.03)) + 1))
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
    """Freehand marks — curved sweeps, filled blobs and irregular splotches — scattered
    across the WHOLE canvas (mostly the empty areas, not hugging the word) and baked into
    the ControlNet mask as extra texture. Composited BEHIND the glyphs (erased over each
    letter + a halo), so they never mark a letter and the reveal SVG stays clean."""
    from PIL import Image, ImageChops, ImageDraw, ImageFilter

    stroke = Image.new("L", img.size, 255)  # white = no mark
    d = ImageDraw.Draw(stroke)
    C = CANVAS
    # Precompute each glyph's clearance circle, then sample mark centres in the FREE area
    # only (reject points that fall inside a clearance zone). Placing marks off the letters
    # from the start — instead of scattering everywhere and erasing the ones that overlap —
    # keeps the mark COUNT high (few get wiped) while still never touching a letter.
    zones = []
    for L in layout:
        r = 0.5 * math.hypot(
            L["fontSize"] * 0.72 * L["scaleX"], L["fontSize"] * 0.82 * L["scaleY"]
        )
        zones.append((L["x"] * C, L["y"] * C, r + max(70.0, 0.6 * r)))

    def _free_center(tries: int = 14) -> tuple[float, float]:
        px, py = random.uniform(0, C), random.uniform(0, C)
        for _ in range(tries):
            if all((px - zx) ** 2 + (py - zy) ** 2 > cz * cz for zx, zy, cz in zones):
                return px, py
            px, py = random.uniform(0, C), random.uniform(0, C)
        return px, py  # canvas is crowded — fall back; the exclusion wipe still guards it

    for _ in range(n if n is not None else random.randint(12, 20)):
        tone = random.randint(0, 175)  # opacity: 0 = strong black, ~175 = faint ghost
        kind = random.random()
        if kind < 0.5:
            # curved sweep starting in a free area, short dab to long sweep
            width = random.randint(5, 42)
            ang = random.uniform(0, 2 * math.pi)
            length = random.uniform(90, 940)
            sx, sy = _free_center()
            ex, ey = sx + length * math.cos(ang), sy + length * math.sin(ang)
            steps = random.randint(2, 9)
            bow = random.uniform(0, 1) * random.uniform(30, 170)
            phase = random.uniform(0.5, 2.2)
            nx, ny = -math.sin(ang), math.cos(ang)
            pts = [
                (
                    sx + (ex - sx) * (t := i / (steps - 1) if steps > 1 else 0) + math.sin(t * math.pi * phase) * bow * nx,
                    sy + (ey - sy) * t + math.sin(t * math.pi * phase) * bow * ny,
                )
                for i in range(steps)
            ]
            d.line(pts, fill=tone, width=width, joint="curve")
        elif kind < 0.78:
            # filled blob (rotated-ish ellipse) in a free area
            bx, by = _free_center()
            rw = random.uniform(25, 155)
            rh = rw * random.uniform(0.4, 1.7)
            d.ellipse([bx - rw, by - rh, bx + rw, by + rh], fill=tone)
        else:
            # irregular splotch (random polygon) in a free area
            bx, by = _free_center()
            k = random.randint(5, 9)
            rad = random.uniform(30, 165)
            poly = [
                (
                    bx + rad * random.uniform(0.5, 1.2) * math.cos(2 * math.pi * j / k),
                    by + rad * random.uniform(0.5, 1.2) * math.sin(2 * math.pi * j / k),
                )
                for j in range(k)
            ]
            d.polygon(poly, fill=tone)
    # Keep marks WELL AWAY from every letter — not just off it, but out of a generous
    # clearance circle around each glyph — so nothing ever sits over, under or touching a
    # letter (which would wreck the hidden-word generation). Marks live only in the empty
    # areas. Then composite behind the letters as a final safety.
    excl = Image.new("L", img.size, 0)
    ed = ImageDraw.Draw(excl)
    for L in layout:
        r = 0.5 * math.hypot(
            L["fontSize"] * 0.72 * L["scaleX"], L["fontSize"] * 0.82 * L["scaleY"]
        )
        clear = r + max(70.0, 0.6 * r)  # big clearance around the glyph
        gx, gy = L["x"] * CANVAS, L["y"] * CANVAS
        ed.ellipse([gx - clear, gy - clear, gx + clear, gy + clear], fill=255)
    stroke.paste(255, (0, 0), mask=excl)  # wipe any mark inside a letter's clearance zone
    # belt-and-suspenders: also erase over the actual glyph pixels + a halo
    letter_mask = img.point(lambda v: 255 if v < 200 else 0).filter(ImageFilter.MaxFilter(13))
    stroke.paste(255, (0, 0), mask=letter_mask)
    img.paste(ImageChops.darker(img, stroke))


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
    paint = (random.random() < 0.5) if paint is None else bool(paint)
    mask_png = render_mask(layout, svg, paint=paint)
    # Difficulty = ControlNet strength: HIGHER -> the mask is imposed more strongly, so the
    # word comes out MORE visible -> EASIER to find. We set it INVERSELY to how visible the
    # layout already is: big/bold letters are easy to spot, so we blend them harder (lower
    # strength); small/subtle letters get imposed more (higher strength). That keeps the real
    # find-difficulty roughly even across very different layouts, instead of a blind ±0.1.
    avg_fs = sum(L["fontSize"] for L in layout) / len(layout)
    # avg_fs spans ~135 (letters near the floor) .. ~300+ (big). Normalise to visibility 0..1.
    visibility = min(1.0, max(0.0, (avg_fs - 135.0) / 165.0))
    # visible layout -> low strength (harder); subtle layout -> high strength (easier).
    # range [0.90, 1.00]: subtle layout -> 1.00 (easier), full-visibility -> 0.90 floor.
    difficulty = 1.00 - visibility * 0.10 + random.uniform(-0.02, 0.02)
    if paint:
        difficulty += 0.03  # paint clutter hides the word a bit -> compensate strength up
    difficulty = round(min(1.00, max(0.90, difficulty)), 3)

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
