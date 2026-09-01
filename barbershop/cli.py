"""Command line for the barbershop app.

    python -m barbershop serve [--port 8080]   # run the booking site
    python -m barbershop today                 # print the day's chair
    python -m barbershop day 2026-09-05
    python -m barbershop pin 4821              # change the admin PIN
    python -m barbershop services              # print the price list
"""

import argparse
from datetime import date, timedelta

from . import SHOP, __version__
from . import schedule as sched
from .store import Store

STATUS_MARK = {"booked": " ", "confirmed": "*", "completed": "done",
               "cancelled": "cancelled", "no_show": "no show"}


def print_day(store, day):
    info = store.day_info(day)
    print(f"\n{SHOP['name']} - {info['label']}")
    if info["holiday"]:
        print(f"  public holiday: {info['holiday']}")
    if info["closed"]:
        print(f"  CLOSED ({info['reason']})\n")
        return
    print(f"  open {sched.friendly(info['open_min'])} - "
          f"{sched.friendly(info['close_min'])}")
    print("-" * 62)
    rows = store.bookings_for_day(day)
    breaks = store.blocks(day)
    if not rows and not breaks:
        print("  nothing booked yet")
    entries = [(b["start_min"], "booking", b) for b in rows]
    entries += [(b["start_min"], "block", b) for b in breaks]
    for _, kind, item in sorted(entries, key=lambda e: e[0]):
        if kind == "block":
            print(f"  {sched.friendly(item['start_min']):>8} - "
                  f"{sched.friendly(item['end_min']):<8}  "
                  f"[blocked] {item['reason']}")
            continue
        note = STATUS_MARK.get(item["status"], item["status"])
        print(f"  {item['time']:>8} - {item['end_time']:<8}  "
              f"{item['customer_name'][:18]:<18} {item['service_name'][:22]:<22} "
              f"{SHOP['currency']}{item['price']:<4} {note}")
    live = [b for b in rows if b["status"] in ("booked", "confirmed")]
    print("-" * 62)
    print(f"  {len(live)} in the chair, "
          f"{sum(b['duration_min'] for b in live)} minutes, "
          f"{SHOP['currency']}{sum(b['price'] for b in live)} expected\n")


def cmd_serve(store, args):
    from .webapp import serve
    store.close()
    return serve(args.db, host=args.host, port=args.port, quiet=args.quiet)


def cmd_today(store, args):
    print_day(store, store.today().isoformat())
    for offset in range(1, args.ahead + 1):
        print_day(store, (store.today() + timedelta(days=offset)).isoformat())
    return 0


def cmd_day(store, args):
    print_day(store, date.fromisoformat(args.date).isoformat())
    return 0


def cmd_pin(store, args):
    if not args.pin.isdigit() or not (4 <= len(args.pin) <= 8):
        print("The PIN has to be 4 to 8 digits.")
        return 1
    store.set_setting("admin_pin", args.pin)
    print(f"Admin PIN set to {args.pin}.")
    return 0


def cmd_services(store, args):
    category = None
    for service in store.services(active_only=not args.all):
        if service["category"] != category:
            category = service["category"]
            print(f"\n{category.upper()}")
        flag = "" if service["active"] else "  (off)"
        print(f"  {service['name']:<32} {SHOP['currency']}{service['price']:<5}"
              f" {service['duration_min']:>3} min{flag}")
    print()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="barbershop",
        description=f"{SHOP['name']} booking system")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--db", default="barbershop.db",
                        help="database file (default: barbershop.db)")
    subs = parser.add_subparsers(dest="command", required=True)

    run = subs.add_parser("serve", help="run the booking site")
    run.add_argument("--host", default="0.0.0.0")
    run.add_argument("--port", type=int, default=8080)
    run.add_argument("--quiet", action="store_true")
    run.set_defaults(func=cmd_serve)

    today = subs.add_parser("today", help="print today's appointments")
    today.add_argument("--ahead", type=int, default=0,
                       help="also print this many days after today")
    today.set_defaults(func=cmd_today)

    one = subs.add_parser("day", help="print one day's appointments")
    one.add_argument("date", help="YYYY-MM-DD")
    one.set_defaults(func=cmd_day)

    pin = subs.add_parser("pin", help="set the admin PIN")
    pin.add_argument("pin")
    pin.set_defaults(func=cmd_pin)

    price = subs.add_parser("services", help="print the price list")
    price.add_argument("--all", action="store_true",
                       help="include services switched off")
    price.set_defaults(func=cmd_services)

    args = parser.parse_args(argv)
    store = Store(args.db)
    try:
        return args.func(store, args)
    except BrokenPipeError:                            # `... | head`
        return 0
    finally:
        try:
            store.close()
        except Exception:                              # already closed by serve
            pass
