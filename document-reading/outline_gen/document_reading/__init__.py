"""Independent, source-aware DOCX reading pipeline."""

from .config import ReadingConfig
from .pipeline import read_document

__all__ = ["ReadingConfig", "read_document"]
