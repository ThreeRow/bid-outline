"""读写与模板工具。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"文件为空: {path}")
    return text


def write_text(path: Path, text: str) -> None:
    _atomic_write_text(path, text if text.endswith("\n") else text + "\n")


def dumps_pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def write_json(path: Path, obj: Any) -> None:
    _atomic_write_text(path, dumps_pretty(obj))


def _atomic_write_text(path: Path, text: str) -> None:
    """Write one text file without exposing a partially written target.

    The temporary file is created beside the target so that ``os.replace`` is
    a same-filesystem rename.  It is flushed and fsynced before the replace;
    callers therefore observe either the previous complete file or the new
    complete file.  On failure only this invocation's explicitly named
    temporary file is removed.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fill_template(template: str, mapping: dict[str, str]) -> str:
    result = template
    for key, value in mapping.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def model_dir_name(model: str) -> str:
    return model.replace("/", "_")


def temp_tag(temperature: float) -> str:
    if temperature == int(temperature):
        return str(int(temperature))
    return str(temperature).replace(".", "")


def infer_case_id(tender_file: Path, case_id: str | None) -> str:
    if case_id:
        return case_id
    stem = tender_file.stem
    prefix = "招标信息"
    if stem.startswith(prefix) and len(stem) > len(prefix):
        return stem[len(prefix) :]
    return stem
