"""Real Tk render/interaction checks; no network or school login is exercised."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import statistics
import struct
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import tkinter as tk
from ppt_downloader_app import PptDownloaderApp
from tools.generate_ui_sounds import analyse, SOUNDS, OUTPUT_DIR


def settle(root, seconds=.25):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        root.update()
        time.sleep(.005)


def capture(root, path):
    root.lift()
    settle(root)
    x, y = root.winfo_rootx(), root.winfo_rooty()
    try:
        ImageGrab.grab(bbox=(x, y, x + root.winfo_width(), y + root.winfo_height())).save(path)
    except OSError:
        # Screen capture can be unavailable in a locked/remote Windows session.
        return False
    return True


def check_bounds(menu):
    problems = []
    for tile in menu.home_targets + menu.selection.targets:
        if not tile.winfo_ismapped():
            continue
        boxes = [b for b in tile.text_bounds if b]
        for box in boxes:
            x1, y1, x2, y2 = box
            if x1 < 0 or y1 < 0 or x2 > tile.winfo_width() or y2 > tile.winfo_height():
                problems.append((tile.title, "bounds", box, (tile.winfo_width(), tile.winfo_height())))
        for i, a in enumerate(boxes):
            for b in boxes[i+1:]:
                if min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1]):
                    problems.append((tile.title, "overlap", a, b))
    assert not problems, problems


def one_selected(menu):
    assert sum(t.selected for t in menu.selection.targets) == 1
    assert menu.selection.selected.selected


def interactions(root, app):
    menu = app.menu
    menu.feedback.sound_enabled = True
    menu.feedback.motion_enabled = True
    assert menu.feedback.audio_ready
    assert len(menu.feedback.sounds) == 7
    root.focus_force()
    menu.selection.selected.focus_set()
    root.update()
    initial = menu.selection.selected
    with patch.object(menu.feedback, "play") as sound:
        menu.tiles["date"].set_hover(True)
        assert menu.selection.selected is initial
        menu.tiles["date"].event_generate("<FocusIn>")
        root.update()
        sound.assert_not_called()
        menu.tiles["date"].set_hover(False)
    timings = []
    direction_path = ["Right", "Up", "Right", "Down", "Down", "Left", "Up", "Down", "Right", "Up"] * 3
    selection_changes = 0
    # Patching file open after startup catches interaction-time asset loading.
    with patch("builtins.open", side_effect=AssertionError("file I/O during navigation")):
        for direction in direction_path:
            target = menu.selection.selected
            t0 = time.perf_counter()
            target.event_generate(f"<KeyPress-{direction}>")
            root.update()
            timings.append((time.perf_counter()-t0)*1000)
            selection_changes += menu.selection.selected is not target
            one_selected(menu)
            settle(root, .065)
    assert selection_changes >= 20, selection_changes
    calls = []
    tile = menu.tiles["location"]
    command = tile.command
    tile.command = lambda: calls.append(1)
    tile.mouse_down(None)
    settle(root, .13)
    tile.mouse_up(SimpleNamespace(x=20, y=20))
    assert len(calls) == 1, "long click lost after press animation"
    tile.key_down()
    tile.key_down()
    assert len(calls) == 2, "key autorepeat dispatched twice"
    tile.key_up()
    tile.command = command
    stop = menu.tiles["stop"]
    with patch.object(stop, "command") as action:
        stop.key_down()
        stop.mouse_down(None)
        stop.mouse_up(SimpleNamespace(x=20, y=20))
        action.assert_not_called()
    original = app.selected_date()
    menu.open_view("date")
    settle(root)
    menu.pick_day(20)
    menu.back()
    assert app.selected_date() == original
    menu.open_view("date")
    app.year_var.set("2028")
    app.month_var.set("2")
    app.day_var.set("31")
    menu.date_changed()
    assert app.day_var.get() == "29"
    menu.apply_date()
    assert app.selected_date().isoformat() == "2028-02-29"
    menu.tiles["date"].key_down()
    menu.back_button.key_down()
    assert menu.view == "date", "held Enter bounced between views"
    menu.back_button.key_up()
    menu.back_button.key_down()
    menu.back_button.key_up()
    assert menu.view == "home"
    menu.tiles["date"].key_down()
    assert menu.view == "date", "hidden key-release blocked reopening"
    menu.back_button.key_up()
    menu.back()
    app.year_var.set(str(original.year))
    app.month_var.set(str(original.month))
    app.day_var.set(str(original.day))
    app.on_date_part_changed()
    app.append_log("Found 2 PPT/PPTX link(s).")
    app.append_log("Downloaded: C:/QA/example.pptx")
    app.append_log("Stream download failed for QA, trying page fetch")
    assert app.error_count == 0
    app.append_log("Download failed: 2026-09-03 | QA COURSE | QA FILE | HTTP 404")
    assert app.error_count == 1
    content = app.log.get("1.0", "end")
    menu.open_view("activity")
    menu.open_view("settings")
    menu.back()
    assert app.log.get("1.0", "end") == content
    old_path = app.output_var.get()
    long_path = "C:/课程资料/" + "非常长的中文课程归档目录/" * 6 + "2026学年"
    with patch("ppt_downloader_app.filedialog.askdirectory", return_value=long_path):
        app.choose_output()
    assert app.output_var.get() == long_path
    settle(root)
    assert "..." in menu.tiles["location"].detail
    check_bounds(menu)
    app.output_var.set(old_path)
    app.set_status("任务未完成")
    assert app.console_state_var.get() == "ERROR"
    menu.feedback.motion_enabled = False
    menu.open_view("activity")
    menu.back()
    settle(root)
    assert menu.home.winfo_ismapped() and not menu.detail.winfo_ismapped()
    one_selected(menu)
    menu.feedback.motion_enabled = True
    return {"events": 30, "selection_changes":selection_changes, "input_to_tk_update_ms_median": statistics.median(timings),
            "input_to_tk_update_ms_max": max(timings), "device_output_latency": "not measured"}


def controller_contract(root, app):
    # Existing subprocess args/stdin/terminate contract; no real browser or download.
    process = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None, terminate=lambda: None)
    output = str(ROOT / "ppt_downloads")
    with patch("ppt_downloader_app.subprocess.Popen", return_value=process) as launch, patch("ppt_downloader_app.threading.Thread"), patch.object(app.feedback, "play") as sound:
        app.output_var.set(output)
        app.start_download()
        command = launch.call_args.args[0]
        assert command[1] == "-u"
        assert command[2] == str(ROOT / "download_ppt_by_week.py")
        assert command[3:] == [
            "--date",
            app.selected_date().strftime("%#d/%#m/%Y"),
            "--output",
            output,
        ]
        assert app.menu.running and app.stop_button.enabled and not app.start_button.enabled
        assert not app.menu.tiles["date"].enabled
        settle(root)
        check_bounds(app.menu)
        app.continue_after_login()
        assert process.stdin.getvalue() == "\n"
        assert not app.continue_button.enabled
        app.downloaded_count, app.download_total = 3, 12
        app.download_count_var.set("3 / 12")
        check_bounds(app.menu)
        with patch("ppt_downloader_app.threading.Thread") as stop_thread:
            app.stop_download()
            assert stop_thread.call_args.kwargs["args"] == (process,)
            assert app.stopping
        app.error_count = 1
        app.log_queue.put("__PROCESS_DONE__:0")
        settle(root)
        assert app.console_state_var.get() == "ERROR"
        assert not app.menu.running and not app.stop_button.enabled
        sound.assert_not_called()
        app.menu.open_view("activity")
        settle(root)
        check_bounds(app.menu)
        app.menu.back()
    app.process = None
    app.output_var.set(str(ROOT / "ppt_downloads"))
    app.error_count = app.downloaded_count = app.download_total = 0
    app.download_count_var.set("0 / 0")
    app.set_status("准备就绪")
    app.log.configure(state="normal")
    app.log.delete("1.0", "end")
    app.log.insert("end", "SYSTEM READY\n\n等待任务启动\n")
    app.log.configure(state="disabled")


def log_batch_contract(root, app):
    """A noisy downloader must yield back to Tk instead of owning the event loop."""
    for index in range(61):
        app.log_queue.put(f"Fetching record {index}\n")
    app.drain_log_queue()
    assert app.log_queue.qsize() == 31
    while not app.log_queue.empty():
        app.drain_log_queue()
    assert "Fetching record 60" in app.log.get("1.0", "end")


def waves():
    plot = Image.new("RGB", (1280, 180*7), "#101010")
    draw = ImageDraw.Draw(plot)
    rows = []
    ranges = {"navigate":(25,45), "confirm":(40,70), "back":(30,60), "success":(100,220), "error":(80,140)}
    for i, name in enumerate(SOUNDS):
        path = OUTPUT_DIR / (name + ".wav")
        duration, leading, trailing, peak = analyse(path)
        with wave.open(str(path), "rb") as wav:
            values = struct.unpack("<" + "h"*wav.getnframes(), wav.readframes(wav.getnframes()))
        assert leading <= 1 and trailing <= 48 and peak < .99 and values[-1] == 0
        if name in ranges:
            assert ranges[name][0] <= duration <= ranges[name][1]
        row = {"name":name, "duration_ms":duration, "leading_samples":leading, "trailing_samples":trailing, "peak":peak}
        rows.append(row)
        y = i*180
        draw.text((20,y+10), f"{name} | {duration:.1f} ms | onset: {leading} samples | tail: {trailing} samples", fill="white")
        draw.line((20,y+100,1260,y+100), fill="#555555")
        for x in range(1240):
            segment = values[int(x*len(values)/1240):max(int((x+1)*len(values)/1240),int(x*len(values)/1240)+1)]
            draw.line((20+x,y+100-max(segment)/32768*64,20+x,y+100-min(segment)/32768*64), fill="#78DCCA")
    plot.save(ROOT / "assets/ui_sound_waveforms.png")
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=1.25)
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    root = tk.Tk()
    failures = []
    root.report_callback_exception = lambda *exc: failures.append(str(exc))
    root.tk.call("tk", "scaling", args.scale * 96 / 72)
    app = PptDownloaderApp(root)
    root.maxsize(8192, 8192)
    root.geometry(f"{app.px(1000)}x{app.px(720)}+10+10")
    root.focus_force()
    settle(root, .8)
    report = {"scale_simulated":args.scale, "actual_ui_scale":app.ui_scale}
    check_bounds(app.menu)
    if args.full:
        report["interaction"] = interactions(root, app)
        controller_contract(root, app)
        log_batch_contract(root, app)
        report["waveforms"] = waves()
    sizes = [("normal",1000,720), ("minimum",920,650)]
    for name, width, height in sizes:
        root.geometry(f"{app.px(width)}x{app.px(height)}+10+10")
        settle(root, .5)
        check_bounds(app.menu)
        if args.scale <= 1.25:
            capture(root, ROOT / f"assets/menu_{name}_{int(args.scale*100)}.png")
        for view in ("date", "activity", "settings", "login"):
            app.menu.open_view(view)
            settle(root)
            check_bounds(app.menu)
            if args.full and name == "normal":
                capture(root, ROOT / f"assets/menu_{view}.png")
            app.menu.back()
            settle(root)
    if args.full:
        root.state("zoomed")
        settle(root, .5)
        check_bounds(app.menu)
        capture(root, ROOT / "assets/menu_maximized.png")
        root.state("normal")
        root.geometry("1250x900+10+10")
        app.menu.selection.select(app.menu.tiles["featured"], sound=False)
        app.menu.tiles["featured"].focus_set()
        settle(root)
        capture(root, ROOT / "assets/course_menu_final.png")
    assert not failures, failures
    report["result"] = "passed"
    (ROOT / f"assets/menu_qa_{int(args.scale*100)}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    for job in root.tk.call("after", "info"):
        root.after_cancel(job)
    root.destroy()


if __name__ == "__main__":
    main()
