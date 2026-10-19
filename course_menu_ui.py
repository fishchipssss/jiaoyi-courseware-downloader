"""Tkinter tile-menu presentation. The downloader controller owns all task work."""

from __future__ import annotations

import calendar
from datetime import date
import time
import tkinter as tk
from tkinter import ttk
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, ImageTk


BLACK = "#080808"
SURFACE = "#171717"
WHITE = "#F7F7F4"
GRAY = "#A8A8AD"
YELLOW = "#F7C94B"
ASSETS = Path(__file__).resolve().parent / "assets"


def ellipsize(value, font, width):
    if font.measure(value) <= max(1, width):
        return value
    while value and font.measure(value + "...") > width:
        value = value[:-1]
    return value + "..."


class SelectionController:
    def __init__(self, feedback):
        self.feedback = feedback
        self.selected = None
        self.targets = []

    def select(self, tile, sound=True):
        if tile is self.selected or not tile.enabled:
            return
        old, self.selected = self.selected, tile
        if old:
            old.selected = False
            old.cancel_edge()
            old.draw_edge()
        tile.selected = True
        tile.begin_edge()
        tile.menu.context_var.set(tile.menu.context_for(tile))
        if sound:
            self.feedback.play("navigate")

    def move(self, direction):
        candidates = [t for t in self.targets if t.enabled and t.winfo_ismapped()]
        if not candidates:
            return "break"
        if self.selected not in candidates:
            target = candidates[0]
        else:
            current = self.selected
            cx, cy = current.center()
            dx, dy = {"Left": (-1, 0), "Right": (1, 0), "Up": (0, -1), "Down": (0, 1)}[direction]
            scored = []
            for tile in candidates:
                x, y = tile.center()
                forward = (x - cx) * dx + (y - cy) * dy
                sideways = abs((x - cx) * dy - (y - cy) * dx)
                if tile is not current and forward > 1:
                    scored.append((forward + sideways * 2.4, tile))
            if not scored:
                return "break"
            target = min(scored, key=lambda pair: pair[0])[1]
        self.select(target)
        target.focus_set()
        return "break"


