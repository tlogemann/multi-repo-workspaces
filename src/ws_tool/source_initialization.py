from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .errors import WsError
from .git import clone_repository
from .models import RepoConfig


def initialize_source_clone_root(root: Path, repos: Iterable[RepoConfig]) -> Path:
    clone_root = root / "repos"
    if clone_root.exists() or clone_root.is_symlink():
        raise WsError(f"source clone root already exists: {clone_root}")
    repos = tuple(repos)
    for repo in repos:
        expected_source = (clone_root / repo.name).resolve(strict=False)
        if repo.source_path.resolve(strict=False) != expected_source:
            raise WsError(
                f"repository {repo.name!r} source path is not under the workspace clone root: "
                f"{repo.source_path}"
            )
    try:
        clone_root.mkdir()
    except FileExistsError as exc:
        raise WsError(f"source clone root appeared during initialization: {clone_root}") from exc

    marker = clone_root / ".ws-init.lock"
    marker_owned = False
    temporary_root: Path | None = None
    published: list[Path] = []
    try:
        try:
            marker.mkdir()
            marker_owned = True
        except FileExistsError as exc:
            raise WsError(f"source initialization marker already exists: {marker}") from exc
        temporary_root = Path(tempfile.mkdtemp(prefix=".repos.init-", dir=root))
        assert temporary_root is not None
        for repo in repos:
            clone_repository(repo.url, temporary_root / repo.name)
        for repo in repos:
            staged = temporary_root / repo.name
            destination = clone_root / repo.name
            if destination.exists() or destination.is_symlink():
                raise WsError(f"source clone appeared during initialization: {destination}")
            staged.rename(destination)
            published.append(destination)
        shutil.rmtree(temporary_root)
        temporary_root = None
        marker.rmdir()
        marker_owned = False
        return clone_root
    except BaseException:
        for destination in reversed(published):
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
        if marker_owned and marker.exists():
            marker.rmdir()
            clone_root.rmdir()
        raise
    finally:
        if temporary_root is not None and temporary_root.exists():
            shutil.rmtree(temporary_root)
