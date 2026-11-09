"""Recoverable, finite technical bid workflow with centralized version commits."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from bid_outline import __version__
from bid_outline.config import AppConfig
from bid_outline.document import chunk_document, lookup_references
from bid_outline.document.lookup import owned_keyword_hit, reference_candidates
from bid_outline.domain import (
    Document, ExtensionSuggestion, OutlineConstraint, OutlineVersion,
    RequirementAssignment, RequirementGroup, ReviewFinding,
)
from bid_outline.model.executor import ModelExecutor, TaskExecutionError, BudgetExceeded, OutputTruncated
from bid_outline.model.call_view import stage as call_stage
from bid_outline.model.task_specs import get_spec, render_task
from bid_outline.model.references import prepare_inputs, prepare_call_inputs
from bid_outline.model.packing import pack_items
from bid_outline.storage import ArtifactStore, RunRepository
from bid_outline.export.markdown import write_atomic

from .repair_scope import RepairScope
from .source_lists import bind_source_list, SourceListConflict
from .common import batches, bounded_map, dump, identity, ids, now, present
from .outline import closed_list_constraint, depth, descendants, make_version, nodes_from_records, replace_branch, retain_skeleton_nodes


class WorkflowRunner:
    """One run owns one executor and one repository; independent runs never share cache."""

    def __init__(self, run_dir: Path, config: AppConfig, client: Any):
        self.run_dir = Path(run_dir)
        self.config = config
        self.repository = RunRepository(self.run_dir)
        self.artifacts = ArtifactStore(self.run_dir)
        self.executor = ModelExecutor(config, client, self.repository, self.artifacts)
        self.issues: list[dict] = []
        self.unprocessed: list[dict] = []
        self.materials: dict[str, dict] = {}
        self.requirements: list[RequirementGroup] = []
        self.assignments: list[RequirementAssignment] = []
        self.findings: list[ReviewFinding] = []
        self.extensions: list[ExtensionSuggestion] = []
        self.version: OutlineVersion | None = None
        self.document: Document | None = None
        self.structure_evidence: list[dict] = []
        self.document_artifact_id: str | None = None
        self._screen_inputs = {}
        self._structure_artifacts = []
        self._outline_artifacts = {}
        self._requirement_order: dict[str, tuple] = {}
        from .history_view import HistoryPresenter
        self.present = HistoryPresenter(self.repository, self.artifacts)

    def issue(self, kind: str, detail: Any, **refs: Any) -> None:
        item = {"kind": kind, "detail": dump(detail), **refs}
        if item not in self.issues:
            self.issues.append(item)

    def save_manifest(self, manifest: dict) -> None:
        import json

        self.repository.save_run(manifest)
        write_atomic(self.run_dir / "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

    async def parallel(self, fn, items):
        return await bounded_map(fn, list(items), workers=self.config.workers, capacity=self.config.queue_capacity)

    def source_location(self, element_ids):
        elements = [e for e in self.document.elements if e.element_id in element_ids] if self.document else []
        if not elements:
            return {}
        first = min(elements, key=lambda e:e.order)
        return {'element_ids':[e.element_id for e in elements], 'section_path':first.section_path, 'order':first.order}

    def requirement_location(self, requirements):
        keys = {mid for r in requirements for mid in r.material_refs}
        return self.source_location({eid for mid in keys for eid in self.materials.get(mid, {}).get('element_ids', [])})

    @staticmethod
    def result_inputs(*results):
        return sorted({r['result_ref']['artifact_id'] for r in results if r and r.get('result_ref')})

    def outline_input(self, version=None):
        version = version or self.version
        if version is None:
            return []
        if version.version_id not in self._outline_artifacts:
            self._outline_artifacts[version.version_id] = self.artifacts.put(
                'outline_version', dump(version), parents=version.input_artifact_ids)
        return [self._outline_artifacts[version.version_id]]

    def requirement_inputs(self, requirements, *, background=False, outline=False, assignments=False):
        inputs = set()
        for req in requirements:
            ref = self.repository.result_ref(req.producing_task_id) if req.producing_task_id else None
            if ref:
                inputs.add(ref['artifact_id'])
            if assignments:
                inputs.update(a.producing_artifact_id for a in self.assignments
                              if a.requirement_id == req.requirement_id and a.producing_artifact_id)
        for source in self.sources(requirements, background=background):
            aid = self.materials[source['material_id']].get('artifact_id')
            if aid:
                inputs.add(aid)
        if outline:
            inputs.update(self.outline_input())
        return sorted(inputs)

    async def call(self, task_type: str, material: str, *, context: str = "无",
                   scope: str = "仅提供材料", allowed: list[str] | None = None,
                   expected: list[str] | None = None, supplement: bool = True,
                   required: bool = True, resizable: bool = False, trigger_stage: str | None = None,
                   input_artifacts: list[str] | None = None, source_location: dict | None = None) -> dict | None:
        """Complete input determines identity. Reruns with supplements replace, never append."""
        original = material
        dependencies = sorted(set(input_artifacts or []))
        source_location = source_location or {}
        accumulated: dict[str, dict] = {}
        last_usable = None
        inputs = {"material": material, "context": context, "scope": scope,
                  "allowed_ids": sorted(set(allowed or [])), "expected_ids": expected or [], "short_ids": True}
        subjects = [n.title for n in self.version.nodes if n.node_id == scope] if self.version else []
        if not subjects:
            subjects = [r.topic for r in self.requirements if r.requirement_id in (expected or [])]
        if not subjects and self.document:
            subjects = [e.section_path[-1] for e in self.document.elements
                        if e.element_id in (allowed or []) and e.section_path]
        chunk = getattr(self, '_leaf_chunks', {}).get(scope)
        if not subjects and chunk:
            subjects = chunk.section_context
        if not subjects and scope in self.materials:
            source_chunk = self.materials[scope].get('source_chunk_id')
            chunk = getattr(self, '_leaf_chunks', {}).get(source_chunk)
            subjects = chunk.section_context if chunk else []
        inputs['display_name'] = '、'.join(dict.fromkeys(subjects)) or (
            scope.rsplit(':', 2)[-1] if scope != '仅提供材料' else get_spec(task_type).task_type)
        if trigger_stage:
            inputs['trigger_stage'] = trigger_stage
        inputs = prepare_inputs(inputs)
        for round_no in range(self.config.supplement_rounds + 1):
            task_id = identity("task_", [task_type, {k: v for k, v in inputs.items()
                                                   if k not in ('display_name', 'trigger_stage')}, dependencies, source_location])
            try:
                result = await self.executor.execute(task_type, inputs, task_id=task_id,
                                                     dependencies=sorted(set(dependencies)))
            except TaskExecutionError as exc:
                if accumulated and last_usable is not None:
                    self.unprocessed.append({"task_type":task_type, "task_id":task_id, "scope":scope,
                                             "error":f"补读重做未完成，保留此前可用结果: {type(exc).__name__}: {exc}",
                                             "pending_supplement_ids":sorted(accumulated),
                                             "retained_result_ref":last_usable.get('result_ref')})
                    self.issue("unresolved_reference", last_usable.get('supplements', []), task_id=task_id, scope=scope)
                    return last_usable
                if resizable and isinstance(exc, (BudgetExceeded, OutputTruncated)):
                    raise
                item = {"task_type": task_type, "task_id": task_id, "scope": scope,
                        "error": f"{type(exc).__name__}: {exc}"}
                if required:
                    self.unprocessed.append(item)
                else:
                    self.issue("optional_task_failed", item)
                return None
            result = {**result, "task_id": task_id, "result_ref": self.repository.result_ref(task_id),
                      "supplement_material_ids": sorted(accumulated)}
            last_usable = result
            needs = result.get("supplements", [])
            if not needs:
                return result
            if not supplement or round_no >= self.config.supplement_rounds:
                self.issue("unresolved_reference", needs, task_id=task_id, scope=scope)
                return result
            additions = []
            confirmed_inputs = []
            for need in needs:
                clues = self.present(**need)
                selection = reference_candidates(self.document, clues, source_location)
                candidates = selection.pop('elements')
                selection_artifact = self.artifacts.put('reference_candidates', {**selection, 'need':need}, parents=dependencies + self.result_inputs(result), task_id=task_id)
                if not candidates:
                    self.issue("missing_reference", need, task_id=task_id)
                    continue
                compact_candidates = [
                    {"element_id": e.element_id, "kind": e.kind, "text": e.text,
                     "section_path": e.section_path,
                     "source_precision": e.source_ref.precision,
                     "table_id": e.source_ref.table_id}
                    for e in candidates
                ]
                def confirmation_payload(page):
                    visible = {e["element_id"] for e in page}
                    page_scope = {**selection, "groups": [
                        {**g, "element_ids": [eid for eid in g["element_ids"] if eid in visible]}
                        for g in selection["groups"] if visible.intersection(g["element_ids"])]}
                    return self.present(引用需要=need, 候选=page, 查找范围=page_scope,
                                        限定="本次仅确认所给完整候选单元；其他候选分批核对。位置不证明关联。")
                pages = pack_items(compact_candidates, lambda page: self.fits(
                    "P06", confirmation_payload(page), allowed_ids=[e["element_id"] for e in page]))
                chosen = set()
                for page in pages:
                    confirmation_material = confirmation_payload(page)
                    visible = [e["element_id"] for e in page]
                    if not self.fits("P06", confirmation_material, allowed_ids=visible):
                        self.unprocessed.append({"task_type": "reference_lookup", "scope": scope,
                            "need": need, "element_ids": visible, "candidate_scope": selection,
                            "error": "indivisible reference candidate exceeds complete request budget; 未截断"})
                        continue
                    confirmation = await self.call(
                        "P06", confirmation_material, allowed=visible, supplement=False,
                        required=False, scope=f"引用候选确认:{task_id}",
                        trigger_stage=call_stage(task_type),
                        input_artifacts=dependencies + self.result_inputs(result) + [selection_artifact] +
                                        ([self.document_artifact_id] if self.document_artifact_id else []))
                    if confirmation and confirmation["fields"].get("结论") in {"已找到", "部分找到"}:
                        chosen.update(ids(confirmation["fields"].get("采用候选", "无")))
                        confirmed_inputs.extend(self.result_inputs(confirmation))
                    elif not confirmation:
                        self.issue("unresolved_reference", need, task_id=task_id, candidate_scope=selection)
                if selection['strategy'] in {'explicit_section_or_attachment', 'explicit_heading_range', 'local_reference_candidate'}:
                    chosen.update(eid for group in selection['groups'] if chosen & set(group['element_ids']) for eid in group['element_ids'])
                selected = [e for e in candidates if e.element_id in chosen]
                if not selected:
                    self.issue("unresolved_reference", need, task_id=task_id)
                    continue
                for element in selected:
                    # Supplement extraction can legitimately cite an original
                    # element ID. Keep that material in the same provenance index
                    # as refined blocks so later source lookup never drops it.
                    if element.element_id not in self.materials:
                        source = {"material_id": element.element_id, "text": element.text,
                                  "kind": "reader_source", "uncertainties": "无",
                                  "source_order": [element.order, 0, 0],
                                  "source_chunk_id": None,
                                  "source_refs": [dump(element.source_ref)],
                                  "element_ids": [element.element_id]}
                        source["artifact_id"] = self.artifacts.put(
                            "supplement_source", source, parents=([self.document_artifact_id] if self.document_artifact_id else []))
                        self.materials[element.element_id] = source
                additions.extend(candidate for candidate in compact_candidates
                                 if candidate["element_id"] in chosen)
            if not additions:
                return result
            dependencies = sorted(set(dependencies + self.result_inputs(result) + confirmed_inputs + [self.materials[a["element_id"]]["artifact_id"] for a in additions]))
            accumulated.update({a["element_id"]: a for a in additions})
            material = self.present(原任务完整材料=original, 补充材料=list(accumulated.values()), 待解决问题=needs)
            inputs = prepare_inputs({**inputs, "material": material})
        raise AssertionError("bounded supplement loop must terminate")

    async def run(self, document: Document | None = None, *, resume: bool = False) -> dict:
        """The CLI holds a filesystem run lock; programmatic callers need the same ownership."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        previous = self.repository.get_run()
        if resume:
            if not previous:
                raise ValueError("run has no persisted manifest")
            if previous.get("config_snapshot") != self.config.model_dump(mode="json"):
                raise ValueError("resume configuration differs; create a new run")
            if previous.get("code_version") != __version__:
                raise ValueError("resume code version differs; create a new run")
            document = Document.model_validate(self.artifacts.get(previous["document_artifact_id"]))
        elif previous:
            raise ValueError("run already exists; use resume or a new run directory")
        if document is None:
            raise ValueError("a document is required")
        self.document = document
        doc_artifact = previous.get("document_artifact_id") if previous else None
        if doc_artifact is None:
            doc_artifact = self.artifacts.put("document", dump(document))
        self.document_artifact_id = doc_artifact
        manifest = {
            "run_id": previous.get("run_id") if previous else identity("run_", [str(self.run_dir.resolve()), now()]),
            "input_hashes": [document.source_hash], "document_artifact_id": doc_artifact,
            "config_snapshot": self.config.model_dump(mode="json"), "code_version": __version__,
            "reader_version": document.reader_version, "reader_name": document.reader_name,
            "status": "running", "started_at": previous.get("started_at", now()) if previous else now(),
            "model_identity": self.config.model, "completion_scope": "extracted_requirements_to_outline",
            "semantic_quality": "not_evaluated", "stage": "screening",
        }
        self.save_manifest(manifest)
        if document.diagnostics:
            self.issue("reader_diagnostics", document.diagnostics)
            for diagnostic in document.diagnostics:
                if isinstance(diagnostic, dict) and diagnostic.get("code", diagnostic.get("kind")) in {
                    "unrepresented_reader_block", "reader_fragment_coverage_gap_or_overlap",
                    "missing_table_content", "image_not_read",
                }:
                    self.unprocessed.append({"scope": "upstream_reader", "diagnostic": diagnostic})
        try:
            # Preflight the rendered screening/refinement envelopes, not a character
            # count mistaken for tokens. A whole indivisible row can still be rejected.
            if self.executor.budget.tokenizer:
                from bid_outline.document.token_chunks import token_chunks
                chunks = token_chunks(document, allowance=self.source_allowance,
                                      text_tokens=self.executor.budget.text_tokens,
                                      fits=self.source_fits)
                self.artifacts.put("chunk_budget_plan", {
                    "strategy": "chapter_then_semantic_units_tokens",
                    "service_context_tokens": self.config.service_context_tokens,
                    "logical_context_tokens": self.config.context_tokens,
                    "generation_includes_thinking": True,
                    "chunks": [{"chunk_id": c.chunk_id, "body_allowance": self.source_allowance(c),
                                "body_tokens": self.executor.budget.text_tokens(c.text),
                                "fits": self.source_fits(c), "sections": c.section_spans or c.section_context}
                               for c in chunks]}, parents=[doc_artifact])
            else:
                # Compatibility path for offline fixtures without a tokenizer.
                chars = self.config.chunk_chars
                while True:
                    chunks = chunk_document(document, max_chars=chars)
                    try:
                        for chunk in chunks:
                            self.check_budget("P03", self.present(完整候选块=chunk.text, 初筛结果="相关；主题及疑点待初筛"), context=self.chunk_context(chunk))
                        break
                    except BudgetExceeded:
                        if chars <= 256:
                            break
                        chars = max(256, chars // 2)
            self.artifacts.put("chunks", {"chunks": dump(chunks)}, parents=[doc_artifact])
            self._leaf_chunks = {c.chunk_id: c for c in chunks}
            await self.parallel(self.process_chunk, chunks)
            # Leaf identity tracks only its owned range; refined sibling ranges
            # never suppress a negative leaf, even within the same source element.
            negative_chunks = [c for c in self._leaf_chunks.values()
                               if c.chunk_id in getattr(self, "_negative", set())
                               and c.chunk_id not in getattr(self, "_positive", set())
                               and owned_keyword_hit(document, c)]
            await self.parallel(lambda c: self.process_chunk(c, keyword=True), negative_chunks)
            self.requirements.sort(key=lambda r: self._requirement_order[r.requirement_id])
            await self.resolve_categories()
            for requirement in self.requirements:
                if requirement.uncertainties:
                    self.issue("requirement_uncertainty", requirement.uncertainties,
                               requirement_id=requirement.requirement_id)
            for mid, material in self.materials.items():
                if material["uncertainties"] != "无":
                    self.issue("refinement_uncertainty", material["uncertainties"], material_id=mid)
            manifest["stage"] = "outline"
            self.save_manifest(manifest)
            if self.requirements:
                await self.build_skeleton()
                if self.version:
                    await self.assign_all()
                    await self.resolve_unassigned()
                    await self.generate()
                    pending = {a.requirement_id for a in self.assignments if a.status == "待确认"}
                    if pending:
                        await self.assign_all(pending)
                    manifest["stage"] = "review"
                    self.save_manifest(manifest)
                    await self.review_and_repair()
                    manifest["stage"] = "extensions"
                    self.save_manifest(manifest)
                    await self.extend()
            else:
                self.issue("no_requirements", "未提取到目录依据；不能据此认定招标文件没有相关要求")
            if self.unprocessed:
                status = "incomplete"
            elif not self.version:
                status = "completed_with_issues" if not self.requirements else "failed"
            else:
                status = "completed_with_issues" if self.issues or any(f.status == "open" for f in self.findings) else "completed"
            result = self.result(status)
            result_artifact = self.artifacts.put("run_result", result, parents=sorted(set(
                [doc_artifact] + self.outline_input() + [t['accepted_artifact_id'] for t in self.repository.list_tasks() if t.get('accepted_artifact_id')])))
            manifest.update(status=status, stage="finished", finished_at=now(), result_artifact_id=result_artifact,
                            unprocessed_ranges=self.unprocessed, issue_count=len(self.issues),
                            model_stats=dump(self.executor.stats))
            self.save_manifest(manifest)
            return result
        except asyncio.CancelledError:
            manifest.update(status="cancelled", finished_at=now())
            self.save_manifest(manifest)
            raise
        except BaseException as exc:
            manifest.update(status="failed", finished_at=now(), error=f"{type(exc).__name__}: {exc}")
            self.save_manifest(manifest)
            raise

    @staticmethod
    def chunk_ids(chunk) -> set[str]:
        return {key for ref in chunk.owned_refs for key in ref.element_ids}

    def budget_messages(self, task_type, material, **inputs):
        values = {"material": material, "context": "无", "scope": "仅提供材料",
                  "allowed_ids": [], "expected_ids": [], "short_ids": True, **inputs}
        return render_task(task_type, prepare_call_inputs(values),
                           view_version=self.config.prompt_view, protocol=self.config.prompt_protocol,
                           directory_protocol=self.config.directory_protocol)

    def check_budget(self, task_type: str, material: str, **inputs):
        return self.executor.budget.check(self.budget_messages(task_type, material, **inputs),
                                         get_spec(task_type).max_tokens or self.config.max_tokens, task_type=task_type)

    def fits(self, task_type, material, **inputs):
        try:
            self.check_budget(task_type, material, **inputs)
            return True
        except BudgetExceeded:
            return False

    def source_messages(self, task_type, chunk, text):
        material = (self.present(完整候选块=text, 初筛结果="相关；主题及疑点待初筛")
                    if task_type == "P03" else text)
        return self.budget_messages(task_type, material, context=self.chunk_context(chunk), scope=chunk.chunk_id)

    def source_allowance(self, chunk):
        return min(self.executor.budget.allowance(
            self.source_messages(task, chunk, ""), self.config.max_tokens +
            (self.config.screening_reserve_tokens if task == "P03" else 0), task_type=task)
            for task in ("P01", "P02", "P03"))

    def source_fits(self, chunk):
        try:
            for task in ("P01", "P02", "P03"):
                self.executor.budget.check(self.source_messages(task, chunk, chunk.text),
                                          self.config.max_tokens +
                                          (self.config.screening_reserve_tokens if task == "P03" else 0), task_type=task)
            return True
        except BudgetExceeded:
            return False

    async def process_chunk(self, chunk, keyword: bool = False) -> None:
        try:
            await self._process_chunk(chunk, keyword)
        except (BudgetExceeded, OutputTruncated) as exc:
            from bid_outline.document import split_chunk
            # These partial semantic products are superseded by smaller source tasks;
            # their immutable files and calls remain available as history.
            discarded = {mid for mid, m in self.materials.items() if m["source_chunk_id"] == chunk.chunk_id}
            self.requirements = [r for r in self.requirements if not (set(r.material_refs) & discarded)]
            for mid in discarded:
                self.materials.pop(mid)
            children = split_chunk(self.document, chunk)
            if not children:
                self.unprocessed.append({"scope": chunk.chunk_id, "owned_refs": dump(chunk.owned_refs),
                                         "error": f"indivisible source unit: {exc}"})
                return
            self.artifacts.put("split_chunk", {"parent": dump(chunk), "children": dump(children)},
                               parents=[self.document_artifact_id])
            self._leaf_chunks.pop(chunk.chunk_id, None)
            self._leaf_chunks.update({c.chunk_id: c for c in children})
            # A worker processes its children directly; it never blocks pushing them
            # into its own bounded input queue. The other workers remain independent.
            for child in children:
                # A keyword parent can split into a hit and a context-only leaf.
                # Screen the latter normally; do not invent a negative result or
                # count an auxiliary header as this leaf's keyword obligation.
                await self.process_chunk(child, keyword=keyword and owned_keyword_hit(self.document, child))

    @staticmethod
    def chunk_context(chunk):
        sections = {"分段章节": chunk.section_spans} if chunk.section_spans else {}
        return present(章节上下文=chunk.section_context, **sections, 延续来源=chunk.continuation_of or "无",
                       辅助条件=[{'text':n['text'], 'status':n['status'], 'candidate_scope':n.get('candidate_scope',{})} for n in chunk.context_notes],
                       范围说明="完整候选块是负责范围；辅助条件仅供理解，不计为本片已扫描或新增义务")

    async def _process_chunk(self, chunk, keyword: bool = False) -> None:
        self._positive = getattr(self, "_positive", set())
        self._negative = getattr(self, "_negative", set())
        chunk_artifact = self.artifacts.put("source_chunk", dump(chunk), parents=[self.document_artifact_id] if self.document_artifact_id else [])
        screen = await self.call(
            "P02" if keyword else "P01", chunk.text,
            context=self.chunk_context(chunk),
            scope=chunk.chunk_id, resizable=True, input_artifacts=[chunk_artifact] + (self._screen_inputs.get(chunk.chunk_id, []) if keyword else []), source_location=self.source_location(self.chunk_ids(chunk)),
        )
        if screen is None:
            return
        self._screen_inputs[chunk.chunk_id] = self.result_inputs(screen)
        if screen["fields"]["判断"] == "未发现":
            self._negative.add(chunk.chunk_id)
            return
        self._positive.add(chunk.chunk_id)
        refined = await self.call("P03", self.present(完整候选块=chunk.text, 初筛结果=screen["fields"]),
                                  scope=chunk.chunk_id, resizable=True, context=self.chunk_context(chunk),
                                  input_artifacts=[chunk_artifact] + self.result_inputs(screen), source_location=self.source_location(self.chunk_ids(chunk)))
        if refined is None:
            return
        state = refined["fields"]["处理结果"]
        if state == "无法完整处理":
            raise OutputTruncated("model reported incomplete refinement")
        if state == "未发现相关内容":
            return
        mid = identity("M_", [chunk.chunk_id, refined["task_id"]])
        material = {"material_id": mid, "text": refined["fields"]["保留内容"],
                    "source_chunk_id": chunk.chunk_id,
                    "owned_refs": dump(chunk.owned_refs),
                    "supplement_material_ids": refined.get("supplement_material_ids", []),
                    "source_refs": dump(chunk.owned_refs + chunk.context_refs) + [
                        ref for key in refined.get("supplement_material_ids", [])
                        for ref in self.materials[key]["source_refs"]],
                    "element_ids": sorted(self.chunk_ids(chunk)), "producing_task_id": refined["task_id"],
                    "uncertainties": refined["fields"].get("疑点", "无"), "kind": "refined_text"}
        element_order = {e.element_id: e.order for e in self.document.elements}
        material["source_order"] = min((
            element_order[ref.element_ids[0]],
            ref.reader_ref.get("element_text_range", {}).get("start", ref.start or 0),
            min(ref.reader_ref.get("owned_rows", [0])),
        ) for ref in chunk.owned_refs)
        self.materials[mid] = material
        material["artifact_id"] = self.artifacts.put("refined_block", material,
                                                   parents=[chunk_artifact] + self.result_inputs(screen, refined) + [self.materials[key]["artifact_id"]
                                                            for key in refined.get("supplement_material_ids", [])],
                                                   task_id=refined["task_id"])

        extraction_material = self.present(精炼材料={
                "material_id": mid, "text": material["text"], "kind": "refined_text",
                "uncertainties": material["uncertainties"],
                "section_path": list(dict.fromkeys(
                    " / ".join(e.section_path) for e in self.document.elements
                    if e.element_id in self.chunk_ids(chunk) and e.section_path)),
            })
        for category in "ABC":
            self.check_budget(f"P04-{category}", extraction_material, allowed_ids=[mid], scope=mid)
        resize_errors = []
        async def extract(category: str) -> None:
            try:
                output = await self.call(f"P04-{category}", extraction_material,
                                         allowed=[mid], scope=mid, resizable=True, input_artifacts=[material["artifact_id"]], source_location=self.source_location(self.chunk_ids(chunk)))
            except (BudgetExceeded, OutputTruncated) as exc:
                resize_errors.append(exc)
                return
            if output is None:
                return
            for ordinal, record in enumerate(output["records"].get("要求", [])):
                rid = identity("R_", [output["task_id"], record["局部编号"]])
                self._requirement_order[rid] = (*material["source_order"], "ABC".index(category), ordinal)
                self.requirements.append(RequirementGroup(
                    requirement_id=rid, categories=[record["类别"]], topic=record["要求主题"],
                    content=record["要求内容"], material_refs=ids(record["材料依据"]),
                    uncertainties=[] if record["疑点"] == "无" else [record["疑点"]],
                    producing_task_id=output["task_id"],
                ))
        async with asyncio.TaskGroup() as group:
            for category in "ABC":
                group.create_task(extract(category))
        if resize_errors:
            raise resize_errors[0]

    async def resolve_categories(self) -> None:
        async def resolve(req: RequirementGroup):
            if "待定" not in req.categories:
                return req
            result = await self.call("P05", self.present(待定要求=dump(req), 来源=self.sources([req])),
                                     allowed=[req.requirement_id] + req.material_refs, expected=[req.requirement_id], input_artifacts=self.requirement_inputs([req]), source_location=self.requirement_location([req]))
            if not result:
                return req
            records = result["records"].get("判定", [])
            if not records:
                return req
            req.producing_task_id = result['task_id']
            r = records[0]
            state = r["结论"]
            if state in {"不纳入", "项目需求"}:
                self.artifacts.put("category_resolution", {"requirement": dump(req), "decision": r}, parents=self.result_inputs(result), task_id=result['task_id'])
                return None
            if state in {"A", "B", "C"}:
                req.categories = [state]
            elif state == "多类":
                req.categories = ids(r["涉及类别"])
            else:
                self.issue("category_pending", r, requirement_id=req.requirement_id)
            return req
        self.requirements = [r for r in await self.parallel(resolve, self.requirements) if r is not None]

    def sources(self, requirements: list[RequirementGroup], *, background: bool = False) -> list[dict]:
        keys = {m for r in requirements for m in r.material_refs}
        if background:
            from .context import background_candidates
            selected = background_candidates(self.materials, requirements, self.document)
            keys.update(selected)
        return [{key: self.materials[k][key] for key in ("material_id", "text", "kind", "uncertainties", "source_order")}
                for k in sorted(keys) if k in self.materials]

    def allowed(self) -> list[str]:
        return [r.requirement_id for r in self.requirements] + list(self.materials) + (
            [n.node_id for n in self.version.nodes] if self.version else [])

    def scoped_ids(self, requirements: list[RequirementGroup], include_outline: bool = False,
                   *, background: bool = False) -> list[str]:
        allowed = {r.requirement_id for r in requirements}
        allowed.update(mid for r in requirements for mid in r.material_refs)
        if background:
            allowed.update(m["material_id"] for m in self.sources(requirements, background=True))
        if include_outline and self.version:
            allowed.update(n.node_id for n in self.version.nodes)
            allowed.update(rid for n in self.version.nodes for rid in n.basis_requirement_ids)
        return sorted(allowed)

    def commit(self, nodes, reason: str, constraints=None, *, input_artifacts=None) -> None:
        if self.version:
            old_nodes = {n.node_id: n for n in self.version.nodes}
            protected = {key for c in self.version.constraints for key in c.affected_node_ids}
            changes = []
            for node in nodes:
                old = old_nodes.get(node.node_id)
                if old and node.node_id in protected and node.title != old.title:
                    if ''.join(node.title.split()) == ''.join(old.title.split()):
                        changes.append({'node_id': node.node_id, 'candidate': node.title, 'retained': old.title})
                        node.title = old.title
            if changes:
                self.artifacts.put('preserved_title_whitespace', {'reason': reason, 'changes': changes})
        by_id = {r.requirement_id: r for r in self.requirements}
        for node in nodes:
            references = {}
            for rid in node.basis_requirement_ids:
                for mid in by_id[rid].material_refs:
                    for reference in self.materials.get(mid, {}).get("source_refs", []):
                        references[identity("src_", reference)] = reference
            from bid_outline.domain import SourceRef
            node.source_refs = [SourceRef.model_validate(references[k]) for k in sorted(references)]
        previous_version = self.version
        version_inputs = sorted(set((input_artifacts or []) + self.outline_input()))
        version = make_version(nodes, self.version, reason, constraints, input_artifact_ids=version_inputs)
        historical = self.repository.get_outline_version(version.version_id)
        current = self.repository.get_outline()
        if historical:
            # Preserve the original timestamp and payload, so downstream input hashes
            # stay identical when replaying completed stages after interruption.
            self.version = OutlineVersion.model_validate(historical)
            self.refresh_assignments(previous_version)
            self._replaying = bool(current and current["version_id"] != version.version_id)
            return
        if current and (self.version is None or current["version_id"] != self.version.version_id):
            raise ValueError("replay diverged from persisted outline history; create a new run")
        self.repository.commit_outline(dump(version), expected_parent=self.version.version_id if self.version else None)
        self.version = version
        self.outline_input(version)
        self.refresh_assignments(previous_version)

    def refresh_assignments(self, previous: OutlineVersion | None) -> None:
        """Keep a surviving ancestor as a provisional location, never proof of coverage."""
        if previous is None:
            return
        old = {n.node_id: n for n in previous.nodes}
        new = {n.node_id: n for n in self.version.nodes}
        requirements = {r.requirement_id: r for r in self.requirements}
        for assignment in self.assignments:
            targets = []
            invalidated = False
            for target in assignment.targets:
                key = target["node_id"]
                original = key
                while key not in new and key in old:
                    key = old[key].parent_id
                if original not in new or key != original or (key in old and key in new and old[key].parent_id != new[key].parent_id):
                    invalidated = True
                if key in new:
                    targets.append({**target, "node_id": key})
            assignment.targets = [dict(t) for i, t in enumerate(targets) if t not in targets[:i]]
            assignment.outline_version_id = self.version.version_id
            if invalidated:
                assignment.status = "待确认"
                assignment.unassigned_content = requirements[assignment.requirement_id].content
                assignment.basis = "目录改变，保留最近存续祖先作为候选位置；须重新归属及覆盖检查"

    def skeleton_material(self, group, evidence, sources, *, paged=False):
        return self.present(
            操作="建立" if self.version is None else "增补", 当前骨架=dump(self.version),
            本批要求=group, 结构依据=evidence,
            累计封闭清单=[record for record in self.structure_evidence if record["性质"] == "禁止新增"],
            项目背景=sources,
            批次说明=("本次只提供该要求的部分背景；直接来源完整保留。其他背景分批处理，"
                      "不得据此宣称全文完整。当前骨架是本次唯一基准，保留其中已承接义务。"
                      if paged else "仅本批；其他批次另行处理，不声明整体完成"),
        )

    async def build_skeleton(self) -> None:
        # Establish A's original chapter sequence before fallback B/C batches. This
        # is organization, never a priority rule for discarding obligations.
        ordered = sorted(self.requirements, key=lambda r: (0 if "A" in r.categories else 1, self._requirement_order[r.requirement_id]))
        def structure_material(group):
            return self.present(要求=group, 来源=self.sources(group))
        if self.executor.budget.tokenizer:
            grouped = pack_items(ordered, lambda group:
                self.fits("P07", structure_material(group), allowed_ids=self.scoped_ids(group))
                and self.fits("P08", self.skeleton_material(group, None, self.sources(group, background=True)),
                              allowed_ids=self.scoped_ids(group, include_outline=True, background=True)))
        else:
            grouped = batches(ordered, self.config.chunk_chars, lambda r: self.present(要求=r))
        prepared = [(self.present(要求=group, 来源=self.sources(group)), self.scoped_ids(group),
                     self.requirement_inputs(group), self.requirement_location(group)) for group in grouped]
        async def structure_input(item):
            material, allowed, dependencies, location = item
            return await self.call("P07", material, allowed=allowed, input_artifacts=dependencies, source_location=location)
        evidence_batches = await self.parallel(structure_input, prepared)
        queue = [(group, evidence, None) for group, evidence in zip(grouped, evidence_batches)]
        while queue:
            group, evidence, source_page = queue.pop(0)
            if evidence:
                for record in evidence["records"].get("结构依据", []):
                    if record not in self.structure_evidence:
                        self.structure_evidence.append(record)
                self._structure_artifacts = sorted(set(self._structure_artifacts + self.result_inputs(evidence)))
            sources = source_page if source_page is not None else self.sources(group, background=True)
            allowed = sorted(set(self.scoped_ids(group, include_outline=True)) |
                             {s["material_id"] for s in sources} | {
                rid for record in self.structure_evidence if record["性质"] == "禁止新增"
                for rid in ids(record["要求依据"])})
            material = self.skeleton_material(group, evidence, sources, paged=source_page is not None)
            if self.executor.budget.tokenizer and not self.fits("P08", material, allowed_ids=allowed):
                if len(group) > 1:
                    halves = [group[:len(group)//2], group[len(group)//2:]]
                    # Evidence is batch-scoped. Recompute for the smaller scopes;
                    # discovered global prohibitions remain in the cumulative set.
                    for half in reversed(halves):
                        ev = await structure_input((structure_material(half), self.scoped_ids(half),
                                                    self.requirement_inputs(half), self.requirement_location(half)))
                        queue.insert(0, (half, ev, source_page))
                    continue
                direct = self.sources(group)
                direct_ids = {s["material_id"] for s in direct}
                extras = [s for s in sources if s["material_id"] not in direct_ids]
                def page_fits(page):
                    page_ids = sorted(set(self.scoped_ids(group, include_outline=True)) |
                                      {s["material_id"] for s in direct + page} |
                                      {rid for record in self.structure_evidence if record["性质"] == "禁止新增"
                                       for rid in ids(record["要求依据"])})
                    return self.fits("P08", self.skeleton_material(group, evidence, direct + page, paged=True),
                                     allowed_ids=page_ids)
                if extras and page_fits([]):
                    pages = pack_items(extras, page_fits)
                    # Only replace with strictly smaller requests. An indivisible
                    # source or growing current outline becomes an explicit block.
                    if len(pages) > 1 or source_page is None:
                        queue[0:0] = [(group, evidence, direct + page) for page in pages]
                        self.artifacts.put("skeleton_background_pages", {
                            "requirement_ids": [r.requirement_id for r in group],
                            "pages": [[s["material_id"] for s in direct + page] for page in pages],
                            "reason": "complete_request_budget",
                        }, parents=self.requirement_inputs(group, background=True, outline=True))
                        continue
            result = await self.call("P08", material, allowed=allowed,
                                     input_artifacts=self.requirement_inputs(group, outline=True) +
                                     [self.materials[s["material_id"]]["artifact_id"] for s in sources
                                      if self.materials[s["material_id"]].get("artifact_id")] +
                                     self._structure_artifacts, source_location=self.requirement_location(group))
            if result is None:
                continue
            if result["fields"]["状态"] != "已形成候选骨架":
                self.issue("skeleton_information_insufficient", result["fields"])
                continue
            try:
                nodes = nodes_from_records(result["records"].get("节点", []),
                                           self.version.nodes if self.version else [],
                                           {r.requirement_id for r in self.requirements}, result["task_id"])
                if not nodes:
                    raise ValueError("empty skeleton")
                constraints = list(self.version.constraints) if self.version else []
                existing_protected = {n for c in constraints for n in c.affected_node_ids}
                a_ids = {r.requirement_id for r in self.requirements if "A" in r.categories}
                a_nodes = [n.node_id for n in nodes if set(n.basis_requirement_ids) & a_ids and n.node_id not in existing_protected]
                # Unconfirmed A members retain names/hierarchy individually;
                # no ordering relationship is inferred from batching alone.
                for node_id in a_nodes:
                    constraints.append(OutlineConstraint(
                        constraint_id=identity("C_", ["A-member", node_id]), affected_node_ids=[node_id],
                        kind="preserve_names_order_hierarchy", origin="系统策略",
                        basis="A成员默认保留；清单间顺序须有独立证据", requirement_ids=sorted(a_ids),
                    ))
                for record in self.structure_evidence:
                    if record.get("清单材料", "无") == "无":
                        if set(ids(record["要求依据"])) & a_ids:
                            self.issue("source_list_pending", "缺少可确认的有界清单身份，未跨批推定顺序", requirement_ids=ids(record["要求依据"]))
                        continue
                    try:
                        constraint = bind_source_list(record, self.materials, self.requirements, nodes, constraints)
                    except SourceListConflict:
                        raise
                    except ValueError as exc:
                        self.issue("source_list_pending", str(exc), evidence=record)
                        continue
                    constraints = [c for c in constraints if c.constraint_id != constraint.constraint_id] + [constraint]
                for record in self.structure_evidence:
                    if record["性质"] == "禁止新增":
                        constraints.append(closed_list_constraint(record, nodes))
                        continue
                    if record["性质"] == "待确认":
                        self.issue("structure_constraint_pending", record)
                    if record["性质"] != "章节或顺序约束":
                        continue
                    basis_ids = ids(record["要求依据"])
                    affected = [n.node_id for n in nodes if set(n.basis_requirement_ids) & set(basis_ids)]
                    if affected:
                        constraints.append(OutlineConstraint(
                            constraint_id=identity("C_", [affected, record]), affected_node_ids=affected,
                            kind="source_declared_structure", origin="文件明确",
                            basis=record["原文表达"] + "\n" + record["结构含义"], requirement_ids=basis_ids,
                        ))
                constraints = list({c.constraint_id: c for c in constraints}.values())
                self.commit(nodes, f"skeleton:{result['task_id']}", constraints, input_artifacts=self.result_inputs(result) + self._structure_artifacts)
                text = result["fields"].get("待核查事项", "无")
                if text != "无":
                    self.issue("skeleton_issue", text)
            except ValueError as exc:
                self.unprocessed.append({"task_id": result["task_id"], "error": str(exc), "scope": "skeleton"})

        # A rejected last candidate must not leave a known restriction absent
        # from the older usable skeleton. Bind it there or withhold that skeleton.
        if self.version:
            try:
                constraints = {c.constraint_id: c for c in self.version.constraints}
                for record in self.structure_evidence:
                    if record["性质"] == "禁止新增":
                        constraint = closed_list_constraint(record, self.version.nodes)
                        constraints[constraint.constraint_id] = constraint
                if dump(list(constraints.values())) != dump(self.version.constraints):
                    self.commit([n.model_copy(deep=True) for n in self.version.nodes],
                                "bind_known_restrictions", list(constraints.values()), input_artifacts=self._structure_artifacts)
            except ValueError as exc:
                self.unprocessed.append({"scope": "skeleton_restrictions", "error": str(exc)})
                self.version = None

    async def assign_all(self, requirement_ids: set[str] | None = None) -> None:
        version = self.version
        async def assign(req):
            result = await self.call("P09", self.present(骨架=version, 要求=req, 必要依据=self.sources([req])),
                                     allowed=self.scoped_ids([req], include_outline=True), expected=[req.requirement_id], input_artifacts=self.requirement_inputs([req], outline=True), source_location=self.requirement_location([req]))
            if result is None:
                return RequirementAssignment(requirement_id=req.requirement_id, outline_version_id=version.version_id,
                                             status="待确认", unassigned_content=req.content)
            record = result["records"]["归属"][0]
            target_ids = ids(record["承接节点"])
            if not set(target_ids) <= {n.node_id for n in version.nodes}:
                self.unprocessed.append({"scope": req.requirement_id, "error": "assignment references non-node IDs"})
                target_ids = []
                record = {**record, "状态": "待确认", "未承接内容": req.content}
            if record["状态"] == "已分配" and (not target_ids or record["未承接内容"] != "无"):
                self.issue("assignment_inconsistent", record, requirement_id=req.requirement_id)
                record = {**record, "状态": "部分分配" if target_ids else "未承接"}
            return RequirementAssignment(
                requirement_id=req.requirement_id, outline_version_id=version.version_id, status=record["状态"],
                producing_artifact_id=next(iter(self.result_inputs(result)), ''),
                targets=[{"node_id": n, "scope": record["承接范围"]} for n in target_ids],
                shared_conditions=req.content, unassigned_content=record["未承接内容"], basis=record["依据或疑点"],
            )
        selected = [r for r in self.requirements if requirement_ids is None or r.requirement_id in requirement_ids]
        updated = {a.requirement_id: a for a in await self.parallel(assign, selected)}
        existing = {a.requirement_id: a for a in self.assignments}
        existing.update(updated)
        self.assignments = [existing[r.requirement_id] for r in self.requirements if r.requirement_id in existing]

    async def resolve_unassigned(self) -> None:
        pending = [a for a in self.assignments if a.status != "已分配"]
        if not pending:
            return
        suggestions = []
        suggestion_inputs = []
        # One complete requirement group may yield several placement suggestions.
        # Keep groups independent so a long batch cannot silently omit inputs.
        prepared = []
        for assignment in pending:
            reqs = [r for r in self.requirements if r.requirement_id == assignment.requirement_id]
            prepared.append((self.present(骨架=self.version, 未承接=[assignment], 要求=reqs, 依据=self.sources(reqs)),
                             self.scoped_ids(reqs, include_outline=True), [r.requirement_id for r in reqs],
                             self.requirement_inputs(reqs, outline=True, assignments=True), self.requirement_location(reqs)))
        async def adjust(item):
            material, allowed, expected, dependencies, location = item
            return await self.call("P10", material, allowed=allowed, expected=expected,
                                   input_artifacts=dependencies, source_location=location)
        for result in await self.parallel(adjust, prepared):
            if result:
                suggestions.extend(result["records"].get("调整建议", []))
                suggestion_inputs.extend(self.result_inputs(result))
        if not suggestions:
            self.issue("unassigned_requirements", dump(pending))
            return
        result = await self.call("P08", self.present(操作="统一最小增补", 当前骨架=self.version,
                                                统一调整建议=suggestions, 未承接要求=[r for r in self.requirements if r.requirement_id in {a.requirement_id for a in pending}]),
                                 allowed=self.allowed(), input_artifacts=suggestion_inputs + self.requirement_inputs(
                                     [r for r in self.requirements if r.requirement_id in {a.requirement_id for a in pending}], outline=True),
                                 source_location=self.requirement_location([r for r in self.requirements if r.requirement_id in {a.requirement_id for a in pending}]))
        if result and result["fields"]["状态"] == "已形成候选骨架":
            try:
                nodes = nodes_from_records(result["records"].get("节点", []), self.version.nodes,
                                           {r.requirement_id for r in self.requirements}, result["task_id"])
                self.commit(nodes, f"unassigned:{result['task_id']}", input_artifacts=self.result_inputs(result))
                await self.assign_all()
            except ValueError as exc:
                self.issue("skeleton_update_rejected", str(exc))
        remaining = [a for a in self.assignments if a.status != "已分配"]
        if remaining:
            self.issue("unassigned_requirements", dump(remaining))

    def branch_requirements(self, root: str) -> list[RequirementGroup]:
        branch_ids = {n.node_id for n in descendants(self.version.nodes, root)}
        required = {a.requirement_id for a in self.assignments if any(t["node_id"] in branch_ids for t in a.targets)}
        return [r for r in self.requirements if r.requirement_id in required]

    async def generate(self) -> None:
        from .context import chapter_constraints, chapter_candidate_views
        base = self.version
        generated_inputs = {}
        roots = [n for n in base.nodes if n.parent_id is None]
        async def branch(root):
            reqs = self.branch_requirements(root.node_id)
            def payload(group, sources=None):
                return self.present(
                    本章=root, 绝对层级=depth(base.nodes, root.node_id), 基准版本=base.version_id,
                    保留结构=descendants(base.nodes, root.node_id, include_root=False),
                    结构约束=chapter_constraints(base.constraints, base.nodes, root.node_id),
                    分配要求=group, 归属=[a for a in self.assignments if a.requirement_id in {r.requirement_id for r in group}],
                    必要原文及项目需求=self.sources(group, background=True) if sources is None else sources,
                    范围说明="只处理本批要求与本次提供的背景；其余批次另行处理，候选在本章范围内整合。",
                )
            groups = (pack_items(reqs, lambda group: self.fits("P11", payload(group, self.sources(group)),
                      allowed_ids=self.scoped_ids(group, include_outline=True), scope=root.node_id))
                      if self.executor.budget.tokenizer else
                      batches(reqs, self.config.chunk_chars, lambda r: self.present(要求=r))) or [[]]
            jobs = [(group, None) for group in groups]
            candidates = []
            while jobs:
                group, source_page = jobs.pop(0)
                sources = self.sources(group, background=True) if source_page is None else source_page
                material = payload(group, sources)
                allowed = sorted(set(self.scoped_ids(group, include_outline=True)) | {s['material_id'] for s in sources})
                try:
                    self.check_budget("P11", material, allowed_ids=allowed, scope=root.node_id)
                except BudgetExceeded:
                    if len(group) > 1:
                        middle = len(group) // 2
                        jobs[0:0] = [(group[:middle], source_page), (group[middle:], source_page)]
                        continue
                    direct = self.sources(group)
                    direct_ids = {s['material_id'] for s in direct}
                    extras = [s for s in sources if s['material_id'] not in direct_ids]
                    def page_fits(page):
                        return self.fits("P11", payload(group, direct+page),
                            allowed_ids=sorted(set(self.scoped_ids(group, include_outline=True)) |
                                               {s['material_id'] for s in direct+page}), scope=root.node_id)
                    if self.executor.budget.tokenizer and extras and page_fits([]):
                        pages = pack_items(extras, page_fits)
                        if len(pages) > 1 or source_page is None:
                            jobs[0:0] = [(group, direct+page) for page in pages]
                            continue
                result = await self.call("P11", material, allowed=allowed,
                    scope=root.node_id, input_artifacts=self.requirement_inputs(group, assignments=True) +
                    self.outline_input(base) + [self.materials[s['material_id']]['artifact_id'] for s in sources
                                               if self.materials[s['material_id']].get('artifact_id')],
                    source_location=self.requirement_location(group))
                if result is None:
                    return root, None
                if result["fields"]["章节"] != root.node_id:
                    self.unprocessed.append({"scope": root.node_id, "error": "generation returned another chapter"})
                    return root, None
                state = result["fields"]["处理结果"]
                if state == "信息不足":
                    self.issue("chapter_information_insufficient", result["fields"], node_id=root.node_id)
                    return root, None
                for key in ("未承接事项", "结构调整建议"):
                    if result["fields"].get(key, "无") != "无":
                        self.issue(key, result["fields"][key], node_id=root.node_id)
                if state == "无须新增子目录":
                    continue
                candidates.append(result)
            if not candidates:
                return root, None
            if len(candidates) > 1:
                merged = await self.call("P12", self.present(
                    本章=root, 绝对层级=depth(base.nodes, root.node_id),
                    已确认结构=descendants(base.nodes, root.node_id),
                    结构约束=chapter_constraints(base.constraints, base.nodes, root.node_id),
                    候选=chapter_candidate_views(candidates, base.nodes), 要求=reqs,
                    归属=[a for a in self.assignments if a.requirement_id in {r.requirement_id for r in reqs}],
                    必要原文及项目需求=self.sources(reqs, background=True)),
                    allowed=self.allowed(), scope=root.node_id,
                    input_artifacts=self.result_inputs(*candidates)
                    + self.requirement_inputs(reqs, background=True, assignments=True) + self.outline_input(base))
                if merged is None:
                    return root, None
                if merged["fields"]["章节"] != root.node_id:
                    self.unprocessed.append({"scope": root.node_id, "error": "merge returned another chapter"})
                    return root, None
                if merged["fields"]["未解决事项"] != "无":
                    self.issue("chapter_merge_issue", merged["fields"]["未解决事项"], node_id=root.node_id)
                candidates = [merged]
            result = candidates[0]
            generated_inputs[root.node_id] = self.result_inputs(result)
            try:
                nodes = nodes_from_records(result["records"].get("节点", []), base.nodes,
                                           {r.requirement_id for r in reqs}, result["task_id"])
                nodes = retain_skeleton_nodes(base, root.node_id, nodes)
                replace_branch(base, root.node_id, nodes)
                return root, nodes
            except ValueError as exc:
                self.unprocessed.append({"scope": root.node_id, "error": str(exc)})
                return root, None
        results = await self.parallel(branch, roots)
        # Results read one immutable base. Only disjoint branches are combined.
        for root, nodes in results:
            if nodes is not None:
                try:
                    self.commit(replace_branch(self.version, root.node_id, nodes), f"generate:{root.node_id}", input_artifacts=generated_inputs[root.node_id])
                except ValueError as exc:
                    self.unprocessed.append({"scope": root.node_id, "error": str(exc)})

    async def review(self, affected_roots: set[str] | None = None) -> list[ReviewFinding]:
        version = self.version
        findings = []
        selected_roots = [n for n in version.nodes if n.parent_id is None
                          and (affected_roots is None or n.node_id in affected_roots)]
        selected_req_ids = {r.requirement_id for n in selected_roots for r in self.branch_requirements(n.node_id)}
        selected_req_ids.update(getattr(self, "_repair_review_requirements", set()))
        review_reqs = self.requirements if affected_roots is None else [r for r in self.requirements if r.requirement_id in selected_req_ids]
        async def coverage(req):
            assignments = [a for a in self.assignments if a.requirement_id == req.requirement_id]
            material = self.present(当前目录=version, 要求=[req], 依据=self.sources([req], background=True), 归属=assignments)
            scope = "当前完整目录"
            try:
                self.check_budget("P14", material, allowed_ids=self.allowed(), expected_ids=[req.requirement_id])
            except BudgetExceeded:
                target_ids = {t["node_id"] for a in assignments for t in a.targets}
                nodes = {n.node_id: n for target in target_ids for n in descendants(version.nodes, target)}
                lookup = {n.node_id: n for n in version.nodes}
                for target in target_ids:
                    node = lookup[target]
                    while node.parent_id is not None:
                        node = lookup[node.parent_id]
                        nodes[node.node_id] = node
                scope = "仅已分配的相关分支；不能据此认定其他分支不存在承接，必要时报告信息不足"
                material = self.present(基准版本=version.version_id, 目录=list(nodes.values()), 范围=scope,
                                   要求=[req], 依据=self.sources([req], background=True), 归属=assignments)
            result = await self.call("P14", material, allowed=self.scoped_ids([req], include_outline=True), expected=[req.requirement_id], scope=scope, input_artifacts=self.requirement_inputs([req], background=True, outline=True, assignments=True), source_location=self.requirement_location([req]))
            if not result:
                return None
            record = result["records"]["覆盖检查"][0]
            if not set(ids(record["承接节点"])) <= {n.node_id for n in version.nodes}:
                self.unprocessed.append({"scope": req.requirement_id, "error": "coverage references non-node IDs"})
                return None
            self.artifacts.put("coverage", {"version_id": version.version_id, "result": record}, task_id=result["task_id"], parents=self.result_inputs(result) + self.outline_input(version))
            if record["结论"] == "已覆盖" and record["未覆盖或不明确内容"] == "无" and ids(record["承接节点"]):
                return None
            return ReviewFinding(finding_id=identity("F_", [result["task_id"], req.requirement_id]),
                                 producing_artifact_id=next(iter(self.result_inputs(result)), ''),
                                 kind="coverage", requirement_ids=[req.requirement_id],
                                 node_ids=ids(record["承接节点"]), base_version_id=version.version_id,
                                 evidence=self.present(**record), suggested_scope=ids(record["承接节点"]))
        findings.extend(f for f in await self.parallel(coverage, review_reqs) if f is not None)
        async def structure(root):
            branch_nodes = descendants(version.nodes, root.node_id)
            branch_ids = {n.node_id for n in branch_nodes}
            branch_reqs = self.branch_requirements(root.node_id)
            result = await self.call("P15", self.present(
                基准版本=version.version_id, 目录=branch_nodes, 相关要求=branch_reqs,
                项目背景=self.sources(branch_reqs, background=True),
                结构约束=[c for c in version.constraints if (c.kind == "no_add_children" and c.parent_node_id is None) or set(c.affected_node_ids) & branch_ids],
                机械校验="通过ID、深度、顺序、保留节点校验", 检查范围="仅本章，不能判断其他章的义务缺失"),
                allowed=self.allowed(), scope=root.node_id, input_artifacts=self.requirement_inputs(branch_reqs, background=True, outline=True), source_location=self.requirement_location(branch_reqs))
            found = []
            if result:
                if result["fields"]["检查结论"] == "信息不足":
                    found.append(ReviewFinding(finding_id=identity("F_", result["task_id"]), kind="structure_insufficient", producing_artifact_id=next(iter(self.result_inputs(result)), ''),
                                               node_ids=[root.node_id], base_version_id=version.version_id, evidence="结构审查信息不足"))
                for record in result["records"].get("问题", []):
                    found.append(ReviewFinding(finding_id=identity("F_", [result["task_id"], record["局部编号"]]),
                                               producing_artifact_id=next(iter(self.result_inputs(result)), ''), kind=record["类型"], requirement_ids=ids(record["涉及要求"]),
                                               node_ids=ids(record["涉及节点"]), base_version_id=version.version_id,
                                               evidence=self.present(**record), suggested_scope=ids(record["涉及节点"])))
            return found
        for group in await self.parallel(structure, selected_roots):
            findings.extend(group)
        # Only concrete duplicate-title candidates trigger P13; equal titles are not
        # automatically removed or declared semantic duplicates.
        by_title = {}
        for root in [n for n in version.nodes if n.parent_id is None]:
            for node in descendants(version.nodes, root.node_id):
                by_title.setdefault(node.title, []).append((root.node_id, node))
        prepared_cross = []
        from .context import ancestor_path
        for title, entries in by_title.items():
            roots = {r for r, _ in entries}
            if len(roots) < 2 or (affected_roots is not None and not roots & affected_roots):
                continue
            branch_ids = {n.node_id for root_id in roots for n in descendants(version.nodes, root_id)}
            assignments = [a for a in self.assignments if any(t['node_id'] in branch_ids for t in a.targets)]
            req_ids = ({rid for node in version.nodes if node.node_id in branch_ids for rid in node.basis_requirement_ids}
                       | {a.requirement_id for a in assignments})
            reqs = [r for r in self.requirements if r.requirement_id in req_ids]
            prepared_cross.append((self.present(版本=version.version_id, 疑似问题=f"跨章同名标题:{title}",
                                            分支=[{
                                                '比较节点': node,
                                                '祖先路径': ancestor_path(version.nodes, node.node_id),
                                                '相关子树': descendants(version.nodes, node.node_id),
                                                '章节承接范围': [a for a in assignments if any(
                                                    t['node_id'] in {n.node_id for n in descendants(version.nodes, root_id)}
                                                    for t in a.targets)],
                                            } for root_id, node in entries],
                                            要求=reqs, 依据=self.sources(reqs)),
                                   self.requirement_inputs(reqs, assignments=True) + self.outline_input(version)))
        async def compare(item):
            material, dependencies = item
            return await self.call("P13", material, allowed=self.allowed(), input_artifacts=dependencies)
        for result in await self.parallel(compare, prepared_cross):
            if result and result["fields"]["结论"] != "合理交叉":
                findings.append(ReviewFinding(finding_id=identity("F_", result["task_id"]), kind="cross_branch", producing_artifact_id=next(iter(self.result_inputs(result)), ''),
                                              node_ids=ids(result["fields"]["涉及节点"]), requirement_ids=ids(result["fields"]["涉及要求"]),
                                              base_version_id=version.version_id, evidence=self.present(**result["fields"])))
        return findings

    async def review_and_repair(self) -> None:
        current = await self.review()
        for round_no in range(self.config.repair_rounds):
            if not current:
                break
            changed = False
            changed_roots = set()
            before_req_ids = set()
            before_node_ids = set()
            roots = [n for n in self.version.nodes if n.parent_id is None]
            pending_scopes = []
            prepared_repairs = []
            round_base = self.version.model_copy(deep=True)
            blocked_ids = {f.finding_id for f in current if f.kind == 'cross_branch'}
            for finding in current:
                if finding.kind == 'cross_branch':
                    self.issue('cross_branch_repair_pending', '局部修正不能单独撤销联合范围发现', finding_id=finding.finding_id)
            for root in roots:
                scope = {n.node_id for n in descendants(self.version.nodes, root.node_id)}
                reqs = self.branch_requirements(root.node_id)
                req_ids = {r.requirement_id for r in reqs}
                req_ids.update(rid for n in self.version.nodes if n.node_id in scope for rid in n.basis_requirement_ids)
                relevant = [f for f in current if set(f.node_ids) & scope or set(f.requirement_ids) & req_ids]
                req_ids.update(rid for f in relevant for rid in f.requirement_ids)
                pending_scopes.append(RepairScope(
                    root.node_id, root.node_id,
                    [r.model_copy(deep=True) for r in self.requirements if r.requirement_id in req_ids],
                    [a.model_copy(deep=True) for a in self.assignments if a.requirement_id in req_ids],
                    [c.model_copy(deep=True) for c in self.version.constraints
                     if (c.kind == "no_add_children" and c.parent_node_id is None)
                     or set(c.affected_node_ids) & scope or c.parent_node_id in scope],
                    [f.model_copy(deep=True) for f in relevant], set()))
            while pending_scopes:
                repair = pending_scopes.pop(0)
                root = next(n for n in self.version.nodes if n.node_id == repair.root_id)
                owner_root = repair.owner_id
                scope = {n.node_id for n in descendants(self.version.nodes, root.node_id)}
                reqs, relevant = repair.requirements, repair.findings
                req_ids = {r.requirement_id for r in reqs}
                if not relevant:
                    continue
                material = self.present(
                    基准版本=self.version.version_id, 边界父节点=root,
                    边界父节点绝对层级=depth(self.version.nodes, root.node_id),
                    允许修改范围=descendants(self.version.nodes, root.node_id, include_root=False),
                    范围内全部要求=reqs, 必要原文=self.sources(reqs, background=True),
                    必须保留=repair.constraints, 冻结归属及共同条件=repair.assignments,
                    继承的父级要求ID=sorted(repair.inherited_ids),
                    问题=relevant, 范围说明="在原文约束允许时可在边界父节点下新增或替换下级，现有下级为空不代表禁止新增；不修改边界父节点或其他章，总深度最多四级。继承的父级要求须遵守完整条件，但不是每个子树都必须新增的义务；仅处理问题在本范围内有依据的部分，跨范围或定位不明的剩余问题不得声称已解决",
                )
                try:
                    self.check_budget("P16", material, allowed_ids=self.scoped_ids(reqs, include_outline=True, background=True))
                except BudgetExceeded as exc:
                    children, blocked = repair.subdivide(self.version.nodes)
                    if blocked:
                        blocked_ids.update(f.finding_id for f in blocked)
                        self.unprocessed.append({
                            "task_type": "P16", "scope": root.node_id, "owner_scope": owner_root,
                            "error": f"repair budget blocked; no safe child scope: {exc}",
                            "requirements": dump(reqs), "findings": dump(blocked),
                            "assignments": dump(repair.assignments), "constraints": dump(repair.constraints),
                        })
                    pending_scopes[0:0] = children
                    continue
                prepared_repairs.append((repair, material, scope,
                    self.scoped_ids(reqs, include_outline=True, background=True),
                    self.requirement_inputs(reqs, background=True, assignments=True) + self.outline_input(round_base) +
                    [f.producing_artifact_id for f in relevant if f.producing_artifact_id], self.requirement_location(reqs)))
            async def request_repair(entry):
                repair, material, scope, allowed, dependencies, location = entry
                return await self.call("P16", material, allowed=allowed, scope=repair.root_id,
                                       input_artifacts=dependencies, source_location=location)
            # Independent jobs overlap; shared findings, overlapping boundaries or
            # constraints spanning both writable regions form separate waves.
            from .repair_scope import repair_waves
            responses = {}
            for wave in repair_waves(prepared_repairs, round_base):
                values = await self.parallel(request_repair, [prepared_repairs[i] for i in wave])
                responses.update(zip(wave, values))
            if self.version.version_id != round_base.version_id:
                raise ValueError("repair round base changed before centralized commit")
            for index, entry in enumerate(prepared_repairs):
                repair, material, scope, _, _, _ = entry
                root = next(n for n in round_base.nodes if n.node_id == repair.root_id)
                owner_root = repair.owner_id
                reqs, relevant = repair.requirements, repair.findings
                req_ids = {r.requirement_id for r in reqs}
                result = responses[index]
                if not result:
                    blocked_ids.update(f.finding_id for f in relevant)
                    continue
                if result["fields"]["处理结论"] not in {"已修正", "部分修正"}:
                    continue
                try:
                    # A serialized request wave still reads round_base. Never
                    # overwrite a branch changed by an earlier accepted result.
                    expected_branch = descendants(round_base.nodes, root.node_id)
                    actual_branch = descendants(self.version.nodes, root.node_id)
                    if dump(expected_branch) != dump(actual_branch):
                        raise ValueError('stale repair branch changed since frozen base')
                    replacement = result["fields"].get("替换目录")
                    if replacement == "无":
                        continue
                    if replacement == "清空":
                        child_ids = set(scope) - {root.node_id}
                        if any(set(c.affected_node_ids) & child_ids for c in self.version.constraints):
                            raise ValueError("cannot clear protected descendants")
                        if any(t["node_id"] in child_ids for a in self.assignments for t in a.targets):
                            raise ValueError("cannot clear descendants with assigned requirements")
                        nodes = []
                    else:
                        nodes = nodes_from_records(result["records"].get("节点", []), self.version.nodes,
                                                   req_ids, result["task_id"])
                    proposed = replace_branch(self.version, root.node_id, nodes)
                    admission_inputs = []
                    if replacement == "清空":
                        basis_ids = {rid for n in self.version.nodes if n.node_id in child_ids for rid in n.basis_requirement_ids}
                        basis_reqs = [r for r in self.requirements if r.requirement_id in basis_ids]
                        if {r.requirement_id for r in basis_reqs} != basis_ids:
                            raise ValueError("clear candidate refers to unavailable requirements")
                        before_req_ids.update(basis_ids)
                        for req in basis_reqs:
                            checked = await self.call("P14", self.present(
                                当前候选目录=proposed, 要求=[req], 依据=self.sources([req], background=True),
                                检查范围="清空提交前检查；仅在剩余目录仍明确承接时允许删除，不能按旧依据推断覆盖"),
                                expected=[req.requirement_id], scope=root.node_id,
                                input_artifacts=self.result_inputs(result) + self.requirement_inputs([req], background=True, outline=True),
                                source_location=self.requirement_location([req]))
                            if not checked:
                                raise ValueError("clear candidate coverage check failed")
                            admission_inputs.extend(self.result_inputs(checked))
                            coverage = checked["records"]["覆盖检查"][0]
                            if (coverage["结论"] != "已覆盖" or coverage["未覆盖或不明确内容"] != "无"
                                    or not ids(coverage["承接节点"])
                                    or not set(ids(coverage["承接节点"])) <= {n.node_id for n in proposed}):
                                raise ValueError("clear candidate leaves requirement uncovered")
                    if dump(proposed) != dump(self.version.nodes):
                        before_req_ids.update(req_ids)
                        before_req_ids.update(rid for finding in relevant for rid in finding.requirement_ids)
                        before_node_ids.update(scope)
                        self.commit(proposed, f"repair:{round_no}:{root.node_id}:{'clear' if replacement == '清空' else 'replace'}",
                                    input_artifacts=self.result_inputs(result) + admission_inputs + [f.producing_artifact_id for f in relevant if f.producing_artifact_id])
                        changed = True
                        changed_roots.add(owner_root)
                except ValueError as exc:
                    self.issue("repair_rejected", str(exc), node_id=root.node_id)
                    blocked_ids.update(f.finding_id for f in relevant)
                    if result["fields"].get("替换目录") == "清空":
                        blocked_ids.update(f.finding_id for f in relevant)
                        self.unprocessed.append({"task_type": "P16", "scope": root.node_id,
                                                 "error": str(exc), "action": "clear",
                                                 "requirements": dump(reqs), "findings": dump(relevant)})
            if not changed:
                break
            pending = {a.requirement_id for a in self.assignments if a.status == "待确认"}
            if pending:
                await self.assign_all(pending)
            # Reassignment may move an obligation to another chapter.
            for root in [n for n in self.version.nodes if n.parent_id is None]:
                if before_req_ids & {r.requirement_id for r in self.branch_requirements(root.node_id)}:
                    changed_roots.add(root.node_id)
            changed_nodes = before_node_ids | {n.node_id for root in changed_roots for n in descendants(self.version.nodes, root)}
            changed_reqs = before_req_ids | {r.requirement_id for root in changed_roots for r in self.branch_requirements(root)}
            affected = [f for f in current if f.finding_id not in blocked_ids
                        and (set(f.node_ids) & changed_nodes or set(f.requirement_ids) & changed_reqs)]
            self._repair_review_requirements = changed_reqs
            failures_before = len(self.unprocessed)
            try:
                checked = await self.review(changed_roots)
            finally:
                self._repair_review_requirements = set()
            # A failed follow-up is not evidence that the old problem went away.
            if len(self.unprocessed) == failures_before:
                for finding in affected:
                    finding.status = "superseded"
                self.findings.extend(affected)
                current = [f for f in current if f not in affected] + checked
            else:
                current.extend(checked)
        self.findings.extend(current)

    async def extend(self) -> None:
        from .context import chapter_constraints
        async def suggest(root):
            reqs = self.branch_requirements(root.node_id)
            suggestions = []
            chapter_scope = {n.node_id for n in descendants(self.version.nodes, root.node_id)}
            def payload(group):
                return self.present(骨干=descendants(self.version.nodes, root.node_id),
                    项目需求=self.sources(group, background=True), 要求=group, 已有建议=suggestions,
                    约束=chapter_constraints(self.version.constraints, self.version.nodes, root.node_id))
            remaining = list(reqs)
            groups = [[]] if not reqs else None
            if not self.executor.budget.tokenizer:
                groups = batches(reqs, self.config.chunk_chars, lambda r: self.present(要求=r)) or [[]]
            while remaining or groups:
                if groups is None:
                    group = pack_items(remaining, lambda batch: self.fits("P17", payload(batch),
                        allowed_ids=self.scoped_ids(batch, include_outline=True, background=True)))[0]
                    remaining = remaining[len(group):]
                else:
                    group = groups.pop(0)
                    remaining = []
                result = await self.call("P17", payload(group),
                    allowed=self.scoped_ids(group, include_outline=True, background=True), required=False,
                    input_artifacts=self.requirement_inputs(group, background=True, outline=True) +
                                    [s.producing_artifact_id for s in suggestions if s.producing_artifact_id])
                if not result:
                    continue
                for record in result["records"].get("建议", []):
                    if record["对应章节"] not in chapter_scope:
                        self.issue("extension_outside_scope", record, node_id=root.node_id)
                        continue
                    suggestions.append(ExtensionSuggestion(
                        suggestion_id=identity("E_", [result["task_id"], record["局部编号"]]),
                        outline_version_id=self.version.version_id, chapter_id=record["对应章节"],
                        topic=record["建议主题"], content=record["建议内容"], source_kind=record["来源性质"],
                        material_refs=[] if record["相关依据"] == "常规经验" else ids(record["相关依据"]),
                        conditions=record["适用理由与条件"], producing_artifact_id=next(iter(self.result_inputs(result)), ''),
                    ))
            if not suggestions:
                return []
            review = {}
            prepared_checks = []
            for suggestion in suggestions:
                supporting = [self.materials[mid] for mid in suggestion.material_refs if mid in self.materials]
                prepared_checks.append((self.present(建议=[suggestion], 其他已有建议=[s for s in suggestions if s != suggestion],
                                                骨干=descendants(self.version.nodes, root.node_id),
                                                项目需求=self.sources(reqs, background=True),
                                                要求=reqs,
                                                约束=chapter_constraints(self.version.constraints, self.version.nodes, root.node_id),
                                                建议引用材料=[{k:m[k] for k in ('material_id','text','kind')} for m in supporting]),
                                        suggestion.suggestion_id,
                                        self.requirement_inputs(reqs, background=True, outline=True)
                                        + [s.producing_artifact_id for s in suggestions if s.producing_artifact_id] +
                                        [m['artifact_id'] for m in supporting if m.get('artifact_id')]))
            async def check_suggestion(item):
                material, sid, dependencies = item
                return await self.call("P18", material, expected=[sid], required=False, input_artifacts=dependencies)
            for checked in await self.parallel(check_suggestion, prepared_checks):
                if checked:
                    review.update({r["建议"]:r for r in checked["records"].get("建议检查", [])})
            for suggestion in suggestions:
                record = review.get(suggestion.suggestion_id)
                if not record:
                    suggestion.review_status = "待确认"
                    self.issue("extension_unreviewed", suggestion.suggestion_id)
                    continue
                suggestion.review_status = record["结论"]
                if record["结论"] == "待确认":
                    self.issue("extension_pending", record, suggestion_id=suggestion.suggestion_id)
                if record["结论"] == "修改后保留":
                    # A review gives instructions, not an automatically accepted rewrite.
                    suggestion.review_status = "待确认"
                    self.issue("extension_needs_revision", record, suggestion_id=suggestion.suggestion_id)
            return suggestions
        groups = await self.parallel(suggest, [n for n in self.version.nodes if n.parent_id is None])
        self.extensions = [s for group in groups for s in group]

    def result(self, status: str) -> dict:
        return {"status": status, "semantic_quality": "not_evaluated",
                "completion_scope": "已提取要求到目录的覆盖，不证明全文提取无遗漏",
                "document_id": self.document.document_id, "reader_diagnostics": self.document.diagnostics,
                "materials": self.materials, "requirements": dump(self.requirements),
                "outline": dump(self.version), "assignments": dump(self.assignments),
                "findings": dump(self.findings), "extensions": dump(self.extensions),
                "issues": self.issues, "unprocessed_ranges": self.unprocessed,
                "structure_evidence": self.structure_evidence}

    async def aclose(self) -> None:
        await self.executor.aclose()
        self.repository.close()
