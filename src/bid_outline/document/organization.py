"""Route candidate material to its keyword pool before planning model inputs."""
import hashlib
import json

from .locate import LocatedRange, RULES, SUBMISSION, normalized

VERSION = 'category-routing-1'


def route_organization(document, candidates):
    """Prefer direct pool evidence over neighboring selection and generic submission.

    C is supplementary material outside identified A/B positions. A generic
    submission expression alone must not replicate a known A/B item into C.
    Multiple direct A/B matches, or unresolved neighboring-pool matches, remain
    explicit multi-category candidates rather than defaulting everything to ABC.
    """
    memberships = {}
    for candidate in candidates:
        for element in candidate.document.elements:
            memberships.setdefault(element.element_id, set()).add(candidate.category)
    routes = {}
    for element in sorted(document.elements, key=lambda e: e.order):
        categories = memberships.get(element.element_id)
        if not categories:
            continue
        path, text = normalized(' / '.join(element.section_path)), normalized(element.text)
        hits = {category: ['章节:' + word for word in words if word in path] +
                          ['正文:' + word for word in words if word in text]
                for category, words in RULES.items()}
        if SUBMISSION.search(text):
            hits['C'] = ['正文:提交或方案内容线索']
        specific = {category for category in 'AB' if hits[category] and category in categories}
        if specific:
            targets = specific
            basis = 'matched_pool' if len(targets) == 1 else 'multiple_matched_pools'
        elif hits.get('C') and 'C' in categories:
            targets, basis = {'C'}, 'submission_pool'
        else:
            targets = categories
            basis = 'neighbor_pool' if len(targets) == 1 else 'ambiguous_neighbor_pools'
        routes[element.element_id] = {
            'element_id': element.element_id, 'candidate_categories': sorted(categories),
            'target_categories': sorted(targets), 'basis': basis,
            'pool_hits': {key: value for key, value in hits.items() if value},
            'suppressed_categories': sorted(categories - targets),
        }
    output, inputs = [], []
    for candidate in candidates:
        elements = [e for e in candidate.document.elements
                    if candidate.category in routes[e.element_id]['target_categories']]
        if not elements:
            continue
        payload = [VERSION, candidate.range_id, candidate.category, [e.element_id for e in elements]]
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()[:20]
        routed = LocatedRange('organization_' + digest, candidate.category,
            candidate.document.model_copy(update={'elements': elements}), candidate.reasons)
        output.append(routed)
        inputs.append({**routed.record(), 'candidate_range_id': candidate.range_id,
                       'task_type': 'N01-' + candidate.category})
    return output, {'version': VERSION, 'routes': list(routes.values()), 'inputs': inputs}
