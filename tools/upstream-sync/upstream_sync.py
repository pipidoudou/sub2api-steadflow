"""Ownership, migration, and resumable Git primitives for upstream sync."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import urllib.parse
import uuid
from pathlib import Path, PurePosixPath


OWNER_LAYER_KEYS = (
    "data",
    "distributor",
    "branding_settings",
    "thesis_public",
    "integration_adapter",
)

MANIFEST_TOP_LEVEL_KEYS = frozenset(
    (
        "schema_version",
        *OWNER_LAYER_KEYS,
        "shared_seams",
        "generated",
        "critical_commands",
    )
)
MIGRATION_BASELINE_KEYS = frozenset(("schema_version", "algorithm", "migrations"))
MIGRATION_BASELINE_OPTIONAL_KEYS = frozenset(("reviewed_additions",))
UPSTREAM_LOCK_KEYS = frozenset(
    ("schema_version", "remote", "repository", "release", "peeled_commit", "tree")
)
RELEASE_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ManifestValidationError(ValueError):
    """Raised when the customization manifest violates ownership rules."""


class MigrationValidationError(ValueError):
    """Raised when migration history is invalid or unsafe."""


class UpgradeBlocked(RuntimeError):
    """Raised when an upgrade precondition fails closed."""


def _redact_diagnostic(value):
    """Remove URL credentials and query-like secrets from diagnostics."""
    text = str(value)

    def redact_url(match):
        candidate = match.group(0)
        try:
            parsed = urllib.parse.urlsplit(candidate)
        except ValueError:
            return "<redacted-url>"
        if (
            parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        ):
            return candidate
        hostname = parsed.hostname or "<redacted-host>"
        try:
            port = f":{parsed.port}" if parsed.port is not None else ""
        except ValueError:
            port = ""
        authority = hostname + port
        if parsed.username is not None or parsed.password is not None:
            authority = "<redacted>@" + authority
        suffix = ""
        if parsed.query:
            suffix += "?<redacted>"
        if parsed.fragment:
            suffix += "#<redacted>"
        return urllib.parse.urlunsplit(
            (parsed.scheme, authority, parsed.path, "", "")
        ) + suffix

    text = re.sub(
        r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"]+",
        redact_url,
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;&\"']+",
        r"\1<redacted>",
        text,
    )
    secret_name = (
        r"(?:password|passwd|token|secret|client[_-]?secret|api[_-]?key|"
        r"private[_-]?key|key)"
    )
    return re.sub(
        rf"(?i)([\"']?{secret_name}[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        r"\1<redacted>",
        text,
    )


class GitRepository:
    """Small argv-only Git runner with isolated configuration inputs."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.commands = []
        self.environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    @classmethod
    def discover(cls, cwd):
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        completed = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise UpgradeBlocked("current directory is not a Git working tree")
        return cls(completed.stdout.strip())

    def run(
        self,
        *arguments,
        cwd=None,
        check=True,
        operation="Git command",
        extra_environment=None,
        read_only=False,
    ):
        argv = ["git", "-C", str(cwd or self.root), *arguments]
        self.commands.append(argv)
        environment = dict(self.environment)
        if extra_environment:
            environment.update(extra_environment)
        if read_only:
            environment["GIT_OPTIONAL_LOCKS"] = "0"
        completed = subprocess.run(
            argv,
            env=environment,
            capture_output=True,
            text=True,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            if detail:
                detail = detail.splitlines()[-1]
                raise UpgradeBlocked(
                    _redact_diagnostic(f"{operation} failed: {detail}")
                )
            raise UpgradeBlocked(f"{operation} failed")
        return completed

    def run_without_hooks(self, *arguments, **options):
        """Run a mutating Git command with hooks disabled for this invocation."""
        return self.run(
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "commit.gpgSign=false",
            *arguments,
            **options,
        )

    def hash_blob_bytes(self, content, cwd=None):
        expected = _git_blob_oid(content)
        argv = ["git", "-C", str(cwd or self.root), "hash-object", "-w", "--stdin"]
        self.commands.append(argv)
        completed = subprocess.run(
            argv,
            env=self.environment,
            input=content,
            capture_output=True,
        )
        if completed.returncode != 0:
            raise UpgradeBlocked("report blob creation failed")
        actual = completed.stdout.decode("ascii", errors="strict").strip()
        if actual != expected:
            raise UpgradeBlocked("report blob identity mismatch")
        return actual

    def read_blob_bytes(self, revision, path, cwd=None):
        if not OBJECT_ID_PATTERN.fullmatch(revision):
            raise UpgradeBlocked("Git blob revision is invalid")
        try:
            _validate_repo_paths("Git blob path", [path])
        except ManifestValidationError as error:
            raise UpgradeBlocked(str(error)) from error
        argv = [
            "git",
            "-C",
            str(cwd or self.root),
            "cat-file",
            "blob",
            f"{revision}:{path}",
        ]
        self.commands.append(argv)
        environment = dict(self.environment)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        completed = subprocess.run(argv, env=environment, capture_output=True)
        if completed.returncode != 0:
            raise UpgradeBlocked("Git configuration blob lookup failed")
        return completed.stdout


def parse_cli(arguments):
    parser = argparse.ArgumentParser(
        prog="upgrade",
        usage=(
            "upgrade vX.Y.Z | upgrade --continue | upgrade --verify-current | "
            "upgrade --run-critical"
        ),
        description="Create or resume an isolated Steadflow upstream candidate.",
    )
    parser.add_argument("release", nargs="?")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--continue", dest="continue_upgrade", action="store_true")
    modes.add_argument("--verify-current", action="store_true")
    modes.add_argument("--run-critical", action="store_true")
    options = parser.parse_args(arguments)
    selected = sum(
        (
            options.release is not None,
            options.continue_upgrade,
            options.verify_current,
            options.run_critical,
        )
    )
    if selected != 1:
        parser.error(
            "choose exactly one release, --continue, --verify-current, or "
            "--run-critical"
        )
    if options.release is not None and not RELEASE_PATTERN.fullmatch(options.release):
        parser.error("release must match vX.Y.Z without leading zeroes")
    return options


def main(arguments=None):
    options = parse_cli(sys.argv[1:] if arguments is None else arguments)
    try:
        repository = GitRepository.discover(Path.cwd())
        if options.run_critical:
            result = verify_current(repository)
            command_reports = run_critical_suite(
                repository.root, result["manifest"]["critical_commands"]
            )
            for report in command_reports:
                print(
                    f"{report['status']}: {report['id']} "
                    f"tests_executed={report['tests_executed']}"
                )
            return 0
        if options.verify_current:
            result = verify_current(repository)
            print_current_verification(result)
            return 0
        if options.continue_upgrade:
            candidate = resume_upgrade(repository)
            if candidate.get("_completed"):
                print(
                    f"PASS: completed candidate {candidate['branch']} archived; "
                    "next release may start"
                )
            else:
                print(
                    f"PASS: candidate {candidate['branch']} is merged at "
                    f"{candidate['worktree']}"
                )
            return 0
        result = verify_current(repository)
        if options.release == result["lock"]["release"]:
            print(
                f"PASS: {options.release} is already current; "
                "configuration and lock verified"
            )
            return 0
        candidate = create_upgrade_candidate(repository, options.release)
        if candidate.get("_existing_candidate"):
            print(
                f"PASS: existing candidate {candidate['branch']} is merged at "
                f"{candidate['worktree']}"
            )
        else:
            print(
                f"PASS: candidate {candidate['branch']} merged at "
                f"{candidate['worktree']}"
            )
        return 0
    except (OSError, ValueError, UpgradeBlocked) as error:
        print(f"BLOCKED: {_redact_diagnostic(error)}", file=sys.stderr)
        return 2


def load_json_document(path):
    """Load strict JSON, including JSON-compatible YAML files."""
    def reject_duplicate_keys(pairs):
        document = {}
        for key, value in pairs:
            if key in document:
                raise ValueError(f"duplicate JSON key: {key}")
            document[key] = value
        return document

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("JSON document must be a regular file")
        document = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = None
        with document:
            loaded = json.load(document, object_pairs_hook=reject_duplicate_keys)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(loaded, dict):
        raise ValueError("JSON document root must be an object")
    return loaded


def _load_json_bytes(content, label):
    try:
        document = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=lambda pairs: _strict_object(pairs),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UpgradeBlocked(f"{label} is not strict JSON") from error
    if not isinstance(document, dict):
        raise UpgradeBlocked(f"{label} root must be an object")
    return document


def _canonical_json_bytes(document):
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _directory_flags():
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def _directory_identity(descriptor):
    entry = os.fstat(descriptor)
    if not stat.S_ISDIR(entry.st_mode):
        raise UpgradeBlocked("opened path must be a real directory")
    return (entry.st_dev, entry.st_ino)


def _open_child_directory(parent_fd, name, label):
    if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
        raise UpgradeBlocked(f"{label} component is invalid")
    descriptor = None
    try:
        descriptor = os.open(name, _directory_flags(), dir_fd=parent_fd)
        _directory_identity(descriptor)
        return descriptor
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise UpgradeBlocked(f"unable to open {label} safely") from error
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _directory_chain_identity(root, components):
    descriptors = []
    try:
        descriptors.append(os.open(Path(root).absolute(), _directory_flags()))
        identities = [_directory_identity(descriptors[-1])]
        for component in components:
            descriptors.append(
                _open_child_directory(descriptors[-1], component, component)
            )
            identities.append(_directory_identity(descriptors[-1]))
        return tuple(identities)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _migration_inventory(root):
    """Read migration bytes only through a fixed no-follow descriptor chain."""
    root = Path(root).absolute()
    descriptors = []
    inventory = {}
    try:
        descriptors.append(os.open(root, _directory_flags()))
        descriptors.append(_open_child_directory(descriptors[-1], "backend", "backend"))
        descriptors.append(
            _open_child_directory(descriptors[-1], "migrations", "migrations")
        )
        identities = tuple(_directory_identity(fd) for fd in descriptors)
        directory_fd = descriptors[-1]
        sql_names = sorted(
            name
            for name in os.listdir(directory_fd)
            if name.endswith(".sql") and "/" not in name and "\x00" not in name
        )
        file_identities = {}
        for name in sql_names:
            relative = f"backend/migrations/{name}"
            try:
                entry_mode = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                ).st_mode
            except OSError as error:
                raise MigrationValidationError(
                    f"unable to inspect migration safely: {relative}"
                ) from error
            if not stat.S_ISREG(entry_mode):
                raise MigrationValidationError(
                    f"migration must be regular file: {relative}"
                )
            file_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                file_flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(name, file_flags, dir_fd=directory_fd)
            except OSError as error:
                raise MigrationValidationError(
                    f"unable to open migration safely: {relative}"
                ) from error
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise MigrationValidationError(
                        f"migration must be regular file: {relative}"
                    )
                file_identities[name] = (
                    opened.st_dev,
                    opened.st_ino,
                    stat.S_IFMT(opened.st_mode),
                    stat.S_IMODE(opened.st_mode),
                )
                chunks = []
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                inventory[relative] = b"".join(chunks)
            finally:
                os.close(descriptor)
        final_sql_names = sorted(
            name
            for name in os.listdir(directory_fd)
            if name.endswith(".sql") and "/" not in name and "\x00" not in name
        )
        if final_sql_names != sql_names:
            raise MigrationValidationError(
                "migration SQL file set changed during validation"
            )
        for name in final_sql_names:
            relative = f"backend/migrations/{name}"
            try:
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
            except OSError as error:
                raise MigrationValidationError(
                    f"unable to reopen migration safely: {relative}"
                ) from error
            try:
                reopened = os.fstat(descriptor)
                reopened_identity = (
                    reopened.st_dev,
                    reopened.st_ino,
                    stat.S_IFMT(reopened.st_mode),
                    stat.S_IMODE(reopened.st_mode),
                )
                if (
                    not stat.S_ISREG(reopened.st_mode)
                    or reopened_identity != file_identities[name]
                ):
                    raise MigrationValidationError(
                        f"migration identity changed during validation: {relative}"
                    )
            finally:
                os.close(descriptor)
        if _directory_chain_identity(root, ("backend", "migrations")) != identities:
            raise MigrationValidationError(
                "migration directory path changed during validation"
            )
    except (OSError, UpgradeBlocked) as error:
        raise MigrationValidationError(
            "migrations directory cannot be read safely"
        ) from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return inventory


def migration_checksums(root):
    """Return sorted raw-byte SHA-256 checksums for SQL migrations."""
    return {
        path: hashlib.sha256(content).hexdigest()
        for path, content in _migration_inventory(root).items()
    }


def audit_migrations(root, baseline):
    """Compare current migrations with a checksum baseline."""
    actual_keys = set(baseline)
    allowed_keys = MIGRATION_BASELINE_KEYS | MIGRATION_BASELINE_OPTIONAL_KEYS
    if not MIGRATION_BASELINE_KEYS.issubset(actual_keys) or not actual_keys.issubset(
        allowed_keys
    ):
        missing = ",".join(sorted(MIGRATION_BASELINE_KEYS - actual_keys)) or "none"
        extra = ",".join(sorted(actual_keys - allowed_keys)) or "none"
        raise MigrationValidationError(
            f"migration baseline keys missing={missing} extra={extra}"
        )
    if (
        type(baseline.get("schema_version")) is not int
        or baseline["schema_version"] != 1
    ):
        raise MigrationValidationError(
            "migration baseline schema_version must be integer 1"
        )
    if baseline.get("algorithm") != "sha256":
        raise MigrationValidationError(
            "migration baseline algorithm must be sha256"
        )
    historical = baseline["migrations"]
    if not isinstance(historical, dict):
        raise MigrationValidationError(
            "migration baseline migrations must be an object"
        )
    if list(historical) != sorted(historical):
        raise MigrationValidationError(
            "migration baseline migrations must be sorted by path"
        )
    for path, checksum in historical.items():
        relative_name = path.removeprefix("backend/migrations/")
        valid_path = (
            isinstance(path, str)
            and path.startswith("backend/migrations/")
            and relative_name
            and "/" not in relative_name
            and relative_name.endswith(".sql")
            and PurePosixPath(path).as_posix() == path
        )
        if not valid_path:
            raise MigrationValidationError(
                f"invalid migration baseline path: {path}"
            )
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdef" for character in checksum)
        ):
            raise MigrationValidationError(
                f"invalid sha256 for migration: {path}"
            )
    reviewed = baseline.get("reviewed_additions", {})
    if not isinstance(reviewed, dict) or list(reviewed) != sorted(reviewed):
        raise MigrationValidationError(
            "migration reviewed_additions must be a sorted object"
        )
    for path, review in reviewed.items():
        _validate_repo_paths("migration reviewed_additions", [path])
        if not path.startswith("backend/migrations/") or not path.endswith(".sql"):
            raise MigrationValidationError(
                f"invalid reviewed migration path: {path}"
            )
        if (
            not isinstance(review, dict)
            or set(review) != {"rationale", "sha256"}
            or not isinstance(review["rationale"], str)
            or not review["rationale"].strip()
        ):
            raise MigrationValidationError(
                f"invalid reviewed migration record: {path}"
            )
        try:
            _require_sha256(review["sha256"], f"reviewed migration {path}")
        except ValueError as error:
            raise MigrationValidationError(str(error)) from error
    current_content = _migration_inventory(root)
    current = {
        path: hashlib.sha256(content).hexdigest()
        for path, content in current_content.items()
    }
    unchanged = sorted(
        path
        for path, checksum in historical.items()
        if current.get(path) == checksum
    )
    changed = sorted(
        path
        for path, checksum in historical.items()
        if path in current and current[path] != checksum
    )
    deleted = sorted(set(historical) - set(current))
    added = sorted(set(current) - set(historical))
    added_risk = {path: classify_migration_sql(current_content[path]) for path in added}
    for path in added:
        review = reviewed.get(path)
        checksum = current[path]
        if (
            added_risk[path] == "destructive"
            and review is not None
            and review["sha256"] == checksum
        ):
            added_risk[path] = "reviewed-destructive"
    return {
        "unchanged": unchanged,
        "changed": changed,
        "deleted": deleted,
        "added": added,
        "added_risk": added_risk,
    }


