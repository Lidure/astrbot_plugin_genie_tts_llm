"""运行日志与合成记录：把插件自己的日志行和每次合成的结构化信息留在内存里。

WebUI 的「日志」面板靠这里回答两个问题：

* 这一条语音，LLM 原话是什么、最后送去合成的是什么、用了哪个情感、情感是谁定的？
* 哪些情感容易失败、耗时异常，值得换一条参考音频？

三个部分：

``RunLogBuffer``
    一个 ``logging.Handler``。挂到 astrbot 的 logger 上之后，只保留由本插件目录下的
    文件产生的记录（按 ``record.pathname`` 判定），因此现有的日志调用一行都不用改。
    每条记录会按正则匹配出一个中文分类标签，便于在面板里筛选。

``SynthLog`` / ``SynthTrace``
    结构化的「合成记录」。调用点先 ``begin()`` 拿一个 trace，用 ``set()`` 往里补
    字段，最后用 ``ok()`` / ``fail()`` / ``skip()`` 定稿。开关关掉时
    ``begin()`` 返回一个空实现，调用点不需要写 if。

``RunLog``
    上面两者的外壳，插件只持有这一个对象。

本模块不硬依赖 astrbot 或 httpx，可以脱离宿主单独做单元测试。
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

try:  # pragma: no cover - 仅为与 tts_engine 的采样率保持一致
    from .tts_engine import BYTES_PER_SAMPLE, CHANNELS, SAMPLE_RATE
except Exception:  # pragma: no cover - 单测直接 import 本模块时走这里
    BYTES_PER_SAMPLE = 2
    CHANNELS = 1
    SAMPLE_RATE = 32000


# 判定「这条日志是本插件打的」：record.pathname 就是真实调用方的文件路径，
# 比消息前缀可靠得多（本插件的日志前缀历史上并不统一）。
PLUGIN_PATH_MARKER = "astrbot_plugin_genie_tts_llm"
# 兜底判据：万一某条日志被宿主代理转发，pathname 会指向别处。
MESSAGE_PREFIXES = ("Genie TTS", "LLM TTS 插件")

DEFAULT_CAPACITY = 500
MIN_CAPACITY = 50
MAX_CAPACITY = 5000

DEFAULT_SYNTH_CAPACITY = 200
MIN_SYNTH_CAPACITY = 20
MAX_SYNTH_CAPACITY = 2000

# 摘要模式下每个文本字段保留的长度，以及全文模式下的硬上限。
PREVIEW_TEXT_LENGTH = 160
MAX_TEXT_LENGTH = 4000
# 短字段（角色/情感/路径一类）统一的长度上限。
MAX_LABEL_LENGTH = 400

LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
ISSUE_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
# 面板里的「只看问题」伪级别。
ISSUE_LEVEL_KEY = "ISSUE"

# 日志分类：顺序即匹配优先级，先具体后宽泛，第一个命中的即为分类。
TAG_PATTERNS: Tuple[Tuple[str, str, Any], ...] = (
    ("webui", "WebUI", re.compile(r"WebUI|界面偏好|工作台")),
    ("pack", "感情包", re.compile(r"感情包|感情导出|感情导入|快照")),
    (
        "library",
        "感情库",
        re.compile(r"感情文件|emotions\.json|感情库|感情配置|注册感情|删除感情"),
    ),
    ("translate", "翻译", re.compile(r"翻译|译文|Provider ID")),
    ("emotion", "情感", re.compile(r"情感|\[emotion=")),
    ("prompt", "提示词", re.compile(r"提示词|注入|内部标签")),
    (
        "trigger",
        "触发",
        re.compile(
            r"已按时间间隔跳过|已按随机概率跳过|触发模式|已由 LLM 主动语音工具发送"
        ),
    ),
    ("server", "服务器", re.compile(r"服务器|保活|keepalive|Space", re.IGNORECASE)),
    ("queue", "队列", re.compile(r"队列|Worker-")),
    (
        "chunk",
        "分段停顿",
        re.compile(r"切分|语音块|块 \d|停顿|静音|合并|补充句末标点"),
    ),
    ("synth", "合成", re.compile(r"合成|音频|TTS文本|清洗|截断|朗读")),
    ("session", "会话", re.compile(r"会话|群组|白名单|黑名单|开关")),
    ("lifecycle", "生命周期", re.compile(r"插件已加载|插件已卸载|已注册")),
)
GENERAL_TAG_KEY = "general"
GENERAL_TAG_LABEL = "其它"

TAG_LABELS: Dict[str, str] = {key: label for key, label, _ in TAG_PATTERNS}
TAG_LABELS[GENERAL_TAG_KEY] = GENERAL_TAG_LABEL

# 从日志正文里抽会话 ID：插件里有 "会话 [x]" / "群组 [x]" / 行首 "[x]" 三种写法。
SESSION_PATTERNS = (
    re.compile(r"会话\s*\[([^\]]{1,120})\]"),
    re.compile(r"群组\s*\[([^\]]{1,120})\]"),
    re.compile(r"^\[([^\]]{1,120})\]"),
)


# --------------------------------------------------------------- 合成记录字段

# 合成来源 -> 面板上显示的中文标签。
SYNTH_SOURCES: Dict[str, str] = {
    "auto": "自动配音",
    "tool": "主动语音",
    "command": "指令合成",
    "context": "固定情感",
    "webui": "工作台",
}
SYNTH_STATUSES: Dict[str, str] = {
    "pending": "进行中",
    "ok": "成功",
    "failed": "失败",
    "skipped": "跳过",
}

# 会按文本长度上限裁剪的字段。
SYNTH_TEXT_FIELDS = frozenset(
    {"llm_text", "tts_text", "translated_text", "display_text", "ref_text", "reason"}
)
SYNTH_INT_FIELDS = frozenset({"audio_bytes", "chunks", "text_chars", "retries"})
SYNTH_FLOAT_FIELDS = frozenset({"audio_seconds"})
SYNTH_BOOL_FIELDS = frozenset({"translated", "truncated"})
# 允许写入的字段全集；不在表里的会被收进 extra，不会静默丢掉。
SYNTH_FIELDS = frozenset(
    {
        "session",
        "group",
        "character",
        "emotion",
        "emotion_source",
        "candidates",
        "workflow",
        "language",
        "ref_audio",
        "output_mode",
        "audio_path",
    }
    | SYNTH_TEXT_FIELDS
    | SYNTH_INT_FIELDS
    | SYNTH_FLOAT_FIELDS
    | SYNTH_BOOL_FIELDS
)


# ------------------------------------------------------------------ 小工具


def _clamp(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except Exception:
        number = low
    return max(low, min(high, number))


def _clip(value: Any, limit: int) -> str:
    """转成字符串并裁剪长度；裁掉的部分用省略号标记。"""
    if value is None:
        return ""
    text = str(value)
    if limit > 0 and len(text) > limit:
        return text[:limit] + "…"
    return text


def _to_int(value: Any) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return 0


def _to_float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except Exception:
        return 0.0


def wav_seconds(size_bytes: Any) -> float:
    """按裸 PCM 估算 WAV 时长；44 字节文件头的误差可以忽略。"""
    frame = max(1, SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE)
    return round(max(0, _to_int(size_bytes) - 44) / frame, 3)


def classify(message: str) -> Tuple[str, str]:
    """给一条日志正文挑一个分类标签。"""
    text = message or ""
    for key, label, pattern in TAG_PATTERNS:
        if pattern.search(text):
            return key, label
    return GENERAL_TAG_KEY, GENERAL_TAG_LABEL


def extract_session(message: str) -> str:
    """尽力从日志正文里抽出会话 ID，抽不到就返回空串。"""
    text = message or ""
    for pattern in SESSION_PATTERNS:
        matched = pattern.search(text)
        if not matched:
            continue
        token = (matched.group(1) or "").strip()
        # "[emotion=开心]" / "[pause=300ms]" 这类内部标签不是会话 ID。
        if not token or "=" in token or len(token) > 100:
            continue
        return token
    return ""


def _time_fields(moment: float) -> Dict[str, Any]:
    local = time.localtime(moment)
    return {
        "ts": round(moment, 3),
        "time": time.strftime("%H:%M:%S", local),
        "date": time.strftime("%m-%d", local),
    }


# ------------------------------------------------------------ 运行日志缓冲


class RunLogBuffer(logging.Handler):
    """把本插件产生的日志行存进定长环形缓冲，供 WebUI 查询。"""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        super().__init__(level=logging.DEBUG)
        self._entries: Deque[Dict[str, Any]] = deque(
            maxlen=_clamp(capacity, MIN_CAPACITY, MAX_CAPACITY)
        )
        self._guard = threading.RLock()
        self._next_id = 1
        # 累计收到多少条（含已被挤出缓冲的），面板上用来提示"更早的已滚出"。
        self.total = 0

    @property
    def capacity(self) -> int:
        return int(self._entries.maxlen or DEFAULT_CAPACITY)

    # -------------------------------------------------------- Handler 接口

    def emit(self, record: logging.LogRecord) -> None:
        # 日志链路上的异常绝不能冒出去，否则会把宿主的日志系统搞坏。
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(getattr(record, "msg", ""))
            if not self._accept(record, message):
                return
            entry = self._build(record, message)
        except Exception:
            return
        with self._guard:
            entry["id"] = self._next_id
            self._next_id += 1
            self.total += 1
            self._entries.append(entry)

    @staticmethod
    def _accept(record: logging.LogRecord, message: str) -> bool:
        pathname = str(getattr(record, "pathname", "") or "").replace("\\", "/")
        if PLUGIN_PATH_MARKER in pathname:
            return True
        return bool(message) and message.startswith(MESSAGE_PREFIXES)

    @staticmethod
    def _build(record: logging.LogRecord, message: str) -> Dict[str, Any]:
        text = (message or "").strip()
        exc_info = getattr(record, "exc_info", None)
        if exc_info and len(exc_info) > 1 and exc_info[1] is not None:
            detail = type(exc_info[1]).__name__ + ": " + str(exc_info[1])
            text = (text + " | " + detail).strip()
        tag, label = classify(text)
        created = float(getattr(record, "created", 0.0) or time.time())
        entry: Dict[str, Any] = {"id": 0}
        entry.update(_time_fields(created))
        entry["level"] = str(getattr(record, "levelname", "INFO") or "INFO").upper()
        entry["tag"] = tag
        entry["tag_label"] = label
        entry["session"] = extract_session(text)
        entry["message"] = _clip(text, MAX_TEXT_LENGTH)
        entry["source"] = (
            os.path.basename(str(getattr(record, "pathname", "") or "?"))
            + ":"
            + str(getattr(record, "lineno", 0))
        )
        extra = getattr(record, "genie", None)
        if isinstance(extra, dict) and extra:
            entry["extra"] = {
                str(key): _clip(value, MAX_LABEL_LENGTH) for key, value in extra.items()
            }
        return entry

    # ------------------------------------------------------------ 查询

    def query(
        self,
        limit: int = 100,
        offset: int = 0,
        level: str = "",
        tag: str = "",
        search: str = "",
        session: str = "",
    ) -> Tuple[List[Dict[str, Any]], int]:
        """倒序（最新在前）过滤 + 分页。返回 (当页条目, 过滤后总数)。"""
        with self._guard:
            items = list(self._entries)
        items.reverse()

        wanted_level = str(level or "").strip().upper()
        wanted_tag = str(tag or "").strip()
        wanted_session = str(session or "").strip()
        needle = str(search or "").strip().lower()

        picked: List[Dict[str, Any]] = []
        for entry in items:
            if wanted_level and wanted_level != "ALL":
                if wanted_level == ISSUE_LEVEL_KEY:
                    if entry["level"] not in ISSUE_LEVELS:
                        continue
                elif entry["level"] != wanted_level:
                    continue
            if wanted_tag and wanted_tag != "all" and entry["tag"] != wanted_tag:
                continue
            if wanted_session and entry.get("session") != wanted_session:
                continue
            if needle:
                haystack = " ".join(
                    (
                        entry.get("message", ""),
                        entry.get("source", ""),
                        entry.get("tag_label", ""),
                        entry.get("session", ""),
                    )
                ).lower()
                if needle not in haystack:
                    continue
            picked.append(entry)

        total = len(picked)
        start = max(0, _to_int(offset))
        stop = start + _clamp(limit or 100, 1, MAX_CAPACITY)
        return [dict(item) for item in picked[start:stop]], total

    def facets(self) -> Dict[str, Any]:
        """给前端下拉框用的分类/级别清单与计数。"""
        with self._guard:
            items = list(self._entries)
        levels: Dict[str, int] = {}
        tags: Dict[str, int] = {}
        for entry in items:
            levels[entry["level"]] = levels.get(entry["level"], 0) + 1
            tags[entry["tag"]] = tags.get(entry["tag"], 0) + 1
        issues = sum(count for name, count in levels.items() if name in ISSUE_LEVELS)
        return {
            "size": len(items),
            "capacity": self.capacity,
            "total": self.total,
            "issues": issues,
            "levels": [
                {"key": name, "count": levels[name]}
                for name in LEVEL_NAMES
                if name in levels
            ],
            "tags": [
                {"key": key, "label": TAG_LABELS.get(key, key), "count": count}
                for key, count in sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))
            ],
        }

    def clear(self) -> int:
        with self._guard:
            dropped = len(self._entries)
            self._entries.clear()
        return dropped

    def export_text(self, limit: int = 0, **filters: Any) -> str:
        """导出成纯文本，方便贴到 issue 里。"""
        items, total = self.query(limit=limit or MAX_CAPACITY, **filters)
        head = [
            "# Genie TTS 运行日志",
            "# 导出时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "# 本次导出 " + str(len(items)) + " 条，过滤后共 " + str(total) + " 条",
            "",
        ]
        body = [
            " | ".join(
                (
                    entry["date"] + " " + entry["time"],
                    entry["level"].ljust(8),
                    entry["tag_label"],
                    entry["source"],
                    entry["message"],
                )
            )
            for entry in reversed(items)
        ]
        return "\n".join(head + body) + "\n"


# ---------------------------------------------------------------- 合成记录


class SynthTrace:
    """一次合成的记录句柄。所有方法都返回 self，方便串起来写。"""

    __slots__ = ("_log", "_record", "_started", "_finished")

    def __init__(self, log: "SynthLog", record: Dict[str, Any], started: float) -> None:
        self._log = log
        self._record = record
        self._started = started
        self._finished = False

    @property
    def active(self) -> bool:
        return True

    @property
    def id(self) -> int:
        return _to_int(self._record.get("id"))

    @property
    def finished(self) -> bool:
        return self._finished

    def set(self, **fields: Any) -> "SynthTrace":
        self._log.update(self._record, fields)
        return self

    def ok(self, **fields: Any) -> "SynthTrace":
        return self._finish("ok", "", fields)

    def fail(self, reason: str = "", **fields: Any) -> "SynthTrace":
        return self._finish("failed", reason, fields)

    def skip(self, reason: str = "", **fields: Any) -> "SynthTrace":
        return self._finish("skipped", reason, fields)

    def _finish(self, status: str, reason: str, fields: Dict[str, Any]) -> "SynthTrace":
        payload = dict(fields)
        if reason:
            payload["reason"] = reason
        if self._finished:
            # 重复收尾时只补字段、不改状态：调用点两条分支都收尾也不会互相盖掉。
            self._log.update(self._record, payload)
            return self
        self._finished = True
        elapsed = max(0, int(round((time.time() - self._started) * 1000)))
        self._log.finish(self._record, status, elapsed, payload)
        return self


class _NullTrace:
    """开关关闭时返回的空实现，让调用点保持一行、不用写 if。"""

    __slots__ = ()

    @property
    def active(self) -> bool:
        return False

    @property
    def id(self) -> int:
        return 0

    @property
    def finished(self) -> bool:
        return True

    def set(self, **fields: Any) -> "_NullTrace":
        return self

    def ok(self, **fields: Any) -> "_NullTrace":
        return self

    def fail(self, reason: str = "", **fields: Any) -> "_NullTrace":
        return self

    def skip(self, reason: str = "", **fields: Any) -> "_NullTrace":
        return self


NULL_TRACE = _NullTrace()


def _normalize_candidates(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [chunk.strip() for chunk in value.split(",")]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = [str(item).strip() for item in value]
    elif isinstance(value, dict):
        items = [str(item).strip() for item in value.keys()]
    else:
        items = [str(value).strip()]
    return [item for item in items if item][:24]


def _summarize_counts(counts: Dict[str, int], labels: Dict[str, str]) -> str:
    """把 {key: count} 压成 "自动配音×3 / 主动语音×1" 这样的一行摘要。"""
    if not counts:
        return ""
    parts = [
        (labels.get(key, key) or key) + "×" + str(count)
        for key, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return " / ".join(parts[:4])


class SynthLog:
    """结构化合成记录的环形缓冲，并能按 (角色, 情感) 聚合成功率。"""

    def __init__(
        self,
        capacity: int = DEFAULT_SYNTH_CAPACITY,
        full_text: bool = True,
        enabled: bool = True,
    ) -> None:
        self._entries: Deque[Dict[str, Any]] = deque(
            maxlen=_clamp(capacity, MIN_SYNTH_CAPACITY, MAX_SYNTH_CAPACITY)
        )
        self._guard = threading.RLock()
        self._next_id = 1
        self.total = 0
        self.enabled = bool(enabled)
        self.full_text = bool(full_text)
        self.on_finish = None

    @property
    def capacity(self) -> int:
        return int(self._entries.maxlen or DEFAULT_SYNTH_CAPACITY)

    @property
    def text_limit(self) -> int:
        return MAX_TEXT_LENGTH if self.full_text else PREVIEW_TEXT_LENGTH

    # ------------------------------------------------------------ 写入

    def begin(self, source: str = "auto", **fields: Any) -> Any:
        """开一条记录并立刻入缓冲，这样卡住的合成也能在面板上看见。"""
        if not self.enabled:
            return NULL_TRACE
        now = time.time()
        key = str(source or "auto").strip().lower()
        record: Dict[str, Any] = {"id": 0}
        record.update(_time_fields(now))
        record["source"] = key
        record["source_label"] = SYNTH_SOURCES.get(key, key or "未知")
        record["status"] = "pending"
        record["status_label"] = SYNTH_STATUSES["pending"]
        record["elapsed_ms"] = 0
        record["candidates"] = []
        for name in SYNTH_INT_FIELDS:
            record[name] = 0
        for name in SYNTH_FLOAT_FIELDS:
            record[name] = 0.0
        for name in SYNTH_BOOL_FIELDS:
            record[name] = False
        for name in SYNTH_FIELDS:
            record.setdefault(name, "")
        with self._guard:
            record["id"] = self._next_id
            self._next_id += 1
            self.total += 1
            self._entries.append(record)
        trace = SynthTrace(self, record, now)
        if fields:
            trace.set(**fields)
        return trace

    def update(self, record: Dict[str, Any], fields: Dict[str, Any]) -> None:
        if not fields:
            return
        with self._guard:
            for raw_key, value in fields.items():
                name = str(raw_key)
                if name in SYNTH_TEXT_FIELDS:
                    record[name] = _clip(value, self.text_limit)
                elif name == "candidates":
                    record[name] = _normalize_candidates(value)
                elif name in SYNTH_INT_FIELDS:
                    record[name] = _to_int(value)
                elif name in SYNTH_FLOAT_FIELDS:
                    record[name] = _to_float(value)
                elif name in SYNTH_BOOL_FIELDS:
                    record[name] = bool(value)
                elif name in SYNTH_FIELDS:
                    record[name] = _clip(value, MAX_LABEL_LENGTH)
                else:
                    # 不认识的键收进 extra，宁可多显示也不要静默丢掉。
                    extra = record.get("extra")
                    if not isinstance(extra, dict):
                        extra = {}
                        record["extra"] = extra
                    extra[name] = _clip(value, MAX_LABEL_LENGTH)
            if "tts_text" in fields:
                record["text_chars"] = len(str(fields.get("tts_text") or ""))

    def finish(
        self,
        record: Dict[str, Any],
        status: str,
        elapsed_ms: int,
        fields: Dict[str, Any],
    ) -> None:
        with self._guard:
            self.update(record, fields)
            key = str(status or "ok").strip().lower()
            record["status"] = key
            record["status_label"] = SYNTH_STATUSES.get(key, key)
            record["elapsed_ms"] = _to_int(elapsed_ms)
            self._fill_audio(record)
            snapshot = dict(record)
        if self.on_finish:
            try:
                self.on_finish(snapshot)
            except Exception:
                pass  # Observability must never fail the synthesis itself.

    def get(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._guard:
            return next((dict(r) for r in self._entries if r["id"] == record_id), None)

    @staticmethod
    def _fill_audio(record: Dict[str, Any]) -> None:
        """成功记录只给了路径时，顺手补上体积与时长。"""
        path = str(record.get("audio_path") or "")
        if not path or record.get("audio_bytes"):
            return
        try:
            size = os.path.getsize(path)
        except Exception:
            return
        if size > 0:
            record["audio_bytes"] = int(size)
            record["audio_seconds"] = wav_seconds(size)

    # ------------------------------------------------------------ 查询

    def query(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        source: str = "",
        character: str = "",
        emotion: str = "",
        session: str = "",
        search: str = "",
    ) -> Tuple[List[Dict[str, Any]], int]:
        with self._guard:
            items = list(self._entries)
        items.reverse()

        wanted_status = str(status or "").strip().lower()
        wanted_source = str(source or "").strip().lower()
        wanted_character = str(character or "").strip()
        wanted_emotion = str(emotion or "").strip()
        wanted_session = str(session or "").strip()
        needle = str(search or "").strip().lower()

        picked: List[Dict[str, Any]] = []
        for entry in items:
            if wanted_status and wanted_status != "all":
                if wanted_status == "issue":
                    if entry.get("status") not in ("failed", "skipped"):
                        continue
                elif entry.get("status") != wanted_status:
                    continue
            if wanted_source and wanted_source != "all":
                if entry.get("source") != wanted_source:
                    continue
            if wanted_character and entry.get("character") != wanted_character:
                continue
            if wanted_emotion and entry.get("emotion") != wanted_emotion:
                continue
            if wanted_session and entry.get("session") != wanted_session:
                continue
            if needle:
                haystack = " ".join(
                    str(entry.get(field, ""))
                    for field in (
                        "llm_text",
                        "tts_text",
                        "translated_text",
                        "character",
                        "emotion",
                        "emotion_source",
                        "reason",
                        "session",
                        "ref_audio",
                    )
                ).lower()
                if needle not in haystack:
                    continue
            picked.append(entry)

        total = len(picked)
        start = max(0, _to_int(offset))
        stop = start + _clamp(limit or 50, 1, MAX_SYNTH_CAPACITY)
        return [dict(item) for item in picked[start:stop]], total

    def facets(self) -> Dict[str, Any]:
        with self._guard:
            items = list(self._entries)
        statuses: Dict[str, int] = {}
        sources: Dict[str, int] = {}
        characters: Dict[str, int] = {}
        elapsed: List[int] = []
        for entry in items:
            status = str(entry.get("status") or "")
            statuses[status] = statuses.get(status, 0) + 1
            source = str(entry.get("source") or "")
            sources[source] = sources.get(source, 0) + 1
            name = str(entry.get("character") or "")
            if name:
                characters[name] = characters.get(name, 0) + 1
            if status == "ok" and entry.get("elapsed_ms"):
                elapsed.append(_to_int(entry.get("elapsed_ms")))
        done = sum(count for key, count in statuses.items() if key != "pending")
        ok = statuses.get("ok", 0)
        return {
            "size": len(items),
            "capacity": self.capacity,
            "total": self.total,
            "ok": ok,
            "failed": statuses.get("failed", 0),
            "skipped": statuses.get("skipped", 0),
            "pending": statuses.get("pending", 0),
            "success_rate": round(ok * 100.0 / done, 1) if done else 0.0,
            "avg_elapsed_ms": int(round(sum(elapsed) / len(elapsed))) if elapsed else 0,
            "full_text": self.full_text,
            "statuses": [
                {"key": key, "label": SYNTH_STATUSES.get(key, key), "count": count}
                for key, count in sorted(
                    statuses.items(), key=lambda kv: (-kv[1], kv[0])
                )
                if key
            ],
            "sources": [
                {"key": key, "label": SYNTH_SOURCES.get(key, key), "count": count}
                for key, count in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))
                if key
            ],
            "characters": [
                {"key": key, "count": count}
                for key, count in sorted(
                    characters.items(), key=lambda kv: (-kv[1], kv[0])
                )
            ],
        }

    def emotion_stats(self, limit: int = 60) -> List[Dict[str, Any]]:
        """按 (角色, 情感) 聚合，失败率高的排前面，直接回答"哪些情感不好"。"""
        with self._guard:
            items = list(self._entries)
        buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for entry in items:
            character = str(entry.get("character") or "")
            emotion = str(entry.get("emotion") or "")
            if not character and not emotion:
                continue
            key = (character, emotion)
            row = buckets.get(key)
            if row is None:
                row = {
                    "character": character,
                    "emotion": emotion,
                    "total": 0,
                    "ok": 0,
                    "failed": 0,
                    "skipped": 0,
                    "pending": 0,
                    "elapsed_sum": 0,
                    "elapsed_count": 0,
                    "chars_sum": 0,
                    "last_ts": 0.0,
                    "last_time": "",
                    "last_status": "",
                    "last_reason": "",
                    "sources": {},
                    "emotion_sources": {},
                }
                buckets[key] = row
            row["total"] += 1
            status = str(entry.get("status") or "")
            if status in ("ok", "failed", "skipped", "pending"):
                row[status] += 1
            if status == "ok" and entry.get("elapsed_ms"):
                row["elapsed_sum"] += _to_int(entry.get("elapsed_ms"))
                row["elapsed_count"] += 1
            row["chars_sum"] += _to_int(entry.get("text_chars"))
            source = str(entry.get("source") or "")
            if source:
                row["sources"][source] = row["sources"].get(source, 0) + 1
            emotion_source = str(entry.get("emotion_source") or "")
            if emotion_source:
                row["emotion_sources"][emotion_source] = (
                    row["emotion_sources"].get(emotion_source, 0) + 1
                )
            moment = _to_float(entry.get("ts"))
            if moment >= row["last_ts"]:
                row["last_ts"] = moment
                row["last_time"] = (
                    str(entry.get("date", "")) + " " + str(entry.get("time", ""))
                ).strip()
                row["last_status"] = status
                row["last_reason"] = str(entry.get("reason") or "")

        rows: List[Dict[str, Any]] = []
        for row in buckets.values():
            done = row["ok"] + row["failed"] + row["skipped"]
            bad = row["failed"] + row["skipped"]
            row["fail_rate"] = round(bad * 100.0 / done, 1) if done else 0.0
            row["avg_elapsed_ms"] = (
                int(round(row["elapsed_sum"] / row["elapsed_count"]))
                if row["elapsed_count"]
                else 0
            )
            row["avg_chars"] = (
                int(round(row["chars_sum"] / row["total"])) if row["total"] else 0
            )
            row["source_summary"] = _summarize_counts(row.pop("sources"), SYNTH_SOURCES)
            row["emotion_source_summary"] = _summarize_counts(
                row.pop("emotion_sources"), {}
            )
            row.pop("elapsed_sum", None)
            row.pop("elapsed_count", None)
            row.pop("chars_sum", None)
            rows.append(row)

        rows.sort(
            key=lambda item: (
                -item["fail_rate"],
                -item["total"],
                item["character"],
                item["emotion"],
            )
        )
        return rows[: _clamp(limit or 60, 1, MAX_SYNTH_CAPACITY)]

    def clear(self) -> int:
        with self._guard:
            dropped = len(self._entries)
            self._entries.clear()
        return dropped

    def export_text(self, limit: int = 0, **filters: Any) -> str:
        items, total = self.query(limit=limit or MAX_SYNTH_CAPACITY, **filters)
        lines = [
            "# Genie TTS 合成记录",
            "# 导出时间: " + time.strftime("%Y-%m-%d %H:%M:%S"),
            "# 本次导出 " + str(len(items)) + " 条，过滤后共 " + str(total) + " 条",
        ]
        for entry in reversed(items):
            lines.append("")
            lines.append(
                "["
                + str(entry.get("id"))
                + "] "
                + str(entry.get("date", ""))
                + " "
                + str(entry.get("time", ""))
                + "  "
                + str(entry.get("status_label", ""))
                + " / "
                + str(entry.get("source_label", ""))
                + "  "
                + str(entry.get("elapsed_ms", 0))
                + "ms"
            )
            lines.append(
                "  角色: "
                + str(entry.get("character") or "-")
                + "   情感: "
                + str(entry.get("emotion") or "-")
                + "   情感来源: "
                + str(entry.get("emotion_source") or "-")
            )
            for label, field in (
                ("会话", "session"),
                ("LLM原文", "llm_text"),
                ("译文", "translated_text"),
                ("送合成", "tts_text"),
                ("参考音频", "ref_audio"),
                ("输出模式", "output_mode"),
                ("原因", "reason"),
            ):
                value = str(entry.get(field) or "")
                if value:
                    lines.append("  " + label + ": " + value)
            if entry.get("audio_seconds"):
                lines.append(
                    "  产出: "
                    + str(entry.get("audio_seconds"))
                    + "s / "
                    + str(entry.get("audio_bytes"))
                    + " bytes"
                )
        return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 外壳


class RunLog:
    """插件持有的唯一日志对象：运行日志缓冲 + 合成记录。"""

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        synth_capacity: int = DEFAULT_SYNTH_CAPACITY,
        full_text: bool = True,
        enabled: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.buffer = RunLogBuffer(capacity)
        self.synth = SynthLog(synth_capacity, full_text=full_text, enabled=self.enabled)
        self.instance_id = uuid.uuid4().hex
        self.synth.on_finish = self._synth_finished
        self._logger: Optional[Any] = None
        self._attached = False

    def _synth_finished(self, record: Dict[str, Any]) -> None:
        """Link a completion log to the exact synthesis, never guess by timestamp."""
        if self._logger is None:
            return
        self._logger.info(
            "Genie TTS 合成记录 #%s | %s | %s · %s | 会话 [%s]",
            record["id"], record["status_label"], record.get("character", ""),
            record.get("emotion", ""), record.get("session", ""),
            extra={"genie": {"synth_id": record["id"], "instance": self.instance_id}},
        )

    @property
    def attached(self) -> bool:
        return self._attached

    def attach(self, logger_obj: Any) -> bool:
        """把缓冲挂到宿主 logger 上。失败只返回 False，绝不抛。"""
        if not self.enabled or self._attached:
            return False
        add_handler = getattr(logger_obj, "addHandler", None)
        if not callable(add_handler):
            return False
        try:
            add_handler(self.buffer)
        except Exception:
            return False
        self._logger = logger_obj
        self._attached = True
        return True

    def detach(self) -> bool:
        if not self._attached:
            return False
        target = self._logger
        self._logger = None
        self._attached = False
        remove_handler = getattr(target, "removeHandler", None)
        if callable(remove_handler):
            try:
                remove_handler(self.buffer)
            except Exception:
                pass
        try:
            self.buffer.close()
        except Exception:
            pass
        return True

    def begin_synth(self, source: str = "auto", **fields: Any) -> Any:
        return self.synth.begin(source, **fields)

    def clear(self, scope: str = "all") -> Dict[str, int]:
        key = str(scope or "all").strip().lower()
        dropped = {"logs": 0, "synths": 0}
        if key in ("all", "logs"):
            dropped["logs"] = self.buffer.clear()
        if key in ("all", "synths"):
            dropped["synths"] = self.synth.clear()
        return dropped

    def snapshot(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "attached": self._attached,
            "logs": self.buffer.facets(),
            "synths": self.synth.facets(),
        }
