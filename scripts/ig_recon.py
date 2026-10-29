#!/usr/bin/env python3
# RECON ONLY — do not use in production.
# A real adapter must store the session encrypted in the vault,
# the same way VK and Yandex sessions are stored (Fernet via VAULT_KEY).
"""Instagram Reels upload reconnaissance — DO NOT USE FOR PRODUCTION.

Captures DOM selectors, flow steps and timings for the browser-tier adapter design doc.
Stops immediately on any challenge / checkpoint page.

Usage:
    .venv/bin/python scripts/ig_recon.py [--profile <path>] [--video <path>] [--no-login]

  --profile  storageState JSON path  (default: runtime/ig_profile.json)
  --video    path to test mp4        (default: runtime/test_reel_real.mp4)
  --no-login skip the login wait, assume storageState is already valid
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from playwright.async_api import Page, async_playwright


def _save_state(profile_path: Path, state: dict) -> None:
    """Write storageState as owner-only (600) — these are live login cookies."""
    profile_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    os.chmod(profile_path, 0o600)


FINDINGS: list[dict] = []
TIMINGS: dict[str, float] = {}

STOP_PATTERNS = [
    "suspicious login",
    "unusual login",
    "action blocked",
    "we noticed an unusual",
    "confirm your identity",
    "checkpoint",
    "verify",
    "challenge",
    "this account has been disabled",
    "there was a problem",
    "let us know it's you",
    "enter the code",
    "подозрительный",
    "заблокировано",
]


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def record(key: str, value: object) -> None:
    FINDINGS.append({"key": key, "value": value})
    log(f"  → {key}: {value}")


async def check_for_challenge(page: Page, screenshot_dir: Path, step: str) -> bool:
    """Returns True if a challenge/block page is detected → caller must stop."""
    content = (await page.content()).lower()
    url = page.url.lower()
    for pat in STOP_PATTERNS:
        if pat in content or pat in url:
            log(f"  ⚠ CHALLENGE detected at step '{step}' — pattern: '{pat}'")
            path = screenshot_dir / f"challenge_{step.replace(' ', '_')}.png"
            await page.screenshot(path=str(path), full_page=True)
            record(
                "CHALLENGE",
                {"step": step, "pattern": pat, "screenshot": str(path), "url": page.url},
            )
            return True
    return False


async def get_egress_ip(page: Page) -> str:
    try:
        resp = await page.evaluate("""async () => {
            const r = await fetch('https://api.ipify.org?format=json');
            return (await r.json()).ip;
        }""")
        return str(resp)
    except Exception as exc:
        return f"error: {exc}"


async def main(profile_path: Path, video_path: Path, skip_login: bool) -> None:
    screenshot_dir = Path("runtime/ig_recon_screenshots")
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        storage_state = str(profile_path) if profile_path.exists() else None
        ctx_kwargs: dict = {
            "headless": False,
            "slow_mo": 800,
            "args": ["--start-maximized"],
        }
        if storage_state:
            log(f"Loading storageState from {profile_path}")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path.parent / "chromium_data"),
                **ctx_kwargs,
                storage_state=storage_state,
            )
        else:
            log("No storageState found — fresh context (will need manual login)")
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(profile_path.parent / "chromium_data"),
                **ctx_kwargs,
            )

        page = context.pages[0] if context.pages else await context.new_page()

        # ── 0. Egress IP ────────────────────────────────────────────────────
        log("Fetching egress IP...")
        await page.goto("https://api.ipify.org?format=json", wait_until="networkidle")
        ip = await get_egress_ip(page)
        record("egress_ip", ip)

        # ── 1. Navigate to Instagram ────────────────────────────────────────
        log("Navigating to instagram.com...")
        await page.goto("https://www.instagram.com/", wait_until="networkidle")
        await asyncio.sleep(2)

        if await check_for_challenge(page, screenshot_dir, "initial_load"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        # ── 2. Login (manual) ───────────────────────────────────────────────
        if not skip_login:
            # Check if already logged in (no login form visible)
            login_form = page.locator('input[name="username"]')
            if await login_form.count() > 0:
                log("Login form visible. Please log in MANUALLY in the browser window.")
                log("Waiting up to 3 minutes for login to complete...")
                try:
                    # Wait for the nav/home feed to appear — indicates successful login
                    await page.wait_for_selector(
                        '[aria-label="Home"], [aria-label="Главная"], nav[role="navigation"]',
                        timeout=180_000,
                    )
                    log("Login detected — home feed visible")
                    await asyncio.sleep(3)

                    # Save storageState right after login
                    state = await context.storage_state()
                    _save_state(profile_path, state)
                    log(f"storageState saved to {profile_path} (chmod 600)")
                    record("storageState_saved_after_login", str(profile_path))
                except Exception as exc:
                    log(f"Login wait timed out or failed: {exc}")
                    await _dump_and_exit(context, profile_path, screenshot_dir)
                    return
            else:
                log("Already appears logged in (no login form)")
        else:
            log("--no-login: skipping login wait")

        if await check_for_challenge(page, screenshot_dir, "post_login"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        # Screenshot: home feed
        await page.screenshot(path=str(screenshot_dir / "home_feed.png"), full_page=False)

        # ── 3. Probe the Create / "+" button ────────────────────────────────
        log("Probing Create button...")
        create_selectors_to_try = [
            '[aria-label="New post"]',
            '[aria-label="Новая публикация"]',
            '[aria-label="Create"]',
            '[aria-label="Создать"]',
            'svg[aria-label="New post"]',
            'svg[aria-label="Создать"]',
        ]
        create_btn = None
        matched_create_selector = None
        for sel in create_selectors_to_try:
            el = page.locator(sel).first
            if await el.count() > 0:
                create_btn = el
                matched_create_selector = sel
                break

        if create_btn is None:
            # Try finding by role
            log("  Create button not found by aria-label; scanning all links for 'create' text...")
            # Fall back to inner text scan
            for sel in [
                'a[href*="create"]',
                'span:text("Create")',
                'span:text("Создать")',
                'div[role="button"]:has(svg)',
            ]:
                el = page.locator(sel).first
                if await el.count() > 0:
                    create_btn = el
                    matched_create_selector = sel
                    break

        if create_btn is None:
            log("  ✗ Create button not found — capturing page source")
            await page.screenshot(path=str(screenshot_dir / "no_create_btn.png"), full_page=True)
            record("create_btn_selector", "NOT_FOUND")
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        record("create_btn_selector", matched_create_selector)
        await page.screenshot(path=str(screenshot_dir / "before_create_click.png"))

        # ── 4. Click Create ─────────────────────────────────────────────────
        log(f"Clicking Create button ({matched_create_selector})...")
        TIMINGS["create_click"] = time.time()
        await create_btn.click()
        await asyncio.sleep(2)

        if await check_for_challenge(page, screenshot_dir, "after_create_click"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        await page.screenshot(path=str(screenshot_dir / "after_create_click.png"))

        # ── 5. Probe post type dialog / modal ───────────────────────────────
        log("Probing post type options...")
        type_selectors = {
            "post": [
                '[role="menuitem"]:has-text("Post")',
                '[role="menuitem"]:has-text("Публикация")',
                'button:has-text("Post")',
                'button:has-text("Пост")',
            ],
            "reel": [
                '[role="menuitem"]:has-text("Reel")',
                '[role="menuitem"]:has-text("Reels")',
                '[role="menuitem"]:has-text("Рилс")',
                'button:has-text("Reel")',
                'button:has-text("Рилс")',
            ],
            "story": [
                '[role="menuitem"]:has-text("Story")',
                '[role="menuitem"]:has-text("Сторис")',
            ],
        }
        found_types: dict[str, str] = {}
        for type_name, sels in type_selectors.items():
            for sel in sels:
                el = page.locator(sel).first
                if await el.count() > 0:
                    found_types[type_name] = sel
                    break
        record("visible_post_types", found_types)

        # Decide: if Reel option visible, click it; else proceed with current flow
        if "reel" in found_types:
            log(f"Clicking Reel option ({found_types['reel']})...")
            await page.locator(found_types["reel"]).first.click()
            await asyncio.sleep(2)
            record("reel_explicit_selection", True)
        else:
            log("  No explicit Reel type option visible — will try direct 'Post' flow")
            record("reel_explicit_selection", False)
            if "post" in found_types:
                await page.locator(found_types["post"]).first.click()
                await asyncio.sleep(2)

        await page.screenshot(path=str(screenshot_dir / "after_type_select.png"))
        if await check_for_challenge(page, screenshot_dir, "after_type_select"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        # ── 6. File input — select video ────────────────────────────────────
        log("Locating file input for video upload...")
        file_input_sel = 'input[type="file"]'
        file_input = page.locator(file_input_sel).first
        if await file_input.count() == 0:
            log("  ✗ No file input visible — checking if 'Select from computer' button exists")
            select_btn_sels = [
                'button:has-text("Select from computer")',
                'button:has-text("Выбрать с компьютера")',
                'button:has-text("Select from gallery")',
                'button:has-text("Выбрать из галереи")',
                'div[role="button"]:has-text("Select")',
            ]
            for sel in select_btn_sels:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    log(f"  Found select button: {sel}")
                    record("select_from_computer_btn", sel)
                    await btn.click()
                    await asyncio.sleep(1)
                    break

        await page.screenshot(path=str(screenshot_dir / "file_dialog_ready.png"))

        if await file_input.count() == 0:
            log("  ✗ Still no file input — capturing and exiting")
            record("file_input", "NOT_FOUND")
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        record("file_input_selector", file_input_sel)
        log(f"Setting file input to {video_path}...")
        TIMINGS["file_set"] = time.time()
        await file_input.set_input_files(str(video_path))
        log("File set — waiting for upload/processing indicator...")
        await asyncio.sleep(3)

        await page.screenshot(path=str(screenshot_dir / "after_file_set.png"))
        if await check_for_challenge(page, screenshot_dir, "after_file_set"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        # ── 7. Probe crop / aspect ratio step ───────────────────────────────
        log("Probing crop/aspect step...")
        crop_sels = [
            '[aria-label="Select crop"]',
            '[aria-label="Выбрать кадрирование"]',
            'button[aria-label*="aspect"]',
            'button[aria-label*="crop"]',
            '[role="radiogroup"]',  # aspect ratio radio group
        ]
        for sel in crop_sels:
            el = page.locator(sel).first
            if await el.count() > 0:
                record("crop_step_selector", sel)
                await page.screenshot(path=str(screenshot_dir / "crop_step.png"))
                log(f"  Crop step found: {sel}")
                break
        else:
            record("crop_step_selector", "NOT_FOUND")

        # ── 8. Find Next button ─────────────────────────────────────────────
        log("Finding Next button...")
        next_sels = [
            'button:has-text("Next")',
            'button:has-text("Далее")',
            '[aria-label="Next"]',
            '[aria-label="Далее"]',
            'div[role="button"]:has-text("Next")',
        ]
        next_btn = None
        matched_next_sel = None
        for sel in next_sels:
            el = page.locator(sel).first
            if await el.count() > 0:
                next_btn = el
                matched_next_sel = sel
                break

        if next_btn is None:
            log("  ✗ No Next button found")
            record("next_btn_selector", "NOT_FOUND")
            await page.screenshot(path=str(screenshot_dir / "no_next_btn.png"), full_page=True)
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        record("next_btn_selector", matched_next_sel)
        log(f"  Next button: {matched_next_sel}")

        # Click through steps until we reach caption/Share
        step_num = 0
        while step_num < 5:
            step_num += 1
            log(f"Clicking Next (step {step_num})...")
            await next_btn.click()
            await asyncio.sleep(2)
            await page.screenshot(path=str(screenshot_dir / f"step_{step_num}.png"))

            if await check_for_challenge(page, screenshot_dir, f"step_{step_num}"):
                await _dump_and_exit(context, profile_path, screenshot_dir)
                return

            # Detect caption field
            caption_sels = [
                'textarea[aria-label*="caption"]',
                'textarea[aria-label*="подпись"]',
                'div[aria-label*="caption"][contenteditable]',
                'div[aria-label*="Write a caption"][contenteditable]',
                'div[role="textbox"][aria-label*="caption"]',
            ]
            caption_el = None
            matched_caption_sel = None
            for sel in caption_sels:
                el = page.locator(sel).first
                if await el.count() > 0:
                    caption_el = el
                    matched_caption_sel = sel
                    break

            if caption_el:
                record("caption_field_selector", matched_caption_sel)
                record("steps_before_caption", step_num)
                log(f"  Caption field found at step {step_num}: {matched_caption_sel}")
                await page.screenshot(path=str(screenshot_dir / "caption_step.png"))
                break

            # Still on a "Next" step
            next_btn_new = None
            for sel in next_sels:
                el = page.locator(sel).first
                if await el.count() > 0:
                    next_btn_new = el
                    matched_next_sel = sel
                    break
            if next_btn_new:
                next_btn = next_btn_new
            else:
                log("  No more Next buttons found")
                break

        # ── 9. Caption input ────────────────────────────────────────────────
        if caption_el is None:
            log("  ✗ Caption field not found — cannot fill caption")
            record("caption_field_selector", "NOT_FOUND")
        else:
            log("Filling caption 'тест'...")
            await caption_el.click()
            await asyncio.sleep(0.5)
            await caption_el.fill("тест")
            await asyncio.sleep(1)

        # ── 10. Probe Share / Publish button ────────────────────────────────
        log("Finding Share/Publish button...")
        share_sels = [
            'button:has-text("Share")',
            'button:has-text("Поделиться")',
            'button:has-text("Опубликовать")',
            'button:has-text("Publish")',
            '[aria-label="Share"]',
            '[aria-label="Поделиться"]',
            'div[role="button"]:has-text("Share")',
        ]
        share_btn = None
        matched_share_sel = None
        for sel in share_sels:
            el = page.locator(sel).first
            if await el.count() > 0:
                share_btn = el
                matched_share_sel = sel
                break

        if share_btn is None:
            log("  ✗ Share button not found")
            record("share_btn_selector", "NOT_FOUND")
            await page.screenshot(path=str(screenshot_dir / "no_share_btn.png"), full_page=True)
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        record("share_btn_selector", matched_share_sel)
        log(f"  Share button: {matched_share_sel}")

        # ── 11. Click Share ─────────────────────────────────────────────────
        log("Clicking Share...")
        TIMINGS["share_click"] = time.time()
        await share_btn.click()
        await asyncio.sleep(3)
        await page.screenshot(path=str(screenshot_dir / "after_share.png"))

        if await check_for_challenge(page, screenshot_dir, "after_share"):
            await _dump_and_exit(context, profile_path, screenshot_dir)
            return

        TIMINGS["after_share"] = time.time()

        # ── 12. Detect success marker ────────────────────────────────────────
        log("Waiting for success marker...")
        success_sels = [
            'span:has-text("Your reel has been shared")',
            'span:has-text("Ваш Reel опубликован")',
            'span:has-text("Your post has been shared")',
            'span:has-text("Публикация отправлена")',
            'span:has-text("Reel shared")',
            '[role="dialog"]:has-text("shared")',
            '[role="dialog"]:has-text("опубликован")',
        ]
        success_found = False
        success_sel = None
        for _ in range(20):  # up to 40s
            for sel in success_sels:
                el = page.locator(sel).first
                if await el.count() > 0:
                    success_found = True
                    success_sel = sel
                    TIMINGS["success"] = time.time()
                    break
            if success_found:
                break
            # Also check URL change to reel
            if "/reel/" in page.url:
                success_found = True
                success_sel = f"URL: {page.url}"
                TIMINGS["success"] = time.time()
                break
            await asyncio.sleep(2)

        await page.screenshot(path=str(screenshot_dir / "success_state.png"))

        if success_found:
            record("success_marker", success_sel)
            record("final_url", page.url)
            log(f"  ✓ Success: {success_sel}")
        else:
            record("success_marker", "NOT_FOUND")
            record("final_url", page.url)
            log("  ✗ No success marker found within 40s")

        # ── 13. Timings ──────────────────────────────────────────────────────
        if "file_set" in TIMINGS and "share_click" in TIMINGS:
            record("timing_file_to_share_s", round(TIMINGS["share_click"] - TIMINGS["file_set"], 1))
        if "share_click" in TIMINGS and "success" in TIMINGS:
            record(
                "timing_share_to_success_s", round(TIMINGS["success"] - TIMINGS["share_click"], 1)
            )

        # ── 14. Session persistence test ─────────────────────────────────────
        log("Saving storageState for persistence test...")
        state = await context.storage_state()
        _save_state(profile_path, state)
        record("storageState_final_saved", str(profile_path))
        log(f"  storageState saved to {profile_path} (chmod 600)")

        await context.close()

    # ── Output ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("RECON FINDINGS")
    print("=" * 60)
    for f in FINDINGS:
        print(f"  {f['key']:40s}  {f['value']}")
    print("\nTimings:", TIMINGS)
    print(f"\nScreenshots: {screenshot_dir}/")
    print("=" * 60)

    # Write JSON findings
    out = Path("runtime/ig_recon_findings.json")
    out.write_text(
        json.dumps({"findings": FINDINGS, "timings": TIMINGS}, ensure_ascii=False, indent=2)
    )
    log(f"Findings written to {out}")


async def _dump_and_exit(context, profile_path: Path, screenshot_dir: Path) -> None:
    try:
        state = await context.storage_state()
        _save_state(profile_path, state)
        log(f"storageState saved to {profile_path} (partial session, chmod 600)")
    except Exception:
        pass
    print("\n" + "=" * 60)
    print("RECON STOPPED (challenge / incomplete flow)")
    print("=" * 60)
    for f in FINDINGS:
        print(f"  {f['key']:40s}  {f['value']}")
    print(f"\nScreenshots: {screenshot_dir}/")
    out = Path("runtime/ig_recon_findings.json")
    out.write_text(
        json.dumps({"findings": FINDINGS, "timings": TIMINGS}, ensure_ascii=False, indent=2)
    )
    log(f"Partial findings written to {out}")
    await context.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Instagram Reels upload recon")
    parser.add_argument("--profile", default="runtime/ig_profile.json", help="storageState path")
    parser.add_argument("--video", default="runtime/test_reel_real.mp4", help="Test video path")
    parser.add_argument(
        "--no-login", action="store_true", help="Skip login wait (storageState must be valid)"
    )
    args = parser.parse_args()

    profile_path = Path(args.profile)
    video_path = Path(args.video)

    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        sys.exit(1)

    asyncio.run(main(profile_path, video_path, args.no_login))