def validate_migrations(root, baseline):
    """Validate historical migrations while preserving added-file reporting."""
    report = audit_migrations(root, baseline)
    if report["changed"]:
        raise MigrationValidationError(
            "changed historical migrations: " + ", ".join(report["changed"])
        )
    if report["deleted"]:
        raise MigrationValidationError(
            "deleted historical migrations: " + ", ".join(report["deleted"])
        )
    destructive = sorted(
        path
        for path, risk in report["added_risk"].items()
        if risk == "destructive"
    )
    if destructive:
        raise MigrationValidationError(
            "destructive new migrations: " + ", ".join(destructive)
        )
    return report


def classify_migration_sql(content):
    """Classify a new SQL migration conservatively without decoding failures."""
    text = bytes(content).decode("utf-8", errors="replace")
    text = re.sub(r"(?s)/\*.*?\*/", " ", text)
    text = re.sub(r"(?m)--[^\n]*$", " ", text)
    normalized = " ".join(text.upper().split())
    destructive_patterns = (
        r"\bDROP\s+(?:TABLE|SCHEMA|DATABASE|INDEX|VIEW|MATERIALIZED\s+VIEW|TYPE)\b",
        r"\bTRUNCATE\b",
        r"\bDELETE\s+FROM\b",
        r"\bALTER\s+TABLE\b.*\bDROP\s+(?:COLUMN|CONSTRAINT)\b",
        r"\bALTER\s+TABLE\b.*\bRENAME\s+COLUMN\b",
        r"\bALTER\s+TABLE\b.*\bRENAME\s+TO\b",
        r"\bALTER\s+TABLE\b.*\bALTER\s+COLUMN\b.*\bTYPE\b",
    )
    if any(re.search(pattern, normalized) for pattern in destructive_patterns):
        return "destructive"
    statements = [part.strip() for part in normalized.split(";") if part.strip()]
    additive_patterns = (
        r"^CREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX|VIEW|TYPE)\b",
        r"^ALTER\s+TABLE\b.*\bADD\s+(?:COLUMN|CONSTRAINT)\b",
        r"^COMMENT\s+ON\b",
    )
    if statements and all(
        any(re.search(pattern, statement) for pattern in additive_patterns)
        for statement in statements
    ):
        return "additive"
    return "review-required"


KNOWN_FAILURE_TOP_LEVEL_KEYS = frozenset(("schema_version", "baseline", "entries"))
KNOWN_FAILURE_BASELINE_KEYS = frozenset(
    ("release", "commands", "evidence", "result", "historical_observation")
)
KNOWN_FAILURE_RESULT_KEYS = frozenset(("failed", "go_failed", "vitest_failed"))
KNOWN_FAILURE_EVIDENCE_KEYS = frozenset(
    ("go_json_sha256", "vitest_json_sha256")
)
KNOWN_FAILURE_ENTRY_KEYS = frozenset(
    ("id", "category", "reason", "first_seen", "expires", "evidence_command")
)


def _require_exact_keys(document, expected, label):
    if not isinstance(document, dict) or set(document) != expected:
        actual = set(document) if isinstance(document, dict) else set()
        missing = ",".join(sorted(expected - actual)) or "none"
        extra = ",".join(sorted(actual - expected)) or "none"
        raise ValueError(f"{label} keys missing={missing} extra={extra}")


def _require_sha256(value, label):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{label} must be a lowercase sha256")


