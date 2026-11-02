# -*- coding: utf-8 -*-
"""课程提醒

Windows 本地独立运行的课程提醒程序。
仅使用 Python 标准库（tkinter），不调用任何网络 API 或大模型。
除首次自动下载节假日 JSON 外不联网，全程静音。
"""

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
from tkinter import messagebox, ttk

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    Image = None
    ImageTk = None
    PIL_AVAILABLE = False

APP_TITLE = "课程提醒"
DEFAULT_TEMPLATE = "到时间了，现在应该去上{课程名}课"
DEFAULT_EXTRA_TEMPLATE = "还有 {minutes} 分钟就要去上 {课程名} 课了"
TRANSPARENT_KEY = "#010203"
HOLIDAY_URL = "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
HOLIDAY_MIRROR = "https://fastly.jsdelivr.net/gh/NateScarlet/holiday-cn@master/{year}.json"
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# 作息表由用户通过“设置每日作息”自行配置，程序不再写死节次。
# PERIODS / PERIOD_LABELS 在载入 schedule.json 后动态生成。
PERIODS = ()
PERIOD_LABELS = ["自动识别"]
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
    return rows


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
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_weeks(text):
    """把 'all'、'1,2,3'、'1-16' 解析为 'all' 或整数列表。"""
    if isinstance(text, list):
        nums = sorted({int(item) for item in text})
        return nums if nums else "all"
    value = (text or "").strip().lower()
    if not value or value == "all":
        return "all"
    result = set()
    for piece in value.replace("，", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            start, end = int(left), int(right)
            if start > end:
                start, end = end, start
            result.update(range(start, end + 1))
        else:
            result.add(int(piece))
    if not result:
        return "all"
    if min(result) < 1 or max(result) > 60:
        raise ValueError("周次必须在 1-60 之间")
    return sorted(result)


def weeks_to_text(weeks):
    if weeks == "all" or not isinstance(weeks, list) or not weeks:
        return "all"
    nums = sorted({int(item) for item in weeks})
    ranges = []
    start = prev = nums[0]
    for number in nums[1:]:
        if number == prev + 1:
            prev = number
            continue
        ranges.append((start, prev))
        start = prev = number
    ranges.append((start, prev))
    return ",".join(str(a) if a == b else "%d-%d" % (a, b) for a, b in ranges)


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
    if not name or start is None or end is None or not 1 <= weekday <= 7:
        return None
    try:
        advance = int(raw.get("advance", 0) or 0)
    except (TypeError, ValueError):
        advance = 0
    advance = max(0, min(advance, 720))
    try:
        weeks = parse_weeks(raw.get("weeks", "all"))
    except (TypeError, ValueError):
        weeks = "all"

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
        "weeks": weeks,
        "text": str(raw.get("text", "")).strip(),
        "period_from": period_from,
        "period_to": period_to,
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


def render_text(template, course, minutes=None):
    """替换 {课程名} {时间} {地点} 和额外提醒的 {minutes}，只替换一次。"""
    values = {
        "课程名": course.get("name", ""),
        "时间": course.get("start", ""),
        "地点": course.get("place", ""),
        "minutes": "" if minutes is None else str(minutes),
    }
    if not template:
        return ""
    return re.sub(r"\{(课程名|时间|地点|minutes)\}", lambda m: values[m.group(1)], template)


def build_course_reminders(course):
    """把基础提醒和额外提醒合并，按提前分钟数从大到小排序。

    每个提醒保留 kind/index，用于独立去重。即使分钟数相同也互不覆盖。
    """
    reminders = [{
        "kind": "base",
        "index": 0,
        "advance": int(course.get("advance", 0)),
        "text": str(course.get("text", "") or ""),
    }]
    if course.get("extra_enabled"):
        for index, extra in enumerate(course.get("extras", [])[:3]):
            try:
                advance = int(extra.get("advance", 0))
            except (TypeError, ValueError):
                continue
            reminders.append({
                "kind": "extra",
                "index": index,
                "advance": max(0, min(advance, 720)),
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
        max_edge = 300
        if original_w > max_edge or original_h > max_edge:
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
        self.top.bind("<Button-1>", lambda _event: self.close())
        self.canvas.bind("<Button-1>", lambda _event: self.close())
        self.top.bind("<Escape>", lambda _event: self.close())

    def _place_window(self, index):
        screen_w = self.top.winfo_screenwidth()
        screen_h = self.top.winfo_screenheight()
        self.target_x = max(8, screen_w - self.width - 24)
        lowest = max(8, screen_h - self.height - 24)
        stacked = screen_h - self.height - 56 - index * (self.height + 10)
        self.base_y = max(8, min(lowest, stacked))
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
    """自由添加、删除节次并设置起止时间的作息表编辑器。"""

    def __init__(self, parent, periods):
        super().__init__(parent)
        self.result = None
        self.rows = []
        self.title("设置每日作息")
        self.resizable(True, True)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.geometry("720x580")
        self.minsize(620, 420)

        outer = ttk.Frame(self, padding=(14, 12, 14, 10))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="设置每日作息",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="自由添加或删除节次，并填写每节课的开始、结束时间（HH:MM）。"
                 "保存后课表会按这里的节次数量自动重绘。",
            style="Hint.TLabel", wraplength=660,
        ).pack(anchor="w", pady=(4, 10))

        table_head = ttk.Frame(outer)
        table_head.pack(fill="x", pady=(0, 4))
        ttk.Label(table_head, text="节次", width=9).pack(side="left")
        ttk.Label(table_head, text="开始时间", width=13).pack(side="left", padx=(8, 0))
        ttk.Label(table_head, text="结束时间", width=13).pack(side="left", padx=(24, 0))
        ttk.Label(table_head, text="操作", width=10).pack(side="left", padx=(28, 0))

        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
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
        self.bind("<Escape>", lambda _event: self._cancel())

        bottom = ttk.Frame(outer)
        bottom.pack(fill="x", pady=(10, 0))
        ttk.Button(bottom, text="添加节次", command=self._add_row).pack(side="left")
        ttk.Button(bottom, text="取消", command=self._cancel).pack(
            side="right", padx=(8, 0))
        ttk.Button(bottom, text="保存作息", command=self._save).pack(side="right")
        self.count_label = ttk.Label(bottom, text="", style="Hint.TLabel")
        self.count_label.pack(side="left", padx=(12, 0))

        for item in periods or []:
            self._add_row(item.get("start"), item.get("end"), scroll=False)
        if not self.rows:
            self._add_row()
        self._renumber()

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
        if self.rows:
            self.rows[0]["start_entry"].focus_set()

    def _on_mousewheel(self, event):
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")

    def _default_times(self):
        if self.rows:
            previous_end = parse_time(self.rows[-1]["end"].get())
            start_minutes = time_to_minutes(previous_end) if previous_end else 8 * 60
        else:
            start_minutes = 8 * 60
        end_minutes = start_minutes + 45
        if end_minutes >= 24 * 60:
            start_minutes = 8 * 60
            end_minutes = 8 * 60 + 45
        return minutes_to_time(start_minutes), minutes_to_time(end_minutes)

    def _add_row(self, start=None, end=None, scroll=True):
        default_start, default_end = self._default_times()
        start = parse_time(start) or default_start
        end = parse_time(end) or default_end
        if time_to_minutes(start) >= time_to_minutes(end):
            end = minutes_to_time(time_to_minutes(start) + 45)

        frame = ttk.Frame(self.inner)
        frame.pack(fill="x", pady=3)
        label = ttk.Label(frame, text="", width=9)
        label.pack(side="left")
        start_var = tk.StringVar(value=start)
        end_var = tk.StringVar(value=end)
        start_entry = ttk.Entry(frame, textvariable=start_var, width=11)
        start_entry.pack(side="left", padx=(8, 0))
        ttk.Label(frame, text="—").pack(side="left", padx=(10, 0))
        end_entry = ttk.Entry(frame, textvariable=end_var, width=11)
        end_entry.pack(side="left", padx=(10, 0))
        ttk.Button(
            frame, text="删除", width=8,
            command=lambda: self._delete_row(row),
        ).pack(side="left", padx=(24, 0))

        row = {
            "frame": frame,
            "label": label,
            "start": start_var,
            "end": end_var,
            "start_entry": start_entry,
            "end_entry": end_entry,
        }
        self.rows.append(row)
        if scroll:
            self.after(30, self._scroll_to_bottom)
        return row

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

    def _renumber(self):
        for index, row in enumerate(self.rows, start=1):
            row["label"].configure(text="第%d节" % index)
        self.count_label.configure(text="共 %d 节" % len(self.rows))

    def _save(self):
        rows = []
        for index, row in enumerate(self.rows, start=1):
            start = parse_time(row["start"].get())
            end = parse_time(row["end"].get())
            if start is None or end is None:
                messagebox.showerror(
                    APP_TITLE, "第%d节的开始或结束时间格式不正确，请使用 HH:MM。" % index,
                    parent=self)
                (row["start_entry"] if start is None else row["end_entry"]).focus_set()
                return
            if time_to_minutes(start) >= time_to_minutes(end):
                messagebox.showerror(
                    APP_TITLE, "第%d节的结束时间必须晚于开始时间。" % index, parent=self)
                row["end_entry"].focus_set()
                return
            rows.append({"start": start, "end": end})
        self.result = normalize_schedule(rows)
        self.destroy()

    def _cancel(self):
        self.result = None
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

    def _draw(self):
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
        for course in self.courses:
            weekday = int(course.get("weekday", 0))
            if weekday not in by_day:
                continue
            period_from, period_to = resolve_course_periods(course)
            if period_from is None or period_to is None:
                continue
            period_from = max(1, min(self.period_count, period_from))
            period_to = max(period_from, min(self.period_count, period_to))
            by_day[weekday].append({
                "course": course,
                "from": period_from,
                "to": period_to,
            })

        for weekday, entries in by_day.items():
            if not entries:
                continue
            for cluster in self._interval_clusters(entries):
                lane_count = self._assign_lanes(cluster)
                lane_w = self.day_w / lane_count
                base_x = self.PERIOD_WIDTH + (weekday - 1) * self.day_w
                for entry in cluster:
                    course = entry["course"]
                    x1 = base_x + entry["lane"] * lane_w + 3
                    x2 = base_x + (entry["lane"] + 1) * lane_w - 3
                    y1 = (entry["from"] - 1) * self.ROW_HEIGHT + 3
                    y2 = entry["to"] * self.ROW_HEIGHT - 3
                    width = max(24, x2 - x1)
                    height = max(24, y2 - y1)
                    self.canvas.create_rectangle(
                        x1, y1, x2, y2,
                        fill=self._course_color(course["name"]),
                        outline="#93a7bd", width=1,
                    )
                    text_w = width - 12
                    name_id = self.canvas.create_text(
                        x1 + 6, y1 + 6, text=course["name"], anchor="nw",
                        width=text_w, font=("Microsoft YaHei UI", 9, "bold"),
                        fill="#1e293b",
                    )
                    bbox = self.canvas.bbox(name_id)
                    cursor_y = (bbox[3] + 2) if bbox else (y1 + 26)
                    if cursor_y + 30 <= y2:
                        time_id = self.canvas.create_text(
                            x1 + 6, cursor_y,
                            text="%s-%s" % (course["start"], course["end"]),
                            anchor="nw", width=text_w,
                            font=("Microsoft YaHei UI", 7), fill="#475569",
                        )
                        time_bbox = self.canvas.bbox(time_id)
                        place_y = (time_bbox[3] + 1) if time_bbox else cursor_y + 14
                        if place_y + 10 <= y2:
                            self.canvas.create_text(
                                x1 + 6, place_y,
                                text=course.get("place", "") or "",
                                anchor="nw", width=text_w,
                                font=("Microsoft YaHei UI", 7), fill="#475569",
                            )
        self.canvas.configure(scrollregion=(0, 0, self.total_w, self.total_h))

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
    def __init__(self, parent, title, course=None):
        super().__init__(parent)
        self.result = None
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.configure(padx=14, pady=12)
        self.columnconfigure(1, weight=1)

        course = course or {}
        self.name_var = tk.StringVar(value=course.get("name", ""))
        self.weekday_var = tk.StringVar(
            value=WEEKDAY_NAMES[int(course.get("weekday", 1)) - 1]
        )
        self.start_var = tk.StringVar(value=course.get("start", "08:00"))
        self.end_var = tk.StringVar(value=course.get("end", "09:40"))
        self.place_var = tk.StringVar(value=course.get("place", ""))
        self.advance_var = tk.StringVar(value=str(course.get("advance", 10)))
        self.weeks_var = tk.StringVar(value=weeks_to_text(course.get("weeks", "all")))
        self.extra_enabled_var = tk.BooleanVar(value=bool(course.get("extra_enabled", False)))
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
        ttk.Label(self, text="格式 HH:MM").grid(
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
        ttk.Label(self, text="生效周次").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(self, textvariable=self.weeks_var, width=40).grid(
            row=row, column=1, columnspan=3, sticky="we", pady=4)
        ttk.Label(self, text="all 或 1,2,3 或 1-16；未设置学期开始日期时忽略",
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
        ttk.Label(self, text="支持 {课程名} {时间} {地点}；留空则使用全局默认模板",
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
        end = parse_time(self.end_var.get())
        if start is None:
            messagebox.showerror(APP_TITLE, "开始时间格式应为 HH:MM，例如 08:00", parent=self)
            return
        if end is None:
            messagebox.showerror(APP_TITLE, "结束时间格式应为 HH:MM，例如 09:40", parent=self)
            return
        if end < start:
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
        try:
            weeks = parse_weeks(self.weeks_var.get())
        except (TypeError, ValueError):
            messagebox.showerror(APP_TITLE, "生效周次格式应为 all、1,2,3 或 1-16", parent=self)
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
            "end": end,
            "place": self.place_var.get().strip(),
            "advance": advance,
            "weeks": weeks,
            "text": self.text_box.get("1.0", "end-1c").strip(),
            "period_from": period_from,
            "period_to": period_to,
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
    def __init__(self, root):
        self.root = root
        self.courses = []
        self.schedule = []
        self.term_start = None
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

        self.root.title(APP_TITLE)
        self.root.geometry("1040x850")
        self.root.minsize(940, 820)
        self._build_style()
        self._build_ui()
        self._load_all()
        self._refresh_courses()
        self._refresh_makeup()
        self._update_week_label()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind_all("<Escape>", self._close_all_reminders, add="+")

        self._check_pet()
        self.root.update_idletasks()
        self._center_window()
        self.root.after(300, self._ensure_holidays)
        self.root.after(300, self._poll_holiday_queue)
        self.root.after(2500, self._pet_watch)

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
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9))
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

        courses_frame = ttk.LabelFrame(outer, text="课程表", padding=8)
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
        self.count_label = ttk.Label(toolbar, text="", style="Hint.TLabel")
        self.count_label.pack(side="right")

        tree_frame = ttk.Frame(courses_frame)
        tree_frame.pack(fill="both", expand=True)
        columns = (
            ("name", "课程名", 160, "w"),
            ("weekday", "星期", 62, "center"),
            ("start", "开始", 72, "center"),
            ("end", "结束", 72, "center"),
            ("place", "地点", 130, "w"),
            ("advance", "提前(分)", 74, "center"),
            ("weeks", "周次", 86, "center"),
            ("text", "自定义提醒", 220, "w"),
        )
        self.tree = ttk.Treeview(
            tree_frame, columns=[item[0] for item in columns],
            show="headings", selectmode="browse", height=8,
        )
        for key, label, width, anchor in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor=anchor,
                             stretch=key in ("name", "place", "text"))
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.edit_course())

        settings = ttk.LabelFrame(outer, text="设置", padding=10)
        settings.pack(fill="x", pady=(0, 8))
        settings.columnconfigure(1, weight=1)

        ttk.Label(settings, text="学期开始日期").grid(row=0, column=0, sticky="w")
        self.term_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.term_var, width=14).grid(
            row=0, column=1, sticky="w", padx=(10, 6), pady=3)
        ttk.Button(settings, text="保存", command=self.save_term_start).grid(
            row=0, column=2, sticky="w", pady=3)
        self.week_var = tk.StringVar()
        ttk.Label(settings, textvariable=self.week_var, style="Hint.TLabel").grid(
            row=0, column=3, sticky="w", padx=(14, 0))

        ttk.Label(settings, text="默认提醒模板").grid(row=1, column=0, sticky="w")
        self.template_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.template_var).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=(10, 6), pady=3)
        ttk.Button(settings, text="保存", command=self.save_template).grid(
            row=1, column=3, sticky="e", pady=3)
        ttk.Label(settings, text="支持 {课程名} {时间} {地点}；课程自定义提醒优先",
                  style="Hint.TLabel").grid(row=2, column=1, columnspan=3,
                                            sticky="w", padx=(10, 0))

        ttk.Label(settings, text="全局额外提醒模板").grid(row=3, column=0, sticky="w")
        self.extra_template_var = tk.StringVar()
        ttk.Entry(settings, textvariable=self.extra_template_var).grid(
            row=3, column=1, columnspan=2, sticky="we", padx=(10, 6), pady=3)
        ttk.Button(settings, text="保存", command=self.save_extra_template).grid(
            row=3, column=3, sticky="e", pady=3)
        ttk.Label(settings, text="支持 {minutes} {课程名}；额外提醒自定义文字优先",
                  style="Hint.TLabel").grid(row=4, column=1, columnspan=3,
                                            sticky="w", padx=(10, 0))

        makeup_frame = ttk.LabelFrame(
            outer, text="补班映射（补班日按指定星期的课表提醒）", padding=8)
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

        reminder_bar = ttk.Frame(outer)
        reminder_bar.pack(fill="x", pady=(2, 0))
        self.start_button = ttk.Button(reminder_bar, text="启动提醒",
                                       command=self.start_reminders)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(reminder_bar, text="停止提醒",
                                      command=self.stop_reminders, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Label(reminder_bar, text="关闭主程序即停止提醒",
                  style="Hint.TLabel").pack(side="right")

    # ------------------------------ 数据 ------------------------------

    def _load_all(self):
        self.schedule = load_schedule()
        raw_courses = load_json(COURSES_PATH, None)
        if isinstance(raw_courses, list):
            self.term_start = None
            raw_list = raw_courses
        elif isinstance(raw_courses, dict):
            self.term_start = parse_date(str(raw_courses.get("term_start", "")))
            raw_list = raw_courses.get("courses", [])
        else:
            self.term_start = None
            raw_list = []
        self.courses = sort_courses(
            [c for c in (normalize_course(item) for item in raw_list) if c])
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
            config["extra_template"] = extra_template or DEFAULT_EXTRA_TEMPLATE
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
        self.term_var.set(self.term_start.isoformat() if self.term_start else "")

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
                "term_start": self.term_start.isoformat() if self.term_start else "",
                "courses": self.courses,
            })
            return True
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 courses.json 失败：%s" % exc)
            return False

    def _refresh_courses(self):
        self.courses = sort_courses(self.courses)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, course in enumerate(self.courses):
            text = course.get("text", "").replace("\n", " ")
            if course.get("extra_enabled"):
                extra_note = " +%d次额外" % len(course.get("extras", []))
            else:
                extra_note = ""
            self.tree.insert("", "end", iid=str(index), values=(
                course["name"],
                WEEKDAY_NAMES[course["weekday"] - 1],
                course["start"],
                course["end"],
                course.get("place", ""),
                course.get("advance", 0),
                weeks_to_text(course.get("weeks", "all")),
                text + extra_note,
            ))
        self.count_label.configure(text="共 %d 门课程" % len(self.courses))
        if self.timetable_window is not None:
            try:
                if self.timetable_window.win.winfo_exists():
                    self.timetable_window.refresh(self.courses, self.schedule)
                else:
                    self.timetable_window = None
            except tk.TclError:
                self.timetable_window = None

    def _refresh_makeup(self):
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

    def open_schedule_dialog(self):
        dialog = ScheduleDialog(self.root, self.schedule)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        normalized = normalize_schedule(dialog.result)
        try:
            save_json(SCHEDULE_PATH, normalized)
        except OSError as exc:
            messagebox.showerror(APP_TITLE, "保存 schedule.json 失败：%s" % exc,
                                 parent=self.root)
            return
        self.schedule = set_periods(normalized)
        if self.timetable_window is not None:
            try:
                if self.timetable_window.win.winfo_exists():
                    self.timetable_window.refresh(self.courses, self.schedule)
            except tk.TclError:
                self.timetable_window = None
        if self.schedule:
            self._set_status("已保存作息时间表：共 %d 节" % len(self.schedule))
        else:
            self._set_status("作息时间表为空，打开课表会提示先设置")

    # ------------------------------ 课程操作 ------------------------------

    def add_course(self):
        dialog = CourseDialog(self.root, "添加课程")
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
        dialog = CourseDialog(self.root, "编辑课程", old_course)
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

    def save_term_start(self):
        text = self.term_var.get().strip()
        if text:
            parsed = parse_date(text)
            if parsed is None:
                messagebox.showerror(APP_TITLE, "学期开始日期格式应为 YYYY-MM-DD",
                                     parent=self.root)
                return
        else:
            parsed = None
        self.term_start = parsed
        if self._save_courses():
            self.term_var.set(parsed.isoformat() if parsed else "")
            self._update_week_label()
            self._set_status("学期开始日期已保存")

    def _update_week_label(self):
        if not self.term_start:
            self.week_var.set("未设置学期开始日期，生效周次将被忽略")
            return
        week = self.current_week(dt.date.today())
        if week is None or week <= 0:
            self.week_var.set("当前教学周：尚未开学（开学 %s）" % self.term_start.isoformat())
        else:
            self.week_var.set("当前教学周：第 %d 周" % week)

    def current_week(self, day):
        if not self.term_start:
            return None
        delta = (day - self.term_start).days
        if delta < 0:
            return 0
        return delta // 7 + 1

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

    def _course_active(self, course, week):
        if week is None:
            return True
        weeks = course.get("weeks", "all")
        if weeks == "all" or not isinstance(weeks, list):
            return True
        return week in weeks

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
        try:
            self._check_pet()
        except tk.TclError:
            return
        self.root.after(2500, self._pet_watch)

    def start_reminders(self):
        if not self._check_pet():
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
        if self.last_week_day != today:
            self.last_week_day = today
            self._update_week_label()

        weekday, note = self._holiday_status(today)
        if note:
            key = (today.isoformat(), note)
            if self.last_note != key:
                self.last_note = key
                self._set_status(note)
        if weekday is None:
            return

        week = self.current_week(today)
        due = []
        for course in self.courses:
            if course["weekday"] != weekday:
                continue
            if not self._course_active(course, week):
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
            "text": course.get("text", ""),
        }
        custom = (reminder.get("text") or "").strip()
        kind = reminder.get("kind", "base")
        if kind == "extra":
            template = custom or (self.config.get("extra_template") or
                                  DEFAULT_EXTRA_TEMPLATE)
            text = render_text(template, course, reminder["advance"])
        else:
            template = custom or (self.config.get("default_template") or
                                  DEFAULT_TEMPLATE)
            text = render_text(template, course)

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
        self.status_var.set(text)

    def on_close(self):
        self.reminding = False
        if self.reminder_job:
            try:
                self.root.after_cancel(self.reminder_job)
            except tk.TclError:
                pass
        self._close_all_reminders()
        if self.timetable_window is not None:
            try:
                self.timetable_window.close()
            except tk.TclError:
                pass
            self.timetable_window = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    CourseReminderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
