"""Navis N4 slot-booking bot for ICTSI Durban Gateway Terminal (DGT).

Attach-mode automation: the user logs in to N4 by hand in a debug Chrome;
the bot connects over CDP and does only the repetitive part - set tower,
enter container, read openings, Save.
"""

__version__ = "1.0.0"

VALID_TOWERS = ("109", "202", "203", "205")

# Gate/Zone dropdown has near-duplicates (109 REEFER, 109A, 204A...), so
# towers are always matched by their EXACT full option label.
DEFAULT_GATE_LABELS = {
    "109": "109 (ITZ 109)",
    "202": "202 (ITZ 202)",
    "203": "203 (ITZ 203 Virtual Gate)",   # worded differently - keep as-is!
    "205": "205 (ITZ 205)",
}
