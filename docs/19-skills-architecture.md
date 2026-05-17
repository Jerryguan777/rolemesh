# Skills Architecture

This document explains how RoleMesh supports user-defined Skills — reusable workflow definitions that a coworker can invoke autonomously — uniformly across both agent backends (Claude SDK and Pi), with PostgreSQL as the single source of truth and per-spawn container projection.

The goal is to document the *why* behind the shape: which alternatives were rejected, which two-backend asymmetries this design papers over, and which extensibility points it deliberately leaves open.

Target audience: developers adding a new skill backend, a new REST endpoint for skill management, or anyone debugging why a coworker fails to discover an enabled skill at runtime.

---

## Background: The Fourth Coworker Configuration Axis

A RoleMesh coworker is configured along four orthogonal axes:

1. **System prompt** — identity, role, default behavior.
2. **Tools** — MCP servers the coworker can reach.
3. **Permissions** — what the coworker is allowed to do (data scope, scheduling, delegation).
4. **Skills** — reusable workflow definitions the agent invokes autonomously when a task matches.

The first three already live in the `coworkers` table and flow through `/api/admin/agents`. Skills is the fourth axis and the only one without first-class support before this design.

Both supported agent backends already understand the same canonical "skill" format:

- **Claude Agent SDK** loads skills from `~/.claude/skills/<name>/SKILL.md` (with optional supporting files in the same directory). The model reads each skill's frontmatter `description` to decide when to invoke it. RoleMesh already passes `setting_sources=["project","user"]` and includes `"Skill"` in `allowed_tools`, so the SDK side is wired.
- **Pi** loads skills from `~/.pi/agent/skills/<name>/SKILL.md` via `pi.coding_agent.core.skills`. Trigger semantics are identical: the model reads `description` and invokes the skill autonomously. Pi-specific `disable-model-invocation: true` opts out.

The trigger model is identical on both sides: **no slash command, no terminal, no human typing `/skill-name`**. RoleMesh containers have no interactive terminal — skills must work entirely through autonomous model invocation driven by frontmatter `description`. This shapes the entire design.

---

## Design Goals

1. **Single source of truth** — a skill exists in exactly one place (PostgreSQL). No file-system duplication, no hybrid metadata-in-DB-content-in-FS split.
2. **Backend transparency** — the same skill row materializes correctly into either backend's directory layout. Most skills are written once and run on both.
3. **Strict tenant isolation** — skills are tenant-scoped via RLS, attached to one coworker, never shared across coworkers in v1. Cross-tenant leakage is blocked at three layers (app, RLS, trigger).
4. **Read-only at runtime** — the agent inside the container cannot mutate skill files. Skills are configuration, not workspace.
5. **No restart for new skills** — every container spawn re-materializes from DB, so the next conversation picks up edits without orchestrator restart.
6. **No detail in `pg.py` god-module beyond what already lives there** — the schema and CRUD land in `pg.py` alongside the other 12 domains; a later refactor splits all domains together.
7. **REST-only edit surface in v1** — no CLI, no WebUI, no IDE integration. A future CLI (`rolemesh skill pull/push`) is the documented evolution path.

---

## What Is a Skill

A skill is a folder. Concretely, it is:

- **One `SKILL.md`** at the root, the entry point. The frontmatter `description` is what the model reads to decide when to invoke. The body explains the workflow.
- **Zero or more supporting files** — reference docs, examples, scripts, templates — that the SKILL.md may reference. The model reads them on demand when the workflow needs them.

```
code-review/
├── SKILL.md            ← entry point; frontmatter drives invocation
├── reference.md        ← detailed guidance loaded on demand
├── examples.md
└── scripts/
    └── helper.py
```

This folder shape is the canonical form recommended by Claude and matched by Pi. A flat single-file form is supported by both backends but no longer recommended; RoleMesh standardizes on folders.

---

## Storage Decision: PostgreSQL as Source of Truth

The most consequential design call was **where the skill content lives**. The choice is between PostgreSQL, the host file system, or a hybrid (metadata in DB, content on FS).

**PostgreSQL wins**, ranked by weight:

