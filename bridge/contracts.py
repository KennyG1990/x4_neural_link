from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


VALID_CHANNELS = {"chat", "event", "health", "tool", "legacy", "npc"}
MAX_MESSAGES = 16
MAX_CONTENT_CHARS = 12000


class ContractError(ValueError):
    """Raised when a Neural Link request is invalid."""


def now_ms() -> int:
    return int(time.time() * 1000)


def _safe_identifier(value: str, field_name: str) -> str:
    value = str(value or "").strip()
    if not value:
        raise ContractError(f"{field_name} is required")
    if len(value) > 96:
        raise ContractError(f"{field_name} is too long")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
    if any(ch not in allowed for ch in value):
        raise ContractError(f"{field_name} contains unsafe characters")
    if ".." in value or value.startswith("."):
        raise ContractError(f"{field_name} contains unsafe path-like syntax")
    return value


def _normalize_messages(raw: Any, payload: dict[str, Any]) -> list[dict[str, str]]:
    if raw is None:
        user_text = payload.get("user_text") or payload.get("prompt") or ""
        prompt_vars = payload.get("prompt_vars") if isinstance(payload.get("prompt_vars"), dict) else {}
        if not user_text and prompt_vars:
            user_text = prompt_vars.get("user_text") or prompt_vars.get("message") or ""
        raw = [{"role": "user", "content": str(user_text or "Status check.")}]

    if not isinstance(raw, list) or not raw:
        raise ContractError("messages must be a non-empty list")
    if len(raw) > MAX_MESSAGES:
        raise ContractError(f"messages exceeds max length {MAX_MESSAGES}")

    messages: list[dict[str, str]] = []
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ContractError("each message must be an object")
        role = str(item.get("role") or "user").strip()
        if role not in {"system", "user", "assistant", "tool"}:
            raise ContractError(f"unsupported message role: {role}")
        content = str(item.get("content") or "")
        total += len(content)
        if total > MAX_CONTENT_CHARS:
            raise ContractError(f"message content exceeds {MAX_CONTENT_CHARS} characters")
        messages.append({"role": role, "content": content})
    return messages


@dataclass(frozen=True)
class NeuralRequest:
    request_id: str
    source_mod: str
    channel: str
    messages: list[dict[str, str]]
    target: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_ms: int = field(default_factory=now_ms)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "NeuralRequest":
        if not isinstance(payload, dict):
            raise ContractError("request body must be a JSON object")

        request_id = payload.get("request_id") or str(uuid.uuid4())
        source_mod = payload.get("source_mod") or payload.get("source") or "unknown_mod"
        channel = str(payload.get("channel") or payload.get("trigger_type") or "legacy").strip().lower()
        if channel not in VALID_CHANNELS:
            raise ContractError(f"unsupported channel: {channel}")

        target = payload.get("target") if isinstance(payload.get("target"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if isinstance(payload.get("prompt_vars"), dict):
            metadata = {**payload["prompt_vars"], **metadata}

        return cls(
            request_id=_safe_identifier(str(request_id), "request_id"),
            source_mod=_safe_identifier(str(source_mod), "source_mod"),
            channel=channel,
            target=target,
            metadata=metadata,
            messages=_normalize_messages(payload.get("messages"), payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "source_mod": self.source_mod,
            "channel": self.channel,
            "target": self.target,
            "metadata": self.metadata,
            "messages": self.messages,
            "created_ms": self.created_ms,
        }


@dataclass(frozen=True)
class NeuralResponse:
    request_id: str
    status: str
    source_mod: str
    channel: str
    reply: str
    actions: list[dict[str, Any]] = field(default_factory=list)
    provider: str = "player2"
    error: str | None = None
    latency_ms: int = 0
    created_ms: int = field(default_factory=now_ms)

    @classmethod
    def safe_error(cls, request: NeuralRequest, error: str, latency_ms: int = 0) -> "NeuralResponse":
        return cls(
            request_id=request.request_id,
            status="degraded",
            source_mod=request.source_mod,
            channel=request.channel,
            reply="Neural Link could not reach a usable AI response. No game action was taken.",
            actions=[],
            error=error,
            latency_ms=latency_ms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "source_mod": self.source_mod,
            "channel": self.channel,
            "reply": self.reply,
            "actions": self.actions,
            "provider": self.provider,
            "error": self.error,
            "latency_ms": self.latency_ms,
            "created_ms": self.created_ms,
        }
