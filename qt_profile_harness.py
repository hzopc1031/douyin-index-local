"""Local-only subprocess fixture for test_webview_startup (no real login)."""
import json
import os
import sys
import threading
import time
import webview
from webview_profile_patch import install_shared_profile

if sys.argv[4] == 'patched' and not install_shared_profile(sys.argv[2]):
    sys.exit(3)
control = webview.create_window('control', html='<html><body>fixture control</body></html>')
page = webview.create_window('official fixture', sys.argv[1])

def check():
    deadline = time.monotonic() + 20
    for window in (control, page):
        if not window.events.loaded.wait(max(0, deadline - time.monotonic())):
            os._exit(4)
    value = page.evaluate_js("localStorage.getItem('synthetic')")
    if sys.argv[3] == '1':
        page.evaluate_js("localStorage.setItem('synthetic', 'fixture-only')")
    elif value != 'fixture-only':
        os._exit(5)
    print(json.dumps({'loaded': True, 'previous': value}), flush=True)
    # Allow Chromium to flush storage, then close normally for the next cycle.
    time.sleep(1)
    page.destroy()
    control.destroy()

webview.start(check, private_mode=False, storage_path=sys.argv[2], http_server=False, debug=False, gui='qt')
