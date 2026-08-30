"""Local settings (`config.json`), created on first run.

The file is gitignored. It holds the email connection details, and it is
the only place a password may live besides the environment - which is
the preferred place, since `config.json` is a file you might one day
copy somewhere careless.
"""

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_FILE = Path("config.json")

#: Preferred over storing the password in the file.
PASSWORD_ENV = "HEALTHVAULT_EMAIL_PASSWORD"


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = ""                 # imap.gmail.com, outlook.office365.com...
    port: int = 993
    user: str = ""
    password: str = ""             # leave blank and use HEALTHVAULT_EMAIL_PASSWORD
    mailbox: str = "INBOX"
    since_days: int = 365
    limit: int = 200
    senders: list[str] = field(default_factory=list)   # empty = built-in list
    subjects: list[str] = field(default_factory=list)

    def resolved_password(self) -> str:
        return os.environ.get(PASSWORD_ENV, "") or self.password


@dataclass
class Config:
    db_path: str = "healthvault.db"
    documents_dir: str = "documents"
    card_budget: int = 800
    share_default_hours: int = 24
    share_default_scope: str = "summary"
    #: Host and port the local UI binds to. 127.0.0.1 keeps the record off
    #: the network; change to 0.0.0.0 only if a practitioner must reach a
    #: share from another device on the same wifi.
    host: str = "127.0.0.1"
    port: int = 8137
    #: Base URL written into practitioner QR codes. Must be reachable by
    #: the device doing the scanning, so on a phone this is the machine's
    #: LAN address rather than localhost.
    share_base_url: str = ""
    email: EmailConfig = field(default_factory=EmailConfig)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_FILE) -> "Config":
        path = Path(path)
        if not path.exists():
            config = cls()
            config.save(path)
            return config
        data = json.loads(path.read_text(encoding="utf-8"))
        email = EmailConfig(**{k: v for k, v in (data.pop("email", {}) or {}).items()
                               if k in EmailConfig.__dataclass_fields__})
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(email=email, **known)

    def save(self, path: Path | str = DEFAULT_CONFIG_FILE) -> None:
        Path(path).write_text(json.dumps(asdict(self), indent=2) + "\n",
                              encoding="utf-8")

    def base_url(self) -> str:
        return (self.share_base_url or f"http://{self.host}:{self.port}").rstrip("/")

    def share_url(self, token: str) -> str:
        return f"{self.base_url()}/s/{token}"
