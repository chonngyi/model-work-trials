"""One text-only Chat Completions request via the caller's configured endpoint."""
from http.client import HTTPException
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, Request


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def settings(endpoint, timeout):
    url = urlsplit(endpoint)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or url.password is not None or url.query or url.fragment):
        raise ValueError("Supply an HTTPS endpoint without user info, query, or fragment")
    if timeout < 1:
        raise ValueError("Specify a positive timeout in seconds")
    key = os.environ.get("TRIAL_API_KEY")
    if not key:
        raise ValueError("Set TRIAL_API_KEY in the environment for this endpoint")
    return key


def complete(endpoint, key, timeout, model, effort, messages):
    payload = {"model": model, "messages": messages, "stream": False}
    if effort != "unspecified":
        payload["reasoning_effort"] = effort
    req = Request(endpoint, data=json.dumps(payload).encode(),
                  headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    start = time.monotonic()
    receipt = {"endpoint": endpoint}
    try:
        with build_opener(NoRedirect()).open(req, timeout=timeout) as response:
            reply = json.load(response)
        choice = reply["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
        if not isinstance(text, str):
            raise ValueError("Unsupported non-text content")
        reason = choice.get("finish_reason")
        receipt.update(actual_model=reply.get("model"), finish_reason=reason, usage=reply.get("usage"))
        if not text.strip():
            outcome = ("failed", "", "No usable text in the response")
        elif reason != "stop":
            outcome = ("partial", text, f"Incomplete completion: {reason}")
        else:
            outcome = ("complete", text, "")
    except HTTPError as error:
        outcome = ("failed", "", f"HTTP {error.code}")
    except (URLError, OSError, EOFError, HTTPException, ValueError, KeyError, TypeError, IndexError, AttributeError) as error:
        outcome = ("failed", "", f"Transport or response error: {type(error).__name__}")
    receipt["elapsed_seconds"] = round(time.monotonic() - start, 3)
    return (*outcome, receipt)
