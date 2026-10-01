// Audio collection, local preparation and provider synthesis are different
// failures. A synthesis/decoder problem must never ask for more interview time.
export function workflowFailure(error, translate, phase='') {
  const raw=error?.detail;
  const structured=raw&&typeof raw==='object'?raw:null;
  const code=structured?.code||'';
  const message=String(structured?.message??raw??'').toLowerCase();
  const details=structured?.details||{};
  const t=translate;
  if(code==='outcome_unknown'||code==='provider_outcome_unknown')return t('outcomePending');
  if(error?.message==='request_timeout')return t('operationTimeout');
  if(code==='audio_analysis_incomplete')return t('audioAnalysisIncomplete');
  if(code==='insufficient_audio'){
    const reasons=details.rejected_reasons||{};
    if(!details.decoded_source_ms&&Object.keys(reasons).some((key)=>!['insufficient_audible_audio','clipped_audio'].includes(key)))return t('audioReadFailed');
    if(!details.selected_active_ms&&reasons.clipped_audio)return t('audioClipped');
    if(['sample_duration','sample_and_activity'].includes(details.insufficiency)&&Number.isFinite(details.selected_duration_ms)&&Number.isFinite(details.minimum_sample_ms)){
      return t('sampleAmount').replace('{seconds}',String(Math.floor(details.selected_duration_ms/1000))).replace('{minimum}',String(Math.ceil(details.minimum_sample_ms/1000)));
    }
    if(Number.isFinite(details.selected_active_ms)&&Number.isFinite(details.minimum_active_ms)){
      return t('speechAmount').replace('{seconds}',String(Math.floor(details.selected_active_ms/1000))).replace('{minimum}',String(Math.ceil(details.minimum_active_ms/1000)));
    }
    return t('moreSpeech');
  }
  if(code==='decoder_unavailable')return t('audioCheckUnavailable');
  if(['audio_prepare_timeout','audio_decode_timeout'].includes(code))return t('audioCheckTimeout');
  if(code==='audio_sequence_gap')return t('saveFirst');
  if(['invalid_audio','audio_unavailable','audio_checksum_mismatch','audio_analysis_changed','audio_too_long','invalid_audio_file','audio_too_large','unsupported_audio','invalid_audio_manifest','invalid_audio_directory','invalid_audio_limits','invalid_speaker_role','invalid_final_sequence'].includes(code))return t('audioReadFailed');
  if(code==='voice_clone_failed')return t('cloneFailed');
  if(['voice_preview_failed','invalid_voice_preview','preview_operation_failed'].includes(code)||error?.message==='empty_preview')return t('sampleFailed');
  if(message.includes('verification'))return t('verificationPending');
  if(message.includes('unknown')||message.includes('dispatching')||message.includes('reconcile'))return t('outcomePending');
  if(phase==='makingSamples')return t('sampleFailed');
  if(message.includes('confirmed')||message.includes('evidence'))return t('noExamples');
  // Compatibility with a tab connected to the previous plain-text API.
  if(/^(need more (clear )?contributor (audio|microphone speech)|record more contributor speech)/.test(message))return t('moreSpeech');
  if(message.includes('audio preparation took too long'))return t('audioCheckTimeout');
  if(error?.status===503)return t('notConfigured');
  if(phase==='creatingVoice')return t('cloneFailed');
  const names={NotAllowedError:'denied',NotFoundError:'missing',NotReadableError:'busyMic'};
  return t(names[error?.name]||({401:'accessExpired',403:'accessExpired',503:'unavailable',409:'conflict',413:'tooLarge'}[error?.status])||'failed');
}
