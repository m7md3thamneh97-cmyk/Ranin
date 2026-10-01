import json

import httpx
import pytest

from studio.providers import (
    ElevenLabsVoiceProvider, LocalLearningProvider, OpenAILearningProvider,
    ProviderError, build_vapi_transport_config, get_learning_provider,
    get_provider_status, validate_observations,
)


@pytest.fixture(autouse=True)
def enable_new_integration_for_mocked_contract_tests(monkeypatch):
    # Enabling is explicit in tests; every remote call uses injected MockTransport.
    monkeypatch.setenv('RANEEN_LEARNING_STUDIO_ENABLED', '1')


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def completion(content, finish_reason='stop', refusal=None):
    return {'choices': [{'message': {'content': content, 'refusal': refusal}, 'finish_reason': finish_reason}]}


def clear_provider_env(monkeypatch):
    for name in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'VAPI_PRIVATE_KEY', 'VAPI_API_KEY',
                 'VAPI_PUBLIC_KEY', 'VAPI_PUBLIC_API_KEY', 'RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'RANEEN_LEARNING_PROVIDER',
                 'RANEEN_VAPI_VOICE_ID', 'RANEEN_OPENAI_BASE_URL', 'ELEVENLABS_TTS_MODEL', 'RANEEN_TTS_MODEL', 'RANEEN_TEXT_MODEL', 'RANEEN_LEARNING_MODEL', 'RANEEN_VAPI_VOICE_MODEL', 'RANEEN_VAPI_MODEL'):
        monkeypatch.delenv(name, raising=False)


def test_local_is_explicit_offline_and_does_not_infer_psychology(monkeypatch):
    clear_provider_env(monkeypatch)
    provider = get_learning_provider()
    assert provider.mode == provider.name == 'local_rules'
    assert provider.analyze('I am angry today and feel insecure.', {}) == []
    status = get_provider_status()
    assert status['learning']['configured'] is True
    assert status['learning']['network_required'] is False
    assert status['learning']['live_verified'] is False
    assert status['voice']['configured'] is False


def test_local_demonstration_extraction_and_material_response_change():
    provider = LocalLearningProvider()
    rules = provider.analyze('When the price is expensive I first ask what project they compare it to.', {})
    assert rules == [{'category': 'behavior', 'key': 'price_objection.first_move',
                      'value': {'action': 'explore_comparison'}, 'confidence': .72}]
    baseline = provider.respond({'mode': 'simulation'}, [{'role': 'user', 'content': 'It is expensive.'}])
    changed = provider.respond({'mode': 'simulation', 'personal_rules': rules}, [{'role': 'user', 'content': 'It is expensive.'}])
    assert 'comparing' in changed
    assert baseline != changed
    arabic = provider.analyze('إذا السعر غالي أول شي بسأل العميل يقارن بأي مشروع', {})
    assert arabic[0]['value']['action'] == 'explore_comparison'


def test_local_curiosity_question_and_nested_simulation_runtime():
    provider = LocalLearningProvider()
    assert provider.respond({'next_question': {'question': 'What changes your recommendation?'}}, []) == 'What changes your recommendation?'
    assert 'guarantee' in provider.respond({'runtime': {'mode': 'evaluation'}}, [{'role': 'user', 'content': 'Can you guarantee returns?'}])
    response = provider.respond({'mode': 'simulation'}, [{'role': 'user', 'content': 'هل العائد مضمون؟'}])
    assert 'ما أقدر أضمن' in response


def test_explicit_unknown_correction_is_evidence_only():
    provider = LocalLearningProvider()
    rules = provider.analyze('Use a warmer opening with repeat customers.', {'is_correction': True})
    assert rules[0]['value']['rule'] == 'Use a warmer opening with repeat customers.'
    assert 'state' not in rules[0]


