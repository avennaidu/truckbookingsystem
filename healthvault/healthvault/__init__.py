"""HealthVault - a local-first personal health record.

Pulls your medical history together from the places it already lives
(email attachments, medical-aid claim statements, clinic data dumps,
FHIR exports) and turns it into two things you can actually hand over:

  * an EMERGENCY CARD - a QR any phone camera reads with no app and no
    network, carrying the short list that changes what a paramedic does;
  * a PRACTITIONER SHARE - an expiring, revocable, PIN-protected link to
    the full record, for a first visit to a new doctor.

Design rules, in order of importance:

1. LOCAL FIRST. The record is one SQLite file on your machine. There is
   no account, no server, no sync. Nothing leaves the machine until you
   create a share, and a share is time-limited and logged.
2. NOTHING IS AUTO-ASSERTED. Importers never write to the record. They
   write to a review queue (`staged`) that you approve item by item. A
   parser that guesses "you have diabetes" from a claim line and files
   it silently is worse than no parser.
3. EVERY FACT KEEPS ITS SOURCE. Each clinical row points at the import
   that produced it, so you can always answer "says who?".
4. THE CARD IS OPT-IN PER ITEM. A QR on a keyring is readable by anyone
   holding it, so only rows you flag `on_card` are ever encoded, and the
   full record is never on the card - only behind a share.
"""

__version__ = "0.1.0"

APP_NAME = "HealthVault"

#: Clinical tables, in the order they are shown and exported.
RECORD_TABLES = (
    "allergy",
    "condition",
    "medication",
    "immunisation",
    "procedure",
    "observation",
    "encounter",
    "document",
)

#: Severity words we understand, worst first. Used to order the card.
SEVERITY_ORDER = ("life-threatening", "severe", "moderate", "mild", "")
