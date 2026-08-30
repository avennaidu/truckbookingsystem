"""Build the emergency card payload.

What goes in the QR on your keyring is *not* your medical history. It is
the short list that changes what a stranger does in the first ten
minutes: what will kill you if given, what you already have, what you
already take, and who to phone.

Two constraints shape everything here:

* SIZE. A QR that holds 2 000 characters scans badly - dense modules,
  poor light, a cracked phone camera. Staying under roughly 700-900
  characters keeps the symbol coarse and readable, so the payload has a
  BUDGET and drops the least critical lines when it is exceeded.
* EXPOSURE. Anyone holding the card can read it. So only rows flagged
  `on_card` are ever encoded, and the payload is deliberately plain text
  rather than a link: in a casualty unit with no signal, a link is a
  blank screen.

Priority order below is the clinical one, and truncation follows it:
allergies survive at the cost of medications, medications at the cost of
past procedures. Identity and the "who to phone" line are never dropped.
"""

from dataclasses import dataclass, field
from datetime import date, datetime

from . import SEVERITY_ORDER

#: Default character budget for the QR payload. Sized so a typical card
#: encodes at or below `qr.CARD_MAX_VERSION`, which is where every
#: decoder we tested still reads it reliably. Raising this trades
#: scanning reliability for completeness - the wrong way round for a
#: card whose whole job is to be read by a stranger's phone, in a hurry,
#: in bad light. The printed card carries the same facts as text anyway.
DEFAULT_BUDGET = 400

#: Lower number = kept longer under pressure.
PRIORITY = {
    "identity": 0,
    "allergies": 1,
    "conditions": 2,
    "medications": 3,
    "contacts": 4,
    "aid": 5,
    "footer": 6,
}

#: Sections that must appear even if the budget is blown. Without the
#: name the card is anonymous; without a contact nobody can be phoned.
NEVER_DROP = ("identity", "contacts", "footer")

#: Sections where saying nothing would be read as "there is nothing".
#: A missing MEDS line implies the patient takes no medication; a missing
#: MEDICAL AID line implies nothing clinical at all, so it is left off
#: silently rather than spending characters on a marker.
MARK_WHEN_EMPTY = {"allergies", "conditions", "medications"}

#: Sections that may be trimmed but never emptied. A card that has run
#: out of room and responds by deleting "Penicillin - anaphylaxis"
#: has optimised away the one line most likely to matter. When the floor
#: and the budget conflict, the FLOOR WINS: the card comes back over
#: budget and flagged, the QR gets denser, and the printed text still
#: carries every word. That is the right way round.
MIN_ITEMS = {"allergies": 1}


@dataclass
class Section:
    key: str
    label: str
    items: list[str] = field(default_factory=list)
    dropped: int = 0

    @property
    def priority(self) -> int:
        return PRIORITY.get(self.key, 9)

    def render(self) -> str:
        if not self.items:
            # A section emptied by truncation still says so. Omitting the
            # MEDS line entirely reads as "takes no medication", which is
            # a worse error than admitting the card ran out of room.
            if self.dropped and self.label and self.key in MARK_WHEN_EMPTY:
                return f"{self.label}: ({self.dropped} not shown)"
            return ""
        body = "; ".join(self.items)
        if self.dropped:
            body += f" (+{self.dropped} more)"
        return f"{self.label}: {body}" if self.label else body


@dataclass
class Card:
    sections: list[Section]
    text: str
    truncated: bool
    dropped_total: int
    budget: int

    @property
    def length(self) -> int:
        return len(self.text)


