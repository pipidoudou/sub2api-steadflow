import atexit
import contextlib
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


# Normalize Apple Python's external cache prefix so the black-box regression
# exercises the ordinary checkout-local bytecode behavior seen in CI/review.
sys.pycache_prefix = None
MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parents[1]


def _darwin_process_start_ns():
    class ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("pbi_flags", ctypes.c_uint32),
            ("pbi_status", ctypes.c_uint32),
            ("pbi_xstatus", ctypes.c_uint32),
            ("pbi_pid", ctypes.c_uint32),
            ("pbi_ppid", ctypes.c_uint32),
            ("pbi_uid", ctypes.c_uint32),
            ("pbi_gid", ctypes.c_uint32),
            ("pbi_ruid", ctypes.c_uint32),
            ("pbi_rgid", ctypes.c_uint32),
            ("pbi_svuid", ctypes.c_uint32),
            ("pbi_svgid", ctypes.c_uint32),
            ("rfu_1", ctypes.c_uint32),
            ("pbi_comm", ctypes.c_char * 16),
            ("pbi_name", ctypes.c_char * 32),
            ("pbi_nfiles", ctypes.c_uint32),
            ("pbi_pgid", ctypes.c_uint32),
            ("pbi_pjobc", ctypes.c_uint32),
            ("e_tdev", ctypes.c_uint32),
            ("e_tpgid", ctypes.c_uint32),
            ("pbi_nice", ctypes.c_int32),
            ("pbi_start_tvsec", ctypes.c_uint64),
            ("pbi_start_tvusec", ctypes.c_uint64),
        ]

    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = (
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    )
    proc_pidinfo.restype = ctypes.c_int
    info = ProcBsdInfo()
    size = ctypes.sizeof(info)
    if proc_pidinfo(os.getpid(), 3, 0, ctypes.byref(info), size) != size:
        return None
    return info.pbi_start_tvsec * 1_000_000_000 + info.pbi_start_tvusec * 1_000


def _linux_process_start_upper_bound_ns():
    stat_fields = Path("/proc/self/stat").read_text(encoding="ascii").split()
    start_ticks = int(stat_fields[21])
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    boottime = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    realtime_after = time.time_ns()
    realtime_minus_boottime_upper = realtime_after - boottime
    return realtime_minus_boottime_upper + (
        start_ticks * 1_000_000_000 // ticks_per_second
    )


def _process_start_time_ns():
    try:
        if sys.platform == "darwin":
            return _darwin_process_start_ns()
        if sys.platform.startswith("linux"):
            return _linux_process_start_upper_bound_ns()
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _cache_was_created_by_current_process(cache_path):
    process_start_ns = _process_start_time_ns()
    if process_start_ns is None:
        return False
    cache_stat = cache_path.stat()
    if sys.platform == "darwin":
        created_ns = int(cache_stat.st_birthtime * 1_000_000_000)
    elif sys.platform.startswith("linux"):
        created_ns = cache_stat.st_ctime_ns
    else:
        return False
    return created_ns > process_start_ns


_CACHED_TEST_MODULE = Path(globals().get("__cached__", ""))
_CACHED_TEST_MODULE_CREATED_BY_SUITE = False
if _CACHED_TEST_MODULE.name and _CACHED_TEST_MODULE.exists():
    _CACHED_TEST_MODULE_CREATED_BY_SUITE = (
        _CACHED_TEST_MODULE.parent.resolve() == (MODULE_DIR / "__pycache__").resolve()
        and _cache_was_created_by_current_process(_CACHED_TEST_MODULE)
    )
sys.dont_write_bytecode = True
sys.path.insert(0, str(MODULE_DIR))

from upstream_sync import (
    _git_change_summary,
    _summary_paths,
    audit_migrations,
    compare_test_failures,
    classify_migration_sql,
    create_upgrade_candidate,
    EVIDENCE_KEYS,
    GitRepository,
    ManifestValidationError,
    MigrationValidationError,
    OWNER_LAYER_KEYS,
    UpgradeBlocked,
    load_json_document,
    migration_checksums,
    parse_go_test_jsonl,
    parse_vitest_json,
    redact_report_secrets,
    resume_upgrade,
    run_critical_commands,
    run_critical_suite,
    validate_known_failures,
    validate_manifest,
    validate_migrations,
    write_upgrade_reports,
)


def _cleanup_suite_cache(cache_path, *, created_by_suite):
    """Remove only bytecode proven to have been created by this suite run."""
    cache_path = Path(cache_path)
    if not created_by_suite:
        return
    with contextlib.suppress(FileNotFoundError):
        cache_path.unlink()
    cache_directory = cache_path.parent
    with contextlib.suppress(FileNotFoundError, OSError):
        cache_directory.rmdir()


atexit.register(
    _cleanup_suite_cache,
    _CACHED_TEST_MODULE,
    created_by_suite=_CACHED_TEST_MODULE_CREATED_BY_SUITE,
)


PROTECTED_REPO_ROOT = Path(
    os.environ.get("STEADFLOW_PROTECTED_REPO_ROOT", REPO_ROOT)
).resolve()
_PROTECTED_REPO_BEFORE = None


def protected_repo_snapshot(root=PROTECTED_REPO_ROOT):
    environment = {
        "PATH": os.environ["PATH"],
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }

    def git_bytes(*arguments):
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            env=environment,
            check=True,
            capture_output=True,
        ).stdout

    common = Path(git_bytes("rev-parse", "--git-common-dir").decode().strip())
    if not common.is_absolute():
        common = root / common
    config_path = common.resolve() / "config"
    state_path = common.resolve() / "steadflow-upstream-sync" / "state.json"
    return {
        "config": config_path.read_bytes(),
        "head": git_bytes("rev-parse", "HEAD"),
        "index": (common.resolve() / "index").read_bytes(),
        "refs": git_bytes(
            "for-each-ref", "--format=%(refname) %(objectname)"
        ),
        "remotes": git_bytes("remote", "-v"),
        "status": git_bytes("status", "--porcelain=v2", "-z"),
        "steadflow_state": state_path.read_bytes() if state_path.is_file() else None,
        "worktrees": git_bytes("worktree", "list", "--porcelain"),
    }


def setUpModule():
    global _PROTECTED_REPO_BEFORE
    _PROTECTED_REPO_BEFORE = protected_repo_snapshot()


def tearDownModule():
    after = protected_repo_snapshot()
    changed = sorted(
        key for key, value in _PROTECTED_REPO_BEFORE.items() if after[key] != value
    )
    if changed:
        raise AssertionError(
            "protected repository changed during test suite: " + ", ".join(changed)
        )


class UpgradeWrapperTests(unittest.TestCase):
    def run_wrapper(self, *arguments, cwd=None):
        return subprocess.run(
            [str(MODULE_DIR / "upgrade"), *arguments],
            cwd=cwd or REPO_ROOT,
            env={
                "PATH": os.environ["PATH"],
                "LANG": "C",
                "LC_ALL": "C",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
            },
            capture_output=True,
            text=True,
        )

    def test_wrapper_is_strict_executable_and_delegates_to_sibling_module(self):
        wrapper = MODULE_DIR / "upgrade"

        self.assertTrue(wrapper.is_file(), "upgrade wrapper must exist")
        self.assertEqual(
            wrapper.read_text(encoding="utf-8"),
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'exec python3 "$SCRIPT_DIR/upstream_sync.py" "$@"\n',
        )
        self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)

    def test_help_documents_all_four_modes(self):
        completed = self.run_wrapper("--help")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("upgrade vX.Y.Z", completed.stdout)
        self.assertIn("upgrade --continue", completed.stdout)
        self.assertIn("upgrade --verify-current", completed.stdout)
        self.assertIn("upgrade --run-critical", completed.stdout)

    def test_invalid_release_and_argument_shapes_fail_before_git_access(self):
        invalid_argv = (
            (),
            ("",),
            ("v1.2",),
            ("v1.2.3-rc1",),
            ("v01.2.3",),
            ("v1.02.3",),
            ("v1.2.03",),
            ("v1.2.3/../../escape",),
            (" v1.2.3",),
            ("v1.2.3 ",),
            ("v1.2.3", "extra"),
            ("--continue", "v1.2.3"),
            ("--verify-current", "v1.2.3"),
            ("--continue", "--verify-current"),
            ("--run-critical", "--verify-current"),
        )
        with tempfile.TemporaryDirectory() as directory:
            for arguments in invalid_argv:
                with self.subTest(arguments=arguments):
                    completed = self.run_wrapper(*arguments, cwd=directory)

                    self.assertNotEqual(completed.returncode, 0)
                    self.assertNotIn("not a git repository", completed.stderr)


def manifest_with_paths(**layers):
    manifest = {
        "schema_version": 1,
        "data": {"paths": []},
        "distributor": {"paths": []},
        "branding_settings": {"paths": []},
        "thesis_public": {"paths": []},
        "integration_adapter": {"paths": []},
        "shared_seams": [],
        "generated": {"commands": [], "paths": []},
        "critical_commands": [],
    }
    for name, paths in layers.items():
        manifest[name]["paths"] = paths
    return manifest


class OwnershipManifestTests(unittest.TestCase):
    def test_owner_layer_keys_are_stable(self):
        self.assertEqual(
            OWNER_LAYER_KEYS,
            (
                "data",
                "distributor",
                "branding_settings",
                "thesis_public",
                "integration_adapter",
            ),
        )

    def test_exactly_one_layer_ownership_passes(self):
        manifest = manifest_with_paths(data=["data/README.md"])

        report = validate_manifest(manifest, ["data/README.md"])

        self.assertEqual(report["owned"], ["data/README.md"])
        self.assertEqual(report["unowned"], [])
        self.assertEqual(report["multiply_owned"], {})

    def test_ownership_report_paths_are_deterministically_sorted(self):
        manifest = manifest_with_paths(data=["a/path", "z/path"])

        report = validate_manifest(manifest, ["z/path", "a/path"])

        self.assertEqual(report["owned"], ["a/path", "z/path"])

    def test_unowned_diff_path_blocks_validation(self):
        manifest = manifest_with_paths(data=["data/README.md"])

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"unowned paths: backend/missing\.go",
        ):
            validate_manifest(manifest, ["backend/missing.go"])

    def test_registered_path_absent_from_real_diff_blocks_exact_ownership(self):
        manifest = manifest_with_paths(data=["data/README.md", "stale/path.txt"])

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"registered paths absent from fork diff: stale/path\.txt",
        ):
            validate_manifest(manifest, ["data/README.md"], require_exact=True)

    def test_path_in_two_owner_layers_blocks_validation(self):
        manifest = manifest_with_paths(
            data=["shared/file.go"],
            distributor=["shared/file.go"],
        )

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"multiply owned paths: shared/file\.go \[data, distributor\]",
        ):
            validate_manifest(manifest, ["shared/file.go"])

    def test_duplicate_owner_path_blocks_even_when_not_in_diff(self):
        manifest = manifest_with_paths(
            data=["shared/file.go"],
            distributor=["shared/file.go"],
            integration_adapter=["changed/file.go"],
        )

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"multiply owned paths: shared/file\.go \[data, distributor\]",
        ):
            validate_manifest(manifest, ["changed/file.go"])

    def test_shared_seams_and_generated_paths_can_overlap_unique_owner(self):
        manifest = manifest_with_paths(
            integration_adapter=[
                "backend/cmd/server/wire_gen.go",
                "backend/internal/server/router.go",
            ]
        )
        manifest["shared_seams"] = ["backend/internal/server/router.go"]
        manifest["generated"]["paths"] = ["backend/cmd/server/wire_gen.go"]

        report = validate_manifest(
            manifest,
            [
                "backend/internal/server/router.go",
                "backend/cmd/server/wire_gen.go",
            ],
        )

        self.assertEqual(
            report["shared_seams"], ["backend/internal/server/router.go"]
        )
        self.assertEqual(
            report["generated"], ["backend/cmd/server/wire_gen.go"]
        )

    def test_orphan_shared_seam_blocks_validation(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        manifest["shared_seams"] = ["backend/internal/server/router.go"]

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"orphan shared_seams: backend/internal/server/router\.go",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_orphan_generated_path_blocks_validation(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        manifest["generated"]["paths"] = ["backend/cmd/server/wire_gen.go"]

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"orphan generated paths: backend/cmd/server/wire_gen\.go",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_manifest_requires_exact_top_level_keys(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        del manifest["generated"]
        manifest["unexpected"] = {}

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"top-level keys missing=generated extra=unexpected",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_manifest_requires_schema_version_one(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        manifest["schema_version"] = 2

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"schema_version must be integer 1",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_owner_layer_paths_must_be_sorted(self):
        manifest = manifest_with_paths(data=["z/path", "a/path"])

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"data\.paths must be sorted",
        ):
            validate_manifest(manifest, ["z/path", "a/path"])

    def test_owner_layer_paths_must_be_unique(self):
        manifest = manifest_with_paths(data=["data/README.md", "data/README.md"])

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"data\.paths must be unique",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_manifest_paths_must_be_repository_relative_posix(self):
        cases = (
            ("data.paths", "/absolute/path"),
            ("shared_seams", r"backend\router.go"),
            ("generated.paths", "../escape.sql"),
        )
        for field, invalid_path in cases:
            with self.subTest(field=field):
                escaped_field = field.replace(".", r"\.")
                manifest = manifest_with_paths(data=["data/README.md"])
                if field == "data.paths":
                    manifest["data"]["paths"] = [invalid_path]
                elif field == "shared_seams":
                    manifest["shared_seams"] = [invalid_path]
                else:
                    manifest["generated"]["paths"] = [invalid_path]

                with self.assertRaisesRegex(
                    ManifestValidationError,
                    rf"{escaped_field} contains invalid repository-relative POSIX path",
                ):
                    validate_manifest(manifest, ["data/README.md"])

    def test_annotation_paths_must_be_sorted_and_unique(self):
        cases = (
            ("shared_seams", ["z/path", "a/path"]),
            ("generated.paths", ["a/path", "a/path"]),
        )
        for field, values in cases:
            with self.subTest(field=field):
                escaped_field = field.replace(".", r"\.")
                manifest = manifest_with_paths(
                    integration_adapter=["a/path", "z/path"]
                )
                if field == "shared_seams":
                    manifest["shared_seams"] = values
                else:
                    manifest["generated"]["paths"] = values

                with self.assertRaisesRegex(
                    ManifestValidationError,
                    rf"{escaped_field} must be sorted and unique",
                ):
                    validate_manifest(manifest, ["a/path", "z/path"])

    def test_commands_require_structured_argv(self):
        for field in ("generated.commands", "critical_commands"):
            with self.subTest(field=field):
                manifest = manifest_with_paths(data=["data/README.md"])
                commands = [{"name": "unsafe", "argv": "go test ./..."}]
                if field == "generated.commands":
                    manifest["generated"]["commands"] = commands
                else:
                    manifest["critical_commands"] = commands

                escaped_field = field.replace(".", r"\.")
                with self.assertRaisesRegex(
                    ManifestValidationError,
                    rf"{escaped_field}\[0\]\.argv must be a non-empty list of strings",
                ):
                    validate_manifest(manifest, ["data/README.md"])

    def test_command_names_must_be_sorted_and_unique(self):
        cases = (
            ("generated.commands", ["ent", "ent"]),
            ("critical_commands", ["z-test", "a-test"]),
        )
        for field, names in cases:
            with self.subTest(field=field):
                manifest = manifest_with_paths(data=["data/README.md"])
                commands = [
                    {"name": name, "argv": ["tool", name]} for name in names
                ]
                if field == "generated.commands":
                    manifest["generated"]["commands"] = commands
                else:
                    manifest["critical_commands"] = commands

                escaped_field = field.replace(".", r"\.")
                with self.assertRaisesRegex(
                    ManifestValidationError,
                    rf"{escaped_field} names must be sorted and unique",
                ):
                    validate_manifest(manifest, ["data/README.md"])

    def test_manifest_nested_shapes_raise_explicit_validation_errors(self):
        cases = (
            (
                "layer-object",
                lambda manifest: manifest.__setitem__("data", []),
                r"data must be an object containing a paths list",
            ),
            (
                "layer-paths-list",
                lambda manifest: manifest["data"].__setitem__(
                    "paths", "data/README.md"
                ),
                r"data must be an object containing a paths list",
            ),
            (
                "shared-list",
                lambda manifest: manifest.__setitem__("shared_seams", "router.go"),
                r"shared_seams must be a list",
            ),
            (
                "generated-shape",
                lambda manifest: manifest.__setitem__("generated", {"paths": []}),
                r"generated must be an object containing commands and paths lists",
            ),
            (
                "critical-list",
                lambda manifest: manifest.__setitem__(
                    "critical_commands", {}
                ),
                r"critical_commands must be a list",
            ),
        )
        for label, mutate, message in cases:
            with self.subTest(label=label):
                manifest = manifest_with_paths(data=["data/README.md"])
                mutate(manifest)

                with self.assertRaisesRegex(ManifestValidationError, message):
                    validate_manifest(manifest, ["data/README.md"])

    def test_owner_layers_reject_nested_extra_keys(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        manifest["data"]["note"] = "not part of the schema"

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"data keys must be paths",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_generated_rejects_nested_extra_keys(self):
        manifest = manifest_with_paths(data=["data/README.md"])
        manifest["generated"]["shell"] = True

        with self.assertRaisesRegex(
            ManifestValidationError,
            r"generated keys must be commands and paths",
        ):
            validate_manifest(manifest, ["data/README.md"])

    def test_command_objects_reject_invalid_name_cwd_and_extra_keys(self):
        cases = (
            (
                "name",
                {"name": "", "argv": ["go", "test"]},
                r"critical_commands\[0\]\.name must be a non-empty string",
            ),
            (
                "cwd",
                {
                    "name": "test",
                    "argv": ["go", "test"],
                    "cwd": "/absolute/path",
                },
                r"critical_commands\[0\]\.cwd contains invalid repository-relative POSIX path",
            ),
            (
                "extra-key",
                {"name": "test", "argv": ["go", "test"], "shell": True},
                r"critical_commands\[0\] keys must be name, argv, and optional cwd",
            ),
        )
        for label, command, message in cases:
            with self.subTest(label=label):
                manifest = manifest_with_paths(data=["data/README.md"])
                manifest["critical_commands"] = [command]

                with self.assertRaisesRegex(ManifestValidationError, message):
                    validate_manifest(manifest, ["data/README.md"])


class JsonDocumentTests(unittest.TestCase):
    def test_load_json_document_accepts_json_compatible_yaml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customization.yml"
            path.write_text('{"schema_version": 1}\n', encoding="utf-8")

            self.assertEqual(load_json_document(path), {"schema_version": 1})

    def test_load_json_document_rejects_duplicate_keys_at_any_depth(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "customization.yml"
            path.write_text(
                '{"generated": {"paths": [], "paths": ["other"]}}\n',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, r"duplicate JSON key: paths"):
                load_json_document(path)

    def test_load_json_document_requires_object_root(self):
        for label, document in (("array", "[]\n"), ("null", "null\n")):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "document.json"
                path.write_text(document, encoding="utf-8")

                with self.assertRaisesRegex(
                    ValueError,
                    r"JSON document root must be an object",
                ):
                    load_json_document(path)


class MigrationIntegrityTests(unittest.TestCase):
    def test_migration_inventory_is_sql_only_repo_relative_and_sorted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "002_beta.sql").write_text(" beta \n", encoding="utf-8")
            (migrations / "001_alpha.sql").write_text("alpha\n", encoding="utf-8")
            (migrations / "README.md").write_text("not sql\n", encoding="utf-8")
            nested = migrations / "nested"
            nested.mkdir()
            (nested / "003_nested.sql").write_text("nested\n", encoding="utf-8")

            inventory = migration_checksums(root)

        expected = {
            "backend/migrations/001_alpha.sql": hashlib.sha256(
                b"alpha\n"
            ).hexdigest(),
            "backend/migrations/002_beta.sql": hashlib.sha256(
                b" beta \n"
            ).hexdigest(),
        }
        self.assertEqual(inventory, expected)
        self.assertEqual(list(inventory), sorted(inventory))

    def test_migration_inventory_hashes_arbitrary_raw_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            raw_sql = b"\xff\x00 SELECT 1;\r\n\xfe"
            (migrations / "001_raw.sql").write_bytes(raw_sql)

            inventory = migration_checksums(root)

        self.assertEqual(
            inventory,
            {
                "backend/migrations/001_raw.sql": hashlib.sha256(
                    raw_sql
                ).hexdigest()
            },
        )

    def test_migration_backend_replacement_cannot_redirect_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            backend = root / "backend"
            migrations = backend / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_inside.sql").write_bytes(b"SELECT 1;\n")
            outside = Path(directory) / "outside"
            (outside / "migrations").mkdir(parents=True)
            (outside / "migrations" / "999_outside.sql").write_bytes(b"OUTSIDE")
            saved = root / "backend-saved"
            real_open = os.open
            outside_reads = 0
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal outside_reads, swapped
                if path == "migrations" and dir_fd is not None and not swapped:
                    backend.rename(saved)
                    backend.symlink_to(outside, target_is_directory=True)
                    swapped = True
                if path == "999_outside.sql":
                    outside_reads += 1
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch("upstream_sync.os.open", side_effect=racing_open):
                with self.assertRaises(MigrationValidationError):
                    migration_checksums(root)

            self.assertTrue(swapped)
            self.assertEqual(outside_reads, 0)

    def test_migration_directory_replacement_cannot_redirect_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_inside.sql").write_bytes(b"SELECT 1;\n")
            outside = Path(directory) / "outside"
            outside.mkdir()
            (outside / "999_outside.sql").write_bytes(b"OUTSIDE")
            saved = root / "backend" / "migrations-saved"
            real_open = os.open
            outside_reads = 0
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal outside_reads, swapped
                if path == "001_inside.sql" and dir_fd is not None and not swapped:
                    migrations.rename(saved)
                    migrations.symlink_to(outside, target_is_directory=True)
                    swapped = True
                if path == "999_outside.sql":
                    outside_reads += 1
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch("upstream_sync.os.open", side_effect=racing_open):
                with self.assertRaises(MigrationValidationError):
                    migration_checksums(root)

            self.assertTrue(swapped)
            self.assertEqual(outside_reads, 0)

    def test_migration_file_inode_swap_cannot_hide_malicious_current_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            migration = migrations / "001_initial.sql"
            malicious = b"DROP TABLE users;\n"
            baseline = b"SELECT 1;\n"
            migration.write_bytes(malicious)
            hidden = migrations / "001_initial.hidden"
            real_open = os.open
            real_read = os.read
            opened_fd = None
            swapped = False
            restored = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal opened_fd, swapped
                if path == "001_initial.sql" and dir_fd is not None and not swapped:
                    migration.rename(hidden)
                    migration.write_bytes(baseline)
                    swapped = True
                if dir_fd is None:
                    descriptor = real_open(path, flags, mode)
                else:
                    descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
                if path == "001_initial.sql" and swapped and opened_fd is None:
                    opened_fd = descriptor
                return descriptor

            def racing_read(descriptor, size):
                nonlocal restored
                chunk = real_read(descriptor, size)
                if descriptor == opened_fd and not chunk and not restored:
                    migration.unlink()
                    hidden.rename(migration)
                    restored = True
                return chunk

            with mock.patch("upstream_sync.os.open", side_effect=racing_open), mock.patch(
                "upstream_sync.os.read", side_effect=racing_read
            ):
                with self.assertRaises(MigrationValidationError):
                    migration_checksums(root)

            self.assertTrue(swapped)
            self.assertTrue(restored)
            self.assertEqual(migration.read_bytes(), malicious)

    def test_raw_whitespace_and_line_ending_changes_block_validation(self):
        cases = {
            "trailing-newline": (b"SELECT 1;", b"SELECT 1;\n"),
            "trailing-space": (b"SELECT 1;", b"SELECT 1; "),
            "leading-space": (b"SELECT 1;", b" SELECT 1;"),
            "lf-to-crlf": (b"SELECT 1;\n", b"SELECT 1;\r\n"),
        }
        for label, (original, changed_bytes) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                migrations = root / "backend" / "migrations"
                migrations.mkdir(parents=True)
                migration = migrations / "001_initial.sql"
                migration.write_bytes(original)
                baseline = {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "migrations": {
                        "backend/migrations/001_initial.sql": hashlib.sha256(
                            original
                        ).hexdigest()
                    },
                }
                migration.write_bytes(changed_bytes)

                report = audit_migrations(root, baseline)

                self.assertEqual(
                    report["changed"], ["backend/migrations/001_initial.sql"]
                )
                with self.assertRaisesRegex(
                    MigrationValidationError,
                    r"changed historical migrations: backend/migrations/001_initial\.sql",
                ):
                    validate_migrations(root, baseline)

    def test_sql_migration_must_be_a_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            target = root / "target.sql"
            target.write_text("SELECT 1;\n", encoding="utf-8")
            (migrations / "001_link.sql").symlink_to(target)

            with self.assertRaisesRegex(
                MigrationValidationError,
                r"migration must be regular file: backend/migrations/001_link\.sql",
            ):
                migration_checksums(root)

    def test_unchanged_historical_sql_passes_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_initial.sql").write_text(
                "SELECT 1;\n", encoding="utf-8"
            )
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }

            report = audit_migrations(root, baseline)

        self.assertEqual(
            report,
            {
                "unchanged": ["backend/migrations/001_initial.sql"],
                "changed": [],
                "deleted": [],
                "added": [],
                "added_risk": {},
            },
        )

    def test_changed_historical_sql_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            migration = migrations / "001_initial.sql"
            migration.write_text("SELECT 1;\n", encoding="utf-8")
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            migration.write_text("SELECT 2;\n", encoding="utf-8")

            report = audit_migrations(root, baseline)

        self.assertEqual(report["changed"], ["backend/migrations/001_initial.sql"])

    def test_deleted_historical_sql_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            migration = migrations / "001_initial.sql"
            migration.write_text("SELECT 1;\n", encoding="utf-8")
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            migration.unlink()

            report = audit_migrations(root, baseline)

        self.assertEqual(report["deleted"], ["backend/migrations/001_initial.sql"])

    def test_new_sql_migrations_are_sorted_and_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_initial.sql").write_text(
                "SELECT 1;\n", encoding="utf-8"
            )
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            (migrations / "003_zeta.sql").write_text("SELECT 3;\n", encoding="utf-8")
            (migrations / "002_alpha.sql").write_text("SELECT 2;\n", encoding="utf-8")

            report = audit_migrations(root, baseline)

        self.assertEqual(
            report["added"],
            [
                "backend/migrations/002_alpha.sql",
                "backend/migrations/003_zeta.sql",
            ],
        )

    def test_new_sql_migration_does_not_block_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_initial.sql").write_text(
                "SELECT 1;\n", encoding="utf-8"
            )
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            (migrations / "002_added.sql").write_text(
                "SELECT 2;\n", encoding="utf-8"
            )

            report = validate_migrations(root, baseline)

        self.assertEqual(report["added"], ["backend/migrations/002_added.sql"])

    def test_changed_historical_sql_blocks_validation_with_sorted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            alpha = migrations / "001_alpha.sql"
            zeta = migrations / "002_zeta.sql"
            alpha.write_text("SELECT 1;\n", encoding="utf-8")
            zeta.write_text("SELECT 2;\n", encoding="utf-8")
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            zeta.write_text("SELECT 20;\n", encoding="utf-8")
            alpha.write_text("SELECT 10;\n", encoding="utf-8")

            with self.assertRaises(MigrationValidationError) as raised:
                validate_migrations(root, baseline)

        self.assertEqual(
            str(raised.exception),
            "changed historical migrations: "
            "backend/migrations/001_alpha.sql, backend/migrations/002_zeta.sql",
        )

    def test_deleted_historical_sql_blocks_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            migration = migrations / "001_initial.sql"
            migration.write_text("SELECT 1;\n", encoding="utf-8")
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "migrations": migration_checksums(root),
            }
            migration.unlink()

            with self.assertRaisesRegex(
                MigrationValidationError,
                r"deleted historical migrations: backend/migrations/001_initial\.sql",
            ):
                validate_migrations(root, baseline)

    def test_migration_baseline_requires_sha256_algorithm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend" / "migrations").mkdir(parents=True)
            baseline = {
                "schema_version": 1,
                "algorithm": "md5",
                "migrations": {},
            }

            with self.assertRaisesRegex(
                MigrationValidationError,
                r"migration baseline algorithm must be sha256",
            ):
                audit_migrations(root, baseline)

    def test_migration_baseline_requires_exact_top_level_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend" / "migrations").mkdir(parents=True)
            baseline = {
                "schema_version": 1,
                "algorithm": "sha256",
                "unexpected": {},
            }

            with self.assertRaisesRegex(
                MigrationValidationError,
                r"migration baseline keys missing=migrations extra=unexpected",
            ):
                audit_migrations(root, baseline)

    def test_migration_baseline_requires_schema_version_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "backend" / "migrations").mkdir(parents=True)
            baseline = {
                "schema_version": 2,
                "algorithm": "sha256",
                "migrations": {},
            }

            with self.assertRaisesRegex(
                MigrationValidationError,
                r"migration baseline schema_version must be integer 1",
            ):
                audit_migrations(root, baseline)

    def test_migration_baseline_requires_migrations_object(self):
        for label, migrations_value in (("null", None), ("list", [])):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "backend" / "migrations").mkdir(parents=True)
                baseline = {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "migrations": migrations_value,
                }

                with self.assertRaisesRegex(
                    MigrationValidationError,
                    r"migration baseline migrations must be an object",
                ):
                    audit_migrations(root, baseline)

    def test_migration_baseline_mapping_is_sorted_sql_with_lowercase_sha256(self):
        cases = (
            (
                "unsorted",
                {
                    "backend/migrations/002_zeta.sql": "0" * 64,
                    "backend/migrations/001_alpha.sql": "1" * 64,
                },
                r"migration baseline migrations must be sorted by path",
            ),
            (
                "non-sql",
                {"backend/migrations/not_sql.go": "0" * 64},
                r"invalid migration baseline path: backend/migrations/not_sql\.go",
            ),
            (
                "invalid-checksum",
                {"backend/migrations/001_initial.sql": "A" * 64},
                r"invalid sha256 for migration: backend/migrations/001_initial\.sql",
            ),
        )
        for label, mapping, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "backend" / "migrations").mkdir(parents=True)
                baseline = {
                    "schema_version": 1,
                    "algorithm": "sha256",
                    "migrations": mapping,
                }

                with self.assertRaisesRegex(MigrationValidationError, message):
                    audit_migrations(root, baseline)


