# HealthVault — your medical history, on your machine

Builds a personal health record out of the places your medical history
already lives — email attachments, medical-aid claim statements, clinic
data dumps, FHIR exports — and turns it into two things you can hand to
someone who needs it:

- an **emergency card**: a QR any phone camera reads with no app and no
  signal, carrying the short list that changes what a paramedic does;
- a **practitioner share**: an expiring, revocable, PIN-protected link to
  the full record, for a first visit to a new doctor.

There is no account and no cloud. The record is one SQLite file in a
folder you control.

## The four rules it is built on

1. **Local first.** No server, no sync, no sign-up. Nothing leaves the
   machine until you create a share, and every share is time-limited,
   revocable and logged.
2. **Nothing is auto-asserted.** Importers never write to your record.
   They write to a review queue you approve item by item. A parser that
   quietly concludes "you have diabetes" from a billing code is worse
   than no parser at all.
3. **Every fact keeps its source.** Each row points at the import that
   produced it, so you can always answer *says who?*
4. **The card is opt-in, per item.** A QR on a keyring can be read by
   anyone holding it, so only rows you tick as *on card* are ever
   encoded. Your full history is never in the QR — only behind a share.

## Install

```
git clone <this repo> && cd healthvault
pip install -r requirements.txt
python -m healthvault init
python -m healthvault serve
```

Python 3.10+. The UI opens at http://127.0.0.1:8137.

## Getting your history in

| Source | What it does |
|---|---|
| **Email** (IMAP) | Read-only scan for medical post — lab results, scripts, referrals, statements. Saves attachments locally and reads the values off lab reports. Never sends, moves or deletes anything. |
| **Medical aid statement** (`.csv`) | Turns claim lines into visits: who you saw, when, where. Column names are matched by alias, so Discovery, Bonitas, Momentum, Medihelp and Bestmed exports all work. |
| **FHIR bundle** (`.json`) | The standards-based export used by hospital portals and Apple Health Records. Highest-confidence source. |
| **Apple Health** (`export.xml`) | Keeps only standing measurements (weight, BP, glucose, SpO₂) — not your step count. |
| **Plain lists** (`.csv`) | Anything you typed yourself. Friendly headers (`Medication, Dose, How often`) map onto the schema. |
| **Documents** (`.pdf`, `.txt`) | Catalogued, text-extracted, classified as lab result / script / referral / discharge / imaging / invoice. |

Everything lands in **Review** first. Nothing enters your record until
you approve it, and each candidate shows its source, its reasoning and a
confidence score.

### Email setup

Put the connection in `config.json` and the password in the environment —
use a provider **app-password**, never your real one:

```json
"email": { "enabled": true, "host": "imap.gmail.com", "user": "you@gmail.com" }
```
```
export HEALTHVAULT_EMAIL_PASSWORD='your-app-password'
python -m healthvault email-scan --dry-run
```

`--dry-run` reports what it would save without saving anything.

## The emergency card

`python -m healthvault card --print card.html` produces one A4 sheet
holding two wallet-sized cards and a fridge sheet, each with the QR and
the same facts in plain text.

The text matters as much as the code: a QR is useless to a first
responder whose phone is flat, so everything in the code is also printed
where a human can read it.

**What goes in it** is only what you tick as *on card* — by default your
allergies, plus whatever conditions, medications and contacts you
choose. Truncation, when the card outgrows its budget, follows clinical
priority: medications give way before conditions, conditions before
allergies, and identity and emergency contacts are never dropped. The
last allergy is never dropped at all — if it will not fit, the card goes
over budget and says so rather than deleting the line most likely to
matter.

A section emptied by truncation still says `MEDS: (3 not shown)`, because
a missing line reads as *takes no medication*, which is a worse error
than admitting the card ran out of room.

