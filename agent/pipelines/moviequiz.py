"""AI Movie Quiz level pipeline.

Flow: Gemini picks a movie (deduped against all existing level names) and
writes a Veo prompt evoking an iconic scene -> Veo 3.1 Lite generates the
8s clip -> uploaded to temp_uploads/ -> the game's EXISTING
processUploadedVideo callable does ffmpeg/watermark/thumbnail (reused, not
reimplemented) -> level doc + counter transaction exactly per the
dashboard's write path and the firestore.rules schema.
"""

import logging
import time
import uuid

import requests
from google.cloud import firestore, storage

from agent import config, state
from agent.tools import genai_client

log = logging.getLogger("stagenator.moviequiz")

BUCKET = f"{config.GAMES['ai-movie-quiz']['project']}.firebasestorage.app"
PROCESS_FN_URL = (f"https://{config.REGION}-{config.GAMES['ai-movie-quiz']['project']}"
                 ".cloudfunctions.net/processUploadedVideo")
VEO_MODEL = "veo-3.1-lite-generate-001"

# distinct values observed in existing levels; Gemini picks the closest
SOUND_TAGS = ["action", "comedy", "drama", "fantasy", "horror", "romance", "sci-fi", "thriller"]


def existing_movies() -> set[str]:
    gdb = state.game_db("ai-movie-quiz")
    return {
        (s.to_dict() or {}).get("name", "").strip().lower()
        for s in gdb.collection("levels").stream()
    }


def design_level(used: set[str], culture: str | None = None) -> dict | None:
    culture_line = (
        f"OPTIONAL: many active players are in {culture} right now — you MAY pick a film "
        f"especially beloved there, but ONLY if it is still globally recognizable; never "
        f"force it.\n" if culture else ""
    )
    reply = genai_client.generate_json(
        "You design levels for 'AI Movie Quiz' — players watch an 8-second AI-generated "
        "clip that *evokes* a famous movie (mood, setting, iconic imagery) and guess the film.\n"
        f"Movies already used (do NOT repeat): {sorted(used)[:300]}\n"
        + culture_line +
        "Pick ONE widely-known movie (any era, internationally recognizable) — give its title "
        "in proper official capitalization — and write a "
        "Veo video prompt for an 8-second cinematic clip that clearly evokes it WITHOUT "
        "using the title, character names, actor likenesses, or any on-screen text/logos. "
        "Be creative and cinematic — characters, scenery, mood and iconic imagery are all "
        "fair game (only real actor LIKENESSES are off-limits). "
        "Think: the iconic scenario, reimagined.\n"
        "Choose a prompt STRATEGY (controls difficulty — vary across levels):\n"
        "- 'visual': pure imagery, no sound direction (hardest)\n"
        "- 'scene+ambience': add atmosphere/SFX/score direction (medium)\n"
        "- 'scene+dialogue': a character SPEAKS one VERY SHORT line — a handful of words that "
        "comfortably fit in 8 seconds — write it into the prompt in quotes; NOT the movie's "
        "famous verbatim quote (that is the in-game hint) and never naming title or "
        "characters (easiest)\n"
        "Also provide level metadata: sound (closest of "
        f"{SOUND_TAGS}), characteristic (ONE evocative adjective), and hints: actor "
        "(lead actor's real name), quote (a famous line from the film), year (release year "
        "as a 4-digit string).\n"
        'Reply JSON: {"movie": "...", "strategy": "visual|scene+ambience|scene+dialogue", '
        '"veo_prompt": "...", "sound": "...", '
        '"characteristic": "...", "actor": "...", "quote": "...", "year": "..."}'
    )
    if not reply or not reply.get("movie") or not reply.get("veo_prompt"):
        return None
    if reply["movie"].strip().lower() in used:
        return None
    return reply


def generate_clip(prompt: str) -> bytes:
    """Veo 3.1 Lite, 8 seconds. Long-running operation, polled to completion."""
    from google.genai import types

    client = genai_client.client_for_location("us-central1")
    operation = client.models.generate_videos(
        model=VEO_MODEL,
        prompt=prompt,
        config=types.GenerateVideosConfig(
            duration_seconds=8,
            aspect_ratio="16:9",
            number_of_videos=1,
        ),
    )
    deadline = time.monotonic() + 600
    while not operation.done:
        if time.monotonic() > deadline:
            raise RuntimeError("Veo generation timed out after 600s")
        time.sleep(15)
        operation = client.operations.get(operation)
    videos = operation.response.generated_videos if operation.response else None
    if not videos:
        raise RuntimeError(f"Veo returned no video: {operation.error or 'unknown'}")
    video = videos[0].video
    if video.video_bytes:
        return video.video_bytes
    # served as a URI (file service) — download it
    resp = requests.get(video.uri, timeout=120)
    resp.raise_for_status()
    return resp.content


