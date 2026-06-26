# X4 native action cheat-sheet (our own handlers)

Learned from **DeadAir Dynamic Universe** (reference only) + cross-checked against our already-proven `ai_influence_actions.xml` (ran in X4, validates clean in the Forge). **These are native X4 MD verbs we implement ourselves — no DeadAir dependency, nothing copied.** DeadAir confirmed the same primitives: `set_faction_relation` (6×), `write_to_logbook` (9×), `relationto` reads (18×), `signal_cue` (66×).

## 1. Faction relations — the core verb
- **Read:** `$faction.relationto.{$other}` → float in `[-1.0, 1.0]`.
- **Set:** `<set_faction_relation faction="$a" otherfaction="$b" value="$v"/>` (`$v` in `[-1,1]`).
- **Resolve id → object:** `if faction.{$id}? then faction.{$id} else null`; the player is `faction.player`.

## 2. War / peace = relation thresholds (deterministic, no special "declare war" verb)
You don't "call war" — you drop the relation below a threshold and the engine + faction AI take over. Thresholds (from our proven dispatcher, DeadAir-derived):

| const | value | meaning |
|---|---|---|
| `WAR_ELIGIBLE` | −0.10 | crossing *below* = hostilities |
| `WAR_STRONG` | −0.32 | strong war |
| `WAR_AUTO` | −0.50 | guaranteed war |
| `PEACE_ELIGIBLE` | −0.01 | crossing *above* = peace possible |
| `PEACE_AUTO` | 0.0 | guaranteed peace |

- **Declare war** = `set_faction_relation(... value ≤ WAR_ELIGIBLE)`. Fire the war path only when `oldRel > WAR_ELIGIBLE AND newRel ≤ WAR_ELIGIBLE` (the *crossing*, so we don't re-declare).
- **Declare peace** = `set_faction_relation(... value > PEACE_ELIGIBLE)`. Fire only when `oldRel ≤ WAR_ELIGIBLE AND newRel > PEACE_ELIGIBLE`.

## 3. News / logbook — immersion
- **Logbook entry:** `<write_to_logbook category="alerts" title="'WAR DECLARED: '+$a.knownname+' vs '+$b.knownname" text="'A new conflict has been initiated.'" faction="$a"/>`
- **Player alert:** `<show_notification text="'...'" priority="8"/>`
- DeadAir layers a news-grouping/output system on top; for our slice, **logbook + notification suffice**. (Grouping is a scale-phase nicety.)

## 4. Our handler shape (the in-game adapter)
`Dispatch` receives `{type, params}` from the bridge → handler resolves factions → reads `oldRel` → `set_faction_relation(newRel)` → if a threshold was *crossed*, `write_to_logbook` + `show_notification`. **The deterministic guards live here** — this is the in-game half of the validator, mirroring the headless world model. No external mod calls; fully self-contained.

## 5. Scope note
For the first slice we only need: **set relation, war/peace threshold crossing, logbook, notification.** DeadAir's deeper machinery (war target selection, favors, relations-fix, news grouping, station/economy evolution) is reference for *later* economic/territorial actions — not the slice.
