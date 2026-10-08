"""QA-054 and QA-055: what a person sees when a page has gone stale.

* A bad or missing form token showed Django's bare "CSRF verification failed… Reason given for failure".
* A form posted after the session ended sent the person to sign in with the form's own POST-only address as
  the place to return to, so signing in landed on an empty "405 Method Not Allowed" page."""
import pytest
from django.test import Client
from django.urls import reverse


@pytest.fixture
def strict_client():
    return Client(enforce_csrf_checks=True)


# ---------------------------------------------------------------- QA-054


@pytest.mark.django_db
def test_bad_token_shows_a_plain_expired_page(strict_client, user):
    strict_client.force_login(user)

    r = strict_client.post(reverse("review:upload"), {"csrfmiddlewaretoken": "wrong"})

    html = r.content.decode()
    assert r.status_code == 403
    assert "This page has expired" in html and "Nothing was changed" in html
    for jargon in ("CSRF", "Reason given", "verification failed", "Help"):
        assert jargon not in html


@pytest.mark.django_db
def test_missing_token_on_the_sign_in_form_is_also_friendly(strict_client):
    r = strict_client.post(reverse("accounts:login"), {"username": "x", "password": "y"})
    assert r.status_code == 403 and "This page has expired" in r.content.decode()


@pytest.mark.django_db
def test_expired_page_links_back_to_the_page_you_were_on(strict_client, user):
    strict_client.force_login(user)
    r = strict_client.post(reverse("review:upload"), {}, HTTP_REFERER="http://testserver/review/documents/")
    assert 'href="http://testserver/review/documents/"' in r.content.decode()


@pytest.mark.django_db
def test_expired_page_never_links_to_another_site(strict_client, user):
    strict_client.force_login(user)
    r = strict_client.post(reverse("review:upload"), {}, HTTP_REFERER="https://evil.example/phish")
    html = r.content.decode()
    assert "evil.example" not in html and 'href="/"' in html


# ---------------------------------------------------------------- QA-055


FORM_URL = "/work/shipments/42/assign/"   # accepts POST only


@pytest.mark.django_db
def test_signed_out_form_post_returns_to_the_page_it_came_from(client):
    r = client.post(FORM_URL, {"assignee": "none"}, HTTP_REFERER="http://testserver/review/shipments/42/")
    assert r.status_code == 302
    assert r["Location"] == reverse("accounts:login") + "?next=/review/shipments/42/"


@pytest.mark.django_db
def test_the_page_address_keeps_its_query(client):
    r = client.post(FORM_URL, {}, HTTP_REFERER="http://testserver/review/?status=ready&page=2")
    assert r["Location"].startswith(reverse("accounts:login") + "?next=")
    assert "status%3Dready" in r["Location"] and "page%3D2" in r["Location"]


@pytest.mark.django_db
@pytest.mark.parametrize("referer", ["", "https://evil.example/x", "not a url", "http://testserver" + FORM_URL])
def test_without_a_usable_referer_the_person_just_signs_in(client, referer):
    r = client.post(FORM_URL, {}, HTTP_REFERER=referer)
    assert r["Location"] == reverse("accounts:login")


@pytest.mark.django_db
def test_signed_out_page_visits_are_unchanged(client):
    r = client.get(reverse("review:queue"))
    assert r["Location"] == reverse("accounts:login") + "?next=/review/"


@pytest.mark.django_db
def test_idle_timeout_on_a_form_post_also_returns_to_its_page(client, user, settings):
    settings.SESSION_IDLE_TIMEOUT = 10
    client.force_login(user)
    session = client.session
    session["last_activity"] = 1   # a long time ago
    session.save()

    r = client.post(FORM_URL, {}, HTTP_REFERER="http://testserver/review/shipments/42/")

    assert r.status_code == 302
    assert r["Location"] == reverse("accounts:login") + "?next=/review/shipments/42/"


@pytest.mark.django_db
def test_after_signing_in_the_page_they_were_on_opens(client, user):
    from tests.conftest import PASSWORD

    r = client.post(FORM_URL, {}, HTTP_REFERER="http://testserver/review/documents/")
    login = client.post(r["Location"], {"username": user.username, "password": PASSWORD})
    assert login.status_code == 302 and login["Location"] == "/review/documents/"
    assert client.get(login["Location"]).status_code == 200
