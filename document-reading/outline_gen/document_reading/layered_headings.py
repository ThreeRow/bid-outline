"""Primary target selection and chapter-local secondary selection.

Both stages run bounded independent calls. Only the stage barrier establishes
chapter ranges; no model-selected deep hierarchy is generated.
"""

from __future__ import annotations
from outline_gen.runtime.tokenization import selected_tokenizer, set_thread_tokenizer


from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import re
import time

from .alignment import normalized
from .models import Candidate, HeadingGroup, payload
from .heading_candidates import CHAPTER, TOC_LINE
from .heading_inputs import excerpt, primary_input, secondary_input


@dataclass
class HeadingResult:
    candidates: list[Candidate]
    decisions: list[dict]
    audit: dict
    issues: list[dict]


def title_key(text):
    text = TOC_LINE.sub("", text.strip())
    return re.sub(r'[\s，,。:：、．.（）()“”"‘’]', "", text)


def short_title(text):
    return (
        bool(text.strip())
        and len(text.strip()) <= 90
        and not re.search(r"[。；;！？!?]", text)
    )


def pattern_match(text, patterns):
    return any(re.search(pattern, text.strip()) for pattern in patterns)


def primary_key(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text.strip())
        if match:
            # Numbered chapter identity allows a body title to have extra words.
            return title_key(match.group())
    return title_key(text)


def parse_selection(raw, candidates, single=False):
    text = raw.strip()
    if text == "无":
        return []
    tokens = [line.strip() for line in text.splitlines() if line.strip()]
    allowed = {int(c.id[1:]): c.id for c in candidates}
    if not tokens or any(not re.fullmatch(r"H\d+", token) for token in tokens):
        raise ValueError("每行只写给定候选ID；没有结果只写“无”")
    ids = [allowed.get(int(token[1:])) for token in tokens]
    if any(cid is None for cid in ids) or len(ids) != len(set(ids)):
        raise ValueError("候选ID未知或重复")
    if single and len(ids) != 1:
        raise ValueError("本次只选择一个一级标题位置，或写“无”")
    # The program restores source order; output order has no positional meaning.
    selected = set(ids)
    return [c.id for c in candidates if c.id in selected]


def toc_children(toc, index):
    if index is None:
        return []
    children = []
    for entry in toc[index + 1 :]:
        if entry["level"] <= 1:
            break
        if entry["level"] == 2:
            children.append(entry["title"])
    return children


def make_primary_groups(blocks, toc, toc_ids, config):
    groups = []
    for index, entry in enumerate(toc):
        if entry["level"] == 1:
            groups.append(
                HeadingGroup(f"P{len(groups)+1:04d}", entry["title"], [], index)
            )
    known = {primary_key(g.title, config.primary_patterns) for g in groups}
    for block in blocks:
        if block.kind != "paragraph" or block.id in toc_ids:
            continue
        if short_title(block.text) and pattern_match(
            block.text, config.primary_patterns
        ):
            key = primary_key(block.text, config.primary_patterns)
            if key not in known:
                groups.append(
                    HeadingGroup(f"P{len(groups)+1:04d}", block.text.strip(), [])
                )
                known.add(key)
    # Unnumbered Word/Docling headings at the outermost native level supplement
    # primary discovery; numbered default targets already provide that baseline.
    if not groups:
        native = []
        for block in blocks:
            if (
                block.kind != "paragraph"
                or block.id in toc_ids
                or not short_title(block.text)
            ):
                continue
            levels = [
                h.get("outline_level")
                for h in block.structure
                if h.get("source") == "word" and h.get("outline_level") is not None
            ]
            if levels:
                native.append((min(levels), block))
        if native:
            top = min(level for level, _ in native)
            for level, block in native:
                if level == top and title_key(block.text) not in known:
                    groups.append(
                        HeadingGroup(f"P{len(groups)+1:04d}", block.text.strip(), [])
                    )
                    known.add(title_key(block.text))
    for group in groups:
        key = primary_key(group.title, config.primary_patterns)
        for block in blocks:
            if block.kind != "paragraph":
                continue
            clean = TOC_LINE.sub("", block.text.strip())
            exact = title_key(clean) == title_key(group.title)
            numbered = (
                short_title(clean)
                and pattern_match(clean, config.primary_patterns)
                and primary_key(clean, config.primary_patterns) == key
            )
            reference = title_key(group.title) in title_key(clean) and len(clean) < 200
            if exact or numbered or reference:
                group.candidates.append(
                    Candidate(
                        "",
                        block.id,
                        block.text,
                        ["目录位置" if block.id in toc_ids else "正文匹配"],
                        target_title=TOC_LINE.sub("", group.title).strip(),
                    )
                )
    # Repeated TOC entries are independent targets. When their exact body
    # occurrences are unambiguous, allocate one local range per occurrence.
    # Otherwise preserve ambiguity; duplicate selections become explicit issues.
    positions = {b.id: i for i, b in enumerate(blocks)}
    for key in dict.fromkeys(title_key(g.title) for g in groups):
        same = [g for g in groups if title_key(g.title) == key]
        if len(same) < 2:
            continue
        anchors = sorted(
            {
                positions[c.block_id]
                for g in same
                for c in g.candidates
                if c.block_id not in toc_ids and title_key(c.text) == key
            }
        )
        if len(anchors) != len(same):
            continue
        for rank, g in enumerate(same):
            lower = anchors[rank - 1] + 1 if rank else 0
            upper = anchors[rank + 1] if rank + 1 < len(anchors) else len(blocks)
            own_toc = [
                b.id
                for b in blocks
                if b.id in toc_ids and title_key(TOC_LINE.sub("", b.text)) == key
            ]
            g.candidates = [
                c
                for c in g.candidates
                if (
                    c.block_id == own_toc[rank]
                    if c.block_id in toc_ids and rank < len(own_toc)
                    else lower <= positions[c.block_id] < upper
                    and c.block_id not in toc_ids
                )
            ]
    counter = 0
    for group in groups:
        for candidate in group.candidates:
            counter += 1
            candidate.id = f"H{counter:05d}"
    return groups


