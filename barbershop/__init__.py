"""Faded Studio by Jay - appointment booking for a single-chair barbershop.

    python -m barbershop serve            ->  http://localhost:8080

Two pages come out of the one server:

    /        customers pick a service, a day and a time, and book
    /admin   Jay sees the day's chair, adds walk-ins, blocks time off

Everything is stdlib: sqlite3 for storage, http.server for the web, no
packages to install on the shop laptop.
"""

__version__ = "1.0.0"

SHOP = {
    "name": "Faded Studio by Jay",
    "tagline": "Precision. Style. Confidence.",
    "barber": "Jay Professional Barber",
    "phone": "062 541 0305",
    "address": "Inside Blak Carwash, Shop 34, 7-10 Lagoon Drive, Ocean Mall",
    "timezone": "Africa/Johannesburg",
    "currency": "R",
}
