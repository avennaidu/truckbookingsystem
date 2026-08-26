"""Engine logic tests using a scripted fake dialog - no browser needed."""

import threading

import pytest

from truckbot.config import Config
from truckbot.containers import ErrorCapture, ResultsStore
from truckbot.engine import MAX_SAME_UNKNOWN_ERRORS, Engine


class FakeDialog:
    """Scripted per-container behaviour.

    script[container] = dict with optional keys:
      validate_error: str        -> error dialog on container entry
      openings: list[str]        -> options shown
      no_openings_popup: bool    -> the 'No Appointment Openings' box
      save_error: str            -> error dialog after Save
      book_ok: bool              -> dialog closes after Save (booked)
    """

    def __init__(self, script):
        self.script = script
        self.container = None
        self._present = True
        self.saves = []

    def _s(self):
        return self.script.get(self.container, {})

    def enter_container(self, c):
        self.container = c
        self._present = True
        return self._s().get("validate_error")

    def set_date(self, d):
        pass

    def dismiss(self, contains):
        return self._s().get("no_openings_popup", False)

    def openings(self):
        return self._s().get("openings", [])

    def click_opening(self, t):
        pass

    def close_openings(self):
        pass

    def save(self):
        self.saves.append(self.container)
        err = self._s().get("save_error")
        if err:
            return err
        if self._s().get("book_ok"):
            self._present = False
        return None

    def present(self):
        return self._present


class FakeSession:
    def __init__(self, dialog):
        self._dialog = dialog
        self.opened = []            # (container-agnostic) tower sequence
        self.reconnects = 0

    def open_dialog(self, tower, transaction_type=None):
        self.opened.append(tower)
        self.transaction_type = transaction_type
        return self._dialog

    def reconnect(self):
        self.reconnects += 1
        return True


SLOT = "17:00-17:59 (Current Openings: 7)"
NO_SLOT = "09:00-09:59 (Current Openings: 0)"


def make_engine(tmp_path, script, containers_csv, **cfg_kw):
    cfg = Config(
        containers_file=str(tmp_path / "containers.csv"),
        results_file=str(tmp_path / "results.csv"),
        errors_file=str(tmp_path / "errors.csv"),
        poll_seconds=0, max_hours=0.001, **cfg_kw)
    (tmp_path / "containers.csv").write_text(containers_csv)
    dialog = FakeDialog(script)
    session = FakeSession(dialog)
    events = []
    eng = Engine(cfg, session,
                 results=ResultsStore(cfg.results_file),
                 errors=ErrorCapture(cfg.errors_file),
                 on_event=lambda kind, **d: events.append((kind, d)),
                 sleeper=lambda s: None)
    return eng, session, dialog, events


def test_books_first_open_slot(tmp_path):
    eng, session, dlg, events = make_engine(
        tmp_path, {"AAAA1111111": {"openings": [NO_SLOT, SLOT],
                                   "book_ok": True}},
        "container,tower\nAAAA1111111,109\n")
    status, detail = eng.attempt("AAAA1111111", "109")
    assert status == "BOOKED"
    assert "17:00-17:59" in detail and "tower 109" in detail


def test_import_release_skips_permanently(tmp_path):
    eng, *_ = make_engine(
        tmp_path,
        {"AAAA1111111": {"validate_error": "App Error !IMPORT RELEASE"}},
        "container,tower\nAAAA1111111,109\n")
    status, detail = eng.attempt("AAAA1111111", "109")
    assert status == "SKIPPED"


def test_transient_validation_error_is_retried_not_skipped(tmp_path):
    # THE prototype bug: a one-off server error permanently skipped the
    # container. It must stay pending now.
    eng, *_ = make_engine(
        tmp_path,
        {"AAAA1111111": {"validate_error":
                         "the create operation has failed unexpectedly"}},
        "container,tower\nAAAA1111111,109\n")
    status, _ = eng.attempt("AAAA1111111", "109")
    assert status == "RETRY"


