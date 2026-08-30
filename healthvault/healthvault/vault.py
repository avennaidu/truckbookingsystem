"""The facade the CLI and the web UI both talk to.

Holds the config, the connection and the small number of operations that
are more than one call - chiefly "build the emergency card", which has
to gather five tables and apply the budget.
"""

import sqlite3
import threading
from datetime import date
from pathlib import Path

from . import db, emergency, share, store
from .config import Config
from .emergency import Card
from .ingest import import_file
from .ingest.email_scan import EmailScanner


class Vault:
    """Config plus a connection, with the record-level operations.

    The web UI is threaded, and a SQLite connection belongs to the thread
    that created it, so connections are thread-local and opened on demand.
    WAL mode (set in `db.connect`) lets those connections read while
    another writes. A `:memory:` path therefore gives each thread its own
    empty database - fine for tests, never used by the server.
    """

    def __init__(self, config: Config | None = None, db_path: str | Path | None = None):
        self.config = config or Config.load()
        self.db_path = str(db_path or self.config.db_path)
        self._local = threading.local()
        self.conn                      # open now, so a bad path fails here

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = db.connect(self.db_path)
            self._local.conn = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # -- the card ------------------------------------------------------

    def card(self, budget: int | None = None, today: date | None = None) -> Card:
        """Assemble the emergency card from the rows flagged `on_card`."""
        return emergency.build(
            store.get_profile(self.conn),
            store.rows(self.conn, "allergy", "on_card = 1 AND status != 'resolved'"),
            store.rows(self.conn, "condition", "on_card = 1 AND status != 'resolved'"),
            store.rows(self.conn, "medication", "on_card = 1 AND status != 'stopped'"),
            store.rows(self.conn, "emergency_contact", "on_card = 1"),
            budget=budget if budget is not None else self.config.card_budget,
            today=today,
        )

    # -- imports -------------------------------------------------------

    def import_path(self, path: Path | str, table: str = ""):
        return import_file(self.conn, path, table)

    def scan_email(self, dry_run: bool = False):
        cfg = self.config.email
        password = cfg.resolved_password()
        if not (cfg.host and cfg.user and password):
            raise ValueError(
                "Email is not configured. Set host, user and either a password "
                "in config.json or the HEALTHVAULT_EMAIL_PASSWORD environment "
                "variable (use a provider app-password, not your login).")
        scanner = EmailScanner(
            host=cfg.host, port=cfg.port, user=cfg.user, password=password,
            mailbox=cfg.mailbox, documents_dir=Path(self.config.documents_dir),
            senders=cfg.senders or None, subjects=cfg.subjects or None)
        return scanner.scan(self.conn, since_days=cfg.since_days,
                            limit=cfg.limit, dry_run=dry_run)

    # -- shares --------------------------------------------------------

    def new_share(self, label: str = "", scope: str = "", hours: int | None = None,
                  pin: str = "", max_views: int = 0) -> tuple[str, str]:
        token = share.create(
            self.conn, label=label,
            scope=scope or self.config.share_default_scope,
            hours=self.config.share_default_hours if hours is None else hours,
            pin=pin, max_views=max_views)
        return token, self.config.share_url(token)

    # -- convenience ---------------------------------------------------

    @property
    def profile(self):
        return store.get_profile(self.conn)

    def counts(self) -> dict[str, int]:
        return store.counts(self.conn)
