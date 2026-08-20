"""Ownership and migration integrity primitives for Steadflow upstream sync."""

import hashlib
import json
import stat
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


class ManifestValidationError(ValueError):
    """Raised when the customization manifest violates ownership rules."""


class MigrationValidationError(ValueError):
    """Raised when migration history is invalid or unsafe."""


def load_json_document(path):
    """Load strict JSON, including JSON-compatible YAML files."""
    with open(path, "r", encoding="utf-8") as document:
        return json.load(document)


def migration_checksums(root):
    """Return sorted runner-compatible SHA-256 checksums for SQL migrations."""
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
