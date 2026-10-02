"""Synthetic browser/API smoke for the actual enrollment page and its CSP.

Run after installing requirements-browser.txt and Playwright Chromium:
    python tests/enrollment_dom.py

Every HTTP request is intercepted and sent to an in-process FastAPI TestClient.
The microphone is synthetic, WebRTC is fake, and the only provider adapter is a
strict fake. This establishes neither physical-microphone nor live-provider proof.
Screenshots go to RANEEN_UI_OUTPUT_DIR, default /tmp/raneen-enrollment-ui.
Set CHROMIUM_EXECUTABLE only when using an already installed browser binary.
"""
from __future__ import annotations

import os
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from studio.runtime import create_app
import studio.enrollment as enrollment
from studio.enrollment_evidence import RealtimeEvidenceBridge


ORIGIN = "https://raneen.test"
FAKE_MEDIA = r"""
(() => {
  const state = window.__syntheticMedia = {requested: 0, active: 0, recorders: []};
  Object.defineProperty(navigator.mediaDevices, 'getUserMedia', {value: async () => {
    state.requested++;
    // A real MediaStream from a synthetic oscillator lets the local meter execute
    // its actual Web Audio path without opening a physical microphone or speaker.
    const context = new AudioContext();
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    gain.gain.value = 0.05;
    const destination = context.createMediaStreamDestination();
    oscillator.connect(gain).connect(destination);
    oscillator.start();
    await context.resume();
    const stream = destination.stream;
    for (const track of stream.getTracks()) {
      state.active++;
      const stop = track.stop.bind(track);
      let stopped = false;
      track.stop = () => {
        if (stopped) return;
        stopped = true;
        state.active--;
        stop();
        oscillator.stop();
        context.close().catch(() => {});
      };
    }
    return stream;
  }});
  window.MediaRecorder = class {
    static isTypeSupported() { return true; }
    constructor(stream, options) {
      this.stream = stream;
      this.mimeType = options.mimeType;
      this.state = 'inactive';
      state.recorders.push(this);
    }
    start() { this.state = 'recording'; }
    stop() {
      if (this.state !== 'recording') return;
      this.state = 'inactive';
      queueMicrotask(() => {
        // Real, decodable Opus in an independent WebM container. The content is
        // a generated tone, never a person's recording or a speech-quality test.
        const bytes = Uint8Array.from(atob(window.__syntheticWebm), c => c.charCodeAt(0));
        this.ondataavailable?.({data: new Blob([bytes], {type: this.mimeType})});
        this.onstop?.();
      });
    }
  };
  window.RTCPeerConnection = class {
    constructor() { this.connectionState = 'new'; state.peer = this; }
    addTrack() {}
    createDataChannel() {
      this.channel = {readyState: 'open', send() {}, close() { this.readyState = 'closed'; }};
      return this.channel;
    }
    async createOffer() { return {type: 'offer', sdp: 'v=0\r\ns=synthetic-ui-test\r\n'}; }
    async setLocalDescription(value) { this.localDescription = value; }
    async setRemoteDescription(value) {
      this.remoteDescription = value;
      this.connectionState = 'connected';
      queueMicrotask(() => this.channel.onopen?.());
    }
    close() { this.connectionState = 'closed'; }
  };
})();
"""

FAKE_DAILY = r"""
// Synthetic SDK boundary only; /vapi-frame, its CSP and message validation are real.
window.__syntheticDaily = {joined: null, left: 0, destroyed: 0};
window.DailyIframe = {createCallObject(options) {
  if (!options.audioSource || options.videoSource !== false || !options.avoidEval) throw Error('Unsafe call options');
  const handlers = {};
  return {
    on(name, fn) { handlers[name] = fn; return this; },
    async join(value) {
      if (value.url !== 'https://raneen.daily.co/synthetic' || value.token !== 'synthetic-room-token') throw Error('Unexpected room');
      window.__syntheticDaily.joined = value;
      handlers['joined-meeting']?.();
    },
    async leave() { window.__syntheticDaily.left++; handlers['left-meeting']?.(); },
    async destroy() { window.__syntheticDaily.destroyed++; },
  };
}};
"""


def synthetic_audio(container: str) -> bytes:
    """Generate private in-memory test tones in the actual provider containers."""
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000:duration=1",
        "-af", "volume=2", "-ac", "1",
    ]
    command += ["-c:a", "libopus", "-b:a", "32000", "-f", "webm"] if container == "webm" else ["-c:a", "libmp3lame", "-b:a", "64000", "-f", "mp3"]
    result = subprocess.run(command + ["pipe:1"], capture_output=True, check=True, timeout=10)
    assert 500 < len(result.stdout) < 80 * 1024
    return result.stdout