def validate_known_failures(document):
    """Validate the exact, JSON-compatible known-failure baseline."""
    _require_exact_keys(document, KNOWN_FAILURE_TOP_LEVEL_KEYS, "known failures")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError("known failures schema_version must be integer 1")
    baseline = document["baseline"]
    _require_exact_keys(baseline, KNOWN_FAILURE_BASELINE_KEYS, "baseline")
    if not isinstance(baseline["release"], str) or not RELEASE_PATTERN.fullmatch(
        baseline["release"]
    ):
        raise ValueError("baseline release is invalid")
    commands = baseline["commands"]
    if (
        not isinstance(commands, list)
        or not commands
        or any(not isinstance(command, str) or not command for command in commands)
    ):
        raise ValueError("baseline commands must be a non-empty string list")
    evidence = baseline["evidence"]
    _require_exact_keys(evidence, KNOWN_FAILURE_EVIDENCE_KEYS, "baseline evidence")
    for name, digest in evidence.items():
        _require_sha256(digest, f"baseline evidence {name}")
    result = baseline["result"]
    _require_exact_keys(result, KNOWN_FAILURE_RESULT_KEYS, "baseline result")
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise ValueError("baseline result counts must be non-negative integers")
    if result["failed"] != result["go_failed"] + result["vitest_failed"]:
        raise ValueError("baseline failed count must equal Go plus Vitest failures")
    if (
        not isinstance(baseline["historical_observation"], str)
        or not baseline["historical_observation"].strip()
    ):
        raise ValueError("baseline historical_observation must be non-empty")
    entries = document["entries"]
    if not isinstance(entries, list):
        raise ValueError("known failure entries must be a list")
    ids = []
    for index, entry in enumerate(entries):
        _require_exact_keys(entry, KNOWN_FAILURE_ENTRY_KEYS, f"entries[{index}]")
        identifier = entry["id"]
        if not isinstance(identifier, str) or not re.fullmatch(
            r"(?:go|vitest):[^:]+:.+", identifier
        ):
            raise ValueError(f"entries[{index}].id is invalid")
        if entry["category"] != "upstream-known":
            raise ValueError(f"entries[{index}].category must be upstream-known")
        for field in ("reason", "evidence_command"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise ValueError(f"entries[{index}].{field} must be non-empty")
        if not isinstance(entry["first_seen"], str) or not RELEASE_PATTERN.fullmatch(
            entry["first_seen"]
        ):
            raise ValueError(f"entries[{index}].first_seen is invalid")
        if not isinstance(entry["expires"], str) or not RELEASE_PATTERN.fullmatch(
            entry["expires"]
        ):
            raise ValueError(f"entries[{index}].expires must be a release vX.Y.Z")
        ids.append(identifier)
    if ids != sorted(set(ids)):
        raise ValueError("known failure entry IDs must be sorted and unique")
    return document


def parse_go_test_jsonl(raw):
    """Parse `go test -json` output into exact stable IDs."""
    buckets = {"failed": set(), "passed": set(), "skipped": set()}
    package_failures = set()
    failed_test_packages = set()
    for line_number, line in enumerate(str(raw).splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid Go test JSON at line {line_number}") from error
        if not isinstance(event, dict):
            raise ValueError(f"Go test JSON line {line_number} must be an object")
        action = event.get("Action")
        package = event.get("Package")
        test = event.get("Test")
        if action is not None and not isinstance(action, str):
            raise ValueError(f"Go test JSON line {line_number} Action must be a string")
        for field, value in (("Package", package), ("Test", test)):
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    f"Go test JSON line {line_number} {field} must be a string"
                )
        if action == "fail" and package and not test:
            package_failures.add(package)
        if action not in ("pass", "fail", "skip") or not package or not test:
            continue
        bucket = {"pass": "passed", "fail": "failed", "skip": "skipped"}[action]
        buckets[bucket].add(f"go:{package}:{test}")
        if action == "fail":
            failed_test_packages.add(package)
    unexplained_package_failures = sorted(package_failures - failed_test_packages)
    if unexplained_package_failures:
        raise ValueError(
            "Go package failed without exact test: "
            + ", ".join(unexplained_package_failures)
        )
    return {key: sorted(value) for key, value in buckets.items()}


def parse_vitest_json(document, repo_root=None):
    """Parse Vitest JSON reporter output into exact stable IDs."""
    if not isinstance(document, dict) or not isinstance(document.get("testResults"), list):
        raise ValueError("Vitest JSON must contain testResults")
    success = document.get("success")
    if success is not None and type(success) is not bool:
        raise ValueError("Vitest success must be a boolean")
    root = Path(repo_root).resolve() if repo_root is not None else None
    frontend_cwd = root / "frontend" if root is not None else None
    buckets = {"failed": set(), "passed": set(), "skipped": set()}
    failed_suites_without_test = []
    canonical_sources = {}
    seen_test_ids = {}
    for suite in document["testResults"]:
        if not isinstance(suite, dict):
            raise ValueError("Vitest testResults entries must be objects")
        file_name = suite.get("name") or suite.get("testFilePath")
        if not isinstance(file_name, str) or not file_name:
            raise ValueError("Vitest result file name is missing")
        file_path = Path(file_name)
        if root is not None:
            candidate = file_path if file_path.is_absolute() else frontend_cwd / file_path
            try:
                resolved_file = candidate.resolve()
                resolved_file.relative_to(frontend_cwd)
                normalized_file = resolved_file.relative_to(root).as_posix()
            except ValueError as error:
                raise ValueError("Vitest result file escapes frontend cwd") from error
        else:
            pure = PurePosixPath(file_name)
            if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
                raise ValueError("Vitest result file must be canonical relative POSIX")
            normalized_file = pure.as_posix()
        prior_source = canonical_sources.setdefault(normalized_file, file_name)
        if prior_source != file_name:
            raise ValueError("Vitest result paths collide after canonicalization")
        assertions = suite.get("assertionResults")
        if not isinstance(assertions, list):
            raise ValueError("Vitest assertionResults must be a list")
        suite_status = suite.get("status")
        if suite_status is not None and (
            not isinstance(suite_status, str)
            or suite_status
            not in {"passed", "failed", "pending", "skipped", "todo"}
        ):
            raise ValueError("Vitest suite status is invalid")
        suite_failed = suite_status == "failed"
        exact_failure = False
        for assertion in assertions:
            if not isinstance(assertion, dict):
                raise ValueError("Vitest assertions must be objects")
            status_value = assertion.get("status")
            name = assertion.get("fullName") or assertion.get("title")
            if status_value not in ("passed", "failed", "pending", "skipped", "todo"):
                raise ValueError("Vitest assertion status is invalid")
            if not isinstance(name, str) or not name:
                raise ValueError("Vitest assertion name is missing")
            bucket = {
                "passed": "passed",
                "failed": "failed",
                "pending": "skipped",
                "skipped": "skipped",
                "todo": "skipped",
            }[status_value]
            identifier = f"vitest:{normalized_file}:{name}"
            if identifier in seen_test_ids:
                raise ValueError(
                    "duplicate Vitest stable test ID: " + identifier
                )
            seen_test_ids[identifier] = status_value
            buckets[bucket].add(identifier)
            exact_failure = exact_failure or status_value == "failed"
        if suite_failed and not exact_failure:
            failed_suites_without_test.append(normalized_file)
    if document.get("success") is False and not buckets["failed"]:
        failed_suites_without_test.append("unknown")
    if failed_suites_without_test:
        raise ValueError(
            "Vitest suite failed without exact test: "
            + ", ".join(sorted(set(failed_suites_without_test)))
        )
    return {key: sorted(value) for key, value in buckets.items()}


def _release_tuple(release):
    match = RELEASE_PATTERN.fullmatch(release) if isinstance(release, str) else None
    if match is None:
        raise ValueError("target release must match vX.Y.Z")
    return tuple(int(part) for part in match.groups())


def compare_test_failures(
    actual_failures, known_document, critical_ids=(), target_release=None
):
    """Compare exact failure IDs and fail closed on new, expired, or critical IDs."""
    validate_known_failures(known_document)
    actual = set(actual_failures)
    entries = {entry["id"]: entry for entry in known_document["entries"]}
    known_ids = set(entries)
    critical = sorted(actual & set(critical_ids))
    target_version = _release_tuple(target_release)
    expired = sorted(
        identifier
        for identifier in actual & known_ids
        if _release_tuple(entries[identifier]["expires"]) <= target_version
        and identifier not in critical
    )
    known = sorted((actual & known_ids) - set(expired) - set(critical))
    new = sorted(actual - known_ids - set(critical))
    fixed = sorted(known_ids - actual)
    blocked = bool(new or expired or critical or fixed)
    status_value = "BLOCKED" if blocked else ("KNOWN-FAIL" if known else "PASS")
    return {
        "critical": critical,
        "expired": expired,
        "fixed": fixed,
        "known": known,
        "new": new,
        "status": status_value,
    }


_SECRET_KEY_PATTERN = re.compile(
    r"(?i)(?:password|passwd|token|secret|(?:api|private|client)[_-]?key|^key$)"
)


def redact_report_secrets(value):
    """Return a recursively redacted report suitable for persisted evidence."""
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>"
                if _SECRET_KEY_PATTERN.search(str(key))
                else redact_report_secrets(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_report_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_report_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_diagnostic(value)
    return value


def _sort_report_lists(value):
    if isinstance(value, dict):
        return {key: _sort_report_lists(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        converted = [_sort_report_lists(item) for item in value]
        if all(isinstance(item, str) for item in converted):
            return sorted(set(converted))
        if converted and all(isinstance(item, dict) for item in converted):
            for stable_key in ("id", "path", "name", "test"):
                if all(stable_key in item for item in converted):
                    return sorted(
                        converted,
                        key=lambda item: json.dumps(
                            item[stable_key], sort_keys=True, separators=(",", ":")
                        ),
                    )
        return converted
    return value


REPORT_STATUSES = frozenset(("PASS", "FAIL", "KNOWN-FAIL", "BLOCKED", "NOT-RUN"))
SUCCESS_REPORT_SCHEMA_VERSION = 2
SUCCESS_REPORT_KEYS = frozenset(
    (
        "candidate_head",
        "candidate_tree",
        "configuration_hashes",
        "critical_commands",
        "exact_comparison",
        "file_changes",
        "full_go",
        "full_vitest",
        "generated",
        "migrations",
        "ownership",
        "peeled_commit",
        "release",
        "schema_version",
        "seams",
        "source_commit",
        "status",
        "tag_object",
        "upstream_tree",
        "validation_summary_sha256",
    )
)


def _validate_report_statuses(value, path="report"):
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "status" and (
                not isinstance(item, str) or item not in REPORT_STATUSES
            ):
                raise ValueError(f"report status is invalid at {child}")
            _validate_report_statuses(item, child)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate_report_statuses(item, f"{path}[{index}]")


def _canonical_report_contents(release, report):
    """Return the only canonical JSON/Markdown byte representation."""
    _validate_report_statuses(report)
    safe = _sort_report_lists(redact_report_secrets(report))
    json_content = (json.dumps(safe, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    markdown_content = (
        f"# Steadflow upstream report: {release}\n\n"
        "```json\n"
        + json_content.decode("utf-8").rstrip("\n")
        + "\n```\n"
    ).encode("utf-8")
    return safe, json_content, markdown_content


def _git_blob_oid(content):
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


class ReportArtifacts:
    def __init__(self, paths, canonical_bytes, identity_token):
        self.paths = paths
        self.canonical_bytes = canonical_bytes
        self.identity_token = identity_token

    def __iter__(self):
        return iter(self.paths)


def _open_or_create_child_directory(parent_fd, name, label):
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        descriptor = _open_child_directory(parent_fd, name, label)
    except UpgradeBlocked as error:
        raise UpgradeBlocked(f"{label} directory must be a real directory") from error
    os.fchmod(descriptor, 0o700)
    return descriptor, created


def _atomic_write_at(directory_fd, name, content):
    try:
        existing = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(existing.st_mode):
            raise UpgradeBlocked("report file must be a regular file")
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        final_fd = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
        )
        try:
            entry = os.fstat(final_fd)
            if not stat.S_ISREG(entry.st_mode):
                raise UpgradeBlocked("report file must be a regular file")
            os.fchmod(final_fd, 0o600)
            os.fsync(final_fd)
            return {
                "dev": entry.st_dev,
                "ino": entry.st_ino,
                "mode": 0o600,
                "sha256": hashlib.sha256(content).hexdigest(),
                "blob_oid": _git_blob_oid(content),
                "name": name,
            }
        finally:
            os.close(final_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)


def _atomic_write_configuration(root, name, content):
    """Replace one tracked .steadflow file without following path symlinks."""
    descriptors = []
    try:
        descriptors.append(os.open(Path(root).absolute(), _directory_flags()))
        descriptors.append(
            _open_child_directory(descriptors[-1], ".steadflow", ".steadflow")
        )
        try:
            existing = os.stat(name, dir_fd=descriptors[-1], follow_symlinks=False)
        except FileNotFoundError as error:
            raise UpgradeBlocked(f"configuration file is missing: {name}") from error
        if not stat.S_ISREG(existing.st_mode):
            raise UpgradeBlocked(f"configuration file must be regular: {name}")
        token = _atomic_write_at(descriptors[-1], name, content)
        os.chmod(name, 0o644, dir_fd=descriptors[-1], follow_symlinks=False)
        os.fsync(descriptors[-1])
        token["mode"] = 0o644
        return token
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular_at(directory_fd, name):
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd
    )
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode):
            raise UpgradeBlocked("candidate report must be a regular file")
        if stat.S_IMODE(entry.st_mode) != 0o600:
            raise UpgradeBlocked("candidate report mode must be 0600")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        return content, {
            "dev": entry.st_dev,
            "ino": entry.st_ino,
            "mode": stat.S_IMODE(entry.st_mode),
            "sha256": hashlib.sha256(content).hexdigest(),
            "blob_oid": _git_blob_oid(content),
            "name": name,
        }
    finally:
        os.close(descriptor)


def _read_report_artifacts(root, release):
    root = Path(root).absolute()
    descriptors = []
    try:
        descriptors.append(os.open(root, _directory_flags()))
        descriptors.append(
            _open_child_directory(descriptors[-1], ".steadflow", ".steadflow")
        )
        descriptors.append(
            _open_child_directory(descriptors[-1], "reports", "reports")
        )
        json_name = f"{release}.json"
        markdown_name = f"{release}.md"
        json_content, json_identity = _read_regular_at(descriptors[-1], json_name)
        markdown_content, markdown_identity = _read_regular_at(
            descriptors[-1], markdown_name
        )
        token = {
            "directories": tuple(_directory_identity(fd) for fd in descriptors),
            "files": {"json": json_identity, "markdown": markdown_identity},
        }
        return (json_content, markdown_content), token
    except OSError as error:
        raise UpgradeBlocked("candidate reports are missing or unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _require_report_path_identity(root, release, expected):
    _, actual = _read_report_artifacts(root, release)
    if actual != expected:
        raise UpgradeBlocked("candidate report path changed during validation")


def write_upgrade_reports(root, release, report, event_hook=None):
    """Write deterministic redacted JSON and Markdown review reports."""
    if not RELEASE_PATTERN.fullmatch(release):
        raise ValueError("report release is invalid")
    safe, json_content, markdown_content = _canonical_report_contents(
        release, report
    )
    root = Path(root).absolute()
    descriptors = []
    try:
        descriptors.append(os.open(root, _directory_flags()))
        steadflow_fd, _ = _open_or_create_child_directory(
            descriptors[-1], ".steadflow", ".steadflow"
        )
        descriptors.append(steadflow_fd)
        reports_fd, _ = _open_or_create_child_directory(
            descriptors[-1], "reports", "reports"
        )
        descriptors.append(reports_fd)
        json_name = f"{release}.json"
        markdown_name = f"{release}.md"
        json_identity = _atomic_write_at(reports_fd, json_name, json_content)
        if event_hook is not None:
            event_hook("after_json_report")
        markdown_identity = _atomic_write_at(
            reports_fd, markdown_name, markdown_content
        )
        os.fsync(reports_fd)
        token = {
            "directories": tuple(_directory_identity(fd) for fd in descriptors),
            "files": {"json": json_identity, "markdown": markdown_identity},
        }
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    _require_report_path_identity(root, release, token)
    if event_hook is not None:
        event_hook("after_success_reports")
    reports = root / ".steadflow" / "reports"
    relative_json, relative_markdown = _validation_report_relative_paths(release)
    return ReportArtifacts(
        (reports / f"{release}.json", reports / f"{release}.md"),
        {relative_json: json_content, relative_markdown: markdown_content},
        token,
    )


def _critical_environment():
    allowed = (
        "PATH",
        "HOME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "GOPATH",
        "GOMODCACHE",
        "GOCACHE",
        "PNPM_HOME",
    )
    environment = {name: os.environ[name] for name in allowed if name in os.environ}
    environment.setdefault("LANG", "C")
    environment.setdefault("LC_ALL", "C")
    return environment


def run_critical_commands(root, commands, runner=subprocess.run):
    """Run declared zero-waiver commands with machine-readable test evidence."""
    root = Path(root).resolve()
    reports = []
    for command in commands:
        name = command["name"]
        command_id = f"command:{name}"
        argv = list(command["argv"])
        if name == "backend_seo_public":
            dist = root / "backend" / "internal" / "web" / "dist"
            index = dist / "index.html"
            assets = dist / "assets"
            if not index.is_file() or not assets.is_dir() or not any(assets.iterdir()):
                raise UpgradeBlocked(
                    f"{command_id} requires a real frontend build before web critical tests"
                )
        is_go = argv[:2] == ["go", "test"]
        is_vitest = "vitest" in argv and "run" in argv
        if is_go and "-json" not in argv:
            argv.insert(2, "-json")
        if is_vitest and not any(arg.startswith("--reporter") for arg in argv):
            argv.append("--reporter=json")
        completed = runner(
            argv,
            cwd=root / command.get("cwd", "."),
            env=_critical_environment(),
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise UpgradeBlocked(
                _redact_diagnostic(f"{command_id} failed: {detail}")
            )
        if is_go:
            parsed = parse_go_test_jsonl(completed.stdout)
        elif is_vitest:
            try:
                document = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise UpgradeBlocked(f"{command_id} did not emit Vitest JSON") from error
            parsed = parse_vitest_json(document, repo_root=root)
        else:
            raise UpgradeBlocked(f"{command_id} is not a supported critical test command")
        executed = len(parsed["passed"]) + len(parsed["failed"])
        if executed == 0:
            raise UpgradeBlocked(f"{command_id} executed zero tests")
        if parsed["failed"]:
            raise UpgradeBlocked(
                f"{command_id} failed exact tests: " + ", ".join(parsed["failed"])
            )
        reports.append(
            {
                "failed": [],
                "id": command_id,
                "status": "PASS",
                "tests_executed": executed,
            }
        )
    return reports


def run_critical_suite(root, commands, runner=subprocess.run):
    """Build the real frontend once, then execute every declared critical gate."""
    root = Path(root).resolve()
    dist = root / "backend" / "internal" / "web" / "dist"
    try:
        dist_mode = dist.lstat().st_mode
    except FileNotFoundError:
        dist_mode = None
    if dist_mode is not None:
        for parent in (
            root / "backend",
            root / "backend" / "internal",
            root / "backend" / "internal" / "web",
        ):
            parent_mode = parent.lstat().st_mode
            if not stat.S_ISDIR(parent_mode) or stat.S_ISLNK(parent_mode):
                raise UpgradeBlocked(
                    "fresh frontend build parents must be real directories"
                )
        try:
            dist.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise UpgradeBlocked("fresh frontend build output escapes repository") from error
        if not stat.S_ISDIR(dist_mode) or stat.S_ISLNK(dist_mode):
            raise UpgradeBlocked("fresh frontend build output must be a real directory")
        try:
            shutil.rmtree(dist)
        except OSError as error:
            raise UpgradeBlocked("unable to remove stale frontend build output") from error
    build_argv = ["pnpm", "--dir", "frontend", "run", "build"]
    completed = runner(
        build_argv,
        cwd=root,
        env=_critical_environment(),
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise UpgradeBlocked(
            _redact_diagnostic(f"frontend build prerequisite failed: {detail}")
        )
    return run_critical_commands(root, commands, runner=runner)


def _validate_repo_paths(field, paths):
    for path in paths:
        valid = (
            isinstance(path, str)
            and bool(path)
            and "\\" not in path
            and "\x00" not in path
            and not path.startswith("/")
            and path != "."
            and "." not in PurePosixPath(path).parts
            and ".." not in PurePosixPath(path).parts
            and PurePosixPath(path).as_posix() == path
        )
        if not valid:
            raise ManifestValidationError(
                f"{field} contains invalid repository-relative POSIX path"
            )


def _validate_commands(field, commands):
    names = []
    for index, command in enumerate(commands):
        if isinstance(command, dict) and (
            not {"name", "argv"}.issubset(command)
            or not set(command).issubset({"name", "argv", "cwd"})
        ):
            raise ManifestValidationError(
                f"{field}[{index}] keys must be name, argv, and optional cwd"
            )
        name = command.get("name") if isinstance(command, dict) else None
        if not isinstance(name, str) or not name:
            raise ManifestValidationError(
                f"{field}[{index}].name must be a non-empty string"
            )
        argv = command.get("argv") if isinstance(command, dict) else None
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(argument, str) or not argument for argument in argv)
        ):
            raise ManifestValidationError(
                f"{field}[{index}].argv must be a non-empty list of strings"
            )
        if "cwd" in command:
            _validate_repo_paths(f"{field}[{index}].cwd", [command["cwd"]])
        names.append(name)
    if names != sorted(set(names)):
        raise ManifestValidationError(f"{field} names must be sorted and unique")


def validate_manifest(manifest, changed_paths, *, require_exact=False):
    """Return deterministic ownership details for changed repository paths."""
    actual_keys = set(manifest)
    if actual_keys != MANIFEST_TOP_LEVEL_KEYS:
        missing = ",".join(sorted(MANIFEST_TOP_LEVEL_KEYS - actual_keys)) or "none"
        extra = ",".join(sorted(actual_keys - MANIFEST_TOP_LEVEL_KEYS)) or "none"
        raise ManifestValidationError(
            f"top-level keys missing={missing} extra={extra}"
        )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
    ):
        raise ManifestValidationError("schema_version must be integer 1")

    for layer in OWNER_LAYER_KEYS:
        value = manifest[layer]
        if not isinstance(value, dict) or not isinstance(value.get("paths"), list):
            raise ManifestValidationError(
                f"{layer} must be an object containing a paths list"
            )
        if set(value) != {"paths"}:
            raise ManifestValidationError(f"{layer} keys must be paths")
    if not isinstance(manifest["shared_seams"], list):
        raise ManifestValidationError("shared_seams must be a list")
    generated = manifest["generated"]
    if (
        not isinstance(generated, dict)
        or not isinstance(generated.get("commands"), list)
        or not isinstance(generated.get("paths"), list)
    ):
        raise ManifestValidationError(
            "generated must be an object containing commands and paths lists"
        )
    if set(generated) != {"commands", "paths"}:
        raise ManifestValidationError("generated keys must be commands and paths")
    if not isinstance(manifest["critical_commands"], list):
        raise ManifestValidationError("critical_commands must be a list")

    owners_by_path = {}
    for layer in OWNER_LAYER_KEYS:
        paths = manifest[layer]["paths"]
        _validate_repo_paths(f"{layer}.paths", paths)
        if paths != sorted(paths):
            raise ManifestValidationError(f"{layer}.paths must be sorted")
        if len(paths) != len(set(paths)):
            raise ManifestValidationError(f"{layer}.paths must be unique")
        for path in paths:
            owners_by_path.setdefault(path, []).append(layer)

    _validate_repo_paths("shared_seams", manifest["shared_seams"])
    _validate_repo_paths("generated.paths", manifest["generated"]["paths"])
    for field, paths in (
        ("shared_seams", manifest["shared_seams"]),
        ("generated.paths", manifest["generated"]["paths"]),
    ):
        if paths != sorted(set(paths)):
            raise ManifestValidationError(f"{field} must be sorted and unique")
    _validate_commands("generated.commands", manifest["generated"]["commands"])
    _validate_commands("critical_commands", manifest["critical_commands"])

    changed = sorted(changed_paths)
    report = {
        "owned": [path for path in changed if len(owners_by_path.get(path, [])) == 1],
        "unowned": [path for path in changed if path not in owners_by_path],
        "multiply_owned": {
            path: owners_by_path[path]
            for path in sorted(owners_by_path)
            if len(owners_by_path[path]) > 1
        },
        "shared_seams": sorted(manifest["shared_seams"]),
        "generated": sorted(manifest["generated"]["paths"]),
        "registered": sorted(owners_by_path),
    }
    extra_registered = sorted(set(owners_by_path) - set(changed))
    orphan_seams = [
        path for path in report["shared_seams"] if path not in owners_by_path
    ]
    orphan_generated = [
        path for path in report["generated"] if path not in owners_by_path
    ]
    if report["unowned"]:
        raise ManifestValidationError(
            "unowned paths: " + ", ".join(report["unowned"])
        )
    if report["multiply_owned"]:
        details = ", ".join(
            f"{path} [{', '.join(layers)}]"
            for path, layers in report["multiply_owned"].items()
        )
        raise ManifestValidationError("multiply owned paths: " + details)
    if require_exact and extra_registered:
        raise ManifestValidationError(
            "registered paths absent from fork diff: " + ", ".join(extra_registered)
        )
    if orphan_seams:
        raise ManifestValidationError(
            "orphan shared_seams: " + ", ".join(orphan_seams)
        )
    if orphan_generated:
        raise ManifestValidationError(
            "orphan generated paths: " + ", ".join(orphan_generated)
        )
    return report


def _nul_fields(value, label):
    if not isinstance(value, str) or (value and not value.endswith("\x00")):
        raise UpgradeBlocked(f"{label} did not return a terminated NUL stream")
    return value[:-1].split("\x00") if value else []


def _tree_regular_file(repository, commit, path, cwd):
    output = repository.run(
        "ls-tree",
        "-z",
        commit,
        "--",
        path,
        cwd=cwd,
        operation="Git tree path inspection",
        read_only=True,
    ).stdout
    fields = _nul_fields(output, "Git tree path inspection")
    if not fields:
        return None
    if len(fields) != 1:
        raise UpgradeBlocked("Git tree path inspection returned duplicate entries")
    metadata, separator, actual_path = fields[0].partition("\t")
    parts = metadata.split(" ")
    if separator != "\t" or actual_path != path or len(parts) != 3:
        raise UpgradeBlocked("Git tree path inspection was malformed")
    mode, object_type, object_id = parts
    if (
        object_type != "blob"
        or mode not in {"100644", "100755"}
        or not OBJECT_ID_PATTERN.fullmatch(object_id)
    ):
        raise UpgradeBlocked(f"fork diff path is not a regular file: {path}")
    return (mode, object_id)


def _git_change_summary(repository, old_commit, new_commit, cwd):
    for label, commit in (("old", old_commit), ("new", new_commit)):
        if not isinstance(commit, str) or not OBJECT_ID_PATTERN.fullmatch(commit):
            raise UpgradeBlocked(f"{label} Git diff commit is invalid")
        object_type = repository.run(
            "cat-file",
            "-t",
            commit,
            cwd=cwd,
            operation=f"{label} Git diff commit inspection",
            read_only=True,
        ).stdout.strip()
        if object_type != "commit":
            raise UpgradeBlocked(f"{label} Git diff object is not a commit")
    fields = _nul_fields(
        repository.run(
            "diff",
            "--name-status",
            "-z",
            "--no-renames",
            old_commit,
            new_commit,
            "--",
            cwd=cwd,
            operation="fork file-change inspection",
            read_only=True,
        ).stdout,
        "fork file-change inspection",
    )
    if len(fields) % 2:
        raise UpgradeBlocked("fork file-change inspection was malformed")
    buckets = {"A": [], "D": [], "M": []}
    seen = set()
    for index in range(0, len(fields), 2):
        status_code, path = fields[index : index + 2]
        if status_code not in buckets:
            raise UpgradeBlocked(f"unsupported fork file-change status: {status_code}")
        try:
            _validate_repo_paths("fork diff", [path])
        except ManifestValidationError as error:
            raise UpgradeBlocked(str(error)) from error
        if path in seen:
            raise UpgradeBlocked("fork file-change paths are not unique")
        seen.add(path)
        old_entry = _tree_regular_file(repository, old_commit, path, cwd)
        new_entry = _tree_regular_file(repository, new_commit, path, cwd)
        if (
            (status_code == "A" and (old_entry is not None or new_entry is None))
            or (status_code == "D" and (old_entry is None or new_entry is not None))
            or (status_code == "M" and (old_entry is None or new_entry is None))
        ):
            raise UpgradeBlocked(f"fork file-change identity is inconsistent: {path}")
        buckets[status_code].append(path)
    for paths in buckets.values():
        paths.sort()
    all_paths = sorted(seen)
    path_bytes = ("\x00".join(all_paths) + ("\x00" if all_paths else "")).encode(
        "utf-8"
    )
    return {
        "added": buckets["A"],
        "counts": {
            "added": len(buckets["A"]),
            "deleted": len(buckets["D"]),
            "modified": len(buckets["M"]),
            "total": len(all_paths),
        },
        "deleted": buckets["D"],
        "modified": buckets["M"],
        "paths_sha256": hashlib.sha256(path_bytes).hexdigest(),
    }


def _summary_paths(summary):
    return sorted(summary["added"] + summary["deleted"] + summary["modified"])


def _strict_fork_ownership(repository, manifest, old_commit, source_commit, cwd):
    changes = _git_change_summary(repository, old_commit, source_commit, cwd)
    changed = _summary_paths(changes)
    ownership = validate_manifest(manifest, changed, require_exact=True)
    return changes, {
        "changed": changed,
        "multiply_owned": ownership["multiply_owned"],
        "registered": ownership["registered"],
        "status": "PASS",
        "unowned": ownership["unowned"],
    }


def _require_clean_source(repository):
    status = repository.run(
        "status",
        "--porcelain=v2",
        "-z",
        operation="source cleanliness check",
        read_only=True,
    ).stdout
    if status:
        raise UpgradeBlocked("source working tree must be clean")


def _validate_upstream_lock(lock):
    if set(lock) != UPSTREAM_LOCK_KEYS:
        missing = ",".join(sorted(UPSTREAM_LOCK_KEYS - set(lock))) or "none"
        extra = ",".join(sorted(set(lock) - UPSTREAM_LOCK_KEYS)) or "none"
        raise UpgradeBlocked(f"upstream lock keys missing={missing} extra={extra}")
    if type(lock["schema_version"]) is not int or lock["schema_version"] != 1:
        raise UpgradeBlocked("upstream lock schema_version must be integer 1")
    if lock["remote"] != "upstream":
        raise UpgradeBlocked("upstream lock remote must be upstream")
    if (
        not isinstance(lock["repository"], str)
        or not lock["repository"]
        or "\x00" in lock["repository"]
        or "\n" in lock["repository"]
    ):
        raise UpgradeBlocked("upstream lock repository must be a non-empty URL or path")
    if not isinstance(lock["release"], str) or not RELEASE_PATTERN.fullmatch(
        lock["release"]
    ):
        raise UpgradeBlocked("upstream lock release must match vX.Y.Z")
    for field in ("peeled_commit", "tree"):
        if not isinstance(lock[field], str) or not OBJECT_ID_PATTERN.fullmatch(
            lock[field]
        ):
            raise UpgradeBlocked(f"upstream lock {field} must be a 40 character oid")
    _canonical_repository_locator(lock["repository"])


def _canonical_repository_locator(value):
    if not isinstance(value, str) or not value or any(
        character in value for character in ("\x00", "\r", "\n")
    ):
        raise UpgradeBlocked("repository locator is invalid")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme:
        if parsed.scheme.lower() not in {"https", "http", "ssh", "file"}:
            raise UpgradeBlocked("repository URL scheme is unsupported")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise UpgradeBlocked(
                "repository URL must not include credentials, query, or fragment"
            )
        if parsed.scheme.lower() in {"https", "http", "ssh"}:
            if not parsed.hostname or not parsed.path.strip("/"):
                raise UpgradeBlocked("repository URL must include host and path")
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError as error:
                raise UpgradeBlocked("repository URL port is invalid") from error
            path = parsed.path.rstrip("/")
            if path.endswith(".git"):
                path = path[:-4]
            return (
                parsed.scheme.lower(),
                (parsed.hostname or "").lower() + port,
                path,
            )
        return ("file", parsed.netloc, parsed.path.rstrip("/"))
    if "?" in value or "#" in value:
        raise UpgradeBlocked("repository path must not include query or fragment")
    return ("local", value.rstrip("/"))


def _repository_urls_match(expected, actual):
    return _canonical_repository_locator(expected) == _canonical_repository_locator(
        actual
    )


def _raw_sha256(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UpgradeBlocked("configuration hash input must be a regular file")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(descriptor)


def _configuration_paths(repository):
    steadflow = repository.root / ".steadflow"
    try:
        steadflow_mode = steadflow.lstat().st_mode
    except FileNotFoundError as error:
        raise UpgradeBlocked(".steadflow configuration directory is missing") from error
    if not stat.S_ISDIR(steadflow_mode):
        raise UpgradeBlocked(".steadflow configuration directory must be a real directory")
    if steadflow.resolve() != steadflow.absolute():
        raise UpgradeBlocked(".steadflow configuration directory must not be a symlink")
    paths = {
        "customization": steadflow / "customization.yml",
        "known_failures": steadflow / "known-failures.yml",
        "migrations": steadflow / "migration-checksums.json",
        "lock": steadflow / "upstream-lock.json",
    }
    root = repository.root.resolve()
    for name, path in paths.items():
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError as error:
            raise UpgradeBlocked(f"{name} configuration file is missing") from error
        if stat.S_ISLNK(mode):
            raise UpgradeBlocked(f"{name} configuration file must not be a symlink")
        if not stat.S_ISREG(mode):
            raise UpgradeBlocked(f"{name} configuration must be a regular file")
        try:
            path.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as error:
            raise UpgradeBlocked(
                f"{name} configuration escapes repository"
            ) from error
    return paths


def verify_current(repository, *, ownership_commit=None, check_ownership=True):
    """Verify the locked fork baseline without writing Git or state."""
    _require_clean_source(repository)
    paths = _configuration_paths(repository)
    manifest = load_json_document(paths["customization"])
    known_failures = load_json_document(paths["known_failures"])
    baseline = load_json_document(paths["migrations"])
    lock = load_json_document(paths["lock"])
    migrations = validate_migrations(repository.root, baseline)
    _validate_upstream_lock(lock)
    validate_known_failures(known_failures)
    if known_failures["baseline"]["release"] != lock["release"]:
        raise UpgradeBlocked("known failure baseline release does not match upstream lock")

    object_type = repository.run(
        "cat-file",
        "-t",
        lock["peeled_commit"],
        operation="locked commit object validation",
        read_only=True,
    ).stdout.strip()
    if object_type != "commit":
        raise UpgradeBlocked("locked peeled_commit is not a commit object")
    actual_tree = repository.run(
        "rev-parse",
        f"{lock['peeled_commit']}^{{tree}}",
        operation="locked commit tree validation",
        read_only=True,
    ).stdout.strip()
    if actual_tree != lock["tree"]:
        raise UpgradeBlocked("locked commit tree does not match upstream lock")
    ancestor = repository.run(
        "merge-base",
        "--is-ancestor",
        lock["peeled_commit"],
        "HEAD",
        check=False,
        read_only=True,
    )
    if ancestor.returncode == 1:
        raise UpgradeBlocked("locked current commit is not an ancestor of source HEAD")
    if ancestor.returncode != 0:
        raise UpgradeBlocked("unable to validate locked commit ancestry")

    customization_changes = {}
    ownership = {}
    if check_ownership:
        customization_changes, ownership = _strict_fork_ownership(
            repository,
            manifest,
            lock["peeled_commit"],
            ownership_commit
            or repository.run(
                "rev-parse", "HEAD", operation="source commit lookup", read_only=True
            ).stdout.strip(),
            repository.root,
        )

    remote_url = repository.run(
        "remote",
        "get-url",
        "upstream",
        operation="upstream remote lookup",
        read_only=True,
    ).stdout.strip()
    if not _repository_urls_match(lock["repository"], remote_url):
        raise UpgradeBlocked("upstream fetch URL does not match locked repository")

    return {
        "lock": lock,
        "manifest": manifest,
        "known_failures": known_failures,
        "migrations": migrations,
        "ownership": ownership,
        "customization_changes": customization_changes,
        "hashes": {
            name: _raw_sha256(path) for name, path in sorted(paths.items())
        },
    }


def print_current_verification(result):
    added = result["migrations"]["added"]
    print("PASS: current Steadflow lock and configuration verified")
    print("added migrations: " + (", ".join(added) if added else "none"))
    for name, digest in sorted(result["hashes"].items()):
        print(f"{name}_sha256={digest}")
    print(
        "scope: manifest ownership, migration integrity, known-failure schema, "
        "and locked ancestry"
    )


def _git_common_directory(repository, cwd=None):
    common = repository.run(
        "rev-parse",
        "--git-common-dir",
        cwd=cwd,
        operation="Git common directory lookup",
        read_only=True,
    ).stdout.strip()
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = Path(cwd or repository.root) / common_path
    return common_path.resolve()


def _state_path(repository):
    return _git_common_directory(repository) / "steadflow-upstream-sync" / "state.json"


class _UpgradeStorage:
    """Locked, no-follow access to upgrade state in the Git common directory."""

    def __init__(self, repository):
        self.repository = repository
        self.common_path = _git_common_directory(repository)
        self.directory_path = self.common_path / "steadflow-upstream-sync"
        self.state_path = self.directory_path / "state.json"
        self.common_fd = None
        self.directory_fd = None
        self.lock_fd = None

    def __enter__(self):
        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        try:
            self.common_fd = os.open(self.common_path, directory_flags)
            created_state_directory = False
            try:
                os.mkdir("steadflow-upstream-sync", 0o700, dir_fd=self.common_fd)
                created_state_directory = True
            except FileExistsError:
                entry = os.stat(
                    "steadflow-upstream-sync",
                    dir_fd=self.common_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(entry.st_mode):
                    raise UpgradeBlocked("upgrade state directory must be a real directory")
            if created_state_directory:
                os.fsync(self.common_fd)
            self.directory_fd = os.open(
                "steadflow-upstream-sync", directory_flags, dir_fd=self.common_fd
            )
            os.fchmod(self.directory_fd, 0o700)
            lock_flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                lock_flags |= os.O_NOFOLLOW
            self.lock_fd = os.open(
                "upgrade.lock", lock_flags, 0o600, dir_fd=self.directory_fd
            )
            if not stat.S_ISREG(os.fstat(self.lock_fd).st_mode):
                raise UpgradeBlocked("upgrade lock must be a regular file")
            os.fchmod(self.lock_fd, 0o600)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX)
            self._validate_state_entry()
            return self
        except OSError as error:
            self.__exit__(None, None, None)
            raise UpgradeBlocked("upgrade state directory or lock is unsafe") from error
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, exception_type, exception, traceback):
        if self.lock_fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
            self.lock_fd = None
        if self.directory_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.directory_fd)
            self.directory_fd = None
        if self.common_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.common_fd)
            self.common_fd = None

    def _validate_state_entry(self):
        try:
            entry = os.stat(
                "state.json", dir_fd=self.directory_fd, follow_symlinks=False
            )
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(entry.st_mode):
            raise UpgradeBlocked("upgrade state file must be a regular file")
        return True

    def state_exists(self):
        return self._validate_state_entry()

    def read_state_bytes(self):
        if not self._validate_state_entry():
            raise UpgradeBlocked("no active upgrade state exists")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open("state.json", flags, dir_fd=self.directory_fd)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise UpgradeBlocked("upgrade state file must be a regular file")
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "rb") as state_file:
                descriptor = None
                return state_file.read()
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def write_state(self, state):
        if self._validate_state_entry():
            existing = os.stat(
                "state.json", dir_fd=self.directory_fd, follow_symlinks=False
            )
            if not stat.S_ISREG(existing.st_mode):
                raise UpgradeBlocked("upgrade state file must be a regular file")
        temporary_name = f"state.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(
            temporary_name, flags, 0o600, dir_fd=self.directory_fd
        )
        try:
            os.fchmod(descriptor, 0o600)
            content = (
                json.dumps(state, indent=2, sort_keys=True) + "\n"
            ).encode("utf-8")
            with os.fdopen(descriptor, "wb") as temporary:
                descriptor = None
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(
                temporary_name,
                "state.json",
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            state_fd = os.open(
                "state.json",
                os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
                dir_fd=self.directory_fd,
            )
            try:
                os.fchmod(state_fd, 0o600)
                os.fsync(state_fd)
            finally:
                os.close(state_fd)
            os.fsync(self.directory_fd)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name, dir_fd=self.directory_fd)

    def archive_state(self, state):
        """Atomically rename the active state without overwriting prior audit."""
        self._validate_state_entry()
        archive_name = f"completed-{state['release']}.json"
        try:
            os.stat(archive_name, dir_fd=self.directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise UpgradeBlocked("completed state archive already exists")
        try:
            os.rename(
                "state.json",
                archive_name,
                src_dir_fd=self.directory_fd,
                dst_dir_fd=self.directory_fd,
            )
            archive_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                archive_flags |= os.O_NOFOLLOW
            archive_fd = os.open(
                archive_name, archive_flags, dir_fd=self.directory_fd
            )
            try:
                if not stat.S_ISREG(os.fstat(archive_fd).st_mode):
                    raise UpgradeBlocked(
                        "completed state archive must be a regular file"
                    )
                os.fchmod(archive_fd, 0o600)
                os.fsync(archive_fd)
            finally:
                os.close(archive_fd)
            os.fsync(self.directory_fd)
        except OSError as error:
            raise UpgradeBlocked("unable to archive completed upgrade state") from error
        return self.directory_path / archive_name


def _write_state(storage, state):
    storage.write_state(state)


STATE_KEYS = frozenset(
    (
        "schema_version",
        "release",
        "tag_object",
        "peeled_commit",
        "internal_ref",
        "branch",
        "worktree",
        "phase",
        "source_commit",
        "source_branch",
    )
)

EVIDENCE_KEYS = frozenset(
    (
        "candidate_head",
        "candidate_tree",
        "configuration_hashes",
        "report_blobs",
        "report_content_sha256",
        "validated_head",
        "validated_tree",
        "validation_summary_sha256",
    )
)
EVIDENCE_MAPPING_KEYS = {
    "configuration_hashes": frozenset(
        ("customization", "known_failures", "lock", "migrations")
    ),
    "report_blobs": frozenset(("json", "markdown")),
    "report_content_sha256": frozenset(("json", "markdown")),
}
CONFLICT_KEYS = frozenset(
    ("binding_sha256", "customization_sha256", "paths")
)
BASELINE_PENDING_KEYS = frozenset(("configuration_hashes", "pre_head"))


def _validate_hash_mapping(values, expected_keys, pattern, label):
    if not isinstance(values, dict) or set(values) != expected_keys:
        raise UpgradeBlocked(f"upgrade state {label} is invalid")
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or not pattern.fullmatch(value)
        for key, value in values.items()
    ):
        raise UpgradeBlocked(f"upgrade state {label} is invalid")