def process_video(clip: bytes, level_number: int) -> str:
    """Reuse the game's processUploadedVideo (ffmpeg+watermark+thumbnail)."""
    temp_path = f"temp_uploads/{uuid.uuid4()}/stagenator.mp4"
    bucket = storage.Client(project=config.GAMES["ai-movie-quiz"]["project"]).bucket(BUCKET)
    bucket.blob(temp_path).upload_from_string(clip, content_type="video/mp4")

    r = requests.post(
        PROCESS_FN_URL,
        json={"data": {"tempFilePath": temp_path, "levelNumber": level_number}},
        headers={"Content-Type": "application/json"},
        timeout=300,
    )
    r.raise_for_status()
    final_path = ((r.json() or {}).get("result") or {}).get("finalPath")
    if not final_path:
        raise RuntimeError(f"processUploadedVideo returned no finalPath: {r.text[:200]}")
    return final_path


def qa_clip(clip: bytes, movie: str) -> dict:
    reply = genai_client.generate_json_with_video(
        f"This 8s AI clip should evoke the movie {movie!r} for a guessing game — "
        "recognizable to fans but with no title text, no real actor likenesses.\n"
        'Judge it. Reply JSON: {"pass": true/false, "recognizable": "no|maybe|yes", "note": "..."}',
        clip,
    )
    return reply or {"pass": False, "note": "QA call failed"}


def submit_level(design: dict, final_path: str, level_number: int) -> dict:
    gdb = state.game_db("ai-movie-quiz")
    counter_ref = gdb.collection("counters").document("levelsCounter")
    level_ref = gdb.collection("levels").document(str(level_number))

    @firestore.transactional
    def _tx(tx: firestore.Transaction):
        count = (counter_ref.get(transaction=tx).to_dict() or {}).get("count", 0)
        if level_number != count + 1:
            raise RuntimeError(f"level number race: expected {count + 1}, had {level_number}")
        if level_ref.get(transaction=tx).exists:
            raise RuntimeError(f"level {level_number} already exists")
        tx.set(
            level_ref,
            {
                # exact schema enforced by firestore.rules isValidLevelData
                "levelNumber": level_number,
                "name": design["movie"].strip(),  # proper title case requested from the model
                "path": final_path,
                "sound": design.get("sound") or "drama",
                "characteristic": design.get("characteristic") or "Iconic",
                "hints": {
                    "actor": str(design.get("actor") or ""),
                    "quote": str(design.get("quote") or ""),
                    "year": str(design.get("year") or ""),
                },
            },
        )
        tx.update(counter_ref, {"count": firestore.Increment(1)})

    _tx(gdb.transaction())
    return {"level": level_number, "movie": design["movie"], "path": final_path}


def _self_validate(level_number: int, video_path: str) -> None:
    """Post-publish: level doc matches schema and the processed video blob exists."""
    from google.cloud import storage

    gdb = state.game_db("ai-movie-quiz")
    doc = gdb.collection("levels").document(str(level_number)).get().to_dict() or {}
    problems = []
    for k in ("levelNumber", "name", "path", "sound", "characteristic", "hints"):
        if k not in doc:
            problems.append(f"missing {k}")
    if not all(h in (doc.get("hints") or {}) for h in ("actor", "quote", "year")):
        problems.append("hints incomplete")
    bucket = storage.Client(project=config.GAMES["ai-movie-quiz"]["project"]).bucket(BUCKET)
    if not bucket.blob(video_path).exists():
        problems.append(f"video blob missing: {video_path}")
    if problems:
        # roll back cleanly (delete level + decrement counter) so a retry re-ships
        # at the same number — AMQ shows levels by counter, so a broken one can't
        # just be disabled.
        gdb.collection("levels").document(str(level_number)).delete()
        gdb.collection("counters").document("levelsCounter").update(
            {"count": firestore.Increment(-1)})
        state.critical(f"AMQ level {level_number} failed self-validation, rolled back: {problems}")
        raise RuntimeError(f"self-validation failed: {problems}")


def run(task: dict) -> dict:
    used = existing_movies()
    culture = task.get("payload", {}).get("culture")
    design = None
    for _ in range(3):
        design = design_level(used, culture)
        if design:
            break
    if not design:
        raise RuntimeError("could not design a novel movie level")

    if config.DRY_RUN:
        return {"dry_run": True, "movie": design["movie"], "veo_prompt": design["veo_prompt"][:120]}

    clip = generate_clip(design["veo_prompt"])
    qa = qa_clip(clip, design["movie"])
    if not qa.get("pass"):
        raise RuntimeError(f"QA rejected clip for {design['movie']}: {qa.get('note', '')[:200]}")

    gdb = state.game_db("ai-movie-quiz")
    next_n = (gdb.collection("counters").document("levelsCounter").get().to_dict() or {}).get("count", 0) + 1
    final_path = process_video(clip, next_n)
    result = submit_level(design, final_path, next_n)
    _self_validate(next_n, final_path)

    from agent.tools import preview

    media = {"clip": preview.upload(clip, f"{design['movie'].replace(' ', '_')}.mp4", "video/mp4")}
    return {**result, "qa": qa.get("recognizable"), "media": media,
            "design": {k: design.get(k) for k in ("strategy", "veo_prompt", "sound", "characteristic", "actor", "quote", "year")}}
