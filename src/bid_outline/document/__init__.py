"""Frozen document adaptation, ownership-aware chunks and lexical retrieval."""

from .readers import read_document
from .chunks import chunk_document, split_chunk
from .lookup import keyword_candidates, lookup_references
