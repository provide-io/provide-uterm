from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("provide-terminal-cloudflare")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"  # pragma: no cover

from .config import CloudflareConfig

__all__ = ["CloudflareConfig", "__version__"]
