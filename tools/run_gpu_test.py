"""Compatibility entry point; canonical launcher is scripts/run_gpu_test.py."""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    sys.argv.insert(1, "start")
    runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/run_gpu_test.py"), run_name="__main__")
