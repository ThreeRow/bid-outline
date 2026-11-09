"""Frozen obligations and conservative routing for budget-limited repairs."""
from dataclasses import dataclass

from .outline import descendants


@dataclass
class RepairScope:
    root_id: str
    owner_id: str
    requirements: list
    assignments: list
    constraints: list
    findings: list
    inherited_ids: set

    def subdivide(self, nodes):
        """Route only evidenced local work; retain unlocatable parent findings."""
        children = [n for n in nodes if n.parent_id == self.root_id]
        scopes = {c.node_id: {n.node_id for n in descendants(nodes, c.node_id)} for c in children}
        assigned = {key: {a.requirement_id for a in self.assignments
                          if any(t['node_id'] in scope for t in a.targets)}
                    for key, scope in scopes.items()}
        parent_ids = {r.requirement_id for r in self.requirements} - set().union(*assigned.values())
        inherited = self.inherited_ids | parent_ids
        routed = {key: [] for key in scopes}
        blocked = []
        for finding in self.findings:
            # Cross-branch comparisons and changes to the parent itself need
            # their original joint scope; child replacements cannot settle them.
            if finding.kind == 'cross_branch' or self.root_id in finding.node_ids:
                blocked.append(finding)
                continue
            # Explicit local nodes take precedence over broad requirement links.
            targets = [key for key, scope in scopes.items() if set(finding.node_ids) & scope]
            if not targets and not finding.node_ids:
                targets = [key for key in scopes if set(finding.requirement_ids) & assigned[key]]
            if not targets:
                blocked.append(finding)
            for key in targets:
                routed[key].append(finding)
        tasks = []
        for key, findings in routed.items():
            if not findings:
                continue
            required = assigned[key] | inherited | {rid for f in findings for rid in f.requirement_ids}
            tasks.append(RepairScope(key, self.owner_id,
                                    [r for r in self.requirements if r.requirement_id in required],
                                    self.assignments, self.constraints, findings,
                                    required - assigned[key]))
        return tasks, blocked


def repair_waves(entries, base):
    """Stable conflict partition; all waves read the same frozen base."""
    def conflicts(a, b):
        ra, _, sa, *_ = entries[a]
        rb, _, sb, *_ = entries[b]
        if sa & sb or {f.finding_id for f in ra.findings} & {f.finding_id for f in rb.findings}:
            return True
        for constraint in base.constraints:
            touched = set(constraint.affected_node_ids)
            if constraint.parent_node_id:
                touched.add(constraint.parent_node_id)
            if touched & sa and touched & sb:
                return True
        return False
    waves = []
    for index in range(len(entries)):
        for wave in waves:
            if not any(conflicts(index, prior) for prior in wave):
                wave.append(index)
                break
        else:
            waves.append([index])
    return waves