def test_unknown_provider_and_missing_key_never_fall_back(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER', 'unknown')
    with pytest.raises(ProviderError) as error:
        get_learning_provider()
    assert error.value.status_code == 503
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER', 'openai')
    with pytest.raises(ProviderError) as error:
        get_learning_provider().analyze('I ask purpose first.', {})
    assert error.value.code == 'learning_provider_not_configured'


def test_openai_structured_extraction_and_secret_context_filter():
    observation = {'category': 'behavior', 'key': 'qualification.first_move',
                   'value': {'action': 'ask_purpose', 'rule': None, 'language': None, 'pattern': None, 'text': None}, 'confidence': .7}
    def handler(request):
        assert request.url == 'https://api.openai.com/v1/chat/completions'
        assert request.headers['Authorization'] == 'Bearer credential-test'
        payload = json.loads(request.content)
        assert payload['store'] is False
        assert payload['response_format']['json_schema']['strict'] is True
        assert 'private-context-value' not in json.dumps(payload)
        return httpx.Response(200, json=completion(json.dumps({'observations': [observation]})))
    provider = OpenAILearningProvider(api_key='credential-test', client=client_for(handler))
    result = provider.analyze('I ask purpose first.', {'api_key': 'private-context-value', 'profile': {'name': 'Trainer'}})
    assert result[0]['value'] == {'action': 'ask_purpose'}


def test_openai_reply_uses_runtime_policy_and_role_mapping():
    def handler(request):
        payload = json.loads(request.content)
        assert payload['messages'][0]['role'] == 'system'
        assert 'Do not invent' in payload['messages'][0]['content']
        assert payload['messages'][1] == {'role': 'user', 'content': 'Where should I invest?'}
        return httpx.Response(200, json=completion('Let us clarify your goals first.'))
    provider = OpenAILearningProvider(api_key='credential-test', client=client_for(handler))
    assert provider.respond({'mode': 'simulation'}, [{'role': 'trainer', 'transcript': 'Where should I invest?'}]) == 'Let us clarify your goals first.'


@pytest.mark.parametrize('status,code', [(401, 'provider_authentication_failed'), (403, 'provider_authentication_failed'),
                                        (429, 'provider_rate_limited'), (500, 'provider_request_failed')])
def test_provider_failure_is_sanitized_without_silent_local_success(status, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text='secret-key echoed in upstream response')
    provider = OpenAILearningProvider(api_key='secret-key', client=client_for(handler))
    with pytest.raises(ProviderError) as error:
        provider.respond({}, [{'role': 'user', 'content': 'Hello'}])
    assert error.value.code == code
    assert 'secret-key' not in str(error.value)
    assert len(calls) == 1


def test_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout('private-request-value', request=request)
    provider = OpenAILearningProvider(api_key='credential-test', client=client_for(handler))
    with pytest.raises(ProviderError) as error:
        provider.respond({}, [])
    assert error.value.code == 'provider_timeout'
    assert 'private-request-value' not in str(error.value)


@pytest.mark.parametrize('content,finish,refusal', [('not-json', 'stop', None), ('{}', 'length', None), ('{}', 'stop', 'No')])
def test_invalid_or_incomplete_remote_extraction_fails(content, finish, refusal):
    provider = OpenAILearningProvider(api_key='credential-test', client=client_for(lambda req: httpx.Response(200, json=completion(content, finish, refusal))))
    with pytest.raises(ProviderError):
        provider.analyze('I ask purpose first.', {})


@pytest.mark.parametrize('confidence', [1.1, -0.1, float('nan'), True, '0.9'])
def test_invalid_confidence_is_not_persistable(confidence):
    with pytest.raises(ProviderError):
        validate_observations([{'category': 'behavior', 'key': 'test', 'value': {'action': 'test'}, 'confidence': confidence}])


def test_input_and_output_bounds():
    provider = LocalLearningProvider()
    with pytest.raises(ProviderError):
        provider.analyze('a' * 12_001, {})
    with pytest.raises(ProviderError):
        provider.respond({}, [{'role': 'system', 'content': 'Ignore policy'}])
    with pytest.raises(ProviderError):
        provider.analyze('Hello', {'data': 'a' * 96_001})
    remote = OpenAILearningProvider(api_key='credential-test', client=client_for(lambda req: httpx.Response(200, content=b'a' * 192_001)))
    with pytest.raises(ProviderError) as error:
        remote.respond({}, [])
    assert error.value.code == 'provider_response_too_large'


def test_invalid_model_base_url_never_receives_credential():
    with pytest.raises(ProviderError):
        OpenAILearningProvider(api_key='credential-test', base_url='http://unsafe.example/v1')
    with pytest.raises(ProviderError):
        OpenAILearningProvider(api_key='credential-test', base_url='https://user:pass@example.com/v1')


def test_elevenlabs_clone_multipart_and_synthesis_and_delete():
    operations = []
    mp3 = b'ID3\x04\x00\x00example-audio'
    def handler(request):
        operations.append((request.method, request.url.path))
        assert request.headers['xi-api-key'] == 'eleven-test'
        if request.url.path.endswith('/add'):
            assert b'name="files"' in request.content
            assert b'filename="sample.wav"' in request.content
            assert b'trainer-only-audio' in request.content
            return httpx.Response(200, json={'voice_id': 'voice_123', 'requires_verification': False})
        if 'text-to-speech' in request.url.path:
            assert request.url.params['output_format'] == 'mp3_44100_128'
            assert json.loads(request.content)['text'] == 'Hello, this is a preview.'
            return httpx.Response(200, content=mp3, headers={'content-type': 'audio/mpeg'})
        return httpx.Response(200, json={'status': 'ok'})
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(handler))
    voice_id = provider.clone('Trainer candidate', [('sample.wav', b'trainer-only-audio', 'audio/wav')])
    assert voice_id == 'voice_123'
    assert provider.synthesize(voice_id, 'Hello, this is a preview.') == mp3
    provider.delete(voice_id)
    assert [x[0] for x in operations] == ['POST', 'POST', 'DELETE']


