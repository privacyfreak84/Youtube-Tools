"""
Enforces the layer rules from DESIGN.md (section 15) by reading the code, so they hold without anyone remembering them.

- Only ytt/ui may print, ask for input, or import menu/argument libraries.
- Every module must be covered by a rule below, so a new module cannot slip in without declaring what it may use.
- Each area may import only the areas its rule lists. Research and compiling never import each other.
"""
import ast
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "ytt"

# module prefix -> the ytt prefixes it may import (a module may always import itself and its own prefix).
# The longest matching prefix decides. Third-party/stdlib imports are not restricted here, except for UI libraries.
RULES = {
    "ytt":                [],                                   # package root (version only)
    "ytt.engine":         [],                                   # ffmpeg code: imports nothing from the rest
    "ytt.sources":        [],                                   # yt-dlp access: imports nothing from the rest
    "ytt.workspace":      [],                                   # database, config, paths
    "ytt.ops.research":   ["ytt.sources", "ytt.workspace.paths"],
    "ytt.ops.compile":    ["ytt.sources", "ytt.workspace", "ytt.engine", "ytt.ops.plan", "ytt.ops.errors"],
    "ytt.ops.library":    ["ytt.sources", "ytt.workspace", "ytt.engine", "ytt.ops.compile", "ytt.ops.plan", "ytt.ops.errors"],
    "ytt.ops.legacy_import": ["ytt.workspace", "ytt.ops.compile.style"],
    "ytt.ops":            [],                                   # the ops package itself (plan, errors): pure data
    "ytt.ui":             ["ytt"],                              # may use anything below (checked separately)
}
UI_ONLY_IMPORTS = {"argparse", "rich", "questionary", "prompt_toolkit"}
UI_ONLY_CALLS = {"print", "input"}


def module_name(path):
    rel = path.relative_to(PKG.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def all_modules():
    return sorted((module_name(p), p) for p in PKG.rglob("*.py"))


def rule_for(name):
    best = None
    for prefix in RULES:
        if name == prefix or name.startswith(prefix + "."):
            if best is None or len(prefix) > len(best):
                best = prefix
    return best


def imports_of(path, this):
    """(imported module name, line) for every import in the file; relative imports are resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = this.split(".")
                base = base[:len(base) - node.level + (1 if path.name == "__init__.py" else 0)]
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append((mod, node.lineno))
            out += [(f"{mod}.{a.name}", node.lineno) for a in node.names]
    return out


class ArchitectureTests(unittest.TestCase):
    def test_every_module_is_covered_by_a_rule(self):
        for name, _ in all_modules():
            self.assertIsNotNone(rule_for(name), f"{name} has no rule in tests/test_architecture.py - "
                                                 f"decide which area it belongs to and what it may import")

    def test_modules_import_only_what_their_area_allows(self):
        for name, path in all_modules():
            rule = rule_for(name)
            if rule == "ytt.ui":
                continue
            own = rule
            allowed = RULES[rule] + [own, name]
            for mod, line in imports_of(path, name):
                if not (mod == "ytt" or mod.startswith("ytt.")):
                    continue
                if mod == "ytt":
                    continue
                ok = any(mod == a or mod.startswith(a + ".") or a.startswith(mod + ".") for a in allowed)
                self.assertTrue(ok, f"{name}:{line} imports {mod}, which the '{rule}' area may not use "
                                    f"(allowed: {RULES[rule] or 'nothing outside itself'})")

    def test_only_the_ui_prints_prompts_or_uses_menu_libraries(self):
        for name, path in all_modules():
            if rule_for(name) == "ytt.ui":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in UI_ONLY_CALLS:
                    self.fail(f"{name}:{node.lineno} calls {node.func.id}(); only ytt/ui may talk to the user")
            for mod, line in imports_of(path, name):
                self.assertNotIn(mod.split(".")[0], UI_ONLY_IMPORTS,
                                 f"{name}:{line} imports {mod}; only ytt/ui may use it")

    def test_research_and_compiling_never_import_each_other(self):
        for name, path in all_modules():
            for mod, line in imports_of(path, name):
                if name.startswith("ytt.ops.research") and mod.startswith(("ytt.ops.compile", "ytt.ops.library")):
                    self.fail(f"{name}:{line} research imports {mod}")
                if name.startswith(("ytt.ops.compile", "ytt.ops.library")) and mod.startswith("ytt.ops.research"):
                    self.fail(f"{name}:{line} compiling imports {mod}")

    def test_the_rules_catch_a_violation(self):
        """The test itself must be able to fail: check the matcher on invented cases."""
        self.assertEqual(rule_for("ytt.ops.research.outliers"), "ytt.ops.research")
        self.assertEqual(rule_for("ytt.ops.legacy_import"), "ytt.ops.legacy_import")
        self.assertEqual(rule_for("ytt.ops"), "ytt.ops")
        self.assertIsNone(rule_for("ytt_other"))
        self.assertNotIn("ytt.ops.compile", RULES["ytt.ops.research"])


if __name__ == "__main__":
    unittest.main()
