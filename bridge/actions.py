"""#57 — Action proposal contract + whitelist gate (Bannerlord-proven {response, actions[]} shape).

Player2 may return, alongside its dialogue, a list of PROPOSED actions. Captured Bannerlord AI-Influence traffic
proved the working shape: a JSON reply with `response` (the spoken line) plus `actions:[...]`, where each action is
either an object {type, params} or a terse string verb like "relation:main_hero,change:negative" / "attack:main_hero".

This module is the DETERMINISTIC gate between "the model proposed it" and "X4 executes it". It:
  1. parse_actions()    — pull the actions out of a Player2 response (dict, JSON string, or bare list);
  2. normalize_action() — coerce object OR string into a canonical {type, params, description, source}; map the raw
     reference verbs ("relation", "say") onto our canonical types; unknown verbs keep their raw type (→ default-deny);
  3. classify_action()  — verdict against the action whitelist: allowed (mvp_enabled) / gated (disabled_until_tested) /
     unknown (not in whitelist → default-deny).

It NEVER executes and NEVER mutates state. It returns a verdict the caller audits (decision_records) and the in-game
MD dispatcher actuates ONLY for status == "allowed". This is the spec boundary in code: Player2 proposes intent; the
bridge validates; X4 executes only the validated subset.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

# Canonical whitelist — mirrors config/action_whitelist.json so the module is self-contained (hermetic selftest,
# safe default if the file is missing). The on-disk file, when found, OVERRIDES these.
DEFAULT_MVP_ENABLED = ("dialogue_only", "memory_write", "logbook_entry", "status_update", "relation_delta_limited")
DEFAULT_GATED = ("credit_transfer_limited", "mission_offer",
                 "trade_request", "temporary_diplomatic_flag", "faction_to_faction_proposal")

# Reference-verb → canonical-type alias. The Bannerlord capture used terse verbs; map the ones we have a canonical
# type for. Anything not here keeps its raw verb as the type and is therefore default-denied by classify_action.
VERB_ALIAS = {
    "say": "dialogue_only", "response": "dialogue_only", "dialogue": "dialogue_only", "speak": "dialogue_only",
    "remember": "memory_write", "memory": "memory_write", "note": "memory_write",
    "log": "logbook_entry", "logbook": "logbook_entry",
    "status": "status_update",
    "relation": "relation_delta_limited", "relationship": "relation_delta_limited",
    "credits": "credit_transfer_limited", "pay": "credit_transfer_limited", "transfer": "credit_transfer_limited",
    "mission": "mission_offer", "offer_mission": "mission_offer",
    "trade": "trade_request",
    "flag": "temporary_diplomatic_flag", "diplomacy": "temporary_diplomatic_flag",
    "proposal": "faction_to_faction_proposal", "propose": "faction_to_faction_proposal",
    # deliberately UNMAPPED (default-deny): "attack" and other hostile orders are not whitelisted.
}


# Generation-time grammar for each canonical action type — the Bannerlord-proven pattern enumerates the LEGAL verbs
# WITH grammar in the system prompt ("use only listed verbs"), constraining the model at generation, not just after.
# Only ENABLED (mvp) verbs are advertised to the model; gated/disabled verbs are deliberately NOT shown (don't tempt
# the model with actions the validator will drop). Each entry: (grammar, one-line purpose).
ACTION_GRAMMAR: dict[str, tuple[str, str]] = {
    "dialogue_only":  ('"dialogue_only"', "say something with no world effect (usually just use the response field)"),
    "memory_write":   ('"memory_write:<key>,value:<value>"', "remember a durable fact about this conversation/partner"),
    "logbook_entry":  ('"logbook_entry:<short text>"', "post a line to the player's logbook"),
    "status_update":  ('"status_update:<one-word status>"', "declare your faction's current posture/status"),
    "relation_delta_limited": ('"relation:<faction_id>,change:positive|negative"',
                               "nudge your standing toward another faction (bounded; attitude only)"),
}


def prompt_action_spec(whitelist: Optional[dict[str, set[str]]] = None, root: Optional[Path] = None) -> str:
    """Render the `### Actions ###` block for the system prompt — the proven Bannerlord generation-time constraint:
    enumerate ONLY the enabled verbs with grammar + the 'use only listed verbs / world changes go through actions[]'
    rule. Returns '' if no enabled verb has a known grammar (then the caller omits the section)."""
    wl = whitelist or load_whitelist(root)
    enabled = wl.get("mvp", set())
    lines = [f"- {ACTION_GRAMMAR[t][0]} — {ACTION_GRAMMAR[t][1]}." for t in sorted(enabled) if t in ACTION_GRAMMAR]
    if not lines:
        return ""
    return ("### Actions ###\n"
            "Any world change must go through `actions[]`. Use ONLY the verbs listed below, with the exact grammar "
            "shown; never invent a verb. Omit `actions` or return `[]` if no action is needed.\n"
            # Bannerlord proxy lesson (2026-07-01, wiki bannerlord-proxy-lessons): prose is not state. Captured
            # failure: an NPC narrated accepting 20,000 denars with actions:[] — nothing moved. Guard at
            # GENERATION time, not just at validation time:
            "You may roleplay requests, offers, and intentions freely. You may NOT state that resources changed "
            "hands, payments completed, jobs finished, treaties concluded, or relations changed unless you emit "
            "the corresponding action here and it is valid. If your counterpart CLAIMS to have paid or delivered "
            "something, treat it as an unverified claim — acknowledge the offer, but do not confirm receipt or "
            "outcome without the action. If required facts are missing, ask, or return no action.\n"
            + "\n".join(lines))


def load_whitelist(root: Optional[Path] = None) -> dict[str, set[str]]:
    """Return {"mvp": set, "gated": set}. Tries the on-disk config from a few candidate locations (env override
    first), else falls back to the embedded DEFAULT. Never raises — a bad/absent file degrades to the safe default."""
    candidates: list[Path] = []
    env = os.environ.get("AIC_ACTION_WHITELIST")
    if env:
        candidates.append(Path(env))
    if root is not None:
        r = Path(root)
        # bridge root is x4_neural_link/ ; the config lives in the x4_ai_influence mod (sibling or parent layout).
        candidates += [
            r / "config" / "action_whitelist.json",
            r.parent / "config" / "action_whitelist.json",
            r.parent / "x4_ai_influence" / "config" / "action_whitelist.json",
            r.parent.parent / "x4_ai_influence" / "config" / "action_whitelist.json",
        ]
    for c in candidates:
        try:
            if c and c.is_file():
                data = json.loads(c.read_text(encoding="utf-8"))
                mvp = {str(x).strip().lower() for x in (data.get("mvp_enabled_actions") or []) if str(x).strip()}
                gated = {str(x).strip().lower() for x in (data.get("disabled_until_tested") or []) if str(x).strip()}
                if mvp or gated:
                    return {"mvp": mvp, "gated": gated}
        except Exception:
            continue
    return {"mvp": set(DEFAULT_MVP_ENABLED), "gated": set(DEFAULT_GATED)}


def _loads_loose(s: str) -> Any:
    """Tolerant JSON load for a raw LLM reply: strips ```json fences and falls back to the outermost {...} span.
    Returns the parsed object, or None if nothing parseable is found."""
    if not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    # strip a leading/trailing markdown code fence if present
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
        t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if 0 <= i < j:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            return None
    return None


def _parse_string_action(s: str) -> dict[str, Any]:
    """Parse the terse reference shape "verb:arg,key:val,key2:val2" → {raw_verb, params}.
    The first token's value (if any) becomes params['target'] (positional); the rest are key:val pairs."""
    s = (s or "").strip()
    params: dict[str, Any] = {}
    if not s:
        return {"raw_verb": "", "params": params}
    tokens = [t for t in s.split(",") if t.strip()]
    head = tokens[0]
    if ":" in head:
        verb, val = head.split(":", 1)
        verb = verb.strip().lower()
        if val.strip():
            params["target"] = val.strip()
    else:
        verb = head.strip().lower()
    for tok in tokens[1:]:
        if ":" in tok:
            k, v = tok.split(":", 1)
            params[k.strip().lower()] = v.strip()
        elif tok.strip():
            params.setdefault("args", []).append(tok.strip())  # type: ignore[union-attr]
    return {"raw_verb": verb, "params": params}


def normalize_action(raw: Any) -> dict[str, Any]:
    """Coerce one proposed action (object OR terse string) into the canonical shape:
        {type, params, description, source_verb, source}
    `type` is the canonical whitelist key (after verb-alias) when known, else the raw verb (→ default-deny)."""
    description = ""
    source = ""
    if isinstance(raw, str):
        p = _parse_string_action(raw)
        verb, params = p["raw_verb"], p["params"]
        source = raw
    elif isinstance(raw, dict):
        verb = str(raw.get("type") or raw.get("action") or raw.get("verb") or "").strip().lower()
        params = raw.get("params")
        if not isinstance(params, dict):
            # treat any non-reserved keys as params
            params = {k: v for k, v in raw.items()
                      if k not in ("type", "action", "verb", "params", "description", "needs_confirm")}
        description = str(raw.get("description") or "")
        source = json.dumps(raw, ensure_ascii=False, sort_keys=True)
    else:
        verb, params = "", {}
    canonical = VERB_ALIAS.get(verb, verb)
    return {"type": canonical, "params": params or {}, "description": description,
            "source_verb": verb, "source": source}


def classify_action(action: dict[str, Any], whitelist: dict[str, set[str]]) -> dict[str, Any]:
    """Verdict for one normalized action: status ∈ {allowed, gated, unknown} + reason. Default-deny."""
    t = str(action.get("type") or "").strip().lower()
    if not t:
        return {"status": "unknown", "reason": "empty action type"}
    if t in whitelist.get("mvp", set()):
        return {"status": "allowed", "reason": "mvp_enabled"}
    if t in whitelist.get("gated", set()):
        return {"status": "gated", "reason": "disabled_until_tested"}
    return {"status": "unknown", "reason": "not in whitelist (default-deny)"}


def _extract_action_list(response: Any) -> list[Any]:
    """Pull the bare actions list out of whatever Player2 returned: a dict with 'actions', a JSON string, or a list."""
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, str):
        parsed = _loads_loose(response)
        return _extract_action_list(parsed) if parsed is not None else []
    if isinstance(response, dict):
        acts = response.get("actions")
        if isinstance(acts, list):
            return acts
        if isinstance(acts, (str, dict)):
            return [acts]
        return []
    return []


def validate_actions(response: Any, whitelist: Optional[dict[str, set[str]]] = None,
                     root: Optional[Path] = None) -> dict[str, Any]:
    """The public entry: parse → normalize → classify a Player2 response's actions[]. Returns:
        {ok, reply, actions:[{...,status,reason}], allowed:[...], gated:[...], unknown:[...], counts}
    `reply` is the spoken line if present. NOTHING is executed; `allowed` is what the dispatcher may actuate."""
    wl = whitelist or load_whitelist(root)
    reply = ""
    if isinstance(response, dict):
        reply = str(response.get("response") or response.get("reply") or "")
    elif isinstance(response, str):
        d = _loads_loose(response)
        if isinstance(d, dict):
            reply = str(d.get("response") or d.get("reply") or "")
    out: list[dict[str, Any]] = []
    for raw in _extract_action_list(response):
        a = normalize_action(raw)
        a.update(classify_action(a, wl))
        out.append(a)
    allowed = [a for a in out if a["status"] == "allowed"]
    gated = [a for a in out if a["status"] == "gated"]
    unknown = [a for a in out if a["status"] == "unknown"]
    return {"ok": True, "reply": reply, "actions": out, "allowed": allowed, "gated": gated, "unknown": unknown,
            "counts": {"total": len(out), "allowed": len(allowed), "gated": len(gated), "unknown": len(unknown)}}


def run_actions_selftest() -> dict:
    """Oracle for #57: the parse→normalize→classify contract, hermetic against the embedded DEFAULT whitelist."""
    checks: list[dict] = []

    def chk(n: str, c: bool, d: str = "") -> None:
        checks.append({"name": n, "ok": bool(c), "detail": d})

    wl = {"mvp": set(DEFAULT_MVP_ENABLED), "gated": set(DEFAULT_GATED)}

    # object dialogue → allowed
    a1 = normalize_action({"type": "dialogue_only", "params": {"text": "Greetings."}})
    chk("object_dialogue_allowed", classify_action(a1, wl)["status"] == "allowed", str(a1))

    # object memory_write → allowed
    a2 = normalize_action({"type": "memory_write", "key": "met_player", "value": True})
    chk("object_memory_allowed", classify_action(a2, wl)["status"] == "allowed",
        f"params={a2['params']}")
    chk("object_loose_keys_as_params", a2["params"].get("key") == "met_player", str(a2["params"]))

    # Bannerlord string shape → relation_delta_limited, now ENABLED (#64), with parsed params
    a3 = normalize_action("relation:main_hero,change:negative")
    c3 = classify_action(a3, wl)
    chk("string_relation_canonical", a3["type"] == "relation_delta_limited", a3["type"])
    chk("string_relation_allowed", c3["status"] == "allowed", str(c3))
    chk("string_relation_params", a3["params"].get("target") == "main_hero"
        and a3["params"].get("change") == "negative", str(a3["params"]))

    # hostile verb not in whitelist or alias → unknown (default-deny)
    a4 = normalize_action("attack:main_hero")
    chk("string_attack_unknown", classify_action(a4, wl)["status"] == "unknown", str(a4))

    # object gated type
    a5 = normalize_action({"type": "credit_transfer_limited", "amount": 5000})
    chk("object_credit_gated", classify_action(a5, wl)["status"] == "gated", str(a5))

    # object vs string parity: same canonical type for the relation intent
    a6 = normalize_action({"type": "relation", "target": "argon", "change": "positive"})
    chk("verb_alias_parity", a6["type"] == a3["type"] == "relation_delta_limited",
        f"{a6['type']} vs {a3['type']}")

    # full response: reply extracted + mixed bucket counts correct (dialogue+relation allowed, credit gated, attack unknown)
    resp = {"response": "We will not forget this.",
            "actions": [{"type": "dialogue_only"}, "relation:argon,change:negative",
                        {"type": "credit_transfer_limited", "amount": 1}, "attack:argon"]}
    v = validate_actions(resp, whitelist=wl)
    chk("response_reply_extracted", v["reply"] == "We will not forget this.", v["reply"])
    chk("response_counts", v["counts"] == {"total": 4, "allowed": 2, "gated": 1, "unknown": 1}, str(v["counts"]))
    chk("response_allowed_types", sorted(x["type"] for x in v["allowed"]) == ["dialogue_only", "relation_delta_limited"],
        str([x["type"] for x in v["allowed"]]))

    # JSON-string response (the literal Player2 wire shape) parses too
    v2 = validate_actions('{"response":"ok","actions":["status:busy"]}', whitelist=wl)
    chk("json_string_response", v2["counts"]["total"] == 1
        and v2["actions"][0]["type"] == "status_update", str(v2["counts"]))

    # empty / no actions → empty, ok (no crash)
    v3 = validate_actions({"response": "Hello."}, whitelist=wl)
    chk("empty_actions_ok", v3["ok"] and v3["counts"]["total"] == 0, str(v3["counts"]))

    # prompt_action_spec: generation-time constraint enumerates ONLY enabled verbs with grammar + the contract rule
    spec = prompt_action_spec(whitelist=wl)
    chk("spec_lists_enabled_verb", "logbook_entry:<short text>" in spec, spec[:200])
    chk("spec_has_contract_rule", "Any world change must go through" in spec and "use only" in spec.lower(), "")
    chk("spec_hides_gated_verbs", "relation_delta_limited" not in spec and "credit_transfer" not in spec, spec)

    # markdown-fenced JSON (the common real Player2 wrapping) still parses
    fenced = '```json\n{"response":"Understood.","actions":["logbook:done"]}\n```'
    v4 = validate_actions(fenced, whitelist=wl)
    chk("fenced_json_parsed", v4["reply"] == "Understood." and v4["counts"]["total"] == 1
        and v4["actions"][0]["type"] == "logbook_entry", str(v4["counts"]) + "|" + v4["reply"])

    passed = sum(1 for c in checks if c["ok"])
    return {"ok": passed == len(checks), "passed": passed, "total": len(checks), "checks": checks}


if __name__ == "__main__":
    print(json.dumps(run_actions_selftest(), indent=1))
