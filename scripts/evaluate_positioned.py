"""Offline position-first rule replay; model gates are intentionally unevaluated."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from bid_outline.document.locate import position_hits
from bid_outline.document.location_rules import position_categories
from bid_outline.document.source_index import SourceIndex
from bid_outline.domain import Document


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,default=ROOT/'reviews/document_reading_location_24_20261109')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    results=[]
    comparisons=json.loads((args.input/'comparison.json').read_text())
    for case in comparisons:
        sid=case['sample'];doc=Document.model_validate(json.loads((args.input/sid/'extraction/document.json').read_text()))
        index=SourceIndex(doc)
        positions={tuple(path):set(position_categories(path)) for path in index.sections}
        hits=position_hits(index,positions)
        texts={c:[] for c in 'ABC'}
        spans={c:{} for c in 'ABC'}
        for hit in hits:
            for unit in index.complete_units(hit['index']):
                spans[hit['category']].setdefault(unit.element.element_id,[]).append((unit.start,unit.end))
        for category in 'ABC':
            for eid,ranges in spans[category].items():
                merged=[]
                for a,b in sorted(set(ranges)):
                    if merged and a<=merged[-1][1]:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
                    else:merged.append((a,b))
                texts[category].extend(index.elements[eid].text[a:b] for a,b in merged)
        compact=lambda text: ''.join(text.split())
        probes=[]
        for p in case['probes']:
            probes.append({**p,'rule_only_selected':compact(p['phrase']) in compact('\n'.join(texts[p['category']]))})
        result={'sample':sid,'position_paths':sum(bool(c) for c in positions.values()),'local_hits':len(hits),
            'gates_pending':sum(h['weak'] for h in hits),'chars':{c:sum(map(len,texts[c])) for c in 'ABC'},'probes':probes}
        folder=args.output/sid;folder.mkdir()
        (folder/'hits.json').write_text(json.dumps(hits,ensure_ascii=False,indent=2))
        for c in 'ABC':(folder/f'{c}.md').write_text('\n\n'.join(texts[c]))
        results.append(result)
        print(sid,result['local_hits'],result['gates_pending'],flush=True)
    (args.output/'results.json').write_text(json.dumps({'model_calls':0,
        'scope':'rule-only candidate replay; directory assistance and local model gates not executed',
        'results':results},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
