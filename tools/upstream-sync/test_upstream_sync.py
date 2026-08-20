import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parents[1]
sys.path.insert(0, str(MODULE_DIR))

from upstream_sync import (
    audit_migrations,
    ManifestValidationError,
    MigrationValidationError,
    OWNER_LAYER_KEYS,
    load_json_document,
    migration_checksums,
    validate_manifest,
    validate_migrations,
)


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

    def test_customization_manifest_matches_certified_task3_inventory(self):
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
                28,
                "440dbf19a5ecdcdfa0a1528e7ea491c119db9e2fb0d5dc60392fb1a3e4074638",
            ),
            "integration_adapter": (
                400,
                "326ff21ba98627bdbafb3a559393dc2d9ecb6d0e4e5e9afc608f65fb837a91f4",
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
        self.assertEqual(len(certified_paths), 503)
        self.assertEqual(len(set(certified_paths)), 503)
        report = validate_manifest(manifest, certified_paths)
        self.assertEqual(report["owned"], sorted(certified_paths))

        self.assertEqual(
            manifest["shared_seams"],
            [
                "backend/internal/handler/handler.go",
                "backend/internal/handler/wire.go",
                "backend/internal/server/router.go",
                "backend/internal/service/wire.go",
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
        self.assertEqual(len(generated_from_tree), 29)
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

    def test_every_go_critical_command_lists_real_tests(self):
        manifest = self.load_deterministic_json(".steadflow/customization.yml")
        go_commands = [
            command
            for command in manifest["critical_commands"]
            if command["argv"][:2] == ["go", "test"]
        ]
        self.assertEqual(len(go_commands), 5)

        for command in go_commands:
            with self.subTest(command=command["name"]):
                list_argv, completed = self.run_go_critical_command_discovery(
                    command, REPO_ROOT
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
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "fresh-clone"
            environment = dict(os.environ)
            environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(REPO_ROOT), str(clone)],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            manifest = self.load_deterministic_json(
                ".steadflow/customization.yml", clone
            )
            command = next(
                command
                for command in manifest["critical_commands"]
                if command["name"] == "backend_seo_public"
            )
            dist = clone / "backend" / "internal" / "web" / "dist"
            self.assertFalse(dist.exists())
            list_argv, completed = self.run_go_critical_command_discovery(
                command, clone
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
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "fresh-clone"
            environment = dict(os.environ)
            environment["GIT_CONFIG_GLOBAL"] = "/dev/null"
            environment["GIT_CONFIG_NOSYSTEM"] = "1"
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(REPO_ROOT), str(clone)],
                env=environment,
                check=True,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(self.git("remote", root=clone), "origin")

            self.assert_upstream_lock_matches_git_objects(clone)

    def test_migration_baseline_matches_every_current_sql_file(self):
        baseline = self.load_deterministic_json(
            ".steadflow/migration-checksums.json"
        )
        self.assertEqual(set(baseline), {"algorithm", "migrations", "schema_version"})
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
        report = validate_migrations(REPO_ROOT, baseline)
        self.assertEqual(len(report["unchanged"]), 268)
        self.assertEqual(report["changed"], [])
        self.assertEqual(report["deleted"], [])
        self.assertEqual(report["added"], [])


if __name__ == "__main__":
    unittest.main()
