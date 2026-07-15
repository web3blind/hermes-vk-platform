"""VK Messenger platform plugin for Hermes Agent."""

try:
    from .adapter import register
except ImportError:  # Allows pytest to import this directory as a flat test root.
    from adapter import register  # type: ignore

__all__ = ["register"]
