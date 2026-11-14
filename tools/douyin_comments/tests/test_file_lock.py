import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from dyc.file_lock import acquire, release


class PortableLockTests(unittest.TestCase):
    def test_windows_locks_and_unlocks_same_byte(self):
        locking = Mock()
        backend = SimpleNamespace(LK_NBLCK=10, LK_UNLCK=20, locking=locking)
        with tempfile.TemporaryFile() as stream:
            stream.write(b'12345')
            stream.flush()
            os.lseek(stream.fileno(), 3, os.SEEK_SET)
            acquire(stream.fileno(), windows=True, backend=backend)
            self.assertEqual(os.lseek(stream.fileno(), 0, os.SEEK_CUR), 0)
            os.lseek(stream.fileno(), 4, os.SEEK_SET)
            release(stream.fileno(), windows=True, backend=backend)
            self.assertEqual(os.lseek(stream.fileno(), 0, os.SEEK_CUR), 0)
        self.assertEqual([call.args[1:] for call in locking.call_args_list], [(10, 1), (20, 1)])

    def test_windows_busy_is_reported_as_blocking_error(self):
        backend = SimpleNamespace(LK_NBLCK=10, locking=Mock(side_effect=OSError('synthetic')))
        with tempfile.TemporaryFile() as stream:
            with self.assertRaises(BlockingIOError):
                acquire(stream.fileno(), windows=True, backend=backend)


if __name__ == '__main__':
    unittest.main()
