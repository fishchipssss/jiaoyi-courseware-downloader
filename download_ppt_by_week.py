from public_settings import local_setting
import argparse
import base64
import getpass
import json
import os
import re
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlparse

import requests
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from downloader_runtime import RunLock, AlreadyRunning, live_school_page, refresh_proxy_environment


DEFAULT_CALENDAR_URL = "https://jwstu.shsmu.edu.cn/PaiKeCalendar/PaiKeCalendarIndex"
CALENDAR_API_PATH = "/PaiKeCalendar/TeachingCalendarGetData"
DEFAULT_PROFILE_DIR = str(Path(__file__).resolve().parent / "ppt-browser-profile")
DEFAULT_OUTPUT_ROOT = local_setting("output_root", str(Path.home() / "Documents" / "交医课件"))
DEFAULT_SCHOOL_USERNAME = local_setting("school_username")
DEFAULT_SCHOOL_PASSWORD = local_setting("school_password")
DEFAULT_FILE_USERNAME = local_setting("file_username")
DEFAULT_FILE_PASSWORD = local_setting("file_password")
PPT_EXTENSIONS = (".ppt", ".pptx", ".pdf")


def parse_date(value: str) -> date:
    value = value.strip()
    formats = ("%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d.%m.%Y")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Unsupported date format: {value}. Try 15/5/2026 or 2026-05-15.")


def prompt_for_date(prompt: str) -> date:
    while True:
        raw_value = input(prompt).strip()
        try:
            return parse_date(raw_value)
        except ValueError as exc:
            print(exc)


def monday_of_week(day: date) -> date:
    return day - timedelta(days=day.weekday())


def sunday_of_week(day: date) -> date:
    return monday_of_week(day) + timedelta(days=6)


def school_week_start(day: date) -> date:
    """Return the Sunday that starts the school's displayed teaching week.

    The school can schedule make-up classes on Sunday as the first day of the
    following teaching week (for example 2026-09-20).  Python's calendar-week
    convention would otherwise attach that Sunday to the preceding Monday.
    """
    return day - timedelta(days=(day.weekday() + 1) % 7)


def school_week_end(day: date) -> date:
    return school_week_start(day) + timedelta(days=6)


def infer_school_year(day: date) -> str:
    if day.month >= 9:
        return f"{day.year}-{day.year + 1}"
    return f"{day.year - 1}-{day.year}"


def infer_semester(day: date) -> str:
    # The calendar page uses SemesterCode values 1 and 2.
    return "1" if day.month >= 9 or day.month <= 1 else "2"


def invalid_select_value(value: str) -> bool:
    cleaned = (value or "").strip()
    return (
        not cleaned
        or "请选择" in cleaned
        or cleaned in {"全部", "All", "all"}
        or "\n" in cleaned
        or "\r" in cleaned
    )


def first_day_of_month(day: date) -> date:
    return day.replace(day=1)


def first_day_of_next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def parse_dotnet_date(value: str) -> date | None:
    if not value:
        return None
    match = re.search(r"/Date\((\d+)\)/", value)
    if not match:
        return None
    timestamp_ms = int(match.group(1))
    # The school site renders these timestamps in China time in browser JavaScript.
    return (datetime.fromtimestamp(timestamp_ms / 1000, UTC) + timedelta(hours=8)).date()


def safe_filename(value: str, fallback: str) -> str:
    value = unquote(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value)
    value = re.sub(r"\s+", " ", value).strip(". ")
    return value or fallback


def filename_from_url(url: str, fallback: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return safe_filename(name, fallback)


def filename_from_response(response, fallback_url: str, fallback_name: str) -> str:
    content_disposition = response.headers.get("content-disposition", "")
    utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, re.IGNORECASE)
    plain_match = re.search(r'filename="?([^";]+)"?', content_disposition, re.IGNORECASE)
    if utf8_match:
        return safe_filename(utf8_match.group(1), fallback_name)
    if plain_match:
        return safe_filename(plain_match.group(1), fallback_name)
    return filename_from_url(fallback_url, fallback_name)


