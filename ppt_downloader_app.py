import calendar
import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from tkinter import END, Label, StringVar, Tk, Toplevel, ttk
from tkinter import filedialog, messagebox
from course_menu_ui import TileMenuUI

from download_ppt_by_week import DEFAULT_OUTPUT_ROOT, school_week_end, school_week_start
from downloader_runtime import RunLock, AlreadyRunning, stop_process_tree, refresh_proxy_environment


APP_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = APP_DIR / "download_ppt_by_week.py"
ICON_PATH = APP_DIR / "assets" / "ppt_downloader_icon_luxury_gold_bright.ico"
PREFERENCES_PATH = APP_DIR / "ui_preferences.json"
UI_SOUND_DIR = APP_DIR / "assets" / "ui_sounds"



class UiFeedback:
    """Preloaded, non-blocking UI feedback kept separate from downloader work."""

    def __init__(self):
        preferences = self.load_preferences()
        self.sound_enabled = preferences.get("ui_sounds", False)
        animations = ctypes.c_int(1)
        if os.name == "nt":
            ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(animations), 0)
        self.motion_enabled = not preferences.get("reduced_motion", not bool(animations.value))
        self._last_navigation_sound = 0.0
        self.audio_ready = False
        self.sounds = {}
        self.channels = {}
        self._load_sounds()

    @staticmethod
    def load_preferences():
        try:
            return json.loads(PREFERENCES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save_preferences(self):
        payload = {
            "ui_sounds": self.sound_enabled,
            "reduced_motion": not self.motion_enabled,
        }
        try:
            PREFERENCES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _load_sounds(self):
        """Initialise one tiny mixer and decode every asset before interaction."""
        try:
            os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
            import pygame

            pygame.mixer.pre_init(frequency=48_000, size=-16, channels=1, buffer=256)
            pygame.mixer.init()
            pygame.mixer.set_num_channels(6)
            volumes = {
                "navigate": 0.30,
                "confirm": 0.46,
                "back": 0.34,
                "task_start": 0.42,
                "success": 0.45,
                "warning": 0.40,
                "error": 0.43,
            }
            self.sounds = {
                name: pygame.mixer.Sound(str(UI_SOUND_DIR / f"{name}.wav"))
                for name in volumes
            }
            for name, sound in self.sounds.items():
                sound.set_volume(volumes[name])
            self.channels = {
                "navigate": pygame.mixer.Channel(0),
                "confirm": pygame.mixer.Channel(1),
                "back": pygame.mixer.Channel(1),
                "task_start": pygame.mixer.Channel(1),
                "success": pygame.mixer.Channel(3),
                "warning": pygame.mixer.Channel(4),
                "error": pygame.mixer.Channel(4),
            }
            self.audio_ready = True
        except Exception:
            # Audio is optional. A missing device must never block downloads or the UI.
            self.audio_ready = False
            self.sounds = {}
            self.channels = {}

    def play(self, name: str):
        if not self.sound_enabled or not self.audio_ready or name not in self.sounds:
            return
        now = time.monotonic()
        if name == "navigate" and now - self._last_navigation_sound < 0.045:
            return
        if name == "navigate":
            self._last_navigation_sound = now
        else:
            self.channels["navigate"].stop()
        channel = self.channels[name]
        channel.stop()
        channel.play(self.sounds[name])




class Tooltip:
    """Expose a shortened path on either mouse hover or keyboard focus."""

    def __init__(self, widget, text_getter):
        self.widget = widget
        self.text_getter = text_getter
        self.window = None
        self.after_id = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<FocusIn>", self.schedule, add="+")
        widget.bind("<FocusOut>", self.hide, add="+")
        widget.bind("<Unmap>", self.hide, add="+")

    def schedule(self, _event=None):
        self.hide()
        self.after_id = self.widget.after(550, self.show)

    def show(self):
        self.after_id = None
        if not self.widget.winfo_ismapped():
            return
        text = self.text_getter()
        if not text:
            return
        window = Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.attributes("-topmost", True)
        label = Label(
            window,
            text=text,
            justify="left",
            background="#171717",
            foreground="#F3F7FF",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            font=("Microsoft YaHei UI", 9),
            wraplength=550,
        )
        label.pack()
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        window.update_idletasks()
        x = min(x, self.widget.winfo_screenwidth() - window.winfo_reqwidth() - 10)
        y = min(y, self.widget.winfo_screenheight() - window.winfo_reqheight() - 40)
        window.wm_geometry(f"+{max(0, x)}+{max(0, y)}")
        self.window = window

    def hide(self, _event=None):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.window:
            self.window.destroy()
            self.window = None


def enable_high_dpi():
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


enable_high_dpi()


class PptDownloaderApp:
    def __init__(self, root: Tk):
        self.root = root
        self.ui_scale = max(1.0, min(root.winfo_fpixels("1i") / 96.0, 2.0))
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        window_width = min(self.px(1000), screen_width - self.px(40))
        window_height = min(self.px(720), screen_height - self.px(60))
        self.root.title("课程课件下载器")
        self.root.geometry(f"{window_width}x{window_height}")
        self.root.minsize(min(self.px(920), screen_width - self.px(40)), min(self.px(650), screen_height - self.px(60)))
        self.root.configure(bg="#080808")
        if ICON_PATH.exists():
            self.root.iconbitmap(default=str(ICON_PATH))

        today = date.today()
        self.year_var = StringVar(value=str(today.year))
        self.month_var = StringVar(value=str(today.month))
        self.day_var = StringVar(value=str(today.day))
        self.week_var = StringVar()
        self.week_start_var = StringVar()
        self.week_end_var = StringVar()
        self.status_var = StringVar(value="准备就绪")
        self.console_state_var = StringVar(value="READY")
        self.output_var = StringVar(value=DEFAULT_OUTPUT_ROOT)
        self.location_short_var = StringVar()
        self.download_count_var = StringVar(value="0 / 0")
        self.downloaded_count = 0
        self.download_total = 0
        self.error_count = 0
        self.feedback = UiFeedback()

        self.process = None
        self.stopping = False
        self.stop_thread = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.build_ui()
        self.output_var.trace_add("write", lambda *_args: self.refresh_location_display())
        self.refresh_days()
        self.refresh_week_label()
        self.refresh_location_display()
        self.root.after(100, self.drain_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self.close_app)

    def px(self, value):
        return max(1, int(round(value * self.ui_scale)))

    def build_ui(self):
        self.menu = TileMenuUI(self, Tooltip)

    def bind_input_feedback(self, widget):
        widget.bind("<FocusIn>", lambda _event: self.feedback.play("navigate"), add="+")
        if isinstance(widget, ttk.Combobox):
            widget.bind("<<ComboboxSelected>>", lambda _event: self.feedback.play("navigate"), add="+")

    def sound_label(self):
        return "声音：开" if self.feedback.sound_enabled else "声音：关"

    def motion_label(self):
        return "动效：开" if self.feedback.motion_enabled else "动效：简"

    def toggle_sounds(self):
        self.feedback.sound_enabled = not self.feedback.sound_enabled
        self.feedback.save_preferences()
        if self.feedback.sound_enabled:
            self.feedback.play("confirm")
        self.menu.request_refresh(immediate=True)

    def toggle_motion(self):
        self.feedback.motion_enabled = not self.feedback.motion_enabled
        self.feedback.save_preferences()
        self.menu.request_refresh(immediate=True)

    def show_preferences(self):
        self.menu.open_view("settings")


    @staticmethod
    def abbreviate_path(value: str, limit: int = 24) -> str:
        if len(value) <= limit:
            return value
        parts = Path(value).parts
        if len(parts) >= 2:
            candidate = f"{parts[0]}...\\{parts[-1]}"
            if len(candidate) <= limit:
                return candidate
        return value[: max(1, limit - 3)] + "..."

    def refresh_location_display(self):
        self.location_short_var.set(self.abbreviate_path(self.output_var.get()))

    def set_mission_running(self, running: bool):
        self.menu.set_running(running)

    def set_status(self, text: str):
        if "未完成" in text or "失败" in text:
            console_state = "ERROR"
        elif "停止" in text:
            console_state = "STOPPED"
        elif "完成" in text:
            console_state = "COMPLETE"
        elif "等待" in text:
            console_state = "AUTH"
        elif "启动" in text:
            console_state = "STARTING"
        elif "下载" in text or "获取" in text:
            console_state = "DOWNLOADING"
        else:
            console_state = "READY"
        self.console_state_var.set(console_state)
        self.status_var.set(text)
        self.menu.request_refresh(immediate=True)



    def selected_date(self) -> date:
        return date(int(self.year_var.get()), int(self.month_var.get()), int(self.day_var.get()))

    def on_date_part_changed(self, _event=None):
        self.refresh_days()
        self.refresh_week_label()

    def refresh_days(self):
        year = int(self.year_var.get())
        month = int(self.month_var.get())
        max_day = calendar.monthrange(year, month)[1]
        current_day = min(int(self.day_var.get()), max_day)
        self.day_box.configure(values=[str(day) for day in range(1, max_day + 1)])
        self.day_var.set(str(current_day))

    def refresh_week_label(self):
        selected = self.selected_date()
        week_start = school_week_start(selected)
        week_end = school_week_end(selected)
        self.week_start_var.set(f"{week_start:%m.%d}")
        self.week_end_var.set(f"{week_end:%m.%d}")
        self.week_var.set(f"{week_start:%m.%d} — {week_end:%m.%d}")

    def choose_output(self):
        chosen = filedialog.askdirectory(initialdir=self.output_var.get() or str(APP_DIR))
        if chosen:
            self.output_var.set(chosen)

    def append_log(self, text: str):
        lowered = text.lower()
        if re.match(r"\s*download failed(?:\s*\[\d+\])?:", lowered):
            self.error_count += 1
        if "downloaded:" in lowered:
            tag, event_type = "success", "SUCCESS"
            self.downloaded_count += 1
            self.download_count_var.set(f"{self.downloaded_count} / {self.download_total or '?'}")
        elif "done." in lowered or "finished with code 0" in lowered:
            tag, event_type = "success", "SYSTEM"
        elif "trying" in lowered or "retry" in lowered:
            tag, event_type = "warning", "WARNING"
        elif any(token in lowered for token in ("failed", "error", "traceback", "http 4", "http 5")):
            tag, event_type = "error", "ERROR"
        elif "login" in lowered or "登录" in text:
            tag, event_type = "running", "AUTH"
        elif any(token in lowered for token in ("starting", "fetching", "login", "found", "target date week")):
            tag, event_type = "running", "SYSTEM"
        else:
            tag, event_type = "muted", "SYSTEM"
        found = re.search(r"Found\s+(\d+)\s+PPT", text, re.IGNORECASE)
        summary = re.search(r"Download summary: success=(\d+) failed=(\d+)", text)
        if summary:
            self.downloaded_count, self.error_count = map(int, summary.groups())
            self.download_total = self.downloaded_count + self.error_count
            self.download_count_var.set(f"{self.downloaded_count} / {self.download_total}")
        if found:
            self.download_total = int(found.group(1))
            self.download_count_var.set(f"{self.downloaded_count} / {self.download_total}")

        message = " ".join(part.strip() for part in text.splitlines() if part.strip())
        if not message:
            return
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert(END, f"{timestamp}   {event_type:<10} {message}\n", tag)
        self.log.see(END)
        self.log.configure(state="disabled")
        if "press enter here" in lowered and self.menu.running:
            self.continue_button.set_state("normal")
            self.set_status("等待网页登录")
        self.menu.request_refresh()

    def start_download(self):
        if self.stopping:
            return
        if self.process and self.process.poll() is None:
            messagebox.showinfo("正在下载", "当前已有一个下载任务正在运行。")
            return

        selected = self.selected_date()
        date_arg = f"{selected.day}/{selected.month}/{selected.year}"
        output_dir = Path(self.output_var.get()).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)

        command = [
            sys.executable,
            "-u",
            str(SCRIPT_PATH),
            "--date",
            date_arg,
            "--output",
            str(output_dir),
        ]

        self.downloaded_count = 0
        self.download_total = 0
        self.error_count = 0
        self.download_count_var.set("0 / 0")
        self.append_log("开始下载：" + date_arg)
        self.set_status("正在启动任务")
        self.set_mission_running(True)
        self.start_button.set_variant("running")
        self.start_button.set_text("任务运行中  //  RUNNING")
        self.start_button.set_state("disabled")
        self.continue_button.set_state("disabled")
        self.stop_button.set_state("normal")
        self.menu.request_refresh(immediate=True)

        env = refresh_proxy_environment(os.environ.copy())
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.process = subprocess.Popen(
                command, cwd=str(APP_DIR), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except OSError as exc:
            self.log_queue.put(f"启动失败：{exc}")
            self.log_queue.put("__PROCESS_DONE__:1")
            return
        threading.Thread(target=self.read_process_output, args=(self.process,), daemon=True).start()

    def read_process_output(self, process):
        assert process and process.stdout
        for line in process.stdout:
            self.log_queue.put(line)
        return_code = process.wait()
        self.log_queue.put(f"\n任务结束，返回代码：{return_code}\n")
        self.log_queue.put(f"__PROCESS_DONE__:{return_code}")

    def drain_log_queue(self):
        processed = 0
        try:
            while processed < 30:
                line = self.log_queue.get_nowait()
                processed += 1
                if line.startswith("__PROCESS_DONE__:"):
                    return_code = int(line.rsplit(":", 1)[-1])
                    self.start_button.set_variant("primary")
                    self.start_button.set_text("下载本周课件   >")
                    self.start_button.set_state("normal")
                    self.continue_button.set_state("disabled")
                    self.stop_button.set_state("disabled")
                    self.set_mission_running(False)
                    self.stopping = False
                    if return_code == 0 and self.error_count == 0:
                        self.set_status("下载完成")
                    else:
                        self.set_status("任务未完成")
                else:
                    self.append_log(line)
        except queue.Empty:
            pass
        self.root.after(35 if processed else 120, self.drain_log_queue)

    def continue_after_login(self):
        if not self.process or not self.process.stdin:
            return
        if self.process.poll() is not None:
            messagebox.showinfo("任务未运行", "当前下载任务已经结束，请重新点击“下载本周课件”。")
            self.start_button.set_variant("primary")
            self.start_button.set_text("下载本周课件   >")
            self.start_button.set_state("normal")
            self.continue_button.set_state("disabled")
            self.stop_button.set_state("disabled")
            self.set_mission_running(False)
            return
        try:
            self.append_log("已继续，正在获取课表数据...\n")
            self.process.stdin.write("\n")
            self.process.stdin.flush()
            self.set_status("正在下载课件")
            self.continue_button.set_state("disabled")
            self.menu.request_refresh(immediate=True)
        except OSError as exc:
            messagebox.showerror("无法继续", str(exc))

    def stop_download(self):
        if self.process and self.process.poll() is None and not self.stopping:
            self.stopping = True
            self.stop_button.set_state("disabled")
            self.continue_button.set_state("disabled")
            self.set_status("正在关闭下载浏览器")
            self.stop_thread = threading.Thread(target=stop_process_tree, args=(self.process,), daemon=True)
            self.stop_thread.start()

    def close_app(self):
        if self.stop_thread and self.stop_thread.is_alive():
            self.root.after(150, self.close_app)
            return
        if self.process and self.process.poll() is None:
            self.stop_download()
            self.root.after(150, self.close_app)
            return
        self.root.destroy()

    def open_downloads(self):
        output_dir = Path(self.output_var.get()).expanduser()
        output_dir.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(output_dir)])


def main():
    try:
        with RunLock(APP_DIR / ".downloader-app.lock"):
            root = Tk()
            app = PptDownloaderApp(root)
            root.mainloop()
    except AlreadyRunning:
        root = Tk()
        root.withdraw()
        messagebox.showinfo("下载器已打开", "下载器已经在运行，请使用任务栏中的课件下载器窗口。")
        root.destroy()


if __name__ == "__main__":
    main()
