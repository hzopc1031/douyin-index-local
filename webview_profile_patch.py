"""Narrow workaround for pywebview 6.2.1's duplicate Qt persistent profiles."""
import importlib
import logging
from importlib.metadata import version

log = logging.getLogger(__name__)


def install_shared_profile(storage_path):
    """Return False on an unsupported backend/version: caller must use private mode.

    Both windows share one QObject, retained for the process lifetime. No Qt
    object is constructed here; the factory runs on Qt's GUI thread as before.
    """
    try:
        if version('pywebview') != '6.2.1':
            raise RuntimeError('unsupported pywebview version')
        qt = importlib.import_module('webview.platforms.qt')
        # qt.py snapshots storage_path at import, before webview.start sets it.
        qt._profile_storage_path = str(storage_path)
        original = qt.QWebEngineProfile
        if not callable(original):
            raise RuntimeError('Qt WebEngine profile unavailable')
        if getattr(original, '_index_hub_shared', False):
            return True

        class SharedProfile:
            _index_hub_shared = True
            _persistent = None

            def __new__(cls, *args, **kwargs):
                if args and args[0] == 'pywebview':
                    if cls._persistent is None:
                        cls._persistent = original(*args, **kwargs)
                        cls._persistent.setPersistentStoragePath(str(storage_path))
                        cls._persistent.setCachePath(str(storage_path) + '/cache')
                    return cls._persistent
                return original(*args, **kwargs)

        qt.QWebEngineProfile = SharedProfile
        return True
    except Exception:
        log.warning('Persistent Qt profile workaround unavailable; falling back to private mode')
        return False
