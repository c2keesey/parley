"""Two-way voice for terminal coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("parley-voice")
except PackageNotFoundError:  # pragma: no cover - direct, uninstalled source import
    __version__ = "0+unknown"
