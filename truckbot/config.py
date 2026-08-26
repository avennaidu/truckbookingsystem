"""Load and validate config.json.

Credentials (if present) stay in the local file only - never commit it.
The bot itself never types credentials: login is done by hand in the
debug Chrome (attach mode). They are kept in config only for reference.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import DEFAULT_GATE_LABELS, VALID_TOWERS

DEFAULT_CONFIG_FILE = Path("config.json")


@dataclass
class Notify:
    toast: bool = True                 # Windows desktop toast on BOOKED
    email_to: str = ""                 # empty = email off
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""


@dataclass
class Config:
    url: str = "https://n4dgt.ictsi.net/apex/n4.zul"
    # N4 credentials for auto-login (LOCAL config.json only - gitignored).
    # Leave blank to keep pure attach mode (log in by hand).
    username: str = ""
    password: str = ""
    # auto-launch of the debug Chrome when none is running; chrome_path
    # blank = auto-detect, user_data_dir blank = per-tower default
    auto_launch: bool = True
    chrome_path: str = ""
    user_data_dir: str = ""
    tower: str = "109"
    tower_order: list = field(default_factory=lambda: list(VALID_TOWERS))
    transaction_type: str = "Pick Up Import"
    # choices offered by the UI; None/blank at run time = leave the
    # Transaction Type combobox exactly as hand-set in N4
    transaction_types: list = field(default_factory=lambda: [
        "Pick Up Import", "Drop Off Export"])
    trucking_company: str = "AVEMEL"
    date_format: str = "%Y-%m-%d"
    days_ahead: int = 0                # also try today+1..+N when today has nothing
    poll_seconds: int = 20             # pause between passes (keep polite)
    max_hours: float = 6.0
    refresh_wait_ms: int = 700         # server wait after date poke; lower = faster but stale-risk
    debug_url: str = "http://localhost:9222"
    # one Chrome debug session per tower (concurrent logins are allowed,
    # so the fastest setup is a separate bot per tower)
    debug_ports: dict = field(default_factory=lambda: {
        "109": 9222, "202": 9223, "203": 9224, "205": 9225})
    gate_labels: dict = field(default_factory=lambda: dict(DEFAULT_GATE_LABELS))
    containers_file: str = "containers_all.csv"
    results_file: str = "results.csv"
    errors_file: str = "n4_errors.csv"  # verbatim N4 error capture
    log_file: str = "bot.log"
    notify: Notify = field(default_factory=Notify)

    def gate_label(self, tower: str) -> str:
        return self.gate_labels.get(tower, tower)

    def debug_url_for(self, tower: str | None) -> str:
        """Per-tower CDP endpoint when running one bot per tower."""
        port = self.debug_ports.get(str(tower)) if tower else None
        return f"http://localhost:{port}" if port else self.debug_url


def load_config(path: Path | str = DEFAULT_CONFIG_FILE) -> Config:
    """Read config.json, tolerating extra keys (credentials etc.)."""
    path = Path(path)
    raw = json.loads(path.read_text()) if path.exists() else {}
    notify_raw = raw.get("notify", {}) or {}
    notify = Notify(**{k: v for k, v in notify_raw.items()
                       if k in Notify.__dataclass_fields__})
    cfg = Config(notify=notify)
    for k, v in raw.items():
        if k in ("notify",):
            continue
        if k in Config.__dataclass_fields__:
            setattr(cfg, k, v)
    cfg.tower = str(cfg.tower).strip()
    cfg.tower_order = [str(t).strip() for t in cfg.tower_order]
    # merge user labels over defaults so a partial gate_labels still works
    labels = dict(DEFAULT_GATE_LABELS)
    labels.update({str(k): v for k, v in (raw.get("gate_labels") or {}).items()})
    cfg.gate_labels = labels
    return cfg


def validate_tower(tower: str) -> str:
    tower = str(tower).strip()
    if tower not in VALID_TOWERS:
        raise ValueError(
            f"tower must be one of {sorted(VALID_TOWERS)} - got '{tower}'")
    return tower