1. **Multi-tenant isolation is already solved** — RLS via `current_tenant_id()` is the established pattern in `pg.py`. An FS-based scheme would require a path-based isolation system from scratch.
2. **Multi-host deployment needs shared storage** — PostgreSQL is already the shared service. FS would need NFS, S3FS, or a sync layer.
3. **Backup and DR are free** — `pg_dump` covers everything in one pass. FS needs a parallel backup pipeline.
4. **Consistency with existing RoleMesh conventions** — prompt, tools, permissions all live in DB. Skills as the fourth axis matches.
5. **Audit trail is implicit** — standard `created_at` / `updated_at` / actor columns suffice.

**Cost**: IDE and git-based editing of skill content is awkward — users cannot just `vim ~/.claude/skills/foo/SKILL.md`. This is real, but mitigated:

- v1 has no end-user terminal in containers, so direct editing was never the workflow.
- A future `rolemesh skill pull/push` CLI provides the IDE/git affordance as an opt-in sync layer, analogous to `kubectl apply -f` or `aws ssm get-parameter`. The data model is forward-compatible with this CLI; v1 does not ship it but does not block it either.

**The hybrid alternative was explicitly rejected**: DB metadata + FS content gives the complexity surface of both sides without the single-source-of-truth guarantee of either.

---

## Data Model

Two tables, both tenant-scoped, both with RLS:

```
┌──────────────────────────────────────────┐      ┌──────────────────────────────────────┐
│  skills                                  │      │  skill_files                         │
│                                          │ 1..n │                                      │
│  id              UUID                    │◀─────│  skill_id   UUID  FK                 │
│  tenant_id       UUID                    │      │  path       TEXT                     │
│  coworker_id     UUID  FK (CASCADE)      │      │  content    TEXT                     │
│  name            TEXT  (regex-validated) │      │  mime_type  TEXT                     │
│  frontmatter_common    JSONB             │      │                                      │
│  frontmatter_backend   JSONB             │      │  PK (skill_id, path)                 │
│  enabled         BOOLEAN                 │      │  CHECK no abs path, no '..', no '\\' │
│  created_at, updated_at, created_by      │      │                                      │
│  UNIQUE (coworker_id, name)              │      │                                      │
└──────────────────────────────────────────┘      └──────────────────────────────────────┘
```

Key constraints, named here because they encode invariants the rest of the system relies on:

- `UNIQUE (coworker_id, name)` — one skill name per coworker.
- Application-layer invariant: every skill has exactly one `skill_files` row with `path = 'SKILL.md'`. Deleting it returns 400.
- Cross-tenant defense via BEFORE-INSERT trigger: `skills.coworker_id` must belong to a coworker in the same tenant — caught at the SQL layer, not the app.
- Path-traversal blocked by `CHECK` at write time, then re-validated at projection time via `realpath` against the skill root.

The schema deliberately omits a `scope` column, a `scope_id` column, and a `coworker_skills` join table — v1 does not support sharing a skill across coworkers. Adding sharing later is an additive migration (one new table) with zero changes to the existing rows.

---

## Why Frontmatter Is Split: `common` + `backend`

The two backends accept **different sets of frontmatter fields**:

| Field                      | Claude SDK | Pi |
|----------------------------|:---:|:---:|
| `name`                     | ✅ | ✅ |
| `description`              | ✅ | ✅ |
| `allowed-tools`            | ✅ | — |
| `model`                    | ✅ | — |
| `argument-hint`            | ✅ | — |
| `disable-model-invocation` | — | ✅ |

The intersection is `{name, description}`. Everything else is backend-specific.

A naive design — store the raw `SKILL.md` including frontmatter as one blob — has two failure modes:

- **Pollute the file with both backends' fields** and rely on each side ignoring foreign keys. This works in practice but ships skills with field noise the wrong backend silently drops.
- **Maintain two `SKILL.md` copies per skill, one per backend**. This duplicates the body and defeats "write once, run on both."

The chosen shape separates concerns:

- **`frontmatter_common` (JSONB)** — fields valid on every backend. Always includes `name` and `description`. **For most skills this is the only frontmatter field populated.**
- **`frontmatter_backend` (JSONB)** — shape `{claude: {...}, pi: {...}}`. Holds backend-specific fields only. Often empty.
- **`skill_files.content WHERE path = 'SKILL.md'`** — the body **only**, never the frontmatter block. The frontmatter is reconstructed at projection time.

The REST API matches: clients send `frontmatter_common`, `frontmatter_backend`, and `files` as separate fields. Submitting a `SKILL.md` body that contains a `---` block is a 422; the body lives in `files`, the frontmatter lives in JSONB.

---

## Container Projection

