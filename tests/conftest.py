import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache

from apps.core.models import Membership, Organization
from synthetic.generator import generate

PASSWORD = "pw-123456789-test"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path, monkeypatch):
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", "q1Zr6cXlq3mP0b8yXe2bW3l0cHk3Vt9sQq7m2r5n8xA=")
    settings.MEDIA_ROOT = tmp_path / "media"
    settings.EXTRACTION_PROVIDER = "rules"
    settings.OCR_PROVIDER = "text"
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    cache.clear()  # lockout counters, rate limits and TOTP replay keys live in the cache
    yield
    cache.clear()


@pytest.fixture(scope="session")
def dataset(tmp_path_factory):
    out = tmp_path_factory.mktemp("synthetic")
    generate(out, n_shipments=15, seed=11, scanned=1)
    return out


@pytest.fixture
def org(db):
    return Organization.objects.create(name="Test Imports", slug="test")


def _member(org, username, role, limit=None):
    u = get_user_model().objects.create_user(username, email=f"{username}@example.com", password=PASSWORD)
    Membership.objects.create(user=u, organization=org, role=role, approval_limit=limit)
    return u


@pytest.fixture
def user(db, org):
    """A reviewer: may edit and accept warnings, may not override errors or approve."""
    return _member(org, "reviewer", Membership.Role.REVIEWER)


@pytest.fixture
def viewer(db, org):
    return _member(org, "viewer", Membership.Role.VIEWER)


@pytest.fixture
def approver(db, org):
    return _member(org, "approver", Membership.Role.APPROVER)


@pytest.fixture
def approver2(db, org):
    return _member(org, "approver2", Membership.Role.APPROVER)


@pytest.fixture
def admin_user(db, org):
    return _member(org, "orgadmin", Membership.Role.ADMIN)
