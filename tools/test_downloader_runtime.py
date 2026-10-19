import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from downloader_runtime import RunLock, AlreadyRunning, live_school_page, stop_process_tree
from playwright.sync_api import sync_playwright


class RuntimeTests(unittest.TestCase):
    def test_parallel_worker_rejected_and_restart_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.lock'
            with RunLock(path):
                with self.assertRaises(AlreadyRunning):
                    with RunLock(path):
                        self.fail('second worker acquired active lock')
            with RunLock(path):
                pass

    def test_chrome_start_close_restart_and_closed_tab_recovery(self):
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as p:
            for _ in range(2):
                with RunLock(Path(directory) / 'run.lock'):
                    context = p.chromium.launch_persistent_context(
                        str(Path(directory) / 'profile'), channel='chrome', headless=True,
                    )
                    try:
                        previous = context.pages[0]
                        actual = context.new_page()
                        actual.goto('about:blank')
                        previous.close()
                        recovered = live_school_page(context, previous)
                        self.assertFalse(recovered.is_closed())
                        recovered.set_content('<h1>ready</h1>')
                        self.assertEqual(recovered.locator('h1').inner_text(), 'ready')
                    finally:
                        context.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows process cleanup')
    def test_stop_closes_child_process(self):
        import ctypes
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / 'child.pid'
            code = (
                'import subprocess,sys,time; from pathlib import Path; '
                "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                'Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(60)'
            )
            process = subprocess.Popen([sys.executable, '-c', code, str(pidfile)])
            handle = None
            try:
                deadline = time.monotonic() + 10
                while not pidfile.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(pidfile.exists())
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                handle = kernel.OpenProcess(0x100000, False, int(pidfile.read_text()))
                self.assertTrue(handle)
                stop_process_tree(process)
                self.assertIsNotNone(process.poll())
                self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
            finally:
                stop_process_tree(process)
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == '__main__':
    unittest.main()
