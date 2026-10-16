"""Regrade saved evidence uniformly, preserving the original run and scores."""
import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.agent.evaluate import GRADER_VERSION, grade
from benchmarks.agent.tasks import DEFAULT_DATASET, load_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    args = parser.parse_args()
    data = json.loads((args.run / 'results.json').read_text(encoding='utf-8'))
    before, definitions, metadata = load_dataset(args.dataset)
    by_id = {t.id: t for t in definitions}
    if data.get('dataset', {}).get('tasks_sha256', metadata['tasks_sha256']) != metadata['tasks_sha256']:
        raise ValueError('Tasks have changed; refusing to grade against a different task set')
    fingerprint = hashlib.sha256(json.dumps(before, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    for result in data['episodes']:
        if result['initial_sha256'] != fingerprint:
            raise ValueError('Fixture has changed; refusing to grade against a different corpus')
        task = by_id.get(result['task_id'])
        if task is None or task.prompt != result['prompt']:
            raise ValueError('Task is missing or its prompt has changed')
        directory = args.run / 'episodes' / result['episode']
        after = json.loads((directory / 'final-files.json').read_text(encoding='utf-8'))
        calls = json.loads((directory / 'tool-trace.json').read_text(encoding='utf-8'))
        result['original_evaluation'] = result['evaluation']
        result['evaluation'] = grade(task, before, after, result['final'], calls)
        if not result['valid_run'] or result['timed_out'] or result['returncode'] != 0:
            result['evaluation']['success'] = False
            result['evaluation']['outcome_success'] = False
        result['evaluation']['error_recovered'] = result['evaluation']['success'] and bool(result['agent_tool_errors'])
    data['grader_version'] = GRADER_VERSION
    data['grading_dataset'] = metadata
    data['grader_sha256'] = hashlib.sha256(Path('benchmarks/agent/evaluate.py').read_bytes()).hexdigest()
    data['grading_note'] = 'All saved episodes regraded uniformly. Exact-file checks remain strict; paragraph-spacing-only differences are diagnosed separately from false completion. Correct refusals with completed=true remain protocol inconsistencies. Original scores are retained. No model reruns or substituted episodes.'
    destination = args.run / 'graded-results.json'
    if destination.exists():
        raise FileExistsError('Do not overwrite earlier grading evidence')
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    print(destination)


if __name__ == '__main__':
    main()
