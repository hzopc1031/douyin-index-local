import unittest

from hub_store import HubError, PLATFORMS, validate_query
from platforms import Placeholder, Registry, registry, require_platform


class PlatformTests(unittest.TestCase):
    def test_register_lookup_and_duplicate(self):
        platforms = Registry()
        adapter = Placeholder({'id': 'sample', 'available': False})
        platforms.register(adapter)
        self.assertIs(platforms.get('sample'), adapter)
        self.assertIsNone(platforms.get('missing'))
        with self.assertRaises(ValueError):
            platforms.register(adapter)

    def test_placeholders_and_unknown(self):
        self.assertEqual([p['id'] for p in PLATFORMS], ['douyin', 'baidu', 'google_trends'])
        self.assertEqual(PLATFORMS, registry.descriptions())
        for identifier in ('baidu', 'google_trends', 'unknown'):
            with self.subTest(identifier=identifier), self.assertRaises(HubError) as error:
                require_platform(identifier)
            self.assertEqual((error.exception.code, error.exception.status), ('platform_unavailable', 422))
            with self.assertRaises(HubError) as error:
                validate_query({'platform': identifier})
            self.assertEqual((error.exception.code, error.exception.status), ('platform_unavailable', 422))
