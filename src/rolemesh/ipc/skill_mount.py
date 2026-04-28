"""Skill bind-mount paths shared between the orchestrator (which
projects skills to host disk and tells Docker where to mount them)
and the in-container agent runtime (which scans those paths to
load skills into the model's system prompt).

These constants MUST agree byte-for-byte. A divergence — e.g. the
orchestrator mounts at ``/home/agent/.pi/skills`` while the agent
runtime scans ``/home/agent/.pi/agent/skills`` — produces a silent
failure: the spawn succeeds, the container starts, but the model
never learns about the projected skills and treats trigger phrases
as ordinary prompts. The bug surfaced in production once already;
we keep the constants here so any future change has to touch one
file and both call sites simultaneously.

The depth-1 invariant is documented in
``docs/skills-architecture.md`` "Pi backend mount strategy" and
in the comment block above ``CONTAINER_TARGETS_BY_BACKEND`` —
shorthand: the bind target must be a direct child of an
agent-owned parent. Going deeper on a tmpfs makes Docker's
mount-prep mkdir create root-owned intermediates that block the
agent process from writing siblings.

Lives under ``rolemesh/ipc/`` because that subtree is COPYed into
the container image (see ``container/Dockerfile``) and is the
established location for orchestrator-container shared protocol /
constants. Adding a new module here means the Dockerfile must
also COPY it; the ``test_image_has_skill_mount_module`` test in
``tests/container/test_skill_projection.py`` guards that.
"""

from __future__ import annotations

# Backend-specific bind-mount targets. Keys match the canonical
# names returned by ``BACKEND_CONFIGS`` in ``rolemesh.agent.executor``;
# the legacy alias ``"claude-code"`` maps to the same path as
# ``"claude"`` so both routes land in the same dir.
CLAUDE_SKILLS_PATH: str = "/home/agent/.claude/skills"
PI_SKILLS_PATH: str = "/home/agent/.pi/skills"

CONTAINER_TARGETS_BY_BACKEND: dict[str, str] = {
    "claude": CLAUDE_SKILLS_PATH,
    "claude-code": CLAUDE_SKILLS_PATH,
    "pi": PI_SKILLS_PATH,
}
