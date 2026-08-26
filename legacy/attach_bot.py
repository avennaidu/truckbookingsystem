"""
Navis N4 Appointment Bot - ATTACH MODE
======================================
You do the setup by hand; the bot does only the repetitive slot-grabbing.

HOW IT WORKS
------------
1. You start Chrome in "debug" mode (one-time command below).
2. You log in to N4, click +, open the Add Appointment screen, and set the
   fixed fields: Gate/Zone (tower), Transaction Type = Pick Up Import,
   Trucking Company = AVEMEL. Leave the dialog open on the first container
   blank / ready for input.
3. You run this bot. It connects to that SAME window and, for each
   container in containers.csv: types the container, sets today's date,
   picks the first open slot, saves. On success it moves to the next
   container - reopening a fresh Add Appointment dialog by clicking + again
   (the fixed fields are re-selected automatically from config).

STARTING CHROME IN DEBUG MODE (do this instead of opening Chrome normally)
--------------------------------------------------------------------------
Close all Chrome windows first, then in Command Prompt run ONE line:

  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\\navis-chrome"

(If Chrome is in Program Files (x86), use that path instead.)
A fresh Chrome opens. Log in to N4 in it and get to the appointment screen.

THEN RUN
--------
  python attach_bot.py
"""

import csv
import json
import logging
import random
import time
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CONFIG_FILE = Path("config.json")
CONTAINERS_FILE = Path("containers.csv")
RESULTS_FILE = Path("results.csv")
DEBUG_URL = "http://localhost:9222"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("attach-bot")

TERMINAL = {"BOOKED", "SKIPPED"}


def load_config():
    return json.loads(CONFIG_FILE.read_text())


def done_set():
    d = set()
    if RESULTS_FILE.exists():
        for r in csv.DictReader(open(RESULTS_FILE)):
            if r["status"] in TERMINAL:
                d.add(r["container"])
    return d


def load_containers():
    done = done_set()
    out = []
    for r in csv.DictReader(open(CONTAINERS_FILE)):
        c = r.get("container", "").strip().upper()
        if c and c not in done:
            out.append(c)
    return out


def record(container, status, detail=""):
    new = not RESULTS_FILE.exists()
    with open(RESULTS_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "container", "status", "detail"])
        w.writerow([datetime.now().isoformat(), container, status, detail])


# --- ZK dialog helpers (same anchoring as the main bot) -------------------
class Dialog:
    def __init__(self, page):
        self.page = page

    @property
    def win(self):
        return self.page.locator(
            ".z-window:has-text('Add Appointment')"
        ).last

    def input_after(self, label):
        return self.win.locator(f"input:right-of(:text('{label}'))").first

    def combo_btn_after(self, label):
        return self.win.locator(
            f".z-combobox-btn:right-of(:text('{label}')), "
            f"i.z-combobox-icon:right-of(:text('{label}'))"
        ).first

    def pick_combo(self, label, match):
        self.combo_btn_after(label).click()
        self.page.wait_for_timeout(600)
        item = self.page.locator(
            f".z-comboitem:visible:has-text('{match}')"
        ).first
        if item.count() == 0:
            self.page.keyboard.press("Escape")
            return False
        item.click()
        self.page.wait_for_timeout(400)
        return True

    def openings(self):
        self.combo_btn_after("Appointment Openings").click()
        self.page.wait_for_timeout(800)
        items = self.page.locator(".z-comboitem:visible")
        return [items.nth(i).inner_text().strip() for i in range(items.count())]

    def click_opening(self, text):
        self.page.locator(
            f".z-comboitem:visible:has-text('{text}')"
        ).first.click()
        self.page.wait_for_timeout(400)


