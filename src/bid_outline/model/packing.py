"""Stable packing by complete-request feasibility, never text truncation."""


def pack_items(items, fits):
    """Keep records intact. Oversized singletons are returned for explicit handling."""
    groups, current = [], []
    for item in items:
        candidate = current + [item]
        if current and not fits(candidate):
            groups.append(current)
            current = [item]
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups
