"""The printable emergency card.

Produces one HTML page holding three things, sized to be printed on a
single A4 sheet and cut up:

* a WALLET CARD (85 x 54 mm, bank-card sized) with the QR and the few
  lines that must be readable without a phone at all - because a QR is
  useless to a first responder whose phone is flat;
* a second identical card, so one lives in a wallet and one in the car;
* a FRIDGE SHEET at full size, which is where paramedics in most
  countries are trained to look when attending a home.

The human-readable text is not decoration. It is the fallback for every
failure mode the QR has: no phone, no camera, cracked screen, bad light.
"""

from datetime import date

from . import qr
from .emergency import Card
from .render import esc

PRINT_CSS = """
@page{size:A4;margin:12mm}
body{margin:0;font:12px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
Helvetica,Arial,sans-serif;color:#000;background:#fff}
.sheet{max-width:186mm;margin:0 auto}
.cards{display:flex;gap:6mm;flex-wrap:wrap;margin-bottom:8mm}
.wallet{width:85mm;height:54mm;border:1px dashed #999;border-radius:3mm;
padding:3mm;display:flex;gap:3mm;overflow:hidden;page-break-inside:avoid}
.wallet .qr{flex:0 0 40mm}
.wallet .qr svg{width:40mm;height:40mm}
.wallet .txt{flex:1;min-width:0;font-size:7.4pt;line-height:1.28}
.wallet h3{margin:0 0 1mm;font-size:9pt;letter-spacing:.02em}
.wallet .lbl{font-weight:700;text-transform:uppercase;font-size:6.4pt;
letter-spacing:.04em}
.wallet .ice{margin-top:1mm;border-top:.4mm solid #000;padding-top:1mm}
.fridge{border:1.5mm solid #b42318;border-radius:3mm;padding:6mm;
page-break-inside:avoid}
.fridge h2{margin:0 0 2mm;font-size:20pt;color:#b42318;letter-spacing:-.01em}
.fridge .row{display:flex;gap:6mm;align-items:flex-start}
.fridge .qr svg{width:52mm;height:52mm}
.fridge dl{margin:0;font-size:11pt}
.fridge dt{font-weight:700;text-transform:uppercase;font-size:8pt;
letter-spacing:.05em;color:#555;margin-top:2.5mm}
.fridge dd{margin:0}
.cut{font-size:8pt;color:#777;margin:0 0 2mm}
.dense{color:#b42318;font-size:8pt}
"""


def _sections(card: Card) -> dict[str, list[str]]:
    return {section.key: section.items for section in card.sections}


def _dl(card: Card) -> str:
    out = []
    for key, label in (("allergies", "Allergies"), ("conditions", "Conditions"),
                       ("medications", "Medications"), ("contacts", "In an emergency call"),
                       ("aid", "Medical aid")):
        items = _sections(card).get(key) or []
        if items:
            out.append(f"<dt>{label}</dt><dd>{esc('; '.join(items))}</dd>")
    return f"<dl>{''.join(out)}</dl>"


def _wallet(card: Card, symbol_svg: str) -> str:
    parts = _sections(card)
    name = "; ".join(parts.get("identity") or []) or "Emergency medical card"
    allergies = "; ".join(parts.get("allergies") or []) or "none recorded"
    ice = "; ".join(parts.get("contacts") or []) or ""
    conditions = "; ".join(parts.get("conditions") or [])
    body = [f"<h3>{esc(name.split(';')[0])}</h3>",
            f"<div>{esc('; '.join(name.split(';')[1:]).strip())}</div>",
            f"<div style='margin-top:1mm'><span class=lbl>Allergies</span><br>"
            f"{esc(allergies)}</div>"]
    if conditions:
        body.append(f"<div style='margin-top:1mm'><span class=lbl>Conditions</span><br>"
                    f"{esc(conditions)}</div>")
    if ice:
        body.append(f"<div class=ice><span class=lbl>Emergency contact</span><br>"
                    f"{esc(ice)}</div>")
    return (f"<div class=wallet><div class=qr>{symbol_svg}</div>"
            f"<div class=txt>{''.join(body)}</div></div>")


def sheet(card: Card, title: str = "Emergency medical card") -> str:
    """Full printable page. Raises `qr.QRUnavailable` if segno is missing."""
    symbol = qr.card_svg(card.text, scale=4, border=2)
    code, level = qr.for_card(card.text)
    warning = ""
    if code.version > qr.CARD_MAX_VERSION:
        warning = (f"<p class=dense>This code is dense (version {code.version}, "
                   f"error level {level}). Some scanners struggle above version "
                   f"{qr.CARD_MAX_VERSION} - take a few items off the card so it "
                   f"reads first time in poor light.</p>")
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<title>{esc(title)}</title><style>{PRINT_CSS}</style></head><body>"
            f"<div class=sheet>"
            f"<p class=cut>Print at 100% (no 'fit to page'), then cut along the "
            f"dashed lines. One card for your wallet, one for the car, the sheet "
            f"for the fridge.</p>"
            f"{warning}"
            f"<div class=cards>{_wallet(card, symbol)}{_wallet(card, symbol)}</div>"
            f"<div class=fridge><h2>EMERGENCY MEDICAL INFORMATION</h2>"
            f"<div class=row><div class=qr>{symbol}</div><div>{_dl(card)}</div></div>"
            f"<p style='margin:4mm 0 0;font-size:8pt;color:#555'>"
            f"Scan the code with any phone camera - no app needed. "
            f"Updated {date.today().isoformat()}.</p></div>"
            f"</div></body></html>")
