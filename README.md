# taskmine

Make agent benchmark tasks from a repo's own history. A fix commit whose test is red
at the parent and green at the fix becomes a task with a proven check: start from the
parent, make the check pass. taskmine mines and proves such tasks. It does not run an
agent, so any runner can use the output.

```
pip install git+https://github.com/decentralizeallengineering/taskmine
```

## 30-second tour

```
$ taskmine mine ./my-repo --check "python -m pytest -q {files}" --want 3
9f2c1ab04e	skip: parent run exit 0: green, not a fix
3b7d0c9e11	task
c4a8e2d760	skip: parent run exit 2: not a plain failure (collection error, timeout or missing tool)
51e90fb3aa	task
2 proven tasks written to tasks.tsv

$ taskmine checkout tasks.tsv ./my-repo t1 /tmp/t1
/tmp/t1 holds 8d41f07c22 plus tests of 3b7d0c9e11; check: python -m pytest -q {files}
```

`/tmp/t1` now holds the parent commit's tree plus the fix commit's test files. The check
fails there. An agent edits the source until it passes; you rerun the check outside the
agent. The sample output above is the shape of a run, with your own commit ids.

## How a commit becomes a task

1. Single-parent commit that changes at least one test file and at least one other file,
   with at most `--max-lines` changed lines outside tests (default 200).
   Test files: a `tests/`, `test/`, `spec/` folder, `test_*` or `*_test.*` or `*.test.*`
   names (`--test-re` replaces the pattern).
2. The check runs at the parent with the fix's test files put on top. It must exit with
   a code in `--red-rc` (default `1`). Exit 0 means the test was already green. Any other
   code, a timeout or a missing tool means a collection error or a broken setup, so the
   commit is skipped, not counted as red.
3. The same check runs at the fix and must exit 0.
4. With `--repeat N` each end runs N times. One run that disagrees rejects the commit as
   flaky, so a task that proved once does not fail later for no reason.

Each run is in a throwaway `git worktree`, removed afterwards. Your working tree is never touched.

## Commands

- `taskmine mine REPO --check CMD [--rev HEAD] [--limit 200] [--want 10] [--max-lines 200] [--red-rc 1] [--timeout 120] [--repeat 1] [--out tasks.tsv]`
  `{files}` in CMD becomes the commit's test files; without it they are appended.
- `taskmine prove tasks.tsv REPO [--repeat 1]` reruns red and green for each task. Exit 1 if any fails.
- `taskmine checkout tasks.tsv REPO ID DEST` unpacks parent plus test files into an empty DEST.

`tasks.tsv` columns: `id, base_sha, fix_sha, test_files, check, source_lines, subject`.
Python: `from taskmine import mine, prove, checkout`. Standard library and git only.

Not covered: tests that depend on the machine or a service (they will not prove), and
a fair task text. Whether a commit message hints too much is for you to judge.

## Tests

```
pip install -e ".[test]" && pytest
```

Licence: Apache-2.0.
