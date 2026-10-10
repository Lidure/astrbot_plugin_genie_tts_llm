import asyncio
import httpx
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.api.provider import LLMResponse, ProviderRequest

# 从新模块导入功能
from . import audio_compat
from . import emotion_pack
from . import voice_vault
from . import session_state
from .emotion_pack import EmotionPackError
from .emotion_manager import EmotionManager
from .session_character_bindings import SessionCharacterBindings
from .conversation_tts_policy import (
    is_private_tts_active, parse_character_default_emotions, resolve_default_emotion,
)
from .emotion_routing import should_provider_autofill_emotion
from .tts_engine import DEFAULT_MAX_TEXT_LENGTH, TTSEngine, has_pronounceable
from .external_apis import translate_text
from .run_log import RunLog
from .voice_vault import VoiceVault, VoiceVaultError


@register(
    "astrbot_plugin_genie_tts_llm",
    "Whereis-Alice",
    "一个通过 LLM、翻译和 Genie TTS 实现语音合成的插件，支持主动语音工具",
    "1.11.1",
    "https://github.com/Lidure/astrbot_plugin_genie_tts_llm",
)
class GenieTtsLlmPlugin(Star):
    # 会话开关/音色选择的持久化键。AstrBot 的插件 KV 存储按 plugin_id 隔离。
    STATE_KV_KEY = "session_state_v1"
    # 一次性迁移标记：v1.9.6 把「失败提示追加到回复」的默认值从开改成了关。
    # 老安装的配置文件里已经写着 true，只改 schema 默认值救不了它们，得迁移一次。
    NOTICE_MIGRATION_KV_KEY = "failure_notice_default_off_v196"
    # 插件版本号：WebUI 总览与感情包元数据都会读它。
    PLUGIN_VERSION = "1.11.1"
    # 感情包快照目录名（位于插件数据目录下）。
    PACK_DIR_NAME = "emotion_packs"
    # 能被识别为「导入模式」的 token，真正的语义交给 emotion_pack 归一化。
    PACK_MODE_TOKENS = frozenset(
        {
            "merge",
            "overwrite",
            "replace",
            "skip",
            "update",
            "force",
            "合并",
            "覆盖",
            "替换",
            "清空",
        }
    )
    # 只预演不落盘的关键字。
    PACK_DRY_TOKENS = frozenset(
        {"试运行", "预演", "预览", "dry", "dryrun", "dry-run", "-n", "--dry-run"}
    )
    # 最多记住多少个会话的发送历史，避免长期运行后字典无限增长。
    MAX_REMEMBERED_AUDIO = 200
    # 每个会话往回记多少条已发语音。引用某条语音时要靠这段历史反查原始文件，
    # 一条根本不够用（用户完全可能引用三句之前那条）。
    SENT_VOICE_RING = 16
    # 收藏目录名（位于插件数据目录下）。
    VAULT_DIR_NAME = "voice_vault"
    # 引用消息与本地发送记录的时间匹配窗口（秒）。协议端回报的时间是整秒，
    # 加上网络往返和队列延迟，放宽到 45s 才不会漏；窗口内取最近的一条。
    QUOTE_MATCH_WINDOW = 45.0
    # 收藏包的固定文件名前缀。
    VAULT_BUNDLE_PREFIX = "genie-voices"
    # /收藏列表 每页条数。QQ 单条消息太长会被折叠，8 条正好一屏。
    FAVORITE_PAGE_SIZE = 8
    # /收藏清空 必须显式确认，避免手滑清库。
    VAULT_CONFIRM_TOKENS = frozenset(
        {"确认", "确定", "yes", "y", "confirm", "--yes", "-y"}
    )
    # /收藏清空 连置顶一起清掉的关键字。
    VAULT_PINNED_TOKENS = frozenset({"含置顶", "包括置顶", "全部", "all", "--all"})
    # 能被识别为「收藏导入模式」的 token，语义交给 voice_vault 归一化。
    VAULT_MODE_TOKENS = frozenset(
        {
            "merge",
            "overwrite",
            "replace",
            "skip",
            "update",
            "force",
            "合并",
            "覆盖",
            "替换",
            "清空",
            "补新",
        }
    )
    # 清理 LLM 复读的历史 TTS 失败提示。整条匹配，不留残渣。
    TTS_FAILURE_NOTICE_PATTERN = re.compile(
        r"\n?\(TTS(?:失败[：:][^)]*|合成失败|音频发送失败)\)"
    )
    # LLM 工具遇到「内部管道坏了」时回给模型的收尾约束。工具返回值会进模型上下文，
    # 不加这句的话模型很容易把「请检查翻译配置或日志」原样念给用户听。
    TOOL_INTERNAL_FAILURE_TAIL = (
        "回复用户时只用角色口吻简短提一句这次没能发出语音，"
        "不要复述工具名、配置项、日志或任何技术细节。"
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 运行日志缓冲：越早装上越好，才能抓到后续初始化阶段的日志。
        self.run_log = RunLog(
            capacity=self.config.get("run_log_capacity", 500),
            synth_capacity=self.config.get("run_log_synth_capacity", 200),
            full_text=self.config.get("run_log_full_text", True),
            enabled=self.config.get("enable_run_log", True),
        )
        self.run_log.attach(logger)
        self.active_sessions: Set[str] = set()
        self.w_active_sessions: Set[str] = set()
        self.active_groups: Set[str] = set()  # 新增：群组级TTS开关
        self.inactive_groups: Set[str] = set()
        self.inactive_private_sessions: Set[str] = set()
        self.session_emotions: Dict[str, Dict[str, str]] = {}
        self.session_w_settings: Dict[str, Dict[str, str]] = {}
        self.known_sessions: Dict[str, Dict[str, Any]] = {}
        self.last_tts_trigger_at: Dict[str, float] = {}
        self.skip_next_auto_tts_sessions: Set[str] = set()
        self.pending_auto_tts_sessions: Set[str] = set()
        self.checked_auto_tts_sessions: Set[str] = set()
        self._keepalive_stop_event = asyncio.Event()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._llm_translation_conflict_logged = False
        # 会话 -> 最近若干条已发语音的元数据环，供 /tts-again、引用收藏、引用转文件复用。
        # 只存元数据（含临时文件路径），音频本体仍在 tts_engine 的临时目录里，
        # 30 分钟后会被清理——所以"收藏"必须当场把字节拷进收藏库。
        self.sent_voices: Dict[str, List[Dict[str, Any]]] = {}
        self._voice_seq = 0
        self._state_lock = asyncio.Lock()
        self._state_restored = asyncio.Event()
        self._state_restore_task: Optional[asyncio.Task] = None
        self._config_migration_task: Optional[asyncio.Task] = None

        # 初始化辅助模块
        plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_genie_tts_llm")
        # WebUI 工作台与感情包指令都要用到数据目录，存成属性方便复用。
        self.plugin_data_dir = plugin_data_dir
        self.session_character_bindings = SessionCharacterBindings(
            plugin_data_dir / "session_characters.json"
        )
        emotions_file_path = plugin_data_dir / "emotions.json"
        self.emotion_manager = EmotionManager(emotions_file_path)
        # 感情库整体写盘要串行：WebUI 与聊天指令可能并发提交。
        self._emotion_write_lock = asyncio.Lock()

        # 语音收藏库。索引与音频都落在数据目录下，重启不丢；首次 load() 推迟到真正用到时。
        self.voice_vault = VoiceVault(
            plugin_data_dir / self.VAULT_DIR_NAME,
            limit=self.config.get("voice_favorite_limit", voice_vault.DEFAULT_LIMIT),
            max_bytes=int(
                max(float(self.config.get("voice_favorite_max_mb", 32) or 32), 1)
                * 1024
                * 1024
            ),
        )
        # 从平台把语音捞回来时用的转码缓存（有损兜底路径专用）。
        self.audio_converter = audio_compat.AudioConverter(
            plugin_data_dir / "audio_cache"
        )

        self.http_client = httpx.AsyncClient(timeout=300.0)
        self.tts_engine = TTSEngine(self.config, self.http_client, plugin_data_dir)

        if self.config.get("enable_space_keepalive"):
            self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

        # 群白名单实时从配置读取，不能复制进持久化的手动开关。
        # 否则从配置删除群号后，旧开关仍会在重启时把群重新打开。

        if self.config.get("enable_group_tts_by_default", False):
            logger.info("已开启全部群默认语音合成；群组黑名单仍优先。")

        # KV 读取是协程，__init__ 里只能挂后台任务；恢复完成后置位 _state_restored。
        self._state_restore_task = asyncio.create_task(self._restore_state())
        # 配置默认值迁移同样要读 KV，一并挂到后台。
        self._config_migration_task = asyncio.create_task(
            self._migrate_failure_notice_default()
        )

        # 注册 WebUI 语音合成工作台。注册失败只记日志，不影响插件主流程。
        self.web_api = None
        try:
            from .web_api import GenieWebApi

            self.web_api = GenieWebApi(self)
            # register() 内部已经打过日志，这里只兜住异常。
            self.web_api.register(context)
        except Exception as exc:
            self.web_api = None
            logger.error(f"Genie TTS: WebUI 工作台注册失败: {exc}")

        logger.info("LLM TTS 插件已加载。")

    # ------------------------------------------------------------ 状态持久化

    @staticmethod
    def _as_str_set(value: object) -> Set[str]:
        """把持久化数据里的任意结构安全转成字符串集合。"""
        if not isinstance(value, (list, tuple, set)):
            return set()
        return {str(item) for item in value if item}

    @staticmethod
    def _as_profile_map(
        value: object, keys: Tuple[str, ...]
    ) -> Dict[str, Dict[str, str]]:
        """转成 {会话: {字段: 值}}；字段不齐的脏数据直接丢弃，避免后续 KeyError。"""
        if not isinstance(value, dict):
            return {}
        restored: Dict[str, Dict[str, str]] = {}
        for session_id, payload in value.items():
            if not session_id or not isinstance(payload, dict):
                continue
            if any(not payload.get(key) for key in keys):
                continue
            restored[str(session_id)] = {key: str(payload[key]) for key in keys}
        return restored

    def _state_persistence_enabled(self) -> bool:
        return bool(self.config.get("enable_state_persistence", True))

    async def _restore_state(self) -> None:
        """从插件 KV 存储恢复上次的会话/群组开关与音色选择。"""
        try:
            if not self._state_persistence_enabled():
                return

            saved = await self.get_kv_data(self.STATE_KV_KEY, None)
            if not isinstance(saved, dict):
                return

            self.active_sessions.update(self._as_str_set(saved.get("active_sessions")))
            self.w_active_sessions.update(
                self._as_str_set(saved.get("w_active_sessions"))
            )
            saved_groups = self._as_str_set(saved.get("active_groups"))
            if saved.get("version", 1) == 1:
                # 旧存储混合了配置白名单和手动开启；当前白名单交回配置管理。
                saved_groups -= session_state.group_ids(self.config.get("group_whitelist", []))
            self.active_groups.update(saved_groups)
            self.inactive_groups.update(self._as_str_set(saved.get("inactive_groups")))
            self.inactive_private_sessions.update(
                self._as_str_set(saved.get("inactive_private_sessions"))
            )
            self.known_sessions.update(session_state.restore_known_sessions(saved.get("known_sessions")))
            self.session_emotions.update(
                self._as_profile_map(
                    saved.get("session_emotions"), ("character", "emotion")
                )
            )
            self.session_w_settings.update(
                self._as_profile_map(saved.get("session_w_settings"), ("character",))
            )

            # 同一会话不能同时是固定情感模式和自动情感识别模式，自动模式优先。
            self.active_sessions -= self.w_active_sessions
            # 手动关闭优先于群白名单和默认全开。
            self.active_groups -= self.inactive_groups

            logger.info(
                "已恢复 TTS 会话状态 | 固定情感会话: %d | 自动情感会话: %d | "
                "已开启群: %d | 已关闭群: %d"
                % (
                    len(self.active_sessions),
                    len(self.w_active_sessions),
                    len(self.active_groups),
                    len(self.inactive_groups),
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"恢复 TTS 会话状态失败，本次以空状态启动: {exc}")
        finally:
            self._state_restored.set()

    async def _migrate_failure_notice_default(self) -> None:
        """把老安装的「失败提示追加到回复」关掉一次。

        这一项在 v1.9.6 之前默认是开的，AstrBot 会把当时的默认值写进
        data/config 里的插件配置文件，所以单改 schema 默认值对已经装过的实例无效。
        迁移只做一次（做完写 KV 标记），之后用户自己再打开就不会又被关掉。
        """
        try:
            if await self.get_kv_data(self.NOTICE_MIGRATION_KV_KEY, False):
                return
            if self.config.get("enable_tts_failure_notice", False):
                self.config["enable_tts_failure_notice"] = False
                try:
                    self.config.save_config()
                except Exception:
                    # 存不下就把内存里的值还原，别让这次运行的行为和配置文件不一致。
                    self.config["enable_tts_failure_notice"] = True
                    raise
                logger.info(
                    "Genie TTS: 已按 v1.9.6 的新默认值关闭「失败提示追加到回复」，"
                    "合成失败改到 WebUI 工作台的「日志」页查看；"
                    "想要旧行为可以在「配置 → 输出形态」里重新打开。"
                )
            await self.put_kv_data(self.NOTICE_MIGRATION_KV_KEY, True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"Genie TTS: 失败提示默认值迁移失败（不影响本次运行）: {exc}")

    async def _persist_state(self) -> None:
        """把会话/群组开关与音色选择写回插件 KV 存储。失败只告警，不影响本次指令。"""
        if not self._state_persistence_enabled():
            return

        payload = {
            "version": 2,
            "known_sessions": session_state.restore_known_sessions(self.known_sessions),
            "active_sessions": sorted(self.active_sessions),
            "w_active_sessions": sorted(self.w_active_sessions),
            "active_groups": sorted(self.active_groups),
            "inactive_groups": sorted(self.inactive_groups),
            "inactive_private_sessions": sorted(self.inactive_private_sessions),
            "session_emotions": {
                key: dict(value) for key, value in self.session_emotions.items()
            },
            "session_w_settings": {
                key: dict(value) for key, value in self.session_w_settings.items()
            },
        }
        try:
            async with self._state_lock:
                await self.put_kv_data(self.STATE_KV_KEY, payload)
        except Exception as exc:
            logger.warning(f"保存 TTS 会话状态失败（不影响本次操作）: {exc}")

    async def _observe_session(self, event: AstrMessageEvent, *, persist: bool = True) -> None:
        """Record real event origins, including sessions enabled only by group config."""
        await self._state_restored.wait()
        sid = str(event.unified_msg_origin or "")
        if not sid:
            return
        gid = self._normalize_group_id(getattr(event.message_obj, "group_id", ""))
        now = int(time.time())
        previous = self.known_sessions.get(sid)
        # Persist first sightings immediately; coalesce repeated LLM/tool rounds.
        if previous and previous["group_id"] == gid and now - previous["last_seen"] < 60:
            return
        self.known_sessions.pop(sid, None)
        self.known_sessions[sid] = {"group_id": gid, "last_seen": now}
        while len(self.known_sessions) > session_state.MAX_KNOWN_SESSIONS:
            self.known_sessions.pop(next(iter(self.known_sessions)))
        if persist:
            await self._persist_state()

    # -------------------------------------------------------- 已发语音历史

    def _remember_last_audio(
        self,
        session_id: str,
        audio_path: Optional[str],
        text: str = "",
        character: str = "",
        emotion: str = "",
    ) -> Optional[str]:
        """把刚发出去的语音记进会话历史环，供 /tts-again、引用收藏、引用转文件复用。

        环按「新的在前」排列，长度 SENT_VOICE_RING；会话数仍按 LRU 限制在
        MAX_REMEMBERED_AUDIO 以内。sent_at 用当前时间：合成完到真正送达之间
        只差毫秒级，而引用匹配的窗口是几十秒，够用。
        """
        if not session_id or not audio_path:
            return audio_path
        try:
            size = os.path.getsize(audio_path)
        except OSError:
            size = 0
        self._voice_seq += 1
        record: Dict[str, Any] = {
            "seq": self._voice_seq,
            "path": audio_path,
            "text": (text or "").strip()[: voice_vault.MAX_TEXT_CHARS],
            "character": (character or "").strip()[:48],
            "emotion": (emotion or "").strip()[:48],
            "sent_at": time.time(),
            "size": size,
        }
        ring = self.sent_voices.pop(session_id, None) or []
        ring.insert(0, record)
        del ring[self.SENT_VOICE_RING :]
        # 先 pop 再插入，让 dict 的插入顺序等价于访问顺序（LRU）。
        self.sent_voices[session_id] = ring
        while len(self.sent_voices) > self.MAX_REMEMBERED_AUDIO:
            self.sent_voices.pop(next(iter(self.sent_voices)), None)
        return audio_path

    def _session_voice_ring(self, session_id: str) -> List[Dict[str, Any]]:
        """返回该会话仍有文件存在的已发语音（新的在前），顺手剔除已被清理的。"""
        ring = self.sent_voices.get(session_id) or []
        alive = [item for item in ring if os.path.isfile(item.get("path") or "")]
        if len(alive) != len(ring):
            if alive:
                self.sent_voices[session_id] = alive
            else:
                self.sent_voices.pop(session_id, None)
        return alive

    def _latest_sent_voice(self, session_id: str) -> Optional[Dict[str, Any]]:
        ring = self._session_voice_ring(session_id)
        return ring[0] if ring else None

    def has_sent_voice(self, session_id: str) -> bool:
        """WebUI 会话页要用：这个会话还有没有可重发的语音。"""
        return bool(self._session_voice_ring(session_id))

    def sent_voice_count(self, session_id: str) -> int:
        return len(self._session_voice_ring(session_id))

    def _append_tts_failure_notice(self, resp: LLMResponse, reason: str) -> None:
        """把 TTS 失败提示追加到回复末尾。默认关闭。

        内部管道的失败不该挤进对话内容（"里面还有一只粉色小猪(TTS合成失败)" 就是典型），
        所以 v1.9.6 起失败默认只落到 WebUI「日志」页，想要旧行为的去「配置 → 输出形态」打开。
        """
        if not self.config.get("enable_tts_failure_notice", False):
            # 提示不进聊天之后，日志就是这次失败唯一的出口：用 WARNING 才能被日志页
            # 的「只看问题」一档筛出来。
            logger.warning(f"Genie TTS: {reason}（失败提示未追加到回复，按配置隐藏）")
            return
        resp.result_chain.chain.append(Comp.Plain(f"\n({reason})"))

    # ------------------------------------------------------------ 群组与触发

    def _normalize_group_id(self, group_id: Optional[object]) -> str:
        """统一群号格式，避免配置里的字符串和事件里的数字不匹配。"""
        return str(group_id) if group_id else ""

    def _is_group_blacklisted(self, group_id: Optional[object]) -> bool:
        """检查群组是否在黑名单中"""
        group_id = self._normalize_group_id(group_id)
        if not group_id:
            return False
        return group_id in session_state.group_ids(self.config.get("group_blacklist", []))

    def _is_group_tts_active(self, group_id: Optional[object]) -> bool:
        """检查群组级 TTS 是否开启。黑名单 > 运行时关闭 > 默认全开/白名单/手动开启。"""
        return session_state.group_state(self, self._normalize_group_id(group_id))["enabled"]

    def _is_private_tts_active(self, session_id: str, group_id: Optional[object]) -> bool:
        if self._normalize_group_id(group_id):
            return False
        return is_private_tts_active(
            enable_by_default=bool(self.config.get("enable_private_tts_by_default", True)),
            session_id=session_id,
            active_sessions=self.active_sessions,
            w_active_sessions=self.w_active_sessions,
            inactive_private_sessions=self.inactive_private_sessions,
        )

    def _get_default_emotion_for_character(self, character_name: Optional[str]) -> Optional[str]:
        if not character_name:
            return None
        return resolve_default_emotion(
            character_name,
            self.emotion_manager.emotions_data.get(character_name, {}).keys(),
            self.config.get("default_emotion_name"),
            parse_character_default_emotions(self.config.get("character_default_emotions", [])),
        )

    def _should_generate_tts_now(self, session_id: str) -> bool:
        """按配置判断本次 LLM 回复是否需要生成语音。"""
        mode = str(self.config.get("tts_trigger_mode", "always")).strip().lower()

        if mode in {"always", "一直触发"}:
            return True

        if mode in {"interval", "time", "按间隔"}:
            try:
                interval_seconds = max(
                    int(self.config.get("tts_trigger_interval_seconds", 300) or 0), 0
                )
            except (TypeError, ValueError):
                interval_seconds = 300
            if interval_seconds <= 0:
                return True

            now = time.monotonic()
            last_trigger_at = self.last_tts_trigger_at.get(session_id)
            if last_trigger_at is None or now - last_trigger_at >= interval_seconds:
                self.last_tts_trigger_at[session_id] = now
                return True

            remaining = interval_seconds - (now - last_trigger_at)
            logger.info(
                f"[{session_id}] 已按时间间隔跳过本次 TTS，约 {remaining:.1f} 秒后可再次触发。"
            )
            return False

        if mode in {"random", "probability", "随机概率"}:
            try:
                probability = float(self.config.get("tts_trigger_probability", 30) or 0)
            except (TypeError, ValueError):
                probability = 30.0
            probability = min(max(probability, 0.0), 100.0)
            triggered = random.random() * 100 < probability
            if not triggered:
                logger.info(
                    f"[{session_id}] 已按随机概率跳过本次 TTS（当前概率: {probability:g}%）。"
                )
            return triggered

        logger.warning(f"未知 TTS 触发模式: {mode}，已按 always 处理。")
        return True

    def _preview_log_text(self, text: Optional[str], max_length: int = 180) -> str:
        if not text:
            return ""
        compact = re.sub(r"\s+", " ", text).strip()
        if len(compact) <= max_length:
            return compact
        return f"{compact[:max_length]}..."

    def _log_translation_result(
        self, session_id: str, source_text: str, target_text: Optional[str]
    ) -> None:
        if not self.config.get("enable_translation_debug_log", False):
            return
        logger.info(
            f"[{session_id}] TTS翻译结果 | 原文: {self._preview_log_text(source_text)} | "
            f"合成文本: {self._preview_log_text(target_text)}"
        )

    def _normalize_translation_workflow(self, workflow: Optional[object]) -> str:
        normalized = str(workflow or "").strip().lower()
        workflow_aliases = {
            "llm_injection": "llm_injection",
            "llm": "llm_injection",
            "inject": "llm_injection",
            "prompt_injection": "llm_injection",
            "provider_translation": "provider_translation",
            "provider": "provider_translation",
            "astrbot_provider": "provider_translation",
            "backend": "provider_translation",
        }
        return workflow_aliases.get(normalized, "")

    def _get_translation_workflow(self) -> str:
        settings = self.config.get("llm_injection_settings", {})
        configured_workflow = self._normalize_translation_workflow(
            settings.get("translation_workflow")
        )
        if configured_workflow:
            return configured_workflow

        if settings.get("enable_llm_translation", False):
            return "llm_injection"

        if settings.get("use_astrbot_provider", False) or self._has_external_translation_api_config():
            return "provider_translation"

        return "llm_injection"

    def _should_inject_llm_translation_tags(self) -> bool:
        if not self.config.get("enable_translation", True):
            return False
        return self._get_translation_workflow() == "llm_injection"

    def _should_inject_llm_emotion_tags(self) -> bool:
        """情感标签与翻译链路解耦，只受“生成情感标签”开关控制。

        [emotion=xxx] 与 $翻译$ 是两件独立的事：provider_translation 链路下，
        情感标签会在文本送去翻译之前先被剥离，既不会污染翻译输入，也不会漏进聊天。
        """
        settings = self.config.get("llm_injection_settings", {})
        return bool(settings.get("enable_llm_emotion", False))

    def _get_tts_target_language_name(self) -> str:
        language_code = str(self.config.get("tts_default_language", "jp") or "jp").strip().lower()
        language_names = {
            "jp": "日语",
            "ja": "日语",
            "zh": "中文",
            "en": "英语",
        }
        return language_names.get(language_code, language_code or "目标语言")

    def _extract_tool_text_directives(
        self, text: str
    ) -> Tuple[str, Optional[str], Optional[str]]:
        working_text = (text or "").strip()
        emotion_matches = re.findall(r"\[emotion=(.*?)\]", working_text)
        tagged_emotion = emotion_matches[-1].strip() if emotion_matches else None
        working_text = re.sub(r"\s*\[emotion=.*?\]\s*", " ", working_text).strip()

        tagged_translation = None
        translation_match = re.search(
            r"(?:\$(.+?)\$|\uFF04(.+?)\uFF04)\s*$", working_text, re.DOTALL
        )
        if translation_match:
            tagged_translation = (
                translation_match.group(1) or translation_match.group(2) or ""
            ).strip()
            working_text = working_text[: translation_match.start()].strip()

        display_text = working_text or tagged_translation or ""
        display_text = re.sub(r"\s+", " ", display_text).strip()
        return display_text, tagged_emotion, tagged_translation

    def _strip_pause_markers(self, text: str) -> str:
        """移除自定义停顿标记 [pause=ms]，避免它出现在用户可见的聊天文本里。"""
        if not text:
            return text
        stripped = re.sub(
            r"\[pause\s*=\s*\d+\s*(?:ms)?\]", " ", text, flags=re.IGNORECASE
        )
        stripped = re.sub(r"[ \t]{2,}", " ", stripped)
        return stripped.strip()

    def _strip_llm_tts_directives(
        self,
        text: str,
        strip_translation: bool = True,
        strip_emotion: bool = True,
        strip_pause: bool = False,
    ) -> Tuple[str, Optional[str], Optional[str], bool]:
        """Remove hidden TTS directives from text before it reaches chat."""
        working_text = (text or "").strip()
        if not working_text:
            return "", None, None, False

        changed = False
        tagged_emotion = None
        tagged_translation = None

        if strip_emotion:
            emotion_matches = re.findall(r"\[emotion=(.*?)\]", working_text)
            if emotion_matches:
                tagged_emotion = emotion_matches[-1].strip()
                working_text = re.sub(
                    r"\s*\[emotion=.*?\]\s*", " ", working_text
                ).strip()
                changed = True

        if strip_translation:
            while True:
                translation_match = re.search(
                    r"\s*(?:\$(.+?)\$|\uFF04(.+?)\uFF04)\s*$",
                    working_text,
                    re.DOTALL,
                )
                if not translation_match:
                    break
                tagged_translation = (
                    translation_match.group(1)
                    or translation_match.group(2)
                    or ""
                ).strip()
                working_text = working_text[: translation_match.start()].strip()
                changed = True

        if strip_pause:
            pause_stripped = self._strip_pause_markers(working_text)
            if pause_stripped != working_text:
                working_text = pause_stripped
                changed = True

        cleaned_text = re.sub(r"[ \t]+", " ", working_text)
        cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
        return cleaned_text, tagged_emotion, tagged_translation, changed

    def _sanitize_plain_components(
        self, chain: Optional[list], strip_translation: Optional[bool] = None
    ) -> bool:
        if not chain:
            return False

        if strip_translation is None:
            strip_translation = self._should_inject_llm_translation_tags()

        changed = False
        for component in chain:
            if not isinstance(component, Comp.Plain):
                continue
            cleaned_text, _, _, component_changed = self._strip_llm_tts_directives(
                getattr(component, "text", ""),
                strip_translation=strip_translation,
                strip_emotion=True,
                strip_pause=True,
            )
            if component_changed:
                component.text = cleaned_text
                changed = True
        return changed

    def _should_use_astrbot_provider_translation(
        self, disable_when_llm_translation_enabled: bool = False
    ) -> bool:
        settings = self.config.get("llm_injection_settings", {})
        provider_id = settings.get("astrbot_provider_id")

        if disable_when_llm_translation_enabled and self._should_inject_llm_translation_tags():
            if provider_id and not self._llm_translation_conflict_logged:
                logger.info(
                    "当前“外语TTS准备方式”为主 LLM 注入标签，AstrBot Provider 翻译将自动忽略。"
                )
                self._llm_translation_conflict_logged = True
            return False

        if self._get_translation_workflow() != "provider_translation":
            return False

        return bool(provider_id)

    def _has_external_translation_api_config(self) -> bool:
        api_config = self.config.get("translation_api", {})
        return bool(api_config.get("base_url") and api_config.get("api_key"))

    def _normalize_tts_output_mode(
        self, mode: Optional[object], default: str
    ) -> str:
        normalized = str(mode or "").strip().lower()
        mode_aliases = {
            "audio_only": "audio_only",
            "voice_only": "audio_only",
            "only_audio": "audio_only",
            "only_voice": "audio_only",
            "纯语音": "audio_only",
            "只发语音": "audio_only",
            "只发语音不发文字": "audio_only",
            "audio_and_text": "audio_and_text",
            "voice_and_text": "audio_and_text",
            "text_with_audio": "audio_and_text",
            "both": "audio_and_text",
            "full_text": "audio_and_text",
            "语音加文字": "audio_and_text",
            "语音跟原文都有": "audio_and_text",
            "原文和语音": "audio_and_text",
            "split_audio_text": "split_audio_text",
            "mixed": "split_audio_text",
            "hybrid": "split_audio_text",
            "partial_text": "split_audio_text",
            "一半文字一半语音": "split_audio_text",
            "半文字半语音": "split_audio_text",
        }
        return mode_aliases.get(normalized, default)

    def _get_auto_tts_output_mode(self) -> str:
        configured_mode = self.config.get("auto_tts_output_mode")
        if configured_mode in (None, ""):
            legacy_value = self.config.get("send_text_with_audio")
            if legacy_value is not None:
                return "audio_and_text" if legacy_value else "audio_only"
        return self._normalize_tts_output_mode(
            configured_mode, default="audio_and_text"
        )

    def _get_llm_tool_tts_output_mode(self) -> str:
        configured_mode = self.config.get("llm_tool_tts_output_mode", "audio_only")
        return self._normalize_tts_output_mode(
            configured_mode, default="audio_only"
        )

    def _split_text_for_mixed_output(self, text: str) -> Tuple[str, str]:
        compact_text = re.sub(r"\s+", " ", (text or "")).strip()
        if not compact_text:
            return "", ""

        regex_pattern = self.config.get(
            "sentence_split_regex", r"([。、，！？,.!?])"
        )
        try:
            parts = re.split(regex_pattern, compact_text)
        except re.error:
            parts = re.split(r"([。！？!?；;，,、])", compact_text)

        sentences = []
        for index in range(0, len(parts) - 1, 2):
            sentence = parts[index]
            delimiter = parts[index + 1] if index + 1 < len(parts) else ""
            merged = f"{sentence}{delimiter}".strip()
            if merged:
                sentences.append(merged)
        if len(parts) % 2 == 1 and parts[-1].strip():
            sentences.append(parts[-1].strip())

        if len(sentences) >= 2:
            split_index = max(1, len(sentences) // 2)
            audio_text = "".join(sentences[:split_index]).strip()
            plain_text = "".join(sentences[split_index:]).strip()
            return audio_text, plain_text

        pivot = max(1, len(compact_text) // 2)
        split_index = -1
        for match in re.finditer(r"[。！？!?；;，,、]", compact_text):
            candidate = match.end()
            if candidate >= pivot:
                split_index = candidate
                break

        if split_index <= 0:
            split_index = compact_text.rfind(" ", 0, pivot)
        if split_index <= 0 or split_index >= len(compact_text):
            return compact_text, ""

        audio_text = compact_text[:split_index].strip()
        plain_text = compact_text[split_index:].strip()
        return audio_text, plain_text

    async def _send_audio_message(self, session_id: str, audio_path: str) -> bool:
        return await self.context.send_message(
            session_id, MessageChain(chain=[Comp.Record(file=audio_path)])
        )

    async def _send_text_message(self, session_id: str, text: str) -> bool:
        text = self._strip_pause_markers(text)
        return await self.context.send_message(
            session_id, MessageChain(chain=[Comp.Plain(text)])
        )

    def _prepare_tts_output_segments(
        self, display_text: str, output_mode: str
    ) -> Tuple[str, str, str]:
        resolved_mode = self._normalize_tts_output_mode(
            output_mode, default="audio_and_text"
        )

        if resolved_mode == "audio_only":
            return display_text, "", resolved_mode

        if resolved_mode == "split_audio_text":
            audio_text, plain_text = self._split_text_for_mixed_output(display_text)
            if audio_text and plain_text:
                return audio_text, plain_text, resolved_mode
            logger.info("Mixed TTS output could not split text cleanly, fallback to audio_and_text.")
            resolved_mode = "audio_and_text"

        return display_text, display_text, resolved_mode

    async def _apply_auto_tts_output_mode(
        self,
        session_id: str,
        resp: LLMResponse,
        audio_path: str,
        full_display_text: str,
        plain_display_text: str,
        output_mode: str,
    ) -> bool:
        """把合成好的语音接到这次回复上，返回语音是否真的送出去了。"""
        if output_mode == "audio_only":
            resp.result_chain.chain = [Comp.Record(file=audio_path)]
            return True

        audio_sent = await self._send_audio_message(session_id, audio_path)
        if audio_sent:
            resp.completion_text = plain_display_text
            resp.result_chain.chain = [Comp.Plain(plain_display_text)]
            return True

        # 语音已经合成好了，只是发不出去。这里以前硬拼一行 "(TTS音频发送失败)"：
        # 既绕过了失败提示开关，也从来不写日志，于是日志页里查不到这类失败。
        logger.warning(
            f"Genie TTS: [{session_id}] 语音已合成但音频发送失败，本次回退为纯文本。"
        )
        resp.completion_text = full_display_text
        resp.result_chain.chain = [Comp.Plain(full_display_text)]
        self._append_tts_failure_notice(resp, "TTS音频发送失败")
        return False

    async def _dispatch_llm_tool_tts_output(
        self,
        session_id: str,
        audio_path: str,
        full_display_text: str,
        plain_display_text: str,
        output_mode: str,
    ) -> Tuple[bool, Optional[str]]:
        if output_mode == "audio_only":
            ok = await self._send_audio_message(session_id, audio_path)
            if ok:
                return True, None
            return False, "语音已经合成成功，但 AstrBot 主动发送语音失败了。"

        ok = await self._send_audio_message(session_id, audio_path)
        if not ok:
            return False, "语音已经合成成功，但 AstrBot 主动发送语音失败了。"

        if plain_display_text:
            text_ok = await self._send_text_message(session_id, plain_display_text)
            if not text_ok:
                return False, "语音已经发出，但补发文字失败了。"

        return True, None
    # ---------------------------------------------- 引用语音：定位 / 取回 / 外发

    @staticmethod
    def _find_reply_component(event: AstrMessageEvent) -> Optional[Comp.Reply]:
        """取出这条消息引用的那条消息；没有引用则返回 None。"""
        try:
            components = list(event.get_messages() or [])
        except Exception:
            return None
        for comp in components:
            if isinstance(comp, Comp.Reply):
                return comp
        return None

    @staticmethod
    def _reply_records(reply: Optional[Comp.Reply]) -> List[Any]:
        """被引用消息链里的语音段。有些协议端不回填 chain，空不代表没语音。"""
        if reply is None:
            return []
        return [
            sub
            for sub in (getattr(reply, "chain", None) or [])
            if isinstance(sub, Comp.Record)
        ]

    @staticmethod
    def _reply_is_self(event: AstrMessageEvent, reply: Optional[Comp.Reply]) -> bool:
        """被引用的那条是不是 bot 自己发的。"""
        if reply is None:
            return False
        sender = str(getattr(reply, "sender_id", "") or "").strip()
        self_id = str(getattr(event.message_obj, "self_id", "") or "").strip()
        return bool(sender) and bool(self_id) and sender == self_id

    async def _fetch_quoted_payload(
        self, event: AstrMessageEvent, message_id: Any
    ) -> Dict[str, Any]:
        """向协议端问被引用消息的原始数据（时间戳 + 消息段）。

        Comp.Reply.time 在 aiocqhttp 适配器里被改写成了「收到事件的时间」，拿它
        做匹配会把所有引用都算成「刚刚」；get_msg 回的 time 才是原消息时间戳。
        get_msg 是标准 OneBot v11 action，NapCat / LLOneBot(LLBot) / SnowLuma 都有。
        非 OneBot 平台没有 bot 对象，直接返回空字典走近似匹配。
        """
        bot = getattr(event, "bot", None)
        raw_id = str(message_id or "").strip()
        if bot is None or not raw_id:
            return {}
        try:
            target_id: Any = int(raw_id)
        except ValueError:
            target_id = raw_id
        try:
            raw = await bot.get_msg(message_id=target_id)
        except Exception as exc:
            logger.debug(f"Genie TTS: get_msg 取引用消息失败，改用近似匹配: {exc}")
            return {}
        if not isinstance(raw, dict):
            return {}
        # aiocqhttp 已经拆掉一层 data，别的桥接可能还包着。
        inner = raw.get("data")
        if isinstance(inner, dict) and ("message" in inner or "time" in inner):
            return inner
        return raw

    @staticmethod
    def _payload_time(payload: Dict[str, Any]) -> Optional[float]:
        """从 get_msg 返回体里读发送时间（秒）。读不到返回 None。"""
        try:
            stamp = float((payload or {}).get("time"))
        except (TypeError, ValueError):
            return None
        if stamp <= 0:
            return None
        # 少数实现回的是毫秒；秒级时间戳在本世纪内不可能超过 1e11。
        if stamp > 1e11:
            stamp /= 1000.0
        return stamp

    @staticmethod
    def _payload_record_segments(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 get_msg 返回体里挑出语音段。message 既可能是数组也可能是 CQ 码串。"""
        message = (payload or {}).get("message")
        found: List[Dict[str, Any]] = []
        if isinstance(message, list):
            for seg in message:
                if not isinstance(seg, dict):
                    continue
                if str(seg.get("type") or "").lower() != "record":
                    continue
                data = seg.get("data")
                found.append(data if isinstance(data, dict) else {})
            return found
        if isinstance(message, str) and "[CQ:record" in message:
            for chunk in re.findall(r"\[CQ:record,([^\]]*)\]", message):
                data: Dict[str, Any] = {}
                for pair in chunk.split(","):
                    key, sep, value = pair.partition("=")
                    if sep:
                        data[key.strip()] = value.strip()
                found.append(data)
        return found

    def _match_sent_voice(
        self, session_id: str, quoted_at: Optional[float]
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """在本会话已发语音环里找出被引用的那条。

        有真实时间戳就按时间就近匹配（窗口 QUOTE_MATCH_WINDOW）；拿不到时间
        （非 OneBot 平台 / get_msg 不可用）就退化成「只有一条就用它，否则用最近
        一条」，跟 /tts-again 的语义保持一致。
        """
        ring = self._session_voice_ring(session_id)
        if not ring:
            return None, "本会话没有还在的语音记录"
        if quoted_at is None:
            if len(ring) == 1:
                return ring[0], "会话内唯一记录"
            return ring[0], "拿不到引用时间，取最近一条"
        best: Optional[Dict[str, Any]] = None
        best_delta = 0.0
        for item in ring:
            delta = abs(float(item.get("sent_at") or 0.0) - quoted_at)
            if best is None or delta < best_delta:
                best, best_delta = item, delta
        if best is None or best_delta > self.QUOTE_MATCH_WINDOW:
            return None, "本地记录与引用时间对不上"
        return best, f"时间匹配 ±{best_delta:.1f}s"

    @staticmethod
    def _file_uri_to_path(uri: str) -> str:
        """把 file:// URI 还原成本地路径，Windows 盘符与 UNC 都照顾到。

        SDK 里的 convert_to_file_path 是硬切 8 个字符，遇到规范的
        file:///home/x 会丢掉前导斜杠，所以这里自己解一遍。
        """
        from urllib.parse import unquote, urlparse

        try:
            parsed = urlparse(uri)
        except Exception:
            return ""
        local = unquote(parsed.path or "")
        host = (parsed.netloc or "").strip()
        if host and host.lower() != "localhost":
            return "//" + host + local
        if re.match(r"^/[A-Za-z]:", local):
            local = local[1:]
        return local

    async def _localize_voice_hint(self, hint: Any) -> str:
        """把协议端给的 路径 / file:// / http / base64:// 变成本地文件路径。

        失败返回空串——这条链本来就是兜底，任何一步不通都还有下一个候选。
        """
        text = str(hint or "").strip()
        if not text:
            return ""
        if os.path.isfile(text):
            return os.path.abspath(text)
        lowered = text.lower()
        if lowered.startswith("file://"):
            local = self._file_uri_to_path(text)
            return os.path.abspath(local) if local and os.path.isfile(local) else ""
        if not lowered.startswith(("http://", "https://", "base64://")):
            return ""
        try:
            resolved = await Comp.Record(file=text).convert_to_file_path()
        except Exception as exc:
            logger.debug(f"Genie TTS: 语音候选取本地失败: {exc}")
            return ""
        return resolved if resolved and os.path.isfile(resolved) else ""

    async def _fetch_record_via_action(
        self,
        event: AstrMessageEvent,
        reply: Optional[Comp.Reply],
        payload: Dict[str, Any],
    ) -> str:
        """用 OneBot 的 get_record 让协议端把语音转成 WAV 落到本地。

        NapCat / LLOneBot(LLBot) / SnowLuma 都实现了这个 action。file 参数要的是
        协议端自己的语音标识（file / file_id / 文件名），所以候选值挨个试。
        """
        bot = getattr(event, "bot", None)
        if bot is None:
            return ""
        hints: List[str] = []

        def push(value: Any) -> None:
            item = str(value or "").strip()
            if item and item not in hints:
                hints.append(item)

        for rec in self._reply_records(reply):
            push(getattr(rec, "file", ""))
            push(getattr(rec, "path", ""))
        for data in self._payload_record_segments(payload):
            for key in ("file_id", "file", "file_unique", "path"):
                push(data.get(key))

        for hint in hints[:4]:
            try:
                raw = await bot.call_action("get_record", file=hint, out_format="wav")
            except Exception as exc:
                logger.debug(f"Genie TTS: get_record 失败（候选已跳过）: {exc}")
                continue
            body = raw if isinstance(raw, dict) else {}
            inner = body.get("data")
            if isinstance(inner, dict):
                body = inner
            for key in ("path", "file", "url"):
                local = await self._localize_voice_hint(body.get(key))
                if local:
                    return local
        return ""

    async def _extract_platform_voice(
        self,
        event: AstrMessageEvent,
        reply: Optional[Comp.Reply],
        payload: Dict[str, Any],
    ) -> Tuple[str, str]:
        """兜底：把平台侧存的那份语音捞下来转成 WAV。返回 (路径, 说明)。

        协议端存的通常是它自己转码过的 silk/amr，比插件本地那份原始 WAV 有损，
        所以只在本地记录对不上时才走这条路，并且必须告诉用户音质降级了。
        """
        raw_path = ""
        channel = ""

        for rec in self._reply_records(reply):
            direct = str(getattr(rec, "path", "") or "").strip()
            if direct and os.path.isfile(direct):
                raw_path, channel = os.path.abspath(direct), "引用消息段"
                break
            resolved = await self._localize_voice_hint(getattr(rec, "file", ""))
            if not resolved:
                try:
                    candidate = await rec.convert_to_file_path()
                except Exception as exc:
                    logger.debug(f"Genie TTS: 语音段转本地文件失败: {exc}")
                    candidate = ""
                if candidate and os.path.isfile(candidate):
                    resolved = candidate
            if resolved:
                raw_path, channel = resolved, "引用消息段"
                break

        if not raw_path:
            for data in self._payload_record_segments(payload):
                for key in ("path", "file", "url"):
                    resolved = await self._localize_voice_hint(data.get(key))
                    if resolved:
                        raw_path, channel = resolved, "get_msg"
                        break
                if raw_path:
                    break

        if not raw_path:
            fetched = await self._fetch_record_via_action(event, reply, payload)
            if fetched:
                raw_path, channel = fetched, "get_record"

        if not raw_path:
            return "", "协议端没给出可读的语音文件"

        try:
            wav_path, convert_note = await self.audio_converter.to_wav(raw_path)
        except Exception as exc:
            logger.warning(f"Genie TTS: 引用语音转码失败: {exc}")
            return "", f"转码失败（{type(exc).__name__}）"
        return wav_path, channel + " → " + convert_note

    @staticmethod
    def _describe_local_voice(
        record: Dict[str, Any], note: str = ""
    ) -> Dict[str, Any]:
        """把本地发送记录包成统一的解析结果。"""
        return {
            "ok": True,
            "error": "",
            "path": str(record.get("path") or ""),
            "text": str(record.get("text") or ""),
            "character": str(record.get("character") or ""),
            "emotion": str(record.get("emotion") or ""),
            "lossy": False,
            "note": note,
            "source": "plugin",
        }

    async def _resolve_quoted_voice(
        self, event: AstrMessageEvent, allow_fallback: bool = True
    ) -> Dict[str, Any]:
        """把「用户引用的那条语音」解析成一个本地可读的音频文件。

        优先用插件自己的发送记录（原始 WAV，无损）；对不上才退回平台侧的有损
        副本。返回值里的 lossy 决定要不要在回复里提醒用户音质有损。
        """
        session_id = event.unified_msg_origin
        reply = self._find_reply_component(event)

        if reply is None:
            if not allow_fallback:
                return {"ok": False, "error": "请先引用我发的那条语音，再发这条指令。"}
            latest = self._latest_sent_voice(session_id)
            if latest is None:
                return {
                    "ok": False,
                    "error": (
                        "没找到语音。用法：引用我发的那条语音再发这条指令；"
                        "不引用时会自动用本会话最近一条语音。"
                    ),
                }
            return self._describe_local_voice(latest, "未引用，取本会话最近一条")

        payload = await self._fetch_quoted_payload(event, getattr(reply, "id", None))
        quoted_at = self._payload_time(payload)
        matched, note = self._match_sent_voice(session_id, quoted_at)
        if matched is not None:
            return self._describe_local_voice(matched, note)

        has_voice = bool(self._reply_records(reply)) or bool(
            self._payload_record_segments(payload)
        )
        quoted_text = str(getattr(reply, "message_str", "") or "").strip()
        if not has_voice and quoted_text:
            return {
                "ok": False,
                "error": "引用的那条消息里只有文字，没有语音。请引用一条语音消息。",
            }

        path, channel = await self._extract_platform_voice(event, reply, payload)
        if not path:
            return {
                "ok": False,
                "error": (
                    "没能取到这条语音（" + (channel or note) + "）。\n"
                    "常见原因：AstrBot 重启过、临时音频超过 30 分钟被清理、"
                    "或协议端不支持 get_record。\n"
                    "不引用直接发指令可以取本会话最近一条语音。"
                ),
            }
        return {
            "ok": True,
            "error": "",
            "path": path,
            "text": "",
            "character": "",
            "emotion": "",
            "lossy": True,
            "note": channel,
            "source": "platform",
        }

    def _voice_file_name(
        self,
        alias: str = "",
        text: str = "",
        character: str = "",
        emotion: str = "",
        suffix: str = ".wav",
    ) -> str:
        """给外发的语音文件起个能看懂的名字。"""
        seed = alias or (text or "")[:24] or "-".join(
            x for x in (character, emotion) if x
        )
        stem = voice_vault.safe_filename(seed, fallback=self.VAULT_BUNDLE_PREFIX)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        tail = suffix if suffix.startswith(".") else "." + (suffix or "wav")
        return stem + "-" + stamp + tail

    async def _send_voice_as_file(
        self, event: AstrMessageEvent, source_path: str, filename: str
    ) -> Tuple[bool, str]:
        """把音频当「文件」发出去，返回 (是否成功, 通道说明或失败原因)。

        先走 Comp.File：配了 callback_api_base 时 AstrBot 会把它注册成 HTTP 直链，
        协议端和 AstrBot 不在同一台机器上也能下到。失败再退回 OneBot 的
        upload_group_file / upload_private_file（NapCat / LLOneBot / SnowLuma 都有）。
        """
        abs_path = os.path.abspath(source_path)
        if not os.path.isfile(abs_path):
            return False, "音频文件已经不在了"
        mode = (
            str(self.config.get("voice_file_upload_mode", "auto") or "auto")
            .strip()
            .lower()
        )
        if mode not in ("auto", "component", "onebot_action"):
            mode = "auto"

        problems: List[str] = []
        if mode in ("auto", "component"):
            try:
                await event.send(
                    MessageChain(chain=[Comp.File(name=filename, file=abs_path)])
                )
                return True, "文件消息段"
            except Exception as exc:
                problems.append(f"文件消息段: {exc}")
                logger.warning(f"Genie TTS: 语音转文件走消息段失败: {exc}")

        if mode in ("auto", "onebot_action"):
            ok, err = await self._upload_file_via_onebot(event, abs_path, filename)
            if ok:
                return True, "OneBot 上传接口"
            problems.append(f"OneBot 上传接口: {err}")

        return False, "; ".join(problems) or "当前平台不支持发送文件"

    async def _upload_file_via_onebot(
        self, event: AstrMessageEvent, abs_path: str, filename: str
    ) -> Tuple[bool, str]:
        """走 OneBot v11 的群/私聊文件上传 action。"""
        bot = getattr(event, "bot", None)
        if bot is None:
            return False, "当前平台没有 OneBot 接口"
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        try:
            if group_id:
                await bot.call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=abs_path,
                    name=filename,
                )
            else:
                await bot.call_action(
                    "upload_private_file",
                    user_id=int(sender_id),
                    file=abs_path,
                    name=filename,
                )
        except Exception as exc:
            return False, str(exc)[:180]
        return True, ""

    # ------------------------------------------------------------ 收藏库辅助

    def _sync_vault_config(self) -> None:
        """把配置里的收藏容量同步给收藏库。WebUI 改完配置不用重启即生效。"""
        try:
            limit = int(
                self.config.get("voice_favorite_limit", voice_vault.DEFAULT_LIMIT)
                or voice_vault.DEFAULT_LIMIT
            )
        except (TypeError, ValueError):
            limit = voice_vault.DEFAULT_LIMIT
        try:
            max_mb = float(self.config.get("voice_favorite_max_mb", 32) or 32)
        except (TypeError, ValueError):
            max_mb = 32.0
        self.voice_vault.configure(
            limit=limit, max_bytes=int(max(max_mb, 1.0) * 1024 * 1024)
        )

    def _favorites_enabled(self) -> bool:
        return bool(self.config.get("enable_voice_favorites", True))

    @staticmethod
    def _favorites_disabled_hint() -> str:
        return (
            "语音收藏功能当前是关闭的。\n"
            "去 AstrBot WebUI 的插件配置里打开 enable_voice_favorites，"
            "或在「语音合成工作台」的设置页勾上「启用语音收藏」。"
        )

    def _vault_bundle_dir(self) -> Path:
        """收藏包（.zip）的服务端目录，惰性创建。"""
        base = (
            Path(getattr(self, "plugin_data_dir", "."))
            / self.VAULT_DIR_NAME
            / "bundles"
        )
        base.mkdir(parents=True, exist_ok=True)
        return base

    @staticmethod
    def _format_favorite_row(entry: Dict[str, Any]) -> str:
        """把一条收藏渲染成列表里的一行。"""
        index = int(entry.get("index") or 0)
        head = f"{index}. " if index else "• "
        pin = "📌" if entry.get("pinned") else ""
        name = str(entry.get("alias") or "").strip() or (
            "#" + str(entry.get("id") or "")[:6]
        )
        meta = [audio_compat.format_duration(int(entry.get("duration_ms") or 0))]
        tone = "/".join(
            str(x) for x in (entry.get("character"), entry.get("emotion")) if x
        )
        if tone:
            meta.append(tone)
        if entry.get("source") == "platform":
            meta.append("有损")
        plays = int(entry.get("play_count") or 0)
        if plays:
            meta.append(f"播{plays}")
        body = re.sub(r"\s+", " ", str(entry.get("text") or "")).strip()
        if len(body) > 28:
            body = body[:28] + "…"
        line = head + pin + name + " [" + " · ".join(meta) + "]"
        return line + " " + body if body else line

    def _favorite_index(self, entry_id: str) -> int:
        """查一条收藏在全库里的序号（1 起）。查不到返回 0。"""
        target = str(entry_id or "")
        if not target:
            return 0
        for position, row in enumerate(self.voice_vault.entries(), start=1):
            if row.get("id") == target:
                return position
        return 0

    async def _pick_favorite(
        self, token: str
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """按用户给的「序号 / 名字 / id」取一条收藏，返回 (带 index 的副本, 错误)。"""
        self._sync_vault_config()
        try:
            await self.voice_vault.load()
        except Exception as exc:
            logger.error(f"Genie TTS: 读取收藏库失败: {exc}")
            return None, f"读取收藏库失败: {exc}"
        entry, err = self.voice_vault.resolve(token)
        if entry is None:
            return None, err or "没找到这条收藏。"
        picked = dict(entry)
        picked["index"] = self._favorite_index(str(entry.get("id") or ""))
        return picked, ""

    def _favorite_stats_line(self) -> str:
        stats = self.voice_vault.stats()
        line = (
            f"共 {stats.get('count')}/{stats.get('limit')} 条 · "
            f"{stats.get('total_bytes_human')} · {stats.get('total_duration_human')}"
        )
        if stats.get("pinned"):
            line += f" · 置顶 {stats.get('pinned')}"
        return line

    async def _bundle_path_from_event(
        self, event: AstrMessageEvent
    ) -> Tuple[str, str]:
        """从附件或引用消息里找出收藏包文件，返回 (本地路径, 来源说明)。"""
        try:
            components = list(event.get_messages() or [])
        except Exception:
            components = []
        candidates: List[Any] = []
        for comp in components:
            if isinstance(comp, Comp.File):
                candidates.append(comp)
        for comp in components:
            if not isinstance(comp, Comp.Reply):
                continue
            for sub in getattr(comp, "chain", None) or []:
                if isinstance(sub, Comp.File):
                    candidates.append(sub)
        for comp in candidates:
            try:
                local_path = await comp.get_file()
            except Exception as exc:
                logger.warning(f"Genie TTS: 获取收藏包附件失败: {exc}")
                continue
            if not local_path or not os.path.isfile(local_path):
                continue
            name = str(getattr(comp, "name", "") or "") or os.path.basename(
                str(local_path)
            )
            return os.path.abspath(str(local_path)), f"附件 {name}"
        return "", ""

    def _get_keepalive_urls(self) -> list[str]:
        """获取所有需要保活的目标地址。包括配置的TTS服务器和额外的保活地址。"""
        urls = set()

        # 添加所有配置的TTS服务器
        servers = self.config.get("tts_servers", [])
        if servers:
            for server in servers:
                if isinstance(server, str) and server:
                    urls.add(server.rstrip("/"))

        # 添加额外配置的保活地址
        custom_url = self.config.get("space_keepalive_url")
        if custom_url:
            urls.add(custom_url.rstrip("/"))

        return list(urls)

    async def _keep_alive_loop(self):
        """定时ping所有目标地址以避免休眠。"""
        interval_minutes = max(
            int(self.config.get("space_keepalive_interval_minutes", 25)), 1
        )

        async def ping(url):
            try:
                response = await self.http_client.get(url, timeout=30)
                logger.info(f"保活请求已发送到 {url}，状态码: {response.status_code}")
            except Exception as exc:
                logger.warning(f"向 {url} 发送保活请求失败: {exc}")

        while not self._keepalive_stop_event.is_set():
            try:
                target_urls = self._get_keepalive_urls()
                if not target_urls:
                    logger.warning("未找到任何可用于保活的地址，已跳过本次保活任务。")
                else:
                    await asyncio.gather(*(ping(url) for url in target_urls))
            except Exception as e:
                logger.error(f"保活任务发生意外错误: {e}")

            try:
                await asyncio.wait_for(
                    self._keepalive_stop_event.wait(), timeout=interval_minutes * 60
                )
            except asyncio.TimeoutError:
                continue

    @filter.command("注册感情")
    async def register_emotion_command(
        self,
        event: AstrMessageEvent,
        character_name: str,
        emotion_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        language: str = None,
    ):
        """注册一个新的感情并保存到文件"""
        if ".." in ref_audio_path or os.path.isabs(ref_audio_path):
            yield event.plain_result(
                "❌ 错误：参考音频路径无效。它必须是一个相对路径，且不能包含 '..'。"
            )
            return

        if self.emotion_manager.register_emotion(
            character_name, emotion_name, ref_audio_path, ref_audio_text, language
        ):
            yield event.plain_result(
                f"✅ 感情 '{emotion_name}' 已成功注册到角色 '{character_name}' 下。"
            )
        else:
            self.emotion_manager.reload()  # 如果保存失败，从文件重新加载以恢复状态
            yield event.plain_result("❌ 保存感情时发生错误，注册失败。")

    @filter.command("删除感情")
    async def delete_emotion_command(
        self, event: AstrMessageEvent, character_name: str, emotion_name: str
    ):
        """删除一个已注册的感情"""
        if not self.emotion_manager.character_exists(character_name):
            yield event.plain_result(f"❌ 错误：未找到角色 '{character_name}'。")
            return

        if not self.emotion_manager.get_emotion_data(character_name, emotion_name):
            yield event.plain_result(
                f"❌ 错误：角色 '{character_name}' 下未找到名为 '{emotion_name}' 的感情。"
            )
            return

        if self.emotion_manager.delete_emotion(character_name, emotion_name):
            yield event.plain_result(
                f"✅ 已成功删除角色 '{character_name}' 的感情 '{emotion_name}'。"
            )
        else:
            self.emotion_manager.reload()  # 如果保存失败，从文件重新加载以恢复状态
            yield event.plain_result("❌ 保存文件时发生错误，删除失败。")

    @filter.command("查看感情")
    async def view_emotions_command(
        self, event: AstrMessageEvent, character_name: str = ""
    ):
        """查看已注册的感情，可选只看某一个角色"""
        emotions_data = self.emotion_manager.emotions_data
        if not emotions_data:
            yield event.plain_result("当前未注册任何感情。")
            return

        wanted = (character_name or "").strip()
        if wanted:
            if wanted not in emotions_data:
                available = "、".join(str(name) for name in list(emotions_data)[:20])
                yield event.plain_result(
                    f"❌ 未找到角色 {wanted}。\n已注册角色: {available}"
                )
                return
            items = [(wanted, emotions_data.get(wanted) or {})]
            formatted_lines = [f"角色 {wanted} 的感情列表："]
        else:
            items = list(emotions_data.items())
            formatted_lines = ["所有已注册的感情列表："]

        total = 0
        for character, emotions in items:
            formatted_lines.append(f"\n角色: {character}")
            if isinstance(emotions, dict) and emotions:
                for emotion_name in emotions.keys():
                    formatted_lines.append(f"  - {emotion_name}")
                    total += 1
            else:
                formatted_lines.append("  (暂无感情)")

        formatted_lines.append(f"\n共 {len(items)} 个角色 / {total} 条感情。")
        formatted_lines.append("导出用 /感情导出，导入用 /感情导入，快照用 /感情包。")
        yield event.plain_result("\n".join(formatted_lines))

    # --------------------------------------------------- 感情包（导入导出）

    def _pack_dir(self) -> Path:
        """感情包快照目录，惰性创建。WebUI 与指令共用同一个目录。"""
        base = Path(getattr(self, "plugin_data_dir", ".")) / self.PACK_DIR_NAME
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _clean_emotion_characters(self) -> Tuple[Dict, list]:
        """返回通过校验的感情数据，以及被判定不合法的记录说明。"""
        try:
            return emotion_pack.normalize_characters(self.emotion_manager.emotions_data)
        except EmotionPackError as exc:
            logger.error(f"Genie TTS: 感情库整体不可解析: {exc}")
            return {}, []

    async def _commit_emotion_characters(self, characters: Dict) -> bool:
        """整体写回 emotions.json；失败时回滚到磁盘上的旧值。"""
        async with self._emotion_write_lock:
            manager = self.emotion_manager
            manager.emotions_data = characters
            ok = False
            try:
                ok = bool(manager._save_emotions_to_file())
            except Exception as exc:
                logger.error(f"Genie TTS: 写入 emotions.json 异常: {exc}")
                ok = False
            if not ok:
                try:
                    manager.reload()
                except Exception:
                    pass
            return ok

    @staticmethod
    def _strip_command_prefix(message_str: str, names: Tuple[str, ...]) -> str:
        """去掉指令名，把剩下的整段原文交还调用方当 JSON 载荷。

        AstrBot 的指令过滤器只会把消息切成 token 塞进声明的形参，多余的会被
        丢掉；感情包 JSON 里全是空格和花括号，只能自己从原文里切。

        注意：这里**不能**把空白压成一行。感情包常常裹在 Markdown 代码块里，
        压掉换行后 "\u0060\u0060\u0060json" 会和 JSON 正文粘成一行，围栏就剥不掉了。
        """
        text = (message_str or "").strip().lstrip("\ufeff").strip()
        if not text:
            return ""
        for name in names:
            for prefix in (name, "/" + name, "!" + name, "！" + name, "#" + name):
                if not text.startswith(prefix):
                    continue
                rest = text[len(prefix):]
                if not rest:
                    return ""
                if rest[0].isspace():
                    return rest.strip()
        return text

    def _split_pack_args(self, raw: str) -> Tuple[str, bool, str]:
        """从指令尾巴里解析出 (模式, 是否试运行, 剩余载荷)。

        只从头部最多吃两个 token，避免把 JSON 正文误当成参数；一个不认识但
        明显是「想写模式却写错了」的 token 也会被吃掉，好让上层报出清晰的
        模式错误，而不是让它混进 JSON 里变成难懂的解析失败。
        """
        text = (raw or "").strip()
        mode = ""
        dry = False
        for _round in range(2):
            if not text:
                break
            # 载荷可能从下一行才开始，所以按任意空白切，而不是只切空格。
            head_match = re.match(r"(\S+)\s*", text)
            if not head_match:
                break
            token = head_match.group(1)
            rest = text[head_match.end():].strip()
            lowered = token.lower()
            if not mode and (
                lowered in self.PACK_MODE_TOKENS or token in self.PACK_MODE_TOKENS
            ):
                mode = token
                text = rest
                continue
            if not dry and lowered in self.PACK_DRY_TOKENS:
                dry = True
                text = rest
                continue
            if (
                not mode
                and len(token) <= 24
                and not self._looks_like_pack_text(token)
                and (not rest or self._looks_like_pack_text(rest))
            ):
                # 写错的模式词，交给 normalize_import_mode 报「模式只能是…」
                mode = token
                text = rest
                continue
            break
        return mode, dry, text

    @staticmethod
    def _looks_like_pack_text(text: str) -> bool:
        """粗判一段文本是不是感情包 JSON（裸对象或 Markdown 代码块）。"""
        stripped = (text or "").strip().lstrip("\ufeff").strip()
        if not stripped:
            return False
        return stripped.startswith("{") or stripped.startswith("\u0060\u0060\u0060")

    async def _pack_text_from_event(
        self, event: AstrMessageEvent, inline_text: str
    ) -> Tuple[str, str]:
        """按 消息正文 > 引用消息 > 上传附件 的优先级找出感情包内容。"""
        inline = (inline_text or "").strip()
        if self._looks_like_pack_text(inline):
            return inline, "消息正文"

        try:
            components = list(event.get_messages() or [])
        except Exception:
            components = []

        for comp in components:
            if not isinstance(comp, Comp.Reply):
                continue
            quoted = (getattr(comp, "message_str", "") or "").strip()
            if not quoted:
                pieces = []
                for sub in getattr(comp, "chain", None) or []:
                    if isinstance(sub, Comp.Plain):
                        pieces.append(getattr(sub, "text", "") or "")
                quoted = "".join(pieces).strip()
            if quoted:
                return quoted, "引用消息"

        for comp in components:
            if not isinstance(comp, Comp.File):
                continue
            try:
                local_path = await comp.get_file()
            except Exception as exc:
                logger.warning(f"Genie TTS: 获取感情包附件失败: {exc}")
                continue
            if not local_path or not os.path.isfile(local_path):
                continue
            try:
                with open(local_path, "r", encoding="utf-8-sig") as handle:
                    content = handle.read(emotion_pack.MAX_PACK_BYTES + 1)
            except Exception as exc:
                logger.warning(f"Genie TTS: 读取感情包附件失败: {exc}")
                continue
            if content.strip():
                name = getattr(comp, "name", "") or os.path.basename(local_path)
                return content, f"附件 {name}"

        return inline, "消息正文"

    async def _apply_pack_import(
        self, incoming: Dict, mode: str, dry: bool, invalid: Optional[list] = None
    ) -> Tuple[Dict, str]:
        """算出合并结果并按需落盘，返回 (变更报告, 可直接回显的摘要)。"""
        current, _current_invalid = self._clean_emotion_characters()
        merged, report = emotion_pack.compute_import(current, incoming, mode, invalid)
        summary = emotion_pack.describe_report(report)
        if dry:
            return report, summary + "\n\n🧪 试运行结束，未写入。去掉「试运行」即真正导入。"
        if not report.get("changed"):
            return report, summary + "\n\n没有需要写入的变化，emotions.json 保持原样。"
        if not await self._commit_emotion_characters(merged):
            return report, summary + "\n\n❌ 写入 emotions.json 失败，已回滚到旧数据。"
        return report, summary + "\n\n✅ 已写入 emotions.json，立即生效，无需重启。"

    @filter.command("感情导出", alias={"导出感情"})
    async def export_emotions_command(
        self, event: AstrMessageEvent, character_name: str = ""
    ):
        """导出感情包 JSON；省略角色名则导出全部角色"""
        characters, invalid = self._clean_emotion_characters()
        if not characters:
            yield event.plain_result(
                "当前没有可导出的感情。先用 /注册感情 或 WebUI 工作台添加几条。"
            )
            return

        wanted = (character_name or "").strip()
        if wanted:
            picked = emotion_pack.select_characters(characters, [wanted], None)
            if not picked:
                available = "、".join(list(characters)[:20])
                yield event.plain_result(f"❌ 未找到角色 {wanted}。\n可导出: {available}")
                return
        else:
            picked = characters

        pack = emotion_pack.build_pack(
            picked, plugin_version=self.PLUGIN_VERSION, source="command"
        )
        text = emotion_pack.dumps_pack(pack)
        filename = emotion_pack.default_pack_filename(picked)

        target = None
        try:
            target = self._pack_dir() / filename
            target.write_text(text, encoding="utf-8")
            saved_hint = f"服务端快照: {filename}"
        except Exception as exc:
            logger.error(f"Genie TTS: 保存感情包快照失败: {exc}")
            target = None
            saved_hint = f"⚠ 服务端快照保存失败: {exc}"

        counts = emotion_pack.summarize(picked)
        char_count = counts.get("characters", 0)
        emotion_count = counts.get("emotions", 0)
        size_kb = max(1, len(text.encode("utf-8")) // 1024)
        scope_label = wanted or "全部角色"
        summary = [
            "📦 感情包已导出",
            f"范围: {scope_label}",
            f"内容: {char_count} 个角色 / {emotion_count} 条感情（约 {size_kb}KB）",
            saved_hint,
        ]
        if invalid:
            summary.append(f"⚠ 跳过 {len(invalid)} 条不合法记录，可在 WebUI 工作台里修。")
        summary.append(f"恢复: /感情包恢复 {filename} [模式] [试运行]")
        summary.append("也可以在 WebUI 工作台「感情包」页直接下载 .json。")
        yield event.plain_result("\n".join(summary))

        # QQ 侧才有文件消息段；发失败不影响上面的摘要与快照。
        if target is not None and event.get_platform_name() == "aiocqhttp":
            try:
                await event.send(
                    MessageChain(chain=[Comp.File(name=filename, file=str(target))])
                )
            except Exception as exc:
                logger.warning(f"Genie TTS: 感情包文件发送失败: {exc}")

        # 短包直接贴原文，方便复制到别处；/感情导入 能自动剥掉代码围栏。
        fence = "\u0060\u0060\u0060"
        if len(text) <= 1800:
            yield event.plain_result(fence + "json\n" + text + "\n" + fence)
        else:
            yield event.plain_result(
                "JSON 超过 1800 字就不贴原文了，请用上面的快照文件或 WebUI 下载。"
            )

    @filter.command("感情导入", alias={"导入感情"})
    async def import_emotions_command(self, event: AstrMessageEvent, mode: str = ""):
        """导入感情包：正文附 JSON、引用含 JSON 的消息，或上传 .json 附件"""
        raw_tail = self._strip_command_prefix(
            event.get_message_str(), ("感情导入", "导入感情")
        )
        parsed_mode, dry, payload = self._split_pack_args(raw_tail)
        chosen_mode = parsed_mode
        if not chosen_mode:
            # 过滤器会把正文的第一个 token 塞进声明的形参，那可能是 "{" 或者
            # 代码围栏，只有确实是模式词/试运行词时才采信。
            hint = (mode or "").strip()
            lowered = hint.lower()
            if lowered in self.PACK_MODE_TOKENS or hint in self.PACK_MODE_TOKENS:
                chosen_mode = hint
            elif lowered in self.PACK_DRY_TOKENS:
                dry = True

        text, origin = await self._pack_text_from_event(event, payload)
        if not (text or "").strip():
            yield event.plain_result(self._pack_import_usage())
            return

        try:
            incoming, meta, invalid_incoming = emotion_pack.loads_pack(text)
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ 感情包解析失败（来源: {origin}）：{exc}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 感情包解析异常（来源: {origin}）：{exc}")
            return

        if not incoming:
            reason = f"，其中 {len(invalid_incoming)} 条不合法" if invalid_incoming else ""
            yield event.plain_result(
                f"❌ 感情包里没有任何可用记录（来源: {origin}）{reason}。"
            )
            return

        try:
            _report, tail = await self._apply_pack_import(
                incoming, chosen_mode, dry, invalid_incoming
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        title = "📥 感情包导入（试运行）" if dry else "📥 感情包导入"
        head = [title, f"来源: {origin}"]
        note = str((meta or {}).get("note") or "").strip()
        if note:
            head.append(f"备注: {note[:120]}")
        source_tag = str((meta or {}).get("source") or "").strip()
        if source_tag:
            head.append(f"来源标记: {source_tag[:60]}")
        yield event.plain_result("\n".join(head) + "\n" + tail)

    @staticmethod
    def _pack_import_usage() -> str:
        fence = "\u0060\u0060\u0060"
        return (
            "❌ 没找到感情包内容。三种用法：\n"
            "1) 直接贴 JSON：/感情导入 merge {...}\n"
            "2) 引用一条含 JSON 的消息，再发 /感情导入\n"
            "3) 上传 .json 文件，在同一条消息里写 /感情导入\n"
            "模式：merge 只补新（默认）/ overwrite 冲突覆盖 / replace 先清空\n"
            "加「试运行」只预演不写盘，例如 /感情导入 overwrite 试运行\n"
            "JSON 可以是 /感情导出 的完整包，也可以是裸的 emotions.json；"
            "包在 " + fence + " 代码块里也能识别。"
        )

    @filter.command("感情包", alias={"感情包列表"})
    async def list_emotion_packs_command(self, event: AstrMessageEvent):
        """列出服务端保存的感情包快照"""
        try:
            base = self._pack_dir()
            entries = sorted(
                (
                    item
                    for item in base.iterdir()
                    if item.is_file() and item.suffix.lower() == ".json"
                ),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except Exception as exc:
            yield event.plain_result(f"❌ 读取快照目录失败：{exc}")
            return

        if not entries:
            yield event.plain_result(
                "还没有任何感情包快照。\n"
                "用 /感情包保存 [文件名] 存一份，/感情导出 也会自动入库。"
            )
            return

        lines = [f"📦 感情包快照（共 {len(entries)} 份，按时间倒序）"]
        for item in entries[:20]:
            try:
                stat = item.stat()
                stamp = time.strftime("%m-%d %H:%M", time.localtime(stat.st_mtime))
                size_kb = max(1, stat.st_size // 1024)
            except Exception:
                stamp = "??"
                size_kb = 0
            try:
                pack_chars, _meta, _invalid = emotion_pack.loads_pack(
                    item.read_text(encoding="utf-8-sig")
                )
                counts = emotion_pack.summarize(pack_chars)
                char_count = counts.get("characters", 0)
                emotion_count = counts.get("emotions", 0)
                detail = f"{char_count} 角色 / {emotion_count} 感情"
            except Exception:
                detail = "⚠ 解析失败"
            lines.append(f"• {item.name}\n  {detail} · {size_kb}KB · {stamp}")

        if len(entries) > 20:
            lines.append(f"…另有 {len(entries) - 20} 份未列出。")
        lines.append("恢复: /感情包恢复 文件名 [模式] [试运行]")
        yield event.plain_result("\n".join(lines))

    @filter.command("感情包保存")
    async def save_emotion_pack_command(
        self, event: AstrMessageEvent, filename: str = ""
    ):
        """把当前感情库存成一份服务端快照"""
        characters, invalid = self._clean_emotion_characters()
        if not characters:
            yield event.plain_result("当前没有可保存的感情。")
            return

        name = (filename or "").strip()
        target_name = (
            emotion_pack.safe_pack_filename(name)
            if name
            else emotion_pack.default_pack_filename(characters)
        )
        try:
            target = self._pack_dir() / target_name
        except Exception as exc:
            yield event.plain_result(f"❌ 无法访问快照目录：{exc}")
            return

        if target.exists():
            yield event.plain_result(
                f"❌ 快照 {target_name} 已存在。换个文件名，或在 WebUI 里先删掉旧的。"
            )
            return

        pack = emotion_pack.build_pack(
            characters, plugin_version=self.PLUGIN_VERSION, source="command"
        )
        try:
            target.write_text(emotion_pack.dumps_pack(pack), encoding="utf-8")
        except Exception as exc:
            yield event.plain_result(f"❌ 写入快照失败：{exc}")
            return

        counts = emotion_pack.summarize(characters)
        char_count = counts.get("characters", 0)
        emotion_count = counts.get("emotions", 0)
        lines = [
            f"✅ 已保存快照 {target_name}",
            f"{char_count} 个角色 / {emotion_count} 条感情",
        ]
        if invalid:
            lines.append(f"⚠ 跳过 {len(invalid)} 条不合法记录。")
        lines.append(f"恢复: /感情包恢复 {target_name}")
        yield event.plain_result("\n".join(lines))

    @filter.command("感情包恢复")
    async def restore_emotion_pack_command(
        self,
        event: AstrMessageEvent,
        filename: str,
        mode: str = "merge",
        option: str = "",
    ):
        """从服务端快照恢复感情库"""
        target_name = emotion_pack.safe_pack_filename(filename)
        try:
            target = self._pack_dir() / target_name
        except Exception as exc:
            yield event.plain_result(f"❌ 无法访问快照目录：{exc}")
            return

        if not target.is_file():
            yield event.plain_result(
                f"❌ 找不到快照 {target_name}。用 /感情包 看看有哪些。"
            )
            return

        tokens = [str(mode or "").strip(), str(option or "").strip()]
        dry = any(token.lower() in self.PACK_DRY_TOKENS for token in tokens if token)
        chosen_mode = ""
        for token in tokens:
            if token and token.lower() not in self.PACK_DRY_TOKENS:
                chosen_mode = token
                break

        try:
            incoming, meta, invalid_incoming = emotion_pack.loads_pack(
                target.read_text(encoding="utf-8-sig")
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ 快照 {target_name} 解析失败：{exc}")
            return
        except Exception as exc:
            yield event.plain_result(f"❌ 读取快照 {target_name} 失败：{exc}")
            return

        if not incoming:
            yield event.plain_result(f"❌ 快照 {target_name} 里没有可用记录。")
            return

        try:
            _report, tail = await self._apply_pack_import(
                incoming, chosen_mode, dry, invalid_incoming
            )
        except EmotionPackError as exc:
            yield event.plain_result(f"❌ {exc}")
            return

        title = "♻️ 感情包恢复（试运行）" if dry else "♻️ 感情包恢复"
        head = [title, f"快照: {target_name}"]
        note = str((meta or {}).get("note") or "").strip()
        if note:
            head.append(f"备注: {note[:120]}")
        yield event.plain_result("\n".join(head) + "\n" + tail)

    @filter.command("合成")
    async def direct_tts_command(
        self,
        event: AstrMessageEvent,
        character_name: str,
        emotion_name: str,
        text_to_synthesize: str,
    ):
        """根据角色和感情名直接合成语音"""
        group_id = event.message_obj.group_id
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        # 指令过滤器不是 greedy 的：只有 GreedyStr 才会吃掉剩余全部 token，所以
        # "/合成 角色 感情 今天 天气 不错" 只会把「今天」塞进形参，后面全被丢掉。
        # 自己从原文里把第三段起的整句捞回来，带空格的文本才不会被截断。
        raw_tail = self._strip_command_prefix(event.get_message_str(), ("合成",))
        tail_parts = re.split(r"\s+", raw_tail.strip(), maxsplit=2)
        if (
            len(tail_parts) >= 3
            and tail_parts[0] == character_name
            and tail_parts[1] == emotion_name
        ):
            recovered = tail_parts[2].strip()
            if recovered:
                text_to_synthesize = recovered

        trace = self.run_log.begin_synth(
            "command",
            session=event.unified_msg_origin,
            group=self._normalize_group_id(group_id) or "",
            character=character_name,
            emotion=emotion_name,
            emotion_source="指令指定",
            tts_text=text_to_synthesize,
        )
        emotion_data = self.emotion_manager.get_emotion_data(
            character_name, emotion_name
        )
        if not emotion_data:
            trace.fail("感情不存在")
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}' 的感情 '{emotion_name}'。请先使用 /注册感情 指令添加。"
            )
            return

        trace.set(
            ref_audio=emotion_data.get("ref_audio_path"),
            ref_text=emotion_data.get("ref_audio_text"),
            language=emotion_data.get("language"),
        )
        yield event.plain_result("收到合成请求，正在处理...")
        audio_path = await self.tts_engine.synthesize(
            character_name=character_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=text_to_synthesize,
            session_id_for_log=event.unified_msg_origin,
            language=emotion_data.get("language"),
        )

        if audio_path:
            trace.ok(audio_path=audio_path, output_mode="仅语音")
            self._remember_last_audio(
                event.unified_msg_origin,
                audio_path,
                text=text_to_synthesize,
                character=character_name,
                emotion=emotion_name,
            )
            yield event.chain_result([Comp.Record(file=audio_path)])
        else:
            trace.fail("TTS合成失败")
            yield event.plain_result(
                "语音合成失败。\n"
                "• 若文本里只有标点或表情符号，是不会生成语音的；\n"
                "• 其它情况可用 /tts-status 检查服务器与队列。"
            )
        event.stop_event()

    @filter.command("tts-llm", alias={"开启语音合成"})
    async def start_tts(self, event: AstrMessageEvent):
        await self._observe_session(event, persist=False)
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.active_sessions.add(session_id)
        self.w_active_sessions.discard(session_id)
        if not group_id:
            self.inactive_private_sessions.discard(session_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        default_emotion = self.config.get("default_emotion_name")
        logger.info(f"会话 [{session_id}] 的 LLM TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的LLM语音合成已开启。\n将使用默认感情: {default_char} - {default_emotion}"
        )

    @filter.command("tts-q", alias={"关闭语音合成"})
    async def stop_tts(self, event: AstrMessageEvent):
        await self._observe_session(event, persist=False)
        session_id = event.unified_msg_origin
        self.active_sessions.discard(session_id)
        self.w_active_sessions.discard(session_id)
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id and self.config.get("enable_private_tts_by_default", True):
            self.inactive_private_sessions.add(session_id)
        await self._persist_state()
        logger.info(f"会话 [{session_id}] 的单独 LLM TTS 设置已清除。")
        group_id = self._normalize_group_id(event.message_obj.group_id)
        suffix = "\n本群仍通过群设置自动配音；关闭群配音请用 /ttg-q。" if self._is_group_tts_active(group_id) else ""
        yield event.plain_result("⏹️ 已清除本对话的单独配音设置。" + suffix)

    @filter.command("ttg", alias={"开启群语音"})
    async def start_group_tts(self, event: AstrMessageEvent):
        """开启当前群组的语音合成 (对所有人生效)"""
        await self._observe_session(event, persist=False)
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        self.inactive_groups.discard(group_id)
        self.active_groups.add(group_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        default_emotion = self.config.get("default_emotion_name")

        settings = self.config.get("llm_injection_settings", {})
        enable_emotion = settings.get("enable_llm_emotion", False)

        logger.info(f"群组 [{group_id}] 的 LLM TTS 功能已开启。")

        if enable_emotion:
            yield event.plain_result(
                f"▶️ 本群组的LLM语音合成已开启 (全员生效)。\n当前已启用LLM情感注入，情感将由AI自动决定。\n(默认保底情感: {default_char} - {default_emotion})"
            )
        else:
            yield event.plain_result(
                f"▶️ 本群组的LLM语音合成已开启 (全员生效)。\n当前为固定情感模式: {default_char} - {default_emotion}"
            )

    @filter.command("ttg-q", alias={"关闭群语音"})
    async def stop_group_tts(self, event: AstrMessageEvent):
        """关闭当前群组的语音合成"""
        await self._observe_session(event, persist=False)
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        # 必须同时记录"已手动关闭"并移出"已手动开启"：
        # 否则白名单群 /ttg-q 之后一重启就会被白名单初始化重新打开。
        self.inactive_groups.add(group_id)
        self.active_groups.discard(group_id)
        await self._persist_state()
        logger.info(f"群组 [{group_id}] 的 LLM TTS 功能已关闭。")
        yield event.plain_result("⏹️ 已关闭本群的群级自动配音。会话通过 /tts-llm 或 /tts-w 单独开启的仍有效。")

    @filter.command("tts-w", alias={"开启自动情感识别"})
    async def start_tts_w(self, event: AstrMessageEvent):
        await self._observe_session(event, persist=False)
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.w_active_sessions.add(session_id)
        if not self._normalize_group_id(event.message_obj.group_id):
            self.inactive_private_sessions.discard(session_id)
        self.active_sessions.discard(session_id)
        await self._persist_state()
        default_char = self.config.get("default_character")
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的自动情感识别语音合成已开启。\n将使用默认角色: {default_char}"
        )

    @filter.command("tts-w-q", alias={"关闭自动情感识别"})
    async def stop_tts_w(self, event: AstrMessageEvent):
        await self._observe_session(event, persist=False)
        session_id = event.unified_msg_origin
        self.w_active_sessions.discard(session_id)
        if not self._normalize_group_id(event.message_obj.group_id) and self.config.get("enable_private_tts_by_default", True):
            self.inactive_private_sessions.add(session_id)
        await self._persist_state()
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本对话的自动情感识别语音合成已关闭。")

    @filter.command("sw", alias={"切换感情"})
    async def switch_emotion(
        self, event: AstrMessageEvent, character_name: str, emotion_name: str
    ):
        await self._observe_session(event, persist=False)
        if self.emotion_manager.get_emotion_data(character_name, emotion_name):
            self.session_emotions[event.unified_msg_origin] = {
                "character": character_name,
                "emotion": emotion_name,
            }
            await self._persist_state()
            logger.info(
                f"会话 [{event.unified_msg_origin}] 切换感情至: {character_name} - {emotion_name}"
            )
            yield event.plain_result(
                f"本会话感情已切换为: {character_name} - {emotion_name}"
            )
        else:
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}' 的感情 '{emotion_name}'。"
            )

    @filter.command("sw-w", alias={"切换w角色"})
    async def switch_w_character(self, event: AstrMessageEvent, character_name: str):
        await self._observe_session(event, persist=False)
        if self.emotion_manager.character_exists(character_name):
            self.session_w_settings[event.unified_msg_origin] = {
                "character": character_name
            }
            await self._persist_state()
            logger.info(
                f"会话 [{event.unified_msg_origin}] 切换自动情感识别角色至: {character_name}"
            )
            yield event.plain_result(
                f"本会话自动情感识别角色已切换为: {character_name}"
            )
        else:
            yield event.plain_result(f"❌ 未找到角色 '{character_name}'。")

    @filter.command("tts-status", alias={"语音状态"})
    async def tts_status(self, event: AstrMessageEvent):
        """查看本会话/本群的语音开关、当前音色、队列与 TTS 服务器状态"""
        await self._observe_session(event, persist=False)
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)

        if session_id in self.w_active_sessions:
            session_label = "✅ 自动情感识别模式（/tts-w）"
        elif session_id in self.active_sessions:
            session_label = "✅ 固定情感模式（/tts-llm）"
        else:
            session_label = "⏹️ 未开启"

        effective = session_state.effective_state(self, session_id, group_id)
        lines = ["🎙️ Genie TTS 状态", f"• 会话单独设置: {session_label}",
                 "• 实际自动配音: " + ("✅ 已开启" if effective["enabled"] else "⏹️ 未开启"),
                 "• 生效来源: " + " / ".join(effective["sources"])]

        if group_id:
            if self._is_group_blacklisted(group_id):
                group_state = "⛔ 已被群黑名单禁用"
            elif self._is_group_tts_active(group_id):
                group_state = "✅ 已开启（全员生效）"
            else:
                group_state = "⏹️ 未开启"
            lines.append(f"• 本群 [{group_id}]: {group_state}")

        char_name, emotion_name, emotion_data = self._resolve_tts_profile(session_id)
        if emotion_data:
            lines.append(f"• 当前音色: {char_name} - {emotion_name}")
        else:
            lines.append(
                "• 当前音色: ❌ 不可用（角色 "
                + repr(char_name or "未设置")
                + " / 情感 "
                + repr(emotion_name or "未设置")
                + "），请用 /查看感情 检查注册情况"
            )

        trigger_mode = str(self.config.get("tts_trigger_mode", "always"))
        lines.append(f"• 触发模式: {trigger_mode}")
        lines.append(f"• 自动回复输出: {self._get_auto_tts_output_mode()}")
        lines.append(f"• 主动语音输出: {self._get_llm_tool_tts_output_mode()}")

        stats = self.tts_engine.stats
        lines.append(
            "• 合成统计: 成功 %d / 失败 %d / 无朗读内容跳过 %d / "
            "泄漏拦截 %d / 截断拦截 %d"
            % (
                stats.get("succeeded", 0),
                stats.get("failed", 0),
                stats.get("skipped_no_speech", 0),
                stats.get("leak_guard_hits", 0),
                stats.get("truncation_guard_hits", 0),
            )
        )
        text_truncated = stats.get("text_truncated", 0)
        max_text_length = self.config.get(
            "tts_max_text_length", DEFAULT_MAX_TEXT_LENGTH
        )
        truncated_note = (
            "（超长部分未被朗读，调大「语音文本长度上限」可避免）"
            if text_truncated
            else ""
        )
        lines.append(
            "• 文本超长截断: %s 次 / 上限 %s 字%s"
            % (text_truncated, max_text_length, truncated_note)
        )
        empty_retries = stats.get("empty_result_retries", 0)
        if empty_retries:
            lines.append(
                "• 后端空结果重试: %d 次（整段合成失败已自动重抽，"
                "频繁出现请更新 Space 端 app.py）" % empty_retries
            )
        lines.append(f"• 排队中的合成请求: {self.tts_engine.queue_size()}")

        ring_count = self.sent_voice_count(session_id)
        lines.append(
            f"• 可重发的历史语音: {ring_count} 条"
            + ("（引用其中任意一条即可 /语音收藏）" if ring_count else "（先合成一句）")
        )
        if self._favorites_enabled():
            try:
                self._sync_vault_config()
                await self.voice_vault.load()
                lines.append("• 语音收藏: " + self._favorite_stats_line())
            except Exception as exc:
                lines.append(f"• 语音收藏: ⚠ 读取失败（{exc}）")
        else:
            lines.append("• 语音收藏: ⏹️ 已关闭（enable_voice_favorites）")

        yield event.plain_result("\n".join(lines) + "\n\n🛰️ 正在探测 TTS 服务器…")

        probes = await self.tts_engine.probe_servers()
        if not probes:
            yield event.plain_result(
                "❌ 没有配置任何 TTS 服务器地址，请在插件配置的 tts_servers 里填写。"
            )
            return

        probe_lines = ["🛰️ TTS 服务器"]
        has_failure = False
        for index, probe in enumerate(probes, start=1):
            url = probe.get("url", "")
            latency = probe.get("latency") or 0.0
            if probe.get("ok"):
                characters = list(probe.get("characters") or [])
                busy_flag = "（正在合成中）" if probe.get("busy") else ""
                preview = "、".join(characters[:6]) or "无"
                if len(characters) > 6:
                    preview += " …"
                probe_lines.append(
                    f"{index}. ✅ {url}{busy_flag}\n"
                    f"   延迟 {latency:.2f}s | 可用角色 {len(characters)} 个: {preview}"
                )
            else:
                has_failure = True
                probe_lines.append(
                    f"{index}. ❌ {url}\n   {probe.get('error') or '连接失败'}"
                )

        if has_failure:
            probe_lines.append(
                "提示: HuggingFace Space 休眠后首次唤醒需要 1~3 分钟，"
                "可稍后重试，或在配置里开启 enable_space_keepalive 保活。"
            )

        yield event.plain_result("\n".join(probe_lines))

    @filter.command("tts-again", alias={"重发语音"})
    async def tts_again(self, event: AstrMessageEvent):
        """重新发送本会话最近一条合成成功的语音，不重复消耗 TTS 算力"""
        session_id = event.unified_msg_origin
        if self._is_group_blacklisted(event.message_obj.group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        latest = self._latest_sent_voice(session_id)
        if latest is None:
            yield event.plain_result(
                "本会话没有可重发的语音。\n"
                "• 还没合成过：先用 /合成 或开启 /tts-llm 让我说一句；\n"
                "• 合成过但隔了一段时间：临时音频默认只保留 30 分钟，请重新合成；\n"
                "• 想长期留住某条语音：引用它发 /语音收藏。"
            )
            return

        yield event.chain_result([Comp.Record(file=str(latest.get("path") or ""))])
        event.stop_event()

    @filter.command("语音收藏", alias={"tts-fav", "收藏语音"})
    async def favorite_voice_command(self, event: AstrMessageEvent, alias: str = ""):
        """引用我发过的那条语音再发这条指令，把它原封不动收进收藏夹"""
        if self._is_group_blacklisted(event.message_obj.group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw_tail = self._strip_command_prefix(
            event.get_message_str(), ("语音收藏", "收藏语音", "tts-fav")
        )
        name = voice_vault.clean_alias(raw_tail or alias or "")

        resolved = await self._resolve_quoted_voice(event)
        if not resolved.get("ok"):
            yield event.plain_result("❌ " + str(resolved.get("error") or "没找到语音。"))
            return

        self._sync_vault_config()
        try:
            result = await self.voice_vault.add(
                resolved.get("path") or "",
                alias=name,
                character=str(resolved.get("character") or ""),
                emotion=str(resolved.get("emotion") or ""),
                text=str(resolved.get("text") or ""),
                session_id=event.unified_msg_origin,
                source=str(resolved.get("source") or "plugin"),
            )
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 收藏语音失败: {exc}")
            yield event.plain_result(f"❌ 收藏失败: {exc}")
            return

        entry = dict(result.get("entry") or {})
        entry["index"] = self._favorite_index(str(entry.get("id") or ""))
        lines = [
            "⭐ 这条早就收藏过了，已把新信息补上" if result.get("duplicate") else "⭐ 已收藏",
            self._format_favorite_row(entry),
            self._favorite_stats_line(),
        ]
        note = str(resolved.get("note") or "").strip()
        if note:
            lines.append("来源: " + note)
        if resolved.get("lossy"):
            lines.append(
                "⚠ 这份是从协议端捞回来的副本，音质已经被压过一次。"
                "想要无损，请在我发出语音后 30 分钟内引用收藏。"
            )
        dropped = result.get("dropped") or []
        if dropped:
            lines.append(
                f"⚠ 超出容量，淘汰了最旧的 {len(dropped)} 条（置顶的不会被淘汰）。"
            )
        handle = str(entry.get("alias") or "") or str(entry.get("index") or "")
        lines.append(f"听: /发收藏 {handle} · 存文件: /收藏文件 {handle}")
        yield event.plain_result("\n".join(lines))

    @filter.command("收藏列表", alias={"tts-favs", "语音收藏列表"})
    async def list_favorites_command(self, event: AstrMessageEvent, token: str = ""):
        """列出收藏夹。参数是纯数字当页码，否则当关键词筛选"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = self._strip_command_prefix(
            event.get_message_str(), ("收藏列表", "语音收藏列表", "tts-favs")
        ) or (token or "")
        raw = re.sub(r"\s+", " ", raw).strip()

        page = 1
        keyword = ""
        if raw.isdigit():
            page = max(int(raw), 1)
        elif raw:
            head, _, tail = raw.rpartition(" ")
            if head and tail.isdigit():
                keyword = head.strip()
                page = max(int(tail), 1)
            else:
                keyword = raw

        self._sync_vault_config()
        try:
            await self.voice_vault.load()
        except Exception as exc:
            logger.error(f"Genie TTS: 读取收藏库失败: {exc}")
            yield event.plain_result(f"❌ 读取收藏库失败: {exc}")
            return

        rows = self.voice_vault.search(keyword=keyword)
        if not rows:
            if keyword:
                yield event.plain_result(
                    f"没有匹配「{keyword}」的收藏（全库共 {self.voice_vault.count()} 条）。"
                )
            else:
                yield event.plain_result(
                    "收藏夹还是空的。\n"
                    "用法：引用我发的任意一条语音，发 /语音收藏 [名字]。\n"
                    "也可以在 WebUI「语音合成工作台」的「语音收藏」页里管理。"
                )
            return

        size = self.FAVORITE_PAGE_SIZE
        pages = max((len(rows) + size - 1) // size, 1)
        page = min(page, pages)
        chunk = rows[(page - 1) * size : page * size]

        title = "⭐ 语音收藏"
        if keyword:
            title += f"（关键词「{keyword}」命中 {len(rows)} 条）"
        lines = [f"{title} 第 {page}/{pages} 页"]
        lines.extend(self._format_favorite_row(row) for row in chunk)
        lines.append(self._favorite_stats_line())
        if pages > 1:
            nav = f"翻页: /收藏列表 {min(page + 1, pages)}"
            if keyword:
                nav = f"翻页: /收藏列表 {keyword} {min(page + 1, pages)}"
            lines.append(nav)
        lines.append("听: /发收藏 序号 · 存文件: /收藏文件 序号 · 全部指令: /语音帮助")
        yield event.plain_result("\n".join(lines))

    @filter.command("发收藏", alias={"tts-fav-send", "播放收藏"})
    async def send_favorite_command(self, event: AstrMessageEvent, token: str = ""):
        """把收藏夹里的某条语音直接发出来听"""
        if self._is_group_blacklisted(event.message_obj.group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("发收藏", "播放收藏", "tts-fav-send")
            )
            or token
            or ""
        ).strip()
        if not raw:
            yield event.plain_result(
                "用法: /发收藏 序号或名字\n先用 /收藏列表 看序号。"
            )
            return

        entry, err = await self._pick_favorite(raw)
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        path = self.voice_vault.audio_path(entry)
        if not path.is_file():
            yield event.plain_result(
                "❌ 这条收藏的音频文件不见了（可能被手动删过）。\n"
                f"用 /删收藏 {entry.get('index') or entry.get('id')} 清掉这条记录。"
            )
            return

        await self.voice_vault.touch(str(entry.get("id") or ""))
        # 记进已发环，这样刚发出来的收藏也能被引用去转文件 / 再收藏。
        self._remember_last_audio(
            event.unified_msg_origin,
            str(path),
            text=str(entry.get("text") or ""),
            character=str(entry.get("character") or ""),
            emotion=str(entry.get("emotion") or ""),
        )
        yield event.chain_result([Comp.Record(file=str(path))])
        event.stop_event()

    @filter.command("收藏文件", alias={"tts-fav-file", "收藏转文件"})
    async def favorite_file_command(self, event: AstrMessageEvent, token: str = ""):
        """把收藏夹里的某条语音当文件发出来，方便存到手机或电脑"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏文件", "收藏转文件", "tts-fav-file")
            )
            or token
            or ""
        ).strip()
        if not raw:
            yield event.plain_result("用法: /收藏文件 序号或名字（/收藏列表 看序号）")
            return

        entry, err = await self._pick_favorite(raw)
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        path = self.voice_vault.audio_path(entry)
        if not path.is_file():
            yield event.plain_result("❌ 这条收藏的音频文件不见了，用 /删收藏 清掉记录。")
            return

        filename = self._voice_file_name(
            alias=str(entry.get("alias") or ""),
            text=str(entry.get("text") or ""),
            character=str(entry.get("character") or ""),
            emotion=str(entry.get("emotion") or ""),
            suffix=str(entry.get("suffix") or ".wav"),
        )
        ok, channel = await self._send_voice_as_file(event, str(path), filename)
        if not ok:
            yield event.plain_result(
                f"❌ 文件没发出去（{channel}）。\n"
                "可以去 WebUI「语音合成工作台」的「语音收藏」页直接下载。"
            )
            return
        yield event.plain_result(
            f"📎 已发送 {filename}（{channel}）\n"
            f"原始格式 {str(entry.get('suffix') or '.wav')}，字节级原样，未再压缩。"
        )

    @filter.command("语音转文件", alias={"tts-to-file", "语音存文件"})
    async def voice_to_file_command(self, event: AstrMessageEvent):
        """引用一条语音（不引用则取本会话最近一条），把它当文件发回来"""
        resolved = await self._resolve_quoted_voice(event)
        if not resolved.get("ok"):
            yield event.plain_result("❌ " + str(resolved.get("error") or "没找到语音。"))
            return

        path = str(resolved.get("path") or "")
        suffix = os.path.splitext(path)[1] or ".wav"
        filename = self._voice_file_name(
            text=str(resolved.get("text") or ""),
            character=str(resolved.get("character") or ""),
            emotion=str(resolved.get("emotion") or ""),
            suffix=suffix,
        )
        ok, channel = await self._send_voice_as_file(event, path, filename)
        if not ok:
            yield event.plain_result(
                f"❌ 文件没发出去（{channel}）。\n"
                "常见原因：协议端不允许上传文件，或 AstrBot 与协议端不在同一台机器"
                "且没配 callback_api_base。"
            )
            return

        lines = [f"📎 已发送 {filename}（{channel}）"]
        if resolved.get("lossy"):
            lines.append("⚠ 这份是协议端的转码副本，音质有损；30 分钟内引用可拿到原始 WAV。")
        else:
            lines.append("这是我合成时的原始 WAV，没有二次压缩。")
        lines.append("想长期留住它: 引用同一条语音发 /语音收藏 [名字]")
        yield event.plain_result("\n".join(lines))

    @filter.command("删收藏", alias={"tts-fav-del", "删除收藏"})
    async def delete_favorite_command(self, event: AstrMessageEvent, token: str = ""):
        """从收藏夹里删掉一条"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("删收藏", "删除收藏", "tts-fav-del")
            )
            or token
            or ""
        ).strip()
        if not raw:
            yield event.plain_result("用法: /删收藏 序号或名字（/收藏列表 看序号）")
            return

        entry, err = await self._pick_favorite(raw)
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        try:
            removed = await self.voice_vault.remove(str(entry.get("id") or ""))
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 删除收藏失败: {exc}")
            yield event.plain_result(f"❌ 删除失败: {exc}")
            return
        label = str(removed.get("alias") or "") or ("#" + str(removed.get("id") or "")[:6])
        yield event.plain_result(
            f"🗑 已删除「{label}」。\n{self._favorite_stats_line()}"
        )

    @filter.command("收藏改名", alias={"tts-fav-rename"})
    async def rename_favorite_command(self, event: AstrMessageEvent, token: str = ""):
        """给收藏改个好记的名字：/收藏改名 序号 新名字"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏改名", "tts-fav-rename")
            )
            or token
            or ""
        ).strip()
        parts = re.split(r"\s+", raw, maxsplit=1)
        if len(parts) < 2 or not parts[0]:
            yield event.plain_result(
                "用法: /收藏改名 序号或旧名 新名字\n名字留空可以清掉名字，改回按序号引用。"
            )
            return

        entry, err = await self._pick_favorite(parts[0])
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        try:
            updated = await self.voice_vault.rename(
                str(entry.get("id") or ""), parts[1]
            )
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 收藏改名失败: {exc}")
            yield event.plain_result(f"❌ 改名失败: {exc}")
            return
        updated = dict(updated)
        updated["index"] = self._favorite_index(str(updated.get("id") or ""))
        yield event.plain_result("✏️ 已改名\n" + self._format_favorite_row(updated))

    @filter.command("收藏备注", alias={"tts-fav-note"})
    async def note_favorite_command(self, event: AstrMessageEvent, token: str = ""):
        """给收藏补一段正文/备注，方便用关键词搜到：/收藏备注 序号 文本"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏备注", "tts-fav-note")
            )
            or token
            or ""
        ).strip()
        parts = re.split(r"\s+", raw, maxsplit=1)
        if not parts or not parts[0]:
            yield event.plain_result(
                "用法: /收藏备注 序号或名字 备注文本\n备注留空则清掉原有备注。"
            )
            return

        entry, err = await self._pick_favorite(parts[0])
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        body = parts[1] if len(parts) > 1 else ""
        try:
            updated = await self.voice_vault.update(
                str(entry.get("id") or ""), text=body
            )
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 收藏备注失败: {exc}")
            yield event.plain_result(f"❌ 备注失败: {exc}")
            return
        updated = dict(updated)
        updated["index"] = self._favorite_index(str(updated.get("id") or ""))
        yield event.plain_result("📝 备注已更新\n" + self._format_favorite_row(updated))

    @filter.command("收藏置顶", alias={"tts-fav-pin"})
    async def pin_favorite_command(self, event: AstrMessageEvent, token: str = ""):
        """置顶/取消置顶一条收藏。置顶的永远排在前面，也永远不会被容量淘汰"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏置顶", "tts-fav-pin")
            )
            or token
            or ""
        ).strip()
        if not raw:
            yield event.plain_result("用法: /收藏置顶 序号或名字（再发一次取消置顶）")
            return

        entry, err = await self._pick_favorite(raw)
        if entry is None:
            yield event.plain_result(f"❌ {err}")
            return
        want = not bool(entry.get("pinned"))
        try:
            updated = await self.voice_vault.update(
                str(entry.get("id") or ""), pinned=want
            )
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 收藏置顶失败: {exc}")
            yield event.plain_result(f"❌ 操作失败: {exc}")
            return
        updated = dict(updated)
        updated["index"] = self._favorite_index(str(updated.get("id") or ""))
        head = "📌 已置顶" if want else "已取消置顶"
        yield event.plain_result(head + "\n" + self._format_favorite_row(updated))

    @filter.command("收藏导出", alias={"tts-fav-export", "导出收藏"})
    async def export_favorites_command(self, event: AstrMessageEvent, token: str = ""):
        """把收藏打包成 .zip 发出来；给序号则只导出指定几条"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏导出", "导出收藏", "tts-fav-export")
            )
            or token
            or ""
        ).strip()

        self._sync_vault_config()
        try:
            await self.voice_vault.load()
        except Exception as exc:
            logger.error(f"Genie TTS: 读取收藏库失败: {exc}")
            yield event.plain_result(f"❌ 读取收藏库失败: {exc}")
            return
        if self.voice_vault.count() <= 0:
            yield event.plain_result("收藏夹还是空的，没什么可导出的。")
            return

        ids: Optional[List[str]] = None
        wanted = [item for item in re.split(r"[,，\s]+", raw) if item]
        if wanted:
            picked: List[str] = []
            for item in wanted:
                entry, err = self.voice_vault.resolve(item)
                if entry is None:
                    yield event.plain_result(f"❌ {err}")
                    return
                entry_id = str(entry.get("id") or "")
                if entry_id and entry_id not in picked:
                    picked.append(entry_id)
            ids = picked

        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = self._vault_bundle_dir() / f"{self.VAULT_BUNDLE_PREFIX}-{stamp}.zip"
        try:
            result = await self.voice_vault.export_bundle(dest, ids)
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 导出收藏失败: {exc}")
            yield event.plain_result(f"❌ 导出失败: {exc}")
            return

        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        yield event.plain_result(
            "📦 收藏包已导出\n"
            f"内容: {result.get('count')} 条（{audio_compat.format_size(size)}）\n"
            f"服务端快照: {dest.name}\n"
            "恢复: 把这个 .zip 连同 /收藏导入 一起发回来，"
            "或在工作台「语音收藏」页导入。"
        )

        ok, channel = await self._send_voice_as_file(event, str(dest), dest.name)
        if not ok:
            yield event.plain_result(
                f"⚠ 压缩包没发出去（{channel}）。"
                "文件已存在服务端，可去 WebUI 工作台「语音收藏」页下载。"
            )

    @filter.command("收藏导入", alias={"tts-fav-import", "导入收藏"})
    async def import_favorites_command(self, event: AstrMessageEvent, mode: str = ""):
        """导入收藏包 .zip：上传附件，或引用一条带附件的消息"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏导入", "导入收藏", "tts-fav-import")
            )
            or ""
        ).strip()
        chosen = ""
        for item in [x for x in re.split(r"\s+", raw) if x]:
            if item.lower() in self.VAULT_MODE_TOKENS or item in self.VAULT_MODE_TOKENS:
                chosen = item
                break
        if not chosen:
            hint = (mode or "").strip()
            if hint.lower() in self.VAULT_MODE_TOKENS or hint in self.VAULT_MODE_TOKENS:
                chosen = hint

        path, origin = await self._bundle_path_from_event(event)
        if not path:
            yield event.plain_result(
                "❌ 没找到收藏包文件。两种用法：\n"
                "1) 上传 /收藏导出 给的 .zip，在同一条消息里写 /收藏导入\n"
                "2) 引用那条带 .zip 的消息，再发 /收藏导入\n"
                "模式：merge 只补新（默认）/ overwrite 冲突覆盖 / replace 先清空\n"
                "也可以在 WebUI 工作台「语音收藏」页直接拖文件导入。"
            )
            return

        self._sync_vault_config()
        resolved_mode = voice_vault.normalize_import_mode(chosen)
        try:
            report = await self.voice_vault.import_bundle(path, resolved_mode)
        except VoiceVaultError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error(f"Genie TTS: 导入收藏失败: {exc}")
            yield event.plain_result(f"❌ 导入失败: {exc}")
            return

        yield event.plain_result(
            "📥 收藏包导入完成\n"
            f"来源: {origin} · 模式: {resolved_mode}\n"
            + voice_vault.describe_import(report)
            + "\n"
            + self._favorite_stats_line()
        )

    @filter.command("收藏清空", alias={"tts-fav-clear", "清空收藏"})
    async def clear_favorites_command(self, event: AstrMessageEvent, token: str = ""):
        """清空收藏夹。必须显式加「确认」，默认保留置顶"""
        if not self._favorites_enabled():
            yield event.plain_result(self._favorites_disabled_hint())
            return

        raw = (
            self._strip_command_prefix(
                event.get_message_str(), ("收藏清空", "清空收藏", "tts-fav-clear")
            )
            or token
            or ""
        ).strip()
        tokens = [x for x in re.split(r"\s+", raw) if x]
        confirmed = any(
            x.lower() in self.VAULT_CONFIRM_TOKENS or x in self.VAULT_CONFIRM_TOKENS
            for x in tokens
        )
        include_pinned = any(
            x.lower() in self.VAULT_PINNED_TOKENS or x in self.VAULT_PINNED_TOKENS
            for x in tokens
        )

        self._sync_vault_config()
        try:
            await self.voice_vault.load()
        except Exception as exc:
            logger.error(f"Genie TTS: 读取收藏库失败: {exc}")
            yield event.plain_result(f"❌ 读取收藏库失败: {exc}")
            return

        stats = self.voice_vault.stats()
        total = int(stats.get("count") or 0)
        pinned = int(stats.get("pinned") or 0)
        if total <= 0:
            yield event.plain_result("收藏夹已经是空的了。")
            return

        if not confirmed:
            target = total if include_pinned else total - pinned
            yield event.plain_result(
                "⚠ 这一步会真删音频文件，不可撤销。\n"
                f"将删除 {target} 条"
                + ("（含置顶）" if include_pinned else f"，保留 {pinned} 条置顶")
                + "。\n确认请发: /收藏清空 确认"
                + ("含置顶" if include_pinned else "")
                + "\n只想删一条: /删收藏 序号"
            )
            return

        try:
            removed = await self.voice_vault.clear(keep_pinned=not include_pinned)
        except Exception as exc:
            logger.error(f"Genie TTS: 清空收藏失败: {exc}")
            yield event.plain_result(f"❌ 清空失败: {exc}")
            return
        yield event.plain_result(
            f"🧹 已清空 {removed} 条收藏。\n{self._favorite_stats_line()}"
        )

    @filter.command("tts-help", alias={"语音帮助"})
    async def tts_help(self, event: AstrMessageEvent):
        """列出本插件的全部指令"""
        yield event.plain_result(
            "🎙️ Genie TTS 指令一览\n"
            "【开关】\n"
            "• /tts-llm（开启语音合成）本会话开启固定情感模式\n"
            "• /tts-q（关闭语音合成）本会话关闭全部语音\n"
            "• /tts-w（开启自动情感识别）由 AI 决定情感\n"
            "• /tts-w-q（关闭自动情感识别）\n"
            "• /ttg（开启群语音）本群全员生效\n"
            "• /ttg-q（关闭群语音）\n"
            "【音色】\n"
            "• /sw 角色 感情（切换感情）\n"
            "• /sw-w 角色（切换w角色）\n"
            "• /查看感情 [角色] 列出已注册角色与感情\n"
            "• /注册感情 角色 感情 参考音频相对路径 参考文本 [语言]\n"
            "• /删除感情 角色 感情\n"
            "【合成】\n"
            "• /合成 角色 感情 文本 手动合成一条语音\n"
            "• /tts-again（重发语音）重发本会话最近一条语音\n"
            "【语音收藏】引用我发的语音再发指令\n"
            "• /语音收藏 [名字]（tts-fav）原样收进收藏夹，音质无损\n"
            "• /收藏列表 [页码|关键词]（tts-favs）看收藏与序号\n"
            "• /发收藏 序号或名字（播放收藏）直接发出来听\n"
            "• /收藏文件 序号或名字 把收藏当文件发来存档\n"
            "• /语音转文件（语音存文件）引用一条语音直接转文件\n"
            "• /收藏改名 序号 新名 · /收藏备注 序号 文本 · /收藏置顶 序号\n"
            "• /删收藏 序号 · /收藏清空 确认 [含置顶]\n"
            "• /收藏导出 [序号…] · /收藏导入 [模式]（.zip 整包搬家）\n"
            "【感情包】\n"
            "• /感情导出 [角色] 导出感情包 JSON（省略角色=全部）\n"
            "• /感情导入 [模式] [试运行] 附 JSON / 引用消息 / 上传文件\n"
            "• /感情包 列出服务端快照\n"
            "• /感情包保存 [文件名] 存一份当前感情库\n"
            "• /感情包恢复 文件名 [模式] [试运行]\n"
            "  模式：merge 只补新（默认）/ overwrite 冲突覆盖 / replace 先清空\n"
            "【诊断】\n"
            "• /tts-status（语音状态）开关、音色、队列与服务器状态\n"
            "• /tts-help（语音帮助）显示本帮助\n"
            "开关与音色选择会自动持久化，重启 AstrBot 后不会丢。\n"
            "更省事的做法：在 AstrBot WebUI 的插件页打开「语音合成工作台」，"
            "可视化管理感情、试听分段、收藏语音、导入导出感情包与收藏包。"
        )

    @filter.command("tts-role", alias={"语音角色"})
    async def session_character_command(
        self, event: AstrMessageEvent, character_name: str = ""
    ):
        """查看、设置或清除当前会话绑定的 Genie 角色。"""
        session_id = event.unified_msg_origin
        character_name = str(character_name or "").strip()
        default_character = str(self.config.get("default_character") or "").strip()

        if not character_name:
            bound_character = self.session_character_bindings.get(session_id)
            if bound_character:
                yield event.plain_result(
                    f"当前会话语音角色: {bound_character}（会话绑定）"
                )
            else:
                yield event.plain_result(
                    f"当前会话未单独绑定语音角色，将使用默认角色: {default_character}"
                )
            return

        if character_name.lower() in {"default", "reset"} or character_name in {
            "默认",
            "清除",
            "重置",
        }:
            self.session_character_bindings.clear(session_id)
            logger.info(f"会话 [{session_id}] 已清除语音角色绑定。")
            yield event.plain_result(
                f"已清除本会话语音角色绑定，将使用默认角色: {default_character}"
            )
            return

        if not self.emotion_manager.character_exists(character_name):
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}'。请先在 emotions.json 中注册该角色。"
            )
            return

        self.session_character_bindings.set(session_id, character_name)
        default_emotion = self._get_default_emotion_for_character(character_name)
        logger.info(
            f"会话 [{session_id}] 已绑定 Genie 语音角色: {character_name}"
        )
        yield event.plain_result(
            f"本会话语音角色已绑定为: {character_name}。"
            f"默认兜底情感: {default_emotion or '未配置'}。AstrBot 重启后仍会保留。"
        )

    @filter.llm_tool(name="genie_tts_speak")
    async def llm_tool_genie_tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        character_name: Optional[str] = None,
        emotion_name: Optional[str] = None,
        **_extra_tool_metadata,
    ) -> str:
        """在当前会话中直接发送一条 TTS 语音。

        仅当用户明确要求“说一句”“发语音”“让我听听声音”“念给我听”时使用。
        普通闲聊不要调用这个工具；日常语音仍由插件自己的自动触发模式控制。

        Args:
            text(string): 要合成为语音并直接发给用户的文本。必须是完整句子，句末要有标点；如果使用主LLM注入翻译模式，请传入目标语言文本。
            character_name(string): 可选。要使用的角色名；仅在明确知道已注册角色时填写，否则留空沿用当前会话或默认角色。
            emotion_name(string): 可选但建议填写。要使用的情感名；请从当前角色已注册情感中选择，让语音匹配要朗读内容的语气。
        """
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        tool_emotion_arg = (emotion_name or "").strip()
        text = text.strip()

        trace = self.run_log.begin_synth(
            "tool",
            session=session_id,
            group=group_id or "",
            llm_text=text,
            character=(character_name or "").strip(),
            emotion=tool_emotion_arg,
            workflow=self._get_translation_workflow(),
        )

        if self._is_group_blacklisted(group_id):
            trace.skip("群组黑名单")
            return "当前群组已禁用语音功能，不能直接发送 TTS 语音。"
        if not text:
            trace.skip("文本为空")
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"

        display_text, tagged_emotion, tagged_translation = self._extract_tool_text_directives(text)
        if not display_text:
            trace.skip("剥离标签后文本为空")
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"
        if not emotion_name and tagged_emotion:
            emotion_name = tagged_emotion

        if tool_emotion_arg:
            emotion_source = "LLM工具指定"
        elif tagged_emotion:
            emotion_source = "LLM情感标签"
        else:
            emotion_source = "默认情感"
        trace.set(
            display_text=display_text,
            emotion=(emotion_name or "").strip(),
            emotion_source=emotion_source,
        )

        char_name, resolved_emotion, emotion_data = self._resolve_tts_profile(
            session_id, character_name, emotion_name
        )
        trace.set(character=char_name or "", emotion=resolved_emotion or "")
        if not char_name or not self.emotion_manager.character_exists(char_name):
            trace.fail("角色无效")
            return "没有找到可用角色，请先检查默认角色或角色注册情况。"
        if not emotion_data or not resolved_emotion:
            trace.fail("感情无效")
            if emotion_name:
                return (
                    f"角色 '{char_name}' 下未找到情感 '{emotion_name}'。"
                    "请改用已注册的情感名，或留空让插件自动选择。"
                )
            return f"角色 '{char_name}' 目前没有可用的情感配置。"

        trace.set(
            ref_audio=emotion_data.get("ref_audio_path"),
            ref_text=emotion_data.get("ref_audio_text"),
            language=emotion_data.get("language"),
        )
        translation_enabled = self.config.get("enable_translation", True)
        translation_workflow = self._get_translation_workflow()

        if translation_enabled and translation_workflow == "provider_translation":
            provider_auto = should_provider_autofill_emotion(
                enabled=bool(self.config.get("llm_injection_settings", {}).get("enable_provider_emotion_autofill", False)),
                translation_workflow=translation_workflow,
                has_manual_emotion=bool(emotion_name) or bool(self.session_emotions.get(session_id)),
                has_injected_emotion=bool(tagged_emotion),
                w_mode_active=session_id in self.w_active_sessions,
            )
            target_text = None
            if provider_auto:
                target_text, guessed_emotion = await self._translate_text_and_pick_emotion_with_backends(
                    display_text, list(self.emotion_manager.emotions_data.get(char_name, {}).keys())
                )
                if guessed_emotion:
                    guessed_data = self.emotion_manager.get_emotion_data(char_name, guessed_emotion)
                    if guessed_data:
                        resolved_emotion, emotion_data = guessed_emotion, guessed_data
                        trace.set(emotion=resolved_emotion, emotion_source="Provider自动补判情感")
            if not target_text:
                target_text = await self._translate_text_with_backends(display_text)
        elif tagged_translation:
            target_text = tagged_translation
        else:
            target_text = display_text
        self._log_translation_result(session_id, display_text, target_text)

        if not target_text:
            trace.fail("译文准备失败")
            return (
                "语音发送失败：用于 TTS 的文本准备失败了，请检查翻译配置或日志。"
                + self.TOOL_INTERNAL_FAILURE_TAIL
            )
        if target_text != display_text:
            trace.set(translated_text=target_text, translated=True)

        output_mode = self._get_llm_tool_tts_output_mode()
        tts_text, plain_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        trace.set(output_mode=output_mode)
        if not tts_text:
            trace.skip("无可朗读文本")
            return "语音发送失败：没有可用于朗读的文本。"

        tts_target_text = target_text
        if output_mode == "split_audio_text":
            if translation_enabled and translation_workflow == "provider_translation":
                tts_target_text = await self._translate_text_with_backends(tts_text)
                self._log_translation_result(session_id, tts_text, tts_target_text)
            elif tagged_translation:
                translated_audio_text, _, _ = self._prepare_tts_output_segments(
                    tagged_translation, output_mode
                )
                tts_target_text = translated_audio_text or tagged_translation
            else:
                tts_target_text = tts_text

            if not tts_target_text:
                trace.fail("混合模式译文准备失败")
                return (
                    "语音发送失败：混合模式下用于 TTS 的文本准备失败了，"
                    "请检查翻译配置或日志。" + self.TOOL_INTERNAL_FAILURE_TAIL
                )

        if not has_pronounceable(tts_target_text):
            trace.skip("无可朗读字符", tts_text=tts_target_text)
            return (
                "语音发送失败：这段文本里没有任何可以朗读的字（只有标点或表情符号）。"
                "请给出包含实际文字的内容再调用一次。"
            )

        trace.set(tts_text=tts_target_text)
        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=tts_target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )
        if not audio_path:
            trace.fail("TTS合成失败")
            return (
                "语音发送失败：TTS 合成没有成功，请检查服务状态或日志。"
                + self.TOOL_INTERNAL_FAILURE_TAIL
            )
        self._remember_last_audio(
            session_id,
            audio_path,
            text=display_text,
            character=char_name,
            emotion=resolved_emotion,
        )

        ok, error_message = await self._dispatch_llm_tool_tts_output(
            session_id=session_id,
            audio_path=audio_path,
            full_display_text=display_text,
            plain_display_text=plain_text,
            output_mode=output_mode,
        )
        if not ok:
            trace.fail("主动发送失败", audio_path=audio_path)
            return (
                (error_message or "语音已经合成成功，但 AstrBot 主动发送失败了。")
                + "请确认当前会话对应的平台实例仍然在线。"
                + self.TOOL_INTERNAL_FAILURE_TAIL
            )

        trace.ok(audio_path=audio_path)
        self.skip_next_auto_tts_sessions.add(session_id)
        logger.info(
            f"[{session_id}] LLM 工具已主动发送 TTS 语音: {char_name} - {resolved_emotion}"
        )
        return (
            "语音已发送到当前会话。请不要逐字重复刚才朗读的整段内容，"
            "只需简短确认已经发出，或继续正常对话。"
        )

    def _pick_available_emotion_name(
        self, character_name: str, preferred_emotion: Optional[str] = None
    ) -> Optional[str]:
        if preferred_emotion and self.emotion_manager.get_emotion_data(
            character_name, preferred_emotion
        ):
            return preferred_emotion

        default_emotion = self._get_default_emotion_for_character(character_name)
        if default_emotion:
            return default_emotion

        character_emotions = self.emotion_manager.emotions_data.get(character_name, {})
        return next(iter(character_emotions.keys()), None)

    def _resolve_tts_profile(
        self,
        session_id: str,
        character_name: Optional[str] = None,
        emotion_name: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str], Optional[Dict[str, str]]]:
        session_setting = self.session_emotions.get(session_id)

        resolved_char = character_name
        if not resolved_char:
            if session_setting:
                resolved_char = session_setting.get("character")
                if not emotion_name:
                    emotion_name = session_setting.get("emotion")
            else:
                resolved_char = self.session_character_bindings.get(session_id)
                if not resolved_char and session_id in self.w_active_sessions:
                    resolved_char = self.session_w_settings.get(session_id, {}).get(
                        "character"
                    )

        if not resolved_char:
            resolved_char = self.config.get("default_character")

        if not resolved_char or not self.emotion_manager.character_exists(resolved_char):
            return resolved_char, emotion_name, None

        resolved_emotion = emotion_name
        if resolved_emotion:
            emotion_data = self.emotion_manager.get_emotion_data(
                resolved_char, resolved_emotion
            )
            return resolved_char, resolved_emotion, emotion_data

        preferred_emotion = None
        if session_setting and session_setting.get("character") == resolved_char:
            preferred_emotion = session_setting.get("emotion")

        resolved_emotion = self._pick_available_emotion_name(
            resolved_char, preferred_emotion
        )
        if not resolved_emotion:
            return resolved_char, None, None

        emotion_data = self.emotion_manager.get_emotion_data(
            resolved_char, resolved_emotion
        )
        return resolved_char, resolved_emotion, emotion_data

    async def _translate_text_with_backends(
        self,
        original_text: str,
        disable_provider_during_llm_translation: bool = False,
    ) -> Optional[str]:
        settings = self.config.get("llm_injection_settings", {})
        target_text = None
        translation_prompt = str(settings.get("translation_prompt", "") or "").strip()
        if not translation_prompt:
            target_language_name = self._get_tts_target_language_name()
            translation_prompt = (
                f"请把以下文本翻译成{target_language_name}，保留原文的语气和句末标点，"
                "只输出译文，不要输出解释、引号或原文。"
            )

        if self._should_use_astrbot_provider_translation(
            disable_when_llm_translation_enabled=disable_provider_during_llm_translation
        ):
            try:
                provider_id = settings.get("astrbot_provider_id")
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=original_text, system_prompt=translation_prompt
                    )
                    target_text = llm_resp.completion_text.strip()
                else:
                    logger.error(f"未找到 Provider ID: {provider_id}")
            except Exception as e:
                logger.error(f"AstrBot Provider 翻译失败: {e}")

        if not target_text:
            api_config = self.config.get("translation_api", {})
            if self._has_external_translation_api_config():
                target_text = await translate_text(
                    original_text,
                    self.http_client,
                    api_config,
                    translation_prompt,
                )

        return target_text

    async def _translate_text_and_pick_emotion_with_backends(
        self, original_text: str, emotion_names: list[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        api_config = self.config.get("translation_api", {})
        prompt_template = api_config.get(
            "w_mode_prompt",
            "请对以下中文内容进行翻译和情感分析。首先翻译成日语，然后从以下情感列表中选择最合适的一个：{emotion_list}。请按以下格式输出：\n[翻译后的日语文本][选择的情感名]\n\n原文：{text}",
        )
        emotion_list_str = ", ".join(emotion_names)
        try:
            request_prompt = prompt_template.format(
                emotion_list=emotion_list_str, text=original_text
            )
        except KeyError:
            request_prompt = prompt_template

        strict_system_prompt = (
            "你是翻译与情感分析助手。请严格按照用户要求的格式作答，"
            "只输出翻译后的文本和末尾方括号中的情感名，不要添加解释。"
        )

        backend_result = None
        if self._should_use_astrbot_provider_translation():
            settings = self.config.get("llm_injection_settings", {})
            provider_id = settings.get("astrbot_provider_id")
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    llm_resp = await provider.text_chat(
                        prompt=request_prompt, system_prompt=strict_system_prompt
                    )
                    backend_result = llm_resp.completion_text.strip()
                else:
                    logger.error(f"未找到 Provider ID: {provider_id}")
            except Exception as e:
                logger.error(f"AstrBot Provider 自动情感翻译失败: {e}")

        if not backend_result and self._has_external_translation_api_config():
            backend_result = await translate_text(
                request_prompt,
                self.http_client,
                api_config,
                strict_system_prompt,
            )

        if not backend_result:
            return None, None

        match = re.search(r"(.*)\[(.+?)\]\s*$", backend_result.strip(), re.DOTALL)
        if not match:
            return backend_result.strip(), None

        return match.group(1).strip(), match.group(2).strip()

    def _build_llm_tool_prompt(self, session_id: Optional[str] = None) -> Optional[str]:
        settings = self.config.get("llm_injection_settings", {})
        if not settings.get("enable_llm_tts_tool_prompt", False):
            return None

        prompt_template = settings.get("llm_tts_tool_prompt", "")
        char_name = None
        if session_id:
            char_name, _, _ = self._resolve_tts_profile(session_id)
        if not char_name:
            char_name = self.config.get("default_character")

        emotions = []
        if char_name and self.emotion_manager.character_exists(char_name):
            emotions = list(self.emotion_manager.emotions_data.get(char_name, {}).keys())

        prompt_template = str(prompt_template).strip()
        if not prompt_template:
            return None

        emotions_text = ", ".join(emotions)
        try:
            prompt = prompt_template.format(
                character=char_name or "",
                emotions=emotions_text,
            )
        except (KeyError, IndexError, ValueError):
            prompt = prompt_template

        prompt = prompt.strip()
        if not prompt:
            return None

        runtime_lines = []
        if emotions:
            runtime_lines.append(
                f"genie_tts_speak 当前可用情感：{emotions_text}。"
                "调用工具时必须优先根据朗读内容选择最贴切的 emotion_name；"
                "只有完全无法判断时才允许留空使用默认情感。"
            )

        if self.config.get("enable_translation", True):
            target_language_name = self._get_tts_target_language_name()
            if self._get_translation_workflow() == "llm_injection":
                runtime_lines.append(
                    f"当前语音合成目标语言是{target_language_name}。如果你调用 genie_tts_speak，"
                    f"请先把要朗读的内容翻成{target_language_name}，再把翻译后的完整句子直接填入 text 参数，"
                    "并保留句末标点。"
                )
            else:
                runtime_lines.append(
                    f"当前语音合成目标语言是{target_language_name}。如果你调用 genie_tts_speak，"
                    "text 参数直接填写完整原文即可，插件会在发送前自动翻译。"
                )
        else:
            runtime_lines.append(
                "当前语音合成不做翻译，text 参数直接填写最终要朗读的完整句子即可。"
            )

        return "\n".join([prompt, *runtime_lines]).strip()

    def _build_pause_prompt(self) -> Optional[str]:
        """自定义停顿标记开启时，返回要注入给 LLM 的提示词；关闭时返回 None。"""
        if not self.config.get("enable_custom_pause_marker", False):
            return None
        prompt = self.config.get("custom_pause_prompt", "")
        if isinstance(prompt, str):
            prompt = prompt.strip()
        return prompt or None

    async def _synthesize_speech_from_context(
        self, text: str, session_id: str
    ) -> Optional[str]:
        """根据当前会话设置合成语音（固定感情模式）"""
        trace = self.run_log.begin_synth(
            "context", session=session_id, tts_text=text
        )
        char_name, emotion_name, emotion_data = self._resolve_tts_profile(session_id)
        if not char_name or not emotion_name:
            trace.fail("未配置角色或感情")
            logger.error(f"[{session_id}] 未配置默认角色或感情。")
            return None
        trace.set(
            character=char_name,
            emotion=emotion_name,
            emotion_source="会话固定情感",
        )
        if not emotion_data:
            trace.fail("感情不存在")
            logger.error(f"[{session_id}] 找不到感情配置: {char_name} - {emotion_name}")
            return None

        # 整段没有可发音字符时直接放弃：Genie 的 t2s 对纯标点段会返回空音频，
        # 旧版本甚至会把参考音频原样拼进结果里。
        if not has_pronounceable(text):
            trace.skip("无可朗读字符")
            logger.info(f"[{session_id}] 文本没有可朗读字符，已跳过合成: {text[:40]}")
            return None

        trace.set(
            ref_audio=emotion_data.get("ref_audio_path"),
            ref_text=emotion_data.get("ref_audio_text"),
            language=emotion_data.get("language"),
        )
        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )
        if audio_path:
            trace.ok(audio_path=audio_path)
        else:
            trace.fail("TTS合成失败")
        return self._remember_last_audio(
            session_id,
            audio_path,
            text=text,
            character=char_name,
            emotion=emotion_name,
        )

    @filter.on_llm_request()
    async def inject_llm_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在LLM请求前注入提示词"""
        await self._observe_session(event)
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 只有在开启了TTS模式（自动或固定，或群组模式）时才注入
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = session_state.effective_state(self, session_id, group_id)["enabled"]

        if not is_active:
            tool_prompt = self._build_llm_tool_prompt(session_id)
            if tool_prompt:
                pause_prompt = self._build_pause_prompt()
                if pause_prompt:
                    tool_prompt = f"{tool_prompt}\n\n{pause_prompt}"
                req.system_prompt += f"\n\n{tool_prompt}"
                logger.info(f"[{session_id}] 已注入LLM语音工具提示。")
            return

        settings = self.config.get("llm_injection_settings", {})
        auto_tts_this_turn = self._should_generate_tts_now(session_id)
        self.checked_auto_tts_sessions.add(session_id)
        if auto_tts_this_turn:
            self.pending_auto_tts_sessions.add(session_id)
        else:
            self.pending_auto_tts_sessions.discard(session_id)

        enable_emotion = (
            auto_tts_this_turn and self._should_inject_llm_emotion_tags()
        )
        enable_translation = (
            auto_tts_this_turn and self._should_inject_llm_translation_tags()
        )
        tool_prompt = self._build_llm_tool_prompt(session_id)

        if not enable_emotion and not enable_translation and not tool_prompt:
            return

        prompts_to_inject = []

        if enable_emotion:
            # 确定当前角色以获取可用情感列表
            char_name = None
            if session_id in self.w_active_sessions:
                char_name = self.session_w_settings.get(session_id, {}).get(
                    "character"
                ) or self.config.get("default_character")
            elif session_id in self.active_sessions or is_group_tts_active:
                # 固定模式或群组模式下
                session_setting = self.session_emotions.get(session_id)
                char_name = (
                    session_setting["character"] if session_setting else
                    self.session_character_bindings.get(session_id) or self.config.get("default_character")
                )

            if char_name and self.emotion_manager.character_exists(char_name):
                emotions = list(self.emotion_manager.emotions_data[char_name].keys())
                emotions_str = ", ".join(emotions)

                prompt_template = settings.get("llm_emotion_prompt", "")
                try:
                    emotion_prompt = prompt_template.format(emotions=emotions_str)
                except KeyError:
                    emotion_prompt = prompt_template
                prompts_to_inject.append(emotion_prompt)
            else:
                logger.warning(
                    f"[{session_id}] 情感注入被跳过：角色 '{char_name}' 未注册任何情感，"
                    "本轮不会要求 LLM 输出 [emotion=xxx] 标签，自动TTS将回落默认情感。"
                    "可用 /注册感情 为该角色注册情感。"
                )

        if enable_translation:
            trans_prompt = settings.get("llm_translation_prompt", "")
            if trans_prompt:
                prompts_to_inject.append(trans_prompt)

        if tool_prompt:
            prompts_to_inject.append(tool_prompt)

        pause_prompt = self._build_pause_prompt()
        pause_injected = bool(pause_prompt) and (enable_translation or bool(tool_prompt))
        if pause_injected:
            prompts_to_inject.append(pause_prompt)

        if prompts_to_inject:
            final_prompt = "\n\n".join(prompts_to_inject)
            req.system_prompt += f"\n\n{final_prompt}"
            logger.info(
                f"[{session_id}] 已注入LLM提示词 "
                f"(AutoTTS: {auto_tts_this_turn}, Emotion: {enable_emotion}, "
                f"Trans: {enable_translation}, Tool: {bool(tool_prompt)}, "
                f"Pause: {pause_injected})"
            )

    @filter.on_llm_response()
    async def intercept_llm_response_for_tts(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        await self._observe_session(event)
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        original_text = resp.completion_text.strip()
        if not original_text:
            return

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 0. 清理可能存在的幻觉报错 (防止LLM复读之前的错误提示)
        #    整条正则匹配。旧实现用 "(TTS失败: 角色" 做模糊匹配，
        #    会把 "'角色名'无效)" 这种残渣留在正文里发给用户。
        cleaned_text = self.TTS_FAILURE_NOTICE_PATTERN.sub("", original_text).strip()
        removed_failure_notice = cleaned_text != original_text
        original_text = cleaned_text
        if not original_text:
            return

        configured_llm_emotion = self._should_inject_llm_emotion_tags()
        configured_llm_translation = self._should_inject_llm_translation_tags()
        original_text, injected_emotion, injected_translation, stripped_directives = (
            self._strip_llm_tts_directives(
                original_text,
                strip_translation=configured_llm_translation,
                strip_emotion=configured_llm_emotion or "[emotion=" in original_text,
            )
        )
        # 旧版本只在剥离了内部标签时才写回，导致被清理掉的幻觉失败提示
        # 依然会原样发给用户；这里把清理结果一并写回可见回复。
        if stripped_directives or removed_failure_notice:
            resp.completion_text = original_text.strip()
            resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

        # 检查是否开启了TTS (个人会话 或 群组)
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = session_state.effective_state(self, session_id, group_id)["enabled"]

        if not is_active:
            return

        settings = self.config.get("llm_injection_settings", {})
        enable_llm_emotion = configured_llm_emotion
        enable_llm_translation = configured_llm_translation
        translation_workflow = self._get_translation_workflow()

        # 更新 LLM 回复文本为净化后的文本 (去除标签和翻译部分)
        resp.completion_text = original_text.strip()
        # 同时更新 result_chain 中的 Plain 消息，否则用户还是会看到标签
        # 注意：这里假设 result_chain 第一个是 Plain。如果不是，可能需要遍历。
        # 简单起见，我们重建 chain
        resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

        if session_id in self.skip_next_auto_tts_sessions:
            self.skip_next_auto_tts_sessions.discard(session_id)
            self.pending_auto_tts_sessions.discard(session_id)
            self.checked_auto_tts_sessions.discard(session_id)
            logger.info(f"[{session_id}] 已由 LLM 主动语音工具发送语音，跳过本次自动 TTS。")
            return

        if session_id in self.checked_auto_tts_sessions:
            should_generate_auto_tts = session_id in self.pending_auto_tts_sessions
        else:
            should_generate_auto_tts = self._should_generate_tts_now(session_id)
        self.pending_auto_tts_sessions.discard(session_id)
        self.checked_auto_tts_sessions.discard(session_id)

        if not should_generate_auto_tts:
            return

        # --- 开始 TTS 处理流程 ---
        trace = self.run_log.begin_synth(
            "auto",
            session=session_id,
            group=group_id or "",
            llm_text=original_text,
            workflow=translation_workflow,
        )

        audio_path: Optional[str] = None
        target_emotion = None
        emotion_source = ""
        target_text = None
        char_name = None

        # 确定角色
        if session_id in self.w_active_sessions:
            char_name = self.session_w_settings.get(session_id, {}).get(
                "character"
            ) or self.config.get("default_character")
        else:
            # 固定模式 或 群组模式
            session_setting = self.session_emotions.get(session_id)
            char_name = (
                session_setting["character"] if session_setting else
                self.session_character_bindings.get(session_id) or self.config.get("default_character")
            )
            # 固定模式下，如果没有注入情感，使用默认情感
            if not injected_emotion:
                target_emotion = (
                    session_setting["emotion"]
                    if session_setting
                    else self._get_default_emotion_for_character(char_name)
                )
                emotion_source = "会话固定情感" if session_setting else "角色默认情感"

        if not char_name or not self.emotion_manager.character_exists(char_name):
            trace.fail("角色无效", character=char_name or "")
            self._append_tts_failure_notice(resp, f"TTS失败: 角色'{char_name}'无效")
            return
        trace.set(
            character=char_name,
            candidates=list(
                self.emotion_manager.emotions_data.get(char_name, {}).keys()
            ),
        )

        # 确定情感
        if enable_llm_emotion and injected_emotion:
            target_emotion = injected_emotion
            emotion_source = "LLM情感标签"
        elif enable_llm_emotion and not injected_emotion:
            logger.info(
                f"[{session_id}] 已开启LLM情感标签，但本轮回复未解析到 [emotion=xxx]，"
                "将使用会话固定/默认情感。请确认角色已注册情感、且情感提示词包含 {emotions} 占位符。"
            )

        # 确定翻译文本
        if enable_llm_translation and injected_translation:
            target_text = injected_translation
        elif not self.config.get("enable_translation", True):
            # 翻译功能已关闭，直接使用原文（适合中文模型）
            target_text = original_text
        else:
            if translation_workflow == "provider_translation":
                # provider 模式下，自动情感识别可由独立翻译 Provider 一并完成。
                provider_auto = should_provider_autofill_emotion(
                    enabled=bool(settings.get("enable_provider_emotion_autofill", False)),
                    translation_workflow=translation_workflow,
                    has_manual_emotion=bool(self.session_emotions.get(session_id)),
                    has_injected_emotion=bool(enable_llm_emotion and injected_emotion),
                    w_mode_active=session_id in self.w_active_sessions,
                )
                if provider_auto:
                    character_emotions = list(
                        self.emotion_manager.emotions_data[char_name].keys()
                    )
                    target_text, target_emotion = (
                        await self._translate_text_and_pick_emotion_with_backends(
                            original_text, character_emotions
                        )
                    )
                    if target_emotion:
                        emotion_source = "翻译Provider情感识别"

                if not target_text:
                    target_text = await self._translate_text_with_backends(
                        original_text,
                        disable_provider_during_llm_translation=True,
                    )

        self._log_translation_result(session_id, original_text, target_text)

        if not target_text:
            if translation_workflow == "llm_injection":
                trace.skip("主LLM未返回翻译标签")
                logger.warning(
                    f"[{session_id}] 本轮自动TTS已触发，但主LLM没有返回 $...$ 翻译标签，"
                    "已跳过语音合成并保留原文本回复。"
                )
                return
            trace.fail("翻译无结果")
            self._append_tts_failure_notice(resp, "TTS失败: 翻译无结果")
            return
        if target_text != original_text:
            trace.set(translated_text=target_text, translated=True)

        display_text = original_text
        output_mode = self._get_auto_tts_output_mode()
        tts_source_text, plain_display_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        trace.set(display_text=display_text, output_mode=output_mode)
        if not tts_source_text:
            trace.skip("无可朗读文本")
            self._append_tts_failure_notice(resp, "TTS失败: 没有可用于朗读的文本")
            return

        if output_mode == "split_audio_text":
            if enable_llm_translation and injected_translation:
                translated_audio_text, _, _ = self._prepare_tts_output_segments(
                    injected_translation, output_mode
                )
                target_text = translated_audio_text or target_text
            elif self.config.get("enable_translation", True) and translation_workflow == "provider_translation":
                target_text = await self._translate_text_with_backends(
                    tts_source_text,
                    disable_provider_during_llm_translation=True,
                )
                self._log_translation_result(session_id, tts_source_text, target_text)
            else:
                target_text = tts_source_text

            if not target_text:
                trace.fail("混合模式翻译无结果")
                self._append_tts_failure_notice(resp, "TTS失败: 混合模式翻译无结果")
                return

        # 最终合成
        # 如果此时还没有 target_emotion (比如固定模式没注入，或者自动模式失败)，使用默认
        if not target_emotion:
            target_emotion = self.config.get("default_emotion_name")
            emotion_source = "默认情感兜底"
        trace.set(emotion=target_emotion or "", emotion_source=emotion_source)

        emotion_data = self.emotion_manager.get_emotion_data(char_name, target_emotion)
        if not emotion_data:
            # 尝试回落到默认情感
            invalid_emotion = target_emotion
            default_emotion = self.config.get("default_emotion_name")
            emotion_data = self.emotion_manager.get_emotion_data(
                char_name, default_emotion
            )
            if not emotion_data:
                trace.fail("情感无效")
                self._append_tts_failure_notice(
                    resp, f"TTS失败: 情感'{target_emotion}'无效"
                )
                return
            target_emotion = default_emotion
            emotion_source = f"无效情感'{invalid_emotion}'回落默认"
            logger.warning(
                f"[{session_id}] 自动TTS情感无效，已回落默认情感: "
                f"{char_name} - {target_emotion}（原情感: {invalid_emotion}）"
            )

        trace.set(
            emotion=target_emotion or "",
            emotion_source=emotion_source,
            ref_audio=emotion_data.get("ref_audio_path"),
            ref_text=emotion_data.get("ref_audio_text"),
            language=emotion_data.get("language"),
        )
        logger.info(
            f"[{session_id}] 自动TTS情感选择 | 角色: {char_name} | "
            f"情感: {target_emotion} | 来源: {emotion_source or '未标记'} | "
            f"参考音频: {emotion_data.get('ref_audio_path')}"
        )

        # 整段都是标点/表情符号时不要送去合成：不仅浪费一次请求，
        # 还会在正文后面挂一条毫无意义的"合成失败"。
        if not has_pronounceable(target_text):
            trace.skip("无可朗读字符", tts_text=target_text)
            logger.info(
                f"[{session_id}] 待合成文本没有可朗读字符，已跳过本轮自动TTS: "
                f"{target_text[:40]}"
            )
            return

        # 合成语音
        trace.set(tts_text=target_text)
        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )

        if audio_path:
            self._remember_last_audio(
                session_id,
                audio_path,
                text=display_text or target_text,
                character=char_name,
                emotion=target_emotion,
            )
            audio_sent = await self._apply_auto_tts_output_mode(
                session_id=session_id,
                resp=resp,
                audio_path=audio_path,
                full_display_text=display_text,
                plain_display_text=plain_display_text,
                output_mode=output_mode,
            )
            # 收尾放在发送之后：合成成功但没送出去也算这次失败，否则日志页只会显示
            # 一条「成功」，用户根本查不到。和 LLM 工具那条链路的记法保持一致。
            if audio_sent:
                trace.ok(audio_path=audio_path)
            else:
                trace.fail("音频发送失败", audio_path=audio_path)
        else:
            trace.fail("TTS合成失败")
            self._append_tts_failure_notice(resp, "TTS合成失败")

    @filter.on_decorating_result()
    async def sanitize_tts_directives_before_send(self, event: AstrMessageEvent):
        """Final guard to keep internal TTS directives out of visible chat."""
        result = event.get_result()
        chain = getattr(result, "chain", None) if result else None
        if not chain:
            return

        if self._sanitize_plain_components(chain):
            result.chain = chain
            event.set_result(result)
            logger.info(f"[{event.unified_msg_origin}] 已清理残留的TTS内部标签。")

    async def terminate(self):
        """插件卸载/停用时关闭http客户端"""
        self._keepalive_stop_event.set()
        if self._keepalive_task:
            await asyncio.gather(self._keepalive_task, return_exceptions=True)

        if self._state_restore_task and not self._state_restore_task.done():
            self._state_restore_task.cancel()
            await asyncio.gather(self._state_restore_task, return_exceptions=True)

        if self._config_migration_task and not self._config_migration_task.done():
            self._config_migration_task.cancel()
            await asyncio.gather(self._config_migration_task, return_exceptions=True)

        await self.tts_engine.terminate()
        await self.http_client.aclose()
        logger.info("LLM TTS 插件已卸载，HTTP客户端已关闭。")
        # 最后再摘日志 handler，保证上面那条卸载日志也能进缓冲。
        self.run_log.detach()
