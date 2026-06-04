"""Built-in safety checks bundled with RoleMesh.

V1 ships ``pii.regex`` only. V2 adds orchestrator-only slow checks
(``presidio.pii``, ``llm_guard.prompt_injection``, etc.) here as well.

================================================================
GLOBAL ACTION MATRIX  (single readable source of truth)
================================================================
Descriptive metadata for the rule-editor UI — see the SafetyCheck
Protocol in ``rolemesh/safety/types.py``. These two tables MUST stay in
sync with each check's own ``natural_actions`` / ``supported_actions``
declarations; ``tests/safety/checks/test_action_matrix.py`` enforces
the agreement. When adding a new check, add its row here FIRST, then
implement the per-check declaration, so this global view never drifts.

``action_model`` (per check):
    fixed         action hardcoded in check() — a hit always returns
                  the same action (today: block). UI: "defaults to X".
    config_routed action chosen per-finding by the rule config;
                  default/empty config = inert (allow). UI: configure
                  per-category, no default badge.
    aggregated    check only votes; a later layer decides the effective
                  verdict. natural is the check's own return.

natural_actions  (action a hit returns under the DEFAULT config)
                              INPUT_   PRE_TOOL  POST_TOOL  MODEL_   EGRESS_
  check                model  PROMPT   _CALL     _RESULT    OUTPUT   REQUEST
  pii.regex            fixed  block    block     block      block    —
  presidio.pii         cfg    allow    —         allow      allow    —
  secret_scanner       fixed  block    —         block      block    —
  domain_allowlist     fixed  —        block     —          —        —
  egress.domain_rule   aggr   —        —         —          —        allow
  llm_guard.prompt_inj fixed  block    —         —          —        —
  llm_guard.jailbreak  fixed  block    —         —          —        —
  llm_guard.toxicity   fixed  block    —         —          block    —
  openai_moderation    cfg    allow    —         —          allow    —

supported_actions  (b=block r=redact w=warn a=allow R=require_approval)
                              INPUT_   PRE_TOOL  POST_TOOL  MODEL_   EGRESS_
  check                       PROMPT   _CALL     _RESULT    OUTPUT   REQUEST
  pii.regex            b a w R  b a w R  b a w    b a R     —
  presidio.pii         b r a w R  —      b r a w  b r a R   —
  secret_scanner       b a w R  —        b a w    b a R     —
  domain_allowlist     —        b a w R  —        —         —
  egress.domain_rule   —        —        —        —         b a
  llm_guard.prompt_inj b a w R  —        —        —         —
  llm_guard.jailbreak  b a w R  —        —        —         —
  llm_guard.toxicity   b a w R  —        —        b a R     —
  openai_moderation    b a w R  —        —        b a R     —

Capability gates that drive the exclusions above:
  - redact      only where the check can emit a modified_payload —
                today that is presidio.pii ALONE.
  - warn        only where a stage consumes appended_context — excluded
                on MODEL_OUTPUT (post-output) and EGRESS_REQUEST.
  - require_app only where the stage has an approval surface — excluded
                on POST_TOOL_RESULT (tool already ran) and
                EGRESS_REQUEST (gateway has no agent/human UX).
  - block/allow available everywhere.
================================================================
"""

from .pii_regex import PIICode, PIIRegexCheck, PIIRegexConfig

__all__ = ["PIICode", "PIIRegexCheck", "PIIRegexConfig"]
