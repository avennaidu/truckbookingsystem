"""Classify N4 error dialogs.

N4's exact wording is only partially known, so keywords are matched
loosely BUT the default for anything unrecognised is RETRY, not skip:
the prototype once permanently skipped a container on a transient
"the create operation has failed..." server error. The engine caps
repeated identical unknown errors so a genuinely-broken container
still drops out after a few attempts.

Every dialog text should also be fed to ErrorCapture so unknown
wordings (e.g. "already has an appointment") get recorded verbatim
and can be added below.
"""

SKIP = "SKIP"      # permanent: no retry will ever work
RETRY = "RETRY"    # transient: try again next pass

# already booked / duplicate appointment -> permanent skip
_ALREADY_BOOKED = (
    "already has an appointment", "already has appointment",
    "existing appointment", "appointment already",
    "already booked", "duplicate appointment",
    "an appointment exists", "already an appointment",
)

# customs / release hold -> permanent skip (fix the hold, delete the
# results.csv row, and the container is picked up again)
_HOLD = (
    "import release", "!import release", "not released",
    "customs hold", "hold on unit", "unit is on hold", "on hold",
)

_NO_OPENINGS = ("no appointment opening", "no openings")


def classify_error(text: str | None) -> tuple[str, str]:
    """Map a dialog's text to (SKIP|RETRY, human reason)."""
    t = (text or "").lower()
    if any(k in t for k in _ALREADY_BOOKED):
        return SKIP, "already has an appointment"
    if any(k in t for k in _HOLD):
        return SKIP, "unit on hold / not released"
    if any(k in t for k in _NO_OPENINGS):
        return RETRY, "no openings"
    return RETRY, (t.strip()[:120] if t.strip() else "unknown error")
