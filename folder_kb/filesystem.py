"""Native filesystem paths without changing the paths used for boundary checks."""
import os
from pathlib import Path


def native_path(path: str | Path) -> Path:
    """Use Windows extended paths for I/O, including when long paths are disabled."""
    path = Path(path)
    if os.name != 'nt':
        return path
    value = os.path.abspath(path)
    if value.startswith('\\\\?\\'):
        return Path(value)
    # Leave ordinary paths unchanged; directory creation has a lower limit than
    # file access, so switch before reaching the legacy 248/260-character limits.
    if len(value) < 240:
        return path
    if value.startswith('\\\\'):
        return Path('\\\\?\\UNC\\' + value[2:])
    return Path('\\\\?\\' + value)


def resolved_path(path: str | Path, *, strict=False) -> Path:
    value = str(native_path(path).resolve(strict=strict))
    if os.name == 'nt':
        if value.startswith('\\\\?\\UNC\\'):
            value = '\\\\' + value[8:]
        elif value.startswith('\\\\?\\'):
            value = value[4:]
    return Path(value)
