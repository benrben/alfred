"""Tests for .quality/dependency_edges.py, the language-aware import-edge
adapter the module-dependency gate uses (see its own module docstring for
why it exists: the gate's built-in resolver can spuriously resolve a Python
`import voicebridge` to the sibling `voicebridge.lua` file, since its
candidate-extension set has no defined iteration order).

Run: ./.venv/bin/python -m pytest tests/test_dependency_edges.py -q

Classes whose tests call load_gate_module() (which imports
repo_quality_gate.py from the code-discipline skill's LOCAL install path —
the same absolute-path dependency .quality/quality-gate.json's own
smoke/coverage commands already have) are skipped when that skill isn't
installed, e.g. on a CI runner that never has it — matching CI's existing
scope, which runs this repo's own pytest/ruff/luacheck/vitest suites but not
the code-discipline coverage/complexity gate itself. Everything that doesn't
need the gate module (the pure candidate-generation helpers) always runs.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_module():
    path = Path(__file__).resolve().parent.parent / ".quality" / "dependency_edges.py"
    spec = importlib.util.spec_from_file_location("dependency_edges", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dependency_edges"] = module
    spec.loader.exec_module(module)
    return module


de = _load_module()
REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = (
    Path.home() / ".claude" / "skills" / "code-discipline" / "scripts" / "repo_quality_gate.py"
)
requires_gate_skill = unittest.skipUnless(
    GATE_SCRIPT.is_file(), "code-discipline skill not installed at ~/.claude/skills"
)


class RelativeImportBase(unittest.TestCase):
    def test_dot_slash_resolves_against_the_importing_files_directory(self):
        source = REPO_ROOT / "raycast/src/dictate.tsx"
        base = de._relative_import_base(source, "./lib/engine")
        self.assertEqual(base, (source.parent / "lib/engine").resolve())

    def test_dot_dot_slash_walks_up_one_directory(self):
        source = REPO_ROOT / "raycast/src/lib/engine-contract.ts"
        base = de._relative_import_base(source, "../history")
        self.assertEqual(base, (source.parent / "../history").resolve())

    def test_bare_leading_dots_walk_up_per_extra_dot(self):
        source = REPO_ROOT / "raycast/src/lib/a/b.ts"
        # ".." here (no slash) means "one level above the current package".
        base = de._relative_import_base(source, "..sibling")
        self.assertEqual(base, (source.parent.parent / "sibling").resolve())


class AbsoluteCandidates(unittest.TestCase):
    def test_plain_specifier_yields_direct_index_and_init_candidates_per_extension(self):
        candidates, clean = de._absolute_candidates("voicebridge", {".py"})
        self.assertEqual(clean, "voicebridge")
        self.assertEqual(
            set(candidates),
            {"voicebridge.py", "voicebridge/index.py", "voicebridge/__init__.py", "voicebridge"},
        )

    def test_rust_style_crate_prefix_and_double_colons_are_normalized(self):
        _, clean = de._absolute_candidates("crate::foo::bar", {".py"})
        self.assertEqual(clean, "foo/bar")

    def test_leading_slash_is_stripped(self):
        _, clean = de._absolute_candidates("/abs/path", {".py"})
        self.assertEqual(clean, "abs/path")


class EndsWithSpecifier(unittest.TestCase):
    def test_true_when_path_ends_with_slash_specifier(self):
        self.assertTrue(de._ends_with_specifier("src/lib/engine.ts", "lib/engine.ts"))

    def test_true_when_path_ends_with_specifier_plus_its_own_suffix(self):
        self.assertTrue(de._ends_with_specifier("src/lib/engine.ts", "lib/engine"))

    def test_false_for_an_unrelated_path(self):
        self.assertFalse(de._ends_with_specifier("src/lib/other.ts", "lib/engine"))


class SuffixFallback(unittest.TestCase):
    def test_returns_the_single_matching_file(self):
        files = {"raycast/src/lib/engine.ts": None, "raycast/src/dictate.tsx": None}
        result = de._suffix_fallback(files, "lib/engine", {".ts", ".tsx"})
        self.assertEqual(result, "raycast/src/lib/engine.ts")

    def test_returns_none_when_no_file_matches(self):
        files = {"raycast/src/dictate.tsx": None}
        self.assertIsNone(de._suffix_fallback(files, "nothing/here", {".ts", ".tsx"}))

    def test_returns_none_when_multiple_files_match_ambiguously(self):
        files = {
            "a/lib/engine.ts": None,
            "b/lib/engine.ts": None,
        }
        self.assertIsNone(de._suffix_fallback(files, "lib/engine", {".ts"}))

    def test_ignores_a_match_whose_suffix_is_outside_the_language_family(self):
        files = {"lib/engine.lua": None}
        self.assertIsNone(de._suffix_fallback(files, "lib/engine", {".ts", ".tsx"}))


@requires_gate_skill
class ResolveImportSameLanguage(unittest.TestCase):
    """The core bug fix: a specifier must never resolve across languages."""

    def setUp(self):
        self.gate = de.load_gate_module()

    def test_python_self_import_does_not_cross_resolve_to_the_sibling_lua_file(self):
        # This is the exact real-world case that motivated this file: every
        # voicebridge/*.py submodule does `import voicebridge as _pkg`, and a
        # sibling `voicebridge.lua` (a different language entirely) must never
        # be offered as a candidate.
        source = REPO_ROOT / "voicebridge" / "audio.py"
        relative_files = {
            "voicebridge.lua": REPO_ROOT / "voicebridge.lua",
            "voicebridge/__init__.py": REPO_ROOT / "voicebridge" / "__init__.py",
        }
        target = de.resolve_import_same_language(
            self.gate, source, "voicebridge", REPO_ROOT, relative_files
        )
        self.assertEqual(target, "voicebridge/__init__.py")

    def test_unknown_importer_extension_resolves_nothing(self):
        source = REPO_ROOT / "README.md"
        target = de.resolve_import_same_language(
            self.gate, source, "voicebridge", REPO_ROOT, {"voicebridge.py": source}
        )
        self.assertIsNone(target)

    def test_typescript_relative_import_resolves_within_the_ts_family(self):
        source = REPO_ROOT / "raycast/src/dictate.tsx"
        relative_files = {
            "raycast/src/lib/engine.ts": REPO_ROOT / "raycast/src/lib/engine.ts",
        }
        target = de.resolve_import_same_language(
            self.gate, source, "./lib/engine", REPO_ROOT, relative_files
        )
        self.assertEqual(target, "raycast/src/lib/engine.ts")

    def test_typescript_import_never_resolves_to_a_python_file_of_the_same_stem(self):
        source = REPO_ROOT / "raycast/src/lib/foo.ts"
        relative_files = {"raycast/src/lib/foo.py": REPO_ROOT / "raycast/src/lib/foo.py"}
        target = de.resolve_import_same_language(
            self.gate, source, "./foo", REPO_ROOT, relative_files
        )
        self.assertIsNone(target)


@requires_gate_skill
class LoadSourceConfig(unittest.TestCase):
    def test_uses_the_projects_configured_source_section_when_present(self):
        gate = de.load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".quality").mkdir()
            (root / ".quality" / "quality-gate.json").write_text(
                json.dumps({"source": {"include": [], "exclude": ["skip/**"], "extensions": [".py"]}})
            )
            source = de._load_source_config(gate, root)
            self.assertEqual(source["exclude"], ["skip/**"])
            self.assertEqual(source["extensions"], [".py"])

    def test_falls_back_to_the_gate_default_when_no_project_config_exists(self):
        gate = de.load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            source = de._load_source_config(gate, Path(tmp))
            self.assertEqual(source, gate.default_config()["source"])

    def test_falls_back_to_the_gate_default_when_the_project_config_omits_source(self):
        gate = de.load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".quality").mkdir()
            (root / ".quality" / "quality-gate.json").write_text(json.dumps({}))
            source = de._load_source_config(gate, root)
            self.assertEqual(source, gate.default_config()["source"])


@requires_gate_skill
class EdgesForFileAndComputeEdges(unittest.TestCase):
    def test_edges_for_file_emits_an_edge_for_each_resolved_import_with_its_line(self):
        gate = de.load_gate_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("import b\nimport nonexistent\n")
            (root / "b.py").write_text("")
            relative_files = {"a.py": root / "a.py", "b.py": root / "b.py"}
            edges = de._edges_for_file(gate, root / "a.py", root, relative_files)
            # import_specs() runs every language's regex over every file, so a
            # plain "import b" also matches the generic Java/C#-style pattern
            # -- one real resolved import can legitimately emit more than one
            # identical edge. Assert presence, not an exact duplicate-free list.
            self.assertIn({"from": "a.py", "to": "b.py", "line": 1}, edges)
            self.assertTrue(all(e == {"from": "a.py", "to": "b.py", "line": 1} for e in edges))

    def test_compute_edges_never_crosses_python_to_lua_on_the_real_repo(self):
        # Regression test for the bug this whole file exists to fix.
        edges = de.compute_edges(REPO_ROOT)
        crossings = [e for e in edges if e["from"].endswith(".py") and e["to"].endswith(".lua")]
        self.assertEqual(crossings, [])

    def test_compute_edges_finds_a_known_real_edge_in_this_repo(self):
        edges = de.compute_edges(REPO_ROOT)
        self.assertIn(
            {"from": "raycast/src/dictate.tsx", "to": "raycast/src/lib/engine.ts", "line": 7},
            edges,
        )


@requires_gate_skill
class MainCli(unittest.TestCase):
    def test_writes_an_edges_json_file_and_reports_the_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.py").write_text("import b\n")
            (root / "b.py").write_text("")
            out = root / "out" / "edges.json"
            rc = de.main(["--root", str(root), "--out", str(out)])
            self.assertEqual(rc, 0)
            data = json.loads(out.read_text())
            self.assertIn({"from": "a.py", "to": "b.py", "line": 1}, data["edges"])


if __name__ == "__main__":
    unittest.main()
