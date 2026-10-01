"""Package a knowledge plugin: plugin.json + package.zip."""
import argparse
import json
from pathlib import Path
from .template_package import package_template


def package_plugin(source: Path, definition: Path, output: Path):
    result = package_template(source, definition, output)
    manifest = output / 'plugin.json'
    (output / 'registration.json').rename(manifest)
    result['manifest'] = str(manifest.resolve())
    result.pop('registration')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'definition', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package_plugin(args.source, args.definition, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
