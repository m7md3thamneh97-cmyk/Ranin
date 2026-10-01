// Contract verified against RAN-BE-01 studio/backend.py and LearningEngine.learning_state.
// Enrollment IDs and teaching-session IDs are different. Only the server may bind them.
export function learningBinding(journey, workflow) {
  const binding = journey?.learning || workflow?.learning;
  if (binding?.contract !== "raneen-backend-v1") return null;
  const { profile_id, session_id } = binding;
  if (
    ![profile_id, session_id].every(
      (id) => typeof id === "string" && /^[a-zA-Z0-9_-]{1,64}$/.test(id),
    )
  )
    return null;
  return { profile_id, session_id };
}
export class LearningConnection {
  constructor(request) {
    this.request = request;
    this.reset();
  }
  reset() {
    this.epoch = (this.epoch || 0) + 1;
    this.binding = null;
    this.cursor = 0;
    this.inflight = null;
  }
  bind(binding) {
    const key = JSON.stringify(binding);
    if (key === JSON.stringify(this.binding)) return;
    this.reset();
    this.binding = binding;
  }
  async refresh() {
    if (!this.binding) return null;
    if (this.inflight) return this.inflight;
    const epoch = this.epoch,
      { profile_id, session_id } = this.binding;
    const current = () => epoch === this.epoch;
    const request = this.request;
    const run = async () => {
      const [state, events] = await Promise.all([
        request(
          `/api/profiles/${encodeURIComponent(profile_id)}/learning-state`,
        ),
        request(
          `/api/sessions/${encodeURIComponent(session_id)}/events?after=${this.cursor}&limit=100`,
        ),
      ]);
      if (!current()) return null;
      if (
        state.profile_id !== profile_id ||
        !Array.isArray(state.hypotheses) ||
        !Array.isArray(events.items) ||
        !Number.isInteger(events.cursor) ||
        events.cursor < this.cursor
      )
        throw Error("invalid_learning_response");
      this.cursor = events.cursor;
      return { state, events: events.items };
    };
    const pending = run().finally(() => {
      if (current()) this.inflight = null;
    });
    this.inflight = pending;
    return pending;
  }
}
