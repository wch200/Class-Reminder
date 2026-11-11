# -*- coding: utf-8 -*-
"""课程提醒

Windows 本地独立运行的课程提醒程序。
仅使用 Python 标准库（tkinter），不调用任何网络 API 或大模型。
除首次自动下载节假日 JSON 外不联网，全程静音。
"""

import ctypes
import datetime as dt
import json
import math
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import urllib.request
import winreg
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageTk = None
    PIL_AVAILABLE = False

APP_TITLE = "课程提醒"
# 桌宠出厂默认值：旧版写死的 300 像素上限，以及右下角 24 像素边距。
DEFAULT_SIZE = 300
DEFAULT_X = 24
DEFAULT_Y = 24
PET_MIN_SIZE = 150
PET_MAX_SIZE = 600
# 预览区背景图候选：同一目录下按此顺序查找，png 优先，其次 jpg / jpeg。
PREVIEW_BG_NAMES = ("win11_bg.png", "win11_bg.jpg", "win11_bg.jpeg")
DEFAULT_TEMPLATE = "到时间了，现在应该去上{课程名}课"
DEFAULT_EXTRA_TEMPLATE = "还有 {leave_minutes} 分钟就要去上 {课程名} 课了"
TRANSPARENT_KEY = "#010203"
HOLIDAY_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
HOLIDAY_MIRROR = "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 开机自启写在当前用户的 Run 键下，不需要管理员权限。
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "ClassReminder"

# 作息表由用户通过“设置每日作息”自行配置，程序不再写死节次。
# PERIODS / PERIOD_LABELS 在载入 schedule.json 后动态生成。
PERIODS = ()
PERIOD_LABELS = ["自动识别"]
MAX_PERIODS = 20
BUBBLE_MAX_WIDTH = 350
REMINDER_DISPLAY_MS = 60000       # 每次弹出停留 60 秒
REMINDER_SNOOZE_MS = 180000       # 自动隐藏后 180 秒重弹


# ---------------------------------------------------------------------------
# 路径与通用工具
# ---------------------------------------------------------------------------

def app_dir():
    """程序数据所在目录：打包后为 exe 所在目录，开发时为脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = app_dir()
COURSES_PATH = os.path.join(BASE_DIR, "courses.json")
HOLIDAYS_PATH = os.path.join(BASE_DIR, "holidays.json")
MAKEUP_PATH = os.path.join(BASE_DIR, "makeup.json")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "schedule.json")


def pet_path():
    """优先使用 pet.png，其次使用随项目发布的 default_pet.png。

    同时兼容开发目录与 PyInstaller 的临时资源目录。用户自己的 pet.png
    始终优先，避免出厂默认桌宠覆盖个人素材。
    """
    roots = [app_dir()]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(mei)
    for filename in ("pet.png", "default_pet.png"):
        for root in roots:
            candidate = os.path.join(root, "assets", filename)
            if os.path.isfile(candidate):
                return candidate
    return os.path.join(app_dir(), "assets", "pet.png")


def preview_bg_path():
    """桌宠设置预览区的背景图：previews 目录下自动识别 png/jpg/jpeg。

    找不到任何一张时返回 None，由调用方回落到纯色背景（绝不读取系统壁纸）。
    """
    roots = [app_dir()]
    mei = getattr(sys, "_MEIPASS", None)
    if mei:
        roots.append(mei)
    for filename in PREVIEW_BG_NAMES:
        for root in roots:
            candidate = os.path.join(root, "previews", filename)
            if os.path.isfile(candidate):
                return candidate
    return None


def autostart_command():
    """开机自启命令：程序绝对路径 + --silent，登录后静默挂后台。"""
    exe = os.path.abspath(sys.executable)
    if getattr(sys, "frozen", False):
        return '"%s" --silent' % exe
    # 开发环境：解释器 + 脚本路径，保证命令行也能直接运行。
    return '"%s" "%s" --silent' % (exe, os.path.abspath(__file__))


def autostart_enabled():
    """读取 HKCU Run 中是否存在 ClassReminder 键值。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, AUTOSTART_NAME)
        return bool(str(value).strip())
    except OSError:
        return False


