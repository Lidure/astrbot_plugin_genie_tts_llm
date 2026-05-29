import asyncio
import httpx
import os
import random
import re
import time
from typing import Dict, Optional, Set, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
import astrbot.api.message_components as Comp
from astrbot.api.provider import LLMResponse, ProviderRequest

# 从新模块导入功能
from .emotion_manager import EmotionManager
from .tts_engine import TTSEngine
from .external_apis import translate_text


@register(
    "astrbot_plugin_genie_tts_llm",
    "Whereis-Alice",
    "一个通过 LLM、翻译和 Genie TTS 实现语音合成的插件，支持主动语音工具",
    "1.6.1",
    "https://github.com/Whereis-Alice/astrbot_plugin_genie_tts_llm",
)
class GenieTtsLlmPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.active_sessions: Set[str] = set()
        self.w_active_sessions: Set[str] = set()
        self.active_groups: Set[str] = set()  # 新增：群组级TTS开关
        self.inactive_groups: Set[str] = set()
        self.session_emotions: Dict[str, Dict[str, str]] = {}
        self.session_w_settings: Dict[str, Dict[str, str]] = {}
        self.last_tts_trigger_at: Dict[str, float] = {}
        self.skip_next_auto_tts_sessions: Set[str] = set()
        self._keepalive_stop_event = asyncio.Event()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._llm_translation_conflict_logged = False

        # 初始化辅助模块
        plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_genie_tts_llm")
        emotions_file_path = plugin_data_dir / "emotions.json"
        self.emotion_manager = EmotionManager(emotions_file_path)

        self.http_client = httpx.AsyncClient(timeout=300.0)
        self.tts_engine = TTSEngine(self.config, self.http_client, plugin_data_dir)

        if self.config.get("enable_space_keepalive"):
            self._keepalive_task = asyncio.create_task(self._keep_alive_loop())

        # 初始化白名单群组（自动开启 TTS）
        whitelist = self.config.get("group_whitelist", [])
        for group_id in whitelist:
            normalized_group_id = self._normalize_group_id(group_id)
            if normalized_group_id:
                self.active_groups.add(normalized_group_id)
                logger.info(f"白名单群组 [{normalized_group_id}] 已自动开启语音合成。")

        if self.config.get("enable_group_tts_by_default", False):
            logger.info("已开启全部群默认语音合成；群组黑名单仍优先。")

        logger.info("LLM TTS 插件已加载。")

    def _normalize_group_id(self, group_id: Optional[object]) -> str:
        """统一群号格式，避免配置里的字符串和事件里的数字不匹配。"""
        return str(group_id) if group_id else ""

    def _is_group_blacklisted(self, group_id: Optional[object]) -> bool:
        """检查群组是否在黑名单中"""
        group_id = self._normalize_group_id(group_id)
        if not group_id:
            return False
        blacklist = self.config.get("group_blacklist", [])
        return str(group_id) in [str(g) for g in blacklist]

    def _is_group_tts_active(self, group_id: Optional[object]) -> bool:
        """检查群组级 TTS 是否开启。黑名单 > 运行时关闭 > 默认全开/白名单/手动开启。"""
        group_id = self._normalize_group_id(group_id)
        if not group_id or self._is_group_blacklisted(group_id):
            return False
        if group_id in self.inactive_groups:
            return False
        return bool(self.config.get("enable_group_tts_by_default", False)) or (
            group_id in self.active_groups
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

    def _should_use_astrbot_provider_translation(
        self, disable_when_llm_translation_enabled: bool = False
    ) -> bool:
        settings = self.config.get("llm_injection_settings", {})
        if not settings.get("use_astrbot_provider", False):
            return False

        provider_id = settings.get("astrbot_provider_id")
        if not provider_id:
            return False

        if disable_when_llm_translation_enabled and settings.get(
            "enable_llm_translation", False
        ):
            if not self._llm_translation_conflict_logged:
                logger.info(
                    "已启用主 LLM 直接生成翻译标签，框架内翻译将自动忽略，避免与注入模式冲突。"
                )
                self._llm_translation_conflict_logged = True
            return False

        return True

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
    ) -> None:
        if output_mode == "audio_only":
            resp.result_chain.chain = [Comp.Record(file=audio_path)]
            return

        audio_sent = await self._send_audio_message(session_id, audio_path)
        if audio_sent:
            resp.completion_text = plain_display_text
            resp.result_chain.chain = [Comp.Plain(plain_display_text)]
            return

        resp.completion_text = full_display_text
        resp.result_chain.chain = [
            Comp.Plain(full_display_text),
            Comp.Plain("\n(TTS音频发送失败)"),
        ]

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
    async def view_emotions_command(self, event: AstrMessageEvent):
        """查看所有已注册的感情"""
        emotions_data = self.emotion_manager.emotions_data
        if not emotions_data:
            yield event.plain_result("当前未注册任何感情。")
            return

        formatted_lines = ["所有已注册的感情列表："]
        for character, emotions in emotions_data.items():
            formatted_lines.append(f"\n角色: {character}")
            if emotions:
                for emotion_name in emotions.keys():
                    formatted_lines.append(f"  - {emotion_name}")
            else:
                formatted_lines.append("  (暂无感情)")

        final_message = "\n".join(formatted_lines)
        yield event.plain_result(final_message)

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

        emotion_data = self.emotion_manager.get_emotion_data(
            character_name, emotion_name
        )
        if not emotion_data:
            yield event.plain_result(
                f"❌ 未找到角色 '{character_name}' 的感情 '{emotion_name}'。请先使用 /注册感情 指令添加。"
            )
            return

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
            yield event.chain_result([Comp.Record(file=audio_path)])
        else:
            yield event.plain_result("语音合成失败，请检查服务器状态或日志。")
        event.stop_event()

    @filter.command("tts-llm", alias={"开启语音合成"})
    async def start_tts(self, event: AstrMessageEvent):
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.active_sessions.add(session_id)
        self.w_active_sessions.discard(session_id)
        default_char = self.config.get("default_character")
        default_emotion = self.config.get("default_emotion_name")
        logger.info(f"会话 [{session_id}] 的 LLM TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的LLM语音合成已开启。\n将使用默认感情: {default_char} - {default_emotion}"
        )

    @filter.command("tts-q", alias={"关闭语音合成"})
    async def stop_tts(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        self.active_sessions.discard(session_id)
        self.w_active_sessions.discard(session_id)
        logger.info(f"会话 [{session_id}] 的所有 LLM TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本对话的所有LLM语音合成功能已关闭。")

    @filter.command("ttg", alias={"开启群语音"})
    async def start_group_tts(self, event: AstrMessageEvent):
        """开启当前群组的语音合成 (对所有人生效)"""
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        self.inactive_groups.discard(group_id)
        self.active_groups.add(group_id)
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
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if not group_id:
            yield event.plain_result("❌ 此指令仅限群聊使用。")
            return

        if self.config.get("enable_group_tts_by_default", False):
            self.inactive_groups.add(group_id)
        else:
            self.active_groups.discard(group_id)
        logger.info(f"群组 [{group_id}] 的 LLM TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本群组的LLM语音合成已关闭。")

    @filter.command("tts-w", alias={"开启自动情感识别"})
    async def start_tts_w(self, event: AstrMessageEvent):
        group_id = self._normalize_group_id(event.message_obj.group_id)
        if self._is_group_blacklisted(group_id):
            yield event.plain_result("❌ 本群组已被禁用语音合成功能。")
            return

        session_id = event.unified_msg_origin
        self.w_active_sessions.add(session_id)
        self.active_sessions.discard(session_id)
        default_char = self.config.get("default_character")
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已开启。")
        yield event.plain_result(
            f"▶️ 本对话的自动情感识别语音合成已开启。\n将使用默认角色: {default_char}"
        )

    @filter.command("tts-w-q", alias={"关闭自动情感识别"})
    async def stop_tts_w(self, event: AstrMessageEvent):
        session_id = event.unified_msg_origin
        self.w_active_sessions.discard(session_id)
        logger.info(f"会话 [{session_id}] 的 LLM 自动情感识别 TTS 功能已关闭。")
        yield event.plain_result("⏹️ 本对话的自动情感识别语音合成已关闭。")

    @filter.command("sw", alias={"切换感情"})
    async def switch_emotion(
        self, event: AstrMessageEvent, character_name: str, emotion_name: str
    ):
        if self.emotion_manager.get_emotion_data(character_name, emotion_name):
            self.session_emotions[event.unified_msg_origin] = {
                "character": character_name,
                "emotion": emotion_name,
            }
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
        if self.emotion_manager.character_exists(character_name):
            self.session_w_settings[event.unified_msg_origin] = {
                "character": character_name
            }
            logger.info(
                f"会话 [{event.unified_msg_origin}] 切换自动情感识别角色至: {character_name}"
            )
            yield event.plain_result(
                f"本会话自动情感识别角色已切换为: {character_name}"
            )
        else:
            yield event.plain_result(f"❌ 未找到角色 '{character_name}'。")

    @filter.llm_tool(name="genie_tts_speak")
    async def llm_tool_genie_tts_speak(
        self,
        event: AstrMessageEvent,
        text: str,
        character_name: Optional[str] = None,
        emotion_name: Optional[str] = None,
    ) -> str:
        """在当前会话中直接发送一条 TTS 语音。

        仅当用户明确要求“说一句”“发语音”“让我听听声音”“念给我听”时使用。
        普通闲聊不要调用这个工具；日常语音仍由插件自己的自动触发模式控制。

        Args:
            text(string): 要合成为语音并直接发给用户的文本。尽量简短、自然，适合直接朗读。
            character_name(string): 可选。要使用的角色名；仅在明确知道已注册角色时填写，否则留空沿用当前会话或默认角色。
            emotion_name(string): 可选。要使用的情感名；仅在明确知道已注册情感时填写，否则留空沿用当前会话、默认情感或该角色的首个可用情感。
        """
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        text = text.strip()

        if self._is_group_blacklisted(group_id):
            return "当前群组已禁用语音功能，不能直接发送 TTS 语音。"
        if not text:
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"

        char_name, resolved_emotion, emotion_data = self._resolve_tts_profile(
            session_id, character_name, emotion_name
        )
        if not char_name or not self.emotion_manager.character_exists(char_name):
            return "没有找到可用角色，请先检查默认角色或角色注册情况。"
        if not emotion_data or not resolved_emotion:
            if emotion_name:
                return (
                    f"角色 '{char_name}' 下未找到情感 '{emotion_name}'。"
                    "请改用已注册的情感名，或留空让插件自动选择。"
                )
            return f"角色 '{char_name}' 目前没有可用的情感配置。"

        if self.config.get("enable_translation", True):
            target_text = await self._translate_text_with_backends(text)
        else:
            target_text = text
        self._log_translation_result(session_id, text, target_text)

        if not target_text:
            return "语音发送失败：用于 TTS 的文本准备失败了，请检查翻译配置或日志。"

        output_mode = self._get_llm_tool_tts_output_mode()
        tts_text, plain_text, output_mode = self._prepare_tts_output_segments(
            text, output_mode
        )
        if not tts_text:
            return "语音发送失败：没有可用于朗读的文本。"

        tts_target_text = target_text
        if output_mode == "split_audio_text":
            if self.config.get("enable_translation", True):
                tts_target_text = await self._translate_text_with_backends(tts_text)
                self._log_translation_result(session_id, tts_text, tts_target_text)
            else:
                tts_target_text = tts_text

            if not tts_target_text:
                return "语音发送失败：混合模式下用于 TTS 的文本准备失败了，请检查翻译配置或日志。"

        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=tts_target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )
        if not audio_path:
            return "语音发送失败：TTS 合成没有成功，请检查服务状态或日志。"

        ok, error_message = await self._dispatch_llm_tool_tts_output(
            session_id=session_id,
            audio_path=audio_path,
            full_display_text=text,
            plain_display_text=plain_text,
            output_mode=output_mode,
        )
        if not ok:
            return (
                (error_message or "语音已经合成成功，但 AstrBot 主动发送失败了。")
                + "请确认当前会话对应的平台实例仍然在线。"
            )

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

        default_emotion = self.config.get("default_emotion_name")
        if default_emotion and self.emotion_manager.get_emotion_data(
            character_name, default_emotion
        ):
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
            elif session_id in self.w_active_sessions:
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

        provider_id = settings.get("astrbot_provider_id")
        if self._should_use_astrbot_provider_translation(
            disable_when_llm_translation_enabled=disable_provider_during_llm_translation
        ) and provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
                if provider:
                    trans_prompt = settings.get(
                        "translation_prompt",
                        "Translate the following text to Japanese. Output only the translation, nothing else.",
                    )
                    llm_resp = await provider.text_chat(
                        prompt=original_text, system_prompt=trans_prompt
                    )
                    target_text = llm_resp.completion_text
                else:
                    logger.error(f"未找到 Provider ID: {provider_id}")
            except Exception as e:
                logger.error(f"AstrBot Provider 翻译失败: {e}")

        if not target_text:
            api_config = self.config.get("translation_api", {})
            if self._has_external_translation_api_config():
                target_text = await translate_text(
                    original_text, self.http_client, api_config
                )

        return target_text

    def _build_llm_tool_prompt(self) -> Optional[str]:
        settings = self.config.get("llm_injection_settings", {})
        if not settings.get("enable_llm_tts_tool_prompt", False):
            return None

        prompt_template = settings.get("llm_tts_tool_prompt", "")
        prompt = str(prompt_template).strip()
        return prompt or None

    async def _synthesize_speech_from_context(
        self, text: str, session_id: str
    ) -> Optional[str]:
        """根据当前会话设置合成语音（固定感情模式）"""
        char_name, emotion_name, emotion_data = self._resolve_tts_profile(session_id)
        if not char_name or not emotion_name:
            logger.error(f"[{session_id}] 未配置默认角色或感情。")
            return None
        if not emotion_data:
            logger.error(f"[{session_id}] 找不到感情配置: {char_name} - {emotion_name}")
            return None

        return await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )

    @filter.on_llm_request()
    async def inject_llm_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """在LLM请求前注入提示词"""
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 只有在开启了TTS模式（自动或固定，或群组模式）时才注入
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = (
            session_id in self.active_sessions
            or session_id in self.w_active_sessions
            or is_group_tts_active
        )

        if not is_active:
            return

        settings = self.config.get("llm_injection_settings", {})
        enable_emotion = settings.get("enable_llm_emotion", False)
        enable_translation = settings.get("enable_llm_translation", False)

        if not enable_emotion and not enable_translation:
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
                    session_setting["character"]
                    if session_setting
                    else self.config.get("default_character")
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

        if enable_translation:
            trans_prompt = settings.get("llm_translation_prompt", "")
            if trans_prompt:
                prompts_to_inject.append(trans_prompt)

        tool_prompt = self._build_llm_tool_prompt()
        if tool_prompt:
            prompts_to_inject.append(tool_prompt)

        if prompts_to_inject:
            final_prompt = "\n\n".join(prompts_to_inject)
            req.system_prompt += f"\n\n{final_prompt}"
            logger.info(
                f"[{session_id}] 已注入LLM提示词 "
                f"(Emotion: {enable_emotion}, Trans: {enable_translation}, Tool: {bool(tool_prompt)})"
            )

    @filter.on_llm_response()
    async def intercept_llm_response_for_tts(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        original_text = resp.completion_text.strip()
        if not original_text:
            return

        # 黑名单群组不进行任何处理
        if self._is_group_blacklisted(group_id):
            return

        # 0. 清理可能存在的幻觉报错 (防止LLM复读之前的错误提示)
        original_text = original_text.replace("(TTS失败: 翻译无结果)", "")
        original_text = original_text.replace("(TTS合成失败)", "")
        original_text = original_text.replace("(TTS失败: 角色", "")  # 模糊匹配

        # 检查是否开启了TTS (个人会话 或 群组)
        is_group_tts_active = self._is_group_tts_active(group_id)
        is_active = (
            session_id in self.active_sessions
            or session_id in self.w_active_sessions
            or is_group_tts_active
        )

        if not is_active:
            return

        settings = self.config.get("llm_injection_settings", {})
        enable_llm_emotion = settings.get("enable_llm_emotion", False)
        enable_llm_translation = settings.get("enable_llm_translation", False)

        # 1. 提取情感标签 [emotion=xxx]
        emotion_match = re.search(r"\[emotion=(.*?)\]", original_text)
        injected_emotion = None
        if emotion_match:
            injected_emotion = emotion_match.group(1).strip()
            # 从原文中移除标签，保持回复干净
            original_text = original_text.replace(emotion_match.group(0), "")

        injected_translation = None
        # 2. 提取翻译内容（仅在开启 LLM 翻译注入时处理）
        # 仅匹配显式标记，避免误删普通文本中的反斜杠/金额符号等内容。
        if enable_llm_translation:
            translation_match = re.search(
                r"(?:\$(.+?)\$|\uFF04(.+?)\uFF04)\s*$", original_text, re.DOTALL
            )
            if translation_match:
                injected_translation = (
                    translation_match.group(1) or translation_match.group(2) or ""
                ).strip()
                # 从原文中移除翻译，保持回复干净
                original_text = original_text.replace(translation_match.group(0), "", 1)

        # 更新 LLM 回复文本为净化后的文本 (去除标签和翻译部分)
        resp.completion_text = original_text.strip()
        # 同时更新 result_chain 中的 Plain 消息，否则用户还是会看到标签
        # 注意：这里假设 result_chain 第一个是 Plain。如果不是，可能需要遍历。
        # 简单起见，我们重建 chain
        resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

        if session_id in self.skip_next_auto_tts_sessions:
            self.skip_next_auto_tts_sessions.discard(session_id)
            logger.info(f"[{session_id}] 已由 LLM 主动语音工具发送语音，跳过本次自动 TTS。")
            return

        if not self._should_generate_tts_now(session_id):
            return

        # --- 开始 TTS 处理流程 ---

        audio_path: Optional[str] = None
        target_emotion = None
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
                session_setting["character"]
                if session_setting
                else self.config.get("default_character")
            )
            # 固定模式下，如果没有注入情感，使用默认情感
            if not injected_emotion:
                target_emotion = (
                    session_setting["emotion"]
                    if session_setting
                    else self.config.get("default_emotion_name")
                )

        if not char_name or not self.emotion_manager.character_exists(char_name):
            resp.result_chain.chain.append(
                Comp.Plain(f"\n(TTS失败: 角色'{char_name}'无效)")
            )
            return

        # 确定情感
        if enable_llm_emotion and injected_emotion:
            target_emotion = injected_emotion

        # 确定翻译文本
        if enable_llm_translation and injected_translation:
            target_text = injected_translation
        elif not self.config.get("enable_translation", True):
            # 翻译功能已关闭，直接使用原文（适合中文模型）
            target_text = original_text
        else:
            # 需要翻译
            api_config = self.config.get("translation_api", {})
            has_external_translation_api = self._has_external_translation_api_config()
            # 如果是在 w 模式下且没有注入情感，我们需要同时获取情感和翻译 (旧逻辑)
            if (
                session_id in self.w_active_sessions
                and not target_emotion
                and has_external_translation_api
            ):
                # 旧的自动情感识别逻辑
                character_emotions = self.emotion_manager.emotions_data[char_name]
                w_prompt_template = api_config.get("w_mode_prompt")
                if w_prompt_template:
                    emotion_list_str = ", ".join(character_emotions.keys())
                    augmented_prompt = w_prompt_template.format(
                        emotion_list=emotion_list_str, text=original_text
                    )
                    japanese_text_with_emotion = await translate_text(
                        augmented_prompt,
                        self.http_client,
                        api_config,
                        w_prompt_template,
                    )

                    if japanese_text_with_emotion:
                        match = re.search(
                            r"(.*)\[(.+?)\]\s*$",
                            japanese_text_with_emotion.strip(),
                            re.DOTALL,
                        )
                        if match:
                            target_text, target_emotion = (
                                match.group(1).strip(),
                                match.group(2).strip(),
                            )

            # 普通翻译逻辑
            if not target_text:
                target_text = await self._translate_text_with_backends(
                    original_text,
                    disable_provider_during_llm_translation=True,
                )

        self._log_translation_result(session_id, original_text, target_text)

        if not target_text:
            resp.result_chain.chain.append(Comp.Plain("\n(TTS失败: 翻译无结果)"))
            return

        display_text = original_text
        output_mode = self._get_auto_tts_output_mode()
        tts_source_text, plain_display_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        if not tts_source_text:
            resp.result_chain.chain.append(Comp.Plain("\n(TTS失败: 没有可用于朗读的文本)"))
            return

        if output_mode == "split_audio_text":
            if enable_llm_translation and injected_translation:
                translated_audio_text, _, _ = self._prepare_tts_output_segments(
                    injected_translation, output_mode
                )
                target_text = translated_audio_text or target_text
            elif self.config.get("enable_translation", True):
                target_text = await self._translate_text_with_backends(
                    tts_source_text,
                    disable_provider_during_llm_translation=True,
                )
                self._log_translation_result(session_id, tts_source_text, target_text)
            else:
                target_text = tts_source_text

            if not target_text:
                resp.result_chain.chain.append(Comp.Plain("\n(TTS失败: 混合模式翻译无结果)"))
                return

        # 最终合成
        # 如果此时还没有 target_emotion (比如固定模式没注入，或者自动模式失败)，使用默认
        if not target_emotion:
            target_emotion = self.config.get("default_emotion_name")

        emotion_data = self.emotion_manager.get_emotion_data(char_name, target_emotion)
        if not emotion_data:
            # 尝试回落到默认情感
            default_emotion = self.config.get("default_emotion_name")
            emotion_data = self.emotion_manager.get_emotion_data(
                char_name, default_emotion
            )
            if not emotion_data:
                resp.result_chain.chain.append(
                    Comp.Plain(f"\n(TTS失败: 情感'{target_emotion}'无效)")
                )
                return

        # 合成语音
        audio_path = await self.tts_engine.synthesize(
            character_name=char_name,
            ref_audio_path=emotion_data["ref_audio_path"],
            ref_audio_text=emotion_data["ref_audio_text"],
            text=target_text,
            session_id_for_log=session_id,
            language=emotion_data.get("language"),
        )

        if audio_path:
            await self._apply_auto_tts_output_mode(
                session_id=session_id,
                resp=resp,
                audio_path=audio_path,
                full_display_text=display_text,
                plain_display_text=plain_display_text,
                output_mode=output_mode,
            )
        else:
            resp.result_chain.chain.append(Comp.Plain("\n(TTS合成失败)"))

    async def terminate(self):
        """插件卸载/停用时关闭http客户端"""
        self._keepalive_stop_event.set()
        if self._keepalive_task:
            await asyncio.gather(self._keepalive_task, return_exceptions=True)

        await self.tts_engine.terminate()
        await self.http_client.aclose()
        logger.info("LLM TTS 插件已卸载，HTTP客户端已关闭。")
