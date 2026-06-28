from __future__ import annotations

import json
import re
import socket
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .contracts import NeuralRequest, NeuralResponse

try:
    from .rolerag import RoleRAG
except ImportError:  # allow direct (non-package) import in tests
    from rolerag import RoleRAG

try:
    from .persona import PersonaCardBuilder
except ImportError:
    from persona import PersonaCardBuilder


class Player2Client:
    """Small stdlib-only Player2 client for Neural Link."""

    def __init__(self, base_url: str, game_client_id: str, timeout_seconds: int = 30, memory_store: Any = None,
                 chat_concurrency: int = 3):
        self.base_url = base_url.rstrip("/")
        self.game_client_id = game_client_id
        self.timeout_seconds = timeout_seconds
        # Optional durable NPC memory (bridge.memory.MemoryStore). When present,
        # NPC-id bindings persist, prior memory is injected into each turn, and the
        # exchange is recorded + condensed.
        self.memory = memory_store
        # SPEC 1k: RoleRAG boundary-aware retrieval (paper §3.4) — caches a canonical X4 entity index per
        # save and routes per-message entities by cognitive scope (specific / general / out-of-scope).
        self._rolerag = RoleRAG(memory_store) if memory_store is not None else None
        # SPEC 2a: per-NPC PersonaCard + authority model — situated roleplay within an authority boundary.
        self._persona = PersonaCardBuilder(memory_store) if memory_store is not None else None
        # CONCURRENCY GATE (#68, 2026-06-26). Player2 is a client/gateway to a HOSTED model (NOT local — a 120B
        # can't run on the user's GPU), so a hosted backend serves parallel requests natively. We previously
        # serialized ALL chat with a single Lock(); that throttled news/narrator/reactions/chat to one-at-a-time.
        # Now a BOUNDED semaphore (cap configurable, default 2): the per-request threads (server spawns one per
        # /v1/request) can hit Player2 up to `chat_concurrency` at once. Bounded, not unlimited — Player2's API
        # may have a max-concurrent/rate ceiling; validate live and tune (revert to 1 if it 429s/destabilises).
        self._chat_concurrency = max(1, int(chat_concurrency))
        self._chat_lock = threading.BoundedSemaphore(self._chat_concurrency)
        # A7 (blueprint §19/§20): per-session LLM-call budget + kill switch so the mod can never silently drain
        # the user's Player2 AI power ("joules"). The bridge can't see per-call joule cost, so it caps CALLS.
        # budget=0 → unlimited (opt-in cap, default); killed=True → all generation returns a graceful fallback.
        self._llm_lock = threading.Lock()
        self._llm_calls = 0
        self._llm_budget = 0
        self._llm_killed = False
        # NPC API state: a cache of spawned NPCs keyed by persona, plus a resolved
        # default voice id. The NPC API (/v1/npc/...) is the supported path for game
        # characters and, unlike raw /v1/chat/completions, returns clean message +
        # command reliably with the same reasoning model.
        self._npc_cache: dict[tuple, str] = {}
        self._npc_lock = threading.Lock()
        self._voice_id: str | None = None

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "player2-game-key": self.game_client_id,
            "X-Game-Client-Id": self.game_client_id,
        }

    def _json(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=timeout or self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Player2 HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Player2 connection failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Player2 request timed out") from exc

    def probe_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: int = 10,
    ) -> dict[str, Any]:
        start = time.time()
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                body = json.loads(raw) if raw else {}
                return {
                    "ok": True,
                    "status_code": resp.status,
                    "latency_ms": int((time.time() - start) * 1000),
                    "response": body,
                }
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body: Any = json.loads(raw)
            except Exception:
                body = {"raw": raw[:4000]}
            return {
                "ok": False,
                "status_code": exc.code,
                "latency_ms": int((time.time() - start) * 1000),
                "error": f"HTTP {exc.code}",
                "response": body if isinstance(body, dict) else {"value": body},
            }
        except Exception as exc:
            return {
                "ok": False,
                "status_code": None,
                "latency_ms": int((time.time() - start) * 1000),
                "error": str(exc),
                "response": {},
            }

    def health(self) -> dict[str, Any]:
        try:
            data = self._json("GET", "/v1/health", timeout=5)
            return {"ok": True, "client_version": data.get("client_version"), "base_url": self.base_url}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "base_url": self.base_url}

    def models(self) -> dict[str, Any]:
        try:
            data = self._json("GET", "/v1/models", timeout=5)
            model_ids = [item.get("id") for item in data.get("data", []) if isinstance(item, dict)]
            return {"ok": True, "models": model_ids}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "models": []}

    def run_probe_suite(self) -> list[dict[str, Any]]:
        probes: list[tuple[str, str, str, dict[str, Any] | None, int]] = [
            ("health", "GET", "/v1/health", None, 5),
            ("models", "GET", "/v1/models", None, 5),
            ("selected_characters", "GET", "/v1/selected_characters", None, 5),
            ("developer_openapi", "GET", "/v1/openapi.json", None, 5),
            ("npc_openapi", "GET", "/v1/npc/openapi.json", None, 5),
            ("ai_profiles", "GET", "/v1/ai_profiles", None, 5),
            ("joules", "GET", "/v1/joules", None, 5),
            ("stt_languages", "GET", "/v1/stt/languages", None, 5),
            ("stt_language", "GET", "/v1/stt/language", None, 5),
            ("stt_whisper_models", "GET", "/v1/stt/whisper/models", None, 5),
            ("tts_eleven_models", "GET", "/v1/tts/eleven/models", None, 5),
            ("tts_eleven_user", "GET", "/v1/tts/eleven/user", None, 5),
            ("tts_eleven_subscription", "GET", "/v1/tts/eleven/user/subscription", None, 5),
            ("tts_eleven_voices", "GET", "/v1/tts/eleven/voices", None, 5),
            ("tts_eleven_default_voice_settings", "GET", "/v1/tts/eleven/voices/settings/default", None, 5),
            ("tts_voices", "GET", "/v1/tts/voices", None, 5),
            ("tts_volume", "GET", "/v1/tts/volume", None, 5),
            (
                "chat_completions",
                "POST",
                "/v1/chat/completions",
                {
                    "messages": [{"role": "user", "content": "Reply with exactly: neural-link-probe"}],
                    "stream": False,
                    # Player2's default chat model (e.g. GLM-4.7-Flash) is a reasoning
                    # model that spends ~130-160 completion tokens on hidden reasoning
                    # before emitting visible content. A small max_tokens budget (e.g. 32)
                    # is consumed entirely by reasoning, so message.content comes back empty
                    # and the probe falsely reports "no usable text". Give it real headroom.
                    "max_tokens": 256,
                    "temperature": 0.1,
                },
                self.timeout_seconds,
            ),
        ]
        results: list[dict[str, Any]] = []
        for name, method, path, payload, timeout in probes:
            result = self.probe_json(method, path, payload, timeout=timeout)
            result.update({"name": name, "method": method, "path": path})
            if name == "chat_completions" and result.get("ok"):
                text = self._extract_reply(result.get("response") or {})
                result["usable_text"] = bool(text)
                if not text:
                    result["ok"] = False
                    result["error"] = "chat completion returned no text content"
            results.append(result)
        return results

    def api_catalog(self) -> dict[str, Any]:
        specs = [
            ("developer", "/v1/openapi.json"),
            ("npc", "/v1/npc/openapi.json"),
        ]
        documents = []
        endpoints = []
        for name, path in specs:
            result = self.probe_json("GET", path, timeout=5)
            document = {
                "name": name,
                "path": path,
                "ok": bool(result.get("ok")),
                "status_code": result.get("status_code"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error"),
            }
            documents.append(document)
            spec = result.get("response") if isinstance(result.get("response"), dict) else {}
            for route, methods in (spec.get("paths") or {}).items():
                if not isinstance(methods, dict):
                    continue
                for method, info in methods.items():
                    if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                        continue
                    info = info if isinstance(info, dict) else {}
                    endpoints.append(
                        {
                            "spec": name,
                            "method": method.upper(),
                            "path": route,
                            "operation_id": info.get("operationId"),
                            "summary": info.get("summary"),
                            "tags": info.get("tags") or [],
                            "mutating": method.lower() in {"post", "put", "delete", "patch"},
                            "capability": self._classify_endpoint(method.upper(), route, info),
                        }
                    )
        endpoints.sort(key=lambda item: (str(item["spec"]), str(item["path"]), str(item["method"])))
        return {"ok": all(doc["ok"] for doc in documents), "documents": documents, "endpoints": endpoints}

    def capability_matrix(self) -> dict[str, Any]:
        catalog = self.api_catalog()
        capabilities = catalog.get("endpoints", [])
        counts: dict[str, int] = {}
        for endpoint in capabilities:
            key = str(endpoint.get("capability", {}).get("class", "unknown"))
            counts[key] = counts.get(key, 0) + 1
        return {
            "ok": catalog.get("ok", False),
            "documents": catalog.get("documents", []),
            "counts": counts,
            "endpoints": capabilities,
        }

    @staticmethod
    def _classify_endpoint(method: str, route: str, info: dict[str, Any]) -> dict[str, str]:
        mutating = method in {"POST", "PUT", "DELETE", "PATCH"}
        tags = {str(tag).lower() for tag in (info.get("tags") or [])}
        route_lower = route.lower()
        summary = str(info.get("summary") or "").lower()

        if "login" in route_lower:
            return {
                "class": "external_auth",
                "risk": "medium",
                "reason": "starts an authentication flow",
            }

        if "{" in route or "}" in route:
            if method == "GET" and "{job_id}" in route:
                return {
                    "class": "fixture_required",
                    "risk": "low",
                    "reason": "requires a known job id fixture",
                }
            if "kill" in route_lower or method == "DELETE":
                return {
                    "class": "destructive",
                    "risk": "high",
                    "reason": "can remove game, NPC, or stored state",
                }
            return {
                "class": "fixture_required",
                "risk": "medium" if mutating else "low",
                "reason": "requires a known path fixture before it can be tested safely",
            }

        if "image" in tags or "sprite" in tags or "video" in tags or "model3d" in tags:
            return {
                "class": "costly_or_async",
                "risk": "medium",
                "reason": "can consume credits or create asynchronous media jobs",
            }

        if "logs/upload" in route_lower:
            return {
                "class": "upload_external",
                "risk": "medium",
                "reason": "transmits log data to Player2",
            }

        if "tts" in tags and mutating:
            return {
                "class": "side_effect",
                "risk": "medium",
                "reason": "can play speech or change audio state",
            }

        if "stt" in tags and mutating:
            return {
                "class": "side_effect",
                "risk": "medium",
                "reason": "can start, stop, or alter speech recognition state",
            }

        if route_lower == "/chat/completions":
            return {
                "class": "safe_probe",
                "risk": "low",
                "reason": "non-streaming short prompt probe is allowed by bridge diagnostics",
            }

        if mutating:
            return {
                "class": "side_effect",
                "risk": "medium",
                "reason": "mutating endpoint without a dedicated safe fixture",
            }

        if method == "GET":
            return {
                "class": "safe_probe",
                "risk": "low",
                "reason": "read-only endpoint without required path fixtures",
            }

        return {
            "class": "unknown",
            "risk": "unknown",
            "reason": summary or "no classification rule matched",
        }

    def _llm_gate(self) -> "str | None":
        """A7: return a block-reason if generation must be refused (kill switch on, or call budget exhausted),
        else None — and count the call when allowed. Thread-safe. The ONE chokepoint for both complete paths."""
        with self._llm_lock:
            if self._llm_killed:
                return "AI Influence is paused (kill switch on) — no AI power is being used."
            if self._llm_budget and self._llm_calls >= self._llm_budget:
                return "AI power budget for this session is exhausted."
            self._llm_calls += 1
            return None

    def llm_status(self) -> dict[str, Any]:
        with self._llm_lock:
            return {"calls": self._llm_calls, "budget": self._llm_budget, "killed": self._llm_killed,
                    "remaining": (max(0, self._llm_budget - self._llm_calls) if self._llm_budget else None)}

    def set_llm_controls(self, budget: Any = None, killed: Any = None, reset: bool = False) -> dict[str, Any]:
        with self._llm_lock:
            if reset:
                self._llm_calls = 0
            if budget is not None:
                self._llm_budget = max(0, int(budget))
            if killed is not None:
                self._llm_killed = bool(killed)
        return self.llm_status()

    def complete(self, request: NeuralRequest) -> NeuralResponse:
        start = time.time()
        _blocked = self._llm_gate()
        if _blocked:
            return NeuralResponse.safe_error(request, _blocked, latency_ms=0)
        # Player2's default chat model (GLM-4.7-Flash) is a reasoning model: it spends
        # a large, variable number of completion tokens on hidden reasoning before
        # emitting visible content. If max_tokens is hit mid-reasoning, message.content
        # comes back empty even though the call "succeeded". This is intermittent and
        # prompt-dependent, so we (1) give generous headroom and (2) retry once with an
        # even larger budget before giving up.
        requested_max = int(request.target.get("max_tokens", 512) or 512)
        temperature = request.target.get("temperature", 0.4)
        # First attempt: real headroom for reasoning + answer.
        token_budgets = [max(requested_max, 512), 1024]

        last_error: str | None = None
        # Serialize the actual Player2 chat calls so concurrent requests don't thrash.
        with self._chat_lock:
            for attempt, budget in enumerate(token_budgets):
                payload: dict[str, Any] = {
                    "messages": request.messages,
                    "stream": False,
                    "temperature": temperature,
                    "max_tokens": budget,
                }
                try:
                    data = self._json("POST", "/v1/chat/completions", payload, timeout=self.timeout_seconds)
                    reply = self._extract_reply(data)
                    if reply:
                        latency = int((time.time() - start) * 1000)
                        return NeuralResponse(
                            request_id=request.request_id,
                            status="ok",
                            source_mod=request.source_mod,
                            channel=request.channel,
                            reply=reply,
                            actions=[],
                            latency_ms=latency,
                        )
                    # Empty content: the reasoning pass likely consumed the whole budget.
                    # Retry with a larger budget on the next loop iteration.
                    last_error = "Player2 returned a completion without text content"
                except Exception as exc:
                    # A hard error (timeout, connection) is not worth retrying with more
                    # tokens, so stop and report it.
                    latency = int((time.time() - start) * 1000)
                    return NeuralResponse.safe_error(request, str(exc), latency_ms=latency)

        latency = int((time.time() - start) * 1000)
        return NeuralResponse.safe_error(
            request,
            last_error or "Player2 returned no usable text content after retry",
            latency_ms=latency,
        )

    # ------------------------------------------------------------------ NPC API

    def _request_text(self, method: str, path: str, payload: dict[str, Any] | None = None, timeout: int | None = None) -> str:
        """Like _json but returns the raw decoded body (some NPC endpoints return a bare string)."""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = Request(f"{self.base_url}{path}", data=data, headers=self._headers(), method=method)
        try:
            with urlopen(req, timeout=timeout or self.timeout_seconds) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Player2 HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Player2 connection failed: {exc.reason}") from exc

    def default_voice_id(self) -> str:
        if self._voice_id:
            return self._voice_id
        try:
            data = self._json("GET", "/v1/tts/voices", timeout=5)
            voices = data if isinstance(data, list) else (data.get("voices") or data.get("data") or [])
            for v in voices:
                if isinstance(v, dict):
                    vid = v.get("id") or v.get("voice_id")
                    if vid:
                        self._voice_id = str(vid)
                        break
        except Exception:
            pass
        # Stable fallback (Caleb, american_english) if voice listing is unavailable.
        return self._voice_id or "01955d76-ed5b-74de-83e5-800a44fee0d1"

    # In-universe guardrail prepended to EVERY NPC persona so the LLM stays in character and never
    # leaks real-world / other-fiction knowledge (e.g. identifying "Darth Vader" — an immersion break).
    # POSITIVE framing only — never enumerate what is "not X4" (that set is infinite). State what the
    # character KNOWS; the model generalises that anything outside it is unknown.
    X4_IN_CHARACTER = (
        "You are a person who lives entirely within the universe of X4: Foundations and have never "
        "left it. You know ONLY this galaxy — its factions, sectors, jump gates, stations, ships, "
        "wares, technology, and history. Anything that is not part of this galaxy, you have simply "
        "never heard of: do not recognise, describe, explain, or speculate about it — react with "
        "honest confusion and ask what the player means in terms of this galaxy. You are not an AI "
        "assistant; you are this person, and you never break character."
    )

    def _compose_system_prompt(self, persona: dict[str, Any]) -> str:
        base = str(persona.get("system_prompt") or "").strip()
        parts = [self.X4_IN_CHARACTER]
        if base and base not in self.X4_IN_CHARACTER:
            parts.append(base)
        parts.append("Reply in one or two short sentences. Never include analysis or reasoning, and never break character.")
        return "\n\n".join(parts)

    def npc_spawn(self, game_id: str, persona: dict[str, Any]) -> str:
        body = {
            "short_name": str(persona.get("short_name") or persona.get("name") or "NPC")[:48],
            "name": str(persona.get("name") or "Neural Link NPC")[:96],
            "character_description": str(persona.get("character_description") or persona.get("system_prompt") or self.X4_IN_CHARACTER),
            "system_prompt": self._compose_system_prompt(persona),
            "voice_id": str(persona.get("voice_id") or self.default_voice_id()),
            "keep_game_state": False,
        }
        raw = self._request_text("POST", f"/v1/npc/games/{game_id}/npcs/spawn", body, timeout=30).strip()
        try:
            val = json.loads(raw)
            if isinstance(val, str):
                return val
            if isinstance(val, dict):
                return str(val.get("npc_id") or val.get("id") or "")
        except Exception:
            pass
        return raw.strip('"')

    @staticmethod
    def _persona_key(game_id: str, persona: dict[str, Any]) -> tuple:
        return (
            game_id,
            str(persona.get("system_prompt") or ""),
            str(persona.get("name") or ""),
            str(persona.get("voice_id") or ""),
        )

    def _ensure_npc(self, game_id: str, persona: dict[str, Any]) -> str:
        key = self._persona_key(game_id, persona)
        with self._npc_lock:
            npc_id = self._npc_cache.get(key)
        if npc_id:
            return npc_id
        npc_id = self.npc_spawn(game_id, persona)
        with self._npc_lock:
            self._npc_cache[key] = npc_id
        return npc_id

    def _forget_npc(self, game_id: str, persona: dict[str, Any]) -> None:
        with self._npc_lock:
            self._npc_cache.pop(self._persona_key(game_id, persona), None)

    def npc_chat(
        self,
        game_id: str,
        npc_id: str,
        sender_name: str,
        sender_message: str,
        game_state_info: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Open the NPC responses stream, post a chat, and read the NDJSON reply.

        Player2's /v1/npc/.../responses endpoint streams newline-delimited JSON
        objects ({npc_id, message, command, audio}) — NOT data:-prefixed SSE. We open
        the stream first so the reply isn't missed, post the chat, then read lines
        until the object whose npc_id matches arrives (or we hit the deadline).
        """
        timeout = timeout or self.timeout_seconds
        stream_url = f"{self.base_url}/v1/npc/games/{game_id}/npcs/responses"
        stream_req = Request(stream_url, headers={**self._headers(), "Accept": "application/x-ndjson"}, method="GET")
        stream = urlopen(stream_req, timeout=timeout)
        try:
            chat_payload: dict[str, Any] = {"sender_name": sender_name, "sender_message": sender_message}
            if game_state_info:
                chat_payload["game_state_info"] = game_state_info
            # 404 here means the cached NPC expired; surface it so the caller can respawn.
            self._json("POST", f"/v1/npc/games/{game_id}/npcs/{npc_id}/chat", chat_payload, timeout=timeout)

            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    line = stream.readline()
                except (socket.timeout, TimeoutError):
                    break
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("npc_id") == npc_id and obj.get("message"):
                    return {"message": str(obj.get("message")), "command": obj.get("command")}
            return {"message": "", "command": None}
        finally:
            try:
                stream.close()
            except Exception:
                pass

    @staticmethod
    def _strip_speaker(message: str) -> str:
        # Player2 prefixes replies with the speaker, e.g. "<Argon> Hello." Strip it.
        return re.sub(r"^\s*<[^>]{0,48}>\s*", "", message or "").strip()

    @staticmethod
    def _system_prompt_from(request: NeuralRequest) -> str:
        parts = [m.get("content", "") for m in request.messages if m.get("role") == "system" and m.get("content")]
        explicit = request.target.get("system_prompt") or request.metadata.get("system_prompt")
        if explicit:
            return str(explicit)
        if parts:
            return "\n".join(parts)
        return "You are a character in the X4 universe. Reply in one or two short sentences. Never include analysis or reasoning."

    @staticmethod
    def _last_user_message(request: NeuralRequest) -> str:
        for m in reversed(request.messages):
            if m.get("role") == "user" and m.get("content"):
                return str(m.get("content"))
        # Fall back to the last message of any role.
        return str(request.messages[-1].get("content", "")) if request.messages else ""

    @staticmethod
    def _x4_safe(text: str) -> str:
        """X4's in-game font lacks smart punctuation (curly quotes, en/em dashes, ellipsis, NBSP) —
        they render as tofu boxes. Normalize to ASCII so dialogue displays cleanly."""
        if not text:
            return text
        for bad, good in (
            ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
            ("—", "-"), ("–", "-"), ("…", "..."), (" ", " "),
            ("′", "'"), ("″", '"'), ("«", '"'), ("»", '"'),
        ):
            text = text.replace(bad, good)
        # Hyphen/dash family + stray spaces/zero-width chars that render as tofu boxes in X4's font
        # (the "cease‑fire" box was U+2011 non-breaking hyphen). Use \u escapes so the source bytes
        # are unambiguous.
        for cp, good in (
            ("‐", "-"), ("‑", "-"), ("‒", "-"), ("―", "-"),
            ("−", "-"), ("­", "-"), ("•", "-"),
            (" ", " "), (" ", " "), (" ", " "), (" ", " "),
            ("​", ""), ("‌", ""), ("‍", ""), ("﻿", ""),
            ("‚", ","), ("„", '"'),
        ):
            text = text.replace(cp, good)
        # Definitive catch-all keyed by CODEPOINT (ASCII source, unambiguous) — covers anything the
        # literal-character maps above might miss or that an editor normalized away. This is what
        # actually kills the "cease-fire" tofu box (U+2011) and its dash/space/zero-width cousins.
        _dash = {0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212, 0x00AD, 0x2022, 0x2043}
        _space = {0x00A0, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000}
        _zero = {0x200B, 0x200C, 0x200D, 0xFEFF}
        _out = []
        for ch in text:
            o = ord(ch)
            if o < 0x80:
                _out.append(ch)
            elif o in _dash:
                _out.append("-")
            elif o in _space:
                _out.append(" ")
            elif o in _zero:
                continue
            elif o == 0x2026:
                _out.append("...")
            elif o in (0x2018, 0x2019, 0x2032):
                _out.append("'")
            elif o in (0x201C, 0x201D, 0x2033, 0x00AB, 0x00BB):
                _out.append('"')
            else:
                _out.append(ch)
        return "".join(_out)

    @staticmethod
    def _parse_suggestions(raw: str, count: int) -> list[dict[str, str]]:
        """Pull [{label,line}] out of the model's reply, defensively (it may wrap JSON in prose
        or use loose formatting). Always returns exactly `count` items."""
        out: list[dict[str, str]] = []
        try:
            start, end = raw.find("["), raw.rfind("]")
            if start >= 0 and end > start:
                for item in json.loads(raw[start:end + 1]):
                    if isinstance(item, dict):
                        label = str(item.get("label") or item.get("title") or "").strip()
                        line = str(item.get("line") or item.get("text") or item.get("prompt") or "").strip()
                        if line:
                            out.append({"label": (label or " ".join(line.split()[:3]))[:40], "line": line[:240]})
        except Exception:
            pass
        if len(out) < count:  # fallback: salvage non-empty lines
            for ln in (l.strip("-*0123456789. \t") for l in raw.splitlines()):
                if ln and len(out) < count and not ln.startswith(("[", "{", "]")):
                    out.append({"label": " ".join(ln.split()[:3])[:40], "line": ln[:240]})
        while len(out) < count:
            out.append({"label": "Say something", "line": "I have something to ask you."})
        return [{"label": Player2Client._x4_safe(o["label"]),
                 "line": Player2Client._x4_safe(o["line"])} for o in out[:count]]

    def generate_suggestions(self, save_id: str, game_id: str, faction_id: str, npc_name: str,
                             count: int = 3, recent_message: str = "") -> list[dict[str, str]]:
        """ME-wheel openers: `count` short paraphrase labels + the fuller line the player would say,
        RAG-grounded in the NPC's faction standing + recent memory. In-world only."""
        fac = str(faction_id or "argon")
        ctx_parts: list[str] = []
        in_progress = False
        last_npc = ""
        if self.memory is not None:
            npc_key = self.memory.make_key(save_id, game_id, npc_name or "")
            # Read the conversation FIRST so we know whether it is in progress.
            try:
                turns = self.memory.get_recent_turns(npc_key, limit=6)
                if turns:
                    in_progress = True
                    convo = "\n".join(
                        (("Player" if str(t.get("role")) == "user" else (npc_name or "NPC")) + ": " + str(t.get("content") or ""))
                        for t in turns)
                    last_npc = next((str(t.get("content") or "") for t in reversed(turns) if str(t.get("role")) != "user"), "")
                    ctx_parts.append("The conversation so far:\n" + convo)
            except Exception:
                pass
            # World context (faction mood / wars / sectors / personal memory + faction standing) ONLY for
            # OPENERS. During a conversation it DROWNS OUT the specific last reply and the LLM drifts to generic
            # war/patrol/supply topics — so an in-progress chat is grounded ONLY in the conversation above.
            if not in_progress:
                try:
                    brief = self.memory.build_situation_briefing(npc_key)
                    if brief:
                        ctx_parts.append(str(brief))
                except Exception:
                    pass
                try:
                    sub = self.memory.graph_retrieve(
                        save_id, fac, recent_message or "tensions allies enemies current situation", k=5)
                    if sub:
                        ctx_parts.append("Faction standing:\n" + "\n".join("- " + str(d.get("text") or "") for d in sub))
                except Exception:
                    pass
        role = ""
        try:
            if self.memory is not None:
                role = str((self.memory.get_npc(npc_key) or {}).get("role") or "")
        except Exception:
            role = ""
        who = (npc_name or "this character") + ((" (" + role + ")") if role else "") + ", a " + fac + " character"
        # A low-ranking crew member can't broker faction deals — keep suggestions in their lane.
        low_rank = role.lower() in ("", "crew", "service crew", "service", "engineer", "marine", "pilot")
        instruction = (
            "Suggest exactly " + str(count) + " things the PLAYER could say to " + who +
            " in the universe of X4: Foundations." +
            (" The conversation is IN PROGRESS — each suggestion MUST directly engage a SPECIFIC detail from their "
             "most recent reply: name the exact thing they said (a faction, place, event, claim or number) and "
             "respond to THAT. A generic follow-up ('how's it going?', 'tell me more', 'how's the crew holding "
             "up?') is WRONG — it must show you heard their actual words. Do NOT restart or change the subject."
             if in_progress else
             " This opens the conversation — propose specific, natural opening lines.") +
            (" This person is low-ranking crew — they talk about their own work, their ship, and the local "
             "situation. They CANNOT broker truces, sanctions, trade pacts or faction policy, so do NOT propose "
             "those; keep it to what THEY would actually know and do."
             if low_rank else
             " This person has authority — diplomacy, requests and faction matters are in scope.") +
            " Each item has: `label` = a SHORT, CONCRETE preview of the actual line so the player knows what they "
            "are about to say (e.g. 'Ask about the raids' or 'Thank her for the resupply' — NOT a vague category "
            "like 'Demand Information' or 'Offer Exchange'); and `line` = the full single sentence the player "
            "speaks. Make the three DISTINCT. Stay strictly inside the X4 universe. Respond with ONLY a JSON "
            'array, no prose: [{"label":"...","line":"..."}]'
        )
        messages = [{"role": "system", "content": instruction}]
        if ctx_parts:
            messages.append({"role": "system", "content": "Context:\n" + "\n\n".join(ctx_parts)})
        if in_progress and last_npc:
            messages.append({"role": "user", "content":
                "They just said: \"" + last_npc + "\"\nGive me 3 options that EACH react to a specific detail in "
                "that exact line — name the thing they mentioned, don't ask a generic question. JSON array only."})
        else:
            messages.append({"role": "user", "content": "Give me the menu options now."})
        raw = ""
        with self._chat_lock:
            try:
                data = self._json("POST", "/v1/chat/completions",
                                  {"messages": messages, "stream": False,
                                   "temperature": 0.6, "max_tokens": 400},
                                  timeout=self.timeout_seconds)
                raw = self._extract_reply(data)
            except Exception:
                raw = ""
        return self._parse_suggestions(raw, count)

    def summarize_conversation(self, npc_name: str, recent_turns: list, prior_summary: str = "") -> str:
        """Rolling TOPIC summary of a conversation (thematic, NOT verbatim) for long-range continuity —
        so the NPC recalls what you've discussed over many talks, beyond the last few raw turns."""
        if not recent_turns:
            return prior_summary
        convo = "\n".join(
            (("Player" if str(t.get("role")) in ("user",) else (npc_name or "You")) + ": "
             + str(t.get("content") or t.get("text") or "")) for t in recent_turns)
        instruction = (
            "Summarize, in ONE or TWO short phrases, the TOPICS this person and "
            + (npc_name or "the character") + " have discussed across their conversations — thematic, "
            "NOT verbatim (e.g. 'the war with Argon; a possible trade alliance; the Xenon threat'). Fold in "
            "anything new from the recent exchange. Reply with ONLY the summary phrases, no preamble."
        )
        messages = [{"role": "system", "content": instruction}]
        if prior_summary:
            messages.append({"role": "system", "content": "Running summary so far: " + str(prior_summary)})
        messages.append({"role": "user", "content": "Recent conversation:\n" + convo[:2000]})
        raw = ""
        with self._chat_lock:
            try:
                data = self._json("POST", "/v1/chat/completions",
                                  {"messages": messages, "stream": False, "temperature": 0.3, "max_tokens": 120},
                                  timeout=self.timeout_seconds)
                raw = self._extract_reply(data)
            except Exception:
                raw = ""
        return self._x4_safe(self._strip_speaker(raw)) if raw else prior_summary

    def _make_entity_classifier(self):
        """A cheap LLM completion fn (low tokens, temp 0) for RoleRAG's entity/scope classification step.
        Serialized on the chat lock like other Player2 calls; returns '' on any failure so RoleRAG degrades
        to deterministic in-scope matching."""
        def _classify(messages: list) -> str:
            with self._chat_lock:
                try:
                    data = self._json("POST", "/v1/chat/completions",
                                      {"messages": messages, "stream": False, "temperature": 0.0, "max_tokens": 240},
                                      timeout=self.timeout_seconds)
                    return self._extract_reply(data)
                except Exception:
                    return ""
        return _classify

    @staticmethod
    def _qualify_prose(text: str) -> str:
        """For player-facing AUTHORING calls, convert the grounding's raw sim-numbers to qualitative LEVELS so the
        LLM has the situation but no figures to copy — it then phrases wars/standings in its own words. Conservative:
        only touches numbers (war intensity %, the trust/fear/bias/dependency tallies in parentheses), never prose."""
        if not text:
            return text

        def _ilvl(m: "re.Match") -> str:
            v = int(m.group(1))
            word = "all-out" if v >= 80 else "fierce" if v >= 50 else "moderate" if v >= 25 else "low-level"
            return "intensity: " + word

        t = text
        t = re.sub(r"intensity\s+(\d+)\s*%", _ilvl, t, flags=re.I)
        # drop number-bearing parentheticals/tallies: "(trust 10, fear 0, ...)", "(aggression 70/100, ...)"
        t = re.sub(r"\s*\([^()]*\d[^()]*\)", "", t)
        t = re.sub(r"\b\d+\s*/\s*100\b", "", t)
        t = re.sub(r"\b\d+\s*%", "", t)
        t = re.sub(r"\s+([,.])", r"\1", t)
        t = re.sub(r"\s{2,}", " ", t)
        return t.strip()

    def npc_complete(self, request: NeuralRequest) -> NeuralResponse:
        start = time.time()
        _blocked = self._llm_gate()
        if _blocked:
            return NeuralResponse.safe_error(request, _blocked, latency_ms=0)
        game_id = str(request.target.get("game_id") or request.metadata.get("game_id") or "x4_neural_link")
        save_id = str(request.target.get("save_id") or request.metadata.get("save_id") or "")
        system_prompt = self._system_prompt_from(request)
        persona = {
            "name": request.target.get("npc_name") or request.metadata.get("npc_name") or request.source_mod,
            "short_name": request.target.get("npc_short_name") or request.metadata.get("npc_short_name"),
            "character_description": request.target.get("character_description") or request.metadata.get("character_description"),
            "system_prompt": system_prompt,
            "voice_id": request.target.get("voice_id") or request.metadata.get("voice_id"),
        }
        sender_name = str(request.target.get("sender_name") or request.metadata.get("sender_name") or "Player")
        sender_message = self._last_user_message(request)
        game_state_info = request.target.get("game_state_info") or request.metadata.get("game_state_info") or ""

        # Durable, save-scoped memory key. When a memory store is attached, prior
        # memory is injected into this turn's context and the exchange is recorded.
        npc_key = None
        if self.memory is not None:
            npc_key = self.memory.make_key(save_id, game_id, persona.get("name") or system_prompt[:64])
            # Grounded injection: personal memory + faction/relationship/war/sector/
            # world-event context, so replies reference the real, specific universe.
            mem_ctx = self.memory.build_situation_briefing(npc_key)
            if mem_ctx:
                game_state_info = (game_state_info + "\n\n" + mem_ctx).strip() if game_state_info else mem_ctx
            # SPEC 1e — ground EVERY faction-tagged call (news, autonomous decisions, crisis messages), not
            # just face-to-face chat. When the persona isn't this faction's bound NPC, the briefing above has
            # no faction context — add it explicitly so the faction's mood/wars/grudges/recent events back the
            # call. (GraphRAG below also fires off the same faction_id.)
            fac_for_ground = str(request.target.get("faction_id") or request.metadata.get("faction_id") or "")
            if fac_for_ground:
                _npc = self.memory.get_npc(npc_key)
                if not (_npc and _npc.get("faction_id") == fac_for_ground):
                    fb = self.memory.build_faction_briefing(save_id, fac_for_ground)
                    if fb:
                        game_state_info = (game_state_info + "\n\n" + fb).strip() if game_state_info else fb
            # Vector-RAG v0 (RoleRAG-style retrieval): surface the durable memories most relevant to
            # THIS message prominently, so the model attends to them. Grows in value as memory
            # accumulates; lexical now, embedding-based later (same call site).
            try:
                relevant = self.memory.retrieve_relevant(npc_key, sender_message, k=4)
                if relevant:
                    rel_txt = "\n".join("- " + str(d.get("text") or "") for d in relevant)
                    game_state_info = ((game_state_info + "\n\n") if game_state_info else "") + \
                        "Most relevant to what was just said:\n" + rel_txt
            except Exception:
                pass
            # SPEC 1k — RoleRAG boundary-aware retrieval (paper §3.4). Supersedes the faction-only graph_retrieve:
            # classify the entities the player named (specific X4 entity / general concept / OUT-OF-SCOPE), retrieve
            # the right subgraph per entity, AND inject an explicit cognitive-boundary rejection for any out-of-scope
            # entity so the NPC refuses instead of hallucinating. The general route still covers the NPC's own
            # faction 1-hop, so this is a strict superset of the prior behavior.
            try:
                fac = str(request.target.get("faction_id") or request.metadata.get("faction_id") or "")
                # FACT HIERARCHY (Codex 2026-06-26): the NPC's OWN posting outranks the refusal guard. Gather its
                # local assignment facts (ship, sector) so RoleRAG treats them as KNOWN even though they're absent
                # from the galaxy-lore corpus — fixes "tell me about the Vigilant" → "never heard of it".
                _lf_st = request.target.get("stats") if isinstance(request.target.get("stats"), dict) else {}
                _lf_ship = request.target.get("ship_name") or request.metadata.get("ship_name") or _lf_st.get("ship_name")
                _lf_sector = request.target.get("sector") or request.metadata.get("sector") or _lf_st.get("sector")
                local_facts: list[dict] = []
                if _lf_ship:
                    local_facts.append({"name": str(_lf_ship), "kind": "ship",
                                        "note": "It is the ship you serve aboard; you know her decks, routes and squad routines."})
                if _lf_sector:
                    local_facts.append({"name": str(_lf_sector), "kind": "sector",
                                        "note": "It is the sector where you are currently posted."})
                if self._rolerag is not None and sender_message:
                    # Run the LLM scope-classifier only for genuine PLAYER turns (not internal authoring calls
                    # like news/comms) AND only when the message has an unknown proper noun the cheap
                    # deterministic pass didn't resolve — keeps the common "ask about known factions" case LLM-free.
                    classify = None
                    if str(getattr(request, "source_mod", "") or "") not in ("galaxy_news", "player_comms"):
                        idx = self._rolerag.index_for(save_id)
                        if idx.unknown_proper_nouns(sender_message):
                            classify = self._make_entity_classifier()
                    rr = self._rolerag.analyze_and_retrieve(save_id, fac, sender_message, classify_llm=classify, local_facts=local_facts)
                    if rr.get("context"):
                        ctx_txt = "\n".join("- " + c for c in rr["context"])
                        game_state_info = ((game_state_info + "\n\n") if game_state_info else "") + \
                            "Relevant galaxy standing (entities you know, most relevant):\n" + ctx_txt
                    if rr.get("boundary"):
                        b_txt = "\n".join("- " + b for b in rr["boundary"])
                        game_state_info = ((game_state_info + "\n\n") if game_state_info else "") + \
                            "COGNITIVE BOUNDARY — these lie OUTSIDE your knowledge of the X4 universe:\n" + b_txt
            except Exception:
                pass

        # X4 NPC stats (crew skills, role, race, ship assignment) attached to this
        # NPC. Accept a whole `stats` dict, or individual stat fields on target.
        npc_stats: dict[str, Any] = dict(request.target.get("stats")) if isinstance(request.target.get("stats"), dict) else {}
        for field in ("race", "role", "gender", "ship_class", "ship_name", "sector", "skills", "macro"):
            value = request.target.get(field)
            if value is not None and field not in npc_stats:
                npc_stats[field] = value
        # Grounded crew skill (md/Boarding.xml combinedskill, 0-100): used ONLY to color the persona
        # (a veteran talks like one). NOT stored as a displayed stat — the raw number isn't useful on
        # the dashboard, per Ken.
        _stat_line = ""
        _sk = None
        try:
            _sk = int(request.target.get("npc_skill"))
        except (TypeError, ValueError):
            _sk = None
        _role = str(npc_stats.get("role") or "")
        if _role or _sk is not None:
            _lvl = ""
            if _sk is not None:
                _lvl = "a seasoned veteran" if _sk >= 75 else ("experienced" if _sk >= 50 else ("competent" if _sk >= 25 else "still green"))
            # Map the coarse engine entityrole to a concrete posting so the LLM stops inventing titles
            # ("logistics officer"). marine = boarding soldier; service = maintenance/ops; else crew.
            _role_desc = {
                "marine": "a marine — a boarding soldier and shipboard security, not an administrator or trader",
                "service crew": "service crew — you handle maintenance, repairs and the day-to-day running of the ship",
                "service": "service crew — you handle maintenance, repairs and the day-to-day running of the ship",
            }.get(_role, ("a " + _role) if _role else "a member of the crew")
            _stat_line = ("Your actual posting in this galaxy is " + _role_desc
                          + ((", and you are " + _lvl + " at it") if _lvl else "")
                          + ". Speak from this posting; do not invent a different job or title for yourself.")

        # Build chat-completions messages — PROVEN to hold character where the NPC-API spawn
        # system_prompt LEAKS (validated headlessly: Zelda/Hulk/Darth Vader get "never heard of it"
        # while real X4 questions answer normally). Player2 community guidance: inject context on
        # EVERY call, keep the system rule SHORT, and layer dynamic context separately.
        short_rule = (
            "You are a character living within the universe of X4: Foundations and know ONLY the X4 "
            "galaxy. Anything that is not part of this galaxy you have simply never heard of — do not "
            "explain or describe it, just say plainly that you have never heard of such a thing. "
            "You are a real person of this galaxy, not an assistant: you always answer in-world as this "
            "character would. When the player raises politics, war, alliances, or your faction's enemies, "
            "you respond with your faction's perspective and your own opinion — interested, wary, "
            "defiant, or approving — even about things you would not personally carry out. "
            "Stay in character. Reply in one or two short sentences."
        )
        faction_id = str(request.target.get("faction_id") or request.metadata.get("faction_id") or "argon")
        # SPEC 2a — PersonaCard: for a genuine player↔NPC chat turn, LEAD the context with a situated ROLE CARD
        # (identity + archetype + AUTHORITY + live concerns) so the NPC roleplays within what its posting could
        # plausibly do. Internal authoring/reaction calls (news/comms/faction-reaction) are NOT player NPC chat,
        # so they keep the lighter persona line and never get an authority contract.
        persona_card_text = ""
        if self._persona is not None:
            _internal = (str(getattr(request, "source_mod", "") or "") in ("galaxy_news", "player_comms")
                         or str(request.target.get("game_id") or "") in ("reaction", "news", "comms"))
            if not _internal:
                try:
                    card = self._persona.build(save_id, {
                        "npc_name": persona.get("name") or request.target.get("npc_name"),
                        "npc_short_name": request.target.get("npc_short_name"),
                        "faction_id": faction_id,
                        "role": npc_stats.get("role"),
                        "npc_skill": request.target.get("npc_skill"),
                        "ship_name": npc_stats.get("ship_name"),
                        "sector": npc_stats.get("sector"),
                    })
                    persona_card_text = self._persona.card_to_prompt(card)
                except Exception:
                    persona_card_text = ""
        # Dynamic context, layered separately (persona card / line + the grounded situation briefing).
        if persona_card_text:
            ctx_parts = [persona_card_text]   # the card subsumes the basic persona + posting line
        else:
            ctx_parts = ["You are " + str(persona.get("name") or "an officer") + ", of the " + faction_id + " faction."]
            if _stat_line:
                ctx_parts.append(_stat_line)
        # Ken (2026-06-26): player-facing AUTHORING calls (news bulletins, player comms) get the grounding
        # CONVERTED to qualitative levels first — the LLM never sees raw sim-numbers ('intensity 100%', 'trust 10',
        # 'aggression 70/100') to copy, so it describes wars and standings vividly in ITS OWN words. The router's
        # output humanizer stays as a last-resort net. Chat/decision calls keep the precise numbers.
        _authoring = (str(getattr(request, "source_mod", "") or "") in ("galaxy_news", "player_comms")
                      or str(request.target.get("game_id") or "") in ("reaction", "news", "comms"))
        if game_state_info and _authoring:
            game_state_info = self._qualify_prose(str(game_state_info))
        if game_state_info:
            ctx_parts.append(str(game_state_info))
        cc_messages: list[dict[str, str]] = [
            {"role": "system", "content": short_rule},
            {"role": "system", "content": "Context for this conversation:\n" + "\n\n".join(ctx_parts)},
        ]
        # Recent conversation history for continuity.
        if self.memory and npc_key:
            try:
                for turn in self.memory.get_recent_turns(npc_key, limit=8):
                    trole = "assistant" if str(turn.get("role")) in ("assistant", "npc") else "user"
                    ttext = str(turn.get("text") or "")
                    if ttext:
                        cc_messages.append({"role": trole, "content": ttext})
            except Exception:
                pass
        cc_messages.append({"role": "user", "content": sender_message})

        # Keep this NPC visible in the registry (the chat-completions path needs no spawned npc_id).
        if self.memory and npc_key:
            try:
                self.memory.index_npc(
                    npc_key, save_id=save_id, game_id=game_id,
                    name=str(persona.get("name") or ""), faction_id=faction_id, stats=npc_stats or None,
                )
            except Exception:
                pass

        # Direct chat-completions call, with a short retry on empty (Player2's GLM is reasoning-bound).
        requested_max = int(request.target.get("max_tokens", 512) or 512)
        temperature = request.target.get("temperature", 0.4)
        message = ""
        last_err: str | None = None
        with self._chat_lock:
            for budget in (max(requested_max, 512), 1024):
                try:
                    data = self._json("POST", "/v1/chat/completions",
                                      {"messages": cc_messages, "stream": False,
                                       "temperature": temperature, "max_tokens": budget},
                                      timeout=self.timeout_seconds)
                    raw_reply = self._extract_reply(data)
                    if raw_reply:
                        message = self._x4_safe(self._strip_speaker(raw_reply))
                        break
                except Exception as exc:
                    last_err = str(exc)
        latency = int((time.time() - start) * 1000)
        if not message:
            return NeuralResponse.safe_error(request, last_err or "Player2 returned no message", latency_ms=latency)

        # Record the exchange and condense if the raw window overflows. Memory is advisory —
        # never fail a reply because bookkeeping hiccuped.
        if self.memory and npc_key:
            try:
                self.memory.record_turn(npc_key, "user", sender_message)
                self.memory.record_turn(npc_key, "assistant", message)
                self.memory.condense_if_needed(npc_key)
                # I1/I2 (2026-06-28): re-identify this conversation NPC against the persistent identity
                # layer from the evidence we have (name+faction+role+skills, + macro/sector when the UI
                # captured them). macro is the corroborator that lifts a re-encountered NPC tentative→bound,
                # so memory survives a reload (#99). Advisory — never break a reply over identity bookkeeping.
                # I8 GATE: only REAL player conversations (game_id=='chat') drive the identity rebind.
                # npc_complete is also called for faction reactions / news / influence steps — those are NOT
                # persons and were polluting the identities table (Galaxy News Desk ×5, High Command dups).
                if str(game_id) == "chat":
                    try:
                        _row = self.memory.get_npc(npc_key) or {}
                        # The UI chat sends evidence in prompt_vars → router promotes pv.macro/sector/runtime to
                        # request.TARGET (see router build); read target first, then metadata + the stored row.
                        _meta = request.metadata if isinstance(request.metadata, dict) else {}
                        self.memory.rebind_session(save_id or "live", [{
                            "npc_key": npc_key,
                            "runtime_component_id": str(request.target.get("runtime_component_id") or _meta.get("runtime_component_id") or request.target.get("component") or ""),
                            "name": _row.get("name") or persona.get("name") or "",
                            "faction": _row.get("faction_id") or fac,
                            "role": _row.get("role") or npc_stats.get("role") or "",
                            "macro": request.target.get("macro") or _meta.get("macro") or npc_stats.get("macro") or _row.get("macro") or "",
                            "sector": request.target.get("sector") or npc_stats.get("sector") or _meta.get("sector") or _row.get("sector") or "",
                            "skills": _row.get("skills") or npc_stats.get("skills"),
                            "recently_talked": True,
                        }], save_id=save_id)
                        # Promote AFTER the rebind links the identity (record_turn's hook ran before the link
                        # existed on a brand-new NPC). Guarantees a talked-to NPC reaches Tier 1 immediately.
                        self.memory.promote_identity_for_npc(npc_key, "talked")
                        # I7: if this bind only reached TENTATIVE and the player's message asserts shared
                        # history that MATCHES stored memory, soft-confirm it to BOUND. Anti-abuse: a no-op
                        # unless the claim overlaps a real fact/turn (never promotes on an unsupported claim).
                        self.memory.soft_confirm_identity(npc_key, sender_message)
                    except Exception:
                        pass
                # Rolling TOPIC summary for long-range continuity. Every 4 turns (to bound LLM calls):
                # re-summarize recent turns into a thematic gist stored on the NPC (auto-injected by
                # build_memory_context as "What you remember overall").
                tc = self.memory.turn_count(npc_key)
                if tc >= 4 and tc % 4 == 0:
                    recent = self.memory.get_recent_turns(npc_key, limit=12)
                    prior = (self.memory.get_npc(npc_key) or {}).get("summary") or ""
                    gist = self.summarize_conversation(persona.get("name"), recent, prior)
                    if gist:
                        self.memory.set_summary(npc_key, gist)
            except Exception:
                pass

        return NeuralResponse(
            request_id=request.request_id,
            status="ok",
            source_mod=request.source_mod,
            channel=request.channel,
            reply=message,
            actions=[],
            latency_ms=latency,
        )

    @staticmethod
    def _extract_reply(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                message = first.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if content:
                        return str(content)
                text = first.get("text")
                if text:
                    return str(text)
        if data.get("reply"):
            return str(data["reply"])
        return ""
