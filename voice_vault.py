"""语音收藏库：把 bot 发过的语音原样存下来，随时重听 / 重发 / 导出成文件。

设计要点：

1. **音质零损耗**。收藏走的是「逐字节复制插件自己合成出来的原始 WAV」，
   不重编码、不重采样。只有在实在映射不回本地文件、只能从 QQ 把语音捞
   回来时才会转码（那份音频已经被平台压过 SILK，属于不可逆的有损兜底），
   这种条目会被打上 source="platform" 并在 UI / 指令回复里明确标注。
2. **索引与音频分离**。index.json 只存元数据，音频按 <id>.<ext> 落在 audio/ 下，
   索引损坏也不会丢音频，反过来也一样。
3. **sha256 去重**。同一段音频反复收藏只会命中已有条目并刷新时间戳。
4. **写盘串行 + 原子替换**。WebUI 与聊天指令可能并发提交。
5. **zip 打包导入导出**。音频是二进制，JSON 装不下，所以用 zip；
   里面就是一份 index.json 加一个 audio/ 目录，人手也能改。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import audio_compat

# 索引文件名与音频子目录名，导出的 zip 里沿用同一套布局。
INDEX_NAME = "index.json"
AUDIO_DIR_NAME = "audio"
# 索引格式版本；将来结构有变时靠它做迁移。
INDEX_VERSION = 1
# 单条备注/正文最多留这么多字符，防止把整篇小说塞进索引。
MAX_TEXT_CHARS = 600
# 别名长度上限（按字符算），太长的名字在列表里没法看。
MAX_ALIAS_CHARS = 32
# 默认容量。超了就从最旧的未置顶条目开始淘汰。
DEFAULT_LIMIT = 200
# 单条音频体积上限（字节）。默认 32MB，正常语音远远够。
DEFAULT_MAX_BYTES = 32 * 1024 * 1024
# 导入 zip 时允许的最大解压总量，防 zip bomb。
MAX_IMPORT_BYTES = 512 * 1024 * 1024
# 允许的音频扩展名。导入时不在白名单里的一律丢弃。
ALLOWED_SUFFIXES = frozenset(
    {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".amr", ".silk", ".opus"}
)
# 别名里禁止出现的字符：既防路径穿越，也防指令解析歧义。
_ALIAS_BAD = re.compile(r"[\\/:*?\"<>|\r\n\t]")
# 合法 id 形状：12 位十六进制。导入时用它挡掉伪造路径。
_ID_RE = re.compile(r"^[0-9a-f]{12}$")

IMPORT_MODES = ("merge", "overwrite", "replace")
_MODE_ALIASES = {
    "merge": "merge",
    "合并": "merge",
    "skip": "merge",
    "补新": "merge",
    "overwrite": "overwrite",
    "覆盖": "overwrite",
    "update": "overwrite",
    "force": "overwrite",
    "replace": "replace",
    "替换": "replace",
    "清空": "replace",
}


class VoiceVaultError(RuntimeError):
    """收藏库层面的可预期错误，调用方直接把 message 回给用户即可。"""


def normalize_import_mode(token: Any) -> str:
    """把中英文别名统一成 merge / overwrite / replace。"""
    text = str(token or "").strip().lower()
    return _MODE_ALIASES.get(text, "merge")


def clean_alias(value: Any) -> str:
    """洗掉危险字符并截断。空别名是合法的（列表里按序号引用）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = _ALIAS_BAD.sub("", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:MAX_ALIAS_CHARS]


def clean_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        return text[: MAX_TEXT_CHARS - 1] + "…"
    return text


def safe_filename(value: str, fallback: str = "voice") -> str:
    """把别名/正文压成能落盘的文件名（导出单条语音时用）。"""
    text = _ALIAS_BAD.sub("", str(value or "")).strip()
    text = re.sub(r"[\s.]+", "_", text).strip("_")
    text = re.sub(r"_{2,}", "_", text)
    if not text:
        return fallback
    return text[:60]


