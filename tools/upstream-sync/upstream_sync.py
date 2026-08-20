"""Ownership, migration, and resumable Git primitives for upstream sync."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
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
UPSTREAM_LOCK_KEYS = frozenset(
    ("schema_version", "remote", "repository", "release", "peeled_commit", "tree")
)
RELEASE_PATTERN = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
OBJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


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
        r"(?:https?|ssh|file)://[^\s'\"]+",
        redact_url,
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"(?i)(token|password|passwd|secret|api[_-]?key|key)=([^&\s]+)",
        r"\1=<redacted>",
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
            *arguments,
            **options,
        )


def parse_cli(arguments):
    parser = argparse.ArgumentParser(
        prog="upgrade",
        usage="upgrade vX.Y.Z | upgrade --continue | upgrade --verify-current",
        description="Create or resume an isolated Steadflow upstream candidate.",
    )
    parser.add_argument("release", nargs="?")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--continue", dest="continue_upgrade", action="store_true")
    modes.add_argument("--verify-current", action="store_true")
    options = parser.parse_args(arguments)
    selected = sum(
        (
            options.release is not None,
            options.continue_upgrade,
            options.verify_current,
        )
    )
    if selected != 1:
        parser.error("choose exactly one release, --continue, or --verify-current")
    if options.release is not None and not RELEASE_PATTERN.fullmatch(options.release):
        parser.error("release must match vX.Y.Z without leading zeroes")
    return options


def main(arguments=None):
    options = parse_cli(sys.argv[1:] if arguments is None else arguments)
    try:
        repository = GitRepository.discover(Path.cwd())
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


def migration_checksums(root):
    """Return sorted raw-byte SHA-256 checksums for SQL migrations."""
    root = Path(root)
    migrations = root / "backend" / "migrations"
    inventory = {}
    for path in sorted(migrations.glob("*.sql")):
        relative = path.relative_to(root).as_posix()
        if not stat.S_ISREG(path.lstat().st_mode):
            raise MigrationValidationError(
                f"migration must be regular file: {relative}"
            )
        content = path.read_bytes()
        inventory[relative] = hashlib.sha256(content).hexdigest()
    return inventory


def audit_migrations(root, baseline):
    """Compare current migrations with a checksum baseline."""
    actual_keys = set(baseline)
    if actual_keys != MIGRATION_BASELINE_KEYS:
        missing = ",".join(sorted(MIGRATION_BASELINE_KEYS - actual_keys)) or "none"
        extra = ",".join(sorted(actual_keys - MIGRATION_BASELINE_KEYS)) or "none"
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
    current = migration_checksums(root)
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
    return {
        "unchanged": unchanged,
        "changed": changed,
        "deleted": deleted,
        "added": added,
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
    return report


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


def validate_manifest(manifest, changed_paths):
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
    }
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
    if orphan_seams:
        raise ManifestValidationError(
            "orphan shared_seams: " + ", ".join(orphan_seams)
        )
    if orphan_generated:
        raise ManifestValidationError(
            "orphan generated paths: " + ", ".join(orphan_generated)
        )
    return report


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


def verify_current(repository):
    """Verify the locked fork baseline without writing Git or state."""
    _require_clean_source(repository)
    paths = _configuration_paths(repository)
    manifest = load_json_document(paths["customization"])
    baseline = load_json_document(paths["migrations"])
    lock = load_json_document(paths["lock"])
    owned_paths = []
    for layer in OWNER_LAYER_KEYS:
        value = manifest.get(layer)
        if isinstance(value, dict) and isinstance(value.get("paths"), list):
            owned_paths.extend(value["paths"])
    ownership = validate_manifest(manifest, owned_paths)
    if ownership["owned"] != sorted(owned_paths):
        raise UpgradeBlocked("manifest owner union is not internally exhaustive")
    migrations = validate_migrations(repository.root, baseline)
    _validate_upstream_lock(lock)

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
        "migrations": migrations,
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
        "scope: manifest internal ownership only; current diff coverage is deferred "
        "until Task 7 updates the manifest"
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
    if set(state) != STATE_KEYS:
        missing = ",".join(sorted(STATE_KEYS - set(state))) or "none"
        extra = ",".join(sorted(set(state) - STATE_KEYS)) or "none"
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
    if state["phase"] not in {"merging", "conflicted", "merged"}:
        raise UpgradeBlocked("upgrade state phase is invalid")
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
        state["phase"] = "conflicted"
        _write_state(storage, state)
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
    state["phase"] = "merged"
    _write_state(storage, state)
    return state


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
    elif state["phase"] == "merged":
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
    verify_current(repository)
    with _UpgradeStorage(repository) as storage:
        verify_current(repository)
        return _resume_upgrade_locked(repository, storage)


def _resume_upgrade_locked(repository, storage):
    state_path, state = _load_state(repository, storage)
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

    unmerged = repository.run(
        "diff",
        "--name-only",
        "--diff-filter=U",
        cwd=worktree,
        operation="unmerged path inspection",
        read_only=True,
    ).stdout.splitlines()
    if unmerged:
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
            state["phase"] = "conflicted"
            _write_state(storage, state)
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
    state["phase"] = "merged"
    _write_state(storage, state)
    return state


if __name__ == "__main__":
    sys.exit(main())
