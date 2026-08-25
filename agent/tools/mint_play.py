"""Play Console promo-code minting — deterministic Playwright automation.

Design choice: this is a FIXED, known click-path (walked and verified by hand),
so it runs as deterministic Playwright — no LLM in the loop to mis-click.
Chrome DevTools MCP remains the tool for exploratory/assisted browser work;
production cron work wants replayability.

Session model: a dedicated agent Google account's Chrome profile lives in a
private GCS bucket (SA-only). Each run: download -> drive -> re-upload (cookies
refresh with use). Any login wall -> SessionExpired -> CRITICAL alert upstream;
re-auth is routine maintenance (~2 min local), not failure.
"""

import datetime as dt
import io
import logging
import shutil
import tarfile
import tempfile
from pathlib import Path

from google.cloud import storage

from agent import config

log = logging.getLogger("stagenator.mint_play")

BUCKET = f"{config.HOME_PROJECT}.firebasestorage.app"
PROFILE_BLOB = "stagenator_browser/chrome-profile.tar.gz"
DEV_ACCOUNT = "7030085917427251773"


class SessionExpired(RuntimeError):
    """The stored Google session no longer works — needs a human re-login."""


def _bucket():
    return storage.Client(project=config.HOME_PROJECT).bucket(BUCKET)


def push_profile(profile_dir: str) -> None:
    """One-time (and post-run) upload of the browser profile to GCS."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(profile_dir, arcname="profile",
                filter=lambda ti: None if "/Cache" in ti.name or "/Code Cache" in ti.name else ti)
    buf.seek(0)
    _bucket().blob(PROFILE_BLOB).upload_from_file(buf, content_type="application/gzip")
    log.info("browser profile pushed (%d KB)", buf.getbuffer().nbytes // 1024)


def pull_profile(dest_dir: str) -> str:
    blob = _bucket().blob(PROFILE_BLOB)
    if not blob.exists():
        raise SessionExpired("no stored browser profile — run the one-time login capture")
    data = blob.download_as_bytes()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest_dir)
    return str(Path(dest_dir) / "profile")


def mint(play_app_id: str, gift_type: str, gift_label: str | None,
         n_codes: int = 50, window_days: int = 364) -> tuple[list[str], str]:
    """Create a promotion and return (codes, end_date). Raises SessionExpired
    when the stored session hits a login wall."""
    from playwright.sync_api import sync_playwright

    end_date = dt.date.today() + dt.timedelta(days=window_days)
    name = f"Stagenator {dt.date.today().isoformat()}"
    workdir = tempfile.mkdtemp(prefix="stagenator-chrome-")
    try:
        profile = pull_profile(workdir)
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                profile, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                accept_downloads=True,
            )
            page = ctx.new_page()
            page.goto(
                f"https://play.google.com/console/u/0/developers/{DEV_ACCOUNT}"
                f"/app/{play_app_id}/promotions/create",
                wait_until="domcontentloaded", timeout=60_000,
            )
            page.wait_for_timeout(6_000)
            if "accounts.google.com" in page.url or "signin" in page.url:
                raise SessionExpired(f"login wall at {page.url[:80]}")

            page.get_by_label("Promotion name").fill(name)

            # dates: fields accept typed input (verified in the manual run)
            page.get_by_text("Select a date").first.click()
            page.get_by_placeholder("Enter date").fill(dt.date.today().strftime("%m/%d/%Y"))
            page.keyboard.press("Enter")
            page.get_by_text("Select a date").first.click()
            page.get_by_placeholder("Enter date").fill(end_date.strftime("%m/%d/%Y"))
            page.keyboard.press("Enter")

            kind = "One-time product" if gift_type == "one_time" else "Subscription"
            page.get_by_text(kind, exact=True).click()
            page.get_by_text("Product", exact=False).first.click()
            page.wait_for_timeout(2_000)
            if gift_label:
                page.get_by_text(gift_label, exact=False).first.click()
            # pick the first radio in the product dialog (single-product apps)
            page.locator("mat-dialog-container input[type=radio], "
                         "[role=dialog] [role=radio]").first.check()
            page.get_by_role("button", name="Apply").click()

            page.get_by_label("Number of codes").fill(str(n_codes))
            page.get_by_role("button", name="Save").click()
            page.get_by_role("button", name="Create").click()
            page.wait_for_url("**/promotions/*", timeout=60_000)

            with page.expect_download(timeout=60_000) as dl:
                page.get_by_text("Download codes").click()
            csv_path = dl.value.path()
            codes = [line.strip() for line in Path(csv_path).read_text().splitlines()[1:]
                     if line.strip()]
            ctx.close()
        push_profile(profile)  # refreshed cookies back to GCS
        return codes, end_date.isoformat()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
