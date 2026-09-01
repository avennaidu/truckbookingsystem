"""The price list, as it hangs on the wall of the shop.

Prices come straight off the board. Durations for the three services Jay
quoted are exact (haircut 30-45, shave 30, cut/colour/wash/set 90, package
one 75, package two 90); the rest are first estimates so every service can
be booked from day one. The chair is booked for `duration_min`, so a
haircut holds 45 minutes and finishes early when Jay is quick.

This module is only the seed. Once the database exists, prices and
durations live in the `services` table and Jay edits them from /admin -
changing a price here will not move a price that is already in the shop's
database.
"""

# Extras Jay offers on the end of a sitting - the page asks about these
# before a customer picks their day, at the add-on discount.
ADDONS = {"wax-nose", "wax-ears"}

# id, name, category, price (rand), duration (minutes), note
CATALOGUE = [
    ("haircut",        "Haircut",                      "Cuts & Shaves", 100, 45,
     "30-45 minutes depending on the cut"),
    ("blade-fade",     "Blade Fade",                   "Cuts & Shaves", 130, 45, ""),
    ("shave",          "Shave",                        "Cuts & Shaves",  80, 30, ""),
    ("steam-shave",    "Steam Shave (face massage)",   "Cuts & Shaves", 120, 40, ""),
    ("hot-towel",      "Hot Towel",                    "Cuts & Shaves",  30, 15, ""),
    ("head-wash",      "Head Wash",                    "Cuts & Shaves",  30, 15, ""),

    ("face-scrub",     "Face Scrub",                   "Skin",           90, 20, ""),
    ("facial",         "Facial",                       "Skin",          160, 30, ""),

    ("colour-only",    "Colour Only",                  "Colour",        180, 60, ""),
    ("cut-colour-set", "Cut, Colour, Wash, Set",       "Colour",        300, 90, ""),

    ("massage-hns",    "Head, Neck & Shoulder Massage","Massages",      140, 20, ""),
    ("massage-hn",     "Head & Neck Massage",          "Massages",       80, 10, ""),
    ("massage-sb",     "Shoulder & Back Massage",      "Massages",       80, 10, ""),

    ("wax-nose",       "Nose Wax",                     "Waxing",         50, 10, ""),
    ("wax-ears",       "Ear Wax",                      "Waxing",         50, 10, ""),

    ("thread-face",    "Full Face Threading",          "Threading",     110, 25, ""),
    ("thread-eyebrow", "Eyebrow Threading",            "Threading",      70, 15, ""),
    ("thread-tint",    "Eyebrow Tint",                 "Threading",      50, 15, ""),
    ("thread-lips",    "Upper Lip Threading",          "Threading",      40, 10, ""),
    ("thread-cheeks",  "Cheek Threading",              "Threading",      70, 15, ""),

    ("package-1",      "Package 1",                    "Packages",      350, 75,
     "Haircut, steam shave, hot towel, scrub, and a 10 minute head & neck "
     "or shoulder & back massage"),
    ("package-2",      "Package 2",                    "Packages",      450, 90,
     "Haircut, shave, facial, hot towel, and a 20 minute full massage"),
]

# The order categories appear on the booking page.
CATEGORY_ORDER = ["Packages", "Cuts & Shaves", "Colour", "Skin",
                  "Massages", "Waxing", "Threading"]


def seed_rows():
    """Catalogue as rows ready for the `services` table."""
    order = {name: i for i, name in enumerate(CATEGORY_ORDER)}
    rows = []
    for i, (sid, name, category, price, duration, note) in enumerate(CATALOGUE):
        rows.append({
            "id": sid,
            "name": name,
            "category": category,
            "price": price,
            "duration_min": duration,
            "note": note,
            "active": 1,
            "addon": 1 if sid in ADDONS else 0,
            "sort": order.get(category, 99) * 100 + i,
        })
    return rows
