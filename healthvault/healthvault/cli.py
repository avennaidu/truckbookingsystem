"""Command line.

    healthvault init                      set up the vault in this folder
    healthvault serve                     the web UI (what most people use)
    healthvault import <file> [--as ...]  bring in a file
    healthvault email-scan [--dry-run]    scan the configured mailbox
    healthvault review [--approve-all]    work the queue from the terminal
    healthvault card [--print out.html] [--ascii]
    healthvault share new|list|revoke
    healthvault export <file.html>
    healthvault status
"""

import argparse
import json
import sys
from pathlib import Path

from . import RECORD_TABLES, __version__, qr, share, store
from .card import sheet
from .config import DEFAULT_CONFIG_FILE, Config
from .render import TABLE_TITLES
from .vault import Vault


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="healthvault", description="Your medical history, on your machine.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_FILE))
    parser.add_argument("--db", default="", help="override the record file")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create config.json and an empty record")

    serve = sub.add_parser("serve", help="run the local web UI")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--no-browser", action="store_true")

    imp = sub.add_parser("import", help="import a file into the review queue")
    imp.add_argument("path")
    imp.add_argument("--as", dest="table", default="", choices=("",) + RECORD_TABLES,
                     help="for a plain CSV, say what it is a list of")

    scan = sub.add_parser("email-scan", help="scan the configured mailbox")
    scan.add_argument("--dry-run", action="store_true",
                      help="report what would be saved, save nothing")

    review = sub.add_parser("review", help="work the review queue")
    review.add_argument("--approve-all", action="store_true")
    review.add_argument("--reject-all", action="store_true")

    card = sub.add_parser("card", help="show or print the emergency card")
    card.add_argument("--print", dest="out", default="",
                      help="write the printable wallet/fridge sheet here")
    card.add_argument("--ascii", action="store_true", help="QR in the terminal")
    card.add_argument("--budget", type=int, default=0)

    shares = sub.add_parser("share", help="practitioner share links")
    shares_sub = shares.add_subparsers(dest="share_command", required=True)
    new = shares_sub.add_parser("new")
    new.add_argument("--label", default="")
    new.add_argument("--scope", default="", choices=("",) + tuple(share.SCOPES))
    new.add_argument("--hours", type=int, default=None)
    new.add_argument("--pin", default="")
    new.add_argument("--max-views", type=int, default=0)
    new.add_argument("--qr", action="store_true", help="also print a scannable QR")
    shares_sub.add_parser("list")
    revoke = shares_sub.add_parser("revoke")
    revoke.add_argument("token", nargs="?", default="")
    revoke.add_argument("--all", action="store_true")

    export = sub.add_parser("export", help="write the whole record to one HTML file")
    export.add_argument("out")

    sub.add_parser("status", help="what is in the record")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _init(args)

    config = Config.load(args.config)
    vault = Vault(config, db_path=args.db or None)
    try:
        return _dispatch(args, vault)
    finally:
        vault.close()


def _dispatch(args, vault: Vault) -> int:
    if args.command == "serve":
        from .webui import serve
        serve(vault, args.host, args.port, open_browser=not args.no_browser)
        return 0

    if args.command == "import":
        report = vault.import_path(args.path, args.table)
        print(report.summary())
        for note in report.notes:
            print(f"  {note}")
        if report.staged:
            print(f"\nNothing has been added to your record yet. Run "
                  f"'healthvault review' or open the web UI to confirm them.")
        return 0

    if args.command == "email-scan":
        try:
            report = vault.scan_email(dry_run=args.dry_run)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(report.summary())
        for note in report.notes:
            print(f"  {note}")
        return 0

    if args.command == "review":
        return _review(args, vault)

    if args.command == "card":
        return _card(args, vault)

    if args.command == "share":
        return _share(args, vault)

    if args.command == "export":
        from .render import footer_note, page, profile_header, record_html
        html = page("Health record",
                    f"<div class=card>{profile_header(vault.profile)}</div>"
                    + record_html(vault.conn)
                    + f"<div class=card>{footer_note()}</div>")
        Path(args.out).write_text(html, encoding="utf-8")
        print(f"Wrote {args.out} — one self-contained file, opens in any browser.")
        return 0

    if args.command == "status":
        return _status(vault)
    return 1


def _init(args) -> int:
    config = Config.load(args.config)
    vault = Vault(config, db_path=args.db or None)
    Path(config.documents_dir).mkdir(parents=True, exist_ok=True)
    vault.close()
    print(f"Ready.\n"
          f"  record   {config.db_path}\n"
          f"  settings {args.config}\n"
          f"  files    {config.documents_dir}/\n\n"
          f"Keep all three off shared drives and out of git. Next:\n"
          f"  healthvault serve")
    return 0


