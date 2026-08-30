"""The web layer's job is to keep two audiences apart: you on loopback,
and a practitioner holding one share link."""

import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from healthvault import share, store, webui
from healthvault.config import Config
from healthvault.vault import Vault


@pytest.fixture
def server(tmp_path):
    config = Config(db_path=str(tmp_path / "web.db"),
                    documents_dir=str(tmp_path / "docs"))
    vault = Vault(config, db_path=config.db_path)
    store.save_profile(vault.conn, dict(full_name="Aven Naidu", blood_type="O+"))
    store.insert(vault.conn, "allergy", dict(substance="Penicillin",
                                             severity="life-threatening"))
    store.insert(vault.conn, "document", dict(title="Scan report", kind="imaging"))
    webui.Handler.vault = vault
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
    config.port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", vault
    httpd.shutdown()
    httpd.server_close()
    vault.close()


def get(url, data=None, headers=None):
    request = urllib.request.Request(
        url, data=data.encode() if data else None, headers=headers or {})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


@pytest.mark.parametrize("path", ["/", "/record", "/review", "/import",
                                  "/card", "/shares", "/export.html"])
def test_management_pages_serve_on_loopback(server, path):
    base, _ = server
    status, _ = get(base + path)
    assert status == 200


def test_share_requires_its_pin(server):
    base, vault = server
    token = share.create(vault.conn, scope="summary", pin="4821")
    status, body = get(f"{base}/s/{token}")
    assert status == 401
    assert "Penicillin" not in body                 # nothing leaks before the PIN
    status, body = get(f"{base}/s/{token}", data="pin=4821")
    assert status == 200 and "Penicillin" in body


def test_wrong_pin_is_refused(server):
    base, vault = server
    token = share.create(vault.conn, pin="4821")
    status, body = get(f"{base}/s/{token}", data="pin=0000")
    assert status == 410 and "Penicillin" not in body


def test_unknown_token_gives_nothing_away(server):
    base, _ = server
    status, body = get(f"{base}/s/notarealtoken")
    assert status == 410
    assert "Penicillin" not in body


def test_scope_is_enforced_on_the_shared_page(server):
    base, vault = server
    summary = share.create(vault.conn, scope="summary")
    _, body = get(f"{base}/s/{summary}")
    assert "Penicillin" in body
    assert "Scan report" not in body                # documents are out of scope
    full = share.create(vault.conn, scope="full")
    _, body = get(f"{base}/s/{full}")
    assert "Scan report" in body


def test_revoked_share_stops_working(server):
    base, vault = server
    token = share.create(vault.conn)
    assert get(f"{base}/s/{token}")[0] == 200
    share.revoke(vault.conn, token)
    assert get(f"{base}/s/{token}")[0] == 410


def test_every_view_is_logged(server):
    base, vault = server
    token = share.create(vault.conn)
    get(f"{base}/s/{token}")
    row = share.find(vault.conn, token)
    assert len(share.access_log(vault.conn, row["id"])) == 1


def test_cross_site_posts_are_refused(server):
    base, _ = server
    status, _ = get(f"{base}/shares/new", data="label=x",
                    headers={"Origin": "http://evil.example",
                             "Content-Type": "application/x-www-form-urlencoded"})
    assert status == 403


def test_same_origin_posts_are_allowed(server):
    base, vault = server
    host = base.replace("http://", "")
    status, _ = get(f"{base}/shares/new", data="label=Dr+Moodley&scope=summary&hours=24",
                    headers={"Origin": base, "Host": host,
                             "Content-Type": "application/x-www-form-urlencoded"})
    assert status in (200, 303)
    assert any(r["label"] == "Dr Moodley" for r in share.active(vault.conn))


def test_a_non_loopback_client_gets_no_management_pages():
    """Binding to the LAN must expose shares and nothing else."""
    assert webui.is_loopback("127.0.0.1")
    assert webui.is_loopback("::1")
    assert not webui.is_loopback("192.168.1.20")
    assert not webui.is_loopback("not-an-ip")


def test_unknown_pages_are_404(server):
    base, _ = server
    assert get(base + "/nope")[0] == 404
