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

// The interview refreshes journey more often than workflow. Its explicit null
// clears a previous review rather than falling back to an older workflow copy.
function pendingSpokenReview(journey, workflow) {
  return Object.hasOwn(journey ?? {}, "pending_review")
    ? journey.pending_review
    : workflow?.pending_review;
}

export function hasPendingSpokenReview(journey = {}, workflow = {}) {
  if (journey?.revoked) return false;
  const review = pendingSpokenReview(journey, workflow);
  if (Object.hasOwn(journey ?? {}, "pending_review")) return Boolean(review);
  return Boolean(review || Number(journey?.pending_patterns) > 0);
}

// Render only the server's safe status enum. Provider text and internal IDs are
// never used as UI copy, and readback completion is not contributor approval.
export function spokenReviewMessage(journey = {}, workflow = {}, t) {
  const review = pendingSpokenReview(journey, workflow);
  const keys = {
    awaiting_readback: "spokenReviewAwaitingHint",
    readback_incomplete: "spokenReviewIncompleteHint",
    readback_mismatch: "spokenReviewMismatchHint",
    review_ready: review?.can_confirm === true ? "spokenReviewReadyHint" : "spokenReviewAwaitingHint",
  };
  const key = Object.hasOwn(keys, review?.status) ? keys[review.status] : "spokenReviewHint";
  const phrase = ["نعم احفظ هذا", "Yes, save this"].includes(review?.confirmation_phrase)
    ? review.confirmation_phrase
    : t("spokenReviewConfirmationPhrase");
  return t(key).replace("{confirmation}", phrase);
}
