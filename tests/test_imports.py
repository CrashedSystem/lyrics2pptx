"""Module-boundary rules P1..P9, enforced mechanically (docs/arch/02 §4).

The package is parsed with ``ast`` — no imports are executed — and an import
graph is built. Every rule the architecture declares as "테스트로 강제" has one
test method here, so a boundary violation fails the suite instead of surviving
in review.

Coverage notes (docs/arch/02 §3 is the source of truth for the whitelist):

* Site adapters (rule P7) are discovered from ``src/lyrics2pptx/crawl/*.py``
  instead of being hardcoded, so a newly added adapter is covered at once.
* Dynamic imports (``importlib.import_module`` / ``__import__``) are folded into
  the graph when their target is a literal string, and a non-literal target
  fails the suite instead of escaping every rule.

Run standalone::

    python -m unittest tests.test_imports
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

PACKAGE = "lyrics2pptx"
SRC = Path(__file__).resolve().parents[1] / "src" / PACKAGE

ENTRYPOINTS = {f"{PACKAGE}.cli", f"{PACKAGE}.webapp"}
PPTX_LOW_LEVEL = {
    f"{PACKAGE}.pptx.clone",
    f"{PACKAGE}.pptx.fill",
    f"{PACKAGE}.pptx.template_reader",
}
CONFIG_OR_ERRORS = {f"{PACKAGE}.config", f"{PACKAGE}.errors"}

_ENVIRON_ATTRS = {"environ", "getenv", "putenv"}
# The L1 politeness layer + L0 base are infrastructure, not site adapters.
_CRAWL_INFRASTRUCTURE = {
    f"{PACKAGE}.crawl",
    f"{PACKAGE}.crawl.base",
    f"{PACKAGE}.crawl.httputil",
}


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([PACKAGE, *parts])


def _discover_adapters() -> set[str]:
    """Site adapters (L2) taken from the filesystem — no hardcoded module list.

    Rule P7 (adapters must not touch ``urllib`` directly) then applies to every
    ``crawl/<site>.py`` as soon as it is created.
    """
    discovered = {_module_name(path) for path in (SRC / "crawl").glob("*.py")}
    return discovered - _CRAWL_INFRASTRUCTURE


ADAPTER_MODULES = _discover_adapters()


def _category(module: str) -> str:
    if module == PACKAGE:
        return "root"
    tail = module[len(PACKAGE) + 1 :]
    if tail in {"cli", "webapp", "pipeline", "config", "errors"}:
        return tail
    for axis in ("text", "pptx", "crawl"):
        if tail == axis or tail.startswith(f"{axis}."):
            return axis
    return "other"


def _resolve_relative(module: str, level: int, imported: str | None, *, is_package: bool) -> str:
    """Resolve ``level`` for a module (``__init__`` modules are "packages")."""
    if level == 0:
        return imported or ""
    parts = module.split(".")
    # For a plain module the last part is the module name; for a package
    # (``__init__.py``) it is the package itself, so one part fewer is dropped.
    keep = len(parts) - level + (1 if is_package else 0)
    base = parts[:keep]
    if imported:
        return ".".join([*base, *imported.split(".")])
    return ".".join(base)


class ImportGraph:
    """Import graph + ``os.environ`` and dynamic-import usage from the source tree."""

    # Placeholder recorded when a dynamic import target cannot be resolved statically.
    DYNAMIC_UNKNOWN = "<dynamic>"

    def __init__(self) -> None:
        self.imports: dict[str, set[str]] = {}
        self.dynamic_imports: dict[str, list[str]] = {}
        self.environ_uses: dict[str, list[str]] = {}
        self.errors: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        for path in sorted(SRC.rglob("*.py")):
            module = _module_name(path)
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as exc:  # pragma: no cover - would fail elsewhere too
                self.errors[module] = str(exc)
                continue
            is_package = path.name == "__init__.py"
            static = self._imports_of(module, tree, is_package=is_package)
            dynamic = self._dynamic_imports_of(tree)
            self.dynamic_imports[module] = dynamic
            self.imports[module] = self._merge_dynamic(module, static, dynamic)
            self.environ_uses[module] = self._environ_uses_of(tree)

    @staticmethod
    def _imports_of(module: str, tree: ast.AST, *, is_package: bool) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module is None and node.level:
                    base = _resolve_relative(module, node.level, None, is_package=is_package)
                    for alias in node.names:
                        found.add(f"{base}.{alias.name}")
                else:
                    found.add(
                        _resolve_relative(
                            module, node.level, node.module, is_package=is_package
                        )
                    )
        if module in found:
            found.discard(module)
        return found

    @classmethod
    def _dynamic_imports_of(cls, tree: ast.AST) -> list[str]:
        """Targets of ``importlib.import_module`` / ``__import__`` calls.

        A literal string target is returned verbatim so it can be folded back
        into the graph; anything else is returned as ``DYNAMIC_UNKNOWN`` because
        it cannot be verified statically.
        """
        found: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_import_call = (isinstance(func, ast.Name) and func.id == "__import__") or (
                isinstance(func, ast.Attribute) and func.attr in {"import_module", "__import__"}
            )
            if not is_import_call:
                continue
            if not node.args:
                found.append(cls.DYNAMIC_UNKNOWN)
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append(first.value)
            else:
                found.append(cls.DYNAMIC_UNKNOWN)
        return found

    @staticmethod
    def _merge_dynamic(module: str, imports: set[str], dynamic: list[str]) -> set[str]:
        """Fold literal *package* dynamic imports into the static edge set."""
        for target in dynamic:
            if target.startswith(f"{PACKAGE}.") and target != module:
                imports.add(target)
        return imports

    @staticmethod
    def _environ_uses_of(tree: ast.AST) -> list[str]:
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _ENVIRON_ATTRS:
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    hits.append(node.attr)
            elif isinstance(node, ast.ImportFrom) and node.module == "os":
                hits.extend(alias.name for alias in node.names if alias.name in _ENVIRON_ATTRS)
        return hits

    # -- helpers used by the rule tests ----------------------------------

    def modules_of(self, category: str) -> list[str]:
        return sorted(m for m in self.imports if _category(m) == category)

    def internal(self, module: str) -> set[str]:
        return {name for name in self.imports[module] if name.startswith(PACKAGE)}

    def find_edges(self, sources, forbidden_categories, forbidden_modules=()) -> list[tuple[str, str]]:
        """Return (source, target) pairs that violate a rule."""
        violations: list[tuple[str, str]] = []
        for source in sorted(sources):
            for target in sorted(self.internal(source)):
                if target in forbidden_modules or _category(target) in forbidden_categories:
                    violations.append((source, target))
        return violations

    def find_cycles(self) -> list[tuple[str, ...]]:
        internal = {m: {t for t in self.internal(m)} for m in self.imports}
        cycles: list[tuple[str, ...]] = []
        seen: set[frozenset[str]] = set()

        def walk(node: str, stack: list[str]) -> None:
            for target in internal.get(node, ()):  # only known package modules
                if target not in internal:
                    continue
                if target in stack:
                    cycle = tuple(stack[stack.index(target) :] + [target])
                    key = frozenset(cycle)
                    if key not in seen:
                        seen.add(key)
                        cycles.append(cycle)
                    continue
                walk(target, [*stack, target])

        for module in sorted(internal):
            walk(module, [module])
        return cycles


class ModuleBoundaryTests(unittest.TestCase):
    """One test per rule in docs/arch/02 §4."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.graph = ImportGraph()

    def test_source_tree_is_discovered(self) -> None:
        # Guards against a silently empty graph (e.g. wrong path), which would
        # make every rule below vacuously pass.
        self.assertGreaterEqual(len(self.graph.imports), 15)
        self.assertEqual(self.graph.errors, {})
        self.assertTrue(Path(SRC).is_dir())

    def test_adapters_are_discovered_from_the_directory(self) -> None:
        # Coverage-gap fix: a new ``crawl/<site>.py`` enters P7 without edits here.
        known = {f"{PACKAGE}.crawl.lyrics_ovh", f"{PACKAGE}.crawl.html_generic"}
        self.assertTrue(ADAPTER_MODULES, "no site adapter was discovered")
        self.assertTrue(known <= ADAPTER_MODULES, f"발견되지 않은 어댑터: {sorted(known - ADAPTER_MODULES)}")
        self.assertEqual(ADAPTER_MODULES & _CRAWL_INFRASTRUCTURE, set())
        for adapter in sorted(ADAPTER_MODULES):
            self.assertIn(adapter, self.graph.imports)

    def test_dynamic_imports_are_resolved_statically(self) -> None:
        # Coverage-gap fix: a non-literal dynamic import cannot be checked and
        # therefore fails instead of silently bypassing every rule below.
        unresolved = {
            module: targets
            for module, targets in sorted(self.graph.dynamic_imports.items())
            if ImportGraph.DYNAMIC_UNKNOWN in targets
        }
        self.assertEqual(unresolved, {}, f"정적으로 해석할 수 없는 동적 import: {unresolved}")

    def test_dynamic_import_detector_recognises_literal_and_opaque_targets(self) -> None:
        tree = ast.parse(
            "import importlib\n"
            "importlib.import_module('lyrics2pptx.pptx.fill')\n"
            "__import__('os')\n"
            "importlib.import_module(name)\n"
        )
        self.assertEqual(
            ImportGraph._dynamic_imports_of(tree),
            [f"{PACKAGE}.pptx.fill", "os", ImportGraph.DYNAMIC_UNKNOWN],
        )

    def test_literal_dynamic_package_import_joins_the_import_graph(self) -> None:
        edges = ImportGraph._merge_dynamic(
            f"{PACKAGE}.cli",
            {f"{PACKAGE}.config"},
            [f"{PACKAGE}.pptx.writer", "os", ImportGraph.DYNAMIC_UNKNOWN, f"{PACKAGE}.cli"],
        )
        self.assertIn(f"{PACKAGE}.pptx.writer", edges)
        self.assertNotIn("os", edges)
        self.assertNotIn(f"{PACKAGE}.cli", edges)  # never a self-edge

    def test_documented_writer_chain_exists(self) -> None:
        """docs/arch/02 §3: builder → writer → {writer_parts, writer_zip} → low level."""
        expected = {
            f"{PACKAGE}.pptx.builder": {f"{PACKAGE}.pptx.writer"},
            f"{PACKAGE}.pptx.writer": {
                f"{PACKAGE}.pptx.writer_parts",
                f"{PACKAGE}.pptx.writer_zip",
            },
            f"{PACKAGE}.pptx.writer_parts": {
                f"{PACKAGE}.pptx.clone",
                f"{PACKAGE}.pptx.fill",
                f"{PACKAGE}.pptx.template_reader",
            },
            f"{PACKAGE}.pptx.writer_zip": {
                f"{PACKAGE}.pptx.clone",
                f"{PACKAGE}.pptx.fill",
                f"{PACKAGE}.pptx.template_reader",
            },
        }
        for source, targets in sorted(expected.items()):
            missing = sorted(targets - self.graph.internal(source))
            self.assertEqual(missing, [], f"{source} → {missing} 엣지 누락 (docs/arch/02 §3)")

    def test_p1_text_never_imports_pptx(self) -> None:
        violations = self.graph.find_edges(self.graph.modules_of("text"), {"pptx"})
        self.assertEqual(violations, [], f"P1 위반: text → pptx {violations}")

    def test_p2_text_never_imports_crawl(self) -> None:
        violations = self.graph.find_edges(self.graph.modules_of("text"), {"crawl"})
        self.assertEqual(violations, [], f"P2 위반: text → crawl {violations}")

    def test_p3_pptx_never_imports_crawl(self) -> None:
        violations = self.graph.find_edges(self.graph.modules_of("pptx"), {"crawl"})
        self.assertEqual(violations, [], f"P3 위반: pptx → crawl {violations}")

    def test_p4_crawl_never_imports_text_or_pptx(self) -> None:
        violations = self.graph.find_edges(self.graph.modules_of("crawl"), {"text", "pptx"})
        self.assertEqual(violations, [], f"P4 위반: crawl → text/pptx {violations}")

    def test_p5_low_level_pptx_never_imports_builder(self) -> None:
        violations = self.graph.find_edges(
            PPTX_LOW_LEVEL, set(), forbidden_modules={f"{PACKAGE}.pptx.builder"}
        )
        self.assertEqual(violations, [], f"P5 위반: clone/fill/template_reader → builder {violations}")

    def test_p6_no_module_imports_an_entrypoint(self) -> None:
        violations = self.graph.find_edges(self.graph.imports, set(), forbidden_modules=ENTRYPOINTS)
        # An entrypoint importing itself is impossible via ast; keep the guard explicit.
        self.assertEqual(violations, [], f"P6 위반: 진입점 import {violations}")

    def test_p7_adapters_do_not_touch_urllib_directly(self) -> None:
        violations: list[tuple[str, str]] = []
        for adapter in sorted(ADAPTER_MODULES):
            for name in sorted(self.graph.imports.get(adapter, set())):
                top = name.split(".")[0]
                if top in {"urllib", "http", "socket"}:
                    violations.append((adapter, name))
        self.assertEqual(violations, [], f"P7 위반: 어댑터 직접 네트워크 사용 {violations}")

    def test_p8_import_graph_has_no_cycles(self) -> None:
        cycles = self.graph.find_cycles()
        rendered = [" → ".join(cycle) for cycle in cycles]
        self.assertEqual(cycles, [], f"P8 위반: 순환 의존 {rendered}")

    def test_p9_only_config_and_errors_touch_os_environ(self) -> None:
        violations = [
            (module, hits)
            for module, hits in sorted(self.graph.environ_uses.items())
            if hits and module not in CONFIG_OR_ERRORS
        ]
        self.assertEqual(violations, [], f"P9 위반: os.environ 접근 {violations}")


if __name__ == "__main__":
    unittest.main()
