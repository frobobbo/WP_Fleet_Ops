import stat
import subprocess
import sys
import tarfile
from pathlib import Path


BUILDER = Path(__file__).parents[1] / "scripts" / "build_source_bundle.py"


def make_source_tree(root: Path) -> None:
    (root / "wp_fleet_ops" / "__pycache__").mkdir(parents=True)
    (root / "templates").mkdir()
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'example'\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "wp_fleet_ops" / "main.py").write_text("app = object()\n")
    (root / "wp_fleet_ops" / "__pycache__" / "main.pyc").write_bytes(b"cache")
    (root / "templates" / "index.html").write_text("<h1>FleetOps</h1>\n")
    (root / "tests" / "test_app.py").write_text("def test_app(): pass\n")
    (root / ".git" / "config").write_text("credential = secret\n")
    (root / "github.env").write_text("GITHUB_TOKEN=secret\n")
    (root / "harvester-kubeconfig.yaml").write_text("token: secret\n")
    (root / "README.md").write_text("not needed at runtime\n")


def test_source_bundle_contains_only_runtime_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    make_source_tree(root)
    output = tmp_path / "source-bundle.tar.gz"

    subprocess.run(
        [sys.executable, str(BUILDER), "--root", str(root), "--output", str(output)],
        check=True,
        capture_output=True,
        text=True,
    )

    with tarfile.open(output, "r:gz") as archive:
        assert set(archive.getnames()) == {
            "pyproject.toml",
            "templates/index.html",
            "uv.lock",
            "wp_fleet_ops/main.py",
        }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_source_bundle_rejects_symlinks_in_runtime_tree(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    make_source_tree(root)
    (root / "templates" / "linked-secret").symlink_to(root / "github.env")

    result = subprocess.run(
        [
            sys.executable,
            str(BUILDER),
            "--root",
            str(root),
            "--output",
            str(tmp_path / "source-bundle.tar.gz"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "symlink" in result.stderr
