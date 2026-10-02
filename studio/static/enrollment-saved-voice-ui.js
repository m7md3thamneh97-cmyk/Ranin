// Saved synthesized samples only. No provider credentials, synthesis or training.
export async function openSavedVoice({ request, dialog, text, escape }) {
  const d = dialog(text('myVoice'), `<p>${escape(text('savedVoiceIntro'))}</p><div id="savedVoiceContent" role="region" aria-label="${escape(text('myVoice'))}"><p role="status">${escape(text('savedVoiceLoading'))}</p></div>`);
  const host = d.querySelector('#savedVoiceContent');
  let urls = [], epoch = 0;
  const clear = () => {
    host.querySelectorAll('audio').forEach(audio => { audio.pause(); audio.removeAttribute('src'); audio.load(); });
    urls.forEach(url => URL.revokeObjectURL(url));
    urls = [];
  };
  d.addEventListener('close', () => { epoch++; clear(); }, { once: true });
  const labels = { question: 'questionSample', number: 'numberSample', correction: 'correctionSample' };
  try {
    const result = await request('/api/enrollment/sessions?limit=50');
    if (!d.isConnected) return;
    const sessions = result.sessions.filter(s => !s.revoked && s.voice_state === 'ready');
    if (!sessions.length) {
      host.innerHTML = `<p role="status">${escape(text('savedVoiceEmpty'))}</p>`;
      return;
    }
    host.innerHTML = `<label for="savedVoiceSession">${escape(text('savedVoiceSession'))}</label><select id="savedVoiceSession">${sessions.map((s, i) => `<option value="${escape(s.id)}">${escape(text('myVoice'))} ${i + 1}${s.created ? ' · ' + escape(s.created.slice(0, 10)) : ''}</option>`).join('')}</select><div id="savedVoiceSamples"></div>`;
    const select = host.querySelector('select'), samples = host.querySelector('#savedVoiceSamples');
    const load = async () => {
      const version = ++epoch;
      clear();
      samples.innerHTML = `<p role="status">${escape(text('savedVoiceLoading'))}</p>`;
      const base = '/api/enrollment/sessions/' + encodeURIComponent(select.value) + '/voice-samples';
      try {
        const metadata = await request(base);
        const kinds = metadata.samples.filter(kind => Object.hasOwn(labels, kind));
        const audio = await Promise.all(kinds.map(kind => request(base + '/' + kind, { blob: true })));
        if (!d.isConnected || version !== epoch) return;
        if (!kinds.length) {
          samples.innerHTML = `<p role="status">${escape(text('savedVoiceUnavailable'))}</p>`;
          return;
        }
        urls = audio.map(blob => URL.createObjectURL(blob));
        samples.innerHTML = `<div class="sample-list">${kinds.map((kind, i) => `<div class="sample-card"><h3>${escape(text(labels[kind]))}</h3><audio data-saved-voice="${kind}" controls preload="metadata" src="${escape(urls[i])}" aria-label="${escape(text(labels[kind]))}"></audio></div>`).join('')}</div>`;
        const players = [...samples.querySelectorAll('audio')];
        players.forEach(player => player.addEventListener('play', () => players.forEach(other => { if (other !== player) other.pause(); })));
      } catch {
        if (d.isConnected && version === epoch) samples.innerHTML = `<p role="alert">${escape(text('savedVoiceUnavailable'))}</p>`;
      }
    };
    select.addEventListener('change', load);
    await load();
  } catch {
    if (d.isConnected) host.innerHTML = `<p role="alert">${escape(text('savedVoiceUnavailable'))}</p>`;
  }
}
