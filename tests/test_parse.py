from pathlib import Path

from trailmark.parse import parse_repo


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def make_repo(root: Path) -> None:
    write(root, "pkg/__init__.py", "from .core import Engine\n")
    write(root, "pkg/core.py", (
        "from .util import helper\n"
        "from . import util\n"
        "class Base:\n"
        "    def ping(self):\n"
        "        return helper()\n"
        "class Engine(Base):\n"
        "    def run(self):\n"
        "        self.ping()\n"
        "        return util.other()\n"
    ))
    write(root, "pkg/util.py", "def helper():\n    return 1\n\ndef other():\n    return helper()\n")
    write(root, "app.py", "from pkg import Engine\n\ndef main():\n    return Engine().run()\n")
    write(root, "tests/test_app.py", "import app\n\ndef test_main():\n    app.main()\n")
    write(root, "broken.py", "def (:\n")


def edges(graph, kind):
    files = {f.id: f.path for f in graph.files}
    syms = {s.id: f"{s.path}::{s.qualname}" for s in graph.symbols}
    name = {**files, **syms}
    return {(name[a], name[b]) for (a, b) in graph.edges.get(kind, {})}


def test_parse_resolves_imports_calls_and_inheritance(tmp_path):
    make_repo(tmp_path)
    g = parse_repo(tmp_path)

    assert g.parse_errors == ["broken.py: SyntaxError"]
    assert {f.path for f in g.files} == {"pkg/__init__.py", "pkg/core.py", "pkg/util.py", "app.py", "tests/test_app.py"}
    assert next(f for f in g.files if f.path == "tests/test_app.py").is_test

    imports = edges(g, "IMPORTS")
    assert ("pkg/core.py", "pkg/util.py") in imports
    assert ("app.py", "pkg/__init__.py") in imports
    # re-export through __init__ is followed to the defining file
    assert ("app.py", "pkg/core.py") in imports
    assert ("tests/test_app.py", "app.py") in imports

    calls = edges(g, "CALLS")
    assert ("pkg/core.py::Base.ping", "pkg/util.py::helper") in calls
    # self.ping() resolves through the base class
    assert ("pkg/core.py::Engine.run", "pkg/core.py::Base.ping") in calls
    # module alias attribute call
    assert ("pkg/core.py::Engine.run", "pkg/util.py::other") in calls
    # class instantiation through a re-export, then a method on the result is opaque
    assert ("app.py::main", "pkg/core.py::Engine") in calls
    assert ("tests/test_app.py::test_main", "app.py::main") in calls

    assert ("pkg/core.py::Engine", "pkg/core.py::Base") in edges(g, "INHERITS")

    refs = edges(g, "REFS")
    assert ("pkg/core.py", "pkg/util.py") in refs
    assert ("app.py", "pkg/core.py") in refs
    # no self loops at file level
    assert all(a != b for a, b in refs)
