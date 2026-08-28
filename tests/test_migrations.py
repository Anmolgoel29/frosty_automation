# tests/test_migrations.py
"""Guards on the migration graph itself.

These need no database: Django builds the graph by importing every migration
module off disk, which is where the failures below actually happen.
"""
import pytest
from django.db.migrations.loader import MigrationLoader


@pytest.fixture(scope="module")
def loader():
    """Imports every migration module. Raises if any of them can't be loaded."""
    return MigrationLoader(None, ignore_no_migrations=True)


class TestMigrationGraph:
    def test_every_migration_module_imports(self, loader):
        """A migration that can't be imported breaks `migrate` for the whole project.

        Django loads the entire graph before running anything, so one bad
        module takes down every app's migrations — and the daemon with them,
        since rundaemon calls `migrate` on startup. This is how a stale
        top-level `import joblib` in linkedin/0004, left behind when the GP
        stack was dropped, turned a removed dependency into a boot failure.
        """
        assert loader.disk_migrations, "no migrations found — loader misconfigured"

    def test_migrations_do_not_import_optional_dependencies_at_module_scope(self):
        """Third-party imports in a migration must be inside the function.

        Migrations live forever but requirements don't. Anything a migration
        imports at module scope becomes a permanent install dependency, so a
        package dropped from requirements/ later breaks the graph. Import it
        lazily inside the RunPython callable and tolerate its absence.
        """
        from pathlib import Path

        repo = Path(__file__).resolve().parent.parent
        allowed = {"django", "io", "json", "logging", "pathlib", "urllib", "linkedin"}
        offenders = []

        for path in sorted(repo.glob("*/migrations/0*.py")):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if line != stripped:  # indented => inside a function, which is fine
                    continue
                root = stripped.split()[1].split(".")[0]
                if root not in allowed:
                    offenders.append(f"{path.relative_to(repo)}:{lineno}: {stripped}")

        assert not offenders, (
            "module-scope third-party imports in migrations:\n  " + "\n  ".join(offenders)
        )
