"""Turning files into text, and text into a guess about what it is."""

from .text import extract_text, sha256_of          # noqa: F401
from .classify import classify, find_observations  # noqa: F401