def set_autostart(enabled):
    """勾选时写入注册表，取消时删除键值；成功返回 True。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY,
                            0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ,
                                  autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def normalize_schedule(raw):
    """把 schedule.json 内容规范成 [{"start": "HH:MM", "end": "HH:MM"}]。"""
    if isinstance(raw, dict):
        raw = raw.get("periods", [])
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw:
        if isinstance(item, dict):
            start, end = item.get("start"), item.get("end")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            start, end = item[0], item[1]
        else:
            continue
        start = parse_time(str(start)) if start is not None else None
        end = parse_time(str(end)) if end is not None else None
        if not start or not end:
            continue
        if time_to_minutes(start) >= time_to_minutes(end):
            continue
        rows.append({"start": start, "end": end})
    rows.sort(key=lambda row: (time_to_minutes(row["start"]),
                               time_to_minutes(row["end"])))
    return rows[:MAX_PERIODS]


def set_periods(periods):
    """更新全局作息表，供课表、自动识别和下拉框共同使用。"""
    global PERIODS, PERIOD_LABELS
    normalized = normalize_schedule(periods)
    PERIODS = tuple((row["start"], row["end"]) for row in normalized)
    PERIOD_LABELS = ["自动识别"] + [
        "第%d节" % index for index in range(1, len(PERIODS) + 1)
    ]
    return normalized


def load_schedule():
    """读取本地 schedule.json；不存在或为空时返回空作息表。"""
    return set_periods(load_json(SCHEDULE_PATH, []))


def parse_time(text):
    if not isinstance(text, str):
        return None
    value = text.strip().replace("：", ":")
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return "%02d:%02d" % (hour, minute)


def time_to_minutes(text):
    parsed = parse_time(text)
    if parsed is None:
        return None
    hour, minute = [int(part) for part in parsed.split(":")]
    return hour * 60 + minute


def minutes_to_time(total):
    total = int(total) % (24 * 60)
    return "%02d:%02d" % (total // 60, total % 60)


def detect_period_start(text):
    """找与输入时间最接近的一节课的开始节次。"""
    minutes = time_to_minutes(text)
    if minutes is None or not PERIODS:
        return None
    return min(
        range(1, len(PERIODS) + 1),
        key=lambda number: abs(minutes - time_to_minutes(PERIODS[number - 1][0])),
    )


def detect_period_end(text):
    """找与输入时间最接近的一节课的结束节次。"""
    minutes = time_to_minutes(text)
    if minutes is None or not PERIODS:
        return None
    return min(
        range(1, len(PERIODS) + 1),
        key=lambda number: abs(minutes - time_to_minutes(PERIODS[number - 1][1])),
    )


def default_end_for_start(start):
    """结束时间留空时：开始节次对应到下一节下课；无作息时顺延 45 分钟。"""
    number = detect_period_start(start)
    if number and PERIODS:
        index = min(number, len(PERIODS) - 1)
        return PERIODS[index][1]
    total = time_to_minutes(start)
    if total is None:
        return None
    return minutes_to_time(min(total + 45, 23 * 60 + 59))


def valid_period(value):
    """接受 4 / "4" / "第4节"，返回有效节次，否则 None。"""
    if value is None:
        return None
    if isinstance(value, int):
        number = value
    else:
        text = str(value).strip()
        if text.startswith("第") and text.endswith("节"):
            text = text[1:-1].strip()
        try:
            number = int(text)
        except (TypeError, ValueError):
            return None
    return number if 1 <= number <= len(PERIODS) else None


def resolve_course_periods(course):
    """手动指定节次优先；返回 (起始节, 结束节)，没有作息表时返回 (None, None)。"""
    if not PERIODS:
        return None, None
    auto_from = detect_period_start(course.get("start", "")) or 1
    auto_to = detect_period_end(course.get("end", "")) or auto_from
    period_from = valid_period(course.get("period_from")) or auto_from
    period_to = valid_period(course.get("period_to")) or auto_to
    if period_to < period_from:
        period_to = period_from
    return period_from, period_to


def course_is_expired(course, today=None):
    """课程结束日期早于今天时返回 True；没有结束日期视为不限期。"""
    end_date = parse_date(course.get("end_date", ""))
    if end_date is None:
        return False
    return end_date < (today or dt.date.today())


def course_is_active_on(course, today=None):
    """严格按纯日期判断：start <= today <= end，时间部分完全不参与。"""
    day = today or dt.date.today()
    start_date = parse_date(course.get("start_date", ""))
    end_date = parse_date(course.get("end_date", ""))
    if start_date is not None and start_date > day:
        return False
    if end_date is not None and end_date < day:
        return False
    return True



def course_sort_key(course):
    """课程列表/课表统一排序：星期升序，再按开始时间升序。"""
    return (
        int(course.get("weekday", 0)),
        course.get("start", ""),
        course.get("end", ""),
        course.get("name", ""),
    )


def sort_courses(courses):
    return sorted(courses, key=course_sort_key)


def wrap_text(text, font, max_width):
    """按像素宽度逐字换行，保证文字不会超过气泡可用宽度。"""
    text = (text or "").strip()
    if not text:
        return [""]
    lines = []
    for paragraph in text.splitlines():
        current = ""
        for char in paragraph:
            trial = current + char
            if not current or font.measure(trial) <= max_width:
                current = trial
            else:
                lines.append(current)
                current = char
        lines.append(current)
    return lines or [""]


def parse_date(text):
    """接受 YYYY-M-D / YYYY-MM-DD，返回 date 或 None。"""
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    parts = value.split("-")
    if len(parts) != 3:
        return None
    try:
        year, month, day = (int(part) for part in parts)
        return dt.date(year, month, day)
    except (TypeError, ValueError):
        return None


def format_date(value):
    """统一显示为不补零的 YYYY-M-D；解析失败返回空字符串。"""
    if isinstance(value, str):
        value = parse_date(value)
    if not isinstance(value, dt.date):
        return ""
    return "%d-%d-%d" % (value.year, value.month, value.day)


def course_date_text(course):
    start = format_date(course.get("start_date"))
    end = format_date(course.get("end_date"))
    if start and end:
        return "%s ~ %s" % (start, end)
    if start:
        return "%s 起" % start
    if end:
        return "至 %s" % end
    return "未限制"


def normalize_extra(raw):
    if not isinstance(raw, dict):
        return None
    try:
        advance = int(raw.get("advance", raw.get("minutes", 0)) or 0)
    except (TypeError, ValueError):
        return None
    return {
        "advance": max(0, min(advance, 720)),
        "text": str(raw.get("text", "")).strip(),
    }


def normalize_course(raw):
    if not isinstance(raw, dict):
        return None
    try:
        name = str(raw.get("name", "")).strip()
        weekday = int(raw.get("weekday", 0))
        start = parse_time(str(raw.get("start", "")))
        end = parse_time(str(raw.get("end", "")))
    except (TypeError, ValueError):
        return None
    if not name or start is None or not 1 <= weekday <= 7:
        return None
    # 结束时间留空时按开始节次补到下一节下课；这样旧数据、临时课程
    # 和只填开始时间的快速录入都能得到完整的长条块。
    if end is None:
        end = default_end_for_start(start)
    if end is None or end < start:
        end = start
    try:
        advance = int(raw.get("advance", 0) or 0)
    except (TypeError, ValueError):
        advance = 0
    advance = max(0, min(advance, 720))
    start_date = parse_date(str(raw.get("start_date", "") or ""))
    end_date = parse_date(str(raw.get("end_date", "") or ""))
    # 结束日期留空默认只生效开始当天。
    if start_date and not end_date:
        end_date = start_date
    if start_date and end_date and end_date < start_date:
        end_date = start_date

    period_from = valid_period(raw.get("period_from"))
    period_to = valid_period(raw.get("period_to"))
    if period_from is not None and period_to is not None and period_to < period_from:
        period_to = period_from

    extras = []
    raw_extras = raw.get("extras", [])
    if isinstance(raw_extras, list):
        for item in raw_extras[:3]:
            extra = normalize_extra(item)
            if extra is not None:
                extras.append(extra)
    extra_enabled = bool(raw.get("extra_enabled", False))
    try:
        extra_count = int(raw.get("extra_count", len(extras)) or 0)
    except (TypeError, ValueError):
        extra_count = len(extras)
    extra_count = max(0, min(extra_count, 3))
    if extra_enabled:
        if extra_count == 0:
            extra_count = 1
        while len(extras) < extra_count:
            extras.append({"advance": 5, "text": ""})
        extras = extras[:extra_count]
    else:
        extra_count = 0
        extras = []

    return {
        "name": name,
        "weekday": weekday,
        "start": start,
        "end": end,
        "place": str(raw.get("place", "")).strip(),
        "advance": advance,
        "start_date": format_date(start_date),
        "end_date": format_date(end_date),
        "text": str(raw.get("text", "")).strip(),
        "period_from": period_from,
        "period_to": period_to,
        "is_evening_study": bool(raw.get("is_evening_study", False)),
        "extra_enabled": extra_enabled,
        "extra_count": extra_count,
        "extras": extras,
    }


def normalize_makeup(raw):
    if not isinstance(raw, dict):
        return None
    date_text = str(raw.get("date", "")).strip()
    if parse_date(date_text) is None:
        return None
    try:
        follow = int(raw.get("follow_weekday", 0))
    except (TypeError, ValueError):
        return None
    if not 1 <= follow <= 7:
        return None
    return {"date": date_text, "follow_weekday": follow}


def render_text(template, course, class_minutes=None, leave_minutes=None):
    """替换课程变量、{class_minutes} 和 {leave_minutes}。"""
    values = {
        "课程名": course.get("name", ""),
        "时间": course.get("start", ""),
        "地点": course.get("place", ""),
        "class_minutes": "" if class_minutes is None else str(class_minutes),
        "leave_minutes": "" if leave_minutes is None else str(leave_minutes),
    }
    if not template:
        return ""
    return re.sub(
        r"\{(课程名|时间|地点|class_minutes|leave_minutes)\}",
        lambda m: values[m.group(1)], template)


def build_course_reminders(course):
    """把基础提醒和额外提醒合并，按提前分钟数从大到小排序。

    每个提醒保留 kind/index，用于独立去重。class_minutes 是距正式上课的
    分钟数；leave_minutes 是该提醒距“出门”的分钟数（额外提前量减基础提前量）。
    """
    try:
        base_advance = max(0, min(int(course.get("advance", 0)), 720))
    except (TypeError, ValueError):
        base_advance = 0
    reminders = [{
        "kind": "base",
        "index": 0,
        "advance": base_advance,
        "class_minutes": base_advance,
        "leave_minutes": 0,
        "text": str(course.get("text", "") or ""),
    }]
    if course.get("extra_enabled"):
        for index, extra in enumerate(course.get("extras", [])[:3]):
            try:
                advance = int(extra.get("advance", 0))
            except (TypeError, ValueError):
                continue
            advance = max(0, min(advance, 720))
            reminders.append({
                "kind": "extra",
                "index": index,
                "advance": advance,
                "class_minutes": advance,
                "leave_minutes": max(0, advance - base_advance),
                "text": str(extra.get("text", "") or ""),
            })
    reminders.sort(key=lambda item: (
        -item["advance"],
        0 if item["kind"] == "base" else 1,
        item["index"],
    ))
    return reminders


def build_holiday_index(entries):
    """同一天 holiday 与 workday 都存在时，workday 优先。"""
    index = {}
    if not isinstance(entries, list):
        return index
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        date_text = str(entry.get("date", "")).strip()
        kind = str(entry.get("type", "")).strip().lower()
        if not date_text or kind not in ("holiday", "workday"):
            continue
        record = index.setdefault(date_text, {"holiday": False, "workday": False, "name": ""})
        if kind == "workday":
            record["workday"] = True
            record["name"] = str(entry.get("name", ""))
        else:
            record["holiday"] = True
            if not record["name"]:
                record["name"] = str(entry.get("name", ""))
    return index


def build_makeup_index(entries):
    index = {}
    if not isinstance(entries, list):
        return index
    for entry in entries:
        item = normalize_makeup(entry)
        if item:
            index[item["date"]] = item
    return index


def holiday_covers_year(entries, year):
    prefix = "%d-" % year
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("date", "")).startswith(prefix):
            return True
    return False


def sample_holidays(year):
    """下载失败且本地无数据时使用的内置示例。"""
    return [
        {"date": "%d-01-01" % year, "type": "holiday", "name": "元旦（示例）"},
        {"date": "%d-05-01" % year, "type": "holiday", "name": "劳动节（示例）"},
        {"date": "%d-10-01" % year, "type": "holiday", "name": "国庆节（示例）"},
    ]


def convert_holiday_payload(payload):
    days = payload.get("days") if isinstance(payload, dict) else payload
    if not isinstance(days, list):
        raise ValueError("节假日数据格式不正确")
    result = []
    for day in days:
        if not isinstance(day, dict):
            continue
        date_text = str(day.get("date", "")).strip()
        if not date_text:
            continue
        result.append({
            "date": date_text,
            "type": "holiday" if day.get("isOffDay") else "workday",
            "name": str(day.get("name", "")),
        })
    if not result:
        raise ValueError("节假日数据为空")
    return result


def fetch_holidays(year):
    urls = [HOLIDAY_URL.format(year=year), HOLIDAY_MIRROR.format(year=year)]
    errors = []
    for url in urls:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return convert_holiday_payload(payload)
        except Exception as exc:  # 网络或格式异常，尝试下一个地址
            errors.append("%s: %s" % (url, exc))
    raise RuntimeError("；".join(errors))


def rounded_rect(canvas, x1, y1, x2, y2, radius, **kwargs):
    radius = max(1, min(radius, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1,
        x2, y1 + radius, x2, y2 - radius, x2, y2,
        x2 - radius, y2, x1 + radius, y2, x1, y2,
        x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


def enable_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class SingleInstance:
    """单例 + 唤醒：命名互斥体判断是否已有实例，命名事件通知旧实例显示窗口。

    第一个进程创建互斥体并持有；之后双击 exe 的新进程会发现互斥体已存在，
    于是 SetEvent 唤醒旧进程，自己立即退出。旧进程后台线程收到信号后，
    只设置一个标志位，由 Tk 主线程的轻量定时器完成 deiconify，避免跨线程操作控件。
    """

    MUTEX_NAME = r"Local\ClassReminder_SingleInstance_Mutex"
    EVENT_NAME = r"Local\ClassReminder_SingleInstance_Wake"
    ERROR_ALREADY_EXISTS = 183
    EVENT_MODIFY_STATE = 0x0002
    WAIT_OBJECT_0 = 0
    INFINITE = 0xFFFFFFFF

    def __init__(self):
        self.enabled = False
        self.already_running = False
        self.mutex = None
        self.event = None
        self._kernel32 = None
        self._ctypes = None
        self._watcher = None
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CreateEventW.restype = ctypes.c_void_p
            kernel32.CreateEventW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.OpenEventW.restype = ctypes.c_void_p
            kernel32.OpenEventW.argtypes = [
                ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.SetEvent.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

            mutex = kernel32.CreateMutexW(None, False, self.MUTEX_NAME)
            last_error = kernel32.GetLastError()
            if not mutex:
                return
            self.mutex = mutex
            self.already_running = (last_error == self.ERROR_ALREADY_EXISTS)
            self.event = kernel32.CreateEventW(
                None, False, False, self.EVENT_NAME)
            self._kernel32 = kernel32
            self._ctypes = ctypes
            self.enabled = True
        except Exception:
            self.enabled = False

    def signal_existing(self):
        """通知已在运行的实例显示主窗口；成功返回 True。"""
        if not self.enabled or self._kernel32 is None:
            return False
        try:
            handle = self._kernel32.OpenEventW(
                self.EVENT_MODIFY_STATE, False, self.EVENT_NAME)
            if not handle:
                return False
            try:
                self._kernel32.SetEvent(handle)
            finally:
                self._kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False

    def start_watcher(self, callback):
        """后台线程等待唤醒事件；回调里只应做线程安全的轻量操作。"""
        if not self.enabled or not self.event:
            return
        kernel32 = self._kernel32
        event_handle = self.event
        infinite = self.INFINITE
        wait_object_0 = self.WAIT_OBJECT_0
        c_void_p = self._ctypes.c_void_p

        def run():
            while True:
                try:
                    result = kernel32.WaitForSingleObject(
                        c_void_p(event_handle), infinite)
                except Exception:
                    return
                if result != wait_object_0:
                    return
                try:
                    callback()
                except Exception:
                    pass

        self._watcher = threading.Thread(
            target=run, name="singleton-wake", daemon=True)
        self._watcher.start()

    def close(self):
        kernel32 = self._kernel32
        if kernel32 is None:
            return
        for handle in (self.event, self.mutex):
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
        self.event = None
        self.mutex = None
        self.enabled = False


# ---------------------------------------------------------------------------
# 桌宠提醒窗口
# ---------------------------------------------------------------------------

class ReminderWindow:
    """无边框、透明、置顶、不抢焦点的桌宠提醒窗口。

    基础提醒独立计时：停留 60 秒未点击则隐藏，180 秒后重弹一次；
    重弹后 60 秒仍未点击则彻底结束。额外提醒只停留 60 秒，不重弹。
    点击/ESC 会立即取消该窗口及其重弹。
    """

    def __init__(self, app, course, text, subtitle, index=0, on_close=None,
                 kind="base", allow_snooze=None):
        self.app = app
        self.course = course
        self.on_close = on_close
        self.kind = kind
        self.allow_snooze = ((kind == "base") if allow_snooze is None
                             else (bool(allow_snooze) and kind == "base"))
        self.closed = False
        self.visible = False
        self.display_round = 0
        self.anim_job = None
        self.clock_job = None
        self.hide_job = None
        self.repeat_job = None

        self.top = tk.Toplevel(app.root)
        self.top.withdraw()
        self.top.overrideredirect(True)
        try:
            self.top.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.top.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        try:
            self.top.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        self.top.configure(bg=TRANSPARENT_KEY)

        try:
            self.photo = self._load_pet()
            self._build_canvas(text, subtitle)
        except Exception:
            try:
                self.top.destroy()
            except tk.TclError:
                pass
            raise

        self._bind_events()
        self._place_window(index)
        try:
            self.top.deiconify()
            self.top.lift()
        except tk.TclError:
            self.close()
            return
        self._show_display()

    def _load_pet(self):
        if not PIL_AVAILABLE:
            raise RuntimeError("Pillow 未安装")
        path = pet_path()
        if not os.path.isfile(path):
            raise RuntimeError("未找到 assets/pet.png 或 assets/default_pet.png")
        image = Image.open(path).convert("RGBA")
        original_w, original_h = image.size
        max_edge = self.app.pet_size()
        # 滑块允许放大也允许缩小，始终把最长边缩放到当前设定值。
        if max(original_w, original_h) != max_edge:
            scale = max_edge / float(max(original_w, original_h))
            new_w = max(1, int(round(original_w * scale)))
            new_h = max(1, int(round(original_h * scale)))
            if original_w >= original_h:
                new_w = max_edge
            else:
                new_h = max_edge
            resampling = getattr(Image, "Resampling", Image)
            image = image.resize((new_w, new_h), resampling.LANCZOS)
        return ImageTk.PhotoImage(image, master=self.top)

    def _layout_bubble(self, text, subtitle, max_bubble_w=BUBBLE_MAX_WIDTH):
        screen_h = self.top.winfo_screenheight()
        max_text_w = max(120, max_bubble_w - 36)
        title_font = tkfont.Font(root=self.top, family="Microsoft YaHei UI",
                                  size=13, weight="bold")
        sub_font = tkfont.Font(root=self.top, family="Microsoft YaHei UI", size=9)
        lines = [""]
        sub_lines = []
        bubble_w = min(220, max_bubble_w)
        bubble_h = 104
        for size in (13, 12, 11, 10, 9, 8):
            title_font = tkfont.Font(
                root=self.top, family="Microsoft YaHei UI", size=size, weight="bold")
            sub_font = tkfont.Font(
                root=self.top, family="Microsoft YaHei UI", size=max(8, size - 4))
            lines = wrap_text(text, title_font, max_text_w)
            sub_lines = wrap_text(subtitle, sub_font, max_text_w) if subtitle else []
            text_w = min(
                max_text_w,
                max([title_font.measure(line) for line in lines] or [1]),
            )
            sub_w = max([sub_font.measure(line) for line in sub_lines] or [0])
            bubble_w = max(180, min(max_bubble_w, max(text_w, sub_w) + 36))
            title_h = len(lines) * title_font.metrics("linespace")
            sub_h = len(sub_lines) * sub_font.metrics("linespace") if sub_lines else 0
            bubble_h = 20 + title_h + (8 if sub_lines else 0) + sub_h + 18
            bubble_h = max(104, bubble_h)
            if bubble_h <= screen_h - 140 or size == 8:
                break
        if bubble_h > screen_h - 120:
            bubble_h = max(120, screen_h - 120)
        return title_font, sub_font, lines, sub_lines, bubble_w, bubble_h

    def _build_canvas(self, text, subtitle):
        self.pet_w = self.photo.width()
        self.pet_h = self.photo.height()
        self.clock_size = 58

        # 图片尺寸变化后，按实际桌宠宽度为气泡和时钟重新分配空间；
        # 极窄屏幕下适当收窄气泡，保证整个提醒窗口仍留在屏幕内。
        screen_w = self.top.winfo_screenwidth()
        fixed_width = 12 + self.pet_w + 16 + 14 + self.clock_size + 12 + 24
        available_bubble = max(180, screen_w - fixed_width)
        max_bubble_w = min(BUBBLE_MAX_WIDTH, available_bubble)
        (self.title_font, self.sub_font, self.lines, self.sub_lines,
         self.bubble_w, self.bubble_h) = self._layout_bubble(
            text, subtitle, max_bubble_w)

        self.width = 12 + self.pet_w + 16 + self.bubble_w + 14 + self.clock_size + 12
        self.height = max(self.pet_h + 24, self.bubble_h + 24, self.clock_size + 24)

        self.canvas = tk.Canvas(
            self.top, width=self.width, height=self.height,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.pet_x = 12
        self.pet_y = max(0, (self.height - self.pet_h) // 2)
        self.bx = 12 + self.pet_w + 16
        self.by = max(10, (self.height - self.bubble_h) // 2)
        bx, by = self.bx, self.by
        bw, bh = self.bubble_w, self.bubble_h

        rounded_rect(self.canvas, bx + 3, by + 5, bx + bw + 3, by + bh + 5,
                     18, fill="#dce2ea", outline="")
        rounded_rect(self.canvas, bx, by, bx + bw, by + bh,
                     18, fill="#ffffff", outline="#cfd8e3", width=1)
        self.canvas.create_image(self.pet_x, self.pet_y,
                                 image=self.photo, anchor="nw")

        self.canvas.create_text(
            bx + 18, by + 18, text="\n".join(self.lines), anchor="nw",
            width=bw - 36, font=self.title_font, fill="#1f2937",
        )
        if subtitle:
            self.canvas.create_text(
                bx + 18, by + bh - 16, text="\n".join(self.sub_lines), anchor="sw",
                width=bw - 36, font=self.sub_font, fill="#6b7280",
            )

        self.clock_x = bx + bw + 14
        self.clock_y = (self.height - self.clock_size) // 2
        self.clock_center = (
            self.clock_x + self.clock_size / 2.0,
            self.clock_y + self.clock_size / 2.0,
        )
        self.clock_radius = self.clock_size / 2.0 - 4
        self._draw_clock_face()

    def _draw_clock_face(self):
        cx, cy = self.clock_center
        radius = self.clock_radius
        self.canvas.create_oval(cx - radius - 2, cy - radius - 2,
                                cx + radius + 2, cy + radius + 2,
                                fill="#dce2ea", outline="")
        self.canvas.create_oval(cx - radius, cy - radius,
                                cx + radius, cy + radius,
                                fill="#f8fafc", outline="#94a3b8", width=2)
        for number in range(12):
            angle = math.radians(number * 30 - 90)
            inner = radius - (7 if number % 3 == 0 else 5)
            x1 = cx + math.cos(angle) * inner
            y1 = cy + math.sin(angle) * inner
            x2 = cx + math.cos(angle) * (radius - 3)
            y2 = cy + math.sin(angle) * (radius - 3)
            self.canvas.create_line(x1, y1, x2, y2, fill="#94a3b8",
                                    width=2 if number % 3 == 0 else 1)
        self.hour_hand = self.canvas.create_line(cx, cy, cx, cy, fill="#1f2937",
                                                 width=3, capstyle="round")
        self.minute_hand = self.canvas.create_line(cx, cy, cx, cy, fill="#334155",
                                                   width=2, capstyle="round")
        self.second_hand = self.canvas.create_line(cx, cy, cx, cy, fill="#dc2626",
                                                   width=1, capstyle="round")
        self.canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2,
                                fill="#1f2937", outline="")

    def _bind_events(self):
        # 拖拽桌宠可移动位置并记忆；轻点则关闭提醒。
        self.top.bind("<ButtonPress-1>", self._on_press)
        self.top.bind("<B1-Motion>", self._on_drag)
        self.top.bind("<ButtonRelease-1>", self._on_release)
        self.top.bind("<Escape>", lambda _event: self.close())

    def _on_press(self, event):
        # 按住就先停掉入场的滑动/漂浮动画，拖拽位置才不会被动画覆盖。
        self._cancel_job("anim_job")
        self._press_root = (event.x_root, event.y_root)
        self._drag_origin = (self.top.winfo_x(), self.top.winfo_y())
        self._dragging = True
        self._drag_moved = False
        self._drag_pos = self._drag_origin

    def _on_drag(self, event):
        if not getattr(self, "_dragging", False):
            return
        dx = event.x_root - self._press_root[0]
        dy = event.y_root - self._press_root[1]
        if abs(dx) > 3 or abs(dy) > 3:
            self._drag_moved = True
        # 拖动时停止漂浮动画，避免动画和几何位置互相打架。
        self._cancel_job("anim_job")
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        x = max(0, min(screen_w - self.width, self._drag_origin[0] + dx))
        y = max(0, min(screen_h - self.height, self._drag_origin[1] + dy))
        try:
            self.top.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            return
        self._drag_pos = (x, y)
        self.target_x, self.base_y = x, y

    def _on_release(self, _event):
        if not getattr(self, "_dragging", False):
            return
        self._dragging = False
        if not self._drag_moved:
            self.close()
            return
        # 松手后把位置写回 config.json，下次提醒仍出现在这个位置。
        try:
            # 用拖动过程中算好的坐标，避免 winfo_x 尚未刷新导致存旧值。
            x, y = getattr(self, "_drag_pos", None) or (
                self.top.winfo_x(), self.top.winfo_y())
            self.app.remember_pet_position(x, y, self.width, self.height)
            self.target_x, self.base_y = x, y
            # 拖动时停掉了浮动动画，松手后必须用新坐标重新起浮：
            # 把入场滑动起点对齐到当前位置，并让相位从 0 开始。
            self.start_x = x
            self.birth = time.time() - 0.6
            self._cancel_job("anim_job")
            self._animate()
        except tk.TclError:
            pass

    def _place_window(self, index):
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        mx, my = self.app.pet_margins()
        # 记忆的是“距右侧 / 距底部”的边距，换分辨率也不会跑到屏幕外。
        self.target_x = max(8, min(screen_w - self.width - 8,
                                   screen_w - self.width - mx))
        base = screen_h - self.height - my
        if index > 0:
            base -= index * (self.height + 10)
        self.base_y = max(8, min(screen_h - self.height - 8, base))
        self.top.geometry("%dx%d+%d+%d" % (
            self.width, self.height, screen_w + 8, self.base_y))

    def _show_display(self):
        if self.closed:
            return
        self.display_round += 1
        self.visible = True
        self.birth = time.time()
        self.start_x = self.top.winfo_screenwidth() + 8
        try:
            self.top.attributes("-alpha", 1.0)
            self.top.deiconify()
            self.top.lift()
            self.top.geometry("+%d+%d" % (self.start_x, self.base_y))
        except tk.TclError:
            self.close()
            return
        self._cancel_job("anim_job")
        self._cancel_job("clock_job")
        self._cancel_job("hide_job")
        self._animate()
        self._update_clock()
        self.hide_job = self.top.after(REMINDER_DISPLAY_MS, self._display_timeout)

    def _cancel_job(self, name):
        job = getattr(self, name, None)
        if job:
            try:
                self.top.after_cancel(job)
            except tk.TclError:
                pass
            setattr(self, name, None)

    def _animate(self):
        if self.closed or not self.visible:
            return
        elapsed = time.time() - self.birth
        if elapsed < 0.6:
            progress = elapsed / 0.6
            eased = 1 - (1 - progress) ** 3
            x = int(self.start_x + (self.target_x - self.start_x) * eased)
            y = self.base_y
        else:
            x = self.target_x
            y = self.base_y + int(round(5 * math.sin((elapsed - 0.6) * 2.2)))
        try:
            self.top.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            return
        self.anim_job = self.top.after(40, self._animate)

    def _update_clock(self):
        if self.closed or not self.visible:
            return
        now = time.localtime()
        cx, cy = self.clock_center
        hour_angle = math.radians(((now.tm_hour % 12) + now.tm_min / 60.0) * 30 - 90)
        minute_angle = math.radians((now.tm_min + now.tm_sec / 60.0) * 6 - 90)
        second_angle = math.radians(now.tm_sec * 6 - 90)
        specs = (
            (self.hour_hand, hour_angle, self.clock_radius - 13),
            (self.minute_hand, minute_angle, self.clock_radius - 7),
            (self.second_hand, second_angle, self.clock_radius - 4),
        )
        for item, angle, length in specs:
            self.canvas.coords(item, cx, cy,
                               cx + math.cos(angle) * length,
                               cy + math.sin(angle) * length)
        self.clock_job = self.top.after(250, self._update_clock)

    def _display_timeout(self):
        if self.closed:
            return
        self.hide_job = None
        # 只有基础提醒允许贪睡重弹；额外提醒到 60 秒直接结束。
        if self.display_round == 1 and self.allow_snooze:
            self._snooze()
        else:
            self.close()

    def _snooze(self):
        if self.closed:
            return
        self.visible = False
        self._cancel_job("anim_job")
        self._cancel_job("clock_job")
        try:
            self.top.withdraw()
        except tk.TclError:
            self.close()
            return
        self.repeat_job = self.top.after(REMINDER_SNOOZE_MS, self._repeat)

    def _repeat(self):
        if self.closed:
            return
        self.repeat_job = None
        self._show_display()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.visible = False
        for name in ("anim_job", "clock_job", "hide_job", "repeat_job"):
            self._cancel_job(name)
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close(self)


class ScheduleDialog(tk.Toplevel):
    """自由添加、删除节次并设置起止时间的作息表编辑器（无感自动保存）。

    交互规则：
    - 初始只有第 1 节，开始/结束都有效时自动追加下一节（最多 20 节）。
    - 没有“确定”按钮：输入框失去焦点或停止输入约 0.7 秒后，立即静默写入
      schedule.json；点“关闭”或右上角 X 也只是结束窗口，数据早已存好。
    - 保存时自动过滤开始或结束时间为空的半行，以及结束早于开始的无效行。
    """

    def __init__(self, parent, periods, on_saved=None):
        super().__init__(parent)
        self.result = None
        self.rows = []
        self._building = True
        self._auto_job = None
        self._save_job = None
        self._saving = False
        self._last_saved = None
        self.on_saved = on_saved
        self.title("设置每日作息")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.geometry("560x640")
        self.minsize(520, 520)

        outer = ttk.Frame(self, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="设置每日作息",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="填写时间后会自动保存，无需点“确定”：输入框失去焦点（点别处）"
                 "或停止输入片刻即写入 schedule.json。开始/结束时间为空的行会被忽略。",
            style="Hint.TLabel", wraplength=510,
        ).pack(anchor="w", pady=(4, 8))

        # 只保留“关闭”按钮；真正的保存发生在输入过程中。
        bottom = ttk.Frame(outer)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.count_label = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.count_label.pack(side="left")
        self.save_label = ttk.Label(bottom, text="自动保存已开启", style="Hint.TLabel")
        self.save_label.pack(side="left", padx=(12, 0))
        ttk.Button(bottom, text="关闭", command=self._close).pack(side="right")

        table_head = ttk.Frame(outer)
        table_head.pack(fill="x", pady=(0, 4))
        ttk.Label(table_head, text="节次", width=9).pack(side="left")
        ttk.Label(table_head, text="开始时间", width=13).pack(side="left", padx=(8, 0))
        ttk.Label(table_head, text="结束时间", width=13).pack(side="left", padx=(24, 0))
        ttk.Label(table_head, text="操作", width=10).pack(side="left", padx=(28, 0))

        body = ttk.Frame(outer)
        body.pack(side="top", fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg="#ffffff", highlightthickness=1,
                                highlightbackground="#dbe2ea", bd=0)
        scrollbar = ttk.Scrollbar(body, orient="vertical",
                                  command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, padding=(4, 4))
        self.window_id = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(
                scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(self.window_id, width=event.width))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<Escape>", lambda _event: self._close())

        # 已有作息直接显示；最后一行完整时补一个空行，方便继续添加。
        for item in (periods or [])[:MAX_PERIODS]:
            self._append_row(item.get("start"), item.get("end"),
                             blank=False, scroll=False)
        if not self.rows:
            self._append_row(blank=True, scroll=False)
        self._building = False
        self._renumber()

        self.update_idletasks()
        width = max(560, self.winfo_reqwidth())
        height = max(640, self.winfo_reqheight())
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = min(max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2),
                max(0, screen_w - width - 20))
        y = min(max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 3),
                max(0, screen_h - height - 60))
        self.geometry("+%d+%d" % (x, y))
        self.after(80, self._maybe_append)
        self.after(50, self._activate)

    def _activate(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass
        if self.rows:
            self.rows[0]["start_entry"].focus_set()

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _append_row(self, start=None, end=None, blank=False, scroll=True):
        if len(self.rows) >= MAX_PERIODS:
            return None
        if blank:
            start_value, end_value = "", ""
        else:
            start_value = parse_time(start) or ""
            end_value = parse_time(end) or ""

        frame = ttk.Frame(self.inner)
        frame.pack(fill="x", pady=3)
        label = ttk.Label(frame, text="", width=9)
        label.pack(side="left")
        start_var = tk.StringVar(value=start_value)
        end_var = tk.StringVar(value=end_value)
        start_entry = ttk.Entry(frame, textvariable=start_var, width=11)
        start_entry.pack(side="left", padx=(8, 0))
        ttk.Label(frame, text="—").pack(side="left", padx=(10, 0))
        end_entry = ttk.Entry(frame, textvariable=end_var, width=11)
        end_entry.pack(side="left", padx=(10, 0))

        row = {
            "frame": frame,
            "label": label,
            "start": start_var,
            "end": end_var,
            "start_entry": start_entry,
            "end_entry": end_entry,
        }
        self.rows.append(row)
        ttk.Button(
            frame, text="删除", width=8,
            command=lambda r=row: self._delete_row(r),
        ).pack(side="left", padx=(24, 0))

        start_var.trace_add("write", self._schedule_auto_append)
        end_var.trace_add("write", self._schedule_auto_append)
        start_var.trace_add("write", self._schedule_auto_save)
        end_var.trace_add("write", self._schedule_auto_save)
        # 焦点离开输入框（点别处、切到别的行、点关闭）立刻静默保存。
        start_entry.bind("<FocusOut>", self._auto_save)
        end_entry.bind("<FocusOut>", self._auto_save)
        start_entry.bind("<Return>", self._auto_save)
        end_entry.bind("<Return>", self._auto_save)
        self._renumber()
        if scroll:
            self.after(30, self._scroll_to_bottom)
        return row

    def _row_complete(self, row):
        start = parse_time(row["start"].get())
        end = parse_time(row["end"].get())
        return bool(start and end and
                    time_to_minutes(start) < time_to_minutes(end))

    def _schedule_auto_append(self, *_args):
        if self._building:
            return
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except tk.TclError:
                pass
        self._auto_job = self.after(120, self._maybe_append)

    def _cancel_auto(self):
        if self._auto_job is not None:
            try:
                self.after_cancel(self._auto_job)
            except tk.TclError:
                pass
            self._auto_job = None

    def _maybe_append(self):
        self._auto_job = None
        if self._building or not self.rows:
            return
        if len(self.rows) >= MAX_PERIODS:
            return
        if self._row_complete(self.rows[-1]):
            self._append_row(blank=True, scroll=True)

    def _scroll_to_bottom(self):
        try:
            self.canvas.update_idletasks()
            self.canvas.yview_moveto(1.0)
        except tk.TclError:
            pass

    def _delete_row(self, row):
        if row not in self.rows:
            return
        try:
            row["frame"].destroy()
        except tk.TclError:
            pass
        self.rows.remove(row)
        self._renumber()
        self._schedule_auto_append()
        self._schedule_auto_save()

    def _renumber(self):
        for index, row in enumerate(self.rows, start=1):
            row["label"].configure(text="第%d节" % index)
        self.count_label.configure(
            text="共 %d 行（最多 %d 行，空行不保存）" % (
                len(self.rows), MAX_PERIODS))

    def _schedule_auto_save(self, *_args):
        """停止输入约 0.7 秒后再保存，避免每敲一个字符就写一次盘。"""
        if self._building:
            return
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except tk.TclError:
                pass
        self._save_job = self.after(700, self._auto_save)

    def _auto_save(self, _event=None):
        """静默保存：只写完整且有效的行，界面保持不动，也不弹任何提示。"""
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except tk.TclError:
                pass
            self._save_job = None
        if self._building or self._saving:
            return
        rows = []
        for row in self.rows:
            start = parse_time(row["start"].get())
            end = parse_time(row["end"].get())
            # 半行、空行、结束不晚于开始的行一律跳过，不打扰用户。
            if not start or not end:
                continue
            if time_to_minutes(start) >= time_to_minutes(end):
                continue
            rows.append({"start": start, "end": end})
        normalized = normalize_schedule(rows)
        if normalized == self._last_saved:
            return
        self._saving = True
        try:
            save_json(SCHEDULE_PATH, normalized)
        except OSError as exc:
            self._saving = False
            self.save_label.configure(text="自动保存失败：%s" % exc)
            return
        self._saving = False
        self._last_saved = normalized
        self.result = normalized
        self.save_label.configure(
            text="已自动保存 %d 节 · %s" % (
                len(normalized), time.strftime("%H:%M:%S")))
        if self.on_saved is not None:
            try:
                self.on_saved(normalized)
            except Exception:
                pass

    def _close(self):
        """关闭窗口前再补一次保存，点 X、按 Esc、点“关闭”都不会丢数据。"""
        try:
            self._auto_save()
        except Exception:
            pass
        self._cancel_auto()
        self.destroy()


class TimetableWindow:
    """网格化课表：按 schedule.json 动态生成节次，支持跨节次合并与滚动。"""

    PERIOD_WIDTH = 94
    ROW_HEIGHT = 64

    def __init__(self, app, parent, courses):
        self.app = app
        self.courses = courses
        self.schedule = normalize_schedule(getattr(app, "schedule", []) or [])
        self.period_count = len(self.schedule)
        self.win = tk.Toplevel(parent)
        self.win.title("课程表")
        self.win.configure(bg="#ffffff")
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.close)

        screen_w = self.win.winfo_screenwidth()
        screen_h = self.win.winfo_screenheight()
        self.win_w = max(980, min(int(screen_w * 0.55), 1600))
        self.win_h = max(620, min(int(screen_h * 0.62), 1100))
        self.day_w = max(110, min(190, int((self.win_w - 120) / 7)))
        self.total_w = self.PERIOD_WIDTH + 7 * self.day_w
        self.total_h = max(80, self.period_count * self.ROW_HEIGHT)

        x = max(0, (screen_w - self.win_w) // 2)
        y = max(0, (screen_h - self.win_h) // 3)
        self.win.geometry("%dx%d+%d+%d" % (self.win_w, self.win_h, x, y))
        self.win.minsize(900, 520)

        # 顶部信息与左右切换开关固定在滚动区外面，滚动课表时不会移动。
        self.show_all = False
        self._font_cache = {}
        self.info_var = tk.StringVar(value="")
        topbar = tk.Frame(self.win, bg="#ffffff")
        topbar.pack(fill="x")
        tk.Label(
            topbar, textvariable=self.info_var, bg="#ffffff", fg="#94a3b8",
            font=("Microsoft YaHei UI", 8), anchor="w",
        ).pack(side="left", padx=12, pady=(5, 2))
        self.mode_toggle = tk.Canvas(
            topbar, width=220, height=30, bg="#ffffff",
            highlightthickness=0, bd=0, cursor="hand2",
        )
        self.mode_toggle.pack(side="right", padx=10, pady=(3, 2))
        self.mode_toggle.bind("<Button-1>", self._toggle_mode)

        self.header = tk.Canvas(
            self.win, height=44, width=self.total_w, bg="#f3f4f6",
            highlightthickness=0, bd=0,
        )
        self.header.pack(fill="x")

        body = tk.Frame(self.win, bg="#ffffff")
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            body, bg="#ffffff", highlightthickness=0, bd=0,
            scrollregion=(0, 0, self.total_w, self.total_h),
        )
        self.scrollbar = ttk.Scrollbar(
            body, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda _e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind("<Button-5>", lambda _e: self.canvas.yview_scroll(1, "units"))
        self._draw()

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _toggle_mode(self, _event=None):
        self.show_all = not self.show_all
        self._draw_toggle()
        self._draw()

    def _draw_toggle(self):
        canvas = self.mode_toggle
        canvas.delete("all")
        width, height = 220, 30
        active_color = "#1d4ed8"
        inactive_color = "#94a3b8"
        canvas.create_text(
            42, height / 2, text="当前生效",
            font=("Microsoft YaHei UI", 8, "bold"),
            fill=active_color if not self.show_all else inactive_color)
        canvas.create_text(
            178, height / 2, text="全部课程",
            font=("Microsoft YaHei UI", 8, "bold"),
            fill=active_color if self.show_all else inactive_color)

        # iOS 风格开关：圆角蓝色底座 + 白色圆形滑块，点击整块切换。
        track_left, track_right = 84, 136
        track_top, track_bottom = 3, 27
        canvas.create_oval(track_left, track_top, track_left + 24, track_bottom,
                           fill="#2563eb", outline="")
        canvas.create_rectangle(track_left + 12, track_top,
                                track_right - 12, track_bottom,
                                fill="#2563eb", outline="")
        canvas.create_oval(track_right - 24, track_top, track_right, track_bottom,
                           fill="#2563eb", outline="")
        knob_x = 96 if not self.show_all else 124
        canvas.create_oval(knob_x - 10, height / 2 - 10,
                           knob_x + 10, height / 2 + 10,
                           fill="#ffffff", outline="#dbeafe", width=1)

    def _update_info(self):
        start = format_date(getattr(self.app, "term_start", None)) or "未设置"
        end = format_date(getattr(self.app, "term_end", None)) or "未设置"
        self.info_var.set("学期：%s 至 %s" % (start, end))

    def _visible_courses(self):
        """左：今天在课程日期内；右：结束日期不早于今天。"""
        today = dt.date.today()
        visible = []
        for course in self.courses:
            if self.show_all:
                end = parse_date(course.get("end_date", ""))
                if end is not None and end < today:
                    continue
            elif not course_is_active_on(course, today):
                continue
            visible.append(course)
        return visible

    def _is_temporary(self, course):
        global_start = format_date(getattr(self.app, "term_start", None))
        global_end = format_date(getattr(self.app, "term_end", None))
        return (course.get("start_date", "") != global_start
                or course.get("end_date", "") != global_end)

    @staticmethod
    def _short_date(value):
        parsed = parse_date(value) if value else None
        return "%d.%d" % (parsed.month, parsed.day) if parsed else ""

    def _course_date_label(self, course):
        start = self._short_date(course.get("start_date", ""))
        end = self._short_date(course.get("end_date", ""))
        if start and end:
            return start if start == end else "%s-%s" % (start, end)
        if start:
            return "%s 起" % start
        if end:
            return "至 %s" % end
        return ""

    def _course_color(self, name):
        palette = ("#e8f1fb", "#eaf7ee", "#fff4e5", "#f3eafb",
                   "#e6f7f7", "#fdecec", "#eef1ff")
        return palette[sum(ord(ch) for ch in name) % len(palette)]

    def _interval_clusters(self, entries):
        """把同一天中相互重叠的课程分到一组，组内再分配并列泳道。"""
        clusters = []
        current = []
        current_end = 0
        for entry in sorted(entries, key=lambda item: (item["from"], item["to"])):
            if current and entry["from"] > current_end:
                clusters.append(current)
                current = []
                current_end = 0
            current.append(entry)
            current_end = max(current_end, entry["to"])
        if current:
            clusters.append(current)
        return clusters

    def _assign_lanes(self, cluster):
        lanes = []
        for entry in cluster:
            placed = False
            for index, end in enumerate(lanes):
                if entry["from"] > end:
                    lanes[index] = entry["to"]
                    entry["lane"] = index
                    placed = True
                    break
            if not placed:
                lanes.append(entry["to"])
                entry["lane"] = len(lanes) - 1
        return max(1, len(lanes))

    def _evening_geometry(self, course):
        """晚自习课：按真实开始/结束时间计算跨越的节次行，返回 (from, to, y1, y2)。

        普通课程用“最近节次”上下取整，晚自习则要求方块真正包裹实际时长，
        因此起止像素还会在首/末行内按分钟比例插值，看起来就是被向下拉长。
        """
        count = self.period_count or len(self.schedule)
        start = time_to_minutes(course.get("start", ""))
        end = time_to_minutes(course.get("end", ""))
        if not count or start is None or end is None:
            return None, None, 0, 0
        if end <= start:
            end = start + 1

        def period_bounds(index):
            period = self.schedule[index]
            return time_to_minutes(period["start"]), time_to_minutes(period["end"])

        def locate(minutes, boundary_end=False):
            for index in range(count):
                ps, pe = period_bounds(index)
                if ps is None or pe is None:
                    continue
                if boundary_end:
                    if ps < minutes <= pe:
                        return index, ps, pe
                elif ps <= minutes < pe:
                    return index, ps, pe
            return None, None, None

        from_index, fs, fe = locate(start)
        if from_index is None:
            from_index = min(
                range(count),
                key=lambda i: abs(start - (period_bounds(i)[0] or start)))
            fs, fe = period_bounds(from_index)
        to_index, ts, te = locate(end, boundary_end=True)
        if to_index is None:
            to_index = min(
                range(count),
                key=lambda i: abs(end - (period_bounds(i)[1] or end)))
            ts, te = period_bounds(to_index)
        if to_index < from_index:
            to_index = from_index
            ts, te = period_bounds(to_index)
        if fs is None or ts is None:
            return None, None, 0, 0

        span_start = max(1, (fe or fs) - fs)
        span_end = max(1, (te or ts) - ts)
        frac_start = min(1.0, max(0.0, (start - fs) / float(span_start)))
        frac_end = min(1.0, max(0.0, (end - ts) / float(span_end)))
        if to_index == from_index and frac_end <= frac_start:
            frac_end = min(1.0, frac_start + 0.25)

        y1 = from_index * self.ROW_HEIGHT + frac_start * self.ROW_HEIGHT + 3
        y2 = to_index * self.ROW_HEIGHT + frac_end * self.ROW_HEIGHT - 3
        if y2 - y1 < 28:
            y2 = y1 + 28
        return from_index + 1, to_index + 1, y1, y2

    def _draw(self):
        self._update_info()
        self._draw_toggle()
        self.header.delete("all")
        self.canvas.delete("all")

        for day in range(7):
            x1 = self.PERIOD_WIDTH + day * self.day_w
            x2 = x1 + self.day_w
            self.header.create_rectangle(
                x1, 0, x2, 44, fill="#eef2f7", outline="#cbd5e1")
            self.header.create_text(
                (x1 + x2) / 2, 22, text=WEEKDAY_NAMES[day],
                font=("Microsoft YaHei UI", 10, "bold"), fill="#334155")
        self.header.create_rectangle(
            0, 0, self.PERIOD_WIDTH, 44, fill="#e2e8f0", outline="#cbd5e1")
        self.header.create_text(
            self.PERIOD_WIDTH / 2, 22, text="节次",
            font=("Microsoft YaHei UI", 10, "bold"), fill="#334155")

        self.period_count = len(self.schedule)
        if not self.schedule:
            self.total_h = 100
            self.canvas.create_text(
                self.total_w / 2, 32, text="请先设置作息时间表",
                font=("Microsoft YaHei UI", 15, "bold"), fill="#334155")
            self.canvas.create_text(
                self.total_w / 2, 66,
                text="点击主界面的“设置每日作息”，自由添加节次和起止时间。",
                font=("Microsoft YaHei UI", 9), fill="#64748b")
            self.canvas.configure(scrollregion=(0, 0, self.total_w, self.total_h))
            return

        self.total_h = self.period_count * self.ROW_HEIGHT
        for row, period in enumerate(self.schedule):
            period_start = period["start"]
            period_end = period["end"]
            y1 = row * self.ROW_HEIGHT
            y2 = y1 + self.ROW_HEIGHT
            fill = "#ffffff" if row % 2 == 0 else "#fbfcfe"
            self.canvas.create_rectangle(
                0, y1, self.total_w, y2, fill=fill, outline="#e5e7eb")
            self.canvas.create_text(
                self.PERIOD_WIDTH / 2, y1 + 18, text="第%d节" % (row + 1),
                font=("Microsoft YaHei UI", 9, "bold"), fill="#334155")
            self.canvas.create_text(
                self.PERIOD_WIDTH / 2, y1 + 40,
                text="%s-%s" % (period_start, period_end),
                font=("Microsoft YaHei UI", 7), fill="#64748b")

        for day in range(1, 8):
            x = self.PERIOD_WIDTH + day * self.day_w
            self.canvas.create_line(x, 0, x, self.total_h, fill="#dbe2ea")
        for row in range(1, self.period_count):
            y = row * self.ROW_HEIGHT
            self.canvas.create_line(0, y, self.total_w, y, fill="#dbe2ea")

        by_day = {day: [] for day in range(1, 8)}
        for course in self._visible_courses():
            weekday = int(course.get("weekday", 0))
            if weekday not in by_day:
                continue
            if course.get("is_evening_study"):
                period_from, period_to, y1, y2 = self._evening_geometry(course)
                if period_from is None or period_to is None:
                    continue
            else:
                period_from, period_to = resolve_course_periods(course)
                if period_from is None or period_to is None:
                    continue
                period_from = max(1, min(self.period_count, period_from))
                period_to = max(period_from, min(self.period_count, period_to))
                y1 = (period_from - 1) * self.ROW_HEIGHT + 3
                y2 = period_to * self.ROW_HEIGHT - 3
            by_day[weekday].append({
                "course": course,
                "from": period_from,
                "to": period_to,
                "y1": y1,
                "y2": y2,
            })

        # 按“局部重叠簇”分配列：当天完全没有重叠的课程独占整行宽度，
        # 只有相互重叠的那一组课程才分左右列，且每个方块仍是完整长条。
        for weekday, entries in by_day.items():
            if not entries:
                continue
            entries.sort(key=lambda item: (
                item["from"], item["to"], item["course"]["name"]))
            base_x = self.PERIOD_WIDTH + (weekday - 1) * self.day_w
            for cluster in self._interval_clusters(entries):
                lane_count = self._assign_lanes(cluster)
                lane_w = self.day_w / lane_count
                for entry in cluster:
                    course = entry["course"]
                    x1 = base_x + entry["lane"] * lane_w + 2
                    x2 = base_x + (entry["lane"] + 1) * lane_w - 2
                    self._draw_course_block(
                        course, x1, entry["y1"], x2, entry["y2"])
        self.canvas.configure(scrollregion=(0, 0, self.total_w, self.total_h))

    def _font_for(self, size, bold=False):
        key = (int(size), bool(bold))
        if key not in self._font_cache:
            self._font_cache[key] = tkfont.Font(
                family="Microsoft YaHei UI", size=int(size),
                weight="bold" if bold else "normal")
        return self._font_cache[key]

    def _fit_line(self, text, font, max_width):
        """把单行文字压缩到可用宽度；放不下时截断并补省略号。"""
        text = str(text or "").strip()
        if not text:
            return ""
        if font.measure(text) <= max_width:
            return text
        ellipsis = "…"
        if font.measure(ellipsis) > max_width:
            return ""
        while text and font.measure(text + ellipsis) > max_width:
            text = text[:-1]
        return (text + ellipsis) if text else ellipsis

    def _draw_course_block(self, course, x1, y1, x2, y2):
        """绘制一个课程方块，所有文字都严格裁剪在方块内部。"""
        width = max(20, x2 - x1)
        if y2 - y1 < 24:
            y2 = y1 + 24
        self.canvas.create_rectangle(
            x1, y1, x2, y2,
            fill=self._course_color(course["name"]),
            outline="#93a7bd", width=1,
        )
        max_w = max(8, int(width) - 10)
        max_bottom = y2 - 3
        cursor = y1 + 4

        # 课程名：优先缩小字号，仍放不下才截断加省略号。
        name = str(course.get("name", "")).strip()
        name_size = 6
        name_label = ""
        for size in (10, 9, 8, 7, 6):
            font = self._font_for(size, True)
            if font.measure(name) <= max_w:
                name_size = size
                name_label = name
                break
        if not name_label:
            name_size = 6
            name_label = self._fit_line(
                name, self._font_for(name_size, True), max_w)
        if name_label:
            item = self.canvas.create_text(
                x1 + 5, cursor, text=name_label, anchor="nw", width=0,
                font=("Microsoft YaHei UI", name_size, "bold"),
                fill="#1e293b")
            bbox = self.canvas.bbox(item)
            if bbox and bbox[3] <= max_bottom and bbox[2] <= x2 - 2:
                cursor = bbox[3] + 2
            else:
                self.canvas.delete(item)

        # 时间、地点、临时日期：逐行放入剩余高度；放不下就隐藏，
        # 单行仍超宽则截断。绝不越出方块。
        date_text = (self._course_date_label(course)
                     if self._is_temporary(course) else "")
        details = [
            ("%s-%s" % (course.get("start", ""), course.get("end", "")),
             7, "#475569"),
            (course.get("place", "") or "", 7, "#475569"),
            (date_text, 6, "#94a3b8"),
        ]
        for text, size, color in details:
            if not text:
                continue
            font = self._font_for(size)
            line_h = font.metrics("linespace")
            if cursor + line_h > max_bottom:
                break
            label = self._fit_line(text, font, max_w)
            if not label:
                break
            item = self.canvas.create_text(
                x1 + 5, cursor, text=label, anchor="nw", width=0,
                font=("Microsoft YaHei UI", size), fill=color)
            bbox = self.canvas.bbox(item)
            if (not bbox or bbox[3] > max_bottom
                    or bbox[2] > x2 - 2):
                self.canvas.delete(item)
                break
            cursor = bbox[3] + 1

    def refresh(self, courses, schedule=None):
        self.courses = courses
        if schedule is not None:
            self.schedule = normalize_schedule(schedule)
        self._draw()

    def close(self):
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        if self.app and getattr(self.app, "timetable_window", None) is self:
            self.app.timetable_window = None


# ---------------------------------------------------------------------------
# 添加/编辑课程对话框
# ---------------------------------------------------------------------------

class CourseDialog(tk.Toplevel):
    def __init__(self, parent, title, course=None, term_start=None, term_end=None):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.configure(padx=14, pady=12)
        self.columnconfigure(1, weight=1)

        course = course or {}
        default_start = format_date(term_start)
        default_end = format_date(term_end)
        self.name_var = tk.StringVar(value=course.get("name", ""))
        self.weekday_var = tk.StringVar(
            value=WEEKDAY_NAMES[int(course.get("weekday", 1)) - 1]
        )
        self.start_var = tk.StringVar(value=course.get("start", "08:00"))
        self.end_var = tk.StringVar(value=course.get("end", "09:40"))
        self.place_var = tk.StringVar(value=course.get("place", ""))
        self.advance_var = tk.StringVar(value=str(course.get("advance", 10)))
        self.start_date_var = tk.StringVar(
            value=format_date(course.get("start_date")) or default_start)
        self.end_date_var = tk.StringVar(
            value=format_date(course.get("end_date")) or default_end)
        self.extra_enabled_var = tk.BooleanVar(value=bool(course.get("extra_enabled", False)))
        self.evening_var = tk.BooleanVar(
            value=bool(course.get("is_evening_study", False)))
        self.extra_count_var = tk.StringVar(value="1次")
        self.extra_rows = []

        period_from = valid_period(course.get("period_from"))
        period_to = valid_period(course.get("period_to"))
        self.period_from_user = period_from is not None
        self.period_to_user = period_to is not None
        self.period_from_var = tk.StringVar(
            value=PERIOD_LABELS[period_from] if period_from else PERIOD_LABELS[0])
        self.period_to_var = tk.StringVar(
            value=PERIOD_LABELS[period_to] if period_to else PERIOD_LABELS[0])

        row = 0
        ttk.Label(self, text="课程名").grid(row=row, column=0, sticky="w", pady=4)
        self.name_entry = ttk.Entry(self, textvariable=self.name_var, width=40)
        self.name_entry.grid(row=row, column=1, columnspan=3, sticky="we", pady=4)

        row += 1
        ttk.Label(self, text="星期").grid(row=row, column=0, sticky="w", pady=4)
        self.weekday_box = ttk.Combobox(
            self, textvariable=self.weekday_var, values=WEEKDAY_NAMES,
            state="readonly", width=10,
        )
        self.weekday_box.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(self, text="开始时间").grid(row=row, column=2, sticky="e", padx=(18, 6))
        self.start_entry = ttk.Entry(self, textvariable=self.start_var, width=9)
        self.start_entry.grid(row=row, column=3, sticky="w")

        row += 1
        ttk.Label(self, text="结束时间").grid(row=row, column=0, sticky="w", pady=4)
        self.end_entry = ttk.Entry(self, textvariable=self.end_var, width=9)
        self.end_entry.grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(self, text="留空自动顺延两节").grid(
            row=row, column=2, columnspan=2, sticky="w", padx=(18, 0))

        row += 1
        ttk.Label(self, text="手动指定节次").grid(row=row, column=0, sticky="w", pady=4)
        period_frame = ttk.Frame(self)
        period_frame.grid(row=row, column=1, columnspan=3, sticky="w", pady=4)
        ttk.Label(period_frame, text="从").pack(side="left")
        self.period_from_box = ttk.Combobox(
            period_frame, textvariable=self.period_from_var, values=PERIOD_LABELS,
            state="readonly", width=9,
        )
        self.period_from_box.pack(side="left", padx=(4, 8))
        ttk.Label(period_frame, text="到").pack(side="left")
        self.period_to_box = ttk.Combobox(
            period_frame, textvariable=self.period_to_var, values=PERIOD_LABELS,
            state="readonly", width=9,
        )
        self.period_to_box.pack(side="left", padx=(4, 8))
        ttk.Label(period_frame, text="时间变化会自动预填，可手动修改",
                  style="Hint.TLabel").pack(side="left", padx=(6, 0))

        row += 1
        ttk.Label(self, text="晚自习").grid(row=row, column=0, sticky="w", pady=4)
        self.evening_check = ttk.Checkbutton(
            self, text="是否为晚自习（课表按真实起止时间拉长方块）",
            variable=self.evening_var,
        )
        self.evening_check.grid(row=row, column=1, columnspan=3, sticky="w", pady=4)

        row += 1
        ttk.Label(self, text="地点").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.place_var, width=40).grid(
            row=row, column=1, columnspan=3, sticky="we", pady=4)

        row += 1
        ttk.Label(self, text="提前提醒").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Spinbox(self, from_=0, to=720, textvariable=self.advance_var,
                    width=9).grid(row=row, column=1, sticky="w", pady=4)
        ttk.Label(self, text="分钟（必定触发一次）").grid(
            row=row, column=2, columnspan=2, sticky="w", padx=(18, 0))

        row += 1
        self.extra_check = ttk.Checkbutton(
            self, text="开启额外重复提醒", variable=self.extra_enabled_var,
            command=self._toggle_extra,
        )
        self.extra_check.grid(row=row, column=1, sticky="w", pady=(2, 4))

        row += 1
        self.extra_section = ttk.Frame(self)
        self.extra_section.grid(row=row, column=1, columnspan=3, sticky="we", pady=(0, 4))
        self.extra_section.columnconfigure(1, weight=1)
        count_line = ttk.Frame(self.extra_section)
        count_line.grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(count_line, text="额外提醒次数").pack(side="left")
        self.extra_count_box = ttk.Combobox(
            count_line, textvariable=self.extra_count_var,
            values=("1次", "2次", "3次"), state="readonly", width=7,
        )
        self.extra_count_box.pack(side="left", padx=(8, 0))
        self.extra_count_box.bind("<<ComboboxSelected>>", self._on_count_change)
        ttk.Label(count_line, text="按提前时间从大到小依次触发",
                  style="Hint.TLabel").pack(side="left", padx=(10, 0))
        self.extra_rows_frame = ttk.Frame(self.extra_section)
        self.extra_rows_frame.grid(row=1, column=0, columnspan=2, sticky="we", pady=(4, 0))
        self.extra_rows_frame.columnconfigure(4, weight=1)

        initial_extras = list(course.get("extras", []))[:3]
        if course.get("extra_enabled"):
            count = max(1, min(3, int(course.get("extra_count", len(initial_extras)) or 1)))
            self.extra_count_var.set("%d次" % count)
            self._rebuild_extra_rows(initial_extras)
            self.extra_section.grid()
        else:
            self.extra_section.grid_remove()
            self._rebuild_extra_rows([])

        row += 1
        ttk.Label(self, text="生效日期").grid(row=row, column=0, sticky="w", pady=4)
        date_frame = ttk.Frame(self)
        date_frame.grid(row=row, column=1, columnspan=3, sticky="w", pady=4)
        ttk.Label(date_frame, text="从").pack(side="left")
        ttk.Entry(date_frame, textvariable=self.start_date_var, width=12).pack(
            side="left", padx=(4, 10))
        ttk.Label(date_frame, text="到").pack(side="left")
        ttk.Entry(date_frame, textvariable=self.end_date_var, width=12).pack(
            side="left", padx=(4, 0))
        ttk.Label(self, text="格式 YYYY-M-D；留空表示不限制，可临时修改",
                  style="Hint.TLabel").grid(row=row + 1, column=1, columnspan=3,
                                            sticky="w")
        row += 2

        ttk.Label(self, text="自定义提醒").grid(row=row, column=0, sticky="nw", pady=4)
        text_frame = ttk.Frame(self)
        text_frame.grid(row=row, column=1, columnspan=3, sticky="we", pady=4)
        self.text_box = tk.Text(text_frame, width=40, height=3, wrap="word",
                                font=("Microsoft YaHei UI", 9))
        self.text_box.pack(side="left", fill="both", expand=True)
        self.text_box.insert("1.0", course.get("text", ""))
        scroll = ttk.Scrollbar(text_frame, orient="vertical",
                               command=self.text_box.yview)
        scroll.pack(side="right", fill="y")
        self.text_box.configure(yscrollcommand=scroll.set)

        row += 1
        ttk.Label(self, text="支持 {课程名} {时间} {地点} {class_minutes} {leave_minutes}；留空则使用全局默认模板",
                  style="Hint.TLabel").grid(row=row, column=1, columnspan=3, sticky="w")

        row += 1
        buttons = ttk.Frame(self)
        buttons.grid(row=row, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(
            side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", command=self._save).pack(side="right")

        self.start_entry.bind("<FocusOut>", self._auto_fill_periods)
        self.end_entry.bind("<FocusOut>", self._auto_fill_periods)
        self.period_from_box.bind("<<ComboboxSelected>>",
                                  lambda _e: self._on_period_selected("from"))
        self.period_to_box.bind("<<ComboboxSelected>>",
                                lambda _e: self._on_period_selected("to"))

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = min(max(0, parent.winfo_rootx() + (parent.winfo_width() - width) // 2),
                max(0, screen_w - width - 20))
        y = min(max(0, parent.winfo_rooty() + (parent.winfo_height() - height) // 3),
                max(0, screen_h - height - 60))
        self.geometry("+%d+%d" % (x, y))
        self.after(50, self._activate)

    def _activate(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.name_entry.focus_set()

    def _on_period_selected(self, side):
        label = self.period_from_var.get() if side == "from" else self.period_to_var.get()
        manual = valid_period(label) is not None
        if side == "from":
            self.period_from_user = manual
        else:
            self.period_to_user = manual

    def _auto_fill_periods(self, _event=None):
        start = parse_time(self.start_var.get())
        end = parse_time(self.end_var.get())
        if start is not None and not self.period_from_user:
            number = detect_period_start(start)
            if number:
                self.period_from_var.set(PERIOD_LABELS[number])
        if end is not None and not self.period_to_user:
            number = detect_period_end(end)
            if number:
                self.period_to_var.set(PERIOD_LABELS[number])

    def _extra_count(self):
        try:
            return max(1, min(3, int(str(self.extra_count_var.get()).replace("次", ""))))
        except ValueError:
            return 1

    def _capture_extra_values(self):
        result = []
        for item in self.extra_rows:
            result.append({
                "advance": item["advance"].get(),
                "text": item["text"].get(),
            })
        return result

    def _rebuild_extra_rows(self, values=None):
        if values is None:
            values = self._capture_extra_values()
        for child in self.extra_rows_frame.winfo_children():
            child.destroy()
        self.extra_rows = []
        count = self._extra_count() if self.extra_enabled_var.get() else 0
        for index in range(count):
            saved = values[index] if index < len(values) else {}
            advance_var = tk.StringVar(value=str(saved.get("advance", "5") or "5"))
            text_var = tk.StringVar(value=saved.get("text", ""))
            ttk.Label(self.extra_rows_frame, text="提前").grid(
                row=index, column=0, sticky="w", pady=3)
            ttk.Spinbox(self.extra_rows_frame, from_=0, to=720,
                        textvariable=advance_var, width=6).grid(
                row=index, column=1, sticky="w", padx=(4, 4), pady=3)
            ttk.Label(self.extra_rows_frame, text="分钟").grid(
                row=index, column=2, sticky="w", pady=3)
            ttk.Label(self.extra_rows_frame, text="自定义文字").grid(
                row=index, column=3, sticky="w", padx=(14, 4), pady=3)
            ttk.Entry(self.extra_rows_frame, textvariable=text_var, width=28).grid(
                row=index, column=4, sticky="we", pady=3)
            self.extra_rows.append({"advance": advance_var, "text": text_var})

    def _toggle_extra(self):
        if self.extra_enabled_var.get():
            self.extra_section.grid()
            if not self.extra_rows:
                self._rebuild_extra_rows([])
        else:
            self._capture_extra_values()
            self.extra_section.grid_remove()

    def _on_count_change(self, _event=None):
        self._rebuild_extra_rows()

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror(APP_TITLE, "请填写课程名", parent=self)
            return
        start = parse_time(self.start_var.get())
        end_text = self.end_var.get().strip()
        end = parse_time(end_text) if end_text else None
        if start is None:
            messagebox.showerror(APP_TITLE, "开始时间格式应为 HH:MM，例如 08:00", parent=self)
            return
        if end_text and end is None:
            messagebox.showerror(APP_TITLE, "结束时间格式应为 HH:MM，例如 09:40；留空可自动顺延", parent=self)
            return
        if end is not None and end < start:
            messagebox.showerror(APP_TITLE, "结束时间不能早于开始时间", parent=self)
            return
        try:
            advance = int(self.advance_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_TITLE, "提前提醒分钟数必须是整数", parent=self)
            return
        if not 0 <= advance <= 720:
            messagebox.showerror(APP_TITLE, "提前提醒分钟数应在 0-720 之间", parent=self)
            return
        start_date_text = self.start_date_var.get().strip()
        end_date_text = self.end_date_var.get().strip()
        start_date = parse_date(start_date_text) if start_date_text else None
        end_date = parse_date(end_date_text) if end_date_text else None
        if start_date_text and start_date is None:
            messagebox.showerror(APP_TITLE, "课程生效开始日期格式应为 YYYY-M-D",
                                 parent=self)
            return
        if end_date_text and end_date is None:
            messagebox.showerror(APP_TITLE, "课程生效结束日期格式应为 YYYY-M-D",
                                 parent=self)
            return
        if start_date and end_date and end_date < start_date:
            messagebox.showerror(APP_TITLE, "课程生效结束日期不能早于开始日期",
                                 parent=self)
            return
        weekday = WEEKDAY_NAMES.index(self.weekday_var.get()) + 1

        # 未手动选择节次时保持 None，由课表和提醒按时间自动识别；
        # 用户在下拉框中明确选过的节次才写入数据。
        period_from = (valid_period(self.period_from_var.get())
                       if self.period_from_user else None)
        period_to = (valid_period(self.period_to_var.get())
                     if self.period_to_user else None)
        if period_from is not None and period_to is not None and period_to < period_from:
            period_to = period_from

        extra_enabled = bool(self.extra_enabled_var.get())
        extras = []
        if extra_enabled:
            for index, item in enumerate(self.extra_rows[:3]):
                try:
                    extra_advance = int(item["advance"].get().strip())
                except ValueError:
                    messagebox.showerror(
                        APP_TITLE, "第 %d 条额外提醒的提前分钟数必须是整数" % (index + 1),
                        parent=self)
                    return
                if not 0 <= extra_advance <= 720:
                    messagebox.showerror(
                        APP_TITLE, "第 %d 条额外提醒的分钟数应在 0-720 之间" % (index + 1),
                        parent=self)
                    return
                extras.append({
                    "advance": extra_advance,
                    "text": item["text"].get().strip(),
                })

        self.result = {
            "name": name,
            "weekday": weekday,
            "start": start,
            "end": end or "",
            "place": self.place_var.get().strip(),
            "advance": advance,
            "start_date": format_date(start_date),
            "end_date": format_date(end_date),
            "text": self.text_box.get("1.0", "end-1c").strip(),
            "period_from": period_from,
            "period_to": period_to,
            "is_evening_study": bool(self.evening_var.get()),
            "extra_enabled": extra_enabled,
            "extra_count": len(extras) if extra_enabled else 0,
            "extras": extras,
        }
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# 添加补班映射对话框
# ---------------------------------------------------------------------------

class MakeupDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.title("添加补班映射")
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.configure(padx=14, pady=12)

        self.date_var = tk.StringVar(value=dt.date.today().isoformat())
        self.weekday_var = tk.StringVar(value=WEEKDAY_NAMES[0])
        ttk.Label(self, text="补班日期").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(self, textvariable=self.date_var, width=16).grid(
            row=0, column=1, sticky="w", pady=5)
        ttk.Label(self, text="格式 YYYY-MM-DD", style="Hint.TLabel").grid(
            row=0, column=2, sticky="w", padx=(8, 0))
        ttk.Label(self, text="按周几的课表").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(self, textvariable=self.weekday_var, values=WEEKDAY_NAMES,
                     state="readonly", width=13).grid(
            row=1, column=1, sticky="w", pady=5)
        ttk.Label(self, text="补班日按这一天星期几的课程提醒",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w")
        buttons = ttk.Frame(self)
        buttons.grid(row=3, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="保存", command=self._save).pack(side="right")

        self.bind("<Return>", lambda _event: self._save())
        self.bind("<Escape>", lambda _event: self._cancel())
        self.update_idletasks()
        width = self.winfo_reqwidth()
        height = self.winfo_reqheight()
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 3)
        self.geometry("+%d+%d" % (x, y))
        self.after(50, self._activate)

    def _activate(self):
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def _save(self):
        date_text = self.date_var.get().strip()
        if parse_date(date_text) is None:
            messagebox.showerror(APP_TITLE, "补班日期格式应为 YYYY-MM-DD", parent=self)
            return
        follow = WEEKDAY_NAMES.index(self.weekday_var.get()) + 1
        self.result = {"date": date_text, "follow_weekday": follow}
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------

class CourseReminderApp:
    def __init__(self, root, single_instance=None):
        self.root = root
        self.single_instance = single_instance
        self.hidden = False
        self._wake_requested = False
        self._wake_job = None
        self._quitting = False
        self._pending_status = None
        self.courses = []
        self.schedule = []
        self.term_start = None
        self.term_end = None
        self.config = {"default_template": DEFAULT_TEMPLATE,
                       "extra_template": DEFAULT_EXTRA_TEMPLATE}
        self.timetable_window = None
        self.holidays = []
        self.makeup = []
        self.holiday_index = {}
        self.makeup_index = {}
        self.reminding = False
        self.reminder_job = None
        self.reminder_windows = []
        self.reminded = set()
        self.last_note = None
        self.last_week_day = None
        self.pet_missing = None
        self.holiday_queue = queue.Queue()
        # 启动即读取注册表，勾选状态与系统实际配置保持一致。
        self.autostart_var = tk.BooleanVar(value=autostart_enabled())
        self._pet_save_job = None
        self._pet_source_image = None
        self._pet_preview_photo = None
        self._preview_fonts = {}
        self._preview_redraw_job = None
        self._preview_last = None
        self._preview_bg_photo = None
        self._preview_bg_key = None
        self._preview_bg_name = None
        self._preview_bg_frame = None
        self._preview_bg_draw_rect = None
        self._clock_job = None
        self._clock_family = None
        self._course_tree_fonts = None
        self._column_width_job = None
        self._widths_before_drag = None
        self._applying_column_widths = False

        self.root.title(APP_TITLE)
        self.root.geometry("1040x850")
        self.root.minsize(940, 820)
        self._build_style()
        self._build_ui()
        self._load_all()
        self._refresh_courses()
        self._refresh_makeup()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_all("<Escape>", self._close_all_reminders, add="+")

        self._check_pet()
        self.root.update_idletasks()
        self._center_window()
        self.root.after(300, self._ensure_holidays)
        self.root.after(300, self._poll_holiday_queue)
        self.root.after(2500, self._pet_watch)
        # 单例唤醒：后台线程只写标志位，由主线程的轻量定时器 deiconify。
        if self.single_instance is not None:
            self.single_instance.start_watcher(self._request_wake)
        self._wake_job = self.root.after(400, self._poll_wake)

    # ------------------------------ 界面 ------------------------------

    def _center_window(self):
        """按内容实际需求调整窗口，避免高 DPI 下设置区/启动按钮被裁掉。"""
        self.root.update_idletasks()
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = max(1040, self.root.winfo_reqwidth() + 40)
        height = max(850, self.root.winfo_reqheight() + 120)
        width = max(940, min(width, screen_w - 80))
        height = max(820, min(height, screen_h - 100))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 3)
        self.root.geometry("%dx%d+%d+%d" % (width, height, x, y))

    def _build_style(self):
        style = ttk.Style()
        for theme in ("vista", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Status.TLabel", foreground="#374151")
        style.configure("Treeview", rowheight=30, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    def _build_ui(self):
        status_bar = ttk.Frame(self.root)
        status_bar.pack(side="bottom", fill="x")
        ttk.Separator(status_bar, orient="horizontal").pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(status_bar, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w", padding=(14, 6)).pack(fill="x")

        outer = ttk.Frame(self.root, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)

        head = ttk.Frame(outer)
        head.pack(fill="x")
        ttk.Label(head, text="课程提醒", style="Title.TLabel").pack(side="left")
        self.pet_state = ttk.Label(head, text="", style="Hint.TLabel")
        self.pet_state.pack(side="right")

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(8, 0))
        body = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        pet_tab = ttk.Frame(notebook, padding=(0, 8, 0, 0))
        notebook.add(body, text="课程与提醒")
        notebook.add(pet_tab, text="桌宠设置")
        self.notebook = notebook
        self.pet_tab = pet_tab

        reminder_bar = ttk.Frame(body)
        reminder_bar.pack(side="bottom", fill="x", pady=(2, 0))
        self.start_button = ttk.Button(reminder_bar, text="启动提醒",
                                       command=self.start_reminders)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(reminder_bar, text="停止提醒",
                                      command=self.stop_reminders, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(reminder_bar, text="关闭窗口不会停止提醒",
                  style="Hint.TLabel").pack(side="right")

        courses_frame = ttk.LabelFrame(body, text="课程表", padding=8)
        courses_frame.pack(fill="both", expand=True, pady=(10, 8))
        toolbar = ttk.Frame(courses_frame)
        toolbar.pack(fill="x", pady=(0, 6))
        ttk.Button(toolbar, text="添加课程", command=self.add_course).pack(side="left")
        ttk.Button(toolbar, text="编辑课程", command=self.edit_course).pack(
            side="left", padx=(8, 0))
        ttk.Button(toolbar, text="删除课程", command=self.delete_course).pack(
            side="left", padx=(8, 0))
        ttk.Button(toolbar, text="查看课表", command=self.show_timetable).pack(
            side="left", padx=(8, 0))
        ttk.Button(toolbar, text="设置每日作息", command=self.open_schedule_dialog).pack(
            side="left", padx=(8, 0))
        ttk.Button(toolbar, text="恢复自适应列宽",
                   command=self.reset_course_column_widths).pack(
            side="left", padx=(8, 0))
        self.count_label = ttk.Label(toolbar, text="", style="Hint.TLabel")
        self.count_label.pack(side="right")

        tree_frame = ttk.Frame(courses_frame)
        tree_frame.pack(fill="both", expand=True)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        columns = (
            ("name", "课程名", 160, "center"),
            ("weekday", "星期", 62, "center"),
            ("start", "开始", 72, "center"),
            ("end", "结束", 72, "center"),
            ("place", "地点", 130, "center"),
            ("advance", "提前(分)", 66, "center"),
            ("date_range", "起止日期", 150, "center"),
            ("text", "自定义提醒", 200, "w"),
        )
        self._course_columns = columns
        self.tree = ttk.Treeview(
            tree_frame, columns=[item[0] for item in columns],
            show="headings", selectmode="browse", height=7,
        )
        for key, label, width, anchor in columns:
            self.tree.heading(key, text=label, anchor="center")
            self.tree.column(key, width=width, anchor=anchor,
                             minwidth=40, stretch=False)
        self.tree.bind("<ButtonPress-1>", self._on_column_press, add="+")
        self.tree.bind("<ButtonRelease-1>", self._remember_column_widths, add="+")
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.course_hscroll = ttk.Scrollbar(
            tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set,
                            xscrollcommand=self.course_hscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.course_hscroll.grid(row=1, column=0, sticky="ew")
        self.tree.bind("<Double-1>", lambda _event: self.edit_course())
        # 鼠标放在课程列表上即可用滚轮上下滑动，不必去拖右侧滚动条。
        self.tree.bind("<MouseWheel>", self._on_course_wheel)
        self.tree.bind("<Button-4>", lambda _event: self.tree.yview_scroll(-1, "units"))
        self.tree.bind("<Button-5>", lambda _event: self.tree.yview_scroll(1, "units"))

        settings = ttk.LabelFrame(body, text="设置", padding=10)
        settings.pack(fill="x", pady=(0, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="学期日期").grid(row=0, column=0, sticky="w")
        self.term_var = tk.StringVar()
        self.term_end_var = tk.StringVar()
        term_frame = ttk.Frame(settings)
        term_frame.grid(row=0, column=1, columnspan=3, sticky="we",
                        padx=(10, 6), pady=3)
        ttk.Label(term_frame, text="开始").pack(side="left")
        ttk.Entry(term_frame, textvariable=self.term_var, width=12).pack(
            side="left", padx=(4, 10))
        ttk.Label(term_frame, text="结束").pack(side="left")
        ttk.Entry(term_frame, textvariable=self.term_end_var, width=12).pack(
            side="left", padx=(4, 10))
        ttk.Button(term_frame, text="保存", command=self.save_term_dates).pack(
            side="left")
        ttk.Label(term_frame, text="格式 YYYY-M-D", style="Hint.TLabel").pack(
            side="left", padx=(12, 0))

        ttk.Label(settings, text="默认提醒模板").grid(row=1, column=0, sticky="w")
        self.template_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.template_var).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=(10, 6), pady=3)
        ttk.Button(settings, text="保存", command=self.save_template).grid(
            row=1, column=3, sticky="e", pady=3)
        ttk.Label(settings, text="支持 {课程名} {时间} {地点} {class_minutes}；课程自定义提醒优先",
                  style="Hint.TLabel").grid(row=2, column=1, columnspan=3,
                                            sticky="w", padx=(10, 0))

        ttk.Label(settings, text="全局额外提醒模板").grid(row=3, column=0, sticky="w")
        self.extra_template_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.extra_template_var).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=(10, 6), pady=3)
        ttk.Button(settings, text="保存", command=self.save_extra_template).grid(
            row=3, column=3, sticky="e", pady=3)
        ttk.Label(settings, text="支持 {leave_minutes} {class_minutes} {课程名}；额外提醒自定义文字优先",
                  style="Hint.TLabel").grid(row=4, column=1, columnspan=3,
                                            sticky="w", padx=(10, 0))

        ttk.Label(settings, text="点右上角 X 只是隐藏到后台，提醒继续运行",
                  style="Hint.TLabel").grid(row=5, column=1, columnspan=2,
                                            sticky="w", padx=(10, 0), pady=(8, 0))
        self.quit_button = ttk.Button(settings, text="彻底退出程序",
                                      command=self.quit_app)
        self.quit_button.grid(row=5, column=3, sticky="e", pady=(8, 0))

        self.autostart_check = ttk.Checkbutton(
            settings, text="随 Windows 开机自动启动（静默挂后台）",
            variable=self.autostart_var, command=self._toggle_autostart)
        self.autostart_check.grid(row=6, column=0, columnspan=3,
                                  sticky="w", pady=(10, 0))

        makeup_frame = ttk.LabelFrame(
            body, text="补班映射（补班日按指定星期的课表提醒）", padding=8)
        makeup_frame.pack(fill="x", pady=(0, 8))
        makeup_bar = ttk.Frame(makeup_frame)
        makeup_bar.pack(fill="x", pady=(0, 6))
        ttk.Button(makeup_bar, text="添加映射", command=self.add_makeup).pack(side="left")
        ttk.Button(makeup_bar, text="删除映射", command=self.delete_makeup).pack(
            side="left", padx=(8, 0))
        ttk.Label(makeup_bar, text="workday 当天按 follow_weekday 的课表提醒",
                  style="Hint.TLabel").pack(side="right")
        self.makeup_tree = ttk.Treeview(
            makeup_frame, columns=("date", "follow"), show="headings",
            selectmode="browse", height=3,
        )
        self.makeup_tree.heading("date", text="补班日期")
        self.makeup_tree.heading("follow", text="按周几的课表")
        self.makeup_tree.column("date", width=180, anchor="center", stretch=False)
        self.makeup_tree.column("follow", width=180, anchor="center", stretch=False)
        self.makeup_tree.pack(fill="x")

        self._build_pet_tab(pet_tab)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _on_tab_changed(self, _event=None):
        """每次切换标签页都重新读取 previews 背景图，绝不沿用旧缓存。"""
        self._preview_bg_photo = None
        self._preview_bg_key = None
        self._preview_bg_name = None
        self._preview_bg_frame = None
        self._preview_bg_draw_rect = None
        self._redraw_pet_preview()

    def _on_course_wheel(self, event):
        """课程列表的滚轮滚动：每格 120 单位折合一行。"""
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.tree.yview_scroll(delta, "units")
        return "break"

    # ------------------------------ 桌宠设置 ------------------------------

    def pet_size(self):
        """当前桌宠最长边像素，读不到或越界时回落到出厂默认值。"""
        try:
            size = int(self.config.get("pet_size", DEFAULT_SIZE))
        except (TypeError, ValueError):
            size = DEFAULT_SIZE
        return max(PET_MIN_SIZE, min(PET_MAX_SIZE, size))

    def pet_margins(self):
        """桌宠位置以“距右侧 / 距底部”的边距保存，换分辨率也不会跑出屏幕。"""
        try:
            mx = int(self.config.get("pet_x", DEFAULT_X))
            my = int(self.config.get("pet_y", DEFAULT_Y))
        except (TypeError, ValueError):
            mx, my = DEFAULT_X, DEFAULT_Y
        return max(0, mx), max(0, my)

    def set_pet_size(self, size, save=True):
        try:
            size = int(round(float(size)))
        except (TypeError, ValueError):
            size = DEFAULT_SIZE
        size = max(PET_MIN_SIZE, min(PET_MAX_SIZE, size))
        self.config["pet_size"] = size
        if save:
            self._save_config_now()
        return size

    def remember_pet_position(self, x, y, width, height):
        """桌宠拖拽松手后调用：把绝对坐标换算成右下角边距并保存。"""
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        self.config["pet_x"] = max(0, screen_w - int(x) - int(width))
        self.config["pet_y"] = max(0, screen_h - int(y) - int(height))
        self._save_config_now()

    def _save_config_now(self):
        self._pet_save_job = None
        try:
            save_json(CONFIG_PATH, self.config)
        except OSError:
            pass

    def _schedule_pet_save(self):
        """滑块拖动很密集，稍作延迟再落盘，避免频繁写文件。"""
        if self._pet_save_job:
            try:
                self.root.after_cancel(self._pet_save_job)
            except tk.TclError:
                pass
        self._pet_save_job = self.root.after(350, self._save_config_now)

    def _build_pet_tab(self, parent):
        preview_box = ttk.LabelFrame(parent, text="效果图预览（模拟屏幕）", padding=8)
        preview_box.pack(fill="x")

        # 预览区固定 16:9：缩放因子只取“预览区宽度 / 真实屏幕宽度”，
        # 桌宠与气泡按该因子等比绘制，占屏比例与真实桌面一致。
        screen_h = max(1, self.root.winfo_screenheight())
        budget_h = min(620, int(screen_h * 0.33))
        budget_h = max(240, budget_h)
        canvas_w = min(1000, int(round(budget_h * 16.0 / 9.0)))
        canvas_w = max(320, canvas_w)
        canvas_h = max(180, int(round(canvas_w * 9.0 / 16.0)))

        self.pet_preview_info = ttk.Label(
            preview_box, text="", style="Hint.TLabel", anchor="center")
        self.pet_preview_info.pack(fill="x", pady=(0, 4))
        self.pet_preview = tk.Canvas(
            preview_box, width=canvas_w, height=canvas_h, bg="#e8eef6",
            highlightthickness=0, bd=0,
        )
        self.pet_preview.pack(anchor="center")
        # 窗口缩放 / DPI 变化时 Canvas 实际宽度会变，重算缩放因子。
        self.pet_preview.bind("<Configure>", self._schedule_preview_redraw)

        size_box = ttk.LabelFrame(parent, text="桌宠大小", padding=10)
        size_box.pack(fill="x", pady=(10, 0))
        self.pet_size_var = tk.DoubleVar(value=self.pet_size())
        self.pet_size_label = ttk.Label(size_box, text="%d px" % self.pet_size())
        self.pet_size_label.pack(side="right")
        ttk.Scale(
            size_box, from_=PET_MIN_SIZE, to=PET_MAX_SIZE, orient="horizontal",
            variable=self.pet_size_var, command=self._on_pet_size_slide,
        ).pack(side="left", fill="x", expand=True, padx=(0, 12))
        ttk.Label(parent, text="范围 %d - %d px，拖动时效果图实时缩放并保存到 config.json"
                  % (PET_MIN_SIZE, PET_MAX_SIZE),
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        reset_box = ttk.LabelFrame(parent, text="恢复出厂设置", padding=10)
        reset_box.pack(fill="x", pady=(10, 0))
        ttk.Button(reset_box, text="重置大小",
                   command=self.reset_pet_size).pack(side="left")
        ttk.Button(reset_box, text="重置位置",
                   command=self.reset_pet_position).pack(side="left", padx=(10, 0))
        ttk.Button(reset_box, text="一键重置",
                   command=self.reset_pet_all).pack(side="left", padx=(10, 0))
        ttk.Label(reset_box,
                  text="出厂默认：%d px，右下角 %d px 边距" % (DEFAULT_SIZE, DEFAULT_X),
                  style="Hint.TLabel").pack(side="right")

        ttk.Label(parent,
                  text="提示：提醒弹出时按住桌宠即可拖动，松手后位置自动记忆。",
                  style="Hint.TLabel").pack(anchor="w", pady=(10, 0))
        self._redraw_pet_preview()

    def _preview_font(self, size, bold=False):
        """预览用的测量字体：按实际字号缓存，避免每次重绘都新建 Tk 字体。"""
        key = (int(size), bool(bold))
        font = self._preview_fonts.get(key)
        if font is None:
            font = tkfont.Font(root=self.root, family="Microsoft YaHei UI",
                               size=int(size),
                               weight="bold" if bold else "normal")
            self._preview_fonts[key] = font
        return font

    def _preview_pet_source(self):
        if not PIL_AVAILABLE:
            return None
        if self._pet_source_image is None:
            path = pet_path()
            if not os.path.isfile(path):
                return None
            self._pet_source_image = Image.open(path).convert("RGBA")
        return self._pet_source_image

    def _preview_pet_photo_px(self, width, height):
        """按“精确像素”缩放桌宠，保证预览里的长宽比例与真实弹窗一致。"""
        try:
            source = self._preview_pet_source()
            if source is None:
                return None
            resized = source.resize(
                (max(1, int(round(width))), max(1, int(round(height)))),
                getattr(Image, "Resampling", Image).LANCZOS,
            )
            return ImageTk.PhotoImage(resized, master=self.root)
        except Exception:
            return None

    def _preview_real_layout(self, size):
        """完全复刻 ReminderWindow._build_canvas 的尺寸公式（单位：屏幕像素）。

        预览只是把这套真实尺寸整体乘以缩放因子，因此比例与真机弹窗一致。
        """
        screen_w = max(1, self.root.winfo_screenwidth())
        screen_h = max(1, self.root.winfo_screenheight())

        source = self._preview_pet_source()
        if source is not None:
            src_w, src_h = source.size
        else:
            src_w = src_h = 1
        # 与 ReminderWindow._load_pet 一致：最长边缩放到 size，另一边按比例。
        if src_w >= src_h:
            pet_w = max(1, int(size))
            pet_h = max(1, int(round(pet_w * src_h / float(src_w))))
        else:
            pet_h = max(1, int(size))
            pet_w = max(1, int(round(pet_h * src_w / float(src_h))))

        clock_size = 58
        fixed_width = 12 + pet_w + 16 + 14 + clock_size + 12 + 24
        available_bubble = max(180, screen_w - fixed_width)
        max_bubble_w = min(BUBBLE_MAX_WIDTH, available_bubble)

        title_text = render_text(
            self.config.get("default_template") or DEFAULT_TEMPLATE,
            {"name": "高等数学"}).strip() or DEFAULT_TEMPLATE
        sub_text = "还有 5 分钟 · 高等数学"
        max_text_w = max(120, max_bubble_w - 36)
        lines = [title_text]
        sub_lines = [sub_text]
        bubble_w = min(220, max_bubble_w)
        bubble_h = 104
        title_font = self._preview_font(13, bold=True)
        sub_font = self._preview_font(9)
        for font_size in (13, 12, 11, 10, 9, 8):
            title_font = self._preview_font(font_size, bold=True)
            sub_font = self._preview_font(max(8, font_size - 4))
            lines = wrap_text(title_text, title_font, max_text_w)
            sub_lines = wrap_text(sub_text, sub_font, max_text_w)
            text_w = min(max_text_w, max([title_font.measure(line) for line in lines] or [1]))
            sub_w = max([sub_font.measure(line) for line in sub_lines] or [0])
            bubble_w = max(180, min(max_bubble_w, max(text_w, sub_w) + 36))
            title_h = len(lines) * title_font.metrics("linespace")
            sub_h = len(sub_lines) * sub_font.metrics("linespace")
            bubble_h = max(104, 20 + title_h + 8 + sub_h + 18)
            if bubble_h <= screen_h - 140 or font_size == 8:
                break
        if bubble_h > screen_h - 120:
            bubble_h = max(120, screen_h - 120)

        width = 12 + pet_w + 16 + bubble_w + 14 + clock_size + 12
        height = max(pet_h + 24, bubble_h + 24, clock_size + 24)
        return {
            "pet_w": pet_w, "pet_h": pet_h,
            "bubble_w": bubble_w, "bubble_h": bubble_h,
            "clock_size": clock_size, "width": width, "height": height,
            "title_font": title_font, "sub_font": sub_font,
            "lines": lines, "sub_lines": sub_lines,
        }

    def _preview_pixel_font(self, px, bold=False):
        key = ("px", int(px), bool(bold))
        font = self._preview_fonts.get(key)
        if font is None:
            font = tkfont.Font(root=self.root, family="Microsoft YaHei UI",
                               size=-max(1, int(px)),
                               weight="bold" if bold else "normal")
            self._preview_fonts[key] = font
        return font

    def _fit_pixel_font(self, px, bold, lines, max_width):
        """在极小字号下逐像素收缩，保证文字实测宽度不超过气泡内宽。"""
        px = max(1, int(px))
        while px > 1:
            font = self._preview_pixel_font(px, bold)
            widest = max([font.measure(line) for line in lines] or [0])
            if widest <= max_width:
                return px, font
            px -= 1
        return 1, self._preview_pixel_font(1, bold)

    def _preview_background_photo(self, width, height, force_reload=False):
        """previews 下的背景图等比例完整显示（contain），不裁掉任何内容。

        缩放后不足的部分用黑边补齐，因此带任务栏的整张壁纸都会完整留在
        Canvas 里；返回 None 表示没有背景图，调用方使用纯色默认背景。
        """
        if not PIL_AVAILABLE or width <= 1 or height <= 1:
            return None
        path = preview_bg_path()
        if not path:
            self._preview_bg_photo = None
            self._preview_bg_key = None
            self._preview_bg_name = None
            self._preview_bg_frame = None
            self._preview_bg_draw_rect = None
            return None
        try:
            key = (path, os.path.getmtime(path), os.path.getsize(path),
                   int(width), int(height))
            if (not force_reload and key == self._preview_bg_key
                    and self._preview_bg_photo is not None):
                return self._preview_bg_photo
            with Image.open(path) as raw:
                image = raw.convert("RGB")
            src_w, src_h = image.size
            if src_w <= 0 or src_h <= 0:
                return None
            # cover：等比放大到刚好铺满整个 Canvas，优先保留底部任务栏，
            # 多出来的部分从顶部裁掉（左右居中裁），不再留黑边。
            scale = max(width / float(src_w), height / float(src_h))
            new_w = max(1, int(round(src_w * scale)))
            new_h = max(1, int(round(src_h * scale)))
            image = image.resize((new_w, new_h),
                                 getattr(Image, "Resampling", Image).LANCZOS)
            left = max(0, (new_w - int(width)) // 2)
            top = max(0, new_h - int(height))
            frame = image.crop((left, top, left + int(width), top + int(height)))
            photo = ImageTk.PhotoImage(frame, master=self.root)
            self._preview_bg_photo = photo
            self._preview_bg_key = key
            self._preview_bg_name = os.path.basename(path)
            self._preview_bg_frame = frame
            self._preview_bg_draw_rect = (0, 0, int(width), int(height))
            return photo
        except Exception:
            self._preview_bg_photo = None
            self._preview_bg_key = None
            self._preview_bg_name = None
            self._preview_bg_frame = None
            self._preview_bg_draw_rect = None
            return None

    # ---------------------- 预览区任务栏时钟 ----------------------

    def _clock_font_family(self):
        """优先使用 Windows 默认无衬线中文字体。"""
        cached = getattr(self, "_clock_family", None)
        if cached:
            return cached
        try:
            families = set(tkfont.families(self.root))
        except tk.TclError:
            families = set()
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                     "Microsoft JhengHei UI", "Arial"):
            if name in families:
                self._clock_family = name
                return name
        self._clock_family = "TkDefaultFont"
        return self._clock_family

    def _clock_texts(self):
        now = dt.datetime.now()
        return now.strftime("%H:%M"), "%d/%d/%d" % (now.year, now.month, now.day)

    def _taskbar_top(self):
        """从背景图底部向上找任务栏的上边缘（返回 Canvas 坐标 y）。"""
        frame = getattr(self, "_preview_bg_frame", None)
        if frame is None:
            return None
        width, height = frame.size
        if width <= 2 or height <= 2:
            return None
        step_x = max(1, width // 120)
        columns = list(range(0, width, step_x))
        bottom = {}
        for x in columns:
            try:
                bottom[x] = frame.getpixel((x, height - 1))
            except Exception:
                return None
        limit = int(height * 0.80)
        for y in range(height - 1, limit, -1):
            matched = 0
            total = 0
            for x in columns:
                try:
                    pixel = frame.getpixel((x, y))
                except Exception:
                    continue
                base = bottom[x]
                total += 1
                if (abs(pixel[0] - base[0]) + abs(pixel[1] - base[1])
                        + abs(pixel[2] - base[2])) <= 60:
                    matched += 1
            if total and matched / float(total) < 0.55:
                return y + 1
        return limit

    def _taskbar_base_color(self, y1, y2):
        """任务栏底色：对整条任务栏取中位数，图标只占少数，不影响结果。"""
        frame = self._preview_bg_frame
        width, height = frame.size
        step_x = max(1, width // 80)
        rows = list(range(int(y1) + 1, max(int(y1) + 2, int(y2)), 3))
        samples = []
        for x in range(0, width, step_x):
            for y in rows:
                try:
                    samples.append(frame.getpixel((x, y)))
                except Exception:
                    continue
        if not samples:
            return (32, 66, 100)
        count = len(samples)
        return tuple(sorted(pixel[i] for pixel in samples)[count // 2]
                     for i in range(3))

    def _taskbar_tray_left(self, base, y1, y2):
        """从右往左扫任务栏，定位托盘区最左边（时钟+网络/音量/电池等图标）。"""
        frame = self._preview_bg_frame
        width, height = frame.size
        rows = list(range(int(y1) + 1, max(int(y1) + 2, int(y2)), 2))
        if not rows:
            return None
        step_x = max(1, width // 500)

        def active(x):
            hits = 0
            total = 0
            for y in rows:
                try:
                    pixel = frame.getpixel((x, y))
                except Exception:
                    continue
                total += 1
                if (abs(pixel[0] - base[0]) + abs(pixel[1] - base[1])
                        + abs(pixel[2] - base[2])) > 45:
                    hits += 1
            return total > 0 and hits / float(total) > 0.18

        gap = 0
        seen_active = False
        limit = int(width * 0.60)
        x = width - 1
        while x > limit:
            if active(x):
                seen_active = True
                gap = 0
            else:
                gap += step_x
                if seen_active and gap >= 48:
                    # 托盘已经结束（图标之间的小空档不算），再往左是任务栏空白区。
                    return x + gap
            x -= step_x
        return None

    def _taskbar_clock_box(self):
        """时钟遮罩：覆盖托盘图标 + 时钟，高度严格在任务栏内部。"""
        frame = getattr(self, "_preview_bg_frame", None)
        if frame is None:
            return None
        width, height = frame.size
        top = self._taskbar_top()
        if top is None or top >= height - 2:
            return None
        taskbar_h = height - top
        # 没有识别出任务栏（或底边是一大块纯色壁纸）时不画遮罩，免得糊一块色块。
        if taskbar_h > height * 0.22:
            return None
        # 上边从任务栏上边缘内缩 1 px，下边一直贴到画布底边（即任务栏底边）。
        y1 = float(top + 1)
        y2 = float(height)
        box_h = max(4.0, y2 - y1)
        x2 = float(width)
        # 默认只盖时钟；识别到托盘图标时向左扩展，把网络/音量/电池一起盖掉，
        # 但不越过任务栏中间的应用图标区（最多占到画布 62% 处）。
        box_w = min(width * 0.16, max(box_h * 3.4, 64.0))
        x1 = x2 - box_w
        tray_left = self._taskbar_tray_left(self._taskbar_base_color(y1, y2),
                                           y1, y2)
        if tray_left is not None:
            # 再向左多留 34 px：既避免图标边缘残留半截，也盖掉托盘左侧的
            # “显示隐藏图标”箭头 ^。
            x1 = min(x1, float(tray_left) - 34.0)
        x1 = max(x1, width * 0.62)
        x1 = min(x1, x2 - 24.0)
        return (x1, y1, x2, y2)

    def _clock_background_color(self, box):
        """取任务栏里（时钟左侧）纯色区域的颜色作为遮罩底色。"""
        frame = getattr(self, "_preview_bg_frame", None)
        if frame is None:
            return "#1f3b57"
        x1, y1, x2, y2 = box
        xs = range(int(max(0, x1 - 26)), int(max(1, x1 - 8)))
        ys = range(int(y1 + 2), int(max(y1 + 3, y2 - 1)), 2)
        samples = []
        for x in xs:
            for y in ys:
                try:
                    samples.append(frame.getpixel((int(x), int(y))))
                except Exception:
                    continue
        if not samples:
            samples = [frame.getpixel((int(x2 - 4), int(y1 + 2)))]
        count = len(samples)
        channels = [sorted(pixel[i] for pixel in samples)[count // 2]
                    for i in range(3)]
        return "#%02x%02x%02x" % tuple(channels)

    def _draw_taskbar_clock(self, canvas, width, height):
        """在任务栏右下角画两行黑色时间：HH:MM + YYYY/M/D，整体靠右。"""
        canvas.delete("taskbar_clock")
        box = self._taskbar_clock_box()
        if box is None:
            return
        x1, y1, x2, y2 = box
        canvas.create_rectangle(x1, y1, x2, y2,
                                fill=self._clock_background_color(box),
                                outline="", tags="taskbar_clock")
        time_text, date_text = self._clock_texts()
        right_x = width - 15
        inner_w = max(6.0, right_x - x1 - 2)
        inner_h = max(6.0, (y2 - y1) - 2)
        # 粗体、日期略大，保证小字号下依然清晰。
        time_px, date_px = 1, 1
        for candidate in range(max(1, int(inner_h)), 0, -1):
            small = max(1, int(round(candidate * 0.86)))
            big_font = self._preview_pixel_font(candidate, True)
            small_font = self._preview_pixel_font(small, True)
            total_h = (big_font.metrics("linespace")
                       + small_font.metrics("linespace"))
            if total_h > inner_h:
                continue
            if big_font.measure(time_text) > inner_w:
                continue
            if small_font.measure(date_text) > inner_w:
                continue
            time_px, date_px = candidate, small
            break
        time_font = self._preview_pixel_font(time_px, True)
        date_font = self._preview_pixel_font(date_px, True)
        canvas.create_text(right_x, y1 + (y2 - y1) * 0.32, text=time_text,
                           anchor="e", fill="#000000", font=time_font,
                           tags="taskbar_clock")
        canvas.create_text(right_x, y1 + (y2 - y1) * 0.74, text=date_text,
                           anchor="e", fill="#000000", font=date_font,
                           tags="taskbar_clock")

    def _tick_clock(self):
        """每分钟刷新一次预览里的时间。"""
        self._clock_job = None
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        self._redraw_pet_preview()
        if self._clock_job is None:
            self._clock_job = self.root.after(60000, self._tick_clock)

    def _redraw_pet_preview(self):
        canvas = getattr(self, "pet_preview", None)
        if canvas is None:
            return
        self._preview_redraw_job = None

        screen_w = max(1, self.root.winfo_screenwidth())
        screen_h = max(1, self.root.winfo_screenheight())
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width <= 1:
            width = int(canvas.cget("width"))
        if height <= 1:
            height = int(canvas.cget("height"))
        width = max(1, width)
        height = max(1, height)

        # 缩放因子 = 预览区宽度 / 真实屏幕宽度；Canvas 固定 16:9，
        # 桌宠/气泡一律乘以该因子绘制，保证占屏比例与真实桌面一致。
        scale = width / float(screen_w)
        size = self.pet_size()

        canvas.delete("all")
        background = self._preview_background_photo(width, height)
        if background is not None:
            canvas.create_image(0, 0, image=background, anchor="nw")
        else:
            # 没有背景图时用最开始的纯色默认背景，绝不读取系统壁纸。
            canvas.create_rectangle(0, 0, width, height,
                                    fill="#e3ebf5", outline="#c3cfdd")
        # 背景图铺好后，先在右下角盖掉旧时间并绘制当前系统时间。
        self._draw_taskbar_clock(canvas, width, height)
        if self._clock_job is None:
            self._clock_job = self.root.after(60000, self._tick_clock)

        layout = self._preview_real_layout(size)
        margin_x, margin_y = self.pet_margins()

        real_w, real_h = layout["width"], layout["height"]
        win_w, win_h = real_w * scale, real_h * scale
        # 与真机“距右侧 / 距底部”的边距一致，贴住预览区的右下角。
        x = width - win_w - margin_x * scale
        y = height - win_h - margin_y * scale
        x = max(0.0, min(width - win_w, x))
        y = max(0.0, min(height - win_h, y))

        # 真机弹窗是透明底（只显示桌宠、气泡和时钟），这里也不画窗口底色，
        # 只用极淡的投影勾出“弹窗在屏幕上的占位范围”。
        pet_w = layout["pet_w"] * scale
        pet_h = layout["pet_h"] * scale
        photo = self._preview_pet_photo_px(pet_w, pet_h)
        pet_y = y + max(0.0, (win_h - pet_h) / 2.0)
        # 600 px 上限下也保证桌宠完整画在预览区内，不越界。
        pet_y = max(0.0, min(max(0.0, height - pet_h), pet_y))
        if photo is not None:
            self._pet_preview_photo = photo
            canvas.create_image(x + 12 * scale, pet_y, image=photo, anchor="nw")
        else:
            canvas.create_rectangle(x + 12 * scale, pet_y,
                                    x + 12 * scale + pet_w, pet_y + pet_h,
                                    fill="#cbd5e1", outline="#94a3b8")

        bx = x + (12 + layout["pet_w"] + 16) * scale
        bw = layout["bubble_w"] * scale
        bh = layout["bubble_h"] * scale
        by = y + max(10.0 * scale, (win_h - bh) / 2.0)
        by = max(0.0, min(max(0.0, height - bh), by))
        radius = max(1.0, 18 * scale)
        canvas.create_rectangle(bx + 1, by + 2, bx + bw + 1, by + bh + 2,
                                fill="#dce2ea", outline="")
        rounded_rect(canvas, bx, by, bx + bw, by + bh, radius,
                     fill="#ffffff", outline="#cfd8e3", width=1)

        pad_x = max(1.0, 18 * scale)
        text_w = max(1, int(bw - 2 * pad_x))
        title_px = max(1, int(round(layout["title_font"].metrics("linespace") * scale)))
        sub_px = max(1, int(round(layout["sub_font"].metrics("linespace") * scale)))
        # 极小字号下 Windows 字体宽度不再严格线性，按实测逐级缩小，
        # 避免文字比气泡还宽（真机里文字始终包在气泡内）。
        title_px, title_font = self._fit_pixel_font(
            title_px, True, layout["lines"], text_w)
        sub_px, sub_font = self._fit_pixel_font(
            sub_px, False, layout["sub_lines"], text_w)
        canvas.create_text(
            bx + pad_x, by + pad_x,
            text="\n".join(layout["lines"]), anchor="nw", width=text_w,
            font=title_font, fill="#1f2937",
        )
        canvas.create_text(
            bx + pad_x, by + bh - pad_x,
            text="\n".join(layout["sub_lines"]), anchor="sw", width=text_w,
            font=sub_font, fill="#6b7280",
        )

        # 时钟：真机窗口右侧 58 px 的圆形闹钟。
        clock_real = layout["clock_size"]
        cx = x + (12 + layout["pet_w"] + 16 + layout["bubble_w"] + 14
                  + clock_real / 2.0) * scale
        cy = y + win_h / 2.0
        radius = (clock_real / 2.0 - 4) * scale
        if radius >= 1:
            canvas.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                               fill="#f8fafc", outline="#94a3b8",
                               width=1 if radius < 8 else 2)
            if radius >= 5:
                for number in range(12):
                    angle = math.radians(number * 30 - 90)
                    inner = radius * (0.7 if number % 3 == 0 else 0.82)
                    canvas.create_line(cx + math.cos(angle) * inner,
                                       cy + math.sin(angle) * inner,
                                       cx + math.cos(angle) * (radius - 1),
                                       cy + math.sin(angle) * (radius - 1),
                                       fill="#94a3b8", width=1)
            canvas.create_line(cx, cy, cx, cy - radius * 0.55,
                               fill="#1f2937", width=1 if radius < 10 else 2)
            canvas.create_line(cx, cy, cx + radius * 0.7, cy,
                               fill="#1f2937", width=1 if radius < 10 else 2)

        info = getattr(self, "pet_preview_info", None)
        if info is not None:
            bg_name = getattr(self, "_preview_bg_name", None)
            suffix = (" · 背景 %s（完整显示）" % bg_name) if bg_name else " · 默认纯色背景"
            info.config(
                text=("模拟屏幕 %d × %d · 16:9 预览 · 缩放 %.0f%% · 桌宠 %d px%s"
                      % (screen_w, screen_h, scale * 100, size, suffix)))

    def _schedule_preview_redraw(self, _event=None):
        """窗口尺寸变化时稍作合并再重绘，避免拖动边框时频繁重算。"""
        if self._preview_redraw_job:
            try:
                self.root.after_cancel(self._preview_redraw_job)
            except tk.TclError:
                pass
        self._preview_redraw_job = self.root.after(60, self._redraw_pet_preview)


    def _on_pet_size_slide(self, value):
        try:
            size = self.set_pet_size(value, save=False)
        except (TypeError, ValueError):
            return
        self.pet_size_label.config(text="%d px" % size)
        self._redraw_pet_preview()
        self._schedule_pet_save()

    def _sync_pet_settings(self):
        """config.json 载入（或重置）后，让滑块与效果图跟上真实数值。"""
        size = self.pet_size()
        try:
            self.pet_size_var.set(size)
            self.pet_size_label.config(text="%d px" % size)
        except (tk.TclError, AttributeError):
            pass
        self._redraw_pet_preview()

    def reset_pet_size(self):
        if not messagebox.askyesno(
                APP_TITLE, "确认重置大小吗？\n桌宠大小将恢复为出厂默认 %d px。"
                % DEFAULT_SIZE, parent=self.root):
            return
        self.set_pet_size(DEFAULT_SIZE)
        self._sync_pet_settings()
        self._set_status("桌宠大小已重置为 %d px" % DEFAULT_SIZE)

    def reset_pet_position(self):
        if not messagebox.askyesno(
                APP_TITLE,
                "确认重置位置吗？\n桌宠将回到屏幕右下角（距右侧/底部 %d px）。"
                % DEFAULT_X, parent=self.root):
            return
        self.config["pet_x"] = DEFAULT_X
        self.config["pet_y"] = DEFAULT_Y
        self._save_config_now()
        self._sync_pet_settings()
        self._set_status("桌宠位置已重置到右下角")

    def reset_pet_all(self):
        if not messagebox.askyesno(
                APP_TITLE, "确认一键重置吗？\n桌宠大小和位置都将恢复出厂默认。",
                parent=self.root):
            return
        self.set_pet_size(DEFAULT_SIZE, save=False)
        self.config["pet_x"] = DEFAULT_X
        self.config["pet_y"] = DEFAULT_Y
        self._save_config_now()
        self._sync_pet_settings()
        self._set_status("桌宠大小和位置已恢复出厂默认")

    # ------------------------------ 数据 ------------------------------

    def _load_all(self):
        self.schedule = load_schedule()
        raw_courses = load_json(COURSES_PATH, None)
        if isinstance(raw_courses, list):
            self.term_start = None
            self.term_end = None
            raw_list = raw_courses
        elif isinstance(raw_courses, dict):
            self.term_start = parse_date(str(raw_courses.get("term_start", "")))
            self.term_end = parse_date(str(raw_courses.get("term_end", "")))
            raw_list = raw_courses.get("courses", [])
        else:
            self.term_start = None
            self.term_end = None
            raw_list = []
        courses = []
        today = dt.date.today()
        for item in raw_list:
            course = normalize_course(item)
            if course is None:
                continue
            # 旧数据没有课程日期时，用学期日期作为默认值；留空则视为不限制。
            if not course.get("start_date"):
                course["start_date"] = format_date(self.term_start)
            if not course.get("end_date"):
                course["end_date"] = format_date(self.term_end)
            # 结束日期已过的课程在启动时静默清掉；没有结束日期的课程保留。
            if course_is_expired(course, today):
                continue
            courses.append(course)
        self.courses = sort_courses(courses)
        try:
            self._save_courses()
        except Exception:
            pass

        raw_config = load_json(CONFIG_PATH, None)
        if isinstance(raw_config, dict):
            config = dict(raw_config)
            template = str(config.get("default_template", "")).strip()
            extra_template = str(config.get("extra_template", "")).strip()
            config["default_template"] = template or DEFAULT_TEMPLATE
            extra_template = extra_template or DEFAULT_EXTRA_TEMPLATE
            # 旧版本保存过 {minutes}，升级后按额外提醒的 {leave_minutes} 处理。
            extra_template = extra_template.replace("{minutes}", "{leave_minutes}")
            config["extra_template"] = extra_template
            self.config = config
        else:
            self.config = {"default_template": DEFAULT_TEMPLATE,
                           "extra_template": DEFAULT_EXTRA_TEMPLATE}
        try:
            save_json(CONFIG_PATH, self.config)
        except OSError:
            pass
        self.template_var.set(self.config["default_template"])
        self.extra_template_var.set(self.config["extra_template"])
        self.term_var.set(format_date(self.term_start))
        self.term_end_var.set(format_date(self.term_end))
        self._sync_pet_settings()

        raw_makeup = load_json(MAKEUP_PATH, None)
        items = []
        if isinstance(raw_makeup, list):
            items = [m for m in (normalize_makeup(item) for item in raw_makeup) if m]
        seen = set()
        self.makeup = []
        for item in sorted(items, key=lambda m: m["date"]):
            if item["date"] in seen:
                continue
            seen.add(item["date"])
            self.makeup.append(item)
        if raw_makeup is None:
            try:
                save_json(MAKEUP_PATH, [])
            except OSError:
                pass
        self.makeup_index = build_makeup_index(self.makeup)

        raw_holidays = load_json(HOLIDAYS_PATH, None)
        self.holidays = raw_holidays if isinstance(raw_holidays, list) else []
        if raw_holidays is None:
            try:
                save_json(HOLIDAYS_PATH, [])
            except OSError:
                pass
        self.holiday_index = build_holiday_index(self.holidays)

    def _save_courses(self):
        try:
            self.courses = sort_courses(self.courses)
            save_json(COURSES_PATH, {
                "term_start": format_date(self.term_start),
                "term_end": format_date(self.term_end),
                "courses": self.courses,
            })
            return True
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 courses.json 失败：%s" % exc)
            return False

    def _course_row_values(self, course):
        text = course.get("text", "").replace("\n", " ")
        if course.get("extra_enabled"):
            extra_note = " +%d次额外" % len(course.get("extras", []))
        else:
            extra_note = ""
        return (
            course["name"],
            WEEKDAY_NAMES[course["weekday"] - 1],
            course["start"],
            course["end"],
            course.get("place", ""),
            course.get("advance", 0),
            course_date_text(course),
            text + extra_note,
        )

    def _refresh_courses(self):
        if self.hidden:
            return
        self.courses = sort_courses(self.courses)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, course in enumerate(self.courses):
            self.tree.insert("", "end", iid=str(index),
                             values=self._course_row_values(course))
        self._apply_course_column_widths()
        self.count_label.configure(text="共 %d 门课程" % len(self.courses))
        # 刷新后始终回到最左侧，优先显示课程名与星期。
        self.tree.xview_moveto(0)
        self.tree.yview_moveto(0)
        if self.timetable_window is not None:
            try:
                if self.timetable_window.win.winfo_exists():
                    self.timetable_window.refresh(self.courses, self.schedule)
                else:
                    self.timetable_window = None
            except tk.TclError:
                self.timetable_window = None

    def _get_course_tree_fonts(self):
        if self._course_tree_fonts is None:
            style = ttk.Style(self.root)
            cell_spec = style.lookup("Treeview", "font") or (
                "Microsoft YaHei UI", 9)
            head_spec = style.lookup("Treeview.Heading", "font") or (
                "Microsoft YaHei UI", 9, "bold")
            cell_font = tkfont.Font(root=self.root, font=cell_spec)
            head_font = tkfont.Font(root=self.root, font=head_spec)
            self._course_tree_fonts = (cell_font, head_font)
        return self._course_tree_fonts

    def _measure_course_column_widths(self):
        """按表头和当前所有单元格的最长像素宽度 + 30px 计算列宽。"""
        cell_font, head_font = self._get_course_tree_fonts()
        labels = {key: label for key, label, _width, _anchor
                  in self._course_columns}
        widths = {}
        for key, label, _width, _anchor in self._course_columns:
            widest = head_font.measure(label)
            for course in self.courses:
                value = self._course_row_values(course)[
                    [item[0] for item in self._course_columns].index(key)]
                widest = max(widest, cell_font.measure(str(value)))
            widths[key] = max(50, widest + 30)
        return widths

    def _saved_course_column_widths(self):
        raw = self.config.get("course_column_widths")
        if not isinstance(raw, dict):
            return None
        widths = {}
        for key, _label, _width, _anchor in self._course_columns:
            try:
                value = int(raw[key])
            except (KeyError, TypeError, ValueError):
                return None
            if value < 40:
                return None
            widths[key] = value
        return widths

    def _apply_course_column_widths(self, force_adaptive=False):
        widths = None if force_adaptive else self._saved_course_column_widths()
        if not widths:
            widths = self._measure_course_column_widths()
        self._applying_column_widths = True
        try:
            for key, _label, _width, _anchor in self._course_columns:
                self.tree.column(key, width=widths[key])
        finally:
            self._applying_column_widths = False
        return widths

    def _course_width_snapshot(self):
        return {key: int(self.tree.column(key, "width"))
                for key, _label, _width, _anchor in self._course_columns}

    def _on_column_press(self, _event=None):
        if self._applying_column_widths:
            return
        self._widths_before_drag = self._course_width_snapshot()

    def _remember_column_widths(self, event=None):
        if self._applying_column_widths:
            return
        before = self._widths_before_drag
        self._widths_before_drag = None
        if before is None or self._course_width_snapshot() == before:
            return
        if self._column_width_job:
            try:
                self.root.after_cancel(self._column_width_job)
            except tk.TclError:
                pass
        self._column_width_job = self.root.after(80, self._persist_column_widths)

    def _persist_column_widths(self):
        self._column_width_job = None
        self._widths_before_drag = None
        if self._applying_column_widths:
            return
        widths = {}
        for key, _label, _width, _anchor in self._course_columns:
            widths[key] = int(self.tree.column(key, "width"))
        if widths != self.config.get("course_column_widths"):
            self.config["course_column_widths"] = widths
            self._save_config_now()

    def reset_course_column_widths(self):
        """清除列宽记忆，按当前课程内容重新自适应一次。"""
        if self._column_width_job:
            try:
                self.root.after_cancel(self._column_width_job)
            except tk.TclError:
                pass
            self._column_width_job = None
        self.config.pop("course_column_widths", None)
        self._save_config_now()
        self._apply_course_column_widths(force_adaptive=True)
        self.tree.xview_moveto(0)
        self._set_status("已恢复自适应列宽")

    def _refresh_makeup(self):
        if self.hidden:
            return
        for item in self.makeup_tree.get_children():
            self.makeup_tree.delete(item)
        for entry in self.makeup:
            self.makeup_tree.insert("", "end", iid=entry["date"], values=(
                entry["date"],
                WEEKDAY_NAMES[entry["follow_weekday"] - 1],
            ))

    def _selected_course_index(self):
        selection = self.tree.selection()
        if not selection:
            return None
        try:
            return int(selection[0])
        except (TypeError, ValueError):
            return None

    def _select_course(self, target):
        for index, course in enumerate(self.courses):
            if course is target:
                self.tree.selection_set(str(index))
                self.tree.see(str(index))
                return

    def show_timetable(self):
        if self.timetable_window is not None:
            try:
                if self.timetable_window.win.winfo_exists():
                    self.timetable_window.refresh(self.courses, self.schedule)
                    self.timetable_window.win.lift()
                    return
            except tk.TclError:
                pass
            self.timetable_window = None
        self.timetable_window = TimetableWindow(self, self.root, self.courses)

    def _on_schedule_saved(self, _periods=None):
        """作息表被自动保存后，主界面与“网格化课表”实时跟随刷新。"""
        try:
            self.schedule = load_schedule()
        except OSError:
            return
        if self.timetable_window is not None:
            try:
                if self.timetable_window.win.winfo_exists():
                    self.timetable_window.refresh(self.courses, self.schedule)
                else:
                    self.timetable_window = None
            except tk.TclError:
                self.timetable_window = None
        if self.schedule:
            self._set_status("作息时间表已自动保存：共 %d 节" % len(self.schedule))
        else:
            self._set_status("作息时间表为空，打开课表会提示先设置")

    def open_schedule_dialog(self):
        # 自动保存模式下窗口没有“确定”，关闭即生效；on_saved 负责实时刷新。
        dialog = ScheduleDialog(self.root, self.schedule,
                                on_saved=self._on_schedule_saved)
        self.root.wait_window(dialog)
        self._on_schedule_saved(dialog.result)

    # ------------------------------ 课程操作 ------------------------------

    def add_course(self):
        dialog = CourseDialog(self.root, "添加课程",
                              term_start=self.term_start, term_end=self.term_end)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        course = normalize_course(dialog.result)
        if course is None:
            messagebox.showerror(APP_TITLE, "课程数据无效，未保存", parent=self.root)
            return
        self.courses.append(course)
        if self._save_courses():
            self._refresh_courses()
            self._select_course(course)
            self._set_status("已添加课程：%s" % course["name"])

    def edit_course(self):
        index = self._selected_course_index()
        if index is None or not 0 <= index < len(self.courses):
            messagebox.showinfo(APP_TITLE, "请先在列表中选择一门课程", parent=self.root)
            return
        old_course = self.courses[index]
        dialog = CourseDialog(self.root, "编辑课程", old_course,
                              term_start=self.term_start, term_end=self.term_end)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        course = normalize_course(dialog.result)
        if course is None:
            messagebox.showerror(APP_TITLE, "课程数据无效，未保存", parent=self.root)
            return
        for position, item in enumerate(self.courses):
            if item is old_course:
                self.courses[position] = course
                break
        if self._save_courses():
            self._refresh_courses()
            self._select_course(course)
            self._set_status("已保存课程：%s" % course["name"])

    def delete_course(self):
        index = self._selected_course_index()
        if index is None or not 0 <= index < len(self.courses):
            messagebox.showinfo(APP_TITLE, "请先在列表中选择一门课程", parent=self.root)
            return
        course = self.courses[index]
        if not messagebox.askyesno(APP_TITLE, "确定删除课程“%s”吗？" % course["name"],
                                   parent=self.root):
            return
        self.courses = [item for item in self.courses if item is not course]
        if self._save_courses():
            self._refresh_courses()
            self._set_status("已删除课程")

    # ------------------------------ 设置 ------------------------------

    def save_term_dates(self):
        start_text = self.term_var.get().strip()
        end_text = self.term_end_var.get().strip()
        start = parse_date(start_text) if start_text else None
        end = parse_date(end_text) if end_text else None
        if start_text and start is None:
            messagebox.showerror(APP_TITLE, "学期开始日期格式应为 YYYY-M-D",
                                 parent=self.root)
            return
        if end_text and end is None:
            messagebox.showerror(APP_TITLE, "学期结束日期格式应为 YYYY-M-D",
                                 parent=self.root)
            return
        if start and end and end < start:
            messagebox.showerror(APP_TITLE, "学期结束日期不能早于开始日期",
                                 parent=self.root)
            return
        self.term_start = start
        self.term_end = end
        if self._save_courses():
            self.term_var.set(format_date(start))
            self.term_end_var.set(format_date(end))
            self._set_status("学期日期已保存")

    def save_template(self):
        template = self.template_var.get().strip()
        if not template:
            messagebox.showerror(APP_TITLE, "默认提醒模板不能为空", parent=self.root)
            return
        self.config["default_template"] = template
        try:
            save_json(CONFIG_PATH, self.config)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 config.json 失败：%s" % exc)
            return
        self._set_status("默认提醒模板已保存")

    def save_extra_template(self):
        template = self.extra_template_var.get().strip()
        if not template:
            messagebox.showerror(APP_TITLE, "全局额外提醒模板不能为空", parent=self.root)
            return
        self.config["extra_template"] = template
        try:
            save_json(CONFIG_PATH, self.config)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 config.json 失败：%s" % exc)
            return
        self._set_status("全局额外提醒模板已保存")

    # ------------------------------ 补班映射 ------------------------------

    def add_makeup(self):
        dialog = MakeupDialog(self.root)
        self.root.wait_window(dialog)
        if not dialog.result:
            return
        entry = dialog.result
        self.makeup = [m for m in self.makeup if m["date"] != entry["date"]]
        self.makeup.append(entry)
        self.makeup.sort(key=lambda m: m["date"])
        try:
            save_json(MAKEUP_PATH, self.makeup)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 makeup.json 失败：%s" % exc)
            return
        self.makeup_index = build_makeup_index(self.makeup)
        self._refresh_makeup()
        self._set_status("补班映射已保存：%s → %s" % (
            entry["date"], WEEKDAY_NAMES[entry["follow_weekday"] - 1]))

    def delete_makeup(self):
        selection = self.makeup_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "请先选择一条补班映射", parent=self.root)
            return
        date_text = selection[0]
        if not messagebox.askyesno(APP_TITLE, "确定删除 %s 的补班映射吗？" % date_text,
                                   parent=self.root):
            return
        self.makeup = [m for m in self.makeup if m["date"] != date_text]
        try:
            save_json(MAKEUP_PATH, self.makeup)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 makeup.json 失败：%s" % exc)
            return
        self.makeup_index = build_makeup_index(self.makeup)
        self._refresh_makeup()
        self._set_status("已删除补班映射")

    # ------------------------------ 节假日 ------------------------------

    def _ensure_holidays(self):
        year = dt.date.today().year
        if holiday_covers_year(self.holidays, year):
            return
        self._set_status("正在获取 %d 年节假日数据…" % year)

        def worker():
            try:
                data = fetch_holidays(year)
                self.holiday_queue.put(("ok", year, data))
            except Exception as exc:
                self.holiday_queue.put(("error", year, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_holiday_queue(self):
        try:
            while True:
                kind, year, payload = self.holiday_queue.get_nowait()
                if kind == "ok":
                    prefix = "%d-" % year
                    kept = [e for e in self.holidays
                            if not str(e.get("date", "")).startswith(prefix)]
                    self.holidays = kept + payload
                    try:
                        save_json(HOLIDAYS_PATH, self.holidays)
                    except OSError:
                        pass
                    self.holiday_index = build_holiday_index(self.holidays)
                    self._set_status("%d 年节假日数据已更新" % year)
                else:
                    if not self.holidays:
                        self.holidays = sample_holidays(year)
                        try:
                            save_json(HOLIDAYS_PATH, self.holidays)
                        except OSError:
                            pass
                        self.holiday_index = build_holiday_index(self.holidays)
                        self._set_status("节假日下载失败，已使用内置示例")
                    else:
                        self._set_status("节假日下载失败，继续使用本地数据")
        except queue.Empty:
            pass
        self.root.after(500, self._poll_holiday_queue)

    def _holiday_status(self, day):
        """返回 (要使用的星期几 或 None, 需要提示的文字)。"""
        info = self.holiday_index.get(day.isoformat())
        if not info:
            return day.isoweekday(), ""
        if info.get("workday"):
            mapping = self.makeup_index.get(day.isoformat())
            if not mapping:
                return None, "补班日未设置补哪天的课，已跳过"
            return int(mapping["follow_weekday"]), ""
        if info.get("holiday"):
            return None, ""
        return day.isoweekday(), ""

    def _course_in_date_range(self, course, today):
        start = parse_date(course.get("start_date", ""))
        end = parse_date(course.get("end_date", ""))
        if start is not None and today < start:
            return False
        if end is not None and today > end:
            return False
        return True

    # ------------------------------ 提醒 ------------------------------

    def _check_pet(self):
        exists = os.path.isfile(pet_path())
        ready = exists and PIL_AVAILABLE
        if ready:
            if not self.reminding:
                self.start_button.state(["!disabled"])
            if self.pet_missing is not False:
                self._set_status("已找到桌宠图片，可以启动提醒")
            self.pet_state.configure(text="桌宠资源：已找到")
        else:
            self.start_button.state(["disabled"])
            if self.reminding:
                self.stop_reminders(silent=True)
            if not exists:
                if self.pet_missing is not True:
                    self._set_status("未找到 assets/pet.png 或 assets/default_pet.png，无法提醒")
                self.pet_state.configure(text="桌宠资源：缺失")
            else:
                if self.pet_missing is not True:
                    self._set_status("未安装 Pillow，无法显示桌宠")
                self.pet_state.configure(text="Pillow：未安装")
        self.pet_missing = not ready
        return ready

    def _pet_watch(self):
        if self.hidden:
            # 后台隐身时不做任何界面刷新，只保留轻量检查。
            self.root.after(2500, self._pet_watch)
            return
        try:
            self._check_pet()
        except tk.TclError:
            return
        self.root.after(2500, self._pet_watch)

    def start_reminders(self, silent=False):
        if not self._check_pet():
            if silent:
                return
            if not os.path.isfile(pet_path()):
                message = "未找到 assets/pet.png 或 assets/default_pet.png，无法提醒"
            else:
                message = "未安装 Pillow，无法显示桌宠"
            messagebox.showwarning(APP_TITLE, message, parent=self.root)
            return
        if self.reminding:
            return
        self.reminding = True
        self.start_button.state(["disabled"])
        self.stop_button.state(["!disabled"])
        self.reminded.clear()
        self.last_note = None
        if self.courses:
            self._set_status("提醒已启动")
        else:
            self._set_status("提醒已启动（尚未添加课程）")
        self._tick()

    def stop_reminders(self, silent=False):
        self.reminding = False
        if self.reminder_job:
            try:
                self.root.after_cancel(self.reminder_job)
            except tk.TclError:
                pass
            self.reminder_job = None
        self.start_button.state(["!disabled"] if self.pet_missing is False else ["disabled"])
        self.stop_button.state(["disabled"])
        self._close_all_reminders()
        if not silent:
            self._set_status("提醒已停止")

    def _tick(self):
        if not self.reminding:
            return
        try:
            self._check_due()
        except Exception:
            pass
        self.reminder_job = self.root.after(5000, self._tick)

    def _check_due(self):
        now = dt.datetime.now()
        today = now.date()
        weekday, note = self._holiday_status(today)
        if note:
            key = (today.isoformat(), note)
            if self.last_note != key:
                self.last_note = key
                self._set_status(note)
        if weekday is None:
            return

        due = []
        for course in self.courses:
            if not self._course_in_date_range(course, today):
                continue
            if course["weekday"] != weekday:
                continue
            try:
                hour, minute = [int(part) for part in course["start"].split(":")]
                start_at = dt.datetime.combine(today, dt.time(hour, minute))
            except (ValueError, TypeError):
                continue
            for reminder in build_course_reminders(course):
                remind_at = start_at - dt.timedelta(minutes=int(reminder["advance"]))
                delta = (now - remind_at).total_seconds()
                if not 0 <= delta <= 60:
                    continue
                key = (today.isoformat(), course["name"], course["start"],
                       course["weekday"], reminder["kind"],
                       reminder["advance"], reminder["index"])
                if key not in self.reminded:
                    due.append((course, reminder, key))

        due.sort(key=lambda item: (
            -item[1]["advance"],
            course_sort_key(item[0]),
            0 if item[1]["kind"] == "base" else 1,
            item[1]["index"],
        ))
        for course, reminder, key in due:
            self.reminded.add(key)
            self._show_reminder(course, reminder)

    def _show_reminder(self, course, reminder=None):
        if not (PIL_AVAILABLE and os.path.isfile(pet_path())):
            return
        reminder = reminder or {
            "kind": "base", "index": 0, "advance": course.get("advance", 0),
            "class_minutes": course.get("advance", 0), "leave_minutes": 0,
            "text": course.get("text", ""),
        }
        custom = (reminder.get("text") or "").strip()
        kind = reminder.get("kind", "base")
        advance = int(reminder.get("advance", 0))
        class_minutes = int(reminder.get("class_minutes", advance))
        leave_minutes = int(reminder.get("leave_minutes", 0))
        if kind == "extra":
            template = custom or (self.config.get("extra_template") or
                                  DEFAULT_EXTRA_TEMPLATE)
            text = render_text(template, course, class_minutes, leave_minutes)
        else:
            template = custom or (self.config.get("default_template") or
                                  DEFAULT_TEMPLATE)
            text = render_text(template, course, class_minutes, 0)

        # 基础提醒是正式提醒：立即结束所有尚未关闭的额外提醒窗口。
        if kind == "base":
            for old_window in list(self.reminder_windows):
                if getattr(old_window, "kind", None) == "extra":
                    old_window.close()

        subtitle = "%s  %s" % (course["start"], course.get("place", ""))
        subtitle = subtitle.strip()
        try:
            window = ReminderWindow(
                self, course, text, subtitle,
                index=len(self.reminder_windows),
                on_close=self._on_reminder_closed,
                kind=kind,
                allow_snooze=(kind == "base"),
            )
        except Exception as exc:
            self._set_status("桌宠窗口创建失败：%s" % exc)
            return
        self.reminder_windows.append(window)
        self._set_status("提醒：%s %s" % (course["name"], course["start"]))

    def _on_reminder_closed(self, window):
        if window in self.reminder_windows:
            self.reminder_windows.remove(window)

    def _close_all_reminders(self, _event=None):
        for window in list(self.reminder_windows):
            window.close()

    def _set_status(self, text):
        if self.hidden:
            self._pending_status = text
            return
        self.status_var.set(text)

    # ------------------------------ 后台常驻 ------------------------------

    def _request_wake(self):
        """子线程调用：只置标志位，真正的 deiconify 交给主线程定时器。"""
        self._wake_requested = True

    def _poll_wake(self):
        if self._quitting:
            return
        if self._wake_requested:
            self._wake_requested = False
            try:
                self.show_window()
            except tk.TclError:
                return
        self._wake_job = self.root.after(400, self._poll_wake)

    def show_window(self):
        """从后台恢复主界面（再次双击 exe 触发）。"""
        if self._quitting:
            return
        was_hidden = self.hidden
        self.hidden = False
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass
        if was_hidden:
            self._refresh_courses()
            self._refresh_makeup()
            self._pending_status = None
            self._set_status("已从后台回到前台，提醒继续运行")

    def hide_to_background(self):
        """点右上角 X：隐藏主窗口，任务栏不留图标，定时器继续跑以准时提醒。"""
        if self._quitting or self.hidden:
            return
        # 隐藏前确保提醒已启动，否则后台常驻形同虚设。
        if not self.reminding:
            try:
                self.start_reminders(silent=True)
            except tk.TclError:
                pass
        if self.timetable_window is not None:
            try:
                self.timetable_window.close()
            except tk.TclError:
                pass
            self.timetable_window = None
        self.hidden = True
        try:
            self.root.withdraw()
        except tk.TclError:
            pass

    def quit_app(self):
        """彻底退出：取消所有定时器、关闭提醒窗口并结束进程。"""
        if self._quitting:
            return
        self._quitting = True
        self.reminding = False
        for job in (self.reminder_job, self._wake_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except tk.TclError:
                    pass
        self.reminder_job = None
        self._wake_job = None
        self._close_all_reminders()
        if self.timetable_window is not None:
            try:
                self.timetable_window.close()
            except tk.TclError:
                pass
            self.timetable_window = None
        if self.single_instance is not None:
            self.single_instance.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        os._exit(0)

    def _toggle_autostart(self):
        """勾选/取消时同步写入或删除 HKCU Run 中的自启项。"""
        want = bool(self.autostart_var.get())
        if set_autostart(want):
            self._set_status(
                "已开启开机自启，登录后静默挂后台" if want else "已关闭开机自启")
        else:
            self.autostart_var.set(not want)
            messagebox.showerror(APP_TITLE, "写入注册表失败，无法修改开机自启",
                                 parent=self.root)

    def on_close(self):
        self.hide_to_background()


SILENT_ARGS = ("--silent", "-s")


def main():
    enable_dpi_awareness()
    # 开机自启时由计划任务/快捷方式传入 --silent 或 -s，启动后直接隐身挂后台。
    silent = any(arg in SILENT_ARGS for arg in sys.argv[1:])
    instance = SingleInstance()
    if instance.already_running:
        # 已经有实例在运行：静默启动时不去打扰它，否则唤醒旧实例显示窗口。
        if not silent:
            instance.signal_existing()
        instance.close()
        return
    root = tk.Tk()
    if silent:
        # 关键：必须紧跟在 Tk() 之后立即 withdraw，
        # 此时还没有任何布局、update 或 mainloop 渲染，窗口不会闪现。
        root.withdraw()
    app = CourseReminderApp(root, instance)
    if silent:
        app.hide_to_background()
    try:
        root.mainloop()
    finally:
        instance.close()


if __name__ == "__main__":
    main()
