"""taskmine: turn a repo's own history into agent tasks that carry a proven check.

A fix commit becomes a task when it changes both test files and other files, the commit's test files
fail on the parent commit, and the same check passes at the fix. Then "make this check pass starting from
the parent" is a task with a ground truth. The tool only mines and proves; it does not run any agent.

  taskmine mine REPO --check "python -m pytest -q {files}"   write tasks.tsv with every proven task
  taskmine prove tasks.tsv REPO                              run each task's red and green check again
  taskmine checkout tasks.tsv REPO ID DEST                   unpack the parent plus the fix's test files into DEST

Tasks file (tab separated): id, base_sha, fix_sha, test_files (comma), check, source_lines, subject.
"""
import argparse
import csv
import io
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time

__version__ = "0.2.0"
TEST_RE = r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]*$|_test\.[A-Za-z]+$|\.(test|spec)\.[A-Za-z]+$"
FIELDS = ["id", "base_sha", "fix_sha", "test_files", "check", "source_lines", "subject"]


class MineError(Exception):
    pass


def git(repo, *args, raw=False):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, timeout=120)
    if p.returncode:
        raise MineError(f"git {' '.join(args[:2])} failed: {p.stderr.decode(errors='replace').strip()[:200]}")
    return p.stdout if raw else p.stdout.decode(errors="replace")


def candidates(repo, rev="HEAD", limit=200, test_re=TEST_RE, max_lines=200):
    """Yield (sha, parent, test_files, source_lines, subject) for single-parent commits that change tests and other files."""
    rx = re.compile(test_re)
    out = git(repo, "log", rev, "--no-merges", "-n", str(limit), "--format=%H %P%x09%s")
    for line in out.splitlines():
        head, _, subject = line.partition("\t")
        parts = head.split()
        if len(parts) != 2:
            continue  # root commit has no parent
        sha, parent = parts
        stat = git(repo, "diff-tree", "--no-commit-id", "--numstat", "--no-renames", "-r", sha).splitlines()
        status = dict(l.split("\t", 1)[::-1] for l in git(repo, "diff-tree", "--no-commit-id", "--name-status", "--no-renames", "-r", sha).splitlines())
        tests, lines, other = [], 0, 0
        for s in stat:
            a, d, path = s.split("\t", 2)
            if status.get(path) == "D":
                continue
            if rx.search(path):
                tests.append(path)
            else:
                other += 1
                lines += (int(a) if a.isdigit() else 0) + (int(d) if d.isdigit() else 0)
        if tests and other and lines <= max_lines:
            yield sha, parent, sorted(tests), lines, subject


def _command(check, files):
    q = " ".join(shlex.quote(f) for f in files)
    return shlex.split(check.replace("{files}", q)) if "{files}" in check else shlex.split(check) + list(files)


def run_at(repo, rev, overlay_rev, files, check, timeout=120):
    """Check out rev in a throwaway worktree, put the files from overlay_rev on top, run the check. Returns (rc, output_tail)."""
    tmp = tempfile.mkdtemp(prefix="taskmine-")
    wt = os.path.join(tmp, "wt")
    git(repo, "worktree", "add", "-q", "--detach", wt, rev)
    try:
        if overlay_rev != rev:
            git(wt, "checkout", overlay_rev, "--", *files)
        try:
            p = subprocess.run(_command(check, files), cwd=wt, capture_output=True, text=True, timeout=timeout)
            return p.returncode, (p.stdout + p.stderr).strip()[-300:]
        except subprocess.TimeoutExpired:
            return 124, "timeout"
        except FileNotFoundError as e:
            return 127, str(e)
    finally:
        subprocess.run(["git", "-C", repo, "worktree", "remove", "--force", wt], capture_output=True)
        subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True)
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def prove(repo, base, fix, files, check, red_rc=(1,), timeout=120, repeat=1):
    """Return (True, "") when the check is red at base (exit code in red_rc) and green (0) at fix; else (False, reason).

    With repeat > 1 every run is done that many times at each end; one disagreeing run rejects the task as flaky."""
    for i in range(max(1, repeat)):
        rc, out = run_at(repo, base, fix, files, check, timeout)
        if rc not in red_rc:
            if i:
                return False, f"flaky: parent run {i + 1} of {repeat} exit {rc}"
            return False, f"parent run exit {rc}: " + ("green, not a fix" if rc == 0 else "not a plain failure (collection error, timeout or missing tool)")
    for i in range(max(1, repeat)):
        rc2, out2 = run_at(repo, fix, fix, files, check, timeout)
        if rc2 != 0:
            if i:
                return False, f"flaky: fix run {i + 1} of {repeat} exit {rc2}"
            return False, f"fix run exit {rc2}: still red at the fix"
    return True, ""


