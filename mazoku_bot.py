#!/usr/bin/env python3
"""Playwright runner for Mazoku Legacy on Discord (Desktop & Termux ARM64).

Handles 31-minute /summon cycles, passive channel/event spawn claiming,
and /daily + /packs opening using an exported browser storage state.
"""

import argparse
import asyncio
import ctypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import io
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Set, Tuple
import urllib.request

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

try:
    from PIL import Image, ImageStat
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pytesseract
    for _win_path in (
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
    ):
        if os.path.isfile(_win_path):
            pytesseract.pytesseract.tesseract_cmd = _win_path
            break
    HAS_OCR = True
except ImportError:
    HAS_OCR = False


def load_dotenv_file(filepath: str = ".env") -> None:
    """Minimal .env loader so we don't need python-dotenv installed."""
    if not os.path.isfile(filepath):
        return
    with open(filepath, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


load_dotenv_file()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("mazoku_automation.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("mazoku")

# Summons refresh every 30m -> wait 31m (1860s) + random jitter before calling /summon
DEFAULT_SUMMON_INTERVAL = 1860

# Rarity rules from the Mazoku Legacy guide:
# C = v4000 max (120s claim cd), R = v2000 (300s), SR = v500 (900s), SSR = v150 (3600s), UR = v50
RARITY_TABLE: Dict[str, Dict[str, int]] = {
    "C":   {"base": 25,     "max_ver": 4000, "cd": 120},
    "R":   {"base": 150,    "max_ver": 2000, "cd": 300},
    "SR":  {"base": 3000,   "max_ver": 500,  "cd": 900},
    "SSR": {"base": 20000,  "max_ver": 150,  "cd": 3600},
    "UR":  {"base": 50000,  "max_ver": 50,   "cd": 3600},
    "EX":  {"base": 100000, "max_ver": 10,   "cd": 3600},
}

DEFAULT_WISHLIST = [
    w.strip().lower()
    for w in os.environ.get(
        "MAZOKU_WISHLIST",
        "gojo,sukuna,makima,frieren,sung jinwoo,rem,mikasa,luffy,zoro,levi",
    ).split(",")
    if w.strip()
]

CHAT_HEARTBEATS = [
    "gl on summons",
    "hoping for a low print today",
    "let's see what drops next",
    "saving bloodstones",
    "manifesting an ssr pull",
    "checking cooldowns",
]

PACK_NAMES = ["daily", "event", "starter", "standard", "premium"]


@dataclass
class CardOption:
    index: int
    name: str
    rarity: str
    version: int
    visual_bonus: float = 0.0
    score: float = 0.0


def keep_windows_awake() -> None:
    """Calls SetThreadExecutionState on Windows so the PC doesn't sleep mid-loop."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
            log.info("Windows sleep prevention active (ES_SYSTEM_REQUIRED).")
        except Exception as err:
            log.debug("SetThreadExecutionState skipped: %s", err)


def score_card(card: CardOption) -> float:
    tier = card.rarity.upper()
    if tier not in RARITY_TABLE:
        tier = "C"

    info = RARITY_TABLE[tier]
    base = float(info["base"])
    max_v = float(info["max_ver"])

    clamped_v = max(1, min(card.version, int(max_v)))
    scarcity_ratio = (max_v - clamped_v) / max_v

    low_print_bump = 0.0
    if card.version == 1:
        low_print_bump = base * 3.0 + 1500.0
    elif card.version <= 10:
        low_print_bump = base * 1.5 + 500.0
    elif card.version <= 50:
        low_print_bump = base * 0.5 + 150.0

    scarcity_pts = base * scarcity_ratio * 0.8 + low_print_bump
    wishlist_pts = 2500.0 if any(w in card.name.lower() for w in DEFAULT_WISHLIST) else 0.0

    card.score = round(base + scarcity_pts + wishlist_pts + card.visual_bonus, 2)
    return card.score


def parse_card_snippet(text: str, idx: int) -> CardOption:
    rarity_re = re.compile(
        r"\b(EX|UR|SSR|SR|R|C|Common|Rare|Super\s*Rare|Special\s*Super\s*Rare|Ultra\s*Rare)\b",
        re.IGNORECASE,
    )
    ver_re = re.compile(r"(?:\bv|ver\.?\s*|#)(\d{1,4})\b", re.IGNORECASE)

    norm_map = {
        "common": "C",
        "rare": "R",
        "super rare": "SR",
        "superrare": "SR",
        "special super rare": "SSR",
        "ultra rare": "UR",
    }

    r_hit = rarity_re.search(text)
    v_hit = ver_re.search(text)

    raw_r = r_hit.group(1) if r_hit else "C"
    tier = norm_map.get(re.sub(r"\s+", " ", raw_r.lower()), raw_r.upper())
    if tier not in RARITY_TABLE:
        tier = "C"

    default_ver = int(RARITY_TABLE[tier]["max_ver"] * 0.5)
    ver = int(v_hit.group(1)) if v_hit else default_ver

    cleaned = rarity_re.sub("", text)
    cleaned = ver_re.sub("", cleaned)
    cleaned = re.sub(r"[^a-zA-Z0-9\s\-']", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 2:
        cleaned = f"Card #{idx + 1}"

    opt = CardOption(index=idx, name=cleaned[:40], rarity=tier, version=ver)
    score_card(opt)
    return opt


def score_summon_image_slices(img_bytes: bytes, num_cards: int = 3) -> List[CardOption]:
    """Splits the composite summon image into N vertical slices and evaluates each.

    Common cards in Mazoku have a darkened frame shader, while R/SR/SSR cards
    have higher saturation and brighter top/bottom frame ribbons.
    """
    if not HAS_PIL:
        return []

    try:
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        w, h = img.size
        if w < 60 or h < 40:
            return []

        slice_w = w // num_cards
        results: List[CardOption] = []

        for idx in range(num_cards):
            x0 = idx * slice_w
            x1 = w if idx == num_cards - 1 else (idx + 1) * slice_w
            crop = img.crop((x0, 0, x1, h))

            hsv = crop.convert("HSV")
            stat = ImageStat.Stat(hsv)
            sat_mean = stat.mean[1]
            val_mean = stat.mean[2]
            val_std = stat.stddev[2]

            top_band = crop.crop((0, 0, crop.width, int(h * 0.22))).convert("HSV")
            bot_band = crop.crop((0, int(h * 0.75), crop.width, h)).convert("HSV")
            frame_sat = (ImageStat.Stat(top_band).mean[1] + ImageStat.Stat(bot_band).mean[1]) / 2.0
            frame_val = (ImageStat.Stat(top_band).mean[2] + ImageStat.Stat(bot_band).mean[2]) / 2.0

            vis_bonus = round(
                (sat_mean * 0.35)
                + (val_mean * 0.25)
                + (val_std * 0.30)
                + (frame_sat * 0.40)
                + (frame_val * 0.20),
                2,
            )

            ocr_txt = ""
            if HAS_OCR:
                try:
                    scaled = crop.resize((crop.width * 2, crop.height * 2))
                    ocr_txt = pytesseract.image_to_string(scaled, config="--psm 6")
                except Exception:
                    ocr_txt = ""

            opt = parse_card_snippet(ocr_txt, idx)
            opt.visual_bonus = vis_bonus
            if not ocr_txt.strip() and frame_sat > 115 and val_mean > 135:
                opt.rarity = "R"

            score_card(opt)
            results.append(opt)

        return results
    except Exception as err:
        log.warning("Image slice evaluation failed: %s", err)
        return []


def extract_cooldown_seconds(text: str) -> Optional[int]:
    lower = text.lower()
    if not any(k in lower for k in ("cooldown", "wait", "try again", "available in", "slow down")):
        return None

    secs = 0
    hr = re.search(r"(\d+)\s*(?:h|hr|hour|hours)\b", lower)
    mn = re.search(r"(\d+)\s*(?:m|min|minute|minutes)\b", lower)
    sc = re.search(r"(\d+)\s*(?:s|sec|second|seconds)\b", lower)
    if hr:
        secs += int(hr.group(1)) * 3600
    if mn:
        secs += int(mn.group(1)) * 60
    if sc:
        secs += int(sc.group(1))

    if secs == 0:
        rel = re.search(r"in\s+(\d+)\s+minute", lower)
        if rel:
            secs = int(rel.group(1)) * 60

    return secs if secs > 0 else None


class CooldownStore:
    def __init__(self, db_path: str = "mazoku_state.db") -> None:
        self.conn = sqlite3.connect(db_path)
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    card_name TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    score REAL NOT NULL,
                    source TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timers (
                    key TEXT PRIMARY KEY,
                    next_available_utc TEXT NOT NULL
                )
                """
            )

    def clear_timers(self) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM timers")

    def record_claim(self, card: CardOption, source: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO claims (timestamp, card_name, rarity, version, score, source) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    card.name,
                    card.rarity,
                    card.version,
                    card.score,
                    source,
                ),
            )

    def set_timer(self, key: str, seconds: int) -> None:
        ready_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO timers (key, next_available_utc)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET next_available_utc = excluded.next_available_utc
                """,
                (key, ready_at.isoformat()),
            )

    def check_timer(self, key: str) -> Tuple[bool, float]:
        cur = self.conn.cursor()
        cur.execute("SELECT next_available_utc FROM timers WHERE key = ?", (key,))
        row = cur.fetchone()
        if not row:
            return True, 0.0
        target = datetime.fromisoformat(row[0])
        now = datetime.now(timezone.utc)
        if now >= target:
            return True, 0.0
        return False, (target - now).total_seconds()


def notify_webhook(url: Optional[str], title: str, body: str, color: int = 0x0F766E) -> None:
    if not url or "YOUR_WEBHOOK" in url:
        return
    payload = {
        "username": "Mazoku Runner",
        "embeds": [
            {
                "title": title,
                "description": body,
                "color": color,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "MazokuRunner/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as err:
        log.warning("Webhook POST failed: %s", err)


class MazokuRunner:
    def __init__(
        self,
        channel_url: str,
        state_file: str = "discord_state.json",
        webhook_url: Optional[str] = None,
        summon_interval: int = DEFAULT_SUMMON_INTERVAL,
        is_premium: bool = False,
        headless: bool = False,
        keep_timers: bool = False,
        duration_minutes: int = 0,
        run_once: bool = False,
    ) -> None:
        self.channel_url = channel_url
        self.state_file = state_file
        self.webhook_url = webhook_url
        self.summon_interval = summon_interval
        self.is_premium = is_premium
        if sys.platform != "win32" and not os.environ.get("DISPLAY"):
            self.headless = True
        else:
            self.headless = headless
        self.duration_minutes = duration_minutes
        self.run_once = run_once
        self.store = CooldownStore()
        if not keep_timers:
            self.store.clear_timers()
        self.seen_messages: Set[str] = set()
        self._mx = random.randint(350, 850)
        self._my = random.randint(250, 600)

    async def jitter_sleep(self, low: float, high: float) -> None:
        await asyncio.sleep(random.triangular(low, high, (low + high) / 2.0))

    async def close_popups(self, page: Page) -> None:
        """Dismisses Discord's 'Continue in Browser' splash or modal overlays."""
        try:
            cont = page.locator('button:has-text("Continue in Browser"), a:has-text("Continue in Browser")')
            if await cont.count() > 0 and await cont.first.is_visible():
                log.info("Clicking 'Continue in Browser'...")
                await cont.first.click()
                await self.jitter_sleep(1.5, 2.5)

            close_btn = page.locator('[aria-label="Close"], button:has-text("Got it")')
            if await close_btn.count() > 0 and await close_btn.first.is_visible():
                await close_btn.first.click()
                await self.jitter_sleep(0.4, 0.9)
        except Exception:
            pass

    async def smooth_click(self, page: Page, target: Locator) -> None:
        try:
            await target.scroll_into_view_if_needed(timeout=3000)
            box = await target.bounding_box()
            if not box:
                await target.click(timeout=5000)
                return

            tx = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            ty = box["y"] + box["height"] * random.uniform(0.25, 0.75)

            steps = random.randint(10, 20)
            for s in range(1, steps + 1):
                frac = s / float(steps)
                cx = self._mx + (tx - self._mx) * frac + random.uniform(-2.0, 2.0)
                cy = self._my + (ty - self._my) * frac + random.uniform(-2.0, 2.0)
                await page.mouse.move(cx, cy)
                await asyncio.sleep(random.uniform(0.008, 0.020))

            self._mx, self._my = tx, ty
            await self.jitter_sleep(0.12, 0.32)
            await page.mouse.down()
            await self.jitter_sleep(0.05, 0.14)
            await page.mouse.up()
        except Exception:
            try:
                await target.click(force=True, timeout=5000)
            except Exception:
                pass

    async def type_human(self, page: Page, text: str) -> None:
        for ch in text:
            await page.keyboard.type(ch, delay=random.randint(60, 150))
            if random.random() < 0.10:
                await self.jitter_sleep(0.10, 0.25)

    async def focus_input(self, page: Page) -> Locator:
        await self.close_popups(page)
        box = page.locator('div[role="textbox"]').first
        await self.smooth_click(page, box)
        await self.jitter_sleep(0.3, 0.6)
        await page.keyboard.press("Control+A")
        await asyncio.sleep(0.08)
        await page.keyboard.press("Backspace")
        await self.jitter_sleep(0.2, 0.4)
        return box

    async def send_slash_command(
        self, page: Page, command: str, option_val: Optional[str] = None
    ) -> None:
        """Types a slash command and clicks its entry in Discord's autocomplete popup.

        Without clicking the div[role="option"] node in the popup, Slate.js often
        submits '/summon' as a normal text message instead of an app command.
        """
        await self.focus_input(page)
        await page.keyboard.type("/", delay=random.randint(80, 140))
        await self.jitter_sleep(0.4, 0.8)

        name = command.lstrip("/")
        await self.type_human(page, name)
        await self.jitter_sleep(1.2, 2.0)

        popup_items = page.locator(
            'div[id^="autocomplete-"] div[role="option"], div[class*="autocomplete"] div[role="option"]'
        )
        n_items = await popup_items.count()
        picked = False
        if n_items > 0:
            for i in range(min(n_items, 5)):
                item = popup_items.nth(i)
                label = (await item.inner_text()).lower()
                if name.lower() in label:
                    await self.smooth_click(page, item)
                    picked = True
                    break
            if not picked:
                await self.smooth_click(page, popup_items.first)
                picked = True

        if not picked:
            await page.keyboard.press("Tab")

        await self.jitter_sleep(0.6, 1.1)

        if option_val:
            await self.type_human(page, option_val)
            await self.jitter_sleep(0.8, 1.4)
            sub_items = page.locator('div[id^="autocomplete-"] div[role="option"]')
            if await sub_items.count() > 0:
                await page.keyboard.press("Tab")
                await self.jitter_sleep(0.3, 0.6)

        await page.keyboard.press("Enter")
        await self.jitter_sleep(0.3, 0.6)
        await page.keyboard.press("Enter")
        log.info("Sent command: %s%s", command, f" ({option_val})" if option_val else "")

    async def post_heartbeat(self, page: Page) -> None:
        line = random.choice(CHAT_HEARTBEATS)
        await self.focus_input(page)
        await self.type_human(page, line)
        await self.jitter_sleep(0.4, 0.8)
        await page.keyboard.press("Enter")
        log.info("Sent anti-sniping chat line: '%s'", line)
        self.store.set_timer("heartbeat", random.randint(2100, 3300))

    async def find_card_buttons(self, msg_loc: Locator) -> List[Locator]:
        """Finds interactive component buttons inside #message-accessories, ignoring hover icons."""
        max_btns = 4 if self.is_premium else 3
        btns = msg_loc.locator('[id^="message-accessories-"] button')
        total = await btns.count()

        valid: List[Locator] = []
        for i in range(total):
            b = btns.nth(i)
            if not await b.is_visible() or await b.is_disabled():
                continue
            aria = (await b.get_attribute("aria-label") or "").lower()
            if any(skip in aria for skip in ("reaction", "reply", "more", "edit", "delete")):
                continue
            valid.append(b)

        return valid[:max_btns] if len(valid) >= 3 else []

    async def evaluate_spawn(
        self, msg_loc: Locator, buttons: List[Locator]
    ) -> List[CardOption]:
        count = len(buttons)
        raw_text = await msg_loc.inner_text()

        slice_options: List[CardOption] = []
        try:
            imgs = msg_loc.locator('[id^="message-accessories-"] img')
            n_imgs = await imgs.count()
            largest_bytes: Optional[bytes] = None
            best_area = 0
            for i in range(n_imgs):
                im = imgs.nth(i)
                box = await im.bounding_box()
                if box and (box["width"] * box["height"]) > best_area and box["width"] >= 150:
                    best_area = box["width"] * box["height"]
                    largest_bytes = await im.screenshot()

            if largest_bytes:
                slice_options = score_summon_image_slices(largest_bytes, num_cards=count)
        except Exception as err:
            log.debug("Image capture skipped: %s", err)

        final_options: List[CardOption] = []
        for idx in range(count):
            btn_label = ""
            try:
                btn_label = (await buttons[idx].inner_text()).strip()
            except Exception:
                pass

            if idx < len(slice_options):
                opt = slice_options[idx]
                if btn_label:
                    from_btn = parse_card_snippet(btn_label, idx)
                    if from_btn.rarity != "C" or opt.rarity == "C":
                        opt.rarity = from_btn.rarity
                    if from_btn.version != int(RARITY_TABLE[from_btn.rarity]["max_ver"] * 0.5):
                        opt.version = from_btn.version
                    if "Card #" in opt.name and "Card #" not in from_btn.name:
                        opt.name = from_btn.name
                    score_card(opt)
                final_options.append(opt)
            else:
                final_options.append(parse_card_snippet(btn_label or raw_text, idx))

        return final_options

    async def claim_visible_spawn(self, page: Page, source: str = "channel_spawn") -> bool:
        ready, _ = self.store.check_timer("claim_cooldown")
        if not ready:
            return False

        rows = page.locator('li[id^="chat-messages-"]')
        total = await rows.count()
        if total == 0:
            return False

        for idx in range(total - 1, max(0, total - 6) - 1, -1):
            msg = rows.nth(idx)
            msg_id = await msg.get_attribute("id") or f"row_{idx}"
            if msg_id in self.seen_messages:
                continue

            buttons = await self.find_card_buttons(msg)
            if len(buttons) >= 3:
                log.info("Found %d-card spawn (%s) in %s", len(buttons), source, msg_id)
                options = await self.evaluate_spawn(msg, buttons)

                for opt in options:
                    log.info(
                        "  -> Card #%d: [%s] %s (v%d, vis=%.1f) => Score: %.1f",
                        opt.index + 1,
                        opt.rarity,
                        opt.name,
                        opt.version,
                        opt.visual_bonus,
                        opt.score,
                    )

                await self.jitter_sleep(3.2, 7.5)
                winner = max(options, key=lambda c: c.score)
                btn_idx = min(winner.index, len(buttons) - 1)

                log.info(
                    "Clicking Button #%d: [%s] %s (v%d) | Score: %.1f",
                    btn_idx + 1,
                    winner.rarity,
                    winner.name,
                    winner.version,
                    winner.score,
                )
                await self.smooth_click(page, buttons[btn_idx])
                self.seen_messages.add(msg_id)

                cd_sec = RARITY_TABLE.get(winner.rarity, RARITY_TABLE["C"])["cd"]
                self.store.record_claim(winner, source=source)
                self.store.set_timer("claim_cooldown", cd_sec + random.randint(8, 25))

                notify_webhook(
                    self.webhook_url,
                    title=f"Claimed ({source}): [{winner.rarity}] {winner.name}",
                    body=(
                        f"**Button**: `#{btn_idx + 1}` of `{len(buttons)}`\n"
                        f"**Tier**: `{winner.rarity}` | **Version**: `v{winner.version}`\n"
                        f"**Score**: `{winner.score}` | **Claim CD**: `{cd_sec}s`"
                    ),
                )
                return True

        return False

    async def trigger_scheduled_summon(self, page: Page) -> None:
        ready, wait_s = self.store.check_timer("claim_cooldown")
        if not ready:
            log.info("Claim cooldown active (%.0fs left); deferring /summon.", wait_s)
            return

        log.info("Running scheduled /summon...")
        await self.send_slash_command(page, "/summon")

        deadline = asyncio.get_event_loop().time() + 25.0
        while asyncio.get_event_loop().time() < deadline:
            await self.jitter_sleep(2.2, 3.5)

            if await self.claim_visible_spawn(page, source="manual_summon"):
                nxt = self.summon_interval + random.randint(15, 75)
                self.store.set_timer("summon", nxt)
                log.info("Next /summon scheduled in %.1f minutes.", nxt / 60.0)
                return

            rows = page.locator('li[id^="chat-messages-"]')
            n_rows = await rows.count()
            if n_rows > 0:
                latest_txt = await rows.nth(n_rows - 1).inner_text()
                cd = extract_cooldown_seconds(latest_txt)
                if cd is not None:
                    nxt = cd + random.randint(45, 95)
                    log.info("Cooldown detected (%ds). Next /summon in %.1f min.", cd, nxt / 60.0)
                    self.store.set_timer("summon", nxt)
                    return

        log.warning("No summon buttons detected within 25s; retrying in 2 minutes.")
        self.store.set_timer("summon", random.randint(110, 145))

    async def sweep_daily_and_packs(self, page: Page) -> None:
        ready, _ = self.store.check_timer("daily_pack")
        if not ready:
            return

        log.info("Claiming /daily reward...")
        await self.send_slash_command(page, "/daily")
        await self.jitter_sleep(5.0, 8.5)

        log.info("Checking inventory via /packs...")
        await self.send_slash_command(page, "/packs")
        await self.jitter_sleep(5.0, 8.0)

        rows = page.locator('li[id^="chat-messages-"]')
        n_rows = await rows.count()
        to_open: List[str] = []

        if n_rows > 0:
            buf = ""
            for i in range(max(0, n_rows - 3), n_rows):
                buf += "\n" + (await rows.nth(i).inner_text()).lower()
            for p_name in PACK_NAMES:
                if p_name in buf and not re.search(rf"{p_name}[^\n:]*[:\-]?\s*0\b", buf):
                    to_open.append(p_name)

        fallback_pack = "premium" if self.is_premium else "daily"
        if fallback_pack not in to_open:
            to_open.insert(0, fallback_pack)

        log.info("Opening packs: %s", ", ".join(to_open))
        for p_name in to_open:
            await self.send_slash_command(page, "/open", option_val=p_name)
            await self.jitter_sleep(5.5, 9.5)

        nxt_sweep = 21600 + random.randint(300, 1200)
        self.store.set_timer("daily_pack", nxt_sweep)
        notify_webhook(
            self.webhook_url,
            title="Daily & Pack Sweep Finished",
            body=f"Opened: `{', '.join(to_open)}`. Next check in `{round(nxt_sweep / 3600.0, 1)}h`.",
        )

    def _prepare_session_file(self) -> None:
        raw_env = os.environ.get("DISCORD_STATE_JSON", "").strip()
        if raw_env and not os.path.exists(self.state_file):
            with open(self.state_file, "w", encoding="utf-8") as f:
                f.write(raw_env)
            log.info("Wrote %s from DISCORD_STATE_JSON env var.", self.state_file)

        if not os.path.exists(self.state_file):
            log.error("Missing '%s'. Run `python mazoku_bot.py --login` first.", self.state_file)
            sys.exit(1)

    async def _start_chromium(self, playwright_instance):
        flags = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-first-run",
        ]
        try:
            return await playwright_instance.chromium.launch(headless=self.headless, args=flags)
        except Exception as err:
            for candidate in ("/usr/bin/chromium-browser", "/usr/bin/chromium", shutil.which("chromium") or ""):
                if candidate and os.path.isfile(candidate):
                    log.info("Falling back to system Chromium at %s", candidate)
                    return await playwright_instance.chromium.launch(
                        executable_path=candidate,
                        headless=self.headless,
                        args=flags + ["--single-process"],
                    )
            raise err

    async def run(self) -> None:
        self._prepare_session_file()
        keep_windows_awake()
        started = time.time()

        while True:
            try:
                async with async_playwright() as pw:
                    browser = await self._start_chromium(pw)
                    ctx: BrowserContext = await browser.new_context(
                        storage_state=self.state_file,
                        viewport={"width": 1366, "height": 768},
                        locale="en-US",
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                        ),
                    )
                    await ctx.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    )

                    page = await ctx.new_page()
                    log.info("Navigating to channel: %s", self.channel_url)
                    await page.goto(self.channel_url, wait_until="domcontentloaded", timeout=60000)
                    await self.jitter_sleep(8.0, 12.0)
                    await self.close_popups(page)

                    if "login" in page.url:
                        notify_webhook(
                            self.webhook_url,
                            title="Session Expired",
                            body="Discord redirected to `/login`. Refresh `discord_state.json`.",
                            color=0xDC2626,
                        )
                        raise RuntimeError("Discord session expired (redirected to /login).")

                    log.info("Connected. Entering 24/7 monitoring loop...")
                    notify_webhook(
                        self.webhook_url,
                        title="Mazoku Runner Online",
                        body="Monitoring 31m `/summon`, passive/event spawns, and `/daily` packs.",
                    )

                    ticks = 0
                    while True:
                        ticks += 1
                        await self.claim_visible_spawn(page, source="event_or_auto_spawn")

                        hb_ok, _ = self.store.check_timer("heartbeat")
                        if hb_ok:
                            await self.post_heartbeat(page)
                            await self.jitter_sleep(3.5, 6.5)

                        sum_ok, _ = self.store.check_timer("summon")
                        clm_ok, _ = self.store.check_timer("claim_cooldown")
                        if sum_ok and clm_ok:
                            await self.trigger_scheduled_summon(page)
                            await self.jitter_sleep(3.0, 6.0)

                        await self.sweep_daily_and_packs(page)

                        if self.run_once:
                            log.info("Single run (--once) complete.")
                            return

                        if self.duration_minutes > 0 and (time.time() - started) / 60.0 >= self.duration_minutes:
                            log.info("Duration limit (%d min) reached.", self.duration_minutes)
                            return

                        if ticks % 12 == 0:
                            _, sw = self.store.check_timer("summon")
                            _, cw = self.store.check_timer("claim_cooldown")
                            log.info("Watching channel | Next /summon in %.1f min", max(sw, cw) / 60.0)

                        await self.jitter_sleep(6.0, 10.5)

            except KeyboardInterrupt:
                log.info("Interrupted by user.")
                break
            except Exception as err:
                log.error("Loop error (%s); restarting browser in 30s...", err)
                await asyncio.sleep(30.0)


