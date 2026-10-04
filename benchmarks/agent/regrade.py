"""Regrade saved evidence uniformly, preserving the original run and scores."""
import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.agent.evaluate import GRADER_VERSION, grade
from benchmarks.agent.tasks import corpus, tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    data = json.loads((args.run / 'results.json').read_text(encoding='utf-8'))
    by_id = {t.id: t for t in tasks()}
    before = corpus()
    fingerprint = hashlib.sha256(json.dumps(before, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    for result in data['episodes']:
        if result['initial_sha256'] != fingerprint:
            raise ValueError('Fixture has changed; refusing to grade against a different corpus')
        directory = args.run / 'episodes' / result['episode']
        after = json.loads((directory / 'final-files.json').read_text(encoding='utf-8'))
        calls = json.loads((directory / 'tool-trace.json').read_text(encoding='utf-8'))
        result['original_evaluation'] = result['evaluation']
        result['evaluation'] = grade(by_id[result['task_id']], before, after, result['final'], calls)
        if not result['valid_run'] or result['timed_out'] or result['returncode'] != 0:
            result['evaluation']['success'] = False
            result['evaluation']['outcome_success'] = False
        result['evaluation']['error_recovered'] = result['evaluation']['success'] and bool(result['agent_tool_errors'])
    data['grader_version'] = GRADER_VERSION
    data['grader_sha256'] = hashlib.sha256(Path('benchmarks/agent/evaluate.py').read_bytes()).hexdigest()
    data['grading_note'] = 'All saved episodes regraded uniformly. Separate task outcome from completion-flag compliance: a correct refusal with completed=true is a protocol inconsistency, not fabricated task completion. Original strict scores are retained. No model reruns or substituted episodes.'
    destination = args.run / 'graded-results.json'
    if destination.exists():
        raise FileExistsError('Do not overwrite earlier grading evidence')
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    main()
