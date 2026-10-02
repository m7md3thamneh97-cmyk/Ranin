from fastapi.testclient import TestClient

from studio.app import create_app
from studio.knowledge import install


def client_for(tmp_path):
    app = create_app(data_dir=tmp_path)
    install(app)
    owner = app.state.store.create_user("Owner", "admin")
    contributor = app.state.store.create_user("Contributor", "contributor")
    client = TestClient(app)
    return app, client, owner, contributor


def auth(user):
    return {"Authorization": "Bearer " + user["token"]}


def register_doc(client, owner, external_id, version_key, name="Project X.pdf"):
    response = client.post(
        "/api/knowledge/documents",
        headers=auth(owner),
        json={
            "source_type": "google_drive",
            "external_id": external_id,
            "version_key": version_key,
            "name": name,
            "source_uri": "https://drive.google.com/file/d/" + external_id,
            "mime_type": "application/pdf",
            "size_bytes": 12345,
            "metadata": {"folder": "Raneen Source Data"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_knowledge_is_owner_only_and_status_is_empty(tmp_path):
    _, client, owner, contributor = client_for(tmp_path)
    denied = client.get("/api/knowledge/status", headers=auth(contributor))
    assert denied.status_code == 403
    status = client.get("/api/knowledge/status", headers=auth(owner))
    assert status.status_code == 200
    assert status.json()["raw_file_storage"] == "external"
    assert status.json()["counts"]["documents"] == 0


def test_document_version_is_idempotent_and_new_version_supersedes_old_facts(tmp_path):
    _, client, owner, _ = client_for(tmp_path)
    first = register_doc(client, owner, "drive-file-1", "2026-09-01T10:00:00Z")
    again = register_doc(client, owner, "drive-file-1", "2026-09-01T10:00:00Z")
    assert again["idempotent"] is True
    assert again["document_version_id"] == first["document_version_id"]

    conflicting_retry = client.post(
        "/api/knowledge/documents",
        headers=auth(owner),
        json={
            "source_type": "google_drive",
            "external_id": "drive-file-1",
            "version_key": "2026-09-01T10:00:00Z",
            "name": "Project X.pdf",
            "source_uri": "https://drive.google.com/file/d/drive-file-1",
            "mime_type": "application/pdf",
            "size_bytes": 99999,
            "metadata": {"folder": "Raneen Source Data"},
        },
    )
    assert conflicting_retry.status_code == 409

    entity = client.post(
        "/api/knowledge/entities",
        headers=auth(owner),
        json={
            "entity_type": "project",
            "canonical_name": "Project X",
            "aliases": ["X Residence"],
        },
    ).json()
    unit = client.post(
        "/api/knowledge/units",
        headers=auth(owner),
        json={
            "document_version_id": first["document_version_id"],
            "unit_index": 17,
            "unit_type": "page",
            "label": "Payment plan",
            "text_content": "Payment plan 20/40/40",
            "visual_summary": "Three-stage payment plan graphic.",
        },
    ).json()
    fact = client.post(
        "/api/knowledge/facts",
        headers=auth(owner),
        json={
            "entity_id": entity["id"],
            "field_key": "payment_plan",
            "value": {"booking": 20, "construction": 40, "handover": 40},
            "normalized_value": "20/40/40",
            "document_version_id": first["document_version_id"],
            "unit_id": unit["id"],
            "authority_rank": 90,
            "confidence": 0.99,
        },
    ).json()
    current = client.get(
        f"/api/knowledge/entities/{entity['id']}/facts/payment_plan",
        headers=auth(owner),
    ).json()
    assert current["selected"]["id"] == fact["id"]
    assert current["selected"]["provenance"]["unit"]["unit_index"] == 17

    second = register_doc(client, owner, "drive-file-1", "2026-10-01T10:00:00Z")
    assert second["document_version_id"] != first["document_version_id"]
    no_current = client.get(
        f"/api/knowledge/entities/{entity['id']}/facts/payment_plan",
        headers=auth(owner),
    ).json()
    assert no_current["selected"] is None

    old_write = client.post(
        "/api/knowledge/chunks",
        headers=auth(owner),
        json={
            "document_version_id": first["document_version_id"],
            "chunk_index": 0,
            "text_content": "stale text",
        },
    )
    assert old_write.status_code == 409


def test_conflicting_equal_authority_facts_fail_closed(tmp_path):
    _, client, owner, _ = client_for(tmp_path)
    one = register_doc(client, owner, "brochure", "v1", "Project X brochure.pdf")
    two = register_doc(client, owner, "sales-deck", "v1", "Project X sales deck.pptx")
    entity = client.post(
        "/api/knowledge/entities",
        headers=auth(owner),
        json={"entity_type": "project", "canonical_name": "Project X"},
    ).json()
    for version, value in (
        (one["document_version_id"], "Q4 2028"),
        (two["document_version_id"], "Q1 2029"),
    ):
        response = client.post(
            "/api/knowledge/facts",
            headers=auth(owner),
            json={
                "entity_id": entity["id"],
                "field_key": "handover",
                "value": value,
                "document_version_id": version,
                "authority_rank": 90,
                "confidence": 0.98,
            },
        )
        assert response.status_code == 201, response.text

    result = client.get(
        f"/api/knowledge/entities/{entity['id']}/facts/handover",
        headers=auth(owner),
    ).json()
    assert result["conflict"] is True
    assert result["selected"] is None
    assert {candidate["value"] for candidate in result["candidates"]} == {"Q4 2028", "Q1 2029"}


def test_chunk_search_and_visual_asset_keep_source_provenance(tmp_path):
    _, client, owner, _ = client_for(tmp_path)
    document = register_doc(client, owner, "deck-1", "v1", "Launch deck.pptx")
    unit = client.post(
        "/api/knowledge/units",
        headers=auth(owner),
        json={
            "document_version_id": document["document_version_id"],
            "unit_index": 8,
            "unit_type": "slide",
            "label": "Location",
            "text_content": "Dubai Hills Estate with access to major roads.",
            "visual_summary": "Map with labelled nearby destinations.",
        },
    ).json()
    chunk = client.post(
        "/api/knowledge/chunks",
        headers=auth(owner),
        json={
            "document_version_id": document["document_version_id"],
            "unit_id": unit["id"],
            "chunk_index": 0,
            "text_content": "Dubai Hills Estate location and transport connections.",
            "token_estimate": 9,
        },
    )
    assert chunk.status_code == 201
    asset_payload = {
        "document_version_id": document["document_version_id"],
        "unit_id": unit["id"],
        "asset_type": "location_map",
        "source_locator": "slide:8#map-1",
        "summary": "Map used as evidence for location relationships.",
    }
    asset = client.post("/api/knowledge/assets", headers=auth(owner), json=asset_payload)
    assert asset.status_code == 201
    assert asset.json()["idempotent"] is False
    repeated_asset = client.post("/api/knowledge/assets", headers=auth(owner), json=asset_payload)
    assert repeated_asset.status_code == 201
    assert repeated_asset.json()["id"] == asset.json()["id"]
    assert repeated_asset.json()["idempotent"] is True

    search = client.get(
        "/api/knowledge/search",
        params={"q": "Dubai Hills transport"},
        headers=auth(owner),
    )
    assert search.status_code == 200
    item = search.json()["items"][0]
    assert item["type"] == "chunk"
    assert item["document"]["external_id"] == "deck-1"
    assert item["unit"]["index"] == 8


def test_ingestion_run_records_progress(tmp_path):
    _, client, owner, _ = client_for(tmp_path)
    run = client.post(
        "/api/knowledge/ingestions",
        headers=auth(owner),
        json={"source_type": "google_drive", "root_external_id": "folder-123"},
    )
    assert run.status_code == 201
    ident = run.json()["id"]
    done = client.patch(
        f"/api/knowledge/ingestions/{ident}",
        headers=auth(owner),
        json={
            "status": "completed",
            "counts": {"documents": 437, "processed": 421, "needs_review": 16},
        },
    )
    assert done.status_code == 200
    assert done.json()["status"] == "completed"
