"""One-document parser process; never runs model requests or persists job state."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

from .models import payload


def resource_snapshot():
    status = Path("/proc/self/status")
    if not status.exists():
        return {"pid": os.getpid()}
    fields = dict(line.split(":", 1) for line in status.read_text().splitlines() if ":" in line)
    return {"pid": os.getpid(), **{key: fields[key].strip() for key in (
        "VmSize", "VmPeak", "VmRSS", "VmHWM", "Threads",
    )}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    started = time.monotonic()
    before = resource_snapshot()
    from .docling_reader import _read_docling_in_process
    parsed = _read_docling_in_process(args.input)
    parsed.resource_usage = {
        "execution": "isolated_process", "before": before,
        "after_conversion": resource_snapshot(), "native_threads_per_library": 1,
        "elapsed_seconds": time.monotonic() - started,
        "torch_loaded": "torch" in sys.modules,
    }
    with args.output.open("w", encoding="utf-8") as output:
        json.dump(payload(parsed), output, ensure_ascii=False)


if __name__ == "__main__":
    main()
