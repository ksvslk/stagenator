"""Preview media for Mission Control.

Every level's media (puzzle, solution, clip) gets a copy in the home project's
bucket under stagenator_previews/, with a Firebase download token so the
dashboard can render it directly — no cross-project auth needed."""

import logging
import uuid

from google.cloud import storage

from agent import config

log = logging.getLogger("stagenator.preview")

BUCKET = f"{config.HOME_PROJECT}.firebasestorage.app"


def upload(data: bytes, name: str, content_type: str) -> str:
    """Upload preview bytes; returns a token-authenticated download URL."""
    token = uuid.uuid4().hex
    path = f"stagenator_previews/{uuid.uuid4().hex[:8]}_{name}"
    blob = storage.Client(project=config.HOME_PROJECT).bucket(BUCKET).blob(path)
    blob.metadata = {"firebaseStorageDownloadTokens": token}
    blob.upload_from_string(data, content_type=content_type)
    return (
        f"https://firebasestorage.googleapis.com/v0/b/{BUCKET}/o/"
        f"{path.replace('/', '%2F')}?alt=media&token={token}"
    )
