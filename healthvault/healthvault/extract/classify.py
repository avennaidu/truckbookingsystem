"""Guess what a medical document is, and pull obvious result lines out.

This is keyword scoring, not comprehension. It exists to sort an inbox
of 300 attachments into piles a human can work through, and every guess
it makes lands in the review queue with its confidence attached - which
is why crude-but-transparent beats clever-but-silent here.
"""

import re

#: kind -> (weighted keywords). Lower-cased substring matching.
SIGNALS: dict[str, dict[str, int]] = {
    "lab_result": {
        "pathology": 3, "laboratory": 3, "specimen": 3, "reference range": 4,
        "haematology": 3, "chemistry": 2, "full blood count": 4, "cholesterol": 2,
        "hba1c": 4, "creatinine": 3, "collected": 1, "ampath": 3, "lancet": 2,
        "pathcare": 3, "result": 1,
    },
    "prescription": {
        "prescription": 4, "rx": 2, "dispense": 3, "repeat": 2, "pharmacy": 3,
        "tablets": 2, "mg ": 1, "take one": 3, "prescriber": 3, "script": 2,
    },
    "referral": {"referral": 4, "refer": 2, "kindly see": 3, "dear doctor": 3,
                 "dear colleague": 3},
    "discharge": {"discharge summary": 5, "admitted": 3, "discharged": 3,
                  "ward": 2, "admission": 3, "hospital course": 4},
    "imaging": {"radiology": 4, "x-ray": 3, "xray": 3, "ultrasound": 3, "mri": 3,
                "ct scan": 3, "sonar": 2, "impression": 2, "radiologist": 4},
    "invoice": {"invoice": 4, "tax invoice": 5, "amount due": 4, "statement": 2,
                "claim": 2, "vat": 2, "balance": 2, "account no": 2, "medical aid": 1},
    "immunisation": {"vaccination": 4, "immunisation": 4, "immunization": 4,
                     "vaccine": 3, "booster": 2, "dose 1": 2},
}

#: A document only gets a kind if it clears this, else it stays 'unknown'.
MIN_SCORE = 4

#: Common results worth lifting straight out of a report, with the unit
#: we expect. Anything else is left for a human to read in the document.
OBSERVATION_PATTERNS = [
    ("HbA1c", r"hba1c[^0-9%]{0,20}([0-9]{1,2}[.,][0-9])\s*%?", "%"),
    ("Total cholesterol", r"(?:total\s+)?cholesterol[^0-9]{0,20}([0-9]{1,2}[.,][0-9])", "mmol/L"),
    ("Creatinine", r"creatinine[^0-9]{0,20}([0-9]{1,3})\s*(?:umol|µmol)", "umol/L"),
    ("Haemoglobin", r"(?:haemoglobin|hemoglobin|hb)[^0-9]{0,20}([0-9]{1,2}[.,][0-9])", "g/dL"),
    ("Blood pressure", r"\b([0-9]{2,3}\s*/\s*[0-9]{2,3})\s*mm\s*hg", "mmHg"),
    ("Weight", r"weight[^0-9]{0,15}([0-9]{2,3}[.,]?[0-9]?)\s*kg", "kg"),
]

DATE_PATTERNS = [
    r"\b(20[0-9]{2}-[01][0-9]-[0-3][0-9])\b",
    r"\b([0-3]?[0-9]/[01]?[0-9]/20[0-9]{2})\b",
    r"\b([0-3]?[0-9]\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+20[0-9]{2})\b",
]


def classify(text: str, filename: str = "") -> tuple[str, float]:
    """Return (kind, confidence 0-1). 'unknown' when nothing scores."""
    haystack = f"{filename}\n{text}".lower()
    scores = {
        kind: sum(weight for word, weight in words.items() if word in haystack)
        for kind, words in SIGNALS.items()
    }
    kind, score = max(scores.items(), key=lambda kv: kv[1])
    if score < MIN_SCORE:
        return "unknown", 0.0
    runner_up = sorted(scores.values())[-2] if len(scores) > 1 else 0
    # Confidence rewards a clear winner, not just a high score: a file
    # that looks equally like an invoice and a lab report is a coin toss.
    margin = (score - runner_up) / score
    return kind, round(min(0.95, 0.45 + 0.5 * margin), 2)


def find_date(text: str) -> str:
    for pattern in DATE_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _normalise_date(match.group(1))
    return ""


def _normalise_date(raw: str) -> str:
    raw = raw.strip()
    if re.fullmatch(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]", raw):
        return raw
    match = re.fullmatch(r"([0-3]?[0-9])/([01]?[0-9])/(20[0-9]{2})", raw)
    if match:                      # day/month/year - the SA convention
        day, month, year = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    months = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun",
         "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
    match = re.fullmatch(r"([0-3]?[0-9])\s+([A-Za-z]{3})[a-z]*\s+(20[0-9]{2})", raw)
    if match:
        day, mon, year = match.groups()
        if mon.lower() in months:
            return f"{year}-{months[mon.lower()]:02d}-{int(day):02d}"
    return raw


def find_observations(text: str) -> list[dict]:
    """Lift recognisable result values out of a report.

    Deliberately conservative: a missed value costs a manual entry, a
    wrong value pollutes a medical record.
    """
    date = find_date(text)
    found = []
    for name, pattern, unit in OBSERVATION_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        found.append({
            "name": name,
            "value": match.group(1).replace(",", ".").replace(" ", ""),
            "unit": unit,
            "date": date,
        })
    return found
