# Raneen provider boundaries

`studio/providers.py` owns outbound provider requests. The application owns user
ownership, consent, provenance, holdout isolation, version approval, and database
transactions. Never call a remote provider before the applicable consent gate.

The new learning-studio remote adapters are **off by default**. Set
`RANEEN_LEARNING_STUDIO_ENABLED=1` only for an authorized environment. Configured
keys alone do not enable inference, cloning, synthesis, reconciliation reads, or
realtime provisioning. Offline `local_rules` remains available. Deletion of known
resources remains available while disabled so consent withdrawal can be completed.
This flag does not change the existing voice-enrollment feature gate or its
separate provider implementation.

## Learning

| Variable | Default | Meaning |
| --- | --- | --- |
| `RANEEN_LEARNING_PROVIDER` | `local` | `local`/`local_rules` for the finite offline adapter; `openai` for remote inference |
| `OPENAI_API_KEY` | unset | Private server credential; required for `openai` |
| `RANEEN_TEXT_MODEL` | `gpt-4.1-mini` | Existing server text-model setting; takes precedence over `RANEEN_LEARNING_MODEL` |
| `RANEEN_LEARNING_MODEL` | inherited | Learning-model alias used when `RANEEN_TEXT_MODEL` is absent |
| `RANEEN_OPENAI_BASE_URL` | `https://api.openai.com/v1` | Optional HTTPS OpenAI-compatible deployment URL; server configuration only |

`LearningModelProvider.analyze(transcript, context)` returns bounded, validated
observations `{category, key, value, confidence}`. `respond(context, messages)`
returns one bounded reply. Provider output cannot confirm/lock a hypothesis or
approve a profile version; those decisions belong to the evidence engine.

The offline adapter is explicitly identified as `local_rules`. It supports a small
set of inspectable real-estate communication rules and the teaching planner's
supplied next question. It does not reproduce a person's psychology, dialect,
accent, or voice. Unsupported evidence yields no inference. Configure a model for
open-ended conversational learning; setting a key alone does not change modes.

The remote adapter uses strict structured output and validates again locally. It
rejects refusals, truncated completions, malformed JSON, invalid confidence and
oversized responses. Correction normalization receives the original response and
target rules so a replacement updates the relevant key. It never switches to the
offline adapter after a remote failure.

## Voice

| Variable | Default | Meaning |
| --- | --- | --- |
| `ELEVENLABS_API_KEY` | unset | Private server credential for clone creation, synthesis, and deletion |
| `ELEVENLABS_TTS_MODEL` | `eleven_multilingual_v2` | Existing speech-model setting; takes precedence over `RANEEN_TTS_MODEL` |
| `RANEEN_TTS_MODEL` | inherited | Speech-model alias used when `ELEVENLABS_TTS_MODEL` is absent |

`ElevenLabsVoiceProvider.clone(name, samples)` takes a list of
`(filename, audio_bytes, media_type)` tuples and returns a provider voice ID.
Eligible source speech, ownership and external processing permission must already
be checked by the voice service. The adapter limits combined uploads to 20 MB.
It does not infer sample eligibility from the presence of bytes.

`requires_verification=true` is a failure to produce a usable candidate, never a
ready voice. That exception retains `provider_voice_id` for internal cleanup and
custody; its public error contains only a code and message. The service must track
or delete the remote resource. Missing verification state also fails closed.
`verify_ready(voice_id)` rechecks the provider's current `voice_verification`
metadata before retrying a verification-blocked candidate; synthesis success
alone cannot clear that verification requirement. Unknown metadata fails closed.

`reconcile_created_voice(voice_id, expected_name)` is a read-only reconciliation
check after an uncertain creation. It requires the exact operation-tagged name,
matching voice ID, `category=cloned`, `is_owner=true`, and provider verification
readiness. A stock voice, shared voice, or another operation's clone is rejected.
The service must persist an operation before dispatch and require explicit
reconciliation instead of issuing another clone request after an ambiguous result.

`synthesize(voice_id, text)` requests MP3 and checks content type and MP3 framing.
`delete(voice_id)` treats an already-deleted remote voice as success. New candidates
must use new local versions; approval of one must never be silently replaced.

## Realtime transport

| Variable | Default | Meaning |
| --- | --- | --- |
| `VAPI_API_KEY` | unset | Existing private Vapi server credential; takes precedence over `VAPI_PRIVATE_KEY` |
| `VAPI_PRIVATE_KEY` | inherited | Private-key alias used when `VAPI_API_KEY` is absent |
| `VAPI_PUBLIC_API_KEY` | unset | Existing browser key; restrict it in Vapi to allowed origins/assistants; takes precedence over `VAPI_PUBLIC_KEY` |
| `VAPI_PUBLIC_KEY` | inherited | Public-key alias used when `VAPI_PUBLIC_API_KEY` is absent |
| `RANEEN_VAPI_PUBLIC_KEY_RESTRICTED` | unset | Set `true` after configuring public key restrictions; local attestation only |
| `RANEEN_VAPI_VOICE_ID` | unset | Initial voice available to the Vapi organization; an approved clone can replace it |
| `RANEEN_VAPI_MODEL` | `gpt-4.1-mini` | Initial model alias when `RANEEN_TEXT_MODEL` is absent; the orchestrator replaces this with the custom LLM configuration |
| `RANEEN_VAPI_VOICE_MODEL` | inherited | Existing Vapi voice-model setting; takes precedence over the speech-model settings |

`build_vapi_transport_config(context, voice_id)` produces backend-only assistant
settings. The orchestrator replaces its model with the authenticated Raneen custom
LLM endpoint before `VapiProvider.create_assistant(config)`. Only the resulting
assistant ID and restricted public key should be returned for Web SDK startup.
An inline assistant configuration containing a model API key would reveal that
secret to browser users and must never be returned.

