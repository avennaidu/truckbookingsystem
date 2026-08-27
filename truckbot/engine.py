"""Booking engine: retry loop, tower rotation, skip logic, resume.

Decoupled from Playwright: it drives any `session` object exposing
open_dialog(tower) -> dialog and reconnect(), where the dialog offers
the N4Dialog interface (enter_container / set_date / dismiss /
openings / click_opening / close_openings / save / present). Tests use
a fake; production uses truckbot.session.N4Session.

Strategies (both from the proven prototypes):
- mode="all":    one pass visits each tower once in tower_order and
                 attempts that tower's NEXT pending container, so no
                 single tower's long list blocks the others.
- mode="single": camp on one tower and work the whole list top-to-bottom
                 each pass.

Slot releases happen at scheduled reviews AND randomly (cancellations),
so the engine polls continuously; an empty openings list is the normal
between-release state, not an error.
"""

import logging
import random
import threading
import time
from datetime import date, timedelta

from . import VALID_TOWERS
from .containers import (ErrorCapture, ResultsStore, group_by_tower,
                         load_containers, remove_container)
from .dialog import pick_opening
from .errors import RETRY, SKIP, classify_error

log = logging.getLogger("truckbot")

# A container whose attempts keep dying on the SAME unrecognised error
# is eventually skipped so it can't wedge the run forever.
MAX_SAME_UNKNOWN_ERRORS = 5


