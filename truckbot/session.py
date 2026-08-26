"""Attach to the user's debug Chrome over CDP and manage the N4 tab.

Attach mode: logging in and navigating the ZK app proved fragile to
automate, so the user starts Chrome with --remote-debugging-port=9222,
logs in to N4 by hand and opens the Add Appointment screen; the bot
connects to that same Chrome and only drives the dialog.

The landing page is a "zebra welcome page" launcher; its "+" button
(class zebra-open-new-tab) opens the real Add Appointment screen in a
NEW BROWSER TAB, which we must switch to.

Reconnection: if the CDP link or the tab drops mid-run, reconnect()
re-attaches with backoff so a long run survives hiccups. It cannot
re-do the human part - if the N4 login itself expired, the bot pauses
and asks (via log/UI) for a fresh manual login.
"""

import logging
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .dialog import DIALOG_SELECTOR, PLUS_SELECTOR, N4Dialog

log = logging.getLogger("truckbot")


class SessionLost(Exception):
    """CDP/tab gone and automatic re-attach failed; needs the human."""


class N4Session:
    def __init__(self, cfg, debug_url: str | None = None):
        self.cfg = cfg
        self.debug_url = debug_url or cfg.debug_url
        self._pw = None
        self.browser = None
        self.page = None

    # --- lifecycle ---------------------------------------------------------
    def connect(self):
        if self._pw is None:
            self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.connect_over_cdp(self.debug_url)
        self.page = self._find_n4_page()
        if self.page is None:
            raise SessionLost(
                "No N4 tab found. Open N4 in the debug Chrome and log in, "
                "then start the bot again.")
        return self.page

    def close(self):
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self.browser = self.page = None

    def reconnect(self, attempts: int = 5) -> bool:
        """Re-attach with exponential backoff. True on success."""
        for i in range(attempts):
            wait = min(2 ** i, 30)
            log.warning("Session lost - reconnect attempt %d/%d in %ds",
                        i + 1, attempts, wait)
            time.sleep(wait)
            try:
                try:
                    if self._pw:
                        self._pw.stop()
                except Exception:
                    pass
                self._pw = None
                self.connect()
                log.info("Reconnected to Chrome / N4 tab.")
                return True
            except Exception as e:
                log.warning("Reconnect failed: %s", e)
        return False

    # --- tab handling --------------------------------------------------------
    def _find_n4_page(self):
        """Among open tabs, pick the one showing N4 (dialog or welcome +)."""
        for ctx in self.browser.contexts:
            for p in ctx.pages:
                try:
                    if p.locator(
                        ":text('Add Appointment'), "
                        "button.zebra-open-new-tab, "
                        ":text('Appointment Nbr')"
                    ).count() > 0:
                        return p
                except Exception:
                    continue
        for ctx in self.browser.contexts:
            if ctx.pages:
                return ctx.pages[0]
        return None

    def dialog(self) -> N4Dialog:
        return N4Dialog(self.page, self.cfg)

    def open_dialog(self, tower: str,
                    transaction_type: str | None = None) -> N4Dialog:
        """Click the zebra '+' to open a fresh Add Appointment (new tab)
        and set Gate/Zone for `tower` (and, when the run selected one,
        the Transaction Type). If no '+' is visible, reuse the
        already-open dialog and just re-assert the fields."""
        dlg = self.dialog()
        plus = self.page.locator(PLUS_SELECTOR).first
        if plus.count() == 0:
            if dlg.present():
                dlg.ensure_gate(tower)
                if transaction_type:
                    dlg.ensure_transaction_type(transaction_type)
                return dlg
            raise SessionLost(
                "Neither the Add Appointment dialog nor the '+' button is "
                "visible - open the appointment screen in N4 by hand.")
        ctx = self.page.context
        try:
            with ctx.expect_page(timeout=10000) as info:
                plus.click()
            self.page = info.value
            self.page.wait_for_load_state("networkidle")
        except PWTimeout:
            self.page.wait_for_timeout(800)
        self.page.wait_for_selector(DIALOG_SELECTOR, timeout=15000)
        dlg = self.dialog()
        dlg.ensure_gate(tower)
        if transaction_type:
            dlg.ensure_transaction_type(transaction_type)
        return dlg
