"""Test helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_git_repo(root: Path, name: str = "app") -> Path:
    repo = root / name
    repo.mkdir()
    (repo / "README").write_text("hello\n")
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=onyx-job", "-c", "user.email=onyx-job@local", "add", "-A"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=onyx-job",
            "-c",
            "user.email=onyx-job@local",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def seed_files(repo: Path, names: list[str], *, extra: dict[str, str] | None = None) -> None:
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(f"# {name}\n")
    if extra:
        for name, content in extra.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=onyx-job",
            "-c",
            "user.email=onyx-job@local",
            "commit",
            "-m",
            "seed",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
