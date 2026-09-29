"""Killable HTTP transport; fixed search workers bound child-process count."""

import json
import subprocess
import sys
from pathlib import Path

import requests
from .health import ProviderError

HTTP_WORKER = Path(__file__).with_name("http_worker.py")
LEGACY_CLI_DOH = False


def fetch_response(
    url,
    *,
    params,
    headers,
    connect_timeout,
    read_timeout,
    total_timeout,
    max_bytes,
    proxy=None,
):
    args = dict(
        url=url,
        params=params,
        headers=headers,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        total_timeout=total_timeout,
        max_bytes=max_bytes,
        proxy=proxy,
        legacy_cli_doh=LEGACY_CLI_DOH,
    )
    try:
        # subprocess.run kills and reaps its child when communicate times out.
        result = subprocess.run(
            [sys.executable, str(HTTP_WORKER)],
            input=json.dumps(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=total_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProviderError("deadline", retryable=True) from exc
    if result.returncode != 0:
        raise ProviderError("transport")
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise ProviderError("transport") from exc
    if payload.get("error"):
        raise ProviderError(payload["error"], retryable=payload.get("retryable", False))
    response = requests.Response()
    response.status_code = payload["status"]
    response.url = payload["url"]
    response.headers.update(
        {"Content-Type": payload["content_type"], "Retry-After": payload["retry_after"]}
    )
    response.encoding = "utf-8"
    response._content = payload["body"].encode()
    response._content_consumed = True
    return response
