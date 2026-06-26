# X4 Neural Link

X4 Neural Link is the standalone bridge mod for local AI integration.

Its job is narrow: expose a reusable, plug-and-play X4-to-localhost bridge that talks to Player2. It must not contain AI Influence gameplay logic, faction diplomacy rules, or files owned by other mods.

Current status: staged roadmap/skeleton only. The working source material is in the backed-up `x4_ai_influence` extension and its known-working bridge snapshot.

Default endpoints:

- Bridge: `http://127.0.0.1:8713`
- Player2: `http://127.0.0.1:4315`

Active roadmap: `ROADMAP.md`.

## Start

Run:

```powershell
.\Start-Neural-Link.ps1
```

or double-click `Start-Neural-Link.bat`.

Health check:

```powershell
Invoke-RestMethod http://127.0.0.1:8713/health
```

Smoke test:

```powershell
python tests/smoke_bridge.py
```
