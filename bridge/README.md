# Neural Link Bridge Runtime

This folder will hold only bridge-owned runtime code:

- localhost HTTP server on `127.0.0.1:8713`
- Player2 client/adapter for `127.0.0.1:4315`
- request/response contracts for X4 mods
- logging, idempotency, timeout, and health checks
- launcher scripts needed to start the bridge

It must not hold AI Influence faction personalities, diplomacy policy, generated incidents, old processed response logs, cache folders, or files copied from unrelated mods.