def test_repeated_unknown_errors_eventually_skip(tmp_path):
    eng, *_ = make_engine(
        tmp_path,
        {"AAAA1111111": {"validate_error": "weird error"}},
        "container,tower\nAAAA1111111,109\n")
    for _ in range(MAX_SAME_UNKNOWN_ERRORS - 1):
        assert eng.attempt("AAAA1111111", "109")[0] == "RETRY"
    status, detail = eng.attempt("AAAA1111111", "109")
    assert status == "SKIPPED" and "gave up" in detail


def test_no_openings_retries(tmp_path):
    eng, *_ = make_engine(
        tmp_path, {"AAAA1111111": {"no_openings_popup": True}},
        "container,tower\nAAAA1111111,109\n")
    assert eng.attempt("AAAA1111111", "109") == ("RETRY", "no openings")


def test_save_error_already_booked_skips(tmp_path):
    eng, *_ = make_engine(
        tmp_path,
        {"AAAA1111111": {"openings": [SLOT],
                         "save_error": "unit already has an appointment"}},
        "container,tower\nAAAA1111111,109\n")
    status, detail = eng.attempt("AAAA1111111", "109")
    assert status == "SKIPPED" and "already" in detail


def test_all_mode_rotates_one_container_per_tower(tmp_path):
    script = {c: {"no_openings_popup": True} for c in
              ("AAAA1111111", "BBBB2222222", "CCCC3333333", "DDDD4444444")}
    eng, session, dlg, events = make_engine(
        tmp_path, script,
        "container,tower\n"
        "AAAA1111111,109\nBBBB2222222,109\n"   # 109 has TWO pending
        "CCCC3333333,203\nDDDD4444444,205\n")
    eng.run(mode="all")
    # first pass must visit 109 once (first container only), then 203, 205
    assert session.opened[:3] == ["109", "203", "205"]


def test_single_mode_works_whole_list_each_pass(tmp_path):
    script = {"AAAA1111111": {"no_openings_popup": True},
              "BBBB2222222": {"openings": [SLOT], "book_ok": True}}
    eng, session, dlg, events = make_engine(
        tmp_path, script,
        "container,tower\nAAAA1111111,109\nBBBB2222222,109\n")
    eng.run(mode="single", tower="109")
    booked = [d for k, d in events if k == "booked"]
    assert booked and booked[0]["container"] == "BBBB2222222"
    assert session.opened.count("109") >= 2   # both containers same pass


def test_run_records_and_resumes(tmp_path):
    script = {"AAAA1111111": {"openings": [SLOT], "book_ok": True},
              "BBBB2222222": {"validate_error": "!IMPORT RELEASE"}}
    eng, session, dlg, events = make_engine(
        tmp_path, script,
        "container,tower\nAAAA1111111,109\nBBBB2222222,203\n")
    eng.run(mode="all")
    res = ResultsStore(tmp_path / "results.csv")
    assert res.done_set() == {"AAAA1111111", "BBBB2222222"}
    kinds = [k for k, _ in events]
    assert "finished" in kinds
    # a fresh engine over the same files has nothing to do
    eng2, session2, _, events2 = make_engine(
        tmp_path, script, "container,tower\n")  # rewrite trick not needed
    # reuse original containers file
    eng2.cfg.containers_file = str(tmp_path / "containers.csv")
    (tmp_path / "containers.csv").write_text(
        "container,tower\nAAAA1111111,109\nBBBB2222222,203\n")
    eng2.results = ResultsStore(tmp_path / "results.csv")
    eng2.run(mode="all")
    assert session2.opened == []    # nothing pending


