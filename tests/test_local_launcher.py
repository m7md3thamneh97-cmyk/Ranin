"""The documented local entrypoint must serve the composed, disabled platform."""
import sys

from fastapi.testclient import TestClient

import run


def test_local_launcher_serves_enrollment_without_enabling_providers(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("RANEEN_VOICE_ENROLLMENT_ENABLED", raising=False)
    launched = {}
    monkeypatch.setattr(sys, "argv", ["run.py", "--data-dir", str(tmp_path), "--port", "9876"])
    monkeypatch.setattr(run.uvicorn, "run", lambda app, **settings: launched.update(app=app, settings=settings))

    run.main()
    output = capsys.readouterr().out
    assert "http://localhost:9876/enroll" in output
    assert launched["settings"]["host"] == "127.0.0.1"
    assert launched["settings"]["port"] == 9876

    app = launched["app"]
    owner = app.state.store.create_user("Synthetic local reviewer", "admin")
    client = TestClient(app)
    assert client.get("/enroll").status_code == 200
    status = client.get("/api/enrollment/status", headers={"Authorization": "Bearer " + owner["token"]})
    assert status.status_code == 200
    assert status.json()["enabled"] is False
