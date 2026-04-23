"""Gitoma — AI-powered GitHub repository improvement agent."""

# Suppress urllib3 LibreSSL warning on macOS.
# Must be first: env var prevents urllib3 from ever emitting it at import time.
import os as _os
import warnings as _warnings
_os.environ.setdefault("PYTHONWARNINGS", "ignore::UserWarning:urllib3")
_warnings.filterwarnings("ignore", message=".*NotOpenSSLWarning.*")
_warnings.filterwarnings("ignore", message=".*LibreSSL.*")
_warnings.filterwarnings("ignore", category=UserWarning, module="urllib3")

__version__ = "0.3.0"
__author__ = "FabGPT"
__email__ = "fabgpt.inbox@gmail.com"

