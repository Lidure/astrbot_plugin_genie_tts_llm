"""感情包（emotions 模板）的校验、导入导出与合并逻辑。

WebUI 工作台与聊天指令共用这里的实现，保证两条链路对同一份 JSON 的判定
结果完全一致；任何格式错误都以 EmotionPackError 抛出，消息面向最终用户，
可以直接回显到聊天或页面上。
"""

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple

PACK_FORMAT = "genie-tts-emotions"
PACK_VERSION = 1
IMPORT_MODES = ("merge", "overwrite", "replace")
DEFAULT_IMPORT_MODE = "merge"

ENTRY_FIELDS = ("ref_audio_path", "ref_audio_text", "language")
KNOWN_LANGUAGES = ("jp", "zh", "en")

MAX_PACK_BYTES = 4 * 1024 * 1024
MAX_NAME_LENGTH = 120
MAX_TEXT_LENGTH = 2000
MAX_CHARACTERS = 500
MAX_EMOTIONS_PER_CHARACTER = 1000

# 参考音频路径必须是「相对路径」：Genie 服务端会把它拼到自己的角色目录下，
# 绝对路径和 .. 都可以被用来读取服务器上的任意文件。
_ABSOLUTE_PATH_PATTERN = re.compile(r"^(?:[a-zA-Z]:[\\/]|[\\/]{1,2}|~)")
_PARENT_SEGMENT_PATTERN = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_FILENAME_SAFE_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff._-]+")
_CODE_FENCE = "\u0060\u0060\u0060"


class EmotionPackError(ValueError):
    """感情包格式错误。异常消息面向最终用户，可直接回显。"""


# --------------------------------------------------------------------- 基础校验


