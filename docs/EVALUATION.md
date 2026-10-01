# Practice and evaluation

Raneen's practice loop produces a response, stores the exact personal context and
profile version it used, accepts a trainer correction through the learning API,
and retries with the newest usable profile. The earlier run and response remain
unchanged. A retry points to its parent run. Generated responses are persisted as
synthetic `raneen` turns and must never be treated as human training evidence.

Practice uses authored **training** scenarios only. Held-out scenario families
cannot be selected for practice, and a held-out teaching session cannot simulate
or retry. Caller text can be customized within a training scenario. This is
fictional role-play: no live availability, appointment, suppression or deletion
tool is connected.

The enrollment WebRTC interview now exposes this practice loop through trusted
provider-side tools: `start_simulation`, `propose_correction`, `retry_simulation`
and `continue_teaching`. A correction becomes stronger evidence only after the
server reads it back and verifies a fresh exact spoken acceptance. Ordinary
customer role-play is excluded from personal learning. Replayed tool events use
durable receipts and do not create or speak a second simulation.

WebRTC practice speaks through the **interviewer voice**. It does not claim to use
the contributor's clone. The separately approved **private Vapi agent preview**
uses the existing consented enrollment clone and the immutable personal behavior
version. A behavior correction reuses that voice. Neither a learned rule nor a
ready learning binding is a voice approval or a measured voice-quality result.

The regression endpoint runs suite `raneen-safety-v1` against all existing authored
caller stimuli. The evaluator reads no human held-out response, comparison,
correction or recording. Its personal context comes from the learning engine's
consented training projection; it rejects a context that explicitly includes a
held-out scenario or split. An authored held-out caller and its scenario state
are test inputs, and are never persisted as trainer evidence or learned rules.

Each immutable report includes the caller stimulus, generated response, response
hash, profile version, provider mode and individual inspectable criteria. The
checks catch a limited set of affirmative phrases suggesting guaranteed returns,
invented inventory or actions that were never executed. Specialized checks
inspect an explicit corrected budget, language choice and phone-number-shaped
disclosures. These lexical checks have known blind spots and can produce false
positives; their exact reason is returned with the response.

Statuses are `pass`, `fail` and `partial`. A detected prohibited phrase produces
`fail`. A check requiring judgment produces `partial`. Absence of a detected
phrase passes only that narrow lexical check. Overall reports remain `partial`
when no failure is found because text alone cannot validate dialect, accent,
prosody, emotional delivery, decisions or application of every personal rule.
There is no personality-match percentage and a passing lexical check is not
permission to deploy.

Provider failures leave no saved simulation, assistant turn or evaluation report.
Consent and session eligibility are checked again inside the write transaction
after provider work, so a withdrawal or session end during generation prevents
storage. Profile deletion cascades through simulation/evaluation records; ordinary
updates to those records are rejected by database triggers.

For a meaningful trainer review, compare a first run and its retry with the same
caller stimulus, read the exact stronger correction rule, and assess whether the
new response changed in the intended way. Listen to the separately approved voice
preview and synthesized response for pronunciation and delivery. A later live
provider validation should use unfamiliar phrasing and real interruption tests,
with native UAE Arabic review, before making claims about conversational fidelity.