class AttachBot:
    def __init__(self, cfg, page):
        self.cfg = cfg
        self.page = page
        self.dlg = Dialog(page)

    def dialog_present(self):
        return self.page.locator(
            ".z-window:has-text('Add Appointment'), :text('Appointment Nbr')"
        ).count() > 0

    def dismiss(self, contains):
        box = self.page.locator(f".z-window:visible:has-text('{contains}')")
        if box.count() > 0:
            try:
                box.locator("text='OK'").first.click()
            except Exception:
                self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(400)
            return True
        return False

    def ensure_fixed_fields(self):
        """Re-assert the three fixed fields if they aren't already set."""
        d = self.dlg
        d.pick_combo("Gate/Zone", self.cfg["tower"])
        d.pick_combo("Transaction Type", self.cfg["transaction_type"])
        d.pick_combo("Trucking Company", self.cfg["trucking_company"])

    def book_one(self, container) -> tuple[str, str]:
        d = self.dlg
        if not self.dialog_present():
            return "NODIALOG", "Add Appointment dialog is not open"

        d.input_after("Container Id").fill(container)
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(1200)   # BL auto-populates

        if self.dismiss("Application Error"):
            return "HOLD", "unit problem (e.g. !IMPORT RELEASE)"

        # date = today
        db = d.input_after("Requested Date")
        db.click()
        db.fill(date.today().strftime(self.cfg.get("date_format", "%Y-%m-%d")))
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(1200)

        if self.dismiss("No Appointment Openings"):
            return "RETRY", "no openings today"

        chosen = None
        for text in d.openings():
            import re
            m = re.search(r"Current Openings:\s*(\d+)", text)
            if m and int(m.group(1)) > 0:
                d.click_opening(text.split("(")[0].strip())
                chosen = text
                break
        if not chosen:
            self.page.keyboard.press("Escape")
            return "RETRY", "openings list empty"

        d.win.locator("text='Save'").first.click()
        self.page.wait_for_timeout(1800)

        if self.dismiss("Application Error"):
            return "HOLD", f"save rejected ({chosen})"
        if self.dismiss("No Appointment Openings"):
            return "RETRY", f"slot vanished ({chosen})"

        # success if dialog closed
        if not self.dialog_present():
            return "BOOKED", f"{date.today()} {chosen}"
        return "BROKEN", f"no confirmation ({chosen})"

    def reopen_dialog(self):
        """Click + to open a fresh Add Appointment (new tab) and set the
        fixed fields, for the NEXT container after a booking."""
        plus = self.page.locator(
            "button.zebra-open-new-tab, .zebra-welcome-page-plus-button button"
        ).first
        if plus.count() == 0:
            # dialog may already be reusable; just re-assert fields
            if self.dialog_present():
                self.ensure_fixed_fields()
            return
        ctx = self.page.context
        try:
            with ctx.expect_page(timeout=10000) as info:
                plus.click()
            self.page = info.value
            self.page.wait_for_load_state("networkidle")
            self.dlg = Dialog(self.page)
        except PWTimeout:
            self.page.wait_for_timeout(800)
        self.page.wait_for_selector(
            ".z-window:has-text('Add Appointment'), :text('Appointment Nbr')",
            timeout=15000
        )
        self.ensure_fixed_fields()


def find_n4_page(browser):
    """Among open tabs, pick the one showing N4 (has the dialog or the
    welcome +). Fall back to the first tab."""
    for ctx in browser.contexts:
        for p in ctx.pages:
            try:
                if p.locator(
                    ":text('Add Appointment'), button.zebra-open-new-tab, "
                    ":text('Appointment Nbr')"
                ).count() > 0:
                    return p
            except Exception:
                continue
    # fallback
    for ctx in browser.contexts:
        if ctx.pages:
            return ctx.pages[0]
    return None


def run():
    cfg = load_config()
    poll = int(cfg.get("poll_seconds", 15))

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.connect_over_cdp(DEBUG_URL)
        except Exception as e:
            log.error("Could not connect to Chrome on %s.\n"
                      "Did you start Chrome with --remote-debugging-port=9222?\n%s",
                      DEBUG_URL, e)
            return

        page = find_n4_page(browser)
        if page is None:
            log.error("No open tab found. Open N4 in the debug Chrome first.")
            return
        bot = AttachBot(cfg, page)
        log.info("Attached. Tower %s | %s | %s",
                 cfg["tower"], cfg["trucking_company"], cfg["transaction_type"])

        if not bot.dialog_present():
            log.info("Add Appointment not open yet - opening it for you.")
            bot.reopen_dialog()
        else:
            bot.ensure_fixed_fields()

        deadline = time.time() + float(cfg.get("max_hours", 6)) * 3600

        while time.time() < deadline:
            pending = load_containers()
            if not pending:
                log.info("All containers processed. Done.")
                break
            container = pending[0]
            log.info("Working %s (%d remaining after it)",
                     container, len(pending) - 1)

            attempt = 0
            while time.time() < deadline:
                attempt += 1
                try:
                    status, detail = bot.book_one(container)
                except Exception as e:
                    log.warning("Attempt %d error: %s", attempt, e)
                    status, detail = "BROKEN", str(e)

                if status == "BOOKED":
                    log.info("BOOKED %s -> %s", container, detail)
                    record(container, "BOOKED", detail)
                    bot.reopen_dialog()          # ready next container
                    break
                if status == "HOLD":
                    log.warning("SKIP %s (%s)", container, detail)
                    record(container, "SKIPPED", detail)
                    bot.reopen_dialog()
                    break
                if status == "NODIALOG":
                    log.info("Dialog closed - reopening.")
                    bot.reopen_dialog()
                    continue
                if status == "BROKEN":
                    bot.reopen_dialog()

                wait = poll + random.uniform(0, max(2, poll * 0.3))
                if attempt % 10 == 0:
                    log.info("%s: attempt %d, no slot yet", container, attempt)
                time.sleep(wait)

        log.info("Finished. See results.csv / bot.log")


if __name__ == "__main__":
    run()