def normalize_import_mode(mode: Any) -> str:
    """把用户输入的导入模式规整为合法值；无法识别时报错而不是静默改语义。"""
    if mode is None or mode == "":
        return DEFAULT_IMPORT_MODE
    normalized = str(mode).strip().lower()
    aliases = {
        "合并": "merge",
        "覆盖": "overwrite",
        "替换": "replace",
        "清空": "replace",
        "skip": "merge",
        "update": "overwrite",
        "force": "overwrite",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in IMPORT_MODES:
        raise EmotionPackError(
            "导入模式只能是 merge（合并，冲突保留现有）/ overwrite（覆盖同名）/ "
            "replace（清空后导入），收到: " + str(mode)
        )
    return normalized


def is_safe_ref_audio_path(value: Any) -> bool:
    """参考音频路径是否安全：非空、相对、无 .. 、无控制字符。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        return False
    if _CONTROL_CHAR_PATTERN.search(text):
        return False
    if _ABSOLUTE_PATH_PATTERN.match(text):
        return False
    if _PARENT_SEGMENT_PATTERN.search(text):
        return False
    return True


def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return _CONTROL_CHAR_PATTERN.sub("", value).strip()


def normalize_entry(raw: Any) -> Tuple[Optional[Dict[str, str]], str]:
    """校验单条感情记录。

    返回 (entry, reason)：entry 为 None 时 reason 说明拒绝原因。
    兼容两种写法——完整对象，或只给一个字符串当作参考音频路径。
    """
    if isinstance(raw, str):
        raw = {"ref_audio_path": raw, "ref_audio_text": ""}
    if not isinstance(raw, dict):
        return None, "记录不是 JSON 对象"

    ref_path = raw.get("ref_audio_path")
    if not is_safe_ref_audio_path(ref_path):
        if not isinstance(ref_path, str) or not ref_path.strip():
            return None, "缺少 ref_audio_path"
        return None, "ref_audio_path 必须是不含 .. 的相对路径"

    ref_text = raw.get("ref_audio_text")
    if ref_text is None:
        ref_text = ""
    if not isinstance(ref_text, str):
        return None, "ref_audio_text 必须是字符串"
    ref_text = _CONTROL_CHAR_PATTERN.sub(" ", ref_text).strip()
    if len(ref_text) > MAX_TEXT_LENGTH:
        return None, "ref_audio_text 超过 " + str(MAX_TEXT_LENGTH) + " 字"

    entry: Dict[str, str] = {
        "ref_audio_path": ref_path.strip(),
        "ref_audio_text": ref_text,
    }

    language = raw.get("language")
    if language is not None and str(language).strip():
        language_text = _clean_name(str(language)).lower()
        if len(language_text) > 16:
            return None, "language 取值过长"
        entry["language"] = language_text

    return entry, ""


def normalize_characters(
    raw: Any,
) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], List[Dict[str, str]]]:
    """校验 {角色: {感情: 记录}} 结构，返回 (干净数据, 被跳过的明细)。

    单条记录出错只跳过这一条并记账，不会让整份感情包导入失败——否则一个
    手写错误就会挡住几十条正常配置。
    """
    if not isinstance(raw, dict):
        raise EmotionPackError("characters 必须是 {角色: {感情: 配置}} 结构")
    if len(raw) > MAX_CHARACTERS:
        raise EmotionPackError("角色数量超过上限 " + str(MAX_CHARACTERS))

    cleaned: Dict[str, Dict[str, Dict[str, str]]] = {}
    invalid: List[Dict[str, str]] = []

    for raw_character, raw_emotions in raw.items():
        character = _clean_name(raw_character)
        if not character:
            invalid.append(
                {"character": str(raw_character)[:40], "emotion": "", "reason": "角色名为空"}
            )
            continue
        if len(character) > MAX_NAME_LENGTH:
            invalid.append(
                {"character": character[:40], "emotion": "", "reason": "角色名过长"}
            )
            continue
        if not isinstance(raw_emotions, dict):
            invalid.append(
                {
                    "character": character,
                    "emotion": "",
                    "reason": "角色下不是 {感情: 配置} 结构",
                }
            )
            continue
        if len(raw_emotions) > MAX_EMOTIONS_PER_CHARACTER:
            invalid.append(
                {"character": character, "emotion": "", "reason": "感情数量超过上限"}
            )
            continue

        bucket: Dict[str, Dict[str, str]] = {}
        for raw_emotion, raw_entry in raw_emotions.items():
            emotion = _clean_name(raw_emotion)
            if not emotion:
                invalid.append(
                    {
                        "character": character,
                        "emotion": str(raw_emotion)[:40],
                        "reason": "感情名为空",
                    }
                )
                continue
            if len(emotion) > MAX_NAME_LENGTH:
                invalid.append(
                    {"character": character, "emotion": emotion[:40], "reason": "感情名过长"}
                )
                continue
            entry, reason = normalize_entry(raw_entry)
            if entry is None:
                invalid.append(
                    {"character": character, "emotion": emotion, "reason": reason}
                )
                continue
            bucket[emotion] = entry

        if bucket:
            cleaned[character] = bucket

    return cleaned, invalid


# --------------------------------------------------------------------- 解析构建


def extract_characters(
    payload: Any,
) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], Dict[str, Any], List[Dict[str, str]]]:
    """从感情包对象里取出角色数据，同时兼容裸 emotions.json。"""
    if not isinstance(payload, dict):
        raise EmotionPackError("感情包顶层必须是 JSON 对象")

    meta: Dict[str, Any] = {}
    if isinstance(payload.get("characters"), dict):
        raw_characters = payload["characters"]
        declared_format = payload.get("format")
        if declared_format and str(declared_format) != PACK_FORMAT:
            raise EmotionPackError(
                "不认识的感情包格式: "
                + str(declared_format)
                + "（期望 "
                + PACK_FORMAT
                + "）"
            )
        for key in ("format", "version", "exported_at", "plugin_version", "note", "source"):
            if key in payload:
                meta[key] = payload[key]
    else:
        # 裸 emotions.json：顶层直接就是 {角色: {感情: 配置}}
        for reserved in ("format", "version", "exported_at", "plugin_version"):
            if reserved in payload:
                raise EmotionPackError("检测到感情包头部，但缺少 characters 字段")
        raw_characters = payload
        meta["format"] = "raw-emotions"

    characters, invalid = normalize_characters(raw_characters)
    return characters, meta, invalid


def loads_pack(
    text: Any,
) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], Dict[str, Any], List[Dict[str, str]]]:
    """解析感情包文本（或已解析好的 dict）。"""
    if isinstance(text, (dict, list)):
        return extract_characters(text)

    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_PACK_BYTES:
            raise EmotionPackError(
                "感情包超过 " + str(MAX_PACK_BYTES // 1024 // 1024) + "MB，已拒绝"
            )
        try:
            text = bytes(text).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise EmotionPackError("感情包不是 UTF-8 文本: " + str(exc)) from exc

    if not isinstance(text, str):
        raise EmotionPackError("感情包内容必须是 JSON 文本")

    stripped = text.strip().lstrip("\ufeff").strip()
    if not stripped:
        raise EmotionPackError("感情包内容为空")
    if len(stripped.encode("utf-8")) > MAX_PACK_BYTES:
        raise EmotionPackError(
            "感情包超过 " + str(MAX_PACK_BYTES // 1024 // 1024) + "MB，已拒绝"
        )

    # 常见误操作：把 Markdown 代码块整段粘进来。有些平台/指令解析会把换行
    # 压成空格，所以不能只按行剥围栏，得直接定位到第一个 { 或 [。
    if stripped.startswith(_CODE_FENCE):
        inner = stripped[len(_CODE_FENCE):]
        if inner.rstrip().endswith(_CODE_FENCE):
            inner = inner.rstrip()[: -len(_CODE_FENCE)]
        starts = [pos for pos in (inner.find("{"), inner.find("[")) if pos >= 0]
        if starts:
            # 跳过 ```json / ```JSON5 之类的语言标注
            inner = inner[min(starts):]
        stripped = inner.strip()
        if not stripped:
            raise EmotionPackError("感情包内容为空（代码块里什么都没有）")

    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise EmotionPackError(
            "JSON 解析失败: 第 " + str(exc.lineno) + " 行 " + exc.msg
        ) from exc

    return extract_characters(payload)


def select_characters(
    characters: Dict[str, Dict[str, Dict[str, str]]],
    only_characters: Optional[List[str]] = None,
    only_items: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Dict[str, Dict[str, str]]]:
    """按角色名或 [{character, emotion}] 明细筛出要导出的子集。"""
    if not only_characters and not only_items:
        return {name: dict(emotions) for name, emotions in characters.items()}

    picked: Dict[str, Dict[str, Dict[str, str]]] = {}
    for name in only_characters or []:
        clean = _clean_name(name)
        if clean and clean in characters:
            picked.setdefault(clean, {}).update(characters[clean])
    for item in only_items or []:
        if not isinstance(item, dict):
            continue
        character = _clean_name(item.get("character"))
        emotion = _clean_name(item.get("emotion"))
        if not character or character not in characters:
            continue
        if not emotion:
            picked.setdefault(character, {}).update(characters[character])
            continue
        entry = characters[character].get(emotion)
        if entry:
            picked.setdefault(character, {})[emotion] = entry
    return picked


def build_pack(
    characters: Dict[str, Dict[str, Dict[str, str]]],
    plugin_version: str = "",
    note: str = "",
    source: str = "",
) -> Dict[str, Any]:
    """组装带头部信息的感情包对象。角色与感情都按名称排序，方便 diff。"""
    pack: Dict[str, Any] = {
        "format": PACK_FORMAT,
        "version": PACK_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "plugin_version": str(plugin_version or ""),
        "characters": {
            name: {emotion: dict(entry) for emotion, entry in sorted(emotions.items())}
            for name, emotions in sorted(characters.items())
        },
    }
    if note:
        pack["note"] = str(note)[:MAX_TEXT_LENGTH]
    if source:
        pack["source"] = str(source)[:MAX_NAME_LENGTH]
    return pack


def dumps_pack(pack: Dict[str, Any]) -> str:
    return json.dumps(pack, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------- 导入合并


def _entries_equal(left: Dict[str, str], right: Dict[str, str]) -> bool:
    return all(left.get(field, "") == right.get(field, "") for field in ENTRY_FIELDS)


def compute_import(
    current: Dict[str, Dict[str, Dict[str, str]]],
    incoming: Dict[str, Dict[str, Dict[str, str]]],
    mode: str = DEFAULT_IMPORT_MODE,
    invalid: Optional[List[Dict[str, str]]] = None,
) -> Tuple[Dict[str, Dict[str, Dict[str, str]]], Dict[str, Any]]:
    """按模式合并感情数据，返回 (合并结果, 变更报告)。

    - merge：只补新条目，同名冲突保留现有值（默认，最安全）
    - overwrite：同名条目用导入值覆盖
    - replace：先清空现有数据，再整份写入
    """
    mode = normalize_import_mode(mode)

    report: Dict[str, Any] = {
        "mode": mode,
        "added": [],
        "updated": [],
        "skipped": [],
        "unchanged": [],
        "removed": [],
        "invalid": list(invalid or []),
    }

    if mode == "replace":
        merged: Dict[str, Dict[str, Dict[str, str]]] = {}
        for character, emotions in current.items():
            for emotion in emotions:
                if emotion not in incoming.get(character, {}):
                    # 带上即将被清掉的原值，UI/摘要才能告诉用户丢的是哪条参考音频
                    report["removed"].append(
                        {
                            "character": character,
                            "emotion": emotion,
                            "before": dict(emotions.get(emotion) or {}),
                        }
                    )
    else:
        merged = {name: dict(emotions) for name, emotions in current.items()}

    for character, emotions in incoming.items():
        for emotion, entry in emotions.items():
            existing = current.get(character, {}).get(emotion)
            item = {"character": character, "emotion": emotion}
            if existing is None:
                merged.setdefault(character, {})[emotion] = dict(entry)
                # 新增/跳过也带上具体条目：只报「角色 · 感情」的话，
                # 一次导入 39 条时用户根本看不出每条指向哪个参考音频
                item["after"] = dict(entry)
                report["added"].append(item)
                continue
            if _entries_equal(existing, entry):
                merged.setdefault(character, {})[emotion] = dict(entry)
                report["unchanged"].append(item)
                continue
            if mode == "merge":
                merged.setdefault(character, {})[emotion] = dict(existing)
                item["before"] = dict(existing)
                item["after"] = dict(entry)
                report["skipped"].append(item)
                continue
            merged.setdefault(character, {})[emotion] = dict(entry)
            report["updated"].append(
                {
                    "character": character,
                    "emotion": emotion,
                    "before": dict(existing),
                    "after": dict(entry),
                }
            )

    # 清掉空角色，避免 UI 里出现 0 条感情的幽灵角色
    merged = {name: emotions for name, emotions in merged.items() if emotions}

    report["counts"] = {
        key: len(report[key])
        for key in ("added", "updated", "skipped", "unchanged", "removed", "invalid")
    }
    report["changed"] = bool(
        report["counts"]["added"]
        or report["counts"]["updated"]
        or report["counts"]["removed"]
    )
    report["result"] = summarize(merged)
    return merged, report


def summarize(characters: Dict[str, Dict[str, Dict[str, str]]]) -> Dict[str, int]:
    return {
        "characters": len(characters),
        "emotions": sum(len(emotions) for emotions in characters.values()),
    }


def describe_report(report: Dict[str, Any], limit: int = 8) -> str:
    """把变更报告渲染成聊天里能直接发的中文摘要。"""
    counts = report.get("counts") or {}
    mode_label = {
        "merge": "合并（冲突保留现有）",
        "overwrite": "覆盖（冲突用导入值）",
        "replace": "替换（先清空）",
    }.get(report.get("mode"), str(report.get("mode")))

    lines = [
        "模式: " + mode_label,
        "新增 %d / 更新 %d / 跳过 %d / 无变化 %d / 移除 %d / 无效 %d"
        % (
            counts.get("added", 0),
            counts.get("updated", 0),
            counts.get("skipped", 0),
            counts.get("unchanged", 0),
            counts.get("removed", 0),
            counts.get("invalid", 0),
        ),
    ]

    def _fmt(items: List[Dict[str, Any]]) -> str:
        shown = [
            str(item.get("character", "")) + " · " + str(item.get("emotion", ""))
            for item in items[:limit]
        ]
        if len(items) > limit:
            shown.append("…（共 " + str(len(items)) + " 条）")
        return "、".join(shown)

    for key, label in (
        ("added", "新增"),
        ("updated", "更新"),
        ("skipped", "跳过"),
        ("removed", "移除"),
    ):
        items = report.get(key) or []
        if items:
            lines.append(label + ": " + _fmt(items))

    invalid = report.get("invalid") or []
    if invalid:
        detail = [
            str(item.get("character", ""))
            + " · "
            + str(item.get("emotion", ""))
            + "（"
            + str(item.get("reason", ""))
            + "）"
            for item in invalid[:limit]
        ]
        if len(invalid) > limit:
            detail.append("…（共 " + str(len(invalid)) + " 条）")
        lines.append("无效: " + "、".join(detail))

    result = report.get("result") or {}
    lines.append(
        "结果: %d 个角色 / %d 条感情"
        % (result.get("characters", 0), result.get("emotions", 0))
    )
    return "\n".join(lines)


# --------------------------------------------------------------------- 文件名


def safe_pack_filename(name: Any, fallback: str = "emotions.json") -> str:
    """把用户给的文件名压成安全的单段文件名，杜绝路径穿越。"""
    text = _clean_name(name)
    text = text.replace("\\", "/").split("/")[-1]
    text = _FILENAME_SAFE_PATTERN.sub("_", text).strip("._")
    if not text:
        return fallback
    if not text.lower().endswith(".json"):
        text = text + ".json"
    return text[:MAX_NAME_LENGTH]


def default_pack_filename(
    characters: Optional[Dict[str, Any]] = None, prefix: str = "emotions"
) -> str:
    """生成带时间戳的默认文件名；只导出单个角色时把角色名带上。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    scope = ""
    if characters and len(characters) == 1:
        scope = "-" + _FILENAME_SAFE_PATTERN.sub("_", next(iter(characters)))
    return safe_pack_filename(prefix + scope + "-" + stamp + ".json")