class Engine:
    def __init__(self, cfg, session, results: ResultsStore | None = None,
                 errors: ErrorCapture | None = None,
                 on_event=None, stop_event: threading.Event | None = None,
                 sleeper=time.sleep):
        self.cfg = cfg
        self.session = session
        self.results = results or ResultsStore(cfg.results_file)
        self.errors = errors or ErrorCapture(cfg.errors_file)
        self.on_event = on_event or (lambda kind, **data: None)
        self.stop_event = stop_event or threading.Event()
        self.sleep = sleeper
        self._unknown_counts: dict[str, int] = {}
        # None = leave that field exactly as hand-set in N4
        self.transaction_type: str | None = None
        self.trucking_company: str | None = None

    # --- events -----------------------------------------------------------
    def emit(self, kind, **data):
        try:
            self.on_event(kind, **data)
        except Exception:
            log.exception("event handler failed")

    def _handle_error_text(self, container, stage, text):
        """Classify + capture a dialog's text. Returns (SKIP|RETRY, reason)."""
        kind, reason = classify_error(text)
        self.errors.capture(container, stage, text, f"{kind}:{reason}")
        if kind == RETRY and reason not in ("no openings",):
            n = self._unknown_counts.get(container, 0) + 1
            self._unknown_counts[container] = n
            if n >= MAX_SAME_UNKNOWN_ERRORS:
                return SKIP, f"gave up after {n} repeated errors: {reason}"
        return kind, reason

    # --- one booking attempt -------------------------------------------------
    def attempt(self, container: str, tower: str) -> tuple[str, str]:
        """Try to book `container` on `tower` once.
        Returns (BOOKED|SKIPPED|RETRY|BROKEN, detail)."""
        dlg = self.session.open_dialog(
            tower, transaction_type=self.transaction_type,
            trucking_company=self.trucking_company)

        # A field that would not take (wrong gate, wrong trucking company)
        # must never reach Save - retry next pass instead of booking a slot
        # under the wrong settings.
        # Clear anything left over the form before touching it: N4's modal
        # boxes stack, and one still open makes every click land on the
        # veil instead of the field.
        if hasattr(dlg, "close_all_popups"):
            dlg.close_all_popups()

        unset = getattr(dlg, "setup_errors", None)
        if unset:
            return "RETRY", ("could not set " + "; ".join(unset)
                             + " - set it by hand in N4, or clear it in "
                               "config.json to leave it alone")

        err = dlg.enter_container(container)
        if err is not None:
            kind, reason = self._handle_error_text(container, "validate", err)
            # Transient errors at validation are retried next pass - the
            # prototype permanently skipped here and lost containers to
            # one-off server errors.
            return ("SKIPPED", reason) if kind == SKIP else ("RETRY", reason)

        days = max(0, int(self.cfg.days_ahead))
        probes = max(1, int(self.cfg.fast_retries))
        for offset in range(days + 1):
            if self.stop_event.is_set():
                return "RETRY", "stopped"
            day = date.today() + timedelta(days=offset)
            day_str = day.strftime(self.cfg.date_format)
            dlg.set_date(day_str)

            # Openings vanish within seconds of a release, and rebuilding
            # the dialog costs seconds - so look again several times while
            # this form is still open and warm before moving on.
            for probe in range(probes):
                if self.stop_event.is_set():
                    return "RETRY", "stopped"

                # Cheap pre-check: the openings box shows its own state
                # (pink = nothing yet, white/grey = slots are out). Skip
                # the dropdown read while it says nothing - but never on
                # the last look, so a misread cannot cost a slot.
                if (hasattr(dlg, "openings_available")
                        and probe + 1 < probes
                        and dlg.openings_available() is False):
                    dlg.refresh_openings(day_str)
                    continue

                if dlg.dismiss("No Appointment Openings"):
                    choice = None           # normal between-release state
                else:
                    choice = pick_opening(dlg.openings())
                    if choice is None:
                        dlg.close_openings()

                if choice is None:
                    if probe + 1 < probes:
                        dlg.refresh_openings(day_str)
                    continue

                click_text, full_text = choice
                dlg.click_opening(click_text)

                # The slot can be taken between reading the list and
                # clicking it; Save stays disabled when nothing is
                # selected, so look again rather than pressing a dead
                # button.
                if hasattr(dlg, "opening_selected")                         and not dlg.opening_selected():
                    if probe + 1 < probes:
                        dlg.refresh_openings(day_str)
                    continue

                err = dlg.save()
                if err is not None:
                    kind, reason = self._handle_error_text(
                        container, "save", err)
                    status = "SKIPPED" if kind == SKIP else "RETRY"
                    return status, f"{reason} ({full_text})"
                if not dlg.present():
                    return "BOOKED", f"{day} {full_text} (tower {tower})"
                return "BROKEN", f"no confirmation ({full_text})"

        return "RETRY", "no openings"

    # --- the run loop -------------------------------------------------------
    def run(self, mode: str = "all", tower: str | None = None,
            transaction_type: str | None = None,
            trucking_company: str | None = None):
        """Loop passes until list done, deadline hit, or stop requested."""
        self.transaction_type = transaction_type
        self.trucking_company = trucking_company
        cfg = self.cfg
        deadline = time.time() + float(cfg.max_hours) * 3600
        cycle = 0
        only = tower if mode == "single" else None

        self.emit("started", mode=mode, tower=tower,
                  transaction=transaction_type or "as set in N4",
                  trucking=trucking_company or "as set in N4")
        while not self.stop_event.is_set() and time.time() < deadline:
            pending = load_containers(cfg.containers_file,
                                      self.results.done_set(), only)
            if not pending:
                self.emit("finished", reason="all containers processed",
                          summary=self.results.summary())
                log.info("All containers processed. Done.")
                return

            buckets = group_by_tower(pending, cfg.tower_order)
            cycle += 1
            self.emit("pass", n=cycle,
                      pending={t: len(cs) for t, cs in buckets.items()})
            log.info("--- Pass %d | pending by tower: %s ---", cycle,
                     {t: len(cs) for t, cs in buckets.items()})

            for t, containers in buckets.items():
                if self.stop_event.is_set() or time.time() >= deadline:
                    break
                if t not in VALID_TOWERS:
                    for c in containers:
                        log.error("SKIP %s - invalid tower '%s'", c, t)
                        self.results.record(c, "SKIPPED", f"invalid tower {t}")
                        self.emit("skipped", container=c, tower=t,
                                  detail=f"invalid tower {t}")
                    continue
                # all-towers: ONE container per tower per pass (rapid
                # rotation); single: the whole list every pass.
                batch = containers if mode == "single" else containers[:1]
                for container in batch:
                    if self.stop_event.is_set() or time.time() >= deadline:
                        break
                    self._attempt_and_record(container, t)
                    gap = max(0.0, int(self.cfg.attempt_gap_ms) / 1000.0)
                    self.sleep(gap + random.uniform(0, gap))

            if self.stop_event.is_set() or time.time() >= deadline:
                break
            wait = cfg.poll_seconds + random.uniform(0, max(2, cfg.poll_seconds * 0.3))
            self.emit("waiting", seconds=round(wait))
            log.info("Pass %d done. Next pass in %.0fs", cycle, wait)
            self._interruptible_sleep(wait)

        reason = "stopped by user" if self.stop_event.is_set() else "time limit reached"
        self.emit("finished", reason=reason, summary=self.results.summary())
        log.info("Finished (%s). Summary: %s", reason, self.results.summary())

    def _attempt_and_record(self, container: str, tower: str):
        self.emit("attempt", container=container, tower=tower)
        try:
            status, detail = self.attempt(container, tower)
        except Exception as e:
            log.warning("Attempt %s (tower %s) blew up: %s", container, tower, e)
            self.emit("error", container=container, tower=tower, detail=str(e))
            if not self._recover():
                self.stop_event.set()
            return

        if status == "BOOKED":
            log.info("BOOKED %s -> %s", container, detail)
            self.results.record(container, "BOOKED", detail)
            # operations rule: a booked container comes OFF the working
            # list immediately (results.csv keeps the permanent record)
            try:
                remove_container(self.cfg.containers_file, container)
            except Exception:
                log.exception("could not remove %s from %s", container,
                              self.cfg.containers_file)
            self.emit("booked", container=container, tower=tower, detail=detail)
        elif status == "SKIPPED":
            log.warning("SKIP %s (%s)", container, detail)
            self.results.record(container, "SKIPPED", detail)
            self.emit("skipped", container=container, tower=tower, detail=detail)
        else:   # RETRY / BROKEN - leave pending for the next pass
            log.info("Tower %s: %s not booked (%s)", tower, container, detail)
            self.emit("retry", container=container, tower=tower,
                      status=status, detail=detail)

    def _recover(self) -> bool:
        """Try to re-attach the browser session after an exception."""
        self.emit("reconnecting")
        try:
            ok = self.session.reconnect()
        except Exception:
            ok = False
        if not ok:
            log.error("Could not re-attach to Chrome. Log in to N4 in the "
                      "debug Chrome and start the bot again.")
            self.emit("fatal", detail="lost browser session - manual login needed")
        return ok

    def _interruptible_sleep(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end and not self.stop_event.is_set():
            self.sleep(min(1.0, end - time.time()))