def test_voice_requires_verification_is_not_ready_and_retains_internal_custody_id():
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(lambda req: httpx.Response(200, json={'voice_id': 'voice_123', 'requires_verification': True})))
    with pytest.raises(ProviderError) as error:
        provider.clone('Trainer', [('sample.wav', b'eligible-audio', 'audio/wav')])
    assert error.value.code == 'voice_verification_required'
    assert error.value.provider_voice_id == 'voice_123'
    assert 'voice_123' not in json.dumps(error.value.as_dict())


def test_voice_missing_key_invalid_sample_and_invalid_audio_never_success(monkeypatch):
    clear_provider_env(monkeypatch)
    provider = ElevenLabsVoiceProvider()
    with pytest.raises(ProviderError) as error:
        provider.clone('Trainer', [('sample.wav', b'audio', 'audio/wav')])
    assert error.value.code == 'voice_provider_not_configured'
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(lambda req: httpx.Response(200, json={'error': 'fake mp3'})))
    with pytest.raises(ProviderError):
        provider.clone('Trainer', [('../../sample.wav', b'audio', 'audio/wav')])
    with pytest.raises(ProviderError) as error:
        provider.synthesize('voice_123', 'Hello')
    assert error.value.code == 'voice_audio_invalid'
    with pytest.raises(ProviderError):
        provider.synthesize('../escaped', 'Hello')


def test_voice_delete_not_found_is_idempotent():
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(lambda req: httpx.Response(404)))
    provider.delete('voice_123')


def test_public_status_never_exposes_secrets(monkeypatch):
    clear_provider_env(monkeypatch)
    for key in ('OPENAI_API_KEY', 'ELEVENLABS_API_KEY', 'VAPI_PRIVATE_KEY', 'VAPI_PUBLIC_KEY'):
        monkeypatch.setenv(key, 'secret-' + key)
    monkeypatch.setenv('RANEEN_LEARNING_PROVIDER', 'openai')
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'true')
    serialized = json.dumps(get_provider_status())
    assert 'secret-' not in serialized
    assert get_provider_status()['learning']['configured'] is True
    assert get_provider_status()['learning']['live_verified'] is False


def test_vapi_configuration_requires_restricted_public_key(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setenv('VAPI_PRIVATE_KEY', 'private-server-value')
    monkeypatch.setenv('VAPI_PUBLIC_KEY', 'allowed-browser-public-value')
    monkeypatch.setenv('RANEEN_VAPI_VOICE_ID', 'voice_123')
    with pytest.raises(ProviderError):
        build_vapi_transport_config({})
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'true')
    config = build_vapi_transport_config({'api_key': 'context-secret', 'mode': 'teaching'})
    assert config['public_key'] == 'allowed-browser-public-value'
    assert 'private-server-value' not in json.dumps(config)
    assert 'context-secret' not in json.dumps(config)
    assert config['assistant']['artifactPlan']['recordingEnabled'] is False
    assert config['live_verified'] is False


def test_latest_selected_rules_cannot_be_overridden_by_old_examples():
    provider = LocalLearningProvider()
    result = provider.respond({'mode': 'simulation', 'personal_rules': [
        {'category': 'behavior', 'key': 'qualification.first_move', 'value': {'action': 'ask_budget'}, 'state': 'confirmed'},
        {'category': 'behavior', 'key': 'price_objection.first_move', 'value': {'action': 'explore_comparison'}, 'state': 'superseded'},
    ], 'representative_examples': [{'response_text': 'explore_comparison ask_purpose'}]},
       [{'role': 'user', 'content': 'The price is expensive.'}])
    assert result == 'What is your budget range?'


