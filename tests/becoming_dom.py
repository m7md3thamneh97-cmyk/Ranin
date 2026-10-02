"""Synthetic browser regression for the actual single-conversation page and CSP.

Run after installing requirements-browser.txt and Playwright Chromium:
    python tests/becoming_dom.py

HTML, CSS, modules, the PCM worklet and response security headers are served by
the real FastAPI app. Becoming API responses, the event relay and Daily SDK are explicit
synthetic fixtures; getUserMedia returns an oscillator, never a physical mic.
This does not establish live provider, real microphone or voice-quality proof.
No API keys, real speech, provider calls or external HTTP requests are allowed.
Screenshots go to RANEEN_UI_OUTPUT_DIR (default /tmp/raneen-becoming-ui).
Set CHROMIUM_EXECUTABLE only for an already installed Chromium binary.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from studio.runtime import create_app

ORIGIN = "https://raneen.test"
SESSION_ID = "1234567890abcdef1234567890abcdef"
CAPABILITY = "synthetic-private-capability-never-a-real-token"
CALL_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
VOICE_ID = "synthetic_voice_fixture"

SYNTHETIC_MEDIA = r"""
(() => {
  const state = window.__syntheticMedia = {requested: 0, active: 0, stopped: 0};
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: async options => {
    state.requested++;
    if (window.__denyMicrophone) throw new DOMException('Synthetic permission denial', 'NotAllowedError');
    if (options.video !== false || options.audio.autoGainControl !== false) throw Error('Unexpected microphone configuration');
    // Chromium cannot bridge Web Audio-produced streams between different
    // context sample rates. Physical microphone capture normally resamples;
    // this oscillator fixture must match the actual 24 kHz capture context.
    const context = new AudioContext({sampleRate: 24000});
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const destination = context.createMediaStreamDestination();
    gain.gain.value = 0.04;
    oscillator.frequency.value = 440;
    oscillator.connect(gain).connect(destination);
    oscillator.start();
    await context.resume();
    const stream = destination.stream;
    for (const track of stream.getTracks()) {
      state.active++;
      const originalStop = track.stop.bind(track);
      let stopped = false;
      track.stop = () => {
        if (stopped) return;
        stopped = true;
        state.active--; state.stopped++;
        originalStop(); oscillator.stop(); context.close().catch(() => {});
      };
    }
    return stream;
  }});
})();
"""

SYNTHETIC_RELAY = r"""
(() => {
  const state = window.__syntheticRelay = {sources: [], closed: 0, sequence: 0};
  window.EventSource = class {
    static CONNECTING = 0; static OPEN = 1; static CLOSED = 2;
    constructor(url, options) {
      const parsed = new URL(url, location.origin);
      if (parsed.origin !== location.origin || !/^\/api\/becoming\/sessions\/[0-9a-f]{32}\/events-stream$/.test(parsed.pathname) || parsed.search || parsed.hash || options?.withCredentials !== true) throw Error('Unsafe provider relay');
      this.url = parsed.href; this.withCredentials = true;
      this.readyState = 1; this.listeners = new Map();
      state.sources.push(this);
      const heartbeat = () => {
        if (this.readyState !== 1) return;
        const event = new MessageEvent('heartbeat', {data: JSON.stringify({server_time_ms: Date.now()})});
        for (const callback of this.listeners.get('heartbeat') || []) callback(event);
      };
      queueMicrotask(() => {
        const event = new Event('open');
        this.onopen?.(event);
        for (const callback of this.listeners.get('open') || []) callback(event);
        heartbeat();
      });
      this.heartbeat = setInterval(heartbeat, 1000);
    }
    addEventListener(name, callback) {
      if (!this.listeners.has(name)) this.listeners.set(name, []);
      this.listeners.get(name).push(callback);
    }
    removeEventListener(name, callback) {
      this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== callback));
    }
    close() { if (this.readyState !== 2) { this.readyState = 2; state.closed++; clearInterval(this.heartbeat); } }
  };
  window.__emitRelay = message => {
    const source = state.sources.at(-1);
    if (!source || source.readyState === 2) throw Error('Relay is not open');
    const timestamp = Date.now();
    const payload = {call_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', event_at_ms: timestamp, received_at_ms: timestamp, relay_at_ms: timestamp, event_time_source: 'provider_timestamp', ...message};
    const eventId = String(++state.sequence);
    const event = new MessageEvent('provider_event', {data: JSON.stringify(payload), origin: location.origin, lastEventId: eventId});
    for (const callback of source.listeners.get('provider_event') || []) callback(event);
  };
})();
"""

SYNTHETIC_DAILY = r"""
window.__syntheticDaily = {created: 0, joined: 0, left: 0, destroyed: 0, handlers: {}};
window.DailyIframe = {createCallObject(options) {
  const state = window.__syntheticDaily;
  if (options.audioSource?.kind !== 'audio' || options.videoSource !== false || options.avoidEval !== true) throw Error('Unexpected Daily options');
  state.created++;
  const handlers = state.handlers;
  return {
    on(name, callback) { handlers[name] = callback; return this; },
    async join(options) {
      if (options.url !== 'https://raneen.daily.co/synthetic-room' || options.token !== 'synthetic-room-token') throw Error('Unexpected Daily room');
      state.joined++; handlers['joined-meeting']?.();
      // Remote silence exercises the separate playback/analyser graph while
      // the microphone oscillator alone supplies contributor PCM.
      const context = state.remoteContext = new AudioContext();
      const oscillator = state.remoteOscillator = context.createOscillator();
      const gain = context.createGain(); gain.gain.value = 0;
      const destination = context.createMediaStreamDestination();
      oscillator.connect(gain).connect(destination); oscillator.start();
      await context.resume();
      state.remoteStream = destination.stream;
      handlers['track-started']?.({participant: {local: false}, track: destination.stream.getAudioTracks()[0]});
    },
    async leave() { state.left++; handlers['left-meeting']?.(); },
    async destroy() {
      state.destroyed++;
      state.remoteStream?.getTracks().forEach(track => track.stop());
      state.remoteOscillator?.stop();
      await state.remoteContext?.close();
    },
  };
}};
window.__emitDaily = message => window.__syntheticDaily.handlers['app-message']?.({data: message});
"""


def synthetic_preview() -> bytes:
    """Decodable synthetic tone for the optional retained-voice audio control."""
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
        "-i", "sine=frequency=440:sample_rate=24000:duration=0.3", "-ac", "1",
        "-c:a", "libmp3lame", "-b:a", "64000", "-f", "mp3", "pipe:1",
    ], capture_output=True, check=True, timeout=10)
    return result.stdout


class SyntheticBecoming:
    """Browser contract fixture, independent of the separately tested service."""

    def __init__(self, client, *, configured=True):
        self.client = client
        self.configured = configured
        self.state = "COLLECTING_VOICE"
        self.voice_ready = False
        self.created = 0
        self.calls = 0
        self.ended = 0
        self.revoked = 0
        self.chunks = []
        self.events = []
        self.external_requests = []
        self.errors = []
        self.console_errors = []
        self.failed_requests = []
        self.headers = {}
        self.preview = synthetic_preview()

    def snapshot(self):
        return {
            "id": SESSION_ID, "state": self.state, "call_id": CALL_ID,
            "call_state": "closed" if self.state in {"ENDED", "REVOKED"} else "open",
            "voice_ready": self.voice_ready, "voice_id": VOICE_ID if self.voice_ready else None,
            "eligible_audio_seconds": 31 if self.voice_ready else 0,
            "minimum_speech_seconds": 30, "next_sequence": len(self.chunks),
            "telemetry": {}, "failure": None,
        }

    def fulfill_json(self, route, payload, *, status=200, headers=None):
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload), headers=headers or {})

    def route(self, route):
        try:
            self._route(route)
        except Exception as error:
            self.errors.append(repr(error))
            route.fulfill(status=500, content_type="application/json", body='{"error":"synthetic fixture assertion failed"}')

    def _route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
            self.external_requests.append(request.url)
            route.abort()
            return
        path = parsed.path
        if path == "/static/vendor/daily-0.87.0.js":
            route.fulfill(status=200, content_type="application/javascript", body=SYNTHETIC_DAILY)
            return
        if path == "/api/becoming/readiness":
            self.fulfill_json(route, {"enabled": True, "configured": self.configured,
                "minimum_speech_seconds": 30, "max_duration_seconds": 90,
                "provider_access_verified": False, "consent_version": "becoming-v1", "retention_days": 7})
            return
        if path == "/api/becoming/sessions":
            assert request.method == "POST"
            body = request.post_data_json
            assert body["consent_version"] == "becoming-v1" and body["language"] in {"ar", "en"}
            assert all(body[key] is True for key in ("own_voice", "recording", "external_processing", "voice_cloning", "private_preview"))
            self.created += 1
            assert self.created == 1, "A single-conversation flow must not create another enrollment"
            self.fulfill_json(route, {"id": SESSION_ID, "capability": CAPABILITY, "state": "IDLE"}, status=201,
                headers={"Set-Cookie": f"raneen_becoming={CAPABILITY}; Path=/api/becoming/sessions/{SESSION_ID}; Max-Age=604800; Secure; HttpOnly; SameSite=Strict"})
            return
        prefix = "/api/becoming/sessions/" + SESSION_ID
        if path == prefix or path.startswith(prefix + "/"):
            assert request.headers.get("authorization") == "Bearer " + CAPABILITY or f"raneen_becoming={CAPABILITY}" in request.headers.get("cookie", ""), "Private session requests must retain their capability"
            suffix = path[len(prefix):]
            if suffix == "" and request.method == "GET":
                self.fulfill_json(route, self.snapshot())
            elif suffix == "/call" and request.method == "POST":
                self.calls += 1
                assert self.calls == 1, "Voice handoff must keep the original call"
                self.fulfill_json(route, {"call_id": CALL_ID, "web_call_url": "https://raneen.daily.co/synthetic-room",
                    "call_token": "synthetic-room-token", "max_duration_seconds": 90})
            elif suffix == "/events" and request.method == "POST":
                event = request.post_data_json
                assert event["call_id"] == CALL_ID
                assert CAPABILITY not in request.post_data
                self.events.append(event)
                self.fulfill_json(route, {"accepted": True, "state": self.state})
            elif suffix.startswith("/chunks/") and request.method == "PUT":
                raw = request.post_data_buffer
                assert hashlib.sha256(raw).hexdigest() == request.headers["x-chunk-sha256"]
                settings = json.loads(request.headers["x-capture-settings"])
                assert settings["source"] == "isolated_microphone" and settings["speaker"] == "user"
                assert settings["assistant_overlap"] is False
                with wave.open(io.BytesIO(raw)) as audio:
                    assert audio.getframerate() == 24000 and audio.getnchannels() == 1 and audio.getsampwidth() == 2
                    assert 0 < audio.getnframes() <= 125 * 24000
                sequence = int(suffix.rsplit("/", 1)[1])
                assert sequence == len(self.chunks)
                self.chunks.append({"seq": sequence, "bytes": len(raw)})
                self.fulfill_json(route, {"seq": sequence, "state": self.state})
            elif suffix == "/end" and request.method == "POST":
                self.ended += 1
                self.state = "ENDED"
                self.fulfill_json(route, self.snapshot())
            elif suffix == "/revoke" and request.method == "POST":
                assert request.post_data_json == {"confirm": True}
                self.revoked += 1
                self.state = "REVOKED"
                self.voice_ready = False
                self.fulfill_json(route, self.snapshot())
            elif suffix == "/voice-check" and request.method == "GET":
                assert self.voice_ready and self.state == "ENDED"
                route.fulfill(status=200, content_type="audio/mpeg", body=self.preview)
            else:
                raise AssertionError("Unexpected becoming request: " + request.method + " " + suffix)
            return
        response = self.client.request(request.method, path + ("?" + parsed.query if parsed.query else ""),
            headers=request.headers, content=request.post_data_buffer)
        headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}}
        if path == "/":
            self.headers = headers
        route.fulfill(status=response.status_code, headers=headers, body=response.content)


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "The experience overflows a mobile viewport"


def start(page, fixture):
    expect(page.locator("#create")).to_be_disabled()
    assert page.locator('input[type="text"], input[type="password"], textarea').count() == 0
    assert page.evaluate("window.__syntheticMedia.requested") == 0
    assert fixture.created == fixture.calls == 0
    page.locator("#consent").check()
    expect(page.locator("#create")).to_be_enabled()
    page.locator("#create").click()
    try:
        expect(page.locator("#experience")).to_have_attribute("data-state", "COLLECTING_VOICE")
    except AssertionError:
        try:
            debug = json.loads(page.locator("#debug").inner_text())
            debug_failure = {key: debug[key] for key in ("failure_stage", "failure_name", "last_failure_stage", "last_failure_name") if key in debug}
        except (ValueError, TypeError):
            debug_failure = {}
        print("Synthetic bootstrap diagnostics:", json.dumps({
            "state": page.locator("#experience").get_attribute("data-state"),
            "notice": page.locator("#notice").inner_text(),
            "media": page.evaluate("window.__syntheticMedia"),
            "sessions_created": fixture.created, "calls_created": fixture.calls,
            "fixture_errors": fixture.errors, "browser_console_errors": fixture.console_errors,
            "failed_request_paths": fixture.failed_requests,
            "failure_stage_and_name": debug_failure,
        }, ensure_ascii=False))
        raise
    assert page.evaluate("window.__syntheticMedia.active") == 1
    assert page.evaluate("window.__syntheticDaily.created") == 1
    assert page.evaluate("window.__syntheticRelay.sources.length === 1 && window.__syntheticRelay.sources[0].readyState === 1")
    assert page.evaluate("localStorage.getItem('raneen-becoming-session')") == SESSION_ID
    assert page.evaluate("sessionStorage.getItem('raneen-becoming-session')") is None
    assert page.evaluate("!JSON.stringify({...sessionStorage, ...localStorage}).includes('synthetic-private-capability') && !JSON.stringify({...sessionStorage, ...localStorage}).includes('synthetic-room-token')")
    assert fixture.created == fixture.calls == 1


def main():
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "ELEVENLABS_", "VAPI_", "RANEEN_")) and key != "RANEEN_UI_OUTPUT_DIR":
            os.environ.pop(key, None)
    os.environ["RANEEN_BECOMING_ENABLED"] = "1"
    output = Path(os.environ.get("RANEEN_UI_OUTPUT_DIR", "/tmp/raneen-becoming-ui"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="raneen-becoming-ui-") as data_dir:
        app = create_app(data_dir, public_origin=ORIGIN, owner_only=True)
        with TestClient(app, base_url=ORIGIN) as client, sync_playwright() as playwright:
            options = {"headless": True, "args": ["--no-sandbox"]}
            if os.environ.get("CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
            browser = playwright.chromium.launch(**options)
            errors, fixtures = [], []

            def page_for(*, denied=False, configured=True):
                context = browser.new_context(viewport={"width": 1280, "height": 960})
                context.add_init_script("window.__denyMicrophone = " + json.dumps(denied) + ";")
                context.add_init_script(SYNTHETIC_MEDIA)
                context.add_init_script(SYNTHETIC_RELAY)
                fixture = SyntheticBecoming(client, configured=configured)
                fixtures.append(fixture)
                page = context.new_page()
                page.set_default_timeout(10000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda item: errors.append(item.text) if item.type == "error" and "Content Security Policy" in item.text else None)
                page.on("console", lambda item: fixture.console_errors.append(item.text) if item.type == "error" else None)
                page.on("requestfailed", lambda request: fixture.failed_requests.append({"path": urlsplit(request.url).path, "reason": request.failure}))
                page.on("dialog", lambda dialog: dialog.accept())
                context.route("**/*", fixture.route)
                page.goto(ORIGIN + "/", wait_until="networkidle")
                expect(page.locator("#experience")).to_have_attribute("data-state", "IDLE")
                assert "'unsafe-eval'" not in fixture.headers["content-security-policy"]
                assert "'unsafe-inline'" not in fixture.headers["content-security-policy"]
                return page, fixture

            denied, fixture = page_for(denied=True)
            denied.locator("#consent").check()
            expect(denied.locator("#create")).to_be_enabled()
            denied.locator("#create").click()
            expect(denied.locator("#experience")).to_have_attribute("data-state", "FAILED")
            expect(denied.locator("#notice")).to_be_visible()
            assert fixture.created == fixture.calls == 0
            assert denied.evaluate("window.__syntheticMedia.active") == 0
            assert denied.evaluate("window.__syntheticDaily.created") == 0
            assert denied.evaluate("window.__syntheticRelay.sources.length") == 0
            denied.context.close()

            unavailable, fixture = page_for(configured=False)
            unavailable.locator("#consent").check()
            expect(unavailable.locator("#create")).to_be_disabled()
            expect(unavailable.locator("#notice")).to_be_visible()
            assert fixture.created == fixture.calls == 0
            assert unavailable.evaluate("window.__syntheticMedia.requested") == 0
            unavailable.context.close()

            page, fixture = page_for()
            no_overflow(page)
            page.screenshot(path=str(output / "becoming-desktop-ar.png"), full_page=True)
            page.set_viewport_size({"width": 390, "height": 844})
            no_overflow(page)
            assert page.locator("#create").evaluate("button => button.getBoundingClientRect().bottom <= innerHeight"), "Arabic primary action is below the mobile fold"
            page.screenshot(path=str(output / "becoming-mobile-ar.png"), full_page=True)
            page.locator("#language").click()
            expect(page.locator("html")).to_have_attribute("lang", "en")
            expect(page.locator("html")).to_have_attribute("dir", "ltr")
            no_overflow(page)
            assert page.locator("#create").evaluate("button => button.getBoundingClientRect().bottom <= innerHeight"), "English primary action is below the mobile fold"
            page.screenshot(path=str(output / "becoming-mobile-en.png"), full_page=True)
            # Keep ordinary layout screenshots free of diagnostics, then expose
            # only the safe failure stage/name for bootstrap failure reporting.
            page.goto(ORIGIN + "/?debug=1", wait_until="networkidle")
            page.locator("#language").click()
            start(page, fixture)
            # Actual PCM/worklet/WAV/upload code executes on an oscillator.
            # Provider configuration disables Daily app messages. Only the
            # same-origin sanitized relay can drive speaker capture and labels.
            page.evaluate("window.__emitDaily({type:'speech-update',role:'user',status:'started'})")
            expect(page.locator("#experience")).to_have_attribute("data-speaking", "")
            page.evaluate("window.__emitRelay({type:'speech-update',role:'user',status:'started'})")
            page.wait_for_function("document.querySelector('#progress') !== null")
            with page.expect_response("**/chunks/0", timeout=15000):
                page.wait_for_timeout(4500)
            assert len(fixture.chunks) >= 1
            page.evaluate("window.__emitRelay({type:'speech-update',role:'user',status:'stopped'})")

            fixture.voice_ready = True
            fixture.state = "CLONE_READY"
            expect(page.locator("#experience")).to_have_attribute("data-state", "CLONE_READY")
            expect(page.locator("#status")).not_to_have_text("Raneen is speaking with your voice")
            assert page.evaluate("window.__syntheticMedia.active") == 1
            fixture.state = "SWITCHING_VOICE"
            expect(page.locator("#experience")).to_have_attribute("data-state", "SWITCHING_VOICE")
            page.evaluate("window.__emitRelay({type:'assistant.started',new_assistant_voice:{provider:'11labs',voice_id:'synthetic_voice_fixture'}})")
            page.evaluate("window.__emitRelay({type:'speech-update',role:'assistant',status:'started'})")
            expect(page.locator("#experience")).to_have_attribute("data-state", "SWITCHING_VOICE")
            expect(page.locator("#status")).not_to_have_text("Raneen is speaking with your voice")
            # Only the authoritative mocked server status activates the label.
            fixture.state = "CLONED_ACTIVE"
            expect(page.locator("#experience")).to_have_attribute("data-state", "CLONED_ACTIVE")
            expect(page.locator("#status")).to_have_text("Raneen is speaking with your voice")
            assert page.evaluate("window.__syntheticDaily.created === 1 && window.__syntheticDaily.joined === 1")
            assert fixture.calls == 1
            no_overflow(page)
            page.screenshot(path=str(output / "becoming-active-mobile-en.png"), full_page=True)
            page.locator("#stop").click()
            expect(page.locator("#experience")).to_have_attribute("data-state", "ENDED")
            assert page.evaluate("window.__syntheticMedia.active === 0 && window.__syntheticDaily.destroyed === 1 && window.__syntheticRelay.closed === 1")
            assert fixture.ended == 1
            expect(page.locator("#savedVoice")).to_be_visible()
            expect(page.locator("#voicePreview")).to_have_attribute("src", "/api/becoming/sessions/" + SESSION_ID + "/voice-check")
            page.locator("#voicePreview").evaluate("async audio => { audio.muted = true; await audio.play(); }")
            page.wait_for_function("document.querySelector('#voicePreview').ended")
            # Retained private voice access survives reload without automatically
            # restarting a microphone or creating another provider call.
            page.reload(wait_until="networkidle")
            expect(page.locator("#experience")).to_have_attribute("data-state", "ENDED")
            assert page.evaluate("window.__syntheticMedia.requested === 0")
            assert page.evaluate("window.__syntheticRelay.sources.length === 0")
            assert fixture.created == fixture.calls == 1
            assert page.evaluate("localStorage.getItem('raneen-becoming-session')") == SESSION_ID
            assert page.evaluate("!JSON.stringify({...sessionStorage, ...localStorage}).includes('synthetic-private-capability')")
            page.locator("#revoke").click()
            expect(page.locator("#experience")).to_have_attribute("data-state", "REVOKED")
            assert fixture.revoked == 1
            assert page.evaluate("localStorage.getItem('raneen-becoming-session')") is None
            page.context.close()

            # Revocation while speaking must release the local mic and room too.
            revoked, fixture = page_for()
            start(revoked, fixture)
            revoked.locator("#revoke").click()
            expect(revoked.locator("#experience")).to_have_attribute("data-state", "REVOKED")
            assert revoked.evaluate("window.__syntheticMedia.active === 0 && window.__syntheticDaily.destroyed === 1 && window.__syntheticRelay.closed === 1")
            assert fixture.revoked == 1 and fixture.calls == 1
            revoked.context.close()

            remote, fixture = page_for()
            start(remote, fixture)
            fixture.state = "REVOKED"
            expect(remote.locator("#experience")).to_have_attribute("data-state", "REVOKED")
            assert remote.evaluate("window.__syntheticMedia.active === 0 && window.__syntheticDaily.destroyed === 1 && window.__syntheticRelay.closed === 1")
            assert fixture.revoked == 0, "A provider-confirmed terminal state must not repeat the mutation"
            remote.context.close()

            disconnected, fixture = page_for()
            start(disconnected, fixture)
            disconnected.evaluate("window.__syntheticDaily.handlers['error']({errorMsg:'Synthetic lost connection'})")
            expect(disconnected.locator("#experience")).to_have_attribute("data-state", "ENDED")
            assert disconnected.evaluate("window.__syntheticMedia.active === 0 && window.__syntheticDaily.destroyed === 1 && window.__syntheticRelay.closed === 1")
            assert fixture.ended == 1 and fixture.calls == 1
            disconnected.context.close()

            assert not errors, errors
            assert not any(fixture.errors for fixture in fixtures), [fixture.errors for fixture in fixtures]
            assert not any(fixture.external_requests for fixture in fixtures), [fixture.external_requests for fixture in fixtures]
            assert app.state.store.all("SELECT * FROM becoming_sessions") == [], "This browser fixture must not open real provider sessions"
            browser.close()
    print("Synthetic browser regression passed: real page/CSP/worklet, explicit consent, denied mic, isolated PCM upload, one call through authoritative voice handoff, stop/revoke, private restore and Arabic/English mobile layouts. API, Daily and microphone were synthetic; live cloning and human acceptance were not tested.")


if __name__ == "__main__":
    main()
