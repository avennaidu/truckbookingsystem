"""ZK Add Appointment dialog interaction (Playwright).

Everything here was verified against the live N4 system via DevTools:

- Element ids are RANDOM each session (e.g. rU4Qjk0) - never select by id.
- Combobox input:        input.z-combobox-input
- Combobox dropdown btn: a.z-combobox-button   ("button", not "btn")
- Combobox popup items:  .z-comboitem
- Text inputs:           input.z-textbox
- Date input:            input.z-datebox-input
- Fields are matched by proximity to their visible label with
  :near(:text('Label'), N). Radius is kept TIGHT (60px) for comboboxes:
  Line Operator sits directly below Gate/Zone and an early bug opened it
  by mistake with loose geometry matching.

The bot sets Gate/Zone, Container Id, Requested Date and Appointment
Openings, plus - when config asks for them - Transaction Type and
Trucking Company. A blank value in config means "leave that field
exactly as the user set it by hand". Line Operator is never touched.

Failing to SET a field is not the same as failing to book: a booking
made under the wrong Gate/Zone or the wrong Trucking Company is worse
than no booking, so every ensure_* returns a bool and the caller
collects the failures in `setup_errors` for the engine to refuse on.
"""

import re

from playwright.sync_api import TimeoutError as PWTimeout

DIALOG_SELECTOR = ".z-window:has-text('Add Appointment'), :text('Appointment Nbr')"
PLUS_SELECTOR = ("button.zebra-open-new-tab, "
                 ".zebra-welcome-page-plus-button button")

OPENINGS_RE = re.compile(r"Current Openings:\s*(\d+)")


def pick_opening(options: list[str]) -> tuple[str, str] | None:
    """First option with Current Openings > 0.

    Returns (click_text, full_text) or None. Options look like
    '17:00-17:59 (Current Openings: 7)'; we click by the time part so
    a count that ticks down between read and click still matches.
    """
    for text in options:
        m = OPENINGS_RE.search(text)
        if m and int(m.group(1)) > 0:
            return text.split("(")[0].strip(), text
    return None


