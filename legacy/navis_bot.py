"""
Navis N4 Appointment Bot - one instance per tower
==================================================
Workflow per container (matches the Add Appointment dialog):
  1. Open Add Appointment
  2. Gate/Zone = configured tower, Transaction Type = Pick Up Import,
     Trucking Company = AVEMEL (all preset in config)
  3. Fill Container Id + GIR/BL Nbr from containers.csv
  4. Set Requested Date, open Appointment Openings dropdown,
     pick the first opening with Current Openings > 0, Save
  5. If "No Appointment Openings Available" -> OK, try next date
  6. If Application Error (e.g. !IMPORT RELEASE hold) -> OK,
     record as HOLD and move to next container (retry won't help)
  7. On success, record BOOKED and move to next container
Keeps cycling the pending list until all are booked or max_hours reached.

Setup:
    pip install playwright
    playwright install chromium
    python navis_bot.py
"""

import csv
import json
import logging
import random
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

CONFIG_FILE = Path("config.json")
CONTAINERS_FILE = Path("containers.csv")
RESULTS_FILE = Path("results.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger("navis-bot")

TERMINAL_STATUSES = {"BOOKED", "SKIPPED"}  # HOLD is retried next run, not this one


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        return json.load(f)


def already_done() -> set:
    done = set()
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE) as f:
            for row in csv.DictReader(f):
                if row["status"] in TERMINAL_STATUSES:
                    done.add(row["container"])
    return done


def load_containers() -> list[dict]:
    done = already_done()
    out = []
    with open(CONTAINERS_FILE) as f:
        for row in csv.DictReader(f):
            c = row.get("container", "").strip().upper()
            if c and c not in done:
                out.append({"container": c})
    return out


def record(container: str, status: str, detail: str = ""):
    new = not RESULTS_FILE.exists()
    with open(RESULTS_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "container", "status", "detail"])
        w.writerow([datetime.now().isoformat(), container, status, detail])


# --------------------------------------------------------------------------
# ZK helpers. ZK ids are random per session, so everything anchors on the
# visible labels ("Container Id:", "Gate/Zone:", ...) which are stable.
# --------------------------------------------------------------------------
class Dialog:
    """Wraps the Add Appointment dialog."""

    def __init__(self, page):
        self.page = page

    # the dialog window itself (ZK modal with caption "Add Appointment")
    @property
    def win(self):
        return self.page.locator(".z-window:has-text('Add Appointment')").last

    def input_after(self, label: str):
        """Text input immediately to the right of a label."""
        return self.win.locator(
            f"input:right-of(:text('{label}'))"
        ).first

    def combo_after(self, label: str):
        """ZK combobox input to the right of a label."""
        return self.win.locator(
            f".z-combobox-input:right-of(:text('{label}')), "
            f"input.z-combobox-inp:right-of(:text('{label}'))"
        ).first

    def combo_button_after(self, label: str):
        """The dropdown arrow button of the combobox next to a label."""
        return self.win.locator(
            f".z-combobox-btn:right-of(:text('{label}')), "
            f"i.z-combobox-icon:right-of(:text('{label}'))"
        ).first

    def open_combo_and_pick(self, label: str, match_text: str) -> bool:
        """Open combobox popup, click item containing match_text."""
        self.combo_button_after(label).click()
        self.page.wait_for_timeout(700)
        item = self.page.locator(
            f".z-comboitem:visible:has-text('{match_text}')"
        ).first
        if item.count() == 0:
            self.page.keyboard.press("Escape")
            return False
        item.click()
        self.page.wait_for_timeout(500)
        return True

    def list_combo_items(self, label: str) -> list[str]:
        """Open combobox and return the visible item texts (popup stays open)."""
        self.combo_button_after(label).click()
        self.page.wait_for_timeout(900)
        items = self.page.locator(".z-comboitem:visible")
        return [items.nth(i).inner_text().strip() for i in range(items.count())]

    def click_visible_item(self, text_contains: str):
        self.page.locator(
            f".z-comboitem:visible:has-text('{text_contains}')"
        ).first.click()
        self.page.wait_for_timeout(400)


