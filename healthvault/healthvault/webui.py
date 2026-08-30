"""The local web UI (stdlib only - no framework, nothing from a CDN).

    python -m healthvault serve     ->  http://127.0.0.1:8137

Two audiences share one server, and keeping them apart is the main piece
of security work here:

* YOU, on the loopback interface, managing the record.
* A PRACTITIONER, holding a share link, seeing only `/s/<token>`.

So when the server is bound to anything other than loopback (which you
must do for a phone on the clinic wifi to reach a share), every
management route refuses non-loopback clients. Widening the bind address
exposes the share endpoint and nothing else.

Two further guards, both cheap:

* POSTs are rejected when an Origin or Referer header is present and
  points elsewhere, so a page you happen to have open in the same
  browser cannot drive this app behind your back.
* The share view sets `Referrer-Policy: no-referrer` and a restrictive
  CSP, so a token is not leaked onward by the practitioner's browser.
"""

import io
import ipaddress
import json
import logging
import shutil
import tempfile
import urllib.parse
from datetime import datetime, timezone
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import RECORD_TABLES, __version__, qr, share, store
from .card import sheet
from .render import (TABLE_TITLES, TABLE_VIEWS, esc, footer_note, page,
                     profile_header, record_html, table_html)
from .vault import Vault

log = logging.getLogger("healthvault")

NAV = [("/", "Overview"), ("/record", "Record"), ("/review", "Review"),
       ("/import", "Import"), ("/card", "Emergency card"), ("/shares", "Shares")]

PROFILE_FIELDS = [
    ("full_name", "Full name"), ("preferred_name", "Preferred name"),
    ("dob", "Date of birth (YYYY-MM-DD)"), ("sex", "Sex"),
    ("blood_type", "Blood type"), ("id_number", "ID number"),
    ("scheme", "Medical aid scheme"), ("plan", "Plan"),
    ("member_number", "Member number"), ("dependant_code", "Dependant code"),
]

#: Fields offered when adding a row by hand, per table.
ADD_FIELDS = {
    table: [key for key, _ in view] for table, view in TABLE_VIEWS.items()
}
ADD_FIELDS["emergency_contact"] = ["name", "relationship", "phone", "alt_phone"]


def nav_html(current: str) -> str:
    links = "".join(
        f"<a href='{href}' class='{'on' if href == current else ''}'>{esc(label)}</a>"
        for href, label in NAV)
    return (f"<header><h1>HealthVault</h1><nav>{links}</nav>"
            f"<span class=note style='margin-left:auto'>v{__version__}</span></header>")


