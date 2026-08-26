"""
Navis N4 Appointment Bot - ATTACH MODE, SINGLE TOWER
====================================================
ONE tower, ONE list. You do the login/setup by hand; the bot books
every container in containers.csv onto the tower set in config.json.

The container list is a plain single column:

    container
    PCIU9529335
    TRHU8231290

The tower comes from config.json ("tower": "109"). The bot camps: it
works down the list top-to-bottom, keeping the dialog on that tower,
entering each container and grabbing the first open slot. Booked / held /
already-booked containers drop out via results.csv.

HOW IT WORKS
------------
1. Start Chrome in debug mode (see the command below).
2. Log in to N4, click +, open the Add Appointment screen. The bot sets
   the tower for you, so you only need the dialog open.
3. Run:  python attach_one.py

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
CONTAINERS_FILE = Path("containers.csv")   # plain single column: container
RESULTS_FILE = Path("results.csv")
DEBUG_URL = "http://localhost:9222"
VALID_TOWERS = {"109", "202", "203", "205"}

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
    """Return list of container strings not yet done, in file order."""
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
        # Text inputs on the form are z-textbox / z-datebox / z-combobox
        # inputs. Match the nearest input on the same row as the label.
        return self.win.locator(
            f"input.z-textbox:near(:text('{label}'), 80), "
            f"input.z-datebox-input:near(:text('{label}'), 80), "
            f"input.z-combobox-input:near(:text('{label}'), 80), "
            f"input:near(:text('{label}'), 80)"
        ).first

    def combo_input(self, label):
        """The combobox's text input (class z-combobox-input) on the same
        row as the label."""
        return self.win.locator(
            f"input.z-combobox-input:near(:text('{label}'), 60)"
        ).first

    def combo_btn_after(self, label):
        # Real structure (from DevTools): the arrow is <a class=
        # "z-combobox-button"> next to input.z-combobox-input.
        return self.win.locator(
            f"a.z-combobox-button:near(:text('{label}'), 60)"
        ).first

    def pick_combo(self, label, match, exact=False):
        """Open the combobox next to `label` and choose an item. If exact,
        match the option whose full text equals `match` (needed for
        Gate/Zone where 109 / 109 REEFER / 109A all contain '109');
        otherwise substring match."""
        opened = False
        btn = self.combo_btn_after(label)
        if btn.count() > 0:
            try:
                btn.click(timeout=3000)
                opened = True
            except Exception:
                opened = False
        if not opened:
            inp = self.combo_input(label)
            try:
                inp.click(timeout=3000)
                self.page.keyboard.press("Alt+ArrowDown")
                opened = True
            except Exception:
                return False
        self.page.wait_for_timeout(600)

        if exact:
            items = self.page.locator(".z-comboitem:visible")
            item = None
            for i in range(items.count()):
                if items.nth(i).inner_text().strip() == match:
                    item = items.nth(i)
                    break
            if item is None:
                self.page.keyboard.press("Escape")
                return False
        else:
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

    def read_error(self) -> str | None:
        """If an error/info popup is showing, return its text (lowercased)
        and dismiss it. Returns None if no popup."""
        box = self.page.locator(
            ".z-window:visible:has-text('Error'), "
            ".z-messagebox-window:visible, "
            ".z-window:visible:has-text('failed')"
        )
        if box.count() == 0:
            return None
        try:
            txt = box.first.inner_text().lower()
        except Exception:
            txt = ""
        try:
            box.locator("text='OK'").first.click()
        except Exception:
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(400)
        return txt

    def classify_error(self, txt: str) -> tuple[str, str]:
        """Map an error message to a status. Keywords are matched loosely
        so we catch variants without knowing N4's exact wording."""
        t = txt or ""
        # already booked / duplicate appointment -> skip, don't retry
        if any(k in t for k in (
            "already has an appointment", "already has appointment",
            "existing appointment", "appointment already",
            "already booked", "duplicate appointment",
            "an appointment exists", "already an appointment",
        )):
            return "SKIPPED", "already has an appointment"
        # release / customs hold -> skip, retry won't help
        if any(k in t for k in (
            "import release", "!import release", "not released",
            "customs", "hold",
        )):
            return "SKIPPED", "unit on hold / not released"
        # no openings -> retry
        if "no appointment opening" in t or "no openings" in t:
            return "RETRY", "no openings"
        # anything else: treat as a soft error, rebuild and retry
        return "RETRY", (txt[:80] if txt else "unknown error")

    def gate_zone_value(self):
        """Read the current Gate/Zone combobox text, e.g. '109 (ITZ 109)'."""
        try:
            return self.dlg.combo_input("Gate/Zone").input_value()
        except Exception:
            return ""

    def ensure_fixed_fields(self, tower):
        """Set Gate/Zone to the given tower ONLY. Transaction Type and
        Trucking Company are left exactly as the user set them by hand -
        the bot never touches those dropdowns (avoids opening the wrong
        one, like Line Operator)."""
        # The dropdown has near-duplicates (109, 109 REEFER, 109A), so we
        # match the EXACT gate label for each tower, not just the number.
        gate_labels = self.cfg.get("gate_labels", {
            "109": "109 (ITZ 109)",
            "202": "202 (ITZ 202)",
            "203": "203 (ITZ 203 Virtual Gate)",
            "205": "205 (ITZ 205)",
        })
        want_label = gate_labels.get(tower, tower)

        current = self.gate_zone_value()
        if want_label and want_label in current:
            return  # already on the right gate, nothing to do
        ok = self.dlg.pick_combo("Gate/Zone", want_label, exact=True)
        if not ok:
            log.warning("Gate '%s' (tower %s) not found in list",
                        want_label, tower)

    def load_container(self, container) -> tuple[str, str]:
        """Type the container into an open dialog and confirm it's valid.
        Returns ('OK','') | ('SKIPPED', reason) | ('NODIALOG', reason).
        Catches units that are on hold OR already have an appointment."""
        d = self.dlg
        if not self.dialog_present():
            return "NODIALOG", "Add Appointment dialog is not open"
        d.input_after("Container Id").fill(container)
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(1200)   # BL auto-populates + validation
        err = self.read_error()
        if err is not None:
            status, reason = self.classify_error(err)
            # On validation, any error means we can't book now.
            return ("SKIPPED", reason) if status == "SKIPPED" else ("SKIPPED", reason)
        return "OK", ""

    def refresh_openings(self):
        """Re-poke the Requested Date field so the server reloads the
        Appointment Openings list. Cheap - no re-typing the container."""
        d = self.dlg
        db = d.input_after("Requested Date")
        db.click()
        db.fill(date.today().strftime(self.cfg.get("date_format", "%Y-%m-%d")))
        self.page.keyboard.press("Tab")
        # Wait for the server to reload openings. Lower = faster checks but
        # risk reading a stale/half-loaded list. Tune refresh_wait_ms.
        self.page.wait_for_timeout(int(self.cfg.get("refresh_wait_ms", 700)))

    def try_openings_once(self, container) -> tuple[str, str]:
        """One check of the openings list; book if a slot is free."""
        d = self.dlg
        if not self.dialog_present():
            return "NODIALOG", "dialog closed"

        self.refresh_openings()

        if self.dismiss("No Appointment Openings"):
            return "RETRY", "no openings"

        chosen = None
        import re
        for text in d.openings():
            m = re.search(r"Current Openings:\s*(\d+)", text)
            if m and int(m.group(1)) > 0:
                d.click_opening(text.split("(")[0].strip())
                chosen = text
                break
        if not chosen:
            self.page.keyboard.press("Escape")
            return "RETRY", "openings list empty"

        save = d.win.locator("text='Save'").first
        save.click()
        self.page.wait_for_timeout(1800)

        err = self.read_error()
        if err is not None:
            status, reason = self.classify_error(err)
            if status == "SKIPPED":
                return "SKIPPED", f"{reason} ({chosen})"
            return "RETRY", f"{reason} ({chosen})"
        if not self.dialog_present():
            return "BOOKED", f"{date.today()} {chosen}"
        return "BROKEN", f"no confirmation ({chosen})"

    def reopen_dialog(self, tower):
        """Click + to open a fresh Add Appointment (new tab) and set the
        fixed fields for the given tower, for the NEXT container."""
        plus = self.page.locator(
            "button.zebra-open-new-tab, .zebra-welcome-page-plus-button button"
        ).first
        if plus.count() == 0:
            # dialog may already be reusable; just re-assert fields
            if self.dialog_present():
                self.ensure_fixed_fields(tower)
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
        self.ensure_fixed_fields(tower)


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
        tower = str(cfg.get("tower", "")).strip()
        if tower not in VALID_TOWERS:
            log.error("config 'tower' must be one of %s - got '%s'",
                      sorted(VALID_TOWERS), tower)
            return
        log.info("Attached (SINGLE TOWER %s) | %s | %s",
                 tower, cfg["trucking_company"], cfg["transaction_type"])

        deadline = time.time() + float(cfg.get("max_hours", 6)) * 3600
        cycle = 0

        # SINGLE-TOWER STRATEGY:
        # Work down containers.csv in order on the one configured tower.
        # One quick attempt per container per pass, then loop back to the
        # top so cancellation slots get caught. Booked / held / already-
        # booked containers drop out via results.csv.
        while time.time() < deadline:
            pending = load_containers()
            if not pending:
                log.info("All containers processed. Done.")
                break

            cycle += 1
            log.info("--- Pass %d over %d container(s) on tower %s ---",
                     cycle, len(pending), tower)

            for container in pending:
                if time.time() >= deadline:
                    break

                try:
                    bot.reopen_dialog(tower)
                except Exception as e:
                    log.warning("Couldn't open dialog for %s: %s", container, e)
                    continue

                try:
                    st, why = bot.load_container(container)
                except Exception as e:
                    st, why = "BROKEN", str(e)
                if st == "SKIPPED":
                    log.warning("SKIP %s (%s)", container, why)
                    record(container, "SKIPPED", why)
                    continue
                if st in ("NODIALOG", "BROKEN"):
                    continue

                try:
                    status, detail = bot.try_openings_once(container)
                except Exception as e:
                    status, detail = "BROKEN", str(e)

                if status == "BOOKED":
                    log.info("BOOKED %s -> %s", container, detail)
                    record(container, "BOOKED", f"tower {tower} | {detail}")
                elif status in ("HOLD", "SKIPPED"):
                    log.warning("SKIP %s (%s)", container, detail)
                    record(container, "SKIPPED", detail)
                # RETRY / BROKEN: leave for next pass

                time.sleep(random.uniform(0.5, 1.5))

            wait = poll + random.uniform(0, max(2, poll * 0.3))
            log.info("Pass %d done. Next pass in %.0fs", cycle, wait)
            time.sleep(wait)

        log.info("Finished. See results.csv / bot.log")


if __name__ == "__main__":
    run()