**Why the card is small.** QR symbols get harder to scan as they get
denser, and decoders disagree at the top end — during development a
version-15 card that phones read happily was refused outright by
OpenCV's decoder, while the same text at version 13 was read by
everything. You cannot choose the scanner a stranger will point at your
card, so the payload budget (`card_budget`, default 400 characters) keeps
the modules large. `tests/test_qr.py` decodes every generated card to
prove it still reads.

## Sharing with a practitioner

Create a share, show them the QR:

```
python -m healthvault share new --label "Dr Moodley" --scope clinical \
    --hours 24 --pin 4821 --max-views 1 --qr
```

| Control | Default | Why |
|---|---|---|
| **Scope** | summary | A locum needs meds and allergies, not twenty years of claims |
| **Expiry** | 24 hours | Links end up in browser histories and WhatsApp threads |
| **PIN** | off | A number you say out loud, so the link alone is not enough |
| **Max views** | unlimited | Set `1` to make it single-use |
| **Revoke** | — | One click, any time |
| **Access log** | always on | Every hit, allowed or refused, with time and address |

A wrong PIN and an unknown token fail identically, so probing a link
tells an attacker nothing.

**For a practitioner's own device** the link has to be reachable from it,
which means binding beyond loopback:

```json
{ "host": "0.0.0.0", "share_base_url": "http://192.168.1.20:8137" }
```

When you do that, the management pages refuse every client that is not
on loopback. Widening the bind address exposes the share endpoint and
nothing else.

## Command line

```
python -m healthvault init                      set up in this folder
python -m healthvault serve                     the web UI
python -m healthvault import statement.csv      bring in a file
python -m healthvault import list.csv --as medication
python -m healthvault email-scan --dry-run      scan the mailbox
python -m healthvault review                    work the queue
python -m healthvault card --ascii              card + QR in the terminal
python -m healthvault card --print card.html    wallet + fridge sheet
python -m healthvault share new --scope full --hours 2 --pin 1234
python -m healthvault share list | revoke <token> | revoke --all
python -m healthvault export record.html        one self-contained file
python -m healthvault status
```

## Files

| file | what |
|---|---|
| `healthvault.db` | the record — **this is the thing to back up** |
| `documents/` | saved attachments, as they arrived |
| `config.json` | local settings, including the email connection |
| `*.html` cards/exports | your history in plain text — treat like the record |

All of it is gitignored. None of it should sit on a shared drive.

## What this does not protect you from

Said plainly, because health data deserves it:

- **The database is not encrypted.** SQLite stores it in the clear.
  Protecting it at rest is your operating system's job — turn on
  BitLocker, FileVault or LUKS. Anyone with your unlocked machine, or a
  backup of it, can read your record.
- **The emergency QR is readable by anyone holding the card.** That is
  the design, and the reason it carries only what you put on it. Do not
  tick anything onto the card you would not accept a stranger reading.
- **A share is only as private as the person you gave it to.** Expiry,
  PINs and view caps limit the damage; they cannot un-see a screenshot.
- **Exports are plain HTML.** Convenient, and completely unprotected.

## This is not a medical device

HealthVault is a filing system for your own records. It does not
diagnose, does not check drug interactions, and does not validate that
anything in it is correct — including what its own importers guessed,
which is exactly why they cannot write to your record without you.

Treat it as a well-organised copy of information whose authoritative
version lives with your doctors and your scheme. In an emergency, it is
a starting point for a clinician, not an instruction to them.

## Developing

```
pip install -r requirements-dev.txt
python -m pytest
```

Layout: `db`/`store` are the schema and the only writer; `emergency` and
`qr` build the card; `share` holds the access rules; `ingest/*` are the
importers, none of which can write outside the review queue; `extract/*`
turns files into text and guesses what they are; `render`/`card` are the
HTML; `webui` and `cli` are the two front doors; `vault` ties them
together.

The tests worth reading first are `tests/test_emergency.py` (what the
card keeps under pressure) and `tests/test_share.py` (every way a link is
refused).