class RepositoryBaselineTests(unittest.TestCase):
    go_discovery_root = REPO_ROOT

    @classmethod
    def setUpClass(cls):
        cls.git_fixture_directory = tempfile.TemporaryDirectory()
        cls.go_discovery_root = (
            Path(cls.git_fixture_directory.name) / "repository-clone"
        )
        environment = dict(os.environ)
        environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-local",
                str(REPO_ROOT),
                str(cls.go_discovery_root),
            ],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
        for key, value in (
            ("user.name", "Steadflow Test"),
            ("user.email", "steadflow-test@example.invalid"),
        ):
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(cls.go_discovery_root),
                    "config",
                    "--local",
                    key,
                    value,
                ],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

    @classmethod
    def tearDownClass(cls):
        cls.git_fixture_directory.cleanup()

    def load_deterministic_json(self, relative_path, root=REPO_ROOT):
        path = root / relative_path
        text = path.read_text(encoding="utf-8")
        document = json.loads(text)
        self.assertEqual(
            text,
            json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        )
        return document

    def git(self, *arguments, root=REPO_ROOT):
        environment = dict(os.environ)
        environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip()

    def test_customization_manifest_matches_certified_inventory_and_tooling_delta(self):
        manifest = self.load_deterministic_json(".steadflow/customization.yml")
        expected_zlists = {
            "data": (
                2,
                "8ba3da990f920c645f8c15269674563784f557c28079996c42cc464e8939ab83",
            ),
            "distributor": (
                45,
                "93627aae8c3994c8c4c1f47b1c5c3857dfeca64deb0268d2abf1b4e1695aee8b",
            ),
            "branding_settings": (
                28,
                "d8b17dec7473331bb561d72a9e492b7ff5705eab1abcd5ce7e2b20db428acf00",
            ),
            "thesis_public": (
                26,
                "464bd23f4c2b3b976dff7caab98d38fd05e21914df193e9921e356cae184235c",
            ),
            "integration_adapter": (
                422,
                "24aa0bec5a2c36dffff345e089ac0f162b4e3855d03e78263e8b81ea30292188",
            ),
        }
        for layer in OWNER_LAYER_KEYS:
            paths = manifest[layer]["paths"]
            expected_count, expected_digest = expected_zlists[layer]
            nul_stream = ("\0".join(paths) + "\0").encode("utf-8")
            self.assertEqual(len(paths), expected_count)
            self.assertEqual(hashlib.sha256(nul_stream).hexdigest(), expected_digest)

        certified_paths = [
            path for layer in OWNER_LAYER_KEYS for path in manifest[layer]["paths"]
        ]
        self.assertEqual(len(certified_paths), 523)
        self.assertEqual(len(set(certified_paths)), 523)
        report = validate_manifest(manifest, certified_paths)
        self.assertEqual(report["owned"], sorted(certified_paths))

        self.assertEqual(
            manifest["shared_seams"],
            [
                ".github/audit-exceptions.yml",
                ".github/workflows/backend-ci.yml",
                ".github/workflows/release.yml",
                ".github/workflows/security-scan.yml",
                ".gitignore",
                "DEV_GUIDE.md",
                "README.md",
                "README_CN.md",
                "README_JA.md",
                "backend/cmd/server/VERSION",
                "backend/cmd/server/wire_gen.go",
                "backend/ent/group.go",
                "backend/ent/schema/group.go",
                "backend/go.sum",
                "backend/internal/handler/handler.go",
                "backend/internal/handler/wire.go",
                "backend/internal/server/router.go",
                "backend/internal/service/setting_parse.go",
                "backend/internal/service/wire.go",
                "deploy/.env.example",
                "deploy/DOCKER.md",
                "deploy/Dockerfile",
                "deploy/README.md",
                "deploy/config.example.yaml",
                "deploy/docker-compose.dev.yml",
                "deploy/docker-compose.local.yml",
                "deploy/docker-compose.standalone.yml",
                "deploy/docker-compose.yml",
                "docs/COMPOSITE_GROUPS.md",
                "frontend/src/router/index.ts",
            ],
        )
        generated_from_tree = []
        for relative in certified_paths:
            path = REPO_ROOT / relative
            if not path.is_file():
                continue
            with path.open("rb") as source:
                first_line = source.readline()
            if first_line.startswith(
                (b"// Code generated by ent", b"// Code generated by Wire")
            ):
                generated_from_tree.append(relative)
        self.assertEqual(len(generated_from_tree), 30)
        self.assertEqual(manifest["generated"]["paths"], sorted(generated_from_tree))
        self.assertEqual(
            manifest["generated"]["commands"],
            [
                {
                    "argv": ["go", "generate", "./ent"],
                    "cwd": "backend",
                    "name": "backend_ent",
                },
                {
                    "argv": ["go", "generate", "./cmd/server"],
                    "cwd": "backend",
                    "name": "backend_wire",
                },
            ],
        )

    def test_certified_shared_seams_are_owned_annotations(self):
        manifest = self.load_deterministic_json(".steadflow/customization.yml")
        required_handler_seam = "backend/internal/handler/handler.go"

        self.assertIn(required_handler_seam, manifest["shared_seams"])
        report = validate_manifest(manifest, manifest["shared_seams"])
        self.assertEqual(report["owned"], manifest["shared_seams"])

    def test_certified_critical_commands_match_zero_waiver_contracts(self):
        manifest = self.load_deterministic_json(".steadflow/customization.yml")

        self.assertEqual(
            manifest["critical_commands"],
            [
                {
                    "argv": [
                        "go",
                        "test",
                        "./internal/middleware",
                        "-run",
                        "HashToken",
                    ],
                    "cwd": "backend",
                    "name": "backend_distributor_middleware",
                },
                {
                    "argv": ["go", "test", "./migrations", "-run", "Distributor"],
                    "cwd": "backend",
                    "name": "backend_distributor_migrations",
                },
                {
                    "argv": ["go", "test", "./internal/service/distributor/..."],
                    "cwd": "backend",
                    "name": "backend_distributor_service",
                },
                {
                    "argv": [
                        "go",
                        "test",
                        "-tags",
                        "unit",
                        "./internal/handler",
                        "./internal/service",
                        "./internal/service/distributor/...",
                        "-run",
                        "PublicSettings|Fulfillment|UserAPIKey",
                    ],
                    "cwd": "backend",
                    "name": "backend_public_settings_payment_fulfillment_user_api_key",
                },
                {
                    "argv": [
                        "go",
                        "test",
                        "-tags",
                        "embed",
                        "./internal/web",
                        "-run",
                        "SEO|Public",
                    ],
                    "cwd": "backend",
                    "name": "backend_seo_public",
                },
                {
                    "argv": [
                        "pnpm",
                        "--dir",
                        "frontend",
                        "exec",
                        "vitest",
                        "run",
                        "src/router/__tests__/thesis-routes.spec.ts",
                        "src/utils/__tests__/branding.spec.ts",
                        "src/api/__tests__/settings.thesisVertical.spec.ts",
                        "src/seo/__tests__/routeSeo.spec.ts",
                        "src/seo/__tests__/schema.spec.ts",
                    ],
                    "name": "frontend_thesis_branding_seo",
                },
            ],
        )

    def run_go_critical_command_discovery(self, command, root):
        argv = list(command["argv"])
        if "-run" in argv:
            run_index = argv.index("-run")
            list_pattern = argv[run_index + 1]
            del argv[run_index : run_index + 2]
        else:
            list_pattern = "."
        list_argv = [*argv[:2], "-list", list_pattern, *argv[2:]]
        environment = dict(os.environ)
        environment["GOENV"] = "off"
        environment["GOFLAGS"] = ""
        environment["GOWORK"] = "off"
        dist = root / "backend" / "internal" / "web" / "dist"
        embedded_index = dist / "index.html"
        created_dist = False
        created_index = False
        try:
            if "embed" in argv and "./internal/web" in argv:
                # This only satisfies go:embed for list-only test discovery.
                # Task 7 must build the real frontend before running this gate.
                if not dist.exists():
                    dist.mkdir()
                    created_dist = True
                if not embedded_index.exists():
                    embedded_index.write_text(
                        "<!doctype html>\n", encoding="utf-8"
                    )
                    created_index = True
            try:
                completed = subprocess.run(
                    list_argv,
                    cwd=root / command["cwd"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            except subprocess.TimeoutExpired as error:
                self.fail(
                    f"name={command['name']} argv={list_argv!r} timeout=120\n"
                    f"stdout:\n{error.stdout or ''}\n"
                    f"stderr:\n{error.stderr or ''}"
                )
        finally:
            if created_index:
                embedded_index.unlink()
            if created_dist:
                dist.rmdir()
        return list_argv, completed

    def test_go_discovery_uses_disposable_clone_and_preserves_primary_dist(self):
        primary_dist = REPO_ROOT / "backend" / "internal" / "web" / "dist"
        before = (
            primary_dist.exists(),
            sorted(
                (path.relative_to(primary_dist).as_posix(), path.read_bytes())
                for path in primary_dist.rglob("*")
                if path.is_file()
            )
            if primary_dist.exists()
            else [],
        )

        self.assertNotEqual(
            self.go_discovery_root.resolve(),
            REPO_ROOT.resolve(),
            "Go discovery must not run in the primary checkout",
        )
        manifest = self.load_deterministic_json(
            ".steadflow/customization.yml", self.go_discovery_root
        )
        web_command = next(
            command
            for command in manifest["critical_commands"]
            if command["name"] == "backend_seo_public"
        )
        _, completed = self.run_go_critical_command_discovery(
            web_command, self.go_discovery_root
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

        after = (
            primary_dist.exists(),
            sorted(
                (path.relative_to(primary_dist).as_posix(), path.read_bytes())
                for path in primary_dist.rglob("*")
                if path.is_file()
            )
            if primary_dist.exists()
            else [],
        )
        self.assertEqual(after, before)

    def test_every_go_critical_command_lists_real_tests(self):
        manifest = self.load_deterministic_json(
            ".steadflow/customization.yml", self.go_discovery_root
        )
        go_commands = [
            command
            for command in manifest["critical_commands"]
            if command["argv"][:2] == ["go", "test"]
        ]
        self.assertEqual(len(go_commands), 5)

        for command in go_commands:
            with self.subTest(command=command["name"]):
                list_argv, completed = self.run_go_critical_command_discovery(
                    command, self.go_discovery_root
                )
                diagnostic = (
                    f"name={command['name']} argv={list_argv!r} "
                    f"exit={completed.returncode}\nstdout:\n{completed.stdout}\n"
                    f"stderr:\n{completed.stderr}"
                )
                self.assertEqual(completed.returncode, 0, diagnostic)
                self.assertIsNotNone(
                    re.search(r"(?m)^Test\S+$", completed.stdout), diagnostic
                )

    def test_web_discovery_is_portable_without_persisting_embed_artifacts(self):
        manifest = self.load_deterministic_json(
            ".steadflow/customization.yml", self.go_discovery_root
        )
        command = next(
            command
            for command in manifest["critical_commands"]
            if command["name"] == "backend_seo_public"
        )
        dist = (
            self.go_discovery_root / "backend" / "internal" / "web" / "dist"
        )
        self.assertFalse(dist.exists())
        list_argv, completed = self.run_go_critical_command_discovery(
            command, self.go_discovery_root
        )

        diagnostic = (
            f"name={command['name']} argv={list_argv!r} "
            f"exit={completed.returncode}\nstdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
        self.assertEqual(completed.returncode, 0, diagnostic)
        self.assertIsNotNone(
            re.search(r"(?m)^Test\S+$", completed.stdout), diagnostic
        )
        self.assertFalse(dist.exists())

    def assert_upstream_lock_matches_git_objects(self, root):
        lock = self.load_deterministic_json(".steadflow/upstream-lock.json", root)
        expected = {
            "peeled_commit": "e0c48a19ed794a565e3858662520afe0a1f9f0ba",
            "release": "v0.1.178",
            "remote": "upstream",
            "repository": "https://github.com/Wei-Shaw/sub2api.git",
            "schema_version": 1,
            "tree": "6fec3cdac4299b6114d8c7da271401a3cf329be7",
        }
        self.assertEqual(lock, expected)
        self.assertEqual(
            self.git("rev-parse", "refs/tags/v0.1.178^{commit}", root=root),
            lock["peeled_commit"],
        )
        self.assertEqual(
            self.git("rev-parse", "refs/tags/v0.1.178^{tree}", root=root),
            lock["tree"],
        )

    def test_upstream_lock_matches_local_verified_git_objects(self):
        self.assert_upstream_lock_matches_git_objects(REPO_ROOT)

    def test_upstream_lock_validation_is_portable_to_origin_only_clone(self):
        self.assertEqual(self.git("remote", root=self.go_discovery_root), "origin")

        self.assert_upstream_lock_matches_git_objects(self.go_discovery_root)

    def test_migration_baseline_matches_every_current_sql_file(self):
        baseline = self.load_deterministic_json(
            ".steadflow/migration-checksums.json"
        )
        self.assertEqual(
            set(baseline),
            {"algorithm", "migrations", "reviewed_additions", "schema_version"},
        )
        self.assertEqual(baseline["schema_version"], 1)
        self.assertEqual(baseline["algorithm"], "sha256")
        independently_hashed = {
            path.relative_to(REPO_ROOT).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted((REPO_ROOT / "backend" / "migrations").glob("*.sql"))
        }
        self.assertEqual(list(baseline["migrations"]), sorted(baseline["migrations"]))
        self.assertEqual(baseline["migrations"], independently_hashed)
        self.assertEqual(baseline["migrations"], migration_checksums(REPO_ROOT))
        self.assertEqual(len(baseline["migrations"]), 268)
        self.assertEqual(
            baseline["reviewed_additions"]
            ["backend/migrations/227_composite_routes_add_cn_providers.sql"]
            ["sha256"],
            "21d81a064828e8a544992f98e949053e45e9a135bc011f129147675d94612ddf",
        )
        report = validate_migrations(REPO_ROOT, baseline)
        self.assertEqual(len(report["unchanged"]), 268)
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["added"], [])

    def test_certified_known_failure_baseline_records_zero_without_overclaiming(self):
        document = self.load_deterministic_json(".steadflow/known-failures.yml")

        self.assertEqual(validate_known_failures(document), document)
        self.assertEqual(document["entries"], [])
        self.assertEqual(
            document["baseline"]["result"],
            {"failed": 0, "go_failed": 0, "vitest_failed": 0},
        )
        self.assertEqual(
            document["baseline"]["evidence"],
            {
                "go_json_sha256": (
                    "9889e37c20a29ad4c462ee59cd086260e5b4e40c373bea14386961325d251406"
                ),
                "vitest_json_sha256": (
                    "a71c183c8d8ee42024b15fce7bac2f85bf9eba0e8acde6abb759b2f7d5c7aaba"
                ),
            },
        )
        note = document["baseline"]["historical_observation"]
        self.assertIn("12", note)
        self.assertIn("not reproducible", note)
        self.assertIn("does not prove", note)

    def test_makefile_exposes_single_critical_gate_entrypoint(self):
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

        self.assertRegex(
            makefile,
            r"(?m)^test-steadflow-critical:\n"
            r"\t@\./tools/upstream-sync/upgrade --run-critical$",
        )
        phony_line = next(
            line for line in makefile.splitlines() if line.startswith(".PHONY:")
        )
        self.assertIn("test-steadflow-critical", phony_line.split())


class ExactFailureGateTests(unittest.TestCase):
    def empty_baseline(self, entries=None):
        return {
            "baseline": {
                "commands": [
                    "cd backend && go test -json ./...",
                    "pnpm --dir frontend exec vitest run --reporter=json",
                ],
                "evidence": {
                    "go_json_sha256": "a" * 64,
                    "vitest_json_sha256": "b" * 64,
                },
                "historical_observation": (
                    "An earlier count of 12 failures was not reproducible in the "
                    "certified v0.1.178 environment; this does not prove each was "
                    "fixed upstream."
                ),
                "release": "v0.1.178",
                "result": {"failed": 0, "go_failed": 0, "vitest_failed": 0},
            },
            "entries": [] if entries is None else entries,
            "schema_version": 1,
        }

    def test_go_json_parser_returns_sorted_stable_exact_ids(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "Action": "fail",
                        "Package": "example/z",
                        "Test": "TestBeta/subcase",
                    }
                ),
                json.dumps(
                    {"Action": "pass", "Package": "example/a", "Test": "TestOK"}
                ),
                json.dumps(
                    {"Action": "fail", "Package": "example/a", "Test": "TestAlpha"}
                ),
                json.dumps(
                    {"Action": "skip", "Package": "example/a", "Test": "TestSkip"}
                ),
            ]
        )

        result = parse_go_test_jsonl(raw)

        self.assertEqual(
            result,
            {
                "failed": [
                    "go:example/a:TestAlpha",
                    "go:example/z:TestBeta/subcase",
                ],
                "passed": ["go:example/a:TestOK"],
                "skipped": ["go:example/a:TestSkip"],
            },
        )

    def test_go_json_parser_rejects_malformed_and_package_only_failure(self):
        with self.assertRaisesRegex(ValueError, "line 1"):
            parse_go_test_jsonl("not-json\n")
        with self.assertRaisesRegex(ValueError, "package failed without exact test"):
            parse_go_test_jsonl(
                json.dumps({"Action": "fail", "Package": "example/broken"}) + "\n"
            )

    def test_go_json_parser_does_not_hide_package_build_failure_behind_other_test(self):
        raw = "\n".join(
            [
                json.dumps(
                    {
                        "Action": "fail",
                        "Package": "example/tested",
                        "Test": "TestFailure",
                    }
                ),
                json.dumps({"Action": "fail", "Package": "example/tested"}),
                json.dumps({"Action": "fail", "Package": "example/build-broken"}),
            ]
        )

        with self.assertRaisesRegex(ValueError, "example/build-broken"):
            parse_go_test_jsonl(raw)

    def test_go_json_parser_rejects_non_object_and_wrong_field_types(self):
        invalid_events = (
            [],
            {"Action": 1, "Package": "example/a", "Test": "TestA"},
            {"Action": "fail", "Package": 1, "Test": "TestA"},
            {"Action": "fail", "Package": "example/a", "Test": []},
        )
        for event in invalid_events:
            with self.subTest(event=event), self.assertRaisesRegex(
                ValueError, "Go test JSON"
            ):
                parse_go_test_jsonl(json.dumps(event) + "\n")

    def test_vitest_parser_normalizes_absolute_file_and_full_test_name(self):
        document = {
            "success": False,
            "testResults": [
                {
                    "name": "/repo/frontend/src/z.spec.ts",
                    "assertionResults": [
                        {"fullName": "suite beta", "status": "failed"},
                        {"fullName": "suite alpha", "status": "passed"},
                    ],
                }
            ],
        }

        result = parse_vitest_json(document, repo_root=Path("/repo"))

        self.assertEqual(
            result,
            {
                "failed": ["vitest:frontend/src/z.spec.ts:suite beta"],
                "passed": ["vitest:frontend/src/z.spec.ts:suite alpha"],
                "skipped": [],
            },
        )

    def test_vitest_parser_rejects_failed_suite_without_exact_assertion(self):
        with self.assertRaisesRegex(ValueError, "failed without exact test"):
            parse_vitest_json(
                {
                    "success": False,
                    "testResults": [
                        {
                            "name": "frontend/src/z.spec.ts",
                            "status": "failed",
                            "assertionResults": [],
                        }
                    ],
                }
            )

    def test_vitest_parser_rejects_duplicate_stable_ids_even_across_suites(self):
        for second_status in ("passed", "failed"):
            with self.subTest(second_status=second_status):
                document = {
                    "success": second_status == "passed",
                    "testResults": [
                        {
                            "name": "src/duplicate.spec.ts",
                            "status": "passed",
                            "assertionResults": [
                                {"fullName": "duplicate test", "status": "passed"}
                            ],
                        },
                        {
                            "name": "src/duplicate.spec.ts",
                            "status": second_status,
                            "assertionResults": [
                                {
                                    "fullName": "duplicate test",
                                    "status": second_status,
                                }
                            ],
                        },
                    ],
                }

                with self.assertRaisesRegex(
                    ValueError, "duplicate Vitest stable test ID"
                ):
                    parse_vitest_json(document)

    def test_vitest_parser_rejects_escape_collision_and_invalid_assertion_shape(self):
        cases = (
            {
                "success": True,
                "testResults": [
                    {
                        "name": "../../escape.spec.ts",
                        "assertionResults": [
                            {"fullName": "escape", "status": "passed"}
                        ],
                    }
                ],
            },
            {
                "success": True,
                "testResults": [
                    {
                        "name": "/repo/frontend/src/a.spec.ts",
                        "assertionResults": [
                            {"fullName": "same", "status": "passed"}
                        ],
                    },
                    {
                        "name": "src/a.spec.ts",
                        "assertionResults": [
                            {"fullName": "same", "status": "passed"}
                        ],
                    },
                ],
            },
            {
                "success": True,
                "testResults": [
                    {"name": "src/a.spec.ts", "assertionResults": ["not-object"]}
                ],
            },
        )
        for document in cases:
            with self.subTest(document=document), self.assertRaises(ValueError):
                parse_vitest_json(document, repo_root=Path("/repo"))

    def test_vitest_parser_rejects_wrong_status_shapes_and_unknown_values(self):
        invalid_documents = (
            {"success": "yes", "testResults": []},
            {
                "success": True,
                "testResults": [
                    {
                        "name": "src/a.spec.ts",
                        "status": [],
                        "assertionResults": [],
                    }
                ],
            },
            {
                "success": True,
                "testResults": [
                    {
                        "name": "src/a.spec.ts",
                        "status": "mystery",
                        "assertionResults": [],
                    }
                ],
            },
            {
                "success": True,
                "testResults": [
                    {
                        "name": "src/a.spec.ts",
                        "status": "passed",
                        "assertionResults": [
                            {"fullName": "test", "status": "mystery"}
                        ],
                    }
                ],
            },
        )
        for document in invalid_documents:
            with self.subTest(document=document), self.assertRaisesRegex(
                ValueError, "Vitest"
            ):
                parse_vitest_json(document, repo_root=Path("/repo"))

    def test_empty_known_failure_baseline_is_explicit_and_valid(self):
        baseline = self.empty_baseline()

        self.assertEqual(validate_known_failures(baseline), baseline)
        self.assertEqual(baseline["entries"], [])
        self.assertEqual(baseline["baseline"]["result"]["failed"], 0)
        self.assertIn("does not prove", baseline["baseline"]["historical_observation"])

    def test_known_failure_schema_requires_exact_sorted_entries(self):
        entry = {
            "category": "upstream-known",
            "evidence_command": "cd backend && go test -json ./internal/x",
            "expires": "v0.1.179",
            "first_seen": "v0.1.178",
            "id": "go:example/x:TestKnown",
            "reason": "reproducible fixture mismatch",
        }
        baseline = self.empty_baseline([entry])
        self.assertEqual(validate_known_failures(baseline), baseline)

        duplicate = self.empty_baseline([entry, dict(entry)])
        with self.assertRaisesRegex(ValueError, "sorted and unique"):
            validate_known_failures(duplicate)
        wrong_category = self.empty_baseline(
            [dict(entry, category="steadflow-critical")]
        )
        with self.assertRaisesRegex(ValueError, "upstream-known"):
            validate_known_failures(wrong_category)

    def test_comparison_reports_new_fixed_known_and_expired_exact_ids(self):
        entries = sorted([
            {
                "category": "upstream-known",
                "evidence_command": "go test -json ./...",
                "expires": "v0.1.180",
                "first_seen": "v0.1.178",
                "id": "go:pkg:TestFixed",
                "reason": "fixture mismatch",
            },
            {
                "category": "upstream-known",
                "evidence_command": "go test -json ./...",
                "expires": "v0.1.179",
                "first_seen": "v0.1.178",
                "id": "go:pkg:TestExpired",
                "reason": "fixture mismatch",
            },
            {
                "category": "upstream-known",
                "evidence_command": "go test -json ./...",
                "expires": "v0.1.180",
                "first_seen": "v0.1.178",
                "id": "go:pkg:TestKnown",
                "reason": "fixture mismatch",
            },
        ], key=lambda entry: entry["id"])
        result = compare_test_failures(
            ["go:pkg:TestNew", "go:pkg:TestKnown", "go:pkg:TestExpired"],
            self.empty_baseline(entries),
            target_release="v0.1.179",
        )

        self.assertEqual(result["new"], ["go:pkg:TestNew"])
        self.assertEqual(result["fixed"], ["go:pkg:TestFixed"])
        self.assertEqual(result["known"], ["go:pkg:TestKnown"])
        self.assertEqual(result["expired"], ["go:pkg:TestExpired"])
        self.assertEqual(result["status"], "BLOCKED")

    def test_critical_failure_is_never_waived(self):
        entry = {
            "category": "upstream-known",
            "evidence_command": "go test -json ./critical",
            "expires": "v0.1.180",
            "first_seen": "v0.1.178",
            "id": "go:critical:TestPaymentFulfillment",
            "reason": "old upstream issue",
        }
        result = compare_test_failures(
            [entry["id"]],
            self.empty_baseline([entry]),
            critical_ids=[entry["id"]],
            target_release="v0.1.179",
        )

        self.assertEqual(result["critical"], [entry["id"]])
        self.assertEqual(result["known"], [])
        self.assertEqual(result["status"], "BLOCKED")

    def test_unexpired_exact_known_failure_uses_known_fail_status(self):
        entry = {
            "category": "upstream-known",
            "evidence_command": "go test -json ./...",
            "expires": "v0.1.180",
            "first_seen": "v0.1.178",
            "id": "go:example/x:TestKnown",
            "reason": "reproducible upstream fixture mismatch",
        }

        result = compare_test_failures(
            [entry["id"]],
            self.empty_baseline([entry]),
            target_release="v0.1.179",
        )

        self.assertEqual(result["known"], [entry["id"]])
        self.assertEqual(result["status"], "KNOWN-FAIL")

    def test_fixed_only_allowlist_blocks_until_entry_is_removed(self):
        entry = {
            "category": "upstream-known",
            "evidence_command": "go test -json ./...",
            "expires": "v0.1.180",
            "first_seen": "v0.1.178",
            "id": "go:example/x:TestNowFixed",
            "reason": "reproducible upstream fixture mismatch",
        }

        result = compare_test_failures(
            [],
            self.empty_baseline([entry]),
            target_release="v0.1.179",
        )

        self.assertEqual(result["fixed"], [entry["id"]])
        self.assertEqual(result["status"], "BLOCKED")