def _load_state(repository, storage):
    path = storage.state_path
    try:
        raw_state = storage.read_state_bytes().decode("utf-8")
        state = json.loads(
            raw_state,
            object_pairs_hook=lambda pairs: _strict_object(pairs),
        )
        if not isinstance(state, dict):
            raise ValueError("state root must be an object")
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UpgradeBlocked("upgrade state is not valid strict JSON") from error
    allowed_keys = STATE_KEYS | {"baseline", "conflicts", "evidence"}
    if not STATE_KEYS.issubset(state) or not set(state).issubset(allowed_keys):
        missing = ",".join(sorted(STATE_KEYS - set(state))) or "none"
        extra = ",".join(sorted(set(state) - allowed_keys)) or "none"
        raise UpgradeBlocked(f"upgrade state keys missing={missing} extra={extra}")
    if type(state["schema_version"]) is not int or state["schema_version"] != 1:
        raise UpgradeBlocked("upgrade state schema_version must be integer 1")
    if not isinstance(state["release"], str) or not RELEASE_PATTERN.fullmatch(
        state["release"]
    ):
        raise UpgradeBlocked("upgrade state release is invalid")
    for field in ("tag_object", "peeled_commit", "source_commit"):
        if not isinstance(state[field], str) or not OBJECT_ID_PATTERN.fullmatch(
            state[field]
        ):
            raise UpgradeBlocked(f"upgrade state {field} must be a 40 character oid")
    expected_branch = f"upgrade/{state['release']}"
    expected_internal_ref = (
        f"refs/steadflow-upstream/releases/{state['release']}"
    )
    if state["branch"] != expected_branch:
        raise UpgradeBlocked("upgrade state branch does not match release")
    if state["internal_ref"] != expected_internal_ref:
        raise UpgradeBlocked("upgrade state internal_ref does not match release")
    if state["phase"] not in {"merging", "conflicted", "validating", "merged"}:
        raise UpgradeBlocked("upgrade state phase is invalid")
    if state["phase"] == "conflicted" and "conflicts" not in state:
        raise UpgradeBlocked("conflicted upgrade state requires conflict evidence")
    if "conflicts" in state:
        conflicts = state["conflicts"]
        if not isinstance(conflicts, dict) or set(conflicts) != CONFLICT_KEYS:
            raise UpgradeBlocked("upgrade state conflict evidence keys are invalid")
        paths = conflicts["paths"]
        try:
            _validate_repo_paths("conflict paths", paths)
        except ManifestValidationError as error:
            raise UpgradeBlocked(str(error)) from error
        if paths != sorted(set(paths)) or not paths:
            raise UpgradeBlocked("upgrade state conflict paths are invalid")
        for field in ("binding_sha256", "customization_sha256"):
            if not isinstance(conflicts[field], str) or not SHA256_PATTERN.fullmatch(
                conflicts[field]
            ):
                raise UpgradeBlocked(f"upgrade state conflict {field} is invalid")
    if "evidence" in state:
        evidence = state["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
            raise UpgradeBlocked("upgrade state evidence keys are invalid")
        for field in (
            "candidate_head",
            "candidate_tree",
            "validated_head",
            "validated_tree",
        ):
            if not isinstance(evidence[field], str) or not OBJECT_ID_PATTERN.fullmatch(
                evidence[field]
            ):
                raise UpgradeBlocked(f"upgrade state evidence {field} is invalid")
        for field in (
            "configuration_hashes",
            "report_blobs",
            "report_content_sha256",
        ):
            values = evidence[field]
            _validate_hash_mapping(
                values,
                EVIDENCE_MAPPING_KEYS[field],
                OBJECT_ID_PATTERN if field == "report_blobs" else SHA256_PATTERN,
                f"evidence {field}",
            )
        if not isinstance(
            evidence["validation_summary_sha256"], str
        ) or not SHA256_PATTERN.fullmatch(evidence["validation_summary_sha256"]):
            raise UpgradeBlocked(
                "upgrade state evidence validation_summary_sha256 is invalid"
            )
    if "baseline" in state:
        pending = state["baseline"]
        if not isinstance(pending, dict) or set(pending) != BASELINE_PENDING_KEYS:
            raise UpgradeBlocked("upgrade state baseline evidence is invalid")
        if not isinstance(pending["pre_head"], str) or not OBJECT_ID_PATTERN.fullmatch(
            pending["pre_head"]
        ):
            raise UpgradeBlocked("upgrade state baseline pre_head is invalid")
        _validate_hash_mapping(
            pending["configuration_hashes"],
            EVIDENCE_MAPPING_KEYS["configuration_hashes"],
            SHA256_PATTERN,
            "baseline configuration_hashes",
        )
    if state["phase"] == "merged" and "evidence" not in state:
        raise UpgradeBlocked("merged upgrade state requires evidence")
    if state["phase"] != "merged" and "evidence" in state:
        raise UpgradeBlocked("unfinished upgrade state cannot contain evidence")
    if state["phase"] == "merged" and "baseline" in state:
        raise UpgradeBlocked("merged upgrade state cannot retain baseline evidence")
    if not isinstance(state["source_branch"], str) or not state["source_branch"]:
        raise UpgradeBlocked("upgrade state source_branch is invalid")
    if not isinstance(state["worktree"], str) or not Path(
        state["worktree"]
    ).is_absolute():
        raise UpgradeBlocked("upgrade state worktree must be absolute")
    expected_worktree = _upgrade_worktree_path(repository, state["release"])
    if Path(state["worktree"]).resolve() != expected_worktree:
        raise UpgradeBlocked("upgrade state worktree path is inconsistent")
    return path, state


def _strict_object(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _source_identity(repository):
    source_commit = repository.run(
        "rev-parse", "HEAD", operation="source commit lookup", read_only=True
    ).stdout.strip()
    branch = repository.run(
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        check=False,
        read_only=True,
    )
    if branch.returncode != 0 or not branch.stdout.strip():
        raise UpgradeBlocked("source checkout must be on a branch")
    return source_commit, branch.stdout.strip()


def _upgrade_worktree_path(repository, release):
    worktrees_root = (repository.root / ".worktrees").resolve()
    if worktrees_root.parent != repository.root:
        raise UpgradeBlocked("upgrade worktree root escapes repository")
    worktree = (worktrees_root / f"upgrade-{release}").resolve()
    if worktree.parent != worktrees_root:
        raise UpgradeBlocked("upgrade worktree path escapes .worktrees")
    ignored = repository.run(
        "check-ignore", str(worktree), check=False, read_only=True
    )
    if ignored.returncode != 0:
        raise UpgradeBlocked("upgrade worktree path must be ignored")
    return worktree


def _fetch_release(repository, release, internal_ref):
    existing = repository.run(
        "show-ref", "--verify", internal_ref, check=False
    )
    if existing.returncode == 0:
        tag_object = repository.run(
            "rev-parse", internal_ref, operation="internal release object lookup"
        ).stdout.strip()
        remote_object = _remote_release_object(repository, release)
        if tag_object != remote_object:
            raise UpgradeBlocked("upstream release tag changed after internal fetch")
        peeled = repository.run(
            "rev-parse",
            f"{internal_ref}^{{commit}}",
            operation="internal release peel",
        ).stdout.strip()
        if repository.run(
            "cat-file", "-t", peeled, operation="internal release type validation"
        ).stdout.strip() != "commit":
            raise UpgradeBlocked("internal release does not peel to a commit")
        return tag_object, peeled
    advertised_object = _remote_release_object(repository, release)
    repository.run(
        "fetch",
        "--no-tags",
        "upstream",
        f"refs/tags/{release}:{internal_ref}",
        operation=f"fetch of exact upstream release {release}",
    )
    tag_object = repository.run(
        "rev-parse", internal_ref, operation="fetched release object lookup"
    ).stdout.strip()
    if tag_object != advertised_object:
        raise UpgradeBlocked("upstream release tag changed during exact fetch")
    peeled = repository.run(
        "rev-parse",
        f"{internal_ref}^{{commit}}",
        operation="fetched release peel",
    ).stdout.strip()
    if repository.run(
        "cat-file", "-t", peeled, operation="fetched release type validation"
    ).stdout.strip() != "commit":
        raise UpgradeBlocked("fetched release does not peel to a commit")
    return tag_object, peeled


def _remote_release_object(repository, release):
    completed = repository.run(
        "ls-remote",
        "--tags",
        "upstream",
        f"refs/tags/{release}",
        operation=f"upstream release lookup for {release}",
    )
    lines = [line.split("\t", 1) for line in completed.stdout.splitlines()]
    expected_ref = f"refs/tags/{release}"
    matches = [oid for oid, ref in lines if ref == expected_ref]
    if len(matches) != 1 or not OBJECT_ID_PATTERN.fullmatch(matches[0]):
        raise UpgradeBlocked(f"upstream release tag {release} is missing or ambiguous")
    return matches[0]


def _validation_runner(repository):
    return getattr(repository, "validation_runner", subprocess.run)


def _run_validation_process(repository, argv, cwd):
    return _validation_runner(repository)(
        argv,
        cwd=Path(cwd),
        env=_critical_environment(),
        capture_output=True,
        text=True,
    )


def _candidate_binding(repository, worktree):
    head = repository.run(
        "rev-parse", "HEAD", cwd=worktree, operation="candidate report HEAD", read_only=True
    ).stdout.strip()
    tree = repository.run(
        "rev-parse",
        "HEAD^{tree}",
        cwd=worktree,
        operation="candidate report tree",
        read_only=True,
    ).stdout.strip()
    candidate_repository = GitRepository(worktree)
    paths = _configuration_paths(candidate_repository)
    return head, tree, paths, {
        name: _raw_sha256(path) for name, path in sorted(paths.items())
    }


BASELINE_CONFIGURATION_PATHS = {
    "customization": ".steadflow/customization.yml",
    "known_failures": ".steadflow/known-failures.yml",
    "lock": ".steadflow/upstream-lock.json",
    "migrations": ".steadflow/migration-checksums.json",
}


def _source_configuration_documents(repository, state, worktree):
    return {
        name: _load_json_bytes(
            repository.read_blob_bytes(state["source_commit"], path, cwd=worktree),
            f"source {name} configuration",
        )
        for name, path in sorted(BASELINE_CONFIGURATION_PATHS.items())
    }


def _reconciled_manifest(repository, state, worktree, source_manifest):
    manifest = json.loads(json.dumps(source_manifest))
    report_paths = set(_validation_report_relative_paths(state["release"]))
    current_changes = _git_change_summary(
        repository,
        state["peeled_commit"],
        repository.run("rev-parse", "HEAD", cwd=worktree, read_only=True).stdout.strip(),
        worktree,
    )
    expected_paths = set(_summary_paths(current_changes)) | report_paths
    integration = set(manifest["integration_adapter"]["paths"])
    integration.update(report_paths)
    manifest["integration_adapter"]["paths"] = sorted(integration)
    for layer in OWNER_LAYER_KEYS:
        manifest[layer]["paths"] = sorted(
            path for path in manifest[layer]["paths"] if path in expected_paths
        )
    registered = {
        path
        for layer in OWNER_LAYER_KEYS
        for path in manifest[layer]["paths"]
    }
    unowned = sorted(expected_paths - registered)
    if unowned:
        raise UpgradeBlocked(
            "final candidate has unowned paths: " + ", ".join(unowned)
        )
    manifest["shared_seams"] = sorted(
        path for path in manifest["shared_seams"] if path in registered
    )
    manifest["generated"]["paths"] = sorted(
        path for path in manifest["generated"]["paths"] if path in registered
    )
    validate_manifest(manifest, sorted(expected_paths), require_exact=True)
    return manifest


def _advanced_baseline_documents(
    repository,
    state,
    worktree,
    source_documents,
    go_completed,
    vitest_completed,
    go_results,
    vitest_results,
):
    upstream_tree = repository.run(
        "rev-parse", f"{state['peeled_commit']}^{{tree}}", cwd=worktree, read_only=True
    ).stdout.strip()
    lock = dict(source_documents["lock"])
    lock.update(
        {
            "peeled_commit": state["peeled_commit"],
            "release": state["release"],
            "tree": upstream_tree,
        }
    )
    _validate_upstream_lock(lock)

    known = json.loads(json.dumps(source_documents["known_failures"]))
    baseline = known["baseline"]
    baseline["release"] = state["release"]
    baseline["commands"] = [
        "cd backend && go test -json ./... > "
        f"../.steadflow/work/{state['release']}-go-test.jsonl",
        "pnpm --dir frontend exec vitest run --reporter=json --outputFile="
        f"../.steadflow/work/{state['release']}-vitest.json",
    ]
    baseline["evidence"] = {
        "go_json_sha256": hashlib.sha256(
            go_completed.stdout.encode("utf-8")
        ).hexdigest(),
        "vitest_json_sha256": hashlib.sha256(
            vitest_completed.stdout.encode("utf-8")
        ).hexdigest(),
    }
    baseline["result"] = {
        "failed": len(go_results["failed"]) + len(vitest_results["failed"]),
        "go_failed": len(go_results["failed"]),
        "vitest_failed": len(vitest_results["failed"]),
    }
    baseline["historical_observation"] = (
        f"Certified {state['release']} full Go and Vitest run; exact allowed "
        "failures, if any, remain governed by entries."
    )
    validate_known_failures(known)

    migrations = dict(source_documents["migrations"])
    migrations["migrations"] = migration_checksums(worktree)
    if "reviewed_additions" in migrations:
        migrations["reviewed_additions"] = {
            path: review
            for path, review in migrations["reviewed_additions"].items()
            if path not in migrations["migrations"]
        }
        if not migrations["reviewed_additions"]:
            migrations.pop("reviewed_additions")
    validate_migrations(worktree, migrations)
    manifest = _reconciled_manifest(
        repository, state, worktree, source_documents["customization"]
    )
    return {
        "customization": manifest,
        "known_failures": known,
        "lock": lock,
        "migrations": migrations,
    }


def _commit_or_reuse_advanced_baseline(
    repository,
    storage,
    state,
    worktree,
    source_documents,
    go_completed,
    vitest_completed,
    go_results,
    vitest_results,
):
    documents = _advanced_baseline_documents(
        repository,
        state,
        worktree,
        source_documents,
        go_completed,
        vitest_completed,
        go_results,
        vitest_results,
    )
    canonical = {
        BASELINE_CONFIGURATION_PATHS[name]: _canonical_json_bytes(document)
        for name, document in documents.items()
    }
    pre_head = repository.run(
        "rev-parse", "HEAD", cwd=worktree, read_only=True
    ).stdout.strip()
    state["baseline"] = {
        "configuration_hashes": {
            name: hashlib.sha256(canonical[path]).hexdigest()
            for name, path in sorted(BASELINE_CONFIGURATION_PATHS.items())
        },
        "pre_head": pre_head,
    }
    _write_state(storage, state)
    for relative_path, content in canonical.items():
        _atomic_write_configuration(
            worktree, PurePosixPath(relative_path).name, content
        )
    _upgrade_test_hook(repository, "after_baseline_files", state=state)
    trusted_oids = {
        path: repository.hash_blob_bytes(content, cwd=worktree)
        for path, content in canonical.items()
    }
    for path, oid in trusted_oids.items():
        repository.run_without_hooks(
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{oid},{path}",
            cwd=worktree,
            operation="trusted baseline index update",
        )
    staged = sorted(
        path
        for path in repository.run(
            "diff", "--cached", "--name-only", "-z", cwd=worktree, read_only=True
        ).stdout.split("\0")
        if path
    )
    required = {
        BASELINE_CONFIGURATION_PATHS["customization"],
        BASELINE_CONFIGURATION_PATHS["known_failures"],
        BASELINE_CONFIGURATION_PATHS["lock"],
    }
    if not required.issubset(staged) or not set(staged).issubset(canonical):
        raise UpgradeBlocked("candidate baseline staging changed unexpected paths")
    _upgrade_test_hook(repository, "after_baseline_index", state=state)
    validated_date = repository.run(
        "show", "-s", "--format=%cI", "HEAD", cwd=worktree, read_only=True
    ).stdout.strip()
    committed = repository.run_without_hooks(
        "commit",
        "--no-verify",
        "--no-gpg-sign",
        "-m",
        f"chore(upstream): advance baseline to {state['release']}",
        cwd=worktree,
        check=False,
        extra_environment={
            "GIT_AUTHOR_DATE": validated_date,
            "GIT_AUTHOR_EMAIL": "upgrade@steadflow.invalid",
            "GIT_AUTHOR_NAME": "Steadflow Upgrade",
            "GIT_COMMITTER_DATE": validated_date,
            "GIT_COMMITTER_EMAIL": "upgrade@steadflow.invalid",
            "GIT_COMMITTER_NAME": "Steadflow Upgrade",
            "GIT_EDITOR": "true",
            "GIT_SEQUENCE_EDITOR": "true",
        },
    )
    if committed.returncode != 0:
        raise UpgradeBlocked("candidate baseline commit failed; candidate preserved")
    _upgrade_test_hook(repository, "after_baseline_commit", state=state)
    state.pop("baseline", None)
    _write_state(storage, state)
    return documents


def _validate_advanced_baseline(repository, state, worktree):
    paths = _configuration_paths(GitRepository(worktree))
    manifest = load_json_document(paths["customization"])
    known = load_json_document(paths["known_failures"])
    migrations = load_json_document(paths["migrations"])
    lock = load_json_document(paths["lock"])
    _validate_upstream_lock(lock)
    if lock["release"] != state["release"] or lock["peeled_commit"] != state["peeled_commit"]:
        raise UpgradeBlocked("candidate baseline lock does not match target release")
    expected_tree = repository.run(
        "rev-parse", f"{state['peeled_commit']}^{{tree}}", cwd=worktree, read_only=True
    ).stdout.strip()
    if lock["tree"] != expected_tree:
        raise UpgradeBlocked("candidate baseline lock tree does not match target release")
    validate_known_failures(known)
    if known["baseline"]["release"] != state["release"]:
        raise UpgradeBlocked("candidate known-failure baseline was not advanced")
    migration_report = validate_migrations(worktree, migrations)
    if migration_report["added"]:
        raise UpgradeBlocked("candidate migration baseline was not advanced")
    head = repository.run("rev-parse", "HEAD", cwd=worktree, read_only=True).stdout.strip()
    changes = _git_change_summary(
        repository, state["peeled_commit"], head, worktree
    )
    expected_paths = set(_summary_paths(changes)) | set(
        _validation_report_relative_paths(state["release"])
    )
    ownership = validate_manifest(manifest, sorted(expected_paths), require_exact=True)
    return {
        "changed": sorted(expected_paths),
        "multiply_owned": ownership["multiply_owned"],
        "registered": ownership["registered"],
        "status": "PASS",
        "unowned": ownership["unowned"],
    }


def _validation_report_paths(worktree, release):
    reports = Path(worktree) / ".steadflow" / "reports"
    return reports / f"{release}.json", reports / f"{release}.md"


def _validation_report_relative_paths(release):
    return (
        f".steadflow/reports/{release}.json",
        f".steadflow/reports/{release}.md",
    )


def _candidate_status_entries(repository, worktree):
    output = repository.run(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        cwd=worktree,
        operation="candidate validation status inspection",
        read_only=True,
    ).stdout
    entries = []
    for record in output.split("\0"):
        if not record:
            continue
        if len(record) < 4 or record[2] != " ":
            raise UpgradeBlocked("candidate status entry is invalid")
        status = record[:2]
        path = record[3:]
        if any(code in status for code in "RCD"):
            raise UpgradeBlocked("candidate report paths must not be renamed or deleted")
        entries.append((status, path))
    return entries


def _require_report_only_candidate_dirt(repository, state, worktree):
    allowed = set(_validation_report_relative_paths(state["release"]))
    entries = _candidate_status_entries(repository, worktree)
    unexpected = sorted(path for _, path in entries if path not in allowed)
    if unexpected:
        raise UpgradeBlocked(
            "candidate dirt outside exact candidate report paths: "
            + ", ".join(unexpected)
        )
    root = Path(worktree).resolve()
    reports = root / ".steadflow" / "reports"
    if entries:
        try:
            steadflow_mode = (root / ".steadflow").lstat().st_mode
            reports_mode = reports.lstat().st_mode
        except FileNotFoundError as error:
            raise UpgradeBlocked("candidate report directory is missing") from error
        if (
            not stat.S_ISDIR(steadflow_mode)
            or stat.S_ISLNK(steadflow_mode)
            or not stat.S_ISDIR(reports_mode)
            or stat.S_ISLNK(reports_mode)
            or reports.resolve() != reports.absolute()
        ):
            raise UpgradeBlocked("candidate report directory must be a real directory")
        for _, relative_path in entries:
            report_path = root / relative_path
            try:
                mode = report_path.lstat().st_mode
            except FileNotFoundError as error:
                raise UpgradeBlocked("candidate report file is missing") from error
            if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                raise UpgradeBlocked("candidate report file must be regular")
    return entries


def _tracked_report_blob(repository, worktree, revision, relative_path):
    completed = repository.run(
        "rev-parse",
        "--verify",
        f"{revision}:{relative_path}",
        cwd=worktree,
        check=False,
        read_only=True,
    )
    if completed.returncode != 0:
        return None
    blob = completed.stdout.strip()
    if not OBJECT_ID_PATTERN.fullmatch(blob):
        raise UpgradeBlocked("candidate report blob identity is invalid")
    kind = repository.run(
        "cat-file", "-t", blob, cwd=worktree, read_only=True
    ).stdout.strip()
    if kind != "blob":
        raise UpgradeBlocked("candidate report tree entry is not a blob")
    return blob


def _parse_canonical_success_report(state, worktree, canonical_bytes):
    relative_json, relative_markdown = _validation_report_relative_paths(
        state["release"]
    )
    try:
        report = json.loads(
            canonical_bytes[relative_json].decode("utf-8"),
            object_pairs_hook=lambda pairs: _strict_object(pairs),
        )
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise UpgradeBlocked("candidate success report is not strict JSON") from error
    _validate_success_report(report, state, worktree)
    _, expected_json, expected_markdown = _canonical_report_contents(
        state["release"], report
    )
    if (
        canonical_bytes != {
            relative_json: expected_json,
            relative_markdown: expected_markdown,
        }
    ):
        raise UpgradeBlocked("candidate success reports are not canonical")
    return report


def _report_commit_binding(repository, state, worktree, trusted_oids):
    relative_paths = _validation_report_relative_paths(state["release"])
    candidate_head = repository.run(
        "rev-parse", "HEAD", cwd=worktree, read_only=True
    ).stdout.strip()
    candidate_tree = repository.run(
        "rev-parse", "HEAD^{tree}", cwd=worktree, read_only=True
    ).stdout.strip()
    parents = repository.run(
        "show", "-s", "--format=%P", "HEAD", cwd=worktree, read_only=True
    ).stdout.split()
    if len(parents) != 1:
        raise UpgradeBlocked("candidate evidence commit must have one validated parent")
    changed = sorted(
        path
        for path in repository.run(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD",
            cwd=worktree, read_only=True,
        ).stdout.split("\0")
        if path
    )
    if changed != sorted(relative_paths):
        raise UpgradeBlocked("candidate evidence commit changed non-report paths")
    actual_oids = {
        path: _tracked_report_blob(repository, worktree, "HEAD", path)
        for path in relative_paths
    }
    if actual_oids != trusted_oids:
        raise UpgradeBlocked("candidate evidence commit report blobs are invalid")
    validated_head = parents[0]
    validated_tree = repository.run(
        "rev-parse", f"{validated_head}^{{tree}}", cwd=worktree, read_only=True
    ).stdout.strip()
    _, _, _, configuration_hashes = _candidate_binding(repository, worktree)
    return candidate_head, candidate_tree, validated_head, validated_tree, configuration_hashes


def _commit_or_reuse_success_reports(repository, state, worktree, report, artifacts):
    relative_paths = _validation_report_relative_paths(state["release"])
    if set(artifacts.canonical_bytes) != set(relative_paths):
        raise UpgradeBlocked("candidate report artifacts are incomplete")
    _parse_canonical_success_report(state, worktree, artifacts.canonical_bytes)
    _require_report_path_identity(
        worktree, state["release"], artifacts.identity_token
    )
    _upgrade_test_hook(repository, "after_report_identity_check", state=state)
    trusted_oids = {
        path: repository.hash_blob_bytes(content, cwd=worktree)
        for path, content in artifacts.canonical_bytes.items()
    }
    for path in relative_paths:
        repository.run_without_hooks(
            "update-index",
            "--add",
            "--cacheinfo",
            f"100644,{trusted_oids[path]},{path}",
            cwd=worktree,
            operation="trusted report index update",
        )
    staged = sorted(
        path
        for path in repository.run(
            "diff", "--cached", "--name-only", "-z", cwd=worktree, read_only=True
        ).stdout.split("\0")
        if path
    )
    if staged and staged != sorted(relative_paths):
        raise UpgradeBlocked("candidate report staging changed unexpected paths")
    for path in relative_paths:
        index_entry = repository.run(
            "ls-files", "--stage", "--", path, cwd=worktree, read_only=True
        ).stdout.strip().split()
        if len(index_entry) < 3 or index_entry[0] != "100644" or index_entry[1] != trusted_oids[path]:
            raise UpgradeBlocked("candidate report index blob audit failed")
    _upgrade_test_hook(repository, "after_report_index", state=state)
    if staged:
        validated_date = repository.run(
            "show", "-s", "--format=%cI", "HEAD", cwd=worktree, read_only=True
        ).stdout.strip()
        committed = repository.run_without_hooks(
            "commit",
            "--no-verify",
            "--no-gpg-sign",
            "-m",
            f"chore(upstream): record {state['release']} validation report",
            cwd=worktree,
            check=False,
            extra_environment={
                "GIT_AUTHOR_DATE": validated_date,
                "GIT_AUTHOR_EMAIL": "upgrade@steadflow.invalid",
                "GIT_AUTHOR_NAME": "Steadflow Upgrade",
                "GIT_COMMITTER_DATE": validated_date,
                "GIT_COMMITTER_EMAIL": "upgrade@steadflow.invalid",
                "GIT_COMMITTER_NAME": "Steadflow Upgrade",
                "GIT_EDITOR": "true",
                "GIT_SEQUENCE_EDITOR": "true",
            },
        )
        if committed.returncode != 0:
            raise UpgradeBlocked("candidate report commit failed; evidence preserved")
    binding = _report_commit_binding(
        repository, state, worktree, trusted_oids
    )
    _upgrade_test_hook(repository, "after_evidence_commit", state=state)
    _require_report_path_identity(
        worktree, state["release"], artifacts.identity_token
    )
    _require_clean_candidate(repository, worktree)
    candidate_head, candidate_tree, validated_head, validated_tree, configuration_hashes = binding
    if (
        report["candidate_head"] != validated_head
        or report["candidate_tree"] != validated_tree
        or report["configuration_hashes"] != configuration_hashes
    ):
        raise UpgradeBlocked("candidate report evidence binding is invalid")
    _require_merged_ancestry(repository, state, candidate_head, worktree)
    report_blobs = {
        "json": trusted_oids[relative_paths[0]],
        "markdown": trusted_oids[relative_paths[1]],
    }
    return {
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "configuration_hashes": configuration_hashes,
        "report_blobs": report_blobs,
        "report_content_sha256": {
            "json": hashlib.sha256(artifacts.canonical_bytes[relative_paths[0]]).hexdigest(),
            "markdown": hashlib.sha256(artifacts.canonical_bytes[relative_paths[1]]).hexdigest(),
        },
        "validated_head": validated_head,
        "validated_tree": validated_tree,
        "validation_summary_sha256": report["validation_summary_sha256"],
    }


def _require_success_report(repository, state, worktree):
    expected = state.get("evidence")
    if not isinstance(expected, dict):
        raise UpgradeBlocked("candidate state has no completed evidence")
    contents, _ = _read_report_artifacts(worktree, state["release"])
    relative_paths = _validation_report_relative_paths(state["release"])
    canonical_bytes = dict(zip(relative_paths, contents))
    report = _parse_canonical_success_report(state, worktree, canonical_bytes)
    trusted_oids = {
        path: _git_blob_oid(canonical_bytes[path]) for path in relative_paths
    }
    binding = _report_commit_binding(repository, state, worktree, trusted_oids)
    candidate_head, candidate_tree, validated_head, validated_tree, configuration_hashes = binding
    if (
        report["candidate_head"] != validated_head
        or report["candidate_tree"] != validated_tree
        or report["configuration_hashes"] != configuration_hashes
    ):
        raise UpgradeBlocked("candidate report evidence binding is invalid")
    actual = {
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "configuration_hashes": configuration_hashes,
        "report_blobs": {"json": trusted_oids[relative_paths[0]], "markdown": trusted_oids[relative_paths[1]]},
        "report_content_sha256": {
            "json": hashlib.sha256(canonical_bytes[relative_paths[0]]).hexdigest(),
            "markdown": hashlib.sha256(canonical_bytes[relative_paths[1]]).hexdigest(),
        },
        "validated_head": validated_head,
        "validated_tree": validated_tree,
        "validation_summary_sha256": report["validation_summary_sha256"],
    }
    if actual != expected:
        raise UpgradeBlocked("candidate state and report evidence binding is invalid")
    return actual


def _stable_string_list(value, label):
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or value != sorted(set(value))
    ):
        raise UpgradeBlocked(f"candidate success report {label} is invalid")


def _success_report_digest(report):
    summary = dict(report)
    summary.pop("validation_summary_sha256", None)
    return hashlib.sha256(
        json.dumps(
            summary, ensure_ascii=True, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def _source_configuration_hashes(repository, source_commit, worktree):
    relative_paths = {
        "customization": ".steadflow/customization.yml",
        "known_failures": ".steadflow/known-failures.yml",
        "lock": ".steadflow/upstream-lock.json",
        "migrations": ".steadflow/migration-checksums.json",
    }
    return {
        name: hashlib.sha256(
            repository.read_blob_bytes(source_commit, path, cwd=worktree)
        ).hexdigest()
        for name, path in sorted(relative_paths.items())
    }


def _upgrade_audit_context(
    repository, state, worktree, manifest, lock, *, require_source_configuration=True
):
    _validate_upstream_lock(lock)
    locked_tree = repository.run(
        "rev-parse",
        f"{lock['peeled_commit']}^{{tree}}",
        cwd=worktree,
        operation="locked upstream tree inspection",
        read_only=True,
    ).stdout.strip()
    if locked_tree != lock["tree"]:
        raise UpgradeBlocked("candidate lock tree does not match locked commit")
    candidate_paths = _configuration_paths(GitRepository(worktree))
    candidate_configuration_hashes = {
        name: _raw_sha256(path) for name, path in sorted(candidate_paths.items())
    }
    if require_source_configuration and candidate_configuration_hashes != _source_configuration_hashes(
        repository, state["source_commit"], worktree
    ):
        raise UpgradeBlocked("candidate configuration differs from source commit")
    for ancestor, descendant, label in (
        (lock["peeled_commit"], state["source_commit"], "source"),
        (lock["peeled_commit"], state["peeled_commit"], "target release"),
    ):
        if not _is_ancestor(repository, ancestor, descendant, worktree):
            raise UpgradeBlocked(f"locked upstream commit is not an ancestor of {label}")
    customization_changes, ownership = _strict_fork_ownership(
        repository,
        manifest,
        lock["peeled_commit"],
        state["source_commit"],
        worktree,
    )
    upstream_changes = _git_change_summary(
        repository, lock["peeled_commit"], state["peeled_commit"], worktree
    )
    upstream_tree = repository.run(
        "rev-parse",
        f"{state['peeled_commit']}^{{tree}}",
        cwd=worktree,
        operation="target upstream tree inspection",
        read_only=True,
    ).stdout.strip()
    if not OBJECT_ID_PATTERN.fullmatch(upstream_tree):
        raise UpgradeBlocked("target upstream tree is invalid")
    registered_seams = sorted(manifest["shared_seams"])
    changed_seams = sorted(
        set(registered_seams).intersection(_summary_paths(upstream_changes))
    )
    conflict_seams = list(state.get("conflicts", {}).get("paths", []))
    if sorted(set(conflict_seams)) != conflict_seams or not set(
        conflict_seams
    ).issubset(registered_seams):
        raise UpgradeBlocked("candidate conflict seams are invalid")
    return {
        "file_changes": {
            "fork_customizations": customization_changes,
            "upstream": upstream_changes,
        },
        "ownership": ownership,
        "peeled_commit": state["peeled_commit"],
        "seams": {
            "changed": changed_seams,
            "conflicts": conflict_seams,
            "registered": registered_seams,
            "status": "PASS",
        },
        "source_commit": state["source_commit"],
        "tag_object": state["tag_object"],
        "upstream_tree": upstream_tree,
    }


def _validate_success_report(report, state, worktree):
    """Fail closed unless a report fully proves every candidate gate."""
    if not isinstance(report, dict) or set(report) != SUCCESS_REPORT_KEYS:
        raise UpgradeBlocked("candidate success report schema is incomplete")
    if (
        type(report["schema_version"]) is not int
        or report["schema_version"] != SUCCESS_REPORT_SCHEMA_VERSION
        or report["release"] != state["release"]
        or report["status"] not in {"PASS", "KNOWN-FAIL"}
    ):
        raise UpgradeBlocked("candidate success report identity is invalid")
    for field in ("candidate_head", "candidate_tree"):
        if not isinstance(report[field], str) or not OBJECT_ID_PATTERN.fullmatch(
            report[field]
        ):
            raise UpgradeBlocked(f"candidate success report {field} is invalid")
    _validate_hash_mapping(
        report["configuration_hashes"],
        EVIDENCE_MAPPING_KEYS["configuration_hashes"],
        SHA256_PATTERN,
        "success report configuration_hashes",
    )
    for name in ("full_go", "full_vitest"):
        result = report[name]
        if not isinstance(result, dict) or set(result) != {"failed", "passed", "skipped"}:
            raise UpgradeBlocked(f"candidate success report {name} is invalid")
        for bucket in ("failed", "passed", "skipped"):
            _stable_string_list(result[bucket], f"{name}.{bucket}")
        if not result["passed"] and not result["failed"]:
            raise UpgradeBlocked(f"candidate success report {name} executed zero tests")
    comparison = report["exact_comparison"]
    comparison_keys = {"critical", "expired", "fixed", "known", "new", "status"}
    if not isinstance(comparison, dict) or set(comparison) != comparison_keys:
        raise UpgradeBlocked("candidate exact comparison report is invalid")
    for bucket in comparison_keys - {"status"}:
        _stable_string_list(comparison[bucket], f"exact_comparison.{bucket}")
    if (
        comparison["status"] != report["status"]
        or comparison["critical"]
        or comparison["expired"]
        or comparison["fixed"]
        or comparison["new"]
    ):
        raise UpgradeBlocked("candidate exact comparison did not pass")
    migrations = report["migrations"]
    migration_keys = {
        "added", "added_risk", "changed", "deleted", "status", "unchanged"
    }
    if not isinstance(migrations, dict) or set(migrations) != migration_keys:
        raise UpgradeBlocked("candidate migration report is invalid")
    for bucket in ("added", "changed", "deleted", "unchanged"):
        _stable_string_list(migrations[bucket], f"migrations.{bucket}")
    if (
        migrations["status"] != "PASS"
        or migrations["changed"]
        or migrations["deleted"]
        or not isinstance(migrations["added_risk"], dict)
        or set(migrations["added_risk"]) != set(migrations["added"])
        or any(
            risk not in {"additive", "review-required", "reviewed-destructive"}
            for risk in migrations["added_risk"].values()
        )
    ):
        raise UpgradeBlocked("candidate migration report did not pass")
    candidate_repository = GitRepository(worktree)
    paths = _configuration_paths(candidate_repository)
    expected_configuration = {
        name: _raw_sha256(path) for name, path in sorted(paths.items())
    }
    source_documents = _source_configuration_documents(
        candidate_repository, state, worktree
    )
    expected_audit = _upgrade_audit_context(
        candidate_repository,
        state,
        worktree,
        source_documents["customization"],
        source_documents["lock"],
        require_source_configuration=False,
    )
    for field, expected in expected_audit.items():
        if field == "ownership":
            continue
        if report[field] != expected:
            raise UpgradeBlocked(
                f"candidate success report {field} binding is invalid"
            )
    expected_ownership = _validate_advanced_baseline(
        candidate_repository, state, worktree
    )
    if report["ownership"] != expected_ownership:
        raise UpgradeBlocked("candidate success report ownership binding is invalid")
    manifest = load_json_document(paths["customization"])
    expected_critical = [
        f"command:{command['name']}" for command in manifest["critical_commands"]
    ]
    critical = report["critical_commands"]
    if (
        not isinstance(critical, list)
        or [item.get("id") if isinstance(item, dict) else None for item in critical]
        != expected_critical
    ):
        raise UpgradeBlocked("candidate critical command report is incomplete")
    for item in critical:
        if (
            set(item) != {"failed", "id", "status", "tests_executed"}
            or item["failed"] != []
            or item["status"] != "PASS"
            or type(item["tests_executed"]) is not int
            or item["tests_executed"] <= 0
        ):
            raise UpgradeBlocked("candidate critical command did not pass")
    generated = report["generated"]
    if (
        not isinstance(generated, dict)
        or set(generated) != {"paths", "status"}
        or generated["status"] != "PASS"
        or generated["paths"] != manifest["generated"]["paths"]
    ):
        raise UpgradeBlocked("candidate generated-code report did not pass")
    if report["configuration_hashes"] != expected_configuration:
        raise UpgradeBlocked("candidate success report configuration binding is invalid")
    if report["validation_summary_sha256"] != _success_report_digest(report):
        raise UpgradeBlocked("candidate success report summary digest is invalid")
    return report


def validate_upgrade_candidate(repository, storage, state, worktree):
    """Run every candidate gate and return complete evidence before persistence."""
    candidate_head = ""
    candidate_tree = ""
    configuration_hashes = {}
    report = {
        "candidate_head": candidate_head,
        "candidate_tree": candidate_tree,
        "configuration_hashes": configuration_hashes,
        "critical_commands": [],
        "diagnostic": "",
        "generated": {"status": "NOT-RUN"},
        "migrations": {"status": "NOT-RUN"},
        "release": state["release"],
        "status": "NOT-RUN",
        "tests": {"status": "NOT-RUN"},
    }
    try:
        candidate_head, candidate_tree, paths, configuration_hashes = _candidate_binding(
            repository, worktree
        )
        report.update(
            {
                "candidate_head": candidate_head,
                "candidate_tree": candidate_tree,
                "configuration_hashes": configuration_hashes,
            }
        )
        source_documents = _source_configuration_documents(
            repository, state, worktree
        )
        current_hashes = {
            name: _raw_sha256(path) for name, path in sorted(paths.items())
        }
        source_hashes = _source_configuration_hashes(
            repository, state["source_commit"], worktree
        )
        baseline_already_advanced = current_hashes != source_hashes
        if baseline_already_advanced:
            _validate_advanced_baseline(repository, state, worktree)
        manifest = source_documents["customization"]
        lock = source_documents["lock"]
        baseline = source_documents["migrations"]
        known = source_documents["known_failures"]
        audit_context = _upgrade_audit_context(
            repository,
            state,
            worktree,
            manifest,
            lock,
            require_source_configuration=not baseline_already_advanced,
        )
        validate_known_failures(known)
        migrations = validate_migrations(worktree, baseline)
        report["migrations"] = {"status": "PASS", **migrations}

        go_completed = _run_validation_process(
            repository, ["go", "test", "-json", "./..."], Path(worktree) / "backend"
        )
        try:
            go_results = parse_go_test_jsonl(go_completed.stdout)
        except ValueError as error:
            detail = str(error)
            if go_completed.stderr.strip():
                detail += ": " + go_completed.stderr.strip()
            raise UpgradeBlocked(_redact_diagnostic(detail)) from error
        if not go_results["passed"] and not go_results["failed"]:
            raise UpgradeBlocked("full Go executed zero tests")
        if go_completed.returncode != 0 and not go_results["failed"]:
            raise UpgradeBlocked(
                _redact_diagnostic(
                    "full Go tests failed without exact test: "
                    + (go_completed.stderr.strip() or go_completed.stdout.strip())
                )
            )
        vitest_argv = [
            "pnpm",
            "--dir",
            "frontend",
            "exec",
            "vitest",
            "run",
            "--reporter=json",
        ]
        vitest_completed = _run_validation_process(
            repository, vitest_argv, worktree
        )
        try:
            vitest_document = json.loads(vitest_completed.stdout)
        except json.JSONDecodeError as error:
            detail = "full Vitest did not emit valid JSON"
            if vitest_completed.stderr.strip():
                detail += ": " + vitest_completed.stderr.strip()
            raise UpgradeBlocked(_redact_diagnostic(detail)) from error
        try:
            vitest_results = parse_vitest_json(vitest_document, repo_root=worktree)
        except ValueError as error:
            detail = str(error)
            if vitest_completed.stderr.strip():
                detail += ": " + vitest_completed.stderr.strip()
            raise UpgradeBlocked(_redact_diagnostic(detail)) from error
        if not vitest_results["passed"] and not vitest_results["failed"]:
            raise UpgradeBlocked("full Vitest executed zero tests")
        if vitest_completed.returncode != 0 and not vitest_results["failed"]:
            raise UpgradeBlocked(
                _redact_diagnostic(
                    "full Vitest failed without exact test: "
                    + (vitest_completed.stderr.strip() or vitest_completed.stdout.strip())
                )
            )
        comparison = compare_test_failures(
            go_results["failed"] + vitest_results["failed"],
            known,
            target_release=state["release"],
        )
        report["tests"] = {
            "comparison": comparison,
            "go": go_results,
            "status": comparison["status"],
            "vitest": vitest_results,
        }
        if comparison["status"] == "BLOCKED":
            raise UpgradeBlocked("exact failure comparison blocked candidate")

        critical = run_critical_suite(
            worktree,
            manifest["critical_commands"],
            runner=_validation_runner(repository),
        )
        if not critical:
            raise UpgradeBlocked("candidate declares zero critical commands")
        report["critical_commands"] = critical

        generated = _run_validation_process(
            repository, ["make", "-C", "backend", "generate"], worktree
        )
        if generated.returncode != 0:
            raise UpgradeBlocked(
                _redact_diagnostic(
                    "generated-code command failed: "
                    + (generated.stderr.strip() or generated.stdout.strip())
                )
            )
        generated_paths = manifest["generated"]["paths"]
        diff_arguments = ["diff", "--exit-code", "--", *generated_paths]
        generated_diff = repository.run(
            *diff_arguments, cwd=worktree, check=False, read_only=True
        )
        if generated_diff.returncode != 0:
            raise UpgradeBlocked("generated paths changed after regeneration")
        report["generated"] = {
            "paths": generated_paths,
            "status": "PASS",
        }
        if not baseline_already_advanced:
            _commit_or_reuse_advanced_baseline(
                repository,
                storage,
                state,
                worktree,
                source_documents,
                go_completed,
                vitest_completed,
                go_results,
                vitest_results,
            )
        candidate_head, candidate_tree, _, configuration_hashes = _candidate_binding(
            repository, worktree
        )
        report.update(
            {
                "candidate_head": candidate_head,
                "candidate_tree": candidate_tree,
                "configuration_hashes": configuration_hashes,
            }
        )
        advanced_ownership = _validate_advanced_baseline(
            repository, state, worktree
        )
        report["status"] = comparison["status"]
    except (OSError, ValueError, ManifestValidationError, MigrationValidationError, UpgradeBlocked) as error:
        report["diagnostic"] = _redact_diagnostic(error)
        report["status"] = "BLOCKED"
        try:
            write_upgrade_reports(worktree, state["release"], report)
        except (OSError, ValueError, UpgradeBlocked) as report_error:
            raise UpgradeBlocked(
                _redact_diagnostic(f"candidate validation and report failed: {report_error}")
            ) from error
        raise UpgradeBlocked(_redact_diagnostic(error)) from error
    success = {
        "candidate_head": report["candidate_head"],
        "candidate_tree": report["candidate_tree"],
        "configuration_hashes": report["configuration_hashes"],
        "critical_commands": report["critical_commands"],
        "exact_comparison": report["tests"]["comparison"],
        "file_changes": audit_context["file_changes"],
        "full_go": report["tests"]["go"],
        "full_vitest": report["tests"]["vitest"],
        "generated": report["generated"],
        "migrations": report["migrations"],
        "ownership": advanced_ownership,
        "peeled_commit": audit_context["peeled_commit"],
        "release": state["release"],
        "schema_version": SUCCESS_REPORT_SCHEMA_VERSION,
        "seams": audit_context["seams"],
        "source_commit": audit_context["source_commit"],
        "status": report["status"],
        "tag_object": audit_context["tag_object"],
        "upstream_tree": audit_context["upstream_tree"],
        "validation_summary_sha256": "",
    }
    success = _sort_report_lists(redact_report_secrets(success))
    success["validation_summary_sha256"] = _success_report_digest(success)
    return _validate_success_report(success, state, worktree)


def _upgrade_test_hook(repository, event, **values):
    hook = getattr(repository, "upgrade_test_hook", None)
    if hook is not None:
        hook(event, **values)


def _recover_incomplete_baseline(repository, storage, state, worktree):
    pending = state.get("baseline")
    if pending is None:
        return
    head = repository.run(
        "rev-parse", "HEAD", cwd=worktree, read_only=True
    ).stdout.strip()
    expected_hashes = pending["configuration_hashes"]
    source_hashes = _source_configuration_hashes(
        repository, state["source_commit"], worktree
    )
    current_paths = _configuration_paths(GitRepository(worktree))
    current_hashes = {
        name: _raw_sha256(path) for name, path in sorted(current_paths.items())
    }
    if head == pending["pre_head"]:
        for name in expected_hashes:
            if current_hashes[name] not in {expected_hashes[name], source_hashes[name]}:
                raise UpgradeBlocked(
                    f"candidate baseline configuration was tampered: {name}"
                )
        allowed = set(BASELINE_CONFIGURATION_PATHS.values()) | set(
            _validation_report_relative_paths(state["release"])
        )
        unexpected = sorted(
            path for _, path in _candidate_status_entries(repository, worktree)
            if path not in allowed
        )
        if unexpected:
            raise UpgradeBlocked(
                "candidate dirt outside incomplete baseline: " + ", ".join(unexpected)
            )
        for name, relative_path in sorted(BASELINE_CONFIGURATION_PATHS.items()):
            content = repository.read_blob_bytes(
                state["source_commit"], relative_path, cwd=worktree
            )
            _atomic_write_configuration(
                worktree, PurePosixPath(relative_path).name, content
            )
            oid = repository.hash_blob_bytes(content, cwd=worktree)
            repository.run_without_hooks(
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{oid},{relative_path}",
                cwd=worktree,
                operation="incomplete baseline index recovery",
            )
    else:
        if current_hashes != expected_hashes:
            raise UpgradeBlocked("committed candidate baseline configuration was tampered")
        parents = repository.run(
            "show", "-s", "--format=%P", "HEAD", cwd=worktree, read_only=True
        ).stdout.split()
        subject = repository.run(
            "show", "-s", "--format=%s", "HEAD", cwd=worktree, read_only=True
        ).stdout.strip()
        if parents != [pending["pre_head"]] or subject != (
            f"chore(upstream): advance baseline to {state['release']}"
        ):
            raise UpgradeBlocked("candidate baseline commit identity is invalid")
        _validate_advanced_baseline(repository, state, worktree)
    state.pop("baseline", None)
    _write_state(storage, state)


def _run_and_persist_candidate_validation(repository, storage, state, worktree):
    state.pop("evidence", None)
    state["phase"] = "validating"
    _write_state(storage, state)
    _recover_incomplete_baseline(repository, storage, state, worktree)
    _require_report_only_candidate_dirt(repository, state, worktree)
    report = validate_upgrade_candidate(repository, storage, state, worktree)
    _upgrade_test_hook(
        repository,
        "before_success_reports",
        state=state,
        worktree=worktree,
    )
    artifacts = write_upgrade_reports(
        worktree,
        state["release"],
        report,
        event_hook=lambda event: _upgrade_test_hook(
            repository, event, state=state, worktree=worktree
        ),
    )
    evidence = _commit_or_reuse_success_reports(
        repository, state, worktree, report, artifacts
    )
    state["evidence"] = evidence
    state["phase"] = "merged"
    _write_state(storage, state)
    return state


def _conflict_binding_sha256(state, paths, customization_sha256):
    payload = {
        "customization_sha256": customization_sha256,
        "paths": paths,
        "peeled_commit": state["peeled_commit"],
        "release": state["release"],
        "source_commit": state["source_commit"],
        "tag_object": state["tag_object"],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def _unmerged_paths(repository, worktree):
    fields = _nul_fields(
        repository.run(
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=U",
            "--",
            cwd=worktree,
            operation="unmerged path inspection",
            read_only=True,
        ).stdout,
        "unmerged path inspection",
    )
    try:
        _validate_repo_paths("conflict paths", fields)
    except ManifestValidationError as error:
        raise UpgradeBlocked(str(error)) from error
    if len(fields) != len(set(fields)):
        raise UpgradeBlocked("unmerged path inspection returned duplicates")
    return sorted(fields)


def _record_merge_conflicts(repository, storage, state, worktree):
    conflict_paths = _unmerged_paths(repository, worktree)
    if not conflict_paths:
        raise UpgradeBlocked("git merge failed without inspectable conflict paths")
    paths = _configuration_paths(GitRepository(worktree))
    manifest = load_json_document(paths["customization"])
    # Validate the manifest shape and its owned annotations before trusting seams.
    validate_manifest(
        manifest,
        [
            path
            for layer in OWNER_LAYER_KEYS
            for path in manifest[layer]["paths"]
        ],
    )
    customization_sha256 = _raw_sha256(paths["customization"])
    source_customization_sha256 = _source_configuration_hashes(
        repository, state["source_commit"], worktree
    )["customization"]
    if customization_sha256 != source_customization_sha256:
        raise UpgradeBlocked("customization changed during upstream merge")
    state["conflicts"] = {
        "binding_sha256": _conflict_binding_sha256(
            state, conflict_paths, customization_sha256
        ),
        "customization_sha256": customization_sha256,
        "paths": conflict_paths,
    }
    state["phase"] = "conflicted"
    _write_state(storage, state)
    unregistered = sorted(set(conflict_paths) - set(manifest["shared_seams"]))
    if unregistered:
        raise UpgradeBlocked(
            "unregistered conflict paths: " + ", ".join(unregistered)
        )
    return state["conflicts"]


def _require_recorded_conflicts(repository, state, worktree):
    conflicts = state.get("conflicts")
    if conflicts is None:
        if state["phase"] == "conflicted":
            raise UpgradeBlocked("conflicted upgrade state requires conflict evidence")
        return []
    expected_binding = _conflict_binding_sha256(
        state, conflicts["paths"], conflicts["customization_sha256"]
    )
    if conflicts["binding_sha256"] != expected_binding:
        raise UpgradeBlocked("upgrade state conflict evidence binding is invalid")
    source_customization_sha256 = _source_configuration_hashes(
        repository, state["source_commit"], worktree
    )["customization"]
    if conflicts["customization_sha256"] != source_customization_sha256:
        raise UpgradeBlocked("upgrade state conflict configuration binding is invalid")
    source_manifest = _source_configuration_documents(
        repository, state, worktree
    )["customization"]
    paths = _configuration_paths(GitRepository(worktree))
    if _raw_sha256(paths["customization"]) != conflicts["customization_sha256"]:
        if state["phase"] != "validating":
            raise UpgradeBlocked("customization changed after merge conflict capture")
        _validate_advanced_baseline(repository, state, worktree)
    validate_manifest(
        source_manifest,
        [
            path
            for layer in OWNER_LAYER_KEYS
            for path in source_manifest[layer]["paths"]
        ],
    )
    unregistered = sorted(
        set(conflicts["paths"]) - set(source_manifest["shared_seams"])
    )
    if unregistered:
        raise UpgradeBlocked(
            "unregistered conflict paths: " + ", ".join(unregistered)
        )
    return conflicts["paths"]


def create_upgrade_candidate(repository, release):
    """Fetch and normally merge one exact release in an isolated worktree."""
    verify_current(repository)
    with _UpgradeStorage(repository) as storage:
        verify_current(repository)
        return _create_upgrade_candidate_locked(repository, release, storage)


def _create_upgrade_candidate_locked(repository, release, storage):
    if storage.state_exists():
        _, active = _load_state(repository, storage)
        if active["release"] != release:
            raise UpgradeBlocked(
                f"active release {active['release']} blocks requested {release}"
            )
        worktree = _validate_resume_state(repository, active)
        if active["phase"] != "merged":
            raise UpgradeBlocked(
                f"candidate {release} is {active['phase']}; resolve and run --continue"
            )
        candidate_head = repository.run(
            "rev-parse",
            "HEAD",
            cwd=worktree,
            operation="candidate HEAD lookup",
            read_only=True,
        ).stdout.strip()
        _require_merged_ancestry(repository, active, candidate_head, worktree)
        _require_clean_candidate(repository, worktree)
        _require_success_report(repository, active, worktree)
        result = dict(active)
        result["_existing_candidate"] = True
        return result
    source_commit, source_branch = _source_identity(repository)
    branch = f"upgrade/{release}"
    branch_ref = f"refs/heads/{branch}"
    worktree = _upgrade_worktree_path(repository, release)
    if repository.run(
        "show-ref", "--verify", branch_ref, check=False
    ).returncode == 0:
        raise UpgradeBlocked("upgrade branch exists without consistent state")
    if worktree.exists():
        raise UpgradeBlocked("upgrade worktree path exists without consistent state")

    internal_ref = f"refs/steadflow-upstream/releases/{release}"
    tag_object, peeled = _fetch_release(repository, release, internal_ref)
    worktree.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    repository.run_without_hooks(
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        source_commit,
        operation="isolated upgrade worktree creation",
    )
    state = {
        "branch": branch,
        "internal_ref": internal_ref,
        "peeled_commit": peeled,
        "phase": "merging",
        "release": release,
        "schema_version": 1,
        "source_branch": source_branch,
        "source_commit": source_commit,
        "tag_object": tag_object,
        "worktree": str(worktree),
    }
    _write_state(storage, state)
    merge = repository.run_without_hooks(
        "merge",
        "--no-ff",
        "--no-edit",
        peeled,
        cwd=worktree,
        check=False,
    )
    if merge.returncode != 0:
        _record_merge_conflicts(repository, storage, state, worktree)
        raise UpgradeBlocked(
            f"merge conflict preserved in {worktree}; resolve and run --continue"
        )
    candidate_head = repository.run(
        "rev-parse",
        "HEAD",
        cwd=worktree,
        operation="candidate HEAD lookup",
        read_only=True,
    ).stdout.strip()
    _require_merged_ancestry(repository, state, candidate_head, worktree)
    _require_clean_candidate(repository, worktree)
    state["phase"] = "validating"
    _write_state(storage, state)
    return _run_and_persist_candidate_validation(
        repository, storage, state, worktree
    )


def _validate_resume_state(
    repository, state, *, source_identity=None, allow_source_advance=False
):
    source_commit, source_branch = source_identity or _source_identity(repository)
    if source_commit != state["source_commit"] and not allow_source_advance:
        raise UpgradeBlocked("source HEAD changed since upgrade state was created")
    if source_branch != state["source_branch"]:
        raise UpgradeBlocked("source branch changed since upgrade state was created")
    worktree = Path(state["worktree"])
    if not worktree.is_dir():
        raise UpgradeBlocked("upgrade worktree is missing")
    actual_root = repository.run(
        "rev-parse",
        "--show-toplevel",
        cwd=worktree,
        operation="upgrade worktree validation",
        read_only=True,
    ).stdout.strip()
    if Path(actual_root).resolve() != worktree.resolve():
        raise UpgradeBlocked("upgrade worktree registration is inconsistent")
    source_common = _git_common_directory(repository)
    candidate_common = _git_common_directory(repository, cwd=worktree)
    if candidate_common != source_common:
        raise UpgradeBlocked("upgrade worktree common directory is inconsistent")
    branch = repository.run(
        "symbolic-ref",
        "--quiet",
        "--short",
        "HEAD",
        cwd=worktree,
        check=False,
        read_only=True,
    )
    if branch.returncode != 0 or branch.stdout.strip() != state["branch"]:
        raise UpgradeBlocked("upgrade worktree branch is inconsistent")
    candidate_head = repository.run(
        "rev-parse",
        "HEAD",
        cwd=worktree,
        operation="candidate HEAD lookup",
        read_only=True,
    ).stdout.strip()
    registrations = _registered_worktrees(repository)
    matches = [
        entry
        for entry in registrations
        if Path(entry.get("worktree", "")).resolve() == worktree.resolve()
    ]
    if len(matches) != 1:
        raise UpgradeBlocked("upgrade worktree is not registered exactly once")
    registration = matches[0]
    if registration.get("HEAD") != candidate_head or registration.get(
        "branch"
    ) != f"refs/heads/{state['branch']}":
        raise UpgradeBlocked("upgrade worktree registered identity is inconsistent")
    tag_object = repository.run(
        "rev-parse",
        state["internal_ref"],
        operation="upgrade internal ref validation",
        read_only=True,
    ).stdout.strip()
    if tag_object != state["tag_object"]:
        raise UpgradeBlocked("upgrade internal release object changed")
    peeled = repository.run(
        "rev-parse",
        f"{state['internal_ref']}^{{commit}}",
        operation="upgrade internal release peel validation",
        read_only=True,
    ).stdout.strip()
    if peeled != state["peeled_commit"]:
        raise UpgradeBlocked("upgrade peeled commit changed")
    remote_object = _remote_release_object(repository, state["release"])
    if remote_object != state["tag_object"]:
        raise UpgradeBlocked("upstream release tag changed during active candidate")
    merge_head = repository.run(
        "rev-parse",
        "--verify",
        "MERGE_HEAD",
        cwd=worktree,
        check=False,
        read_only=True,
    )
    if merge_head.returncode == 0:
        if candidate_head != state["source_commit"]:
            raise UpgradeBlocked("conflicted candidate HEAD does not match source commit")
        if merge_head.stdout.strip() != state["peeled_commit"]:
            raise UpgradeBlocked("MERGE_HEAD does not match upgrade target")
        if state["phase"] == "merged":
            raise UpgradeBlocked("merged candidate still has an active merge")
    elif state["phase"] in {"validating", "merged"}:
        _require_merged_ancestry(repository, state, candidate_head, worktree)
    elif state["phase"] == "merging" and candidate_head != state["source_commit"]:
        _require_merged_ancestry(repository, state, candidate_head, worktree)
    elif state["phase"] == "conflicted" and candidate_head != state["source_commit"]:
        _require_merged_ancestry(repository, state, candidate_head, worktree)
    return worktree


def _registered_worktrees(repository):
    output = repository.run(
        "worktree",
        "list",
        "--porcelain",
        "-z",
        operation="registered worktree inspection",
        read_only=True,
    ).stdout
    entries = []
    for record in output.split("\0\0"):
        fields = {}
        for field in record.split("\0"):
            if not field:
                continue
            key, separator, value = field.partition(" ")
            fields[key] = value if separator else True
        if fields:
            entries.append(fields)
    return entries


def _is_ancestor(repository, ancestor, descendant, cwd):
    completed = repository.run(
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        cwd=cwd,
        check=False,
        read_only=True,
    )
    if completed.returncode not in (0, 1):
        raise UpgradeBlocked("unable to validate candidate ancestry")
    return completed.returncode == 0


def _require_merged_ancestry(repository, state, candidate_head, worktree):
    if not _is_ancestor(
        repository, state["source_commit"], candidate_head, worktree
    ):
        raise UpgradeBlocked("merged candidate lost source ancestry")
    if not _is_ancestor(
        repository, state["peeled_commit"], candidate_head, worktree
    ):
        raise UpgradeBlocked("merged candidate lost target ancestry")


def _require_clean_candidate(repository, worktree):
    status = repository.run(
        "status",
        "--porcelain=v2",
        "-z",
        cwd=worktree,
        operation="candidate cleanliness check",
        read_only=True,
    ).stdout
    if status:
        raise UpgradeBlocked("candidate working tree must be clean")


def resume_upgrade(repository):
    """Validate and continue a preserved merge without choosing resolutions."""
    verify_current(repository, check_ownership=False)
    with _UpgradeStorage(repository) as storage:
        loaded = _load_state(repository, storage)
        if _source_identity(repository)[0] == loaded[1]["source_commit"]:
            verify_current(repository, ownership_commit=loaded[1]["source_commit"])
        elif loaded[1]["phase"] == "merged":
            verify_current(repository)
        else:
            raise UpgradeBlocked("source HEAD changed since upgrade state was created")
        return _resume_upgrade_locked(repository, storage, loaded=loaded)


def _resume_upgrade_locked(repository, storage, *, loaded=None):
    state_path, state = loaded or _load_state(repository, storage)
    source_identity = _source_identity(repository)
    source_advanced = source_identity[0] != state["source_commit"]
    if source_advanced and state["phase"] != "merged":
        raise UpgradeBlocked("source HEAD changed since upgrade state was created")
    worktree = _validate_resume_state(
        repository,
        state,
        source_identity=source_identity,
        allow_source_advance=source_advanced,
    )
    _require_recorded_conflicts(repository, state, worktree)
    candidate_head = repository.run(
        "rev-parse",
        "HEAD",
        cwd=worktree,
        operation="candidate HEAD lookup",
        read_only=True,
    ).stdout.strip()
    if state["phase"] == "merged":
        _require_merged_ancestry(repository, state, candidate_head, worktree)
        _require_clean_candidate(repository, worktree)
        _require_success_report(repository, state, worktree)
        if source_advanced:
            if not _is_ancestor(
                repository, candidate_head, source_identity[0], repository.root
            ):
                raise UpgradeBlocked(
                    "completed candidate is not an ancestor of source HEAD"
                )
            storage.archive_state(state)
            completed = dict(state)
            completed["_completed"] = True
            return completed
        return state

    if state["phase"] == "validating":
        _require_merged_ancestry(repository, state, candidate_head, worktree)
        return _run_and_persist_candidate_validation(
            repository, storage, state, worktree
        )

    unmerged = _unmerged_paths(repository, worktree)
    if unmerged:
        if state["phase"] == "merging" and "conflicts" not in state:
            _record_merge_conflicts(repository, storage, state, worktree)
        raise UpgradeBlocked(
            "unmerged paths remain in candidate: " + ", ".join(sorted(unmerged))
        )

    merge_head = repository.run(
        "rev-parse",
        "--verify",
        "MERGE_HEAD",
        cwd=worktree,
        check=False,
        read_only=True,
    )
    if merge_head.returncode == 0:
        if merge_head.stdout.strip() != state["peeled_commit"]:
            raise UpgradeBlocked("MERGE_HEAD does not match upgrade target")
        continued = repository.run_without_hooks(
            "merge",
            "--continue",
            cwd=worktree,
            check=False,
            extra_environment={"GIT_EDITOR": "true"},
        )
        if continued.returncode != 0:
            raise UpgradeBlocked("git merge --continue failed; candidate preserved")
        candidate_head = repository.run(
            "rev-parse",
            "HEAD",
            cwd=worktree,
            operation="candidate HEAD lookup",
            read_only=True,
        ).stdout.strip()
    elif state["phase"] == "merging" and candidate_head == state["source_commit"]:
        restarted = repository.run_without_hooks(
            "merge",
            "--no-ff",
            "--no-edit",
            state["peeled_commit"],
            cwd=worktree,
            check=False,
        )
        if restarted.returncode != 0:
            _record_merge_conflicts(repository, storage, state, worktree)
            raise UpgradeBlocked(
                f"merge conflict preserved in {worktree}; resolve and run --continue"
            )
        candidate_head = repository.run(
            "rev-parse",
            "HEAD",
            cwd=worktree,
            operation="candidate HEAD lookup",
            read_only=True,
        ).stdout.strip()
    elif not _is_ancestor(
        repository, state["peeled_commit"], candidate_head, worktree
    ):
        raise UpgradeBlocked(
            "candidate has no matching MERGE_HEAD or completed target merge"
        )

    _require_merged_ancestry(repository, state, candidate_head, worktree)
    _require_clean_candidate(repository, worktree)
    state["phase"] = "validating"
    _write_state(storage, state)
    return _run_and_persist_candidate_validation(
        repository, storage, state, worktree
    )


if __name__ == "__main__":
    sys.exit(main())
