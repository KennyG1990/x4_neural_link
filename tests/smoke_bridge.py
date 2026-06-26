from __future__ import annotations

import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError


BASE = "http://127.0.0.1:8713"


def request_json(method: str, path: str, payload: dict | None = None, timeout: int = 10) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(f"{BASE}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def main() -> int:
    status, health = request_json("GET", "/health")
    assert status == 200 and health["ok"] is True, health
    print("HEALTH_OK", json.dumps(health, indent=2))

    request_id = f"smoke-{int(time.time())}"
    payload = {
        "request_id": request_id,
        "source_mod": "x4_neural_link_smoke",
        "channel": "chat",
        "messages": [
            {"role": "system", "content": "You are a short bridge smoke-test responder."},
            {"role": "user", "content": "Reply with the words Neural Link online."},
        ],
        "target": {"max_tokens": 64, "temperature": 0.1},
        "metadata": {"test": True},
    }
    status, accepted = request_json("POST", "/v1/request", payload)
    assert status == 202 and accepted["ok"] is True, accepted
    print("REQUEST_ACCEPTED", json.dumps(accepted, indent=2))

    status, duplicate = request_json("POST", "/v1/request", payload)
    assert status == 202 and duplicate["ok"] is True and duplicate.get("duplicate") is True, duplicate
    print("DUPLICATE_OK", json.dumps(duplicate, indent=2))

    response = None
    for _ in range(45):
        status, data = request_json("GET", f"/v1/response/{request_id}", timeout=5)
        if status == 200:
            response = data["response"]
            break
        time.sleep(1)

    assert response is not None, "response did not complete"
    print("RESPONSE_OK", json.dumps(response, indent=2))

    status, updates = request_json("GET", "/v1/updates_pool")
    assert status == 200 and updates["ok"] is True, updates
    assert any(u.get("request_id") == request_id for u in updates["updates"]), updates
    print("UPDATES_OK", json.dumps(updates, indent=2))

    status, telemetry = request_json("GET", "/api/telemetry?limit=10")
    assert status == 200 and telemetry["counts"]["total"] >= 1, telemetry
    assert "db_state" in telemetry and "bridge_requests" in telemetry["db_state"]["tables"], telemetry
    print("TELEMETRY_OK", json.dumps({"counts": telemetry["counts"], "db_state": telemetry["db_state"]}, indent=2))

    status, capabilities = request_json("GET", "/api/player2/capabilities")
    assert status == 200 and capabilities["ok"] is True, capabilities
    assert len(capabilities["endpoints"]) >= 1, capabilities
    assert capabilities["counts"].get("safe_probe", 0) >= 1, capabilities
    assert capabilities["counts"].get("destructive", 0) >= 1, capabilities
    print("CAPABILITIES_OK", json.dumps({"counts": capabilities["counts"]}, indent=2))

    status, invalid = request_json(
        "POST",
        "/v1/request",
        {
            "request_id": "../bad",
            "source_mod": "x4_neural_link_smoke",
            "channel": "chat",
            "messages": [{"role": "user", "content": "bad"}],
        },
    )
    assert status == 400 and invalid["ok"] is False, invalid
    print("INVALID_REJECT_OK", json.dumps(invalid, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"SMOKE_FAIL: {exc}", file=sys.stderr)
        raise