class NavisBot:
    def __init__(self, cfg, page):
        self.cfg = cfg
        self.page = page
        self.dlg = Dialog(page)

    # ------------------------------------------------------------- session
    def login(self):
        log.info("Logging in...")
        self.page.goto(self.cfg["url"], wait_until="networkidle")
        self.page.fill("input[type='text']", self.cfg["username"])
        self.page.fill("input[type='password']", self.cfg["password"])
        self.page.keyboard.press("Enter")
        self.page.wait_for_load_state("networkidle")
        self.page.wait_for_timeout(2000)

    def ensure_session(self) -> bool:
        """Returns True if a re-login happened (open dialogs are lost)."""
        expired = self.page.locator(
            ".z-messagebox-window:has-text('session'), "
            ".z-window:has-text('Session')"
        )
        if expired.count() > 0:
            log.warning("Session expired - re-login")
            self.login()
            return True
        return False

    # -------------------------------------------------------------- dialog
    def open_add_appointment(self):
        """The N4 landing page is a 'zebra welcome page' whose '+' button
        (class zebra-open-new-tab) opens the real appointment screen in a
        NEW TAB. So: click the +, switch to that new tab, then continue.
        The id (h23Sf1) is random per session; the classes are stable."""
        plus = self.page.locator(
            "button.zebra-open-new-tab, "
            ".zebra-welcome-page-plus-button button, "
            ".zebra-welcome-page-plus-button"
        ).first
        if plus.count() == 0:
            raise RuntimeError(
                "Could not find the zebra '+' button - selector needs "
                "updating (send a screenshot of the welcome page)."
            )

        # The click spawns a new browser tab; capture and switch to it.
        ctx = self.page.context
        try:
            with ctx.expect_page(timeout=10000) as new_tab_info:
                plus.click()
            new_page = new_tab_info.value
            new_page.wait_for_load_state("networkidle")
            self.page = new_page          # all further actions on new tab
            self.dlg = Dialog(new_page)
            log.info("Switched to new appointment tab.")
        except PWTimeout:
            # No new tab - maybe navigated in place or opened a menu.
            self.page.wait_for_timeout(1000)

        # Some builds then show an "Appointments" option to click.
        appt = self.page.locator(
            ".z-menuitem:visible:has-text('Appointment'), "
            ".z-listitem:visible:has-text('Appointment'), "
            "a:visible:has-text('Appointment'), "
            "td:visible:has-text('Appointment'), "
            "span:visible:has-text('Appointment')"
        )
        if appt.count() > 0:
            try:
                appt.first.click(timeout=4000)
            except Exception:
                pass

        # Confirm the Add Appointment screen is present. Match on several
        # stable labels since it may be a window OR a full-page form.
        self.page.wait_for_selector(
            ".z-window:has-text('Add Appointment'), "
            ":text('Add Appointment'), :text('Appointment Nbr'), "
            ":text('Gate/Zone')",
            timeout=15000
        )
        self.page.wait_for_timeout(800)

    def close_extra_tabs(self):
        """After a booking, close the appointment tab and return to the
        welcome tab so the next container starts clean."""
        ctx = self.page.context
        pages = ctx.pages
        if len(pages) > 1:
            # keep the first (welcome) page, close the rest
            for p in pages[1:]:
                try:
                    p.close()
                except Exception:
                    pass
            self.page = pages[0]
            self.dlg = Dialog(self.page)

    def close_dialog_if_open(self):
        x = self.page.locator(
            ".z-window:has-text('Add Appointment') .z-window-icon-close, "
            ".z-window:has-text('Add Appointment') >> text='Cancel'"
        )
        if x.count() > 0:
            try:
                x.first.click()
                self.page.wait_for_timeout(500)
            except Exception:
                pass

    def preset_fixed_fields(self):
        """Gate/Zone (tower), Transaction Type, Trucking Company."""
        d = self.dlg
        assert d.open_combo_and_pick("Gate/Zone", self.cfg["tower"]), \
            f"Tower {self.cfg['tower']} not found in Gate/Zone list"
        d.open_combo_and_pick("Transaction Type", self.cfg["transaction_type"])
        d.open_combo_and_pick("Trucking Company", self.cfg["trucking_company"])

    def set_requested_date(self, d: date):
        box = self.dlg.input_after("Requested Date")
        box.click()
        box.fill(d.strftime(self.cfg.get("date_format", "%Y-%m-%d")))
        self.page.keyboard.press("Tab")   # blur triggers ZK onChange
        self.page.wait_for_timeout(1200)  # server refreshes openings

    def dismiss_info_dialog(self, contains: str) -> bool:
        """If a message box containing `contains` is showing, click OK."""
        box = self.page.locator(f".z-window:visible:has-text('{contains}')")
        if box.count() > 0:
            ok = box.locator("text='OK'").first
            try:
                ok.click()
            except Exception:
                self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(500)
            return True
        return False

    def pick_first_opening(self) -> str | None:
        """Open the Appointment Openings dropdown and pick the first slot
        with Current Openings > 0. Returns the slot text or None."""
        items = self.dlg.list_combo_items("Appointment Openings")
        for text in items:
            m = re.search(r"Current Openings:\s*(\d+)", text)
            if m and int(m.group(1)) > 0:
                self.dlg.click_visible_item(text.split("(")[0].strip())
                return text
        self.page.keyboard.press("Escape")
        return None

    # --------------------------------------------------------------- book
    def prepare_dialog(self, container: str) -> str:
        """Open dialog and fill everything except the slot.
        The GIR/BL number auto-populates when the container is entered.
        Returns 'READY' or 'HOLD'."""
        self.open_add_appointment()
        d = self.dlg
        self.preset_fixed_fields()

        d.input_after("Container Id").fill(container)
        self.page.keyboard.press("Tab")   # blur triggers unit validation
        self.page.wait_for_timeout(1500)  # wait for BL auto-population

        # Eligibility error (e.g. !IMPORT RELEASE) pops on unit validation
        if self.dismiss_info_dialog("Application Error"):
            self.close_dialog_if_open()
            return "HOLD"
        return "READY"

    # ------------------------------------------------------------- dates
    def in_multiday_window(self) -> bool:
        """True only during the first review of the day (around 6am),
        when hunting future dates is allowed."""
        start = self.cfg.get("multiday_window_start", "06:00")
        end = self.cfg.get("multiday_window_end", "07:00")
        now = datetime.now().strftime("%H:%M")
        return start <= now < end

    def allowed_dates(self) -> list[date]:
        """Today only - except during the 6am review window, when the bot
        may also hunt the next `days_ahead` days."""
        dates = [date.today()]
        if self.in_multiday_window():
            extra = int(self.cfg.get("days_ahead", 3))
            dates += [date.today() + timedelta(days=i)
                      for i in range(1, extra + 1)]
        return dates

    def try_slot_once(self, container: str) -> tuple[str, str]:
        """One fast pass with the dialog ALREADY open and filled.
        Normally Requested Date = TODAY only; during the 6am review
        window it also walks future dates. Returns (status, detail):
        BOOKED / HOLD / RETRY (dialog left open) / BROKEN (dialog lost)."""
        dates = self.allowed_dates()
        for target in dates:
            self.set_requested_date(target)

            if self.dismiss_info_dialog("No Appointment Openings"):
                continue

            slot = self.pick_first_opening()
            if not slot:
                continue

            self.dlg.win.locator("text='Save'").first.click()
            self.page.wait_for_timeout(2000)

            if self.dismiss_info_dialog("Application Error"):
                self.close_dialog_if_open()
                return "HOLD", f"save rejected on {target} ({slot})"
            if self.dismiss_info_dialog("No Appointment Openings"):
                continue  # slot vanished between listing and saving

            if self.page.locator(
                ".z-window:visible:has-text('Add Appointment')"
            ).count() == 0:
                return "BOOKED", f"{target} {slot}"
            # Saved but dialog still open with no error - state unknown,
            # rebuild cleanly rather than guess.
            self.close_dialog_if_open()
            return "BROKEN", f"save gave no confirmation ({target} {slot})"

        scope = f"{len(dates)} day(s)" if len(dates) > 1 else f"today ({dates[0]})"
        return "RETRY", f"no openings for {scope}"

    def book_until_success(self, container: str,
                           deadline: float, poll: int) -> tuple[str, str]:
        """Camp on ONE container. The form is filled once; each retry only
        re-triggers the date field (which makes the server refresh the
        openings list) - no dialog teardown between attempts. The dialog is
        rebuilt only after errors, session loss, or every rebuild_every
        attempts as a staleness guard."""
        rebuild_every = int(self.cfg.get("rebuild_every", 40))
        attempt = 0
        needs_prepare = True

        while time.time() < deadline:
            attempt += 1
            if self.ensure_session():
                needs_prepare = True

            if needs_prepare or attempt % rebuild_every == 0:
                self.close_dialog_if_open()
                self.close_extra_tabs()   # back to welcome tab before re-open
                state = self.prepare_dialog(container)
                if state == "HOLD":
                    self.close_extra_tabs()
                    return "HOLD", "application error on unit (e.g. not released)"
                needs_prepare = False

            try:
                status, detail = self.try_slot_once(container)
            except Exception as e:
                log.warning("Attempt %d error: %s - rebuilding dialog", attempt, e)
                self.close_dialog_if_open()
                self.close_extra_tabs()
                needs_prepare = True
                time.sleep(3)
                continue

            if status in ("BOOKED", "HOLD"):
                self.close_extra_tabs()   # tidy up before next container
                return status, detail
            if status == "BROKEN":
                needs_prepare = True

            wait = poll + random.uniform(0, max(2, poll * 0.3))
            if attempt % 10 == 0:
                log.info("%s: attempt %d, still no slot. Next try in %.0fs",
                         container, attempt, wait)
            time.sleep(wait)

        self.close_dialog_if_open()
        return "RETRY", "time limit reached"


