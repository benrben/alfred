#!/usr/bin/env python3
"""Language-aware import-edge extractor for the repository quality gate's
module-dependency check (wired in via .quality/quality-gate.json's
`dependencies.command` + `dependencies.edges_report`).

Why this exists: the gate's own built-in import resolver tries every
SOURCE_EXTENSIONS candidate for a bare specifier regardless of the importing
file's language, and that set has no defined iteration order (Python
randomizes string-hash order per process). A Python `import voicebridge as
_pkg` — the engine package's own self-referential import, load-bearing for
every `_pkg.name()` cross-module call described in voicebridge/__init__.py's
docstring — can therefore spuriously resolve to the sibling `voicebridge.lua`
file instead of the `voicebridge/` package, purely by iteration-order luck.
Python code obviously cannot import a Lua file; this script reuses the gate's
own regexes/comment-masking/file-discovery (imported directly from
repo_quality_gate.py, so behavior stays identical everywhere it already
works) and only changes ONE thing: an import specifier is resolved against
candidate extensions from the IMPORTING file's own language family, never
across languages.

Run: python3 .quality/dependency_edges.py --root . --out .quality/edges.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Sequence

LANGUAGE_EXTENSIONS = {
    ".py": {".py", ".pyi"},
    ".pyi": {".py", ".pyi"},
    ".ts": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".tsx": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".js": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".jsx": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".mjs": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".cjs": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
    ".lua": {".lua"},
}


def load_gate_module():
    """Import repo_quality_gate.py (the skill's gate engine) by path, so this
    adapter reuses its exact IMPORT_PATTERNS/mask_comments/walk_files/
    discover_source_files/normalize_path — everything except the
    cross-language resolution bug."""
    skill_dir = Path.home() / ".claude" / "skills" / "code-discipline" / "scripts"
    path = skill_dir / "repo_quality_gate.py"
    spec = importlib.util.spec_from_file_location("repo_quality_gate", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # dataclasses.dataclass looks its own defining module up in sys.modules
    # while processing class bodies, so it must be registered before exec.
    sys.modules["repo_quality_gate"] = module
    spec.loader.exec_module(module)
    return module


def _relative_import_base(source: Path, specifier: str) -> Path:
    """The filesystem location a relative specifier (./x, ../x, .x, ..x)
    points at, relative to the importing file."""
    if specifier.startswith(("./", "../")):
        return (source.parent / specifier).resolve()
    leading_dots = len(specifier) - len(specifier.lstrip("."))
    base = source.parent
    for _ in range(max(0, leading_dots - 1)):
        base = base.parent
    remainder = specifier[leading_dots:].replace(".", "/")
    return (base / remainder).resolve()


def _relative_candidates(
    gate, source: Path, specifier: str, root: Path, extensions: set[str]
) -> list[str]:
    base = _relative_import_base(source, specifier)
    candidates: list[str] = []
    for extension in extensions:
        candidates.extend(
            [
                gate.normalize_path(Path(str(base) + extension), root),
                gate.normalize_path(base / ("index" + extension), root),
            ]
        )
    return candidates


def _absolute_candidates(
    specifier: str, extensions: set[str]
) -> tuple[list[str], str]:
    clean = (
        specifier.replace("::", "/").replace(".", "/").removeprefix("crate/").lstrip("/")
    )
    candidates: list[str] = []
    for extension in extensions:
        candidates.extend(
            [clean + extension, clean + "/index" + extension, clean + "/__init__" + extension]
        )
    candidates.append(clean)
    return candidates, clean


def _suffix_fallback(
    relative_files: dict[str, Path], clean: str, extensions: set[str]
) -> str | None:
    """The one file (if exactly one) whose path ends with the specifier, for
    a specifier that didn't match any exact candidate — e.g. a bundler alias."""
    matches = [
        candidate
        for candidate in relative_files
        if Path(candidate).suffix in extensions and _ends_with_specifier(candidate, clean)
    ]
    return min(matches, key=len) if len(matches) == 1 else None


def _ends_with_specifier(candidate: str, clean: str) -> bool:
    return candidate.endswith("/" + clean) or candidate.endswith(
        "/" + clean + Path(candidate).suffix
    )


def resolve_import_same_language(
    gate, source: Path, specifier: str, root: Path, relative_files: dict[str, Path]
) -> str | None:
    """gate.resolve_import's exact candidate-generation logic, restricted to
    extensions in the importing file's own language family."""
    extensions = LANGUAGE_EXTENSIONS.get(source.suffix.lower())
    if not extensions:
        return None
    if specifier.startswith("."):
        candidates = _relative_candidates(gate, source, specifier, root, extensions)
        clean = specifier
    else:
        candidates, clean = _absolute_candidates(specifier, extensions)
    for candidate in candidates:
        if candidate in relative_files:
            return candidate
    return _suffix_fallback(relative_files, clean, extensions)


def _load_source_config(gate, root: Path) -> dict:
    """The project's own source.include/exclude/extensions if configured
    (.quality/quality-gate.json), else the gate's built-in default — so this
    adapter discovers exactly the same file set the real gate run does."""
    default_source = gate.default_config()["source"]
    project_config_path = root / ".quality" / "quality-gate.json"
    if not project_config_path.is_file():
        return default_source
    project_config = json.loads(project_config_path.read_text(encoding="utf-8"))
    return project_config.get("source", default_source)


def _edges_for_file(
    gate, source: Path, root: Path, relative_files: dict[str, Path]
) -> list[dict]:
    edges: list[dict] = []
    for specifier, line in gate.import_specs(source):
        target = resolve_import_same_language(gate, source, specifier, root, relative_files)
        if target:
            edges.append(
                {"from": gate.normalize_path(source, root), "to": target, "line": line}
            )
    return edges


def compute_edges(root: Path) -> list[dict]:
    gate = load_gate_module()
    source_files = gate.discover_source_files(root, _load_source_config(gate, root))
    relative_files = {gate.normalize_path(p, root): p for p in source_files}
    edges: list[dict] = []
    for source in source_files:
        edges.extend(_edges_for_file(gate, source, root, relative_files))
    return edges


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    edges = compute_edges(root)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"edges": edges}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(edges)} edges to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