def mine(repo, check, rev="HEAD", limit=200, want=10, max_lines=200, red_rc=(1,), timeout=120, test_re=TEST_RE, log=None, repeat=1):
    tasks = []
    for sha, parent, tests, lines, subject in candidates(repo, rev, limit, test_re, max_lines):
        ok, why = prove(repo, parent, sha, tests, check, red_rc, timeout, repeat)
        if log:
            log(f"{sha[:10]}\t{'task' if ok else 'skip: ' + why}")
        if ok:
            tasks.append({"id": f"t{len(tasks) + 1}", "base_sha": parent, "fix_sha": sha, "test_files": ",".join(tests),
                          "check": check, "source_lines": lines, "subject": subject.replace("\t", " ")})
            if len(tasks) >= want:
                break
    return tasks


def write_tasks(path, tasks):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(tasks)


def read_tasks(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def checkout(repo, task, dest):
    """Unpack the parent tree, then the fix commit's test files, into the empty folder dest."""
    os.makedirs(dest, exist_ok=True)
    if os.listdir(dest):
        raise MineError(f"{dest} is not empty")
    files = task["test_files"].split(",")
    for rev, paths in ((task["base_sha"], []), (task["fix_sha"], files)):
        data = git(repo, "archive", "--format=tar", rev, "--", *paths, raw=True)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            t.extractall(dest, filter="data") if hasattr(tarfile, "data_filter") else t.extractall(dest)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="taskmine", description="Mine proven agent tasks from a repo's history.")
    ap.add_argument("--version", action="version", version="taskmine " + __version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mine")
    m.add_argument("repo")
    m.add_argument("--check", required=True, help='test command; {files} is replaced by the commit\'s test files, else they are appended')
    m.add_argument("--rev", default="HEAD")
    m.add_argument("--limit", type=int, default=200, help="newest commits to look at")
    m.add_argument("--want", type=int, default=10, help="stop after this many tasks")
    m.add_argument("--max-lines", type=int, default=200, help="most changed lines outside tests")
    m.add_argument("--red-rc", default="1", help="exit codes that count as a failing test (default 1); others, such as a collection error, are skipped")
    m.add_argument("--timeout", type=int, default=120)
    m.add_argument("--test-re", default=TEST_RE, help="regex for test file paths")
    m.add_argument("--out", default="tasks.tsv")
    m.add_argument("--repeat", type=int, default=1, help="run each check this many times at each end; any disagreement marks the task flaky")
    p = sub.add_parser("prove")
    p.add_argument("tasks")
    p.add_argument("repo")
    p.add_argument("--red-rc", default="1")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--repeat", type=int, default=1)
    c = sub.add_parser("checkout")
    c.add_argument("tasks")
    c.add_argument("repo")
    c.add_argument("id")
    c.add_argument("dest")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "mine":
            tasks = mine(a.repo, a.check, a.rev, a.limit, a.want, a.max_lines, tuple(int(x) for x in a.red_rc.split(",")), a.timeout, a.test_re,
                         log=lambda s: print(s, file=sys.stderr), repeat=a.repeat)
            write_tasks(a.out, tasks)
            print(f"{len(tasks)} proven tasks written to {a.out}")
            return 0 if tasks else 1
        if a.cmd == "prove":
            bad = 0
            for t in read_tasks(a.tasks):
                ok, why = prove(a.repo, t["base_sha"], t["fix_sha"], t["test_files"].split(","), t["check"], tuple(int(x) for x in a.red_rc.split(",")), a.timeout, a.repeat)
                print(f"{t['id']}\t{'proven' if ok else 'FAILED ' + why}")
                bad += not ok
            return 1 if bad else 0
        t = next((x for x in read_tasks(a.tasks) if x["id"] == a.id), None)
        if t is None:
            raise MineError(f"no task {a.id}")
        checkout(a.repo, t, a.dest)
        print(f"{a.dest} holds {t['base_sha'][:10]} plus tests of {t['fix_sha'][:10]}; check: {t['check']}")
        return 0
    except MineError as e:
        print("taskmine: " + str(e), file=sys.stderr)
        return 2