class SyntheticProvider:
    def __init__(self):
        self.opened = []
        self.closed = []
        self.interview_close_failures = 0
        self.clone_calls = []
        self.speech_calls = []
        self.assistants = []
        self.preview_calls = []
        self.preview_closed = []
        self.close_failures = 0
        self.deleted_voices = []
        self.deleted_assistants = []
        self.preview_audio = synthetic_audio("mp3")

    async def openai_create_call(self, api_key, safety_id, sdp, session):
        assert api_key == "synthetic-openai-key"
        assert safety_id and sdp.startswith("v=0") and session["type"] == "realtime"
        assert not self.opened or self.opened[-1] in self.closed, "Close the known previous interview before opening a replacement"
        call_id = f"rtc_synthetic_ui_{len(self.opened) + 1}"
        self.opened.append(call_id)
        return {"call_id": call_id, "sdp": "v=0\r\ns=synthetic-answer\r\n"}

    async def openai_hangup(self, api_key, call_id):
        assert api_key == "synthetic-openai-key"
        assert call_id in self.opened
        if self.interview_close_failures:
            self.interview_close_failures -= 1
            raise enrollment.ProviderError("Synthetic unknown interview hangup outcome", uncertain=True)
        self.closed.append(call_id)

    async def eleven_clone(self, api_key, name, files):
        assert api_key == "synthetic-eleven-key"
        assert files and all(Path(item[1]).is_file() for item in files)
        self.clone_calls.append((name, files))
        return {"voice_id": "voice_synthetic_ui_123", "requires_verification": False}

    async def eleven_speech(self, api_key, voice_id, text):
        assert api_key == "synthetic-eleven-key"
        assert voice_id == "voice_synthetic_ui_123"
        assert text in enrollment.PREVIEW_TEXT.values()
        self.speech_calls.append(text)
        return self.preview_audio

    async def eleven_delete_voice(self, api_key, voice_id):
        assert api_key == "synthetic-eleven-key"
        self.deleted_voices.append(voice_id)

    async def vapi_delete_assistant(self, api_key, assistant_id):
        assert api_key == "synthetic-vapi-key"
        self.deleted_assistants.append(assistant_id)

    async def vapi_json(self, api_key, method, path, *, json_body=None):
        assert api_key == "synthetic-vapi-key"
        if method == "GET" and path == "/assistant/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee":
            return {
                "model": {"provider": "openai", "model": "gpt-4.1-mini"},
                "transcriber": {"provider": "speechmatics", "model": "enhanced", "language": "ar_en"},
                "tools": [{"type": "transferCall"}],
                "serverUrl": "https://must-not-copy.invalid",
            }
        if method == "POST" and path == "/assistant":
            assert json_body["voice"]["voiceId"] == "voice_synthetic_ui_123"
            assert json_body["maxDurationSeconds"] == 180
            assert "tools" not in json_body and "serverUrl" not in json_body
            assistant_id = f"11111111-2222-3333-4444-{len(self.assistants) + 1:012d}"
            self.assistants.append((assistant_id, json_body))
            return {"id": assistant_id}
        if method == "POST" and path == "/call":
            assert json_body["assistantId"] == self.assistants[-1][0]
            assert json_body["assistantOverrides"]["maxDurationSeconds"] == 180
            assert "phoneNumberId" not in json_body and "customer" not in json_body
            call_id = f"22222222-3333-4444-5555-{len(self.preview_calls) + 1:012d}"
            self.preview_calls.append((call_id, json_body))
            return {
                "id": call_id,
                "webCallUrl": "https://raneen.daily.co/synthetic",
                "monitor": {"controlUrl": "https://api.vapi.ai/call/" + call_id + "/control"},
                "transport": {"callToken": "synthetic-room-token"},
            }
        raise AssertionError("Unexpected synthetic Vapi request: " + method + " " + path)

    async def vapi_end_call(self, control_url):
        assert control_url.startswith("https://api.vapi.ai/call/")
        assert control_url.endswith("/control")
        if self.close_failures:
            self.close_failures -= 1
            raise enrollment.ProviderError("Synthetic unknown hangup outcome", uncertain=True)
        self.preview_closed.append(control_url)

    def __getattr__(self, name):
        raise AssertionError("Unexpected provider operation: " + name)


class SyntheticSideband:
    """Inject explicitly synthetic provider events at the private socket seam.

    The actual provenance/readback/confirmation parser runs. No browser HTTP text
    upload is promoted into evidence, and no real OpenAI connection is opened.
    """
    def __init__(self, app):
        self.app = app
        self.calls = []
        async def unexpected_failure(*args):
            raise AssertionError("Synthetic sideband failed")
        self.parser = RealtimeEvidenceBridge(app.state.store, app.state.enrollment_evidence, on_failure=unexpected_failure)

    async def attach(self, session_id, call_id, api_key):
        assert api_key == "synthetic-openai-key"
        self.calls.append(call_id)
        if len(self.calls) == 2:
            return  # Plain resume retains the already confirmed first pattern.
        previous = self.app.state.enrollment_evidence.confirmed_rows(session_id)
        correction = len(self.calls) > 2
        interpretation = "Confirm the corrected budget, then ask the preferred area." if correction else "Confirm the corrected budget before continuing."

        def consume(event):
            return self.parser.consume(session_id, call_id, event)

        def spoken(item, transcript):
            consume({"type": "input_audio_buffer.speech_started", "item_id": item})
            consume({"type": "input_audio_buffer.committed", "item_id": item})
            return consume({"type": "conversation.item.input_audio_transcription.completed", "item_id": item, "transcript": transcript})

        spoken("synthetic-demonstration", "Confirm the corrected budget and ask which area they prefer." if correction else "When someone corrects a budget, I repeat the corrected amount.")
        messages = consume({"type": "response.done", "response": {"status": "completed", "output": [{
            "type": "function_call", "call_id": "synthetic-pattern", "name": "propose_evidence",
            "arguments": json.dumps({"kind": "decision_rule", "situation": "A caller corrects their budget.", "interpretation": interpretation, "change_condition": "Ask again if the amount is unclear.", "replaces_id": previous[0]["id"] if correction else ""}),
        }]}})
        challenge = next(item["response"] for item in messages if item["type"] == "response.create")
        readback = json.loads(challenge["instructions"].partition(": ")[2])
        consume({"type": "response.done", "response": {"status": "completed", "metadata": challenge["metadata"], "output": [{"role": "assistant", "content": [{"type": "audio", "transcript": readback}]}]}})
        spoken("synthetic-confirmation", "Yes, save this")
        confirmed = self.app.state.enrollment_evidence.confirmed_rows(session_id)
        assert len(confirmed) == 1
        assert json.loads(confirmed[0]["payload"])["interpretation"] == interpretation

    async def close(self, session_id, call_id=None):
        return None

    async def stop_all(self):
        return None


