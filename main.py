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
from .session_character_bindings import SessionCharacterBindings
from .emotion_routing import extract_emotion_directive, parse_provider_emotion_result


@register(
    "astrbot_plugin_genie_tts_llm",
    "Whereis-Alice",
    "一个通过 LLM、翻译和 Genie TTS 实现语音合成的插件，支持主动语音工具",
    "1.7.1",
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
        self.pending_auto_tts_sessions: Set[str] = set()
        self.checked_auto_tts_sessions: Set[str] = set()
        self._keepalive_stop_event = asyncio.Event()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._llm_translation_conflict_logged = False

        # 初始化辅助模块
        plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_genie_tts_llm")
        emotions_file_path = plugin_data_dir / "emotions.json"
        self.emotion_manager = EmotionManager(emotions_file_path)
        self.session_character_bindings = SessionCharacterBindings(
            plugin_data_dir / "session_characters.json"
        )

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
        working_text, tagged_emotion = extract_emotion_directive(working_text)

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
            cleaned_emotion_text, parsed_emotion = extract_emotion_directive(working_text)
            if parsed_emotion:
                tagged_emotion = parsed_emotion
                working_text = cleaned_emotion_text
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
        logger.info(
            f"会话 [{session_id}] 已绑定 Genie 语音角色: {character_name}"
        )
        yield event.plain_result(
            f"本会话语音角色已绑定为: {character_name}。AstrBot 重启后仍会保留。"
        )

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
            text(string): 要合成为语音并直接发给用户的文本。必须是完整句子，句末要有标点；如果使用主LLM注入翻译模式，请传入目标语言文本。
            character_name(string): 可选。要使用的角色名；仅在明确知道已注册角色时填写，否则留空沿用当前会话或默认角色。
            emotion_name(string): 可选但建议填写。要使用的情感名；请从当前角色已注册情感中选择，让语音匹配要朗读内容的语气。
        """
        session_id = event.unified_msg_origin
        group_id = self._normalize_group_id(event.message_obj.group_id)
        text = text.strip()

        if self._is_group_blacklisted(group_id):
            return "当前群组已禁用语音功能，不能直接发送 TTS 语音。"
        if not text:
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"

        display_text, tagged_emotion, tagged_translation = self._extract_tool_text_directives(text)
        if not display_text:
            return "要发送的语音文本为空，请先给出一段需要朗读的内容。"
        if not emotion_name and tagged_emotion:
            emotion_name = tagged_emotion

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

        translation_enabled = self.config.get("enable_translation", True)
        translation_workflow = self._get_translation_workflow()

        if translation_enabled and translation_workflow == "provider_translation":
            target_text = await self._translate_text_with_backends(display_text)
        elif tagged_translation:
            target_text = tagged_translation
        else:
            target_text = display_text
        self._log_translation_result(session_id, display_text, target_text)

        if not target_text:
            return "语音发送失败：用于 TTS 的文本准备失败了，请检查翻译配置或日志。"

        output_mode = self._get_llm_tool_tts_output_mode()
        tts_text, plain_text, output_mode = self._prepare_tts_output_segments(
            display_text, output_mode
        )
        if not tts_text:
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
            full_display_text=display_text,
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

        if self.config.get("enable_translation_debug_log", False):
            logger.info(
                "Provider自动情感原始结果: "
                f"{self._preview_log_text(backend_result)}"
            )

        translated_text, parsed_emotion = parse_provider_emotion_result(
            backend_result, emotion_names
        )
        return translated_text, parsed_emotion

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
            # 与实际合成共用同一角色解析，确保会话级角色绑定参与情感列表注入。
            char_name, _, _ = self._resolve_tts_profile(session_id)

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

        configured_llm_emotion = self._should_inject_llm_emotion_tags()
        configured_llm_translation = self._should_inject_llm_translation_tags()
        original_text, injected_emotion, injected_translation, stripped_directives = (
            self._strip_llm_tts_directives(
                original_text,
                strip_translation=configured_llm_translation,
                strip_emotion=configured_llm_emotion or "[emotion=" in original_text,
            )
        )
        if stripped_directives:
            resp.completion_text = original_text.strip()
            resp.result_chain.chain = [Comp.Plain(resp.completion_text)]

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

        audio_path: Optional[str] = None
        target_emotion = None
        emotion_source = ""
        target_text = None
        char_name = None

        # 确定角色：与工具调用、Prompt 注入共用同一解析逻辑。
        char_name, _, _ = self._resolve_tts_profile(session_id)
        session_setting = self.session_emotions.get(session_id)
        if session_id not in self.w_active_sessions and not injected_emotion:
            target_emotion = (
                session_setting["emotion"]
                if session_setting
                else self.config.get("default_emotion_name")
            )
            emotion_source = "会话固定情感" if session_setting else "默认情感"

        if not char_name or not self.emotion_manager.character_exists(char_name):
            resp.result_chain.chain.append(
                Comp.Plain(f"\n(TTS失败: 角色'{char_name}'无效)")
            )
            return

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
                if session_id in self.w_active_sessions and not target_emotion:
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
                logger.warning(
                    f"[{session_id}] 本轮自动TTS已触发，但主LLM没有返回 $...$ 翻译标签，"
                    "已跳过语音合成并保留原文本回复。"
                )
                return
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
            elif self.config.get("enable_translation", True) and translation_workflow == "provider_translation":
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
            emotion_source = "默认情感兜底"

        emotion_data = self.emotion_manager.get_emotion_data(char_name, target_emotion)
        if not emotion_data:
            # 尝试回落到默认情感
            invalid_emotion = target_emotion
            default_emotion = self.config.get("default_emotion_name")
            emotion_data = self.emotion_manager.get_emotion_data(
                char_name, default_emotion
            )
            if not emotion_data:
                resp.result_chain.chain.append(
                    Comp.Plain(f"\n(TTS失败: 情感'{target_emotion}'无效)")
                )
                return
            target_emotion = default_emotion
            emotion_source = f"无效情感'{invalid_emotion}'回落默认"
            logger.warning(
                f"[{session_id}] 自动TTS情感无效，已回落默认情感: "
                f"{char_name} - {target_emotion}（原情感: {invalid_emotion}）"
            )

        logger.info(
            f"[{session_id}] 自动TTS情感选择 | 角色: {char_name} | "
            f"情感: {target_emotion} | 来源: {emotion_source or '未标记'} | "
            f"参考音频: {emotion_data.get('ref_audio_path')}"
        )

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

        await self.tts_engine.terminate()
        await self.http_client.aclose()
        logger.info("LLM TTS 插件已卸载，HTTP客户端已关闭。")
