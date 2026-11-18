#!/usr/bin/env python3
"""Fail when the current repository tree contains known deployment hazards.

This intentionally uses only the Python standard library so it can run before
project dependencies are installed. Findings report a rule and location, never
the matched value.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


MAX_TEXT_FILE_BYTES = 1_000_000

UNQUOTED_CONFIG_SUFFIXES = {
    ".cfg",
    ".conf",
    ".env",
    ".example",
    ".ini",
    ".properties",
    ".toml",
    ".yaml",
    ".yml",
}

BANNED_ACTIVE_WORKFLOW_NAMES = {
    "build-desktop.yml",
    "build-desktop.yaml",
    "deploy-hf.yml",
    "deploy-hf.yaml",
}

BANNED_ACTIVE_WORKFLOW_MARKERS = (
    "Habib-HF/Quran-Reels-Generator",
    "Afrasyaab-GH/Quran-Reels-Generator",
    "huggingface_hub",
    "softprops/action-gh-release",
)

SECRET_PATTERNS = (
    (
        "private-key material",
        re.compile(r"-----BEGIN\s+(?:EC|OPENSSH|PGP|RSA)?\s*PRIVATE KEY-----"),
    ),
    (
        "GitHub token literal",
        re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b"),
    ),
    (
        "AWS access-key literal",
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    ),
    (
        "Google API-key literal",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    (
        "Stripe live-key literal",
        re.compile(r"\bsk_live_[0-9A-Za-z]{16,}\b"),
    ),
    (
        "Slack token literal",
        re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    ),
    (
        "npm token literal",
        re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b"),
    ),
    (
        "high-entropy credential-like quoted literal",
        re.compile(
            r"[\"']"
            r"(?=[A-Za-z0-9_-]{32,}[\"'])"
            r"(?=[A-Za-z0-9_-]*[a-z])"
            r"(?=[A-Za-z0-9_-]*[A-Z])"
            r"(?=[A-Za-z0-9_-]*[0-9])"
            r"[A-Za-z0-9_-]+[\"']"
        ),
    ),
)

PROVIDER_TOKEN_RULES = {
    "AWS access-key literal",
    "GitHub token literal",
    "Google API-key literal",
    "npm token literal",
    "Slack token literal",
    "Stripe live-key literal",
}

PROVIDER_TOKEN_PREFIXES = (
    "akia",
    "asia",
    "aiza",
    "gho_",
    "ghp_",
    "ghr_",
    "ghs_",
    "ghu_",
    "github_pat_",
    "npm_",
    "sk_live_",
    "xoxa-",
    "xoxb-",
    "xoxp-",
    "xoxr-",
    "xoxs-",
)

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"""
    (?P<key_quote>[\"']?)
    (?P<name>
        (?:[A-Za-z][A-Za-z0-9_]*_)?
        (?:
            ACCESS_KEY(?:_ID)?|API_KEYS?|PASSWORD(?:_HASH)?|PASSWD|
            PRIVATE_KEY|SECRET|TOKEN
        )
    )
    (?P=key_quote)
    \s*(?:=|:)\s*
    (?:
        (?P<value_quote>[\"'])(?P<quoted_value>[^\"'\r\n]*)(?P=value_quote)
        |
        (?P<unquoted_value>[^\s,;#}\]\"'\r\n]+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

PLACEHOLDER_PREFIXES = (
    "change_me",
    "change-me",
    "changeme",
    "dummy",
    "example",
    "placeholder",
    "redacted",
    "replace_me",
    "replace-me",
    "your_",
    "your-",
)


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    line: int
    rule: str

    def format(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


@dataclass(frozen=True, order=True)
class HistoryViolation:
    ref: str
    path: str
    line: int
    rule: str
    blob: str

    def format(self) -> str:
        return f"{self.ref}:{self.path}:{self.line}: {self.rule} [blob={self.blob[:12]}]"


class SafetyCheckError(RuntimeError):
    """Raised when the repository cannot be scanned safely."""


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _is_active_workflow(relative_path: str) -> bool:
    path = Path(relative_path)
    return (
        path.parent.as_posix() == ".github/workflows"
        and path.suffix.lower() in {".yml", ".yaml"}
    )


def _should_scan_path(relative_path: str, size: int) -> bool:
    return _is_active_workflow(relative_path) or size <= MAX_TEXT_FILE_BYTES


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    # Kubernetes templates use a literal replacement instruction followed by
    # an environment variable name. Accept only this complete uppercase form.
    if stripped == "REPLACE_AT_RUNTIME" or re.fullmatch(r"REPLACE_WITH_[A-Z][A-Z0-9_]*", stripped):
        return True
    normalized = stripped.lower()
    return not normalized or normalized.startswith(PLACEHOLDER_PREFIXES)


def _is_secret_reference(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized.startswith(
        (
            "${",
            "$env:",
            "env.",
            "getenv(",
            "os.environ",
            "os.getenv",
            "process.env",
            "secrets.",
            "vars.",
        )
    ):
        return True
    return re.fullmatch(r"\$[a-z_][a-z0-9_]*", normalized) is not None


def _is_obvious_fake_provider_token(value: str) -> bool:
    normalized = value.strip().lower()
    for prefix in PROVIDER_TOKEN_PREFIXES:
        if not normalized.startswith(prefix):
            continue
        suffix = normalized[len(prefix) :].strip("-_")
        return (
            _is_placeholder(suffix)
            or bool(suffix) and set(suffix) <= {"0", "x"}
        )
    return False


def scan_text(relative_path: str, text: str) -> list[Violation]:
    """Return sanitized violations found in one tracked text file."""
    violations: list[Violation] = []

    if _is_active_workflow(relative_path):
        workflow_name = Path(relative_path).name.lower()
        if workflow_name in BANNED_ACTIVE_WORKFLOW_NAMES:
            violations.append(
                Violation(relative_path, 1, "legacy publish/release workflow is active")
            )

        lowered_markers = tuple(marker.lower() for marker in BANNED_ACTIVE_WORKFLOW_MARKERS)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            lowered_line = line.lower()
            if any(marker in lowered_line for marker in lowered_markers):
                violations.append(
                    Violation(
                        relative_path,
                        line_number,
                        "active workflow references a prohibited imported deployment target",
                    )
                )

    for rule, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if (
                rule in PROVIDER_TOKEN_RULES
                and _is_obvious_fake_provider_token(match.group(0))
            ):
                continue
            violations.append(
                Violation(relative_path, _line_number(text, match.start()), rule)
            )

    matches = SENSITIVE_ASSIGNMENT_RE.finditer(text)
    if Path(relative_path).name.startswith(".env"):
        # A blank dotenv assignment ends at its newline. Keep offsets into the
        # original file so later secrets are reported at their own line.
        matches = (match for line in re.finditer(r"[^\r\n]+", text)
                   for match in SENSITIVE_ASSIGNMENT_RE.finditer(text, line.start(), line.end()))
    for match in matches:
        quoted_value = match.group("quoted_value")
        value = quoted_value if quoted_value is not None else match.group("unquoted_value")
        if quoted_value is None and Path(relative_path).suffix.lower() not in UNQUOTED_CONFIG_SUFFIXES:
            continue
        if len(value) < 8 or _is_placeholder(value) or _is_secret_reference(value):
            continue
        violations.append(
            Violation(
                relative_path,
                _line_number(text, match.start()),
                "credential-like literal assigned to a sensitive setting",
            )
        )

    deduplicated: dict[tuple[str, int], Violation] = {}
    for violation in violations:
        deduplicated.setdefault((violation.path, violation.line), violation)
    return sorted(deduplicated.values())


def _repository_files(repository_root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
        relative_paths = [path for path in result.stdout.split(b"\0") if path]
        return [repository_root / path.decode("utf-8") for path in relative_paths]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        raise SafetyCheckError("unable to enumerate repository files") from error


def scan_repository(repository_root: Path) -> list[Violation]:
    violations: list[Violation] = []

    for path in _repository_files(repository_root):
        relative_path = path.relative_to(repository_root).as_posix()
        if path.is_symlink() or not path.is_file():
            continue
        if not _should_scan_path(relative_path, path.stat().st_size):
            continue

        data = path.read_bytes()
        if b"\0" in data:
            continue

        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue

        violations.extend(scan_text(relative_path, text))

    return sorted(set(violations))


def _git_output(repository_root: Path, arguments: list[str]) -> bytes:
    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            env=environment,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise SafetyCheckError("unable to inspect local Git objects") from error


def _local_ref_snapshot(repository_root: Path) -> tuple[tuple[str, str, str], ...]:
    output = _git_output(
        repository_root,
        ["for-each-ref", "--format=%(refname) %(objectname) %(objecttype)"],
    )
    refs = []
    try:
        for line in output.decode("utf-8").splitlines():
            ref_name, object_id, object_type = line.split(" ", 2)
            refs.append((ref_name, object_id, object_type))
    except (UnicodeDecodeError, ValueError) as error:
        raise SafetyCheckError("unable to parse local Git refs") from error

    environment = dict(os.environ)
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    symbolic_head = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
    )
    if symbolic_head.returncode not in (0, 1):
        raise SafetyCheckError("unable to inspect HEAD")
    if symbolic_head.returncode == 1:
        object_id = _git_output(repository_root, ["rev-parse", "HEAD"]).decode().strip()
        object_type = _git_output(
            repository_root,
            ["cat-file", "-t", object_id],
        ).decode().strip()
        refs.append(("HEAD", object_id, object_type))

    return tuple(sorted(refs))


def _peeled_object(
    repository_root: Path,
    ref_name: str,
    object_id: str,
    object_type: str,
) -> tuple[str, str]:
    if object_type != "tag":
        return object_id, object_type
    peeled_id = _git_output(
        repository_root,
        ["rev-parse", f"{ref_name}^{{}}"],
    ).decode().strip()
    peeled_type = _git_output(
        repository_root,
        ["cat-file", "-t", peeled_id],
    ).decode().strip()
    return peeled_id, peeled_type


def _trees_for_ref(
    repository_root: Path,
    object_id: str,
    object_type: str,
) -> list[str]:
    if object_type == "tree":
        return [object_id]
    if object_type != "commit":
        return []
    # Ask Git for tree identities in one traversal instead of spawning one
    # rev-parse process per historical commit. Repeated trees need scanning once.
    return sorted(set(_git_output(
        repository_root,
        ["rev-list", "--format=%T", "--no-commit-header", object_id],
    ).decode().splitlines()))


def _tree_blobs(repository_root: Path, tree_id: str) -> list[tuple[str, str]]:
    output = _git_output(
        repository_root,
        ["ls-tree", "-rz", "--full-tree", tree_id],
    )
    blobs = []
    try:
        for entry in output.split(b"\0"):
            if not entry:
                continue
            metadata, path_bytes = entry.split(b"\t", 1)
            _mode, object_type, object_id = metadata.split(b" ", 2)
            if object_type != b"blob":
                continue
            blobs.append(
                (
                    path_bytes.decode("utf-8", errors="replace"),
                    object_id.decode("ascii"),
                )
            )
    except (UnicodeDecodeError, ValueError) as error:
        raise SafetyCheckError("unable to parse a reachable Git tree") from error
    return blobs


def scan_all_local_refs(repository_root: Path) -> list[HistoryViolation]:
    """Scan every blob reachable from every local ref without checking it out."""
    initial_refs = _local_ref_snapshot(repository_root)
    reachable_blobs: set[tuple[str, str, str]] = set()
    tree_cache: dict[str, list[tuple[str, str]]] = {}

    for ref_name, object_id, object_type in initial_refs:
        object_id, object_type = _peeled_object(
            repository_root,
            ref_name,
            object_id,
            object_type,
        )
        if object_type == "blob":
            reachable_blobs.add((ref_name, "<direct-blob>", object_id))
            continue
        if object_type not in {"commit", "tree"}:
            raise SafetyCheckError("unsupported local Git ref type")
        for tree_id in _trees_for_ref(repository_root, object_id, object_type):
            if tree_id not in tree_cache:
                tree_cache[tree_id] = _tree_blobs(repository_root, tree_id)
            for path, blob_id in tree_cache[tree_id]:
                reachable_blobs.add((ref_name, path, blob_id))

    findings = []
    scan_cache: dict[tuple[str, str], list[Violation]] = {}
    for ref_name, path, blob_id in sorted(reachable_blobs):
        # Findings depend on both path and content (workflow rules are path
        # sensitive). Retain each ref attribution while sharing identical scans.
        key = (path, blob_id)
        if key not in scan_cache:
            data = _git_output(repository_root, ["cat-file", "blob", blob_id])
            text = data.decode("utf-8", errors="surrogateescape")
            scan_cache[key] = scan_text(path, text)
        for violation in scan_cache[key]:
            findings.append(
                HistoryViolation(
                    ref=ref_name,
                    path=path,
                    line=violation.line,
                    rule=violation.rule,
                    blob=blob_id,
                )
            )

    if _local_ref_snapshot(repository_root) != initial_refs:
        raise SafetyCheckError("local Git refs changed during the scan")
    return sorted(set(findings))


def main(arguments: list[str] | None = None) -> int:
    repository_root = Path(__file__).resolve().parents[1]
    arguments = [] if arguments is None else arguments
    if arguments not in ([], ["--all-local-refs"]):
        print("Usage: check_repository_safety.py [--all-local-refs]", file=sys.stderr)
        return 2
    try:
        if arguments == ["--all-local-refs"]:
            violations = scan_all_local_refs(repository_root)
        else:
            violations = scan_repository(repository_root)
    except (OSError, SafetyCheckError):
        print("Repository safety check could not run.", file=sys.stderr)
        return 2

    if violations:
        print("Repository safety check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation.format()}", file=sys.stderr)
        return 1

    if arguments == ["--all-local-refs"]:
        print("Local Git history safety check passed.")
    else:
        print("Repository safety check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
