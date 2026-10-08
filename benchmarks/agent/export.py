"""Export compact synthetic benchmark evidence without OMP profiles or credentials."""
import argparse
import json
from pathlib import Path
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = json.loads((args.run / args.results).read_text(encoding='utf-8'))
    for row in data['episodes']:
        row.pop('provider_tool_sets', None)
        calls = json.loads((args.run / 'episodes' / row['episode'] / 'tool-trace.json').read_text(encoding='utf-8'))
        row['call_evidence'] = calls
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    if re.search(r'fkb_[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9_-]{10,}', encoded):
        raise ValueError('Possible credential in evidence; inspect before export')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise FileExistsError('Do not overwrite published evidence')
    args.output.write_text(encoded, encoding='utf-8')
    print(args.output)


if __name__ == '__main__':
    main()
