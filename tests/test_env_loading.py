import os
import unittest
from unittest.mock import patch

import aio_scraper as a


class EnvironmentLoadingTests(unittest.TestCase):
    def test_last_file_entry_wins_without_overriding_exported_variables(self):
        content = 'KEY=old\nexport KEY="new"\nEXPORTED=file\n'
        with patch.dict(os.environ, {'EXPORTED': 'process'}, clear=True), \
                patch.object(a, '_ENV_LOADED', False), \
                patch.object(a.Path, 'is_file', return_value=True), \
                patch.object(a.Path, 'read_text', return_value=content):
            a._load_env_file()
            self.assertEqual(os.environ['KEY'], 'new')
            self.assertEqual(os.environ['EXPORTED'], 'process')
            os.environ['KEY'] = 'later'
            a._load_env_file()
            self.assertEqual(os.environ['KEY'], 'later')


if __name__ == '__main__':
    unittest.main()
