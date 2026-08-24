"""Small Gemini helper for pipeline-internal JSON calls (hint filling, QA passes).

The Strategist/Reflector are LlmAgents in the graph; this client is for
deterministic pipeline steps that need one model call outside the graph."""

import json
import logging

from google import genai
from google.genai import types

from agent import config

log = logging.getLogger("stagenator.genai")
_client: genai.Client | None = None


def client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=config.HOME_PROJECT, location="global")
    return _client


def generate_json_with_image(prompt: str, image_bytes: bytes) -> dict | None:
    try:
        resp = client().models.generate_content(
            model=config.MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
                prompt,
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001
        log.warning("generate_json_with_image failed: %s", e)
        return None


def generate_json(prompt: str) -> dict | None:
    try:
        resp = client().models.generate_content(
            model=config.MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001
        log.warning("generate_json failed: %s", e)
        return None