The DB stores skills. The agent reads them as files. Projection is the per-spawn translation:

```
                  ┌────────────────────────────────────┐
                  │   PostgreSQL                       │
                  │   skills + skill_files             │
                  │   WHERE coworker_id = $1           │
                  │     AND enabled = TRUE             │
                  └─────────────────┬──────────────────┘
                                    │ at container spawn
                                    ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  /var/lib/rolemesh/spawns/<job_id>/skills/                  │
       │                                                             │
       │    code-review/                                             │
       │      SKILL.md           ← frontmatter_common merged with    │
       │                            frontmatter_backend.<target>     │
       │                            then serialized as YAML +        │
       │                            content body                     │
       │      reference.md       ← skill_files row, written verbatim │
       │      scripts/helper.py                                      │
       └─────────────────────┬───────────────────────────────────────┘
                             │ read-only bind mount
                             ▼
       ┌─────────────────────────────────────────────────────────────┐
       │  Container target path, chosen by backend:                  │
       │                                                             │
       │    Claude  →  /home/agent/.claude/skills/                   │
       │    Pi      →  /home/agent/.pi/agent/skills/                 │
       └─────────────────────────────────────────────────────────────┘
```

Two properties make this work cleanly:

- **Frontmatter merge is per-file and per-backend.** Only `SKILL.md` gets the merge step (`common ∪ backend.<target>`). Supporting files are projected verbatim, identical across backends.
- **Atomicity is per-skill, not per-file.** Each skill materializes into `<spawn>/.partial/<name>/`, then a single `os.rename` flips it to `<spawn>/<name>/`. The model never observes a half-written skill (e.g. `SKILL.md` already visible but `reference.md` still being written).

The bind mount is read-only, so even if the agent's tools (Bash, Edit) attempt to modify skill files, the kernel rejects the write. Skills are configuration; the workspace bind mount (`/workspace/group`) is the only writable surface.

Disabled skills are filtered at the SQL query, not in the projector — they physically do not enter the container, so the model cannot see them and cannot accidentally invoke them.

Cleanup is two-layered: a per-spawn finalizer removes the temporary directory on normal container exit; an orphan cleaner sweeps abandoned directories on a schedule (`kill -9` safety net).

---

## Trigger Semantics: Description Is The Routing Decision

Both backends trigger skills the same way: **the model reads each skill's `description` and decides autonomously when to invoke**. There is no rules engine, no router on the host side, no slash command.

This places the entire routing burden on the `description` field. The skill author must:

- **State when to use, not just what it does.** "When the user asks for a code review, or asks 'what's wrong with this code'" beats "Code review skill."
- **Include counter-examples where ambiguity exists.** "Do not use for one-off syntax questions" prevents misfires.
- **Keep it short.** One to three sentences. The description is loaded on every turn — long descriptions burn tokens per request.

The guidance is identical on Claude and Pi because the trigger model is identical. Description quality is the single biggest determinant of whether skills are useful in practice.

---

## Architecture Summary

```
                         ┌─────────────────────────────────────────┐
                         │  REST API (/api/admin/agents/{id}/skills)
                         │  AdminUser-gated, sub-resource of coworker
                         │  POST / PATCH / DELETE / GET            │
                         └────────────────┬────────────────────────┘
                                          │ Pydantic schemas
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  pg.py CRUD                             │
                         │  (lives alongside the other 12 domains) │
                         │  enforces RLS + cross-tenant trigger    │
                         └────────────────┬────────────────────────┘
                                          │ SQL, tenant_id GUC bound
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  PostgreSQL                             │
                         │   skills + skill_files                  │
                         │   RLS, FK CASCADE, CHECK constraints    │
                         └────────────────┬────────────────────────┘
                                          │ at every container spawn
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  skill_projection.py                    │
                         │  query enabled skills, materialize to   │
                         │  /var/lib/rolemesh/spawns/<job_id>/...  │
                         │  with per-skill atomic rename           │
                         └────────────────┬────────────────────────┘
                                          │ ro bind mount
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │  Agent container                        │
                         │   Claude: /home/agent/.claude/skills/   │
                         │   Pi:     /home/agent/.pi/agent/skills/ │
                         │   model reads description, invokes      │
                         │   skill autonomously                    │
                         └─────────────────────────────────────────┘
```

---

## Security and Isolation

Five layers, none of them sufficient alone:

