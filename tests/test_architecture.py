"""Import rules that keep the modules in layers (eng review R5, R6, outside voice O6).

An outside import that breaks a rule fails here, so the rules do not depend on anyone remembering them.
"""

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "postpilot"


def _imports(path: Path) -> set[str]:
    """Names of postpilot modules a file imports (relative imports only: `from . import x`, `from .x import y`)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                found.add(node.module.split(".")[0])
            else:
                found.update(alias.name for alias in node.names)
    return found


def _deps(module: str) -> set[str]:
    return _imports(PKG / f"{module}.py")


def test_ranking_is_pure_and_imports_nothing_of_ours():
    assert _deps("ranking") == set()


def test_the_pipeline_never_imports_reports_or_the_cli_file():
    assert not _deps("pipeline") & {"reports", "__main__"}


def test_reports_may_use_the_pipeline_but_never_the_cli_file():
    assert "__main__" not in _deps("reports")


def test_the_display_code_does_not_reach_into_the_model_layer():
    assert "llm" not in _deps("render")


def test_nothing_in_the_package_imports_the_cli_file():
    offenders = [p.name for p in PKG.rglob("*.py") if p.name != "__main__.py" and "__main__" in _imports(p)]
    assert offenders == []


def test_the_eval_uses_only_public_pipeline_names():
    """The eval once imported three private functions; they are public now and stay that way."""
    tree = ast.parse((PKG / "evals" / "runner.py").read_text(encoding="utf-8"))
    private = [n.attr for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "pipeline"
               and n.attr.startswith("_")]
    assert private == []


def test_the_lower_layers_do_not_import_the_flow():
    low = ["config", "db", "llm", "sources", "factgate", "brain", "mailer"]
    assert [m for m in low if _deps(m) & {"pipeline", "reports", "render", "__main__"}] == []
