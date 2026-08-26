"""Booking notifications: Windows desktop toast + optional email.

No extra dependencies: the toast uses a PowerShell one-liner (with a
console-bell fallback on other platforms), email uses stdlib smtplib.
Failures never interrupt a run - a missed toast must not cost a slot.
"""

import logging
import platform
import smtplib
import subprocess
import sys
from email.message import EmailMessage

log = logging.getLogger("truckbot")

_PS_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@"
<toast><visual><binding template='ToastGeneric'>
<text>{title}</text><text>{body}</text>
</binding></visual></toast>
"@)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Truck Booking Bot").Show($toast)
"""


def _toast_windows(title: str, body: str):
    script = _PS_TOAST.replace("{title}", title).replace("{body}", body)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, timeout=15,
    )


def toast(title: str, body: str):
    try:
        if platform.system() == "Windows":
            _toast_windows(title, body)
        else:
            sys.stdout.write("\a")      # console bell fallback
            sys.stdout.flush()
    except Exception as e:
        log.debug("toast failed: %s", e)


def email(cfg_notify, subject: str, body: str):
    n = cfg_notify
    if not (n.email_to and n.smtp_host):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = n.smtp_user or n.email_to
        msg["To"] = n.email_to
        msg.set_content(body)
        with smtplib.SMTP(n.smtp_host, n.smtp_port, timeout=20) as s:
            s.starttls()
            if n.smtp_user:
                s.login(n.smtp_user, n.smtp_password)
            s.send_message(msg)
    except Exception as e:
        log.warning("email notification failed: %s", e)


class Notifier:
    """Engine event hook: fires on BOOKED and on run finish."""

    def __init__(self, cfg):
        self.cfg = cfg

    def __call__(self, kind, **data):
        n = self.cfg.notify
        if kind == "booked":
            title = "Slot BOOKED"
            body = f"{data.get('container')} - {data.get('detail', '')}"
            if n.toast:
                toast(title, body)
            email(n, f"[TruckBot] {title}: {data.get('container')}", body)
        elif kind == "finished":
            s = data.get("summary", {})
            body = (f"Run finished ({data.get('reason', '')}). "
                    f"Booked: {s.get('BOOKED', 0)}, "
                    f"Skipped: {s.get('SKIPPED', 0)}")
            if n.toast:
                toast("Truck Booking Bot", body)
            email(n, "[TruckBot] Run finished", body)
