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
# '15:00-15:59', '9:00 - 9:59' - the part that makes a row a real slot
SLOT_TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}")

# Every ZK message box: errors, the "No Appointment Openings Available"
# info box, and the "Edits will be lost" confirm. They are MODAL and they
# STACK - one left open blocks the form underneath, so they are always
# closed topmost-first (.last, not .first).
#
# Matching on TEXT (":has-text('failed')") was a trap: the outermost N4
# window contains the error text too, so it matched the whole application
# - which meant the error log filled up with the page's menu bar, and
# _close_box would have clicked that window's X and cancelled the form.
# Only ZK's real popup windows are listed here.
POPUP_SELECTOR = (".z-messagebox-window:visible, "
                  ".z-window-modal:visible, "
                  ".z-window-highlighted:visible, "
                  ".z-window-popup:visible")

# ...and the Add Appointment form itself is a modal window, so it has to
# be told apart from a message ABOUT it.
NOT_A_POPUP = ("Add Appointment", "Appointment Nbr", "Unit Information")
MAX_POPUP_CHARS = 1500      # a whole page is not a message box


def pick_opening(options: list[str]) -> tuple[str, str] | None:
    """First bookable slot in the Appointment Openings list.

    Operations rule: N4 only lists a time slot when that slot can be
    booked - a displayed slot IS an opening. So ANY row carrying a time
    range counts, and a row is skipped only when it says outright that
    it has none left ('Current Openings: 0'). Requiring the count to be
    present was wrong: N4 does not always render it in the row itself
    (it can sit in the row's tooltip), and the bot then sat idle in
    front of a list full of bookable slots.

    Returns (click_text, full_text) or None. Rows look like
    '17:00-17:59 (Current Openings: 7)' or just '15:00-15:59'; the click
    goes on the time part, so a count that ticks down between reading
    and clicking still matches.
    """
    for text in options:
        text = (text or "").strip()
        slot = SLOT_TIME_RE.search(text)
        if not slot:
            continue                    # header/placeholder row, not a slot
        count = OPENINGS_RE.search(text)
        if count and int(count.group(1)) <= 0:
            continue                    # says outright it is full
        return slot.group(0), text
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

    def close_combo(self, label) -> bool:
        """Shut an open dropdown WITHOUT pressing Escape.

        Escape does not stop at the popup: with the dropdown already
        closed it reaches the Add Appointment window, N4 reads it as
        Cancel, and puts up the modal "Edits will be lost - do you still
        want to cancel?" confirm. Its mask then swallows every later
        click for the full 30s timeout, which killed whole passes.
        """
        try:
            btn = self.combo_btn(label)
            if btn.count() > 0:
                btn.click(timeout=1500)
                self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))
                return True
        except Exception:
            pass
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
            self.close_combo(label)
            return False
        item = self.page.locator(
            f".z-comboitem:visible:has-text('{match}')").first
        if item.count() == 0:
            self.close_combo(label)
            return False
        item.click()
        self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))
        return True

    # --- error dialogs ----------------------------------------------------
    def _close_box(self, box) -> bool:
        """Close one message box, choosing the answer that loses nothing.

        'OK' dismisses an info/error box. A Yes/No confirm is always N4
        asking whether to throw the form away ("Edits will be lost - do
        you still want to cancel?"), so the answer is No: keep the form
        and let the caller carry on. Escape is never used - it is what
        raises that confirm in the first place.
        """
        for sel in ("text='OK'", "text='No'", "a.z-window-close"):
            try:
                el = box.locator(sel).first
                if el.count() > 0:
                    el.click(timeout=1500)
                    self.page.wait_for_timeout(int(self.cfg.error_wait_ms))
                    return True
            except Exception:
                continue
        return False

    def _popup_boxes(self) -> list:
        """Visible message boxes, excluding the form and the application."""
        out = []
        try:
            boxes = self.page.locator(POPUP_SELECTOR)
            for i in range(boxes.count()):
                box = boxes.nth(i)
                try:
                    text = (box.inner_text() or "").strip()
                except Exception:
                    continue
                if any(k in text for k in NOT_A_POPUP):
                    continue
                if len(text) > MAX_POPUP_CHARS:
                    continue
                out.append((box, text))
        except Exception:
            return []
        return out

    def close_all_popups(self, max_rounds: int = 8) -> list[str]:
        """Clear EVERY open message box, topmost first; return their texts.

        Popups stack: a run that leaves one open ends up with a pile of
        them covering the form, and every later click lands on the modal
        veil instead of the field it was aimed at.
        """
        texts: list[str] = []
        for _ in range(max_rounds):
            boxes = self._popup_boxes()
            if not boxes:
                break
            box, text = boxes[-1]         # topmost modal must go first
            if text:
                texts.append(text)
            if not self._close_box(box):
                break
        return texts

    def read_error(self) -> str | None:
        """If an error/info popup is showing, return its FULL text and
        dismiss it (along with any stacked behind it). Returns None when
        no popup. Text is verbatim - the classifier lowercases, the
        capture log wants the original."""
        texts = self.close_all_popups()
        if not texts:
            return None
        return "\n".join(t for t in texts if t)

    def dismiss(self, contains) -> bool:
        """Close every open popup; True if any mentioned `contains`."""
        texts = self.close_all_popups()
        return any(contains.lower() in (t or "").lower() for t in texts)

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
        for attempt in (1, 2):
            try:
                db.click(timeout=6000)
                break
            except Exception:
                if attempt == 2:
                    raise
                # something modal is over the form - clear it and retry
                # rather than burning the whole 30s default timeout
                self.close_all_popups()
        db.fill(date_str)
        self.page.keyboard.press("Tab")
        self.page.wait_for_timeout(int(self.cfg.refresh_wait_ms))

    def refresh_openings(self, date_str: str):
        """Make N4 re-issue the openings list for `date_str`, cheaply.

        This deliberately stays on the SAME day. Bouncing the date to the
        next day and back does force a reload, but each hop pops N4's
        modal "No Appointment Openings Available" box for that other day;
        they stacked up over a run and covered the form, leaving the
        openings dropdown unclickable and Save greyed out.
        """
        self.close_all_popups()
        self.set_date(date_str)

    def openings_available(self):
        """Cheap read of the Appointment Openings box: True (holds a slot
        already), False (the box is disabled, so there is nothing to
        pick), or None - meaning "cannot tell, go and read the dropdown".

        Colour is deliberately NOT used. N4 tints every REQUIRED field
        pink, whether or not it is filled - Gate/Zone shows pink holding
        a perfectly good '109 (ITZ 109)' - so pink says nothing about
        whether slots exist, and treating it as "nothing to book" would
        skip real openings.
        """
        try:
            box = self.combo_input("Appointment Openings")
            if box.count() == 0:
                return None
            if (box.input_value() or "").strip():
                return True                     # already holds a slot
            if not box.is_enabled():
                return False                    # nothing to pick from
        except Exception:
            return None
        return None                             # look in the dropdown

    def openings(self) -> list[str]:
        """Open the Appointment Openings dropdown and read all options."""
        if not self.open_combo("Appointment Openings"):
            return []
        self.page.wait_for_timeout(int(self.cfg.openings_wait_ms))
        items = self.page.locator(".z-comboitem:visible")
        out = []
        for i in range(items.count()):
            item = items.nth(i)
            try:
                txt = (item.inner_text() or "").strip()
            except Exception:
                continue
            if "Current Openings" not in txt:
                # N4 sometimes carries the count in the row's tooltip
                try:
                    tip = (item.get_attribute("title") or "").strip()
                except Exception:
                    tip = ""
                if tip and tip != txt:
                    txt = f"{txt} {tip}".strip()
            if txt:
                out.append(txt)
        return out

    def click_opening(self, click_text: str):
        self.page.locator(
            f".z-comboitem:visible:has-text('{click_text}')").first.click()
        self.page.wait_for_timeout(int(self.cfg.combo_pick_ms))

    def close_openings(self):
        self.close_combo("Appointment Openings")

    def opening_selected(self) -> str:
        """The slot currently in the Appointment Openings box ('' if none)."""
        try:
            return self.combo_input("Appointment Openings").input_value()
        except Exception:
            return ""

    def save(self) -> str | None:
        """Click Save; returns error text if a dialog popped, else None.

        Save is disabled until an Appointment Opening is selected, so a
        click with an empty box waits out its timeout and achieves
        nothing - the caller is told instead.
        """
        if not self.opening_selected():
            return "no appointment opening is selected"
        try:
            self.win.locator("text='Save'").first.click(timeout=5000)
        except Exception:
            return "the Save button did not accept the click"
        self.page.wait_for_timeout(int(self.cfg.save_wait_ms))
        return self.read_error()
