"""Authenticated requests to the cluster, identity provider and cloud services stay on origin."""

from io import BytesIO

import pytest
import requests
from google_http_sessions import GOOGLE_SESSIONS, google_session

from iki_server.accounts.adapters import SocialAccountAdapter
from iki_server.runs.kubernetes import InCluster


@pytest.fixture(params=["cluster", "identity provider", *GOOGLE_SESSIONS])
def session(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> requests.Session:
    if request.param in GOOGLE_SESSIONS:
        return google_session(request.param, monkeypatch)
    if request.param == "cluster":
        return InCluster("service.example", "443").session
    return SocialAccountAdapter().get_requests_session()


def _redirects(
    monkeypatch: pytest.MonkeyPatch, locations: list[str], status: int = 307
) -> list[requests.PreparedRequest]:
    sent: list[requests.PreparedRequest] = []
    locations_iter = iter(locations)

    def send(
        self: requests.adapters.HTTPAdapter, request: requests.PreparedRequest, **kwargs: object
    ) -> requests.Response:
        sent.append(request)
        response = requests.Response()
        response.request = request
        response.url = str(request.url)
        response.raw = BytesIO()
        location = next(locations_iter, None)
        if location is None:
            response.status_code = 200
        else:
            response.status_code = status
            response.headers["Location"] = location
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", send)
    return sent


def test_same_origin_redirect_keeps_the_authenticated_post(
    session: requests.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _redirects(monkeypatch, ["../next", "https://SERVICE.example:443/end"])
    session.cookies.set("session", "fixture", domain="service.example", path="/")
    response = session.post(
        "https://service.example/one/start",
        headers={"Authorization": "Bearer fixture"},
        data={"code": "example"},
    )
    assert response.status_code == 200
    assert [call.url for call in sent][:2] == [
        "https://service.example/one/start",
        "https://service.example/next",
    ]
    assert len(sent) == 3
    for call in sent:
        assert call.method == "POST" and call.body == "code=example"
        assert call.headers["Authorization"] == "Bearer fixture"
        assert call.headers["Cookie"] == "session=fixture"


@pytest.mark.parametrize(
    ("scheme", "location"),
    [
        ("https", "https://other.example/next"),
        ("https", "//service.example:8443/next"),
        ("https", "http://service.example/next"),
        ("http", "https://service.example/next"),
    ],
)
def test_cross_origin_redirect_stops_before_replaying_the_request(
    session: requests.Session, monkeypatch: pytest.MonkeyPatch, scheme: str, location: str
) -> None:
    sent = _redirects(monkeypatch, ["/same-origin", location], status=308)
    with pytest.raises(requests.RequestException, match="different HTTP origin"):
        session.post(f"{scheme}://service.example/start", data={"code": "example"})
    assert [call.url for call in sent] == [
        f"{scheme}://service.example/start",
        f"{scheme}://service.example/same-origin",
    ]


def test_redirect_chain_stops_after_five_hops(
    session: requests.Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent = _redirects(monkeypatch, [f"/next/{i}" for i in range(6)])
    with pytest.raises(requests.TooManyRedirects):
        session.get("https://service.example/start")
    assert len(sent) == 6
