#!/usr/bin/env python3
"""Build the allowlisted source bundle used by the Helm fallback deployment."""

from __future__ import annotations

import argparse
import gzip
import os
from pathlib import Path
import stat
import tarfile
import tempfile

RUNTIME_FILES = ("pyproject.toml", "uv.lock")
RUNTIME_TREES = ("wp_fleet_ops", "templates")
IGNORED_PARTS = {"__pycache__"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
DEFAULT_MAX_BYTES = 900_000


def _runtime_files(root: Path) -> list[Path]:
    files: list[Path] = []

    for relative_name in RUNTIME_FILES:
        path = root / relative_name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required runtime file is missing or unsafe: {relative_name}")
        files.append(path)

    for relative_name in RUNTIME_TREES:
        tree = root / relative_name
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError(f"required runtime directory is missing or unsafe: {relative_name}")
        for path in tree.rglob("*"):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError(f"source bundle refuses symlink: {relative.as_posix()}")
            if any(part in IGNORED_PARTS for part in relative.parts):
                continue
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError(f"source bundle refuses non-regular file: {relative.as_posix()}")
            if path.suffix in IGNORED_SUFFIXES:
                continue
            files.append(path)

    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _normalized_tar_info(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mtime = 0
    info.mode = 0o755 if info.mode & stat.S_IXUSR else 0o644
    return info


def build_source_bundle(
    root: Path,
    output: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> int:
    """Create a private, atomic archive containing only runtime files."""
    root = root.resolve()
    output = output.resolve()
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    files = _runtime_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".source-bundle-",
        suffix=".tar.gz",
        dir=output.parent,
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(file_descriptor, "wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w") as archive:
                    for path in files:
                        archive.add(
                            path,
                            arcname=path.relative_to(root).as_posix(),
                            recursive=False,
                            filter=_normalized_tar_info,
                        )
        size = temporary.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"source bundle is {size} bytes; limit is {max_bytes} bytes for ConfigMap safety"
            )
        os.chmod(temporary, 0o600)
        os.replace(temporary, output)
        return size
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=project_root)
    parser.add_argument("--output", type=Path, default=project_root / "source-bundle.tar.gz")
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()

    size = build_source_bundle(args.root, args.output, max_bytes=args.max_bytes)
    print(f"Created {args.output.resolve()} ({size} bytes, mode 0600)")


if __name__ == "__main__":
    main()
