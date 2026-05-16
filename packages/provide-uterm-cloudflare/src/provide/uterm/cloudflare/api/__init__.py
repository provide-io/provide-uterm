__all__ = ["route_http"]


def __getattr__(name):  # pragma: no cover
    """Lazy import to avoid module loading issues during Pyodide validation."""
    if name == "route_http":
        from .http_routes import route_http

        return route_http
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