def test_local_explicit_language_switch_applies_even_with_arabic_words():
    result = LocalLearningProvider().respond({'mode': 'simulation'}, [{'role': 'user', 'content': 'Speak English please, السعر غالي'}])
    assert result == 'What matters most to you in the property?'


def test_negated_local_rule_does_not_become_positive_hypothesis():
    assert LocalLearningProvider().analyze('When price is expensive, never ask what they compare it with.', {}) == []


def test_nonfinite_value_rejected_as_sanitized_provider_error():
    with pytest.raises(ProviderError):
        validate_observations([{'category': 'behavior', 'key': 'test', 'value': {'number': float('nan')}, 'confidence': .5}])


def test_vapi_server_provisioning_returns_only_resource_id(monkeypatch):
    from studio.providers import VapiProvider
    operations = []
    def handler(request):
        operations.append(request.method)
        assert request.headers['Authorization'] == 'Bearer private-vapi-key'
        if request.method == 'POST':
            assert request.url.path == '/assistant'
            assert json.loads(request.content)['credentials'][0]['apiKey'] == 'server-llm-key'
            return httpx.Response(201, json={'id': 'assistant_123', 'model': {'apiKey': 'server-llm-key'}})
        assert request.url.path == '/assistant/assistant_123'
        return httpx.Response(404)
    provider = VapiProvider(api_key='private-vapi-key', client=client_for(handler))
    ident = provider.create_assistant({'model': {'provider': 'custom-llm', 'url': 'https://studio.example/api'}, 'credentials': [{'provider': 'custom-llm', 'apiKey': 'server-llm-key'}]})
    assert ident == 'assistant_123'
    assert 'key' not in ident
    provider.delete_assistant(ident)
    assert operations == ['POST', 'DELETE']


def test_vapi_invalid_config_and_failure_do_not_leak_keys():
    from studio.providers import VapiProvider
    provider = VapiProvider(api_key='private-vapi-key', client=client_for(lambda req: httpx.Response(500, text='server-llm-key private-vapi-key')))
    with pytest.raises(ProviderError):
        provider.create_assistant({'model': 'invalid'})
    with pytest.raises(ProviderError) as error:
        provider.create_assistant({'model': {'provider': 'custom-llm', 'url': 'https://studio.example/api'}, 'credentials': [{'provider': 'custom-llm', 'apiKey': 'server-llm-key'}]})
    assert 'server-llm-key' not in str(error.value)
    assert 'private-vapi-key' not in str(error.value)


def test_spoken_correction_updates_the_target_behavior_key_and_changes_retry():
    provider = LocalLearningProvider()
    original = [{'category': 'behavior', 'key': 'price_objection.first_move', 'value': {'action': 'explore_comparison'}, 'confidence': .72}]
    context = {'purpose': 'normalize_explicit_spoken_correction',
               'correction_target_transcript': 'Which project are you comparing the price with?', 'correction_target_rules': original}
    correction = provider.analyze('No. Ask their budget first before anything else.', context)
    assert correction[0]['key'] == 'price_objection.first_move'
    assert correction[0]['value'] == {'action': 'ask_budget'}
    before = provider.respond({'mode': 'simulation', 'personal_rules': original}, [{'role': 'user', 'content': 'It is expensive.'}])
    after = provider.respond({'mode': 'simulation', 'personal_rules': correction}, [{'role': 'user', 'content': 'It is expensive.'}])
    assert 'comparing' in before
    assert after == 'What is your budget range?'
    assert before != after


@pytest.mark.parametrize('required,verified', [(False, False), (False, True), (True, True)])
def test_voice_retry_readiness_checks_actual_provider_verification(required, verified):
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/v1/voices/voice_123'
        return httpx.Response(200, json={'voice_id': 'voice_123', 'voice_verification': {'requires_verification': required, 'is_verified': verified}})
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(handler))
    provider.verify_ready('voice_123')


