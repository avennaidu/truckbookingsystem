"""One entry point for everything:

    python -m truckbot run --all              # rotate 109->202->203->205
    python -m truckbot run --tower 109        # camp on one tower
    python -m truckbot ui [--port 8123]       # web UI for staff
    python -m truckbot make-list report.txt 109
    python -m truckbot status                 # results summary
"""

import argparse
import logging
import sys
from pathlib import Path

from . import VALID_TOWERS, __version__
from .config import load_config, validate_tower
from .containers import ResultsStore, load_containers


def setup_logging(cfg):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(cfg.log_file),
                  logging.StreamHandler()],
    )


def cmd_run(cfg, args):
    from .containers import ErrorCapture
    from .engine import Engine
    from .notify import Notifier
    from .session import N4Session

    if args.tower:
        mode, tower = "single", validate_tower(args.tower)
    else:
        mode, tower = "all", None

    if args.debug_port:
        debug_url = f"http://localhost:{args.debug_port}"
    elif tower:
        debug_url = cfg.debug_url_for(tower)   # per-tower Chrome session
    else:
        debug_url = cfg.debug_url
    session = N4Session(cfg, debug_url=debug_url)
    log = logging.getLogger("truckbot")
    try:
        session.connect()
    except Exception as e:
        log.error(
            "Could not connect to Chrome on %s.\n"
            "Start Chrome with --remote-debugging-port=9222, log in to N4 "
            "and open the Add Appointment screen first.\n%s",
            cfg.debug_url, e)
        return 1

    notifier = Notifier(cfg)
    engine = Engine(cfg, session, on_event=notifier)
    log.info("Attached %s (%s) | %s | %s | v%s", debug_url,
             f"tower {tower}" if tower else "all towers",
             cfg.trucking_company,
             args.transaction or "transaction type as set in N4",
             __version__)
    try:
        engine.run(mode=mode, tower=tower,
                   transaction_type=args.transaction)
    except KeyboardInterrupt:
        log.info("Interrupted. Summary: %s", engine.results.summary())
    finally:
        session.close()
    return 0


def cmd_ui(cfg, args):
    from .webui import serve
    serve(cfg, port=args.port)
    return 0


def cmd_make_list(cfg, args):
    from .reportparse import append_to_list, parse_report
    tower = validate_tower(args.tower)
    text = Path(args.report).read_text(encoding="utf-8", errors="ignore")
    bookable, excluded = parse_report(text)
    new = append_to_list(bookable, tower, cfg.containers_file)
    print(f"Tower {tower}: added {len(new)} new (of {len(bookable)} "
          f"bookable) -> {cfg.containers_file}")
    for c in new:
        print("  ", c)
    if excluded:
        print(f"Excluded {len(excluded)}:")
        for c, why in excluded:
            print("  ", c, f"({why})")
    return 0


def cmd_status(cfg, args):
    results = ResultsStore(cfg.results_file)
    pending = load_containers(cfg.containers_file, results.done_set())
    print(f"Summary: {results.summary() or 'no results yet'}")
    by_tower = {}
    for p in pending:
        by_tower[p["tower"]] = by_tower.get(p["tower"], 0) + 1
    print(f"Pending: {len(pending)} {by_tower}")
    for r in results.rows()[-10:]:
        print(f"  {r['timestamp']}  {r['container']:12s} "
              f"{r['status']:8s} {r['detail'][:60]}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="truckbot",
        description="Navis N4 slot-booking bot (ICTSI Durban DGT)")
    ap.add_argument("--config", default="config.json",
                    help="path to config.json")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="book containers")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--tower", choices=VALID_TOWERS,
                   help="camp on one tower")
    g.add_argument("--all", action="store_true",
                   help="rotate all towers (default)")
    p.add_argument("--transaction", "-t", default=None,
                   help="set Transaction Type (e.g. 'Pick Up Import', "
                        "'Drop Off Export'); default: leave as hand-set "
                        "in N4")
    p.add_argument("--debug-port", type=int, default=None,
                   help="Chrome debug port to attach to (default: the "
                        "tower's port from config debug_ports, else 9222)")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("ui", help="start the local web UI")
    p.add_argument("--port", type=int, default=8123)
    p.set_defaults(fn=cmd_ui)

    p = sub.add_parser("make-list",
                       help="filter an N4 report into the container list")
    p.add_argument("report", help="pasted report text file")
    p.add_argument("tower", help="tower for these containers")
    p.set_defaults(fn=cmd_make_list)

    p = sub.add_parser("status", help="show results summary")
    p.set_defaults(fn=cmd_status)

    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg)
    return args.fn(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
