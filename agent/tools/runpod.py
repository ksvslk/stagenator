"""Runpod ComfyUI worker client for Subliminal Words puzzle generation.

Contract ported verbatim from operation_hermes/functions/index.js:
- payload: {input: {workflow: build_workflow(...), images: [{name, image: b64}]}}
- POST /run -> {id}; poll GET /status/{id} -> {status, output.images[0].data|image}
- difficulty == ControlNet strength (higher = word more visible = easier)
Plus the prepaid-credit balance check (GraphQL).
"""

import logging
import os
import random
import time

import requests

log = logging.getLogger("stagenator.runpod")

GRAPHQL_API = "https://api.runpod.io/graphql"


def _key() -> str:
    key = os.getenv("RUNPOD_API_KEY")
    if not key:
        raise RuntimeError("RUNPOD_API_KEY not set")
    return key


def _endpoint() -> str:
    ep = os.getenv("RUNPOD_ENDPOINT_ID")
    if not ep:
        raise RuntimeError("RUNPOD_ENDPOINT_ID not set")
    return ep


def build_workflow(prompt: str, difficulty: float, solution_file_name: str = "1.png") -> dict:
    """Exact port of buildRunpodWorkflow (operation_hermes functions)."""
    return {
        "10": {"class_type": "CheckpointLoaderSimple",
               "inputs": {"ckpt_name": "Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors"}},
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["10", 1]}},
        "14": {"class_type": "ControlNetLoader",
               "inputs": {"control_net_name": "diffusion_pytorch_model.safetensors"}},
        "15": {"class_type": "CLIPTextEncode",
               "inputs": {"text": "worst quality poor details unrealistic", "clip": ["10", 1]}},
        "16": {"class_type": "ControlNetApplyAdvanced",
               "inputs": {"strength": difficulty, "start_percent": 0.0, "end_percent": 1.0,
                          "positive": ["11", 0], "negative": ["15", 0],
                          "control_net": ["14", 0], "image": ["17", 0]}},
        "17": {"class_type": "LoadImage", "inputs": {"image": solution_file_name, "upload": "image"}},
        "18": {"class_type": "KSampler",
               "inputs": {"seed": random.randint(0, 10**12), "steps": 20, "cfg": 8,
                          "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                          "model": ["10", 0], "positive": ["16", 0], "negative": ["16", 1],
                          "latent_image": ["19", 0]}},
        "19": {"class_type": "EmptyLatentImage",
               "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "20": {"class_type": "VAEDecode", "inputs": {"samples": ["18", 0], "vae": ["10", 2]}},
        "21": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["20", 0]}},
    }


def _extract_image(status_data: dict) -> str:
    """Port of extractGeneratedImage — returns base64 or ''."""
    output = status_data.get("output")
    if isinstance(output, dict):
        images = output.get("images")
        if isinstance(images, list) and images:
            first = images[0]
            if isinstance(first, str):
                return first
            return first.get("data") or first.get("image") or ""
        if isinstance(output.get("message"), str):
            return output["message"]
    if isinstance(output, list) and output:
        return output[0]
    return ""


def generate_puzzle(prompt: str, difficulty: float, mask_png_b64: str,
                    timeout_s: int = 300, poll_s: int = 10) -> bytes:
    """Submit a generation, poll to completion, return the puzzle image bytes."""
    import base64

    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    base = f"https://api.runpod.ai/v2/{_endpoint()}"
    payload = {
        "input": {
            "workflow": build_workflow(prompt, difficulty, "1.png"),
            "images": [{"name": "1.png", "image": mask_png_b64}],
        }
    }
    r = requests.post(f"{base}/run", json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    job_id = str(r.json().get("id") or "").strip()
    if not job_id:
        raise RuntimeError(f"Runpod returned no job id: {r.json()}")
    log.info("runpod job started: %s", job_id)

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        s = requests.get(f"{base}/status/{job_id}", headers=headers, timeout=60)
        s.raise_for_status()
        data = s.json()
        status = str(data.get("status") or "UNKNOWN").upper()
        if status == "COMPLETED":
            b64 = _extract_image(data)
            if not b64:
                raise RuntimeError(f"job {job_id} completed without an image")
            return base64.b64decode(b64)
        if status in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"runpod job {job_id} {status}: {data.get('error') or data}")
    raise RuntimeError(f"runpod job {job_id} timed out after {timeout_s}s")


def account_balance() -> float | None:
    key = os.getenv("RUNPOD_API_KEY")
    if not key:
        log.warning("RUNPOD_API_KEY not set — balance unknown")
        return None
    q = {"query": "query { myself { clientBalance } }"}
    r = requests.post(GRAPHQL_API, json=q, headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code == 401:
        # endpoint-scoped key: can generate but not read account — balance unknowable
        log.warning("Runpod key lacks account scope; balance check skipped")
        return None
    r.raise_for_status()
    return r.json()["data"]["myself"]["clientBalance"]