1. **Read-only bind mount** — kernel-enforced; the agent cannot rewrite skill files even with `Bash` and `Edit` tools available.
2. **Per-spawn directory** — each job gets a unique prefix; no cross-spawn sharing of the skill staging area.
3. **Three-layer tenant isolation** — app-level `WHERE tenant_id`, RLS policy on both tables, and a cross-tenant trigger on `skills` validating that `coworker_id` belongs to the same tenant.
4. **Path traversal blocked twice** — `CHECK` constraint rejects absolute paths, `..`, and backslashes at write time; the projector re-validates `realpath(target).startswith(skill_root)` at materialization.
5. **Body is data, not code** — RoleMesh does not execute skill content. Anything the agent does after reading a skill goes through the existing tool surface (Bash, Edit, MCP), gated by the existing safety framework and approval pipeline.

The mutation REST surface is gated by the same `AdminUser` dependency the rest of `/api/admin/agents` uses. Skill management is an admin-grade operation; regular users cannot reshape an agent's behavior.

---

## v1 Explicit Non-Goals

Each of the following is deliberately out of scope, with a clear forward-compatible extension path:

| Non-goal                          | Forward path |
|-----------------------------------|--------------|
| Cross-coworker skill sharing      | Add a `skill_assignments` join table; no change to existing rows |
| Group / department scope          | Add a `scope` enum + `scope_id` nullable column |
| Binary / executable assets        | Add `content_bytes BYTEA` alongside `content TEXT` |
| Symbolic links, exec bit          | Add `mode` / `link_target` columns |
| Runtime hot reload                | Push-based reload — every spawn already re-reads, so this is opt-in |
| Skill version history             | Add `skill_revisions` table; current row stays canonical |
| Skill-to-skill dependencies       | Add `depends_on TEXT[]` resolved at projection |
| CLI (`rolemesh skill pull/push`)  | Client-side; talks to the same REST surface |
| WebUI editor                      | Client-side; talks to the same REST surface |

Every extension is additive — new column, new table, or new enum value. None requires a data migration of existing skills.

---

## Tradeoffs Worth Naming

| Decision | Why this side | What it costs |
|----------|---------------|---------------|
| DB as source of truth | Multi-tenant + multi-host + DR in one move | IDE/git editing awkward until CLI lands |
| Frontmatter split (common + backend) | "Write once, run on both" with no field-noise on either side | One extra JSONB field to think about |
| Body in `skill_files.content`, frontmatter only in JSONB | Single canonical representation; round-trip clean | POST payload is structured, not "paste your SKILL.md here" |
| Per-skill atomic rename, not per-file | Model never sees half-materialized skill | Marginally more file system operations |
| Read-only bind mount, even on the Pi side over tmpfs | Tamper-proof at runtime | Pi-side mount layering has a Docker-version constraint |
| CRUD lives in `pg.py` | Consistent with the other 12 domains | Defers the god-module split by one more domain |
| No CLI / WebUI in v1 | Ship the substrate before the UX | Early adopters write JSON over curl |
| Skills attach to one coworker, no sharing | Smallest viable model | Shared skills require a future PR |

---

## Known Gaps

- **Pi-side mount layering** — `/home/agent/.pi` is a tmpfs in the current container, and projection adds a read-only bind mount at `/home/agent/.pi/agent/skills/`. Docker supports this overlay, but the behavior is version-sensitive and must be verified per deployment.
- **`coworkers.skills` JSONB column** — predates this design and is referenced by the existing REST API. It is left in place for v1; a follow-up PR removes it once clients migrate to the new sub-resource API.
- **Description quality has no automated check** — beyond minimum length, RoleMesh cannot tell whether a description will reliably route the model. Practical guidance lives in this document; enforcement is a future linter.
- **No skill discovery API for cross-tenant browsing** — intentionally absent in v1. If a "tenant skill library" becomes a product requirement, it adds a new scope, not a redesign.

---

## Related Documentation

- [`3-agent-executor-and-container-runtime.md`](3-agent-executor-and-container-runtime.md) — container lifecycle, mount construction, spawn directories.
- [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md) — RLS pattern, `current_tenant_id()` GUC, cross-tenant trigger pattern reused here.
- [`5-webui-architecture.md`](5-webui-architecture.md) — `/api/admin/*` surface and `AdminUser` dependency that skills mutation endpoints reuse.
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) — the two-backend abstraction; skills sit on the same per-coworker `agent_backend` choice.
