"""Runs Groovy scripts on Jenkins via the Script Console endpoint (POST /scriptText)."""

from __future__ import annotations

import requests

from fpreporter.config import Credentials, JenkinsSettings

_BODY_SNIPPET = 500


class JenkinsError(Exception):
    """Raised when Jenkins cannot be reached or rejects the request."""


class JenkinsClient:
    def __init__(
        self,
        settings: JenkinsSettings,
        credentials: Credentials,
        session: requests.Session | None = None,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._session = session or requests.Session()

    def run_script(self, script: str) -> str:
        """Executes the script and returns everything it printed.

        Note: a script that throws still yields HTTP 200 with the stack trace as the body;
        callers must validate the output themselves.
        """
        url = f"{self._settings.url}/scriptText"
        try:
            # API-token authentication is exempt from CSRF crumbs, so no crumb request is needed.
            response = self._session.post(
                url,
                data={"script": script},
                auth=(self._credentials.user, self._credentials.token),
                timeout=self._settings.timeout_seconds,
                verify=self._settings.verify_tls,
            )
        except requests.Timeout:
            raise JenkinsError(
                f"Timed out after {self._settings.timeout_seconds}s waiting for {url} "
                "(increase [jenkins].timeout_seconds)"
            ) from None
        except requests.RequestException as e:
            raise JenkinsError(f"Could not reach {url}: {e}") from None

        if response.status_code == 401:
            raise JenkinsError("Authentication failed (401): check JENKINS_USER and JENKINS_TOKEN")
        if response.status_code == 403:
            raise JenkinsError(
                f"Forbidden (403): user {self._credentials.user!r} needs Overall/Administer "
                "to use the Script Console"
            )
        if not 200 <= response.status_code < 300:
            raise JenkinsError(
                f"Unexpected HTTP {response.status_code} from {url}: {response.text[:_BODY_SNIPPET]}"
            )

        if response.encoding is None or response.encoding.lower() == "iso-8859-1":
            # Jenkins writes UTF-8; requests assumes ISO-8859-1 for text/plain without a charset.
            response.encoding = "utf-8"
        return response.text
