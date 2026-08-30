"""Get plain text out of the files a medical history arrives in.

PDF is the common case (lab reports, discharge summaries, scheme
statements) and needs `pypdf`. It is an optional dependency: without it
the file is still stored and catalogued, just not searched - losing the
text is far better than refusing the document.
"""

import hashlib
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:                                   # pragma: no cover
    PdfReader = None

TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".htm", ".html"}


def sha256_of(path: Path | str) -> str:
    """Content hash - how we notice the same lab report twice."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_text(path: Path | str, max_chars: int = 200_000) -> str:
    """Best-effort text. Returns '' when the format is not readable here."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(path, max_chars)
    if suffix in TEXT_SUFFIXES:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    return ""


def _pdf_text(path: Path, max_chars: int) -> str:
    if PdfReader is None:
        return ""
    try:
        reader = PdfReader(str(path))
    except Exception:
        # Encrypted or malformed PDFs are common in this domain; the
        # document is still worth keeping, so never let this raise.
        return ""
    out: list[str] = []
    total = 0
    for page in reader.pages:
        try:
            chunk = page.extract_text() or ""
        except Exception:
            continue
        out.append(chunk)
        total += len(chunk)
        if total >= max_chars:
            break
    return "\n".join(out)[:max_chars]


def pdf_supported() -> bool:
    return PdfReader is not None