class SelectableTile(tk.Canvas):
    """Cached artwork, one persistent selection and bounded input animations."""

    def __init__(self, parent, menu, title, command, *, eyebrow="", detail="", accent=YELLOW,
                 fill=SURFACE, art=None, featured=False, compact=False, height=1):
        super().__init__(parent, width=1, height=height, bg=BLACK, bd=0,
                         highlightthickness=0, takefocus=True, cursor="hand2")
        self.menu, self.command = menu, command
        self.title, self.eyebrow, self.detail = title, eyebrow, detail
        self.accent, self.fill, self.art = accent, fill, art
        self.featured, self.compact = featured, compact
        self.enabled, self.selected, self.hovered, self.pressed = True, False, False, False
        self.variant = "normal"
        self.edge_progress = 1.0
        self._edge_job = self._press_job = self._resize_job = None
        self._photo_key, self.photo, self.pressed_photo = None, None, None
        self._key_down = False
        self._mouse_down = False
        self.bind("<Configure>", self._resized)
        self.bind("<Enter>", lambda _e: self.set_hover(True))
        self.bind("<Leave>", lambda _e: self.set_hover(False))
        # Windows can move focus during redraws or after a browser returns. That
        # must update the visible selection silently, not sound like user input.
        self.bind("<FocusIn>", lambda _e: self.menu.selection.select(self, sound=False))
        self.bind("<ButtonPress-1>", self.mouse_down)
        self.bind("<ButtonRelease-1>", self.mouse_up)
        self.bind("<KeyPress-Return>", self.key_down)
        self.bind("<KeyRelease-Return>", self.key_up)
        self.bind("<KeyPress-space>", self.key_down)
        self.bind("<KeyRelease-space>", self.key_up)
        for direction in ("Up", "Down", "Left", "Right"):
            self.bind(f"<{direction}>", lambda _e, d=direction: self.menu.selection.move(d))
        self.bind("<Destroy>", self._destroyed)

    def _destroyed(self, event):
        if event.widget is self:
            for job in (self._edge_job, self._press_job, self._resize_job):
                if job:
                    try:
                        self.after_cancel(job)
                    except tk.TclError:
                        # A parent may already have cancelled this short UI timer.
                        pass

    def center(self):
        return (self.winfo_rootx() + self.winfo_width() / 2,
                self.winfo_rooty() + self.winfo_height() / 2)

    def set_state(self, state):
        self.enabled = state != "disabled"
        self.configure(takefocus=self.enabled, cursor="hand2" if self.enabled else "arrow")
        self.pressed = False
        self.draw()

    def set_variant(self, variant):
        self.variant = variant
        self.draw()

    def set_text(self, text):
        # Controller button messages become state detail; featured identity stays stable.
        if self.featured:
            self.detail = text
        else:
            self.title = text
        self.draw()

    def set_detail(self, detail):
        if detail != self.detail:
            self.detail = detail
            self.draw()

    def set_hover(self, hovered):
        self.hovered = hovered
        self.draw_edge()
        if hovered and self.enabled:
            self.menu.context_var.set(self.menu.context_for(self))

    def _resized(self, _event=None):
        if self._resize_job:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(30, self.draw)

    def cancel_edge(self):
        if self._edge_job:
            self.after_cancel(self._edge_job)
            self._edge_job = None

    def begin_edge(self):
        self.cancel_edge()
        self._edge_start = time.perf_counter()
        self.edge_progress = 0.0 if self.menu.feedback.motion_enabled else 1.0
        self.animate_edge()

    def animate_edge(self):
        self._edge_job = None
        self.edge_progress = (min(1.0, (time.perf_counter() - self._edge_start) / .09)
                              if self.menu.feedback.motion_enabled else 1.0)
        self.draw_edge()
        if self.selected and self.edge_progress < 1:
            self._edge_job = self.after(12, self.animate_edge)

    def mouse_down(self, _event):
        if self.enabled:
            self._mouse_down = True
            self.menu.selection.select(self, sound=False)
            self.focus_set()
            self.begin_press()

    def mouse_up(self, event):
        valid = self.enabled and self._mouse_down and 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._mouse_down = False
        self.release_press()
        if valid:
            self.command()

    def key_down(self, _event=None):
        if self.enabled and not self.menu.activation_held:
            self._key_down = True
            self.menu.activation_held = True
            self.begin_press()
            # Execute on key-down, without waiting for animation or audio completion.
            self.command()
        return "break"

    def key_up(self, _event=None):
        self.menu.release_activation()
        self.release_press()
        return "break"

    def begin_press(self):
        self.pressed = True
        self.menu.feedback.play("confirm")
        self.draw()
        if self._press_job:
            self.after_cancel(self._press_job)
        self._press_job = self.after(80, self.release_visual)

    def release_visual(self):
        self._press_job = None
        self.pressed = False
        self.draw()

    def release_press(self):
        if self._press_job:
            self.after_cancel(self._press_job)
            self._press_job = None
        self.pressed = False
        self.draw()

    def _image(self, width, height):
        key = (width, height)
        if key == self._photo_key:
            return self.photo
        source = self.menu.artwork.get(self.art)
        if source is None:
            return None
        picture = ImageOps.fit(source, (width, height), method=Image.Resampling.LANCZOS,
                               centering=(.61, .48) if self.featured else (.5, .40)).convert("RGBA")
        overlay = Image.new("RGBA", picture.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for y in range(height):
            fraction = y / max(1, height - 1)
            # Artwork remains visible; the bottom alone carries dense text.
            alpha = int(235 * max(0, (fraction - .28) / .72) ** 1.25)
            draw.line((0, y, width, y), fill=(0, 0, 0, alpha))
        composited = Image.alpha_composite(picture, overlay)
        self.photo = ImageTk.PhotoImage(composited)
        self.pressed_photo = ImageTk.PhotoImage(composited.resize((max(1, round(width*.985)), max(1, round(height*.985))), Image.Resampling.LANCZOS))
        self._photo_key = key
        return self.photo

    def draw(self):
        self._resize_job = None
        if not self.winfo_exists():
            return
        self.delete("content")
        s, f = self.menu.px, self.menu.font
        w, h = max(4, self.winfo_width()), max(4, self.winfo_height())
        inset = max(1, round(min(w, h) * .0075)) if self.pressed and self.menu.feedback.motion_enabled else 0
        fill = self.fill if self.enabled or self.featured else "#151515"
        self.create_rectangle(3 + inset, 3 + inset, w - 3 - inset, h - 3 - inset,
                              fill=fill, outline="", tags="content")
        picture = self._image(max(1, w - 6), max(1, h - 6))
        if picture:
            shown = self.pressed_photo if self.pressed and self.menu.feedback.motion_enabled else picture
            self.create_image(w / 2, h / 2, image=shown, anchor="center", tags="content")
        color = WHITE if self.enabled or self.featured else "#737377"
        pad = s(22 if self.featured else 18)
        self.text_bounds = []

        def text(x, y, value, size=14, bold=False, tint=color, anchor="nw", wrap=False):
            font = f(size, bold)
            shown = value if wrap else ellipsize(value, font, w - 2 * pad)
            item = self.create_text(x, y + inset, text=shown, fill=tint, font=font,
                                    anchor=anchor, width=(w - 2 * pad if wrap else 0), tags="content")
            self.text_bounds.append(self.bbox(item))

        if self.compact:
            text(w / 2, h / 2, self.title, 14, True, anchor="center")
        elif self.featured:
            status, status_color = self.menu.status()
            self.create_rectangle(pad, pad, pad + s(106), pad + s(28), fill=status_color, outline="", tags="content")
            text(pad + s(9), pad + s(6), status, 11, True, "#090909")
            block = min(s(217), h - s(78))
            top = h - block
            text(pad, top, "WEEKLY MATERIALS", 11, True, "#F7C94B")
            text(pad, top + s(25), "本周课件", 36, True)
            text(pad, top + s(78), self.menu.app.week_var.get(), 24, True)
            text(pad, top + s(118), self.menu.app.status_var.get() if self.menu.running else "一键下载本周课程资料", 14, False, "#D8D8DD")
            label = self.menu.app.download_count_var.get() if self.menu.running else "下载本周课件  →"
            text(pad, h - s(42), label, 16, True, YELLOW)
            if self.menu.running:
                total = self.menu.app.download_total
                done = self.menu.app.downloaded_count
                fraction = min(1, done / total) if total else 0
                self.create_rectangle(pad, h - s(14), w - pad, h - s(10), fill="#373737", outline="", tags="content")
                if fraction:
                    self.create_rectangle(pad, h - s(14), pad + (w - pad * 2) * fraction, h - s(10), fill=status_color, outline="", tags="content")
        elif picture:
            if h >= s(135):
                text(pad, pad, self.eyebrow, 10, True, self.accent)
            text(pad, h - s(80), self.title, 24, True)
            text(pad, h - s(39), self.detail, 14, False, "#E5E5E7")
        else:
            text(pad, s(15), self.eyebrow, 10, True, self.accent if self.enabled else "#707074")
            if h < s(105):
                text(pad, s(37), self.title, 20, True)
            else:
                text(pad, max(s(38), h - s(67)), self.title, 21, True)
                text(pad, h - s(29), self.detail, 12, False, "#D2D2D6" if self.enabled else "#77777B")
            self.create_line(w - pad - s(18), s(23), w - pad, s(23), fill=self.accent if self.enabled else "#454545", width=2, tags="content")
        self.draw_edge()

    def draw_edge(self):
        self.delete("edge")
        w, h = self.winfo_width(), self.winfo_height()
        if self.selected:
            inset = 3 if self.pressed else 1
            points = [(inset, inset), (w-inset-1, inset), (w-inset-1, h-inset-1), (inset, h-inset-1), (inset, inset)]
            remaining = (2 * (w + h - 4 * inset - 2)) * self.edge_progress
            for a, b in zip(points, points[1:]):
                length = abs(b[0]-a[0]) + abs(b[1]-a[1])
                if remaining <= 0 or not length:
                    break
                t = min(1, remaining / length)
                self.create_line(*a, a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, fill="#FFFFFF", width=2, tags="edge")
                remaining -= length
        elif self.hovered and self.enabled:
            self.create_rectangle(1, 1, w-2, h-2, outline="#818185", width=1, tags="edge")


class TileMenuUI:
    def __init__(self, app, tooltip_class):
        import tkinter.font as tkfont

        self.app, self.root, self.feedback = app, app.root, app.feedback
        self.px = app.px
        self._tkfont = tkfont
        self._fonts = {}
        self.running = False
        self.view = "home"
        self._transition = None
        self._refresh_job = None
        self._featured_visual = None
        self._date_snapshot = None
        self._saved_selection = None
        self.activation_held = False
        self.selection = SelectionController(self.feedback)
        self.artwork = {}
        for name, filename in (("sunset", "open_world_sunset_header.png"), ("date", "menu_dates.png"), ("archive", "menu_archive.png")):
            path = ASSETS / filename
            if path.exists():
                with Image.open(path) as source:
                    self.artwork[name] = source.convert("RGB")
        self.context_var = tk.StringVar(value="准备就绪")
        self._build_shell()
        self._build_home(tooltip_class)
        self._build_details()
        self.home_targets = list(self.tiles.values())
        self.selection.targets = self.home_targets
        self.root.bind("<Escape>", self.back)
        self.root.bind("<KeyRelease>", self._release_hidden_key, add="+")
        for variable in (app.week_var, app.output_var, app.status_var, app.download_count_var):
            variable.trace_add("write", lambda *_args: self.request_refresh())
        self.selection.select(self.tiles["featured"], sound=False)
        self.root.after_idle(self.tiles["featured"].focus_set)
        self.refresh()

    def font(self, size, bold=False, family="Microsoft YaHei UI"):
        key = (size, bold, family)
        if key not in self._fonts:
            self._fonts[key] = self._tkfont.Font(self.root, family=family, size=-self.px(size), weight="bold" if bold else "normal")
        return self._fonts[key]

    def label(self, parent, text="", *, size=14, bold=False, fg=WHITE, variable=None):
        options = {"textvariable": variable} if variable else {"text": text}
        return tk.Label(parent, **options, bg=parent.cget("bg"), fg=fg, font=self.font(size, bold), anchor="w", bd=0)

    def _build_shell(self):
        s = self.px
        self.root.configure(bg=BLACK)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        top = tk.Frame(self.root, bg=BLACK, padx=s(32), pady=s(16))
        top.grid(row=0, column=0, sticky="ew")
        self.label(top, "课件下载器", size=34, bold=True).pack(side="left")
        nav = tk.Frame(top, bg=BLACK)
        nav.pack(side="right", pady=s(10))
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("Menu.TButton", font=self.font(14), padding=(s(18), s(7)), background=BLACK, foreground=GRAY, borderwidth=0, focuscolor=WHITE)
        style.map("Menu.TButton", background=[("pressed", "#FFFFFF"), ("active", "#222222")], foreground=[("pressed", "#080808"), ("active", WHITE)])
        style.configure("Active.Menu.TButton", background=WHITE, foreground=BLACK)
        style.configure("Menu.Vertical.TScrollbar", background="#333333", troughcolor="#111111", bordercolor="#111111", arrowcolor=GRAY, lightcolor="#333333", darkcolor="#333333")
        self.nav = {}
        for key, title in (("home", "课件"), ("activity", "任务活动"), ("settings", "设置")):
            button = ttk.Button(nav, text=title, style="Menu.TButton", command=lambda k=key: self.navigate_view(k))
            button.pack(side="left", padx=(s(6), 0))
            self.nav[key] = button
        self.host = tk.Frame(self.root, bg=BLACK)
        self.host.grid(row=1, column=0, sticky="nsew", padx=s(32), pady=(s(8), s(12)))
        self.host.rowconfigure(0, weight=1)
        self.host.columnconfigure(0, weight=1)
        footer = tk.Frame(self.root, bg=BLACK, padx=s(32), pady=s(12))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.label(footer, variable=self.context_var, fg=GRAY, size=12).grid(row=0, column=0, sticky="w")
        self.footer_status = self.label(footer, "SYSTEM READY", fg=YELLOW, size=11, bold=True)
        self.footer_status.grid(row=0, column=1, sticky="e")

    def _build_home(self, tooltip):
        s = self.px
        self.home = tk.Frame(self.host, bg=BLACK)
        self.home.grid(row=0, column=0, sticky="nsew")
        for col, weight in enumerate((38, 32, 30)):
            self.home.columnconfigure(col, weight=weight, uniform="tiles")
        self.home.rowconfigure(0, weight=1)
        center = tk.Frame(self.home, bg=BLACK)
        center.grid(row=0, column=1, sticky="nsew", padx=s(12))
        center.columnconfigure(0, weight=1)
        center.rowconfigure((0, 1), weight=1, uniform="center")
        right = tk.Frame(self.home, bg=BLACK)
        right.grid(row=0, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        for i, weight in enumerate((28, 25, 28, 19)):
            right.rowconfigure(i, weight=weight, uniform="right")
        self.tiles = {
            "featured": SelectableTile(self.home, self, "本周课件", self.app.start_download, featured=True, art="sunset"),
            "date": SelectableTile(center, self, "选择日期", lambda: self.open_view("date"), eyebrow="SELECT DATE", art="date", accent="#ACF6F5"),
            "location": SelectableTile(center, self, "保存位置", self.app.choose_output, eyebrow="LOCAL ARCHIVE", art="archive"),
            "folder": SelectableTile(right, self, "打开文件夹", self.app.open_downloads, eyebrow="YOUR FILES", art="archive", accent=YELLOW),
            "login": SelectableTile(right, self, "登录状态", lambda: self.open_view("login"), eyebrow="BROWSER SESSION", fill="#442631", accent="#FFB8CB"),
            "activity": SelectableTile(right, self, "任务活动", lambda: self.open_view("activity"), eyebrow="LIVE ACTIVITY", fill="#173C3B", accent="#AFE8CE"),
            "stop": SelectableTile(right, self, "停止任务", self.app.stop_download, eyebrow="STOP", fill="#442626", accent="#EDA6A6"),
        }
        self.tiles["featured"].grid(row=0, column=0, sticky="nsew")
        self.tiles["date"].grid(row=0, column=0, sticky="nsew", pady=(0, s(6)))
        self.tiles["location"].grid(row=1, column=0, sticky="nsew", pady=(s(6), 0))
        for i, key in enumerate(("folder", "login", "activity", "stop")):
            self.tiles[key].grid(row=i, column=0, sticky="nsew", pady=(0 if i == 0 else s(10), 0))
        tooltip(self.tiles["location"], lambda: self.app.output_var.get())
        self.app.start_button = self.tiles["featured"]
        self.app.stop_button = self.tiles["stop"]
        self.app.stop_button.set_state("disabled")

    def _build_details(self):
        s = self.px
        self.detail = tk.Frame(self.host, bg=BLACK)
        self.detail.rowconfigure(1, weight=1)
        self.detail.columnconfigure(0, weight=1)
        heading = tk.Frame(self.detail, bg=BLACK)
        heading.grid(row=0, column=0, sticky="ew", pady=(0, s(18)))
        self.back_button = SelectableTile(heading, self, "← 返回", self.back, compact=True, height=s(38))
        self.back_button.pack(side="left")
        self.back_button.configure(width=s(100))
        self.detail_title = self.label(heading, size=27, bold=True)
        self.detail_title.pack(side="left", padx=s(24))
        self.pages = {}
        self.detail_targets = {}
        for key in ("date", "activity", "login", "settings"):
            page = tk.Frame(self.detail, bg=BLACK)
            page.grid(row=1, column=0, sticky="nsew")
            self.pages[key] = page
            page.grid_remove()
        self._build_date(self.pages["date"])
        self._build_activity(self.pages["activity"])
        self._build_login(self.pages["login"])
        self._build_settings(self.pages["settings"])

    def _build_date(self, page):
        s, app = self.px, self.app
        page.columnconfigure(0, weight=1)
        editor = tk.Frame(page, bg=BLACK)
        editor.grid(row=0, column=0, sticky="w")
        style = ttk.Style(self.root)
        style.configure("Date.TCombobox", font=self.font(17), foreground=WHITE, fieldbackground=SURFACE, background="#333333", arrowcolor=WHITE, bordercolor="#666666", padding=(s(12), s(8)))
        style.map("Date.TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", WHITE)], selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", WHITE)])
        for col, (name, label, var, values, width) in enumerate((
            ("year_box", "年", app.year_var, [str(y) for y in range(2024, date.today().year + 7)], 7),
            ("month_box", "月", app.month_var, [str(m) for m in range(1, 13)], 5),
            ("day_box", "日", app.day_var, [], 5),
        )):
            self.label(editor, label, fg=GRAY, size=12).grid(row=0, column=col, sticky="w", padx=(0, s(18)))
            box = ttk.Combobox(editor, textvariable=var, values=values, width=width, state="readonly", style="Date.TCombobox", font=self.font(17))
            box.grid(row=1, column=col, sticky="w", padx=(0, s(18)), pady=(s(5), s(12)))
            setattr(app, name, box)
            box.bind("<<ComboboxSelected>>", self.date_changed)
        self.calendar_grid = tk.Frame(page, bg=BLACK)
        self.calendar_grid.grid(row=1, column=0, sticky="nsew", pady=s(8))
        page.rowconfigure(1, weight=1)
        for col, label in enumerate(("一", "二", "三", "四", "五", "六", "日")):
            self.calendar_grid.columnconfigure(col, weight=1, uniform="days")
            heading = self.label(self.calendar_grid, label, fg=GRAY, size=12)
            heading.configure(anchor="center")
            heading.grid(row=0, column=col, sticky="ew")
        self.day_buttons = []
        for row in range(6):
            self.calendar_grid.rowconfigure(row+1, weight=1, uniform="weeks")
            for col in range(7):
                button = ttk.Button(self.calendar_grid, style="Menu.TButton")
                button.grid(row=row+1, column=col, sticky="nsew", padx=s(2), pady=s(2))
                self.day_buttons.append(button)
        self.date_week = self.label(page, variable=app.week_var, size=18, bold=True, fg=YELLOW)
        self.date_week.grid(row=2, column=0, sticky="w", pady=s(10))
        apply = SelectableTile(page, self, "应用日期", self.apply_date, compact=True, fill="#5E4918", height=s(44))
        apply.grid(row=3, column=0, sticky="ew")
        self.detail_targets["date"] = [self.back_button, apply]

    def date_changed(self, _event=None):
        self.app.on_date_part_changed()
        self.refresh_calendar()
        self.feedback.play("navigate")

    def refresh_calendar(self):
        year, month = int(self.app.year_var.get()), int(self.app.month_var.get())
        weeks = calendar.monthcalendar(year, month)
        values = [day for week in weeks for day in week] + [0] * (42 - len(weeks)*7)
        selected = int(self.app.day_var.get())
        for button, day in zip(self.day_buttons, values):
            button.configure(text=str(day) if day else "", state="normal" if day else "disabled",
                             style="Active.Menu.TButton" if day == selected else "Menu.TButton",
                             command=lambda d=day: self.pick_day(d))

    def pick_day(self, day):
        self.app.day_var.set(str(day))
        self.app.refresh_week_label()
        self.refresh_calendar()
        self.feedback.play("navigate")

    def apply_date(self):
        self._date_snapshot = None
        self.open_view("home")

    def _build_activity(self, page):
        s = self.px
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=1)
        self.activity_summary = self.label(page, size=16, bold=True, fg=YELLOW)
        self.activity_summary.grid(row=0, column=0, sticky="w", pady=(0, s(16)))
        container = tk.Frame(page, bg=SURFACE)
        container.grid(row=1, column=0, sticky="nsew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)
        log = tk.Text(container, bg="#111111", fg=WHITE, font=self.font(13, family="Consolas"),
                      bd=0, highlightthickness=0, wrap="word", padx=s(16), pady=s(16),
                      selectbackground="#5D5D5D", insertbackground=WHITE)
        log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(container, command=log.yview, style="Menu.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        log.configure(yscrollcommand=scrollbar.set)
        for tag, color in {"success":"#9BD4AA", "warning":YELLOW, "error":"#F0A0A0", "running":"#A1D8DE", "muted":GRAY}.items():
            log.tag_configure(tag, foreground=color)
        log.insert("end", "SYSTEM READY\n\n等待任务启动\n", "warning")
        log.configure(state="disabled")
        self.app.log = log
        copy = SelectableTile(page, self, "复制完整日志", self.copy_log, compact=True, height=s(40))
        copy.grid(row=2, column=0, sticky="ew", pady=(s(12), 0))
        self.detail_targets["activity"] = [self.back_button, copy]

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.app.log.get("1.0", "end-1c"))
        self.context_var.set("日志已复制")

    def _build_login(self, page):
        s = self.px
        page.columnconfigure(0, weight=1)
        self.login_status = self.label(page, size=24, bold=True, fg=YELLOW)
        self.login_status.grid(row=0, column=0, sticky="w", pady=(s(30), s(18)))
        self.login_message = self.label(page, "浏览器登录状态由学校网页决定。", size=16, fg=GRAY)
        self.login_message.grid(row=1, column=0, sticky="ew", pady=(0, s(28)))
        self.app.continue_button = SelectableTile(page, self, "登录完成，继续", self.app.continue_after_login,
                                                  compact=True, fill="#5A3444", height=s(48))
        self.app.continue_button.grid(row=2, column=0, sticky="ew")
        self.app.continue_button.set_state("disabled")
        self.detail_targets["login"] = [self.back_button, self.app.continue_button]

    def _build_settings(self, page):
        s = self.px
        self.sound_var = tk.BooleanVar(value=self.feedback.sound_enabled)
        self.motion_var = tk.BooleanVar(value=self.feedback.motion_enabled)
        for row, (label, var, command) in enumerate((
            ("界面声音", self.sound_var, self.app.toggle_sounds),
            ("界面动效", self.motion_var, self.app.toggle_motion),
        )):
            toggle = tk.Checkbutton(page, text=label, variable=var, command=command, bg=BLACK, fg=WHITE,
                                    activebackground=BLACK, activeforeground=WHITE, selectcolor="#333333",
                                    font=self.font(19, True), padx=s(10), pady=s(16), takefocus=True)
            toggle.grid(row=row, column=0, sticky="w")
        self.audio_state = self.label(page, "", size=13, fg=GRAY)
        self.audio_state.grid(row=2, column=0, sticky="w", pady=s(14))
        preview = SelectableTile(page, self, "试听点击音", lambda: self.feedback.play("confirm"), compact=True, height=s(44))
        preview.grid(row=3, column=0, sticky="ew")
        page.columnconfigure(0, weight=1)
        self.detail_targets["settings"] = [self.back_button, preview]

    def navigate_view(self, key):
        self.feedback.play("confirm")
        if key == "home":
            self.back()
        else:
            self.open_view(key)

    def open_view(self, key):
        if key == self.view:
            return
        if key == "date" and self.running:
            return
        if self.view == "date" and self._date_snapshot is not None:
            year, month, day = self._date_snapshot
            self.app.year_var.set(year)
            self.app.month_var.set(month)
            self.app.day_var.set(day)
            self.app.on_date_part_changed()
            self._date_snapshot = None
        if self.view == "home":
            self._saved_selection = self.selection.selected
        self.view = key
        if self.selection.selected:
            self.selection.selected.selected = False
            self.selection.selected.cancel_edge()
            self.selection.selected.draw_edge()
        self.selection.selected = None
        for tile in self.home_targets:
            tile.selected = False
            tile.cancel_edge()
            tile.draw_edge()
        for page in self.pages.values():
            page.grid_remove()
        if key == "home":
            self.detail.grid_remove()
            self.home.grid()
            self.selection.targets = self.home_targets
            selected = self._saved_selection
            if selected is None or not selected.enabled:
                selected = next((t for t in self.home_targets if t.enabled), self.tiles["featured"])
            self.selection.select(selected, sound=False)
            selected.focus_set()
        else:
            self.home.grid_remove()
            self.detail.grid(row=0, column=0, sticky="nsew")
            self.pages[key].grid()
            self.detail_title.configure(text={"date":"选择日期", "activity":"任务活动", "settings":"设置", "login":"登录状态"}[key])
            self.selection.targets = self.detail_targets[key]
            self.selection.select(self.back_button, sound=False)
            self.back_button.focus_set()
            if key == "date":
                self._date_snapshot = (self.app.year_var.get(), self.app.month_var.get(), self.app.day_var.get())
                self.refresh_calendar()
        self.transition()
        self.refresh()

    def transition(self):
        if self._transition:
            self.root.after_cancel(self._transition)
            self._transition = None
        if not self.feedback.motion_enabled:
            if self.view != "home":
                self.detail.grid_configure(padx=0)
            return
        start = time.perf_counter()

        def step():
            t = min(1, (time.perf_counter() - start) / .16)
            self.detail.grid_configure(padx=(round(self.px(10) * (1-t)**2), 0))
            if t < 1 and self.view != "home":
                self._transition = self.root.after(16, step)
            else:
                self._transition = None
        if self.view != "home":
            step()

    def back(self, _event=None):
        if self.view != "home":
            self.open_view("home")
        return "break"

    def _release_hidden_key(self, event):
        if event.keysym in ("Return", "space"):
            self.release_activation()

    def release_activation(self):
        self.activation_held = False
        for tile in self.home_targets + [t for targets in self.detail_targets.values() for t in targets]:
            tile._key_down = False

    def context_for(self, tile):
        if tile is self.tiles.get("location"):
            return self.app.abbreviate_path(self.app.output_var.get(), 64)
        return tile.title

    def status(self):
        state = self.app.console_state_var.get()
        return state, {"READY":YELLOW, "STARTING":YELLOW, "AUTH":YELLOW, "DOWNLOADING":"#B2D5C6",
                       "RUNNING":"#B2D5C6", "COMPLETE":"#A9D5AC", "ERROR":"#EBACAC", "STOPPED":"#CBB5B5"}.get(state, YELLOW)

    def set_running(self, running):
        self.running = running
        for key in ("date", "location"):
            self.tiles[key].set_state("disabled" if running else "normal")
        for box in (self.app.year_box, self.app.month_box, self.app.day_box):
            box.configure(state="disabled" if running else "readonly")
        self.request_refresh(immediate=True)

    def request_refresh(self, immediate=False):
        """Coalesce bursty log/status writes into one lightweight redraw."""
        if immediate:
            if self._refresh_job:
                self.root.after_cancel(self._refresh_job)
                self._refresh_job = None
            self.refresh()
        elif not self._refresh_job:
            self._refresh_job = self.root.after(80, self.refresh)

    def refresh(self):
        self._refresh_job = None
        if not hasattr(self, "tiles"):
            return
        app = self.app
        details = {
            "date": f"{app.year_var.get()} / {int(app.month_var.get()):02} / {int(app.day_var.get()):02}",
            "location": app.abbreviate_path(app.output_var.get(), 32),
            "folder": "本地课件归档",
            "login": "等待浏览器登录" if self.running and app.continue_button.enabled else "会话进行中" if self.running else "尚未开始",
            "activity": f"{app.downloaded_count} FILES   /   {app.error_count} ERRORS",
            "stop": "终止当前下载" if self.running else "当前没有运行中的任务",
        }
        for key, detail in details.items():
            self.tiles[key].set_detail(detail)
        featured_visual = (app.week_var.get(), app.status_var.get(), app.console_state_var.get(),
                           app.download_count_var.get(), self.running, app.downloaded_count,
                           app.download_total)
        if featured_visual != self._featured_visual:
            self._featured_visual = featured_visual
            self.tiles["featured"].draw()
        if hasattr(self, "activity_summary"):
            state, color = self.status()
            self.activity_summary.configure(text=f"{app.status_var.get()}    /    {app.download_count_var.get()} FILES    /    {app.error_count} ERRORS", fg=color)
            self.footer_status.configure(text=f"SYSTEM  /  {state}", fg=color)
            self.login_status.configure(text=app.status_var.get() if self.running else "尚未开始下载")
            self.login_message.configure(text="请在浏览器完成学校登录与验证码，再点击下方继续。" if self.running else "启动本周课件下载后，浏览器会打开学校登录页。")
            self.sound_var.set(self.feedback.sound_enabled)
            self.motion_var.set(self.feedback.motion_enabled)
            self.audio_state.configure(text="音效已预载" if self.feedback.audio_ready else "音频设备不可用；其他功能可正常使用")
        for key, button in self.nav.items():
            button.configure(style="Active.Menu.TButton" if key == self.view else "Menu.TButton")
