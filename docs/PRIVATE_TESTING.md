# Private Raneen testing

The workspace owner can invite a finite cohort of ten testers without sharing
the owner credential. Enable `RANEEN_TESTER_ACCESS_ENABLED=1` on the existing
service after reviewing this release. The default remains off. Existing owner
access, provider credentials, disk and hosting plan are preserved.

## Invite someone

1. Sign into `/enroll` with the existing private owner code.
2. Open **Workspace → Invite a tester → Create invitation**.
3. Copy the invitation and send it privately to that tester. Share the ordinary
   `/enroll` URL; credentials are never placed in links.
4. The tester enters the invitation on the sign-in page and saves the new
   personal access code shown once. That code lets them return to their own
   workspace after a refresh. Codes stay in page memory, not browser storage.

Each invitation is used once and expires seven days after issuance. Redeemed
access lasts seven days from joining. Only hashes are retained. Every tester has
a separate account, voice enrollment, consent, recording and learned profile.
Testers cannot list other accounts' records, create invitations, use owner tools,
export datasets or configure providers. Revoking an invitation stops its account
and marks its enrollment revoked; provider cleanup reports pending outcomes
honestly. The owner can retry that cleanup by revoking the same invitation again.

## Continue a saved voice

Voice approval and response learning are different checkpoints. If a voice is
already approved but no response is confirmed, **Resume teaching** returns to the
interview without opening the microphone or creating another clone. Start the
conversation, demonstrate one customer response in your own words, then listen
to Raneen's review. If the review is correct, say **“نعم احفظ هذا”** or
**“Yes, save this”** after it finishes. Otherwise correct it verbally. The visible
pending-review hint helps the contributor complete the spoken confirmation.

The app then reuses the approved voice to build the personalized private agent.
Saved audio, browser text and ordinary acknowledgments do not become confirmed
response evidence automatically.

## Test allowance and recovery

Each tester gets one enrollment with the existing 30-minute interview allowance,
one private voice with bounded creation attempts, three fresh voice samples,
up to six assistant versions and three agent calls of at most three minutes.
Calls cannot outlive the remaining access window. The ten-person cohort is a
lifetime limit for this rollout; revoking an account does not reset it.

Paid operations involving a tester admit only one active enrollment across the
service, including owner calls. Another tester can wait and retry after it ends.
Recording uploads also reserve a service storage allowance and keep disk headroom.
No phone numbers, customer calls or existing Sura changes are included.

Expired or paused accounts can still view their session status, stop calls and
revoke/clean up their own enrollment. Access expiry alone does not promise
automatic deletion of saved recordings or previously created artifacts. Explicit
revocation starts scoped cleanup; unknown provider outcomes remain tracked.

Automated tests use synthetic audio and mocked provider boundaries. Actual
microphone behavior, provider access and Arabic voice/style quality require
contributor testing. Configured credentials alone do not establish those results.
