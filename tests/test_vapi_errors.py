"""Synthetic rejection fixtures; provider values must never reach diagnostics."""
import asyncio

import httpx
import pytest

from studio.enrollment import ProviderError, Providers
from studio.vapi_errors import rejection_hint


def test_validation_hints_discard_values_and_unknown_fields():
    response = httpx.Response(400, json={'message': [
        'assistant.voice.model must be one of the following values: PRIVATE-SECRET',
        'assistant.server.headers must be an object; credential PRIVATE-SECRET',
        'privateSecret should not exist',
        'assistant.voice.model must be one of PRIVATE-SECRET',
    ]})
    assert rejection_hint(response) == (' Validation: assistant.voice.model: unsupported option; '
                                        'assistant.server.headers: invalid type.')


@pytest.mark.parametrize('body', [None, [], {'message': {}}, {'message': ['https://private.example/PRIVATE-SECRET']},
                                 {'message': [None, 'x' * 2049]}, {'message': 'x' * 32769}])
def test_unrecognized_rejections_discard_body(body):
    assert rejection_hint(httpx.Response(400, json=body)) == ''


def test_invalid_json_discarded():
    assert rejection_hint(httpx.Response(400, content=b'PRIVATE-SECRET')) == ''


def test_nested_account_error_retains_only_fixed_terms():
    response = httpx.Response(400, json={'message': {'error':
        'Account has insufficient credits for web call. PRIVATE-SECRET https://private.example/voice/123 "secret billing"'}})
    assert rejection_hint(response) == ' Rejection terms: account, insufficient, credits, web, call.'


def test_long_or_nested_unstructured_errors_are_bounded():
    response = httpx.Response(400, json={'message': {'error': {'message': {'error': 'invalid credentials'}}}})
    assert rejection_hint(response) == ''


def test_adapter_stores_only_safe_hint_without_retry(monkeypatch):
    requests = []
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def request(self, method, url, **kwargs):
            requests.append((method, url))
            return httpx.Response(400, json={'message': ['assistant.voice.model should not exist PRIVATE-SECRET']})
    monkeypatch.setattr(httpx, 'AsyncClient', Client)
    with pytest.raises(ProviderError) as failure:
        asyncio.run(Providers().vapi_json('synthetic-key', 'POST', '/call', json_body={}))
    assert str(failure.value) == 'Vapi returned HTTP 400. Validation: assistant.voice.model: unsupported field.'
    assert failure.value.uncertain is False
    assert len(requests) == 1