def context_for(candidate, blocks, positions, chars, *, start=0, end=None):
    return excerpt(candidate, blocks, positions, chars, start=start, end=end)


def secondary_form(text):
    text = text.strip()
    if re.match(r"^第[〇零一二三四五六七八九十百千万两\d]+节", text):
        return "section"
    if re.match(r"^[一二三四五六七八九十百]+[、．.]", text):
        return "chinese_list"
    if re.match(r"^[（(][一二三四五六七八九十百]+[)）]", text):
        return "chinese_parenthesis"
    if re.match(r"^\d+[、．.]\s*[^\d]", text):
        return "numeric_list"
    return None


def secondary_candidates(
    blocks, start, end, primary, directory, word_map, config, counter
):
    expected = {title_key(title) for title in directory}
    result = []
    parent_hints = [
        h.get("outline_level")
        for h in blocks[start].structure
        if h.get("source") == "word" and h.get("outline_level") is not None
    ]
    parent_level = min(parent_hints) if parent_hints else None
    # Use the document's direct-child numbering families to narrow the default
    # pool before calling the model. This avoids sending whole lower-level
    # clause lists simply because they also have numbers.
    native_forms = set()
    if parent_level is not None:
        for block in blocks[start + 1 : end]:
            if block.kind != "paragraph" or not short_title(block.text):
                continue
            if any(
                w.outline_level == parent_level + 1 for w in word_map.get(block.id, [])
            ):
                form = secondary_form(block.text)
                if form:
                    native_forms.add(form)
    if any(
        short_title(b.text) and secondary_form(b.text) == "section"
        for b in blocks[start + 1 : end]
        if b.kind == "paragraph"
    ):
        native_forms.add("section")
    for block in blocks[start + 1 : end]:
        if block.kind != "paragraph" or not short_title(block.text):
            continue
        text = block.text.strip()
        exact = title_key(text) in expected
        field = re.sub(r"^\s*[（(]?\s*(?:\d+[、.．]\s*)?", "", text)
        if not exact and re.match(
            r"^(?:备案编号|项目编号|项目名称|采购人|代理机构|投标人|联系人|联系电话|编制时间|所投采购包)[：:]\s*\S",
            field,
        ):
            continue
        # Names and numbering do not determine structural role. Explicit
        # directory or native heading evidence may nominate a form/attachment;
        # the chapter-local selector judges whether it is a direct subsection.
        evidence = []
        if exact:
            evidence.append("目录列出的直属小节")
        if (
            not directory
            and pattern_match(text, config.secondary_patterns)
            and (
                not native_forms
                or secondary_form(text) in native_forms
                or secondary_form(text) is None
            )
        ):
            evidence.append("章内原生形式对应的默认二级候选")
        for word in word_map.get(block.id, []):
            if word.outline_level is not None and (
                parent_level is None or word.outline_level == parent_level + 1
            ):
                evidence.append("Word大纲线索")
            elif (
                re.match(r"^(Heading|标题)\s*\d+", word.style, re.I)
                and parent_level is None
            ):
                evidence.append("Word标题样式")
        if not directory and any(
            h.get("source") == "docling"
            and h.get("label") in {"section_header", "title"}
            for h in block.structure
        ):
            evidence.append("Docling标题线索")
        if evidence:
            counter += 1
            result.append(
                Candidate(
                    f"H{counter:05d}",
                    block.id,
                    text,
                    list(dict.fromkeys(evidence)),
                    2 if exact else None,
                )
            )
    return result, counter


