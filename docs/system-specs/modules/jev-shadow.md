# Jev Shadow Sidecar

Owners: `kiro_crew.jev`, `kiro_crew.jev.shadow`, `kiro_crew.config.sections.JevConfig`.
Tests: `test/test_jev_shadow.py`, `test/test_jev_config.py`,
`test/test_jev_shadow_session_summary.py`.

## Purpose

A typed, local-only entry that accepts one allowlisted **redacted session-summary**
signal and returns a bounded shadow receipt. After a session summary is stored,
the generator may call that entry when an explicit persisted flag is on, and
write only the receipt under the data home. It does not ask a provider, write
raw content, or change product behavior.

This is not the Decisions (Jev) settings card. That card still has no backend
`decisions` section, and this module does not add one. Enabling this seam does
not mean a live model is wired, and a receipt never drives skills, memory, the
knowledge graph, routing, or cron.

## Config

`jev.shadow_enabled` is a modelled `config.json` section, default `False`.
Only an exact JSON `true` enables (`"true"` and `1` are deny). It is
config-file-only: it is not on the dashboard settings schema and it does not
read `decisions.preview`. A missing or malformed `jev` section degrades to
defaults so a hand-edited file cannot prevent the gateway from starting.
`KiroCrewConfig.jev` is typed as the bare `JevConfig` name so the schema
registry structurally includes `jev.shadow_enabled`.

`jev/shadow.py` calls `redact_payload` as a local validation predicate
(reject when the payload would change) and is classified in
`NON_EGRESS_REDACTION_MODULES`. It is not a redaction egress sink.

`enabled_from_mapping` reads the same `jev.shadow_enabled is True` rule from a
plain mapping.

## Entry

`evaluate_shadow(JevShadowRequest) -> JevShadowReceipt`.

`JevShadowRequest.enabled` defaults to `False`. Only an exact `True` takes the
shadow path.

A disabled request returns before the payload is inspected, so default-off cannot
process raw chat in order to reject it.

`observe_session_summary(enabled, session_key, payload)` is the post-generation
hook. Disabled returns before inspecting `payload` and writes nothing. Enabled
passes the payload through unchanged, calls `evaluate_shadow`, and stores one
bounded receipt. Extra top-level fields are not sliced away: a `messages` key
(or any other raw/unknown field) stays on the payload so evaluation rejects it
instead of turning a mixed document into an accepted summary. Generation
metadata (`generated_at`, `user_turns`, `last_activity`, `sig`, `gen`) is the
only extra top-level set admitted, and only as scalars. Exceptions are swallowed
without a traceback (fail-open for summary generation) and do not write a
success receipt (fail-closed for Jev). Debug logs emit only a reason code, never
the payload, the session identifier, or `exc_info`.

## Allowlist

The only legal `signal` is `session_summary`. Any other name, including the
preview card's `skills.select` / `skills.dedupe` / `cron.novelty` points, is
`unknown_signal`.

The payload may contain `intents` and `constraints` in the shape
`session_summary.normalize_payload` writes, plus the scalar generation envelope
keys above. Unknown keys, empty intents, wrong types, and oversize strings or
lists are `malformed_payload` or `oversize`.

Keys that mean raw chat, memory, or secret material (`messages`, `content`,
`transcript`, `memory`, `role`, …) are `raw_content`. A payload whose
`redact_payload` output differs from its input is `unredacted_secret` — callers
must redact first; this seam will not silently clean and continue.

An untrusted `Mapping` cannot recurse without bound. Ancestor cycles are
`malformed_payload`. Depth, node, mapping-key, and list-length budgets are
`oversize`. `RecursionError` is caught at the entry and becomes
`malformed_payload`, so a cyclic payload cannot crash the gateway.

## Receipt

On success the receipt is `accepted`, `status=shadowed`, and a `result` with a
fixed key set: `mode`, `action`, `backend`, `signal`, `fingerprint`,
`intent_count`, `constraint_count`. `action` is always `none`. `backend` is
always `local`. `fingerprint` is SHA-256 of canonical JSON, so the same payload
is deterministic.

The receipt never echoes payload strings. Debug logs emit only reason codes.

When the post-generation hook is enabled, the receipt is written to
`~/.kiro/crew/jev-shadow/<opaque-id>.json` (under `KIROCREW_HOME` when set).
The directory is created owner-only (`0o700` / inheritable owner-only ACL) and
the file is written with `restrict_to_owner` (`0o600` / owner-only DACL). The
filename is a bounded opaque id derived from the session key (truncated SHA-256
hex); the session identifier is not written into the filename or the body. The
file is the bounded receipt only. It is not mixed into the intent-summary
sidecar the panel reads, so the chat UI cannot grow a live-Jev affordance from
it.

## Hook from session-summary generation

`dashboard.chat_summary._generate_locked` calls `observe_session_summary` only
after `set_cached_intent_summary` has already succeeded. The summary write and
the WS invalidation do not depend on the receipt. The hook never sends the
summary to a network client.

## What it does not do

- No HTTP or provider client. No TypeSafe (or any) network call.
- No config write, and no write of payload strings.
- No import or call into memory, lessons, skills, knowledge, ACP, cron, or
  dashboard handlers from the evaluate path.
- No change to the Decisions (Jev) preview UI.
- A receipt never feeds skill selection, memory, the knowledge graph, routing,
  or cron.
