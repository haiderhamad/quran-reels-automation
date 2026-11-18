#!/usr/bin/env python3
"""Run the Python regression suite in explicit, auditable CI partitions.

Only Git-tracked tests are release checks; unrelated local files are left alone.
CI uses three
partitions because the alignment tests intentionally validate a pinned
QuranEnc dataset and may need network access, while repository-history
safety has a different cost profile and must remain its own gate.
"""

from __future__ import annotations

import argparse
import sys
import subprocess
import unittest
from collections.abc import Iterable, Iterator
from pathlib import Path


TEST_ROOT = "tests"
ALIGNMENT_MODULE = "test_engine_alignments"
SAFETY_MODULE = "test_repository_safety"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

# Running a script by path puts ``scripts/`` on sys.path.  Put the repository
# root first so discovery imports the checked-in test modules rather than an
# unrelated installed package named ``tests``.
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _flatten(suite: unittest.TestSuite) -> Iterator[unittest.case.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _discover(*, all_local: bool = False) -> list[unittest.case.TestCase]:
    loader = unittest.TestLoader()
    if all_local:
        paths = sorted((REPOSITORY_ROOT / TEST_ROOT).glob("test_*.py"))
    else:
        output = subprocess.run(
            ["git", "ls-files", "-z", "--", "tests/test_*.py"],
            cwd=REPOSITORY_ROOT, check=True, capture_output=True,
        ).stdout
        paths = [REPOSITORY_ROOT / path.decode() for path in output.split(b"\0") if path]
    if not paths:
        raise RuntimeError("No release test modules were discovered")
    tests = list(_flatten(loader.loadTestsFromNames([
        "tests." + path.stem for path in paths
    ])))
    if loader.errors:
        raise RuntimeError("Test discovery failed:\n" + "\n".join(loader.errors))
    return tests


def _module_name(test: unittest.case.TestCase) -> str:
    return test.__class__.__module__.removeprefix("tests.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--offline",
        action="store_true",
        help="run deterministic tests, excluding source and history safety gates",
    )
    modes.add_argument(
        "--source-integrity",
        action="store_true",
        help="run the QuranEnc-backed alignment/source integrity module",
    )
    modes.add_argument("--repository-safety", action="store_true", help="run history safety tests")
    parser.add_argument(
        "--fail-on-skip",
        action="store_true",
        help="fail if any selected test is skipped (required in the CV-enabled CI job)",
    )
    parser.add_argument("--all-local", action="store_true", help="also discover untracked local tests")
    return parser.parse_args()


def _select_tests(tests: Iterable[unittest.case.TestCase], mode: str) -> tuple[list[unittest.case.TestCase], list[str]]:
    all_tests = list(tests)
    if mode == "offline":
        selected = [
            test
            for test in all_tests
            if _module_name(test) not in {ALIGNMENT_MODULE, SAFETY_MODULE}
        ]
    elif mode == "source-integrity":
        selected = [test for test in all_tests if _module_name(test) == ALIGNMENT_MODULE]
    elif mode == "repository-safety":
        selected = [test for test in all_tests if _module_name(test) == SAFETY_MODULE]
    else:
        selected = all_tests
    excluded = [test.id() for test in all_tests if test not in selected]
    return selected, excluded


def main() -> int:
    args = _parse_args()
    mode = ("source-integrity" if args.source_integrity else "offline" if args.offline
            else "repository-safety" if args.repository_safety else "complete")
    try:
        tests = _discover(all_local=args.all_local)
        partitions = [_select_tests(tests, partition)[0] for partition in
                      ("offline", "source-integrity", "repository-safety")]
        identities = [test.id() for partition in partitions for test in partition]
        if len(identities) != len(tests) or len(set(identities)) != len(tests):
            raise RuntimeError("Test partitions are incomplete or overlapping")
        selected, excluded = _select_tests(tests, mode)
        if not selected:
            raise RuntimeError("Selected test partition is empty")
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(f"test_partition={mode} selected={len(selected)} excluded={len(excluded)}", flush=True)
    if excluded:
        print("excluded_test_modules=" + ",".join(sorted({
            _module_name(test) for test in tests if test.id() in excluded
        })), flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
    if args.fail_on_skip and result.skipped:
        print("Skipped tests are forbidden by --fail-on-skip:", file=sys.stderr)
        for test, _reason in result.skipped:
            print(f"  {test.id()}", file=sys.stderr)
        return 1
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