def record_label(record: dict) -> str:
    parts = [
        record.get("date", "unknown date"),
        record.get("course", "unknown course"),
        record.get("title", ""),
    ]
    return " | ".join(part for part in parts if part)


def basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def ensure_file_server_credentials(args) -> tuple[str, str]:
    if args.file_username and args.file_password:
        return args.file_username, args.file_password

    print("The course file server is asking for a separate login.")
    if not args.file_username:
        args.file_username = input("Course file username: ").strip()
    if not args.file_password:
        args.file_password = getpass.getpass("Course file password: ")
    return args.file_username, args.file_password


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Too many duplicate file names near {path}")


def extract_week_number(*values: str) -> int | None:
    joined = " ".join(value or "" for value in values)
    patterns = (
        r"(?:第\s*)?(\d{1,2})\s*周",
        r"\bweek\s*(\d{1,2})\b",
        r"\bwk\s*(\d{1,2})\b",
        r"\bw\s*(\d{1,2})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, joined, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def folder_for_records(records: list[dict], target_day: date) -> str:
    for record in records:
        week = extract_week_number(
            record.get("Topical", ""),
            record.get("CourseName", ""),
            record.get("Course", ""),
            " ".join(str(record.get(f"Link{i}", "")) for i in range(1, 6)),
        )
        if week:
            return f"Week {week:02d}"
    start = school_week_start(target_day).isoformat()
    end = school_week_end(target_day).isoformat()
    return f"{start}_to_{end}"


def calendar_origin(calendar_url: str) -> str:
    parsed = urlparse(calendar_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def maybe_login(page, args) -> None:
    if args.skip_login:
        return

    safe_goto(page, args.login_url or calendar_origin(args.calendar_url) + "/Home")
    try:
        page.wait_for_selector("input[type='password'], #StudentID, #calendar", state="attached", timeout=15000)
    except PlaywrightTimeoutError:
        pass
    if page.locator("#StudentID, #calendar").count():
        print("学校登录状态有效，正在读取课表。", flush=True)
        return page

    if args.username and args.password:
        try:
            username_filled = fill_first_available(
                page,
                [
                    args.username_selector,
                    "#username",
                    "#UserName",
                    "#userName",
                    "#account",
                    "input[name='userName']",
                    "input[name='account']",
                    "input[name='loginName']",
                    "input[type='text']",
                ],
                args.username,
            )
            password_filled = fill_first_available(
                page,
                [
                    args.password_selector,
                    "#password",
                    "#Password",
                    "#pwd",
                    "input[name='pwd']",
                    "input[type='password']",
                ],
                args.password,
            )
            if username_filled and password_filled:
                print("学校账号已填写，请在浏览器完成登录后继续。", flush=True)
            else:
                print("Could not find both login fields. Log in manually in the browser, then continue.", flush=True)
            if args.auto_submit_login:
                page.locator(args.login_button_selector + ", button.content_submit").first.click(timeout=5000)
                try:
                    page.wait_for_selector("#StudentID, #calendar", state="attached", timeout=15000)
                    return page
                except PlaywrightTimeoutError:
                    print("仍需在浏览器完成学校登录。", flush=True)
        except Exception as exc:
            print(f"Could not auto-fill login fields: {exc}", flush=True)

    while True:
        print("After login, click the continue button in the app or press Enter here.", flush=True)
        input()
        page = live_school_page(page.context, page)
        wait_for_navigation_to_settle(page)
        if page.locator("#StudentID, #calendar").count():
            return page
        print("还没有进入学校首页或课表，请完成登录后再点继续；不要关闭浏览器。", flush=True)


def wait_for_navigation_to_settle(page) -> None:
    page.wait_for_timeout(1500)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass


def safe_goto(page, url: str, attempts: int = 3) -> None:
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(800)
            return
        except PlaywrightError as exc:
            last_error = exc
            print(f"Navigation retry {attempt}/{attempts}: {exc}", flush=True)
            wait_for_navigation_to_settle(page)
    if last_error:
        raise last_error


def fill_first_available(page, selectors: list[str], value: str) -> bool:
    tried = set()
    for selector in selectors:
        if not selector or selector in tried:
            continue
        tried.add(selector)
        try:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            locator.fill(value, timeout=1200)
            return True
        except Exception:
            continue
    return False


def read_calendar_form_values(page, args, target_day: date) -> dict:
    values = page.evaluate(
        """() => {
            const valueOf = (selector) => {
                const el = document.querySelector(selector);
                if (!el) return "";
                if (el.tagName === "SELECT") {
                    const selected = el.options[el.selectedIndex];
                    return (el.value || (selected && selected.value) || (selected && selected.textContent) || "").trim();
                }
                return el.value || el.textContent || "";
            };
            return {
                Grade: valueOf("#Grade").trim(),
                SchoolYear: valueOf("#SchoolYear").trim(),
                Semester: valueOf("#Semester").trim()
            };
        }"""
    )
    if args.grade:
        values["Grade"] = args.grade
    values["SchoolYear"] = args.school_year or infer_school_year(target_day)
    values["Semester"] = args.semester or infer_semester(target_day)
    return values


def set_calendar_filter(page, selector: str, value: str) -> None:
    if not value:
        return
    try:
        page.locator(selector).select_option(value=value, timeout=1000)
        return
    except Exception:
        pass
    try:
        page.locator(selector).select_option(label=value, timeout=1000)
        return
    except Exception:
        pass
    if selector == "#SchoolYear":
        label = f"{value}学年"
        try:
            page.locator(selector).select_option(label=label, timeout=1000)
        except Exception:
            pass


def apply_calendar_filters(page, form_values: dict) -> None:
    set_calendar_filter(page, "#SchoolYear", form_values.get("SchoolYear", ""))
    set_calendar_filter(page, "#Semester", form_values.get("Semester", ""))


def fetch_calendar_json(page, args, target_day: date) -> dict:
    print("Opening calendar page...", flush=True)
    safe_goto(page, args.calendar_url)
    try:
        page.wait_for_selector("#calendar", state="attached", timeout=15000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("学校尚未显示课表，请从学校首页完成登录后重试。") from exc

    form_values = read_calendar_form_values(page, args, target_day)
    apply_calendar_filters(page, form_values)
    request_start = first_day_of_month(target_day) - timedelta(days=7)
    request_end = first_day_of_next_month(target_day) + timedelta(days=7)
    params = {
        "start": request_start.isoformat(),
        "end": request_end.isoformat(),
        "Grade": form_values.get("Grade", ""),
        "SchoolYear": form_values.get("SchoolYear", ""),
        "Semester": form_values.get("Semester", ""),
    }
    api_url = urljoin(page.url, "TeachingCalendarGetData")

    print(f"Fetching calendar data: {params['start']} to {params['end']}", flush=True)
    print(
        "Calendar params: "
        f"Grade={params['Grade'] or '(empty)'}, "
        f"SchoolYear={params['SchoolYear'] or '(empty)'}, "
        f"Semester={params['Semester'] or '(empty)'}",
        flush=True,
    )
    text = page.evaluate(
        """async ({ apiUrl, params }) => {
            const url = `${apiUrl}?${new URLSearchParams(params).toString()}`;
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 30000);
            let response;
            try {
                response = await fetch(url, {
                    credentials: "include",
                    signal: controller.signal
                });
            } finally {
                clearTimeout(timer);
            }
            if (!response.ok) {
                throw new Error(`Calendar API failed: ${response.status}`);
            }
            return await response.text();
        }""",
        {"apiUrl": api_url, "params": params},
    )
    data = json.loads(text)
    print(f"Calendar API returned {len(data.get('List') or [])} record(s).", flush=True)
    return data


def record_is_in_target_week(record: dict, target_day: date) -> bool:
    target_start = school_week_start(target_day)
    target_end = school_week_end(target_day)

    # ClassDate is the most precise field and must take priority.  The API's
    # WeekStartTime/WeekEndingTime can use a Monday-Sunday range, which places
    # a Sunday make-up class in the wrong teaching week.
    class_date = parse_dotnet_date(record.get("ClassDate", ""))
    if class_date:
        return target_start <= class_date <= target_end

    week_start = parse_dotnet_date(record.get("WeekStartTime", ""))
    week_end = parse_dotnet_date(record.get("WeekEndingTime", ""))
    if week_start and week_end and week_start <= target_day <= week_end:
        return True
    return False


def records_from_calendar_json(data: dict, target_day: date) -> list[dict]:
    records = data.get("List") or []
    return [record for record in records if record_is_in_target_week(record, target_day)]


def ppt_links_from_records(records: list[dict], target_day: date) -> list[dict]:
    folder = folder_for_records(records, target_day)
    links = []
    seen = set()

    for record in records:
        class_date = parse_dotnet_date(record.get("ClassDate", ""))
        class_date_text = class_date.isoformat() if class_date else "unknown date"
        course_bits = [
            record.get("CourseName", ""),
            record.get("Topical", ""),
            record.get("CoursesContent", ""),
            record.get("ApplyName", ""),
        ]
        course_label = safe_filename(" - ".join(bit for bit in course_bits if bit), "course")

        for index in range(1, 6):
            url = (record.get(f"Link{index}") or "").strip()
            title = (record.get(f"Title{index}") or "").strip()
            if not url or url in seen:
                continue
            parsed_path = urlparse(url).path.lower()
            if not parsed_path.endswith(PPT_EXTENSIONS) and "ppt" not in title.lower():
                continue
            seen.add(url)
            fallback_name = filename_from_url(url, f"{course_label}-{index}.pptx")
            links.append(
                {
                    "week": folder,
                    "url": url,
                    "fallback_name": fallback_name,
                    "date": class_date_text,
                    "course": course_label,
                    "title": title or fallback_name,
                }
            )

    return links


def looks_like_ppt_link(url: str, text: str) -> bool:
    parsed_path = urlparse(url).path.lower()
    return parsed_path.endswith(PPT_EXTENSIONS) or "ppt" in (text or "").lower()


def collect_modal_links(page, folder: str, seen: set[str]) -> list[dict]:
    anchors = page.evaluate(
        """() => {
            const modalSelectors = [".modal.in", ".modal.show", ".modal"];
            const modals = modalSelectors
                .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
                .filter((el) => {
                    const style = window.getComputedStyle(el);
                    return style.display !== "none" && style.visibility !== "hidden";
                });
            const root = modals[0] || document;
            return Array.from(root.querySelectorAll("a[href]")).map((a) => ({
                href: a.href,
                text: (a.textContent || "").trim()
            }));
        }"""
    )
    records = []
    for index, anchor in enumerate(anchors, start=1):
        href = anchor.get("href", "")
        text = anchor.get("text", "")
        if not href or href in seen or not looks_like_ppt_link(href, text):
            continue
        seen.add(href)
        records.append(
            {
                "week": folder,
                "url": href,
                "fallback_name": filename_from_url(href, f"{folder}-modal-{index}.pptx"),
                "date": "visible calendar event",
                "course": "calendar popup",
                "title": text or filename_from_url(href, f"{folder}-modal-{index}.pptx"),
            }
        )
    return records


def close_modal_if_open(page) -> None:
    for selector in (".modal.in .close", ".modal.show .close", ".modal.in button:has-text('Close')", ".modal.show button:has-text('Close')"):
        try:
            if page.locator(selector).count():
                page.locator(selector).first.click(timeout=1000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)
    except Exception:
        pass


def collect_links_by_clicking_calendar(page, args, target_day: date) -> list[dict]:
    print("Falling back to clicking calendar event boxes.")
    safe_goto(page, args.calendar_url)
    form_values = read_calendar_form_values(page, args, target_day)
    apply_calendar_filters(page, form_values)

    page.evaluate(
        """(targetDate) => {
            if (window.jQuery && jQuery("#calendar").fullCalendar) {
                jQuery("#calendar").fullCalendar("changeView", "agendaWeek");
                jQuery("#calendar").fullCalendar("gotoDate", targetDate);
                jQuery("#calendar").fullCalendar("refetchEvents");
            }
        }""",
        target_day.isoformat(),
    )
    page.wait_for_timeout(2500)

    folder = f"{school_week_start(target_day).isoformat()}_to_{school_week_end(target_day).isoformat()}"
    events = page.locator(".fc-event, .paikeStyle")
    event_count = events.count()
    print(f"Visible calendar event boxes: {event_count}")

    records = []
    seen = set()
    for index in range(event_count):
        event = events.nth(index)
        try:
            event.scroll_into_view_if_needed(timeout=1000)
            event.click(timeout=2000)
            page.wait_for_timeout(500)
            records.extend(collect_modal_links(page, folder, seen))
        except Exception as exc:
            print(f"Could not inspect event box {index + 1}: {exc}")
        finally:
            close_modal_if_open(page)

    return records


def collect_links_from_current_page(page, args, target_day: date) -> list[dict]:
    print("Navigate the opened browser to the courseware/material page, then press Enter here.")
    input()

    folder = folder_for_records([], target_day)
    links = page.locator(args.ppt_link_selector)
    records = []
    for index in range(links.count()):
        link = links.nth(index)
        href = link.get_attribute("href")
        if not href:
            continue
        absolute_url = urljoin(page.url, href)
        records.append(
            {
                "week": folder,
                "url": absolute_url,
                "fallback_name": filename_from_url(absolute_url, f"{folder}-{index + 1}.pptx"),
                "date": "manual page",
                "course": "manual page",
                "title": filename_from_url(absolute_url, f"{folder}-{index + 1}.pptx"),
            }
        )
    return records


def download_with_requests(record: dict, local_path: Path, args) -> tuple[bool, str]:
    headers = {"Referer": DEFAULT_CALENDAR_URL}
    if args.file_username and args.file_password:
        headers["Authorization"] = basic_auth_header(args.file_username, args.file_password)

    try:
        with requests.get(record["url"], headers=headers, stream=True, timeout=(20, 180)) as response:
            if response.status_code == 401:
                username, password = ensure_file_server_credentials(args)
                headers["Authorization"] = basic_auth_header(username, password)
                response.close()
                with requests.get(record["url"], headers=headers, stream=True, timeout=(20, 180)) as retry:
                    return save_requests_response(retry, record, local_path)
            return save_requests_response(response, record, local_path)
    except Exception as exc:
        return False, str(exc)


def save_requests_response(response, record: dict, local_path: Path) -> tuple[bool, str]:
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}"
    with local_path.open("wb") as file:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                file.write(chunk)
    return True, "ok"


def download_with_browser_context(page, context, records: list[dict], output_root: Path, args) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    failures = []

    for record in records:
        week_dir = output_root / record["week"]
        week_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = unique_path(week_dir / record["fallback_name"])

        ok, reason = download_with_requests(record, fallback_path, args)
        if ok:
            print(f"Downloaded: {fallback_path}", flush=True)
            continue
        print(f"Stream download failed for {record_label(record)}: {reason}", flush=True)

        try:
            headers = {"Referer": DEFAULT_CALENDAR_URL}
            if args.file_username and args.file_password:
                headers["Authorization"] = basic_auth_header(args.file_username, args.file_password)

            response = context.request.get(
                record["url"],
                timeout=120_000,
                headers=headers,
            )
            if response.ok:
                file_name = filename_from_response(response, record["url"], record["fallback_name"])
                local_path = unique_path(week_dir / file_name)
                local_path.write_bytes(response.body())
                print(f"Downloaded: {local_path}")
                continue
            if response.status == 401:
                username, password = ensure_file_server_credentials(args)
                response = context.request.get(
                    record["url"],
                    timeout=120_000,
                    headers={
                        "Referer": DEFAULT_CALENDAR_URL,
                        "Authorization": basic_auth_header(username, password),
                    },
                )
                if response.ok:
                    file_name = filename_from_response(response, record["url"], record["fallback_name"])
                    local_path = unique_path(week_dir / file_name)
                    local_path.write_bytes(response.body())
                    print(f"Downloaded: {local_path}")
                    continue
            print(f"Browser request download failed [{response.status}], trying page fetch: {record['url']}")
        except Exception as exc:
            print(f"Browser request download failed, trying page fetch: {exc}")

        try:
            result = page.evaluate(
                """async ({ url }) => {
                    const response = await fetch(url, {
                        credentials: "include",
                        headers: { "Referer": location.href }
                    });
                    const body = await response.arrayBuffer();
                    const bytes = Array.from(new Uint8Array(body));
                    let binary = "";
                    for (const byte of bytes) binary += String.fromCharCode(byte);
                    return {
                        ok: response.ok,
                        status: response.status,
                        headers: Object.fromEntries(response.headers.entries()),
                        bodyBase64: btoa(binary)
                    };
                }""",
                {"url": record["url"]},
            )
            if not result["ok"]:
                failures.append((record, f"page fetch HTTP {result['status']}"))
                print(f"Download failed [{result['status']}]: {record_label(record)} | {record['url']}", flush=True)
                continue

            fake_response = type("ResponseLike", (), {"headers": result.get("headers", {})})()
            file_name = filename_from_response(fake_response, record["url"], record["fallback_name"])
            local_path = unique_path(week_dir / file_name)
            local_path.write_bytes(base64.b64decode(result["bodyBase64"]))
            print(f"Downloaded: {local_path}")
            continue
        except Exception as exc:
            print(f"Page fetch download failed, trying browser click: {exc}")

        try:
            download_page = context.new_page()
            download_page.goto(DEFAULT_CALENDAR_URL, wait_until="domcontentloaded", timeout=30_000)
            download_page.set_content(
                f'<html><body><a id="download-link" href="{record["url"]}">download</a></body></html>'
            )
            with download_page.expect_download(timeout=60_000) as download_info:
                download_page.locator("#download-link").click()
            download = download_info.value
            suggested = safe_filename(download.suggested_filename, record["fallback_name"])
            local_path = unique_path(week_dir / suggested)
            download.save_as(str(local_path))
            print(f"Downloaded: {local_path}")
            download_page.close()
            continue
        except Exception as exc:
            try:
                download_page.close()
            except Exception:
                pass
            failures.append((record, str(exc)))
            print(f"Download failed: {record_label(record)} | {record['url']} ({exc})", flush=True)

    if failures:
        print("\nFailed downloads:", flush=True)
        for record, reason in failures:
            print(f"- {record_label(record)}", flush=True)
            print(f"  URL: {record['url']}", flush=True)
            print(f"  Reason: {reason}", flush=True)


def build_arg_parser():
    parser = argparse.ArgumentParser(description="Download school PPT/PPTX files by date.")
    parser.add_argument("--calendar-url", default=DEFAULT_CALENDAR_URL, help="School calendar URL.")
    parser.add_argument("--login-url", default="", help="Login URL. Defaults to calendar URL.")
    parser.add_argument("--date", default="", help="Target date, for example 15/5/2026 or 2026-05-15.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT_ROOT, help="Output directory.")
    parser.add_argument("--profile-dir", default=DEFAULT_PROFILE_DIR, help="Browser profile directory.")
    parser.add_argument("--grade", default="", help="Override calendar Grade parameter if needed.")
    parser.add_argument("--school-year", default="", help="Override SchoolYear, for example 2025-2026.")
    parser.add_argument("--semester", default="", help="Override Semester, for example 2.")

    parser.add_argument(
        "--username",
        default=os.getenv("SCHOOL_USERNAME", DEFAULT_SCHOOL_USERNAME),
        help="Username or SCHOOL_USERNAME.",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("SCHOOL_PASSWORD", DEFAULT_SCHOOL_PASSWORD),
        help="Password or SCHOOL_PASSWORD.",
    )
    parser.add_argument(
        "--file-username",
        default=os.getenv("CURRICULUM_USERNAME", DEFAULT_FILE_USERNAME),
        help="Course file server username.",
    )
    parser.add_argument(
        "--file-password",
        default=os.getenv("CURRICULUM_PASSWORD", DEFAULT_FILE_PASSWORD),
        help="Course file server password.",
    )
    parser.add_argument("--skip-login", action="store_true", help="Skip login.")
    parser.add_argument(
        "--auto-submit-login",
        action="store_true",
        help="Click the login button after filling username/password. Do not use if captcha is required.",
    )
    parser.add_argument("--username-selector", default="input[name='username']", help="Username input CSS selector.")
    parser.add_argument("--password-selector", default="input[name='password']", help="Password input CSS selector.")
    parser.add_argument("--login-button-selector", default="button[type='submit']", help="Login button CSS selector.")

    parser.add_argument(
        "--manual-page",
        action="store_true",
        help="Fallback mode: manually open a page and download PPT links from that page.",
    )
    parser.add_argument(
        "--ppt-link-selector",
        default="a[href$='.ppt'], a[href$='.pptx'], a[href*='.ppt?'], a[href*='.pptx?']",
        help="PPT/PPTX link CSS selector for --manual-page mode.",
    )
    parser.add_argument("--headless", action="store_true", help="Run without visible browser.")
    parser.add_argument("--browser", choices=("chrome", "chromium"), default="chrome")
    return parser


def main():
    refresh_proxy_environment()
    if sys.stdout is not None:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = build_arg_parser().parse_args()
    target_day = parse_date(args.date) if args.date else prompt_for_date(
        "Date to download (example 15/5/2026): "
    )
    print(
        "Target date week: "
        f"{school_week_start(target_day).isoformat()} to {school_week_end(target_day).isoformat()}"
    )

    with RunLock(Path(args.profile_dir).with_suffix(".lock")), sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            args.profile_dir,
            headless=args.headless,
            accept_downloads=True,
            channel="chrome" if args.browser == "chrome" else None,
        )
        page = context.pages[0] if context.pages else context.new_page()

        try:
            print("已启动 Chrome。登录后请保留此浏览器窗口。", flush=True)
            page = maybe_login(page, args) or live_school_page(context, page)
            if args.manual_page:
                download_records = collect_links_from_current_page(page, args, target_day)
            else:
                calendar_data = fetch_calendar_json(page, args, target_day)
                week_records = records_from_calendar_json(calendar_data, target_day)
                print(f"Calendar records in target week: {len(week_records)}", flush=True)
                download_records = ppt_links_from_records(week_records, target_day)
                if not download_records:
                    download_records = collect_links_by_clicking_calendar(page, args, target_day)
            if not download_records:
                print("No PPT/PPTX links found for that date/week.", flush=True)
            else:
                print(f"Found {len(download_records)} PPT/PPTX link(s).", flush=True)
                from course_downloads import download_week
                failed = download_week(page, context, download_records, Path(args.output), args)
                if failed:
                    return 1
                print("Done.", flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except (RuntimeError, PlaywrightError, EOFError, ValueError) as exc:
        if "closed" in str(exc).lower():
            print("任务停止：下载浏览器已关闭或未能启动。请重新开始，并在下载结束前保持浏览器打开。", flush=True)
        else:
            print(f"任务失败：{str(exc).split('Call log:')[0]}", flush=True)
        sys.exit(1)
