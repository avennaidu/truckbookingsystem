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

import json
import logging
import os
import platform
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from .dialog import DIALOG_SELECTOR, PLUS_SELECTOR, N4Dialog

log = logging.getLogger("truckbot")


class SessionLost(Exception):
    """CDP/tab gone and automatic re-attach failed; needs the human."""


CHROME_CANDIDATES = {
    "Windows": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ],
    "Darwin": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
    "Linux": ["/usr/bin/google-chrome", "/usr/bin/chromium",
              "/usr/bin/chromium-browser"],
}


def find_chrome(configured: str = "") -> str | None:
    if configured and os.path.exists(configured):
        return configured
    for c in CHROME_CANDIDATES.get(platform.system(), []):
        if os.path.exists(c):
            return c
    return None


def cdp_alive(debug_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{debug_url}/json/version",
                                    timeout=2) as r:
            json.load(r)
        return True
    except Exception:
        return False


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

    # --- auto-launch + auto-login -------------------------------------------
    def _port(self) -> str:
        return self.debug_url.rsplit(":", 1)[-1]

    def launch_chrome(self) -> bool:
        """Start a debug Chrome on our port, opening N4. True on success."""
        chrome = find_chrome(self.cfg.chrome_path)
        if chrome is None:
            log.error("Chrome not found - set chrome_path in config.json")
            return False
        port = self._port()
        base = self.cfg.user_data_dir or (
            os.path.join("C:\\", f"navis-chrome-{port}")
            if platform.system() == "Windows"
            else os.path.expanduser(f"~/.navis-chrome-{port}"))
        log.info("Launching Chrome (port %s)...", port)
        subprocess.Popen(
            [chrome, f"--remote-debugging-port={port}",
             f"--user-data-dir={base}", "--no-first-run",
             "--no-default-browser-check", self.cfg.url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(30):                    # wait up to ~15s for CDP
            if cdp_alive(self.debug_url):
                return True
            time.sleep(0.5)
        return False

    def login_if_needed(self) -> bool:
        """If the current page is the N4 login form and credentials are
        configured, log in (same flow as the legacy bot: first text input
        = username, password input, Enter). True if a login happened."""
        page = self.page
        try:
            pw_box = page.locator("input[type='password']")
            if pw_box.count() == 0 or not pw_box.first.is_visible():
                return False
        except Exception:
            return False
        if not (self.cfg.username and self.cfg.password):
            raise SessionLost(
                "N4 is showing the login page and no credentials are set - "
                "log in by hand or save username/password in the bot.")
        log.info("Logging in to N4 as %s...", self.cfg.username)
        page.fill("input[type='text']", self.cfg.username)
        page.fill("input[type='password']", self.cfg.password)
        page.keyboard.press("Enter")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(2000)
        try:
            if page.locator("input[type='password']").first.is_visible():
                raise SessionLost("N4 login failed - check the username "
                                  "and password saved in the bot.")
        except SessionLost:
            raise
        except Exception:
            pass
        return True

    def attach_or_launch(self):
        """Attach to the debug Chrome; if none is running and credentials
        + auto-launch are configured, start one, open N4 and log in."""
        try:
            self.connect()
        except SessionLost:
            raise
        except Exception:
            if not (self.cfg.auto_launch and self.cfg.username
                    and self.cfg.password):
                raise
            if not cdp_alive(self.debug_url) and not self.launch_chrome():
                raise SessionLost(
                    f"Could not start Chrome for {self.debug_url}")
            self.connect()
        # make sure the tab is actually on N4 and past the login page
        try:
            if self.page.locator(
                ":text('Add Appointment'), button.zebra-open-new-tab, "
                "input[type='password']"
            ).count() == 0:
                self.page.goto(self.cfg.url, wait_until="networkidle")
        except Exception:
            pass
        self.login_if_needed()
        return self.page

    def dialog(self) -> N4Dialog:
        return N4Dialog(self.page, self.cfg)

    def open_dialog(self, tower: str,
                    transaction_type: str | None = None) -> N4Dialog:
        """Click the zebra '+' to open a fresh Add Appointment (new tab)
        and set Gate/Zone for `tower` (and, when the run selected one,
        the Transaction Type). If no '+' is visible, reuse the
        already-open dialog and just re-assert the fields."""
        dlg = self.dialog()
        # N4 sometimes bounces to the login page mid-run (session expiry);
        # with saved credentials we can recover without the human.
        try:
            if self.login_if_needed():
                self.page.wait_for_selector(
                    PLUS_SELECTOR + ", " + DIALOG_SELECTOR, timeout=15000)
                dlg = self.dialog()
        except SessionLost:
            raise
        except Exception:
            pass
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
