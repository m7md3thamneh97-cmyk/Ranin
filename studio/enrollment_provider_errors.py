"""Allowlisted ElevenLabs failure diagnostics; never retain response messages.

HTTP rejection is separate from a network error or an ambiguous create outcome.
Only a small set of explicit client rejection statuses permit an owner-requested
clone retry. No function in this module performs a provider request.
"""
from __future__ import annotations

import json
import re

KNOWN_REJECTIONS = frozenset({400, 401, 402, 403, 404, 413, 415, 422, 429})
MAX_ERROR_BODY = 64 * 1024

_CODE_REASONS = {
    "invalid_api_key": "auth",
    "missing_api_key": "auth",
    "unauthorized": "auth",
    "authentication_error": "auth",
    "authorization_error": "permission",
    "missing_permissions": "permission",
    "insufficient_permissions": "permission",
    "permission_denied": "permission",
    "subscription_required": "plan",
    "paid_plan_required": "plan",
    "upgrade_required": "plan",
    "voice_cloning_not_available": "plan",
    "feature_not_available": "plan",
    "quota_exceeded": "quota",
    "insufficient_credits": "quota",
    "credit_balance_too_low": "quota",
    "payment_required": "quota",
    "voice_limit_reached": "voice_limit",
    "voice_limit_exceeded": "voice_limit",
    "too_many_voices": "voice_limit",
    "max_voice_count_reached": "voice_limit",
    "validation_error": "request_validation",
    "invalid_request": "request_validation",
    "invalid_parameters": "request_validation",
    "missing_required_field": "request_validation",
    "invalid_voice_sample": "request_validation",
    "invalid_audio": "request_validation",
    "rate_limit_exceeded": "rate_limit",
    "concurrent_limit_exceeded": "rate_limit",
    "too_many_requests": "rate_limit",
    "rate_limit_error": "rate_limit",
}
_STATUS_REASONS = {400: "request_validation", 401: "auth", 402: "quota",
                   403: "permission", 404: "request_validation", 413: "request_validation",
                   415: "request_validation", 422: "request_validation", 429: "rate_limit"}
_MESSAGES = {
    "auth": "ElevenLabs rejected the API key. Check the voice provider key in hosting settings.",
    "permission": "The ElevenLabs key does not have permission for this voice operation.",
    "plan": "The ElevenLabs account needs access to this voice operation on its plan.",
    "quota": "The ElevenLabs account has insufficient credits or its quota is used.",
    "voice_limit": "The ElevenLabs account has reached its available voice limit.",
    "request_validation": "ElevenLabs rejected the voice request format or audio sample.",
    "rate_limit": "ElevenLabs temporarily limited this voice request. Wait before trying again.",
}


def sanitize_diagnostics(value: object) -> dict | None:
    """Rebuild known fields, including when reading older or corrupted DB detail."""
    if not isinstance(value, dict):
        return None
    status = value.get("http_status")
    if type(status) is not int or not 300 <= status <= 599:
        return None
    code = value.get("provider_code")
    code = code if isinstance(code, str) and code in _CODE_REASONS else None
    result = {"provider": "elevenlabs", "http_status": status,
              "reason": _CODE_REASONS.get(code, _STATUS_REASONS.get(status, "provider")),
              "rejected": status in KNOWN_REJECTIONS}
    if code:
        result["provider_code"] = code
    return result


def response_diagnostics(response) -> dict:
    """Read only a bounded structured code/status; discard every free-text field."""
    code = None
    if len(response.content) <= MAX_ERROR_BODY:
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, dict):
            for field in ("code", "status", "type"):
                candidate = detail.get(field)
                if isinstance(candidate, str) and candidate in _CODE_REASONS:
                    code = candidate
                    break
    return sanitize_diagnostics({"http_status": response.status_code, "provider_code": code}) or {
        "provider": "elevenlabs", "reason": "provider", "rejected": False,
    }


def provider_failure(error, kind: str) -> dict:
    uncertain = bool(getattr(error, "uncertain", False))
    diagnostics = sanitize_diagnostics(getattr(error, "diagnostics", None))
    code = "outcome_unknown" if uncertain else "voice_clone_failed" if kind == "voice_clone" else "voice_preview_failed"
    message = ("The voice provider outcome is uncertain. Reconcile it before retrying."
               if uncertain else _MESSAGES.get((diagnostics or {}).get("reason"))
               or ("The voice provider could not create the clone." if kind == "voice_clone"
                   else "The voice provider could not generate the fresh voice sample."))
    result = {"code": code, "message": message}
    if diagnostics:
        result["details"] = diagnostics
    return result


def stored_failure(operation: dict) -> dict:
    """Project a safe failure without surfacing historical arbitrary error text."""
    try:
        detail = json.loads(operation.get("detail") or "{}")
    except (ValueError, TypeError):
        detail = {}
    detail = detail if isinstance(detail, dict) else {}
    failure = detail.get("failure")
    diagnostics = sanitize_diagnostics(failure.get("details")) if isinstance(failure, dict) else None
    if not diagnostics:
        if isinstance(failure, dict) and failure.get('code') in {'decoder_unavailable', 'invalid_voice_preview'}:
            return {'code': failure['code'], 'message': 'The generated voice sample could not be decoded and checked for playback.'}
        # This exact historical adapter string establishes an HTTP response;
        # fuzzy matches and arbitrary exception text never establish rejection.
        old = detail.get("error")
        prefix = "clone" if operation["kind"] == "voice_clone" else "speech synthesis"
        matched = re.fullmatch(r"ElevenLabs " + prefix + r" returned HTTP ([345][0-9]{2})\.", old) if isinstance(old, str) else None
        if matched:
            diagnostics = sanitize_diagnostics({"http_status": int(matched[1])})
    class RecordedError:
        uncertain = operation["state"] in {"dispatching", "outcome_unknown"}
    error = RecordedError()
    error.diagnostics = diagnostics
    return provider_failure(error, operation["kind"])


def retryable_clone(operation: dict | None) -> bool:
    if not operation or operation.get("kind") != "voice_clone" or operation.get("state") != "failed" or operation.get("provider_id"):
        return False
    return bool(stored_failure(operation).get("details", {}).get("rejected"))
