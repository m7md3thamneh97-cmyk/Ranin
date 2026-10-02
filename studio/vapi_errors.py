"""Bounded Vapi rejection hints. Never retain provider messages or rejected values."""
import re

# Only schema names can survive into a stored/public error. Arbitrary identifiers,
# quoted values, prompts, URLs and header values are deliberately discarded.
FIELDS = frozenset('assistant transport provider model voice voiceId transcriber language name firstMessage firstMessageMode maxDurationSeconds backgroundSound artifactPlan recordingEnabled monitorPlan listenEnabled controlEnabled controlAuthenticationEnabled clientMessages serverMessages server url headers temperature maxTokens messages role content stability similarityBoost useSpeakerBoost speed roomDeleteOnUserLeaveEnabled'.split())
CONSTRAINTS = (
    ('should not exist', 'unsupported field'),
    ('must be one of', 'unsupported option'),
    ('must be a', 'invalid type'),
    ('should not be empty', 'required field'),
    ('is required', 'required field'),
)
# Diagnostic vocabulary is fixed. Only generic API/account terms can survive an
# unstructured rejection, never names, IDs, URLs, prompt text or credential values.
TERMS = frozenset('assistant assistantId transport provider daily web call calls create created model voice voiceId transcriber server clientMessages serverMessages headers api key public private authorization authentication invalid unauthorized forbidden permission permissions credential credentials custom missing required requires not supported unsupported enabled disabled allowed allow disallowed provided provide found exists exist does must should either one phoneNumberId customer customerId customers squad squadId workflow workflowId org organization billing payment card credit credits balance wallet insufficient limit limits exceeded concurrency concurrent maximum trial free plan subscription account error failed failure bad request valid validation type'.lower().split())


def rejection_hint(response) -> str:
    if len(response.content) > 32768:
        return ''
    try:
        data = response.json()
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    messages = []
    def collect(value, depth=0):
        if depth > 3 or len(messages) >= 32:
            return
        if isinstance(value, str):
            messages.append(value)
        elif isinstance(value, list):
            for item in value[:32]:
                collect(item, depth + 1)
        elif isinstance(value, dict):
            for key in ('message', 'error', 'detail'):
                collect(value.get(key), depth + 1)
    collect(data)
    hints = []
    for message in messages[:32]:
        if not isinstance(message, str) or len(message) > 2048:
            continue
        # Nest validation paths appear before the constraint. Only recognize that
        # prefix; never scan the rejected value or a list of allowed options.
        for phrase, label in CONSTRAINTS:
            prefix, separator, _ = message.partition(phrase)
            if not separator:
                continue
            tokens = re.findall(r'[A-Za-z][A-Za-z0-9]*', prefix)
            fields = [token for token in tokens if token in FIELDS]
            if fields:
                hint = '.'.join(fields[:6]) + ': ' + label
                if hint not in hints:
                    hints.append(hint)
            break
    if hints:
        return ' Validation: ' + '; '.join(hints[:8]) + '.'
    terms = []
    for message in messages:
        if len(message) > 2048:
            continue
        # Mask quoted values and URLs before recognizing vocabulary. Every output
        # token is from the fixed vocabulary; the provider's raw text is discarded.
        message = re.sub(r'https?://\S+|[\"\'][^\"\']*[\"\']|\b[A-Za-z0-9]+[-_][A-Za-z0-9_-]+\b', ' ', message)
        for token in re.findall(r'[A-Za-z][A-Za-z0-9]*', message.lower()):
            if token in TERMS and token not in terms:
                terms.append(token)
    return (' Rejection terms: ' + ', '.join(terms[:32]) + '.') if terms else ''
