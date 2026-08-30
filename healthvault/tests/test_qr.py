"""The QR must actually decode - verified against a real decoder."""

import pytest

from healthvault import qr

cv2 = pytest.importorskip("cv2", reason="opencv-python-headless not installed")

from datetime import date                                    # noqa: E402

from healthvault import emergency, store                     # noqa: E402
from tests.test_emergency import card_for                    # noqa: E402


def decode(text, tmp_path, name="q.png"):
    code, _ = qr.for_card(text)
    path = tmp_path / name
    code.save(str(path), scale=10, border=4)
    decoded, _, _ = cv2.QRCodeDetector().detectAndDecode(cv2.imread(str(path)))
    return decoded


def test_card_round_trips_through_a_real_decoder(populated, tmp_path):
    card = card_for(populated)
    assert decode(card.text, tmp_path) == card.text


def test_card_stays_within_the_scannable_version(populated, tmp_path):
    """Regression guard: a payload that pushes past CARD_MAX_VERSION is
    exactly what stopped decoding during development."""
    code, _ = qr.for_card(card_for(populated).text)
    assert code.version <= qr.CARD_MAX_VERSION


def test_a_full_budget_card_still_decodes(populated, tmp_path):
    for extra in range(12):
        store.insert(populated, "medication",
                     dict(name=f"Medicine {extra}", dose="500 mg",
                          frequency="daily", on_card=1))
    card = card_for(populated)
    code, _ = qr.for_card(card.text)
    assert code.version <= qr.CARD_MAX_VERSION
    assert decode(card.text, tmp_path) == card.text


def test_share_url_round_trips(tmp_path):
    url = "http://192.168.1.20:8137/s/3JfavMGVEQ1JLH17JXW3VA"
    assert decode(url, tmp_path) == url


def test_oversized_payload_is_refused_not_silently_truncated():
    with pytest.raises(qr.PayloadTooLarge):
        qr.make("x" * 4000, error="M")
