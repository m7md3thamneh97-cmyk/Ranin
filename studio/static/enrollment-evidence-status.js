// Saved microphone audio can support a voice preview before the contributor
// has confirmed a response pattern. The server remains the evidence authority.
export function confirmedResponseCount(journey = {}, workflow = {}) {
  const count = workflow?.learning?.confirmed_evidence_count ?? journey?.confirmed_patterns;
  return Number.isInteger(count) && count >= 0 ? count : 0;
}

export function needsResponseTeaching(journey = {}, workflow = {}) {
  return Boolean(
    !journey?.revoked && workflow?.voice_approved && !workflow?.behavior_ready &&
    confirmedResponseCount(journey, workflow) === 0,
  );
}

export function hasPendingSpokenReview(journey = {}, workflow = {}) {
  return Boolean(!journey?.revoked && (
    workflow?.pending_review || Number(journey?.pending_patterns) > 0
  ));
}
