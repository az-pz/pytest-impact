import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe")
SP = os.path.join(ROOT, "sample_project")


def git(*args):
    subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def reset():
    git("checkout", "--", ".")
    git("clean", "-fdq", "-e", "run_poc.py", "-e", ".venv")


def patch(relpath, old, new):
    p = os.path.join(ROOT, relpath)
    with open(p, "r", encoding="utf-8") as fh:
        s = fh.read()
    assert old in s, f"pattern not found in {relpath!r}: {old!r}"
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(s.replace(old, new, 1))


def add_file(relpath, content):
    p = os.path.join(ROOT, relpath)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)


def run_impact():
    out = subprocess.run(
        [PY, "-m", "pytest", "sample_project", "-p", "pytest_impact",
         "--impact", "--impact-explain", "--collect-only", "-q"],
        cwd=ROOT, capture_output=True, text=True,
    )
    selected = {}
    deselected = None
    for line in out.stdout.splitlines():
        if line.startswith("IMPACT-SELECTED "):
            rest = line[len("IMPACT-SELECTED "):]
            nodeid, reason = rest.split(" :: ", 1)
            selected[nodeid.split("::")[-1]] = reason
        elif line.startswith("IMPACT-DESELECTED-COUNT "):
            deselected = int(line.split()[-1])
    return selected, deselected, out.stdout


SCENARIOS = [
    ("No change at all (working tree == HEAD)", lambda: None,
     "expect: 0 selected"),
    ("Edit fixture 'user' body in tests/conftest.py",
     lambda: patch("sample_project/tests/conftest.py", '"regular"', '"regular_v2"'),
     "expect: ONLY test_create_user (override: test_admin_action uses sub's user)"),
    ("Edit fixture 'db_session' body in tests/conftest.py",
     lambda: patch("sample_project/tests/conftest.py", '"tx": "open"', '"tx": "OPEN"'),
     "expect: cascade -> test_list_users, test_create_user, test_admin_action"),
    ("Edit hook pytest_collection_modifyitems in root conftest.py",
     lambda: patch("sample_project/conftest.py", "    return\n",
                   "    items.sort(key=lambda i: i.nodeid)\n    return\n"),
     "expect: conservative subtree -> ALL 6 tests"),
    ("Edit test_config_defaults body only",
     lambda: patch("sample_project/tests/test_config.py", "== 3", "== 4"),
     "expect: ONLY test_config_defaults"),
    ("Decorator-only change: db_engine scope session->function",
     lambda: patch("sample_project/conftest.py",
                   '@pytest.fixture(scope="session")\ndef db_engine',
                   '@pytest.fixture(scope="function")\ndef db_engine'),
     "expect: db_engine users -> test_list_users, test_create_user, test_admin_action"),
    ("Add a brand-new test file",
     lambda: add_file("sample_project/tests/test_new.py",
                      "def test_new(app_config):\n    assert app_config\n"),
     "expect: ONLY test_new (newly added)"),
    ("Change a dependency manifest (requirements.txt)",
     lambda: add_file("requirements.txt", "pytest>=8\n"),
     "expect: fallback -> ALL 6 tests"),
]


def main():
    reset()
    width = 78
    for title, mutate, expectation in SCENARIOS:
        reset()
        mutate()
        selected, deselected, raw = run_impact()
        print("=" * width)
        print(f"SCENARIO: {title}")
        print(f"  {expectation}")
        print(f"  -> selected {len(selected)}, deselected {deselected}")
        for name in sorted(selected):
            print(f"       + {name}")
            print(f"           reason: {selected[name]}")
        if not selected:
            print("       (none selected)")
    reset()
    print("=" * width)


if __name__ == "__main__":
    sys.exit(main())