class VoiceVault:
    """收藏库的全部读写都从这里走。"""

    def __init__(
        self,
        root: str | Path,
        limit: int = DEFAULT_LIMIT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.root = Path(root)
        self.audio_dir = self.root / AUDIO_DIR_NAME
        self.index_path = self.root / INDEX_NAME
        self._limit = max(int(limit or DEFAULT_LIMIT), 1)
        self._max_bytes = max(int(max_bytes or DEFAULT_MAX_BYTES), 1024)
        self._entries: List[Dict[str, Any]] = []
        self._seq = 0
        self._loaded = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------ 配置

    @property
    def limit(self) -> int:
        return self._limit

    def configure(self, limit: Any = None, max_bytes: Any = None) -> None:
        """热更新容量上限。裁剪推迟到下一次写操作，避免这里变成 async。"""
        if limit is not None:
            try:
                self._limit = max(int(limit), 1)
            except (TypeError, ValueError):
                pass
        if max_bytes is not None:
            try:
                self._max_bytes = max(int(max_bytes), 1024)
            except (TypeError, ValueError):
                pass

    # ------------------------------------------------------------ 索引读写

    @staticmethod
    def _new_id() -> str:
        return uuid.uuid4().hex[:12]

    def _sanitize_entry(self, raw: Any) -> Optional[Dict[str, Any]]:
        """把索引里的一行洗成规范结构；音频文件不存在的条目直接丢弃。"""
        if not isinstance(raw, dict):
            return None
        entry_id = str(raw.get("id") or "").strip().lower()
        if not _ID_RE.match(entry_id):
            return None
        suffix = str(raw.get("suffix") or ".wav").lower()
        if suffix not in ALLOWED_SUFFIXES:
            suffix = ".wav"
        audio_path = self.audio_dir / f"{entry_id}{suffix}"
        if not audio_path.is_file():
            return None
        try:
            size = audio_path.stat().st_size
        except OSError:
            return None

        def _int(key: str, default: int = 0) -> int:
            try:
                return int(raw.get(key) or default)
            except (TypeError, ValueError):
                return default

        tags = raw.get("tags")
        tag_list = (
            [str(item).strip()[:16] for item in tags if str(item).strip()][:8]
            if isinstance(tags, (list, tuple))
            else []
        )
        source = str(raw.get("source") or "plugin").strip().lower()
        if source not in ("plugin", "platform", "import", "upload"):
            source = "plugin"
        return {
            "id": entry_id,
            "alias": clean_alias(raw.get("alias")),
            "character": str(raw.get("character") or "").strip()[:48],
            "emotion": str(raw.get("emotion") or "").strip()[:48],
            "text": clean_text(raw.get("text")),
            "session_id": str(raw.get("session_id") or "").strip()[:160],
            "source": source,
            "suffix": suffix,
            "size": size,
            "sha256": str(raw.get("sha256") or "").strip().lower()[:64],
            "duration_ms": max(_int("duration_ms"), 0),
            "created_at": _int("created_at") or int(time.time()),
            "seq": max(_int("seq"), 0),
            "last_played_at": max(_int("last_played_at"), 0),
            "play_count": max(_int("play_count"), 0),
            "pinned": bool(raw.get("pinned")),
            "tags": tag_list,
        }

    async def load(self, force: bool = False) -> None:
        if self._loaded and not force:
            return
        async with self._lock:
            if self._loaded and not force:
                return
            await asyncio.to_thread(self._load_sync)
            self._loaded = True

    def _load_sync(self) -> None:
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        payload: Any = None
        if self.index_path.is_file():
            try:
                payload = json.loads(self.index_path.read_text(encoding="utf-8-sig"))
            except Exception:
                # 索引坏了不是世界末日：音频都还在，下面会从目录重建。
                payload = None
        rows = []
        if isinstance(payload, dict):
            rows = payload.get("entries") or []
        elif isinstance(payload, list):
            rows = payload

        entries: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in rows if isinstance(rows, list) else []:
            entry = self._sanitize_entry(raw)
            if entry and entry["id"] not in seen:
                seen.add(entry["id"])
                entries.append(entry)

        # 索引里没提到但音频还在的文件，补成「孤儿条目」而不是当垃圾删掉。
        for path in sorted(self.audio_dir.glob("*")):
            if not path.is_file() or path.suffix.lower() not in ALLOWED_SUFFIXES:
                continue
            entry_id = path.stem.lower()
            if entry_id in seen or not _ID_RE.match(entry_id):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            seen.add(entry_id)
            entries.append(
                {
                    "id": entry_id,
                    "alias": "",
                    "character": "",
                    "emotion": "",
                    "text": "",
                    "session_id": "",
                    "source": "plugin",
                    "suffix": path.suffix.lower(),
                    "size": stat.st_size,
                    "sha256": "",
                    "duration_ms": audio_compat.probe_duration_ms(path),
                    "created_at": int(stat.st_mtime),
                    "seq": 0,
                    "last_played_at": 0,
                    "play_count": 0,
                    "pinned": False,
                    "tags": [],
                }
            )

        self._entries = entries
        self._reindex_sync()

    def _reindex_sync(self) -> None:
        """补齐并压实 seq，同时让内部列表保持「新的在前」。

        created_at 只有整秒精度，同一秒里连收藏两条时它排不出先后，旧实现会退到
        随机的 uuid 前缀上，导致 /收藏列表 的序号和 WebUI 顺序每次刷新都可能换位。
        seq 是一个单调递增的内部序号，专门用来把这种并列拆开。
        """
        ordered = sorted(
            self._entries,
            key=lambda item: (
                int(item.get("created_at") or 0),
                int(item.get("seq") or 0),
                item["id"],
            ),
        )
        for position, item in enumerate(ordered, start=1):
            item["seq"] = position
        self._seq = len(ordered)
        ordered.reverse()
        self._entries = ordered

    def _next_seq(self) -> int:
        highest = self._seq
        for item in self._entries:
            try:
                highest = max(highest, int(item.get("seq") or 0))
            except (TypeError, ValueError):
                continue
        self._seq = highest + 1
        return self._seq

    def _write_index_sync(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "updated_at": int(time.time()),
            "entries": self._entries,
        }
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.index_path)

    async def _flush(self) -> None:
        await asyncio.to_thread(self._write_index_sync)

    # ------------------------------------------------------------ 查询

    def audio_path(self, entry: Dict[str, Any]) -> Path:
        return self.audio_dir / f"{entry['id']}{entry.get('suffix') or '.wav'}"

    def entries(self) -> List[Dict[str, Any]]:
        """按「置顶优先 + 新的在前」返回列表副本；序号就是这个顺序的下标+1。"""
        ordered = sorted(
            self._entries,
            key=lambda item: (
                0 if item.get("pinned") else 1,
                -int(item.get("created_at") or 0),
                -int(item.get("seq") or 0),
                item["id"],
            ),
        )
        return [dict(item) for item in ordered]

    def count(self) -> int:
        return len(self._entries)

    def stats(self) -> Dict[str, Any]:
        total_bytes = sum(int(item.get("size") or 0) for item in self._entries)
        total_ms = sum(int(item.get("duration_ms") or 0) for item in self._entries)
        characters: Dict[str, int] = {}
        emotions: Dict[str, int] = {}
        lossy = 0
        for item in self._entries:
            if item.get("character"):
                characters[item["character"]] = characters.get(item["character"], 0) + 1
            if item.get("emotion"):
                emotions[item["emotion"]] = emotions.get(item["emotion"], 0) + 1
            if item.get("source") == "platform":
                lossy += 1
        return {
            "count": len(self._entries),
            "limit": self._limit,
            "pinned": sum(1 for item in self._entries if item.get("pinned")),
            "lossy": lossy,
            "total_bytes": total_bytes,
            "total_bytes_human": audio_compat.format_size(total_bytes),
            "total_duration_ms": total_ms,
            "total_duration_human": audio_compat.format_duration(total_ms),
            "characters": characters,
            "emotions": emotions,
        }

    def search(
        self,
        keyword: str = "",
        character: str = "",
        emotion: str = "",
        session_id: str = "",
        source: str = "",
        pinned_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """带筛选的列表。返回的每条都带 index 字段（全库序号，跟指令一致）。"""
        rows = self.entries()
        for position, item in enumerate(rows, start=1):
            item["index"] = position
        needle = str(keyword or "").strip().lower()
        result = []
        for item in rows:
            if character and item.get("character") != character:
                continue
            if emotion and item.get("emotion") != emotion:
                continue
            if session_id and item.get("session_id") != session_id:
                continue
            if source and item.get("source") != source:
                continue
            if pinned_only and not item.get("pinned"):
                continue
            if needle:
                haystack = " ".join(
                    [
                        item.get("alias") or "",
                        item.get("text") or "",
                        item.get("character") or "",
                        item.get("emotion") or "",
                        " ".join(item.get("tags") or []),
                    ]
                ).lower()
                if needle not in haystack:
                    continue
            result.append(item)
        return result

    def get(self, entry_id: str) -> Optional[Dict[str, Any]]:
        target = str(entry_id or "").strip().lower()
        for item in self._entries:
            if item["id"] == target:
                return item
        return None

    def resolve(self, token: Any) -> Tuple[Optional[Dict[str, Any]], str]:
        """按 序号 → 别名精确 → id 前缀 → 别名/正文模糊 解析用户给的引用。

        返回 (条目, 错误原因)。命中多条模糊结果时返回错误，让用户说清楚。
        """
        text = str(token or "").strip()
        if not text:
            return None, "请给出收藏的序号或名字。"
        rows = self.entries()
        if not rows:
            return None, "收藏夹还是空的。"
        for position, item in enumerate(rows, start=1):
            item["index"] = position

        if text.isdigit():
            position = int(text)
            if 1 <= position <= len(rows):
                return self.get(rows[position - 1]["id"]), ""
            return None, f"序号超出范围（当前只有 {len(rows)} 条）。"

        lowered = text.lower()
        for item in rows:
            if (item.get("alias") or "").lower() == lowered:
                return self.get(item["id"]), ""
        prefix_hits = [item for item in rows if item["id"].startswith(lowered)]
        if len(prefix_hits) == 1:
            return self.get(prefix_hits[0]["id"]), ""
        fuzzy = [
            item
            for item in rows
            if lowered in (item.get("alias") or "").lower()
            or lowered in (item.get("text") or "").lower()
        ]
        if len(fuzzy) == 1:
            return self.get(fuzzy[0]["id"]), ""
        if len(fuzzy) > 1:
            preview = "、".join(
                f"{item['index']}.{item.get('alias') or (item.get('text') or '')[:8]}"
                for item in fuzzy[:5]
            )
            return None, f"「{text}」匹配到 {len(fuzzy)} 条，请用序号：{preview}"
        return None, f"没找到「{text}」，用 /收藏列表 看看现有的收藏。"

    def find_by_sha(self, sha256: str) -> Optional[Dict[str, Any]]:
        target = str(sha256 or "").strip().lower()
        if not target:
            return None
        for item in self._entries:
            if item.get("sha256") == target:
                return item
        return None

    def _unique_alias(self, alias: str, exclude_id: str = "") -> str:
        """别名允许留空；非空时保证全库唯一，冲突就挂 -2 / -3。"""
        base = clean_alias(alias)
        if not base:
            return ""
        taken = {
            (item.get("alias") or "").lower()
            for item in self._entries
            if item["id"] != exclude_id and item.get("alias")
        }
        if base.lower() not in taken:
            return base
        for suffix in range(2, 100):
            candidate = f"{base}-{suffix}"[:MAX_ALIAS_CHARS]
            if candidate.lower() not in taken:
                return candidate
        return f"{base}-{uuid.uuid4().hex[:4]}"[:MAX_ALIAS_CHARS]

    # ------------------------------------------------------------ 写操作

    def _evict_sync(self) -> List[Dict[str, Any]]:
        """超容量时淘汰最旧的未置顶条目，返回被删掉的条目。"""
        dropped: List[Dict[str, Any]] = []
        while len(self._entries) > self._limit:
            victims = [item for item in self._entries if not item.get("pinned")]
            if not victims:
                break
            victim = min(
                victims,
                key=lambda item: (
                    int(item.get("created_at") or 0),
                    int(item.get("seq") or 0),
                    item["id"],
                ),
            )
            self._entries = [i for i in self._entries if i["id"] != victim["id"]]
            try:
                self.audio_path(victim).unlink(missing_ok=True)
            except OSError:
                pass
            dropped.append(victim)
        return dropped

    async def add(
        self,
        source_path: str | Path,
        alias: str = "",
        character: str = "",
        emotion: str = "",
        text: str = "",
        session_id: str = "",
        source: str = "plugin",
        tags: Optional[Iterable[str]] = None,
        duration_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """收藏一段音频。返回结果字典：

        {"entry": …, "duplicate": bool, "dropped": [被淘汰的条目]}
        """
        await self.load()
        src = Path(source_path)
        if not src.is_file():
            raise VoiceVaultError("音频文件已经不在了（临时音频默认只保留 30 分钟）。")
        size = src.stat().st_size
        if size <= 0:
            raise VoiceVaultError("音频文件是空的。")
        if size > self._max_bytes:
            raise VoiceVaultError(
                f"音频太大了（{audio_compat.format_size(size)}），"
                f"上限 {audio_compat.format_size(self._max_bytes)}。"
            )
        suffix = src.suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            suffix = ".wav"

        digest = await asyncio.to_thread(audio_compat.sha256_file, src)
        async with self._lock:
            existing = self.find_by_sha(digest)
            if existing is not None:
                # 同一段音频不重复占地方，但把新的名字/标签补进去。
                changed = False
                if alias:
                    new_alias = self._unique_alias(alias, exclude_id=existing["id"])
                    if new_alias and new_alias != existing.get("alias"):
                        existing["alias"] = new_alias
                        changed = True
                if text and not existing.get("text"):
                    existing["text"] = clean_text(text)
                    changed = True
                if character and not existing.get("character"):
                    existing["character"] = str(character).strip()[:48]
                    changed = True
                if emotion and not existing.get("emotion"):
                    existing["emotion"] = str(emotion).strip()[:48]
                    changed = True
                if changed:
                    await self._flush()
                return {"entry": dict(existing), "duplicate": True, "dropped": []}

            entry_id = self._new_id()
            while self.get(entry_id) is not None:
                entry_id = self._new_id()
            target = self.audio_dir / f"{entry_id}{suffix}"
            self.audio_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.audio_dir / f".{entry_id}.tmp"
            try:
                # copyfile 是逐字节拷贝，不碰编码，这就是「音质无损」的实现。
                await asyncio.to_thread(shutil.copyfile, str(src), str(tmp))
                await asyncio.to_thread(os.replace, str(tmp), str(target))
            except Exception as exc:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise VoiceVaultError(f"保存音频失败: {exc}") from exc

            probed = (
                int(duration_ms)
                if duration_ms
                else await asyncio.to_thread(audio_compat.probe_duration_ms, target)
            )
            entry = {
                "id": entry_id,
                "alias": self._unique_alias(alias),
                "character": str(character or "").strip()[:48],
                "emotion": str(emotion or "").strip()[:48],
                "text": clean_text(text),
                "session_id": str(session_id or "").strip()[:160],
                "source": source if source in ("plugin", "platform", "import", "upload") else "plugin",
                "suffix": suffix,
                "size": target.stat().st_size,
                "sha256": digest,
                "duration_ms": max(int(probed or 0), 0),
                "created_at": int(time.time()),
                "seq": self._next_seq(),
                "last_played_at": 0,
                "play_count": 0,
                "pinned": False,
                "tags": [str(t).strip()[:16] for t in (tags or []) if str(t).strip()][:8],
            }
            self._entries.insert(0, entry)
            dropped = self._evict_sync()
            await self._flush()
            return {"entry": dict(entry), "duplicate": False, "dropped": dropped}

    async def rename(self, entry_id: str, alias: str) -> Dict[str, Any]:
        await self.load()
        async with self._lock:
            entry = self.get(entry_id)
            if entry is None:
                raise VoiceVaultError("这条收藏已经不存在了。")
            cleaned = clean_alias(alias)
            entry["alias"] = (
                self._unique_alias(cleaned, exclude_id=entry["id"]) if cleaned else ""
            )
            await self._flush()
            return dict(entry)

    async def update(self, entry_id: str, **fields: Any) -> Dict[str, Any]:
        """改备注文本 / 标签 / 置顶。别名走 rename()，那边要做唯一化。"""
        await self.load()
        async with self._lock:
            entry = self.get(entry_id)
            if entry is None:
                raise VoiceVaultError("这条收藏已经不存在了。")
            if "text" in fields:
                entry["text"] = clean_text(fields["text"])
            if "character" in fields:
                entry["character"] = str(fields["character"] or "").strip()[:48]
            if "emotion" in fields:
                entry["emotion"] = str(fields["emotion"] or "").strip()[:48]
            if "pinned" in fields:
                entry["pinned"] = bool(fields["pinned"])
            if "tags" in fields:
                raw = fields["tags"] or []
                if isinstance(raw, str):
                    raw = [part for part in re.split(r"[,，\s]+", raw) if part]
                entry["tags"] = [
                    str(t).strip()[:16] for t in raw if str(t).strip()
                ][:8]
            await self._flush()
            return dict(entry)

    async def touch(self, entry_id: str) -> None:
        """播放/发送计数。失败不重要，别为它中断发送流程。"""
        try:
            await self.load()
            async with self._lock:
                entry = self.get(entry_id)
                if entry is None:
                    return
                entry["play_count"] = int(entry.get("play_count") or 0) + 1
                entry["last_played_at"] = int(time.time())
                await self._flush()
        except Exception:
            pass

    async def remove(self, entry_id: str) -> Dict[str, Any]:
        await self.load()
        async with self._lock:
            entry = self.get(entry_id)
            if entry is None:
                raise VoiceVaultError("这条收藏已经不存在了。")
            self._entries = [i for i in self._entries if i["id"] != entry["id"]]
            try:
                self.audio_path(entry).unlink(missing_ok=True)
            except OSError:
                pass
            await self._flush()
            return dict(entry)

    async def clear(self, keep_pinned: bool = True) -> int:
        await self.load()
        async with self._lock:
            victims = [
                item
                for item in self._entries
                if not (keep_pinned and item.get("pinned"))
            ]
            for item in victims:
                try:
                    self.audio_path(item).unlink(missing_ok=True)
                except OSError:
                    pass
            victim_ids = {item["id"] for item in victims}
            self._entries = [i for i in self._entries if i["id"] not in victim_ids]
            await self._flush()
            return len(victims)

    # ------------------------------------------------------------ 导入导出

    def _export_sync(self, dest: Path, ids: Optional[List[str]]) -> Dict[str, Any]:
        selected = [
            item
            for item in self.entries()
            if ids is None or item["id"] in set(ids)
        ]
        if not selected:
            raise VoiceVaultError("没有可导出的收藏。")
        dest.parent.mkdir(parents=True, exist_ok=True)
        payload_entries = []
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in selected:
                path = self.audio_path(item)
                if not path.is_file():
                    continue
                arc = f"{AUDIO_DIR_NAME}/{item['id']}{item.get('suffix') or '.wav'}"
                bundle.write(path, arc)
                row = {k: v for k, v in item.items() if k != "index"}
                payload_entries.append(row)
            manifest = {
                "version": INDEX_VERSION,
                "kind": "genie-voice-vault",
                "exported_at": int(time.time()),
                "count": len(payload_entries),
                "entries": payload_entries,
            }
            bundle.writestr(
                INDEX_NAME, json.dumps(manifest, ensure_ascii=False, indent=2)
            )
        return {"count": len(payload_entries), "path": str(dest)}

    async def export_bundle(
        self, dest: str | Path, ids: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        await self.load()
        return await asyncio.to_thread(self._export_sync, Path(dest), ids)

    def _read_manifest(self, bundle: zipfile.ZipFile) -> List[Dict[str, Any]]:
        names = bundle.namelist()
        manifest_name = next(
            (n for n in names if n.rsplit("/", 1)[-1] == INDEX_NAME), None
        )
        if not manifest_name:
            return []
        try:
            payload = json.loads(bundle.read(manifest_name).decode("utf-8-sig"))
        except Exception:
            return []
        rows = payload.get("entries") if isinstance(payload, dict) else payload
        return rows if isinstance(rows, list) else []

    def _import_sync(self, src: Path, mode: str) -> Dict[str, Any]:
        if not src.is_file():
            raise VoiceVaultError("导入文件不存在。")
        if not zipfile.is_zipfile(src):
            raise VoiceVaultError(
                "只认收藏包 zip（里面是 index.json + audio/）。"
            )
        report = {"added": 0, "updated": 0, "skipped": 0, "invalid": 0, "removed": 0}
        with zipfile.ZipFile(src, "r") as bundle:
            total = sum(max(info.file_size, 0) for info in bundle.infolist())
            if total > MAX_IMPORT_BYTES:
                raise VoiceVaultError("收藏包解压后体积过大，已拒绝导入。")
            manifest = {
                str(row.get("id") or "").strip().lower(): row
                for row in self._read_manifest(bundle)
                if isinstance(row, dict)
            }

            if mode == "replace":
                for item in list(self._entries):
                    try:
                        self.audio_path(item).unlink(missing_ok=True)
                    except OSError:
                        pass
                report["removed"] = len(self._entries)
                self._entries = []

            self.audio_dir.mkdir(parents=True, exist_ok=True)
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                name = info.filename.replace("\\", "/")
                base = name.rsplit("/", 1)[-1]
                if base == INDEX_NAME:
                    continue
                stem, _, ext = base.rpartition(".")
                suffix = f".{ext.lower()}" if ext else ""
                if suffix not in ALLOWED_SUFFIXES:
                    report["invalid"] += 1
                    continue
                entry_id = stem.lower()
                if not _ID_RE.match(entry_id):
                    # 外部随手打的包也想能用：给它现分配一个合法 id。
                    entry_id = self._new_id()
                    while self.get(entry_id) is not None:
                        entry_id = self._new_id()
                existing = self.get(entry_id)
                if existing is not None and mode == "merge":
                    report["skipped"] += 1
                    continue
                target = self.audio_dir / f"{entry_id}{suffix}"
                tmp = self.audio_dir / f".{entry_id}.imp"
                try:
                    with bundle.open(info, "r") as reader, tmp.open("wb") as writer:
                        shutil.copyfileobj(reader, writer, 1 << 20)
                    if tmp.stat().st_size <= 0:
                        raise VoiceVaultError("音频为空")
                    os.replace(str(tmp), str(target))
                except Exception:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    report["invalid"] += 1
                    continue

                raw = dict(manifest.get(stem.lower()) or {})
                raw["id"] = entry_id
                raw["suffix"] = suffix
                raw.setdefault("source", "import")
                if not raw.get("sha256"):
                    raw["sha256"] = audio_compat.sha256_file(target)
                if not raw.get("duration_ms"):
                    raw["duration_ms"] = audio_compat.probe_duration_ms(target)
                entry = self._sanitize_entry(raw)
                if entry is None:
                    report["invalid"] += 1
                    continue
                entry["alias"] = self._unique_alias(
                    entry.get("alias") or "", exclude_id=entry_id
                )
                if existing is not None:
                    self._entries = [
                        i for i in self._entries if i["id"] != entry_id
                    ]
                    report["updated"] += 1
                else:
                    report["added"] += 1
                self._entries.append(entry)

        self._reindex_sync()
        dropped = self._evict_sync()
        report["evicted"] = len(dropped)
        report["total"] = len(self._entries)
        self._write_index_sync()
        return report

    async def import_bundle(self, src: str | Path, mode: str = "merge") -> Dict[str, Any]:
        await self.load()
        resolved = normalize_import_mode(mode)
        async with self._lock:
            return await asyncio.to_thread(self._import_sync, Path(src), resolved)


def describe_import(report: Dict[str, Any]) -> str:
    """把导入报告渲染成一行人话。"""
    parts = []
    for key, label in (
        ("added", "新增"),
        ("updated", "覆盖"),
        ("skipped", "跳过"),
        ("invalid", "无效"),
        ("removed", "清空"),
        ("evicted", "超量淘汰"),
    ):
        value = int(report.get(key) or 0)
        if value:
            parts.append(f"{label} {value}")
    body = " | ".join(parts) if parts else "没有任何变化"
    return f"{body}（现共 {int(report.get('total') or 0)} 条）"