def test_stop_event_halts_run(tmp_path):
    script = {"AAAA1111111": {"no_openings_popup": True}}
    eng, session, dlg, events = make_engine(
        tmp_path, script, "container,tower\nAAAA1111111,109\n")
    eng.cfg.max_hours = 1
    calls = {"n": 0}

    def stopping_sleep(s):
        calls["n"] += 1
        if calls["n"] > 3:
            eng.stop_event.set()

    eng.sleep = stopping_sleep
    eng.run(mode="all")
    assert events[-1][0] == "finished"
    assert events[-1][1]["reason"] == "stopped by user"


def test_exception_triggers_reconnect(tmp_path):
    class BoomSession(FakeSession):
        def __init__(self, dialog):
            super().__init__(dialog)
            self.boomed = False

        def open_dialog(self, tower, transaction_type=None):
            if not self.boomed:
                self.boomed = True
                raise RuntimeError("tab crashed")
            return super().open_dialog(tower, transaction_type)

    cfg = Config(containers_file=str(tmp_path / "c.csv"),
                 results_file=str(tmp_path / "r.csv"),
                 errors_file=str(tmp_path / "e.csv"),
                 poll_seconds=0, max_hours=0.001)
    (tmp_path / "c.csv").write_text("container,tower\nAAAA1111111,109\n")
    dialog = FakeDialog({"AAAA1111111": {"no_openings_popup": True}})
    session = BoomSession(dialog)
    events = []
    eng = Engine(cfg, session, results=ResultsStore(cfg.results_file),
                 errors=ErrorCapture(cfg.errors_file),
                 on_event=lambda k, **d: events.append(k),
                 sleeper=lambda s: None)
    eng.run(mode="all")
    assert session.reconnects == 1
    assert "reconnecting" in events


def test_full_error_text_captured_verbatim(tmp_path):
    long_err = ("clusternode10 | ictsi/za/dgt/dgt/trk-smacala | v 4.0.31\n"
                "the create operations has failed: " + "x" * 300)
    eng, *_ = make_engine(
        tmp_path, {"AAAA1111111": {"validate_error": long_err}},
        "container,tower\nAAAA1111111,109\n")
    eng.attempt("AAAA1111111", "109")
    captured = (tmp_path / "errors.csv").read_text()
    assert "x" * 300 in captured     # no 80-char truncation any more


def test_booked_container_removed_from_list_file(tmp_path):
    eng, session, dlg, events = make_engine(
        tmp_path, {"AAAA1111111": {"openings": [SLOT], "book_ok": True},
                   "BBBB2222222": {"no_openings_popup": True}},
        "container,tower\nAAAA1111111,109\nBBBB2222222,203\n")
    eng.run(mode="all")
    left = (tmp_path / "containers.csv").read_text()
    assert "AAAA1111111" not in left        # booked -> physically removed
    assert "BBBB2222222" in left            # unbooked stays for next run


def test_transaction_type_passed_to_dialog(tmp_path):
    eng, session, dlg, events = make_engine(
        tmp_path, {"AAAA1111111": {"no_openings_popup": True}},
        "container,tower\nAAAA1111111,109\n")
    eng.run(mode="all", transaction_type="Drop Off Export")
    assert session.transaction_type == "Drop Off Export"


def test_default_leaves_transaction_type_alone(tmp_path):
    eng, session, dlg, events = make_engine(
        tmp_path, {"AAAA1111111": {"no_openings_popup": True}},
        "container,tower\nAAAA1111111,109\n")
    eng.run(mode="all")
    assert session.transaction_type is None


def test_fifo_first_in_list_attempted_first(tmp_path):
    order = []
    script = {c: {"no_openings_popup": True}
              for c in ("FRSTU111111", "SCNDU222222")}
    eng, session, dlg, events = make_engine(
        tmp_path, script,
        "container,tower\nFRSTU111111,109\nSCNDU222222,109\n")
    real_enter = dlg.enter_container
    dlg.enter_container = lambda c: (order.append(c), real_enter(c))[1]
    eng.run(mode="single", tower="109")
    assert order[0] == "FRSTU111111"
    assert order[:2] == ["FRSTU111111", "SCNDU222222"]
