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
    constructor() { this.connectionState = 'new'; }
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
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
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
                page.locator("#finish").click()
                expect(page.locator("#sample-question")).to_be_visible(timeout=30000)
                expect(page.locator("#approveVoice")).to_be_disabled()
                assert len(fake.clone_calls) == 1
                assert len(fake.speech_calls) == 3
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
                assert not errors, errors
                assert not csp_errors, csp_errors
                assert not external_requests, external_requests
                print("PASS: synthetic owner DOM/API sign-in, Arabic/English, consent, microphone check, one-action interview recovery, trusted evidence, decoded audio, one-action voice preparation, three played fresh samples, explicit approval, bounded frame call, spoken correction with voice reuse, feature-off withdrawal, voice preparation without behavior/Vapi, mobile overflow and CSP.")
                print("Generated tones and private synthetic provider events only: no physical microphone, real provider call, real voice clone, or human voice-quality acceptance was tested.")
            except Exception:
                page.screenshot(path=str(output / "enrollment-failure.png"), full_page=True)
                raise
            finally:
                browser.close()


if __name__ == "__main__":
    main()
