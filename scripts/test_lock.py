"""Cross-platform test for pipeline._acquire_lock (the real function, not a copy).

Runs on Windows (msvcrt) and Linux/macOS (fcntl.flock). CI executes it on both
branches so the flock path is verified on a real Unix kernel, not a stub.

    python scripts/test_lock.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)

# try to take the lock once and print the boolean
CHILD_TRY = (
    "import sys; sys.path.insert(0, sys.argv[1]);"
    "from pipeline import _acquire_lock;"
    "print(_acquire_lock('ci-test'))"
)
# take the lock, hold it a moment, then exit (exit must release it)
CHILD_HOLD = (
    "import sys, time; sys.path.insert(0, sys.argv[1]);"
    "from pipeline import _acquire_lock;"
    "ok = _acquire_lock('ci-test'); time.sleep(8);"
    "sys.exit(0 if ok else 1)"
)


def run(code: str, cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code, REPO, *args],
                          cwd=cwd, capture_output=True, text=True, timeout=60)


def main() -> int:
    print(f"[lock-test] platform={os.name} python={sys.version.split()[0]}")
    with tempfile.TemporaryDirectory() as td:
        # 1) same process: first acquire wins, second must fail (both branches
        #    conflict on a second fd: msvcrt on byte 0, flock per open-file-desc)
        r = run(
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from pipeline import _acquire_lock;"
            "print('first', _acquire_lock('ci-test'));"
            "print('second', _acquire_lock('ci-test'))",
            td)
        assert r.stdout.splitlines()[-2:] == ["first True", "second False"], \
            f"got: {r.stdout!r} err: {r.stderr!r}"

        # 2) cross-process: a live holder blocks the contender -- the exact
        #    scenario auto_keepalive vs manual run on Linux/macOS
        holder = subprocess.Popen([sys.executable, "-c", CHILD_HOLD, REPO],
                                  cwd=td)
        try:
            import time
            time.sleep(2)
            child = run(CHILD_TRY, td)
            assert child.stdout.strip() == "False", \
                f"contender got: {child.stdout!r} err: {child.stderr!r}"
        finally:
            holder.wait(timeout=30)
        assert holder.returncode == 0, f"holder failed: {holder.returncode}"

        # 3) after the holder exits, a fresh process can take the lock again
        #    (the on-disk lock file is inert; nothing to clean up)
        child = run(CHILD_TRY, td)
        assert child.stdout.strip() == "True", \
            f"after holder exit got: {child.stdout!r} err: {child.stderr!r}"

    print("[lock-test] all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
