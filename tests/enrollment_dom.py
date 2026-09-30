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
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from playwright.sync_api import expect, sync_playwright

from studio.runtime import create_app


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
        // Explicit synthetic bytes: this test does not validate audio decoding.
        this.ondataavailable?.({data: new Blob(['synthetic-only-audio-fixture'.repeat(20)], {type: this.mimeType})});
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


class SyntheticProvider:
    def __init__(self):
        self.opened = []
        self.closed = []

    async def openai_create_call(self, api_key, safety_id, sdp, session):
        assert api_key == "synthetic-openai-key"
        assert safety_id and sdp.startswith("v=0") and session["type"] == "realtime"
        call_id = f"rtc_synthetic_ui_{len(self.opened) + 1}"
        self.opened.append(call_id)
        return {"call_id": call_id, "sdp": "v=0\r\ns=synthetic-answer\r\n"}

    async def openai_hangup(self, api_key, call_id):
        assert api_key == "synthetic-openai-key"
        assert call_id in self.opened
        self.closed.append(call_id)

    def __getattr__(self, name):
        raise AssertionError("Unexpected provider operation: " + name)


def main():
    # Discard inherited integration configuration before building the app.
    for key in list(os.environ):
        if key.startswith(("OPENAI_", "ELEVENLABS_", "VAPI_", "RANEEN_")) and key != "RANEEN_UI_OUTPUT_DIR":
            os.environ.pop(key, None)
    os.environ["RANEEN_VOICE_ENROLLMENT_ENABLED"] = "1"
    os.environ["OPENAI_API_KEY"] = "synthetic-openai-key"
    output = Path(os.environ.get("RANEEN_UI_OUTPUT_DIR", "/tmp/raneen-enrollment-ui"))
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="raneen-ui-") as data_dir:
        app = create_app(data_dir, public_origin=ORIGIN, owner_only=True)
        owner = app.state.store.create_user("Synthetic UI owner", "admin")
        fake = SyntheticProvider()
        app.state.enrollment_provider = fake
        with TestClient(app, base_url=ORIGIN) as client, sync_playwright() as playwright:
            options = {"headless": True, "args": ["--no-sandbox"]}
            if os.environ.get("CHROMIUM_EXECUTABLE"):
                options["executable_path"] = os.environ["CHROMIUM_EXECUTABLE"]
            browser = playwright.chromium.launch(**options)
            context = browser.new_context(viewport={"width": 1280, "height": 960})
            page = context.new_page()
            page.set_default_timeout(10000)
            errors, external_requests, csp_errors = [], [], []
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
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                response = client.request(request.method, path, headers=request.all_headers(), content=request.post_data_buffer)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)

            context.route("**/*", bridge)
            context.add_init_script(FAKE_MEDIA)

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
                expect(page.locator("#uploadState")).to_have_text("All completed audio pieces have been acknowledged as saved.")
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
                pause_saved()
                assert len(fake.opened) == len(fake.closed) == 1
                sessions = app.state.store.all("SELECT * FROM enrollment_sessions")
                assert len(sessions) == 1
                session_id = sessions[0]["id"]
                chunks = app.state.store.all("SELECT seq FROM enrollment_chunks WHERE session_id=? ORDER BY seq", (session_id,))
                assert [row["seq"] for row in chunks] == [0]
                assert page.locator("iframe").count() == 0
                assert_no_overflow()
                page.screenshot(path=str(output / "enrollment-desktop-en.png"), full_page=True)

                page.reload()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                expect(page.locator("html")).to_have_attribute("dir", "ltr")
                sign_in_english()
                expect(page.locator("#mainAction")).to_have_text("Continue my interview")
                page.locator("#mainAction").click()
                expect(page.locator("#connect")).to_be_enabled()
                assert len(fake.opened) == 1, "Restoring a session must not automatically start a call"
                assert page.evaluate("window.__syntheticMedia.active") == 0
                assert page.evaluate("sessionStorage.length === 0 && Object.keys(localStorage).every(key => key === 'raneen-language') && localStorage.getItem('raneen-language') === 'en'"), "Only language preference may persist; owner token must remain memory-only"
                page.locator("#connect").click()
                expect(page.locator("#orb")).to_have_class(re.compile(r"\blive\b"))
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

                os.environ["RANEEN_VOICE_ENROLLMENT_ENABLED"] = "0"
                page.reload()
                expect(page.locator("html")).to_have_attribute("lang", "en")
                sign_in_english()
                expect(page.locator("#mainAction")).to_have_text("View saved interview")
                page.locator("#mainAction").click()
                expect(page.locator("#connect")).to_be_disabled()
                expect(page.locator("#revoke")).to_be_visible()
                page.locator("#revoke").click()
                expect(page.locator("#cleanup")).to_be_visible()
                expect(page.locator("#connectionStatus")).to_have_text("Consent withdrawn")
                assert app.state.store.one("SELECT revoked_at FROM enrollment_sessions WHERE id=?", (session_id,))["revoked_at"]
                assert len(fake.opened) == len(fake.closed) == 2
                assert app.state.store.all("SELECT * FROM enrollment_voice_versions") == []
                assert not errors, errors
                assert not csp_errors, csp_errors
                assert not external_requests, external_requests
                print("PASS: synthetic enrollment DOM/API sign-in, Arabic/English, consent, local microphone check, pause, reload/resume, feature-off withdrawal, mobile overflow and CSP smoke.")
                print("No physical microphone, real provider call, voice clone, or voice-quality acceptance was tested.")
            except Exception:
                page.screenshot(path=str(output / "enrollment-failure.png"), full_page=True)
                raise
            finally:
                browser.close()


if __name__ == "__main__":
    main()