class MigrationRiskGateTests(unittest.TestCase):
    def test_migrations_directory_must_be_contained_real_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            outside = Path(directory) / "outside"
            (root / "backend").mkdir(parents=True)
            outside.mkdir()
            (outside / "001.sql").write_text("DROP TABLE users;\n", encoding="utf-8")
            (root / "backend" / "migrations").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(
                MigrationValidationError, "migrations directory"
            ):
                migration_checksums(root)

    def test_new_sql_is_classified_by_risk(self):
        self.assertEqual(
            classify_migration_sql(b"CREATE TABLE safe_table (id bigint);"),
            "additive",
        )
        self.assertEqual(
            classify_migration_sql(b"ALTER TABLE users ADD COLUMN nickname text;"),
            "additive",
        )
        self.assertEqual(
            classify_migration_sql(b"DROP TABLE users;"), "destructive"
        )
        self.assertEqual(
            classify_migration_sql(b"ALTER TABLE users DROP COLUMN email;"),
            "destructive",
        )
        self.assertEqual(
            classify_migration_sql(b"UPDATE users SET active = false;"),
            "review-required",
        )

    def test_high_confidence_rename_type_and_materialized_view_sql_is_destructive(self):
        destructive = (
            b"ALTER TABLE users RENAME COLUMN email TO login;",
            b"alter\n table users /* keep */ rename\n to app_users;",
            b"ALTER TABLE users ALTER COLUMN score TYPE bigint USING score::bigint;",
            b"drop materialized\n view if exists active_users;",
        )
        for sql in destructive:
            with self.subTest(sql=sql):
                self.assertEqual(classify_migration_sql(sql), "destructive")

        self.assertEqual(
            classify_migration_sql(b"ALTER TABLE users ENABLE ROW LEVEL SECURITY;"),
            "review-required",
        )

    def test_destructive_added_migration_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "001_initial.sql").write_text(
                "DROP TABLE users;\n", encoding="utf-8"
            )
            baseline = {
                "algorithm": "sha256",
                "migrations": {},
                "schema_version": 1,
            }

            with self.assertRaisesRegex(
                MigrationValidationError, "destructive new migrations"
            ):
                validate_migrations(root, baseline)

    def test_exact_reviewed_destructive_migration_is_auditable_and_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            content = (
                b"ALTER TABLE routes DROP CONSTRAINT IF EXISTS routes_check;\n"
                b"ALTER TABLE routes ADD CONSTRAINT routes_check CHECK (kind IN ('a', 'b'));\n"
            )
            path = "backend/migrations/002_expand_check.sql"
            (root / path).write_bytes(content)
            baseline = {
                "algorithm": "sha256",
                "migrations": {},
                "reviewed_additions": {
                    path: {
                        "rationale": "Replaces the same CHECK constraint with an expanded allowlist.",
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                },
                "schema_version": 1,
            }

            report = validate_migrations(root, baseline)

        self.assertEqual(report["added_risk"], {path: "reviewed-destructive"})

    def test_reviewed_destructive_migration_requires_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            path = "backend/migrations/002_drop.sql"
            (root / path).write_text("DROP TABLE users;\n", encoding="utf-8")
            baseline = {
                "algorithm": "sha256",
                "migrations": {},
                "reviewed_additions": {
                    path: {"rationale": "reviewed", "sha256": "0" * 64}
                },
                "schema_version": 1,
            }

            with self.assertRaisesRegex(
                MigrationValidationError, "destructive new migrations"
            ):
                validate_migrations(root, baseline)

    def test_added_migration_report_contains_sorted_risk_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "backend" / "migrations"
            migrations.mkdir(parents=True)
            (migrations / "002_review.sql").write_text(
                "UPDATE users SET active = false;\n", encoding="utf-8"
            )
            (migrations / "001_add.sql").write_text(
                "CREATE INDEX idx_users ON users(id);\n", encoding="utf-8"
            )
            baseline = {
                "algorithm": "sha256",
                "migrations": {},
                "schema_version": 1,
            }

            report = validate_migrations(root, baseline)

        self.assertEqual(
            report["added_risk"],
            {
                "backend/migrations/001_add.sql": "additive",
                "backend/migrations/002_review.sql": "review-required",
            },
        )


class CriticalCommandGateTests(unittest.TestCase):
    def test_critical_suite_builds_frontend_before_any_declared_command(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/web", "-run", "SEO|Public"],
                "cwd": "backend",
                "name": "backend_seo_public",
            }
        ]
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def runner(argv, **kwargs):
                calls.append(argv)
                if argv == ["pnpm", "--dir", "frontend", "run", "build"]:
                    dist = root / "backend" / "internal" / "web" / "dist"
                    (dist / "assets").mkdir(parents=True)
                    (dist / "index.html").write_text("built\n", encoding="utf-8")
                    (dist / "assets" / "app.js").write_text("built\n", encoding="utf-8")
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout="\n".join(
                        [
                            json.dumps(
                                {
                                    "Action": "pass",
                                    "Package": "example/web",
                                    "Test": "TestPublicSEO",
                                }
                            ),
                            json.dumps(
                                {"Action": "pass", "Package": "example/web"}
                            ),
                        ]
                    ),
                    stderr="",
                )

            report = run_critical_suite(root, commands, runner=runner)

        self.assertEqual(calls[0], ["pnpm", "--dir", "frontend", "run", "build"])
        self.assertEqual(report[0]["id"], "command:backend_seo_public")

    def test_go_command_zero_tests_is_blocked_even_with_zero_exit(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/middleware", "-run", "NoMatch"],
                "cwd": "backend",
                "name": "backend_empty",
            }
        ]

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "Action": "pass",
                        "Package": "example/middleware",
                        "Elapsed": 0.1,
                    }
                )
                + "\n",
                stderr="",
            )

        with self.assertRaisesRegex(UpgradeBlocked, "executed zero tests"):
            run_critical_commands(REPO_ROOT, commands, runner=runner)

    def test_skipped_only_critical_command_is_blocked(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/middleware"],
                "cwd": "backend",
                "name": "backend_skipped",
            }
        ]

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="\n".join(
                    [
                        json.dumps(
                            {
                                "Action": "skip",
                                "Package": "example/middleware",
                                "Test": "TestOnly",
                            }
                        ),
                        json.dumps(
                            {"Action": "pass", "Package": "example/middleware"}
                        ),
                    ]
                ),
                stderr="",
            )

        with self.assertRaisesRegex(UpgradeBlocked, "executed zero tests"):
            run_critical_commands(REPO_ROOT, commands, runner=runner)

    def test_web_critical_requires_real_frontend_build_before_execution(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/web", "-run", "SEO|Public"],
                "cwd": "backend",
                "name": "backend_seo_public",
            }
        ]
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            raise AssertionError("runner must not execute without frontend build")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(UpgradeBlocked, "frontend build"):
                run_critical_commands(Path(directory), commands, runner=runner)
        self.assertEqual(calls, [])

    def test_critical_suite_cannot_reuse_stale_ignored_frontend_dist(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/web", "-run", "SEO|Public"],
                "cwd": "backend",
                "name": "backend_seo_public",
            }
        ]
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "backend" / "internal" / "web" / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text("stale\n", encoding="utf-8")
            (dist / "assets" / "stale.js").write_text("stale\n", encoding="utf-8")

            def runner(argv, **kwargs):
                calls.append(list(argv))
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with self.assertRaisesRegex(UpgradeBlocked, "real frontend build"):
                run_critical_suite(root, commands, runner=runner)

        self.assertEqual(calls, [["pnpm", "--dir", "frontend", "run", "build"]])

    def test_fresh_build_cleanup_rejects_symlinked_parent_without_deleting_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            actual_web = root / "actual-web"
            dist = actual_web / "dist"
            (dist / "assets").mkdir(parents=True)
            (dist / "index.html").write_text("preserve\n", encoding="utf-8")
            (root / "backend" / "internal").mkdir(parents=True)
            (root / "backend" / "internal" / "web").symlink_to(
                actual_web, target_is_directory=True
            )

            def runner(*args, **kwargs):
                raise AssertionError("runner must not execute through symlink parent")

            with self.assertRaisesRegex(UpgradeBlocked, "real directories"):
                run_critical_suite(root, [], runner=runner)

            self.assertEqual(
                (dist / "index.html").read_text(encoding="utf-8"), "preserve\n"
            )

    def test_vitest_critical_records_command_id_and_actual_test_count(self):
        commands = [
            {
                "argv": ["pnpm", "--dir", "frontend", "exec", "vitest", "run", "x.spec.ts"],
                "name": "frontend_contract",
            }
        ]

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "success": True,
                        "testResults": [
                            {
                                "name": "frontend/x.spec.ts",
                                "assertionResults": [
                                    {"fullName": "x works", "status": "passed"}
                                ],
                            }
                        ],
                    }
                ),
                stderr="",
            )

        report = run_critical_commands(REPO_ROOT, commands, runner=runner)

        self.assertEqual(
            report,
            [
                {
                    "failed": [],
                    "id": "command:frontend_contract",
                    "status": "PASS",
                    "tests_executed": 1,
                }
            ],
        )

    def test_failed_command_diagnostic_is_redacted_and_never_waived(self):
        commands = [
            {
                "argv": ["go", "test", "./internal/service/distributor/..."],
                "cwd": "backend",
                "name": "backend_distributor_service",
            }
        ]

        def runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr="password=hunter2 token=abc secret=def api_key=ghi",
            )

        with self.assertRaises(UpgradeBlocked) as raised:
            run_critical_commands(REPO_ROOT, commands, runner=runner)
        diagnostic = str(raised.exception)
        for secret in ("hunter2", "abc", "def", "ghi"):
            self.assertNotIn(secret, diagnostic)
        self.assertIn("command:backend_distributor_service", diagnostic)


