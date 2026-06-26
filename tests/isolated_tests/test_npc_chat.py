import requests
import json
import time
import sys

BASE_URL = "http://127.0.0.1:8713"

def run_npc_tests():
    print("--- Running Staged NPC Chat Tests ---")
    
    # 1. Health check of bridge
    try:
        r = requests.get(f"{BASE_URL}/health", timeout=5)
        print(f"Bridge Health: {r.status_code}")
        health = r.json()
        print(json.dumps(health, indent=2))
        if not health.get("ok"):
            print("Bridge health is not OK. Aborting.")
            return False
    except Exception as e:
        print(f"Failed to reach bridge: {e}")
        return False

    # 2. Test API player2/npc_chat endpoint
    payload = {
        "request_id": f"npc-test-{int(time.time())}",
        "source_mod": "x4_neural_link_test",
        "channel": "npc",
        "messages": [
            {"role": "user", "content": "Hello, who are you?"}
        ],
        "target": {
            "npc_name": "Dal Busta",
            "npc_short_name": "Dal",
            "character_description": "Dal Busta is a clever, plotting strategist who knows everyone and everything in the X4 universe.",
            "sender_name": "Player",
            "voice_id": "01955d76-ed5b-74de-83e5-800a44fee0d1"
        },
        "metadata": {
            "game_id": "x4_neural_link_test"
        }
    }
    
    print("\nSending synchronous NPC chat request...")
    start = time.time()
    try:
        r = requests.post(f"{BASE_URL}/api/player2/npc_chat", json=payload, timeout=30)
        latency = time.time() - start
        print(f"Status: {r.status_code} in {latency:.2f}s")
        if r.status_code == 200:
            resp = r.json()
            print("Response:")
            print(json.dumps(resp, indent=2))
            if resp.get("ok") and resp.get("response", {}).get("status") == "ok":
                print("NPC Chat successful!")
                return True
            else:
                print("NPC Chat response status not OK.")
                return False
        else:
            print(r.text)
            return False
    except Exception as e:
        print(f"NPC chat request failed: {e}")
        return False

if __name__ == "__main__":
    success = run_npc_tests()
    sys.exit(0 if success else 1)
