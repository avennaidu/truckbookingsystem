"""QR generation for the two card tiers.

Tier 1 - EMERGENCY: the plain-text payload from `emergency.py`, encoded
directly. No app, no network, no decryption step. Any phone camera shows
it as text. This is the whole point: the reader is a stranger under time
pressure with equipment you cannot predict.

Tier 2 - PRACTITIONER: a URL to a share served by this app. The full
record is far too large for a QR (a modest history runs to tens of
kilobytes against a ~2.9 kB ceiling), and it should not be world-readable
off a printed card anyway, so the link carries the data and `share.py`
carries the access rules.

`segno` is a pure-Python encoder with no dependencies of its own; if it
is missing we say so rather than failing obscurely at print time.
"""

from dataclasses import dataclass

try:
    import segno
except ImportError:                                   # pragma: no cover
    segno = None

#: Byte-mode capacity ceilings per error-correction level at version 40.
#: Used only to explain a failure, never to silently truncate.
BYTE_CAPACITY = {"L": 2953, "M": 2331, "Q": 1663, "H": 1273}

#: 'M' (~15% recovery) is the sweet spot for a card that will be
#: scuffed in a wallet but must stay coarse enough to scan fast.
DEFAULT_ERROR_LEVEL = "M"

#: Above this version the symbol gets fine enough that decoders start to
#: disagree. Measured, not guessed: a version-15 card payload that Apple
#: and Google read happily was refused outright by OpenCV's decoder,
#: while the same text at version 13 was read by all of them. We cannot
#: choose the scanner a stranger will point at this card, so we keep the
#: modules large and accept a slightly less redundant symbol.
CARD_MAX_VERSION = 13


class QRUnavailable(RuntimeError):
    """segno is not installed."""


class PayloadTooLarge(ValueError):
    """The text cannot be encoded at the requested error-correction level."""


@dataclass
class Symbol:
    data: str
    version: int
    error: str

    @property
    def modules(self) -> int:
        return self.version * 4 + 17


def _require():
    if segno is None:
        raise QRUnavailable(
            "QR generation needs the 'segno' package - run: pip install segno")


def make(data: str, error: str = DEFAULT_ERROR_LEVEL):
    """Encode `data`, raising a readable error when it will not fit."""
    _require()
    limit = BYTE_CAPACITY.get(error.upper(), BYTE_CAPACITY["M"])
    if len(data.encode("utf-8")) > limit:
        raise PayloadTooLarge(
            f"{len(data)} characters exceeds the {limit}-byte ceiling at "
            f"error level {error}. Lower the card budget, or share a link "
            f"instead of embedding the data.")
    return segno.make(data, error=error)


def svg(data: str, scale: int = 6, error: str = DEFAULT_ERROR_LEVEL,
        border: int = 4) -> str:
    """Inline SVG - what the web UI and the printable card embed."""
    import io
    buf = io.BytesIO()
    make(data, error).save(buf, kind="svg", scale=scale, border=border,
                           xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")


def png(data: str, path, scale: int = 8, error: str = DEFAULT_ERROR_LEVEL,
        border: int = 4) -> None:
    make(data, error).save(str(path), scale=scale, border=border)


def for_card(data: str) -> tuple[object, str]:
    """Encode a card payload as coarsely as possible.

    Steps the error-correction level down only as far as needed to stay
    within `CARD_MAX_VERSION`. Returns the symbol and the level used, so
    the caller can tell the user what it settled on.
    """
    chosen = None
    for error in ("M", "L"):
        code = make(data, error)
        chosen = (code, error)
        if code.version <= CARD_MAX_VERSION:
            return chosen
    return chosen


def card_svg(data: str, scale: int = 6, border: int = 4) -> str:
    """SVG for a card, using the coarsest encoding that fits."""
    import io
    code, _ = for_card(data)
    buf = io.BytesIO()
    code.save(buf, kind="svg", scale=scale, border=border,
              xmldecl=False, svgns=True)
    return buf.getvalue().decode("utf-8")


def describe(data: str, error: str = DEFAULT_ERROR_LEVEL) -> Symbol:
    """Version/size of the symbol, so the UI can warn about density."""
    code = make(data, error)
    return Symbol(data, code.version, error)


def ascii_art(data: str, error: str = DEFAULT_ERROR_LEVEL) -> str:
    """Terminal-scannable QR, for `healthvault card --ascii` over SSH."""
    return make(data, error).terminal(compact=True)
