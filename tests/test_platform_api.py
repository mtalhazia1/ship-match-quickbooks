"""API: key scopes (and keys made before scopes), documents list, shipments with issues and totals, the
API keys page, and the OpenAPI description."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.accounts.models import ApiKey
from apps.accounts.services.apikeys import create_key
from apps.documents.services.ingest import ingest_bytes
from apps.shipments.models import Shipment


@pytest.fixture
def loaded(org, dataset):
    for f in ["S03_1_commercial_invoice.pdf", "S03_2_bill_of_lading.pdf", "S03_3_freight_invoice.pdf",
              "S01_1_commercial_invoice.pdf", "S01_2_bill_of_lading.pdf", "S01_3_freight_invoice.pdf"]:
        ingest_bytes(org, f, (dataset / "pdf" / f).read_bytes(), process="sync")
    return org


def _auth(token):
    return {"HTTP_AUTHORIZATION": f"Bearer {token}"}


@pytest.mark.django_db
def test_keys_made_before_scopes_keep_working(client, loaded, dataset):
    _k, read_only = create_key(loaded, "Old reader", "viewer", None, None)
    _k, uploader = create_key(loaded, "Old uploader", "reviewer", None, None)
    assert ApiKey.objects.get(name="Old reader").scopes == []
    for path in ("shipments", "documents", "exports/shipments"):
        assert client.get(f"/api/{loaded.slug}/{path}", **_auth(read_only)).status_code == 200, path
    pdf = SimpleUploadedFile("x.pdf", (dataset / "pdf" / "S02_1_commercial_invoice.pdf").read_bytes())
    r = client.post(f"/api/{loaded.slug}/documents", {"file": pdf}, **_auth(read_only))
    assert r.status_code == 403 and "read only" in r.json()["detail"]
    pdf.seek(0)
    assert client.post(f"/api/{loaded.slug}/documents", {"file": pdf}, **_auth(uploader)).status_code == 201


@pytest.mark.django_db
def test_scopes_limit_what_a_key_can_read_and_write(client, loaded, dataset):
    _k, shipments_only = create_key(loaded, "Ships", "viewer", None, None, scopes=["shipments:read"])
    assert client.get(f"/api/{loaded.slug}/shipments", **_auth(shipments_only)).status_code == 200
    doc = loaded.documents.first()
    for path in ("documents", f"documents/{doc.pk}", "exports/documents"):
        r = client.get(f"/api/{loaded.slug}/{path}", **_auth(shipments_only))
        assert r.status_code == 403 and "scope" in r.json()["detail"], path
    _k, read_and_upload_no_docs = create_key(loaded, "Upload", "reviewer", None, None,
                                             scopes=["documents:write"])
    pdf = SimpleUploadedFile("x.pdf", (dataset / "pdf" / "S02_1_commercial_invoice.pdf").read_bytes())
    assert client.post(f"/api/{loaded.slug}/documents", {"file": pdf},
                       **_auth(read_and_upload_no_docs)).status_code == 201
    assert client.get(f"/api/{loaded.slug}/shipments", **_auth(read_and_upload_no_docs)).status_code == 403
    _k, no_write = create_key(loaded, "NoWrite", "reviewer", None, None, scopes=["documents:read"])
    pdf.seek(0)
    r = client.post(f"/api/{loaded.slug}/documents", {"file": pdf}, **_auth(no_write))
    assert r.status_code == 403 and "documents:write" in r.json()["detail"]


@pytest.mark.django_db
def test_signed_in_session_uses_the_role_not_scopes(client, loaded, viewer):
    client.force_login(viewer)
    assert client.get(f"/api/{loaded.slug}/documents").status_code == 200
    assert client.get(f"/api/{loaded.slug}/exports/issues", {"status": "all"}).status_code == 200


@pytest.mark.django_db
def test_documents_list_with_fields_and_filters(client, loaded):
    _k, token = create_key(loaded, "Docs", "viewer", None, None, scopes=["documents:read"])
    docs = client.get(f"/api/{loaded.slug}/documents", **_auth(token)).json()
    assert len(docs) == 6 and all("fields" in d and d["shipment"] for d in docs)
    invoice = next(d for d in docs if d["doc_type"] == "freight_invoice")
    assert {f["name"] for f in invoice["fields"]} >= {"invoice_number", "total_amount", "bl_number"}
    bls = client.get(f"/api/{loaded.slug}/documents", {"type": "bill_of_lading"}, **_auth(token)).json()
    assert len(bls) == 2 and {d["doc_type"] for d in bls} == {"bill_of_lading"}
    assert client.get(f"/api/{loaded.slug}/documents", {"view": "attention"}, **_auth(token)).json() == []
    page = client.get(f"/api/{loaded.slug}/documents", {"limit": 2, "offset": 1}, **_auth(token)).json()
    assert [d["id"] for d in page] == [d["id"] for d in docs[1:3]]
    assert client.get(f"/api/{loaded.slug}/documents", {"view": "bogus"}, **_auth(token)).status_code == 422


@pytest.mark.django_db
def test_shipments_with_issue_counts_issues_and_totals(client, loaded):
    _k, token = create_key(loaded, "Ships", "viewer", None, None, scopes=["shipments:read"])
    ships = client.get(f"/api/{loaded.slug}/shipments", **_auth(token)).json()
    bad = Shipment.objects.get(organization=loaded, issues__code="total_mismatch")
    row = next(s for s in ships if s["id"] == bad.pk)
    assert row["open_errors"] >= 1 and "open_warnings" in row and row["created_at"]
    found = client.get(f"/api/{loaded.slug}/shipments", {"q": bad.bl_number}, **_auth(token)).json()
    assert [s["id"] for s in found] == [bad.pk]
    detail = client.get(f"/api/{loaded.slug}/shipments/{bad.pk}", **_auth(token)).json()
    issue = next(i for i in detail["issues"] if i["code"] == "total_mismatch")
    assert issue["title"] == "Total doesn't match line items" and issue["severity"] == "error" and issue["id"]
    assert issue["document_id"] and issue["resolved"] is False
    assert detail["totals"]["by_currency"] and detail["totals"]["home_currency"] == loaded.home_currency
    assert len(detail["documents"]) == 3


@pytest.mark.django_db
def test_api_keys_page_creates_keys_with_scopes(client, admin_user, org):
    client.force_login(admin_user)
    page = client.get(reverse("core:api_keys")).content.decode()
    assert 'name="scopes" value="exports:read"' in page
    client.post(reverse("core:api_keys"), {"name": "BI tool", "role": "viewer", "days": "90", "scopes_present": "1",
                                           "scopes": ["shipments:read", "exports:read"]})
    key = ApiKey.objects.get(name="BI tool")
    assert key.scopes == ["shipments:read", "exports:read"] and key.scope_labels == ["Shipments", "Exports"]
    assert "Shipments, Exports" in client.get(reverse("core:api_keys")).content.decode()
    client.post(reverse("core:api_keys"), {"name": "Uploader", "role": "reviewer", "scopes_present": "1"})
    assert ApiKey.objects.get(name="Uploader").scopes == ["documents:write"]
    r = client.post(reverse("core:api_keys"), {"name": "Nothing", "role": "viewer", "scopes_present": "1"},
                    follow=True)
    assert b"Choose at least one thing the key may read" in r.content
    assert not ApiKey.objects.filter(name="Nothing").exists()
    # A form without the scope choices (an older page) makes a key with everything its access allows.
    client.post(reverse("core:api_keys"), {"name": "Legacy form", "role": "viewer"})
    assert ApiKey.objects.get(name="Legacy form").scopes == []


@pytest.mark.django_db
def test_openapi_describes_scopes_and_new_endpoints(client, admin_user, org):
    client.force_login(admin_user)
    schema = client.get("/api/openapi.json").json()
    assert "documents:write" in schema["info"]["description"]
    paths = schema["paths"]
    assert "/api/{org}/documents" in paths and "get" in paths["/api/{org}/documents"]
    assert "/api/{org}/exports/{kind}" in paths
    assert "exports:read" in paths["/api/{org}/exports/{kind}"]["get"]["description"]


# --------------------------------------------------------------------------- QA-004: the reference page must render


def test_openapi_document_has_no_duplicate_keys(client):
    """Swagger UI parses the spec as YAML and refuses a mapping that repeats a key, so the whole API reference
    page showed "Unable to render this definition". The export operation listed its 200 response twice."""
    import json

    def reject_duplicates(pairs):
        keys = [k for k, _ in pairs]
        repeated = sorted({k for k in keys if keys.count(k) > 1})
        assert not repeated, f"duplicate keys {repeated} in {keys}"
        return dict(pairs)

    r = client.get("/api/openapi.json")
    assert r.status_code == 200
    spec = json.loads(r.content.decode(), object_pairs_hook=reject_duplicates)
    export = spec["paths"]["/api/{org}/exports/{kind}"]["get"]["responses"]
    assert set(export) >= {"200", "400"}
    assert "text/csv" in export["200"]["content"]


def test_openapi_has_a_version_and_every_operation_has_a_response(client):
    import json

    spec = json.loads(client.get("/api/openapi.json").content.decode())
    assert spec["openapi"].startswith("3.")
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            assert op.get("responses"), f"{method} {path} has no responses"
