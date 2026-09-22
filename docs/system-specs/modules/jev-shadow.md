# Jev Shadow Sidecar

Owners: `kiro_crew.jev`, `kiro_crew.jev.shadow`. Tests: `test/test_jev_shadow.py`.

## Purpose

A typed, local-only entry that accepts one allowlisted **redacted session-summary**
signal and returns a bounded shadow receipt. It exists so a later Jev integration
has a seam to call without this release asking a provider, writing raw content, or
changing product behavior.

This is not the Decisions (Jev) settings card. That card still has no backend
`decisions` section, and this module does not add one. Enabling this seam does
not mean a live model is wired, and a receipt never drives skills, memory, the
knowledge graph, routing, or cron.

## Entry

`evaluate_shadow(JevShadowRequest) -> JevShadowReceipt`.

`JevShadowRequest.enabled` defaults to `False`. Only an exact `True` takes the
shadow path. `enabled_from_mapping` reads `jev.shadow_enabled is True` from a
plain mapping; missing, malformed, `"true"`, and `1` are deny. It does not read
`decisions.preview`.

A disabled request returns before the payload is inspected, so default-off cannot
process raw chat in order to reject it.

## Allowlist

The only legal `signal` is `session_summary`. Any other name, including the
preview card's `skills.select` / `skills.dedupe` / `cron.novelty` points, is
`unknown_signal`.

The payload may contain only `intents` and `constraints`, in the shape
`session_summary.normalize_payload` writes. Unknown keys, empty intents, wrong
types, and oversize strings or lists are `malformed_payload` or `oversize`.

Keys that mean raw chat, memory, or secret material (`messages`, `content`,
`transcript`, `memory`, `role`, …) are `raw_content`. A payload whose
`redact_payload` output differs from its input is `unredacted_secret` — callers
must redact first; this seam will not silently clean and continue.

## Receipt

On success the receipt is `accepted`, `status=shadowed`, and a `result` with a
fixed key set: `mode`, `action`, `backend`, `signal`, `fingerprint`,
`intent_count`, `constraint_count`. `action` is always `none`. `backend` is
always `local`. `fingerprint` is SHA-256 of canonical JSON, so the same payload
is deterministic.

The receipt never echoes payload strings. Debug logs emit only reason codes.

## What it does not do

- No HTTP or provider client.
- No file write, config write, or sidecar of its own.
- No import or call into memory, lessons, skills, knowledge, ACP, cron, or
  dashboard handlers.
- No hook from `session_summary` generation or the chat summary pass. Callers
  opt in; nothing in the product invokes this yet.