class UpgradeReportTests(unittest.TestCase):
    def test_steadflow_replacement_cannot_redirect_report_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            steadflow = root / ".steadflow"
            steadflow.mkdir(parents=True)
            outside = Path(directory) / "outside"
            outside.mkdir()
            saved = root / ".steadflow-saved"
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if path == "reports" and dir_fd is not None and not swapped:
                    steadflow.rename(saved)
                    steadflow.symlink_to(outside, target_is_directory=True)
                    swapped = True
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch("upstream_sync.os.open", side_effect=racing_open):
                with self.assertRaises(UpgradeBlocked):
                    write_upgrade_reports(root, "v0.1.179", {"status": "PASS"})

            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])

    def test_reports_replacement_cannot_redirect_report_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            reports = root / ".steadflow" / "reports"
            reports.mkdir(parents=True)
            outside = Path(directory) / "outside"
            outside.mkdir()
            saved = root / ".steadflow" / "reports-saved"
            real_open = os.open
            swapped = False

            def racing_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if (
                    isinstance(path, str)
                    and path.startswith(".v0.1.179.json.")
                    and dir_fd is not None
                    and not swapped
                ):
                    reports.rename(saved)
                    reports.symlink_to(outside, target_is_directory=True)
                    swapped = True
                if dir_fd is None:
                    return real_open(path, flags, mode)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with mock.patch("upstream_sync.os.open", side_effect=racing_open):
                with self.assertRaises(UpgradeBlocked):
                    write_upgrade_reports(root, "v0.1.179", {"status": "PASS"})

            self.assertTrue(swapped)
            self.assertEqual(list(outside.iterdir()), [])

    def test_nested_password_token_secret_and_key_values_are_redacted(self):
        report = {
            "password": "hunter2",
            "nested": {
                "providerToken": "abc",
                "client_secret": "def",
                "api-key": "ghi",
                "safe": "token=inline-value password=inline-pass",
            },
            "items": [{"privateKey": "pem-value"}],
        }

        redacted = redact_report_secrets(report)

        serialized = json.dumps(redacted, sort_keys=True)
        for secret in (
            "hunter2",
            "abc",
            "def",
            "ghi",
            "inline-value",
            "inline-pass",
            "pem-value",
        ):
            self.assertNotIn(secret, serialized)
        self.assertEqual(redacted["nested"]["safe"], "token=<redacted> password=<redacted>")

    def test_redactor_covers_colon_json_and_authorization_bearer_forms(self):
        value = (
            'password: hunter2 client_secret: "client-value" '
            '"api_key":"api-value" Authorization: Bearer bearer-value '
            "token=token-value"
        )

        redacted = redact_report_secrets(value)

        for secret in (
            "hunter2",
            "client-value",
            "api-value",
            "bearer-value",
            "token-value",
        ):
            self.assertNotIn(secret, redacted)

    def test_redactor_covers_database_and_cache_uri_userinfo_without_false_positive(self):
        value = (
            "postgres://user:pg-pass@db.example:5432/app "
            "postgresql://:pg2-pass@db2.example/app "
            "mysql://user:mysql-pass@mysql.example/catalog "
            "redis://:redis-pass@cache.example/0 "
            "rediss://user:rediss-pass@cache.example/1 ordinary x:y"
        )

        redacted = redact_report_secrets(value)

        for secret in ("pg-pass", "pg2-pass", "mysql-pass", "redis-pass", "rediss-pass"):
            self.assertNotIn(secret, redacted)
        for diagnostic in (
            "db.example:5432/app",
            "db2.example/app",
            "mysql.example/catalog",
            "cache.example/0",
            "cache.example/1",
            "ordinary x:y",
        ):
            self.assertIn(diagnostic, redacted)

    def test_redactor_preserves_safe_url_spelling_in_stable_test_ids(self):
        identifier = (
            "go:example.test/proxy:TestCase/"
            "HTTP://PROXY.example.com:8080"
        )

        self.assertEqual(redact_report_secrets(identifier), identifier)

    def test_reports_reject_unknown_status_and_sort_object_lists_by_stable_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "report status"):
                write_upgrade_reports(
                    Path(directory), "v0.1.179", {"status": "SUCCESS"}
                )
            with self.assertRaisesRegex(ValueError, "report status"):
                write_upgrade_reports(
                    Path(directory), "v0.1.179", {"status": []}
                )

        report = {
            "status": "PASS",
            "commands": [
                {"id": "command:z", "status": "PASS"},
                {"id": "command:a", "status": "PASS"},
            ],
            "migrations": [
                {"path": "z.sql", "status": "NOT-RUN"},
                {"path": "a.sql", "status": "PASS"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            json_path, _ = write_upgrade_reports(
                Path(directory), "v0.1.179", report
            )
            written = json.loads(json_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [item["id"] for item in written["commands"]],
            ["command:a", "command:z"],
        )
        self.assertEqual(
            [item["path"] for item in written["migrations"]],
            ["a.sql", "z.sql"],
        )

    def test_json_and_markdown_reports_are_sorted_and_secret_free(self):
        report = {
            "status": "BLOCKED",
            "release": "v0.1.179",
            "tests": {
                "new": ["go:z:TestZ", "go:a:TestA"],
                "fixed": ["vitest:z:z", "vitest:a:a"],
            },
            "diagnostic": "password=hunter2",
        }
        with tempfile.TemporaryDirectory() as directory:
            json_path, markdown_path = write_upgrade_reports(
                Path(directory), "v0.1.179", report
            )
            json_text = json_path.read_text(encoding="utf-8")
            markdown_text = markdown_path.read_text(encoding="utf-8")

        self.assertNotIn("hunter2", json_text + markdown_text)
        self.assertLess(json_text.index("go:a:TestA"), json_text.index("go:z:TestZ"))
        self.assertLess(markdown_text.index("go:a:TestA"), markdown_text.index("go:z:TestZ"))
        self.assertTrue(json_text.endswith("\n"))
        self.assertIn("# Steadflow upstream report: v0.1.179", markdown_text)

    def test_report_directory_symlink_cannot_escape_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            outside = Path(directory) / "outside"
            (root / ".steadflow").mkdir(parents=True)
            outside.mkdir()
            (root / ".steadflow" / "reports").symlink_to(
                outside, target_is_directory=True
            )

            with self.assertRaisesRegex(UpgradeBlocked, "reports directory"):
                write_upgrade_reports(root, "v0.1.179", {"status": "PASS"})

            self.assertEqual(list(outside.iterdir()), [])

    def test_report_files_are_private_and_existing_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            reports = root / ".steadflow" / "reports"
            reports.mkdir(parents=True)
            outside = Path(directory) / "outside.json"
            outside.write_text("preserve\n", encoding="utf-8")
            (reports / "v0.1.179.json").symlink_to(outside)

            with self.assertRaisesRegex(UpgradeBlocked, "report file"):
                write_upgrade_reports(root, "v0.1.179", {"status": "PASS"})
            self.assertEqual(outside.read_text(encoding="utf-8"), "preserve\n")

            (reports / "v0.1.179.json").unlink()
            json_path, markdown_path = write_upgrade_reports(
                root, "v0.1.179", {"status": "PASS"}
            )
            self.assertEqual(stat.S_IMODE(reports.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(json_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(markdown_path.stat().st_mode), 0o600)


_EXPLICIT_FIXTURE_GIT_ROOT_REQUIRED = object()


class HermeticUpgradeFixture:
    current_release = "v1.0.0"

    def __init__(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.official = self.root / "official-working"
        self.upstream_bare = self.root / "official-upstream.git"
        self.fork = self.root / "fork-working"
        self.origin_bare = self.root / "fork-origin.git"
        self.trace_path = self.root / "git-trace.jsonl"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self._write_validation_fakes()
        self.environment = {
            "PATH": str(self.fake_bin) + os.pathsep + os.environ["PATH"],
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.run_git(
            "init",
            "--quiet",
            "--initial-branch=main",
            str(self.official),
            root=None,
        )
        self.configure_identity(self.official)
        (self.official / "backend" / "migrations").mkdir(parents=True)
        (self.official / "backend" / "migrations" / "001_initial.sql").write_text(
            "SELECT 1;\n", encoding="utf-8"
        )
        (self.official / "shared.txt").write_text("base\n", encoding="utf-8")
        self.run_git("add", ".", root=self.official)
        self.run_git("commit", "-m", "official base", root=self.official)
        self.current_commit = self.run_git(
            "rev-parse", "HEAD", root=self.official
        ).stdout.strip()
        self.current_tree = self.run_git(
            "rev-parse", "HEAD^{tree}", root=self.official
        ).stdout.strip()
        self.run_git(
            "tag",
            "-a",
            self.current_release,
            "-m",
            self.current_release,
            root=self.official,
        )
        self.run_git(
            "clone",
            "--quiet",
            "--bare",
            "--no-local",
            str(self.official),
            str(self.upstream_bare),
            root=None,
        )
        self.run_git(
            "remote", "add", "upstream", str(self.upstream_bare), root=self.official
        )
        self.run_git(
            "clone",
            "--quiet",
            "--no-local",
            str(self.official),
            str(self.fork),
            root=None,
        )
        self.configure_identity(self.fork)
        self.write_steadflow_configuration()
        (self.fork / ".gitignore").write_text(
            ".worktrees/\nbackend/internal/web/dist/\n",
            encoding="utf-8",
        )
        (self.fork / "fork.txt").write_text("steadflow\n", encoding="utf-8")
        self.run_git("add", ".", root=self.fork)
        self.run_git("commit", "-m", "steadflow customization", root=self.fork)
        self.source_commit = self.run_git(
            "rev-parse", "HEAD", root=self.fork
        ).stdout.strip()
        self.run_git(
            "clone",
            "--quiet",
            "--bare",
            "--no-local",
            str(self.fork),
            str(self.origin_bare),
            root=None,
        )
        self.run_git(
            "remote", "set-url", "origin", str(self.origin_bare), root=self.fork
        )
        self.run_git(
            "remote", "add", "upstream", str(self.upstream_bare), root=self.fork
        )

    def cleanup(self):
        self.temporary_directory.cleanup()

    def _write_validation_fakes(self):
        go = self.fake_bin / "go"
        go.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'test':\n"
            " print(json.dumps({'Action':'pass','Package':'fixture/pkg','Test':'TestFixture'}))\n"
            " print(json.dumps({'Action':'pass','Package':'fixture/pkg'}))\n"
            " sys.exit(0)\n"
            "if len(sys.argv) > 1 and sys.argv[1] == 'generate': sys.exit(0)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        pnpm = self.fake_bin / "pnpm"
        pnpm.write_text(
            "#!/usr/bin/env python3\n"
            "import json, pathlib, sys\n"
            "root=pathlib.Path.cwd()\n"
            "if 'build' in sys.argv:\n"
            " dist=root/'backend'/'internal'/'web'/'dist'\n"
            " (dist/'assets').mkdir(parents=True,exist_ok=True)\n"
            " (dist/'index.html').write_text('built\\n')\n"
            " (dist/'assets'/'app.js').write_text('built\\n')\n"
            " sys.exit(0)\n"
            "if 'vitest' in sys.argv:\n"
            " print(json.dumps({'success':True,'testResults':[{'name':str(root/'frontend'/'src'/'fixture.spec.ts'),'assertionResults':[{'fullName':'fixture passes','status':'passed'}]}]}))\n"
            " sys.exit(0)\n"
            "sys.exit(2)\n",
            encoding="utf-8",
        )
        make = self.fake_bin / "make"
        make.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        for executable in (go, pnpm, make):
            executable.chmod(0o755)

    def validation_runner(self, argv, **kwargs):
        kwargs["env"] = self.environment
        return subprocess.run(argv, **kwargs)

    def repository(self):
        repository = GitRepository(self.fork)
        repository.validation_runner = self.validation_runner
        return repository

    def configure_identity(self, root):
        self.run_git(
            "config", "--local", "user.name", "Steadflow Fixture", root=root
        )
        self.run_git(
            "config",
            "--local",
            "user.email",
            "steadflow-fixture@example.invalid",
            root=root,
        )

    def run_git(
        self,
        *arguments,
        root=_EXPLICIT_FIXTURE_GIT_ROOT_REQUIRED,
        check=True,
        input_text=None,
    ):
        assert root is not _EXPLICIT_FIXTURE_GIT_ROOT_REQUIRED, (
            "fixture Git calls must pass root explicitly"
        )
        argv = ["git"]
        if root is not None:
            argv.extend(("-C", str(root)))
        argv.extend(arguments)
        return subprocess.run(
            argv,
            env=self.environment,
            check=check,
            input=input_text,
            capture_output=True,
            text=True,
        )

    def write_json(self, relative_path, document):
        path = self.fork / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def write_steadflow_configuration(self):
        manifest = manifest_with_paths(
            data=[
                ".gitignore",
                ".steadflow/customization.yml",
                ".steadflow/known-failures.yml",
                ".steadflow/migration-checksums.json",
                ".steadflow/upstream-lock.json",
                "fork.txt",
            ]
        )
        manifest["critical_commands"] = [
            {
                "argv": ["go", "test", "./..."],
                "cwd": "backend",
                "name": "fixture_critical",
            }
        ]
        self.write_json(".steadflow/customization.yml", manifest)
        self.write_json(
            ".steadflow/known-failures.yml",
            {
                "baseline": {
                    "commands": ["go test -json ./...", "vitest run --reporter=json"],
                    "evidence": {
                        "go_json_sha256": "a" * 64,
                        "vitest_json_sha256": "b" * 64,
                    },
                    "historical_observation": "No historical fixture failures.",
                    "release": self.current_release,
                    "result": {"failed": 0, "go_failed": 0, "vitest_failed": 0},
                },
                "entries": [],
                "schema_version": 1,
            },
        )
        migration = self.fork / "backend" / "migrations" / "001_initial.sql"
        self.write_json(
            ".steadflow/migration-checksums.json",
            {
                "algorithm": "sha256",
                "migrations": {
                    "backend/migrations/001_initial.sql": hashlib.sha256(
                        migration.read_bytes()
                    ).hexdigest()
                },
                "schema_version": 1,
            },
        )
        self.write_json(
            ".steadflow/upstream-lock.json",
            {
                "peeled_commit": self.current_commit,
                "release": self.current_release,
                "remote": "upstream",
                "repository": str(self.upstream_bare),
                "schema_version": 1,
                "tree": self.current_tree,
            },
        )

    def configure_six_critical_commands(self):
        path = self.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["critical_commands"] = [
            {
                "argv": ["go", "test", f"./fixture/{index}"],
                "cwd": "backend",
                "name": f"fixture_critical_{index}",
            }
            for index in range(1, 7)
        ]
        path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.run_git("add", ".steadflow/customization.yml", root=self.fork)
        self.run_git("commit", "-m", "declare six critical fixtures", root=self.fork)
        self.source_commit = self.run_git(
            "rev-parse", "HEAD", root=self.fork
        ).stdout.strip()

    def install_forbidden_gpg_signer(self):
        marker = self.root / "gpg-signer-ran"
        signer = self.root / "forbidden-gpg-signer"
        signer.write_text(
            "#!/bin/sh\n"
            f"printf 'signer ran\\n' > {shlex.quote(str(marker))}\n"
            "exit 99\n",
            encoding="utf-8",
        )
        signer.chmod(0o755)
        self.run_git("config", "--local", "commit.gpgSign", "true", root=self.fork)
        self.run_git("config", "--local", "gpg.program", str(signer), root=self.fork)
        return marker

    def run_upgrade(self, *arguments, cwd=None):
        if self.trace_path.exists():
            self.trace_path.unlink()
        environment = dict(self.environment)
        environment["GIT_TRACE2_EVENT"] = str(self.trace_path)
        return subprocess.run(
            [str(MODULE_DIR / "upgrade"), *arguments],
            cwd=cwd or self.fork,
            env=environment,
            capture_output=True,
            text=True,
        )

    def source_snapshot(self):
        return (
            self.run_git("rev-parse", "HEAD", root=self.fork).stdout,
            self.run_git(
                "status", "--porcelain=v2", "-z", root=self.fork
            ).stdout,
            self.run_git("ls-files", "--stage", "-z", root=self.fork).stdout,
        )

    def origin_refs(self):
        return self.run_git(
            "--git-dir",
            str(self.origin_bare),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            root=None,
        ).stdout

    @property
    def state_path(self):
        common = self.run_git(
            "rev-parse", "--git-common-dir", root=self.fork
        ).stdout.strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = self.fork / common_path
        return common_path.resolve() / "steadflow-upstream-sync" / "state.json"

    @contextlib.contextmanager
    def hold_upgrade_lock(self):
        state_directory = self.state_path.parent
        state_directory.mkdir(mode=0o700, exist_ok=True)
        lock_path = state_directory / "upgrade.lock"
        with lock_path.open("a+b") as lock_file:
            lock_path.chmod(0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def worktree_path(self, release):
        return self.fork / ".worktrees" / f"upgrade-{release}"

    def completed_state_path(self, release):
        return self.state_path.parent / f"completed-{release}.json"

    def environment_with_preflight_barrier(self, marker):
        real_git = shutil.which("git", path=self.environment["PATH"])
        if real_git is None:
            raise AssertionError("fixture Git executable is missing")
        observer_directory = self.root / "git-preflight-observer"
        observer_directory.mkdir()
        observer = observer_directory / "git"
        observer.write_text(
            "#!/bin/sh\n"
            "case \" $* \" in\n"
            f"  *\" remote get-url upstream \"*) : > {shlex.quote(str(marker))};;\n"
            "esac\n"
            f"exec {shlex.quote(real_git)} \"$@\"\n",
            encoding="utf-8",
        )
        observer.chmod(0o755)
        environment = dict(self.environment)
        environment["PATH"] = (
            str(observer_directory) + os.pathsep + environment["PATH"]
        )
        return environment

    def add_release(self, release, *, annotated=True, conflict=False):
        relative = "shared.txt" if conflict else f"upstream-{release}.txt"
        (self.official / relative).write_text(
            f"official {release}\n", encoding="utf-8"
        )
        self.run_git("add", relative, root=self.official)
        self.run_git("commit", "-m", f"official {release}", root=self.official)
        commit = self.run_git("rev-parse", "HEAD", root=self.official).stdout.strip()
        if annotated:
            self.run_git(
                "tag", "-a", release, "-m", release, root=self.official
            )
        else:
            self.run_git("tag", release, root=self.official)
        tag_object = self.run_git(
            "rev-parse", f"refs/tags/{release}", root=self.official
        ).stdout.strip()
        self.run_git(
            "push",
            "--quiet",
            "upstream",
            "HEAD:refs/heads/main",
            f"refs/tags/{release}:refs/tags/{release}",
            root=self.official,
        )
        return commit, tag_object

    def create_source_conflict(self, *, register_seam=True):
        manifest_path = self.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data"]["paths"].append("shared.txt")
        manifest["data"]["paths"].sort()
        if register_seam:
            manifest["shared_seams"] = ["shared.txt"]
        self.write_json(".steadflow/customization.yml", manifest)
        (self.fork / "shared.txt").write_text(
            "steadflow source\n", encoding="utf-8"
        )
        paths = ["shared.txt", ".steadflow/customization.yml"]
        self.run_git("add", *paths, root=self.fork)
        self.run_git(
            "commit", "-m", "steadflow shared customization", root=self.fork
        )
        self.source_commit = self.run_git(
            "rev-parse", "HEAD", root=self.fork
        ).stdout.strip()

    def update_lock(self, **updates):
        lock_path = self.fork / ".steadflow" / "upstream-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock.update(updates)
        self.write_json(".steadflow/upstream-lock.json", lock)
        paths = [".steadflow/upstream-lock.json"]
        if "release" in updates:
            known_path = self.fork / ".steadflow" / "known-failures.yml"
            known = json.loads(known_path.read_text(encoding="utf-8"))
            known["baseline"]["release"] = updates["release"]
            self.write_json(".steadflow/known-failures.yml", known)
            paths.append(".steadflow/known-failures.yml")
        self.run_git("add", *paths, root=self.fork)
        self.run_git("commit", "-m", "update fixture lock", root=self.fork)
        self.source_commit = self.run_git(
            "rev-parse", "HEAD", root=self.fork
        ).stdout.strip()

    def add_noncommit_release(self, release):
        blob = self.run_git(
            "hash-object",
            "-w",
            "--stdin",
            root=self.official,
            input_text="not a commit\n",
        ).stdout.strip()
        self.run_git(
            "update-ref", f"refs/tags/{release}", blob, root=self.official
        )
        self.run_git(
            "push",
            "--quiet",
            "upstream",
            f"refs/tags/{release}:refs/tags/{release}",
            root=self.official,
        )
        return blob

    def move_release_tag(self, release):
        relative = f"moved-{release}.txt"
        (self.official / relative).write_text("moved tag\n", encoding="utf-8")
        self.run_git("add", relative, root=self.official)
        self.run_git("commit", "-m", f"move {release}", root=self.official)
        self.run_git(
            "tag", "-f", "-a", release, "-m", f"moved {release}",
            root=self.official,
        )
        moved_object = self.run_git(
            "rev-parse", f"refs/tags/{release}", root=self.official
        ).stdout.strip()
        self.run_git(
            "push",
            "--quiet",
            "--force",
            "upstream",
            f"refs/tags/{release}:refs/tags/{release}",
            root=self.official,
        )
        return moved_object

    def read_state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def trace_argv(self):
        commands = []
        for line in self.trace_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "child_start" and "argv" in event:
                commands.append(event["argv"])
        return commands


class UpgradeCurrentVerificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_verify_current_is_read_only_and_reports_validation_scope(self):
        source_before = self.fixture.source_snapshot()
        origin_before = self.fixture.origin_refs()

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("PASS", completed.stdout)
        self.assertIn("added migrations: none", completed.stdout)
        self.assertIn(
            "scope: manifest ownership, migration integrity, known-failure schema, "
            "and locked ancestry",
            completed.stdout,
        )
        self.assertNotIn("deferred until Task 7", completed.stdout)
        self.assertIn("customization_sha256=", completed.stdout)
        self.assertIn("known_failures_sha256=", completed.stdout)
        self.assertEqual(self.fixture.source_snapshot(), source_before)
        self.assertEqual(self.fixture.origin_refs(), origin_before)
        self.assertFalse(self.fixture.state_path.exists())
        self.assertFalse((self.fixture.fork / ".worktrees").exists())

    def test_current_locked_release_is_verified_no_op_without_fetch_or_state(self):
        source_before = self.fixture.source_snapshot()
        origin_before = self.fixture.origin_refs()

        completed = self.fixture.run_upgrade(self.fixture.current_release)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("already current", completed.stdout)
        self.assertEqual(self.fixture.source_snapshot(), source_before)
        self.assertEqual(self.fixture.origin_refs(), origin_before)
        self.assertFalse(self.fixture.state_path.exists())
        self.assertFalse((self.fixture.fork / ".worktrees").exists())
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/steadflow-upstream/releases/{self.fixture.current_release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_verify_current_reports_added_migration_without_blocking(self):
        added = self.fixture.fork / "backend" / "migrations" / "002_added.sql"
        added.write_text("SELECT 2;\n", encoding="utf-8")
        manifest_path = self.fixture.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data"]["paths"].append("backend/migrations/002_added.sql")
        manifest["data"]["paths"].sort()
        self.fixture.write_json(".steadflow/customization.yml", manifest)
        self.fixture.run_git(
            "add", str(added), str(manifest_path), root=self.fixture.fork
        )
        self.fixture.run_git(
            "commit", "-m", "add fixture migration", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("backend/migrations/002_added.sql", completed.stdout)

    def test_verify_current_blocks_real_fork_diff_path_missing_from_manifest(self):
        unregistered = self.fixture.fork / "unregistered.txt"
        unregistered.write_text("not declared\n", encoding="utf-8")
        self.fixture.run_git("add", "unregistered.txt", root=self.fixture.fork)
        self.fixture.run_git(
            "commit", "-m", "add undeclared customization", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("unowned paths: unregistered.txt", completed.stderr)

    def test_verify_current_treats_rename_as_deleted_and_added_regular_paths(self):
        self.fixture.run_git("mv", "shared.txt", "renamed-shared.txt", root=self.fixture.fork)
        manifest_path = self.fixture.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data"]["paths"].extend(["renamed-shared.txt", "shared.txt"])
        manifest["data"]["paths"].sort()
        self.fixture.write_json(".steadflow/customization.yml", manifest)
        self.fixture.run_git("add", ".steadflow/customization.yml", root=self.fixture.fork)
        self.fixture.run_git("commit", "-m", "rename fixture path", root=self.fixture.fork)

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_verify_current_rejects_symlink_in_authenticated_fork_diff(self):
        (self.fixture.fork / "linked.txt").symlink_to("fork.txt")
        manifest_path = self.fixture.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data"]["paths"].append("linked.txt")
        manifest["data"]["paths"].sort()
        self.fixture.write_json(".steadflow/customization.yml", manifest)
        self.fixture.run_git(
            "add", "linked.txt", ".steadflow/customization.yml", root=self.fixture.fork
        )
        self.fixture.run_git("commit", "-m", "add fixture symlink", root=self.fixture.fork)

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("not a regular file: linked.txt", completed.stderr)

    def test_verify_current_uses_repo_root_when_invoked_from_subdirectory(self):
        source_before = self.fixture.source_snapshot()

        completed = self.fixture.run_upgrade(
            "--verify-current", cwd=self.fixture.fork / "backend"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.fixture.source_snapshot(), source_before)
        self.assertFalse(self.fixture.state_path.exists())

    def test_verify_current_does_not_refresh_index_after_tracked_file_mtime_change(self):
        tracked = self.fixture.fork / "fork.txt"
        original_stat = tracked.stat()
        os.utime(
            tracked,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
        )
        index_path = self.fixture.fork / ".git" / "index"
        index_before = index_path.read_bytes()
        protected_before = {
            "config": (self.fixture.fork / ".git" / "config").read_bytes(),
            "refs": self.fixture.run_git(
                "for-each-ref", "--format=%(refname) %(objectname)",
                root=self.fixture.fork,
            ).stdout,
            "worktrees": self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout,
        }

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual(
            (self.fixture.fork / ".git" / "config").read_bytes(),
            protected_before["config"],
        )
        self.assertEqual(
            self.fixture.run_git(
                "for-each-ref", "--format=%(refname) %(objectname)",
                root=self.fixture.fork,
            ).stdout,
            protected_before["refs"],
        )
        self.assertEqual(
            self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout,
            protected_before["worktrees"],
        )


class UpgradeCleanMergeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_initial_merge_never_invokes_configured_gpg_signer(self):
        release = "v1.1.0"
        self.fixture.add_release(release)
        marker = self.fixture.install_forbidden_gpg_signer()

        completed = self.fixture.run_upgrade(release)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists())

    def test_annotated_release_creates_isolated_merge_candidate_only(self):
        release = "v1.1.0"
        target_commit, tag_object = self.fixture.add_release(release)
        source_before = self.fixture.source_snapshot()
        origin_before = self.fixture.origin_refs()
        local_tag_before = self.fixture.run_git(
            "show-ref", "--verify", f"refs/tags/{release}",
            root=self.fixture.fork,
            check=False,
        ).returncode
        self.assertNotEqual(local_tag_before, 0)

        repository = self.fixture.repository()
        candidate_state = create_upgrade_candidate(repository, release)

        self.assertEqual(candidate_state["phase"], "merged")
        self.assertEqual(self.fixture.source_snapshot(), source_before)
        self.assertEqual(self.fixture.origin_refs(), origin_before)
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/tags/{release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )
        internal_ref = f"refs/steadflow-upstream/releases/{release}"
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", internal_ref, root=self.fixture.fork
            ).stdout.strip(),
            tag_object,
        )
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", f"{internal_ref}^{{commit}}", root=self.fixture.fork
            ).stdout.strip(),
            target_commit,
        )

        report_path = (
            self.fixture.worktree_path(release)
            / ".steadflow"
            / "reports"
            / f"{release}.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["source_commit"], self.fixture.source_commit)
        self.assertEqual(report["tag_object"], tag_object)
        self.assertEqual(report["peeled_commit"], target_commit)
        self.assertRegex(report["upstream_tree"], r"^[0-9a-f]{40}$")
        self.assertEqual(report["ownership"]["status"], "PASS")
        self.assertEqual(report["ownership"]["changed"], report["ownership"]["registered"])
        self.assertEqual(report["seams"]["conflicts"], [])
        self.assertEqual(
            report["file_changes"]["upstream"]["counts"]["total"], 1
        )

        state = self.fixture.read_state()
        worktree = self.fixture.worktree_path(release).resolve()
        self.assertEqual(
            {key: value for key, value in state.items() if key != "evidence"},
            {
                "branch": f"upgrade/{release}",
                "internal_ref": internal_ref,
                "peeled_commit": target_commit,
                "phase": "merged",
                "release": release,
                "schema_version": 1,
                "source_branch": "main",
                "source_commit": self.fixture.source_commit,
                "tag_object": tag_object,
                "worktree": str(worktree),
            },
        )
        self.assertEqual(set(state["evidence"]), EVIDENCE_KEYS)
        self.assertEqual(stat.S_IMODE(self.fixture.state_path.stat().st_mode), 0o600)
        self.assertEqual(
            self.fixture.run_git(
                "symbolic-ref", "--short", "HEAD", root=worktree
            ).stdout.strip(),
            f"upgrade/{release}",
        )
        candidate = self.fixture.run_git(
            "rev-parse", "HEAD", root=worktree
        ).stdout.strip()
        parents = self.fixture.run_git(
            "show", "-s", "--format=%P", candidate, root=worktree
        ).stdout.split()
        self.assertEqual(parents, [state["evidence"]["validated_head"]])
        validated_parents = self.fixture.run_git(
            "show",
            "-s",
            "--format=%P",
            state["evidence"]["validated_head"],
            root=worktree,
        ).stdout.split()
        self.assertEqual(len(validated_parents), 1)
        merge_parents = self.fixture.run_git(
            "show",
            "-s",
            "--format=%P",
            validated_parents[0],
            root=worktree,
        ).stdout.split()
        self.assertEqual(
            merge_parents, [self.fixture.source_commit, target_commit]
        )
        self.assertEqual(
            self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout.count("worktree "),
            2,
        )
        self.assertEqual(
            self.fixture.run_git(
                "check-ignore", str(worktree), root=self.fixture.fork
            ).returncode,
            0,
        )
        traced = repository.commands
        self.assertTrue(
            any(
                "fetch" in argv
                and "upstream" in argv
                and f"refs/tags/{release}:{internal_ref}" in argv
                for argv in traced
            ),
            traced,
        )
        self.assertTrue(
            any(
                "merge" in argv
                and "--no-ff" in argv
                and "--no-edit" in argv
                and target_commit in argv
                for argv in traced
            ),
            traced,
        )
        forbidden_git_words = {
            "push",
            "tag",
            "reset",
            "--hard",
            "--ours",
            "--theirs",
        }
        forbidden_programs = {"docker", "kubectl", "ssh", "rsync", "gh"}
        for argv in traced:
            self.assertTrue(forbidden_git_words.isdisjoint(argv), argv)
            self.assertTrue(forbidden_programs.isdisjoint(argv), argv)

    def test_lightweight_release_is_peeled_without_creating_local_tag(self):
        release = "v1.2.0"
        target_commit, tag_object = self.fixture.add_release(
            release, annotated=False
        )
        self.assertEqual(tag_object, target_commit)

        completed = self.fixture.run_upgrade(release)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state = self.fixture.read_state()
        self.assertEqual(state["peeled_commit"], target_commit)
        self.assertEqual(state["tag_object"], target_commit)
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/tags/{release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_worktree_merge_and_report_commit_hooks_are_not_executed(self):
        release = "v1.3.0"
        self.fixture.add_release(release)
        hooks = self.fixture.fork / ".git" / "hooks"
        checkout_marker = self.fixture.root / "post-checkout-ran"
        merge_marker = self.fixture.root / "post-merge-ran"
        pre_commit_marker = self.fixture.root / "pre-commit-ran"
        commit_message_marker = self.fixture.root / "commit-msg-ran"
        post_commit_marker = self.fixture.root / "post-commit-ran"
        for hook_name, marker in (
            ("post-checkout", checkout_marker),
            ("post-merge", merge_marker),
            ("pre-commit", pre_commit_marker),
            ("commit-msg", commit_message_marker),
            ("post-commit", post_commit_marker),
        ):
            hook = hooks / hook_name
            hook.write_text(
                "#!/bin/sh\n"
                f"printf 'hook ran\\n' > {shlex.quote(str(marker))}\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)

        completed = self.fixture.run_upgrade(release)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(checkout_marker.exists())
        self.assertFalse(merge_marker.exists())
        self.assertFalse(pre_commit_marker.exists())
        self.assertFalse(commit_message_marker.exists())
        self.assertFalse(post_commit_marker.exists())


class UpgradeCandidateValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.release = "v1.1.0"
        self.fixture.add_release(self.release)

    def tearDown(self):
        self.fixture.cleanup()

    def report_path(self):
        return (
            self.fixture.worktree_path(self.release)
            / ".steadflow"
            / "reports"
            / f"{self.release}.json"
        )

    def test_clean_merge_runs_all_validation_stages_before_phase_merged(self):
        repository = self.fixture.repository()
        calls = []
        base_runner = self.fixture.validation_runner

        def recording_runner(argv, **kwargs):
            calls.append((list(argv), Path(kwargs["cwd"]).resolve()))
            return base_runner(argv, **kwargs)

        repository.validation_runner = recording_runner

        state = create_upgrade_candidate(repository, self.release)

        worktree = self.fixture.worktree_path(self.release).resolve()
        self.assertEqual(state["phase"], "merged")

        self.assertEqual(
            [cwd for _, cwd in calls],
            [worktree / "backend", worktree, worktree, worktree / "backend", worktree],
        )
        self.assertEqual(calls[0][0], ["go", "test", "-json", "./..."])
        self.assertIn("vitest", calls[1][0])
        self.assertEqual(calls[2][0], ["pnpm", "--dir", "frontend", "run", "build"])
        self.assertEqual(
            calls[3][0][:3],
            ["go", "test", "-json"],
        )
        self.assertEqual(calls[4][0], ["make", "-C", "backend", "generate"])
        report = json.loads(self.report_path().read_text(encoding="utf-8"))
        candidate_head = self.fixture.run_git(
            "rev-parse", "HEAD", root=worktree
        ).stdout.strip()
        candidate_tree = self.fixture.run_git(
            "rev-parse", "HEAD^{tree}", root=worktree
        ).stdout.strip()
        parents = self.fixture.run_git(
            "show", "-s", "--format=%P", "HEAD", root=worktree
        ).stdout.split()
        changed = self.fixture.run_git(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD", root=worktree
        ).stdout.splitlines()
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(parents, [report["candidate_head"]])
        self.assertEqual(
            report["candidate_tree"],
            self.fixture.run_git(
                "rev-parse", f"{parents[0]}^{{tree}}", root=worktree
            ).stdout.strip(),
        )
        self.assertEqual(
            changed,
            [
                f".steadflow/reports/{self.release}.json",
                f".steadflow/reports/{self.release}.md",
            ],
        )
        self.assertEqual(
            self.fixture.run_git(
                "status", "--porcelain=v2", root=worktree
            ).stdout,
            "",
        )
        self.assertEqual(
            sorted(report["configuration_hashes"]),
            ["customization", "known_failures", "lock", "migrations"],
        )
        self.assertTrue(self.report_path().with_suffix(".md").is_file())
        self.assertNotEqual(
            self.fixture.run_git(
                "check-ignore", str(self.report_path()), root=worktree, check=False
            ).returncode,
            0,
        )
        evidence = self.fixture.read_state()["evidence"]
        self.assertEqual(evidence["candidate_head"], candidate_head)
        self.assertEqual(evidence["candidate_tree"], candidate_tree)
        self.assertEqual(evidence["validated_head"], report["candidate_head"])
        self.assertEqual(evidence["validated_tree"], report["candidate_tree"])
        self.assertEqual(
            sorted(evidence["report_blobs"]),
            ["json", "markdown"],
        )
        validated_date = self.fixture.run_git(
            "show",
            "-s",
            "--format=%cI",
            evidence["validated_head"],
            root=worktree,
        ).stdout.strip()
        commit_metadata = self.fixture.run_git(
            "show",
            "-s",
            "--format=%an <%ae>|%cn <%ce>|%aI|%cI",
            evidence["candidate_head"],
            root=worktree,
        ).stdout.strip()
        self.assertEqual(
            commit_metadata,
            "Steadflow Upgrade <upgrade@steadflow.invalid>|"
            "Steadflow Upgrade <upgrade@steadflow.invalid>|"
            f"{validated_date}|{validated_date}",
        )

    def test_success_advances_every_certified_baseline_before_report_commit(self):
        migration_path = (
            self.fixture.official
            / "backend"
            / "migrations"
            / "002_additive.sql"
        )
        migration_path.write_text(
            "CREATE TABLE fixture_additive (id bigint);\n", encoding="utf-8"
        )
        self.fixture.run_git(
            "add", "backend/migrations/002_additive.sql", root=self.fixture.official
        )
        target_commit, _ = self.fixture.add_release("v1.2.0")

        state = create_upgrade_candidate(self.fixture.repository(), "v1.2.0")

        worktree = Path(state["worktree"])
        report_commit = self.fixture.run_git(
            "rev-parse", "HEAD", root=worktree
        ).stdout.strip()
        baseline_commit = self.fixture.run_git(
            "rev-parse", "HEAD^", root=worktree
        ).stdout.strip()
        self.assertEqual(
            self.fixture.run_git(
                "show", "-s", "--format=%s", baseline_commit, root=worktree
            ).stdout.strip(),
            "chore(upstream): advance baseline to v1.2.0",
        )
        self.assertEqual(
            sorted(
                self.fixture.run_git(
                    "diff-tree",
                    "--no-commit-id",
                    "--name-only",
                    "-r",
                    baseline_commit,
                    root=worktree,
                ).stdout.splitlines()
            ),
            [
                ".steadflow/customization.yml",
                ".steadflow/known-failures.yml",
                ".steadflow/migration-checksums.json",
                ".steadflow/upstream-lock.json",
            ],
        )

        lock = json.loads(
            (worktree / ".steadflow" / "upstream-lock.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(lock["release"], "v1.2.0")
        self.assertEqual(lock["peeled_commit"], target_commit)
        self.assertEqual(
            lock["tree"],
            self.fixture.run_git(
                "rev-parse", f"{target_commit}^{{tree}}", root=worktree
            ).stdout.strip(),
        )

        known = json.loads(
            (worktree / ".steadflow" / "known-failures.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(known["baseline"]["release"], "v1.2.0")
        self.assertEqual(
            known["baseline"]["result"],
            {"failed": 0, "go_failed": 0, "vitest_failed": 0},
        )
        self.assertTrue(
            all("v1.2.0" in command for command in known["baseline"]["commands"])
        )
        self.assertNotEqual(
            known["baseline"]["evidence"]["go_json_sha256"], "a" * 64
        )
        self.assertNotEqual(
            known["baseline"]["evidence"]["vitest_json_sha256"], "b" * 64
        )

        migrations = json.loads(
            (worktree / ".steadflow" / "migration-checksums.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            migrations["migrations"]["backend/migrations/002_additive.sql"],
            hashlib.sha256(migration_path.read_bytes()).hexdigest(),
        )

        manifest = json.loads(
            (worktree / ".steadflow" / "customization.yml").read_text(
                encoding="utf-8"
            )
        )
        final_changes = _git_change_summary(
            self.fixture.repository(), target_commit, report_commit, worktree
        )
        ownership = validate_manifest(
            manifest, _summary_paths(final_changes), require_exact=True
        )
        self.assertEqual(ownership["unowned"], [])
        self.assertIn(
            ".steadflow/reports/v1.2.0.json",
            manifest["integration_adapter"]["paths"],
        )

    def test_database_uri_secret_is_redacted_from_exception_json_and_markdown(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner
        secret = "provider-password"
        diagnostic = f"postgresql://user:{secret}@db.example:5432/app"

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps({"Action": "fail", "Package": "fixture/pkg"})
                    + "\n",
                    stderr=diagnostic,
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = failing_runner
        with self.assertRaises(UpgradeBlocked) as raised:
            create_upgrade_candidate(repository, self.release)

        json_bytes = self.report_path().read_bytes()
        markdown_bytes = self.report_path().with_suffix(".md").read_bytes()
        combined = str(raised.exception).encode("utf-8") + json_bytes + markdown_bytes
        self.assertNotIn(secret.encode("utf-8"), combined)
        self.assertIn(b"db.example:5432/app", combined)

    def test_validation_failure_preserves_validating_state_and_continue_retries(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps(
                        {"Action": "fail", "Package": "fixture/pkg"}
                    )
                    + "\n",
                    stderr="Authorization: Bearer candidate-secret",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = failing_runner

        with self.assertRaises(UpgradeBlocked):
            create_upgrade_candidate(repository, self.release)

        state = self.fixture.read_state()
        self.assertEqual(state["phase"], "validating")
        report_text = self.report_path().read_text(encoding="utf-8")
        self.assertIn('"status": "BLOCKED"', report_text)
        self.assertNotIn("candidate-secret", report_text)
        self.assertEqual(
            self.fixture.run_git(
                "status", "--porcelain=v1", root=self.fixture.worktree_path(self.release)
            ).stdout.splitlines(),
            ["?? .steadflow/reports/"],
        )

        repository.validation_runner = base_runner
        resumed = resume_upgrade(repository)

        self.assertEqual(resumed["phase"], "merged")
        self.assertEqual(
            json.loads(self.report_path().read_text(encoding="utf-8"))["status"],
            "PASS",
        )
        self.assertEqual(
            self.fixture.run_git(
                "status", "--porcelain=v2", root=self.fixture.worktree_path(self.release)
            ).stdout,
            "",
        )

    def test_full_go_gate_rejects_zero_executed_tests(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner

        def zero_go_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {"Action": "pass", "Package": "fixture/no-tests"}
                    )
                    + "\n",
                    stderr="",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = zero_go_runner

        with self.assertRaisesRegex(UpgradeBlocked, "full Go executed zero tests"):
            create_upgrade_candidate(repository, self.release)

        self.assertEqual(self.fixture.read_state()["phase"], "validating")

    def test_full_vitest_gate_rejects_zero_executed_tests(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner

        def zero_vitest_runner(argv, **kwargs):
            if "vitest" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps({"success": True, "testResults": []}),
                    stderr="",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = zero_vitest_runner

        with self.assertRaisesRegex(
            UpgradeBlocked, "full Vitest executed zero tests"
        ):
            create_upgrade_candidate(repository, self.release)

        self.assertEqual(self.fixture.read_state()["phase"], "validating")

    def test_validating_continue_rejects_dirt_outside_exact_report_paths(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps({"Action": "fail", "Package": "fixture/pkg"})
                    + "\n",
                    stderr="",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = failing_runner
        with self.assertRaises(UpgradeBlocked):
            create_upgrade_candidate(repository, self.release)
        worktree = self.fixture.worktree_path(self.release)
        (worktree / "unexpected.txt").write_text("dirt\n", encoding="utf-8")
        repository.validation_runner = base_runner

        with self.assertRaisesRegex(
            UpgradeBlocked, "outside exact candidate report paths"
        ):
            resume_upgrade(repository)

        self.assertEqual(self.fixture.read_state()["phase"], "validating")

    def test_validating_continue_rejects_symlinked_report_file(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps({"Action": "fail", "Package": "fixture/pkg"})
                    + "\n",
                    stderr="",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = failing_runner
        with self.assertRaises(UpgradeBlocked):
            create_upgrade_candidate(repository, self.release)
        outside = self.fixture.root / "outside-report.json"
        outside.write_text("outside\n", encoding="utf-8")
        self.report_path().unlink()
        self.report_path().symlink_to(outside)
        repository.validation_runner = base_runner

        with self.assertRaisesRegex(UpgradeBlocked, "report file must be regular"):
            resume_upgrade(repository)

        self.assertEqual(outside.read_text(encoding="utf-8"), "outside\n")
        self.assertEqual(self.fixture.read_state()["phase"], "validating")

    def test_report_write_failure_preserves_validating_state_and_evidence(self):
        repository = self.fixture.repository()
        base_runner = self.fixture.validation_runner
        outside = self.fixture.root / "outside-reports"
        outside.mkdir()

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                reports = (
                    self.fixture.worktree_path(self.release)
                    / ".steadflow"
                    / "reports"
                )
                reports.symlink_to(outside, target_is_directory=True)
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps({"Action": "fail", "Package": "fixture/pkg"})
                    + "\n",
                    stderr="token=report-write-secret",
                )
            return base_runner(argv, **kwargs)

        repository.validation_runner = failing_runner

        with self.assertRaises(UpgradeBlocked) as raised:
            create_upgrade_candidate(repository, self.release)

        self.assertIn("validation and report failed", str(raised.exception))
        self.assertNotIn("report-write-secret", str(raised.exception))
        self.assertEqual(self.fixture.read_state()["phase"], "validating")
        self.assertTrue(
            (
                self.fixture.worktree_path(self.release)
                / ".steadflow"
                / "reports"
            ).is_symlink()
        )
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_merged_candidate_requires_bound_success_report(self):
        repository = self.fixture.repository()
        create_upgrade_candidate(repository, self.release)
        report = json.loads(self.report_path().read_text(encoding="utf-8"))
        report["candidate_head"] = "0" * 40
        self.report_path().write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.fixture.run_git(
            "add", str(self.report_path()), root=self.fixture.worktree_path(self.release)
        )
        self.fixture.run_git(
            "commit", "-m", "tamper report", root=self.fixture.worktree_path(self.release)
        )

        with self.assertRaisesRegex(
            UpgradeBlocked, "summary digest|evidence binding"
        ):
            create_upgrade_candidate(repository, self.release)

    def test_forged_canonical_source_identity_is_rejected_after_state_rebinding(self):
        repository = self.fixture.repository()
        create_upgrade_candidate(repository, self.release)
        worktree = self.fixture.worktree_path(self.release)
        report_path = self.report_path()
        markdown_path = report_path.with_suffix(".md")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["source_commit"] = "0" * 40
        summary = dict(report)
        summary.pop("validation_summary_sha256")
        report["validation_summary_sha256"] = hashlib.sha256(
            json.dumps(summary, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        json_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")
        markdown_bytes = (
            f"# Steadflow upstream report: {self.release}\n\n```json\n"
            + json_bytes.decode("utf-8").rstrip("\n")
            + "\n```\n"
        ).encode("utf-8")
        report_path.write_bytes(json_bytes)
        markdown_path.write_bytes(markdown_bytes)
        self.fixture.run_git(
            "add",
            f".steadflow/reports/{self.release}.json",
            f".steadflow/reports/{self.release}.md",
            root=worktree,
        )
        self.fixture.run_git("commit", "--amend", "--no-edit", root=worktree)
        state = self.fixture.read_state()
        evidence = state["evidence"]
        evidence["candidate_head"] = self.fixture.run_git(
            "rev-parse", "HEAD", root=worktree
        ).stdout.strip()
        evidence["candidate_tree"] = self.fixture.run_git(
            "rev-parse", "HEAD^{tree}", root=worktree
        ).stdout.strip()
        evidence["report_blobs"] = {
            "json": self.fixture.run_git(
                "rev-parse", f"HEAD:.steadflow/reports/{self.release}.json", root=worktree
            ).stdout.strip(),
            "markdown": self.fixture.run_git(
                "rev-parse", f"HEAD:.steadflow/reports/{self.release}.md", root=worktree
            ).stdout.strip(),
        }
        evidence["report_content_sha256"] = {
            "json": hashlib.sha256(json_bytes).hexdigest(),
            "markdown": hashlib.sha256(markdown_bytes).hexdigest(),
        }
        evidence["validation_summary_sha256"] = report["validation_summary_sha256"]
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        with self.assertRaisesRegex(UpgradeBlocked, "source_commit binding is invalid"):
            resume_upgrade(repository)

    def assert_complete_validation_calls(self, calls, critical_count=1):
        self.assertEqual(calls[0], ["go", "test", "-json", "./..."])
        self.assertIn("vitest", calls[1])
        self.assertEqual(calls[2], ["pnpm", "--dir", "frontend", "run", "build"])
        critical = calls[3 : 3 + critical_count]
        self.assertEqual(len(critical), critical_count)
        for index, argv in enumerate(critical, 1):
            self.assertEqual(argv[:3], ["go", "test", "-json"])
            if critical_count == 6:
                self.assertIn(f"./fixture/{index}", argv)
        self.assertEqual(
            calls[3 + critical_count], ["make", "-C", "backend", "generate"]
        )
        self.assertEqual(len(calls), 4 + critical_count)

    def test_validating_phase_report_commit_still_reruns_every_gate(self):
        repository = self.fixture.repository()
        create_upgrade_candidate(repository, self.release)
        state = self.fixture.read_state()
        state["phase"] = "validating"
        state.pop("evidence")
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        calls = []
        base_runner = self.fixture.validation_runner

        def recording_runner(argv, **kwargs):
            calls.append(list(argv))
            return base_runner(argv, **kwargs)

        repository.validation_runner = recording_runner

        resumed = resume_upgrade(repository)

        self.assertEqual(resumed["phase"], "merged")
        self.assert_complete_validation_calls(calls)

    def test_state_evidence_requires_exact_hash_mapping_keys(self):
        repository = self.fixture.repository()
        create_upgrade_candidate(repository, self.release)
        for field, extra_key in (
            ("configuration_hashes", "unexpected_configuration"),
            ("report_blobs", "unexpected_report"),
        ):
            with self.subTest(field=field):
                state = self.fixture.read_state()
                state["evidence"][field][extra_key] = (
                    "0" * 64 if field == "configuration_hashes" else "0" * 40
                )
                self.fixture.state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(
                    UpgradeBlocked, f"evidence {field} is invalid"
                ):
                    resume_upgrade(repository)

                state["evidence"][field].pop(extra_key)
                self.fixture.state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

    def test_state_schema_allows_only_final_evidence_on_merged_phase(self):
        repository = self.fixture.repository()
        create_upgrade_candidate(repository, self.release)
        original = self.fixture.read_state()
        mutations = (
            ("merged-without-evidence", lambda state: state.pop("evidence")),
            ("validating-with-evidence", lambda state: state.__setitem__("phase", "validating")),
            ("pending-key", lambda state: state.__setitem__("pending_evidence", {})),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                state = json.loads(json.dumps(original))
                mutate(state)
                self.fixture.state_path.write_text(
                    json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaises(UpgradeBlocked):
                    resume_upgrade(repository)

        self.fixture.state_path.write_text(
            json.dumps(original, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_success_report_has_exact_complete_schema_and_derived_markdown(self):
        create_upgrade_candidate(self.fixture.repository(), self.release)
        json_path = self.report_path()
        markdown_path = json_path.with_suffix(".md")
        json_bytes = json_path.read_bytes()
        report = json.loads(json_bytes)

        self.assertEqual(
            set(report),
            {
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
            },
        )
        self.assertEqual(report["schema_version"], 2)
        summary = dict(report)
        summary_digest = summary.pop("validation_summary_sha256")
        expected_digest = hashlib.sha256(
            json.dumps(
                summary,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(summary_digest, expected_digest)
        expected_markdown = (
            f"# Steadflow upstream report: {self.release}\n\n"
            "```json\n"
            + json_bytes.decode("utf-8").rstrip("\n")
            + "\n```\n"
        ).encode("utf-8")
        self.assertEqual(markdown_path.read_bytes(), expected_markdown)

    def test_forged_five_field_report_commit_without_pending_reruns_validation(self):
        repository = self.fixture.repository()
        created = create_upgrade_candidate(repository, self.release)
        state = self.fixture.read_state()
        worktree = self.fixture.worktree_path(self.release)
        validated_head = state["evidence"]["validated_head"]
        self.fixture.run_git("reset", "--hard", validated_head, root=worktree)
        report_path = self.report_path()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        forged = {
            "candidate_head": validated_head,
            "candidate_tree": self.fixture.run_git(
                "rev-parse", f"{validated_head}^{{tree}}", root=worktree
            ).stdout.strip(),
            "configuration_hashes": state["evidence"]["configuration_hashes"],
            "release": self.release,
            "status": "PASS",
        }
        report_path.write_text(
            json.dumps(forged, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        report_path.with_suffix(".md").write_text(
            "# forged\n", encoding="utf-8"
        )
        self.fixture.run_git("add", ".steadflow/reports", root=worktree)
        self.fixture.run_git("commit", "-m", "forged report", root=worktree)
        state["phase"] = "validating"
        state.pop("evidence")
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        calls = []
        base_runner = self.fixture.validation_runner

        def recording_runner(argv, **kwargs):
            calls.append(list(argv))
            return base_runner(argv, **kwargs)

        repository.validation_runner = recording_runner

        resumed = resume_upgrade(repository)

        self.assertEqual(resumed["phase"], "merged")
        self.assert_complete_validation_calls(calls)
        self.assertEqual(
            json.loads(self.report_path().read_text(encoding="utf-8"))["schema_version"],
            2,
        )

    def test_every_report_crash_point_reruns_the_complete_pipeline(self):
        events = (
            "after_baseline_files",
            "after_baseline_index",
            "after_baseline_commit",
            "before_success_reports",
            "after_json_report",
            "after_success_reports",
            "after_report_index",
            "after_evidence_commit",
        )
        for event in events:
            with self.subTest(event=event):
                fixture = HermeticUpgradeFixture()
                try:
                    release = "v1.1.0"
                    fixture.configure_six_critical_commands()
                    fixture.add_release(release)
                    repository = fixture.repository()

                    def crash(observed, **kwargs):
                        if observed == event:
                            raise UpgradeBlocked(f"simulated crash at {event}")

                    repository.upgrade_test_hook = crash
                    with self.assertRaisesRegex(UpgradeBlocked, "simulated crash"):
                        create_upgrade_candidate(repository, release)
                    self.assertEqual(fixture.read_state()["phase"], "validating")
                    calls = []
                    base_runner = fixture.validation_runner

                    def recording_runner(argv, **kwargs):
                        calls.append(list(argv))
                        return base_runner(argv, **kwargs)

                    repository.upgrade_test_hook = lambda *args, **kwargs: None
                    repository.validation_runner = recording_runner
                    resumed = resume_upgrade(repository)

                    self.assertEqual(resumed["phase"], "merged")
                    self.assert_complete_validation_calls(calls, critical_count=6)
                finally:
                    fixture.cleanup()

    def test_validation_failure_after_report_commit_remains_validating(self):
        repository = self.fixture.repository()

        def crash(event, **kwargs):
            if event == "after_evidence_commit":
                raise UpgradeBlocked("simulated crash after evidence commit")

        repository.upgrade_test_hook = crash
        with self.assertRaises(UpgradeBlocked):
            create_upgrade_candidate(repository, self.release)
        base_runner = self.fixture.validation_runner

        def failing_runner(argv, **kwargs):
            if list(argv) == ["go", "test", "-json", "./..."]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout=json.dumps({"Action": "fail", "Package": "fixture/pkg"}) + "\n",
                    stderr="",
                )
            return base_runner(argv, **kwargs)

        repository.upgrade_test_hook = lambda *args, **kwargs: None
        repository.validation_runner = failing_runner

        with self.assertRaises(UpgradeBlocked):
            resume_upgrade(repository)

        self.assertEqual(self.fixture.read_state()["phase"], "validating")

    def test_report_entry_replacement_never_changes_trusted_staged_blob(self):
        repository = self.fixture.repository()
        worktree = self.fixture.worktree_path(self.release)
        captured = {}
        attacker_bytes = b'{"status":"PASS","token":"attacker"}\n'

        def replace_report(event, **kwargs):
            if event != "after_report_identity_check":
                return
            report = self.report_path()
            captured["canonical"] = report.read_bytes()
            report.write_bytes(attacker_bytes)

        repository.upgrade_test_hook = replace_report

        with self.assertRaisesRegex(UpgradeBlocked, "report path changed"):
            create_upgrade_candidate(repository, self.release)

        self.assertEqual(self.fixture.read_state()["phase"], "validating")
        committed = self.fixture.run_git(
            "show",
            f"HEAD:.steadflow/reports/{self.release}.json",
            root=worktree,
        ).stdout.encode("utf-8")
        self.assertEqual(committed, captured["canonical"])
        self.assertNotEqual(committed, attacker_bytes)

class UpgradeConflictContinueTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.fixture.create_source_conflict()
        self.release = "v1.1.0"
        self.target_commit, _ = self.fixture.add_release(
            self.release, conflict=True
        )

    def tearDown(self):
        self.fixture.cleanup()

    def start_conflict(self):
        source_before = self.fixture.source_snapshot()
        origin_before = self.fixture.origin_refs()

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("--continue", completed.stderr)
        self.assertEqual(self.fixture.source_snapshot(), source_before)
        self.assertEqual(self.fixture.origin_refs(), origin_before)
        state = self.fixture.read_state()
        self.assertEqual(state["phase"], "conflicted")
        worktree = Path(state["worktree"])
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", "MERGE_HEAD", root=worktree
            ).stdout.strip(),
            self.target_commit,
        )
        self.assertEqual(
            self.fixture.run_git("rev-parse", "HEAD", root=worktree).stdout.strip(),
            state["source_commit"],
        )
        self.assertEqual(
            self.fixture.run_git(
                "diff", "--name-only", "--diff-filter=U", root=worktree
            ).stdout.strip(),
            "shared.txt",
        )
        self.assertIn("<<<<<<<", (worktree / "shared.txt").read_text())
        return worktree

    def test_conflict_is_preserved_without_abort_reset_or_resolution(self):
        self.start_conflict()
        state = self.fixture.read_state()
        self.assertEqual(state["conflicts"]["paths"], ["shared.txt"])

    def test_unregistered_conflict_is_blocked_and_cannot_be_waived_on_continue(self):
        fixture = HermeticUpgradeFixture()
        self.addCleanup(fixture.cleanup)
        fixture.create_source_conflict(register_seam=False)
        release = "v1.1.0"
        fixture.add_release(release, conflict=True)

        initial = fixture.run_upgrade(release)

        self.assertEqual(initial.returncode, 2)
        self.assertIn("unregistered conflict paths: shared.txt", initial.stderr)
        state = fixture.read_state()
        self.assertEqual(state["phase"], "conflicted")
        self.assertEqual(state["conflicts"]["paths"], ["shared.txt"])
        worktree = Path(state["worktree"])
        (worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")
        fixture.run_git("add", "shared.txt", root=worktree)

        resumed = fixture.run_upgrade("--continue")

        self.assertEqual(resumed.returncode, 2)
        self.assertIn("unregistered conflict paths: shared.txt", resumed.stderr)
        self.assertEqual(fixture.read_state()["phase"], "conflicted")

    def test_continue_blocks_tampered_conflict_state_binding(self):
        self.start_conflict()
        state = self.fixture.read_state()
        state["conflicts"]["paths"] = ["other.txt"]
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("conflict evidence binding is invalid", completed.stderr)

    def test_continue_blocks_customization_change_after_conflict_capture(self):
        worktree = self.start_conflict()
        manifest_path = worktree / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["shared_seams"] = []
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2)
        self.assertIn("customization changed after merge conflict capture", completed.stderr)

    def test_continue_recovers_conflicted_candidate_after_baseline_commit(self):
        worktree = self.start_conflict()
        (worktree / "shared.txt").write_text(
            "resolved by fixture\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "shared.txt", root=worktree)
        repository = self.fixture.repository()

        def crash(event, **kwargs):
            if event == "after_baseline_commit":
                raise UpgradeBlocked("simulated crash after baseline commit")

        repository.upgrade_test_hook = crash
        with self.assertRaisesRegex(UpgradeBlocked, "simulated crash"):
            resume_upgrade(repository)
        self.assertEqual(self.fixture.read_state()["phase"], "validating")

        repository.upgrade_test_hook = lambda *args, **kwargs: None
        resumed = resume_upgrade(repository)

        self.assertEqual(resumed["phase"], "merged")

    def test_continue_blocks_while_unmerged_and_preserves_conflict(self):
        worktree = self.start_conflict()
        merge_head_before = self.fixture.run_git(
            "rev-parse", "MERGE_HEAD", root=worktree
        ).stdout

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unmerged", completed.stderr)
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", "MERGE_HEAD", root=worktree
            ).stdout,
            merge_head_before,
        )
        self.assertEqual(self.fixture.read_state()["phase"], "conflicted")

    def test_continue_completes_staged_resolution_noninteractively(self):
        worktree = self.start_conflict()
        (worktree / "shared.txt").write_text(
            "resolved by fixture\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "shared.txt", root=worktree)
        marker = self.fixture.install_forbidden_gpg_signer()

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists())
        self.assertIn("PASS", completed.stdout)
        state = self.fixture.read_state()
        self.assertEqual(state["phase"], "merged")
        self.assertEqual(set(state["evidence"]), EVIDENCE_KEYS)
        report = json.loads(
            (
                worktree
                / ".steadflow"
                / "reports"
                / f"{self.release}.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(report["seams"]["registered"], ["shared.txt"])
        self.assertEqual(report["seams"]["changed"], ["shared.txt"])
        self.assertEqual(report["seams"]["conflicts"], ["shared.txt"])
        self.assertEqual(
            self.fixture.run_git(
                "status", "--porcelain=v2", root=worktree
            ).stdout,
            "",
        )
        for suffix in ("json", "md"):
            self.assertEqual(
                self.fixture.run_git(
                    "cat-file",
                    "-t",
                    f"HEAD:.steadflow/reports/{self.release}.{suffix}",
                    root=worktree,
                ).stdout.strip(),
                "blob",
            )
        self.assertNotEqual(
            self.fixture.run_git(
                "rev-parse", "--verify", "MERGE_HEAD", root=worktree, check=False
            ).returncode,
            0,
        )
        self.assertEqual(
            self.fixture.run_git(
                "merge-base", "--is-ancestor", self.target_commit, "HEAD",
                root=worktree,
                check=False,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.fixture.run_git(
                "merge-base", "--is-ancestor", self.fixture.source_commit, "HEAD",
                root=worktree,
                check=False,
            ).returncode,
            0,
        )

    def test_continue_blocks_completed_merge_with_forgotten_untracked_file(self):
        worktree = self.start_conflict()
        (worktree / "shared.txt").write_text(
            "resolved by fixture\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "shared.txt", root=worktree)
        forgotten = worktree / "forgotten-untracked.txt"
        forgotten.write_text("preserve me\n", encoding="utf-8")
        state_before = self.fixture.read_state()

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("candidate working tree must be clean", completed.stderr)
        self.assertEqual(self.fixture.read_state(), state_before)
        self.assertEqual(forgotten.read_text(encoding="utf-8"), "preserve me\n")
        self.assertNotEqual(
            self.fixture.run_git(
                "rev-parse", "--verify", "MERGE_HEAD", root=worktree, check=False
            ).returncode,
            0,
        )

    def test_continue_accepts_user_completed_merge(self):
        worktree = self.start_conflict()
        (worktree / "shared.txt").write_text(
            "manually completed\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "shared.txt", root=worktree)
        self.fixture.run_git(
            "commit", "-m", "manual merge completion", root=worktree
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.fixture.read_state()["phase"], "merged")
        for ancestor in (self.fixture.source_commit, self.target_commit):
            self.assertEqual(
                self.fixture.run_git(
                    "merge-base", "--is-ancestor", ancestor, "HEAD",
                    root=worktree,
                    check=False,
                ).returncode,
                0,
            )

    def test_continue_blocks_if_remote_tag_changed_during_conflict(self):
        worktree = self.start_conflict()
        original_state = self.fixture.read_state()
        self.fixture.move_release_tag(self.release)

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("tag changed", completed.stderr)
        self.assertEqual(self.fixture.read_state(), original_state)
        self.assertEqual(
            self.fixture.run_git(
                "diff", "--name-only", "--diff-filter=U", root=worktree
            ).stdout.strip(),
            "shared.txt",
        )

    def test_continue_does_not_execute_post_merge_hook(self):
        worktree = self.start_conflict()
        marker = self.fixture.root / "continue-post-merge-ran"
        hook = self.fixture.fork / ".git" / "hooks" / "post-merge"
        hook.write_text(
            "#!/bin/sh\n"
            f"printf 'hook ran\\n' > {shlex.quote(str(marker))}\n",
            encoding="utf-8",
        )
        hook.chmod(0o755)
        (worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")
        self.fixture.run_git("add", "shared.txt", root=worktree)

        repository = self.fixture.repository()
        state = resume_upgrade(repository)

        self.assertEqual(state["phase"], "merged")
        self.assertFalse(marker.exists())
        continue_commands = [
            argv
            for argv in repository.commands
            if "merge" in argv and "--continue" in argv
        ]
        self.assertEqual(len(continue_commands), 1, repository.commands)
        self.assertIn("core.hooksPath=/dev/null", continue_commands[0])


class UpgradeStateIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.release = "v1.1.0"

    def tearDown(self):
        self.fixture.cleanup()

    def test_merged_release_rerun_reports_existing_candidate_without_duplicates(self):
        self.fixture.add_release(self.release)
        first = self.fixture.run_upgrade(self.release)
        self.assertEqual(first.returncode, 0, first.stderr)
        worktrees_before = self.fixture.run_git(
            "worktree", "list", "--porcelain", root=self.fixture.fork
        ).stdout
        branch_before = self.fixture.run_git(
            "rev-parse", f"refs/heads/upgrade/{self.release}", root=self.fixture.fork
        ).stdout

        repeated = self.fixture.run_upgrade(self.release)

        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertIn("existing", repeated.stdout)
        self.assertEqual(
            self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout,
            worktrees_before,
        )
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", f"refs/heads/upgrade/{self.release}",
                root=self.fixture.fork,
            ).stdout,
            branch_before,
        )

    def test_merged_rerun_blocks_candidate_that_lost_source_ancestry(self):
        target_commit, _ = self.fixture.add_release(self.release)
        first = self.fixture.run_upgrade(self.release)
        self.assertEqual(first.returncode, 0, first.stderr)
        worktree = self.fixture.worktree_path(self.release)
        self.fixture.run_git("reset", "--hard", target_commit, root=worktree)

        repeated = self.fixture.run_upgrade(self.release)

        self.assertEqual(repeated.returncode, 2)
        self.assertIn("source ancestry", repeated.stderr)

    def test_merged_rerun_blocks_all_candidate_worktree_dirt(self):
        mutations = ("untracked", "staged", "unstaged")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                fixture = HermeticUpgradeFixture()
                try:
                    fixture.add_release(self.release)
                    first = fixture.run_upgrade(self.release)
                    self.assertEqual(first.returncode, 0, first.stderr)
                    state_before = fixture.read_state()
                    worktree = fixture.worktree_path(self.release)
                    if mutation == "untracked":
                        (worktree / "candidate-untracked.txt").write_text(
                            "untracked\n", encoding="utf-8"
                        )
                    else:
                        dirty_path = worktree / f"upstream-{self.release}.txt"
                        dirty_path.write_text(
                            f"{mutation}\n", encoding="utf-8"
                        )
                        if mutation == "staged":
                            fixture.run_git(
                                "add", dirty_path.name, root=worktree
                            )

                    repeated = fixture.run_upgrade(self.release)

                    self.assertEqual(repeated.returncode, 2, repeated.stderr)
                    self.assertIn(
                        "candidate working tree must be clean", repeated.stderr
                    )
                    self.assertEqual(fixture.read_state(), state_before)
                    self.assertNotEqual(
                        fixture.run_git(
                            "status", "--porcelain=v2", "-z", root=worktree
                        ).stdout,
                        "",
                    )
                finally:
                    fixture.cleanup()

    def test_registered_worktree_cannot_be_replaced_by_independent_clone(self):
        self.fixture.add_release(self.release)
        first = self.fixture.run_upgrade(self.release)
        self.assertEqual(first.returncode, 0, first.stderr)
        worktree = self.fixture.worktree_path(self.release)
        self.fixture.run_git(
            "worktree", "remove", "--force", str(worktree), root=self.fixture.fork
        )
        self.fixture.run_git(
            "clone", "--quiet", "--no-local", str(self.fixture.fork), str(worktree),
            root=None,
        )
        self.fixture.configure_identity(worktree)
        self.fixture.run_git(
            "checkout",
            "-b",
            f"upgrade/{self.release}",
            f"origin/upgrade/{self.release}",
            root=worktree,
        )

        repeated = self.fixture.run_upgrade(self.release)

        self.assertEqual(repeated.returncode, 2)
        self.assertRegex(repeated.stderr, r"common directory|not registered")

    def test_conflicted_release_rerun_points_to_continue_without_duplicates(self):
        self.fixture.create_source_conflict()
        self.fixture.add_release(self.release, conflict=True)
        first = self.fixture.run_upgrade(self.release)
        self.assertNotEqual(first.returncode, 0)
        worktrees_before = self.fixture.run_git(
            "worktree", "list", "--porcelain", root=self.fixture.fork
        ).stdout

        repeated = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("--continue", repeated.stderr)
        self.assertEqual(
            self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout,
            worktrees_before,
        )

    def test_different_release_is_blocked_before_fetch_when_state_is_active(self):
        self.fixture.add_release(self.release)
        self.assertEqual(self.fixture.run_upgrade(self.release).returncode, 0)
        second_release = "v1.2.0"
        self.fixture.add_release(second_release)

        completed = self.fixture.run_upgrade(second_release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(self.release, completed.stderr)
        self.assertIn(second_release, completed.stderr)
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/steadflow-upstream/releases/{second_release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_corrupt_state_fails_closed_before_fetch(self):
        self.fixture.add_release(self.release)
        self.fixture.state_path.parent.mkdir(parents=True)
        self.fixture.state_path.write_text("{not json\n", encoding="utf-8")

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("upgrade state", completed.stderr)
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/steadflow-upstream/releases/{self.release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_state_schema_mismatch_fails_closed_before_fetch(self):
        self.fixture.add_release(self.release)
        self.fixture.state_path.parent.mkdir(parents=True)
        self.fixture.state_path.write_text(
            '{"schema_version": 1}\n', encoding="utf-8"
        )

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("keys missing=", completed.stderr)

    def test_branch_without_state_is_never_overwritten(self):
        self.fixture.add_release(self.release)
        self.fixture.run_git(
            "branch", f"upgrade/{self.release}", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("branch exists", completed.stderr)
        self.assertFalse(self.fixture.state_path.exists())


class UpgradeCompletedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.first_release = "v1.1.0"
        self.fixture.add_release(self.first_release)
        created = self.fixture.run_upgrade(self.first_release)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.first_state = self.fixture.read_state()
        self.first_worktree = Path(self.first_state["worktree"])

    def tearDown(self):
        self.fixture.cleanup()

    def merge_candidate_to_source(self):
        self.fixture.run_git(
            "merge",
            "--no-ff",
            "--no-edit",
            self.first_state["branch"],
            root=self.fixture.fork,
        )

    def accept_upgrade_metadata(self):
        manifest_path = self.fixture.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["integration_adapter"]["paths"].extend(
            [
                f".steadflow/reports/{self.first_release}.json",
                f".steadflow/reports/{self.first_release}.md",
            ]
        )
        manifest["integration_adapter"]["paths"].sort()
        self.fixture.write_json(".steadflow/customization.yml", manifest)
        lock_path = self.fixture.fork / ".steadflow" / "upstream-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock.update(
            {
                "peeled_commit": self.first_state["peeled_commit"],
                "release": self.first_release,
                "tree": self.fixture.run_git(
                    "rev-parse",
                    f"{self.first_state['peeled_commit']}^{{tree}}",
                    root=self.fixture.fork,
                ).stdout.strip(),
            }
        )
        self.fixture.write_json(".steadflow/upstream-lock.json", lock)
        known_path = self.fixture.fork / ".steadflow" / "known-failures.yml"
        known = json.loads(known_path.read_text(encoding="utf-8"))
        known["baseline"]["release"] = self.first_release
        self.fixture.write_json(".steadflow/known-failures.yml", known)
        self.fixture.run_git(
            "add",
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            ".steadflow/upstream-lock.json",
            root=self.fixture.fork,
        )
        self.fixture.run_git(
            "commit", "-m", "accept fixture upgrade metadata", root=self.fixture.fork
        )

    def assert_active_state_preserved(self):
        self.assertEqual(self.fixture.read_state(), self.first_state)
        self.assertFalse(
            self.fixture.completed_state_path(self.first_release).exists()
        )

    def test_completed_candidate_is_archived_and_next_release_can_start(self):
        self.merge_candidate_to_source()
        source_after_merge = self.fixture.run_git(
            "rev-parse", "HEAD", root=self.fixture.fork
        ).stdout.strip()

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("completed", completed.stdout)
        self.assertFalse(self.fixture.state_path.exists())
        completed_path = self.fixture.completed_state_path(self.first_release)
        self.assertTrue(completed_path.is_file())
        self.assertEqual(stat.S_IMODE(completed_path.stat().st_mode), 0o600)
        self.assertEqual(
            json.loads(completed_path.read_text(encoding="utf-8")),
            self.first_state,
        )
        self.assertTrue(self.first_worktree.is_dir())
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse", f"refs/heads/{self.first_state['branch']}",
                root=self.fixture.fork,
            ).stdout.strip(),
            self.fixture.run_git(
                "rev-parse", "HEAD", root=self.first_worktree
            ).stdout.strip(),
        )
        self.assertEqual(
            self.fixture.run_git("rev-parse", "HEAD", root=self.fixture.fork).stdout.strip(),
            source_after_merge,
        )

        second_release = "v1.2.0"
        self.fixture.add_release(second_release)
        second = self.fixture.run_upgrade(second_release)

        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(self.fixture.read_state()["release"], second_release)
        self.assertTrue(self.fixture.worktree_path(second_release).is_dir())
        self.assertTrue(completed_path.is_file())
        self.assertEqual(
            self.fixture.run_git(
                "worktree", "list", "--porcelain", root=self.fixture.fork
            ).stdout.count("worktree "),
            3,
        )

    def test_dirty_source_cannot_archive_completed_candidate(self):
        self.merge_candidate_to_source()
        (self.fixture.fork / "dirty-after-merge.txt").write_text(
            "dirty\n", encoding="utf-8"
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("must be clean", completed.stderr)
        self.assert_active_state_preserved()

    def test_source_without_candidate_ancestry_cannot_archive(self):
        (self.fixture.fork / "source-only.txt").write_text(
            "source only\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "source-only.txt", root=self.fixture.fork)
        self.fixture.run_git(
            "commit", "-m", "advance source without candidate", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertRegex(
            completed.stderr, r"source HEAD changed|not.*ancestor|unowned paths"
        )
        self.assert_active_state_preserved()

    def test_tampered_candidate_cannot_archive_completed_state(self):
        self.merge_candidate_to_source()
        self.fixture.run_git(
            "reset", "--hard", self.first_state["peeled_commit"],
            root=self.first_worktree,
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("source ancestry", completed.stderr)
        self.assert_active_state_preserved()

    def test_existing_completed_audit_is_never_overwritten(self):
        self.merge_candidate_to_source()
        completed_path = self.fixture.completed_state_path(self.first_release)
        sentinel = b"preexisting audit\n"
        completed_path.write_bytes(sentinel)
        completed_path.chmod(0o600)

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("completed state archive already exists", completed.stderr)
        self.assertEqual(completed_path.read_bytes(), sentinel)
        self.assertEqual(self.fixture.read_state(), self.first_state)

    def test_dirty_candidate_cannot_be_archived(self):
        self.merge_candidate_to_source()
        dirty = self.first_worktree / "candidate-untracked.txt"
        dirty.write_text("preserve\n", encoding="utf-8")

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("candidate working tree must be clean", completed.stderr)
        self.assert_active_state_preserved()
        self.assertEqual(dirty.read_text(encoding="utf-8"), "preserve\n")


class UpgradePreflightFailureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.release = "v1.1.0"

    def tearDown(self):
        self.fixture.cleanup()

    def assert_no_candidate_side_effects(self):
        self.assertFalse(self.fixture.state_path.exists())
        self.assertFalse(self.fixture.worktree_path(self.release).exists())
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/heads/upgrade/{self.release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_dirty_source_blocks_before_fetch_state_or_worktree(self):
        self.fixture.add_release(self.release)
        dirty = self.fixture.fork / "dirty.txt"
        dirty.write_text("dirty\n", encoding="utf-8")

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("must be clean", completed.stderr)
        self.assert_no_candidate_side_effects()
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref",
                "--verify",
                f"refs/steadflow-upstream/releases/{self.release}",
                root=self.fixture.fork,
                check=False,
            ).returncode,
            0,
        )

    def test_malformed_nested_manifest_is_controlled_block_not_traceback(self):
        manifest_path = self.fixture.fork / ".steadflow" / "customization.yml"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["data"] = []
        self.fixture.write_json(".steadflow/customization.yml", manifest)
        self.fixture.run_git("add", str(manifest_path), root=self.fixture.fork)
        self.fixture.run_git(
            "commit", "-m", "malformed fixture manifest", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("BLOCKED:", completed.stderr)
        self.assertIn("data must be an object", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_missing_upstream_remote_blocks(self):
        self.fixture.run_git("remote", "remove", "upstream", root=self.fixture.fork)

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("upstream remote lookup failed", completed.stderr)
        self.assert_no_candidate_side_effects()

    def test_upstream_url_mismatch_with_lock_blocks_before_fetch(self):
        self.fixture.add_release(self.release)
        self.fixture.run_git(
            "remote", "set-url", "upstream", str(self.fixture.origin_bare),
            root=self.fixture.fork,
        )

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("does not match", completed.stderr)
        self.assert_no_candidate_side_effects()

    def test_symlinked_worktrees_root_cannot_escape_repository(self):
        self.fixture.add_release(self.release)
        outside = self.fixture.root / "outside-worktrees"
        outside.mkdir()
        (self.fixture.fork / ".worktrees").symlink_to(
            outside, target_is_directory=True
        )
        self.fixture.run_git(
            "add", "-f", ".worktrees", root=self.fixture.fork
        )
        self.fixture.run_git(
            "commit", "-m", "tracked malicious worktree symlink",
            root=self.fixture.fork,
        )

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertRegex(completed.stderr, r"escapes repository|not a regular file")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(self.fixture.state_path.exists())

    def test_missing_release_tag_blocks_without_state_or_worktree(self):
        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn(
            f"upstream release tag {self.release} is missing", completed.stderr
        )
        self.assert_no_candidate_side_effects()

    def test_fetch_failure_blocks_without_state_or_worktree(self):
        self.fixture.add_release(self.release)
        invocation_count = self.fixture.root / "upload-pack-count"
        upload_pack = self.fixture.root / "fixture-upload-pack"
        upload_pack.write_text(
            "#!/bin/sh\n"
            f'count_file="{invocation_count}"\n'
            'count=$(cat "$count_file" 2>/dev/null || printf 0)\n'
            'count=$((count + 1))\n'
            'printf "%s\\n" "$count" > "$count_file"\n'
            'if [ "$count" -gt 1 ]; then exit 1; fi\n'
            'exec git-upload-pack "$@"\n',
            encoding="utf-8",
        )
        upload_pack.chmod(0o755)
        self.fixture.run_git(
            "config", "--local", "remote.upstream.uploadpack", str(upload_pack),
            root=self.fixture.fork,
        )

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("fetch of exact upstream release", completed.stderr)
        self.assert_no_candidate_side_effects()

    def test_release_ref_that_does_not_peel_to_commit_blocks(self):
        blob = self.fixture.add_noncommit_release(self.release)

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("peel", completed.stderr)
        self.assert_no_candidate_side_effects()
        self.assertEqual(
            self.fixture.run_git(
                "rev-parse",
                f"refs/steadflow-upstream/releases/{self.release}",
                root=self.fixture.fork,
            ).stdout.strip(),
            blob,
        )

    def test_locked_commit_that_is_not_source_ancestor_blocks(self):
        target_commit, _ = self.fixture.add_release(self.release)
        self.fixture.run_git(
            "fetch",
            "--no-tags",
            "upstream",
            f"refs/tags/{self.release}:refs/fixture/locked-release",
            root=self.fixture.fork,
        )
        target_tree = self.fixture.run_git(
            "rev-parse", f"{target_commit}^{{tree}}", root=self.fixture.fork
        ).stdout.strip()
        self.fixture.update_lock(
            release=self.release,
            peeled_commit=target_commit,
            tree=target_tree,
        )

        completed = self.fixture.run_upgrade("--verify-current")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not an ancestor", completed.stderr)
        self.assert_no_candidate_side_effects()

    def test_configuration_documents_reject_symlinks_nonregular_files_and_escape(self):
        relative_paths = (
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            ".steadflow/upstream-lock.json",
            ".steadflow/migration-checksums.json",
        )
        attacks = ("symlink", "directory", "escape")
        for relative_path in relative_paths:
            for attack in attacks:
                with self.subTest(path=relative_path, attack=attack):
                    fixture = HermeticUpgradeFixture()
                    try:
                        target = fixture.fork / relative_path
                        fixture.run_git(
                            "update-index", "--assume-unchanged", relative_path,
                            root=fixture.fork,
                        )
                        original_bytes = target.read_bytes()
                        target.unlink()
                        if attack == "symlink":
                            safe_target = fixture.fork / ".git" / (
                                target.name + ".fixture"
                            )
                            safe_target.write_bytes(original_bytes)
                            target.symlink_to(safe_target)
                        elif attack == "directory":
                            target.mkdir()
                        else:
                            outside = fixture.root / (target.name + ".outside")
                            outside.write_bytes(original_bytes)
                            target.symlink_to(outside)

                        completed = fixture.run_upgrade("--verify-current")

                        self.assertEqual(completed.returncode, 2)
                        self.assertRegex(
                            completed.stderr,
                            r"regular file|symlink|escapes repository",
                        )
                        self.assertFalse(fixture.state_path.parent.exists())
                    finally:
                        fixture.cleanup()


class UpgradeStateFilesystemBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.release = "v1.1.0"
        self.fixture.add_release(self.release)

    def tearDown(self):
        self.fixture.cleanup()

    def test_state_directory_symlink_is_rejected_without_external_write(self):
        outside = self.fixture.root / "outside-state"
        outside.mkdir()
        self.fixture.state_path.parent.symlink_to(outside, target_is_directory=True)

        completed = self.fixture.run_upgrade(self.release)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("state directory", completed.stderr)
        self.assertEqual(list(outside.iterdir()), [])

    def test_state_file_symlink_is_rejected_without_external_write(self):
        self.fixture.state_path.parent.mkdir(mode=0o700)
        outside = self.fixture.root / "outside-state.json"
        outside.write_text("preserve\n", encoding="utf-8")
        self.fixture.state_path.symlink_to(outside)

        completed = self.fixture.run_upgrade(self.release)

        self.assertEqual(completed.returncode, 2)
        self.assertIn("state file", completed.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "preserve\n")

    def test_existing_state_directory_is_tightened_and_files_are_private(self):
        self.fixture.state_path.parent.mkdir(mode=0o777)
        self.fixture.state_path.parent.chmod(0o777)

        completed = self.fixture.run_upgrade(self.release)

        self.assertEqual(completed.returncode, 0, completed.stderr)
        state_dir = self.fixture.state_path.parent
        self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.fixture.state_path.stat().st_mode), 0o600)
        lock_path = state_dir / "upgrade.lock"
        self.assertTrue(lock_path.is_file())
        self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

    def test_new_state_directory_and_git_common_parent_are_fsynced(self):
        repository = self.fixture.repository()
        fsynced_inodes = []
        real_fsync = os.fsync

        def trace_fsync(descriptor):
            descriptor_stat = os.fstat(descriptor)
            fsynced_inodes.append(
                (descriptor_stat.st_dev, descriptor_stat.st_ino)
            )
            return real_fsync(descriptor)

        with mock.patch("upstream_sync.os.fsync", side_effect=trace_fsync):
            state = create_upgrade_candidate(repository, self.release)

        self.assertEqual(state["phase"], "merged")
        state_directory = self.fixture.state_path.parent
        common_directory = state_directory.parent
        common_inode = (common_directory.stat().st_dev, common_directory.stat().st_ino)
        state_inode = (state_directory.stat().st_dev, state_directory.stat().st_ino)
        self.assertIn(common_inode, fsynced_inodes)
        self.assertIn(state_inode, fsynced_inodes)
        self.assertLess(
            fsynced_inodes.index(common_inode),
            fsynced_inodes.index(state_inode),
            fsynced_inodes,
        )


class UpgradeTagImmutabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.release = "v1.1.0"

    def tearDown(self):
        self.fixture.cleanup()

    def test_matching_internal_release_ref_is_reused_without_fetch(self):
        target_commit, tag_object = self.fixture.add_release(self.release)
        internal_ref = f"refs/steadflow-upstream/releases/{self.release}"
        self.fixture.run_git(
            "fetch",
            "--no-tags",
            "upstream",
            f"refs/tags/{self.release}:{internal_ref}",
            root=self.fixture.fork,
        )
        repository = self.fixture.repository()

        state = create_upgrade_candidate(repository, self.release)

        self.assertEqual(state["tag_object"], tag_object)
        self.assertEqual(state["peeled_commit"], target_commit)
        self.assertFalse(any("fetch" in argv for argv in repository.commands))

    def test_moved_remote_tag_blocks_reuse_of_internal_ref(self):
        self.fixture.add_release(self.release)
        internal_ref = f"refs/steadflow-upstream/releases/{self.release}"
        self.fixture.run_git(
            "fetch",
            "--no-tags",
            "upstream",
            f"refs/tags/{self.release}:{internal_ref}",
            root=self.fixture.fork,
        )
        self.fixture.move_release_tag(self.release)

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("tag changed", completed.stderr)
        self.assertFalse(self.fixture.state_path.exists())
        self.assertFalse(self.fixture.worktree_path(self.release).exists())

    def test_moved_remote_tag_blocks_existing_merged_candidate(self):
        self.fixture.add_release(self.release)
        self.assertEqual(self.fixture.run_upgrade(self.release).returncode, 0)
        original_state = self.fixture.read_state()
        self.fixture.move_release_tag(self.release)

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("tag changed", completed.stderr)
        self.assertEqual(self.fixture.read_state(), original_state)

    def test_path_without_state_is_never_overwritten(self):
        self.fixture.add_release(self.release)
        worktree = self.fixture.worktree_path(self.release)
        worktree.mkdir(parents=True)
        sentinel = worktree / "sentinel"
        sentinel.write_text("preserve\n", encoding="utf-8")

        completed = self.fixture.run_upgrade(self.release)

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("path exists", completed.stderr)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve\n")
        self.assertFalse(self.fixture.state_path.exists())


class UpgradeStateValidationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()
        self.fixture.create_source_conflict()
        self.release = "v1.1.0"
        self.target_commit, _ = self.fixture.add_release(
            self.release, conflict=True
        )
        started = self.fixture.run_upgrade(self.release)
        self.assertNotEqual(started.returncode, 0)
        self.state = self.fixture.read_state()
        self.worktree = Path(self.state["worktree"])

    def tearDown(self):
        self.fixture.cleanup()

    def write_state(self, state):
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_continue_blocks_if_source_head_changed(self):
        (self.fixture.fork / "after-state.txt").write_text(
            "changed\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "after-state.txt", root=self.fixture.fork)
        self.fixture.run_git(
            "commit", "-m", "source advanced after state", root=self.fixture.fork
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("source HEAD changed", completed.stderr)
        self.assertEqual(self.fixture.read_state(), self.state)

    def test_continue_blocks_state_worktree_outside_deterministic_path(self):
        altered = dict(self.state)
        altered["worktree"] = str((self.fixture.root / "outside").resolve())
        self.write_state(altered)

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("worktree path is inconsistent", completed.stderr)

    def test_continue_blocks_state_peeled_commit_mismatch(self):
        altered = dict(self.state)
        altered["peeled_commit"] = self.state["source_commit"]
        self.write_state(altered)

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("peeled commit changed", completed.stderr)

    def test_continue_blocks_candidate_branch_mismatch(self):
        self.fixture.run_git(
            "branch", "-m", "upgrade/v9.9.9", root=self.worktree
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("worktree branch is inconsistent", completed.stderr)

    def test_continue_blocks_merge_head_mismatch_without_resolving(self):
        (self.worktree / "shared.txt").write_text(
            "staged resolution\n", encoding="utf-8"
        )
        self.fixture.run_git("add", "shared.txt", root=self.worktree)
        merge_head_path = Path(
            self.fixture.run_git(
                "rev-parse", "--git-path", "MERGE_HEAD", root=self.worktree
            ).stdout.strip()
        )
        merge_head_path.write_text(
            self.state["source_commit"] + "\n", encoding="ascii"
        )

        completed = self.fixture.run_upgrade("--continue")

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("MERGE_HEAD does not match", completed.stderr)
        self.assertEqual(
            merge_head_path.read_text(encoding="ascii").strip(),
            self.state["source_commit"],
        )


class UpgradePreparedResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_continue_resumes_state_written_before_merge_started(self):
        release = "v1.1.0"
        target_commit, tag_object = self.fixture.add_release(release)
        internal_ref = f"refs/steadflow-upstream/releases/{release}"
        self.fixture.run_git(
            "fetch",
            "--no-tags",
            "upstream",
            f"refs/tags/{release}:{internal_ref}",
            root=self.fixture.fork,
        )
        worktree = self.fixture.worktree_path(release).resolve()
        worktree.parent.mkdir()
        self.fixture.run_git(
            "worktree",
            "add",
            "-b",
            f"upgrade/{release}",
            str(worktree),
            self.fixture.source_commit,
            root=self.fixture.fork,
        )
        state = {
            "branch": f"upgrade/{release}",
            "internal_ref": internal_ref,
            "peeled_commit": target_commit,
            "phase": "merging",
            "release": release,
            "schema_version": 1,
            "source_branch": "main",
            "source_commit": self.fixture.source_commit,
            "tag_object": tag_object,
            "worktree": str(worktree),
        }
        self.fixture.state_path.parent.mkdir()
        self.fixture.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        marker = self.fixture.install_forbidden_gpg_signer()

        completed = self.fixture.run_upgrade("--continue")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(self.fixture.read_state()["phase"], "merged")
        parents = self.fixture.run_git(
            "show", "-s", "--format=%P", "HEAD", root=worktree
        ).stdout.split()
        evidence = self.fixture.read_state()["evidence"]
        self.assertEqual(parents, [evidence["validated_head"]])
        validated_parents = self.fixture.run_git(
            "show", "-s", "--format=%P", evidence["validated_head"], root=worktree
        ).stdout.split()
        self.assertEqual(len(validated_parents), 1)
        merge_parents = self.fixture.run_git(
            "show", "-s", "--format=%P", validated_parents[0], root=worktree
        ).stdout.split()
        self.assertEqual(
            merge_parents, [self.fixture.source_commit, target_commit]
        )


class UpgradeGitEnvironmentTests(unittest.TestCase):
    def test_git_runner_does_not_inherit_trace_write_environment(self):
        trace_variable = "GIT_TRACE2_EVENT"
        previous = os.environ.get(trace_variable)
        os.environ[trace_variable] = "/tmp/steadflow-must-not-write-trace"
        try:
            repository = GitRepository(REPO_ROOT)
        finally:
            if previous is None:
                del os.environ[trace_variable]
            else:
                os.environ[trace_variable] = previous

        self.assertNotIn(trace_variable, repository.environment)


class UpgradeConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_concurrent_releases_create_exactly_one_candidate(self):
        first_release = "v1.1.0"
        second_release = "v1.2.0"
        self.fixture.add_release(first_release)
        self.fixture.add_release(second_release)
        entered = self.fixture.root / "upload-pack-entered"
        release_barrier = self.fixture.root / "upload-pack-release"
        count = self.fixture.root / "upload-pack-count"
        upload_pack = self.fixture.root / "blocking-upload-pack"
        upload_pack.write_text(
            "#!/bin/sh\n"
            f'count_file="{count}"\n'
            f'entered_file="{entered}"\n'
            f'release_file="{release_barrier}"\n'
            'current=$(cat "$count_file" 2>/dev/null || printf 0)\n'
            'current=$((current + 1))\n'
            'printf "%s\\n" "$current" > "$count_file"\n'
            'if [ "$current" -eq 1 ]; then\n'
            '  : > "$entered_file"\n'
            '  while [ ! -e "$release_file" ]; do sleep 0.05; done\n'
            'fi\n'
            'exec git-upload-pack "$@"\n',
            encoding="utf-8",
        )
        upload_pack.chmod(0o755)
        self.fixture.run_git(
            "config", "--local", "remote.upstream.uploadpack", str(upload_pack),
            root=self.fixture.fork,
        )
        environment = dict(self.fixture.environment)

        first = subprocess.Popen(
            [str(MODULE_DIR / "upgrade"), first_release],
            cwd=self.fixture.fork,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(entered.exists(), "first release did not reach upload-pack")
        second = subprocess.Popen(
            [str(MODULE_DIR / "upgrade"), second_release],
            cwd=self.fixture.fork,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        release_barrier.touch()
        first_stdout, first_stderr = first.communicate(timeout=20)
        second_stdout, second_stderr = second.communicate(timeout=20)

        self.assertEqual(first.returncode, 0, first_stderr)
        self.assertEqual(second.returncode, 2, second_stdout + second_stderr)
        self.assertIn("active release", second_stderr)
        self.assertEqual(int(count.read_text(encoding="utf-8")), 2)
        state = self.fixture.read_state()
        self.assertEqual(state["release"], first_release)
        worktrees = self.fixture.run_git(
            "worktree", "list", "--porcelain", root=self.fixture.fork
        ).stdout
        self.assertEqual(worktrees.count("worktree "), 2)
        self.assertTrue(self.fixture.worktree_path(first_release).is_dir())
        self.assertFalse(self.fixture.worktree_path(second_release).exists())
        self.assertNotEqual(
            self.fixture.run_git(
                "show-ref", "--verify", f"refs/heads/upgrade/{second_release}",
                root=self.fixture.fork, check=False,
            ).returncode,
            0,
        )

    def test_release_rechecks_clean_source_after_waiting_for_upgrade_lock(self):
        release = "v1.1.0"
        self.fixture.add_release(release)
        preflight_complete = self.fixture.root / "release-preflight-complete"
        environment = self.fixture.environment_with_preflight_barrier(
            preflight_complete
        )

        with self.fixture.hold_upgrade_lock():
            process = subprocess.Popen(
                [str(MODULE_DIR / "upgrade"), release],
                cwd=self.fixture.fork,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10
            while not preflight_complete.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(preflight_complete.exists(), "preflight did not complete")
            self.assertIsNone(process.poll(), "upgrade did not wait for held lock")
            (self.fixture.fork / "dirty-while-waiting.txt").write_text(
                "dirty\n", encoding="utf-8"
            )

        stdout, stderr = process.communicate(timeout=20)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("must be clean", stderr)
        self.assertFalse(self.fixture.state_path.exists())
        self.assertFalse(self.fixture.worktree_path(release).exists())
        for ref in (
            f"refs/heads/upgrade/{release}",
            f"refs/steadflow-upstream/releases/{release}",
        ):
            self.assertNotEqual(
                self.fixture.run_git(
                    "show-ref", "--verify", ref,
                    root=self.fixture.fork, check=False,
                ).returncode,
                0,
            )

    def test_continue_rechecks_clean_source_after_waiting_for_upgrade_lock(self):
        release = "v1.1.0"
        self.fixture.create_source_conflict()
        self.fixture.add_release(release, conflict=True)
        started = self.fixture.run_upgrade(release)
        self.assertEqual(started.returncode, 2, started.stderr)
        state_before = self.fixture.read_state()
        worktree = Path(state_before["worktree"])
        (worktree / "shared.txt").write_text("resolved\n", encoding="utf-8")
        self.fixture.run_git("add", "shared.txt", root=worktree)
        preflight_complete = self.fixture.root / "continue-preflight-complete"
        environment = self.fixture.environment_with_preflight_barrier(
            preflight_complete
        )

        with self.fixture.hold_upgrade_lock():
            process = subprocess.Popen(
                [str(MODULE_DIR / "upgrade"), "--continue"],
                cwd=self.fixture.fork,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10
            while not preflight_complete.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(preflight_complete.exists(), "preflight did not complete")
            self.assertIsNone(process.poll(), "continue did not wait for held lock")
            (self.fixture.fork / "dirty-while-continuing.txt").write_text(
                "dirty\n", encoding="utf-8"
            )

        stdout, stderr = process.communicate(timeout=20)

        self.assertEqual(process.returncode, 2, stdout + stderr)
        self.assertIn("must be clean", stderr)
        self.assertEqual(self.fixture.read_state(), state_before)
        self.assertEqual(
            self.fixture.run_git("rev-parse", "MERGE_HEAD", root=worktree).stdout.strip(),
            state_before["peeled_commit"],
        )


class UpgradeDiagnosticRedactionTests(unittest.TestCase):
    def test_lock_and_remote_urls_do_not_leak_credentials_query_or_fragment(self):
        dangerous_urls = (
            "https://example.invalid/owner/repo.git?token=TASK6_QUERY_SECRET",
            "https://example.invalid/owner/repo.git#TASK6_FRAGMENT_SECRET",
            "https://TASK6_USER:TASK6_PASSWORD@example.invalid/owner/repo.git",
        )
        for dangerous_url in dangerous_urls:
            with self.subTest(url=dangerous_url.split(":", 1)[0]):
                fixture = HermeticUpgradeFixture()
                try:
                    fixture.update_lock(repository=dangerous_url)
                    fixture.run_git(
                        "remote", "set-url", "upstream", dangerous_url,
                        root=fixture.fork,
                    )

                    completed = fixture.run_upgrade("--verify-current")

                    self.assertEqual(completed.returncode, 2)
                    combined = completed.stdout + completed.stderr
                    for secret in (
                        "TASK6_QUERY_SECRET",
                        "TASK6_FRAGMENT_SECRET",
                        "TASK6_USER",
                        "TASK6_PASSWORD",
                    ):
                        self.assertNotIn(secret, combined)
                    self.assertIn("BLOCKED", completed.stderr)
                finally:
                    fixture.cleanup()

    def test_git_failure_diagnostic_redacts_url_secrets(self):
        fixture = HermeticUpgradeFixture()
        try:
            fake_bin = fixture.root / "fake-git-bin"
            fake_bin.mkdir()
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'fatal: https://TASK6_USER:TASK6_PASSWORD@example.invalid/"
                "repo.git?token=TASK6_QUERY_SECRET#TASK6_FRAGMENT_SECRET' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o755)
            repository = fixture.repository()
            repository.environment["PATH"] = str(fake_bin)

            with self.assertRaises(UpgradeBlocked) as raised:
                repository.run("status", operation="adversarial Git failure")

            combined = str(raised.exception)
            for secret in (
                "TASK6_QUERY_SECRET",
                "TASK6_FRAGMENT_SECRET",
                "TASK6_USER",
                "TASK6_PASSWORD",
            ):
                self.assertNotIn(secret, combined)
            self.assertIn("<redacted>", combined)
        finally:
            fixture.cleanup()


class HermeticFixtureIsolationGuardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = HermeticUpgradeFixture()

    def tearDown(self):
        self.fixture.cleanup()

    def test_fixture_git_helper_requires_explicit_root_before_execution(self):
        previous_cwd = Path.cwd()
        guarded = False
        os.chdir(self.fixture.root)
        try:
            try:
                self.fixture.run_git(
                    "remote",
                    "set-url",
                    "origin",
                    str(self.fixture.origin_bare),
                    check=False,
                )
            except AssertionError:
                guarded = True
        finally:
            os.chdir(previous_cwd)

        self.assertTrue(guarded, "fixture Git calls must reject an omitted root")

    def test_fixture_remote_set_url_only_changes_temporary_fork(self):
        protected_before = protected_repo_snapshot()

        self.fixture.run_git(
            "remote",
            "set-url",
            "origin",
            str(self.fixture.upstream_bare),
            root=self.fixture.fork,
        )

        self.assertEqual(
            self.fixture.run_git(
                "remote", "get-url", "origin", root=self.fixture.fork
            ).stdout.strip(),
            str(self.fixture.upstream_bare),
        )
        self.assertEqual(protected_repo_snapshot(), protected_before)

    def test_suite_cache_cleanup_preserves_preexisting_user_cache(self):
        cache_dir = self.fixture.root / "__pycache__"
        cache_dir.mkdir()
        preexisting = cache_dir / "user-owned.pyc"
        preexisting.write_bytes(b"user cache\n")
        suite_created = cache_dir / "test_upstream_sync.fixture.pyc"
        suite_created.write_bytes(b"suite cache\n")

        _cleanup_suite_cache(suite_created, created_by_suite=True)
        _cleanup_suite_cache(preexisting, created_by_suite=False)

        self.assertFalse(suite_created.exists())
        self.assertEqual(preexisting.read_bytes(), b"user cache\n")
        self.assertTrue(cache_dir.is_dir())

    def test_exact_unittest_command_leaves_disposable_clone_without_pycache(self):
        if os.environ.get("STEADFLOW_NESTED_EXACT_SUITE") == "1":
            self.skipTest("avoid recursive exact-suite self invocation")
        clone = self.fixture.root / "exact-suite-clone"
        self.fixture.run_git(
            "clone", "--quiet", "--no-local", str(REPO_ROOT), str(clone), root=None
        )
        self.fixture.configure_identity(clone)
        for relative in (
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            "Makefile",
            "tools/upstream-sync/test_upstream_sync.py",
            "tools/upstream-sync/upstream_sync.py",
            "tools/upstream-sync/upgrade",
        ):
            source = REPO_ROOT / relative
            destination = clone / relative
            shutil.copy2(source, destination)
        self.fixture.run_git(
            "add",
            "tools/upstream-sync/test_upstream_sync.py",
            "tools/upstream-sync/upstream_sync.py",
            "tools/upstream-sync/upgrade",
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            "Makefile",
            root=clone,
        )
        self.fixture.run_git(
            "commit", "--allow-empty", "-m", "exact suite candidate", root=clone
        )
        environment = dict(os.environ)
        environment.update(self.fixture.environment)
        environment["PATH"] = os.environ["PATH"]
        environment["STEADFLOW_NESTED_EXACT_SUITE"] = "1"
        launcher_directory = self.fixture.root / "python-launcher"
        launcher_directory.mkdir()
        launcher = launcher_directory / "python3"
        launcher.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            "import runpy\n"
            "import sys\n"
            "if sys.argv[1:3] != ['-m', 'unittest']:\n"
            "    os.execv('/usr/bin/python3', ['/usr/bin/python3'] + sys.argv[1:])\n"
            "sys.pycache_prefix = None\n"
            "sys.path.insert(0, os.getcwd())\n"
            "sys.argv = ['unittest'] + sys.argv[3:]\n"
            "runpy.run_module('unittest', run_name='__main__', alter_sys=True)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        environment["PATH"] = str(launcher_directory) + os.pathsep + environment["PATH"]

        completed = subprocess.run(
            [
                "python3", "-m", "unittest",
                "tools/upstream-sync/test_upstream_sync.py", "-v",
            ],
            cwd=clone,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse((clone / "tools/upstream-sync/__pycache__").exists())
        self.assertEqual(
            self.fixture.run_git("status", "--porcelain", root=clone).stdout,
            "",
        )

    def test_exact_unittest_command_preserves_preexisting_same_name_test_cache(self):
        if os.environ.get("STEADFLOW_NESTED_EXACT_SUITE") == "1":
            self.skipTest("avoid recursive exact-suite self invocation")
        clone = self.fixture.root / "exact-suite-preexisting-cache-clone"
        self.fixture.run_git(
            "clone", "--quiet", "--no-local", str(REPO_ROOT), str(clone), root=None
        )
        self.fixture.configure_identity(clone)
        for relative in (
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            "Makefile",
            "tools/upstream-sync/test_upstream_sync.py",
            "tools/upstream-sync/upstream_sync.py",
            "tools/upstream-sync/upgrade",
        ):
            shutil.copy2(REPO_ROOT / relative, clone / relative)
        self.fixture.run_git(
            "add",
            "tools/upstream-sync/test_upstream_sync.py",
            "tools/upstream-sync/upstream_sync.py",
            "tools/upstream-sync/upgrade",
            ".steadflow/customization.yml",
            ".steadflow/known-failures.yml",
            "Makefile",
            root=clone,
        )
        self.fixture.run_git(
            "commit", "--allow-empty", "-m", "preexisting cache candidate",
            root=clone,
        )
        test_source = clone / "tools/upstream-sync/test_upstream_sync.py"
        cache_path = Path(importlib.util.cache_from_source(str(test_source)))
        cache_path.parent.mkdir()
        import py_compile

        py_compile.compile(str(test_source), cfile=str(cache_path), doraise=True)
        cache_path.chmod(0o640)
        fixed_mtime_ns = time.time_ns() - 60_000_000_000
        os.utime(cache_path, ns=(fixed_mtime_ns, fixed_mtime_ns))
        before = (
            cache_path.read_bytes(),
            stat.S_IMODE(cache_path.stat().st_mode),
            cache_path.stat().st_mtime_ns,
        )
        environment = dict(os.environ)
        environment.update(self.fixture.environment)
        environment["PATH"] = os.environ["PATH"]
        environment["STEADFLOW_NESTED_EXACT_SUITE"] = "1"
        launcher_directory = self.fixture.root / "preexisting-python-launcher"
        launcher_directory.mkdir()
        launcher = launcher_directory / "python3"
        launcher.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            "import runpy\n"
            "import sys\n"
            "if sys.argv[1:3] != ['-m', 'unittest']:\n"
            "    os.execv('/usr/bin/python3', ['/usr/bin/python3'] + sys.argv[1:])\n"
            "sys.pycache_prefix = None\n"
            "sys.path.insert(0, os.getcwd())\n"
            "sys.argv = ['unittest'] + sys.argv[3:]\n"
            "runpy.run_module('unittest', run_name='__main__', alter_sys=True)\n",
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        environment["PATH"] = str(launcher_directory) + os.pathsep + environment["PATH"]

        completed = subprocess.run(
            [
                "python3", "-m", "unittest",
                "tools/upstream-sync/test_upstream_sync.py", "-v",
            ],
            cwd=clone,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
        )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertTrue(cache_path.is_file())
        after = (
            cache_path.read_bytes(),
            stat.S_IMODE(cache_path.stat().st_mode),
            cache_path.stat().st_mtime_ns,
        )
        self.assertEqual(after, before)
        self.assertEqual(
            sorted(path.name for path in cache_path.parent.iterdir()),
            [cache_path.name],
        )


if __name__ == "__main__":
    unittest.main()
