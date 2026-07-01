# Neural Link — mod ↔ bridge contract (source of truth)

The X4 mod talks to **this bridge**, never Player2 directly:

```
X4 (MD/Lua)  →  djfhe_http (HTTP)  →  Neural Link bridge :8713  →  Player2 :4315
```

Live, always-current version: `GET http://127.0.0.1:8713/api/contract`. This file is the
versioned snapshot the Forge / mod author references so generated artifacts are correct
and the mod + bridge never drift.

## How a mod calls the bridge (djfhe — the CORRECT fluent API)

```lua
local Request = require("djfhe.http.request")
Request.new("POST")
  :setUrl("http://127.0.0.1:8713/v1/request")
  :setBody({
    request_id  = "r1",
    source_mod  = "your_mod",
    channel     = "npc",
    target      = { mode = "npc", save_id = "SAVE", game_id = "GAME",
                    npc_name = "NAME", faction_id = "argon",
                    game_time = 0 },            -- player.age (recommended; powers aging)
    messages    = { { role = "user", content = "..." } },
  })
  :send(function(response, err)
    if err then return end
    local data = response:getJson()             -- { ok, request_id, status }
  end)
```

> NOTE: it is `Request.new("POST")` (dot-call, method string), **not** `Request:new({...})`.
> djfhe is registered in the Forge's api-registry with this correct scaffold.

## Endpoints the mod uses

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/request` | Submit an NPC/influence request → `{ok, request_id, status:"accepted"}`. Then poll. |
| GET | `/v1/response/{request_id}` | Fetch a completed response → `{ok, response:{ reply, actions }}`. |
| GET | `/v1/updates_pool` | Drain all completed responses since last call → `{ok, updates:[...]}`. |
| GET | `/api/test/llm_action?faction=argon` | TEST: one LLM-determined action → `{ok, action:{type, params}}`. |
| GET | `/api/memory/npc/delete?save_id=&npc_id=` | Purge a dead NPC + memory (call on death). |
| GET | `/health` | Bridge + Player2 health. |

## The action contract (what Player2 may propose, and what X4 may execute)

The governing reference is the captured Bannerlord AI Influence pattern:

```
game context -> Player2 prompt -> JSON response with response/actions[] -> validator -> game execution
```

X4 normalizes that into:

```json
{
  "reply": "in-character text",
  "actions": [
    {
      "type": "<action_type>",
      "params": {},
      "description": "optional player-facing proposal text",
      "needs_confirm": false
    }
  ]
}
```

Player2 may propose actions. The bridge and X4 dispatcher decide whether each action is legal, bounded, affordable,
off cooldown, and grounded in live game objects. Unknown or illegal action types are ignored/rejected and audited.
Failed/unparsed Player2 decisions defer; deterministic scoring must not silently substitute a real action.

**Whitelisted `action_type`s** (the MD dispatcher must have a handler per type it accepts;
unknown types are ignored — the whitelist is enforced on BOTH sides):

`dialogue_only`, `defensive_stance`, `resource_request`, `escalate_pressure`,
`ceasefire_feeler`, `trade_offer`, `sanction`, plus the UI-test `show_notification`.

Current refactor target whitelist, in proof order:

`dialogue_only`, `memory_write`, `logbook_entry`, `status_update`,
`relation_delta_limited`, `threaten`, `attack_intent`, `mission_offer`,
`trade_request`, `temporary_diplomatic_flag`, `faction_to_faction_proposal`.

## Player2 API (reference only — consumed by the BRIDGE, not the mod)

The bridge pulls Player2's live OpenAPI; see `GET /api/player2/catalog` (developer + npc
specs, capability-classified). The mod never calls Player2 directly, so this is reference
for bridge development, not a contract the mod implements.