def age_from(dob: str, today: date | None = None) -> int | None:
    """Whole years from an ISO date of birth, or None if unparseable."""
    try:
        born = datetime.strptime(dob.strip()[:10], "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None
    today = today or date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _severity_rank(value: str) -> int:
    value = (value or "").strip().lower()
    return SEVERITY_ORDER.index(value) if value in SEVERITY_ORDER else len(SEVERITY_ORDER)


def _allergy_line(row) -> str:
    out = row["substance"]
    detail = ", ".join(p for p in (row["reaction"], row["severity"]) if p)
    return f"{out} ({detail})" if detail else out


def _medication_line(row) -> str:
    return " ".join(p for p in (row["name"], row["dose"], row["frequency"]) if p)


def _contact_line(row) -> str:
    who = row["name"]
    if row["relationship"]:
        who += f" ({row['relationship']})"
    return f"{who} {row['phone']}".strip()


def build_sections(profile, allergies, conditions, medications, contacts,
                   today: date | None = None) -> list[Section]:
    """Assemble every candidate line, before any budget is applied.

    Callers pass rows already filtered to `on_card`; this function does
    not second-guess that choice, it only orders and formats.
    """
    ident = []
    name = (profile["preferred_name"] or profile["full_name"]).strip()
    if name:
        ident.append(name)
    age = age_from(profile["dob"] or "", today)
    if profile["dob"]:
        ident.append(f"DOB {profile['dob']}" + (f" ({age})" if age is not None else ""))
    if profile["sex"]:
        ident.append(profile["sex"])
    if profile["blood_type"]:
        ident.append(f"Blood {profile['blood_type']}")
    if profile["organ_donor"]:
        ident.append("ORGAN DONOR")

    allergy_rows = sorted(allergies, key=lambda r: _severity_rank(r["severity"]))
    aid = []
    if profile["scheme"]:
        aid.append(" ".join(p for p in (profile["scheme"], profile["plan"]) if p))
    if profile["member_number"]:
        aid.append(f"member {profile['member_number']}"
                   + (f"/{profile['dependant_code']}" if profile["dependant_code"] else ""))

    return [
        Section("identity", "", ident),
        Section("allergies", "ALLERGIES", [_allergy_line(r) for r in allergy_rows]
                or ["none recorded"]),
        Section("conditions", "CONDITIONS", [r["name"] for r in conditions]),
        Section("medications", "MEDS", [_medication_line(r) for r in medications]),
        Section("contacts", "ICE",
                [_contact_line(r) for r in sorted(contacts, key=lambda r: r["priority"])]),
        Section("aid", "MEDICAL AID", aid),
    ]


def build(profile, allergies, conditions, medications, contacts,
          budget: int = DEFAULT_BUDGET, today: date | None = None,
          heading: str = "EMERGENCY MEDICAL") -> Card:
    """Render the card, trimming lowest-priority items until it fits.

    Trimming removes whole ITEMS, never characters, so the card can never
    show a half-written drug name - a truncated dose is a dangerous dose.
    """
    sections = build_sections(profile, allergies, conditions, medications,
                              contacts, today)
    stamp = (today or date.today()).isoformat()
    footer = Section("footer", "", [f"Updated {stamp}"])
    sections.append(footer)

    dropped_total = 0
    while True:
        text = _render(heading, sections)
        if len(text) <= budget:
            return Card(sections, text, dropped_total > 0, dropped_total, budget)
        victim = _next_victim(sections)
        if victim is None:
            # Everything droppable is gone and it still does not fit -
            # what remains is protected. Return it over budget rather than
            # mangling it; the caller decides whether to raise the budget
            # or shorten the record by hand.
            return Card(sections, text, True, dropped_total, budget)
        victim.items.pop()
        victim.dropped += 1
        dropped_total += 1


def _next_victim(sections: list[Section]) -> Section | None:
    """The lowest-priority section that still has an item it may give up.

    "May" is doing the work: a section at its `MIN_ITEMS` floor is not a
    candidate however low its priority, so the last allergy survives even
    when the budget does not.
    """
    candidates = [s for s in sections
                  if s.key not in NEVER_DROP
                  and len(s.items) > MIN_ITEMS.get(s.key, 0)]
    if not candidates:
        return None
    return max(candidates, key=lambda s: s.priority)


def _render(heading: str, sections: list[Section]) -> str:
    lines = [heading]
    for section in sorted(sections, key=lambda s: s.priority):
        rendered = section.render()
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)
