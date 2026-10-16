"""Deterministic outcome grading, independent of the chosen tool sequence."""
import json
import re

GRADER_VERSION = '1.3'


def same_fact(value, expected):
    """Accept equivalent numeric presentation, not different factual values."""
    actual = str(value).strip()
    target = str(expected).strip()
    if actual == target:
        return True
    if target.endswith('%'):
        # A requested upper threshold can naturally be written "低于 0.8%".
        match = re.fullmatch(r'(?:低于|小于|<)?\s*(\d+(?:\.\d+)?)\s*%', actual)
        return bool(match) and float(match[1]) == float(target[:-1])
    if isinstance(expected, (int, float)) and not isinstance(value, bool):
        try:
            return float(actual) == expected
        except ValueError:
            return False
    return False


def final_payload(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text):
        try:
            value, _ = decoder.raw_decode(text[match.start():])
            if isinstance(value, dict) and 'answer' in value and 'completed' in value:
                return value
        except ValueError:
            pass
    return None


def canonical(text):
    return text.replace('\r\n', '\n').rstrip('\n')


def paragraph_spacing_only(expected, actual):
    """Conservative diagnostic, never a reason to pass an exact-file check.

    Only simple heading/paragraph fixtures qualify. Whitespace in code, lists,
    tables or indented Markdown can carry meaning and must not be normalized.
    """
    if not isinstance(actual, str):
        return False
    def lines(text):
        return [line for line in canonical(text).split('\n') if line.strip()]
    wanted, found = lines(expected), lines(actual)
    sensitive = r'^(?:\s|`|~|>|[-*+]|\d+[.)]|\||<)|  $'
    return bool(wanted) and wanted == found and not any(re.search(sensitive, line) for line in wanted)


def grade(task, before, after, final, tool_calls):
    if task.reference:
        from benchmarks.browsecomp.evaluate import grade as grade_browsecomp
        return grade_browsecomp(task, before, after, final, tool_calls)
    payload = final_payload(final)
    answer = str(payload.get('answer', '')) if payload else final
    claimed = payload.get('completed') if payload else None
    changed = sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
    allowed = set(task.expected_files) | set(task.contains) | set(task.absent)
    unintended = [p for p in changed if p not in allowed]
    spacing_only = []
    checks = {'structured_final': payload is not None and isinstance(claimed, bool),
              'no_unintended_changes': not unintended}
    for fact in (() if task.answer_fields else task.facts):
        checks['fact:' + fact] = fact.casefold() in answer.casefold()
    values = payload.get('facts', {}) if payload else {}
    for key, expected in task.answer_fields.items():
        checks['answer_field:' + key] = isinstance(values, dict) and same_fact(values.get(key, ''), expected)
    for path, expected in task.expected_files.items():
        checks['file:' + path] = path not in after if expected is None else path in after and canonical(after[path]) == canonical(expected)
        if expected is not None and not checks['file:' + path] and paragraph_spacing_only(expected, after.get(path)):
            spacing_only.append('file:' + path)
    for path, phrases in task.contains.items():
        checks['contains:' + path] = path in after and all(p in after[path] for p in phrases)
    for path, phrases in task.absent.items():
        checks['absent:' + path] = path in after and all(p not in after[path] for p in phrases)
    if task.source:
        sources = payload.get('sources', []) if payload else []
        checks['citation'] = isinstance(sources, list) and any(task.source in str(s) for s in sources)
    if task.id.startswith('R'):
        checks['used_evidence_tool'] = any(c.get('tool') in ('read_file', 'grep', 'search') and not c.get('is_error') for c in tool_calls)
    if task.refuse:
        checks['appropriate_refusal'] = bool(re.search(r'无法|不能|没有|未提供|未找到|不包含|无权|只读|权限不足|未记录|无相关|不足以', answer))
        checks['completion_status_consistent'] = claimed is False
    else:
        checks['completion_claim'] = claimed is True
        checks['used_platform'] = bool(tool_calls)
    success = all(checks.values())
    protocol_checks = {'structured_final', 'completion_claim', 'completion_status_consistent'}
    outcome = all(v for k,v in checks.items() if k not in protocol_checks)
    return {'success': success, 'outcome_success': outcome,
            'protocol_success': all(v for k,v in checks.items() if k in protocol_checks),
            'checks': checks, 'failed_checks': [k for k,v in checks.items() if not v],
            'changed_files': changed, 'unintended_changes': unintended,
            'formatting_only_failures': spacing_only,
            'false_completion': claimed is True and not all(v for k,v in checks.items() if k not in protocol_checks | {'citation'} | set(spacing_only)),
            'error_recovered': success and any(c.get('is_error') for c in tool_calls),
            'answer': answer, 'claimed_completed': claimed}
