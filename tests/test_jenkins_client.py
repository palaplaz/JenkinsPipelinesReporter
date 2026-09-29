import pytest
import requests

from fpreporter.config import Credentials, JenkinsSettings
from fpreporter.jenkins_client import JenkinsClient, JenkinsError

SETTINGS = JenkinsSettings(url="https://jenkins.example.com", verify_tls=False, timeout_seconds=42)
CREDS = Credentials(user="svc", token="tok")


class FakeResponse:
    def __init__(self, status_code=200, text="", encoding=None):
        self.status_code = status_code
        self.text = text
        self.encoding = encoding


class FakeSession:
    def __init__(self, response=None, exception=None):
        self.response = response
        self.exception = exception
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exception:
            raise self.exception
        return self.response


def test_posts_script_with_auth_timeout_and_tls_setting():
    session = FakeSession(FakeResponse(text="output"))

    assert JenkinsClient(SETTINGS, CREDS, session).run_script("println 1") == "output"

    url, kwargs = session.calls[0]
    assert url == "https://jenkins.example.com/scriptText"
    assert kwargs == {"data": {"script": "println 1"}, "auth": ("svc", "tok"), "timeout": 42, "verify": False}


def test_defaults_to_utf8_when_charset_missing():
    response = FakeResponse(text="x", encoding="ISO-8859-1")
    JenkinsClient(SETTINGS, CREDS, FakeSession(response)).run_script("s")
    assert response.encoding == "utf-8"


@pytest.mark.parametrize(
    "status, fragment",
    [(401, "JENKINS_TOKEN"), (403, "Overall/Administer"), (500, "HTTP 500")],
)
def test_http_errors(status, fragment):
    client = JenkinsClient(SETTINGS, CREDS, FakeSession(FakeResponse(status, "err")))
    with pytest.raises(JenkinsError, match=fragment):
        client.run_script("s")


@pytest.mark.parametrize(
    "exception, fragment",
    [(requests.Timeout(), "Timed out after 42s"), (requests.ConnectionError("refused"), "Could not reach")],
)
def test_network_errors(exception, fragment):
    client = JenkinsClient(SETTINGS, CREDS, FakeSession(exception=exception))
    with pytest.raises(JenkinsError, match=fragment):
        client.run_script("s")
