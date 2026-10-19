"""Browser ownership and process cleanup for the Windows downloader."""
import os
import subprocess
from pathlib import Path


def refresh_proxy_environment(environment=None):
    """Reload the saved user proxy when launched by an older Windows process."""
    environment = os.environ if environment is None else environment
    if os.name != "nt":
        return environment
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            proxy, kind = winreg.QueryValueEx(key, "ALL_PROXY")
    except OSError:
        return environment
    if not isinstance(proxy, str):
        return environment
    if kind == winreg.REG_EXPAND_SZ:
        proxy = winreg.ExpandEnvironmentStrings(proxy)
    for name in list(environment):
        if name.upper() == "ALL_PROXY":
            del environment[name]
    if proxy:
        environment["ALL_PROXY"] = proxy
    return environment


class AlreadyRunning(RuntimeError):
    pass


class RunLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("已有下载器正在使用此浏览器，请先停止原来的任务。") from exc
        return self

    def __exit__(self, *exc):
        if self.handle:
            self.handle.close()
            self.handle = None


def stop_process_tree(process):
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        # Limit cleanup to the worker we own, including its Playwright browser.
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW, timeout=15,
        )
    else:
        process.terminate()
    process.wait(timeout=15)


def live_school_page(context, previous=None):
    pages = [page for page in context.pages if not page.is_closed()]
    for page in pages:
        if "/PaiKeCalendar/" in page.url:
            return page
    if previous in pages:
        return previous
    for page in pages:
        if ".shsmu.edu.cn" in page.url:
            return page
    return context.new_page()