def _review(args, vault: Vault) -> int:
    rows = store.pending(vault.conn)
    if not rows:
        print("Nothing waiting for review.")
        return 0
    if args.reject_all:
        for row in rows:
            store.reject(vault.conn, row["id"])
        print(f"Discarded {len(rows)}.")
        return 0
    if args.approve_all:
        # Deliberately loud: bulk-approving unreviewed medical facts is
        # exactly what this design exists to discourage.
        print(f"Approving all {len(rows)} without reading them. Every one "
              f"becomes part of your medical history.")
        for row in rows:
            store.approve(vault.conn, row["id"])
        print(f"Added {len(rows)}.")
        return 0
    for row in rows:
        payload = json.loads(row["payload"])
        detail = ", ".join(f"{k}={v}" for k, v in payload.items()
                           if v and k not in ("text",))
        print(f"[{row['id']}] {TABLE_TITLES.get(row['table_name'], row['table_name'])}"
              f" ({int(row['confidence'] * 100)}%)\n"
              f"     {detail[:300]}\n"
              f"     {row['reason']} · {row['source_label']}")
    print(f"\n{len(rows)} waiting. Approve them in the web UI "
          f"('healthvault serve'), or 'review --approve-all' / '--reject-all'.")
    return 0


def _card(args, vault: Vault) -> int:
    card = vault.card(budget=args.budget or None)
    print(card.text)
    print(f"\n[{card.length}/{card.budget} characters]", end="")
    if card.truncated:
        print(f" — {card.dropped_total} item(s) left off to keep the code readable",
              end="")
    print()
    if args.ascii:
        try:
            print("\n" + qr.ascii_art(card.text))
        except qr.QRUnavailable as exc:
            print(exc, file=sys.stderr)
            return 2
    if args.out:
        try:
            Path(args.out).write_text(sheet(card), encoding="utf-8")
        except (qr.QRUnavailable, qr.PayloadTooLarge) as exc:
            print(exc, file=sys.stderr)
            return 2
        print(f"Wrote {args.out} — open it and print at 100%.")
    return 0


def _share(args, vault: Vault) -> int:
    if args.share_command == "new":
        token, url = vault.new_share(
            label=args.label, scope=args.scope, hours=args.hours,
            pin=args.pin, max_views=args.max_views)
        print(url)
        row = share.find(vault.conn, token)
        print(f"  shows   {share.SCOPES[row['scope']][0].lower()}")
        print(f"  expires {row['expires_at'] or 'never'}")
        if args.pin:
            print(f"  PIN     {args.pin} (say it, do not send it with the link)")
        if args.max_views:
            print(f"  views   {args.max_views} maximum")
        if args.qr:
            try:
                print("\n" + qr.ascii_art(url))
            except qr.QRUnavailable as exc:
                print(exc, file=sys.stderr)
        if vault.config.host == "127.0.0.1" and not vault.config.share_base_url:
            print("\nNote: this link only resolves on this machine. For a "
                  "practitioner's own device, set share_base_url and host in "
                  "config.json to your network address.")
        return 0

    if args.share_command == "list":
        rows = share.active(vault.conn)
        if not rows:
            print("No shares.")
            return 0
        for row in rows:
            state = "revoked" if row["revoked"] else share.check(
                dict(row) | {"pin_hash": ""}).reason
            print(f"{row['token']}  {state:9} {row['scope']:9} "
                  f"views={row['views']}{'/' + str(row['max_views']) if row['max_views'] else ''}"
                  f"  {row['label']}")
        return 0

    if args.share_command == "revoke":
        if args.all:
            print(f"Revoked {share.revoke_all(vault.conn)} share(s).")
            return 0
        if not args.token:
            print("Give a token, or --all.", file=sys.stderr)
            return 2
        print("Revoked." if share.revoke(vault.conn, args.token) else "No such share.")
        return 0
    return 1


def _status(vault: Vault) -> int:
    profile = vault.profile
    name = profile["preferred_name"] or profile["full_name"] or "(no name set)"
    print(f"{name}")
    counts = vault.counts()
    for table in RECORD_TABLES:
        print(f"  {TABLE_TITLES[table]:26} {counts.get(table, 0):>4}")
    if counts.get("pending"):
        print(f"\n  {counts['pending']} item(s) waiting for review.")
    card = vault.card()
    print(f"\nEmergency card: {card.length}/{card.budget} characters"
          + (f", {card.dropped_total} item(s) left off" if card.truncated else ""))
    live = [r for r in share.active(vault.conn)
            if not r["revoked"] and share.check(dict(r) | {"pin_hash": ""}).ok]
    print(f"Active shares:  {len(live)}")
    return 0
