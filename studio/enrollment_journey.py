"""Read-only, contributor-safe enrollment summaries.

Saved durations are reported by the recording client. They are not decoded audio,
clean speech measurements, or evidence that a voice is ready for use.
"""
from __future__ import annotations


def recovery_summary(row: dict) -> dict:
    return {
        "id": row["id"],
        "state": row["state"],
        "revoked": bool(row["revoked_at"]),
        "created": row["created"],
        "updated": row["updated"],
        "captured_ms": row["saved_audio_ms"],
        "saved_audio_ms": row["saved_audio_ms"],
        "chunk_count": row["chunk_count"],
        "next_seq": row["next_seq"],
        "voice_state": row["voice_state"],
        "confirmed_patterns": row["confirmed_patterns"],
        "pending_patterns": row["pending_patterns"],
    }


def journey_summary(row: dict, *, enabled: bool, provider_pending: bool, cleanup_state: str, interview_call_state: str) -> dict:
    revoked = bool(row["revoked_at"])
    if revoked:
        stage, action = "revoked", "review_cleanup"
        if provider_pending or row["voice_state"] == "outcome_unknown" or interview_call_state == "open":
            cleanup_state = "manual_reconciliation"
    elif provider_pending or row["voice_state"] == "outcome_unknown":
        # Dispatching operations have no durable completion/lease in the current
        # implementation. Never suggest that refreshing or restarting retries them.
        stage, action = "provider_outcome_unknown", "contact_support"
    elif row["voice_state"] == "verification_required":
        stage, action = "verification_required", "complete_verification"
    elif row["voice_state"] not in {"not_started", "failed"} or row["active_behavior_id"]:
        stage, action = "voice_setup", "review_voice_setup"
    else:
        stage, action = "interview", "continue_interview"
    if stage == "interview" and interview_call_state == "open":
        action = "stop_interview"
    return {
        **recovery_summary(row),
        "enabled": enabled,
        "stage": stage,
        "suggested_action": action,
        "provider_pending": provider_pending or row["voice_state"] == "outcome_unknown",
        "interview_call_state": interview_call_state,
        "can_resume": enabled and not revoked and not provider_pending
        and row["voice_state"] not in {"verification_required", "outcome_unknown"}
        and interview_call_state not in {"open", "close_unknown"}
        and row["state"] not in {"complete", "failed"},
        "cleanup_state": cleanup_state,
        # Provider voice IDs and the legacy ready flag do not establish accepted,
        # playable speech or a bounded browser call. Keep this explicit in the UI.
        "preview_allowed": False,
        "preview_unavailable_reason": "Voice testing is not available yet. Your saved session is kept for the next step.",
    }
