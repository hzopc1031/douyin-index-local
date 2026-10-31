"""Open an isolated, temporary Baidu Index window on the cloud desktop."""
import fcntl
import os
from pathlib import Path
from hub_paths import state_dir

ROOT = Path(__file__).resolve().parent

def main():
    os.umask(0o077)
    lock_path = state_dir(ROOT) / "baidu-window.lock"
    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        import webview
        webview.settings["ALLOW_FILE_URLS"] = False
        webview.settings["IGNORE_SSL_ERRORS"] = False
        webview.settings["REMOTE_DEBUGGING_PORT"] = None
        webview.create_window(
            "百度指数 · 官方登录与核对",
            "https://index.baidu.com/",
            width=1250, height=850, x=160, y=50, text_select=True,
        )
        webview.start(private_mode=True, debug=False, http_server=False)

if __name__ == "__main__":
    main()
