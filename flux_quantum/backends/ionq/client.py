"""The few calls the IonQ backend makes, with the key in a header.

Nothing but the standard library. The endpoint is overridable, which is how
the tests and a dry run without a key point it at the fake service.
"""

import json

from ..base import BackendError

API_URL = "https://api.ionq.co/v0.4"


class APIError(BackendError):
    """The service answered with an error status."""

    def __init__(self, status, method, path, body):
        self.status = status
        text = body.decode(errors="replace") if isinstance(body, bytes) else body
        super().__init__(
            "ionq: {} {} returned {}: {}".format(method, path, status, text[:200])
        )


class Client:
    """The few calls the backend makes, with the key in a header and the
    endpoint overridable."""

    def __init__(self, key, url=API_URL, timeout=30):
        self.url = url.rstrip("/")
        self._key = key
        self.timeout = timeout

    def request(self, method, path, body=None):
        import urllib.error
        import urllib.request

        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "apiKey " + self._key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise APIError(e.code, method, path, e.read())
        except urllib.error.URLError as e:
            raise BackendError("ionq: cannot reach {}: {}".format(self.url, e.reason))
        return json.loads(raw) if raw else {}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body if body is not None else {})

    def put(self, path, body=None):
        return self.request("PUT", path, body)