def recognize_layered(blocks, toc, toc_ids, word_map, calls):
    config = calls.config
    positions = {b.id: i for i, b in enumerate(blocks)}
    groups = make_primary_groups(blocks, toc, toc_ids, config)
    audit = {
        "strategy": "primary_single_secondary_multi_v1",
        "heading_workers": config.heading_workers,
        "primary_groups": payload(groups),
        "primary_calls": [],
        "secondary_calls": [],
    }
    issues = []
    if not groups:
        issues.append({"code": "primary_targets_not_found"})

    direct_primary = {}
    for g in groups:
        matches = [
            c
            for c in g.candidates
            if c.block_id not in toc_ids and title_key(c.text) == title_key(g.title)
        ]
        if g.toc_index is not None and len(matches) == 1:
            direct_primary[g.id] = matches[0]
    ordered = sorted(
        (g.toc_index, positions[direct_primary[g.id].block_id])
        for g in groups
        if g.id in direct_primary
    )
    if any(left[1] >= right[1] for left, right in zip(ordered, ordered[1:])):
        direct_primary = {}  # conflicting order requires inference, not acceptance

    def choose(group):
        if not group.candidates:
            return (
                group,
                None,
                {"code": "primary_target_unmatched", "title": group.title},
                [],
            )
        attempts = []

        def validate(raw):
            result = parse_selection(raw, group.candidates, True)
            if (
                result
                and next(c for c in group.candidates if c.id == result[0]).block_id
                in toc_ids
            ):
                raise ValueError("选中位置属于目录区域，请选择正文起点")
            return result

        if group.id in direct_primary:
            return (
                group,
                direct_primary[group.id],
                None,
                [
                    {
                        "action": "direct_unique_toc_match",
                        "selected": [direct_primary[group.id].id],
                    }
                ],
            )
        for factor in (1, 2):
            prompt = primary_input(
                group.title,
                group.candidates,
                blocks,
                positions,
                (config.heading_context_tokens) * factor,
            )
            try:
                result = calls.ask(
                    f"primary_{group.id}_context{factor}",
                    "document_primary_sys.md",
                    prompt,
                    validate,
                    max_tokens=256,
                )
                attempts.append({"context_factor": factor, "selected": result})
                if result:
                    candidate = next(c for c in group.candidates if c.id == result[0])
                    return group, candidate, None, attempts
                # A none normally ends the task. An exact, standalone body
                # title is specific contrary evidence, so recheck it once.
                strong = any(
                    c.block_id not in toc_ids
                    and short_title(c.text)
                    and (
                        title_key(c.text) == title_key(group.title)
                        or pattern_match(c.text, config.primary_patterns)
                    )
                    for c in group.candidates
                )
                if strong and factor == 1:
                    attempts.append({"action": "recheck_exact_body_match"})
                    continue
                return (
                    group,
                    None,
                    {"code": "primary_heading_unconfirmed", "title": group.title},
                    attempts,
                )
            except ValueError as exc:
                attempts.append({"context_factor": factor, "error": str(exc)})
        return (
            group,
            None,
            {"code": "primary_call_failed", "title": group.title},
            attempts,
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=config.heading_workers, initializer=set_thread_tokenizer, initargs=(selected_tokenizer(),)) as pool:
        primary_results = list(pool.map(choose, groups))
    audit["primary_wall_seconds"] = time.perf_counter() - started
    selected = []
    candidates = [c for g in groups for c in g.candidates]
    used = set()
    chosen = []
    for group, candidate, issue, records in primary_results:
        audit["primary_calls"].append({"group_id": group.id, "attempts": records})
        if issue:
            issues.append(issue)
        if candidate:
            if candidate.block_id in used:
                issues.append(
                    {
                        "code": "primary_duplicate_position",
                        "title": group.title,
                        "block_id": candidate.block_id,
                    }
                )
                continue
            used.add(candidate.block_id)
            selected.append({"id": candidate.id, "level": 1})
            chosen.append((group, candidate))
    toc_order = [
        (g.toc_index, positions[c.block_id])
        for g, c in chosen
        if g.toc_index is not None
    ]
    if any(a[1] >= b[1] for a, b in zip(sorted(toc_order), sorted(toc_order)[1:])):
        issues.append({"code": "primary_toc_order_conflict"})
    chosen.sort(key=lambda item: positions[item[1].block_id])
    tasks = []
    counter = len(candidates)
    for i, (group, candidate) in enumerate(chosen):
        start = positions[candidate.block_id]
        end = (
            positions[chosen[i + 1][1].block_id] if i + 1 < len(chosen) else len(blocks)
        )
        unresolved = [
            g
            for g in groups
            if g.id not in {item[0].id for item in chosen}
            and any(
                start < positions[c.block_id] < end
                and c.block_id not in toc_ids
                and short_title(c.text)
                for c in g.candidates
            )
        ]
        if unresolved:
            issues.append(
                {
                    "code": "secondary_range_unresolved",
                    "chapter": group.title,
                    "unconfirmed_targets": [g.title for g in unresolved],
                }
            )
            continue
        directory = toc_children(toc, group.toc_index)
        chapter_candidates, counter = secondary_candidates(
            blocks, start, end, candidate, directory, word_map, config, counter
        )
        candidates.extend(chapter_candidates)
        tasks.append((group, candidate, start, end, directory, chapter_candidates))

    def secondary(task):
        group, parent, start, end, directory, batch = task
        records, problems, decisions = [], [], []

        # Only a unique full match to an explicit directory entry bypasses
        # inference. A sole keyword/style candidate is not a confirmed heading.
        direct = []
        for title in directory:
            matched = [c for c in batch if title_key(c.text) == title_key(title)]
            if len(matched) == 1:
                direct.append(matched[0])
        if any(
            positions[left.block_id] >= positions[right.block_id]
            for left, right in zip(direct, direct[1:])
        ):
            direct = []
        direct_ids = {c.id for c in direct}
        decisions.extend({"id": c.id, "level": 2} for c in direct)
        if direct:
            records.append(
                {
                    "action": "direct_unique_toc_match",
                    "selected": [c.id for c in direct],
                }
            )

        def process(items, key, factor=1):
            if not items:
                return
            prompt = secondary_input(
                group.title,
                directory,
                items,
                blocks,
                positions,
                (config.secondary_context_tokens) * factor,
                start,
                end,
            )
            try:
                result = calls.ask(
                    key,
                    config.secondary_prompt,
                    prompt,
                    lambda raw: parse_selection(raw, items),
                    max_tokens=min(
                        config.heading_max_tokens, max(128, len(items) * 12)
                    ),
                )
                records.append(
                    {"key": key, "candidate_count": len(items), "selected": result}
                )
                decisions.extend({"id": cid, "level": 2} for cid in result)
            except ValueError as exc:
                records.append(
                    {"key": key, "error": str(exc), "candidate_count": len(items)}
                )
                if len(items) > 1:
                    half = len(items) // 2
                    process(items[:half], key + "_a", factor)
                    process(items[half:], key + "_b", factor)
                elif factor == 1:
                    process(items, key + "_expanded", 2)
                else:
                    problems.append(
                        {
                            "code": "secondary_call_failed",
                            "chapter": group.title,
                            "candidate_id": items[0].id,
                        }
                    )

        process([c for c in batch if c.id not in direct_ids], f"secondary_{group.id}")
        selected_keys = {
            title_key(c.text) for c in batch if c.id in {d["id"] for d in decisions}
        }
        for title in directory:
            if title_key(title) not in selected_keys:
                problems.append(
                    {
                        "code": "secondary_toc_unconfirmed",
                        "chapter": group.title,
                        "title": title,
                    }
                )
        selected_candidates = [c for c in batch if c.id in {d["id"] for d in decisions}]
        selected_forms = {secondary_form(c.text) for c in selected_candidates}
        if "chinese_list" in selected_forms and (
            "numeric_list" in selected_forms or "chinese_parenthesis" in selected_forms
        ):
            problems.append(
                {
                    "code": "secondary_mixed_numbering_review",
                    "chapter": group.title,
                    "candidate_ids": [c.id for c in selected_candidates],
                }
            )
        return decisions, records, problems

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=config.heading_workers, initializer=set_thread_tokenizer, initargs=(selected_tokenizer(),)) as pool:
        secondary_results = list(pool.map(secondary, tasks))
    audit["secondary_wall_seconds"] = time.perf_counter() - started
    for task, (decisions, records, problems) in zip(tasks, secondary_results):
        selected.extend(decisions)
        issues.extend(problems)
        audit["secondary_calls"].append(
            {
                "group_id": task[0].id,
                "start_block_id": blocks[task[2]].id,
                "end_before_block_id": (
                    blocks[task[3]].id if task[3] < len(blocks) else None
                ),
                "directory_titles": task[4],
                "candidate_ids": [c.id for c in task[5]],
                "calls": records,
            }
        )
    selected.sort(
        key=lambda d: positions[next(c.block_id for c in candidates if c.id == d["id"])]
    )
    audit["primary_task_count"] = len(groups)
    audit["secondary_task_count"] = sum(
        any("key" in r for r in results[1]) for results in secondary_results
    )
    audit["secondary_prompt"] = config.secondary_prompt
    return HeadingResult(candidates, selected, audit, issues)
