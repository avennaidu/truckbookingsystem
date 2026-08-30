"""HTML rendering shared by the web UI, the share view and exports.

Deliberately one small set of helpers plus inline CSS rather than a
template engine: the share page has to render correctly on a clinic
computer with no internet, so nothing may load from a CDN.
"""

import html
from datetime import date

from . import RECORD_TABLES
from .emergency import age_from

CSS = """
:root{--ink:#12181f;--muted:#5c6b7a;--line:#dfe5ec;--bg:#f6f8fa;--card:#fff;
--accent:#0b6bcb;--warn:#b42318;--warnbg:#fef3f2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent)}
header{background:var(--card);border-bottom:1px solid var(--line);padding:14px 20px;
display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0;letter-spacing:-.01em}
nav a{margin-right:14px;text-decoration:none;color:var(--muted);font-weight:500}
nav a.on,nav a:hover{color:var(--accent)}
main{max-width:1000px;margin:0 auto;padding:22px 20px 60px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:18px;margin-bottom:18px}
.card h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;
color:var(--muted);margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);
vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted)}
tr:last-child td{border-bottom:none}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;
background:var(--bg);border:1px solid var(--line);color:var(--muted)}
.pill.bad{background:var(--warnbg);border-color:#fecdc9;color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat b{display:block;font-size:24px;line-height:1.1}
.stat span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
input,select,textarea{font:inherit;padding:7px 9px;border:1px solid var(--line);
border-radius:7px;width:100%;background:#fff;color:var(--ink)}
label{display:block;font-size:12px;color:var(--muted);margin:0 0 4px;
text-transform:uppercase;letter-spacing:.05em}
.field{margin-bottom:10px}
button,.btn{font:inherit;font-weight:600;padding:8px 14px;border-radius:7px;
border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;
text-decoration:none;display:inline-block}
button.sec,.btn.sec{background:#fff;color:var(--ink);border-color:var(--line)}
button.danger{background:var(--warn);border-color:var(--warn)}
.note{color:var(--muted);font-size:13px}
.warn{background:var(--warnbg);border:1px solid #fecdc9;color:var(--warn);
padding:10px 12px;border-radius:8px;font-size:14px;margin-bottom:14px}
pre.card-text{white-space:pre-wrap;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:12px;margin:0}
.qr{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.qr svg{width:210px;height:210px;background:#fff;border:1px solid var(--line);
border-radius:8px;padding:8px}
@media print{header,nav,.noprint{display:none!important}
body{background:#fff}main{max-width:none;padding:0}
.card{border:none;page-break-inside:avoid}}
"""

#: Columns shown per table, and the heading for each.
TABLE_VIEWS = {
    "allergy": [("substance", "Substance"), ("reaction", "Reaction"),
                ("severity", "Severity"), ("status", "Status")],
    "condition": [("name", "Condition"), ("code", "Code"), ("status", "Status"),
                  ("onset", "Since")],
    "medication": [("name", "Medication"), ("dose", "Dose"),
                   ("frequency", "Frequency"), ("reason", "For"),
                   ("status", "Status")],
    "immunisation": [("vaccine", "Vaccine"), ("date", "Date"),
                     ("batch", "Batch"), ("provider", "Given by")],
    "procedure": [("name", "Procedure"), ("date", "Date"),
                  ("provider", "By"), ("facility", "Where")],
    "observation": [("name", "Test"), ("value", "Value"), ("unit", "Unit"),
                    ("ref_range", "Range"), ("date", "Date")],
    "encounter": [("date", "Date"), ("kind", "Type"), ("provider", "Provider"),
                  ("reason", "Reason"), ("cost", "Claimed")],
    "document": [("date", "Date"), ("title", "Title"), ("kind", "Kind")],
}

TABLE_TITLES = {
    "allergy": "Allergies", "condition": "Conditions", "medication": "Medications",
    "immunisation": "Immunisations", "procedure": "Procedures",
    "observation": "Results & measurements", "encounter": "Visits",
    "document": "Documents",
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def page(title: str, body: str, nav: str = "") -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
            f"{nav}<main>{body}</main></body></html>")


def table_html(table: str, rows, actions=None) -> str:
    """Render one clinical table. `actions` renders an extra cell per row."""
    view = TABLE_VIEWS.get(table)
    if view is None:
        return ""
    if not rows:
        return f"<p class=note>Nothing recorded.</p>"
    head = "".join(f"<th>{esc(label)}</th>" for _, label in view)
    if actions:
        head += "<th></th>"
    body = []
    for row in rows:
        cells = []
        for key, _ in view:
            value = row[key] if key in row.keys() else ""
            if key == "severity" and str(value).lower() in ("severe", "life-threatening"):
                value = f"<span class='pill bad'>{esc(value)}</span>"
            elif key == "status" and value:
                value = f"<span class=pill>{esc(value)}</span>"
            else:
                value = esc(value)
            cells.append(f"<td>{value}</td>")
        if actions:
            cells.append(f"<td class=noprint>{actions(table, row)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<table><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>")


def profile_header(profile) -> str:
    name = profile["preferred_name"] or profile["full_name"] or "Unnamed record"
    bits = []
    if profile["dob"]:
        age = age_from(profile["dob"])
        bits.append(f"DOB {profile['dob']}" + (f" · {age}" if age is not None else ""))
    for key, label in (("sex", ""), ("blood_type", "Blood ")):
        if profile[key]:
            bits.append(f"{label}{profile[key]}")
    if profile["organ_donor"]:
        bits.append("Organ donor")
    if profile["scheme"]:
        scheme = " ".join(p for p in (profile["scheme"], profile["plan"]) if p)
        if profile["member_number"]:
            scheme += f" · {profile['member_number']}"
        bits.append(scheme)
    return (f"<h1 style='margin:0 0 4px;font-size:22px'>{esc(name)}</h1>"
            f"<p class=note style='margin:0'>{esc(' · '.join(bits))}</p>")


def record_html(conn, tables=RECORD_TABLES, heading: str = "",
                include_text: bool = False) -> str:
    """The record itself - used by the share view and by exports."""
    from . import store
    out = []
    if heading:
        out.append(f"<div class=card>{heading}</div>")
    for table in tables:
        rows = store.rows(conn, table)
        if not rows:
            continue
        out.append(f"<div class=card><h2>{esc(TABLE_TITLES.get(table, table))}</h2>"
                   f"{table_html(table, rows)}</div>")
    if not out or (heading and len(out) == 1):
        out.append("<div class=card><p class=note>This record is empty.</p></div>")
    return "".join(out)


def footer_note() -> str:
    return (f"<p class=note>Generated by HealthVault on {date.today().isoformat()}. "
            f"This is a personal record kept by the patient. It is not a "
            f"clinical document and may be incomplete - confirm anything "
            f"critical with the patient or the treating practitioner.</p>")