`VapiProvider` uses a private server key to create/delete assistants. It returns
only the created resource ID, discarding any echoed provider configuration.
Persistent resources must be tracked for cleanup when a session ends or consent
is withdrawn. A provider timeout must not be automatically retried: creation may
already have completed remotely.

`get_assistant(assistant_id)` performs a read-only provider lookup and returns only
the matching ID, name, and actual `raneen_session_id`/`raneen_operation_id` metadata.
The service compares these with its durable operation before adopting a resource.
Provider credentials and model configuration are never returned by this method.

The custom LLM callback preserves Raneen as the brain: it compiles the latest
applicable profile context and returns OpenAI-compatible completion/SSE output.
The current callback generates the whole model reply before emitting its SSE
chunk. This is protocol-compatible, but first speech waits for full generation;
it is not incremental model token streaming and latency needs live measurement.

The current official SDK schema defines `model.url` as the OpenAI client's
**base URL**: omit `/chat/completions`, which Vapi appends. For bearer
authentication, provide an assistant dynamic credential
`credentials: [{provider: "custom-llm", apiKey: <server secret>}]`.
`model.headers.Authorization` is explicitly unsupported and `model.apiKey` is
not a schema field. The adapter rejects both before network access.
`assistant.server.headers.Authorization` is supported for webhook authentication.
Explicit `serverMessages` includes canonical conversation updates, status, speech
and interruption events. Browser `clientMessages` retains transcript events for
the live interface. The server uses committed cumulative spoken history to
persist turns rather than treating each transcription fragment as a new turn.

`ServerMessageTranscript` has no required event ID and its `timestamp` is optional.
A minimal transcript event therefore cannot establish exactly-once turn identity.
Canonical snapshot message records provide `time`/`endTime`; these are validated
and combined with call ID and role for deterministic receipts. Legacy final
transcript events can be accepted when an explicit stable ID/timestamp exists.
One per-call ingestion mode prevents snapshot and transcript time schemes from
creating duplicate evidence. A final transcript without identity is acknowledged
as pending canonical capture. Never hash the whole webhook for receipt identity:
artifact presigned URLs can change across retries even when the spoken evidence
is unchanged. Hash only canonical semantic fields for payload conflict checks.
Browser transcripts and vendor events are transport observations, not authority to
change learning, consent, ownership or approval. Voice keys must be connected in
the Vapi organization for custom ElevenLabs clone IDs to work. The adapter's
default multilingual transcription configuration is ElevenLabs
`scribe_v2_realtime`; verify enabled account credentials and actual Arabic
transcription with a live acceptance call.

## Failure and readiness contract

All adapters use finite connect/read timeouts, stream response bodies under size
limits, disable automatic redirects, and perform zero automatic mutation retries.
`ProviderError(status_code, code, message, uncertain=..., upstream_status=...)`
deliberately omits upstream request, response and credential details. The internal
`uncertain` and `upstream_status` fields let durable operation tracking distinguish
confirmed rejection from a possible remote creation. Timeouts, transport errors,
HTTP 408/5xx, oversized successful responses and malformed creation success are
uncertain. Confirmed authentication, rate-limit and other 4xx rejections are not.
The public `as_dict()` exposes only code and message. Never log raw provider requests, responses,
objects, or errors from the HTTP client.

`get_provider_status()` reports configuration, enablement and capabilities, not connectivity.
`live_verified=false` is intentional until a real acceptance session proves the
configured service works. Secret values and provider URLs never appear in this
status response. The public-key restriction flag is an operator attestation, not
an API verification of Vapi's account settings.

Offline adapter tests use `httpx.MockTransport` and cannot fall through to the
network. Run `python -m pytest tests/test_providers.py -q`.

## Official API references checked for this implementation

- [OpenAI structured output](https://developers.openai.com/api/docs/guides/structured-outputs): Chat Completions `response_format` with strict JSON schema and refusal handling.
- [ElevenLabs create instant voice clone](https://elevenlabs.io/docs/api-reference/voices/ivc/create): multipart `/v1/voices/add`, `voice_id`, and `requires_verification`.
- [ElevenLabs create speech](https://elevenlabs.io/docs/api-reference/text-to-speech/convert): `/v1/text-to-speech/{voice_id}` and MP3 output format.
- [ElevenLabs get voice](https://elevenlabs.io/docs/api-reference/voices/get): provider verification state for a blocked-candidate retry.
- [ElevenLabs delete voice](https://elevenlabs.io/docs/api-reference/voices/delete).
- [Vapi Web SDK](https://github.com/VapiAI/client-sdk-web): public-key startup with an assistant ID, transcript events and interruption controls.
- [Current official Vapi SDK schema](https://raw.githubusercontent.com/VapiAI/client-sdk-web/main/api.ts): `CustomLLMModel`, `CreateCustomLLMCredentialDTO`, `Server`, and explicit `serverMessages`.
- [Vapi server events](https://docs.vapi.ai/server-url/events): committed conversation updates, optional transcript metadata and informational webhook shapes.
- [Vapi API keys](https://docs.vapi.ai/security-and-privacy/api-keys): private credentials stay server-side; restrict public keys to allowed origins and assistants.
- [Vapi custom LLM server](https://docs.vapi.ai/customization/custom-llm/using-your-server): authenticated OpenAI-compatible completion endpoint.
- [Vapi ElevenLabs voices](https://docs.vapi.ai/providers/voice/elevenlabs) and [custom voices](https://docs.vapi.ai/customization/custom-voices/elevenlabs): organization provider credentials and clone synchronization.
