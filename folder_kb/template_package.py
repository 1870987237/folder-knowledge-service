"""Build an uploadable ZIP and platform registration JSON from developer files."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

from .template_protocol import tree_digest, write_json
from .templates import MAX_PACKAGE_BYTES, TemplateRegistration, relative_path


def build_package(source: Path) -> bytes:
    before = tree_digest(source)
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file():
                name = path.relative_to(source).as_posix()
                relative_path(name)
                archive.write(path, name)
    if tree_digest(source) != before:
        raise ValueError("Source changed while packaging; retry from a stable version")
    result = output.getvalue()
    if len(result) > MAX_PACKAGE_BYTES:
        raise ValueError("ZIP package exceeds 32 MiB")
    return result


def package_template(source: Path, definition: Path, output: Path):
    archive = build_package(source)
    metadata = json.loads(definition.read_text(encoding='utf-8'))
    metadata['source'] = {'type': 'uploaded-zip', 'archive_sha256': hashlib.sha256(archive).hexdigest()}
    registration = TemplateRegistration.model_validate(metadata)
    output.mkdir(parents=True, exist_ok=False)
    (output / 'package.zip').write_bytes(archive)
    write_json(output / 'registration.json', registration.model_dump())
    return {'package': str((output / 'package.zip').resolve()),
            'registration': str((output / 'registration.json').resolve()),
            'archive_sha256': registration.source.archive_sha256}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package_template(args.source, args.definition, args.output), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
