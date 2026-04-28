"""Project DB-stored skills to a host directory and return a
read-only bind mount for the agent container.

The flow is described in detail in docs/skills-architecture.md
"Container Projection". Brief recap:

* Per-spawn build dir: ``<DATA_DIR>/spawns/<job_id>/skills/``.
* Each skill writes into ``<build_dir>/.partial/<skill.name>/`` then
  the whole subtree is atomically renamed to
  ``<build_dir>/<skill.name>/`` once flushed. The model never sees
  a half-populated skill folder, even if the container starts in
  parallel with projection.
* SKILL.md frontmatter is merged from the active backend's section
  of ``frontmatter_backend`` plus all of ``frontmatter_common``;
  fields scoped to other backends are dropped.
* Path traversal is blocked at three layers: DB CHECK, application
  validator (``rolemesh.core.skills``), and projection-time
  ``Path.resolve()`` + symlink rejection.

The mount target inside the container depends on the backend:

* ``claude`` / ``claude-code`` → ``/home/agent/.claude/skills``
* ``pi``                       → ``/home/agent/.pi/agent/skills``
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from rolemesh.container.runtime import VolumeMount
from rolemesh.core.config import DATA_DIR
from rolemesh.core.logger import get_logger
from rolemesh.core.skills import (
    SkillValidationError,
    merge_frontmatter_for_backend,
    serialize_skill_md,
    validate_skill_file_path,
    validate_skill_name,
)
from rolemesh.db.pg import list_skills_for_coworker

if TYPE_CHECKING:
    from rolemesh.core.types import Coworker, Skill, SkillFile


logger = get_logger()


# Root on the host where every spawn directory lives. Each spawn
# (one per agent invocation) gets its own subtree keyed by job_id.
SPAWN_ROOT: Path = DATA_DIR / "spawns"

CONTAINER_TARGETS: dict[str, str] = {
    "claude": "/home/agent/.claude/skills",
    "claude-code": "/home/agent/.claude/skills",
    "pi": "/home/agent/.pi/agent/skills",
}


def _spawn_skills_dir(job_id: str) -> Path:
    """Return the per-spawn skills build directory.

    Caller is responsible for creating and cleaning it; this helper
    just centralizes the path layout so cleanup and projection see
    the same root.
    """
    return SPAWN_ROOT / job_id / "skills"


def _verify_no_symlink(path: Path, root: Path) -> None:
    """Walk every component from ``path`` up to (but not including)
    ``root`` and assert none is a symlink.

    v1 does not allow symlinks in skill folders. They cannot
    legitimately appear (the splitter only writes regular files), so
    encountering one is always a signal that something is wrong —
    either a stale spawn dir, a host-level tampering, or a bug.
    """
    p = path
    while True:
        if p == root or p.parent == p:
            return
        if p.is_symlink():
            raise SkillValidationError(
                f"refusing to write through symlink at {p}"
            )
        p = p.parent


def _resolved_path_inside(root: Path, target: Path) -> Path:
    """Return ``target.resolve()`` if it stays inside ``root``,
    otherwise raise.

    This is the projection-time mirror of the DB CHECK + application
    validator. Even if a malicious row managed to bypass both
    earlier layers, the resolved-path check catches escape attempts.
    """
    real = target.resolve(strict=False)
    real_root = root.resolve(strict=False)
    try:
        real.relative_to(real_root)
    except ValueError as exc:
        raise SkillValidationError(
            f"projected path {real} escapes spawn root {real_root}"
        ) from exc
    return real


def _materialize_one_skill(
    skill: Skill,
    backend: str,
    partial_root: Path,
    final_root: Path,
) -> None:
    """Build one skill into ``partial_root/<name>/`` then rename to
    ``final_root/<name>/`` atomically.

    Pre-conditions enforced at higher layers (DB CHECK + validators)
    are re-asserted here because the projector is the last line of
    defense before bytes hit the disk.
    """
    validate_skill_name(skill.name)
    skill_partial = partial_root / skill.name
    skill_final = final_root / skill.name

    # Refuse to operate on a pre-existing symlink — same contract as
    # the outer build_dir handling. ``shutil.rmtree(.., ignore_errors
    # =True)`` SILENTLY DOES NOT remove a symlink (it raises "Cannot
    # call rmtree on a symbolic link", which ignore_errors swallows),
    # leaving the link in place; the next ``mkdir(exist_ok=False)``
    # then explodes with FileExistsError. Aborting loudly here is
    # both safer (tampering visible in logs) and matches the design
    # invariant that v1 never writes symlinks.
    if skill_partial.is_symlink():
        raise SkillValidationError(
            f"refusing to project skill {skill.name!r}: "
            f"{skill_partial} is a symlink"
        )
    if skill_partial.exists():
        # A prior aborted projection in this same spawn could have
        # left a stale partial subtree (the orphan cleaner only sweeps
        # whole spawn dirs). Remove it cleanly — no ignore_errors.
        shutil.rmtree(skill_partial)
    skill_partial.mkdir(parents=True, exist_ok=False)

    if "SKILL.md" not in skill.files:
        raise SkillValidationError(
            f"skill {skill.name!r} is missing SKILL.md (application invariant)"
        )

    for path, file in skill.files.items():
        validate_skill_file_path(path)
        target = skill_partial / path
        # Each segment must clear the symlink check; the parent dirs
        # are created below.
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve and confirm stay-inside *before* writing, in
        # case the regex was somehow widened to allow an escape.
        _resolved_path_inside(skill_partial, target)
        _verify_no_symlink(target.parent, skill_partial)

        if path == "SKILL.md":
            merged = merge_frontmatter_for_backend(
                skill.frontmatter_common,
                skill.frontmatter_backend,
                backend,
            )
            content: str = serialize_skill_md(merged, file.content)
        else:
            content = file.content

        # Atomic-per-file write inside the still-private partial dir.
        # Even though the whole skill rename is atomic, this guards
        # against partial flushes if the host crashes mid-projection.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(target)

    # Whole-skill atomic publish. If something raised mid-loop, the
    # partial dir is left for the orphan cleaner to remove next pass.
    # Same symlink-aware cleanup as above: rmtree+ignore_errors does
    # not remove symlinks, and ``rename`` over a non-empty / symlinked
    # target has undefined behaviour on POSIX.
    if skill_final.is_symlink():
        raise SkillValidationError(
            f"refusing to publish skill {skill.name!r}: "
            f"{skill_final} is a symlink"
        )
    if skill_final.exists():
        shutil.rmtree(skill_final)
    skill_partial.rename(skill_final)


async def materialize_skills_for_spawn(
    coworker: Coworker,
    job_id: str,
    *,
    backend: str,
) -> VolumeMount | None:
    """Project a coworker's enabled skills to host disk and return a
    read-only bind mount spec.

    Returns ``None`` when the coworker has no enabled skills — saves
    a redundant empty-directory mount and lets the caller branch
    cleanly.

    The caller is responsible for adding the returned mount to the
    container spec and for invoking ``cleanup_spawn_skills(job_id)``
    when the container exits (or letting the orphan cleaner sweep
    the directory on a subsequent run).
    """
    if backend not in CONTAINER_TARGETS:
        raise SkillValidationError(
            f"unknown backend {backend!r}; "
            f"must be one of {sorted(CONTAINER_TARGETS)}"
        )

    skills = await list_skills_for_coworker(
        coworker.id,
        tenant_id=coworker.tenant_id,
        enabled_only=True,
        with_files=True,
    )
    if not skills:
        return None

    build_dir = _spawn_skills_dir(job_id)
    partial_root = build_dir / ".partial"
    # build_dir.parent = <SPAWN_ROOT>/<job_id>; create with
    # restrictive perms on parent so other tenants' agents (running
    # as the same UID) can't peek at sibling spawns by guessing IDs.
    SPAWN_ROOT.mkdir(parents=True, exist_ok=True)
    if build_dir.parent.is_symlink() or SPAWN_ROOT.is_symlink():
        # Tampering — refuse to operate on a path that has a symlink
        # in any ancestor. v1 never writes symlinks, so encountering
        # one is always wrong.
        raise SkillValidationError(
            f"refusing to project into spawn root with symlink ancestor: {build_dir}"
        )
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(build_dir.parent, 0o711)
    if build_dir.is_symlink():
        # Pre-existing symlink at the build dir is a red flag — aborts
        # rather than silently following or unlinking, so a tampering
        # attempt is loudly visible in logs.
        raise SkillValidationError(
            f"refusing to project: {build_dir} is a symlink"
        )
    if build_dir.exists():
        # Stale spawn dir from a prior aborted job reusing the same
        # job_id (extremely unlikely — job_ids carry a uuid suffix).
        # Remove rather than leak content into the new spawn.
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=False)
    partial_root.mkdir(parents=True, exist_ok=False)

    for skill in skills:
        _materialize_one_skill(skill, backend, partial_root, build_dir)

    # Drop the now-empty .partial dir so the container only ever sees
    # final skills. Empty dir → rmdir, never recursive removal here.
    try:
        partial_root.rmdir()
    except OSError:
        # Something left behind a half-projection. Leave it for the
        # orphan cleaner; do not block the spawn — the user-facing
        # skills are already in place.
        logger.warning(
            "skills .partial dir not empty after projection",
            job_id=job_id,
        )

    container_target = CONTAINER_TARGETS[backend]
    logger.info(
        "Skills projected",
        job_id=job_id,
        backend=backend,
        skill_count=len(skills),
        host_path=str(build_dir),
        container_path=container_target,
    )
    return VolumeMount(
        host_path=str(build_dir),
        container_path=container_target,
        readonly=True,
    )


def cleanup_spawn_skills(job_id: str) -> None:
    """Remove the build directory for a spawn.

    Called from the container executor's cleanup path after the
    container exits. Idempotent — missing dir is not an error.
    """
    spawn_dir = SPAWN_ROOT / job_id
    if not spawn_dir.exists():
        return
    try:
        shutil.rmtree(spawn_dir)
    except OSError as exc:
        logger.warning(
            "Failed to remove spawn skills dir",
            job_id=job_id,
            path=str(spawn_dir),
            error=str(exc),
        )


def cleanup_orphan_spawns(active_job_ids: set[str]) -> int:
    """Sweep build dirs whose job_id is not in ``active_job_ids``.

    Run periodically from the orchestrator (e.g. once per minute or
    at startup). Protects against ``kill -9`` of the orchestrator
    process: the per-spawn finalizer never ran, but the build dir
    is still on disk. Returns the count of removed directories.

    Materialize the directory listing before removing — POSIX
    ``readdir`` semantics around concurrent removal are
    implementation-defined (entries can be skipped or revisited),
    and ``shutil.rmtree`` mid-iteration would otherwise expose us to
    that. ``list(SPAWN_ROOT.iterdir())`` snapshots the directory
    while it's still consistent.
    """
    if not SPAWN_ROOT.exists():
        return 0
    removed = 0
    entries = list(SPAWN_ROOT.iterdir())
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name in active_job_ids:
            continue
        try:
            shutil.rmtree(entry)
            removed += 1
        except OSError as exc:
            logger.warning(
                "Failed to remove orphan spawn dir",
                path=str(entry),
                error=str(exc),
            )
    return removed