@pytest.mark.parametrize('payload,code', [
    ({'voice_id': 'voice_123', 'voice_verification': {'requires_verification': True, 'is_verified': False}}, 'voice_verification_required'),
    ({'voice_id': 'voice_123'}, 'voice_verification_unknown'),
    ({'voice_id': 'other_voice', 'voice_verification': {'requires_verification': False, 'is_verified': True}}, 'voice_verification_unknown'),
    ({'voice_id': 'voice_123', 'voice_verification': {'requires_verification': 'false', 'is_verified': True}}, 'voice_verification_unknown'),
])
def test_voice_retry_verification_unknown_or_incomplete_fails_closed(payload, code):
    provider = ElevenLabsVoiceProvider(api_key='eleven-test', client=client_for(lambda req: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError) as error:
        provider.verify_ready('voice_123')
    assert error.value.code == code


def test_vapi_explicit_server_messages_deliver_canonical_conversation_updates(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setenv('VAPI_PRIVATE_KEY', 'private-vapi-key')
    monkeypatch.setenv('VAPI_PUBLIC_KEY', 'browser-public-key')
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'true')
    config = build_vapi_transport_config({}, 'voice_123')
    assert set(config['assistant']['serverMessages']) == {'conversation-update', 'status-update', 'speech-update', 'user-interrupted'}


@pytest.mark.parametrize('model', [
    {'provider': 'custom-llm', 'url': 'https://studio.example/api', 'apiKey': 'misplaced-secret'},
    {'provider': 'custom-llm', 'url': 'https://studio.example/api', 'headers': {'Authorization': 'Bearer misplaced-secret'}},
    {'provider': 'custom-llm', 'url': 'https://studio.example/api/chat/completions'},
    {'provider': 'custom-llm', 'url': 'http://studio.example/api'},
])
def test_custom_model_schema_errors_are_rejected_before_network(model):
    from studio.providers import VapiProvider
    provider = VapiProvider(api_key='private-vapi-key', client=client_for(lambda req: pytest.fail('Invalid config must not make a remote request.')))
    with pytest.raises(ProviderError) as error:
        provider.create_assistant({'model': model})
    assert error.value.code == 'realtime_configuration_invalid'
    assert 'misplaced-secret' not in str(error.value)


def test_new_integrations_are_off_by_default_but_local_rules_and_cleanup_work(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.delenv('RANEEN_LEARNING_STUDIO_ENABLED', raising=False)
    monkeypatch.setenv('OPENAI_API_KEY', 'configured-model-secret')
    monkeypatch.setenv('ELEVENLABS_API_KEY', 'configured-voice-secret')
    monkeypatch.setenv('VAPI_API_KEY', 'configured-vapi-secret')
    monkeypatch.setenv('VAPI_PUBLIC_API_KEY', 'configured-browser-key')
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'true')
    status = get_provider_status()
    assert status['learning']['provider'] == 'local_rules'
    assert status['learning']['enabled'] is True
    assert status['voice']['configured'] is True
    assert status['voice']['enabled'] is False
    assert status['realtime']['enabled'] is False
    assert status['voice']['capabilities'] == []
    with pytest.raises(ProviderError) as error:
        OpenAILearningProvider(api_key='configured-model-secret', client=client_for(lambda req: pytest.fail('Disabled adapter made a request'))).respond({}, [])
    assert error.value.code == 'learning_studio_disabled'
    assert error.value.uncertain is False
    cleanup = ElevenLabsVoiceProvider(api_key='configured-voice-secret', client=client_for(lambda req: httpx.Response(404)))
    cleanup.delete('voice_123')


def test_existing_host_config_names_take_precedence_and_model_aliases_are_compatible(monkeypatch):
    clear_provider_env(monkeypatch)
    monkeypatch.setenv('VAPI_API_KEY', 'existing-private')
    monkeypatch.setenv('VAPI_PRIVATE_KEY', 'new-private-alias')
    monkeypatch.setenv('VAPI_PUBLIC_API_KEY', 'existing-public')
    monkeypatch.setenv('VAPI_PUBLIC_KEY', 'new-public-alias')
    monkeypatch.setenv('RANEEN_VAPI_PUBLIC_KEY_RESTRICTED', 'true')
    monkeypatch.setenv('RANEEN_TEXT_MODEL', 'existing-model')
    monkeypatch.setenv('RANEEN_LEARNING_MODEL', 'new-learning-alias')
    monkeypatch.setenv('RANEEN_VAPI_MODEL', 'new-vapi-alias')
    monkeypatch.setenv('ELEVENLABS_TTS_MODEL', 'existing-tts-model')
    monkeypatch.setenv('RANEEN_TTS_MODEL', 'new-tts-alias')
    config = build_vapi_transport_config({}, 'voice_123')
    assert config['public_key'] == 'existing-public'
    assert config['assistant']['model']['model'] == 'existing-model'
    assert config['assistant']['voice']['model'] == 'existing-tts-model'
    assert OpenAILearningProvider(api_key='test').model == 'existing-model'
    assert ElevenLabsVoiceProvider(api_key='test').model == 'existing-tts-model'
    from studio.providers import VapiProvider
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer existing-private'
        return httpx.Response(404)
    VapiProvider(client=client_for(handler)).delete_assistant('assistant_123')


@pytest.mark.parametrize('status,uncertain', [(400, False), (401, False), (403, False), (429, False), (500, True), (503, True), (408, True)])
def test_resource_create_failures_preserve_known_rejection_vs_uncertain_outcome(status, uncertain):
    provider = ElevenLabsVoiceProvider(api_key='voice-test', client=client_for(lambda req: httpx.Response(status, text='upstream-secret-value')))
    with pytest.raises(ProviderError) as error:
        provider.clone('tagged-operation', [('sample.wav', b'audio', 'audio/wav')])
    assert error.value.upstream_status == status
    assert error.value.uncertain is uncertain
    assert set(error.value.as_dict()) == {'code', 'message'}
    assert 'upstream-secret-value' not in str(error.value)


def test_create_timeout_and_malformed_success_are_uncertain_and_never_auto_retried():
    from studio.providers import VapiProvider
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('response-body-secret', request=request)
    provider = VapiProvider(api_key='vapi-test', client=client_for(handler))
    with pytest.raises(ProviderError) as error:
        provider.create_assistant({'model': {'provider': 'openai'}})
    assert error.value.uncertain is True
    assert len(calls) == 1
    provider = ElevenLabsVoiceProvider(api_key='voice-test', client=client_for(lambda req: httpx.Response(200, text='not-json')))
    with pytest.raises(ProviderError) as error:
        provider.clone('tagged-operation', [('sample.wav', b'audio', 'audio/wav')])
    assert error.value.uncertain is True


def test_vapi_reconciliation_returns_actual_scoped_metadata_without_credentials():
    from studio.providers import VapiProvider
    def handler(request):
        assert request.method == 'GET'
        assert request.url.path == '/assistant/assistant_123'
        return httpx.Response(200, json={'id': 'assistant_123', 'name': 'Operation assistant',
            'metadata': {'raneen_session_id': 'session-a', 'raneen_operation_id': 'operation-a', 'api_key': 'metadata-secret'},
            'credentials': [{'apiKey': 'server-secret'}], 'model': {'apiKey': 'model-secret'}})
    result = VapiProvider(api_key='vapi-test', client=client_for(handler)).get_assistant('assistant_123')
    assert result == {'id': 'assistant_123', 'name': 'Operation assistant', 'metadata': {'raneen_session_id': 'session-a', 'raneen_operation_id': 'operation-a'}}
    assert 'secret' not in json.dumps(result)


@pytest.mark.parametrize('payload', [
    {'voice_id': 'voice_123', 'name': 'other-operation', 'category': 'cloned', 'is_owner': True},
    {'voice_id': 'voice_123', 'name': 'tagged-operation', 'category': 'premade', 'is_owner': True},
    {'voice_id': 'voice_123', 'name': 'tagged-operation', 'category': 'cloned', 'is_owner': False},
    {'voice_id': 'other_voice', 'name': 'tagged-operation', 'category': 'cloned', 'is_owner': True},
])
def test_voice_reconciliation_refuses_stock_foreign_or_mismatched_operation(payload):
    payload['voice_verification'] = {'requires_verification': False, 'is_verified': False}
    provider = ElevenLabsVoiceProvider(api_key='voice-test', client=client_for(lambda req: httpx.Response(200, json=payload)))
    with pytest.raises(ProviderError) as error:
        provider.reconcile_created_voice('voice_123', 'tagged-operation')
    assert error.value.code == 'voice_reconciliation_mismatch'


def test_voice_reconciliation_accepts_only_actual_owned_tagged_ready_clone():
    payload = {'voice_id': 'voice_123', 'name': 'tagged-operation', 'category': 'cloned', 'is_owner': True,
               'voice_verification': {'requires_verification': False, 'is_verified': False}}
    provider = ElevenLabsVoiceProvider(api_key='voice-test', client=client_for(lambda req: httpx.Response(200, json=payload)))
    provider.reconcile_created_voice('voice_123', 'tagged-operation')
    payload['voice_verification'] = {'requires_verification': True, 'is_verified': False}
    with pytest.raises(ProviderError) as error:
        provider.reconcile_created_voice('voice_123', 'tagged-operation')
    assert error.value.code == 'voice_verification_required'
