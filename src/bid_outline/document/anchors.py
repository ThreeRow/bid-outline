"""Fixed multi-anchor extraction. Model output is data, never executable code."""
import re


def normalized_map(text):
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    return ''.join(text[i] for i in positions), positions


def fields(text):
    records, current = [], {}
    for line in text.splitlines():
        match = re.match(r'^\s*(类别|位置|起始句|结束句|中间锚点)\s*[:：]\s*(.+)', line)
        if not match:
            continue
        key, value = match.groups()
        if key == '起始句' and '起始句' in current:
            records.append(current)
            current = {}
        if key == '中间锚点':
            current.setdefault(key, []).append(value)
        elif key in current:
            raise ValueError('重复定位字段: ' + key)
        else:
            current[key] = value
    if current:
        records.append(current)
    if not records or any(not r.get('起始句') or not r.get('结束句') for r in records):
        raise ValueError('定位需要原文起始句和结束句')
    return records


def match_anchors(source, record):
    text, mapping = normalized_map(source)
    def occurrences(value):
        needle = normalized_map(value)[0]
        return [(m.start(), m.start()+len(needle)) for m in re.finditer('(?='+re.escape(needle)+')', text)] if needle else []
    starts, ends = occurrences(record['起始句']), occurrences(record['结束句'])
    candidates = []
    for start, left_end in starts:
        for right_start, end in ends:
            if right_start < start or end < left_end:
                continue
            cursor = left_end
            valid = True
            for anchor in record.get('中间锚点', []):
                options = [(a, b) for a, b in occurrences(anchor) if cursor <= a and b <= end]
                if not options:
                    valid = False
                    break
                cursor = options[0][1]
            if valid:
                a, b = mapping[start], mapping[end-1]+1
                candidates.append({'start': a, 'end': b, 'text': source[a:b]})
    return {'status': 'unique' if len(candidates)==1 else 'multiple' if candidates else 'not_found',
            'matches': candidates}
