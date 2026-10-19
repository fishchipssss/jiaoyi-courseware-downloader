import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from downloader_runtime import refresh_proxy_environment


@unittest.skipUnless(os.name == 'nt', 'Windows proxy environment')
class ProxyEnvironmentTests(unittest.TestCase):
    def test_old_parent_process_proxy_is_refreshed(self):
        import winreg
        environment = {'ALL_PROXY': 'http://127.0.0.1:10809', 'NO_PROXY': 'localhost', 'OTHER': 'unchanged'}
        with patch('winreg.OpenKey', return_value=MagicMock()), patch('winreg.QueryValueEx', return_value=('http://127.0.0.1:10808', winreg.REG_SZ)):
            result = refresh_proxy_environment(environment)
        self.assertEqual(result['ALL_PROXY'], 'http://127.0.0.1:10808')
        self.assertEqual(result['NO_PROXY'], 'localhost')
        self.assertEqual(result['OTHER'], 'unchanged')

    def test_no_saved_proxy_preserves_explicit_shell_setting(self):
        environment = {'ALL_PROXY': 'http://127.0.0.1:10810'}
        with patch('winreg.OpenKey', side_effect=FileNotFoundError):
            self.assertEqual(refresh_proxy_environment(environment), environment)
        self.assertEqual(environment['ALL_PROXY'], 'http://127.0.0.1:10810')

    def test_empty_saved_proxy_removes_stale_inherited_setting(self):
        import winreg
        environment = {'ALL_PROXY': 'http://127.0.0.1:10809'}
        with patch('winreg.OpenKey', return_value=MagicMock()), patch('winreg.QueryValueEx', return_value=('', winreg.REG_SZ)):
            self.assertNotIn('ALL_PROXY', refresh_proxy_environment(environment))


if __name__ == '__main__':
    unittest.main()