class N4Dialog:
    """Drives one Add Appointment dialog on a Playwright page."""

    def __init__(self, page, cfg):
        self.page = page
        self.cfg = cfg
        # fields the caller asked for but could not set (see module docs)
        self.setup_errors: list[str] = []

    # --- locating -------------------------------------------------------
    @property
    def win(self):
        return self.page.locator(".z-window:has-text('Add Appointment')").last

    def present(self) -> bool:
        try:
            return self.page.locator(DIALOG_SELECTOR).count() > 0
        except Exception:
            return False

    def input_after(self, label):
        """Nearest input on the same row as the label (any ZK input kind)."""
        return self.win.locator(
            f"input.z-textbox:near(:text('{label}'), 80), "
            f"input.z-datebox-input:near(:text('{label}'), 80), "
            f"input.z-combobox-input:near(:text('{label}'), 80), "
            f"input:near(:text('{label}'), 80)"
        ).first

    def combo_input(self, label):
        return self.win.locator(
            f"input.z-combobox-input:near(:text('{label}'), 60)").first

    def combo_btn(self, label):
        return self.win.locator(
            f"a.z-combobox-button:near(:text('{label}'), 60)").first

    # --- combobox mechanics ----------------------------------------------
    def open_combo(self, label) -> bool:
        btn = self.combo_btn(label)
        if btn.count() > 0:
            try:
                btn.click(timeout=3000)
                return True
            except Exception:
                pass
        try:
            self.combo_input(label).click(timeout=3000)
            self.page.keyboard.press("Alt+ArrowDown")
            return True
        except Exception:
            return False

    def pick_combo(self, label, match, exact=False) -> bool:
        """Open the combobox next to `label` and choose an item.

        exact=True matches the FULL option text - required for Gate/Zone
        where '109', '109 REEFER' and '109A' all contain '109'.
        """
        if not self.open_combo(label):
            return False
        self.page.wait_for_timeout(int(self.cfg.combo_open_ms))
        if exact:
            items = self.page.locator(".z-comboitem:visible")
            for i in range(items.count()):
                if items.nth(i).inner_text().strip() == match:
                    items.nth(i).click()
                    self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))
                    return True
            self.page.keyboard.press("Escape")
            return False
        item = self.page.locator(
            f".z-comboitem:visible:has-text('{match}')").first
        if item.count() == 0:
            self.page.keyboard.press("Escape")
            return False
        item.click()
        self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))
        return True

    # --- error dialogs ----------------------------------------------------
    def read_error(self) -> str | None:
        """If an error/info popup is showing, return its FULL text and
        dismiss it. Returns None when no popup. Text is returned verbatim
        (classifier lowercases; capture log wants the original)."""
        box = self.page.locator(
            ".z-window:visible:has-text('Error'), "
            ".z-messagebox-window:visible, "
            ".z-window:visible:has-text('failed')"
        )
        if box.count() == 0:
            return None
        try:
            txt = box.first.inner_text()
        except Exception:
            txt = ""
        try:
            box.first.locator("text='OK'").first.click(timeout=2000)
        except Exception:
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(int(self.cfg.error_wait_ms))
        return txt

    def dismiss(self, contains) -> bool:
        box = self.page.locator(f".z-window:visible:has-text('{contains}')")
        if box.count() == 0:
            return False
        try:
            box.first.locator("text='OK'").first.click(timeout=2000)
        except Exception:
            self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(int(self.cfg.error_wait_ms))
        return True

    # --- form steps ---------------------------------------------------------
    def gate_zone_value(self) -> str:
        try:
            return self.combo_input("Gate/Zone").input_value()
        except Exception:
            return ""

    def ensure_gate(self, tower: str) -> bool:
        """Set Gate/Zone to the tower's exact label; no-op if already set."""
        want = self.cfg.gate_label(tower)
        if want and want in self.gate_zone_value():
            return True
        return self.pick_combo("Gate/Zone", want, exact=True)

    def transaction_type_value(self) -> str:
        try:
            return self.combo_input("Transaction Type").input_value()
        except Exception:
            return ""

    def ensure_transaction_type(self, value: str) -> bool:
        """Set Transaction Type (e.g. 'Pick Up Import', 'Drop Off Export').
        Only called when the run explicitly selects a type - by default the
        bot leaves this combobox exactly as the user set it by hand.
        Tries an exact option match first, then substring."""
        if value and value.lower() in self.transaction_type_value().lower():
            return True
        if self.pick_combo("Transaction Type", value, exact=True):
            return True
        return self.pick_combo("Transaction Type", value, exact=False)

    def trucking_company_value(self) -> str:
        try:
            return self.combo_input("Trucking Company").input_value()
        except Exception:
            return ""

    def ensure_trucking_company(self, value: str) -> bool:
        """Set Trucking Company (e.g. 'AVEMEL LOG').

        Operations run every booking under one company, so this is set
        from config rather than by hand. An exact option match is tried
        first, then a substring one - N4 spells the company out in full
        in some dropdowns ('AVEMEL LOG...'), so 'AVEMEL LOG' matches
        either wording.
        """
        if value and value.lower() in self.trucking_company_value().lower():
            return True
        if self.pick_combo("Trucking Company", value, exact=True):
            return True
        return self.pick_combo("Trucking Company", value, exact=False)

    def enter_container(self, container: str) -> str | None:
        """Type the container and blur (Tab) so N4 validates it and
        auto-populates GIR/BL. Returns the error text if N4 objects."""
        self.input_after("Container Id").fill(container)
        self.page.keyboard.press("Tab")
        # BL auto-populate + validation
        self.page.wait_for_timeout(int(self.cfg.validate_wait_ms))
        return self.read_error()

    def set_date(self, date_str: str):
        """(Re-)set Requested Date; this pokes the server into reloading
        the Appointment Openings list - cheap, no container re-typing."""
        db = self.input_after("Requested Date")
        db.click()
        db.fill(date_str)
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(int(self.cfg.refresh_wait_ms))

    def refresh_openings(self, date_str: str, other_str: str):
        """Make N4 re-issue the openings list for `date_str`, cheaply.

        Re-filling the datebox with the SAME value does not reliably fire
        ZK's onChange, so the date is bounced to `other_str` and back:
        two changes the server is certain to act on, and still an order of
        magnitude cheaper than closing and rebuilding the whole dialog.
        """
        self.set_date(other_str)
        self.dismiss("No Appointment Openings")
        self.set_date(date_str)

    def openings(self) -> list[str]:
        """Open the Appointment Openings dropdown and read all options."""
        if not self.open_combo("Appointment Openings"):
            return []
        self.page.wait_for_timeout(int(self.cfg.openings_wait_ms))
        items = self.page.locator(".z-comboitem:visible")
        return [items.nth(i).inner_text().strip()
                for i in range(items.count())]

    def click_opening(self, click_text: str):
        self.page.locator(
            f".z-comboitem:visible:has-text('{click_text}')").first.click()
        self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))

    def close_openings(self):
        self.page.keyboard.press("Escape")

    def save(self) -> str | None:
        """Click Save; returns error text if a dialog popped, else None."""
        self.win.locator("text='Save'").first.click()
        self.page.wait_for_timeout(int(self.cfg.save_wait_ms))
        return self.read_error()