async def export_login_session(state_file: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(viewport={"width": 1366, "height": 768})
        page = await ctx.new_page()
        await page.goto("https://discord.com/login")
        print("\n" + "-" * 64)
        print("1. Log into Discord in the browser window that just opened.")
        print("2. Wait until your server and channel list finishes loading.")
        print(f"3. Press ENTER here to save your session to '{state_file}'.")
        print("-" * 64 + "\n")
        await asyncio.get_event_loop().run_in_executor(None, input)
        await ctx.storage_state(path=state_file)
        print(f"Saved session state to {state_file}")
        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Mazoku Legacy 24/7 Playwright Runner")
    parser.add_argument("--login", action="store_true", help="Open browser to log in and save discord_state.json")
    parser.add_argument(
        "--channel-url",
        type=str,
        default=os.environ.get("MAZOKU_CHANNEL_URL", ""),
        help="Discord channel URL (or set MAZOKU_CHANNEL_URL in .env)",
    )
    parser.add_argument("--state-file", type=str, default="discord_state.json", help="Session state JSON path")
    parser.add_argument(
        "--webhook-url",
        type=str,
        default=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        help="Optional Discord webhook URL for notifications",
    )
    parser.add_argument("--summon-interval", type=int, default=DEFAULT_SUMMON_INTERVAL, help="Seconds between /summon")
    parser.add_argument("--premium", action="store_true", help="Enable if account has Mazoku Premium (4 cards)")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--keep-timers", action="store_true", help="Keep previous SQLite timers on startup")
    parser.add_argument("--duration-minutes", type=int, default=0, help="Stop after N minutes")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    if args.login:
        asyncio.run(export_login_session(args.state_file))
        return

    if not args.channel_url or "YOUR_SERVER_ID" in args.channel_url:
        parser.error(
            "Missing channel URL. Pass --channel-url 'https://discord.com/channels/SERVER_ID/CHANNEL_ID' "
            "or set MAZOKU_CHANNEL_URL in your .env file."
        )

    runner = MazokuRunner(
        channel_url=args.channel_url,
        state_file=args.state_file,
        webhook_url=args.webhook_url,
        summon_interval=args.summon_interval,
        is_premium=args.premium,
        headless=args.headless,
        keep_timers=args.keep_timers,
        duration_minutes=args.duration_minutes,
        run_once=args.once,
    )
    asyncio.run(runner.run())


if __name__ == "__main__":
    main()