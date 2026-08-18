#
# SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
"""Outbound-HTTP mocking for the server suite, built on the ``_http`` seam.

This replaces respx. respx is pinned to httpx (it declares ``httpx>=0.25`` and
validates return values with ``isinstance(value, httpx.Response)``), so it
cannot mock httpx2 -- the two are separate distributions with unrelated class
hierarchies. Keeping respx therefore meant keeping a second HTTP stack
installed forever, which is what let ``starlette``'s move to httpx2 silently
break 323 tests.

Rather than patch an HTTP library process-wide, this intercepts at the one
place the package builds clients: :func:`provide.uterm.server._http.async_client`.
That makes interception explicit and parallel-safe, where respx's global
patching is a known hazard under pytest-xdist.

The API is deliberately respx-shaped -- ``mock`` as decorator/context manager,
``post()``/``get()`` returning a route with ``.mock(return_value=...)`` and
``.called``/``.calls.last.request`` -- because the suite it replaces is 12
security test files (delegated IdP auth, SSRF egress guards, replay defence,
policy gates). Matching the shape keeps those assertions byte-identical
through the migration instead of rewriting ~176 call sites by hand and hoping
none of them quietly got weaker.

Usage::

    @http_mock.mock
    async def test_denies_on_idp_error():
        route = http_mock.post(url).mock(return_value=Response(500))
        ...
        assert route.called
"""

from __future__ import annotations

import functools
import inspect
from typing import TYPE_CHECKING, Any
from unittest import mock as _umock
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import httpx

from provide.uterm.server import _http

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

__all__ = ["Response", "Router", "get", "mock", "post", "request"]

# Tests build expected responses with this name; re-exported so a test file
# never imports the HTTP library directly (which is what the CI guard forbids).
Response = _http.Response


class _Call:
    """One intercepted request/response pair."""

    __slots__ = ("request", "response")

    def __init__(self, request: Any, response: Any) -> None:
        self.request = request
        self.response = response


class _CallList(list):
    """Call history with respx's ``.last`` accessor."""

    @property
    def last(self) -> _Call:
        if not self:
            raise AssertionError("no calls recorded for this route")
        return self[-1]


class Route:
    """A method+URL matcher and its recorded calls."""

    def __init__(self, method: str, url: str) -> None:
        self.method = method.upper()
        self.url = url
        parsed = urlsplit(url)
        self._base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        self._query = dict(parse_qsl(parsed.query))
        self._return_value: Any = None
        self._side_effect: Any = None
        self.calls: _CallList = _CallList()

    def mock(self, *, return_value: Any = None, side_effect: Any = None) -> Route:
        """Set the response, or a side effect to raise/compute one.

        ``side_effect`` accepts either an exception (raised to the caller, so
        connect/timeout failure paths can be exercised) or a callable taking
        the request and returning a response.
        """
        self._return_value = return_value
        self._side_effect = side_effect
        return self

    def respond(self, status_code: int = 200, **kwargs: Any) -> Route:
        return self.mock(return_value=Response(status_code, **kwargs))

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _matches(self, request: Any) -> bool:
        """Match like respx: on method + scheme/host/path, query only if declared.

        Callers pass query parameters separately (``client.get(url,
        params=...)``), so the request URL carries a query string the route
        pattern does not. respx treats a pattern without a query as
        query-agnostic; requiring exact string equality would miss those.
        """
        if request.method.upper() != self.method:
            return False
        actual = urlsplit(str(request.url))
        if urlunsplit((actual.scheme, actual.netloc, actual.path, "", "")) != self._base:
            return False
        if not self._query:
            return True
        actual_query = dict(parse_qsl(actual.query))
        return all(actual_query.get(key) == value for key, value in self._query.items())

    def _resolve(self, request: Any) -> Any:
        if self._side_effect is not None:
            if isinstance(self._side_effect, BaseException):
                raise self._side_effect
            if isinstance(self._side_effect, type) and issubclass(self._side_effect, BaseException):
                raise self._side_effect()
            result = self._side_effect(request)
            if isinstance(result, BaseException):
                raise result
            return result
        if self._return_value is not None:
            return self._return_value
        return Response(200)


