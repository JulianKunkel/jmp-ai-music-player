#!/usr/bin/env python3
"""Compile optional download modules into the app's ``lib/modules``.

The open repository has no download code.  A build that should have the
feature points this script at a checkout of the closed module repository:

    python3 build-modules.py --modules ../download-modules --dest lib/modules

Every subdirectory that carries a ``module.json`` is treated as one module.
Only its declared ``package`` directory is copied, byte-compiled and shipped;
the source is removed afterwards, so ``lib/modules`` holds sourceless ``.pyc``
files.  Without a module the directory is created empty and the app simply has
no download feature.

    --modules DIR        where the module checkouts live
                         (default: $JMP_MODULES, else ./download-modules)
    --dest DIR           default: <this folder>/lib/modules
    --requirements       install the module's requirements.txt (default on)
    --no-requirements    skip the pip step
    --python PATH        interpreter for pip (default: the running one)
    --clean              remove previously compiled modules first
    --dry-run            print the plan, write nothing
"""

from __future__ import annotations

import argparse
import compileall
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = "modules.json"


def _module_dirs(root: Path) -> list[Path]:
    """Every module checkout under *root* (or *root* itself as one module)."""
    if (root / "module.json").is_file():
        return [root]
    found = []
    for child in sorted(root.iterdir() if root.is_dir() else []):
        if child.is_dir() and (child / "module.json").is_file():
            found.append(child)
    return found


def _read_manifest(module_dir: Path) -> dict:
    try:
        data = json.loads((module_dir / "module.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{module_dir}/module.json is not valid JSON: {exc}")
    if not isinstance(data, dict) or not data.get("package"):
        raise SystemExit(f"{module_dir}/module.json must name a 'package'")
    return data


def _compile_package(source: Path, dest: Path) -> int:
    """Copy, byte-compile and strip one package; return its file count."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                  "*.pyo", "tests"))
    if not compileall.compile_dir(str(dest), force=True, quiet=1, legacy=True):
        raise SystemExit(f"could not compile {source}")
    files = 0
    for path in sorted(dest.rglob("*")):
        if path.is_dir():
            continue
        if path.suffix == ".py":
            path.unlink()
            continue
        if path.suffix in {".pyc", ".pyo"}:
            files += 1
    for cache in dest.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modules", default=os.environ.get("JMP_MODULES")
                    or str(HERE / "download-modules"))
    ap.add_argument("--dest", default=str(HERE / "lib" / "modules"))
    ap.add_argument("--requirements", dest="requirements",
                    action="store_true", default=True)
    ap.add_argument("--no-requirements", dest="requirements",
                    action="store_false")
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    modules_root = Path(args.modules).expanduser().resolve()
    dest_root = Path(args.dest).expanduser().resolve()
    module_dirs = _module_dirs(modules_root)

    print(f"modules : {modules_root}")
    print(f"dest    : {dest_root}")
    if not module_dirs:
        print("          no module checkouts found — the build has no "
              "download feature")

    plan = []
    for module_dir in module_dirs:
        manifest = _read_manifest(module_dir)
        package = str(manifest["package"])
        plan.append((module_dir, manifest, package))
        print(f"  {manifest.get('name', package)} {manifest.get('version', '')}"
              f" -> {package}")
    if not plan and not modules_root.exists():
        print("          (the module folder does not exist)")

    if args.dry_run:
        print("dry run: nothing written")
        return 0

    dest_root.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for child in list(dest_root.iterdir()):
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()

    manifest_rows = []
    for module_dir, manifest, package in plan:
        source = module_dir / package
        if not source.is_dir():
            raise SystemExit(f"{module_dir}/module.json names package "
                             f"{package!r}, but {source} does not exist")
        target = dest_root / package
        count = _compile_package(source, target)
        print(f"  compiled {package}: {count} byte-code file"
              f"{'s' if count != 1 else ''}")
        manifest_rows.append({
            "name": manifest.get("name", package),
            "version": manifest.get("version", ""),
            "package": package,
            "provides": manifest.get("provides", ""),
            "importer": manifest.get("importer", ""),
            "description": manifest.get("description", ""),
        })

    (dest_root / MANIFEST).write_text(
        json.dumps({"modules": manifest_rows}, indent=2) + "\n",
        encoding="utf-8")

    if args.requirements:
        for module_dir, _manifest, _package in plan:
            req = module_dir / "requirements.txt"
            if not req.is_file():
                continue
            print(f"  installing {req}")
            code = subprocess.call([args.python, "-m", "pip", "install",
                                    "--quiet", "-r", str(req)])
            if code != 0:
                raise SystemExit(f"could not install {req} (pip exited {code})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
