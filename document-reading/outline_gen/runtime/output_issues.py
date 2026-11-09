"""JSON-safe output issue context shared by model adapters and stage recovery."""
from pathlib import Path


def attach_output_context(error, **context):
    existing = getattr(error, 'output_context', {})
    error.output_context = {**existing, **context}
    return error


def issue_context(error):
    """Retain context across wrappers, including their original exception."""
    result = {}
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for key, value in getattr(current, 'output_context', {}).items():
            result.setdefault(key, value)
        current = getattr(current, 'output_error', None) or current.__cause__
    return result


def evidence_paths(raw_path, repair_raw_path=None):
    result = {}
    if raw_path:
        path = Path(raw_path)
        result['original_response'] = str(path.resolve())
        result['attempts'] = [str(p.resolve()) for p in sorted(path.parent.glob(path.stem + '_attempt_*'))]
    if repair_raw_path and Path(repair_raw_path).exists():
        result['model_repair_response'] = str(Path(repair_raw_path).resolve())
    return result
