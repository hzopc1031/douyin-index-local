"""Local macOS bundle launcher; loads this project's inspectable source files."""
from pathlib import Path
import runpy
import sys
import csv
import decimal
import zoneinfo
import tempfile
import uuid
import io

# Explicit imports let PyInstaller bundle the official registry dependencies.
import webview
import webview.platforms.cocoa
import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding

root = Path(sys.executable).resolve().parents[4] if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
sys.path.insert(0, str(root))
# Expose helpers used by the dynamically loaded app to PyInstaller analysis.
import browser_login
import hotlist_client
import local_comments
import webview_cookies
import webview_shutdown

runpy.run_path(str(root / "app.py"), run_name="__main__")