def run():
    cfg = load_config()
    poll = int(cfg.get("poll_seconds", 90))
    deadline = time.time() + float(cfg.get("max_hours", 6)) * 3600
    log.info("Tower %s | trucking %s | %s",
             cfg["tower"], cfg["trucking_company"], cfg["transaction_type"])

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=cfg.get("headless", False))
        page = browser.new_page()
        bot = NavisBot(cfg, page)
        bot.login()

        while time.time() < deadline:
            pending = load_containers()
            if not pending:
                log.info("All containers processed. Done.")
                break

            # STRICT SEQUENTIAL: camp on the first pending container until
            # it is BOOKED. book_until_success keeps the dialog open and
            # only re-polls the openings on each attempt, so retries are
            # fast. HOLD (e.g. !IMPORT RELEASE) exits early because no
            # retry can fix an unreleased unit.
            row = pending[0]
            c = row["container"]
            log.info("Working container %s (%d remaining after it)",
                     c, len(pending) - 1)

            status, detail = bot.book_until_success(c, deadline, poll)
            log.info("%s -> %s (%s)", c, status, detail)

            if status == "BOOKED":
                record(c, "BOOKED", detail)
                log.info("Moving to next container.")
                time.sleep(random.uniform(2, 4))
                continue

            if status == "HOLD":
                # Problem with the container itself (e.g. !IMPORT RELEASE
                # hold) - booking is impossible until it's resolved, so
                # record it and move straight to the next container.
                record(c, "SKIPPED", detail)
                log.warning("%s has a unit problem (%s) - skipped. Resolve "
                            "it, then delete its row in results.csv to "
                            "re-queue it.", c, detail)
                continue

            # RETRY here means the overall time limit was hit
            log.info("Stopping on %s: %s", c, detail)

        browser.close()
    log.info("Finished. See results.csv / bot.log")


if __name__ == "__main__":
    run()
