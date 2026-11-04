import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from baidu_browser import prepare_profile
from browser_profile import ProfileError


class ProfileTests(unittest.TestCase):
    def test_profile_permissions_and_rejected_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(root / 'profile')}):
                path = prepare_profile(root)
                self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            (root / 'link').symlink_to(root / 'profile', target_is_directory=True)
            for path in (root / 'link' / 'child', root / '.runtime' / 'profile', root / 'results' / 'profile', root / '.cloud-state' / 'profile'):
                with self.subTest(path=path), patch.dict(os.environ, {'INDEX_HUB_BAIDU_PROFILE_DIR': str(path)}):
                    with self.assertRaises(ProfileError):
                        prepare_profile(root)
