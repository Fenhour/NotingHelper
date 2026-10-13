from __future__ import annotations

import base64
import csv
from copy import deepcopy
import hashlib
import struct
import pipe_bridge
from contextlib import closing
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import unicodedata
import urllib.error
import urllib.request
from urllib.parse import urlparse
import zipfile
from xml.etree import ElementTree as ET

try:
    from PIL import Image, ImageGrab, ImageTk
except ImportError:  # Optional dependency used only for image recording.
    Image = None
    ImageGrab = None
    ImageTk = None

# 前端：Obsidian 风格的样式、图标与自绘控件。放在 front/ 下，见 assets/ui/README.md。
try:
    from front import controls as ui_controls
    from front import icons as ui_icons
    from front import theme as ui_theme
except ImportError:  # 缺 Pillow 或 front/ 不完整时退回原有界面，不影响主流程
    ui_controls = ui_icons = ui_theme = None

# 分隔条只依赖 tkinter，独立于 Pillow / 图标资源加载。
try:
    from front.splitter import SmoothVerticalPanedWindow
except ImportError:
    SmoothVerticalPanedWindow = None

# 人物层单独引入：素材或依赖有问题时只丢人物，不该把整套样式一起拖回旧界面
try:
    from front.actor import CharacterActor
except ImportError:
    CharacterActor = None

# 标签层不依赖 Pillow（只用 tkinter 与标准库），所以单独引入：即使主题层整个
# 退回旧界面，标签的解析与写入仍然是好的，只是不高亮、不补全。
try:
    from front import tags as ui_tags
except ImportError:
    ui_tags = None

# 划词后的悬浮入口。同样单独引入：它不可用时整条流程退回「划词直接弹窗」，
# 而不是变成「划词之后什么也不发生」——缺 Pillow 的环境必须还能用。
try:
    from front.float_entry import FloatingEntry, ModePopup
except ImportError:
    FloatingEntry = ModePopup = None


APP_NAME = "划词助手"
APP_DIR = Path(__file__).resolve().parent
ASSETS_UI_DIR = APP_DIR / "assets" / "ui"

# 经典 tk 控件（Text / Canvas / Frame / Label）不走 ttk 样式，颜色得逐个传进去，
# 传的值取自主题返回的令牌表。front/ 或 Pillow 缺失时主题层整个不加载，那份令牌表
# 也就没有，这里给一份与 front/theme.py 亮色调色板一致的兜底，保证界面不会因为
# 少一个模块就变成白底黑字的裸控件。改令牌时两边要一起改（_token 会断言覆盖度）。
FALLBACK_UI_COLORS = {
    "bg_primary": "#ffffff",
    "bg_secondary": "#f6f6f6",
    "bg_field": "#f7f7f7",
    "bg_hover": "#f5f5f5",
    "bg_active": "#ebebeb",
    "border": "#e0e0e0",
    "border_strong": "#bdbdbd",
    "accent": "#8a5cf5",
    "accent_muted": "#9f79f7",
    "text_normal": "#222222",
    "text_muted": "#5c5c5c",
    "text_faint": "#ababab",
    "text_on_accent": "#ffffff",
    "red": "#e93147",
}
DATA_DIR = APP_DIR / "data"
HIGHLIGHT_LOG_PATH = DATA_DIR / "highlight-events.log"
HIGHLIGHT_PERF_FLAG = DATA_DIR / "highlight-latency.enabled"
_HIGHLIGHT_LOG_LOCK = threading.Lock()


def record_highlight_event(event: str, **fields: object) -> None:
    """Persist highlight stages without selection text, clipboard data or auth tokens.

    A logging failure must not prevent a highlight. Keep three files of up to 2 MiB.
    """
    entry = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
             "event": event}
    entry.update({key: str(value).replace("\r", " ").replace("\n", " ")[:700]
                  if isinstance(value, (str, Path, Exception)) else value
                  for key, value in fields.items()})
    try:
        with _HIGHLIGHT_LOG_LOCK:
            path = HIGHLIGHT_LOG_PATH
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= 2 * 1024 * 1024:
                for number in (2, 1):
                    previous = path.with_name(f"{path.name}.{number - 1}") if number > 1 else path
                    target = path.with_name(f"{path.name}.{number}")
                    if previous.exists():
                        os.replace(previous, target)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


# UI 点击路径不得等慢盘日志；后台顺序写，丢弃极端积压的诊断而不影响业务。
_HIGHLIGHT_UI_LOG_QUEUE: queue.Queue = queue.Queue(maxsize=512)
_HIGHLIGHT_UI_LOG_THREAD: threading.Thread | None = None
_HIGHLIGHT_UI_LOG_START_LOCK = threading.Lock()
_HIGHLIGHT_UI_LOG_THREAD_CLASS = threading.Thread  # 独立于业务线程 mock


def record_highlight_ui_event(event: str, **fields: object) -> None:
    global _HIGHLIGHT_UI_LOG_THREAD
    try:
        _HIGHLIGHT_UI_LOG_QUEUE.put_nowait((event, fields))
    except queue.Full:
        return
    with _HIGHLIGHT_UI_LOG_START_LOCK:
        if _HIGHLIGHT_UI_LOG_THREAD is None or not _HIGHLIGHT_UI_LOG_THREAD.is_alive():
            def drain() -> None:
                while True:
                    name, details = _HIGHLIGHT_UI_LOG_QUEUE.get()
                    try:
                        record_highlight_event(name, **details)
                    except Exception:
                        pass  # 诊断不能阻塞或终止高光操作
                    finally:
                        _HIGHLIGHT_UI_LOG_QUEUE.task_done()
            _HIGHLIGHT_UI_LOG_THREAD = _HIGHLIGHT_UI_LOG_THREAD_CLASS(
                target=drain, name="highlight-ui-log", daemon=True)
            _HIGHLIGHT_UI_LOG_THREAD.start()


CONFIG_PATH = DATA_DIR / "config.json"
INDEX_PATH = DATA_DIR / "word-index.sqlite3"
DICTIONARY_INDEX_PATH = DATA_DIR / "ecdict-index.sqlite3"
DEFAULT_DICTIONARY_PATH = DATA_DIR / "ecdict.csv"
DEFAULT_OUTPUT_PATH = DATA_DIR / "我的单词本.md"
DEFAULT_QUESTION_OUTPUT_PATH = DATA_DIR / "我的问题记录.md"
DEFAULT_QUESTION_BODY_DIR = DATA_DIR / "正文库"
DEFAULT_QUESTION_MATERIALS_DIR = DATA_DIR / "文档资料库"
# 软件自己生成的东西（索引文档、正文副本、来源副本）在一个新仓库里的默认位置：
# 统一挂到 NoteDataOfY/ 下面，跟用户自己写的笔记分开。只有「还没有记录过配置的
# 仓库」会用这份模板，已经用过的仓库按它自己的 profile 走，目录布局不受影响。
NOTE_DATA_DIR_NAME = "NoteDataOfY"
QUESTION_DIR_TEMPLATE = {
    "index_dir": f"{NOTE_DATA_DIR_NAME}/索引文档库",
    "body_dir": f"{NOTE_DATA_DIR_NAME}/正文库",
    "materials_dir": f"{NOTE_DATA_DIR_NAME}/资料库",
    "index_filename": DEFAULT_QUESTION_OUTPUT_PATH.name,
}
# 高光的落点。单独一个「高光库」，与记录问题的目录完全分开：两条工作流各自
# 独立，上色不允许碰到记录问题那边的任何文件。库内的「索引 / 注释」两分由插件
# 侧生成，Python 只负责决定「导出到哪个目录」。
HIGHLIGHT_DIR_NAME = "高光库"
# 划词之后等剪贴板的节奏。以前是干等一个固定的 220 ms：不管目标程序多快复制完，
# 用户都要等满，表现出来就是「划完词图标要愣一下才出来」。改成轮询剪贴板序号
# （GetClipboardSequenceNumber 是廉价调用，不开剪贴板也不加锁），复制一到就往下走；
# 220 ms 仍是上限，留给复制慢的程序（大文档、远程桌面之类）。
CLIPBOARD_POLL_INTERVAL_MS = 30
CLIPBOARD_POLL_TIMEOUT_MS = 220
SOURCE_LIBRARY_PATH = DATA_DIR / "source-library.json"
SOURCE_LIBRARY_DIR_NAME = "NotingHelper-Sources"
SOURCE_CONTEXT_SCRIPT = APP_DIR / "tools" / "detect_active_document.ps1"
DOCX_PDF_CONVERTER_SCRIPT = APP_DIR / "tools" / "convert_docx_to_pdf.ps1"
OBSIDIAN_PLUGIN_ID = "noting-helper-bridge"
OBSIDIAN_PLUGIN_SOURCE_DIR = APP_DIR / "obsidian-plugin" / OBSIDIAN_PLUGIN_ID
OBSIDIAN_DEFAULT_PORT = 27124
EXTERNAL_SOURCE_NOTICE = "外部文档作为链接来源，无定位"
OBSIDIAN_FILE_SOURCE_NOTICE = "Obsidian 内部非 Markdown 文档作为链接来源，无定位"
OBSIDIAN_PDF_PAGE_NOTICE = "Obsidian 内部 PDF 来源，定位到第 {page} 页"
# 仓库那一行下面那句子说明。不在仓库里也要把三个目录落下来（写记录不依赖
# .obsidian），但得说清为什么定位和插件还用不了，别让人以为选错了。
OBSIDIAN_STATUS_NOTICE = "Markdown 支持块级定位；PDF 等仓库文件生成无定位链接"
OBSIDIAN_STATUS_NEEDS_VAULT_NOTICE = (
    "这个目录还不是 Obsidian 仓库（没有 .obsidian）：三个目录照常落地，"
    "定位和插件要等它在 Obsidian 里打开。仓库有时在子目录里，注意选对那一层。"
)
# Character asset directories. The constant names remain descriptive for the
# existing animation code; the folder names identify the characters directly.
POPUP_CHARACTER_DIR = APP_DIR / "assets" / "Toix_waiting"
POPUP_WALKING_CHARACTER_DIR = APP_DIR / "assets" / "Yinyao_walking"
POPUP_CHARACTER_DIRS = (POPUP_CHARACTER_DIR, POPUP_WALKING_CHARACTER_DIR)
# One draw is made for each newly opened selection popup. Keep these weights
# together so the per-character probability can be adjusted in one place.
POPUP_CHARACTER_WEIGHTS = (0.5, 0.5)
# 人物在界面上的逻辑宽度。素材原生 172，按原尺寸渲染会大过一个导航栏，
# 而且 90 帧在 200% 缩放下要占约 53 MB 内存；缩到 140 后约 35 MB。
POPUP_CHARACTER_DISPLAY_WIDTH = 140
# 人物离它所在那一边的距离（逻辑像素），别贴边
POPUP_CHARACTER_MARGIN = (16, 10)
DEFAULT_OUTPUT_FORMAT = "md"
MODE_TRANSLATE = "translate"
MODE_QUESTION = "question"
# 高光有独立主窗口页面，捕获后点击色块上色，不打开记录弹窗。
MODE_HIGHLIGHT = "highlight"
# 这个
# 字面量仍然是持久化兼容面：它会写进配置的 mode 字段（__init__ 从 config["mode"]
# 读回 capture_mode），改它等于让已经存过的配置读不回来。测试里专门钉住了。
# 悬浮入口右键菜单的顺序与文案。这里按用户的叫法写「记录单词」，主窗口页面沿用
# 现有的「翻译英语单词」——两处文案不同是有意的。
FLOAT_ENTRY_MODES = (
    (MODE_QUESTION, "记录问题"),
    (MODE_TRANSLATE, "记录单词"),
    (MODE_HIGHLIGHT, "高光模式"),
)
# 高光的可选颜色自 2.4.0 起固定为 8 种，和插件的 HIGHLIGHT_COLOR_DEFS 一一对应：
# 色键 → (色名, 色值, 导出圆点)。桌面端不再持有可编辑的色值——配置页只显示这份图例，
# 真正决定颜色的是插件那张固定表。顺序就是色板与菜单里的顺序，两边不许各写一份顺序。
HIGHLIGHT_COLOR_DEFS = {
    "red": ("红", "#e5484d", "\U0001F534"),
    "orange": ("橙", "#f76b15", "\U0001F7E0"),
    "yellow": ("黄", "#e0ac00", "\U0001F7E1"),
    "green": ("绿", "#08b94e", "\U0001F7E2"),
    "blue": ("蓝", "#086ddd", "\U0001F535"),
    "purple": ("紫", "#8e4ec6", "\U0001F7E3"),
    "magenta": ("品红", "#ff00ff", "\U0001FA77"),
    "gray": ("灰", "#8b8b8b", "⚫"),
}
# 全部 8 个色键，用作「这个键认不认识」的校验集合与导出分档的顺序。真正画在悬浮窗上的
# 是 highlight_active_colors 里那 3 个，两者别混。
HIGHLIGHT_SWATCH_KEYS = tuple(HIGHLIGHT_COLOR_DEFS)
DEFAULT_HIGHLIGHT_PALETTE = {
    key: definition[1] for key, definition in HIGHLIGHT_COLOR_DEFS.items()
}
HIGHLIGHT_COLOR_LABELS = {
    key: definition[0] for key, definition in HIGHLIGHT_COLOR_DEFS.items()
}
# 色块上那个勾用什么颜色：亮底配深字、暗底配白字，不然勾在黄块/品红块上根本看不见。
# 取值和插件 styles.css 里 `.nh-highlight-swatches .nh-hl-*` 的 color 逐条对齐，
# 免得同一个颜色在桌面端和 Obsidian 里选了「反过来」的颜色。
HIGHLIGHT_SWATCH_TEXT_COLORS = {
    "red": "#ffffff", "orange": "#1e1e1e", "yellow": "#1e1e1e", "green": "#1e1e1e",
    "blue": "#ffffff", "purple": "#ffffff", "magenta": "#1e1e1e", "gray": "#1e1e1e",
}
# 8 色是**可记录的全部颜色**，悬浮窗上只摆其中 3 种（配置页 8 选 3）。默认取
# 黄/绿/蓝——正好是 2.3.x 那三个槽位的样子，升级过来观感不变；插件侧同名的
# DEFAULT_ACTIVE_COLORS 必须跟着一起改，两边不许各写一份。
HIGHLIGHT_ACTIVE_COLOR_COUNT = 3
DEFAULT_HIGHLIGHT_ACTIVE_COLORS = ("yellow", "green", "blue")


def normalize_highlight_active_colors(value) -> list[str]:
    """悬浮窗要摆的那 3 种颜色。只认 8 色表里的键、去重、保序；凑不满 3 个就整体
    回默认（半途混搭会让人看不懂列表里到底哪几个是选中的）。和插件的
    normalizeActiveColors 同一套规则。"""
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_HIGHLIGHT_ACTIVE_COLORS)
    picked: list[str] = []
    for item in value:
        key = str(item).strip()
        if key in HIGHLIGHT_COLOR_DEFS and key not in picked:
            picked.append(key)
    if len(picked) != HIGHLIGHT_ACTIVE_COLOR_COUNT:
        return list(DEFAULT_HIGHLIGHT_ACTIVE_COLORS)
    return picked
# 悬浮入口用的图标名，取自 assets/ui/icons（见 assets/ui/README.md）
FLOAT_ENTRY_ICONS = {
    MODE_QUESTION: "pencil", MODE_HIGHLIGHT: "palette", MODE_TRANSLATE: "feather",
}
# 三种入口图标统一按主题蓝着色；模式切换卡里的文案由 FLOAT_ENTRY_MODES 提供。
FLOAT_ENTRY_ICON_COLOR = "#3465a4"
QUESTION_RECORD_MODE_INDEPENDENT = "independent"
QUESTION_RECORD_MODE_INLINE = "inline"
QUESTION_RECORD_MODE_LABELS = {
    "创建独立正文并加入索引（推荐）": QUESTION_RECORD_MODE_INDEPENDENT,
    "直接写入当前索引": QUESTION_RECORD_MODE_INLINE,
}
QUESTION_LINK_MODE_LABELS = {
    "独立正文 + 索引链接": True,
    "直接写入当前索引": False,
}


def question_link_mode_label(mode: str, fallback: str = QUESTION_RECORD_MODE_INLINE) -> str:
    """Convert the persisted mode string to the label shown in the UI."""
    if mode not in {QUESTION_RECORD_MODE_INDEPENDENT, QUESTION_RECORD_MODE_INLINE}:
        mode = fallback
    include_link = mode == QUESTION_RECORD_MODE_INDEPENDENT
    return next(
        label for label, value in QUESTION_LINK_MODE_LABELS.items() if value == include_link
    )
PAGE_LABELS = {
    "翻译英语单词": MODE_TRANSLATE,
    "记录问题": MODE_QUESTION,
    "高光": MODE_HIGHLIGHT,
    "配置 API": "api",
}
# 每个页面对应的导航图标，名字取自 assets/ui/icons（Lucide，见 assets/ui/README.md）
PAGE_ICONS = {
    "翻译英语单词": "languages",
    "记录问题": "square-pen",
    "高光": "highlighter",
    "配置 API": "settings",
}
QUESTION_TITLE_OPTIONS = (
    ("无标题", 0),
    ("一级标题", 1),
    ("二级标题", 2),
    ("三级标题", 3),
    ("四级标题", 4),
    ("五级标题", 5),
    ("六级标题", 6),
)
DEFAULT_QUESTION_TITLE_LABEL = "二级标题"
DEFAULT_QUESTION_TITLE_LEVEL = 2
DEFAULT_QUESTION_SOURCE_LINK_MODE = QUESTION_RECORD_MODE_INLINE
DEFAULT_QUESTION_SUPPLEMENT_LINK_MODE = QUESTION_RECORD_MODE_INLINE
DEFAULT_QUESTION_OBSIDIAN_SOURCE_LINK = True

CODE_LANGUAGE_OPTIONS = (
    ("自动推荐", None),
    ("纯文本", ""),
    ("Python", "python"),
    ("Java", "java"),
    ("JavaScript", "javascript"),
    ("TypeScript", "typescript"),
    ("C", "c"),
    ("C++", "cpp"),
    ("C#", "csharp"),
    ("SQL", "sql"),
    ("Shell", "bash"),
    ("JSON", "json"),
    ("HTML", "html"),
    ("CSS", "css"),
)
OUTPUT_FORMAT_LABELS = {
    "Markdown（三行词条）": "md",
    "JSON（结构化）": "json",
    "纯文本（三行词条）": "txt",
    "Word 文档（docx）": "docx",
}
DEFAULT_BASE_URL = "https://api.openai.com/v1"
GROK_BASE_URL = "https://api.x.ai/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_CHAT_COMPLETIONS = "chat_completions"
JSON_MODE_SCHEMA = "json_schema"
JSON_MODE_OBJECT = "json_object"
PROTOCOL_LABELS = {
    "OpenAI Responses API": PROTOCOL_RESPONSES,
    "Chat Completions（Grok / 中转）": PROTOCOL_CHAT_COMPLETIONS,
}
JSON_MODE_LABELS = {
    "JSON Schema（Grok / 兼容中转）": JSON_MODE_SCHEMA,
    "JSON Object（DeepSeek）": JSON_MODE_OBJECT,
}
API_PRESETS = {
    "openai": {
        "protocol": PROTOCOL_RESPONSES,
        "base_url": DEFAULT_BASE_URL,
        "model": "gpt-5-mini",
        "json_mode": JSON_MODE_SCHEMA,
        "timeout": 60,
    },
    "grok": {
        "protocol": PROTOCOL_CHAT_COMPLETIONS,
        "base_url": GROK_BASE_URL,
        "model": "grok-4",
        "json_mode": JSON_MODE_SCHEMA,
        "timeout": 120,
    },
    "deepseek": {
        "protocol": PROTOCOL_CHAT_COMPLETIONS,
        "base_url": DEEPSEEK_BASE_URL,
        "model": "deepseek-chat",
        "json_mode": JSON_MODE_OBJECT,
        "timeout": 120,
    },
}
IGNORE_DURATION_OPTIONS = (
    ("1 小时", 60 * 60),
    ("6 小时", 6 * 60 * 60),
    ("1 天", 24 * 60 * 60),
    ("3 天", 3 * 24 * 60 * 60),
    ("1 星期", 7 * 24 * 60 * 60),
    ("永久", None),
)

IRREGULAR_LEMMAS = {
    "am": "be", "are": "be", "is": "be", "was": "be", "were": "be", "been": "be",
    "being": "be", "has": "have", "had": "have", "having": "have", "does": "do",
    "did": "do", "done": "do", "doing": "do", "went": "go", "gone": "go",
    "saw": "see", "seen": "see", "made": "make", "took": "take", "taken": "take",
    "came": "come", "became": "become", "become": "become", "got": "get",
    "gotten": "get", "gave": "give", "given": "give", "found": "find", "thought": "think",
    "told": "tell", "said": "say", "knew": "know", "known": "know", "left": "leave",
    "felt": "feel", "brought": "bring", "began": "begin", "begun": "begin", "kept": "keep",
    "held": "hold", "wrote": "write", "written": "write", "stood": "stand", "heard": "hear",
    "meant": "mean", "met": "meet", "ran": "run", "paid": "pay", "sat": "sit",
    "spoke": "speak", "spoken": "speak", "lay": "lie", "lain": "lie", "led": "lead",
    "read": "read", "grew": "grow", "grown": "grow", "lost": "lose", "fell": "fall",
    "fallen": "fall", "sent": "send", "built": "build", "understood": "understand",
    "drew": "draw", "drawn": "draw", "broke": "break", "broken": "break", "spent": "spend",
    "rose": "rise", "risen": "rise", "drove": "drive", "driven": "drive", "bought": "buy",
    "wore": "wear", "worn": "wear", "chose": "choose", "chosen": "choose", "ate": "eat",
    "eaten": "eat", "drank": "drink", "drunk": "drink", "slept": "sleep", "swam": "swim",
    "swum": "swim", "sang": "sing", "sung": "sing", "flew": "fly", "flown": "fly",
    "taught": "teach", "caught": "catch", "fought": "fight", "threw": "throw",
    "thrown": "throw", "forgot": "forget", "forgotten": "forget", "forgave": "forgive",
    "forgiven": "forgive", "hid": "hide", "hidden": "hide", "shook": "shake",
    "shaken": "shake", "stole": "steal", "stolen": "steal", "woke": "wake", "woken": "wake",
    "won": "win", "men": "man", "women": "woman", "children": "child", "people": "person",
    "teeth": "tooth", "feet": "foot", "mice": "mouse", "geese": "goose", "oxen": "ox",
    "indices": "index", "matrices": "matrix", "analyses": "analysis", "criteria": "criterion",
    "phenomena": "phenomenon", "better": "good", "best": "good", "worse": "bad",
    "worst": "bad", "buses": "bus", "leaves": "leaf", "lives": "life", "wives": "wife",
    "knives": "knife", "wolves": "wolf", "shelves": "shelf", "calves": "calf",
}
UNINFLECTED_WORDS = {
    "as", "bias", "business", "canvas", "class", "gas", "glass", "his", "news", "series",
    "species", "this", "thus", "yes", "analysis", "basis", "crisis", "physics", "status",
    "being", "building", "ceiling", "during", "evening", "feeling", "king", "morning",
    "nothing", "offering", "ring", "spring", "string", "thing", "warning", "wing",
    "other", "over", "under", "after", "answer", "center", "computer", "cover", "ever",
    "father", "finger", "flower", "matter", "member", "number", "paper", "power", "river",
    "summer", "teacher", "water", "whether", "winter", "wonder", "worker",
}
COMPARATIVE_LEMMAS = {
    "higher": "high", "highest": "high", "lower": "low", "lowest": "low",
    "faster": "fast", "fastest": "fast", "slower": "slow", "slowest": "slow",
    "larger": "large", "largest": "large", "smaller": "small", "smallest": "small",
    "longer": "long", "longest": "long", "shorter": "short", "shortest": "short",
    "newer": "new", "newest": "new", "older": "old", "oldest": "old",
    "easier": "easy", "easiest": "easy", "earlier": "early", "earliest": "early",
    "later": "late", "latest": "late", "greater": "great", "greatest": "great",
    "stronger": "strong", "strongest": "strong", "weaker": "weak", "weakest": "weak",
    "closer": "close", "closest": "close", "safer": "safe", "safest": "safe",
    "harder": "hard", "hardest": "hard", "softer": "soft", "softest": "soft",
    "deeper": "deep", "deepest": "deep", "wider": "wide", "widest": "wide",
    "bigger": "big", "biggest": "big", "hotter": "hot", "hottest": "hot",
}
CONTRACTION_LEMMAS = {
    "can't": "can", "cannot": "can", "won't": "will", "shan't": "shall",
    "i'm": "I", "you're": "you", "we're": "we", "they're": "they",
    "it's": "it", "that's": "that", "what's": "what", "who's": "who",
    "let's": "let", "i've": "I", "we've": "we", "they've": "they",
}


class AppError(RuntimeError):
    pass


class ConfigError(AppError):
    pass


class LookupError(AppError):
    pass


class DictionaryError(AppError):
    pass


class DocumentError(AppError):
    pass


class ObsidianBridgeError(AppError):
    pass


class ObsidianBridgeTransportError(ObsidianBridgeError):
    """连接中断：POST 可能已经被服务端处理。"""


class ObsidianBridgeTimeout(ObsidianBridgeTransportError):
    """请求超时：高光 POST 的服务端写入结果尚未确认。"""


class SingleInstanceGuard:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str = "Local\\SentenceWordPicker.Singleton"):
        if os.name != "nt":
            self.handle = None
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self._kernel32 = kernel32
        self.handle = kernel32.CreateMutexW(None, False, name)
        if not self.handle:
            raise AppError("无法创建程序单实例锁。")
        if ctypes.get_last_error() == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise AppError("划词助手已经在运行，请先打开现有窗口。")

    def close(self) -> None:
        if self.handle:
            self._kernel32.CloseHandle(self.handle)
            self.handle = None


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob_from_bytes(value: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buffer = ctypes.create_string_buffer(value)
    blob = DATA_BLOB(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def protect_secret(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        raise ConfigError("API Key 的加密存储仅支持 Windows。")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    input_blob, input_buffer = _blob_from_bytes(value.encode("utf-8"))
    output_blob = DATA_BLOB()
    description = "Sentence Word Picker API Key"
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        error_code = ctypes.get_last_error()
        raise ConfigError(f"无法使用 Windows DPAPI 加密 API Key（错误 {error_code}）。")
    try:
        protected = ctypes.string_at(output_blob.pbData, output_blob.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect_secret(value: str) -> str:
    if not value:
        return ""
    if os.name != "nt":
        raise ConfigError("API Key 的解密存储仅支持 Windows。")

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        encrypted = base64.b64decode(value)
    except ValueError as exc:
        raise ConfigError("本机 API Key 配置已损坏。") from exc

    input_blob, input_buffer = _blob_from_bytes(encrypted)
    output_blob = DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )
    del input_buffer
    if not ok:
        error_code = ctypes.get_last_error()
        raise ConfigError(f"无法解密 API Key（错误 {error_code}）；请在设置中重新填写。")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output_blob.pbData)


class AppConfig:
    DEFAULTS = {
        "model": "gpt-5-mini",
        "base_url": DEFAULT_BASE_URL,
        "protocol": PROTOCOL_RESPONSES,
        "json_mode": JSON_MODE_SCHEMA,
        "timeout": 60,
        "output_path": str(DEFAULT_OUTPUT_PATH),
        "output_dir": str(DEFAULT_OUTPUT_PATH.parent),
        "output_filename": DEFAULT_OUTPUT_PATH.name,
        "output_format": DEFAULT_OUTPUT_FORMAT,
        "mode": MODE_TRANSLATE,
        "question_output_path": str(DEFAULT_QUESTION_OUTPUT_PATH),
        "question_output_dir": str(DEFAULT_QUESTION_OUTPUT_PATH.parent),
        "question_output_filename": DEFAULT_QUESTION_OUTPUT_PATH.name,
        "question_body_dir": "",
        "question_materials_dir": "",
        # 高光颜色自 2.4.0 起固定为 8 种（HIGHLIGHT_COLOR_DEFS），旧配置里的
        # highlight_palette 不再读也不再写：色值由插件那张表说了算。
        "highlight_native_entry_enabled": True,
        # 按需导出的根目录（绝对路径）。原来它只写不读——不在 DEFAULTS 里，
        # load() 的回填循环就跳过它，重启后配置里的值等于白存。
        "highlight_export_dir": "",
        # 侧栏「导出这一条」的目标：索引文档与注释目录都是 Vault 内路径（存绝对路径，
        # 推给插件前转成 Vault 相对路径）。索引文档按「目录 + 文件名」两项存，和
        # 「记录问题」同一套结构（也就能用同一个「挑现成 md」的下拉框）；
        # highlight_single_index_path 是这两项拼出来的派生值，不再单独配置。
        "highlight_single_index_dir": "",
        "highlight_single_index_filename": "",
        "highlight_single_index_path": "",
        "highlight_single_note_dir": "",
        # 悬浮窗上摆哪 3 种颜色（从 8 色里选）。键必须是 HIGHLIGHT_COLOR_DEFS 里的。
        "highlight_active_colors": list(DEFAULT_HIGHLIGHT_ACTIVE_COLORS),
        "question_record_mode": QUESTION_RECORD_MODE_INDEPENDENT,
        "question_source_link_mode": DEFAULT_QUESTION_SOURCE_LINK_MODE,
        "question_supplement_link_mode": DEFAULT_QUESTION_SUPPLEMENT_LINK_MODE,
        "question_title_level": DEFAULT_QUESTION_TITLE_LEVEL,
        "question_obsidian_source_link": DEFAULT_QUESTION_OBSIDIAN_SOURCE_LINK,
        "obsidian_vault_path": "",
        "highlight_vault_path": "",
        "highlight_vault_profiles": {},
        # 新仓库的默认目录模板（仓库相对路径），见 QUESTION_DIR_TEMPLATE
        "question_dir_template": dict(QUESTION_DIR_TEMPLATE),
        # 用过的标签留一份在这里：换库或库还没扫到时，补全列表也不是空的
        "question_tags": [],
        "question_vault_profiles": {},
        "dictionary_path": str(DEFAULT_DICTIONARY_PATH),
        "encrypted_api_key": "",
        "drag_threshold": 8,
        "minimum_text_length": 2,
    }

    def __init__(self, path: Path | None = None):
        # 路径在这里现取，不做默认参数：默认参数在函数定义时就固定了，脚本在导入
        # 之后改 CONFIG_PATH 会完全不起作用 —— 照样写真实配置。给脚本留一个
        # WORD_PICKER_CONFIG_PATH 的口子，跑验证就不会碰到真实配置。
        configured = str(os.environ.get("WORD_PICKER_CONFIG_PATH", "")).strip()
        self.path = Path(path or configured or CONFIG_PATH)
        self.values = dict(self.DEFAULTS)
        self.load()
        self.apply_environment_overrides()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            legacy_output_path = loaded.get("output_path")
            for key in self.DEFAULTS:
                if key in loaded:
                    self.values[key] = loaded[key]
        # Migrate once, not on every subsequent question-vault change.
        if isinstance(loaded, dict) and "highlight_vault_path" not in loaded:
            self.values["highlight_vault_path"] = str(loaded.get("obsidian_vault_path", ""))
        # Older releases stored one complete file path. Derive the new directory
        # and filename fields without changing that user's existing document.
        if isinstance(loaded, dict) and "output_dir" not in loaded and legacy_output_path:
            legacy_path = Path(str(legacy_output_path)).expanduser()
            self.values["output_dir"] = str(legacy_path.parent)
        if isinstance(loaded, dict) and "output_filename" not in loaded and legacy_output_path:
            legacy_path = Path(str(legacy_output_path)).expanduser()
            self.values["output_filename"] = legacy_path.name
        if isinstance(loaded, dict) and "output_format" not in loaded:
            suffix = Path(str(self.values.get("output_filename", ""))).suffix.lower().lstrip(".")
            if suffix in {"md", "json", "txt", "docx"}:
                self.values["output_format"] = suffix
        if isinstance(loaded, dict) and "question_output_dir" not in loaded and loaded.get("question_output_path"):
            question_path = Path(str(loaded["question_output_path"])).expanduser()
            self.values["question_output_dir"] = str(question_path.parent)
        if isinstance(loaded, dict) and "question_output_filename" not in loaded and loaded.get("question_output_path"):
            question_path = Path(str(loaded["question_output_path"])).expanduser()
            self.values["question_output_filename"] = question_path.name
        # 2.5.0 把单条导出的「索引文档」从一整条路径拆成「目录 + 文件名」（和记录问题
        # 同一套结构）。旧配置只有完整路径，这里拆开回填，用户不用重新选一次。
        if isinstance(loaded, dict) and "highlight_single_index_dir" not in loaded and loaded.get("highlight_single_index_path"):
            single_path = Path(str(loaded["highlight_single_index_path"])).expanduser()
            self.values["highlight_single_index_dir"] = str(single_path.parent)
        if isinstance(loaded, dict) and "highlight_single_index_filename" not in loaded and loaded.get("highlight_single_index_path"):
            single_path = Path(str(loaded["highlight_single_index_path"])).expanduser()
            self.values["highlight_single_index_filename"] = single_path.name
        # 颜色集合一定要过校验：配置里被手改成 2 个或 4 个时，不能就这么推给插件。
        self.values["highlight_active_colors"] = normalize_highlight_active_colors(
            self.values.get("highlight_active_colors"))

    def apply_environment_overrides(self) -> None:
        mappings = {
            "WORD_PICKER_MODEL": "model",
            "WORD_PICKER_BASE_URL": "base_url",
            "WORD_PICKER_PROTOCOL": "protocol",
            "WORD_PICKER_JSON_MODE": "json_mode",
            "WORD_PICKER_TIMEOUT": "timeout",
            "WORD_PICKER_OUTPUT_PATH": "output_path",
            "WORD_PICKER_OUTPUT_DIR": "output_dir",
            "WORD_PICKER_OUTPUT_FILENAME": "output_filename",
            "WORD_PICKER_OUTPUT_FORMAT": "output_format",
            "WORD_PICKER_MODE": "mode",
            "WORD_PICKER_QUESTION_OUTPUT_PATH": "question_output_path",
            "WORD_PICKER_QUESTION_OUTPUT_DIR": "question_output_dir",
            "WORD_PICKER_QUESTION_OUTPUT_FILENAME": "question_output_filename",
            "WORD_PICKER_QUESTION_BODY_DIR": "question_body_dir",
            "WORD_PICKER_QUESTION_MATERIALS_DIR": "question_materials_dir",
            "WORD_PICKER_OBSIDIAN_VAULT_PATH": "obsidian_vault_path",
            "WORD_PICKER_HIGHLIGHT_VAULT_PATH": "highlight_vault_path",
            "WORD_PICKER_DICTIONARY_PATH": "dictionary_path",
        }
        for environment_name, config_name in mappings.items():
            value = os.environ.get(environment_name, "").strip()
            if value:
                self.values[config_name] = value
        environment_path = os.environ.get("WORD_PICKER_OUTPUT_PATH", "").strip()
        if environment_path and not os.environ.get("WORD_PICKER_OUTPUT_DIR", "").strip():
            legacy_path = Path(environment_path).expanduser()
            self.values["output_dir"] = str(legacy_path.parent)
            if not os.environ.get("WORD_PICKER_OUTPUT_FILENAME", "").strip():
                self.values["output_filename"] = legacy_path.name
            suffix = legacy_path.suffix.lower().lstrip(".")
            if suffix in OUTPUT_FORMAT_LABELS.values() and not os.environ.get("WORD_PICKER_OUTPUT_FORMAT", "").strip():
                self.values["output_format"] = suffix
        question_environment_path = os.environ.get("WORD_PICKER_QUESTION_OUTPUT_PATH", "").strip()
        if question_environment_path:
            question_path = Path(question_environment_path).expanduser()
            self.values["question_output_path"] = str(question_path)
            if not os.environ.get("WORD_PICKER_QUESTION_OUTPUT_DIR", "").strip():
                self.values["question_output_dir"] = str(question_path.parent)
            if not os.environ.get("WORD_PICKER_QUESTION_OUTPUT_FILENAME", "").strip():
                self.values["question_output_filename"] = question_path.name

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 每次运行只留一份「运行前」的快照：万一哪次把配置整份覆盖了，
        # 退得回上一版，不必指望时间点上碰巧留下的手工备份。
        if not getattr(self, "_config_backup_written", False) and self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
                self._config_backup_written = True
            except OSError:
                pass
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.values, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def get_api_key(self) -> str:
        for variable in ("WORD_PICKER_API_KEY", "XAI_API_KEY", "OPENAI_API_KEY"):
            environment_key = os.environ.get(variable, "").strip()
            if environment_key:
                return environment_key
        encrypted = str(self.values.get("encrypted_api_key", ""))
        return unprotect_secret(encrypted) if encrypted else ""

    def set_api_key(self, value: str) -> None:
        value = value.strip()
        self.values["encrypted_api_key"] = protect_secret(value) if value else ""


def normalize_selected_text(value: str) -> str:
    value = value.replace("\x00", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize_note_text(value: str) -> str:
    """Normalize line endings without altering selected code indentation."""
    value = str(value).replace("\x00", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = value.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def normalize_web_source_url(value: str) -> str:
    """把输入整理成可用的网页链接；不是 http/https 就返回空串。

    对话框的即时校验和真正写入走的是同一个判据，免得出现「界面上说链接不合法、
    写进去的却还是原样」这种两套标准。
    """
    url = str(value or "").strip()
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def recommend_code_language(value: str) -> str:
    """Return a conservative Markdown language tag, or an empty tag."""
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (dict, list)):
            return "json"
    except (TypeError, ValueError):
        pass

    checks = (
        ("python", r"(?m)^\s*(?:def|class)\s+\w+.*:\s*$|^\s*(?:from\s+[\w.]+\s+import|import\s+[\w.]+)|if\s+__name__\s*=="),
        ("java", r"(?m)^\s*(?:public\s+)?class\s+\w+|System\.out\.|^\s*(?:package|import\s+java\.)\s+"),
        ("typescript", r"(?m)^\s*(?:interface|type|enum)\s+\w+|:\s*(?:string|number|boolean)(?:\[\])?\b"),
        ("javascript", r"(?m)^\s*(?:const|let|var)\s+\w+\s*=.*(?:=>|require\()|\bconsole\.log\(|^\s*(?:async\s+)?function\s+\w+"),
        ("cpp", r"(?m)^\s*#include\s*<(?:iostream|vector|string|map|memory|algorithm)>|\bstd::|\bcout\s*<<|\bcin\s*>>"),
        ("csharp", r"(?m)^\s*using\s+System(?:\.|;)|\bConsole\.WriteLine\(|\bnamespace\s+\w+"),
        ("sql", r"(?is)\bSELECT\b.+\bFROM\b|\bINSERT\s+INTO\b|\bUPDATE\b.+\bSET\b|\bCREATE\s+TABLE\b"),
        ("bash", r"(?m)^\s*#!.*\b(?:ba|z|k)?sh\b|^\s*(?:export\s+\w+=|(?:if|for|while)\s+.+;\s*then\b)"),
        ("html", r"(?is)<!DOCTYPE\s+html|<html\b|<(?:div|section|main|article|script|style)\b[^>]*>"),
        ("css", r"(?m)^\s*(?:[.#][\w-]+|[a-z][\w-]*)[^\n{]*\{\s*$[\s\S]*^\s*[\w-]+\s*:\s*[^;]+;"),
        ("c", r'(?m)^\s*#include\s*[<"](?:stdio|stdlib|string|stdint)\.h[>"]|\bprintf\s*\(|\bscanf\s*\('),
    )
    for language, pattern in checks:
        if re.search(pattern, text):
            return language
    return ""


def markdown_code_block(value: str, language: str = "") -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise ValueError("代码内容不能为空。")
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", normalized)),
        default=0,
    )
    fence = "`" * max(3, longest_run + 1)
    safe_language = language if re.fullmatch(r"[A-Za-z0-9_+#.-]*", language) else ""
    closing_prefix = "" if normalized.endswith("\n") else "\n"
    return f"{fence}{safe_language}\n{normalized}{closing_prefix}{fence}"


def resolve_output_path(values: dict) -> Path:
    """Resolve the configured document path while accepting legacy configs."""
    directory = str(values.get("output_dir", "")).strip()
    filename = str(values.get("output_filename", "")).strip()
    if directory and filename:
        return Path(directory).expanduser() / filename
    return Path(str(values.get("output_path", DEFAULT_OUTPUT_PATH))).expanduser()


def resolve_dictionary_path(values: dict) -> Path:
    """Resolve the configured dictionary, recovering from moved app directories."""
    configured_text = str(values.get("dictionary_path", "")).strip()
    configured = Path(configured_text).expanduser() if configured_text else None
    if configured and configured.is_file():
        return configured
    # A config file can outlive a moved checkout. Prefer the bundled dictionary
    # when the configured path no longer exists; retain custom paths otherwise.
    if DEFAULT_DICTIONARY_PATH.is_file():
        return DEFAULT_DICTIONARY_PATH
    return configured or DEFAULT_DICTIONARY_PATH

def resolve_question_output_path(values: dict) -> Path:
    """Resolve the Markdown document used by the question-recording mode."""
    directory = str(values.get("question_output_dir", "")).strip()
    filename = str(values.get("question_output_filename", "")).strip()
    if directory and filename:
        return Path(directory).expanduser() / filename
    return Path(
        str(values.get("question_output_path", DEFAULT_QUESTION_OUTPUT_PATH))
    ).expanduser()


def resolve_question_body_dir(values: dict) -> Path:
    configured = str(values.get("question_body_dir", "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        # Treat the legacy app-data default as unset for a new Vault.
        if candidate.resolve() != DEFAULT_QUESTION_BODY_DIR.resolve():
            return candidate
    return resolve_question_output_path(values).parent / "正文库"

HIGHLIGHT_PROFILE_KEYS = (
    "highlight_export_dir", "highlight_single_index_dir", "highlight_single_index_filename",
    "highlight_single_index_path", "highlight_single_note_dir", "highlight_active_colors",
    "highlight_native_entry_enabled",
)


def configured_highlight_vault(values: dict) -> str:
    # Only callers passing legacy dictionaries fall back; migrated configs always have the key.
    return str(values.get("highlight_vault_path", values.get("obsidian_vault_path", ""))).strip()


def highlight_root(values: dict, vault_path: Path | str | None = None) -> Path:
    """The highlighter owns its root; never infer it from question output paths."""
    configured = str(vault_path or configured_highlight_vault(values)).strip()
    return Path(configured).expanduser() if configured else APP_DIR


def resolve_highlight_export_dir(
    values: dict, vault_path: Path | str | None = None,
) -> Path:
    """显式导出时才生成两份 Markdown，默认同目录方便相互跳转。"""
    configured = str(values.get("highlight_export_dir", "")).strip()
    if configured:
        return Path(configured).expanduser()
    return (highlight_root(values, vault_path) / NOTE_DATA_DIR_NAME
            / HIGHLIGHT_DIR_NAME / "导出")


def highlight_vault_relative(path_text: str, vault_text: str, suffix: str) -> str:
    """禁止插件存储/导出越过 Vault，亦拒绝覆盖用户源文件。"""
    vault = Path(vault_text).expanduser().resolve()
    target = Path(path_text).expanduser().resolve()
    try:
        relative = target.relative_to(vault)
    except ValueError as exc:
        raise ObsidianBridgeError("高光数据与导出目录必须位于当前 Obsidian 仓库内。") from exc
    if suffix and target.suffix.lower() != suffix:
        raise ObsidianBridgeError(f"高光数据文件必须以 {suffix} 结尾。")
    if not relative.parts or any(part in {".", ".."} for part in relative.parts):
        raise ObsidianBridgeError("高光路径无效。")
    return relative.as_posix()


def resolve_question_materials_dir(values: dict) -> Path:
    configured = str(values.get("question_materials_dir", "")).strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.resolve() != DEFAULT_QUESTION_MATERIALS_DIR.resolve():
            return candidate
    return resolve_question_output_path(values).parent / "文档资料库"


def normalized_question_dir_template(raw) -> dict:
    """模板里缺失或留空的项，回落到内置的那一份。"""
    template = {}
    for key, fallback in QUESTION_DIR_TEMPLATE.items():
        text = ""
        if isinstance(raw, dict):
            text = str(raw.get(key, "") or "").strip().replace("\\", "/")
        template[key] = text or fallback
    return template


def question_dir_template(values: dict) -> dict:
    return normalized_question_dir_template(values.get("question_dir_template"))


def apply_question_dir_template(template: dict, vault: Path, create: bool = False) -> dict:
    """把模板里的仓库相对路径换算成这个仓库下的绝对路径。

    返回四个可直接写进配置的字段。相对路径按仓库目录拼；写绝对路径就照用，
    这样模板也能指向仓库外。create=True 时顺手把三个目录建出来。
    """
    root = Path(vault).expanduser()

    def resolve(relative: str) -> Path:
        candidate = Path(relative).expanduser()
        return candidate if candidate.is_absolute() else root / candidate

    index_dir = resolve(template["index_dir"])
    body_dir = resolve(template["body_dir"])
    materials_dir = resolve(template["materials_dir"])
    if create:
        for directory in (index_dir, body_dir, materials_dir):
            directory.mkdir(parents=True, exist_ok=True)
    filename = Path(template["index_filename"]).name.strip()
    if not filename or filename in {".", ".."}:
        filename = DEFAULT_QUESTION_OUTPUT_PATH.name
    elif Path(filename).suffix.lower() != ".md":
        filename = Path(filename).stem + ".md"
    return {
        "question_output_dir": str(index_dir),
        "question_output_filename": filename,
        "question_body_dir": str(body_dir),
        "question_materials_dir": str(materials_dir),
    }

def question_record_slug(overview: str, selected_text: str = "") -> str:
    """Create a safe, readable directory/file stem for one body record."""
    source = normalize_note_text(overview) or normalize_note_text(selected_text)
    source = source.split("\n", 1)[0].strip()
    source = re.sub(r"[\[\]`*_#<>|{}\\/:\"?]", " ", source)
    source = re.sub(r"\s+", " ", source).strip(" .")
    source = source[:48].rstrip(" .") or "未命名记录"
    if source.upper() in {"CON", "PRN", "AUX", "NUL"}:
        source = f"{source}-记录"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(
        f"{overview}\n{selected_text}\n{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:4]
    return f"{source}-{stamp}-{digest}"


# 库内相对路径的长度预算。Windows 的 MAX_PATH 是 260，库根路径已经占掉一截，
# 收件人解压时前面还要再加一层目录，所以不能贴着 260 算。这条只管新建的记录名，
# 不会去动已有文件的名字。
PATH_BUDGET_RELATIVE = 140
# 标签池缓存多久。扫库要读几百个文件，弹窗每开一次都扫一遍太浪费，但这个列表
# 也不能太旧 —— 刚在 Obsidian 里建的标签应该很快能补全出来。
TAG_POOL_TTL = 300
_RECORD_STEM_TAIL = re.compile(r"^(?P<desc>.*)-(?P<tail>\d{8}-\d{6}-[0-9a-f]{4})$")


def fit_record_stem(stem: str, body_root: Path, index_path: Path) -> str:
    """把记录名压到「库内相对路径不超过预算」为止。

    只截描述部分：时间戳和哈希是唯一性的来源，动了就会重名。目录名和文件名
    是同一个 stem，所以每砍一个字符，相对路径短两个字符。
    """
    match = _RECORD_STEM_TAIL.match(stem)
    if not match:
        return stem
    desc, tail = match.group("desc"), match.group("tail")
    try:
        relative_dir = os.path.relpath(body_root, index_path.parent).replace("\\", "/")
    except ValueError:
        return stem

    def length(candidate: str) -> int:
        return len(f"{relative_dir}/{candidate}/{candidate}.md")

    overflow = length(stem) - PATH_BUDGET_RELATIVE
    if overflow <= 0:
        return stem
    keep = max(1, len(desc) - (overflow + 1) // 2)
    return f"{desc[:keep].rstrip(' .')}-{tail}"


def _normalized_path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def question_vault_profile(values: dict, vault_path: Path | str) -> dict[str, str]:
    """Return the last saved question-document settings for one Vault."""
    profiles = values.get("question_vault_profiles", {})
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(_normalized_path_key(vault_path), {})
    if not isinstance(profile, dict):
        return {}
    allowed = {
        "question_output_dir",
        "question_output_filename",
        "question_body_dir",
        "question_materials_dir",
        "question_record_mode",
    }
    return {
        key: str(value)
        for key, value in profile.items()
        if key in allowed and str(value).strip()
    }


def record_question_vault_profile(
    values: dict,
    vault_path: Path | str,
    profile: dict[str, str],
) -> None:
    """Store the last explicitly saved question-document settings per Vault."""
    profiles = values.get("question_vault_profiles", {})
    profiles = dict(profiles) if isinstance(profiles, dict) else {}
    profiles[_normalized_path_key(vault_path)] = {
        "question_output_dir": str(profile.get("question_output_dir", "")),
        "question_output_filename": str(profile.get("question_output_filename", "")),
        "question_body_dir": str(profile.get("question_body_dir", "")),
        "question_materials_dir": str(profile.get("question_materials_dir", "")),
        "question_record_mode": str(profile.get("question_record_mode", "")),
    }
    values["question_vault_profiles"] = profiles


def extract_words_offline(sentence: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for match in re.finditer(r"[A-Za-z]+(?:['’-][A-Za-z]+)*", sentence):
        word = match.group(0).replace("’", "'")
        key = word.casefold()
        if key not in seen:
            seen.add(key)
            result.append(word)
    return result


def _remove_doubled_consonant(value: str) -> str:
    if len(value) >= 3 and value[-1] == value[-2] and value[-1] in "bdgmnprt":
        return value[:-1]
    return value


def _restore_silent_e(value: str) -> str:
    endings = (
        "at", "iz", "bl", "iv", "mak", "tak", "writ", "com", "us", "clos", "mov", "lov"
    )
    return value + "e" if value.endswith(endings) else value


def lemmatize_word_offline(value: str) -> str:
    word = value.strip().replace("’", "'").casefold()
    if not word:
        return ""
    if word == "i":
        return "I"
    if word in CONTRACTION_LEMMAS:
        return CONTRACTION_LEMMAS[word]
    if word.endswith("'s") and len(word) > 2:
        return word[:-2]
    if "-" in word:
        parts = word.split("-")
        parts[-1] = lemmatize_word_offline(parts[-1])
        return "-".join(parts)
    if word in IRREGULAR_LEMMAS:
        return IRREGULAR_LEMMAS[word]
    if word in COMPARATIVE_LEMMAS:
        return COMPARATIVE_LEMMAS[word]
    if word in UNINFLECTED_WORDS or len(word) <= 3:
        return word
    if word.endswith("iest") and len(word) > 5:
        return word[:-4] + "y"
    if word.endswith("ier") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ying") and len(word) > 5:
        return word[:-4] + "ie"
    if word.endswith("ing") and len(word) > 5:
        return _restore_silent_e(_remove_doubled_consonant(word[:-3]))
    if word.endswith("ied") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ed") and len(word) > 4:
        return _restore_silent_e(_remove_doubled_consonant(word[:-2]))
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "zes", "sses", "oes")):
        return word[:-2]
    if word.endswith("es") and len(word) > 4:
        return word[:-1]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def lemma_candidates_offline(value: str) -> list[str]:
    """Generate possible lemmas; a dictionary must validate these guesses."""
    word = value.strip().replace("’", "'").casefold()
    candidates: list[str] = []

    def add(candidate: str) -> None:
        normalized = normalize_index_word(candidate)
        if normalized and normalized != normalize_index_word(word) and normalized not in candidates:
            candidates.append(normalized)

    if word in CONTRACTION_LEMMAS:
        add(CONTRACTION_LEMMAS[word])
    if word in IRREGULAR_LEMMAS:
        add(IRREGULAR_LEMMAS[word])
    if word in COMPARATIVE_LEMMAS:
        add(COMPARATIVE_LEMMAS[word])
    add(lemmatize_word_offline(word))
    if word.endswith("'s") and len(word) > 2:
        add(word[:-2])
    if word.endswith("ying") and len(word) > 5:
        add(word[:-4] + "ie")
    if word.endswith("ing") and len(word) > 5:
        stem = word[:-3]
        add(stem)
        add(_remove_doubled_consonant(stem))
        add(stem + "e")
    if word.endswith("ied") and len(word) > 4:
        add(word[:-3] + "y")
    if word.endswith("ed") and len(word) > 4:
        stem = word[:-2]
        add(stem)
        add(_remove_doubled_consonant(stem))
        add(stem + "e")
    if word.endswith("ies") and len(word) > 4:
        add(word[:-3] + "y")
    if word.endswith(("ches", "shes", "xes", "zes", "sses", "oes")):
        add(word[:-2])
    if word.endswith("es") and len(word) > 4:
        add(word[:-1])
        add(word[:-2])
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        add(word[:-1])
    return candidates


def prepare_vocabulary_words(
    sentence: str,
    ignored_words: set[str] | list[str] | tuple[str, ...] | None = None,
    lemma_map: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    ignored = {normalize_index_word(word) for word in (ignored_words or [])}
    result: list[dict[str, str]] = []
    seen_lemmas: set[str] = set()
    for seen_form in extract_words_offline(sentence):
        seen_key = normalize_index_word(seen_form)
        lemma = (lemma_map or {}).get(seen_key) or lemmatize_word_offline(seen_form)
        lemma_key = normalize_index_word(lemma)
        if not lemma_key or lemma_key in seen_lemmas:
            continue
        seen_lemmas.add(lemma_key)
        if lemma_key in ignored or seen_key in ignored:
            continue
        result.append({"word": lemma, "seen_form": seen_form})
    return result


def build_api_endpoint(base_url: str, protocol: str) -> str:
    base_url = base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError("Base URL 必须是完整的 http:// 或 https:// 地址。")
    if protocol == PROTOCOL_RESPONSES:
        if base_url.endswith("/chat/completions"):
            base_url = base_url[: -len("/chat/completions")]
        return base_url if base_url.endswith("/responses") else base_url + "/responses"
    if protocol == PROTOCOL_CHAT_COMPLETIONS:
        if base_url.endswith("/responses"):
            base_url = base_url[: -len("/responses")]
        return base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    raise ConfigError("不支持的 API 协议。")


class VocabularyService:
    RESPONSE_SCHEMA = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "phonetic": {"type": "string"},
                        "part_of_speech": {"type": "string"},
                        "meaning_zh": {"type": "string"},
                        "usage_zh": {"type": "string"},
                        "example": {"type": "string"},
                        "example_zh": {"type": "string"},
                    },
                    "required": [
                        "word",
                        "phonetic",
                        "part_of_speech",
                        "meaning_zh",
                        "usage_zh",
                        "example",
                        "example_zh",
                    ],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    INSTRUCTIONS = (
        "你是严谨的英语词汇老师。用户会提供 JSON，其中 sentence 是上下文，words 是已由本地程序"
        "完成分词、去重和词形还原的固定词表。只补充 words 中每个 word 的词汇信息，严格按输入顺序返回。"
        "不得提取新词、删除词条、改变顺序或修改 word；词形还原不是你的任务。"
        "中文释义必须结合原句语境，简洁准确；用法说明词形变化、常见搭配或语法角色。"
        "例句应简短自然，体现该词在当前含义下的使用，并提供中文翻译。"
        "即使是冠词、代词、介词等功能词也保留，由用户在界面中决定是否收录。"
        "结果将写成三行词条：第一行是单词、发音和词性，第二行是中文释义，"
        "第三行是英文例句及中文翻译。meaning_zh 不超过18个汉字，usage_zh 不超过30个汉字，"
        "example 不超过12个英文单词，example_zh 不超过24个汉字。不要解释你的处理过程。"
    )

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-5-mini",
        base_url: str = DEFAULT_BASE_URL,
        protocol: str = PROTOCOL_RESPONSES,
        json_mode: str = JSON_MODE_SCHEMA,
        timeout: int = 60,
    ):
        self.api_key = api_key.strip()
        self.model = model.strip() or "gpt-5-mini"
        self.base_url = base_url.strip() or DEFAULT_BASE_URL
        self.protocol = protocol
        self.json_mode = json_mode
        try:
            self.timeout = min(600, max(10, int(timeout)))
        except (TypeError, ValueError):
            self.timeout = 60

    @classmethod
    def from_config(cls, config: AppConfig) -> "VocabularyService":
        return cls(
            config.get_api_key(),
            str(config.values.get("model", "gpt-5-mini")),
            str(config.values.get("base_url", DEFAULT_BASE_URL)),
            str(config.values.get("protocol", PROTOCOL_RESPONSES)),
            str(config.values.get("json_mode", JSON_MODE_SCHEMA)),
            config.values.get("timeout", 60),
        )

    def lookup(
        self,
        sentence: str,
        prepared_words: list[dict[str, str]] | list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> list[dict[str, str]]:
        strict_word_list = prepared_words is not None and not (
            prepared_words and all(isinstance(item, str) for item in prepared_words)
        )
        legacy_ignored: list[str] = []
        if prepared_words is None:
            prepared_words = prepare_vocabulary_words(sentence)
        elif prepared_words and all(isinstance(item, str) for item in prepared_words):
            legacy_ignored = [str(item) for item in prepared_words]
            prepared_words = prepare_vocabulary_words(sentence, prepared_words)
        else:
            prepared_words = [
                item
                for item in prepared_words
                if isinstance(item, dict)
                and str(item.get("word", "")).strip()
                and str(item.get("seen_form", "")).strip()
            ]
        if not prepared_words:
            raise LookupError("句子中没有需要翻译的英文单词。")
        if not self.api_key:
            preview = "、".join(item["seen_form"] for item in prepared_words[:8])
            if len(prepared_words) > 8:
                preview += "……"
            raise ConfigError(f"请先在设置中填写 API Key。\n已在本地识别到：{preview}")

        if self.protocol == PROTOCOL_RESPONSES:
            payload = self._responses_payload(sentence, prepared_words, legacy_ignored)
        elif self.protocol == PROTOCOL_CHAT_COMPLETIONS:
            payload = self._chat_completions_payload(sentence, prepared_words, legacy_ignored)
        else:
            raise ConfigError("不支持的 API 协议。")
        data = self._post_json(build_api_endpoint(self.base_url, self.protocol), payload)

        if self.protocol == PROTOCOL_RESPONSES:
            output_text = self._response_text(data)
        else:
            output_text = self._chat_completion_text(data)
        if not output_text:
            raise LookupError("模型 API 没有返回词汇结果。")
        parsed = self._parse_json_output(output_text)
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise LookupError("词汇结果缺少 items 列表。")
        return self._validate_items(items, prepared_words, strict_word_list, legacy_ignored)

    @staticmethod
    def _request_input(sentence: str, prepared_words: list[dict[str, str]]) -> str:
        return json.dumps(
            {"sentence": sentence, "words": prepared_words},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _responses_payload(
        self,
        sentence: str,
        prepared_words: list[dict[str, str]],
        legacy_ignored: list[str] | None = None,
    ) -> dict:
        return {
            "model": self.model,
            "instructions": self._instructions_for(legacy_ignored or []),
            "input": self._request_input(sentence, prepared_words),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "sentence_vocabulary",
                    "strict": True,
                    "schema": self.RESPONSE_SCHEMA,
                }
            },
            "max_output_tokens": self._output_token_budget(len(prepared_words)),
            "store": False,
        }

    def _chat_completions_payload(
        self,
        sentence: str,
        prepared_words: list[dict[str, str]],
        legacy_ignored: list[str] | None = None,
    ) -> dict:
        instructions = self._instructions_for(legacy_ignored or [])
        if self.json_mode == JSON_MODE_OBJECT:
            instructions += (
                "只返回合法的 JSON 对象，不要使用 Markdown 代码块。"
                "JSON 顶层格式必须为 {\"items\": [...]}，items 中每项必须包含 word、"
                "phonetic、part_of_speech、meaning_zh、usage_zh、example、"
                "example_zh 这七个字符串字段。"
            )
            response_format = {"type": "json_object"}
        elif self.json_mode == JSON_MODE_SCHEMA:
            response_format = {
                "type": "json_schema",
                "json_schema": {
                    "name": "sentence_vocabulary",
                    "strict": True,
                    "schema": self.RESPONSE_SCHEMA,
                },
            }
        else:
            raise ConfigError("不支持的 JSON 输出模式。")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": self._request_input(sentence, prepared_words)},
            ],
            "response_format": response_format,
            "max_tokens": self._output_token_budget(len(prepared_words)),
            "stream": False,
        }
        if urlparse(self.base_url).hostname == "api.deepseek.com":
            payload["thinking"] = {"type": "disabled"}
        return payload

    def _instructions_for(self, ignored_words: list[str]) -> str:
        if not ignored_words:
            return self.INSTRUCTIONS
        unique = []
        seen = set()
        for word in ignored_words:
            normalized = normalize_index_word(word)
            if normalized and normalized not in seen:
                seen.add(normalized)
                unique.append(str(word).strip())
        if not unique:
            return self.INSTRUCTIONS
        return (
            self.INSTRUCTIONS
            + "以下单词已加入免翻译词表："
            + ", ".join(unique)
            + "。不要为这些单词返回词条，只处理句子中的其他英文单词。"
        )

    @staticmethod
    def _output_token_budget(word_count: int) -> int:
        word_count = max(1, word_count)
        return min(3600, max(800, word_count * 140))

    def _post_json(self, endpoint: str, payload: dict) -> dict:
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "SentenceWordPicker/1.1 (Windows)",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("error", {}).get("message", body)
            except json.JSONDecodeError:
                message = body
            raise LookupError(f"模型 API 返回 {exc.code}：{message}") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError) or "timed out" in str(exc.reason).lower():
                raise LookupError(f"模型请求超过 {self.timeout} 秒，请重试或调高超时设置。") from exc
            raise LookupError(f"无法连接模型 API：{exc.reason}") from exc
        except (TimeoutError, json.JSONDecodeError) as exc:
            if isinstance(exc, TimeoutError):
                raise LookupError(f"模型请求超过 {self.timeout} 秒，请重试或调高超时设置。") from exc
            raise LookupError("API 返回了无法解析的内容。") from exc

    @staticmethod
    def _parse_json_output(output_text: str) -> dict:
        text = output_text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
            text = re.sub(r"\s*```$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end <= start:
                raise LookupError("词汇结果不是有效的 JSON。")
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError as exc:
                raise LookupError("词汇结果不是有效的 JSON。") from exc
        if not isinstance(parsed, dict):
            raise LookupError("词汇结果不是 JSON 对象。")
        return parsed

    @staticmethod
    def _response_text(data: dict) -> str:
        direct = data.get("output_text")
        if isinstance(direct, str):
            return direct
        for output in data.get("output", []):
            if not isinstance(output, dict) or output.get("type") != "message":
                continue
            for content in output.get("content", []):
                if isinstance(content, dict) and content.get("type") == "output_text":
                    text = content.get("text")
                    if isinstance(text, str):
                        return text
        return ""

    @staticmethod
    def _chat_completion_text(data: dict) -> str:
        choices = data.get("choices", [])
        if not choices or not isinstance(choices[0], dict):
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            return "".join(parts)
        return ""

    @staticmethod
    def _validate_items(
        items: list,
        prepared_words: list[dict[str, str]],
        strict_word_list: bool = True,
        ignored_words: list[str] | None = None,
    ) -> list[dict[str, str]]:
        required = (
            "word",
            "phonetic",
            "part_of_speech",
            "meaning_zh",
            "usage_zh",
            "example",
            "example_zh",
        )
        by_word: dict[str, dict[str, str]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            normalized = {key: str(item.get(key, "")).strip() for key in required}
            word_key = normalize_index_word(normalized["word"])
            if not word_key or word_key in by_word:
                continue
            by_word[word_key] = normalized

        ignored = {normalize_index_word(word) for word in (ignored_words or [])}
        if ignored:
            by_word = {key: value for key, value in by_word.items() if key not in ignored}
        result: list[dict[str, str]] = []
        missing: list[str] = []
        expected_keys = {normalize_index_word(str(item.get("word", ""))) for item in prepared_words}
        unexpected = sorted(key for key in by_word if key not in expected_keys)
        if strict_word_list and unexpected:
            raise LookupError("模型返回了未请求的词条：" + "、".join(unexpected[:8]))
        if not strict_word_list:
            for item in by_word.values():
                item["seen_form"] = item.get("seen_form") or item["word"]
            if not by_word:
                raise LookupError("句子中的单词都在免翻译词表中。")
            return list(by_word.values())
        for prepared in prepared_words:
            word = str(prepared.get("word", "")).strip()
            item = by_word.get(normalize_index_word(word))
            if not item:
                missing.append(word)
                continue
            item["word"] = word
            item["seen_form"] = str(prepared.get("seen_form", word)).strip()
            result.append(item)
        if missing:
            raise LookupError("模型未返回本地词表中的词条：" + "、".join(missing[:8]))
        return result


def _clean_cell(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _entry_rows(
    items: list[dict[str, str]], sentence: str, added_at: int | None = None
) -> list[dict[str, str]]:
    timestamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(added_at or time.time()))
    rows = []
    for item in items:
        rows.append(
            {
                "单词": _clean_cell(item.get("word", "")),
                "句中形式": _clean_cell(item.get("seen_form", "")),
                "音标": _clean_cell(item.get("phonetic", "")),
                "词性": _clean_cell(item.get("part_of_speech", "")),
                "中文释义": _clean_cell(item.get("meaning_zh", "")),
                "用法": _clean_cell(item.get("usage_zh", "")),
                "例句": _clean_cell(item.get("example", "")),
                "例句翻译": _clean_cell(item.get("example_zh", "")),
                "来源句子": _clean_cell(sentence),
                "收录时间": timestamp,
            }
        )
    return rows


@dataclass(frozen=True)
class ForegroundWindowContext:
    handle: int
    process_id: int
    process_name: str
    executable: str
    title: str


@dataclass(frozen=True)
class DetectedSourceContext:
    application: str
    path: str
    window_title: str = ""
    record_id: str = ""

    @classmethod
    def from_mapping(cls, value: dict) -> "DetectedSourceContext | None":
        path = str(value.get("path", "")).strip()
        if not path or not Path(path).expanduser().is_file():
            return None

        return cls(
            application=str(value.get("application", "本地应用")),
            path=str(Path(path).expanduser().resolve()),
            window_title=str(value.get("window_title", "")),
        )


@dataclass(frozen=True)
class ObsidianCaptureContext:
    vault_path: str
    capture_id: str
    file_path: str
    file_name: str
    selection: str


@dataclass(frozen=True)
class ObsidianFileContext:
    vault_path: str
    file_path: str
    file_name: str
    extension: str
    page_number: int | None = None
    # 插件在 /capture 时发的临时凭据。PDF 的高光要让插件在自己的数据里记一条锚点
    # （浮层用），这一步必须带上它——插件无法只凭文件名知道用户选的是哪一段。
    capture_id: str = ""
    selection: str = ""


def find_obsidian_vault(path: Path | str) -> Path | None:
    candidate = Path(path).expanduser()
    if candidate.is_file() or (not candidate.is_dir() and candidate.suffix):
        candidate = candidate.parent
    try:
        candidate = candidate.resolve()
    except OSError:
        return None
    for directory in (candidate, *candidate.parents):
        if (directory / ".obsidian").is_dir():
            return directory
    return None


def highlight_single_export_target(source_vault: str, index_path: str,
                                   note_dir: str) -> dict[str, str]:
    """Infer the destination from the index, independently of the capture vault."""
    target_vault = find_obsidian_vault(index_path)
    # Old configs and in-memory test adapters need no migration for same-vault exports.
    if target_vault is None:
        if not source_vault:
            raise ObsidianBridgeError("索引目录不属于 Obsidian 仓库（缺少 .obsidian）。")
        target_vault = Path(source_vault).expanduser().resolve()
    note_vault = find_obsidian_vault(note_dir)
    if note_vault is not None and note_vault != target_vault:
        raise ObsidianBridgeError("索引文档与注释目录必须位于同一个导出仓库。")
    payload = {
        "index_path": highlight_vault_relative(index_path, str(target_vault), ".md"),
        "note_dir": highlight_vault_relative(note_dir, str(target_vault), ""),
    }
    if not source_vault or target_vault != Path(source_vault).expanduser().resolve():
        payload["vault_path"] = str(target_vault)
    return payload


def highlight_export_target(source_vault: str, directory: str) -> dict[str, str]:
    target_vault = find_obsidian_vault(directory) or Path(source_vault).expanduser().resolve()
    payload = {"directory": highlight_vault_relative(directory, str(target_vault), "")}
    if target_vault != Path(source_vault).expanduser().resolve():
        payload["vault_path"] = str(target_vault)
    return payload


def obsidian_plugin_update_candidates(configured: str = "", export_vault: str = "",
                                      registry_path: Path | None = None,
                                      highlight_vault: str = "") -> list[str]:
    """Open Obsidian vaults first; configured and recent vaults are fallbacks only."""
    registry_path = registry_path or Path(os.environ.get("APPDATA", "~")).expanduser() / "obsidian" / "obsidian.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8-sig"))
        vaults = registry.get("vaults", {})
        records = [v for v in vaults.values() if isinstance(v, dict)] if isinstance(vaults, dict) else []
    except (OSError, ValueError, TypeError, AttributeError):
        records = []
    def timestamp(record):
        try:
            return float(record.get("ts", 0))
        except (ValueError, TypeError):
            return 0.0
    records.sort(key=timestamp, reverse=True)
    paths = [v.get("path", "") for v in records if v.get("open") is True]
    paths.extend((configured, highlight_vault, export_vault))
    paths.extend(v.get("path", "") for v in records)
    result, seen = [], set()
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            candidate = Path(raw).expanduser().resolve()
            key = str(candidate).casefold()
            if (candidate / ".obsidian").is_dir() and key not in seen:
                seen.add(key)
                result.append(str(candidate))
        except (OSError, ValueError):
            continue
    return result


def obsidian_source_link(file_path: str, file_name: str, anchor: str) -> str:
    clean_path = str(file_path).replace("\\", "/").replace("|", "\\|")
    clean_name = str(file_name).replace("|", "\\|").replace("]", "\\]")
    clean_anchor = re.sub(r"[^A-Za-z0-9-]", "", str(anchor))
    if not clean_path or not clean_anchor:
        raise ObsidianBridgeError("Obsidian 返回了无效的来源位置。")
    return f"[[{clean_path}#^{clean_anchor}|来源：{clean_name}]]"


def obsidian_file_source_link(
    file_path: str, file_name: str, page_number: int | None = None
) -> str:
    clean_path = str(file_path).replace("\\", "/").replace("|", "\\|")
    clean_name = str(file_name).replace("|", "\\|").replace("]", "\\]")
    if not clean_path:
        raise ObsidianBridgeError("Obsidian 返回了无效的来源文件。")
    page_suffix = f"#page={page_number}" if page_number and page_number > 0 else ""
    return f"[[{clean_path}{page_suffix}|来源：{clean_name}]]"


class ObsidianBridgeClient:
    def __init__(self, vault_path: Path | str):
        self.vault_path = Path(vault_path).expanduser().resolve()
        settings_path = (
            self.vault_path
            / ".obsidian"
            / "plugins"
            / OBSIDIAN_PLUGIN_ID
            / "data.json"
        )
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ObsidianBridgeError(
                "Obsidian 桥接插件尚未启用，请先安装并在社区插件中启用。"
            ) from exc
        token = str(settings.get("token", "")).strip()
        try:
            port = int(settings.get("port", OBSIDIAN_DEFAULT_PORT))
        except (TypeError, ValueError) as exc:
            raise ObsidianBridgeError("Obsidian 桥接插件端口配置无效。") from exc
        if not token or not 1 <= port <= 65535:
            raise ObsidianBridgeError("Obsidian 桥接插件配置无效。")
        self.token = token
        self.port = port

    def warm_highlight_pipe(self) -> None:
        """Connect and verify reusable framing off the click path."""
        if os.name == "nt":
            pipe_bridge.warm(self.token, self.port)

    def _pipe_request(self, route: str, payload: dict, *, timeout: float) -> dict | None:
        """Only use an already-prepared pipe; unavailable before send falls back HTTP."""
        if os.name != "nt":
            self._highlight_pipe_fallback_reason = "pipe_unavailable"
            return None
        try:
            result = pipe_bridge.warm(self.token, self.port).request(route, payload, timeout)
        except pipe_bridge.PipeReplyError as exc:
            raise ObsidianBridgeError(str(exc)) from exc
        except ValueError as exc:
            raise ObsidianBridgeError("桥接请求过大。") from exc
        except pipe_bridge.PipeUncertain as exc:
            self._highlight_pipe_fallback_reason = exc.reason
            if exc.reason == "timeout":
                raise ObsidianBridgeTimeout(
                    f"Obsidian 命名管道响应超过 {timeout:g} 秒，结果尚未确认。") from exc
            raise ObsidianBridgeTransportError(
                f"Obsidian 命名管道连接中断（{exc.reason}），结果尚未确认。") from exc
        except OSError as exc:
            self._highlight_pipe_fallback_reason = "write_failed"
            raise ObsidianBridgeTransportError(
                "Obsidian 命名管道连接中断，结果尚未确认。") from exc
        if result is None:
            self._highlight_pipe_fallback_reason = "pipe_unavailable"
        elif payload.get("trace"):
            result["_bridge_transport"] = "pipe"  # historical probe key
        return result

    def _request(
        self, route: str, payload: dict | None = None, *, timeout: float = 1.5
    ) -> dict:
        pipe_fallback_reason = ""
        if route == "/highlight" and payload is not None:
            self._highlight_pipe_fallback_reason = ""
            piped = self._pipe_request(route, payload, timeout=timeout)
            if piped is not None:
                return piped
            pipe_fallback_reason = self._highlight_pipe_fallback_reason or "pipe_unavailable"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{route}",
            data=data,
            method="GET" if payload is None else "POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
        )
        http_started_wall_ms = (time.time_ns() // 1_000_000
                                if route == "/highlight" and payload and payload.get("trace")
                                else None)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("error", "")
            except (UnicodeError, json.JSONDecodeError):
                detail = ""
            raise ObsidianBridgeError(detail or f"Obsidian 桥接返回错误 {exc.code}。") from exc
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise ObsidianBridgeTimeout(
                    f"Obsidian 桥接响应超过 {timeout:g} 秒，插件可能仍在处理。"
                ) from exc
            raise ObsidianBridgeTransportError(
                f"无法连接 Obsidian 桥接插件（{type(exc).__name__}），"
                "请确认 Obsidian 正在运行且插件已启用。"
            ) from exc
        if not isinstance(result, dict):
            raise ObsidianBridgeError("Obsidian 桥接返回了无效数据。")
        if http_started_wall_ms is not None:
            result["_bridge_channel"] = "http"
            result["_bridge_transport"] = "http_fallback"
            result["_bridge_fallback_reason"] = pipe_fallback_reason or "pipe_unavailable"
            result["_pipe_fallback_reason"] = pipe_fallback_reason
            result["_bridge_connect_reused"] = False
            result["_bridge_queue_wait_ms"] = None
            result["_bridge_send_ms"] = None  # urllib does not expose TCP send completion.
            result["_bridge_wait_response_ms"] = None
            result["_bridge_pipe_connect_ms"] = None
            result["_http_started_wall_ms"] = http_started_wall_ms
        return result

    def health(self) -> str:
        return str(self._request("/health").get("vault", ""))

    def wait_for_native_mode(self) -> dict:
        """Authenticated long-poll; the plugin replies immediately when its mode bar is clicked."""
        return self._request("/desktop-mode", timeout=18)

    def configure_highlight_entry(self, enabled: bool) -> dict:
        return self._request("/highlight-entry", {"enabled": bool(enabled)}, timeout=5)

    def export_highlights(self, directory: str, color: str = "", vault_path: str = "") -> dict:
        """color 为空导全部（落到「所有高光」档），否则只导该颜色那一档。"""
        payload = {"directory": directory}
        if color:
            payload["color"] = color
        if vault_path:
            if not self._request("/health", timeout=2).get("cross_vault_export"):
                raise ObsidianBridgeError("当前插件不支持跨仓库完整导出，请更新并重新加载 2.5.1。")
            payload["vault_path"] = vault_path
        result = self._request("/highlight-export", payload, timeout=15)
        if vault_path and result.get("vault_path") != vault_path:
            raise ObsidianBridgeError("插件尚不支持跨仓库完整导出，请更新并重新加载 2.5.1。")
        return result

    def highlight_export_options(self) -> dict:
        """每次点下拉现问插件：哪些颜色真的有高光、各多少条。桌面端不缓存。"""
        return self._request("/highlight-export-options", timeout=5)

    def configure_highlight_active_colors(self, colors: list) -> dict:
        try:
            return self._request("/highlight-active-colors", {"colors": list(colors)}, timeout=5)
        except ObsidianBridgeError as exc:
            # 这条路由是 2.5.0 才有的，旧插件的 404 同样值得说得明白点。
            if str(exc) == "not found":
                raise ObsidianBridgeError(
                    "Obsidian 桥接插件运行版本较旧，请正常重载 2.5.0 后再改悬浮窗颜色。") from exc
            raise

    def configure_highlight_single_export(self, index_path: str, note_dir: str,
                                          vault_path: str = "") -> dict:
        try:
            payload = {"index_path": index_path, "note_dir": note_dir}
            if vault_path:
                if not self._request("/health", timeout=2).get("cross_vault_export"):
                    raise ObsidianBridgeError("当前插件不支持跨仓库单条导出，请更新并重新加载 2.5.1。")
                payload["vault_path"] = vault_path
            result = self._request("/highlight-single-export-config", payload, timeout=5)
            if vault_path and str(result.get("config", {}).get("vaultPath", "")) != vault_path:
                raise ObsidianBridgeError(
                    "当前插件不支持跨仓库单条导出。请更新并重新加载 2.5.1 或更高版本后再导出。")
            return result
        except ObsidianBridgeError as exc:
            # 这条路由是 2.4.0 才有的：旧插件的 404 值得说得明白点，
            # 否则用户只看到一句「not found」。
            if str(exc) == "not found":
                raise ObsidianBridgeError(
                    "Obsidian 桥接插件运行版本较旧，请正常重载 2.4.0 后再配置单条导出。") from exc
            raise

    def highlight_diagnostics(self) -> dict:
        return self._request("/highlight-diagnostics", timeout=5)

    def reset_highlights(self, expected_count: int) -> dict:
        return self._request("/highlight-reset", {
            "confirm": "CLEAR_ALL_HIGHLIGHTS", "expected_count": expected_count,
        }, timeout=20)

    def capture(self) -> ObsidianCaptureContext | ObsidianFileContext:
        result = self._request("/capture", {})
        if result.get("kind") == "obsidian-file":
            try:
                page_number = int(result.get("page_number"))
                if page_number <= 0:
                    page_number = None
            except (TypeError, ValueError):
                page_number = None
            context = ObsidianFileContext(
                vault_path=str(self.vault_path),
                file_path=str(result.get("file_path", "")),
                file_name=str(result.get("file_name", "")),
                extension=str(result.get("extension", "")),
                page_number=page_number,
                capture_id=str(result.get("capture_id", "")),
                selection=str((result.get("highlight") or {}).get("exact", "")),
            )
            if not context.file_path or not context.file_name:
                raise ObsidianBridgeError("Obsidian 没有返回有效的来源文件。")
            return context
        context = ObsidianCaptureContext(
            vault_path=str(self.vault_path),
            capture_id=str(result.get("capture_id", "")),
            file_path=str(result.get("file_path", "")),
            file_name=str(result.get("file_name", "")),
            selection=str(result.get("selection", "")),
        )
        if not context.capture_id or not context.file_path or not context.selection.strip():
            raise ObsidianBridgeError("Obsidian 没有返回有效的 Markdown 选区。")
        return context

    def create_anchor(self, capture_id: str) -> dict:
        result = self._request("/anchor", {"capture_id": capture_id})
        if not result.get("file_path") or not result.get("anchor"):
            raise ObsidianBridgeError("Obsidian 没有返回有效的块定位。")
        return result

    def apply_highlight(
        self, capture_id: str, color: str, *, trace: bool = False
    ) -> dict:
        """Save a highlight anchor; retry only if the running plugin promises idempotence."""
        payload = {"capture_id": capture_id, "color": color}
        if trace:
            payload["trace"] = True
        attempts: list[dict] = []

        def post(stage: str) -> dict:
            started = time.perf_counter()
            record_highlight_event("bridge_request", capture_id=capture_id, stage=stage,
                                   route="/highlight")
            try:
                bridge_send_wall_ms = time.time_ns() // 1_000_000
                if trace:
                    attempts.append({"stage": stage, "send_wall_ms": bridge_send_wall_ms})
                result = self._request("/highlight", payload, timeout=5.0)
                if trace:
                    result["_bridge_attempt_stage"] = stage
                    result["_bridge_send_wall_ms"] = bridge_send_wall_ms
                    result["_bridge_response_wall_ms"] = time.time_ns() // 1_000_000
                    result["_bridge_first_send_wall_ms"] = attempts[0]["send_wall_ms"]
                    if len(attempts) > 1:
                        result["_bridge_first_error_wall_ms"] = attempts[0].get("error_wall_ms")
                        result["_bridge_retry_send_wall_ms"] = bridge_send_wall_ms
            except (OSError, ObsidianBridgeError) as exc:
                if trace and attempts:
                    attempts[-1]["error_wall_ms"] = time.time_ns() // 1_000_000
                record_highlight_event("bridge_error", capture_id=capture_id,
                                       stage=stage, error_type=type(exc).__name__,
                                       fallback_reason=getattr(self, "_highlight_pipe_fallback_reason", None),
                                       error=str(exc), elapsed_ms=round((time.perf_counter()-started)*1000))
                raise
            record_highlight_event("bridge_response", capture_id=capture_id,
                                   stage=stage, highlight_id=result.get("highlight_id", ""),
                                   plugin_ms=result.get("plugin_ms"),
                                   storage_ms=result.get("storage_ms"),
                                   storage_mkdir_ms=result.get("storage_mkdir_ms"),
                                   storage_append_ms=result.get("storage_append_ms"),
                                   transport=result.get("_bridge_channel", "unknown"),
                                   pipe_fallback_reason=result.get("_bridge_fallback_reason"),
                                   elapsed_ms=round((time.perf_counter()-started)*1000))
            return result

        try:
            result = post("first")
        except ObsidianBridgeTransportError as exc:
            initial_reason = getattr(self, "_highlight_pipe_fallback_reason", None)
            uncertain = (
                "高光请求连接中断或超时，结果尚未确认；请勿立即重复划选。"
                "可到 Obsidian 查看高光或运行「显示高光诊断」。"
            )
            try:
                health = self._request("/health", timeout=5.0)
            except ObsidianBridgeError as health_error:
                record_highlight_event("retry_unavailable", capture_id=capture_id,
                                       reason="health_failed", error=str(health_error))
                raise ObsidianBridgeError(uncertain) from exc
            if not health.get("highlight_retry"):
                record_highlight_event("retry_unavailable", capture_id=capture_id,
                                       reason="plugin_no_idempotency")
                raise ObsidianBridgeError(uncertain + "请先更新 Obsidian 插件。") from exc
            record_highlight_event("retry_supported", capture_id=capture_id,
                                   last_highlight_ms=health.get("last_highlight_ms", ""))
            try:
                result = post("retry")
                if trace and result.get("_bridge_channel") == "http" and initial_reason:
                    result["_bridge_fallback_reason"] = initial_reason
            except ObsidianBridgeTransportError as retry_error:
                record_highlight_event("result_uncertain", capture_id=capture_id)
                raise ObsidianBridgeError(uncertain) from retry_error
        if not result.get("file_path") or not result.get("highlight_id"):
            record_highlight_event("bridge_invalid_response", capture_id=capture_id)
            raise ObsidianBridgeError("Obsidian 没有返回有效的高光标识。")
        return result

    @staticmethod
    def install(vault_path: Path | str) -> Path:
        vault = Path(vault_path).expanduser().resolve()
        obsidian_dir = vault / ".obsidian"
        if not obsidian_dir.is_dir():
            raise ObsidianBridgeError("所选目录不是有效的 Obsidian 仓库。")
        if not OBSIDIAN_PLUGIN_SOURCE_DIR.is_dir():
            raise ObsidianBridgeError("程序缺少 Obsidian 桥接插件文件。")
        destination = obsidian_dir / "plugins" / OBSIDIAN_PLUGIN_ID
        destination.mkdir(parents=True, exist_ok=True)
        for name in ("manifest.json", "main.js", "styles.css"):
            shutil.copy2(OBSIDIAN_PLUGIN_SOURCE_DIR / name, destination / name)
        return destination

    @staticmethod
    def plugin_version(manifest: Path) -> str:
        """读 manifest.json 的版本号；文件缺失或损坏时返回空串。"""
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        version = payload.get("version") if isinstance(payload, dict) else None
        return str(version).strip() if version else ""

    @staticmethod
    def source_plugin_version() -> str:
        """程序里这份插件源的版本号。"""
        return ObsidianBridgeClient.plugin_version(
            OBSIDIAN_PLUGIN_SOURCE_DIR / "manifest.json"
        )

    @staticmethod
    def installed_plugin_version(vault_path: Path | str) -> str:
        """仓库里当前装着的是哪一版；还没装过时返回空串。"""
        try:
            vault = Path(vault_path).expanduser().resolve()
        except OSError:
            return ""
        return ObsidianBridgeClient.plugin_version(
            vault / ".obsidian" / "plugins" / OBSIDIAN_PLUGIN_ID / "manifest.json"
        )


class SourceContextDetector:
    """Run optional editor adapters without coupling them to capture flow."""

    @staticmethod
    def detect(window: ForegroundWindowContext | None) -> DetectedSourceContext | None:
        if not window or not SOURCE_CONTEXT_SCRIPT.is_file():
            return None
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            return None
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(SOURCE_CONTEXT_SCRIPT),
            "-ProcessId",
            str(window.process_id),
            "-ProcessName",
            window.process_name,
            "-WindowTitle",
            window.title,
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        for line in reversed(completed.stdout.splitlines()):
            try:
                payload = json.loads(line.strip().lstrip("\ufeff"))
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(payload, dict):
                return DetectedSourceContext.from_mapping(payload)
        return None


class DocxPdfConverter:
    """Optional, isolated DOCX-to-PDF adapter for Obsidian source snapshots."""

    def __init__(self, script_path: Path | str = DOCX_PDF_CONVERTER_SCRIPT):
        self.script_path = Path(script_path)
        self.last_error = ""

    @staticmethod
    def _is_docx(path: Path) -> bool:
        if path.suffix.casefold() != ".docx" or not zipfile.is_zipfile(path):
            return False
        try:
            with zipfile.ZipFile(path) as archive:
                return "word/document.xml" in archive.namelist()
        except (OSError, zipfile.BadZipFile):
            return False

    @staticmethod
    def is_valid_pdf(path: Path | str) -> bool:
        candidate = Path(path)
        try:
            if not candidate.is_file() or candidate.stat().st_size <= 5:
                return False
            with candidate.open("rb") as handle:
                return handle.read(5) == b"%PDF-"
        except OSError:
            return False

    def convert(self, source_path: Path | str, output_path: Path | str) -> str | None:
        source = Path(source_path).expanduser().resolve()
        output = Path(output_path).expanduser().resolve()
        self.last_error = ""
        if not self._is_docx(source):
            self.last_error = "来源不是有效 DOCX。"
            return None
        if not self.script_path.is_file():
            self.last_error = "转换脚本缺失。"
            return None
        powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not powershell:
            if not self.last_error:
                self.last_error = "系统缺少 PowerShell，无法调用后备转换器。"
            return None
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists() and not self.is_valid_pdf(output):
            try:
                output.unlink()
            except OSError as exc:
                self.last_error = f"无法清理无效的 PDF 输出：{exc}"
                return None
        command = [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self.script_path),
            "-SourcePath",
            str(source),
            "-OutputPath",
            str(output),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = str(exc)
            return None
        direct_error = self.last_error
        script_error = ""
        reported_engine = ""
        for line in reversed(completed.stdout.splitlines()):
            try:
                result = json.loads(line.strip().lstrip("\ufeff"))
            except (json.JSONDecodeError, AttributeError):
                continue
            if isinstance(result, dict) and result.get("ok"):
                reported_engine = str(result.get("engine", "converter"))
                break
            if isinstance(result, dict) and result.get("error"):
                script_error = str(result.get("error"))
        if completed.returncode != 0 or not self.is_valid_pdf(output):
            try:
                if output.is_file():
                    output.unlink()
            except OSError:
                pass
            self.last_error = "; ".join(
                part
                for part in (direct_error, script_error, completed.stderr.strip())
                if part
            ) or "DOCX 转 PDF 失败。"
            return None
        self.last_error = ""
        return reported_engine or "converter"


class SourceLibrary:
    """Store immutable local-source snapshots for stable Obsidian links."""

    def __init__(
        self,
        path: Path | str = SOURCE_LIBRARY_PATH,
        docx_converter: DocxPdfConverter | None = None,
    ):
        self.path = Path(path)
        self.snapshot_dir = self.path.parent / SOURCE_LIBRARY_DIR_NAME
        self.records: dict[str, dict[str, object]] = {}
        self.docx_converter = docx_converter or DocxPdfConverter()
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(loaded, dict):
            self.records = {str(key): value for key, value in loaded.items() if isinstance(value, dict)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.records, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def register(
        self,
        path: Path | str,
        storage_dir: Path | str | None = None,
        storage_path: Path | str | None = None,
    ) -> str:
        with self._lock:
            source_path = Path(path).expanduser().resolve()
            if not source_path.is_file():
                raise OSError("来源文件不存在。")
            digest = self._sha256(source_path)
            record_id = digest
            existing = self.records.get(record_id, {})
            destination_root = (
                Path(storage_path).expanduser().resolve()
                if storage_path
                else (
                    Path(storage_dir).expanduser().resolve() / SOURCE_LIBRARY_DIR_NAME
                    if storage_dir
                    else self.snapshot_dir
                )
            )
            record_dir = (destination_root / record_id).resolve()
            stored_snapshots = existing.get("snapshots", [])
            snapshot_values = list(stored_snapshots) if isinstance(stored_snapshots, list) else []
            legacy_snapshot = str(existing.get("snapshot", "")).strip()
            if legacy_snapshot:
                snapshot_values.append(legacy_snapshot)

            destination = None
            for snapshot_value in snapshot_values:
                candidate = Path(str(snapshot_value)).expanduser()
                try:
                    belongs_to_record_dir = candidate.resolve().parent == record_dir
                except OSError:
                    belongs_to_record_dir = False
                if belongs_to_record_dir and candidate.is_file():
                    destination = candidate.resolve()
                    break
            if destination is None:
                destination = destination_root / record_id / source_path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
            pdf_snapshots = existing.get("pdf_snapshots", [])
            pdf_values = list(pdf_snapshots) if isinstance(pdf_snapshots, list) else []
            conversion_engine = str(existing.get("conversion_engine", ""))
            conversion_error = str(existing.get("conversion_error", ""))
            if source_path.suffix.casefold() == ".docx":
                pdf_destination = destination.with_suffix(".pdf")
                if not self.docx_converter.is_valid_pdf(pdf_destination):
                    engine = self.docx_converter.convert(destination, pdf_destination)
                    if engine:
                        conversion_engine = engine
                        conversion_error = ""
                    else:
                        conversion_error = str(
                            getattr(self.docx_converter, "last_error", "")
                        )
                if (
                    self.docx_converter.is_valid_pdf(pdf_destination)
                    and str(pdf_destination) not in pdf_values
                ):
                    pdf_values.append(str(pdf_destination))
                if self.docx_converter.is_valid_pdf(pdf_destination):
                    try:
                        destination.unlink()
                    except OSError as exc:
                        conversion_error = f"PDF 已生成，但删除 DOCX 副本失败：{exc}"
            saved_snapshots = [
                str(Path(str(value)).expanduser())
                for value in snapshot_values
                if str(value).strip() and Path(str(value)).expanduser().is_file()
            ]
            if destination.is_file() and str(destination) not in saved_snapshots:
                saved_snapshots.append(str(destination))
            self.records[record_id] = {
                "name": source_path.name,
                "sha256": digest,
                "snapshots": saved_snapshots,
                "pdf_snapshots": [
                    str(Path(str(value)).expanduser())
                    for value in pdf_values
                    if (
                        str(value).strip()
                        and Path(str(value)).expanduser().is_file()
                        and self.docx_converter.is_valid_pdf(
                            Path(str(value)).expanduser()
                        )
                    )
                ],
                "conversion_engine": conversion_engine,
                "conversion_error": conversion_error,
            }
            self._save()
            return record_id

    def resolve(
        self,
        record_id: str,
        storage_dir: Path | str | None = None,
        storage_path: Path | str | None = None,
    ) -> str | None:
        record = self.records.get(record_id)
        if not record:
            return None
        stored_snapshots = record.get("snapshots", [])
        snapshot_values = list(stored_snapshots) if isinstance(stored_snapshots, list) else []
        legacy_snapshot = str(record.get("snapshot", "")).strip()
        if legacy_snapshot:
            snapshot_values.append(legacy_snapshot)
        snapshots = [
            Path(str(value)).expanduser()
            for value in snapshot_values
            if str(value).strip() and Path(str(value)).expanduser().is_file()
        ]
        if storage_dir is not None or storage_path is not None:
            expected_root = (
                Path(storage_path).expanduser().resolve()
                if storage_path
                else (Path(storage_dir).expanduser().resolve() / SOURCE_LIBRARY_DIR_NAME).resolve()
            )
            for snapshot in snapshots:
                try:
                    snapshot.resolve().relative_to(expected_root)
                except (OSError, ValueError):
                    continue
                return str(snapshot)
        return str(snapshots[0]) if snapshots else None

    def display_name(self, record_id: str) -> str:
        return str(self.records.get(record_id, {}).get("name", "来源文件"))

    def conversion_error(self, record_id: str) -> str:
        return str(self.records.get(record_id, {}).get("conversion_error", ""))

    def resolve_link_target(
        self,
        record_id: str,
        storage_dir: Path | str | None = None,
        storage_path: Path | str | None = None,
    ) -> str | None:
        record = self.records.get(record_id, {})
        pdf_values = record.get("pdf_snapshots", [])
        candidates = list(pdf_values) if isinstance(pdf_values, list) else []
        expected_root = None
        if storage_dir is not None or storage_path is not None:
            expected_root = (
                Path(storage_path).expanduser().resolve()
                if storage_path
                else (Path(storage_dir).expanduser().resolve() / SOURCE_LIBRARY_DIR_NAME).resolve()
            )
        for value in candidates:
            candidate = Path(str(value)).expanduser()
            if not candidate.is_file():
                continue
            if expected_root is not None:
                try:
                    candidate.resolve().relative_to(expected_root)
                except (OSError, ValueError):
                    continue
            return str(candidate.resolve())
        return self.resolve(record_id, storage_dir, storage_path)

    def context_from_window(
        self,
        window: ForegroundWindowContext | None,
        storage_dir: Path | str | None = None,
        storage_path: Path | str | None = None,
    ) -> DetectedSourceContext | None:
        if not window or not window.title:
            return None
        title = window.title.casefold()
        matching_record_ids: set[str] = set()
        with self._lock:
            records = list(self.records.items())
        for record_id, record in records:
            name = str(record.get("name", "")).strip()
            if name and name.casefold() in title:
                matching_record_ids.add(record_id)
        if len(matching_record_ids) != 1:
            return None
        record_id = next(iter(matching_record_ids))
        resolved = self.resolve_link_target(record_id, storage_dir, storage_path)
        if not resolved:
            return None
        return DetectedSourceContext(
            application=window.process_name or "本地应用",
            path=resolved,
            window_title=window.title,
            record_id=record_id,
        )

    def markdown_link(
        self,
        record_id: str,
        document_path: Path | str,
        storage_path: Path | str | None = None,
    ) -> str:
        document = Path(document_path).expanduser().resolve()
        snapshot = self.resolve_link_target(record_id, storage_path=storage_path, storage_dir=None if storage_path else document.parent)
        if not snapshot:
            return "[来源快照不可用]"
        try:
            relative = Path(snapshot).resolve().relative_to(document.parent).as_posix()
        except ValueError:
            relative = Path(snapshot).resolve().as_uri()
        display_name = self.display_name(record_id)
        if (
            Path(snapshot).suffix.casefold() == ".pdf"
            and Path(display_name).suffix.casefold() == ".docx"
        ):
            label = f"来源：{Path(display_name).stem}.pdf（由 DOCX 转换）"
        else:
            label = f"来源：{display_name}"
        return f"[{label}](<{relative}>)"

def question_detail_document_path(path: Path | str) -> Path:
    output_path = Path(path).expanduser()
    safe_stem = re.sub(r"[\[\]#|^]", "_", output_path.stem).strip(". ") or "question-records"
    digest = hashlib.sha256(output_path.name.encode("utf-8")).hexdigest()[:8]
    return output_path.with_name(f"{safe_stem}-details-{digest}.md")




class DocumentWriter:
    @classmethod
    def append(
        cls,
        path: Path | str,
        items: list[dict[str, str]],
        sentence: str,
        added_at: int | None = None,
    ) -> None:
        output_path = Path(path).expanduser()
        if not items:
            raise DocumentError("请至少选择一个单词。")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        rows = _entry_rows(items, sentence, added_at)
        suffix = output_path.suffix.lower()
        try:
            if suffix == ".json":
                cls._append_json(output_path, items, sentence, added_at)
            elif suffix == ".md":
                cls._append_markdown(output_path, rows)
            elif suffix == ".txt":
                cls._append_text(output_path, rows)
            elif suffix == ".docx":
                cls._append_docx(output_path, rows)
            else:
                raise DocumentError("仅支持 .json、.md、.txt 和 .docx 文档。")
        except PermissionError as exc:
            raise DocumentError("文档正在被其他程序占用，请关闭文档后重试。") from exc
        except OSError as exc:
            raise DocumentError(f"写入文档失败：{exc}") from exc

    @classmethod
    def _append_markdown(cls, path: Path, rows: list[dict[str, str]]) -> None:
        cls._append_three_line_blocks(path, rows)

    @classmethod
    def _append_text(cls, path: Path, rows: list[dict[str, str]]) -> None:
        cls._append_three_line_blocks(path, rows)

    @staticmethod
    def append_question_record(
        index_path: Path | str,
        body_root: Path | str,
        overview: str,
        selected_text: str,
        title_level: int = 0,
        supplement: str = "",
        content_items: list[tuple[str, object]] | None = None,
        record_mode: str = QUESTION_RECORD_MODE_INDEPENDENT,
        include_index_source_link: bool | None = None,
        include_supplement_link: bool | None = None,
        supplement_items: list[tuple[str, object]] | None = None,
        tags: list[str] | None = None,
    ) -> Path | None:
        """Write one question with independent link choices for each content block.

        tags 只写进索引文件的标题行，不进任何文件名：标签进标题就够 Obsidian
        索引了，进文件名只会让路径变长（Windows 的 260 上限会连带毁掉解压）。
        """
        index_path = Path(index_path).expanduser()
        if index_path.suffix.lower() != ".md":
            raise DocumentError("问题记录仅支持 Markdown 文档。")
        supplement_text = normalize_note_text(supplement)
        supplement_content = list(supplement_items) if supplement_items is not None else ([('text', supplement)] if supplement_text else [])
        source_items = list(content_items) if content_items is not None else [("text", selected_text)]
        # Keep the old direct API behavior for callers that omit the new flags;
        # the popup passes explicit defaults so each block can be controlled.
        if include_index_source_link is None:
            include_index_source_link = True
        if include_supplement_link is None:
            include_supplement_link = True
        body_root = Path(body_root).expanduser()
        if record_mode != QUESTION_RECORD_MODE_INDEPENDENT:
            index_items: list[tuple[str, object]] = [("text", "---- 原始文档"), *source_items]
            if supplement_content:
                index_items.extend([("text", "---- 补充说明"), *supplement_content])
            # 纯内联不生成正文 .md；有图片时仍放进正文库中的记录目录，
            # 而非可能位于 Vault 根的索引目录。无图片不创建空目录。
            image_dir = body_root / fit_record_stem(
                question_record_slug(overview, selected_text), body_root, index_path
            ) if any(kind == "image" for kind, _ in index_items) else None
            if image_dir is not None and image_dir.exists():
                raise DocumentError("记录图片目录已存在，请重试以生成新记录名。")
            try:
                DocumentWriter.append_question(index_path, overview, "", title_level,
                                               content_items=index_items, heading_tags=tags,
                                               image_dir=image_dir)
            except Exception:
                if image_dir is not None and image_dir.exists():
                    shutil.rmtree(image_dir)
                raise
            return None
        record_stem = fit_record_stem(
            question_record_slug(overview, selected_text), body_root, index_path
        )
        record_dir = body_root / record_stem
        body_path = record_dir / f"{record_stem}.md"
        source_hash = hashlib.sha256(record_stem.encode("utf-8")).hexdigest()[:12]
        supplement_hash = hashlib.sha256((record_stem + "-supplement").encode("utf-8")).hexdigest()[:12]
        source_anchor = f"nh-body-source-{source_hash}"
        supplement_anchor = f"nh-body-supplement-{supplement_hash}"
        index_items: list[tuple[str, object]] = []
        body_items: list[tuple[str, object]] = []
        relative_body = os.path.relpath(body_path, index_path.parent).replace("\\", "/")
        if include_index_source_link:
            body_items.extend([("text", "## 原文"), ("text", f"^{source_anchor}"), *source_items])
            index_items.append(("text", f"- [[{relative_body}#^{source_anchor}|原文]]"))
        else:
            index_items.extend([("text", "---- 原始文档"), *source_items])
        if supplement_content:
            if include_supplement_link:
                body_items.extend([("text", "## 补充说明"), ("text", f"^{supplement_anchor}"), *supplement_content])
                index_items.append(("text", f"- [[{relative_body}#^{supplement_anchor}|补充说明]]"))
            else:
                index_items.extend([("text", "---- 补充说明"), *supplement_content])
        if record_dir.exists():
            raise DocumentError("记录目录已存在，请重试以生成新记录名。")
        try:
            if body_items:
                record_dir.mkdir(parents=True, exist_ok=False)
                DocumentWriter.append_question(body_path, overview, "", 1, content_items=body_items, image_dir=record_dir)
            DocumentWriter.append_question(index_path, overview, "", title_level,
                                           content_items=index_items, heading_tags=tags,
                                           image_dir=record_dir if any(kind == "image" for kind, _ in index_items) else None)
        except Exception:
            try:
                if record_dir.exists():
                    shutil.rmtree(record_dir)
            except OSError:
                pass
            raise
        return body_path if body_items else None

    @staticmethod
    def append_compact_question(
        path: Path | str,
        overview: str,
        selected_text: str,
        title_level: int = 0,
        supplement: str = "",
        content_items: list[tuple[str, object]] | None = None,
        include_index_source_link: bool = False,
        include_supplement_link: bool = False,
    ) -> None:
        """Write a short index entry and linked detail sections."""
        output_path = Path(path).expanduser()
        if output_path.suffix.lower() != ".md":
            raise DocumentError("问题记录仅支持 Markdown 文档。")
        supplement_text = normalize_note_text(supplement)
        source_items = list(content_items) if content_items is not None else [("text", selected_text)]
        detail_path = question_detail_document_path(output_path)
        token = f"{time.time_ns():x}-{random.getrandbits(24):06x}"
        source_anchor = f"nh-source-{token}"
        supplement_anchor = f"nh-supplement-{token}"
        index_items: list[tuple[str, object]] = []
        detail_sections: list[tuple[str, str, list[tuple[str, object]]]] = []

        if include_index_source_link:
            detail_sections.append(
                (
                    "问题/知识点来源",
                    selected_text,
                    [("text", f"^{source_anchor}"), *source_items],
                )
            )
            index_items.append(
                ("text", f"- [[./{detail_path.name}#^{source_anchor}|问题/知识点来源]]")
            )
        else:
            index_items.append(("text", "---- 原始文档"))
            index_items.extend(source_items)

        if supplement_text:
            if include_supplement_link:
                detail_sections.append(
                    (
                        "内容补充和对原文的进一步说明",
                        supplement_text,
                        [("text", f"^{supplement_anchor}"), ("text", supplement_text)],
                    )
                )
                index_items.append(
                    (
                        "text",
                        f"- [[./{detail_path.name}#^{supplement_anchor}|内容补充和对原文的进一步说明]]",
                    )
                )
            else:
                index_items.extend(
                    [("text", "---- 补充说明"), ("text", supplement_text)]
                )

        checkpoints = []
        for target in [output_path, *(detail_path for _ in detail_sections)]:
            exists = target.exists()
            checkpoints.append((target, exists, target.stat().st_size if exists else 0))
        try:
            for heading, text, section_items in detail_sections:
                DocumentWriter.append_question(
                    detail_path,
                    heading,
                    text,
                    3,
                    content_items=section_items,
                )
            DocumentWriter.append_question(
                output_path,
                overview,
                "",
                title_level,
                content_items=index_items,
            )
        except Exception:
            for target, existed, original_size in reversed(checkpoints):
                try:
                    if existed and target.exists():
                        with target.open("r+b") as handle:
                            handle.truncate(original_size)
                    elif not existed and target.exists():
                        target.unlink()
                except OSError:
                    pass
            raise

    @staticmethod
    def _heading_tags(tags) -> list[str]:
        """筛出能安全写进标题行的标签。

        界面已经用 front.tags.normalize_tag 筛过一遍，但那层可能整体缺失
        （没有 Pillow / front/ 不全），所以写盘前自己再挡一道：标签里带空格或
        带 `#` 会把标题拆坏。front.tags 在的时候还是用它筛，规则只留一份。
        """
        cleaned: list[str] = []
        seen: set[str] = set()
        for tag in tags or ():
            if ui_tags is not None:
                value = ui_tags.normalize_tag(tag)
            else:
                value = str(tag).strip().lstrip("#")
                if any(char in "# \t" for char in value):
                    value = ""
            key = value.casefold()
            if value and key not in seen:
                seen.add(key)
                cleaned.append(value)
        return cleaned

    @staticmethod
    def append_question(
        path: Path | str,
        question: str,
        selected_text: str,
        title_level: int = 0,
        image_path: Path | str | None = None,
        image_data=None,
        image_items: list[tuple[Path | str | None, object]] | None = None,
        content_items: list[tuple[str, object]] | None = None,
        image_dir: Path | str | None = None,
        heading_tags: list[str] | None = None,
    ) -> None:
        """Append a question and its selected source text as a Markdown block.

        heading_tags 拼在标题行末尾（`### 标题 #标签`），Obsidian 正文里一眼
        看得见，标签面板也认。这里再挡一道：带空格或带 `#` 的写进标题行会把
        标题毁掉，界面已经筛过一遍，写盘前不能只信上游。
        """
        output_path = Path(path).expanduser()
        note = normalize_note_text(selected_text)
        prompt = normalize_note_text(question)
        if not note and content_items is None:
            raise DocumentError("选中的文本不能为空。")
        try:
            level = max(0, min(6, int(title_level)))
        except (TypeError, ValueError):
            level = 0
        # 标签先拼到标题文字上，再统一加 `#` 前缀：只打了标签没写标题时，标题行
        # 也该是 `### #标签`，而不是光秃秃一个没有层级的行。
        heading = [prompt] if prompt else []
        for tag in DocumentWriter._heading_tags(heading_tags):
            heading.append(f"#{tag}")
        if heading and level:
            prompt = f"{'#' * level} " + " ".join(heading)
        elif heading:
            prompt = " ".join(heading)
        content_blocks: list[str] = []
        if content_items is not None:
            for kind, value in content_items:
                if kind == "text":
                    markdown_text = (
                        str(value)
                        .replace("\x00", " ")
                        .replace("\r\n", "\n")
                        .replace("\r", "\n")
                        .strip()
                    )
                    if markdown_text:
                        content_blocks.append(markdown_text)
                elif kind == "image":
                    source_path, source_data = value
                    image_name = DocumentWriter._store_image(
                        output_path, source_path, source_data, image_dir=image_dir
                    )
                    content_blocks.append(f"![图片]({image_name})")
                elif kind == "source":
                    if isinstance(value, dict):
                        source_text = normalize_note_text(str(value.get("text", "")))
                    else:
                        source_text = normalize_note_text(str(value))
                    if source_text:
                        content_blocks.append(source_text)
        else:
            image_sources = image_items
            if image_sources is None and (image_path or image_data is not None):
                image_sources = [(image_path, image_data)]
            image_markdown = []
            for source_path, source_data in image_sources or []:
                image_name = DocumentWriter._store_image(
                    output_path, source_path, source_data, image_dir=image_dir
                )
                image_markdown.append(f"![图片]({image_name})")
            content_blocks = [part for part in (note, *image_markdown) if part]
        blocks = [part for part in (prompt, *content_blocks) if part]
        if not blocks:
            raise DocumentError("问题和选中文本不能同时为空。")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        separator = ""
        if output_path.exists() and output_path.stat().st_size:
            with output_path.open("rb") as existing:
                existing.seek(max(0, output_path.stat().st_size - 2))
                ending = existing.read()
            separator = "" if ending.endswith(b"\n\n") else ("\n" if ending.endswith(b"\n") else "\n\n")
        try:
            with output_path.open("a", encoding="utf-8-sig", newline="") as handle:
                if separator:
                    handle.write(separator)
                handle.write("\n\n".join(blocks))
                handle.write("\n\n")
        except PermissionError as exc:
            raise DocumentError("文档正在被其他程序占用，请关闭文档后重试。") from exc
        except OSError as exc:
            raise DocumentError(f"写入文档失败：{exc}") from exc

    @staticmethod
    def _store_image(
        output_path: Path,
        image_path: Path | str | None,
        image_data,
        image_dir: Path | str | None = None,
    ) -> str:
        source_path = Path(image_path).expanduser() if image_path else None
        if source_path is not None:
            if not source_path.exists() or not source_path.is_file():
                raise DocumentError("图片文件不存在，无法写入。")
            suffix = source_path.suffix.lower() or ".png"
            if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
                raise DocumentError("仅支持 PNG、JPG、GIF、BMP 或 WEBP 图片。")
        else:
            suffix = ".png"
        destination_dir = (
            Path(image_dir).expanduser()
            if image_dir is not None
            else output_path.parent / f"{output_path.stem}-images"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        image_number = int(time.time() * 1000)
        destination = destination_dir / f"image-{image_number}{suffix}"
        while destination.exists():
            image_number += 1
            destination = destination_dir / f"image-{image_number}{suffix}"
        if source_path is not None:
            try:
                shutil.copy2(source_path, destination)
            except OSError as exc:
                raise DocumentError(f"复制图片失败：{exc}") from exc
        else:
            if image_data is None or not hasattr(image_data, "save"):
                raise DocumentError("剪贴板中没有可用图片。")
            try:
                image_data.save(destination, format="PNG")
            except OSError as exc:
                raise DocumentError(f"保存剪贴板图片失败：{exc}") from exc
        # 链接按实际写入的 Markdown 所在目录计算；图片在正文记录目录时，
        # 索引与正文引用的相对路径不同，不能只写 destination.name。
        return os.path.relpath(destination, output_path.parent).replace("\\", "/")

    @staticmethod
    def _append_json(
        path: Path,
        items: list[dict[str, str]],
        sentence: str,
        added_at: int | None = None,
    ) -> None:
        records: list[dict[str, str | int]] = []
        if path.exists() and path.stat().st_size:
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DocumentError("JSON 单词库格式损坏，无法继续写入。") from exc
            if not isinstance(loaded, list) or any(not isinstance(row, dict) for row in loaded):
                raise DocumentError("JSON 单词库必须是对象数组。")
            records = [dict(row) for row in loaded]
        timestamp = int(added_at or time.time())
        for item in items:
            records.append(
                {
                    "word": _clean_cell(item.get("word", "")),
                    "seen_form": _clean_cell(item.get("seen_form", "")),
                    "phonetic": _clean_cell(item.get("phonetic", "")),
                    "part_of_speech": _clean_cell(item.get("part_of_speech", "")),
                    "meaning_zh": _clean_cell(item.get("meaning_zh", "")),
                    "usage_zh": _clean_cell(item.get("usage_zh", "")),
                    "example": _clean_cell(item.get("example", "")),
                    "example_zh": _clean_cell(item.get("example_zh", "")),
                    "source_sentence": _clean_cell(sentence),
                    "added_at": timestamp,
                    "added_at_text": time.strftime(
                        "%Y-%m-%d %H:%M", time.localtime(timestamp)
                    ),
                }
            )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.stem + "-", suffix=".json", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(
                json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _append_three_line_blocks(path: Path, rows: list[dict[str, str]]) -> None:
        separator = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as existing:
                existing.seek(max(0, path.stat().st_size - 2))
                ending = existing.read()
            separator = "" if ending.endswith(b"\n\n") else ("\n" if ending.endswith(b"\n") else "\n\n")
        with path.open("a", encoding="utf-8-sig", newline="") as handle:
            if separator:
                handle.write(separator)
            for row in rows:
                first_line = "  ".join(
                    value for value in (row["单词"], row["音标"], row["词性"]) if value
                )
                example = row["例句"]
                if row["例句翻译"]:
                    example = f"{example}（{row['例句翻译']}）" if example else row["例句翻译"]
                handle.write(f"{first_line}\n{row['中文释义']}\n{example}\n\n")

    @classmethod
    def _append_docx(cls, path: Path, rows: list[dict[str, str]]) -> None:
        if not path.exists():
            cls._create_docx(path)

        with zipfile.ZipFile(path, "r") as source:
            names = source.namelist()
            if "word/document.xml" not in names:
                raise DocumentError("该 .docx 文件缺少 word/document.xml。")
            files = {name: source.read(name) for name in names}

        document = ET.fromstring(files["word/document.xml"])
        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml_namespace = "http://www.w3.org/XML/1998/namespace"
        ET.register_namespace("w", namespace)
        body = document.find(f"{{{namespace}}}body")
        if body is None:
            raise DocumentError("无法读取 Word 文档正文。")
        section = body.find(f"{{{namespace}}}sectPr")
        insert_index = list(body).index(section) if section is not None else len(body)

        def paragraph(parts: list[tuple[str, bool]]) -> ET.Element:
            node = ET.Element(f"{{{namespace}}}p")
            for text, bold in parts:
                run = ET.SubElement(node, f"{{{namespace}}}r")
                if bold:
                    properties = ET.SubElement(run, f"{{{namespace}}}rPr")
                    ET.SubElement(properties, f"{{{namespace}}}b")
                text_node = ET.SubElement(run, f"{{{namespace}}}t")
                text_node.set(f"{{{xml_namespace}}}space", "preserve")
                text_node.text = text
            return node

        for row in rows:
            blocks = [
                paragraph(
                    [
                        (row["单词"], True),
                        (f"  {row['音标']}  {row['词性']}", False),
                    ]
                ),
                paragraph([(row["中文释义"], False)]),
                paragraph(
                    [
                        (row["例句"], False),
                        (f"（{row['例句翻译']}）" if row["例句翻译"] else "", False),
                    ]
                ),
                paragraph([("", False)]),
            ]
            for block in blocks:
                body.insert(insert_index, block)
                insert_index += 1

        files["word/document.xml"] = ET.tostring(
            document, encoding="utf-8", xml_declaration=True
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=path.stem + "-", suffix=".docx", dir=path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as target:
                for name, content in files.items():
                    target.writestr(name, content)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @staticmethod
    def _create_docx(path: Path) -> None:
        content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""
        root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
        document_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
        styles = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
</w:styles>"""
        document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", root_rels)
            archive.writestr("word/_rels/document.xml.rels", document_rels)
            archive.writestr("word/styles.xml", styles)
            archive.writestr("word/document.xml", document)


def normalize_index_word(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).casefold().replace("’", "'")
    return re.sub(r"[^a-z0-9'-]+", "", normalized)


def word_bucket(normalized_word: str) -> str:
    first = normalized_word[:1]
    return first if "a" <= first <= "z" else "#"


@dataclass(frozen=True)
class DictionaryStatus:
    source_path: Path
    entry_count: int
    rebuilt: bool


@dataclass(frozen=True)
class DictionaryEntry:
    word: str
    phonetic: str
    part_of_speech: str
    translation: str
    definition: str

    @property
    def is_complete(self) -> bool:
        return bool(self.phonetic and self.part_of_speech and self.translation)


class LocalDictionary:
    INDEX_SCHEMA_VERSION = "2"
    REQUIRED_COLUMNS = {"word", "phonetic", "definition", "translation", "pos"}
    POS_PATTERN = re.compile(
        r"(?:^|；)\s*(abbr|adj|adv|art|aux|conj|int|interj|n|num|prep|pron|v|vi|vt|a)\.",
        re.IGNORECASE,
    )

    def __init__(
        self,
        source_path: Path | str,
        index_path: Path | str = DICTIONARY_INDEX_PATH,
    ):
        self.source_path = Path(source_path).expanduser()
        self.index_path = Path(index_path).expanduser()
        self._lock = threading.Lock()

    @staticmethod
    def _clean_dictionary_text(value: str) -> str:
        value = str(value or "").replace("\\r", " ").replace("\\n", "；")
        value = value.replace("\r", " ").replace("\n", "；")
        return re.sub(r"\s+", " ", value).strip(" ；")

    @classmethod
    def _part_of_speech(cls, value: str, translation: str, definition: str) -> str:
        direct = cls._clean_dictionary_text(value)
        if direct:
            return direct
        candidates: list[str] = []
        for text in (translation, definition):
            cleaned = cls._clean_dictionary_text(text)
            for match in cls.POS_PATTERN.finditer(cleaned):
                pos = match.group(1).lower() + "."
                if pos not in candidates:
                    candidates.append(pos)
        return " / ".join(candidates)

    @classmethod
    def _exchange_mappings(
        cls, word: str, exchange: str
    ) -> list[tuple[str, str, str, int, int]]:
        fields: dict[str, list[str]] = {}
        for part in str(exchange or "").split("/"):
            code, separator, value = part.partition(":")
            if not separator or not value.strip():
                continue
            values = [item.strip() for item in value.split(",") if item.strip()]
            if values:
                fields.setdefault(code.strip().lower(), []).extend(values)

        word_key = normalize_index_word(word)
        base_values = fields.get("0", [])
        lemma = normalize_index_word(base_values[0]) if base_values else word_key
        if not word_key or not lemma:
            return []

        mappings: list[tuple[str, str, str, int, int]] = []
        if base_values:
            mappings.append((word_key, lemma, "exchange:0", 3, 0))
        for code in ("p", "d", "i", "3", "r", "t", "s"):
            for form in fields.get(code, []):
                form_key = normalize_index_word(form)
                if form_key and form_key != lemma:
                    mappings.append((form_key, lemma, f"exchange:{code}", 2, 0))
        return mappings

    def _source_signature(self) -> dict[str, str]:
        path = self.source_path
        if not path.exists() or not path.is_file():
            raise DictionaryError(f"词典文件不存在：{path}")
        try:
            stat = path.stat()
        except OSError as exc:
            raise DictionaryError(f"无法读取词典文件：{exc}") from exc
        if stat.st_size <= 0:
            raise DictionaryError("词典文件为空，请重新选择或下载完整词典。")
        try:
            with path.open("r", encoding="utf-8-sig", newline="", errors="replace") as handle:
                header = next(csv.reader(handle), [])
        except (OSError, csv.Error) as exc:
            raise DictionaryError(f"无法解析词典 CSV：{exc}") from exc
        missing = self.REQUIRED_COLUMNS.difference(header)
        if missing:
            raise DictionaryError("词典 CSV 缺少字段：" + "、".join(sorted(missing)))
        return {
            "index_schema_version": self.INDEX_SCHEMA_VERSION,
            "source_path": str(path.resolve()),
            "source_size": str(stat.st_size),
            "source_mtime_ns": str(stat.st_mtime_ns),
        }

    def _current_entry_count(self, signature: dict[str, str]) -> int | None:
        if not self.index_path.exists():
            return None
        try:
            with closing(sqlite3.connect(self.index_path)) as connection:
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                if any(metadata.get(key) != value for key, value in signature.items()):
                    return None
                connection.execute("SELECT 1 FROM word_forms LIMIT 1").fetchone()
                row = connection.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()
                return int(row[0]) if row else 0
        except sqlite3.Error:
            return None

    def check(self) -> DictionaryStatus:
        with self._lock:
            signature = self._source_signature()
            count = self._current_entry_count(signature)
            if count is not None:
                return DictionaryStatus(self.source_path, count, False)
            count = self._rebuild_index(signature)
            return DictionaryStatus(self.source_path, count, True)

    def _rebuild_index(self, signature: dict[str, str]) -> int:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=self.index_path.stem + "-", suffix=".sqlite3", dir=self.index_path.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as connection:
                connection.execute("PRAGMA journal_mode=OFF")
                connection.execute("PRAGMA synchronous=OFF")
                connection.execute("PRAGMA temp_store=MEMORY")
                connection.execute(
                    "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                connection.execute(
                    """
                    CREATE TABLE dictionary_entries (
                        normalized_word TEXT PRIMARY KEY,
                        display_word TEXT NOT NULL,
                        phonetic TEXT NOT NULL,
                        part_of_speech TEXT NOT NULL,
                        translation TEXT NOT NULL,
                        definition TEXT NOT NULL
                    ) WITHOUT ROWID
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE word_forms (
                        form TEXT PRIMARY KEY,
                        lemma TEXT NOT NULL,
                        source TEXT NOT NULL,
                        priority INTEGER NOT NULL,
                        ambiguous INTEGER NOT NULL DEFAULT 0
                    ) WITHOUT ROWID
                    """
                )
                insert_sql = """
                    INSERT OR REPLACE INTO dictionary_entries (
                        normalized_word, display_word, phonetic,
                        part_of_speech, translation, definition
                    ) VALUES (?, ?, ?, ?, ?, ?)
                """
                form_insert_sql = """
                    INSERT INTO word_forms (form, lemma, source, priority, ambiguous)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(form) DO UPDATE SET
                        lemma = CASE
                            WHEN excluded.priority > word_forms.priority
                            THEN excluded.lemma ELSE word_forms.lemma END,
                        source = CASE
                            WHEN excluded.priority > word_forms.priority
                            THEN excluded.source ELSE word_forms.source END,
                        priority = MAX(word_forms.priority, excluded.priority),
                        ambiguous = CASE
                            WHEN excluded.lemma = word_forms.lemma
                            THEN word_forms.ambiguous
                            WHEN excluded.priority > word_forms.priority
                            THEN 0
                            WHEN excluded.priority = word_forms.priority
                            THEN 1
                            ELSE word_forms.ambiguous END
                """
                batch: list[tuple[str, str, str, str, str, str]] = []
                form_batch: list[tuple[str, str, str, int, int]] = []
                try:
                    with self.source_path.open(
                        "r", encoding="utf-8-sig", newline="", errors="replace"
                    ) as handle:
                        for row in csv.DictReader(handle):
                            word = self._clean_dictionary_text(row.get("word", ""))
                            normalized = normalize_index_word(word)
                            if not normalized:
                                continue
                            phonetic = self._clean_dictionary_text(row.get("phonetic", ""))
                            translation = self._clean_dictionary_text(row.get("translation", ""))
                            definition = self._clean_dictionary_text(row.get("definition", ""))
                            part_of_speech = self._part_of_speech(
                                row.get("pos", ""), translation, definition
                            )
                            batch.append(
                                (
                                    normalized,
                                    word,
                                    phonetic,
                                    part_of_speech,
                                    translation,
                                    definition,
                                )
                            )
                            form_batch.extend(
                                self._exchange_mappings(word, row.get("exchange", ""))
                            )
                            if len(batch) >= 5000:
                                connection.executemany(insert_sql, batch)
                                batch.clear()
                                if form_batch:
                                    connection.executemany(form_insert_sql, form_batch)
                                    form_batch.clear()
                except (OSError, csv.Error) as exc:
                    raise DictionaryError(f"导入词典失败：{exc}") from exc
                if batch:
                    connection.executemany(insert_sql, batch)
                if form_batch:
                    connection.executemany(form_insert_sql, form_batch)
                connection.executemany(
                    "INSERT INTO metadata (key, value) VALUES (?, ?)", signature.items()
                )
                row = connection.execute("SELECT COUNT(*) FROM dictionary_entries").fetchone()
                count = int(row[0]) if row else 0
                connection.commit()
            if count <= 0:
                raise DictionaryError("词典中没有可用词条。")
            os.replace(temporary, self.index_path)
            return count
        except sqlite3.Error as exc:
            raise DictionaryError(f"建立词典索引失败：{exc}") from exc
        finally:
            if temporary.exists():
                temporary.unlink()

    def lemmatize_many(self, words: list[str] | tuple[str, ...]) -> dict[str, str]:
        normalized_words: list[str] = []
        seen_words: set[str] = set()
        for word in words:
            normalized = normalize_index_word(word)
            if normalized and normalized not in seen_words:
                seen_words.add(normalized)
                normalized_words.append(normalized)
        if not normalized_words:
            return {}
        self.check()

        mappings: dict[str, tuple[str, bool]] = {}
        exact_words: set[str] = set()
        candidate_by_word: dict[str, list[str]] = {}
        try:
            with closing(sqlite3.connect(self.index_path)) as connection:
                for start in range(0, len(normalized_words), 500):
                    chunk = normalized_words[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    for form, lemma, ambiguous in connection.execute(
                        "SELECT form, lemma, ambiguous FROM word_forms "
                        f"WHERE form IN ({placeholders})",
                        chunk,
                    ):
                        mappings[form] = (lemma, bool(ambiguous))
                    exact_words.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT normalized_word FROM dictionary_entries "
                            f"WHERE normalized_word IN ({placeholders})",
                            chunk,
                        )
                    )

                decisions: dict[str, str] = {}
                for word in normalized_words:
                    mapped = mappings.get(word)
                    if mapped:
                        decisions[word] = word if mapped[1] else mapped[0]
                    elif word in CONTRACTION_LEMMAS:
                        decisions[word] = CONTRACTION_LEMMAS[word]
                    elif word in IRREGULAR_LEMMAS:
                        decisions[word] = IRREGULAR_LEMMAS[word]
                    elif word in COMPARATIVE_LEMMAS:
                        decisions[word] = COMPARATIVE_LEMMAS[word]
                    elif word in exact_words:
                        decisions[word] = "I" if word == "i" else word
                    else:
                        candidate_by_word[word] = lemma_candidates_offline(word)

                candidate_words = sorted(
                    {candidate for values in candidate_by_word.values() for candidate in values}
                )
                valid_candidates: set[str] = set()
                for start in range(0, len(candidate_words), 500):
                    chunk = candidate_words[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    valid_candidates.update(
                        row[0]
                        for row in connection.execute(
                            "SELECT normalized_word FROM dictionary_entries "
                            f"WHERE normalized_word IN ({placeholders})",
                            chunk,
                        )
                    )
        except sqlite3.Error as exc:
            raise DictionaryError(f"查询本地词形索引失败：{exc}") from exc

        for word, candidates in candidate_by_word.items():
            valid = [candidate for candidate in candidates if candidate in valid_candidates]
            decisions[word] = valid[0] if len(set(valid)) == 1 else word
        return decisions

    def lookup_many(self, words: list[str] | tuple[str, ...]) -> dict[str, DictionaryEntry]:
        self.check()
        normalized_words = []
        seen = set()
        for word in words:
            normalized = normalize_index_word(word)
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_words.append(normalized)
        if not normalized_words:
            return {}
        rows = []
        try:
            with closing(sqlite3.connect(self.index_path)) as connection:
                for start in range(0, len(normalized_words), 500):
                    chunk = normalized_words[start : start + 500]
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        connection.execute(
                            "SELECT normalized_word, display_word, phonetic, part_of_speech, "
                            "translation, definition FROM dictionary_entries "
                            f"WHERE normalized_word IN ({placeholders})",
                            chunk,
                        )
                    )
        except sqlite3.Error as exc:
            raise DictionaryError(f"查询本地词典失败：{exc}") from exc
        return {
            row[0]: DictionaryEntry(
                word=row[1],
                phonetic=row[2],
                part_of_speech=row[3],
                translation=row[4],
                definition=row[5],
            )
            for row in rows
        }

    @staticmethod
    def to_vocabulary_item(
        entry: DictionaryEntry,
        prepared: dict[str, str],
        sentence: str,
    ) -> dict[str, str]:
        phonetic = entry.phonetic.strip()
        if phonetic and not (
            (phonetic.startswith("/") and phonetic.endswith("/"))
            or (phonetic.startswith("[") and phonetic.endswith("]"))
        ):
            phonetic = f"/{phonetic}/"
        word = str(prepared.get("word", entry.word)).strip()
        return {
            "word": word,
            "seen_form": str(prepared.get("seen_form", word)).strip(),
            "phonetic": phonetic,
            "part_of_speech": entry.part_of_speech,
            "meaning_zh": entry.translation,
            "usage_zh": "本地词典释义；结合划选句理解",
            "example": sentence,
            "example_zh": "",
        }


class HybridVocabularyService:
    def __init__(self, local_dictionary: LocalDictionary, api_service: VocabularyService):
        self.local_dictionary = local_dictionary
        self.api_service = api_service

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        local_dictionary: LocalDictionary | None = None,
    ) -> "HybridVocabularyService":
        dictionary = local_dictionary or LocalDictionary(resolve_dictionary_path(config))
        return cls(dictionary, VocabularyService.from_config(config))

    def lookup(
        self,
        sentence: str,
        prepared_words: list[dict[str, str]] | None = None,
    ) -> list[dict[str, str]]:
        if prepared_words is None:
            try:
                lemma_map = self.local_dictionary.lemmatize_many(
                    extract_words_offline(sentence)
                )
            except DictionaryError:
                lemma_map = None
            prepared = prepare_vocabulary_words(sentence, lemma_map=lemma_map)
        else:
            prepared = prepared_words
        prepared = [
            item
            for item in prepared
            if isinstance(item, dict)
            and str(item.get("word", "")).strip()
            and str(item.get("seen_form", "")).strip()
        ]
        if not prepared:
            raise LookupError("句子中没有需要翻译的英文单词。")

        dictionary_error: DictionaryError | None = None
        try:
            local_entries = self.local_dictionary.lookup_many(
                [str(item["word"]) for item in prepared]
            )
        except DictionaryError as exc:
            dictionary_error = exc
            local_entries = {}

        merged: dict[str, dict[str, str]] = {}
        missing: list[dict[str, str]] = []
        for item in prepared:
            key = normalize_index_word(str(item["word"]))
            entry = local_entries.get(key)
            if entry and entry.is_complete:
                merged[key] = self.local_dictionary.to_vocabulary_item(entry, item, sentence)
            else:
                missing.append(item)

        offline_only = bool(missing and not getattr(self.api_service, "api_key", ""))
        if offline_only:
            for item in missing:
                key = normalize_index_word(str(item["word"]))
                entry = local_entries.get(key)
                if entry and entry.translation:
                    merged[key] = self.local_dictionary.to_vocabulary_item(
                        entry, item, sentence
                    )
            missing = []

        if missing:
            try:
                api_items = self.api_service.lookup(sentence, missing)
            except ConfigError as exc:
                if dictionary_error:
                    raise ConfigError(f"{dictionary_error}\n{exc}") from exc
                missing_preview = "、".join(str(item["word"]) for item in missing[:8])
                raise ConfigError(f"本地词典缺少：{missing_preview}\n{exc}") from exc
            for item in api_items:
                merged[normalize_index_word(item["word"])] = item

        result = []
        for item in prepared:
            key = normalize_index_word(str(item["word"]))
            resolved = merged.get(key)
            if not resolved:
                if offline_only:
                    continue
                raise LookupError(f"未能生成词条：{item['word']}")
            result.append(resolved)
        if not result:
            raise LookupError("本地词典未找到可用词条；可配置 API 补充生词。")
        return result


class QuestionDocumentWriter:
    """Persistence boundary for the independent question-recording mode."""

    append = staticmethod(DocumentWriter.append_question)
    append_record = staticmethod(DocumentWriter.append_question_record)
    append_compact_question = staticmethod(DocumentWriter.append_compact_question)


class DocumentWordExtractor:
    WORD_PATTERN = re.compile(r"^([A-Za-z][A-Za-z'-]*)\b")

    @classmethod
    def extract(cls, path: Path) -> list[str]:
        if not path.exists() or path.stat().st_size == 0:
            return []
        if path.suffix.lower() == ".docx":
            return cls._extract_docx(path)
        if path.suffix.lower() == ".json":
            return cls._extract_json(path)
        return cls._extract_text(path)

    @classmethod
    def _extract_json(cls, path: Path) -> list[str]:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(loaded, list):
            return []
        return cls._unique(
            [str(row.get("word", "")) for row in loaded if isinstance(row, dict)]
        )

    @classmethod
    def _extract_text(cls, path: Path) -> list[str]:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        words: list[str] = []
        plain_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("|") and stripped.endswith("|"):
                cells = [cell.strip() for cell in stripped[1:-1].split("|")]
                if cells and cls._valid_word(cells[0]):
                    words.append(cells[0])
                continue
            plain_lines.append(line)

        for block in re.split(r"\n\s*\n", "\n".join(plain_lines)):
            first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
            match = cls.WORD_PATTERN.match(first_line)
            if match and cls._valid_word(match.group(1)):
                words.append(match.group(1))
        return cls._unique(words)

    @classmethod
    def _extract_docx(cls, path: Path) -> list[str]:
        try:
            with zipfile.ZipFile(path) as archive:
                xml = archive.read("word/document.xml")
        except (OSError, KeyError, zipfile.BadZipFile):
            return []
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        root = ET.fromstring(xml)
        words = []
        for paragraph in root.findall(".//w:p", namespace):
            text = "".join(paragraph.itertext()).strip()
            match = cls.WORD_PATTERN.match(text)
            if match and ("  " in text or "/" in text):
                words.append(match.group(1))
        return cls._unique(words)

    @staticmethod
    def _valid_word(value: str) -> bool:
        normalized = normalize_index_word(value)
        return bool(normalized and normalized not in {"word", "words"})

    @staticmethod
    def _unique(words: list[str]) -> list[str]:
        result = []
        seen = set()
        for word in words:
            normalized = normalize_index_word(word)
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(word)
        return result


@dataclass(frozen=True)
class WriteResult:
    added: tuple[str, ...]
    duplicates: tuple[str, ...]


@dataclass(frozen=True, eq=False)
class IgnoredWord:
    display_word: str
    enabled: bool
    expires_at: int | None

    def __iter__(self):
        # Keep the old ``for display, enabled in rows`` shape usable.
        yield self.display_word
        yield self.enabled

    def __eq__(self, other):
        if isinstance(other, IgnoredWord):
            return (
                self.display_word,
                self.enabled,
                self.expires_at,
            ) == (other.display_word, other.enabled, other.expires_at)
        if isinstance(other, tuple):
            return (self.display_word, self.enabled) == other or (
                self.display_word,
                self.enabled,
                self.expires_at,
            ) == other
        return NotImplemented

    def __hash__(self):
        return hash((self.display_word, self.enabled, self.expires_at))


def ignore_status_text(enabled: bool, expires_at: int | None) -> str:
    if not enabled:
        return "允许翻译"
    if expires_at is None:
        return "永久免翻译"
    return f"免翻译至 {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at))}"


@dataclass(frozen=True)
class VocabularyEntry:
    normalized_word: str
    display_word: str
    added_at: int
    document_count: int


class VocabularyIndex:
    def __init__(self, database_path: Path = INDEX_PATH):
        self.database_path = Path(database_path)

    def append_unique(
        self,
        document_path: Path | str,
        items: list[dict[str, str]],
        sentence: str,
    ) -> WriteResult:
        path = Path(document_path).expanduser().resolve()
        document_key = os.path.normcase(str(path))
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            self._synchronize_document(connection, document_key, path)

            normalized_items: list[tuple[dict[str, str], str, str]] = []
            batch_seen: set[str] = set()
            duplicates: list[str] = []
            for item in items:
                display_word = str(item.get("word", "")).strip()
                normalized = normalize_index_word(display_word)
                if not normalized or normalized in batch_seen:
                    duplicates.append(display_word)
                    continue
                batch_seen.add(normalized)
                normalized_items.append((item, normalized, word_bucket(normalized)))

            existing = self._find_existing(connection, document_key, normalized_items)
            new_items = []
            for item, normalized, bucket in normalized_items:
                if normalized in existing:
                    duplicates.append(str(item.get("word", "")).strip())
                else:
                    new_items.append((item, normalized, bucket))

            if new_items:
                added_at = int(time.time())
                DocumentWriter.append(
                    path,
                    [item for item, _, _ in new_items],
                    sentence,
                    added_at,
                )
                connection.executemany(
                    "INSERT OR IGNORE INTO words "
                    "(document_path, bucket, normalized_word, display_word, added_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            document_key,
                            bucket,
                            normalized,
                            str(item.get("word", "")).strip(),
                            added_at,
                        )
                        for item, normalized, bucket in new_items
                    ],
                )
                self._update_document_metadata(connection, document_key, path)

            return WriteResult(
                tuple(str(item.get("word", "")).strip() for item, _, _ in new_items),
                tuple(word for word in duplicates if word),
            )

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS documents ("
            "document_path TEXT PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS words ("
            "document_path TEXT NOT NULL, bucket TEXT NOT NULL, normalized_word TEXT NOT NULL, "
            "display_word TEXT NOT NULL, added_at INTEGER, "
            "PRIMARY KEY (document_path, normalized_word))"
        )
        word_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(words)")
        }
        if "added_at" not in word_columns:
            connection.execute("ALTER TABLE words ADD COLUMN added_at INTEGER")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS words_by_initial "
            "ON words (document_path, bucket, normalized_word)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS words_by_added_at "
            "ON words (added_at, normalized_word)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS ignored_words ("
            "normalized_word TEXT PRIMARY KEY, display_word TEXT NOT NULL, "
            "enabled INTEGER NOT NULL DEFAULT 1, updated_at INTEGER NOT NULL, "
            "expires_at INTEGER)"
        )
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(ignored_words)")
        }
        if "expires_at" not in columns:
            connection.execute("ALTER TABLE ignored_words ADD COLUMN expires_at INTEGER")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ignored_words_by_enabled "
            "ON ignored_words (enabled, normalized_word)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ignored_words_by_expiry "
            "ON ignored_words (enabled, expires_at, normalized_word)"
        )

    @staticmethod
    def _validate_ignored_word(value: str) -> tuple[str, str]:
        display = str(value).strip().replace("’", "'")
        if not re.fullmatch(r"[A-Za-z]+(?:['-][A-Za-z]+)*", display):
            raise ValueError("请输入一个英文单词，可包含连字符或撇号。")
        return display, normalize_index_word(display)

    @staticmethod
    def _disable_expired(connection: sqlite3.Connection, now: int) -> None:
        connection.execute(
            "UPDATE ignored_words SET enabled = 0, expires_at = NULL, updated_at = ? "
            "WHERE enabled = 1 AND expires_at IS NOT NULL AND expires_at <= ?",
            (time.time_ns(), now),
        )

    def list_ignored_words(self) -> list[IgnoredWord]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            self._disable_expired(connection, int(time.time()))
            rows = connection.execute(
                "SELECT display_word, enabled, expires_at "
                "FROM ignored_words ORDER BY normalized_word"
            ).fetchall()
        return [
            IgnoredWord(str(display), bool(enabled), int(expires_at) if expires_at else None)
            for display, enabled, expires_at in rows
        ]

    def enabled_ignored_words(self) -> set[str]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        now = int(time.time())
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            self._disable_expired(connection, now)
            rows = connection.execute(
                "SELECT normalized_word FROM ignored_words "
                "WHERE enabled = 1 AND (expires_at IS NULL OR expires_at > ?)",
                (now,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def set_ignored_word(
        self, value: str, enabled: bool = True, expires_at: int | None = None
    ) -> str:
        display, normalized = self._validate_ignored_word(value)
        if not enabled:
            expires_at = None
        elif expires_at is not None and expires_at <= int(time.time()):
            raise ValueError("免翻译到期时间必须晚于当前时间。")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            connection.execute(
                "INSERT INTO ignored_words "
                "(normalized_word, display_word, enabled, updated_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(normalized_word) DO UPDATE SET "
                "display_word=excluded.display_word, enabled=excluded.enabled, "
                "updated_at=excluded.updated_at, expires_at=excluded.expires_at",
                (normalized, display, int(enabled), time.time_ns(), expires_at),
            )
        return normalized

    def set_ignored_word_for(self, value: str, seconds: int | None) -> tuple[str, int | None]:
        if seconds is not None and seconds <= 0:
            raise ValueError("免翻译时长必须大于 0。")
        expires_at = None if seconds is None else int(time.time()) + int(seconds)
        normalized = self.set_ignored_word(value, True, expires_at)
        return normalized, expires_at

    def remove_ignored_word(self, value: str) -> None:
        _, normalized = self._validate_ignored_word(value)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection, connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            connection.execute("DELETE FROM ignored_words WHERE normalized_word = ?", (normalized,))

    def list_vocabulary_entries(self, search: str = "") -> list[VocabularyEntry]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        query = str(search).strip().casefold()
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            self._create_schema(connection)
            rows = connection.execute(
                "SELECT w.normalized_word, MIN(w.display_word), "
                "MIN(COALESCE(w.added_at, CAST(d.mtime_ns / 1000000000 AS INTEGER), 0)), "
                "COUNT(DISTINCT w.document_path) "
                "FROM words AS w LEFT JOIN documents AS d "
                "ON d.document_path = w.document_path "
                "GROUP BY w.normalized_word ORDER BY MIN(COALESCE(w.added_at, "
                "CAST(d.mtime_ns / 1000000000 AS INTEGER), 0)) DESC, w.normalized_word"
            ).fetchall()
        result = [
            VocabularyEntry(str(normalized), str(display), int(added_at or 0), int(document_count))
            for normalized, display, added_at, document_count in rows
        ]
        if query:
            result = [entry for entry in result if query in entry.display_word.casefold()]
        return result

    def _synchronize_document(
        self, connection: sqlite3.Connection, document_key: str, path: Path
    ) -> None:
        size, mtime_ns = self._metadata(path)
        row = connection.execute(
            "SELECT size, mtime_ns FROM documents WHERE document_path = ?", (document_key,)
        ).fetchone()
        if row == (size, mtime_ns):
            return
        words = DocumentWordExtractor.extract(path)
        previous_times = {
            str(normalized): int(added_at)
            for normalized, added_at in connection.execute(
                "SELECT normalized_word, added_at FROM words WHERE document_path = ?",
                (document_key,),
            ).fetchall()
            if added_at is not None
        }
        connection.execute("DELETE FROM words WHERE document_path = ?", (document_key,))
        fallback_added_at = int(path.stat().st_mtime) if path.exists() else int(time.time())
        connection.executemany(
            "INSERT OR IGNORE INTO words "
            "(document_path, bucket, normalized_word, display_word, added_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (
                    document_key,
                    word_bucket(normalize_index_word(word)),
                    normalize_index_word(word),
                    word,
                    previous_times.get(normalize_index_word(word), fallback_added_at),
                )
                for word in words
            ],
        )
        self._update_document_metadata(connection, document_key, path)

    @staticmethod
    def _find_existing(
        connection: sqlite3.Connection,
        document_key: str,
        normalized_items: list[tuple[dict[str, str], str, str]],
    ) -> set[str]:
        existing: set[str] = set()
        by_bucket: dict[str, list[str]] = {}
        for _, normalized, bucket in normalized_items:
            by_bucket.setdefault(bucket, []).append(normalized)
        for bucket, words in by_bucket.items():
            placeholders = ",".join("?" for _ in words)
            rows = connection.execute(
                f"SELECT normalized_word FROM words WHERE document_path = ? AND bucket = ? "
                f"AND normalized_word IN ({placeholders})",
                [document_key, bucket, *words],
            )
            existing.update(row[0] for row in rows)
        return existing

    @staticmethod
    def _metadata(path: Path) -> tuple[int, int]:
        if not path.exists():
            return 0, 0
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns

    @classmethod
    def _update_document_metadata(
        cls, connection: sqlite3.Connection, document_key: str, path: Path
    ) -> None:
        size, mtime_ns = cls._metadata(path)
        connection.execute(
            "INSERT INTO documents (document_path, size, mtime_ns) VALUES (?, ?, ?) "
            "ON CONFLICT(document_path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns",
            (document_key, size, mtime_ns),
        )


if os.name == "nt":
    ULONG_PTR = wintypes.WPARAM
    LRESULT = ctypes.c_ssize_t
    HHOOK = ctypes.c_void_p

    class POINT(ctypes.Structure):
        _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


class WindowsSelectionCapture:
    WH_MOUSE_LL = 14
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    WM_QUIT = 0x0012
    WM_HOTKEY = 0x0312
    HOTKEY_ID_STATS = 0x574
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_NOREPEAT = 0x4000
    VK_CONTROL = 0x11
    VK_C = 0x43
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    CF_UNICODETEXT = 13

    def __init__(self, event_queue: queue.Queue, drag_threshold: int = 8):
        if os.name != "nt":
            raise AppError("划词监听仅支持 Windows。")
        self.event_queue = event_queue
        self.drag_threshold = drag_threshold
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._hook = None
        self._callback = None
        self._start_point: tuple[int, int] | None = None
        self._running = threading.Event()
        self.last_pointer_down = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="mouse-hook", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread_id:
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, self.WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.SetWindowsHookExW.restype = HHOOK
        user32.CallNextHookEx.restype = LRESULT
        self._thread_id = kernel32.GetCurrentThreadId()
        kernel32.GetTickCount64.restype = ctypes.c_ulonglong

        def callback(code: int, message: int, data: int) -> int:
            if code >= 0:
                info = ctypes.cast(data, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                point = (int(info.pt.x), int(info.pt.y))
                if message == self.WM_LBUTTONDOWN:
                    self._start_point = point
                    os_to_hook = ((kernel32.GetTickCount64() & 0xffffffff) - int(info.time)) & 0xffffffff
                    self.last_pointer_down = (point, time.perf_counter_ns(),
                                              time.time_ns() // 1_000_000, os_to_hook)
                    # 悬浮入口要「点别处就消失」，所以按下也得入队一条。低层钩子连
                    # 落在我们自己窗口上的点击也看得见，落在入口矩形内的由 Tk 侧排除。
                    self.event_queue.put(("pointer_down", point))
                elif message == self.WM_LBUTTONUP and self._start_point:
                    start_x, start_y = self._start_point
                    self._start_point = None
                    distance = max(abs(point[0] - start_x), abs(point[1] - start_y))
                    if distance >= self.drag_threshold:
                        self.event_queue.put(("drag", point, self.foreground_window_context()))
            return user32.CallNextHookEx(self._hook, code, message, data)

        self._callback = HOOKPROC(callback)
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            HOOKPROC,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.CallNextHookEx.argtypes = [
            HHOOK,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        self._hook = user32.SetWindowsHookExW(
            self.WH_MOUSE_LL, self._callback, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook:
            self.event_queue.put(("hook_error", "无法安装全局鼠标监听。"))
            return
        user32.RegisterHotKey(
            None,
            self.HOTKEY_ID_STATS,
            self.MOD_CONTROL | self.MOD_SHIFT | self.MOD_NOREPEAT,
            ord("W"),
        )
        self._running.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == self.WM_HOTKEY and message.wParam == self.HOTKEY_ID_STATS:
                self.event_queue.put(("stats_hotkey",))
                continue
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.UnregisterHotKey(None, self.HOTKEY_ID_STATS)
        user32.UnhookWindowsHookEx(self._hook)
        self._hook = None
        self._running.clear()

    @staticmethod
    def clipboard_sequence() -> int:
        return int(ctypes.windll.user32.GetClipboardSequenceNumber())

    @staticmethod
    def foreground_window_context() -> ForegroundWindowContext | None:
        try:
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            user32.GetWindowThreadProcessId.argtypes = [
                wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
            ]
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.QueryFullProcessImageNameW.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = int(user32.GetForegroundWindow() or 0)
            if not handle:
                return None
            length = int(user32.GetWindowTextLengthW(handle))
            title_buffer = ctypes.create_unicode_buffer(max(1, length + 1))
            user32.GetWindowTextW(handle, title_buffer, len(title_buffer))
            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
            executable = ""
            process = kernel32.OpenProcess(0x1000, False, process_id.value)
            if process:
                try:
                    path_buffer = ctypes.create_unicode_buffer(32768)
                    path_size = wintypes.DWORD(len(path_buffer))
                    if kernel32.QueryFullProcessImageNameW(
                        process, 0, path_buffer, ctypes.byref(path_size)
                    ):
                        executable = path_buffer.value
                finally:
                    kernel32.CloseHandle(process)
            process_name = Path(executable).name if executable else ""
            return ForegroundWindowContext(
                handle=handle,
                process_id=int(process_id.value),
                process_name=process_name,
                executable=executable,
                title=title_buffer.value,
            )
        except (AttributeError, OSError, ValueError):
            return None

    @staticmethod
    def focus_source_window(handle: int) -> None:
        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetForegroundWindow.restype = wintypes.HWND
        if not user32.IsWindow(handle):
            raise AppError("原划词窗口已关闭，请重新划选。")
        if int(user32.GetForegroundWindow() or 0) != handle:
            user32.SetForegroundWindow(handle)
        if int(user32.GetForegroundWindow() or 0) != handle:
            raise AppError("无法返回原划词窗口，请重新划选后点击记录。")

    @classmethod
    def send_copy(cls) -> None:
        inputs = (INPUT * 4)(
            INPUT(cls.INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(cls.VK_CONTROL, 0, 0, 0, 0))),
            INPUT(cls.INPUT_KEYBOARD, INPUT_UNION(ki=KEYBDINPUT(cls.VK_C, 0, 0, 0, 0))),
            INPUT(
                cls.INPUT_KEYBOARD,
                INPUT_UNION(ki=KEYBDINPUT(cls.VK_C, 0, cls.KEYEVENTF_KEYUP, 0, 0)),
            ),
            INPUT(
                cls.INPUT_KEYBOARD,
                INPUT_UNION(ki=KEYBDINPUT(cls.VK_CONTROL, 0, cls.KEYEVENTF_KEYUP, 0, 0)),
            ),
        )
        user32 = ctypes.windll.user32
        user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        user32.SendInput.restype = wintypes.UINT
        sent = user32.SendInput(4, inputs, ctypes.sizeof(INPUT))
        if sent != 4:
            raise AppError("无法发送复制快捷键。")

    @classmethod
    def read_clipboard_text(cls) -> str:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        if not user32.OpenClipboard(None):
            return ""
        try:
            handle = user32.GetClipboardData(cls.CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return ""
            try:
                return ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()


def popup_character_available(asset_dir: Path = POPUP_CHARACTER_DIR) -> bool:
    """人物素材是否齐备：manifest 加完整的帧序列。

    原来要先把整个动画建出来才知道素材能不能用。渲染已经交给
    front.actor.CharacterActor，这里只留一个判定。
    """
    return (Path(asset_dir) / "manifest.json").is_file() and any(
        Path(asset_dir).glob("frame_*.png")
    )


class WordPickerApp:
    def __init__(self):
        if os.name != "nt":
            raise AppError("该工具仅支持 Windows。")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config = AppConfig()
        # 必须在创建根窗口之前声明 DPI 感知。不声明的话，Windows 会把窗口按缩放
        # 比例位图放大（200% 显示即 2 倍拉伸再插值），细描边图标会糊掉。
        if ui_theme is not None:
            ui_theme.enable_dpi_awareness()
        self.root = tk.Tk()
        # 声明 DPI 感知后坐标是物理像素，界面上所有写死的像素尺寸都要乘这个系数，
        # 否则在缩放显示器上窗口只有一半大。ui_colors / ui_icons 由 _build_styles 填。
        self.ui_scale = ui_theme.detect_scale(self.root) if ui_theme is not None else 1.0
        self.ui_colors: dict | None = None
        self.ui_icons = None
        self.control_images = None            # 自绘勾选框的图集，见 _build_styles
        self.nav_buttons: dict[str, ttk.Button] = {}
        self.nav_icons: dict[str, tuple] = {}
        self.root.title(APP_NAME)
        # 高度按最高的那一页（记录问题，三张卡）给，刚好铺满不留大片空白；
        # 余量给 30 逻辑像素，免得字体度量略有出入就把底部操作条挤出可视区
        self.root.geometry(self._geo(800, 560))
        self.root.minsize(*(self._px(v) for v in (700, 500)))
        self.root.protocol("WM_DELETE_WINDOW", self._minimize)
        self.events: queue.Queue = queue.Queue()
        self.capture = WindowsSelectionCapture(
            self.events, int(self.config.values.get("drag_threshold", 8))
        )
        self.monitoring = True
        self.capture_paused = False
        self.capture_revision = 0
        self.detected_source_context: DetectedSourceContext | ObsidianCaptureContext | ObsidianFileContext | None = None
        self.source_detection_pending = False
        self.source_detection_revision = -1
        self.source_detection_error = ""
        self.source_mark_requested_revision = -1
        self.source_auto_mark_requested_revision = -1
        self.lookup_popup: tk.Toplevel | None = None
        self.popup_character_actor: CharacterActor | None = None
        self.popup_character_asset_dir: Path | None = None
        self.last_sentence = ""
        self.last_pointer = (200, 200)
        # 划词汇浮入口（front/float_entry.py）。窗口本身是惰性创建的，第一次划词
        # 才建；FloatingEntry 为 None 时整条流程退回「划词直接弹窗」。
        self.float_entry: FloatingEntry | None = None
        self.pending_capture: dict | None = None
        self.float_entry_menu: ModePopup | None = None
        # 高光来源是一条**独立**的检测链：它只需要 Obsidian 的 capture（写回来源
        # 文档要有 capture_id），不复用记录问题那条，免得两个模式的状态互相串。
        self.highlight_source_revision = -1
        self.highlight_source_context: ObsidianCaptureContext | None = None
        self.highlight_source_error = ""
        self.highlight_source_pending = False
        self.highlight_requested_key: str | None = None
        self.cache: dict[tuple[str, tuple[str, ...]], list[dict[str, str]]] = {}
        self.lookup_revision = 0
        self.word_index = VocabularyIndex()
        dictionary_path = resolve_dictionary_path(self.config.values)
        self.config.values["dictionary_path"] = str(dictionary_path)
        self.local_dictionary = LocalDictionary(dictionary_path)
        self.dictionary_check_revision = 0
        self.dictionary_status_label: ttk.Label | None = None
        self.dictionary_check_button: ttk.Button | None = None
        self.ignore_window: tk.Toplevel | None = None
        self.ignore_tree: ttk.Treeview | None = None
        self.ignore_search_var: tk.StringVar | None = None
        self.ignore_word_var: tk.StringVar | None = None
        self.ignore_status: ttk.Label | None = None
        self.ignore_rows: dict[str, IgnoredWord] = {}
        self.stats_window: tk.Toplevel | None = None
        self.stats_tree: ttk.Treeview | None = None
        self.stats_search_var: tk.StringVar | None = None
        self.stats_range_var: tk.StringVar | None = None
        self.stats_summary: ttk.Label | None = None
        self.output_preview_label: ttk.Label | None = None
        self.mode_var: tk.StringVar | None = None
        self.capture_mode = str(self.config.values.get("mode", MODE_TRANSLATE))
        if self.capture_mode not in {MODE_TRANSLATE, MODE_QUESTION, MODE_HIGHLIGHT}:
            self.capture_mode = MODE_TRANSLATE
        self.page_frames: dict[str, ttk.Frame] = {}
        self.question_output_dir_var: tk.StringVar | None = None
        self.question_output_filename_var: tk.StringVar | None = None
        self.question_output_preview_label: ttk.Label | None = None
        self.question_output_history_box: ttk.Combobox | None = None
        self.question_output_history_targets: dict[str, Path] = {}
        self.loaded_question_vault_key = ""
        self.question_body_dir_var: tk.StringVar | None = None
        # 高光页的调色板变量。跟问题页一样先声明成空：轻量调用方（测试用
        # __new__ 造壳）不跑 _build_highlight_page，_save_settings 要能兜住。
        # 单条导出目标的输入框（高光页），以及「已推给插件的那份」的幂等缓存：
        # 每次保存都打一次 HTTP 没必要，仓库或内容没变就不重复推。
        self.highlight_single_index_dir_var: tk.StringVar | None = None
        self.highlight_single_index_filename_var: tk.StringVar | None = None
        self.highlight_single_note_var: tk.StringVar | None = None
        self._highlight_single_export_lock = threading.Lock()
        self._highlight_single_export_synced = None
        # 推送失败后的兜底重试时间戳（见 _maybe_retry_highlight_single_export）。
        self._highlight_single_export_check_at = 0.0
        self._highlight_single_export_retry_at = 0.0
        # 悬浮窗的 3 色同理：变过才推。
        self.highlight_active_colors: list[str] = list(DEFAULT_HIGHLIGHT_ACTIVE_COLORS)
        self._highlight_active_colors_lock = threading.Lock()
        self._highlight_active_colors_synced = None
        self._native_highlight_ready = False
        self._native_highlight_sync_revision = 0
        self._native_highlight_sync_lock = threading.Lock()
        self._native_mode_listener_stop = threading.Event()
        self.highlight_native_entry_var: tk.BooleanVar | None = None
        configured_vault = configured_highlight_vault(self.config.values)
        if configured_vault and os.name == "nt":
            def warm_configured_pipe() -> None:
                try:
                    ObsidianBridgeClient(configured_vault).warm_highlight_pipe()
                except (OSError, ObsidianBridgeError):
                    pass  # Offline plugin cannot block startup.
            threading.Thread(target=warm_configured_pipe,
                             name="highlight-pipe-prewarm", daemon=True).start()
        self.question_materials_dir_var: tk.StringVar | None = None
        self.question_record_mode_var: tk.StringVar | None = None
        self.question_settings_window: tk.Toplevel | None = None
        self.web_source_window: tk.Toplevel | None = None
        self.question_source_default_var: tk.StringVar | None = None
        self.question_supplement_default_var: tk.StringVar | None = None
        self.question_title_level_default_var: tk.StringVar | None = None
        self.question_obsidian_source_link_var: tk.BooleanVar | None = None
        self.obsidian_vault_var: tk.StringVar | None = None
        # 记录问题的连接状态；高光页使用自己的状态标签。
        self.obsidian_status_labels: list[ttk.Label] = []
        self.question_title_var: tk.StringVar | None = None
        self.question_selected_text_input: tk.Text | None = None
        self.question_input: tk.Text | None = None
        self.question_supplement_input: tk.Text | None = None
        self.question_editor_panes: tk.PanedWindow | None = None
        self.question_code_target: tk.Text | None = None
        self.question_image_target: tk.Text | None = None
        self.question_image_insert_index: str | None = None
        self.question_source_button: ttk.Button | None = None
        self.question_source_menu: tk.Menu | None = None
        self.question_images: list[dict] = []
        self.source_library = SourceLibrary()
        self.question_sources: list[dict] = []
        self.question_image_selected_index: int | None = None
        self.question_image_selected_widget = None
        self.question_image_status_host: tk.Frame | None = None
        self.question_image_status_label: ttk.Label | None = None
        self.question_image_delete_button: ttk.Button | None = None
        self.question_save_button: ttk.Button | None = None
        self.question_save_status: ttk.Label | None = None
        self.question_save_revision = 0
        self.question_tag_pool: list[str] | None = None
        self.question_tag_pool_at = 0.0
        self.question_tag_scan_pending = False
        self.question_tag_combo: ttk.Combobox | None = None
        self.question_tag_var: tk.StringVar | None = None
        self.question_tag_target: tk.Text | None = None   # 加号插标签的位置
        self.question_tag_supports: list = []       # front.tags.TagTextSupport
        self.save_button: ttk.Button | None = None
        self.save_revision = 0
        self._build_styles()
        self._build_control_window()
        self.root.bind("<Control-Shift-w>", lambda _event: self._open_stats_window())
        self.root.bind("<Control-Shift-W>", lambda _event: self._open_stats_window())
        self.capture.start()
        self.root.after(60, self._process_events)
        self.root.after(250, self._sync_status)
        self.root.after(350, lambda: self._start_dictionary_check(quiet=True))
        self.root.after(900, lambda: self._start_highlight_single_export_sync(notify=False))
        self.root.after(920, lambda: self._start_highlight_active_colors_sync(notify=False))
        self.root.after(950, self._start_highlight_entry_sync)
        self.root.after(1100, self._start_native_mode_listener)

    def _px(self, value: float) -> int:
        """逻辑像素 -> 物理像素。"""
        return max(1, int(round(value * self.ui_scale)))

    def _geo(self, w: int, h: int) -> str:
        return f"{self._px(w)}x{self._px(h)}"

    def _editor_colors(self) -> dict:
        """tk.Text 的配色。经典控件不走 ttk 样式，只能显式传。

        底色用 bg_field（与它所在的「输入区」框一致），选中态跟下拉列表一样是
        强调色配白字 —— Tk 的 Text 没有半透明，Obsidian 那种淡淡一层紫色做不出来。
        """
        return {
            "background": self._token("bg_field"),
            "foreground": self._token("text_normal"),
            "insertbackground": self._token("text_normal"),
            "selectbackground": self._token("accent"),
            "selectforeground": self._token("text_on_accent"),
            "highlightthickness": 0,
        }

    def _token(self, name: str) -> str:
        """取一个主题令牌。经典 tk 控件（Text / Canvas / Frame / Label）不走 ttk
        样式，颜色只能一个个传进去，取的就是这里。主题层没加载时退回亮色默认值，
        这样少一个模块也不会露出白底黑字的裸控件。"""
        colors = getattr(self, "ui_colors", None)
        if colors and name in colors:
            return colors[name]
        return FALLBACK_UI_COLORS[name]

    def _darken_titlebar(self, window: tk.Misc) -> None:
        """把窗口标题栏调成和界面底色一样，免得暗色界面顶上压一条亮栏。

        为什么要挂在 `<Map>` 上，而不是建窗时设一次：建这个窗口的当下它还没映射，
        DWM 会在映射时把深色标题栏属性丢掉。而**每次**映射都会丢——「保存并最小化」
        之后 `deiconify()` 还原（5633/7190）、统计窗和免翻译词表窗重新打开
        （4900/5041）都会再映射一次，只补一次的话那些窗口的标题栏会悄悄变回亮的。
        所以绑 `<Map>`，映射几次就设几次；`after` 那次是首屏兜底，免得首帧闪一下。

        老版 front/ 里没这个函数时跳过：装饰性效果，不该为它把启动打断。
        """
        if ui_theme is None or not hasattr(ui_theme, "apply_dark_titlebar"):
            return
        caption = self._token("bg_primary")

        def apply(_event=None) -> None:
            try:
                if window.winfo_exists():
                    ui_theme.apply_dark_titlebar(window, caption)
            except tk.TclError:
                pass                      # 窗口正在销毁，装饰性效果不值得报错

        window.bind("<Map>", apply, add="+")
        titlebar_timer = window.after(120, apply)

        def cancel_titlebar_timer(event) -> None:
            if event.widget is window:
                try:
                    window.after_cancel(titlebar_timer)
                except tk.TclError:
                    pass

        window.bind("<Destroy>", cancel_titlebar_timer, add="+")

    def _build_styles(self) -> None:
        # 原有的三个命名样式在 Obsidian 主题里都有对应定义（Title/Muted/Word），
        # 所以下面各页面直接引用旧名字也能拿到新外观，不必逐处改。
        if ui_theme is not None:
            # 这套样式自带字体层级（见 front/theme.apply），不能再 option_add("*Font")，
            # 那会覆盖样式字号，把标题和正文压成一样大。
            # 图标缓存要先建：apply() 会用它替换下拉框/微调框的箭头
            self.ui_icons = ui_icons.IconCache(str(ASSETS_UI_DIR / "icons"), self.ui_scale)
            # 外观是暗色，对齐用户的暗色 Obsidian（见 theme.default_palette）。
            # 界面没有主题开关：样式只在这里建一次，各处控件在建成时就抄走了颜色，
            # 所以真要换主题得重启软件，热切换是另一件事。
            self.ui_colors = ui_theme.apply(self.root,
                                           palette=ui_theme.default_palette(),
                                           scale=self.ui_scale,
                                           icons=self.ui_icons)
            # 标题栏跟着暗，并且和页面同色（见 _darken_titlebar）
            self._darken_titlebar(self.root)
            # 勾选框/单选钮按 Obsidian 的样子自绘（clam 的指示器是一条歪斜细线）。
            # 配色要从调色板取，深浅主题才对得上。
            if ui_controls is not None:
                self.control_images = ui_controls.ControlImages(
                    self.ui_icons, self.ui_scale,
                    accent=self.ui_colors["accent"],
                    border=self.ui_colors["border_strong"],
                    on_accent=self.ui_colors["text_on_accent"],
                )
            return

        # 退回原有外观时才需要它：vista 主题没有自定义样式，全靠默认字体
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#5F6368")
        style.configure("Word.TLabel", font=("Microsoft YaHei UI", 11, "bold"))

    def _build_sidebar(self, parent: tk.Misc) -> None:
        """左栏：应用名、监听状态、页面导航。

        导航用按钮而不是下拉框：三个页面是并列关系，摊开来一眼看全，图标也让
        「翻译 / 记录 / 配置」各自有辨识度。选中页由 mode_var 驱动，见 _refresh_nav。
        """
        bar = ttk.Frame(parent, style="Sidebar.TFrame",
                        padding=(self._px(12), self._px(18)))
        bar.grid(row=0, column=0, sticky="nsew")

        ttk.Label(bar, text=APP_NAME, style="SideTitle.TLabel").pack(anchor="w")
        self.status_label = ttk.Label(
            bar, text="", style="SideMuted.TLabel",
            wraplength=self._px(140), justify="left",
        )
        self.status_label.pack(anchor="w", pady=(self._px(8), self._px(18)))

        current_mode = self.capture_mode
        current_page_label = next(
            (label for label, value in PAGE_LABELS.items() if value == current_mode),
            "翻译英语单词",
        )
        self.mode_var = tk.StringVar(value=current_page_label)

        for label in PAGE_LABELS:
            icon_name = PAGE_ICONS.get(label, "file-text")
            kw: dict = {}
            if self.ui_icons is not None:
                # 常态灰、选中强调色，两张图都留在 self.nav_icons 里防止被回收
                self.nav_icons[label] = (
                    self.ui_icons.get(icon_name, 16, self.ui_colors["text_muted"]),
                    self.ui_icons.get(icon_name, 16, self.ui_colors["accent"]),
                )
                kw["image"] = self.nav_icons[label][0]
                kw["compound"] = "left"
            button = ttk.Button(
                bar, text=label, style="Nav.TButton", takefocus=False,
                command=lambda chosen=label: self.mode_var.set(chosen), **kw,
            )
            button.pack(fill="x", pady=(0, self._px(2)))
            self.nav_buttons[label] = button

        # 插件属于整套应用，不属于「记录问题」或「高光」某一个页面。
        # pack(side=bottom) 固定在左栏底部，切页时入口仍在同一位置。
        bridge_tools = ttk.Frame(bar, style="Sidebar.TFrame")
        bridge_tools.pack(side="bottom", fill="x", pady=(self._px(12), 0))
        ttk.Button(
            bridge_tools, text="安装/更新插件", style="Nav.TButton",
            command=self._install_obsidian_plugin,
        ).pack(fill="x", pady=(0, self._px(2)))
        ttk.Button(
            bridge_tools, text="检测连接", style="Nav.TButton",
            command=self._check_obsidian_bridge,
        ).pack(fill="x")
        self.obsidian_sidebar_status = ttk.Label(
            bridge_tools, text=OBSIDIAN_STATUS_NOTICE, style="SideMuted.TLabel",
            wraplength=self._px(140), justify="left",
        )
        self.obsidian_sidebar_status.pack(fill="x", pady=(self._px(6), 0))
        self.obsidian_status_labels.append(self.obsidian_sidebar_status)

        self.mode_var.trace_add("write", lambda *_args: self._on_mode_changed())
        self._refresh_nav()

    def _refresh_nav(self) -> None:
        """按 mode_var 给导航换选中样式与图标颜色。"""
        active = self.mode_var.get() if getattr(self, "mode_var", None) else ""
        for label, button in self.nav_buttons.items():
            highlighted = label == active
            button.configure(style="NavActive.TButton" if highlighted else "Nav.TButton")
            pair = self.nav_icons.get(label)
            if pair:
                button.configure(image=pair[1] if highlighted else pair[0])

    def _scrollable_settings_page(self, parent: tk.Misc):
        """每个设置页各自记住滚动位置；底部保存操作条始终留在视口。"""
        wrapper = ttk.Frame(parent, style="Page.TFrame")
        wrapper.grid(row=0, column=0, sticky="nsew")
        wrapper.columnconfigure(0, weight=1)
        wrapper.rowconfigure(0, weight=1)
        canvas = tk.Canvas(wrapper, background=(getattr(self, "ui_colors", None) or {}).get(
                               "bg_page", self._token("bg_secondary")),
                           borderwidth=0, highlightthickness=0)
        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        page = ttk.Frame(canvas, style="Page.TFrame")
        item = canvas.create_window((0, 0), window=page, anchor="nw")
        page.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
        return wrapper, page, canvas

    def _on_settings_wheel(self, event) -> str | None:
        """只在当前设置页接管滚轮；弹窗、文本框和下拉框仍保留自身行为。"""
        page = PAGE_LABELS.get(self.mode_var.get(), MODE_TRANSLATE)
        canvas = getattr(self, "settings_canvases", {}).get(page)
        wrapper = getattr(self, "page_frames", {}).get(page)
        if canvas is None or wrapper is None:
            return None
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        inside = False
        while widget is not None:
            if isinstance(widget, (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox)):
                return None
            if widget is wrapper:
                inside = True
                break
            widget = widget.master
        if not inside or canvas.yview() == (0.0, 1.0):
            return None
        steps = -1 if event.delta > 0 else 1
        canvas.yview_scroll(steps, "units")
        return "break"

    def _build_control_window(self) -> None:
        # 版面：左边一条窄栏（标题、状态、页面导航），右侧白底内容区。灰白分栏是
        # Obsidian 设置页的基本骨架，也把「切换页面」从下拉框换成了看得见的导航。
        shell = ttk.Frame(self.root, style="Page.TFrame")
        shell.pack(fill="both", expand=True)
        shell.columnconfigure(1, weight=1)
        shell.rowconfigure(0, weight=1)

        self._build_sidebar(shell)

        content = ttk.Frame(shell, style="Page.TFrame",
                            padding=(self._px(20), self._px(16)))
        content.grid(row=0, column=1, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(0, weight=1)

        page_container = ttk.Frame(content, style="Page.TFrame")
        page_container.grid(row=0, column=0, sticky="nsew")
        page_container.rowconfigure(0, weight=1)
        page_container.columnconfigure(0, weight=1)

        self.page_frames = {}
        self.settings_canvases = {}
        pages = {}
        for key in (MODE_TRANSLATE, MODE_QUESTION, MODE_HIGHLIGHT, "api"):
            wrapper, page, canvas = self._scrollable_settings_page(page_container)
            self.page_frames[key] = wrapper
            self.settings_canvases[key] = canvas
            pages[key] = page
        translate_page = pages[MODE_TRANSLATE]
        question_page = pages[MODE_QUESTION]
        highlight_page = pages[MODE_HIGHLIGHT]
        api_page = pages["api"]
        self.root.bind("<MouseWheel>", self._on_settings_wheel, add="+")

        output_section = ttk.LabelFrame(
            translate_page, text="单词库存储", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        output_section.pack(fill="x")
        output_section.columnconfigure(1, weight=1)
        output_path = resolve_output_path(self.config.values)
        self.output_dir_var = tk.StringVar(value=str(output_path.parent))
        self.output_filename_var = tk.StringVar(value=output_path.name)
        output_format = str(
            self.config.values.get("output_format", output_path.suffix.lower().lstrip("."))
        )
        if output_format not in OUTPUT_FORMAT_LABELS.values():
            output_format = "md"
        self.output_format_var = tk.StringVar(
            value=next(
                label for label, value in OUTPUT_FORMAT_LABELS.items() if value == output_format
            )
        )

        ttk.Label(output_section, text="单词库目录").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Entry(output_section, textvariable=self.output_dir_var).grid(
            row=0, column=1, sticky="ew", pady=6
        )
        ttk.Button(output_section, text="选择目录…", command=self._choose_output_dir).grid(
            row=0, column=2, padx=(8, 0), pady=6
        )
        ttk.Button(output_section, text="复制路径", command=self._copy_output_dir).grid(
            row=0, column=3, padx=(8, 0), pady=6
        )

        ttk.Label(output_section, text="文档文件名").grid(
            row=1, column=0, sticky="w", padx=(0, 12), pady=6
        )
        ttk.Entry(output_section, textvariable=self.output_filename_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=6
        )

        ttk.Label(output_section, text="存储格式").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=6
        )
        format_box = ttk.Combobox(
            output_section,
            textvariable=self.output_format_var,
            values=list(OUTPUT_FORMAT_LABELS),
            state="readonly",
        )
        format_box.grid(row=2, column=1, columnspan=3, sticky="ew", pady=6)
        format_box.bind("<<ComboboxSelected>>", self._on_output_format_changed)
        # 预览里是完整路径，比卡片宽就会顶出窗口，让它按可用宽度折行
        self.output_preview_label = ttk.Label(
            output_section, text="", style="Muted.TLabel",
            wraplength=self._px(400), justify="left",
        )
        self.output_preview_label.grid(row=3, column=1, columnspan=3, sticky="w", pady=(0, 6))
        self.output_dir_var.trace_add("write", lambda *_args: self._sync_output_preview())
        self.output_filename_var.trace_add("write", lambda *_args: self._sync_output_preview())
        self._sync_output_preview()

        dictionary_section = ttk.LabelFrame(
            translate_page, text="本地词典", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        dictionary_section.pack(fill="x", pady=(self._px(12), 0))
        dictionary_section.columnconfigure(1, weight=1)
        ttk.Label(dictionary_section, text="词典文件路径").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.dictionary_path_var = tk.StringVar(
            value=str(resolve_dictionary_path(self.config.values))
        )
        ttk.Entry(dictionary_section, textvariable=self.dictionary_path_var).grid(
            row=0, column=1, sticky="ew", pady=6
        )
        ttk.Button(dictionary_section, text="选择词典…", command=self._choose_dictionary_file).grid(
            row=0, column=2, padx=(8, 0), pady=6
        )
        self.dictionary_check_button = ttk.Button(
            dictionary_section, text="检查词典", command=self._start_dictionary_check
        )
        self.dictionary_check_button.grid(row=0, column=3, padx=(8, 0), pady=6)
        self.dictionary_status_label = ttk.Label(dictionary_section, text="尚未检查", style="Muted.TLabel")
        self.dictionary_status_label.grid(row=1, column=1, columnspan=3, sticky="w", pady=(0, 6))

        # 页面底色是灰的，按钮条也得放在白底上，否则灰按钮压在灰底上看不见
        translation_actions = ttk.Frame(translate_page, style="Card.TFrame",
                                        padding=(self._px(14), self._px(10)))
        translation_actions.pack(fill="x", pady=(self._px(12), 0))
        ttk.Button(translation_actions, text="免翻译词表…", command=self._open_ignore_manager).pack(side="left")
        ttk.Button(translation_actions, text="统计单词库", command=self._open_stats_window).pack(side="left", padx=(self._px(8), 0))

        question_path = resolve_question_output_path(self.config.values)
        question_frame = ttk.LabelFrame(
            question_page, text="问题记录文档", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        question_frame.pack(fill="x")
        question_frame.columnconfigure(1, weight=1)
        self.question_output_dir_var = tk.StringVar(value=str(question_path.parent))
        self.question_output_filename_var = tk.StringVar(value=question_path.name)
        ttk.Label(question_frame, text="文档目录").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=4)
        ttk.Entry(question_frame, textvariable=self.question_output_dir_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(question_frame, text="选择目录…", command=self._choose_question_output_dir).grid(row=0, column=2, padx=(8, 0), pady=4)
        ttk.Label(question_frame, text="文档文件名").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=4)
        self.question_output_history_box = ttk.Combobox(
            question_frame,
            textvariable=self.question_output_filename_var,
            state="normal",
            values=(),
        )
        self.question_output_history_box.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)
        self.question_output_history_box.bind(
            "<<ComboboxSelected>>", self._on_question_document_history_selected
        )
        self.question_output_preview_label = ttk.Label(
            question_frame, text="", style="Muted.TLabel",
            wraplength=self._px(400), justify="left",
        )
        self.question_output_preview_label.grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 2))
        self.question_body_dir_var = tk.StringVar(
            value=str(resolve_question_body_dir(self.config.values))
        )
        ttk.Label(question_frame, text="正文库目录").grid(
            row=3, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(question_frame, textvariable=self.question_body_dir_var).grid(
            row=3, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            question_frame, text="选择目录…", command=self._choose_question_body_dir
        ).grid(row=3, column=2, padx=(8, 0), pady=4)
        self.question_materials_dir_var = tk.StringVar(
            value=str(resolve_question_materials_dir(self.config.values))
        )
        ttk.Label(question_frame, text="文档资料库目录").grid(
            row=4, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(question_frame, textvariable=self.question_materials_dir_var).grid(
            row=4, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            question_frame, text="选择目录…", command=self._choose_question_materials_dir
        ).grid(row=4, column=2, padx=(8, 0), pady=4)
        self.question_output_dir_var.trace_add("write", lambda *_args: self._question_output_directory_changed())
        self.question_output_filename_var.trace_add("write", lambda *_args: self._sync_question_output_preview())
        self._sync_question_output_preview()

        inferred_vault = find_obsidian_vault(question_path)
        configured_vault = str(self.config.values.get("obsidian_vault_path", "")).strip()
        obsidian_frame = ttk.LabelFrame(
            question_page, text="Obsidian 定位", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        obsidian_frame.pack(fill="x", pady=(self._px(12), 0))
        obsidian_frame.columnconfigure(1, weight=1)
        self.obsidian_vault_var = tk.StringVar(
            value=configured_vault or (str(inferred_vault) if inferred_vault else "")
        )
        ttk.Label(obsidian_frame, text="仓库目录").grid(
            row=0, column=0, sticky="w", padx=(0, 10), pady=4
        )
        ttk.Entry(obsidian_frame, textvariable=self.obsidian_vault_var).grid(
            row=0, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            obsidian_frame, text="选择目录…", command=self._choose_obsidian_vault
        ).grid(row=0, column=2, padx=(8, 0), pady=4)
        self._register_obsidian_status_label(obsidian_frame, row=1)
        self.loaded_question_vault_key = (
            _normalized_path_key(self.obsidian_vault_var.get())
            if self.obsidian_vault_var.get().strip()
            else ""
        )
        self.obsidian_vault_var.trace_add("write", self._on_obsidian_vault_changed)
        self._refresh_question_document_history()

        self._load_question_default_settings()

        question_settings_actions = ttk.Frame(question_page, style="Card.TFrame",
                                              padding=(self._px(14), self._px(10)))
        question_settings_actions.pack(fill="x", pady=(self._px(12), 0))
        ttk.Button(
            question_settings_actions,
            text="记录设置…",
            command=self._open_question_settings,
        ).pack(side="left")
        ttk.Label(
            question_settings_actions,
            text="配置原文、内容补充、来源链接和标题级别的默认值",
            style="Muted.TLabel",
        ).pack(side="left", padx=(self._px(10), 0))

        self._build_highlight_page(highlight_page)

        api_form = ttk.LabelFrame(
            api_page, text="模型接口", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        api_form.pack(fill="x")
        api_form.columnconfigure(1, weight=1)
        ttk.Label(api_form, text="API Key").grid(
            row=0, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.key_var = tk.StringVar()
        key_entry = ttk.Entry(api_form, textvariable=self.key_var, show="•")
        key_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=6)
        try:
            if self.config.get_api_key():
                self.key_var.set("••••••••••••••••")
        except ConfigError:
            pass

        ttk.Label(api_form, text="模型").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.model_var = tk.StringVar(value=str(self.config.values["model"]))
        ttk.Entry(api_form, textvariable=self.model_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=6
        )

        ttk.Label(api_form, text="接口协议").grid(
            row=2, column=0, sticky="w", padx=(0, 12), pady=6
        )
        current_protocol = str(self.config.values.get("protocol", PROTOCOL_RESPONSES))
        current_label = next(
            (label for label, value in PROTOCOL_LABELS.items() if value == current_protocol),
            next(iter(PROTOCOL_LABELS)),
        )
        self.protocol_var = tk.StringVar(value=current_label)
        ttk.Combobox(
            api_form,
            textvariable=self.protocol_var,
            values=list(PROTOCOL_LABELS),
            state="readonly",
        ).grid(row=2, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(api_form, text="Base URL").grid(
            row=3, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.base_url_var = tk.StringVar(
            value=str(self.config.values.get("base_url", DEFAULT_BASE_URL))
        )
        ttk.Entry(api_form, textvariable=self.base_url_var).grid(
            row=3, column=1, columnspan=3, sticky="ew", pady=6
        )

        ttk.Label(api_form, text="JSON 输出").grid(
            row=4, column=0, sticky="w", padx=(0, 12), pady=6
        )
        current_json_mode = str(self.config.values.get("json_mode", JSON_MODE_SCHEMA))
        current_json_label = next(
            (label for label, value in JSON_MODE_LABELS.items() if value == current_json_mode),
            next(iter(JSON_MODE_LABELS)),
        )
        self.json_mode_var = tk.StringVar(value=current_json_label)
        ttk.Combobox(
            api_form,
            textvariable=self.json_mode_var,
            values=list(JSON_MODE_LABELS),
            state="readonly",
        ).grid(row=4, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(api_form, text="超时（秒）").grid(
            row=5, column=0, sticky="w", padx=(0, 12), pady=6
        )
        self.timeout_var = tk.StringVar(value=str(self.config.values.get("timeout", 60)))
        ttk.Spinbox(
            api_form,
            from_=10,
            to=600,
            increment=10,
            textvariable=self.timeout_var,
        ).grid(row=5, column=1, columnspan=3, sticky="ew", pady=6)

        ttk.Label(api_form, text="预设").grid(row=6, column=0, sticky="w", padx=(0, 12), pady=6)
        presets = ttk.Frame(api_form)
        presets.grid(row=6, column=1, columnspan=3, sticky="w", pady=6)
        ttk.Button(presets, text="OpenAI 官方", command=lambda: self._set_api_preset("openai")).pack(
            side="left"
        )
        ttk.Button(presets, text="Grok 官方", command=lambda: self._set_api_preset("grok")).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(
            presets,
            text="DeepSeek 官方",
            command=lambda: self._set_api_preset("deepseek"),
        ).pack(side="left", padx=(8, 0))

        # 提示跟着表单走，放进同一张白卡，不然在灰底上会糊成一块
        ttk.Label(
            api_form,
            text="选中的句子会发送给所配置的 API。使用中转时，Key 和句子都会经过中转服务。",
            style="MutedCard.TLabel",
            wraplength=self._px(430),
            justify="left",
        ).grid(row=7, column=0, columnspan=4, sticky="w", pady=(self._px(10), 0))

        # 底部操作条压在分隔线下面，和页面内容分开
        ttk.Frame(content, style="Hairline.TFrame", height=1).grid(
            row=1, column=0, sticky="ew", pady=(self._px(16), 0)
        )
        buttons = ttk.Frame(content, style="Page.TFrame")
        buttons.grid(row=2, column=0, sticky="ew", pady=(self._px(12), 0))
        self.toggle_button = ttk.Button(buttons, text="暂停监听", style="Page.TButton",
                                        command=self._toggle_monitoring)
        self.toggle_button.pack(side="left")
        self._icon_button(buttons, "save", "保存并最小化", self._save_and_minimize,
                          style="Page.TButton").pack(
            side="right", padx=(self._px(8), 0)
        )
        self._icon_button(buttons, "x", "退出", self._exit,
                          style="Page.TButton").pack(side="right")
        self._on_mode_changed()

    def _icon_button(self, parent: tk.Misc, icon_name: str, text: str, command,
                     size: int = 16, style: str = "TButton",
                     tooltip_text: str | None = None) -> ttk.Button:
        """图标 + 文字的按钮；图标素材缺失时退回纯文字按钮，不留空位。"""
        if self.ui_icons is None or not self.ui_icons.has(icon_name):
            return ttk.Button(parent, text=text, command=command, style=style)
        return ui_controls.icon_button(
            parent, self.ui_icons, icon_name, size=size,
            color=self.ui_colors["text_muted"], text=text,
            colors=self.ui_colors, scale=self.ui_scale, style=style,
            command=command, tooltip_text=tooltip_text,
        )

    def _flat_checkbox(self, parent: tk.Misc, text: str, variable,
                       command=None, size: int = 16) -> ttk.Checkbutton:
        """Obsidian 样式的勾选框（自绘的勾，替代 clam 那条歪斜细线）。

        变量由调用方持有：这几个开关的值都要读回去写配置。自绘图集缺失时退回
        ttk 自带的指示器，功能不受影响。默认样式 Flat.TCheckbutton 是白面上的，
        放到卡片外或灰底上要传 Card.Flat.TCheckbutton。
        """
        if ui_controls is None or self.control_images is None:
            return ttk.Checkbutton(parent, text=text, variable=variable, command=command)
        return ui_controls.checkbox(
            parent, text=text, images=self.control_images, variable=variable,
            command=command, size=size,
        )

    def _choose_output_dir(self) -> None:
        current = Path(self.output_dir_var.get().strip()).expanduser()
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择单词库目录",
            initialdir=str(current if current.exists() else APP_DIR),
            mustexist=False,
        )
        if path:
            self.output_dir_var.set(path)

    def _choose_question_output_dir(self) -> None:
        if not self.question_output_dir_var:
            return
        current = Path(self.question_output_dir_var.get().strip()).expanduser()
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择问题记录目录",
            initialdir=str(self._question_directory_dialog_start(current)),
            mustexist=False,
        )
        if path:
            self.question_output_dir_var.set(path)

    def _choose_question_body_dir(self) -> None:
        if not self.question_body_dir_var:
            return
        current = Path(self.question_body_dir_var.get().strip()).expanduser()
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择正文库目录",
            initialdir=str(self._question_directory_dialog_start(current)),
            mustexist=False,
        )
        if path:
            self.question_body_dir_var.set(path)

    def _choose_question_materials_dir(self) -> None:
        if not self.question_materials_dir_var:
            return
        current = Path(self.question_materials_dir_var.get().strip()).expanduser()
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择文档资料库目录",
            initialdir=str(self._question_directory_dialog_start(current)),
            mustexist=False,
        )
        if path:
            self.question_materials_dir_var.set(path)

    def _question_output_directory_changed(self) -> None:
        self._sync_question_output_preview()
        self._refresh_question_document_history()

    def _question_directory_dialog_start(self, current: Path) -> Path:
        vault_text = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        if vault_text:
            vault = Path(vault_text).expanduser()
            if vault.is_dir():
                return vault
        return current if current.exists() else APP_DIR

    def _apply_question_paths(
        self,
        index_dir: Path | str,
        index_filename: str,
        body_dir: Path | str,
        materials_dir: Path | str,
    ) -> None:
        """三个目录同时写进 UI 变量和 config.values。

        写记录时索引与资料库取自 config.values、正文却取自 UI 变量，只改一边
        会让同一条记录拆到两个仓库去。空值跳过，留给 resolve_question_* 的兜底。
        """
        pairs = (
            (self.question_output_dir_var, "question_output_dir", str(index_dir)),
            (self.question_output_filename_var, "question_output_filename", str(index_filename)),
            (self.question_body_dir_var, "question_body_dir", str(body_dir)),
            (self.question_materials_dir_var, "question_materials_dir", str(materials_dir)),
        )
        for variable, key, value in pairs:
            if not value:
                continue
            if variable:
                variable.set(value)
            self.config.values[key] = value
        if index_dir and index_filename:
            self.config.values["question_output_path"] = str(
                Path(index_dir) / str(index_filename)
            )

    def _set_obsidian_status_notice(self, is_vault: bool) -> None:
        """仓库那一行下面那句说明。

        目录不是 Obsidian 仓库时也要把话说明白：三个目录照常落地，不然用户
        看着「选了目录却什么都没发生」，只会以为功能坏了。
        """
        if not getattr(self, "obsidian_status_labels", []):
            return
        self._set_obsidian_status(
            OBSIDIAN_STATUS_NOTICE if is_vault else OBSIDIAN_STATUS_NEEDS_VAULT_NOTICE
        )

    def _remember_previous_question_vault(self) -> None:
        """切走之前，把页面上这套配置记回上一个仓库的 profile。

        按「保存」时会记一次（_save_settings），这里补的是「没按保存就换仓库」
        那一路：换回来的时候目录还是原来那套，不必重新填一遍。配置没变就不写盘，
        免得在仓库输入框里敲字时一路写文件。
        """
        previous = self.loaded_question_vault_key
        if not previous or not self.question_output_dir_var:
            return
        # 记录方式的下拉框现在不在页面上（question_record_mode_var 一直是 None），
        # 跟 _save_settings 一样走默认的那一项，别因此把整条记录跳过。
        mode_label = (
            self.question_record_mode_var.get()
            if self.question_record_mode_var
            else next(iter(QUESTION_RECORD_MODE_LABELS))
        )
        fields = {
            "question_output_dir": self.question_output_dir_var.get().strip(),
            "question_output_filename": self.question_output_filename_var.get().strip()
            if self.question_output_filename_var
            else "",
            "question_body_dir": self.question_body_dir_var.get().strip()
            if self.question_body_dir_var
            else "",
            "question_materials_dir": self.question_materials_dir_var.get().strip()
            if self.question_materials_dir_var
            else "",
            "question_record_mode": QUESTION_RECORD_MODE_LABELS.get(
                mode_label, QUESTION_RECORD_MODE_INDEPENDENT
            ),
        }
        if not all(fields.values()):
            return
        # 只填了仓库、目录还是应用自己 data 目录的时候，那是占位值不是用户的选择
        # （resolve_question_body_dir 对老配置也是这么判的），别把它存成这个仓库的配置。
        if (
            Path(fields["question_output_dir"]).expanduser().resolve()
            == DEFAULT_QUESTION_OUTPUT_PATH.parent.resolve()
        ):
            return
        if question_vault_profile(self.config.values, previous) == fields:
            return
        record_question_vault_profile(self.config.values, previous, fields)
        try:
            self.config.save()
        except OSError:
            pass                                  # 记不住上一个仓库不该挡住换仓库

    def _on_obsidian_vault_changed(self, *_args) -> None:
        vault_text = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        if not vault_text:
            self._remember_previous_question_vault()
            self.loaded_question_vault_key = ""
            self._refresh_question_document_history()
            return
        vault = Path(vault_text).expanduser()
        # 存不存在才是关键：三个目录落在哪儿不取决于 .obsidian，写记录也不依赖它。
        # 只有插件和定位需要仓库，缺 .obsidian 就在下面那行提示里说清楚。
        if not vault.is_dir():
            self._refresh_question_document_history()
            return
        vault_key = _normalized_path_key(vault)
        if vault_key == self.loaded_question_vault_key:
            self._refresh_question_document_history()
            return
        self._remember_previous_question_vault()
        self.loaded_question_vault_key = vault_key
        self._set_obsidian_status_notice((vault / ".obsidian").is_dir())
        profile = question_vault_profile(self.config.values, vault)
        if profile:
            self._apply_question_paths(
                profile.get("question_output_dir", ""),
                profile.get("question_output_filename", ""),
                profile.get("question_body_dir", ""),
                profile.get("question_materials_dir", ""),
            )
            record_mode = profile.get("question_record_mode", "")
            if self.question_record_mode_var and record_mode:
                label = next(
                    (
                        label
                        for label, value in QUESTION_RECORD_MODE_LABELS.items()
                        if value == record_mode
                    ),
                    "",
                )
                if label:
                    self.question_record_mode_var.set(label)
        else:
            # 还没记录过配置的仓库：按默认模板落地，三个目录都挂到 NoteDataOfY/
            # 下，跟用户自己写的笔记分开；不沿用上一个仓库的文档名。
            defaults = apply_question_dir_template(
                question_dir_template(self.config.values), vault
            )
            self._apply_question_paths(
                defaults["question_output_dir"],
                defaults["question_output_filename"],
                defaults["question_body_dir"],
                defaults["question_materials_dir"],
            )
        self._refresh_question_document_history()

    def _refresh_question_document_history(self) -> None:
        """List Markdown files directly in the configured index directory."""
        box = self.question_output_history_box
        directory_text = (
            self.question_output_dir_var.get().strip()
            if self.question_output_dir_var
            else ""
        )
        if not box or not directory_text:
            self.question_output_history_targets = {}
            if box:
                box.configure(values=())
            return
        try:
            directory = Path(directory_text).expanduser().resolve()
            files = [path for path in directory.glob("*.md") if path.is_file()]
            files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            targets = {path.name: path for path in files}
        except (OSError, ValueError):
            targets = {}
        self.question_output_history_targets = targets
        box.configure(values=list(targets))

    def _on_question_document_history_selected(self, _event=None) -> None:
        if not self.question_output_filename_var or not self.question_output_dir_var:
            return
        target = self.question_output_history_targets.get(
            self.question_output_filename_var.get()
        )
        if target is None:
            return
        self.question_output_filename_var.set(target.name)
        self.question_output_dir_var.set(str(target.parent))

    def _register_obsidian_status_label(self, parent: tk.Misc, row: int) -> None:
        """在某处放一个仓库状态标签，并登记到公共列表里。

        记录问题页、高光页与左侧全局入口共用状态，必须一起刷新。
        """
        label = ttk.Label(
            parent,
            text=OBSIDIAN_STATUS_NOTICE,
            style="Muted.TLabel",
            wraplength=self._px(430),
            justify="left",
        )
        label.grid(row=row, column=1, columnspan=2, sticky="w", pady=(2, 2))
        # 走 getattr 是为了容忍不跑 __init__ 的轻量调用方（测试里用 __new__ 造壳）
        labels = getattr(self, "obsidian_status_labels", None)
        if labels is None:
            labels = self.obsidian_status_labels = []
        labels.append(label)

    def _set_obsidian_status(self, text: str) -> None:
        for label in getattr(self, "obsidian_status_labels", []):
            try:
                if label.winfo_exists():
                    label.configure(text=text)
            except tk.TclError:
                pass

    def _choose_obsidian_vault(self) -> None:
        if not self.obsidian_vault_var:
            return
        current = Path(self.obsidian_vault_var.get().strip()).expanduser()
        path = filedialog.askdirectory(
            parent=self.root,
            title="选择 Obsidian 仓库",
            initialdir=str(current if current.exists() else APP_DIR),
            mustexist=True,
        )
        if path:
            self.obsidian_vault_var.set(path)
            # 不是仓库时，上面这次设置已经给出提示，别再覆盖回一句「请安装插件」
            if (Path(path) / ".obsidian").is_dir():
                self._set_obsidian_status("仓库已选择，请安装并启用插件")

    def _install_obsidian_plugin(self) -> None:
        configured = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        export_vault = find_obsidian_vault(self._highlight_single_index_path()) if self._highlight_single_index_path() else None
        candidates = obsidian_plugin_update_candidates(configured, str(export_vault or ""),
                                                       highlight_vault=configured_highlight_vault(self.config.values))
        window = tk.Toplevel(self.root)
        window.title("安装 / 更新 Obsidian 插件")
        window.transient(self.root)
        window.resizable(True, False)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="插件更新目标仓库").pack(anchor="w")
        target = tk.StringVar(window, candidates[0] if candidates else "")
        combo = ttk.Combobox(frame, textvariable=target, values=candidates, width=65)
        combo.pack(fill="x", pady=8)
        ttk.Label(frame, text="优先带入 Obsidian 标记为打开的仓库；打开多个库时请确认。\n"
                  "这里只更新插件，不会修改记录问题、高光或单条导出的配置。",
                  justify="left").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        def choose():
            current = Path(target.get()).expanduser() if target.get() else APP_DIR
            chosen = filedialog.askdirectory(title="选择要更新插件的 Obsidian 仓库", parent=window,
                                             initialdir=str(current if current.is_dir() else APP_DIR))
            if chosen:
                target.set(chosen)
        def install():
            if self._install_obsidian_plugin_to(target.get().strip()):
                window.destroy()
        ttk.Button(buttons, text="手动选择…", command=choose).pack(side="left")
        ttk.Button(buttons, text="取消", command=window.destroy).pack(side="right")
        ttk.Button(buttons, text="确认更新", command=install).pack(side="right", padx=8)
        window.grab_set()
        combo.focus_set()

    def _install_obsidian_plugin_to(self, vault_text: str) -> bool:
        if not vault_text:
            messagebox.showerror(APP_NAME, "请先选择 Obsidian 仓库。")
            return False
        # 这个按钮只做「把源文件原样复制过去」：源没更新时点多少次都不会有任何变化
        # （copy2 连时间戳都照搬），用户没法从文件看出到底更新成功没有。所以把
        # 「原来哪一版 → 现在哪一版」写进提示，两版相同时直接说明没有新内容。
        previous = ObsidianBridgeClient.installed_plugin_version(vault_text)
        try:
            destination = ObsidianBridgeClient.install(vault_text)
        except (OSError, ObsidianBridgeError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return False
        installed = ObsidianBridgeClient.source_plugin_version()
        if installed and installed == previous:
            summary = f"仓库里已经是 {installed}，源文件没有更新的版本。"
        elif installed and previous:
            summary = f"插件已从 {previous} 更新到 {installed}。"
        elif installed:
            summary = f"插件已安装为 {installed}。"
        else:
            summary = "插件文件已复制。"
        status = f"{summary}目标：{vault_text}；请在 Obsidian 中重新加载插件"
        if vault_text == configured_highlight_vault(self.config.values):
            self._set_highlight_status(status)
        if self.obsidian_vault_var and vault_text == self.obsidian_vault_var.get().strip():
            self._set_obsidian_status(status)
        self._set_transient_status(status)
        messagebox.showinfo(
            APP_NAME,
            f"{summary}\n\n已复制到：\n{destination}\n\n"
            "复制到插件目录不等于已经生效：请在 Obsidian 的社区插件页面启用 "
            "Noting Helper Bridge；如果原本已经启用，请先关闭再开启该插件，"
            "或重启 Obsidian，否则它仍在运行旧代码。",
        )

        return True

    def _check_obsidian_bridge(self) -> None:
        mode_var = getattr(self, "mode_var", None)
        if mode_var and PAGE_LABELS.get(mode_var.get()) == MODE_HIGHLIGHT:
            self._check_highlight_bridge()
            return
        vault_text = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        if not vault_text:
            messagebox.showerror(APP_NAME, "请先选择 Obsidian 仓库。")
            return
        self._set_obsidian_status("正在检测连接…")

        def worker() -> None:
            try:
                vault_name = ObsidianBridgeClient(vault_text).health()
                self.events.put(("obsidian_bridge_ok", vault_name, vault_text))
            except (OSError, ObsidianBridgeError) as exc:
                self.events.put(("obsidian_bridge_error", str(exc), vault_text))

        threading.Thread(
            target=worker, name="obsidian-bridge-check", daemon=True
        ).start()

    def _sync_question_output_preview(self) -> None:
        if not self.question_output_preview_label or not self.question_output_dir_var or not self.question_output_filename_var:
            return
        directory = self.question_output_dir_var.get().strip()
        filename = self.question_output_filename_var.get().strip()
        preview = str(Path(directory).expanduser() / filename) if directory and filename else "（请填写目录和文件名）"
        self.question_output_preview_label.configure(text=f"写入文件预览：{preview}")

    def _build_highlight_page(self, page: tk.Misc) -> None:
        """高光：唯一 JSONL 事件文件、同目录的两份按需 Markdown 导出。"""
        ttk.Label(page, text="高光", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            page,
            text="Obsidian 内的 Markdown / PDF 只绘制颜色，不改源文件。"
                 "实时高光与注释保存在插件内部；需要阅读时再导出 Markdown。",
            style="Muted.TLabel", wraplength=self._px(560), justify="left",
        ).pack(anchor="w", pady=(self._px(4), self._px(12)))

        self.highlight_vault_var = tk.StringVar(value=configured_highlight_vault(self.config.values))
        self._highlight_status_labels = []
        vault_frame = ttk.LabelFrame(
            page, text="Obsidian 仓库", style="Card.TLabelframe",
            padding=(self._px(14), self._px(12)),
        )
        vault_frame.pack(fill="x")
        vault_frame.columnconfigure(1, weight=1)
        ttk.Label(vault_frame, text="仓库路径").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(vault_frame, textvariable=self.highlight_vault_var).grid(
            row=0, column=1, sticky="ew", pady=6
        )
        ttk.Button(vault_frame, text="选择目录…", command=self._choose_highlight_vault).grid(
            row=0, column=2, padx=(8, 0), pady=6
        )
        ttk.Label(vault_frame, text="仅用于高光来源；与记录问题独立，导出目录可位于另一仓库", style="Muted.TLabel").grid(
            row=1, column=1, columnspan=2, sticky="w"
        )
        status = ttk.Label(vault_frame, text="请安装并启用来源仓库的插件", style="Muted.TLabel")
        status.grid(row=2, column=1, columnspan=2, sticky="w")
        self._highlight_status_labels.append(status)
        actions = ttk.Frame(vault_frame)
        actions.grid(row=3, column=1, columnspan=2, sticky="w", pady=6)
        ttk.Button(actions, text="检测高光连接", command=self._check_highlight_bridge).pack(side="left")
        ttk.Button(actions, text="保存高光配置", command=self._save_highlight_settings).pack(side="left", padx=8)

        entry_frame = ttk.LabelFrame(page, text="划选入口", style="Card.TLabelframe",
                                     padding=(self._px(14), self._px(12)))
        entry_frame.pack(fill="x", pady=(self._px(12), 0))
        self.highlight_native_entry_var = tk.BooleanVar(
            value=self.config.values.get("highlight_native_entry_enabled", True) is not False)
        ttk.Checkbutton(entry_frame, text="高光模式下在 Obsidian 内直接显示色盘",
                        variable=self.highlight_native_entry_var).pack(anchor="w")
        ttk.Label(entry_frame,
                  text="启用后只在 Obsidian 中取选区、写盘和显色；记录问题/单词仍使用桌面悬浮窗。关闭后退回桌面高光色盘。",
                  style="Muted.TLabel", wraplength=self._px(500)).pack(anchor="w", pady=(4, 0))

        ttk.Label(page, text="高光数据由 Obsidian 插件内部固定保存；此处只配置按需导出的目录。",
                  style="Muted.TLabel", wraplength=self._px(560)).pack(
                      anchor="w", pady=(self._px(10), 0))

        export_frame = ttk.LabelFrame(page, text="按需导出 Markdown", style="Card.TLabelframe",
                                      padding=(self._px(14), self._px(12)))
        export_frame.pack(fill="x", pady=(self._px(12), 0))
        export_frame.columnconfigure(1, weight=1)
        self.highlight_export_dir_var = tk.StringVar(value=str(resolve_highlight_export_dir(self.config.values)))
        ttk.Label(export_frame, text="导出文件夹").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        ttk.Entry(export_frame, textvariable=self.highlight_export_dir_var).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(export_frame, text="选择目录…", command=self._choose_highlight_export_dir).grid(
            row=0, column=2, padx=(8, 0), pady=6)
        # 分体按钮：左边按「当前档」直接导出，右边 ▾ 换档（选中即导出该档并改按钮文案）。
        # 当前档只在内存里，重启回到「所有 md」——没有持久化的必要。
        self._highlight_export_scope = ""
        # PhotoImage 必须自己留引用：被 GC 之后菜单项和按钮上的色块会变成空白。
        self._highlight_swatch_images: dict[str, tk.PhotoImage] = {}
        self.highlight_export_button = ttk.Button(
            export_frame, text="导出索引与注释（所有 md）", compound="left",
            command=lambda: self._export_highlights(self._highlight_export_scope))
        self.highlight_export_button.grid(row=1, column=1, sticky="w", pady=(4, 0))
        # 选项每次点开时现问插件（哪些颜色真的有高光、各多少条），桌面端不缓存颜色集
        # ——用户添删高光后不必给软件进程通信。
        self.highlight_export_menu_button = ttk.Button(
            export_frame, text="▾", width=3, command=self._open_highlight_export_menu)
        self.highlight_export_menu_button.grid(
            row=1, column=2, sticky="w", padx=(self._px(4), 0), pady=(4, 0))
        self._highlight_export_menu: tk.Menu | None = None
        ttk.Label(export_frame, text="按钮按当前档导出；点 ▾ 换档（所有 md / 某种颜色的 md），选中即导出。目录名不随文案改：不带颜色落到“所有高光/”，选某种颜色落到“高光-<颜色>/”。两档各自生成“索引”和“注释”两棵目录树（索引按文档名一篇，注释按文档名归拢、每条注释一个目录），再次导出同步删除本档过期生成文件，互不清理。",
                  style="Muted.TLabel", wraplength=self._px(490)).grid(
            row=2, column=1, columnspan=3, sticky="w", pady=(4, 0))

        single_frame = ttk.LabelFrame(page, text="单条导出目标（侧栏每张卡片上的导出图标）",
                                       style="Card.TLabelframe",
                                       padding=(self._px(14), self._px(12)))
        single_frame.pack(fill="x", pady=(self._px(12), 0))
        single_frame.columnconfigure(1, weight=1)
        # 和「记录问题」同一套结构：目录用选择器，文件名用可编辑下拉框（列出该目录下
        # 现成的 md）。原先「索引文档」是一整条路径 + asksaveasfilename，选中已存在的
        # 文件会弹系统那句「当前文件已经存在，是否覆盖源文件」——那是「另存为」的语义，
        # 这里要的是「选择」，所以整条路径拆成目录 + 文件名，那个对话框也就不再需要。
        self.highlight_single_index_dir_var = tk.StringVar(
            value=str(self.config.values.get("highlight_single_index_dir", "")))
        self.highlight_single_index_filename_var = tk.StringVar(
            value=str(self.config.values.get("highlight_single_index_filename", "")))
        self.highlight_single_note_var = tk.StringVar(
            value=str(self.config.values.get("highlight_single_note_dir", "")))
        ttk.Label(single_frame, text="索引文档目录").grid(row=0, column=0, sticky="w", padx=(0, 12), pady=6)
        index_dir_entry = ttk.Entry(single_frame, textvariable=self.highlight_single_index_dir_var)
        index_dir_entry.grid(row=0, column=1, sticky="ew", pady=6)
        for event in ("<FocusOut>", "<Return>"):
            index_dir_entry.bind(event, self._apply_highlight_single_export_target)
        ttk.Button(single_frame, text="选择目录…",
                   command=self._choose_highlight_single_index_dir).grid(
            row=0, column=2, padx=(8, 0), pady=6)
        ttk.Label(single_frame, text="索引文档文件名").grid(row=1, column=0, sticky="w", padx=(0, 12), pady=6)
        self.highlight_single_index_history_box = ttk.Combobox(
            single_frame, textvariable=self.highlight_single_index_filename_var, state="normal")
        self.highlight_single_index_history_box.grid(row=1, column=1, sticky="ew", pady=6)
        self.highlight_single_index_history_box.bind(
            "<<ComboboxSelected>>", self._on_highlight_single_index_history_selected)
        for event in ("<FocusOut>", "<Return>"):
            self.highlight_single_index_history_box.bind(
                event, self._apply_highlight_single_export_target)
        self.highlight_single_index_history_targets: dict[str, Path] = {}
        ttk.Label(single_frame, text="注释目录").grid(row=2, column=0, sticky="w", padx=(0, 12), pady=6)
        note_dir_entry = ttk.Entry(single_frame, textvariable=self.highlight_single_note_var)
        note_dir_entry.grid(row=2, column=1, sticky="ew", pady=6)
        for event in ("<FocusOut>", "<Return>"):
            note_dir_entry.bind(event, self._apply_highlight_single_export_target)
        ttk.Button(single_frame, text="选择目录…",
                   command=self._choose_highlight_single_note).grid(
            row=2, column=2, padx=(8, 0), pady=6)
        ttk.Label(single_frame,
                  text="导出仓库由索引目录自动识别，可以与记录问题/高光的来源仓库不同；索引与注释目录须在同一导出仓库。"
                       "侧栏卡片的导出图标只更新这条高光，其余内容原样保留，"
                       "注释写到“注释目录/<注释名>/注释.md”。索引文档不存在时按上面的文件名新建，一级标题就用这个文件名；"
                       "每条记录的标题用注释标题，没设就取原文前几个字。切换索引后自动保存并同步；"
                       "手动输入后按回车或移开焦点生效，请等同步成功后再导出。两项都留空即关闭这个图标的功能。",
                  style="Muted.TLabel", wraplength=self._px(490), justify="left").grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(4, 0))
        # 选了目录就刷新「有哪些现成的 md」；启动时也刷一次，配过的值打开就有可选列表。
        self.highlight_single_index_dir_var.trace_add(
            "write", lambda *_args: self._refresh_highlight_single_index_history())
        self._refresh_highlight_single_index_history()

        color_frame = ttk.LabelFrame(page, text="高光颜色（8 选 3）", style="Card.TLabelframe",
                                      padding=(self._px(14), self._px(12)))
        color_frame.pack(fill="x", pady=(self._px(12), 0))
        self.highlight_active_colors = normalize_highlight_active_colors(
            self.config.values.get("highlight_active_colors"))
        self.highlight_color_buttons: dict[str, tk.Label] = {}
        self.highlight_color_names: dict[str, ttk.Label] = {}
        legend = ttk.Frame(color_frame)
        legend.pack(fill="x")
        for index, (key, (label, color, dot)) in enumerate(HIGHLIGHT_COLOR_DEFS.items()):
            column = index % 4
            row = index // 4
            chip = tk.Label(legend, background=color, width=3, height=1, cursor="hand2",
                            font=("Microsoft YaHei UI", 10, "bold"))
            chip.grid(row=row, column=column * 2, padx=(0, self._px(6)), pady=self._px(4),
                      sticky="w")
            # 点色块切换选中/取消；选满 3 个之后再点第 4 个只提示，不悄悄挤掉一个。
            chip.bind("<Button-1>",
                      lambda _event, color_key=key: self._toggle_highlight_active_color(color_key))
            self.highlight_color_buttons[key] = chip
            # 色名也做成可点的：光让 12px 的小方块可点，第一眼根本看不出那是按钮。
            # 换成经典 tk.Label 而不是 ttk.Label，是因为选中态要改前景色；底色得自己
            # 传，取的就是 Card.TLabelframe 那一档（front/theme.py 里 = bg_primary）。
            name_label = tk.Label(legend, text=f"{label} {dot}", cursor="hand2",
                                  background=self._token("bg_primary"),
                                  foreground=self._token("text_normal"))
            name_label.grid(row=row, column=column * 2 + 1, padx=(0, self._px(16)),
                            pady=self._px(4), sticky="w")
            name_label.bind("<Button-1>",
                            lambda _event, color_key=key: self._toggle_highlight_active_color(color_key))
            self.highlight_color_names[key] = name_label
        self._refresh_highlight_color_buttons()
        ttk.Label(color_frame,
                  text="点击色块或色名选择／取消（蓝圈打勾的是已选，此按钮上最多留 3 种）。"
                       "这 3 种会摆进划选后弹出的悬浮窗，也是点已有高光时菜单里「切换颜色」的那 3 种；"
                       "导出时目录名用颜色名，索引标题里带同色圆点。",
                  style="Muted.TLabel", wraplength=self._px(490), justify="left").pack(
                      anchor="w", pady=(self._px(6), 0))

        cleanup_frame = ttk.LabelFrame(page, text="清理高光", style="Card.TLabelframe",
                                       padding=(self._px(14), self._px(12)))
        cleanup_frame.pack(fill="x", pady=(self._px(12), 0))
        ttk.Label(cleanup_frame,
                  text="先确认当前有效数量，再从 JSONL 清空并立即移除已打开 Markdown/PDF 的颜色。"
                       "不改来源文档；已导出的 Markdown 是独立快照。",
                  style="Muted.TLabel", wraplength=self._px(490), justify="left").pack(anchor="w")
        ttk.Button(cleanup_frame, text="清空所有高光与注释…",
                   command=self._clear_all_highlights).pack(anchor="w", pady=(self._px(8), 0))
        self.highlight_vault_var.trace_add("write", self._on_highlight_vault_changed)

    def _set_highlight_status(self, text: str) -> None:
        for label in getattr(self, "_highlight_status_labels", []):
            if label.winfo_exists():
                label.configure(text=text)

    def _choose_highlight_vault(self) -> None:
        current = self.highlight_vault_var.get().strip()
        chosen = filedialog.askdirectory(parent=self.root, title="选择高光来源仓库",
                                         initialdir=current if current and Path(current).is_dir() else str(APP_DIR))
        if chosen:
            self.highlight_vault_var.set(chosen)

    def _highlight_ui_values(self) -> dict:
        directory = self.highlight_single_index_dir_var.get().strip()
        filename = self.highlight_single_index_filename_var.get().strip()
        if filename and not filename.lower().endswith(".md"):
            filename += ".md"
        return {
            "highlight_export_dir": self.highlight_export_dir_var.get().strip(),
            "highlight_single_index_dir": directory,
            "highlight_single_index_filename": filename,
            "highlight_single_index_path": str(Path(directory).expanduser() / filename) if directory and filename else "",
            "highlight_single_note_dir": self.highlight_single_note_var.get().strip(),
            "highlight_active_colors": list(self.highlight_active_colors),
            "highlight_native_entry_enabled": self.highlight_native_entry_var.get(),
        }

    def _load_highlight_ui_values(self) -> None:
        v = self.config.values
        for attr, key in (("highlight_export_dir_var", "highlight_export_dir"),
                          ("highlight_single_index_dir_var", "highlight_single_index_dir"),
                          ("highlight_single_index_filename_var", "highlight_single_index_filename"),
                          ("highlight_single_note_var", "highlight_single_note_dir")):
            value = str(v.get(key, ""))
            if key == "highlight_export_dir" and not value:
                value = str(resolve_highlight_export_dir(v))
            getattr(self, attr).set(value)
        self.highlight_active_colors = normalize_highlight_active_colors(v.get("highlight_active_colors"))
        self.highlight_native_entry_var.set(v.get("highlight_native_entry_enabled", True) is not False)
        self._refresh_highlight_color_buttons()
        self._refresh_highlight_single_index_history()
        self._highlight_export_scope = ""
        button = getattr(self, "highlight_export_menu_button", None)
        if button is not None:
            button.state(["!disabled"])
        self._refresh_highlight_export_button()

    def _on_highlight_vault_changed(self, *_args) -> None:
        vault = self.highlight_vault_var.get().strip()
        old_vault = configured_highlight_vault(self.config.values)
        if vault and not (Path(vault).expanduser() / ".obsidian").is_dir():
            self._set_highlight_status("高光仓库缺少 .obsidian，尚未切换")
            return
        if _normalized_path_key(vault) == _normalized_path_key(old_vault):
            return
        previous = deepcopy(self.config.values)
        profiles = deepcopy(previous.get("highlight_vault_profiles") or {})
        if old_vault:
            profiles[_normalized_path_key(old_vault)] = self._highlight_ui_values()
        defaults = {key: deepcopy(AppConfig.DEFAULTS.get(key, "")) for key in HIGHLIGHT_PROFILE_KEYS}
        selected = profiles.get(_normalized_path_key(vault), {}) if vault else {}
        self.config.values.update(defaults)
        self.config.values.update({k: deepcopy(selected[k]) for k in HIGHLIGHT_PROFILE_KEYS if k in selected})
        self.config.values["highlight_vault_path"] = str(Path(vault).expanduser().resolve()) if vault else ""
        self.config.values["highlight_vault_profiles"] = profiles
        try:
            self.config.save()
        except OSError as exc:
            self.config.values.clear()
            self.config.values.update(previous)
            self._set_highlight_status(f"高光仓库未切换：{exc}")
            return
        self._highlight_single_export_synced = None
        self._highlight_active_colors_synced = None
        self._native_highlight_ready = False
        self.highlight_source_pending = False
        self.highlight_source_context = None
        self.highlight_source_revision = -1
        self.highlight_requested_key = None
        self._load_highlight_ui_values()
        self._set_highlight_status("高光仓库已切换，正在同步；记录问题配置未改变")
        self._start_highlight_entry_sync(previous_vault=old_vault)
        self._start_highlight_single_export_sync(notify=False)
        self._start_highlight_active_colors_sync(notify=False)

    def _save_highlight_settings(self) -> bool:
        vault = configured_highlight_vault(self.config.values)
        if _normalized_path_key(self.highlight_vault_var.get().strip()) != _normalized_path_key(vault):
            self._set_highlight_status("界面的高光仓库路径无效或尚未保存，不能写入旧仓库配置")
            return False
        if not vault or not (Path(vault) / ".obsidian").is_dir():
            self._set_highlight_status("请先选择有效的高光来源仓库")
            return False
        updates = self._highlight_ui_values()
        directory = updates["highlight_single_index_dir"]
        filename = updates["highlight_single_index_filename"]
        index = updates["highlight_single_index_path"]
        notes = updates["highlight_single_note_dir"]
        previous = deepcopy(self.config.values)
        try:
            if (directory and (not filename or Path(filename).name != filename)) or (filename and not directory) or bool(index) != bool(notes):
                raise ConfigError("请完整填写单条导出目录、文件名和注释目录，或全部留空。")
            if index:
                highlight_single_export_target(vault, index, notes)
            if updates["highlight_export_dir"]:
                highlight_export_target(vault, updates["highlight_export_dir"])
            colors = updates["highlight_active_colors"]
            if len(colors) != HIGHLIGHT_ACTIVE_COLOR_COUNT or len(set(colors)) != len(colors) or any(c not in HIGHLIGHT_COLOR_DEFS for c in colors):
                raise ConfigError("请选择恰好三个不同的高光颜色。")
            self.config.values.update(updates)
            if hasattr(self, "capture_mode"):
                self.config.values["mode"] = self.capture_mode
            profiles = deepcopy(self.config.values.get("highlight_vault_profiles") or {})
            profiles[_normalized_path_key(vault)] = deepcopy(updates)
            self.config.values["highlight_vault_profiles"] = profiles
            self.config.save()
        except (OSError, ConfigError, ObsidianBridgeError) as exc:
            self.config.values.clear()
            self.config.values.update(previous)
            self._set_highlight_status(f"高光配置未保存：{exc}")
            return False
        self.highlight_single_index_filename_var.set(filename)
        self._set_highlight_status("高光配置已保存，正在同步")
        self._start_highlight_single_export_sync(notify=True)
        self._start_highlight_active_colors_sync(notify=True)
        self._start_highlight_entry_sync()
        return True

    def _check_highlight_bridge(self) -> None:
        vault = configured_highlight_vault(self.config.values)
        self._set_highlight_status("正在检测高光连接…")
        def worker():
            try:
                name = ObsidianBridgeClient(vault).health() if vault else ""
                text = f"高光连接成功：{name}" if name else "请先选择高光来源仓库"
            except (OSError, ObsidianBridgeError) as exc:
                text = f"高光连接失败：{exc}"
            self.events.put(("highlight_bridge_status", vault, text))
        threading.Thread(target=worker, name="highlight-bridge-check", daemon=True).start()

    def _choose_highlight_export_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.root, title="选择高光导出目录",
                                       initialdir=str(APP_DIR))
        if path:
            self.highlight_export_dir_var.set(path)

    def _choose_highlight_single_index_dir(self) -> None:
        """选单条导出的索引文档所在目录（文件名另有一栏下拉框）。

        这里原来调的是 `asksaveasfilename`——那是「另存为」对话框，选中一个已存在的
        md 会弹系统原生的「当前文件已存在，是否覆盖」。语义根本不对，而且它是全项目
        唯一一处。拆成目录 + 文件名之后这个函数连存在都不需要了。
        """
        initial = self.highlight_single_index_dir_var.get().strip() or str(APP_DIR)
        path = filedialog.askdirectory(parent=self.root, title="选择索引文档所在目录",
                                       initialdir=initial)
        if path:
            self.highlight_single_index_dir_var.set(path)
            self._apply_highlight_single_export_target()

    def _refresh_highlight_single_index_history(self) -> None:
        """列出索引文档目录下现成的 md，供下拉框挑。仿记录问题的同名方法。"""
        box = getattr(self, "highlight_single_index_history_box", None)
        directory_text = (
            self.highlight_single_index_dir_var.get().strip()
            if getattr(self, "highlight_single_index_dir_var", None) else ""
        )
        if not box or not directory_text:
            self.highlight_single_index_history_targets = {}
            if box:
                box.configure(values=())
            return
        try:
            directory = Path(directory_text).expanduser().resolve()
            files = [path for path in directory.glob("*.md") if path.is_file()]
            files.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            targets = {path.name: path for path in files}
        except (OSError, ValueError):
            targets = {}
        self.highlight_single_index_history_targets = targets
        box.configure(values=list(targets))

    def _on_highlight_single_index_history_selected(self, _event=None) -> None:
        target = self.highlight_single_index_history_targets.get(
            self.highlight_single_index_filename_var.get()
        )
        if target is None:
            return
        self.highlight_single_index_filename_var.set(target.name)
        self.highlight_single_index_dir_var.set(str(target.parent))
        self._apply_highlight_single_export_target()

    def _apply_highlight_single_export_target(self, _event=None) -> bool:
        """Commit only the export target, without saving unrelated settings pages."""
        directory = self.highlight_single_index_dir_var.get().strip()
        filename = self.highlight_single_index_filename_var.get().strip()
        note_dir = self.highlight_single_note_var.get().strip()
        vault = configured_highlight_vault(self.config.values)
        if filename and not filename.lower().endswith(".md"):
            filename += ".md"
        try:
            if directory and (not filename or Path(filename).name != filename
                              or filename in {".", ".."}):
                raise ConfigError("请填写不含文件夹分隔符的索引文档文件名。")
            index_path = str(Path(directory).expanduser() / filename) if directory and filename else ""
            if bool(index_path) != bool(note_dir) or (not directory and filename):
                raise ConfigError("请完整填写索引文档目录、文件名和注释目录，或全部留空。")
            if index_path:
                highlight_single_export_target(vault, index_path, note_dir)
            updates = {
                "highlight_single_index_dir": str(Path(directory).expanduser()) if directory else "",
                "highlight_single_index_filename": filename,
                "highlight_single_index_path": index_path,
                "highlight_single_note_dir": str(Path(note_dir).expanduser()) if note_dir else "",
            }
            if all(self.config.values.get(key, "") == value for key, value in updates.items()):
                return True
            previous = dict(self.config.values)
            self.config.values.update(updates)
            try:
                self.config.save()
            except OSError:
                self.config.values.clear()
                self.config.values.update(previous)
                raise
        except (OSError, ValueError, ConfigError, ObsidianBridgeError) as exc:
            self._set_transient_status(f"单条导出目标未切换：{exc}")
            return False
        self.highlight_single_index_filename_var.set(filename)
        self._set_transient_status("单条导出目标已保存，正在同步到 Obsidian；同步成功后再导出。")
        self._start_highlight_single_export_sync(notify=True)
        return True

    def _toggle_highlight_active_color(self, key: str) -> None:
        """配置页点色块：8 选 3。选满之后再点第 4 个只提示，不挤掉已选的。"""
        current = list(self.highlight_active_colors)
        if key in current:
            current.remove(key)
        elif len(current) < HIGHLIGHT_ACTIVE_COLOR_COUNT:
            current.append(key)
        else:
            self._set_transient_status(
                f"最多选 {HIGHLIGHT_ACTIVE_COLOR_COUNT} 种颜色，先点掉一个再选。")
            return
        self.highlight_active_colors = current
        self._refresh_highlight_color_buttons()

    def _refresh_highlight_color_buttons(self) -> None:
        """把「哪几个已选」画出来。

        选中的色块上直接打一个勾（颜色按底色深浅取黑或白），而不是只描一圈边框——
        描边在深色主题和低对比度颜色上几乎看不出来，用户会以为点不动。没选中的留空，
        免得 8 个格子里全是符号、反而分不清哪个是选中的。
        """
        for key, chip in getattr(self, "highlight_color_buttons", {}).items():
            selected = key in self.highlight_active_colors
            chip.configure(
                text="✓" if selected else "",
                foreground=HIGHLIGHT_SWATCH_TEXT_COLORS.get(key, "#ffffff"),
                relief="solid" if selected else "flat",
                borderwidth=2 if selected else 0)
            name_label = getattr(self, "highlight_color_names", {}).get(key)
            if name_label is not None:
                label, _color, dot = HIGHLIGHT_COLOR_DEFS[key]
                name_label.configure(
                    text=f"{'✓' if selected else '·'} {label} {dot}",
                    foreground=self._token("text_normal" if selected else "text_faint"))

    def _swatch_image(self, key: str) -> tk.PhotoImage:
        """菜单项和按钮上的颜色小方块。

        不用 emoji 圆点：Tk 在 Windows 上对 emoji 的渲染没保证，品红那个 🩷（U+1FA77，
        非 BMP）基本必碎，几种颜色长得一样的话反而认不出。画一张纯色图片最稳。
        """
        cached = self._highlight_swatch_images.get(key)
        if cached is not None:
            return cached
        size = self._px(12)
        image = tk.PhotoImage(width=size, height=size)
        image.put(HIGHLIGHT_COLOR_DEFS[key][1], to=(0, 0, size, size))
        self._highlight_swatch_images[key] = image
        return image

    def _choose_highlight_single_note(self) -> None:
        initial = self.highlight_single_note_var.get().strip() or str(APP_DIR)
        path = filedialog.askdirectory(parent=self.root, title="选择单条导出的注释目录",
                                       initialdir=initial)
        if path:
            self.highlight_single_note_var.set(path)
            self._apply_highlight_single_export_target()

    def _open_highlight_export_menu(self) -> None:
        """点「按颜色导出 ▾」：先问插件有哪些颜色、各多少条，再弹菜单。

        走后台线程 + events 队列，UI 线程不阻塞在 HTTP 上——这是整个桥接的既定
        分工（见 `_process_events`），也是「开窗卡顿」那一轮定下来的。
        """
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            self._set_transient_status("请先选择 Obsidian 仓库。")
            messagebox.showwarning(APP_NAME, "请先在「Obsidian」中选出仓库目录。")
            return
        button = getattr(self, "highlight_export_menu_button", None)
        if button is not None:
            button.state(["disabled"])
        self._set_transient_status("正在读取高光颜色…")

        def worker() -> None:
            try:
                options = ObsidianBridgeClient(vault).highlight_export_options()
                self.events.put(("highlight_export_options", vault, options, ""))
            except (OSError, ObsidianBridgeError) as exc:
                self.events.put(("highlight_export_options", vault, {}, str(exc)))

        threading.Thread(target=worker, daemon=True, name="highlight-export-options").start()

    def _refresh_highlight_export_button(self) -> None:
        """按钮文案跟着当前档走：所有档写明「所有 md」，颜色档配一块同色小方块。"""
        button = getattr(self, "highlight_export_button", None)
        if button is None or not button.winfo_exists():
            return
        scope = getattr(self, "_highlight_export_scope", "")
        if scope:
            button.configure(text="导出索引与注释（md）", image=self._swatch_image(scope))
        else:
            button.configure(text="导出索引与注释（所有 md）", image="")

    def _select_highlight_export_scope(self, scope: str) -> None:
        """换档：记住当前档、改按钮文案，并立即按这一档导出。"""
        self._highlight_export_scope = scope
        self._refresh_highlight_export_button()
        self._export_highlights(scope)

    def _show_highlight_export_menu(self, vault: str, options: dict) -> None:
        button = getattr(self, "highlight_export_menu_button", None)
        if button is None or not button.winfo_exists():
            return
        menu = tk.Menu(self.root, tearoff=False)
        colors = options.get("colors") if isinstance(options, dict) else None
        total = int(options.get("total", 0) or 0) if isinstance(options, dict) else 0
        if total <= 0:
            menu.add_command(label="没有可导出的高光", state="disabled")
        else:
            menu.add_command(label=f"所有 md ({total})",
                             command=lambda: self._select_highlight_export_scope(""))
            available = [item for item in (colors or []) if int(item.get("count", 0) or 0) > 0]
            if available:
                menu.add_separator()
            for item in available:
                key = str(item.get("key", ""))
                if not key:
                    continue
                # 颜色用色块表示，不写字；闭包必须用默认参数绑住 key，否则整列菜单项
                # 都指向最后一项。
                menu.add_command(image=self._swatch_image(key), compound="left",
                                 label=f"md ({int(item.get('count', 0) or 0)})",
                                 command=lambda color=key: self._select_highlight_export_scope(color))
        # 菜单必须留引用：Tk 的 Menu 被 GC 掉之后弹出的那个窗口会立刻消失。
        self._highlight_export_menu = menu
        try:
            menu.tk_popup(button.winfo_rootx(),
                          button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _export_highlights(self, scope: str = "") -> None:
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            self._set_transient_status("请先选择 Obsidian 仓库。")
            return
        absolute = self.highlight_export_dir_var.get().strip() or str(
            resolve_highlight_export_dir({**self.config.values, "highlight_export_dir": ""}))
        try:
            target = highlight_export_target(vault, absolute)
        except (OSError, ObsidianBridgeError) as exc:
            self._set_transient_status(str(exc))
            return
        label = f"高光-{HIGHLIGHT_COLOR_LABELS.get(scope, scope)}" if scope else "所有高光"
        self._set_transient_status(f"正在导出{label}文档…")

        def worker() -> None:
            try:
                kwargs = {"vault_path": target["vault_path"]} if target.get("vault_path") else {}
                result = ObsidianBridgeClient(vault).export_highlights(target["directory"], scope, **kwargs)
                self.events.put(("highlight_admin_result", absolute, result, "", label, vault))
            except (OSError, ObsidianBridgeError) as exc:
                self.events.put(("highlight_admin_result", absolute, {}, str(exc), label, vault))

        threading.Thread(target=worker, daemon=True, name="highlight-export").start()

    def _clear_all_highlights(self) -> None:
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            self._set_transient_status("请先选择 Obsidian 仓库。")
            return
        self._set_transient_status("正在核对当前有效高光数量…")
        def worker() -> None:
            try:
                diagnostics = ObsidianBridgeClient(vault).highlight_diagnostics()
                count = diagnostics.get("active_highlights")
                if not isinstance(count, int) or count < 0:
                    raise ObsidianBridgeError("插件版本不支持同步清空，请先更新并重载桥接插件。")
                self.events.put(("highlight_reset_preview", vault, count, ""))
            except (OSError, ObsidianBridgeError) as exc:
                self.events.put(("highlight_reset_preview", vault, 0, str(exc)))
        threading.Thread(target=worker, daemon=True, name="highlight-reset-check").start()

    def _confirm_highlight_reset(self, vault: str, count: int) -> None:
        if not messagebox.askyesno(
            APP_NAME, f"即将清空当前仓库全部 {count} 条高光及注释。\n"
                      "Markdown/PDF 来源文件不改；此操作不可撤销。继续吗？",
            parent=self.root,
        ):
            self._set_transient_status("已取消清空高光。")
            return
        self._set_transient_status("正在清空 JSONL 并刷新高光显示…")
        def worker() -> None:
            try:
                result = ObsidianBridgeClient(vault).reset_highlights(count)
                self.events.put(("highlight_reset_done", result, ""))
            except (OSError, ObsidianBridgeError) as exc:
                self.events.put(("highlight_reset_done", {}, str(exc)))
        threading.Thread(target=worker, daemon=True, name="highlight-reset").start()

    def _on_mode_changed(self, _event=None) -> None:
        page = PAGE_LABELS.get(self.mode_var.get(), MODE_TRANSLATE) if self.mode_var else MODE_TRANSLATE
        if page in {MODE_TRANSLATE, MODE_QUESTION, MODE_HIGHLIGHT}:
            if page != getattr(self, "capture_mode", None):
                self.capture_revision = getattr(self, "capture_revision", 0) + 1
                self.pending_capture = None
                self._hide_float_entry()
            self.capture_mode = page
            entry = getattr(self, "float_entry", None)
            if entry is not None:
                entry.set_icon(FLOAT_ENTRY_ICONS.get(page, "pen-line"))
                entry.set_active_mode(page)
        for key, frame in self.page_frames.items():
            if key == page:
                frame.tkraise()
        self._refresh_nav()
        self._sync_status()
        if hasattr(self, "_native_highlight_sync_lock"):
            self._start_highlight_entry_sync()

    def _copy_output_dir(self) -> None:
        value = self.output_dir_var.get().strip()
        if not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.root.update()

    def _choose_dictionary_file(self) -> None:
        current = Path(self.dictionary_path_var.get().strip()).expanduser()
        initial_directory = current.parent if current.parent.exists() else DATA_DIR
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择本地词典 CSV",
            initialdir=str(initial_directory),
            filetypes=(("CSV 词典", "*.csv"), ("所有文件", "*.*")),
        )
        if path:
            self.dictionary_check_revision += 1
            self.dictionary_path_var.set(path)
            if self.dictionary_check_button:
                self.dictionary_check_button.state(["!disabled"])
            if self.dictionary_status_label:
                self.dictionary_status_label.configure(text="路径已更改，请检查词典")

    def _start_dictionary_check(self, quiet: bool = False) -> None:
        path_text = self.dictionary_path_var.get().strip()
        if not path_text:
            if self.dictionary_status_label:
                self.dictionary_status_label.configure(text="请先填写词典文件路径")
            return
        self.dictionary_check_revision += 1
        revision = self.dictionary_check_revision
        dictionary = LocalDictionary(path_text)
        if self.dictionary_check_button:
            self.dictionary_check_button.state(["disabled"])
        if self.dictionary_status_label:
            self.dictionary_status_label.configure(text="正在检查词典和索引…")

        def worker() -> None:
            try:
                status = dictionary.check()
                self.events.put(("dictionary_check_ok", revision, dictionary, status, quiet))
            except (DictionaryError, OSError, sqlite3.Error) as exc:
                self.events.put(("dictionary_check_error", revision, str(exc), quiet))

        threading.Thread(target=worker, name="dictionary-check", daemon=True).start()

    def _show_dictionary_check_result(
        self,
        revision: int,
        dictionary: LocalDictionary,
        status: DictionaryStatus,
        _quiet: bool,
    ) -> None:
        if revision != self.dictionary_check_revision:
            return
        self.local_dictionary = dictionary
        if self.dictionary_check_button:
            self.dictionary_check_button.state(["!disabled"])
        if self.dictionary_status_label:
            action = "，索引已更新" if status.rebuilt else ""
            self.dictionary_status_label.configure(
                text=f"词典可用：{status.entry_count:,} 个词条{action}"
            )

    def _show_dictionary_check_error(self, revision: int, message: str, quiet: bool) -> None:
        if revision != self.dictionary_check_revision:
            return
        if self.dictionary_check_button:
            self.dictionary_check_button.state(["!disabled"])
        if self.dictionary_status_label:
            self.dictionary_status_label.configure(text=message)
        if not quiet:
            messagebox.showerror(APP_NAME, message)

    def _sync_output_preview(self) -> None:
        if not self.output_preview_label:
            return
        directory = self.output_dir_var.get().strip()
        filename = self.output_filename_var.get().strip()
        preview = str(Path(directory).expanduser() / filename) if directory and filename else "（请填写目录和文件名）"
        self.output_preview_label.configure(text=f"写入文件预览：{preview}")

    def _on_output_format_changed(self, _event=None) -> None:
        extension = "." + OUTPUT_FORMAT_LABELS.get(self.output_format_var.get(), "md")
        filename = self.output_filename_var.get().strip()
        if not filename:
            filename = "我的单词库"
        stem = Path(filename).stem or "我的单词库"
        self.output_filename_var.set(stem + extension)
        self._sync_output_preview()

    def _open_stats_window(self) -> None:
        if self.stats_window and self.stats_window.winfo_exists():
            self.stats_window.deiconify()
            self.stats_window.lift()
            self.stats_window.focus_force()
            self._refresh_stats_tree()
            return

        window = tk.Toplevel(self.root)
        self.stats_window = window
        window.title("统计单词库")
        window.geometry(self._geo(680, 620))
        window.minsize(*(self._px(v) for v in (580, 460)))
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_stats_window)
        # 窗口本体也要跟着主题：body 只占中间那块，四周是窗口自己的底，不设就露白边
        window.configure(background=self._token("bg_primary"))
        self._darken_titlebar(window)

        # 和「免翻译词表」「记录设置」一样是一块白面，标题用主题的 Title 级字号。
        # 这里原先是米黄底 + 硬编码 18pt 字体，是唯一一个没跟上主题的窗口。
        outer = ttk.Frame(window, padding=self._px(20))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="统计单词库", style="Title.TLabel").pack(anchor="w")
        self.stats_summary = ttk.Label(outer, text="", style="Muted.TLabel")
        self.stats_summary.pack(anchor="w", pady=(self._px(5), self._px(14)))

        filters = ttk.Frame(outer)
        filters.pack(fill="x", pady=(0, self._px(12)))
        ttk.Label(filters, text="搜索").pack(side="left", padx=(0, 8))
        self.stats_search_var = tk.StringVar()
        ttk.Entry(filters, textvariable=self.stats_search_var).pack(side="left", fill="x", expand=True)
        ttk.Label(filters, text="时间").pack(side="left", padx=(14, 8))
        self.stats_range_var = tk.StringVar(value="全部")
        range_box = ttk.Combobox(
            filters,
            textvariable=self.stats_range_var,
            values=("全部", "今天", "近 7 天", "近 30 天", "更早"),
            state="readonly",
            width=10,
        )
        range_box.pack(side="left")
        self.stats_search_var.trace_add("write", lambda *_args: self._refresh_stats_tree())
        range_box.bind("<<ComboboxSelected>>", lambda _event: self._refresh_stats_tree())

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        self.stats_tree = ttk.Treeview(
            table_frame,
            columns=("word", "added", "source"),
            show="headings",
            selectmode="browse",
        )
        self.stats_tree.heading("word", text="词汇")
        self.stats_tree.heading("added", text="首次加入时间")
        self.stats_tree.heading("source", text="文档数")
        self.stats_tree.column("word", width=270, anchor="w")
        self.stats_tree.column("added", width=190, anchor="center")
        self.stats_tree.column("source", width=90, anchor="center")
        # 分组行是日期表头，用中性灰底 + 淡文字，原来是米黄底配棕色字。
        # 词条行跟表格底色一致，行与行的区分交给上面的分组带。
        group_style = {
            "background": self._token("bg_secondary"),
            "foreground": self._token("text_muted"),
        }
        if ui_theme is not None:
            group_style["font"] = ui_theme.ui_font(10, "bold")
        self.stats_tree.tag_configure("group", **group_style)
        self.stats_tree.tag_configure(
            "entry", background=self._token("bg_primary"),
            foreground=self._token("text_normal"),
        )
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.stats_tree.yview)
        self.stats_tree.configure(yscrollcommand=scrollbar.set)
        self.stats_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 表格右边那列是滚动条，动作按钮单独一行，和免翻译词表一致
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(self._px(12), 0))
        ttk.Button(actions, text="关闭", command=self._close_stats_window).pack(side="right")
        self._refresh_stats_tree()

    def _close_stats_window(self) -> None:
        if self.stats_window and self.stats_window.winfo_exists():
            self.stats_window.destroy()
        self.stats_window = None
        self.stats_tree = None
        self.stats_search_var = None
        self.stats_range_var = None
        self.stats_summary = None

    def _refresh_stats_tree(self) -> None:
        if not self.stats_tree or not self.stats_tree.winfo_exists():
            return
        for child in self.stats_tree.get_children():
            self.stats_tree.delete(child)
        search = self.stats_search_var.get() if self.stats_search_var else ""
        try:
            entries = self.word_index.list_vocabulary_entries(search)
        except sqlite3.Error as exc:
            if self.stats_summary:
                self.stats_summary.configure(text=f"读取失败：{exc}")
            return
        now = int(time.time())
        today_start = int(time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1)))
        selected_range = self.stats_range_var.get() if self.stats_range_var else "全部"
        if selected_range == "今天":
            entries = [entry for entry in entries if entry.added_at >= today_start]
        elif selected_range == "近 7 天":
            entries = [entry for entry in entries if entry.added_at >= now - 7 * 86400]
        elif selected_range == "近 30 天":
            entries = [entry for entry in entries if entry.added_at >= now - 30 * 86400]
        elif selected_range == "更早":
            entries = [entry for entry in entries if entry.added_at < now - 30 * 86400]

        groups: dict[str, list[VocabularyEntry]] = {}
        for entry in entries:
            group = time.strftime("%Y-%m-%d", time.localtime(entry.added_at)) if entry.added_at else "时间未知"
            groups.setdefault(group, []).append(entry)
        for group, group_entries in groups.items():
            group_id = f"group-{group.replace('-', '')}"
            self.stats_tree.insert(
                "",
                "end",
                iid=group_id,
                values=(f"{group}  ·  {len(group_entries)} 个", "", ""),
                tags=("group",),
            )
            for index, entry in enumerate(group_entries):
                added = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.added_at)) if entry.added_at else "未知"
                self.stats_tree.insert(
                    "",
                    "end",
                    iid=f"{group_id}-{index}",
                    values=(entry.display_word, added, str(entry.document_count)),
                    tags=("entry",),
                )
        if self.stats_summary:
            self.stats_summary.configure(text=f"共 {len(entries)} 个词汇 · 按加入日期分组")

    def _open_ignore_manager(self) -> None:
        if self.ignore_window and self.ignore_window.winfo_exists():
            self.ignore_window.deiconify()
            self.ignore_window.lift()
            self.ignore_window.focus_force()
            self._refresh_ignore_tree()
            return

        window = tk.Toplevel(self.root)
        self.ignore_window = window
        window.title("免翻译词表")
        window.geometry(self._geo(560, 540))
        window.minsize(*(self._px(v) for v in (500, 420)))
        window.transient(self.root)
        window.protocol("WM_DELETE_WINDOW", self._close_ignore_manager)
        window.configure(background=self._token("bg_primary"))
        self._darken_titlebar(window)

        outer = ttk.Frame(window, padding=self._px(20))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="免翻译词表", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="启用“免翻译”后，划线句子中的该词不会发送给模型；停用后可恢复翻译。",
            style="Muted.TLabel",
            wraplength=510,
            justify="left",
        ).pack(anchor="w", pady=(5, 14))

        add_row = ttk.Frame(outer)
        add_row.pack(fill="x", pady=(0, 10))
        self.ignore_word_var = tk.StringVar()
        entry = ttk.Entry(add_row, textvariable=self.ignore_word_var)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._add_ignore_word())
        ttk.Button(add_row, text="添加免翻译词", command=self._add_ignore_word).pack(
            side="left", padx=(8, 0)
        )

        search_row = ttk.Frame(outer)
        search_row.pack(fill="x", pady=(0, 10))
        ttk.Label(search_row, text="搜索").pack(side="left", padx=(0, 8))
        self.ignore_search_var = tk.StringVar()
        ttk.Entry(search_row, textvariable=self.ignore_search_var).pack(side="left", fill="x", expand=True)
        self.ignore_search_var.trace_add("write", lambda *_args: self._refresh_ignore_tree())

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        self.ignore_tree = ttk.Treeview(
            table_frame,
            columns=("word", "status"),
            show="headings",
            selectmode="browse",
        )
        self.ignore_tree.heading("word", text="单词")
        self.ignore_tree.heading("status", text="当前状态")
        self.ignore_tree.column("word", width=250, anchor="w")
        self.ignore_tree.column("status", width=150, anchor="center")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.ignore_tree.yview)
        self.ignore_tree.configure(yscrollcommand=scrollbar.set)
        self.ignore_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.ignore_tree.bind("<Double-1>", lambda _event: self._toggle_selected_ignore())

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="启用免翻译", command=lambda: self._set_selected_ignore(True)).pack(
            side="left"
        )
        ttk.Button(actions, text="允许翻译", command=lambda: self._set_selected_ignore(False)).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(actions, text="删除", command=self._remove_selected_ignore).pack(
            side="left", padx=(8, 0)
        )
        self.ignore_status = ttk.Label(actions, text="", style="Muted.TLabel")
        self.ignore_status.pack(side="left", padx=(12, 0))
        ttk.Button(actions, text="关闭", command=self._close_ignore_manager).pack(side="right")
        self._refresh_ignore_tree()
        entry.focus_set()

    def _close_ignore_manager(self) -> None:
        if self.ignore_window and self.ignore_window.winfo_exists():
            self.ignore_window.destroy()
        self.ignore_window = None
        self.ignore_tree = None
        self.ignore_search_var = None
        self.ignore_word_var = None
        self.ignore_status = None

    def _refresh_ignore_tree(self) -> None:
        if not self.ignore_tree or not self.ignore_tree.winfo_exists():
            return
        query = self.ignore_search_var.get().strip().casefold() if self.ignore_search_var else ""
        for child in self.ignore_tree.get_children():
            self.ignore_tree.delete(child)
        self.ignore_rows = {}
        try:
            rows = self.word_index.list_ignored_words()
        except sqlite3.Error as exc:
            if self.ignore_status:
                self.ignore_status.configure(text=f"读取失败：{exc}")
            return
        for row in rows:
            normalized = normalize_index_word(row.display_word)
            self.ignore_rows[normalized] = row
            if query and query not in row.display_word.casefold():
                continue
            self.ignore_tree.insert(
                "",
                "end",
                iid=normalized,
                values=(row.display_word, ignore_status_text(row.enabled, row.expires_at)),
            )
        if self.ignore_status:
            self.ignore_status.configure(text=f"共 {len(self.ignore_rows)} 个词")

    def _add_ignore_word(self) -> None:
        if not self.ignore_word_var:
            return
        value = self.ignore_word_var.get().strip()
        try:
            self.word_index.set_ignored_word(value, True)
        except (ValueError, sqlite3.Error) as exc:
            if self.ignore_status:
                self.ignore_status.configure(text=str(exc))
            return
        self.ignore_word_var.set("")
        self._ignore_words_changed()
        self._refresh_ignore_tree()

    def _selected_ignore(self) -> tuple[str, str] | None:
        if not self.ignore_tree:
            return None
        selection = self.ignore_tree.selection()
        if not selection:
            return None
        normalized = str(selection[0])
        row = self.ignore_rows.get(normalized)
        return (normalized, row.display_word) if row else None

    def _set_selected_ignore(self, enabled: bool) -> None:
        selected = self._selected_ignore()
        if not selected:
            if self.ignore_status:
                self.ignore_status.configure(text="请先选择一个单词")
            return
        try:
            self.word_index.set_ignored_word(selected[1], enabled)
        except (ValueError, sqlite3.Error) as exc:
            if self.ignore_status:
                self.ignore_status.configure(text=str(exc))
            return
        self._ignore_words_changed()
        self._refresh_ignore_tree()

    def _toggle_selected_ignore(self) -> None:
        selected = self._selected_ignore()
        if not selected:
            return
        current = self.ignore_rows[selected[0]].enabled
        self._set_selected_ignore(not current)

    def _remove_selected_ignore(self) -> None:
        selected = self._selected_ignore()
        if not selected:
            if self.ignore_status:
                self.ignore_status.configure(text="请先选择一个单词")
            return
        try:
            self.word_index.remove_ignored_word(selected[1])
        except (ValueError, sqlite3.Error) as exc:
            if self.ignore_status:
                self.ignore_status.configure(text=str(exc))
            return
        self._ignore_words_changed()
        self._refresh_ignore_tree()

    def _ignore_words_changed(self, close_popup: bool = True) -> None:
        self.cache.clear()
        self.lookup_revision += 1
        if close_popup and self.lookup_popup and self.lookup_popup.winfo_exists():
            self._close_popup()

    def _set_result_ignore_duration(
        self,
        word: str,
        seconds: int | None,
        status_label: ttk.Label,
    ) -> None:
        try:
            _, expires_at = self.word_index.set_ignored_word_for(word, seconds)
        except (ValueError, sqlite3.Error) as exc:
            if status_label.winfo_exists():
                status_label.configure(text=f"设置失败：{exc}")
            return
        self._ignore_words_changed(close_popup=False)
        self._refresh_ignore_tree()
        if status_label.winfo_exists():
            status_label.configure(text=f"已设置：{ignore_status_text(True, expires_at)}")

    def _prepared_words_for_sentence(self, sentence: str) -> list[dict[str, str]]:
        try:
            enabled = self.word_index.enabled_ignored_words()
        except sqlite3.Error:
            enabled = set()
        try:
            lemma_map = self.local_dictionary.lemmatize_many(extract_words_offline(sentence))
        except DictionaryError:
            lemma_map = None
        return prepare_vocabulary_words(sentence, enabled, lemma_map)

    def _set_api_preset(self, provider: str) -> None:
        preset = API_PRESETS.get(provider)
        if not preset:
            return
        protocol_label = next(
            label for label, value in PROTOCOL_LABELS.items() if value == preset["protocol"]
        )
        json_mode_label = next(
            label for label, value in JSON_MODE_LABELS.items() if value == preset["json_mode"]
        )
        self.protocol_var.set(protocol_label)
        self.base_url_var.set(str(preset["base_url"]))
        self.model_var.set(str(preset["model"]))
        self.json_mode_var.set(json_mode_label)
        self.timeout_var.set(str(preset["timeout"]))

    def _load_question_default_settings(self) -> None:
        source_mode = str(
            self.config.values.get(
                "question_source_link_mode", DEFAULT_QUESTION_SOURCE_LINK_MODE
            )
        )
        supplement_mode = str(
            self.config.values.get(
                "question_supplement_link_mode", DEFAULT_QUESTION_SUPPLEMENT_LINK_MODE
            )
        )
        source_label = question_link_mode_label(source_mode, DEFAULT_QUESTION_SOURCE_LINK_MODE)
        supplement_label = question_link_mode_label(
            supplement_mode, DEFAULT_QUESTION_SUPPLEMENT_LINK_MODE
        )
        try:
            title_level = int(
                self.config.values.get("question_title_level", DEFAULT_QUESTION_TITLE_LEVEL)
            )
        except (TypeError, ValueError):
            title_level = DEFAULT_QUESTION_TITLE_LEVEL
        title_label = next(
            (label for label, value in QUESTION_TITLE_OPTIONS if value == title_level),
            DEFAULT_QUESTION_TITLE_LABEL,
        )
        if self.question_source_default_var is None:
            self.question_source_default_var = tk.StringVar()
        if self.question_supplement_default_var is None:
            self.question_supplement_default_var = tk.StringVar()
        if self.question_title_level_default_var is None:
            self.question_title_level_default_var = tk.StringVar()
        if self.question_obsidian_source_link_var is None:
            self.question_obsidian_source_link_var = tk.BooleanVar()
        self.question_source_default_var.set(source_label)
        self.question_supplement_default_var.set(supplement_label)
        self.question_title_level_default_var.set(title_label)
        self.question_obsidian_source_link_var.set(
            bool(
                self.config.values.get(
                    "question_obsidian_source_link", DEFAULT_QUESTION_OBSIDIAN_SOURCE_LINK
                )
            )
        )

    def _open_question_settings(self) -> None:
        if self.question_settings_window and self.question_settings_window.winfo_exists():
            self.question_settings_window.lift()
            self.question_settings_window.focus_force()
            return
        window = tk.Toplevel(self.root)
        self.question_settings_window = window
        window.title("记录设置")
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        window.configure(background=self._token("bg_primary"))
        self._darken_titlebar(window)

        # 标题 + 一句说明 + 下面的表单，跟「免翻译词表」一个版式；原来只有一张裸表，
        # 顶上什么都没有，一打开就是三行下拉框顶着窗口边。
        body = ttk.Frame(window, padding=self._px(20))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="记录设置", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="这里定的是新建记录时的默认值，单条记录可以在记录问题弹窗里临时改。",
            style="Muted.TLabel",
            wraplength=self._px(430),
            justify="left",
        ).pack(anchor="w", pady=(self._px(5), self._px(14)))

        self._load_question_default_settings()
        mode_values = list(QUESTION_LINK_MODE_LABELS)
        form = ttk.Frame(body)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="原文默认加入方式").grid(row=0, column=0, sticky="w", padx=(0, 14), pady=6)
        ttk.Combobox(form, textvariable=self.question_source_default_var, values=mode_values, state="readonly", width=22).grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="内容补充默认加入方式").grid(row=1, column=0, sticky="w", padx=(0, 14), pady=6)
        ttk.Combobox(form, textvariable=self.question_supplement_default_var, values=mode_values, state="readonly", width=22).grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Label(form, text="默认标题级别").grid(row=2, column=0, sticky="w", padx=(0, 14), pady=6)
        ttk.Combobox(form, textvariable=self.question_title_level_default_var, values=[label for label, _ in QUESTION_TITLE_OPTIONS], state="readonly", width=22).grid(row=2, column=1, sticky="ew", pady=6)
        self._flat_checkbox(
            form,
            text="Obsidian 本地来源默认添加来源链接",
            variable=self.question_obsidian_source_link_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(self._px(10), self._px(4)))
        ttk.Label(
            form,
            text="仅对 Obsidian Vault 内的 Markdown/PDF 等来源生效，网页和外部文件不受影响。",
            style="Muted.TLabel",
            wraplength=self._px(430),
            justify="left",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(0, self._px(6)))

        template = question_dir_template(self.config.values)
        self.question_template_vars = {
            key: tk.StringVar(value=template[key]) for key in QUESTION_DIR_TEMPLATE
        }
        template_frame = ttk.LabelFrame(
            body, text="新仓库默认模板", style="Card.TLabelframe",
            padding=(self._px(12), self._px(10)),
        )
        template_frame.pack(fill="x", pady=(self._px(16), 0))
        template_frame.columnconfigure(1, weight=1)
        template_rows = (
            ("index_dir", "索引文档目录"),
            ("body_dir", "正文库目录"),
            ("materials_dir", "资料库目录"),
            ("index_filename", "索引文档文件名"),
        )
        for row, (key, label) in enumerate(template_rows):
            ttk.Label(template_frame, text=label).grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=4
            )
            ttk.Entry(
                template_frame, textvariable=self.question_template_vars[key]
            ).grid(row=row, column=1, sticky="ew", pady=4)
        next_row = len(template_rows)
        ttk.Label(
            template_frame,
            text=(
                "相对路径按仓库目录拼，也可以直接写绝对路径。"
                "打开一个还没有记录过配置的仓库时，就按这里的路径落地。"
            ),
            style="Muted.TLabel",
            wraplength=self._px(430),
            justify="left",
        ).grid(row=next_row, column=0, columnspan=2, sticky="w", pady=(self._px(4), self._px(6)))
        template_actions = ttk.Frame(template_frame)
        template_actions.grid(row=next_row + 1, column=0, columnspan=2, sticky="w")
        ttk.Button(
            template_actions,
            text="套用到当前仓库",
            command=self._apply_template_to_current_vault,
        ).pack(side="left")
        self.question_template_status_label = ttk.Label(
            template_actions, text="", style="Muted.TLabel"
        )
        self.question_template_status_label.pack(side="left", padx=(self._px(10), 0))
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(self._px(14), 0))
        ttk.Button(actions, text="取消", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="保存", style="Accent.TButton",
                   command=self._save_question_settings).pack(side="right", padx=(0, 8))
        window.grab_set()

    def _apply_template_to_current_vault(self) -> None:
        """把模板落到当前仓库：写进页面上的那三个目录，并把文件夹建出来。"""
        status = self.question_template_status_label
        vault_text = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        if not vault_text:
            if status:
                status.configure(text="先在「记录问题」页选好仓库目录。")
            return
        vault = Path(vault_text).expanduser()
        if not (vault / ".obsidian").is_dir():
            if status:
                status.configure(text="这个目录里没有 .obsidian，先确认它是 Obsidian 仓库。")
            return
        template = normalized_question_dir_template(
            {
                key: variable.get()
                for key, variable in (self.question_template_vars or {}).items()
            }
        )
        try:
            defaults = apply_question_dir_template(template, vault, create=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.question_settings_window)
            return
        self._apply_question_paths(
            defaults["question_output_dir"],
            defaults["question_output_filename"],
            defaults["question_body_dir"],
            defaults["question_materials_dir"],
        )
        if status:
            status.configure(text=f"已套用到 {vault.name}，按「保存」写进配置。")

    def _save_question_settings(self) -> None:
        source_var = self.question_source_default_var
        supplement_var = self.question_supplement_default_var
        title_var = self.question_title_level_default_var
        obsidian_var = self.question_obsidian_source_link_var
        if not source_var or not supplement_var or not title_var or not obsidian_var:
            return
        source_mode = QUESTION_LINK_MODE_LABELS.get(source_var.get())
        supplement_mode = QUESTION_LINK_MODE_LABELS.get(supplement_var.get())
        title_level = dict(QUESTION_TITLE_OPTIONS).get(title_var.get())
        if source_mode is None or supplement_mode is None or title_level is None:
            messagebox.showerror(APP_NAME, "请选择有效的记录默认值。", parent=self.question_settings_window)
            return
        self.config.values["question_source_link_mode"] = QUESTION_RECORD_MODE_INDEPENDENT if source_mode else QUESTION_RECORD_MODE_INLINE
        self.config.values["question_supplement_link_mode"] = QUESTION_RECORD_MODE_INDEPENDENT if supplement_mode else QUESTION_RECORD_MODE_INLINE
        self.config.values["question_title_level"] = title_level
        self.config.values["question_obsidian_source_link"] = bool(obsidian_var.get())
        if self.question_template_vars:
            self.config.values["question_dir_template"] = {
                key: variable.get().strip()
                for key, variable in self.question_template_vars.items()
            }
        try:
            self.config.save()
        except OSError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.question_settings_window)
            return
        if self.question_settings_window and self.question_settings_window.winfo_exists():
            self.question_settings_window.grab_release()
            self.question_settings_window.destroy()
        self.question_settings_window = None

    def _highlight_single_export_payload(self) -> dict[str, str] | None:
        """配置 → 插件要的 Vault 相对路径。没配返回 None，表示「清掉插件里的目标」。"""
        vault = configured_highlight_vault(self.config.values)
        index_text = self._highlight_single_index_path()
        note_text = str(self.config.values.get("highlight_single_note_dir", "")).strip()
        if not vault or not index_text or not note_text:
            return None
        return highlight_single_export_target(vault, index_text, note_text)

    def _highlight_single_index_path(self) -> str:
        """目录 + 文件名拼成完整路径。旧配置只有 path 时也认，回读不用重配一次。"""
        directory = str(self.config.values.get("highlight_single_index_dir", "")).strip()
        filename = str(self.config.values.get("highlight_single_index_filename", "")).strip()
        if directory and filename:
            return str(Path(directory).expanduser() / filename)
        return str(self.config.values.get("highlight_single_index_path", "")).strip()

    def _highlight_active_colors_config(self) -> list[str]:
        return normalize_highlight_active_colors(
            self.config.values.get("highlight_active_colors"))

    def _ensure_highlight_active_colors(self, client: ObsidianBridgeClient,
                                        vault_path: str) -> None:
        """悬浮窗颜色只在真的变过时才推。这条路由每次保存都会走一遍，没必要重复发。"""
        with self._highlight_active_colors_lock:
            current = configured_highlight_vault(self.config.values)
            if not current or Path(current).resolve() != Path(vault_path).resolve():
                return
            colors = self._highlight_active_colors_config()
            identity = (str(Path(vault_path).resolve()).casefold(), tuple(colors))
            if self._highlight_active_colors_synced != identity:
                client.configure_highlight_active_colors(colors)
                self._highlight_active_colors_synced = identity

    def _start_highlight_active_colors_sync(self, *, notify: bool) -> None:
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            return
        def work() -> None:
            try:
                self._ensure_highlight_active_colors(ObsidianBridgeClient(vault), vault)
            except (OSError, ObsidianBridgeError) as exc:
                if notify:
                    self.events.put(("highlight_active_colors_sync", False, str(exc), vault))
            else:
                if notify:
                    self.events.put(("highlight_active_colors_sync", True, "", vault))
        threading.Thread(target=work, name="highlight-active-colors-sync", daemon=True).start()

    def _highlight_single_export_identity(self, vault_path: str,
                                          payload: dict[str, str]) -> tuple[str, tuple]:
        """仓库 + 目标内容一起算身份；任何一项变了都该重推。"""
        return (str(Path(vault_path).resolve()).casefold(),
                tuple(sorted(payload.items())))

    def _highlight_single_export_pending(self) -> bool:
        """本地目标与「已推给插件的那份」是否不一致。只比内存里的身份，不发请求。"""
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            return False
        try:
            payload = self._highlight_single_export_payload() or {
                "index_path": "", "note_dir": ""}
        except ObsidianBridgeError:
            # 路径越界等配置错误在 _save_settings 已经拦过；这里只做重试判断，
            # 每 60ms 跑一趟的循环不能被它掀翻。
            return False
        return self._highlight_single_export_synced != self._highlight_single_export_identity(
            vault, payload)

    def _ensure_highlight_single_export(self, client: ObsidianBridgeClient,
                                        vault_path: str, payload: dict[str, str]) -> None:
        """Serialize bridge writes; the plugin only holds one single-export target."""
        with self._highlight_single_export_lock:
            # A worker may have read an old target before waiting for the lock.
            # Re-read inside the lock so a queued write cannot restore that target.
            current_vault = configured_highlight_vault(self.config.values)
            if not current_vault or Path(current_vault).resolve() != Path(vault_path).resolve():
                return
            payload = self._highlight_single_export_payload() or {
                "index_path": "", "note_dir": ""}
            identity = self._highlight_single_export_identity(vault_path, payload)
            if self._highlight_single_export_synced != identity:
                kwargs = {"vault_path": payload["vault_path"]} if payload.get("vault_path") else {}
                client.configure_highlight_single_export(
                    payload["index_path"], payload["note_dir"], **kwargs)
                self._highlight_single_export_synced = identity

    def _push_highlight_single_export(self, client: ObsidianBridgeClient,
                                      vault_path: str) -> None:
        payload = self._highlight_single_export_payload() or {
            "index_path": "", "note_dir": ""}
        self._ensure_highlight_single_export(client, vault_path, payload)

    def _start_highlight_single_export_sync(self, *, notify: bool) -> None:
        vault = configured_highlight_vault(self.config.values)
        if not vault:
            return
        def work() -> None:
            try:
                self._push_highlight_single_export(ObsidianBridgeClient(vault), vault)
            except (OSError, ConfigError, ObsidianBridgeError) as exc:
                if notify:
                    self.events.put(("highlight_single_export_sync", False, str(exc), vault))
            else:
                if notify:
                    self.events.put(("highlight_single_export_sync", True, "", vault))
        threading.Thread(target=work, name="highlight-single-export-sync", daemon=True).start()

    def _maybe_retry_highlight_single_export(self) -> None:
        """推送失败的兜底重试。

        推给插件的时机原先只有「保存 / 手动检测连接 / 记录一条高光」三处：保存那次赶上
        Obsidian 没开或插件没响应，插件就长期停在旧目标上，而用户看到的只是「导出这一条」
        写进了旧索引——改了目标却不生效。这里兜底重推，直到身份对上为止；失败不弹窗
        （用户主动保存那一次已经明确提示过），免得反复打扰。
        """
        if not hasattr(self, "_highlight_single_export_lock"):
            return
        now = time.monotonic()
        # 钩子挂在 60ms 的事件循环上，而身份比较要碰文件系统（Path.resolve），
        # 所以「看一眼」按 5 秒节流，真要重推再按 15 秒节流。
        if now < getattr(self, "_highlight_single_export_check_at", 0.0):
            return
        self._highlight_single_export_check_at = now + 5.0
        if not self._highlight_single_export_pending():
            return
        if now < getattr(self, "_highlight_single_export_retry_at", 0.0):
            return
        self._highlight_single_export_retry_at = now + 15.0
        self._start_highlight_single_export_sync(notify=False)

    def _start_native_mode_listener(self) -> None:
        if getattr(self, "_native_mode_listener_thread", None) is not None:
            return
        def listen() -> None:
            while not self._native_mode_listener_stop.is_set():
                vault = configured_highlight_vault(self.config.values)
                if not vault:
                    self._native_mode_listener_stop.wait(1.0)
                    continue
                try:
                    result = ObsidianBridgeClient(vault).wait_for_native_mode()
                except (OSError, ObsidianBridgeError):
                    self._native_mode_listener_stop.wait(1.5)
                    continue
                if result.get("mode") in (MODE_QUESTION, MODE_TRANSLATE, MODE_HIGHLIGHT):
                    self.events.put(("native_mode_switch", result["mode"], vault))
        self._native_mode_listener_thread = threading.Thread(
            target=listen, daemon=True, name="obsidian-mode-events")
        self._native_mode_listener_thread.start()

    def _start_highlight_entry_sync(self, previous_vault: str = "") -> None:
        """Mode/config sync happens off the click path; stale results cannot re-enable an old mode."""
        vault = configured_highlight_vault(self.config.values)
        if previous_vault and previous_vault != vault:
            if not hasattr(self, "_retired_highlight_vaults"):
                self._retired_highlight_vaults = set()
            self._retired_highlight_vaults.add(previous_vault)
        self._native_highlight_sync_revision += 1
        revision = self._native_highlight_sync_revision
        enabled = (self.config.values.get("highlight_native_entry_enabled", True) is not False
                   and self.capture_mode == MODE_HIGHLIGHT
                   and getattr(self, "monitoring", True))
        if self.capture_mode == MODE_HIGHLIGHT:
            # Prevent duplicate native/desktop highlighter entry during handoff.
            self._native_highlight_sync_pending = True
            self.capture_revision = getattr(self, "capture_revision", 0) + 1
            self.pending_capture = None
            self._hide_float_entry()
        else:
            # Disabling the old native palette may take time or need retries.
            # Local question/translation capture must remain independent of it.
            self._native_highlight_ready = False
            self._native_highlight_sync_pending = False
        def work() -> None:
            with self._native_highlight_sync_lock:
                # A newer mode/config wins even if an older HTTP request was queued.
                if revision != self._native_highlight_sync_revision:
                    return
                retirement_errors = []
                for retired in list(getattr(self, "_retired_highlight_vaults", set())):
                    if retired == vault:
                        self._retired_highlight_vaults.discard(retired)
                        continue
                    try:
                        ObsidianBridgeClient(retired).configure_highlight_entry(False)
                        self._retired_highlight_vaults.discard(retired)
                    except (OSError, ObsidianBridgeError) as exc:
                        retirement_errors.append(f"旧高光仓库入口尚未关闭：{retired}（{exc}）")
                if not vault:
                    self.events.put(("highlight_entry_sync", revision, False, "; ".join(retirement_errors)))
                    return
                try:
                    client = ObsidianBridgeClient(vault)
                    client.configure_highlight_entry(enabled)
                except (OSError, ObsidianBridgeError) as exc:
                    # A timed-out write may already have enabled the plugin.
                    # Read back ownership before allowing a desktop fallback.
                    observed = None
                    try:
                        health = ObsidianBridgeClient(vault)._request("/health", timeout=2)
                        observed = bool(health.get("native_highlight_entry", False))
                    except (OSError, ObsidianBridgeError):
                        pass
                    self.events.put(("highlight_entry_sync", revision, observed, str(exc)))
                else:
                    self.events.put(("highlight_entry_sync", revision, enabled, "; ".join(retirement_errors)))
        threading.Thread(target=work, name="highlight-entry-sync", daemon=True).start()

    def _retry_highlight_entry_sync(self, revision: int) -> None:
        if revision == getattr(self, "_native_highlight_sync_revision", -1):
            self._start_highlight_entry_sync()

    def _save_settings(self) -> bool:
        directory_text = self.output_dir_var.get().strip()
        filename = self.output_filename_var.get().strip()
        dictionary_path_text = self.dictionary_path_var.get().strip()
        question_directory_text = self.question_output_dir_var.get().strip() if self.question_output_dir_var else ""
        question_filename = self.question_output_filename_var.get().strip() if self.question_output_filename_var else ""
        question_body_directory_text = self.question_body_dir_var.get().strip() if self.question_body_dir_var else ""
        question_materials_directory_text = self.question_materials_dir_var.get().strip() if self.question_materials_dir_var else ""
        question_record_mode_label = self.question_record_mode_var.get() if self.question_record_mode_var else next(iter(QUESTION_RECORD_MODE_LABELS))
        obsidian_vault_text = self.obsidian_vault_var.get().strip() if self.obsidian_vault_var else ""
        if not directory_text:
            messagebox.showerror(APP_NAME, "单词库目录不能为空。")
            return False
        if not filename or filename in {".", ".."} or Path(filename).name != filename:
            messagebox.showerror(APP_NAME, "请输入不含文件夹分隔符的文档文件名。")
            return False
        if not question_directory_text:
            messagebox.showerror(APP_NAME, "问题记录目录不能为空。")
            return False
        if not question_filename or question_filename in {".", ".."} or Path(question_filename).name != question_filename:
            messagebox.showerror(APP_NAME, "请输入不含文件夹分隔符的问题记录文件名。")
            return False
        if not question_body_directory_text:
            messagebox.showerror(APP_NAME, "正文库目录不能为空。")
            return False
        if not question_materials_directory_text:
            messagebox.showerror(APP_NAME, "文档资料库目录不能为空。")
            return False
        question_record_mode = QUESTION_RECORD_MODE_LABELS.get(question_record_mode_label)
        if not question_record_mode:
            messagebox.showerror(APP_NAME, "请选择有效的记录方式。")
            return False
        if Path(question_filename).suffix.lower() != ".md":
            question_filename = Path(question_filename).stem + ".md"
            self.question_output_filename_var.set(question_filename)
        if obsidian_vault_text:
            obsidian_vault = Path(obsidian_vault_text).expanduser()
            if not (obsidian_vault / ".obsidian").is_dir():
                messagebox.showerror(APP_NAME, "Obsidian 仓库目录中缺少 .obsidian 文件夹。")
                return False
        output_format = OUTPUT_FORMAT_LABELS.get(self.output_format_var.get())
        if not output_format:
            messagebox.showerror(APP_NAME, "请选择有效的存储格式。")
            return False
        extension = "." + output_format
        if Path(filename).suffix.lower() != extension:
            filename = Path(filename).stem + extension
            self.output_filename_var.set(filename)
        directory = Path(directory_text).expanduser()
        path = directory / filename
        if directory.exists() and not directory.is_dir():
            messagebox.showerror(APP_NAME, "单词库目录不是有效文件夹。")
            return False
        if not dictionary_path_text:
            messagebox.showerror(APP_NAME, "词典文件路径不能为空。")
            return False
        dictionary_path = Path(dictionary_path_text).expanduser()
        if dictionary_path.exists() and not dictionary_path.is_file():
            messagebox.showerror(APP_NAME, "词典文件路径不是有效文件。")
            return False
        model = self.model_var.get().strip()
        if not model:
            messagebox.showerror(APP_NAME, "模型名称不能为空。")
            return False
        protocol = PROTOCOL_LABELS.get(self.protocol_var.get())
        if not protocol:
            messagebox.showerror(APP_NAME, "请选择有效的接口协议。")
            return False
        json_mode = JSON_MODE_LABELS.get(self.json_mode_var.get())
        if not json_mode:
            messagebox.showerror(APP_NAME, "请选择有效的 JSON 输出模式。")
            return False
        try:
            timeout = int(self.timeout_var.get().strip())
        except ValueError:
            messagebox.showerror(APP_NAME, "超时必须是 10 到 600 之间的整数秒数。")
            return False
        if not 10 <= timeout <= 600:
            messagebox.showerror(APP_NAME, "超时必须是 10 到 600 之间的整数秒数。")
            return False
        base_url = self.base_url_var.get().strip()
        try:
            build_api_endpoint(base_url, protocol)
        except ConfigError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return False
        key = self.key_var.get().strip()
        dictionary_changed = self.local_dictionary.source_path != dictionary_path
        try:
            if key and set(key) != {"•"}:
                self.config.set_api_key(key)
            self.config.values["output_path"] = str(path)
            self.config.values["output_dir"] = str(directory)
            self.config.values["output_filename"] = filename
            self.config.values["output_format"] = output_format
            mode = self.capture_mode
            self.config.values["mode"] = mode
            self.config.values["question_output_path"] = str(Path(question_directory_text).expanduser() / question_filename)
            self.config.values["question_output_dir"] = str(Path(question_directory_text).expanduser())
            self.config.values["question_output_filename"] = question_filename
            self.config.values["question_body_dir"] = str(Path(question_body_directory_text).expanduser())
            self.config.values["question_materials_dir"] = str(Path(question_materials_directory_text).expanduser())
            self.config.values["question_record_mode"] = question_record_mode
            self.config.values["obsidian_vault_path"] = str(
                Path(obsidian_vault_text).expanduser()
            ) if obsidian_vault_text else ""
            if obsidian_vault_text:
                vault_path = Path(obsidian_vault_text).expanduser()
                record_question_vault_profile(
                    self.config.values,
                    vault_path,
                    {
                        "question_output_dir": str(
                            Path(question_directory_text).expanduser()
                        ),
                        "question_output_filename": question_filename,
                        "question_body_dir": str(
                            Path(question_body_directory_text).expanduser()
                        ),
                        "question_materials_dir": str(
                            Path(question_materials_directory_text).expanduser()
                        ),
                        "question_record_mode": question_record_mode,
                    },
                )
                self.loaded_question_vault_key = _normalized_path_key(vault_path)
            self.config.values["dictionary_path"] = str(dictionary_path)
            self.config.values["model"] = model
            self.config.values["protocol"] = protocol
            self.config.values["base_url"] = base_url.rstrip("/")
            self.config.values["json_mode"] = json_mode
            self.config.values["timeout"] = timeout
            self.config.save()
            self.key_var.set("••••••••••••••••" if self.config.get_api_key() else "")
            if dictionary_changed:
                self.dictionary_check_revision += 1
                self.local_dictionary = LocalDictionary(dictionary_path)
            self.cache.clear()
            self.lookup_revision += 1
        except (OSError, ConfigError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return False
        self._refresh_question_document_history()
        if dictionary_changed:
            self.root.after(10, lambda: self._start_dictionary_check(quiet=True))
        return True

    def _save_and_minimize(self) -> None:
        save = self._save_highlight_settings if PAGE_LABELS.get(self.mode_var.get(), MODE_TRANSLATE) == MODE_HIGHLIGHT else self._save_settings
        if save():
            self.root.iconify()

    def _minimize(self) -> None:
        self.root.iconify()

    def _toggle_monitoring(self) -> None:
        self.monitoring = not self.monitoring
        self._sync_status()
        if hasattr(self, "_native_highlight_sync_lock"):
            self._start_highlight_entry_sync()

    def _sync_status(self) -> None:
        if not self.root.winfo_exists():
            return
        mode = self.capture_mode
        # 高光有独立页面，状态行和标题随当前捕获模式显示
        mode_text = {MODE_QUESTION: "记录问题", MODE_HIGHLIGHT: "高光模式"}.get(
            mode, "翻译英文单词"
        )
        if self.monitoring:
            self.status_label.configure(text="正在监听鼠标划选")
            self.toggle_button.configure(text="暂停监听")
            self.root.title(f"{APP_NAME} · {mode_text}（运行中）")
        else:
            self.status_label.configure(text="监听已暂停")
            self.toggle_button.configure(text="继续监听")
            self.root.title(f"{APP_NAME}（已暂停）")

    def _process_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "drag":
                    self._on_drag(event[1], event[2] if len(event) > 2 else None)
                elif kind == "pointer_down":
                    self._on_pointer_down(event[1])
                elif kind == "lookup_ok":
                    self._show_results(event[1], event[2], event[3])
                elif kind == "lookup_error":
                    self._show_lookup_error(event[1], event[2], event[3])
                elif kind == "save_done":
                    self._show_save_result(event[1], event[2], event[3])
                elif kind == "save_error":
                    self._show_save_error(event[1], event[2], event[3])
                elif kind == "question_save_done":
                    self._show_question_save_result(event[1], event[2])
                elif kind == "question_save_error":
                    self._show_question_save_error(event[1], event[2])
                elif kind == "tags_ready":
                    self._on_question_tag_pool_ready(event[1])
                elif kind == "hook_error":
                    messagebox.showerror(APP_NAME, event[1])
                elif kind == "stats_hotkey":
                    self._open_stats_window()
                elif kind == "dictionary_check_ok":
                    self._show_dictionary_check_result(
                        event[1], event[2], event[3], event[4]
                    )
                elif kind == "dictionary_check_error":
                    self._show_dictionary_check_error(event[1], event[2], event[3])
                elif kind == "source_context_ready":
                    self._handle_source_context_ready(
                        event[1], event[2], event[3] if len(event) > 3 else ""
                    )
                elif kind == "record_selection_ready":
                    self._handle_record_selection_ready(event[1], event[2], event[3])
                elif kind == "highlight_source_ready":
                    self._handle_highlight_source_ready(
                        event[1], event[2], event[3] if len(event) > 3 else ""
                    )
                elif kind == "highlight_bridge_status":
                    if event[1] == configured_highlight_vault(self.config.values):
                        self._set_highlight_status(event[2])
                        if event[2].startswith("高光连接成功"):
                            self._start_highlight_single_export_sync(notify=False)
                            self._start_highlight_active_colors_sync(notify=False)
                            self._start_highlight_entry_sync()
                elif kind == "native_mode_switch":
                    if len(event) > 2 and event[2] != configured_highlight_vault(self.config.values):
                        continue
                    self._switch_capture_mode(event[1])
                elif kind == "highlight_entry_sync":
                    if event[1] == getattr(self, "_native_highlight_sync_revision", -1):
                        if getattr(self, "capture_mode", MODE_TRANSLATE) == MODE_HIGHLIGHT:
                            self._native_highlight_sync_pending = event[2] is None
                            if event[2] is not None:
                                self._native_highlight_ready = event[2]
                        else:
                            self._native_highlight_ready = False
                            self._native_highlight_sync_pending = False
                        if getattr(self, "_native_highlight_ready", False):
                            self.capture_revision = getattr(self, "capture_revision", 0) + 1
                            self.pending_capture = None
                            self._hide_float_entry()
                        if event[3]:
                            if getattr(self, "capture_mode", MODE_TRANSLATE) == MODE_HIGHLIGHT:
                                self._set_transient_status(f"Obsidian 模式同步失败，正在重新确认入口：{event[3]}")
                            else:
                                self._set_transient_status(
                                    f"高光入口关闭尚未确认，后台重试中（不影响记录问题/翻译）：{event[3]}")
                            self.root.after(3000, lambda rev=event[1]: self._retry_highlight_entry_sync(rev))
                elif kind == "highlight_single_export_sync":
                    if len(event) > 3 and event[3] != configured_highlight_vault(self.config.values):
                        continue
                    if event[1]:
                        if len(event) > 3 and self._highlight_single_export_pending():
                            continue  # An in-flight acknowledgement still belongs to the previous target.
                        self._set_highlight_status("高光配置：单条导出目标已同步到 Obsidian")
                        self._set_transient_status("单条导出目标已同步到 Obsidian。")
                    elif self._highlight_single_export_payload():
                        # 配了目标才值得打断。状态栏那行一闪而过，用户会以为改好了，
                        # 而侧栏「导出这一条」仍写进旧索引——正是「改了索引 md 不生效」
                        # 的观感。没配目标时推的是「清空」，失败无所谓，不弹。
                        messagebox.showwarning(
                            APP_NAME,
                            "高光「单条导出目标」已保存到本地，但没能同步到 Obsidian：\n"
                            f"{event[2]}\n\n"
                            "现在在 Obsidian 里点「导出这一条」，仍会写进旧的索引文档。\n"
                            "请确认 Obsidian 已打开、桥接插件已启用；软件会每 15 秒自动重试。",
                            parent=self.root,
                        )
                    else:
                        self._set_transient_status(
                            f"单条导出目标已保存到本地，但 Obsidian 尚未同步：{event[2]}")
                elif kind == "highlight_active_colors_sync":
                    if len(event) > 3 and event[3] != configured_highlight_vault(self.config.values):
                        continue
                    # 颜色没变时这条链什么都不发，所以只在失败时提示，避免和单条导出抢状态栏。
                    if not event[1]:
                        self._set_transient_status(
                            f"高光颜色已保存到本地，但 Obsidian 尚未同步：{event[2]}"
                        )
                elif kind == "highlight_export_options":
                    if event[1] != configured_highlight_vault(self.config.values):
                        continue
                    button = getattr(self, "highlight_export_menu_button", None)
                    if button is not None and button.winfo_exists():
                        button.state(["!disabled"])
                    if event[3]:
                        self._set_transient_status(f"读取高光颜色失败：{event[3]}")
                        # 用户刚点了按钮，状态栏那行字不够醒目。
                        messagebox.showwarning(APP_NAME, f"读取高光颜色失败：{event[3]}")
                    else:
                        self._set_transient_status("请选择要导出的高光颜色。")
                        self._show_highlight_export_menu(event[1], event[2])
                elif kind == "highlight_done":
                    record_highlight_event("ui_result", outcome="success")
                    self._set_transient_status(
                        f"已高光：{event[2] or event[1]}（ID：{event[3]}）"
                    )
                    if len(event) > 4 and isinstance(event[4], dict):
                        record_highlight_ui_event("perf_status", capture_id=event[4]["capture_id"],
                                                  ui_status_wall_ms=time.time_ns() // 1_000_000)
                elif kind == "highlight_reset_preview":
                    _, vault, count, error = event
                    if vault != configured_highlight_vault(self.config.values):
                        continue
                    if error:
                        self._set_transient_status(f"清空前核对失败：{error}")
                    else:
                        self._confirm_highlight_reset(vault, count)
                elif kind == "highlight_reset_done":
                    _, result, error = event
                    self._set_transient_status(
                        f"高光清空失败：{error}" if error else
                        f"已清空 {result.get('removed', 0)} 条高光；来源文档未更改。")
                elif kind == "highlight_admin_result":
                    if len(event) > 5 and event[5] != configured_highlight_vault(self.config.values):
                        continue
                    absolute, result, error = event[1], event[2], event[3]
                    scope = event[4] if len(event) > 4 else ""
                    if error:
                        self._set_transient_status(f"高光导出失败：{error}")
                        messagebox.showwarning(APP_NAME, f"高光导出失败：{error}")
                    else:
                        self.config.values["highlight_export_dir"] = absolute
                        try:
                            self.config.save()
                            suffix = f"（{scope}）" if scope else ""
                            self._set_transient_status(
                                f"已导出 {result.get('count', 0)} 条高光到 {absolute}{suffix}")
                        except OSError as exc:
                            self._set_transient_status(f"文档已导出，但本地配置保存失败：{exc}")
                elif kind == "highlight_error":
                    self._report_highlight_error(
                        f"高光失败：{event[2]}",
                        event[3] if len(event) > 3 else None,
                    )
                elif kind == "obsidian_bridge_ok":
                    if len(event) > 2 and self.obsidian_vault_var and event[2] != self.obsidian_vault_var.get().strip():
                        continue
                    name = event[1] or "当前仓库"
                    self._set_obsidian_status(f"连接成功：{name}")
                elif kind == "obsidian_bridge_error":
                    if len(event) > 2 and self.obsidian_vault_var and event[2] != self.obsidian_vault_var.get().strip():
                        continue
                    self._set_obsidian_status(event[1])
        except queue.Empty:
            pass
        self._maybe_retry_highlight_single_export()
        if self.root.winfo_exists():
            self.root.after(60, self._process_events)

    def _on_drag(
        self,
        point: tuple[int, int],
        window_context: ForegroundWindowContext | None = None,
    ) -> None:
        if not self.monitoring:
            return
        if (getattr(self, "capture_mode", MODE_TRANSLATE) == MODE_HIGHLIGHT
                and (getattr(self, "_native_highlight_ready", False)
                     or getattr(self, "_native_highlight_sync_pending", False))
                and window_context is not None
                and window_context.process_name.casefold() in {"obsidian", "obsidian.exe"}):
            # Native ownership applies only to highlight mode. A slow/offline
            # highlighter must never swallow question/translation selections.
            self.capture_revision = getattr(self, "capture_revision", 0) + 1
            self.pending_capture = None
            self._hide_float_entry()
            return
        if self.capture_paused:
            popup_is_open = bool(self.lookup_popup and self.lookup_popup.winfo_exists())
            if not popup_is_open:
                return
            # Keep the original question-recording workflow: while its
            # editor is open, another drag is ignored until the user closes
            # the editor manually. Translation popups remain replaceable.
            if getattr(self, "capture_mode", MODE_TRANSLATE) == MODE_QUESTION:
                return
            # A new selection replaces the pending page immediately. Any old
            # worker result is ignored by the lookup/save revision guards.
            self._close_popup()
        # 新的划词开始：上一次的悬浮入口与待处理捕获都作废。即便这次划词最终没
        # 取到文本，旧入口也不该留在屏幕上。
        self._hide_float_entry()
        self.pending_capture = None
        # ``capture_revision`` was added after the original capture flow. Use
        # a default so lightweight callers/tests that construct the app
        # without running ``__init__`` remain compatible.
        self.capture_revision = getattr(self, "capture_revision", 0) + 1
        capture_revision = self.capture_revision
        self.last_pointer = point
        drag_mode = getattr(self, "capture_mode", MODE_TRANSLATE)
        self.pending_capture = {
            "text": "", "point": point, "words": None, "revision": capture_revision,
            "mode": drag_mode, "deferred": drag_mode != MODE_HIGHLIGHT,
            "window_context": window_context, "requested": False,
        }
        if not self._show_float_entry(point):
            self._set_transient_status("划词入口未能显示，请检查界面组件后重启助手。")
        self.detected_source_context = None
        self.source_detection_error = ""
        self.source_detection_pending = drag_mode == MODE_QUESTION
        self.source_detection_revision = capture_revision if self.source_detection_pending else -1
        if drag_mode == MODE_HIGHLIGHT:
            self._start_highlight_source_detection(capture_revision, window_context)
        if self.source_detection_pending:
            question_storage_dir = resolve_question_output_path(
                self.config.values
            ).parent
            configured_vault = str(
                self.config.values.get("obsidian_vault_path", "")
            ).strip()
            obsidian_vault = (
                Path(configured_vault).expanduser()
                if configured_vault
                else find_obsidian_vault(question_storage_dir)
            )

            def detect_source() -> None:
                context = None
                error = ""
                process_name = window_context.process_name.casefold() if window_context else ""
                if process_name in {"obsidian", "obsidian.exe"}:
                    if obsidian_vault is None:
                        error = "请先在记录问题页面配置 Obsidian 仓库。"
                    else:
                        try:
                            context = ObsidianBridgeClient(obsidian_vault).capture()
                        except (OSError, ObsidianBridgeError) as exc:
                            error = str(exc)
                        except Exception as exc:
                            error = f"读取 Obsidian 选区失败：{exc}"
                else:
                    context = SourceContextDetector.detect(window_context)
                    if context is None:
                        context = self.source_library.context_from_window(
                            window_context,
                            storage_path=resolve_question_materials_dir(self.config.values),
                        )
                self.events.put(
                    ("source_context_ready", capture_revision, context, error)
                )

            threading.Thread(
                target=detect_source, name="source-context-detection", daemon=True
            ).start()
        if drag_mode == MODE_TRANSLATE and window_context is not None and window_context.process_name.casefold() in {"obsidian", "obsidian.exe"}:
            self.pending_capture["selection_pending"] = True
            vault = str(self.config.values.get("obsidian_vault_path", "")).strip()

            def read_selection() -> None:
                context, error = None, ""
                try:
                    if vault:
                        context = ObsidianBridgeClient(vault).capture()
                except (OSError, ObsidianBridgeError) as exc:
                    error = str(exc)
                except Exception as exc:
                    error = f"读取 Obsidian 选区失败：{exc}"
                self.events.put(("record_selection_ready", capture_revision, context, error))

            threading.Thread(target=read_selection, name="record-selection", daemon=True).start()

    def _poll_clipboard(
        self,
        old_sequence: int,
        point: tuple[int, int],
        capture_revision: int | None,
        waited_ms: int = 0,
    ) -> None:
        """等剪贴板真的换了内容再往下走。

        原来是 `after(220, _finish_capture)`——固定等满 220 ms，用户感知就是「划完
        词图标要愣一下」。这里每 30 ms 查一次剪贴板序号，复制一到就继续；220 ms
        仍是上限。`_finish_capture` 里本来就有一道「序号没变就返回」的校验，所以
        超时那一次走的是和以前完全相同的路径。
        """
        if capture_revision is not None and capture_revision != self.capture_revision:
            return                       # 这一轮已经作废了（用户又划了新的）
        if (
            self.capture.clipboard_sequence() != old_sequence
            or waited_ms >= CLIPBOARD_POLL_TIMEOUT_MS
        ):
            self._finish_capture(old_sequence, point, capture_revision)
            return
        self.root.after(
            CLIPBOARD_POLL_INTERVAL_MS,
            lambda: self._poll_clipboard(
                old_sequence, point, capture_revision,
                waited_ms + CLIPBOARD_POLL_INTERVAL_MS,
            ),
        )

    def _finish_capture(
        self, old_sequence: int, point: tuple[int, int], capture_revision: int | None = None
    ) -> None:
        if capture_revision is not None and capture_revision != self.capture_revision:
            return
        if self.capture_paused:
            return
        if self.capture_mode == MODE_HIGHLIGHT:
            return  # Highlight captures always come from the bridge, never the clipboard.
        pending = getattr(self, "pending_capture", None)
        requested = bool(pending and pending.get("requested") and pending.get("revision") == capture_revision)
        if self.capture.clipboard_sequence() == old_sequence:
            if requested:
                self._record_capture_failed("未读取到选区，请重新划选并点击记录按钮。")
            return
        raw_text = self.capture.read_clipboard_text()
        if requested:
            self._complete_record_capture(raw_text, capture_revision)
            return
        mode = self.capture_mode
        minimum = int(self.config.values.get("minimum_text_length", 2))
        if mode == MODE_TRANSLATE:
            text = normalize_selected_text(raw_text)
            if len(text) < minimum or len(text) > 4000 or not re.search(r"[A-Za-z]", text):
                return
            prepared_words = self._prepared_words_for_sentence(text)
            if not prepared_words:
                return
        else:
            # 记录问题与高光都收任意文本，不做英文词形准备。高光要能给中文上色，
            # 所以不能套翻译那条「必须含拉丁字母」的门槛。
            text = normalize_note_text(raw_text)
            if len(text) < minimum or len(text) > 12000:
                return
            prepared_words = None
        self._offer_capture(text, point, capture_revision, prepared_words)

    def _start_highlight_source_detection(
        self,
        revision: int,
        window_context: ForegroundWindowContext | None,
    ) -> None:
        """高光模式在划词当时取一次 Obsidian 选区上下文。

        高光要**写入插件锚点库**，必须带插件发的那张 capture。刻意不复用记录
        问题那条检测链：那条的结果会被自动来源标注接手，属于问题模式的状态。这里
        另起一条、另发一种事件，两个模式的状态不相互覆盖。

        只在划词当时判一次前台窗口是不是 Obsidian——非 Obsidian 来源（网页、外部
        PDF、WPS/Word）没有可写回的落点，当场给结论，不必起线程。
        """
        self.highlight_source_revision = revision
        self.highlight_source_context = None
        self.highlight_source_error = ""
        self.highlight_source_pending = False
        self.highlight_requested_key = None
        process_name = window_context.process_name.casefold() if window_context else ""
        record_highlight_event("capture_start", revision=revision, process=process_name)
        if process_name not in {"obsidian", "obsidian.exe"}:
            self.highlight_source_error = (
                f"划词窗口是 {window_context.process_name}，不是 Obsidian；"
                "高光只支持 Obsidian 本地 Markdown 与 PDF。"
                if process_name else
                "无法识别划词窗口，请在 Obsidian 内重新划选后重试。"
            )
            record_highlight_event("capture_rejected", revision=revision,
                                   error=self.highlight_source_error)
            return
        configured_vault = configured_highlight_vault(self.config.values)
        obsidian_vault = (
            Path(configured_vault).expanduser()
            if configured_vault
            else None
        )
        if obsidian_vault is None:
            self.highlight_source_error = "请先在高光页面配置高光来源仓库。"
            record_highlight_event("capture_rejected", revision=revision,
                                   error=self.highlight_source_error)
            return

        self.highlight_source_pending = True

        def detect() -> None:
            context: ObsidianCaptureContext | ObsidianFileContext | None = None
            error = ""
            started = time.perf_counter()
            try:
                client = ObsidianBridgeClient(obsidian_vault)
                client.warm_highlight_pipe()
                context = client.capture()
            except (OSError, ObsidianBridgeError) as exc:
                error = str(exc)
            except Exception as exc:  # noqa: BLE001 - background errors must reach the UI/log
                error = f"读取 Obsidian 选区时发生内部错误：{type(exc).__name__}: {exc}"
            if isinstance(context, ObsidianFileContext):
                # PDF 的选区和页码由插件保存，来源文件一字不改。
                # 其余扩展名（图片、表格……）连页码都没有，
                # 说清楚比静默失败强——用户上一次就是这样以为功能坏了。
                if context.extension.casefold() != "pdf":
                    context, error = None, (
                        f"高光暂不支持 Vault 里的 .{context.extension} 文件，"
                        "只支持 Markdown 与 PDF。"
                    )
            elif context is not None and not isinstance(context, ObsidianCaptureContext):
                context, error = None, "高光只支持 Obsidian 本地 Markdown 与 PDF。"
            record_highlight_event(
                "capture_result", revision=revision,
                capture_id=getattr(context, "capture_id", ""),
                source_kind=type(context).__name__ if context else "none",
                file_path=getattr(context, "file_path", ""), error=error,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            )
            self.events.put(
                ("highlight_source_ready", revision, context, error)
            )

        threading.Thread(
            target=detect, name="highlight-source-detection", daemon=True
        ).start()

    def _handle_highlight_source_ready(
        self,
        revision: int,
        context: ObsidianCaptureContext | None,
        error: str = "",
    ) -> None:
        if (revision != getattr(self, "highlight_source_revision", -1)
                or revision != getattr(self, "capture_revision", revision)):
            record_highlight_event("capture_stale", revision=revision)
            return
        self.highlight_source_context = context
        self.highlight_source_error = error
        self.highlight_source_pending = False
        requested = getattr(self, "highlight_requested_key", None)
        self.highlight_requested_key = None
        pending = getattr(self, "pending_capture", None)
        if pending and pending.get("revision") == revision and context is not None:
            pending["text"] = getattr(context, "selection", "")
        if not requested or not pending or pending.get("revision") != revision:
            return
        if context is None:
            self._report_highlight_error(
                f"高光失败：{error or 'Obsidian 没有返回可用的选区。'}",
                pending.get("point"),
            )
            return
        self._apply_highlight(requested)

    def _offer_capture(
        self,
        text: str,
        point: tuple[int, int],
        capture_revision: int | None,
        prepared_words: list[dict[str, str]] | None,
    ) -> None:
        """划词成功，但先不开窗——把话题交给悬浮入口。

        窗口的建立被推迟到记录按钮点击，所以 _begin_lookup /
        _begin_question_record 里的 capture_paused 也跟着推迟：入口显示期间
        capture_paused 仍是 False，新划词会正常替换掉旧的待处理捕获。

        入口层不可用时（缺 Pillow 或 front/ 不完整）当场退回原来的「划词直接
        开窗」，不能变成划完词什么也不发生。
        """
        self.pending_capture = {
            "text": text,
            "point": point,
            "words": prepared_words,
            "revision": (
                capture_revision if capture_revision is not None else self.capture_revision
            ),
        }
        if not self._show_float_entry(point):
            self._open_pending_capture()

    def _open_pending_capture(self) -> None:
        """把待处理的捕获交给对应的记录窗口，并收起入口。"""
        pending = self.pending_capture
        if not pending:
            return
        if pending.get("deferred"):
            if pending.get("revision") != self.capture_revision or pending.get("mode") != self.capture_mode:
                return
            if pending.get("requested"):
                return
            pending["requested"] = True
            if pending.get("selection"):
                self._complete_record_capture(pending["selection"], pending["revision"])
            elif pending.get("selection_pending") or (
                self.capture_mode == MODE_QUESTION and getattr(self, "source_detection_pending", False)
                and self._pending_is_obsidian(pending)
            ):
                self._set_transient_status("正在读取 Obsidian 选区…")
            else:
                self._copy_pending_record()
            return
        self.pending_capture = None
        self._hide_float_entry()
        text = pending["text"]
        point = pending["point"]
        if self.capture_mode == MODE_TRANSLATE:
            self._begin_lookup(text, point, pending.get("words"))
        elif self.capture_mode == MODE_QUESTION:
            self._begin_question_record(text, point)
        # 高光不打开记录弹窗：点击色块直接走 _apply_highlight

    @staticmethod
    def _pending_is_obsidian(pending: dict) -> bool:
        window = pending.get("window_context")
        return bool(window and window.process_name.casefold() in {"obsidian", "obsidian.exe"})

    def _handle_record_selection_ready(self, revision: int, context, error: str = "") -> None:
        pending = getattr(self, "pending_capture", None)
        if (not pending or not pending.get("deferred") or pending.get("revision") != revision
                or revision != self.capture_revision or pending.get("mode") != self.capture_mode):
            return
        pending["selection_pending"] = False
        pending["selection"] = getattr(context, "selection", "") if isinstance(context, (ObsidianCaptureContext, ObsidianFileContext)) else ""
        if pending.get("requested"):
            if pending["selection"]:
                self._complete_record_capture(pending["selection"], revision)
            else:
                self._copy_pending_record()

    def _record_capture_failed(self, message: str) -> None:
        pending = getattr(self, "pending_capture", None)
        if pending:
            pending["requested"] = False
            pending["copying"] = False
        self._set_transient_status(message)

    def _copy_pending_record(self) -> None:
        pending = getattr(self, "pending_capture", None)
        if (not pending or not pending.get("requested") or pending.get("copying")
                or pending.get("revision") != self.capture_revision
                or pending.get("mode") != self.capture_mode
                or self.capture_mode not in (MODE_QUESTION, MODE_TRANSLATE)):
            return
        pending["copying"] = True
        try:
            window = pending.get("window_context")
            if window is not None:
                self.capture.focus_source_window(window.handle)
            sequence = self.capture.clipboard_sequence()
            self.capture.send_copy()
        except AppError as exc:
            self._record_capture_failed(str(exc))
            return
        self._poll_clipboard(sequence, pending["point"], pending["revision"])

    def _complete_record_capture(self, raw_text: str, revision: int) -> None:
        pending = getattr(self, "pending_capture", None)
        if not pending or pending.get("revision") != revision or revision != self.capture_revision or pending.get("mode") != self.capture_mode:
            return
        minimum = int(self.config.values.get("minimum_text_length", 2))
        if self.capture_mode == MODE_TRANSLATE:
            text = normalize_selected_text(raw_text)
            if len(text) < minimum or len(text) > 4000 or not re.search(r"[A-Za-z]", text):
                self._record_capture_failed("选区不是有效的英文内容，请重新划选。")
                return
            words = self._prepared_words_for_sentence(text)
            if not words:
                self._record_capture_failed("选区中没有可记录的单词。")
                return
        else:
            text, words = normalize_note_text(raw_text), None
            if len(text) < minimum or len(text) > 12000:
                self._record_capture_failed("选区为空、过短或过长，请重新划选。")
                return
        pending.update(text=text, words=words, deferred=False)
        self._open_pending_capture()

    # ------------------------------------------------------- 悬浮入口
    def _show_float_entry(self, point: tuple[int, int]) -> bool:
        """在选区结尾浮现入口。返回 False 表示入口层不可用。

        属性一律走 getattr 取默认值：测试与轻量调用方会用 ``__new__`` 造壳、不跑
        ``__init__``，这里不能假设 ui_icons / ui_colors / float_entry 已经存在
        （同 _on_drag 里 capture_revision 的处理）。
        """
        icons = getattr(self, "ui_icons", None)
        colors = getattr(self, "ui_colors", None)
        if FloatingEntry is None or icons is None or colors is None:
            return False
        entry = getattr(self, "float_entry", None)
        if entry is None:
            entry = FloatingEntry(
                self.root,
                icons,
                scale=getattr(self, "ui_scale", 1.0),
                colors=colors,
                icon_name=FLOAT_ENTRY_ICONS.get(self.capture_mode, "pen-line"),
                icon_color=FLOAT_ENTRY_ICON_COLOR,
                on_hover=self._on_float_entry_hover,
                on_context_menu=self._on_float_entry_menu,
                on_pick=self._on_float_entry_pick,
                on_click=self._on_float_entry_click,
                mode_options=[(mode, label, FLOAT_ENTRY_ICONS[mode])
                              for mode, label in ((MODE_QUESTION, "记录问题"), (MODE_HIGHLIGHT, "高光模式"), (MODE_TRANSLATE, "记录单词"))],
                active_mode=self.capture_mode,
                on_mode_select=self._switch_capture_mode,
            )
            self.float_entry = entry
        entry.set_icon(FLOAT_ENTRY_ICONS.get(self.capture_mode, "pen-line"))
        entry.set_active_mode(self.capture_mode)
        entry.show(*point)
        return True

    def _hide_float_entry(self) -> None:
        menu = getattr(self, "float_entry_menu", None)
        if menu is not None:
            menu.close()
        entry = getattr(self, "float_entry", None)
        if entry is not None:
            entry.hide()

    def _on_pointer_down(self, point: tuple[int, int]) -> None:
        """左键按在入口之外 → 收起入口。

        入口自己身上的点击也会走到这里（低层钩子看得见），所以要判矩形，把入口
        自己的区域排除掉，否则点色块时入口会先被收掉。

        **菜单弹着的时候更要让开**：菜单是入口窗口的子窗口，点菜单项的这一次左键
        落在入口矩形之外，于是这里会把入口 withdraw 掉——owner 窗口一没，菜单的
        命令根本不会派发。用户看到的就是「右键切换模式点了没反应，还是原来的功能」。
        """
        menu = getattr(self, "float_entry_menu", None)
        if menu is not None:
            try:
                if not menu.winfo_exists():
                    menu = None
                elif hasattr(menu, "contains"):
                    if menu.contains(*point):
                        return
                    menu.close()
                else:
                    # 兼容仍由旧调用方/测试持有的原生 tk.Menu：菜单模态期间
                    # 任何低层点击都不能收掉其 owner 入口。
                    return
            except tk.TclError:
                menu = None
        entry = getattr(self, "float_entry", None)
        if entry is None or not entry.is_visible():
            return
        if not entry.contains(*point):
            entry.hide()
            self.pending_capture = None
            self.capture_revision = getattr(self, "capture_revision", 0) + 1

    def _on_float_entry_hover(self) -> None:
        """悬停仅在高光模式展开颜色；记录模式不得因此打开页面。"""
        entry = getattr(self, "float_entry", None)
        if entry is not None and self.capture_mode == MODE_HIGHLIGHT:
            entry.expand_items(self._highlight_entry_swatches())

    def _highlight_entry_swatches(self) -> list[dict]:
        """当前高光配置的色块，按默认色排序，交给无业务状态的展示层。"""
        return [
            {"key": key, "kind": "swatch", "color": color}
            for key, color in self._highlight_swatches()
        ]

    def _on_float_entry_click(self, _x: int, _y: int) -> None:
        """左键记录模式图标才消费选区；高光模式必须点具体颜色。"""
        if self.capture_mode in (MODE_QUESTION, MODE_TRANSLATE):
            self._open_pending_capture()

    def _on_float_entry_pick(self, key) -> None:
        """展开区只有高光色块；旧记录动作 key 不得再触发开窗。"""
        if key in HIGHLIGHT_SWATCH_KEYS:
            self._apply_highlight(str(key))

    def _highlight_swatches(self) -> list[tuple[str, str]]:
        """悬浮窗上摆的那 3 种颜色（配置页 8 选 3），键和色值都和插件的
        HIGHLIGHT_COLOR_DEFS 一致。顺序就是配置里的顺序，也就是画出来的顺序。"""
        return [(key, DEFAULT_HIGHLIGHT_PALETTE[key])
                for key in self.highlight_active_colors]

    def _apply_highlight(self, key: str) -> None:
        """高光模式点颜色块：桥接插件记录锚点并绘制 Markdown/PDF。

        这里只做判断和派发，真正写入 JSONL 的调用在后台线程里，
        否则改一份大文档会把主线程堵住——那是「开窗卡顿」那一轮踩过的坑。
        """
        pending = self.pending_capture
        if pending and HIGHLIGHT_PERF_FLAG.exists():
            down = getattr(getattr(self, "capture", None), "last_pointer_down", None)
            callback_ns = time.perf_counter_ns()
            entry = getattr(self, "float_entry", None)
            if (down and 0 <= callback_ns - down[1] < 3_000_000_000 and entry
                    and entry.contains(*down[0])):
                pending["perf_probe"] = {
                    "capture_id": "", "hook_wall_ms": down[2],
                    "os_to_hook_ms": down[3],
                    "hook_to_tk_ms": round((callback_ns - down[1]) / 1e6, 2),
                    "tk_callback_wall_ms": time.time_ns() // 1_000_000,
                }
        record_highlight_ui_event("color_click", revision=pending.get("revision") if pending else "",
                               color=key, has_selection=bool(pending))
        if not pending:
            self._hide_float_entry()
            return
        context = getattr(self, "highlight_source_context", None)
        if context is None and getattr(self, "highlight_source_pending", False):
            # 用户点色块快于桥接 HTTP 往返时，记住本次选择，等结果回来再执行。
            # 旧逻辑把“还没识别完”当“不支持来源”，甚至永远不会补做这次上色。
            self.highlight_requested_key = key
            record_highlight_ui_event("click_waiting_capture", revision=pending.get("revision"),
                                   color=key)
            self._set_transient_status("正在读取 Obsidian 选区，完成后自动添加高光…")
            return
        if context is None:
            # 刻意**不**收起入口：提示就显示在刚点过的色块旁边。只写主窗口状态行
            # 是不够的——窗口最小化时用户什么都看不见，功能看着就像坏了（PDF
            # 那次就是这么被误判的）。
            self._notify_highlight_failure(
                getattr(self, "highlight_source_error", "")
                or "高光只支持 Obsidian 本地 Markdown 与 PDF。"
            )
            return
        if str(key) not in HIGHLIGHT_SWATCH_KEYS:
            self._notify_highlight_failure(f"高光颜色「{key}」不认识。")
            return
        # 插件以稳定的颜色键定位；具体色值由已同步的调色板决定。
        color = key
        record_highlight_ui_event("operation_dispatched", revision=pending.get("revision"),
                               capture_id=context.capture_id, color=key,
                               file_path=context.file_path)
        probe = pending.get("perf_probe")
        if probe:
            probe["capture_id"] = context.capture_id
            if not hasattr(self, "_highlight_perf_pending"):
                self._highlight_perf_pending = {}
            self._highlight_perf_pending[context.capture_id] = probe
            record_highlight_ui_event("perf_click", **probe)
        self._hide_float_entry()
        self.pending_capture = None
        threading.Thread(
            target=self._run_highlight,
            args=(
                context, key, color,
                str(pending.get("text", "")),
                pending.get("point"),
            ),
            name="highlight-apply",
            daemon=True,
        ).start()

    def _run_highlight(
        self,
        context: ObsidianCaptureContext | ObsidianFileContext,
        key: str,
        color: str,
        text: str,
        point: tuple[int, int] | None = None,
    ) -> None:
        """后台：插件内部 JSONL 是高光的唯一实时数据源。Markdown 仅按需导出。"""
        probe = getattr(self, "_highlight_perf_pending", {}).pop(context.capture_id, None)
        if probe:
            probe["worker_start_wall_ms"] = time.time_ns() // 1_000_000
        record_highlight_event(
            "operation_start", capture_id=context.capture_id,
            source_kind="pdf" if isinstance(context, ObsidianFileContext) else "md",
            file_path=context.file_path, color=key,
        )
        try:
            client = ObsidianBridgeClient(context.vault_path)
            if hasattr(self, "_highlight_single_export_lock"):
                try:
                    self._push_highlight_single_export(client, context.vault_path)
                except (OSError, ObsidianBridgeError) as exc:
                    # 单条导出目标是侧栏那个导出图标的配置，推不上去不该挡着用户把
                    # 这条高光记下来——如实提示一句，然后照常往下走。
                    self.events.put(("highlight_single_export_sync", False, str(exc)))
            result = (client.apply_highlight(context.capture_id, key, trace=True)
                      if probe else client.apply_highlight(context.capture_id, key))
            if probe:
                probe.update(result.get("trace", {}))
                probe["bridge_send_wall_ms"] = result.get("_bridge_send_wall_ms")
                probe["bridge_response_wall_ms"] = result.get("_bridge_response_wall_ms")
                for key in ("_bridge_attempt_stage", "_bridge_first_send_wall_ms",
                            "_bridge_first_error_wall_ms", "_bridge_retry_send_wall_ms",
                            "_bridge_channel", "_bridge_fallback_reason",
                            "_bridge_connect_reused", "_bridge_pipe_connect_ms",
                            "_bridge_queue_wait_ms", "_bridge_send_ms",
                            "_bridge_wait_response_ms",
                            "_bridge_transport", "_pipe_fallback_reason",
                            "_pipe_written_wall_ms", "_http_started_wall_ms"):
                    if key in result:
                        probe[key[1:]] = result[key]
                probe["worker_success_wall_ms"] = time.time_ns() // 1_000_000
                record_highlight_ui_event("perf_result", **probe)
            record_highlight_event("anchor_saved", capture_id=context.capture_id,
                                   highlight_id=result.get("highlight_id", ""))
        except (OSError, ObsidianBridgeError) as exc:
            record_highlight_event("operation_error", capture_id=context.capture_id,
                                   error_type=type(exc).__name__, error=str(exc))
            self.events.put(("highlight_error", key, str(exc), point))
            return
        except Exception as exc:  # noqa: BLE001 - 后台异常必须可见
            message = f"内部错误 {type(exc).__name__}: {exc}"
            record_highlight_event("operation_unexpected_error", capture_id=context.capture_id,
                                   error=message)
            self.events.put(("highlight_error", key, message, point))
            return
        record_highlight_event("operation_done", capture_id=context.capture_id,
                               highlight_id=result.get("highlight_id", ""))
        self.events.put(("highlight_done", key, result.get("file_name", context.file_name),
                         result.get("highlight_id", ""), probe))

    def _report_highlight_error(
        self, message: str, point: tuple[int, int] | None
    ) -> None:
        """后台高光失败时的告知。

        这时入口早就收起了（点色块那一刻就收），所以先把入口在**原来那次划词的
        位置**重新浮出来，再就地显示失败原因。只写状态行是不够的——窗口最小化时
        用户什么都看不见。
        """
        record_highlight_event("ui_error", error=message)
        self._set_transient_status(message)
        if not point:
            return
        try:
            if self._show_float_entry(tuple(point)):
                entry = getattr(self, "float_entry", None)
                if entry is not None:
                    entry.flash(message)
        except Exception:                       # noqa: BLE001 - 提示失败不该再抛
            pass

    def _notify_highlight_failure(self, message: str) -> None:
        """把高光失败说清楚：入口上就地显示一份，主窗口可见时状态行也写一份。

        入口层不可用时（缺 Pillow / front 不完整）只剩状态行，那也比静默好。
        """
        record_highlight_event("ui_error", error=message)
        self._set_transient_status(message)
        entry = getattr(self, "float_entry", None)
        if entry is None:
            return
        try:
            if entry.is_visible():
                entry.flash(message)
        except Exception:                       # noqa: BLE001 - 提示失败不该再抛
            pass

    def _set_transient_status(self, message: str) -> None:
        """在状态行说一句话，等下一次 _sync_status 自动复原。"""
        label = getattr(self, "status_label", None)
        if label is not None:
            label.configure(text=message)

    def _build_float_entry_menu(self) -> ModePopup | None:
        entry = getattr(self, "float_entry", None)
        if entry is None or entry.window is None or ModePopup is None:
            return None
        options = [(mode, label, FLOAT_ENTRY_ICONS.get(mode, ""))
                   for mode, label in FLOAT_ENTRY_MODES]
        colors = dict(FALLBACK_UI_COLORS)
        colors.update(getattr(self, "ui_colors", None) or {})
        return ModePopup(self.root, colors, options, self.capture_mode, self._switch_capture_mode,
                         on_close=lambda: setattr(self, "float_entry_menu", None),
                         icons=getattr(self, "ui_icons", None),
                         scale=getattr(self, "ui_scale", 1.0))

    def _on_float_entry_menu(self, x: int, y: int) -> None:
        """三种模式右键只显示切换卡；不能消费选区或打开记录页面。"""
        existing = getattr(self, "float_entry_menu", None)
        if existing is not None:
            existing.close()
        menu = self._build_float_entry_menu()
        if menu is None:
            return
        self.float_entry_menu = menu
        menu.show(x, y)

    def _switch_capture_mode(self, mode: str) -> None:
        """从模式栏（或兼容的右键菜单）切换捕获模式。

        三个模式现在都有主窗口页面，一律走页面切换（mode_var 一动，
        _on_mode_changed 就把 capture_mode 推导出来）。上一轮高光没有页面时这里
        有个「只改 capture_mode、不动 mode_var」的特例，加了页面之后不再需要。

        **切换必须排在最前面**，收起入口要推到 `after(0)`：这条命令是在菜单的
        派发过程中跑的，而入口窗口就是菜单的 owner——在命令中途把它 withdraw 掉，
        菜单的派发会当场断掉，用户看到的就是「菜单出来了，点某项没反应」。先让
        模式真的切过去，再从容收尾。
        """
        label = next(
            (name for name, value in PAGE_LABELS.items() if value == mode), None
        )
        if label is not None and self.mode_var is not None:
            self.mode_var.set(label)
        else:
            self.capture_mode = mode
            self._sync_status()
        try:
            self.root.after(0, self._hide_float_entry)
        except tk.TclError:
            self._hide_float_entry()

    def _begin_lookup(
        self,
        sentence: str,
        point: tuple[int, int],
        prepared_words: list[dict[str, str]] | None = None,
    ) -> None:
        prepared_words = (
            prepared_words
            if prepared_words is not None
            else self._prepared_words_for_sentence(sentence)
        )
        prepared_key = tuple(normalize_index_word(item["word"]) for item in prepared_words)
        cache_key = (sentence, prepared_key)
        self.lookup_revision += 1
        revision = self.lookup_revision
        self.capture_paused = True
        self.last_sentence = sentence
        self.last_pointer = point
        self._create_popup(sentence, point)
        if cache_key in self.cache:
            self.root.after(20, lambda: self._show_results(revision, sentence, self.cache[cache_key]))
            return

        def worker() -> None:
            try:
                service = HybridVocabularyService.from_config(
                    self.config, self.local_dictionary
                )
                items = service.lookup(sentence, prepared_words)
                self.events.put(("lookup_ok", revision, sentence, items))
            except (AppError, OSError) as exc:
                self.events.put(("lookup_error", revision, sentence, str(exc)))
            except Exception as exc:
                self.events.put(("lookup_error", revision, sentence, f"查询失败：{exc}"))

        threading.Thread(target=worker, name="vocabulary-lookup", daemon=True).start()

    def _begin_question_record(self, selected_text: str, point: tuple[int, int]) -> None:
        self.lookup_revision += 1
        self.last_sentence = selected_text
        self.last_pointer = point
        self.capture_paused = True
        self._create_question_popup(selected_text, point)

    def _create_question_popup(self, selected_text: str, point: tuple[int, int]) -> None:
        if self.lookup_popup and self.lookup_popup.winfo_exists():
            self._destroy_popup_character_actor()
            self.lookup_popup.destroy()
        available_assets = [
            asset_dir for asset_dir in POPUP_CHARACTER_DIRS
            if popup_character_available(asset_dir)
        ]
        if available_assets:
            weights = [
                POPUP_CHARACTER_WEIGHTS[index]
                for index, asset_dir in enumerate(POPUP_CHARACTER_DIRS)
                if asset_dir in available_assets
            ]
            self.popup_character_asset_dir = random.choices(available_assets, weights=weights, k=1)[0]
        else:
            self.popup_character_asset_dir = None
        self.question_images = []
        self.question_sources = []
        self.question_tag_target = None
        self.question_tag_supports = []
        self.question_image_selected_index = None
        self.question_image_selected_widget = None
        self.question_image_delete_button = None
        self.question_image_status_host = None
        popup = tk.Toplevel(self.root)
        self.lookup_popup = popup
        popup.title("记录问题")
        popup.attributes("-topmost", True)
        popup.geometry(self._popup_geometry(point, 780, 620))
        # 尺寸在内容建好之后由 _fit_popup_height 定，这里给的是建的过程里的临时值
        popup.protocol("WM_DELETE_WINDOW", self._close_popup)
        popup.configure(background=self._token("bg_primary"))
        self._darken_titlebar(popup)

        header = ttk.Frame(
            popup,
            padding=(self._px(20), self._px(18), self._px(20), self._px(12)),
        )
        header.pack(fill="both", expand=True)
        ttk.Label(header, text="记录问题", style="Title.TLabel").pack(anchor="w")
        overview_row = ttk.Frame(header)
        overview_row.pack(fill="x", pady=(self._px(8), self._px(3)))
        ttk.Label(
            overview_row, text="问题/知识点概述（可留空）", style="Word.TLabel"
        ).pack(side="left")
        self._load_question_default_settings()
        default_title_label = (
            self.question_title_level_default_var.get()
            if self.question_title_level_default_var
            else DEFAULT_QUESTION_TITLE_LABEL
        )
        self.question_title_var = tk.StringVar(value=default_title_label)
        ttk.Combobox(
            overview_row,
            textvariable=self.question_title_var,
            values=[label for label, _ in QUESTION_TITLE_OPTIONS],
            state="readonly",
            width=12,
        ).pack(side="right")
        self.question_input = tk.Text(
            header, height=3, wrap="word", undo=True, **self._editor_colors()
        )
        self.question_input.pack(fill="x", pady=(0, self._px(8)))
        self._attach_question_tag_support(self.question_input)
        self._bind_question_tag_target(self.question_input)
        self._build_question_tag_row(header)
        # Vertical split lets the user give more room to supplements without
        # moving the fixed overview or pushing the save/close toolbar offscreen.
        splitter_class = SmoothVerticalPanedWindow or tk.PanedWindow
        splitter_options = ({
            "grip_color": self._token("border"),
            "active_color": self._token("accent"),
            "scale": self.ui_scale,
        } if SmoothVerticalPanedWindow else {})
        self.question_editor_panes = splitter_class(
            header, orient="vertical", opaqueresize=True, showhandle=False,
            sashwidth=self._px(10), sashpad=0,
            sashcursor="sb_v_double_arrow", relief="flat", borderwidth=0,
            background=self._token("bg_primary"), **splitter_options,
        )
        self.question_editor_panes.pack(fill="both", expand=True)
        supplement_pane = ttk.Frame(self.question_editor_panes)
        supplement_label_row = ttk.Frame(supplement_pane)
        supplement_label_row.pack(fill="x", pady=(0, self._px(3)))
        ttk.Label(supplement_label_row, text="内容补充（可留空，下方分隔条可上下拖动）", style="Word.TLabel").pack(side="left")
        self.question_supplement_index_link_var = tk.StringVar(
            value=(
                self.question_supplement_default_var.get()
                if self.question_supplement_default_var
                else "直接写入当前索引"
            )
        )
        ttk.Combobox(supplement_label_row, textvariable=self.question_supplement_index_link_var, values=list(QUESTION_LINK_MODE_LABELS), state="readonly", width=14).pack(side="right")
        supplement_viewport = ttk.Frame(supplement_pane)
        supplement_viewport.pack(fill="both", expand=True, pady=(self._px(3), 0))
        self.question_supplement_input = tk.Text(
            supplement_viewport, height=6, wrap="word", undo=True, exportselection=False,
            **self._editor_colors(),
        )
        supplement_scrollbar = ttk.Scrollbar(
            supplement_viewport, orient="vertical", command=self.question_supplement_input.yview,
        )
        self.question_supplement_input.configure(yscrollcommand=supplement_scrollbar.set)
        self.question_supplement_input.pack(side="left", fill="both", expand=True)
        supplement_scrollbar.pack(side="right", fill="y")
        self.question_editor_panes.add(supplement_pane, minsize=self._px(90), stretch="always")
        self._attach_question_tag_support(self.question_supplement_input)
        self.question_supplement_input.bind(
            "<FocusIn>", self._remember_question_code_target
        )
        self._bind_question_tag_target(self.question_supplement_input)
        self.question_supplement_input.bind(
            "<ButtonRelease-1>", self._remember_question_image_target
        )
        selected_row = ttk.Frame(self.question_editor_panes)
        self.question_editor_panes.add(selected_row, minsize=self._px(100), stretch="always")
        selected_content = ttk.Frame(selected_row)
        selected_content.pack(side="left", fill="both", expand=True)
        source_label_row = ttk.Frame(selected_content)
        source_label_row.pack(fill="x")
        ttk.Label(source_label_row, text="原文（可编辑）", style="Muted.TLabel").pack(side="left")
        self.question_source_index_link_var = tk.StringVar(
            value=(
                self.question_source_default_var.get()
                if self.question_source_default_var
                else "直接写入当前索引"
            )
        )
        ttk.Combobox(source_label_row, textvariable=self.question_source_index_link_var, values=list(QUESTION_LINK_MODE_LABELS), state="readonly", width=14).pack(side="right")
        # 原文框是「输入区」，底色用 bg_field（比白卡片略深一点），边框跟输入框
        # 一致：常态细灰线，聚焦转强调色。原先是 1px 黑色 solid 边，太重。
        selected_box = tk.Frame(
            selected_content,
            background=self._token("bg_field"),
            relief="flat",
            borderwidth=0,
            highlightthickness=self._px(1),
            highlightbackground=self._token("border"),
            highlightcolor=self._token("accent"),
        )
        selected_box.pack(fill="both", expand=True, pady=(self._px(3), 0))

        # Use one Text widget as the Markdown-like editor. Image previews are
        # embedded windows in this widget, so text can be entered before or
        # after every image and the whole content scrolls as one document.
        selected_viewport = tk.Frame(selected_box, background=self._token("bg_field"))
        selected_viewport.pack(fill="both", expand=True, padx=(1, 1), pady=(1, 0))
        self.question_selected_text_input = tk.Text(
            selected_viewport,
            height=8,
            width=70,
            wrap="word",
            undo=True,
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=6,
            exportselection=False,
            **self._editor_colors(),
        )
        selected_scrollbar = ttk.Scrollbar(
            selected_viewport,
            orient="vertical",
            command=self.question_selected_text_input.yview,
        )
        self.question_selected_text_input.configure(yscrollcommand=selected_scrollbar.set)
        self.question_selected_text_input.pack(side="left", fill="both", expand=True)
        selected_scrollbar.pack(side="right", fill="y")
        self.question_selected_text_input.insert("1.0", selected_text)
        self._attach_question_tag_support(self.question_selected_text_input)
        self.question_selected_text_input.bind(
            "<FocusIn>", self._remember_question_code_target
        )
        self._bind_question_tag_target(self.question_selected_text_input)
        self.question_selected_text_input.bind(
            "<ButtonRelease-1>", self._remember_question_image_target
        )
        # New attachments start after the captured text. Clicking in this
        # editor later moves the insertion point, just like a normal document.
        self.question_selected_text_input.mark_set("insert", "end-1c")
        image_row = tk.Frame(selected_box, background=self._token("bg_field"))
        image_row.pack(fill="x", padx=self._px(8), pady=(0, self._px(6)))
        self.question_image_status_host = image_row
        self.question_image_delete_button = ttk.Button(
            image_row, text="删除图片", command=self._remove_question_image
        )
        if self.popup_character_asset_dir:
            # 人物在原文档编辑框右侧单独占一列，编辑框相应收窄；人本身仍然走
            # place 浮在这一列里，所以这一列只是留白，不是一块画布方块，
            # 显隐人物都不会让编辑框跟着变宽变窄。
            # 人物是抠图，底色必须和弹窗底一致（_mount_popup_character 也取
            # bg_primary），否则会显出一块方形的色差
            character_column = tk.Frame(
                selected_row,
                background=self._token("bg_primary"),
                highlightthickness=0,
                bd=0,
            )
            character_column.pack(side="right", fill="y", padx=(self._px(10), 0))
            character_column.pack_propagate(False)
            self._mount_popup_character(character_column, "center", relx=0.5, rely=0.5)
            if self.popup_character_actor:
                # 列宽按人的实际画布来，换素材或换缩放大小时不用改数字
                character_column.configure(
                    width=self.popup_character_actor.canvas.winfo_reqwidth()
                )
        footer_separator = ttk.Separator(popup)
        footer_separator.pack(side="bottom", before=header, fill="x")
        body = ttk.Frame(popup, padding=self._px(20))
        # Reserve toolbar space first, even when the available screen is short.
        body.pack(side="bottom", before=footer_separator, fill="x")
        editor = ttk.Frame(body)
        editor.pack(fill="both", expand=True)
        question_toolbar = ttk.Frame(editor)
        question_toolbar.pack(fill="x", pady=(0, 7))
        ttk.Label(question_toolbar, text="原文工具", style="Word.TLabel").pack(side="left")
        # 主操作用强调色：这一屏只有它是「把事情办完」，其余都是次级动作
        self.question_save_button = ttk.Button(
            question_toolbar,
            text="写入问题记录",
            style="Accent.TButton",
            command=self._save_question_record,
        )
        self.question_save_button.pack(side="left", padx=(10, 0))
        add_image_button = ttk.Menubutton(question_toolbar, text="加入图片")
        image_menu = tk.Menu(add_image_button, tearoff=False)
        image_menu.add_command(label="选择图片…", command=self._choose_question_image)
        image_menu.add_command(label="从剪贴板粘贴（Ctrl+V）", command=self._paste_question_image)
        add_image_button.configure(menu=image_menu)
        add_image_button.pack(side="left", padx=(6, 0))
        code_button = ttk.Menubutton(question_toolbar, text="代码块")
        code_menu = tk.Menu(code_button, tearoff=False)
        for label, language in CODE_LANGUAGE_OPTIONS:
            code_menu.add_command(
                label=label,
                command=lambda selected_language=language: self._insert_question_code(
                    selected_language
                ),
            )
        code_button.configure(menu=code_menu)
        code_button.pack(side="left", padx=(6, 0))
        source_button = ttk.Button(
            question_toolbar, text="标注来源", command=self._on_source_button
        )
        source_menu = tk.Menu(source_button, tearoff=False)
        source_menu.add_command(
            label="当前文档（自动识别）", command=self._mark_detected_source
        )
        source_menu.add_separator()
        source_menu.add_command(label="网页链接…", command=self._mark_web_source)
        source_menu.add_command(label="本地文件…", command=self._mark_local_source)
        source_button.pack(side="left", padx=(6, 0))
        self.question_source_button = source_button
        self.question_source_menu = source_menu
        ttk.Button(question_toolbar, text="关闭", command=self._close_popup).pack(
            side="left", padx=(6, 0)
        )
        self.question_save_status = ttk.Label(editor, text="", style="Muted.TLabel")
        self.question_save_status.pack(anchor="w")

        self._fit_popup_height(popup, point, 780)

        popup.lift()
        # Ctrl+V 只在能混排图片的两个编辑框里拦截；绑定在 popup 上会让
        # 概述/说明栏的普通文字粘贴冒出“剪贴板中没有图片”的错误提示。
        for image_editor in (self.question_supplement_input, self.question_selected_text_input):
            image_editor.bind("<Control-v>", self._paste_question_image)
            image_editor.bind("<Control-V>", self._paste_question_image)
        popup.bind("<BackSpace>", self._remove_question_image_from_key)
        popup.bind("<Delete>", self._remove_question_image_from_key)
        self.question_input.focus_set()
        self._defer_pending_obsidian_source(popup)

    def _defer_pending_obsidian_source(self, popup: tk.Misc) -> None:
        """把欠下的来源自动标注推到窗口**映射之后**再跑。

        它要经 Obsidian 桥接做一次 HTTP 往返（超时 1.5 秒）。窗口的建立推迟到悬停
        之后，来源识别早就完成了，于是这次往返被塞进了建窗路径——实测点击回调里
        同步阻塞 181.9 ms，其中 85 ms 就是那次往返，窗口要等它回来才出现
        （`D:/tmp/probe_popup_latency.py`，对照组的同步阻塞是 98.1 ms）。
        推到映射之后，窗口先出来，链接晚一瞬补上——也就是划词即开窗那时的原样。

        绑 `<Map>` 而不是定时器，理由同 `_darken_titlebar`：定时器在冷启动或机器忙
        的时候会赌错。`after` 只是兜底（窗口因为某些原因没映射时别把标注丢了），
        两道都由 `fired` 去重。
        """
        fired: dict[str, bool] = {"done": False}

        def run(_event=None) -> None:
            if fired["done"]:
                return
            fired["done"] = True
            try:
                if not popup.winfo_exists():
                    return          # 窗口已经关了，这笔欠账就不必补了
            except tk.TclError:
                return
            self._apply_pending_obsidian_source()

        try:
            popup.bind("<Map>", run, add="+")
            popup.after(120, run)
        except tk.TclError:
            run()

    # ------------------------------------------------------------- 标签
    def _question_tags_in_editors(self) -> list[str]:
        """三个编辑框正文里现有的标签，按原文、内容补充、概述的顺序。

        读控件而不是另记一份状态：标签就住在正文里，用户删掉那个词标签就没了。
        另记一份迟早跟正文对不上（点了叉标签还在这种事），而且写入时也要从正文
        里抽，同一份数据没必要存两次。
        """
        if ui_tags is None:
            return []
        texts: list[str] = []
        for editor in (
            self.question_selected_text_input,
            self.question_supplement_input,
            self.question_input,
        ):
            if editor is None:
                continue
            try:
                if editor.winfo_exists():
                    texts.append(editor.get("1.0", "end-1c"))
            except tk.TclError:
                continue
        return ui_tags.merge_tags(*(ui_tags.extract_tags(text) for text in texts))

    def _question_tag_pool_now(self) -> list[str]:
        """下拉与补全共用的标签池：库里的 + 用过的 + 正文里已经有的。

        库里的排在前面：扫库是按出现次数排的，常用的标签敲下 `#` 就在头几个。
        正文里已有的也并进来，所以在原文里刚插进去的新标签，立刻能在别处补全到。
        """
        if ui_tags is None:
            return []
        return ui_tags.merge_tags(
            self.question_tag_pool or [],
            self.config.values.get("question_tags", []),
            self._question_tags_in_editors(),
        )

    def _refresh_question_tag_pool(self, force: bool = False) -> None:
        """后台扫一遍库，取里面真实存在的标签。读文件不能在 UI 线程里做。"""
        if ui_tags is None or self.question_tag_scan_pending:
            return
        if (
            not force
            and self.question_tag_pool is not None
            and time.time() - self.question_tag_pool_at < TAG_POOL_TTL
        ):
            return
        vault = str(self.config.values.get("obsidian_vault_path", "")).strip()
        if not vault:
            return
        self.question_tag_scan_pending = True

        def worker() -> None:
            try:
                found = ui_tags.scan_vault_tags(vault)
            except Exception:                     # 库不可读不该影响记录问题
                found = []
            self.events.put(("tags_ready", found))

        threading.Thread(target=worker, name="question-tag-scan", daemon=True).start()

    def _on_question_tag_pool_ready(self, tags: list[str]) -> None:
        self.question_tag_scan_pending = False
        self.question_tag_pool = list(tags)
        self.question_tag_pool_at = time.time()
        if self.question_tag_combo and self.question_tag_combo.winfo_exists():
            self.question_tag_combo.configure(values=self._question_tag_pool_now()[:40])

    def _build_question_tag_row(self, parent: tk.Misc) -> None:
        """标签栏：从建过的标签里挑一个，插到光标处。

        能挑是关键 —— 同一个概念两次抄成不同名字，标签就废了，所以下拉里放的是
        库里真实存在的标签（front.tags.scan_vault_tags），不是凭印象打。
        标签跟着所在的编辑框走：加在概述里就进大纲的标题行，加在原文或内容补充里
        就留在正文里，当正文内的定位标记（Obsidian 搜 `tag:` 认它们）。
        哪种都不进文件名。
        """
        if ui_tags is None or not self.ui_colors:
            return
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, self._px(8)))
        ttk.Label(row, text="标签", style="Word.TLabel").pack(side="left")
        self.question_tag_var = tk.StringVar()
        self.question_tag_combo = ttk.Combobox(
            row,
            textvariable=self.question_tag_var,
            values=self._question_tag_pool_now()[:40],
            width=16,
        )
        self.question_tag_combo.pack(side="left", padx=(self._px(8), self._px(4)))
        self.question_tag_combo.bind("<KeyRelease>", self._on_question_tag_typed)
        # 选择/回车仅把标签显示在组合框；用户点右侧 + 才真正写入编辑框。
        self.question_tag_combo.bind("<<ComboboxSelected>>", lambda _event: None)
        self._icon_button(
            row, "plus", "", self._insert_question_tag,
            tooltip_text="插到光标处（也可以直接在编辑框里打 #标签）",
        ).pack(side="left")
        self._refresh_question_tag_pool()

    def _attach_question_tag_support(self, editor: tk.Text) -> None:
        """给一个编辑框接上标签高亮与 `#` 补全。主题层缺失时静默跳过。"""
        if ui_tags is None or not self.ui_colors:
            return
        self.question_tag_supports.append(
            ui_tags.TagTextSupport(
                editor, self._question_tag_pool_now, self.ui_colors, self.ui_scale
            )
        )

    def _bind_question_tag_target(self, editor: tk.Text) -> None:
        """记住「最后一次落在哪个编辑框」，点加号时标签插到那儿。

        必须在已有的 `<FocusIn>` 绑定之后调用：项目里绑 `<FocusIn>` 用的是不带
        add 的 bind，会把先前绑的整条盖掉。所以不能顺手写进
        _attach_question_tag_support 里。
        也不能复用 _remember_question_code_target —— 那个刻意不收概述框（代码块
        与图片只能进原文/内容补充），而标签三个编辑框都要能插。
        """
        editor.bind(
            "<FocusIn>",
            lambda _event, widget=editor: self._remember_question_tag_target(widget),
            add="+",
        )

    def _remember_question_tag_target(self, editor: tk.Text) -> None:
        self.question_tag_target = editor

    def _question_tag_insert_target(self) -> tk.Text | None:
        """加号的落点：最后一次有光标的编辑框，没记到就退回概述框。

        弹窗打开时焦点本来就给概述框，所以「没说在哪儿插」等于「插到概述里」，
        比弹一句「请先点一下编辑框」顺。
        """
        for editor in (self.question_tag_target, self.question_input):
            try:
                if editor is not None and editor.winfo_exists():
                    return editor
            except tk.TclError:
                continue
        return None

    def _on_question_tag_typed(self, event=None) -> None:
        """在标签框里打字时把下拉项收窄到前缀匹配的那些。"""
        if self.question_tag_combo is None or self.question_tag_var is None:
            return
        if event is not None and event.keysym in {
            "Return", "KP_Enter", "Up", "Down", "Escape", "Tab", "ISO_Left_Tab"
        }:
            return
        typed = self.question_tag_var.get().strip().lstrip("#").casefold()
        pool = self._question_tag_pool_now()
        matches = [tag for tag in pool if tag.casefold().startswith(typed)] if typed else pool
        self.question_tag_combo.configure(values=matches[:40])

    def _insert_question_tag(self, _event=None):
        """把标签框里选中的标签插到光标处。"""
        if ui_tags is None or self.question_tag_var is None:
            return None
        name = ui_tags.normalize_tag(self.question_tag_var.get())
        if not name:
            self._set_source_action_status(
                "标签只能用字母、数字、下划线、横线和斜杠，且至少含一个非数字字符。"
            )
            return "break"
        target = self._question_tag_insert_target()
        if target is None:
            self._set_source_action_status("请先点一下要加标签的编辑框。")
            return "break"
        # 库里已经有同一个标签（只是写法不同）时，用库里那种写法。写 `#pytorch`
        # 也能得到 `#Pytorch` —— 用户不必记得当初是怎么拼的，这正是这个功能存在的
        # 理由：Obsidian 里这两个本来就是同一个标签。
        canonical = {tag.casefold(): tag for tag in self._question_tag_pool_now()}
        tag = canonical.get(name.casefold(), name)
        # 前后都补空格：`#` 前必须是行首或空白才算标签（否则 `文本#标签` 只是普通
        # 文字），后面不留空格则紧跟着的那个字会被吃进标签名里（`#标签播`）。补出来
        # 的正是用户手打时的那套写法，解析规则一个字没动。
        caret = target.index("insert")
        previous = target.get(f"{caret} - 1c", caret) if caret != "1.0" else ""
        leading = "" if not previous or previous.isspace() else " "
        # caret 是 index() 出来定死的，不是 "insert - 1 chars" 那种惰性表达式：
        # 下面这一插会改动文本，惰性表达式会重新求值落到别处去。
        target.insert(caret, f"{leading}#{tag} ")
        target.mark_set("insert", f"{caret} + {len(leading) + len(tag) + 2} chars")
        target.see("insert")
        target.focus_set()                   # 把光标亮出来，让用户看见落在了哪儿
        self.question_tag_var.set("")
        if self.question_tag_combo and self.question_tag_combo.winfo_exists():
            self.question_tag_combo.configure(values=self._question_tag_pool_now()[:40])
        return "break"

    def _harvest_question_tags(self, texts) -> list[str]:
        """从给定的几段正文里抽标签（大小写归一，只留一种写法）。

        写入标题行时只喂概述那一段：标题行是「这条记录」的标签集合，原文与内容
        补充里的标签是给正文定位用的，留在正文里就行，没必要再抄进大纲。
        """
        if ui_tags is None:
            return []
        return ui_tags.merge_tags(*(ui_tags.extract_tags(text) for text in texts))

    def _remember_question_tags(self, tags: list[str]) -> None:
        """把这次用到的标签记进配置。换库或者库还没扫到时，列表也不是空的。"""
        stored = self.config.values.get("question_tags", [])
        if not isinstance(stored, list):
            stored = []
        if ui_tags is None:
            return
        merged = ui_tags.merge_tags(stored, tags)[-200:]
        if merged != stored:
            self.config.values["question_tags"] = merged
            try:
                self.config.save()
            except OSError:
                pass                              # 记不住标签不该挡住写入笔记

    def _remember_question_code_target(self, event) -> None:
        if event.widget in {
            self.question_supplement_input,
            self.question_selected_text_input,
        }:
            self.question_code_target = event.widget
            self.question_image_target = event.widget
            self.question_image_insert_index = event.widget.index("insert")

    def _remember_question_image_target(self, event) -> None:
        if event.widget in {
            self.question_supplement_input,
            self.question_selected_text_input,
        }:
            self.question_image_target = event.widget
            self.question_image_insert_index = event.widget.index("insert")

    def _insert_question_code(self, language: str | None = None):
        target = self.question_code_target
        if not target or not target.winfo_exists():
            self._set_source_action_status("请先点击内容补充或原文编辑区。")
            return "break"

        try:
            first = target.index("sel.first")
            last = target.index("sel.last")
            value = target.get(first, last)
            insert_at = first
        except tk.TclError:
            self._set_source_action_status("请先用鼠标选择要转换为代码块的文字。")
            return "break"

        selected_language = (
            recommend_code_language(value) if language is None else language
        )
        try:
            block = markdown_code_block(value, selected_language)
        except ValueError as exc:
            self._set_source_action_status(str(exc))
            return "break"

        target.delete(first, last)
        previous = (
            target.get(f"{insert_at} -2c", insert_at) if insert_at != "1.0" else ""
        )
        following = target.get(insert_at, f"{insert_at} +2c")
        prefix = (
            ""
            if insert_at == "1.0" or previous.endswith("\n\n")
            else "\n"
            if previous.endswith("\n")
            else "\n\n"
        )
        suffix = (
            ""
            if not following or following.startswith("\n\n")
            else "\n"
            if following.startswith("\n")
            else "\n\n"
        )
        target.insert(insert_at, f"{prefix}{block}{suffix}")
        target.focus_set()
        target.see("insert")
        label = selected_language or "纯文本"
        self._set_source_action_status(f"已作为 {label} 代码块插入。")
        return "break"

    def _on_source_button(self) -> None:
        button = self.question_source_button
        if not button or not button.winfo_exists():
            return
        if self.question_sources:
            self._remove_question_sources()
            return
        menu = self.question_source_menu
        if not menu:
            return
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()

    def _refresh_source_button(self) -> None:
        button = self.question_source_button
        if button and button.winfo_exists():
            button.configure(text="取消来源标注" if self.question_sources else "标注来源")

    def _remove_question_sources(self) -> None:
        editor = self.question_selected_text_input
        for item in list(self.question_sources):
            widget = item.get("widget")
            if editor and editor.winfo_exists() and widget and widget.winfo_exists():
                try:
                    window_index = editor.index(str(widget))
                    editor.delete(window_index, f"{window_index} +1c")
                except tk.TclError:
                    pass
                widget.destroy()
        self.question_sources = []
        self.source_mark_requested_revision = -1
        self._refresh_source_button()

    def _mark_detected_source(self) -> None:
        if self.source_detection_pending:
            self.source_mark_requested_revision = self.source_detection_revision
            self._set_source_action_status("正在识别当前文档，完成后会自动加入来源…", busy=True)
            return
        context = self.detected_source_context
        if not context:
            self._set_question_image_error(
                self.source_detection_error
                or "未能识别当前来源，请使用“本地文件”手动选择。"
            )
            return
        if isinstance(context, (ObsidianCaptureContext, ObsidianFileContext)):
            output_path = resolve_question_output_path(self.config.values).resolve()
            vault_path = Path(context.vault_path).resolve()
            try:
                output_path.relative_to(vault_path)
            except ValueError:
                self._set_question_image_error(
                    "问题记录文档必须位于所配置的 Obsidian 仓库内。"
                )
                return
        if isinstance(context, ObsidianFileContext):
            try:
                link = obsidian_file_source_link(
                    context.file_path, context.file_name, context.page_number
                )
            except ObsidianBridgeError as exc:
                self._set_question_image_error(str(exc))
                return
            inserted = self._insert_question_source(
                {
                    "type": "obsidian_file",
                    "link": link,
                    "file_path": context.file_path,
                    "extension": context.extension,
                    "page_number": context.page_number,
                }
            )
            if inserted:
                if context.extension.casefold() == "pdf" and context.page_number:
                    status = f"已加入 Obsidian PDF 第 {context.page_number} 页链接"
                else:
                    status = "已加入 Obsidian 仓库文件链接（无定位）"
                self._set_source_action_status(status)
            return
        if isinstance(context, ObsidianCaptureContext):
            if normalize_note_text(context.selection) != normalize_note_text(
                self.last_sentence
            ):
                self._set_question_image_error(
                    "Obsidian 选区与划词内容不一致，请关闭窗口后重新划选。"
                )
                return
            try:
                anchored = ObsidianBridgeClient(vault_path).create_anchor(
                    context.capture_id
                )
                link = obsidian_source_link(
                    str(anchored.get("file_path", "")),
                    str(anchored.get("file_name", context.file_name)),
                    str(anchored.get("anchor", "")),
                )
            except (OSError, ObsidianBridgeError) as exc:
                self._set_question_image_error(str(exc))
                return
            inserted = self._insert_question_source(
                {
                    "type": "obsidian",
                    "link": link,
                    "file_path": str(anchored.get("file_path", "")),
                }
            )
            if inserted:
                self._set_source_action_status("已加入 Obsidian 定位链接")
            return
        try:
            output_path = resolve_question_output_path(self.config.values)
            record_id = context.record_id or self.source_library.register(
                context.path, storage_path=resolve_question_materials_dir(self.config.values)
            )
            if context.record_id and not self.source_library.resolve_link_target(
                record_id, storage_path=resolve_question_materials_dir(self.config.values)
            ):
                raise OSError("资料库中的来源副本已不存在。")
        except OSError as exc:
            self._set_question_image_error(f"无法保存当前文档快照：{exc}")
            return
        inserted = self._insert_question_source(
            {
                "type": "external",
                "record_id": record_id,
            }
        )
        if inserted:
            self._set_source_action_status(
                self._external_source_status(record_id, output_path)
            )

    def _handle_source_context_ready(
        self,
        revision: int,
        context: DetectedSourceContext | ObsidianCaptureContext | ObsidianFileContext | None,
        error: str = "",
    ) -> None:
        if revision != self.source_detection_revision or revision != getattr(self, "capture_revision", revision):
            return
        self.detected_source_context = context
        self.source_detection_error = error
        self.source_detection_pending = False
        self._handle_record_selection_ready(revision, context, error)
        config = getattr(self, "config", None)
        config_values = getattr(config, "values", {})
        if (
            context is not None
            and not getattr(self, "question_sources", [])
            and bool(
                config_values.get(
                    "question_obsidian_source_link", DEFAULT_QUESTION_OBSIDIAN_SOURCE_LINK
                )
            )
            and isinstance(context, (ObsidianCaptureContext, ObsidianFileContext))
        ):
            editor = getattr(self, "question_selected_text_input", None)
            if not editor or not editor.winfo_exists():
                self.source_auto_mark_requested_revision = revision
                return
            self._set_source_action_status("正在加入 Obsidian 来源链接…", busy=True)
            self._mark_detected_source()
            return
        if self.source_mark_requested_revision != revision:
            return
        self.source_mark_requested_revision = -1
        if context is None:
            message = error or "未能识别当前来源，请使用“本地文件”手动选择。"
            self._set_question_image_error(message)
            self._set_source_action_status(message)
            return
        self._set_source_action_status("正在创建来源副本并生成链接…")
        self._mark_detected_source()

    def _apply_pending_obsidian_source(self) -> None:
        revision = self.source_auto_mark_requested_revision
        if revision < 0 or revision != self.source_detection_revision:
            return
        self.source_auto_mark_requested_revision = -1
        context = self.detected_source_context
        if (
            self.source_detection_pending
            or context is None
            or getattr(self, "question_sources", [])
            or not isinstance(context, (ObsidianCaptureContext, ObsidianFileContext))
        ):
            return
        config_values = getattr(getattr(self, "config", None), "values", {})
        if not bool(
            config_values.get(
                "question_obsidian_source_link", DEFAULT_QUESTION_OBSIDIAN_SOURCE_LINK
            )
        ):
            return
        self._set_source_action_status("正在加入 Obsidian 来源链接…", busy=True)
        self._mark_detected_source()

    def _set_source_action_status(self, message: str, busy: bool = False) -> None:
        status = getattr(self, "question_save_status", None)
        if status and status.winfo_exists():
            status.configure(text=message)
        button = getattr(self, "question_save_button", None)
        if button and button.winfo_exists():
            button.state(["disabled"] if busy else ["!disabled"])

    def _mark_web_source(self) -> None:
        """网页来源：自己画个跟界面同色的小窗收链接。

        原来是 `simpledialog.askstring` —— 那是 Tk 的经典对话框，外观跟着系统走、
        完全不读我们的调色板，暗色界面里会弹出一个浅色小窗，和「记录问题」弹窗
        不是一套东西。改成自建 Toplevel 后收链接变成回调式（原来是同步阻塞），
        所以校验和插入都放在 submit 里。
        """
        parent = self.lookup_popup
        if not parent or not parent.winfo_exists():
            return
        existing = getattr(self, "web_source_window", None)
        if existing and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return
        window = tk.Toplevel(parent)
        self.web_source_window = window
        window.title("标注来源")
        window.transient(parent)
        # 父窗是 -topmost（记录问题弹窗），子窗不跟着设会被压在它下面
        window.attributes("-topmost", True)
        window.resizable(False, False)
        window.configure(background=self._token("bg_primary"))
        self._darken_titlebar(window)
        window.protocol("WM_DELETE_WINDOW", window.destroy)

        body = ttk.Frame(window, padding=self._px(20))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="标注来源", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="粘贴网页链接，它会作为来源插入到问题原文里。",
            style="Muted.TLabel",
            wraplength=self._px(360),
            justify="left",
        ).pack(anchor="w", pady=(self._px(5), self._px(12)))
        url_var = tk.StringVar()
        entry = ttk.Entry(body, textvariable=url_var, width=44)
        entry.pack(fill="x")
        # 就地提示，不把错误甩到下面弹窗的状态行上让用户回头找。用 Danger.TLabel
        # 而不是 Muted：后者和上面的说明文字同一个灰，看不出这句是「错了」。
        # 空 Label 自己就占一行高（ttk 的 reqheight 与文本是否为空无关），所以这一行
        # 的位置是预留好的，出错时窗口不会往上长。
        error_label = ttk.Label(
            body, text="", style="Danger.TLabel",
            wraplength=self._px(360), justify="left",
        )
        error_label.pack(anchor="w", pady=(self._px(6), 0))

        def submit(_event=None) -> None:
            if not url_var.get().strip():
                return                      # 空输入和原来一样：当没发生过
            url = normalize_web_source_url(url_var.get())
            if not url:
                error_label.configure(text="请输入有效的 http/https 网页链接。")
                entry.focus_set()
                return
            window.grab_release()
            window.destroy()
            self._insert_question_source({"type": "web", "url": url})

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(self._px(14), 0))
        ttk.Button(actions, text="取消", command=window.destroy).pack(side="right")
        ttk.Button(actions, text="确定", style="Accent.TButton",
                   command=submit).pack(side="right", padx=(0, 8))
        window.bind("<Return>", submit)
        window.bind("<Escape>", lambda _event: window.destroy())
        entry.focus_set()
        window.grab_set()

    def _mark_local_source(self) -> None:
        if not self.lookup_popup or not self.lookup_popup.winfo_exists():
            return
        path = filedialog.askopenfilename(
            parent=self.lookup_popup,
            title="选择来源文件",
            filetypes=(("所有文件", "*.*"),),
        )
        if not path:
            return
        try:
            output_path = resolve_question_output_path(self.config.values)
            record_id = self.source_library.register(
                Path(path), storage_path=resolve_question_materials_dir(self.config.values)
            )
        except OSError as exc:
            self._set_question_image_error(f"无法记录本地来源：{exc}")
            return
        inserted = self._insert_question_source(
            {
                "type": "external",
                "record_id": record_id,
            }
        )
        if inserted:
            self._set_source_action_status(
                self._external_source_status(record_id, output_path)
            )

    def _external_source_status(self, record_id: str, output_path: Path) -> str:
        source_name = self.source_library.display_name(record_id)
        target = self.source_library.resolve_link_target(
            record_id, storage_path=resolve_question_materials_dir(self.config.values)
        )
        if Path(source_name).suffix.casefold() != ".docx":
            return "已创建来源副本并加入链接"
        if target and Path(target).suffix.casefold() == ".pdf":
            return "已将 DOCX 转为 PDF，并加入资料库链接"
        if self.source_library.conversion_error(record_id):
            return "DOCX 转 PDF 失败，已保留并链接原始 DOCX"
        return "已创建 DOCX 来源副本并加入链接"

    def _source_display_text(self, item: dict) -> str:
        if item.get("type") == "web":
            url = str(item.get("url", ""))
            return f"标注来源\n[网页链接]({url})"
        if item.get("type") == "obsidian":
            return str(item.get("link", ""))
        if item.get("type") == "obsidian_file":
            page_number = item.get("page_number")
            if str(item.get("extension", "")).casefold() == "pdf" and page_number:
                notice = OBSIDIAN_PDF_PAGE_NOTICE.format(page=page_number)
            else:
                notice = OBSIDIAN_FILE_SOURCE_NOTICE
            return f"{item.get('link', '')}\n{notice}"
        record_id = str(item.get("record_id", ""))
        output_path = resolve_question_output_path(self.config.values)
        link = self.source_library.markdown_link(
            record_id, output_path, storage_path=resolve_question_materials_dir(self.config.values)
        )
        return f"{link}\n{EXTERNAL_SOURCE_NOTICE}"

    def _insert_question_source(self, item: dict) -> bool:
        editor = item.get("editor") or self.question_selected_text_input
        if not editor or not editor.winfo_exists():
            return False
        display = self._source_display_text(item)
        label = tk.Label(
            editor,
            text=display,
            anchor="w",
            justify="left",
            wraplength=self._px(520),
            background=self._token("bg_field"),
            foreground=self._token("accent"),
            cursor="hand2" if item.get("type") == "web" else "arrow",
        )
        item["widget"] = label
        self.question_sources.append(item)
        insert_index = editor.index("insert")
        previous = editor.get(f"{insert_index} -1c", insert_index) if insert_index != "1.0" else ""
        if previous and previous != "\n":
            editor.insert("insert", "\n")
        try:
            editor.window_create("insert", window=label, padx=4, pady=4, align="top")
            editor.insert("insert", "\n")
        except tk.TclError as exc:
            label.destroy()
            self.question_sources.pop()
            self._set_question_image_error(f"来源标注失败：{exc}")
            return False
        label.bind("<MouseWheel>", self._scroll_question_editor)
        self.question_image_selected_index = None
        self.question_image_selected_widget = None
        editor.see("insert")
        self._refresh_source_button()
        return True

    def _choose_question_image(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.lookup_popup or self.root,
            title="选择要加入的问题图片",
            filetypes=(
                ("图片文件", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self._set_question_image(Path(path))

    def _paste_question_image(self, event=None):
        # 键盘粘贴先试图片；不是图片时交给 tk.Text 的类绑定粘贴文字，
        # 不在原文图片区显示错误。菜单里的“粘贴图片”是显式操作，才提示无图。
        keyboard_paste = event is not None
        if ImageGrab is None:
            if not keyboard_paste:
                self._set_question_image_error("剪贴板图片预览需要安装 Pillow。")
            return None
        try:
            pasted = ImageGrab.grabclipboard()
        except Exception as exc:
            if not keyboard_paste:
                self._set_question_image_error(f"读取剪贴板图片失败：{exc}")
            return None
        if Image is not None and hasattr(pasted, "convert"):
            if keyboard_paste:
                self.question_image_target = event.widget
            self._insert_question_image(
                {"path": None, "data": pasted.convert("RGBA"), "name": "剪贴板图片"}
            )
            return "break"
        if not keyboard_paste:
            self._set_question_image_error("剪贴板中没有图片。")
        return None

    def _set_question_image(self, path: Path) -> None:
        if Image is None:
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}:
                self._set_question_image_error("仅支持 PNG、JPG、GIF、BMP 或 WEBP 图片。")
                return
            self._insert_question_image({"path": path, "data": None, "name": path.name})
            return
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image_data = image.convert("RGBA")
            self._insert_question_image(
                {"path": path, "data": image_data, "name": path.name}
            )
        except (OSError, ValueError, tk.TclError) as exc:
            self._set_question_image_error(f"无法读取图片：{exc}")

    def _insert_question_image(self, item: dict) -> None:
        editor = getattr(self, "question_image_target", None) or self.question_selected_text_input
        if not editor or not editor.winfo_exists():
            return
        # 图片是嵌在编辑框里的一个窗口，底色和边框都要跟编辑框一致（bg_field +
        # 细灰线），不能写死白色，否则深色主题下会出现白方块
        tile = tk.Frame(
            editor,
            background=self._token("bg_field"),
            relief="flat",
            borderwidth=0,
            highlightbackground=self._token("border"),
            highlightcolor=self._token("border"),
            highlightthickness=self._px(1),
            cursor="hand2",
        )
        preview = item.get("data")
        if preview is not None and ImageTk is not None:
            image = preview.copy()
            image.thumbnail((320, 180))
            photo = ImageTk.PhotoImage(image, master=editor)
            item["preview"] = photo
            label = tk.Label(tile, image=photo, background=self._token("bg_field"),
                             cursor="hand2")
        else:
            label = tk.Label(
                tile, text=item.get("name", "图片"),
                background=self._token("bg_field"), foreground=self._token("text_muted"),
                cursor="hand2",
            )
        label.pack(padx=4, pady=4)
        item["widget"] = tile
        item["window_name"] = str(tile)
        item["editor"] = editor
        image_index = len(self.question_images)
        self.question_images.append(item)

        insert_index = getattr(self, "question_image_insert_index", None) or editor.index("insert")
        self.question_image_insert_index = insert_index
        previous = editor.get(f"{insert_index} -1c", insert_index) if insert_index != "1.0" else ""
        if previous and previous != "\n":
            editor.insert(insert_index, "\n")
            insert_index = editor.index(f"{insert_index} +1c")
        try:
            editor.window_create(insert_index, window=tile, padx=4, pady=4, align="top")
            editor.insert(f"{insert_index} +1c", "\n")
            editor.mark_set("insert", f"{insert_index} +2c")
        except tk.TclError as exc:
            tile.destroy()
            self.question_images.pop()
            self._set_question_image_error(f"图片载入失败：{exc}")
            return
        for widget in (tile, label):
            widget.bind(
                "<Button-1>",
                lambda _event, image_item=item: self._select_question_image(
                    self.question_images.index(image_item), image_item.get("widget")
                ),
            )
            widget.bind("<MouseWheel>", self._scroll_question_editor)
        self.question_image_selected_index = image_index
        self.question_image_selected_widget = tile
        tile.configure(takefocus=True)
        self._refresh_question_image_preview()
        editor.see("insert")

    def _set_question_image_error(self, message: str) -> None:
        frame = self.question_image_status_host
        if not frame or not frame.winfo_exists():
            return
        if not self.question_image_status_label or not self.question_image_status_label.winfo_exists():
            self.question_image_status_label = ttk.Label(frame, style="Muted.TLabel")
            self.question_image_status_label.pack(anchor="w")
        self.question_image_status_label.configure(text=message, image="")

    def _select_question_image(self, index: int, widget=None) -> None:
        if 0 <= index < len(self.question_images):
            self.question_image_selected_index = index
            self.question_image_selected_widget = widget or self.question_images[index].get("widget")
            if self.question_image_selected_widget is not None:
                try:
                    self.question_image_selected_widget.focus_set()
                except tk.TclError:
                    pass
            self._refresh_question_image_preview()

    def _remove_question_image(self) -> None:
        index = self.question_image_selected_index
        if index is None or not (0 <= index < len(self.question_images)):
            return
        item = self.question_images[index]
        editor = item.get("editor") or self.question_selected_text_input
        widget = item.get("widget")
        if editor and editor.winfo_exists() and widget and widget.winfo_exists():
            try:
                window_index = editor.index(str(widget))
                editor.delete(window_index, f"{window_index} +1c")
            except tk.TclError:
                pass
            widget.destroy()
        del self.question_images[index]
        self.question_image_selected_index = min(index, len(self.question_images) - 1) if self.question_images else None
        self.question_image_selected_widget = (
            self.question_images[self.question_image_selected_index].get("widget")
            if self.question_image_selected_index is not None
            else None
        )
        self._refresh_question_image_preview()

    def _scroll_question_editor(self, event):
        editor = getattr(self, "question_image_target", None) or self.question_selected_text_input
        if editor and editor.winfo_exists():
            editor.yview_scroll(int(-1 * (event.delta / 120)), "units")
            return "break"

    def _remove_question_image_from_key(self, _event=None):
        if self.question_image_selected_index is None:
            return None
        focus = self.lookup_popup.focus_get() if self.lookup_popup and self.lookup_popup.winfo_exists() else None
        if focus in {
            self.question_input,
            self.question_supplement_input,
            self.question_selected_text_input,
        }:
            return None
        self._remove_question_image()
        return "break"

    def _question_editor_content_items(self, editor=None) -> list[tuple[str, object]]:
        editor = editor or self.question_selected_text_input
        if not editor or not editor.winfo_exists():
            return []
        image_by_window = {
            str(item.get("widget")): item
            for item in self.question_images
            if item.get("widget") is not None and item.get("editor") is editor
        }
        source_by_window = {
            str(item.get("widget")): item
            for item in self.question_sources
            if item.get("widget") is not None and editor is self.question_selected_text_input
        }
        content_items: list[tuple[str, object]] = []
        for kind, value, _index in editor.dump("1.0", "end-1c", text=True, window=True):
            if kind == "text" and value:
                # dump() 会在 **tag 边界** 处把文本切段（即使没要 -tag），而编辑框里
                # 的 `nh-tag` 高亮恰好把每个标签单独切出来。下游是按 item 分段落的
                # （append_question 用 "\n\n" 拼），不并回一段的话，原文里每出现一个
                # 标签就会多断出一个空行段落。窗口（图片/来源）当然要各占一项，不并。
                if content_items and content_items[-1][0] == "text":
                    content_items[-1] = ("text", str(content_items[-1][1]) + value)
                else:
                    content_items.append(("text", value))
            elif kind == "window" and value in image_by_window:
                item = image_by_window[value]
                content_items.append(("image", (item.get("path"), item.get("data"))))
            elif kind == "window" and value in source_by_window:
                source = source_by_window[value]
                content_items.append(
                    (
                        "source",
                        {
                            "text": self._source_display_text(source),
                            "record_id": source.get("record_id", "")
                            if source.get("type") == "external"
                            else "",
                        },
                    )
                )
        return content_items

    def _refresh_question_image_preview(self) -> None:
        self.question_image_status_label = None
        if not self.question_images:
            if self.question_image_delete_button and self.question_image_delete_button.winfo_exists():
                self.question_image_delete_button.pack_forget()
            return
        for index, item in enumerate(self.question_images):
            selected = index == self.question_image_selected_index
            tile = item.get("widget")
            if not tile or not tile.winfo_exists():
                continue
            # 选中态：描边换成强调色并加粗一圈。原来是用 borderwidth 那条
            # solid 边做加粗，现在边框统一走 highlight 环（见 _insert_question_image），
            # 所以这里也要改环的粗细，否则选中没有视觉反馈。
            ring = self._px(2) if selected else self._px(1)
            color = self._token("accent") if selected else self._token("border")
            tile.configure(
                highlightthickness=ring, highlightbackground=color, highlightcolor=color,
            )
        button = self.question_image_delete_button
        if button and button.winfo_exists():
            # 有图但没选中时不显示：此刻点它是空操作（_remove_question_image 开头就返回），
            # 摆一个按不动的按钮不如不摆。插入来源标注会把选中清掉，这种情况真会出现。
            if self.question_image_selected_index is None:
                button.pack_forget()
            else:
                button.pack(side="right")

    def _save_question_record(self) -> None:
        if not self.question_input or not self.question_save_status:
            return
        if (
            self.source_detection_pending
            and self.source_mark_requested_revision == self.source_detection_revision
        ):
            self._set_source_action_status("正在识别并保存来源副本，请稍候…", busy=True)
            return
        question = self.question_input.get("1.0", "end-1c")
        supplement = self.question_supplement_input.get("1.0", "end-1c") if self.question_supplement_input else ""
        title_label = (
            self.question_title_var.get()
            if self.question_title_var
            else DEFAULT_QUESTION_TITLE_LABEL
        )
        title_level = dict(QUESTION_TITLE_OPTIONS).get(title_label, 0)
        content_items = self._question_editor_content_items()
        supplement_items = self._question_editor_content_items(self.question_supplement_input)
        supplement = "".join(
            str(value) for kind, value in supplement_items if kind == "text"
        )
        selected_text = "".join(
            str(value) for kind, value in content_items if kind == "text"
        )
        if not selected_text and self.question_selected_text_input:
            selected_text = self.question_selected_text_input.get("1.0", "end-1c")
        if not normalize_note_text(selected_text):
            self.question_save_status.configure(text="选中的文本不能为空")
            return
        # 标签分两路走。概述里的标签进大纲的标题行，所以先从概述正文里删掉 ——
        # 由写入端按规范写法统一拼到标题行末尾，不删就会写两遍。原文与内容补充
        # 里的标签留在正文原位：它们是给正文定位用的，不往标题行抄。两边用到的
        # 标签都进标签池，换库或库还没扫到时补全列表也不是空的。
        tags = self._harvest_question_tags((question,))
        pool_tags = self._harvest_question_tags((selected_text, supplement, question))
        if ui_tags is not None:
            question = ui_tags.strip_tags(question)
        self._remember_question_tags(pool_tags)
        self.question_save_revision += 1
        revision = self.question_save_revision
        self.question_save_status.configure(text="正在写入…")
        if self.question_save_button and self.question_save_button.winfo_exists():
            self.question_save_button.state(["disabled"])
        output_path = resolve_question_output_path(self.config.values)
        body_root = Path(
            self.question_body_dir_var.get().strip()
            if self.question_body_dir_var
            else resolve_question_body_dir(self.config.values)
        ).expanduser()
        mode_label = self.question_record_mode_var.get() if self.question_record_mode_var else next(iter(QUESTION_RECORD_MODE_LABELS))
        record_mode = QUESTION_RECORD_MODE_LABELS.get(mode_label, QUESTION_RECORD_MODE_INDEPENDENT)
        include_source_link = QUESTION_LINK_MODE_LABELS.get(
            self.question_source_index_link_var.get() if self.question_source_index_link_var else "直接写入当前索引", False
        )
        include_supplement_link = QUESTION_LINK_MODE_LABELS.get(
            self.question_supplement_index_link_var.get() if self.question_supplement_index_link_var else "直接写入当前索引", False
        )
        def worker() -> None:
            try:
                QuestionDocumentWriter.append_record(
                    output_path,
                    body_root,
                    question,
                    selected_text,
                    title_level,
                    content_items=content_items,
                    supplement=supplement,
                    record_mode=record_mode,
                    include_index_source_link=include_source_link,
                    include_supplement_link=include_supplement_link,
                    supplement_items=supplement_items,
                    tags=tags,
                )
                self.events.put(("question_save_done", revision, "问题记录已写入"))
            except (DocumentError, OSError) as exc:
                self.events.put(("question_save_error", revision, str(exc)))
            except Exception as exc:
                self.events.put(("question_save_error", revision, f"写入失败：{exc}"))

        threading.Thread(target=worker, name="question-document-write", daemon=True).start()

    def _show_question_save_result(self, revision: int, message: str) -> None:
        if revision != self.question_save_revision or not self.question_save_status:
            return
        self.question_save_status.configure(text=message)
        if self.question_save_button and self.question_save_button.winfo_exists():
            self.question_save_button.state(["!disabled"])
        self.root.after(900, self._close_popup)

    def _show_question_save_error(self, revision: int, message: str) -> None:
        if revision != self.question_save_revision or not self.question_save_status:
            return
        self.question_save_status.configure(text=message)
        if self.question_save_button and self.question_save_button.winfo_exists():
            self.question_save_button.state(["!disabled"])

    def _create_popup(self, sentence: str, point: tuple[int, int]) -> None:
        if self.lookup_popup and self.lookup_popup.winfo_exists():
            self._destroy_popup_character_actor()
            self.lookup_popup.destroy()
        available_assets = [
            asset_dir
            for asset_dir in POPUP_CHARACTER_DIRS
            if popup_character_available(asset_dir)
        ]
        if available_assets:
            available_weights = [
                POPUP_CHARACTER_WEIGHTS[index]
                for index, asset_dir in enumerate(POPUP_CHARACTER_DIRS)
                if asset_dir in available_assets
            ]
            self.popup_character_asset_dir = random.choices(
                available_assets,
                weights=available_weights,
                k=1,
            )[0]
        else:
            self.popup_character_asset_dir = None
        popup = tk.Toplevel(self.root)
        self.lookup_popup = popup
        popup.title("选择要收录的单词")
        popup.attributes("-topmost", True)
        popup.geometry(self._popup_geometry(point, 820, 560))
        popup.minsize(*(self._px(v) for v in (760, 420)))
        popup.protocol("WM_DELETE_WINDOW", self._close_popup)
        popup.configure(background=self._token("bg_primary"))
        self._darken_titlebar(popup)

        header = ttk.Frame(
            popup,
            padding=(self._px(20), self._px(18), self._px(20), self._px(12)),
        )
        header.pack(fill="x")
        ttk.Label(header, text="句子词汇", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text=sentence,
            style="Muted.TLabel",
            wraplength=self._px(760),
            justify="left",
        ).pack(anchor="w", pady=(self._px(6), 0))
        separator = ttk.Separator(popup)
        separator.pack(fill="x")
        self.popup_body = ttk.Frame(popup, padding=self._px(20))
        self.popup_body.pack(fill="both", expand=True)
        self.loading_label = ttk.Label(self.popup_body, text="正在查询本地词典…")
        self.loading_label.pack(expand=True)
        popup.lift()

    def _popup_geometry(self, point: tuple[int, int], width: int, height: int) -> str:
        # 调用方给的是逻辑尺寸，屏幕尺寸在声明 DPI 感知后是物理像素，这里统一
        width, height = self._px(width), self._px(height)
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = min(max(12, point[0] + 14), max(12, screen_width - width - 12))
        y = min(max(12, point[1] + 14), max(12, screen_height - height - 70))
        return f"{width}x{height}+{x}+{y}"

    def _fit_popup_height(self, popup: tk.Toplevel, point: tuple[int, int],
                          width: int) -> None:
        """按内容重算弹窗高度。

        按编辑区的初始行数测量首次需要的空间，避免底部留下大块空白。
        创建后由可拖动分隔条分配补充/原文的高度，窗口放大时编辑区跟着增长。

        宽度是逻辑像素；量出来的高度已经是物理像素，所以不再乘缩放系数。
        """
        popup.update_idletasks()
        needed = popup.winfo_reqheight()
        screen_height = self.root.winfo_screenheight()
        height = min(max(needed, self._px(420)), screen_height - self._px(90))
        x = popup.winfo_x()
        y = min(max(12, point[1] + 14), max(12, screen_height - height - 70))
        popup.geometry(f"{self._px(width)}x{height}+{x}+{y}")
        # 保留首次布局的最小窗口大小；分隔条调整的是内部空间分配。
        popup.minsize(self._px(width), height)

    def _clear_popup_body(self) -> bool:
        if not self.lookup_popup or not self.lookup_popup.winfo_exists():
            return False
        self._destroy_popup_character_actor()
        for child in self.popup_body.winfo_children():
            child.destroy()
        return True

    def _mount_popup_character(self, host: tk.Misc, corner: str = "se",
                               margin: tuple[int, int] | None = None,
                               relx: float | None = None,
                               rely: float | None = None) -> None:
        """把人物挂到 host 上，作为浮层盖在内容之上。

        用 place 而不是 pack：人物不参与布局，显隐它不会挪动任何控件。需要给
        人让出位置的地方，由调用方自己留出空白（比如原文编辑框右边那一列），
        这样「人占多宽」这件事写在明面上，不靠画布方块去撑。
        """
        if CharacterActor is None or not self.popup_character_asset_dir:
            return
        # 人物是抠图，底色要跟它落的那块容器一致（弹窗底 / 结果区都是 bg_primary）
        background = self._token("bg_primary")
        actor = CharacterActor(
            host,
            self.popup_character_asset_dir,
            background=background,
            scale=self.ui_scale,
            display_width=POPUP_CHARACTER_DISPLAY_WIDTH,
        )
        if not actor.canvas:            # 素材缺失或读不出来，静默不显示
            return
        actor.attach(corner, margin=margin or (0, 0), relx=relx, rely=rely)
        self.popup_character_actor = actor
        self.root.after_idle(actor.play)

    def _destroy_popup_character_actor(self) -> None:
        if self.popup_character_actor:
            self.popup_character_actor.destroy()
            self.popup_character_actor = None

    def _show_lookup_error(self, revision: int, sentence: str, message: str) -> None:
        if revision != self.lookup_revision or sentence != self.last_sentence or not self._clear_popup_body():
            return
        center = ttk.Frame(self.popup_body)
        center.pack(expand=True, fill="both")
        ttk.Label(center, text="暂时无法生成词条", style="Word.TLabel").pack(pady=(45, 8))
        ttk.Label(center, text=message, style="Muted.TLabel",
                  wraplength=self._px(540), justify="center").pack()
        actions = ttk.Frame(center)
        actions.pack(pady=24)
        ttk.Button(actions, text="打开设置", command=self._open_settings_from_popup).pack(
            side="left", padx=4
        )
        ttk.Button(
            actions,
            text="重试",
            command=lambda: self._retry_lookup(sentence),
        ).pack(side="left", padx=4)
        ttk.Button(actions, text="关闭", command=self._close_popup).pack(side="left", padx=4)

    def _retry_lookup(self, sentence: str) -> None:
        point = self.last_pointer
        self._close_popup()
        self.root.after(50, lambda: self._begin_lookup(sentence, point))

    def _open_settings_from_popup(self) -> None:
        self._close_popup()
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _show_results(
        self, revision: int, sentence: str, items: list[dict[str, str]]
    ) -> None:
        if revision != self.lookup_revision or sentence != self.last_sentence or not self._clear_popup_body():
            return
        prepared_key = tuple(normalize_index_word(item["word"]) for item in items)
        self.cache[(sentence, prepared_key)] = items
        selected: list[tk.BooleanVar] = []

        results_area = ttk.Frame(self.popup_body)
        results_area.pack(fill="both", expand=True)

        canvas_frame = ttk.Frame(results_area)
        canvas_frame.pack(side="left", fill="both", expand=True)
        canvas = tk.Canvas(canvas_frame, highlightthickness=0,
                           background=self._token("bg_primary"))
        scrollbar = ttk.Scrollbar(canvas_frame, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas, padding=(2, 0, 12, 0))
        window = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def resize_content(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(window, width=canvas.winfo_width())

        content.bind("<Configure>", resize_content)
        canvas.bind("<Configure>", resize_content)
        canvas.bind_all(
            "<MouseWheel>",
            lambda event: canvas.yview_scroll(int(-1 * (event.delta / 120)), "units"),
        )

        for index, item in enumerate(items):
            variable = tk.BooleanVar(value=True)
            selected.append(variable)
            row = ttk.Frame(content, padding=(0, 10))
            row.pack(fill="x")
            top = ttk.Frame(row)
            top.pack(fill="x")
            self._flat_checkbox(top, text="", variable=variable).pack(side="left", anchor="n")
            title = f"{item['word']}  {item['phonetic']}  {item['part_of_speech']}"
            ttk.Label(top, text=title, style="Word.TLabel").pack(side="left", padx=(4, 0))
            ttk.Label(row, text=item["meaning_zh"], wraplength=self._px(500),
                      justify="left").pack(
                anchor="w", padx=(self._px(28), 0), pady=(self._px(3), 0)
            )
            ttk.Label(
                row,
                text=f"用法：{item['usage_zh']}",
                style="Muted.TLabel",
                wraplength=self._px(500),
                justify="left",
            ).pack(anchor="w", padx=(self._px(28), 0), pady=(self._px(3), 0))
            ttk.Label(
                row,
                text=f"例句：{item['example']}  {item['example_zh']}",
                style="Muted.TLabel",
                wraplength=self._px(500),
                justify="left",
            ).pack(anchor="w", padx=(self._px(28), 0), pady=(self._px(3), 0))
            ignore_actions = ttk.Frame(row)
            ignore_actions.pack(fill="x", padx=(self._px(28), 0),
                                pady=(self._px(7), 0))
            ignore_button = ttk.Menubutton(ignore_actions, text="下次免翻译")
            ignore_menu = tk.Menu(ignore_button, tearoff=False)
            ignore_status = ttk.Label(ignore_actions, text="", style="Muted.TLabel")
            for duration_label, duration_seconds in IGNORE_DURATION_OPTIONS:
                ignore_menu.add_command(
                    label=duration_label,
                    command=lambda word=item["word"], seconds=duration_seconds,
                    label=ignore_status: self._set_result_ignore_duration(
                        word, seconds, label
                    ),
                )
            ignore_button.configure(menu=ignore_menu)
            ignore_button.pack(side="left")
            ignore_status.pack(side="left", padx=(8, 0))
            if index < len(items) - 1:
                ttk.Separator(content).pack(fill="x")

        actions = ttk.Frame(self.popup_body)
        actions.pack(fill="x", pady=(self._px(14), 0))

        def set_all(value: bool) -> None:
            for variable in selected:
                variable.set(value)

        ttk.Button(actions, text="全选", command=lambda: set_all(True)).pack(side="left")
        ttk.Button(actions, text="全不选", command=lambda: set_all(False)).pack(
            side="left", padx=(self._px(6), 0)
        )
        self.save_status = ttk.Label(actions, text="", style="Muted.TLabel")
        self.save_status.pack(side="left", padx=self._px(12))
        ttk.Button(actions, text="关闭", command=self._close_popup).pack(side="right")
        # 这一屏的主操作，用强调色跟「全选/关闭」那些次级动作分开
        self.save_button = ttk.Button(
            actions,
            text="加入单词本",
            style="Accent.TButton",
            command=lambda: self._save_selected(items, selected, sentence),
        )
        self.save_button.pack(side="right", padx=(0, 8))
        if self.popup_character_asset_dir:
            # 词条文字按 wraplength 折行，右侧那一条本来就是空的，人物浮在这里
            # 不压到任何词条；底部右侧是按钮，所以放右上角。
            self._mount_popup_character(results_area, "ne", POPUP_CHARACTER_MARGIN)

    def _save_selected(
        self,
        items: list[dict[str, str]],
        selected: list[tk.BooleanVar],
        sentence: str,
    ) -> None:
        chosen = [item for item, variable in zip(items, selected) if variable.get()]
        if not chosen:
            self.save_status.configure(text="请至少选择一个单词")
            return
        self.save_revision += 1
        revision = self.save_revision
        self.save_status.configure(text="正在后台检查重复词…")
        if self.save_button and self.save_button.winfo_exists():
            self.save_button.state(["disabled"])
        output_path = str(resolve_output_path(self.config.values))

        def worker() -> None:
            try:
                result = self.word_index.append_unique(output_path, chosen, sentence)
                self.events.put(("save_done", revision, sentence, result))
            except (DocumentError, OSError, sqlite3.Error) as exc:
                self.events.put(("save_error", revision, sentence, str(exc)))
            except Exception as exc:
                self.events.put(("save_error", revision, sentence, f"写入失败：{exc}"))

        threading.Thread(target=worker, name="vocabulary-index-write", daemon=True).start()

    def _show_save_result(self, revision: int, sentence: str, result: WriteResult) -> None:
        if revision != self.save_revision or sentence != self.last_sentence:
            return
        if not self.lookup_popup or not self.lookup_popup.winfo_exists():
            return
        added_count = len(result.added)
        duplicate_count = len(result.duplicates)
        if added_count and duplicate_count:
            message = f"已加入 {added_count} 个，跳过重复 {duplicate_count} 个"
        elif added_count:
            message = f"已加入 {added_count} 个单词"
        else:
            message = f"所选单词均已存在，跳过 {duplicate_count} 个"
        self.save_status.configure(text=message)
        self._refresh_stats_tree()
        if self.save_button and self.save_button.winfo_exists():
            self.save_button.state(["!disabled"])
        self.root.after(1400, self._close_popup)

    def _show_save_error(self, revision: int, sentence: str, message: str) -> None:
        if revision != self.save_revision or sentence != self.last_sentence:
            return
        if not self.lookup_popup or not self.lookup_popup.winfo_exists():
            return
        self.save_status.configure(text=message)
        if self.save_button and self.save_button.winfo_exists():
            self.save_button.state(["!disabled"])

    def _close_popup(self) -> None:
        self._destroy_popup_character_actor()
        # 补全弹层是独立的 Toplevel，父窗口关掉不会自己消失，要先收掉
        for support in self.question_tag_supports:
            support.destroy()
        self.question_tag_supports = []
        if self.lookup_popup and self.lookup_popup.winfo_exists():
            try:
                self.lookup_popup.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            self.lookup_popup.destroy()
        self.lookup_popup = None
        self.popup_character_asset_dir = None
        self.question_input = None
        self.question_supplement_input = None
        self.question_editor_panes = None
        self.question_code_target = None
        self.question_image_target = None
        self.question_image_insert_index = None
        self.question_source_button = None
        self.question_source_menu = None
        self.question_selected_text_input = None
        self.question_images = []
        self.question_sources = []
        self.question_image_selected_index = None
        self.question_image_selected_widget = None
        self.question_image_status_host = None
        self.question_image_status_label = None
        self.question_image_delete_button = None
        self.question_title_var = None
        self.question_save_button = None
        self.question_save_status = None
        self.question_tag_combo = None
        self.question_tag_var = None
        self.question_tag_target = None
        self.detected_source_context = None
        self.source_detection_pending = False
        self.source_detection_revision = -1
        self.source_detection_error = ""
        self.source_mark_requested_revision = -1
        self.source_auto_mark_requested_revision = -1
        self.capture_paused = False

    def _exit(self) -> None:
        self.monitoring = False
        stop = getattr(self, "_native_mode_listener_stop", None)
        if stop is not None:
            stop.set()
        self.capture.stop()
        # 悬浮入口是独立的 Toplevel（无边框，不进窗口树），先自己收掉再销毁 root
        if self.float_entry is not None:
            self.float_entry.destroy()
            self.float_entry = None
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    guard = None
    try:
        guard = SingleInstanceGuard()
        WordPickerApp().run()
        return 0
    except AppError as exc:
        try:
            messagebox.showerror(APP_NAME, str(exc))
        except tk.TclError:
            print(f"{APP_NAME}: {exc}")
        return 1
    finally:
        if guard:
            guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