class Router:
    """Collects routes and serves them through a mock transport."""

    def __init__(self) -> None:
        self.routes: list[Route] = []

    def request(self, method: str, url: str) -> Route:
        route = Route(method, url)
        self.routes.append(route)
        return route

    def post(self, url: str) -> Route:
        return self.request("POST", url)

    def get(self, url: str) -> Route:
        return self.request("GET", url)

    def handler(self, request: Any) -> Any:
        """Transport entry point: dispatch one request to its route."""
        # Most-recently-declared match wins. respx replaces a route when the
        # same pattern is registered again, and the suite leans on that: several
        # tests declare the same URL again between calls to change the response,
        # then assert against the newest route's own call log.
        for route in reversed(self.routes):
            if route._matches(request):
                # Read the body before the response is built so assertions on
                # ``calls.last.request.content`` work for streamed bodies.
                if hasattr(request, "read"):
                    request.read()
                try:
                    response = route._resolve(request)
                except BaseException:
                    # respx records the call before the error escapes, so a
                    # simulated connect/timeout failure still counts as called.
                    route.calls.append(_Call(request, None))
                    raise
                route.calls.append(_Call(request, response))
                return response
        raise AssertionError(f"unmocked request: {request.method} {request.url}")


# The active router stack is module-level so that `post()`/`get()` resolve
# against whichever context is innermost, regardless of which _MockContext
# instance opened it (`mock` vs `mock(assert_all_called=False)`).
_ACTIVE: list[tuple[Router, Any, bool]] = []


class _MockContext:
    """``mock`` as decorator, sync context manager, and async context manager."""

    def __init__(self, *, assert_all_called: bool = False) -> None:
        self._assert_all_called = assert_all_called

    def _start(self) -> Router:
        router = Router()
        patcher = _umock.patch.object(
            _http,
            "async_client",
            side_effect=lambda **kwargs: _build_client(router, **kwargs),
        )
        patcher.start()
        _ACTIVE.append((router, patcher, self._assert_all_called))
        return router

    def _stop(self, failed: bool) -> None:
        router, patcher, assert_all_called = _ACTIVE.pop()
        patcher.stop()
        # respx fails a test that declares a route nothing calls. Preserve that:
        # dropping it would silently weaken every converted test, several of
        # which assert a security path was actually exercised. Skipped when the
        # body already raised, so the real failure is not masked.
        if assert_all_called and not failed:
            uncalled = [f"{r.method} {r.url}" for r in router.routes if not r.called]
            if uncalled:
                raise AssertionError(f"mocked but never called: {', '.join(uncalled)}")

    def __enter__(self) -> Router:
        return self._start()

    def __exit__(self, exc_type: object, *_: object) -> None:
        self._stop(failed=exc_type is not None)

    async def __aenter__(self) -> Router:
        return self._start()

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        self._stop(failed=exc_type is not None)

    def __call__(self, func: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
        """Decorate a test, or configure a fresh context.

        ``@mock`` decorates directly; ``mock(assert_all_called=False)`` returns
        a configured context manager, matching respx's dual-use API.
        """
        if func is None:
            kwargs.setdefault("assert_all_called", True)
            return _MockContext(**kwargs)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def _async_wrapper(*args: Any, **kw: Any) -> Any:
                with self:
                    return await func(*args, **kw)

            return _async_wrapper

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kw: Any) -> Any:
            with self:
                return func(*args, **kw)

        return _sync_wrapper


def _current() -> Router:
    if not _ACTIVE:
        raise AssertionError("http_mock route declared outside an active `mock` context")
    return _ACTIVE[-1][0]


def _build_client(router: Router, **kwargs: Any) -> Any:
    """An async client whose transport is the router, bypassing the network."""
    kwargs.pop("transport", None)
    return httpx.AsyncClient(transport=httpx.MockTransport(router.handler), **kwargs)


mock = _MockContext()


def post(url: str) -> Route:
    return _current().post(url)


def get(url: str) -> Route:
    return _current().get(url)


def request(method: str, url: str) -> Route:
    return _current().request(method, url)


def routes() -> Iterator[Route]:
    return iter(_current().routes)
