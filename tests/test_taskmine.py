import os
import subprocess
import sys

import pytest

from taskmine import MineError, candidates, checkout, main, mine, prove, read_tasks, write_tasks

CHECK = f"{sys.executable} {{files}}"  # each test file is a plain script: an assertion error exits 1
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True, env=ENV).stdout.strip()


def put(repo, name, text):
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def commit(repo, msg):
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD")


def fix_sha(repo):
    return git(repo, "rev-parse", "main~2")


TEST_ADD = "import sys\nsys.path.insert(0, '.')\nfrom calc import add\nassert add(2, 3) == 5\n"


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    put(r, "calc.py", "def add(a, b):\n    return a - b\n")
    commit(r, "start")  # c1
    put(r, "notes.txt", "x\n")
    commit(r, "docs only")  # c2: no tests
    put(r, "calc.py", "def add(a, b):\n    return a + b\n")
    put(r, "tests/test_add.py", TEST_ADD)
    commit(r, "fix add")  # c3: the task
    put(r, "calc.py", "def add(a, b):\n    return a + b  # same\n")
    put(r, "tests/test_add.py", TEST_ADD + "assert add(0, 0) == 0\n")
    commit(r, "polish")  # c4: test already green at the parent
    put(r, "tests/test_other.py", "assert True\n")
    commit(r, "tests only")  # c5: no source change
    return r


def test_candidates_need_tests_and_source(repo):
    subjects = [c[4] for c in candidates(str(repo))]
    assert subjects == ["polish", "fix add"]


def test_mine_keeps_only_red_then_green(repo):
    log = []
    tasks = mine(str(repo), CHECK, log=log.append)
    assert [t["subject"] for t in tasks] == ["fix add"]
    t = tasks[0]
    assert t["fix_sha"] == fix_sha(repo) and t["base_sha"] == git(repo, "rev-parse", fix_sha(repo) + "^") and t["test_files"] == "tests/test_add.py"
    assert any("green, not a fix" in l for l in log)


def test_prove_rejects_wrong_exit_code_and_unfixed(repo):
    parent = git(repo, "rev-parse", fix_sha(repo) + "^")
    assert prove(str(repo), parent, fix_sha(repo), ["tests/test_add.py"], CHECK) == (True, "")
    ok, why = prove(str(repo), parent, fix_sha(repo), ["tests/test_add.py"], CHECK, red_rc=(2,))
    assert not ok and "not a plain failure" in why
    ok, why = prove(str(repo), parent, fix_sha(repo), [], f"{sys.executable} -c 'raise SystemExit(1)'")  # red at both ends
    assert not ok and "still red" in why


def test_tasks_file_round_trip_and_checkout_is_red(repo, tmp_path):
    tasks = mine(str(repo), CHECK)
    tf = tmp_path / "tasks.tsv"
    write_tasks(str(tf), tasks)
    assert read_tasks(str(tf)) == [{k: str(v) for k, v in tasks[0].items()}]
    dest = tmp_path / "work"
    checkout(str(repo), read_tasks(str(tf))[0], str(dest))
    assert (dest / "tests" / "test_add.py").read_text() == TEST_ADD and "a - b" in (dest / "calc.py").read_text()
    assert subprocess.run([sys.executable, "tests/test_add.py"], cwd=dest, capture_output=True).returncode == 1
    with pytest.raises(MineError):
        checkout(str(repo), read_tasks(str(tf))[0], str(dest))  # not empty


def test_cli_mine_prove_checkout(repo, tmp_path, capsys):
    tf = str(tmp_path / "t.tsv")
    assert main(["mine", str(repo), "--check", CHECK, "--out", tf]) == 0
    assert "1 proven tasks" in capsys.readouterr().out
    assert main(["prove", tf, str(repo)]) == 0
    assert main(["checkout", tf, str(repo), "t1", str(tmp_path / "w")]) == 0
    assert main(["checkout", tf, str(repo), "t9", str(tmp_path / "w2")]) == 2
    assert git(repo, "worktree", "list").count("\n") == 0  # throwaway worktrees are gone
