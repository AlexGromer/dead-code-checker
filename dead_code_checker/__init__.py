"""dead-code-checker: dead code in what a commit or a session added."""

from .core import Finding, check_session, check_staged, main, scan

__version__ = "0.1.0"
__all__ = ["Finding", "__version__", "check_session", "check_staged", "main", "scan"]
