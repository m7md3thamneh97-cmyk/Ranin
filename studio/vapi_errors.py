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


def rejection_hint(response) -> str:
    if len(response.content) > 32768:
        return ''
    try:
        data = response.json()
    except ValueError:
        return ''
    if not isinstance(data, dict):
        return ''
    messages = data.get('message', [])
    if isinstance(messages, str):
        messages = [messages]
    if not isinstance(messages, list):
        return ''
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
    return (' Validation: ' + '; '.join(hints[:8]) + '.') if hints else ''