def is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = f"HealthVault/{__version__}"
    vault: Vault = None            # set by serve()

    # -- plumbing ------------------------------------------------------

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    @property
    def client_ip(self) -> str:
        return self.client_address[0]

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8",
              extra: dict | None = None) -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "img-src data:; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, where: str) -> None:
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _deny(self, message: str = "Not available here.") -> None:
        self._send(page("Not available", f"<div class=card><p>{esc(message)}</p></div>"),
                   status=403)

    def _management_allowed(self) -> bool:
        """Management is loopback-only, whatever the server is bound to."""
        return is_loopback(self.client_ip)

    def _csrf_ok(self) -> bool:
        origin = self.headers.get("Origin") or ""
        referer = self.headers.get("Referer") or ""
        source = origin or referer
        if not source:
            return True                     # curl and friends; no browser to abuse
        host = self.headers.get("Host") or ""
        try:
            parsed = urllib.parse.urlparse(source)
        except ValueError:
            return False
        return parsed.netloc == host

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        if ctype.startswith("multipart/form-data"):
            return self._multipart(raw, ctype)
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"),
                                       keep_blank_values=True)
        return {k: v[0] for k, v in parsed.items()}

    def _multipart(self, raw: bytes, ctype: str) -> dict:
        """Parse an upload with the stdlib email parser (cgi is gone in 3.13)."""
        header = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        message = BytesParser(policy=email_policy).parsebytes(header + raw)
        out: dict = {}
        for part in message.iter_parts() if message.is_multipart() else []:
            disposition = part.get("Content-Disposition", "")
            name = part.get_param("name", header="Content-Disposition")
            filename = part.get_filename()
            if not name:
                continue
            if filename:
                out[name] = (filename, part.get_payload(decode=True) or b"")
            else:
                payload = part.get_payload(decode=True) or b""
                out[name] = payload.decode("utf-8", "replace")
        return out

    # -- routing -------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if path.startswith("/s/"):
            return self._share_view(path[3:], query.get("pin", [""])[0])
        if not self._management_allowed():
            return self._deny("This page is only available on the computer "
                              "the record lives on.")
        try:
            return self._route_get(path, query)
        except Exception as exc:                      # never leak a traceback
            log.exception("GET %s failed", path)
            return self._send(page("Error", f"<div class=card><p class=warn>"
                                            f"{esc(exc)}</p></div>"), status=500)

    def _route_get(self, path, query):
        if path == "/":
            return self._send(self._overview())
        if path == "/record":
            return self._send(self._record())
        if path == "/review":
            return self._send(self._review())
        if path == "/import":
            return self._send(self._import_page())
        if path == "/card":
            return self._send(self._card_page())
        if path == "/card/print":
            return self._send(sheet(self.vault.card()))
        if path == "/card/qr.svg":
            return self._send(qr.card_svg(self.vault.card().text),
                              ctype="image/svg+xml")
        if path == "/shares":
            return self._send(self._shares())
        if path.startswith("/share-qr/") and path.endswith(".svg"):
            token = path[len("/share-qr/"):-4]
            return self._send(qr.svg(self.vault.config.share_url(token)),
                              ctype="image/svg+xml")
        if path == "/export.html":
            return self._send(self._export(), extra={
                "Content-Disposition": 'attachment; filename="health-record.html"'})
        return self._send(page("Not found", "<div class=card>No such page.</div>"),
                          status=404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/s/"):
            form = self._form()
            return self._share_view(path[3:], form.get("pin", ""))
        if not self._management_allowed():
            return self._deny()
        if not self._csrf_ok():
            return self._deny("Rejected: that request came from another site.")
        try:
            return self._route_post(path)
        except Exception as exc:
            log.exception("POST %s failed", path)
            return self._send(page("Error", f"<div class=card><p class=warn>"
                                            f"{esc(exc)}</p></div>"), status=500)

    def _route_post(self, path):
        form = self._form()
        conn = self.vault.conn
        parts = [p for p in path.split("/") if p]

        if path == "/profile":
            payload = {k: form.get(k, "") for k, _ in PROFILE_FIELDS}
            payload["organ_donor"] = 1 if form.get("organ_donor") else 0
            store.save_profile(conn, payload)
            return self._redirect("/record")

        if parts[:1] == ["record"] and len(parts) >= 3:
            table = parts[1]
            if table not in set(RECORD_TABLES) | {"emergency_contact"}:
                return self._deny("Unknown section.")
            action = parts[2]
            if action == "add":
                payload = {k: form.get(k, "") for k in ADD_FIELDS.get(table, [])}
                if not any(payload.values()):
                    return self._redirect("/record")
                payload["on_card"] = 1 if form.get("on_card") else 0
                source_id = self.vault.conn.execute(
                    "SELECT id FROM source WHERE kind='manual' LIMIT 1").fetchone()
                if source_id is None:
                    from .db import add_source
                    source = add_source(conn, "manual", "entered by hand")
                else:
                    source = source_id[0]
                store.insert(conn, table, payload, source_id=source)
                return self._redirect(f"/record#{table}")
            row_id = int(parts[2])
            if len(parts) > 3 and parts[3] == "delete":
                store.delete(conn, table, row_id)
            elif len(parts) > 3 and parts[3] == "card":
                row = conn.execute(
                    f"SELECT on_card FROM {table} WHERE id = ?", (row_id,)).fetchone()
                if row is not None:
                    store.update(conn, table, row_id, {"on_card": 0 if row[0] else 1})
            return self._redirect(f"/record#{table}")

        if parts[:1] == ["review"] and len(parts) == 3:
            staged_id = int(parts[1])
            if parts[2] == "approve":
                store.approve(conn, staged_id)
            elif parts[2] == "reject":
                store.reject(conn, staged_id)
            return self._redirect("/review")

        if path == "/review/reject-all":
            conn.execute("UPDATE staged SET status='rejected' WHERE status='pending'")
            conn.commit()
            return self._redirect("/review")

        if path == "/import":
            return self._do_import(form)

        if path == "/email-scan":
            try:
                report = self.vault.scan_email()
                message = report.summary() + " — " + "; ".join(report.notes[:3])
            except ValueError as exc:
                message = str(exc)
            return self._send(self._import_page(message))

        if path == "/shares/new":
            token, url = self.vault.new_share(
                label=form.get("label", ""),
                scope=form.get("scope", "summary"),
                hours=int(form.get("hours") or 24),
                pin=form.get("pin", "").strip(),
                max_views=int(form.get("max_views") or 0))
            return self._redirect(f"/shares?new={urllib.parse.quote(token)}")

        if parts[:1] == ["shares"] and len(parts) == 3 and parts[2] == "revoke":
            share.revoke(conn, parts[1])
            return self._redirect("/shares")

        if path == "/shares/revoke-all":
            share.revoke_all(conn)
            return self._redirect("/shares")

        return self._send(page("Not found", "<div class=card>No such action.</div>"),
                          status=404)

    def _do_import(self, form):
        upload = form.get("file")
        table = form.get("as", "")
        if isinstance(upload, tuple) and upload[1]:
            filename, data = upload
            tmpdir = Path(tempfile.mkdtemp(prefix="healthvault-"))
            path = tmpdir / Path(filename).name
            path.write_bytes(data)
            # Documents must outlive the temp dir, so keep a copy in the vault.
            keep = Path(self.vault.config.documents_dir)
            keep.mkdir(parents=True, exist_ok=True)
            stored = keep / path.name
            if not stored.exists():
                shutil.copy2(path, stored)
            report = self.vault.import_path(stored, table)
        elif form.get("path"):
            report = self.vault.import_path(form["path"].strip(), table)
        else:
            return self._send(self._import_page("Choose a file first."))
        message = report.summary()
        if report.notes:
            message += " — " + "; ".join(report.notes[:3])
        return self._send(self._import_page(message))

    # -- pages ---------------------------------------------------------

    def _overview(self) -> str:
        counts = self.vault.counts()
        card = self.vault.card()
        stats = "".join(
            f"<div class=stat><b>{counts.get(t, 0)}</b><span>{esc(TABLE_TITLES[t])}</span></div>"
            for t in RECORD_TABLES)
        pending = counts.get("pending", 0)
        banner = ""
        if pending:
            banner = (f"<div class=warn>{pending} imported item"
                      f"{'s' if pending != 1 else ''} waiting for you to confirm "
                      f"— <a href='/review'>review now</a>. Nothing is added to "
                      f"your record until you approve it.</div>")
        body = [f"<div class=card>{profile_header(self.vault.profile)}</div>",
                banner,
                f"<div class=grid style='margin-bottom:18px'>{stats}</div>",
                f"<div class=card><h2>Emergency card preview</h2>"
                f"<pre class=card-text>{esc(card.text)}</pre>"
                f"<p class=note style='margin-top:10px'>{card.length} of "
                f"{card.budget} characters"
                + (f" · {card.dropped_total} item(s) left off to keep the QR readable"
                   if card.truncated else "") +
                f" · <a href='/card'>open the card</a></p></div>"]
        return page("HealthVault", "".join(body), nav_html("/"))

    def _record(self) -> str:
        profile = self.vault.profile
        fields = "".join(
            f"<div class=field><label>{esc(label)}</label>"
            f"<input name='{key}' value='{esc(profile[key])}'></div>"
            for key, label in PROFILE_FIELDS)
        donor = "checked" if profile["organ_donor"] else ""
        blocks = [
            f"<div class=card><h2>About you</h2><form method=post action='/profile'>"
            f"<div class=grid>{fields}</div>"
            f"<p><label style='display:inline'><input type=checkbox name=organ_donor "
            f"{donor} style='width:auto'> Organ donor</label></p>"
            f"<button>Save</button></form></div>"]

        for table in ("emergency_contact",) + RECORD_TABLES:
            if table == "document":
                continue
            title = ("Emergency contacts" if table == "emergency_contact"
                     else TABLE_TITLES.get(table, table))
            rows = store.rows(self.vault.conn, table)
            if table == "emergency_contact":
                listing = self._contacts_table(rows)
            else:
                listing = table_html(table, rows, actions=self._row_actions)
            inputs = "".join(
                f"<div class=field><label>{esc(k.replace('_', ' '))}</label>"
                f"<input name='{k}'></div>" for k in ADD_FIELDS.get(table, []))
            blocks.append(
                f"<div class=card id='{table}'><h2>{esc(title)}</h2>{listing}"
                f"<details style='margin-top:12px'><summary class=note>Add an entry"
                f"</summary><form method=post action='/record/{table}/add' "
                f"style='margin-top:10px'><div class=grid>{inputs}</div>"
                f"<p><label style='display:inline'><input type=checkbox name=on_card "
                f"style='width:auto'> Show on the emergency card</label></p>"
                f"<button>Add</button></form></details></div>")
        return page("Record", "".join(blocks), nav_html("/record"))

    def _contacts_table(self, rows) -> str:
        if not rows:
            return "<p class=note>No emergency contacts yet.</p>"
        body = "".join(
            f"<tr><td>{esc(r['name'])}</td><td>{esc(r['relationship'])}</td>"
            f"<td>{esc(r['phone'])}</td>"
            f"<td class=noprint>{self._row_actions('emergency_contact', r)}</td></tr>"
            for r in rows)
        return (f"<table><thead><tr><th>Name</th><th>Relationship</th>"
                f"<th>Phone</th><th></th></tr></thead><tbody>{body}</tbody></table>")

    @staticmethod
    def _row_actions(table: str, row) -> str:
        on_card = "on_card" in row.keys() and row["on_card"]
        label = "On card" if on_card else "Off card"
        style = "" if on_card else " class=sec"
        return (f"<form method=post action='/record/{table}/{row['id']}/card' "
                f"style='display:inline'><button{style} "
                f"style='padding:2px 8px;font-size:12px'>{label}</button></form> "
                f"<form method=post action='/record/{table}/{row['id']}/delete' "
                f"style='display:inline'><button class=sec "
                f"style='padding:2px 8px;font-size:12px'>Delete</button></form>")

    def _review(self) -> str:
        rows = store.pending(self.vault.conn)
        if not rows:
            body = ("<div class=card><h2>Review queue</h2><p class=note>"
                    "Nothing waiting. Imported items appear here first — they are "
                    "never written into your record automatically.</p></div>")
            return page("Review", body, nav_html("/review"))
        items = []
        for row in rows:
            payload = json.loads(row["payload"])
            summary = ", ".join(f"<b>{esc(k)}</b> {esc(v)}"
                                for k, v in payload.items() if v and k != "text")
            confidence = int(row["confidence"] * 100)
            items.append(
                f"<tr><td><span class=pill>{esc(TABLE_TITLES.get(row['table_name'], row['table_name']))}"
                f"</span></td><td>{summary}<div class=note style='margin-top:4px'>"
                f"{esc(row['reason'])} · source: {esc(row['source_label'])} · "
                f"{confidence}% confident</div></td>"
                f"<td class=noprint><form method=post "
                f"action='/review/{row['id']}/approve' style='display:inline'>"
                f"<button style='padding:3px 10px;font-size:12px'>Add</button></form> "
                f"<form method=post action='/review/{row['id']}/reject' "
                f"style='display:inline'><button class=sec "
                f"style='padding:3px 10px;font-size:12px'>Discard</button></form></td></tr>")
        body = (f"<div class=card><h2>Review queue — {len(rows)} waiting</h2>"
                f"<p class=note style='margin-top:-6px'>Imported facts are guesses "
                f"until you confirm them. Check each against what you know before "
                f"adding it.</p><table><tbody>{''.join(items)}</tbody></table>"
                f"<form method=post action='/review/reject-all' style='margin-top:14px'>"
                f"<button class=sec>Discard all</button></form></div>")
        return page("Review", body, nav_html("/review"))

    def _import_page(self, message: str = "") -> str:
        cfg = self.vault.config.email
        configured = bool(cfg.host and cfg.user and cfg.resolved_password())
        banner = f"<div class=warn>{esc(message)}</div>" if message else ""
        options = "".join(f"<option value='{t}'>{esc(TABLE_TITLES[t])}</option>"
                          for t in RECORD_TABLES)
        email_block = (
            f"<form method=post action='/email-scan'><button>Scan "
            f"{esc(cfg.user)}</button> <span class=note>Read-only: never sends, "
            f"moves or deletes anything.</span></form>" if configured else
            "<p class=note>Not configured. Add your IMAP host and username to "
            "<code>config.json</code>, then put an app-password in the "
            "<code>HEALTHVAULT_EMAIL_PASSWORD</code> environment variable.</p>")
        body = (
            f"{banner}"
            f"<div class=card><h2>Import a file</h2>"
            f"<form method=post action='/import' enctype='multipart/form-data'>"
            f"<div class=field><label>File</label><input type=file name=file></div>"
            f"<div class=field><label>If it is a plain list, what of?</label>"
            f"<select name=as><option value=''>Work it out automatically</option>"
            f"{options}</select></div><button>Import</button></form>"
            f"<p class=note style='margin-top:12px'>Understands FHIR bundles "
            f"(.json), medical-aid claim statements (.csv), Apple Health exports "
            f"(export.xml), plain lists (.csv) and documents (.pdf, .txt).</p></div>"
            f"<div class=card><h2>Scan your email</h2>{email_block}</div>"
            f"<div class=card><h2>Export</h2><p class=note>A single self-contained "
            f"HTML file of the whole record, for your own backup.</p>"
            f"<a class='btn sec' href='/export.html'>Download record</a></div>")
        return page("Import", body, nav_html("/import"))

    def _card_page(self) -> str:
        card = self.vault.card()
        try:
            symbol = qr.card_svg(card.text)
            code, level = qr.for_card(card.text)
            note = (f"QR version {code.version}, error level {level}. "
                    + ("Comfortably scannable." if code.version <= qr.CARD_MAX_VERSION
                       else f"Above version {qr.CARD_MAX_VERSION} some scanners "
                            f"struggle — consider trimming the card."))
        except qr.QRUnavailable as exc:
            symbol, note = f"<p class=warn>{esc(exc)}</p>", ""
        except qr.PayloadTooLarge as exc:
            symbol, note = f"<p class=warn>{esc(exc)}</p>", ""
        dropped = ""
        if card.truncated:
            dropped = (f"<p class=note>{card.dropped_total} item(s) were left off "
                       f"to keep the code scannable. Raise <code>card_budget</code> "
                       f"in config.json, or take items off the card on the "
                       f"<a href='/record'>Record</a> page.</p>")
        body = (
            f"<div class=card><h2>Emergency card</h2>"
            f"<p class=note style='margin-top:-6px'>Anyone holding this code can "
            f"read it — that is the point, and the reason only what you tick as "
            f"<em>on card</em> appears. Your full history is never in here.</p>"
            f"<div class=qr>{symbol}"
            f"<div style='flex:1;min-width:280px'>"
            f"<pre class=card-text>{esc(card.text)}</pre>"
            f"<p class=note>{card.length}/{card.budget} characters. {esc(note)}</p>"
            f"{dropped}"
            f"<p><a class=btn href='/card/print' target=_blank>Print wallet card "
            f"&amp; fridge sheet</a> <a class='btn sec' href='/card/qr.svg' "
            f"download='emergency-qr.svg'>Download QR</a></p></div></div></div>")
        return page("Emergency card", body, nav_html("/card"))

    def _shares(self) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        new_token = query.get("new", [""])[0]
        highlight = ""
        if new_token:
            url = self.vault.config.share_url(new_token)
            highlight = (
                f"<div class=card><h2>New share</h2><div class=qr>"
                f"<img src='/share-qr/{esc(new_token)}.svg' width=210 height=210 "
                f"alt='QR code for this share'>"
                f"<div style='flex:1;min-width:260px'><p>Show this code to the "
                f"practitioner, or send them:</p><pre class=card-text>{esc(url)}</pre>"
                f"<p class=note>If they are scanning from another device, the "
                f"address above must be reachable from it — set "
                f"<code>share_base_url</code> and bind to your LAN address.</p>"
                f"</div></div></div>")
        rows = share.active(self.vault.conn)
        if rows:
            listing = []
            now = datetime.now(timezone.utc)
            for row in rows:
                # Probe the state only; a PIN-locked share would otherwise
                # always report "needs_pin" rather than active/expired.
                probe = dict(row)
                probe["pin_hash"] = ""
                decision = share.check(probe, at=now)
                state = ("revoked" if row["revoked"] else
                         "expired" if decision.reason == "expired" else
                         "used up" if decision.reason == "exhausted" else "active")
                pill = "pill bad" if state != "active" else "pill"
                views = f"{row['views']}" + (f"/{row['max_views']}" if row["max_views"] else "")
                listing.append(
                    f"<tr><td>{esc(row['label'] or '—')}</td>"
                    f"<td>{esc(share.SCOPES.get(row['scope'], ('?',))[0])}</td>"
                    f"<td>{esc((row['expires_at'] or 'never')[:16].replace('T', ' '))}</td>"
                    f"<td>{esc(views)}</td>"
                    f"<td>{'PIN' if row['pin_hash'] else '—'}</td>"
                    f"<td><span class='{pill}'>{state}</span></td>"
                    f"<td class=noprint>" +
                    (f"<form method=post action='/shares/{esc(row['token'])}/revoke'>"
                     f"<button class=sec style='padding:2px 8px;font-size:12px'>"
                     f"Revoke</button></form>" if state == "active" else "") +
                    f"</td></tr>")
            table = (f"<table><thead><tr><th>Label</th><th>Shows</th><th>Expires</th>"
                     f"<th>Views</th><th>PIN</th><th></th><th></th></tr></thead>"
                     f"<tbody>{''.join(listing)}</tbody></table>"
                     f"<form method=post action='/shares/revoke-all' "
                     f"style='margin-top:12px'><button class=sec>Revoke every "
                     f"share</button></form>")
        else:
            table = "<p class=note>No shares yet.</p>"
        scopes = "".join(
            f"<option value='{key}'>{esc(label)}</option>"
            for key, (label, _) in share.SCOPES.items())
        form = (
            f"<div class=card><h2>Share with a practitioner</h2>"
            f"<form method=post action='/shares/new'><div class=grid>"
            f"<div class=field><label>Label (who is it for?)</label>"
            f"<input name=label placeholder='Dr Moodley, first visit'></div>"
            f"<div class=field><label>Show them</label><select name=scope>{scopes}"
            f"</select></div>"
            f"<div class=field><label>Expires after (hours, 0 = never)</label>"
            f"<input name=hours value='{self.vault.config.share_default_hours}'></div>"
            f"<div class=field><label>PIN (optional)</label>"
            f"<input name=pin placeholder='4-8 digits you say out loud'></div>"
            f"<div class=field><label>Max views (0 = unlimited)</label>"
            f"<input name=max_views value='0'></div></div>"
            f"<button>Create share</button></form></div>")
        return page("Shares", highlight + form +
                    f"<div class=card><h2>Existing shares</h2>{table}</div>",
                    nav_html("/shares"))

    def _export(self) -> str:
        return page("Health record",
                    f"<div class=card>{profile_header(self.vault.profile)}</div>"
                    + record_html(self.vault.conn)
                    + f"<div class=card>{footer_note()}</div>")

    # -- the practitioner view ----------------------------------------

    def _share_view(self, token: str, pin: str) -> None:
        token = token.split("/")[0]
        row = share.find(self.vault.conn, token)
        decision = share.check(row, pin)
        if row is not None:
            share.record_access(self.vault.conn, row["id"], self.client_ip,
                                decision.ok, decision.reason)
        if not decision.ok:
            if decision.reason == "needs_pin":
                body = (f"<div class=card><h2>Protected record</h2>"
                        f"<form method=post action='/s/{esc(token)}'>"
                        f"<div class=field><label>PIN</label>"
                        f"<input name=pin autofocus inputmode=numeric></div>"
                        f"<button>Open</button></form></div>")
                return self._send(page("Enter PIN", body), status=401)
            return self._send(
                page("Unavailable", f"<div class=card><h2>Not available</h2>"
                                    f"<p>{esc(decision.message)}</p>"
                                    f"<p class=note>Ask the patient for a new "
                                    f"link.</p></div>"), status=410)
        tables = share.tables_for(row["scope"])
        expires = (row["expires_at"] or "")[:16].replace("T", " ")
        heading = (profile_header(self.vault.profile) +
                   f"<p class=note style='margin-top:8px'>Shared by the patient"
                   + (f" · access expires {esc(expires)} UTC" if expires else "")
                   + f" · showing: {esc(share.SCOPES[row['scope']][0].lower())}</p>")
        body = (record_html(self.vault.conn, tables, heading=heading)
                + f"<div class=card>{footer_note()}</div>")
        self._send(page("Shared health record", body))


def serve(vault: Vault, host: str = "", port: int = 0, open_browser: bool = True):
    """Run the UI until interrupted."""
    host = host or vault.config.host
    port = port or vault.config.port
    Handler.vault = vault
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port}"
    print(f"HealthVault is running at {url}")
    if not is_loopback(host) and host != "":
        print("  Bound beyond this machine: share links work on your network,\n"
              "  and the management pages stay refused to everyone but you.")
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return httpd