def main():
    # Discard inherited integration configuration before building the app.
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "ELEVENLABS_", "VAPI_", "RANEEN_")) and key != "RANEEN_UI_OUTPUT_DIR":
            os.environ.pop(key, None)
    os.environ["RANEEN_VOICE_ENROLLMENT_ENABLED"] = "1"
    os.environ["OPENAI_API_KEY"] = "synthetic-openai-key"
    os.environ["ELEVENLABS_API_KEY"] = "synthetic-eleven-key"
    os.environ["VAPI_API_KEY"] = "synthetic-vapi-key"
    os.environ["RANEEN_VAPI_TEMPLATE_ID"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Clearly shortened synthetic developer scenario. This is not long-session
    # reliability or human/native-listener voice acceptance.
    enrollment.MIN_CLONE_MS = 1000
    enrollment.MIN_CLONE_ACTIVE_MS = 500
    capture_audio = synthetic_audio("webm")
    output = Path(os.environ.get("RANEEN_UI_OUTPUT_DIR", "/tmp/raneen-enrollment-ui"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="raneen-ui-") as data_dir:
        app = create_app(data_dir, public_origin=ORIGIN, owner_only=True)
        owner = app.state.store.create_user("Synthetic UI owner", "admin")
        fake = SyntheticProvider()
        app.state.enrollment_provider = fake
        app.state.enrollment_sideband = SyntheticSideband(app)
        with TestClient(app, base_url=ORIGIN) as client, sync_playwright() as playwright:
            options = {"headless": True, "args": ["--no-sandbox"]}
            if os.environ.get("CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1280, "height": 960})
            page = context.new_page()
            page.set_default_timeout(10000)
            errors, external_requests, csp_errors, frame_headers = [], [], [], []
            session_create_requests = []
            clone_requests = []
            preview_requests = []
            faults = {"clone_shortfall": False}
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda item: csp_errors.append(item.text) if item.type == "error" and "Content Security Policy" in item.text else None)
            page.on("dialog", lambda dialog: dialog.accept())

            def bridge(route):
                request = route.request
                parsed = urlsplit(request.url)
                if f"{parsed.scheme}://{parsed.netloc}" != ORIGIN:
                    external_requests.append(request.url)
                    route.abort("blockedbyclient")
                    return
                if parsed.path == "/static/vendor/daily-0.87.0.js":
                    assert "authorization" not in request.all_headers(), "Owner access must not be passed into the call SDK"
                    route.fulfill(status=200, content_type="application/javascript", body=FAKE_DAILY)
                    return
                if request.method == "POST" and parsed.path.endswith("/clone"):
                    clone_requests.append((parsed.path, request.post_data_json))
                if request.method == "POST" and parsed.path.endswith("/preview"):
                    preview_requests.append((parsed.path, request.post_data_json))
                if request.method == "POST" and parsed.path.endswith("/clone") and faults["clone_shortfall"]:
                    faults["clone_shortfall"] = False
                    route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": {
                        "code": "insufficient_audio", "message": "synthetic audible shortfall",
                        "details": {"selected_duration_ms": 49000, "minimum_sample_ms": 60000,
                                    "selected_active_ms": 35000, "minimum_active_ms": 30000,
                                    "insufficiency": "sample_duration", "decoded_source_ms": 123000,
                                    "rejected_reasons": {}},
                    }}))
                    return
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                if request.method == "POST" and parsed.path == "/api/enrollment/sessions":
                    session_create_requests.append(request.url)
                response = client.request(request.method, path, headers=request.all_headers(), content=request.post_data_buffer)
                if parsed.path == "/vapi-frame":
                    assert "authorization" not in request.all_headers(), "Owner access must not be passed into the call frame"
                    frame_headers.append(dict(response.headers))
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            context.route("**/*", bridge)
            context.add_init_script("window.__syntheticWebm = " + repr(base64.b64encode(capture_audio).decode()) + ";\n" + FAKE_MEDIA)

            def sign_in_english():
                expect(page.locator("#loginForm")).to_be_visible()
                if page.locator("html").get_attribute("lang") == "ar":
                    page.locator("#language").click()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                expect(page.locator("html")).to_have_attribute("dir", "ltr")
                page.locator("#token").fill(owner["token"])
                page.locator("#login").click()
                expect(page.locator("#mainAction")).to_be_visible()

            def assert_no_overflow():
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "Horizontal overflow in enrollment UI"

            def pause_saved():
                page.locator("#pause").click()
                page.wait_for_function("window.__syntheticMedia.active === 0")
                expect(page.locator("#uploadState")).to_have_text("Audio saved.")
                expect(page.locator("#connect")).to_be_enabled()
                expect(page.locator("#pause")).to_be_hidden()

            try:
                response = page.goto(ORIGIN + "/enroll")
                assert response.status == 200
                assert "script-src 'self'" in response.headers.get("content-security-policy", "")
                expect(page.locator("html")).to_have_attribute("lang", "ar")
                expect(page.locator("html")).to_have_attribute("dir", "rtl")
                sign_in_english()
                page.screenshot(path=str(output / "conversation-home-desktop-en.png"), full_page=True)
                page.locator("#navSettings").click()
                expect(page.locator("#workspaceDialog")).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.locator("#workspaceDialog")).to_have_count(0)
                page.locator("#navHistory").click()
                expect(page.locator("#workspaceDialog")).to_contain_text("A fresh start.")
                page.locator("#closeDialog").click()
                page.set_viewport_size({"width": 390, "height": 844})
                assert_no_overflow()
                page.screenshot(path=str(output / "conversation-home-mobile-en.png"), full_page=True)
                page.set_viewport_size({"width": 1280, "height": 960})
                page.locator("#mainAction").click()
                expect(page.locator("#consentForm")).to_be_visible()
                for selector in ("#own", "#record", "#external", "#clone", "#preview"):
                    page.locator(selector).check()
                page.locator("#accept").click()
                expect(page.locator("#checkMic")).to_be_visible()
                page.locator("#checkMic").click()
                expect(page.locator("#micContinue")).to_be_visible()
                assert page.evaluate("window.__syntheticMedia.active") == 1
                page.locator("#micContinue").click()
                expect(page.locator("#connect")).to_be_enabled()
                assert page.evaluate("window.__syntheticMedia.active") == 0
                assert app.state.store.all("SELECT * FROM enrollment_chunks") == [], "Microphone check must not upload audio"
                assert fake.opened == [], "Microphone check must not start a provider call"

                page.locator("#connect").click()
                expect(page.locator("#orb")).to_have_class(re.compile(r"\blive\b"))
                page.evaluate("""() => {
                  const send = event => window.__syntheticMedia.peer.channel.onmessage({data:JSON.stringify(event)});
                  send({type:'conversation.item.input_audio_transcription.delta', item_id:'display-only', delta:'Ask about '});
                  send({type:'conversation.item.input_audio_transcription.completed', item_id:'display-only', transcript:'Ask about their priorities. <script>alert(1)</script>'});
                  send({type:'conversation.item.input_audio_transcription.completed', item_id:'display-only', transcript:'duplicate final'});
                  send({type:'response.output_audio_transcript.done', item_id:'assistant-display', transcript:'What would you ask first?'});
                  send({type:'output_audio_buffer.started'});
                }""")
                expect(page.locator("#orb")).to_have_attribute("data-speaker", "raneen")
                expect(page.locator(".transcript-turn.trainer")).to_have_count(1)
                expect(page.locator(".transcript-turn.trainer")).to_contain_text("Ask about their priorities.")
                assert page.locator("#conversationFeed script").count() == 0
                page.evaluate("window.__syntheticMedia.peer.channel.onmessage({data:JSON.stringify({type:'input_audio_buffer.speech_started'})})")
                expect(page.locator("#orb")).to_have_attribute("data-speaker", "trainer")
                page.wait_for_function("window.__syntheticMedia.recorders.some(r => r.state === 'recording')")
                fake.interview_close_failures = 1
                with page.expect_response("**/webrtc-close") as closing_interview:
                    pause_saved()
                assert closing_interview.value.status == 502
                assert len(fake.opened) == 1 and not fake.closed
                sessions = app.state.store.all("SELECT * FROM enrollment_sessions")
                assert len(sessions) == 1
                session_id = sessions[0]["id"]
                assert app.state.store.one("SELECT state FROM enrollment_realtime_calls WHERE session_id=?", (session_id,))["state"] == "close_unknown"
                chunks = app.state.store.all("SELECT seq FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,))
                assert [row["seq"] for row in chunks] == [0]
                assert page.locator("iframe").count() == 0
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-desktop-en.png"), full_page=True)

                page.reload()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                expect(page.locator("html")).to_have_attribute("dir", "ltr")
                sign_in_english()
                expect(page.locator("#mainAction")).to_have_text("Continue")
                page.locator("#mainAction").click()
                expect(page.locator("#connect")).to_be_enabled()
                assert len(fake.opened) == 1, "Restoring a session must not automatically start a call"
                assert page.evaluate("window.__syntheticMedia.active") == 0
                assert page.evaluate("sessionStorage.length === 0 && Object.keys(localStorage).every(key => key === 'raneen-language') && localStorage.getItem('raneen-language') === 'en'"), "Only language preference may persist; owner token must remain memory-only"
                # One primary action recovers a known unresolved server hangup.
                # The contributor never has to expand the technical controls.
                expect(page.locator("#endPrevious")).to_be_hidden()
                page.locator("#connect").click()
                expect(page.locator("#orb")).to_have_class(re.compile(r"\blive\b"))
                assert fake.closed == [fake.opened[0]], "Continue must close the prior interview before starting a new call"
                pause_saved()
                chunks = app.state.store.all("SELECT seq FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,))
                assert [row["seq"] for row in chunks] == [0, 1], "Reload/resume must continue the server sequence"

                page.locator("#language").click()
                expect(page.locator("html")).to_have_attribute("dir", "rtl")
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-desktop-ar.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-mobile-ar.png"), full_page=True)
                page.locator("#language").click()
                expect(page.locator("html")).to_have_attribute("dir", "ltr")
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-mobile-en.png"), full_page=True)

                # Complete the real preparation UI with synthetic provider audio.
                # Playback is muted; real HTML audio decoding/ended events run.
                saved_before = app.state.store.all("SELECT seq,sha256,byte_count FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,))
                faults["clone_shortfall"] = True
                page.locator("#finish").click()
                shortfall = page.locator(".preparation-card .status-note[role=status]")
                expect(shortfall).to_contain_text("Your recording is saved.")
                expect(shortfall).to_contain_text("Selected sample")
                expect(shortfall).to_contain_text("49s")
                expect(shortfall).to_contain_text("60s needed")
                expect(page.locator("#prepareAgent")).to_be_enabled()
                assert faults["clone_shortfall"] is False
                assert not fake.clone_calls and not fake.speech_calls
                assert app.state.store.all("SELECT seq,sha256,byte_count FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,)) == saved_before
                page.screenshot(path=str(output / "enrollment-audio-shortfall-en.png"), full_page=True)
                page.locator("#prepareAgent").click()
                expect(page.locator("#sample-question")).to_be_visible(timeout=30000)
                expect(page.locator("#approveVoice")).to_be_disabled()
                assert len(fake.clone_calls) == 1
                assert len(fake.speech_calls) == 3
                page.locator("#deferVoice").click()
                expect(page.locator("#connect")).to_be_visible()
                assert app.state.store.one("SELECT COUNT(*) AS n FROM enrollment_voice_approvals")["n"] == 0
                assert page.evaluate("window.__syntheticMedia.active") == 0
                page.locator("#finish").click()
                expect(page.locator("#sample-question")).to_be_visible(timeout=30000)
                assert len(fake.clone_calls) == 1, "Deferring a voice must not create another clone"
                for index, kind in enumerate(("question", "number", "correction")):
                    sample = page.locator("#sample-" + kind)
                    sample.evaluate("async audio => { audio.muted = true; await audio.play(); }")
                    expect(page.locator("#heard-" + kind)).to_have_text("Listened")
                    if index < 2:
                        expect(page.locator("#approveVoice")).to_be_disabled()
                expect(page.locator("#approveVoice")).to_be_enabled()
                page.screenshot(path=str(output / "enrollment-voice-review-en.png"), full_page=True)
                page.locator("#approveVoice").click()
                expect(page.locator("#startTest")).to_be_enabled()
                assert len(fake.assistants) == 1
                assert app.state.store.one("SELECT COUNT(*) AS n FROM enrollment_voice_approvals")["n"] == 1

                # The app owns a bounded server-created room. Only the SDK itself
                # is synthetic; the frame, CSP, nonce and parent event path run.
                page.locator("#startTest").click()
                expect(page.locator("#agentFrame")).to_be_visible()
                expect(page.locator("#callStatus")).to_have_text("Your agent is listening")
                expect(page.frame_locator("#agentFrame").locator("#status")).to_have_text("You’re connected. Speak naturally.")
                assert "frame-ancestors 'self'" in frame_headers[-1]["content-security-policy"]
                assert "'unsafe-eval'" not in frame_headers[-1]["content-security-policy"]
                assert "unpkg.com" not in frame_headers[-1]["content-security-policy"]
                assert len(fake.preview_calls) == 1
                page.screenshot(path=str(output / "enrollment-agent-call-en.png"), full_page=True)
                fake.close_failures = 1
                with page.expect_response("**/preview-call/close") as closing:
                    page.locator("#stopTest").click()
                assert closing.value.json()["state"] == "close_unknown"
                expect(page.locator("#agentFrame")).to_have_count(0)
                expect(page.locator("#startTest")).to_be_disabled()
                expect(page.locator("#stopTest")).to_be_visible()
                assert fake.preview_closed == [], "Unknown hangup must not be described as completed"
                # A reload must retain the server's unresolved call and offer a
                # close retry instead of allowing a second paid call.
                page.reload()
                sign_in_english()
                page.locator("#mainAction").click()
                expect(page.locator("#startTest")).to_be_disabled()
                expect(page.locator("#stopTest")).to_be_visible()
                page.locator("#stopTest").click()
                expect(page.locator("#startTest")).to_be_enabled()
                assert len(fake.preview_closed) == 1

                # Teach and confirm a replacement through the synthetic private
                # provider socket, then rebuild behavior without recloning.
                page.locator("#teachCorrection").click()
                expect(page.locator("#connect")).to_be_enabled()
                page.locator("#connect").click()
                expect(page.locator("#orb")).to_have_class(re.compile(r"\blive\b"))
                pause_saved()
                page.locator("#finish").click()
                expect(page.locator("#startTest")).to_be_enabled(timeout=30000)
                assert len(fake.clone_calls) == 1, "A spoken response correction must reuse the approved voice"
                assert len(fake.speech_calls) == 3, "A behavior-only correction must not regenerate the voice samples"
                assert len(fake.assistants) == 2
                versions = app.state.store.all("SELECT version,payload FROM enrollment_behavior_versions WHERE session_id=? ORDER BY version", (session_id,))
                assert [row["version"] for row in versions] == [1, 2]
                assert len(json.loads(versions[-1]["payload"])["evidence"]) == 1
                assert app.state.store.one("SELECT COUNT(*) AS n FROM enrollment_evidence WHERE session_id=? AND status='superseded'", (session_id,))["n"] == 1
                page.locator("#startTest").click()
                expect(page.locator("#callStatus")).to_have_text("Your agent is listening")
                assert fake.preview_calls[-1][1]["assistantId"] == fake.assistants[-1][0]
                page.locator("#stopTest").click()
                expect(page.locator("#agentFrame")).to_have_count(0)
                expect(page.locator("#startTest")).to_be_enabled()

                page.set_viewport_size({"width": 1280, "height": 960})
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-agent-desktop-en.png"), full_page=True)
                page.locator("#language").click()
                expect(page.locator("html")).to_have_attribute("dir", "rtl")
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-agent-desktop-ar.png"), full_page=True)
                page.set_viewport_size({"width": 390, "height": 844})
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-agent-mobile-ar.png"), full_page=True)
                page.locator("#language").click()
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-agent-mobile-en.png"), full_page=True)

                os.environ["RANEEN_VOICE_ENROLLMENT_ENABLED"] = "0"
                page.reload()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                sign_in_english()
                expect(page.locator("#mainAction")).to_have_text("View")
                page.locator("#mainAction").click()
                expect(page.locator("#startTest")).to_be_disabled()
                expect(page.locator("#revoke")).to_be_visible()
                page.locator("#revoke").click()
                expect(page.locator("#cleanup")).to_be_visible()
                expect(page.locator("#connectionStatus")).to_have_text("Consent withdrawn")
                assert app.state.store.one("SELECT revoked_at FROM enrollment_sessions WHERE id=?", (session_id,))["revoked_at"]
                assert len(fake.opened) == len(fake.closed) == 3
                assert len(fake.preview_calls) == len(fake.preview_closed) == 2
                assert len(app.state.store.all("SELECT * FROM enrollment_voice_versions")) == 1
                assert fake.deleted_voices == ["voice_synthetic_ui_123"]
                assert set(fake.deleted_assistants) == {item[0] for item in fake.assistants}

                # A saved recording can produce the first voice samples before
                # any response pattern is confirmed or Vapi is configured.
                # Seed only acknowledged synthetic audio through the private API.
                os.environ["RANEEN_VOICE_ENROLLMENT_ENABLED"] = "1"
                os.environ.pop("VAPI_API_KEY")
                os.environ.pop("RANEEN_VAPI_TEMPLATE_ID")
                voice_only = SyntheticProvider()
                app.state.enrollment_provider = voice_only
                owner_headers = {"Authorization": "Bearer " + owner["token"], "Origin": ORIGIN}
                created = client.post("/api/enrollment/sessions", headers=owner_headers, json={
                    "self_attestation": True, "recording": True, "external_processing": True,
                    "voice_cloning": True, "private_preview": True,
                })
                assert created.status_code == 201
                voice_session = created.json()["id"]
                uploaded = client.put("/api/enrollment/sessions/" + voice_session + "/chunks/0", content=capture_audio, headers=owner_headers | {
                    "Content-Type": "audio/webm", "X-Speaker-Role": "contributor",
                    "X-Chunk-Sha256": hashlib.sha256(capture_audio).hexdigest(), "X-Duration-Ms": "1000",
                })
                assert uploaded.status_code == 200
                assert app.state.enrollment_evidence.confirmed_rows(voice_session) == []
                page.reload()
                sign_in_english()
                page.locator("#mainAction").click()
                page.locator("#finish").click()
                expect(page.locator("#sample-question")).to_be_visible(timeout=30000)
                expect(page.locator("#sample-number")).to_be_visible()
                expect(page.locator("#sample-correction")).to_be_visible()
                expect(page.locator("#approveVoice")).to_be_disabled()
                assert len(voice_only.clone_calls) == 1 and len(voice_only.speech_calls) == 3
                assert not voice_only.assistants and not voice_only.preview_calls
                assert app.state.enrollment_evidence.confirmed_rows(voice_session) == []
                page.screenshot(path=str(output / "enrollment-voice-only-en.png"), full_page=True)

                # An exhausted, already closed interview offers fresh consent,
                # not a disabled Continue button or an implicit new recording.
                stamp = enrollment.now()
                app.state.store.execute("INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)", (
                    "synthetic-exhausted-operation", voice_session, "realtime_call", "synthetic-exhausted-call",
                    "succeeded", "rtc_synthetic_exhausted", json.dumps({"consumed_seconds": 1800}), stamp, stamp,
                ))
                app.state.store.execute("INSERT INTO enrollment_realtime_calls VALUES(?,?,?,?,?)", (
                    voice_session, "rtc_synthetic_exhausted", "closed", stamp, stamp,
                ))
                journey = client.get("/api/enrollment/sessions/" + voice_session + "/journey", headers=owner_headers).json()
                assert journey["resume_limit"] == "time" and journey["can_resume"] is False
                page.locator("#returnInterview").click()
                expect(page.locator("#newInterview")).to_be_visible()
                expect(page.locator("#newInterview")).to_be_enabled()
                expect(page.locator("#connect")).to_be_hidden()
                sessions_before = app.state.store.one("SELECT COUNT(*) AS n FROM enrollment_sessions")["n"]
                requests_before = len(session_create_requests)
                microphones_before = page.evaluate("window.__syntheticMedia.requested")
                provider_before = (len(voice_only.opened), len(voice_only.closed), len(voice_only.clone_calls), len(voice_only.speech_calls), len(voice_only.assistants), len(voice_only.preview_calls))
                page.locator("#newInterview").click()
                expect(page.locator("#consentForm")).to_be_visible()
                expect(page.locator("#consentForm input[type=checkbox]")).to_have_count(5)
                for selector in ("#own", "#record", "#external", "#clone", "#preview"):
                    expect(page.locator(selector)).not_to_be_checked()
                assert len(session_create_requests) == requests_before
                assert app.state.store.one("SELECT COUNT(*) AS n FROM enrollment_sessions")["n"] == sessions_before
                assert page.evaluate("window.__syntheticMedia.requested") == microphones_before
                assert page.evaluate("window.__syntheticMedia.active") == 0
                assert provider_before == (len(voice_only.opened), len(voice_only.closed), len(voice_only.clone_calls), len(voice_only.speech_calls), len(voice_only.assistants), len(voice_only.preview_calls))
                page.screenshot(path=str(output / "enrollment-fresh-consent-en.png"), full_page=True)

                # A persisted, definitively rejected legacy clone remains visible
                # after reload. Reading progress must never repeat a paid create.
                # The old manifest deliberately differs from today's selector;
                # explicit retry still preserves its failed audit and saved audio.
                recovery = SyntheticProvider()
                original_speech = recovery.eleven_speech
                speech_attempts = []

                async def reject_first_question(api_key, voice_id, text):
                    speech_attempts.append(text)
                    if len(speech_attempts) == 1:
                        assert text == enrollment.PREVIEW_TEXT["question"]
                        raise enrollment.ProviderError("safe synthetic rejection", diagnostics={
                            "http_status": 401, "provider_code": "invalid_api_key",
                        })
                    return await original_speech(api_key, voice_id, text)

                recovery.eleven_speech = reject_first_question
                app.state.enrollment_provider = recovery
                created = client.post("/api/enrollment/sessions", headers=owner_headers, json={
                    "self_attestation": True, "recording": True, "external_processing": True,
                    "voice_cloning": True, "private_preview": True,
                })
                assert created.status_code == 201
                failed_session = created.json()["id"]
                uploaded = client.put("/api/enrollment/sessions/" + failed_session + "/chunks/0", content=capture_audio, headers=owner_headers | {
                    "Content-Type": "audio/webm", "X-Speaker-Role": "contributor",
                    "X-Chunk-Sha256": hashlib.sha256(capture_audio).hexdigest(), "X-Duration-Ms": "1000",
                })
                assert uploaded.status_code == 200
                legacy_manifest = json.dumps({"algorithm": "synthetic_legacy_selection", "total_ms": 1000})
                legacy_digest = hashlib.sha256(legacy_manifest.encode()).hexdigest()
                failed_version = "synthetic-legacy-failed-version"
                failed_operation = "synthetic-legacy-failed-operation"
                stamp = enrollment.now()
                app.state.store.execute("INSERT INTO enrollment_voice_versions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    failed_version, failed_session, 1, "elevenlabs", None, "failed",
                    legacy_manifest, legacy_digest, stamp, stamp,
                ))
                app.state.store.execute("INSERT INTO enrollment_operations VALUES(?,?,?,?,?,?,?,?,?)", (
                    failed_operation, failed_session, "voice_clone", "ivc-v1:" + legacy_digest,
                    "failed", None, json.dumps({"error": "ElevenLabs clone returned HTTP 401.",
                                               "voice_version_id": failed_version, "manifest_digest": legacy_digest}), stamp, stamp,
                ))
                app.state.store.execute("UPDATE enrollment_sessions SET voice_state='failed',updated=? WHERE id=?", (stamp, failed_session))
                saved_recovery_audio = app.state.store.all("SELECT seq,sha256,byte_count FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (failed_session,))
                prior_request_count = len(clone_requests)

                def open_failed_interview(expected_clone_count=0, expected_preview_count=0):
                    page.reload()
                    sign_in_english()
                    page.locator("#navHistory").click()
                    expect(page.locator("#workspaceDialog")).to_be_visible()
                    page.locator('#workspaceDialog [data-session="' + failed_session + '"]').click()
                    note = page.locator(".preparation-card .status-note[role=status]")
                    expect(note).to_contain_text("ElevenLabs did not accept the API key.")
                    expect(note).to_contain_text("your recording is saved.")
                    expect(page.locator("#prepareAgent")).to_have_text("Try again")
                    expect(page.locator("#prepareAgent")).to_be_enabled()
                    expect(page.locator("#voiceServiceStatus")).to_have_text("ElevenLabs · HTTP 401")
                    expect(page.locator(".connection-list li").filter(has_text="Voice").locator(".pill")).to_have_text("Needs attention")
                    assert page.locator("#finish").count() == 0, "Persisted voice failure must restore the preparation screen"
                    assert page.evaluate("window.__syntheticMedia.active") == 0
                    assert len(clone_requests) == prior_request_count + expected_clone_count
                    assert len(recovery.clone_calls) == expected_clone_count and not recovery.speech_calls
                    assert len([item for item in preview_requests if item[0] == "/api/enrollment/sessions/" + failed_session + "/preview"]) == expected_preview_count
                    assert len(speech_attempts) == expected_preview_count

                open_failed_interview()
                with page.expect_response("**/" + failed_session + "/workflow") as checked_progress:
                    page.locator("#refreshWorkflow").click()
                assert checked_progress.value.status == 200
                expect(page.locator(".preparation-card .status-note[role=status]")).to_contain_text("ElevenLabs did not accept the API key.")
                assert len(clone_requests) == prior_request_count
                assert not recovery.clone_calls and not recovery.speech_calls
                open_failed_interview()
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-provider-key-rejected-en.png"), full_page=True)
                with page.expect_request("**/" + failed_session + "/clone") as retry_request, page.expect_response("**/" + failed_session + "/preview") as rejected_preview:
                    page.locator("#prepareAgent").click()
                assert retry_request.value.post_data_json == {"approve": True, "final_seq": 0, "retry_failed": True}
                assert rejected_preview.value.status == 502
                rejected_detail = rejected_preview.value.json()["detail"]
                assert rejected_detail["code"] == "voice_preview_failed" and rejected_detail["details"]["http_status"] == 401
                expect(page.locator("#prepareAgent")).to_have_text("Try again")
                expect(page.locator(".preparation-card .status-note[role=status]")).to_contain_text("ElevenLabs did not accept the API key.")
                assert len(recovery.clone_calls) == 1 and not recovery.speech_calls
                assert page.locator("#sample-question").count() == 0
                failure_workflow = client.get("/api/enrollment/sessions/" + failed_session + "/workflow", headers=owner_headers).json()
                assert failure_workflow["voice_failure"]["code"] == "voice_preview_failed"
                assert failure_workflow["preview_retry_allowed"] == {"question": True, "number": False, "correction": False}
                failed_preview_operation = app.state.store.one("SELECT id,state FROM enrollment_operations WHERE session_id=? AND kind='voice_preview'", (failed_session,))
                assert failed_preview_operation["state"] == "failed"
                open_failed_interview(expected_clone_count=1, expected_preview_count=1)
                with page.expect_response("**/" + failed_session + "/workflow") as checked_preview_progress:
                    page.locator("#refreshWorkflow").click()
                assert checked_preview_progress.value.json()["voice_failure"]["code"] == "voice_preview_failed"
                expect(page.locator("#prepareAgent")).to_have_text("Try again")
                assert len(recovery.clone_calls) == 1 and not recovery.speech_calls and len(speech_attempts) == 1
                open_failed_interview(expected_clone_count=1, expected_preview_count=1)
                page.screenshot(path=str(output / "enrollment-preview-key-rejected-en.png"), full_page=True)
                # Retry synthesis using the already-created voice; only the
                # definitively rejected question receives an explicit retry flag.
                with page.expect_request("**/" + failed_session + "/clone") as reused_clone, page.expect_request("**/" + failed_session + "/preview") as retried_question:
                    page.locator("#prepareAgent").click()
                assert reused_clone.value.post_data_json == {"approve": True, "final_seq": 0}
                assert retried_question.value.post_data_json == {"approve": True, "kind": "question", "retry_failed": True}
                expect(page.locator("#sample-question")).to_be_visible(timeout=30000)
                assert len(clone_requests) == prior_request_count + 2
                assert len(recovery.clone_calls) == 1 and len(recovery.speech_calls) == 3
                recovery_previews = [body for path, body in preview_requests if path == "/api/enrollment/sessions/" + failed_session + "/preview"]
                assert recovery_previews == [
                    {"approve": True, "kind": "question"},
                    {"approve": True, "kind": "question", "retry_failed": True},
                    {"approve": True, "kind": "number"},
                    {"approve": True, "kind": "correction"},
                ]
                assert not recovery.assistants and not recovery.preview_calls
                for kind in ("question", "number", "correction"):
                    page.locator("#sample-" + kind).evaluate("async audio => { audio.muted = true; await audio.play(); }")
                    expect(page.locator("#heard-" + kind)).to_have_text("Listened")
                expect(page.locator("#approveVoice")).to_be_enabled()
                assert app.state.store.all("SELECT seq,sha256,byte_count FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (failed_session,)) == saved_recovery_audio
                legacy_operation = app.state.store.one("SELECT state,provider_id,detail FROM enrollment_operations WHERE id=?", (failed_operation,))
                assert legacy_operation["state"] == "failed" and legacy_operation["provider_id"] is None
                assert json.loads(legacy_operation["detail"])["error"] == "ElevenLabs clone returned HTTP 401."
                assert app.state.store.one("SELECT state,provider_voice_id FROM enrollment_voice_versions WHERE id=?", (failed_version,)) == {"state": "failed", "provider_voice_id": None}
                assert len(app.state.store.all("SELECT id FROM enrollment_operations WHERE session_id=? AND kind='voice_clone'", (failed_session,))) == 2
                assert app.state.store.one("SELECT state FROM enrollment_operations WHERE id=?", (failed_preview_operation["id"],))["state"] == "failed"
                assert len(app.state.store.all("SELECT id FROM enrollment_operations WHERE session_id=? AND kind='voice_preview'", (failed_session,))) == 4
                page.screenshot(path=str(output / "enrollment-provider-retry-voice-review-en.png"), full_page=True)
                assert not errors, errors
                assert not csp_errors, csp_errors
                assert not external_requests, external_requests
                print("PASS: synthetic owner DOM/API sign-in, Arabic/English, consent, microphone check, one-action interview recovery, trusted evidence, decoded audio, one-action voice preparation, three played fresh samples, explicit approval, bounded frame call, spoken correction with voice reuse, feature-off withdrawal, voice preparation without behavior/Vapi, exhausted-interview fresh consent, persisted clone/preview rejection with explicit retry and preserved audio/audit, mobile overflow and CSP.")
                print("Generated tones and private synthetic provider events only: no physical microphone, real provider call, real voice clone, or human voice-quality acceptance was tested.")
            except Exception:
                page.screenshot(path=str(output / "enrollment-failure.png"), full_page=True)
                raise
            finally:
                browser.close()


if __name__ == "__main__":
    main()
