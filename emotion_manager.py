import json
import os
import tempfile
import time
from typing import Dict, Optional

from astrbot.api import logger


class EmotionManager:
    """处理所有与感情数据相关的加载、保存和管理逻辑"""

    def __init__(self, file_path):
        """
        初始化感情管理器。
        :param file_path: emotions.json 文件的路径。
        """
        self.file_path = file_path
        self.emotions_data: Dict = self._load_emotions_from_file()

    def _dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.file_path))

    def _backup_broken_file(self) -> str:
        """备份解析失败的感情文件，避免后续写入把用户数据彻底覆盖掉。

        同样内容只留一份：文件一直坏着的话，每次重启都存一份会把数据目录塞满。
        """
        try:
            with open(self.file_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            logger.warning(f"备份损坏的感情文件失败: {exc}")
            return ""

        directory = self._dir()
        try:
            for existing in sorted(os.listdir(directory)):
                if not existing.startswith("emotions.corrupt-"):
                    continue
                if not existing.endswith(".json"):
                    continue
                path = os.path.join(directory, existing)
                if os.path.getsize(path) != len(raw):
                    continue
                with open(path, "rb") as handle:
                    if handle.read() == raw:
                        return path
        except OSError:
            pass

        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = os.path.join(directory, f"emotions.corrupt-{stamp}.json")
        try:
            with open(target, "wb") as handle:
                handle.write(raw)
            return target
        except OSError as exc:
            logger.warning(f"备份损坏的感情文件失败: {exc}")
            return ""

    def _load_emotions_from_file(self) -> Dict:
        """从JSON文件加载感情数据"""
        if not os.path.exists(self.file_path):
            try:
                os.makedirs(self._dir(), exist_ok=True)
                with open(self.file_path, "w", encoding="utf-8") as f:
                    json.dump({}, f)
            except OSError as exc:
                logger.error(f"创建感情文件失败: {exc}")
            return {}
        try:
            # utf-8-sig：容忍用记事本另存出来的 BOM，否则整库会被判定为损坏。
            with open(self.file_path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            backup = self._backup_broken_file()
            hint = f"，已备份为 {os.path.basename(backup)}" if backup else ""
            logger.error(
                f"加载感情文件失败: {e}{hint}。本次以空感情库启动，"
                "修好文件后可用 /感情包恢复 或 WebUI 工作台重新导入。"
            )
            return {}

        if not isinstance(data, dict):
            backup = self._backup_broken_file()
            hint = f"，已备份为 {os.path.basename(backup)}" if backup else ""
            logger.error(f"感情文件顶层不是对象（{type(data).__name__}）{hint}。")
            return {}

        try:
            total = sum(len(v) for v in data.values() if isinstance(v, dict))
        except Exception:
            total = 0
        logger.info(f"成功从 {self.file_path} 加载 {total} 个感情配置。")
        return data

    def _save_emotions_to_file(self) -> bool:
        """将当前感情数据保存到JSON文件。

        先写同目录临时文件再 os.replace 原子替换：磁盘满、进程被杀、导入到一半
        出错时，emotions.json 要么是旧内容要么是新内容，不会留下半个 JSON。
        """
        tmp_path = ""
        try:
            directory = self._dir()
            os.makedirs(directory, exist_ok=True)
            payload = json.dumps(self.emotions_data, ensure_ascii=False, indent=4)
            fd, tmp_path = tempfile.mkstemp(
                prefix=".emotions-", suffix=".tmp", dir=directory
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.file_path)
            tmp_path = ""
            return True
        except (OSError, TypeError, ValueError) as e:
            logger.error(f"保存感情文件失败: {e}")
            return False
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def reload(self):
        """从文件重新加载数据，用于在保存失败时恢复状态"""
        self.emotions_data = self._load_emotions_from_file()

    def get_emotion_data(
        self, character_name: str, emotion_name: str
    ) -> Optional[Dict]:
        """获取指定角色和感情的数据"""
        return self.emotions_data.get(character_name, {}).get(emotion_name)

    def character_exists(self, character_name: str) -> bool:
        """检查角色是否存在"""
        return character_name in self.emotions_data

    def register_emotion(
        self,
        character_name: str,
        emotion_name: str,
        ref_audio_path: str,
        ref_audio_text: str,
        language: str = None,
    ) -> bool:
        """注册一个新的感情并保存"""
        if character_name not in self.emotions_data:
            self.emotions_data[character_name] = {}

        data = {
            "ref_audio_path": ref_audio_path,
            "ref_audio_text": ref_audio_text,
        }
        if language:
            data["language"] = language

        self.emotions_data[character_name][emotion_name] = data
        return self._save_emotions_to_file()

    def delete_emotion(self, character_name: str, emotion_name: str) -> bool:
        """删除一个已注册的感情并保存"""
        if not self.get_emotion_data(character_name, emotion_name):
            return True  # 如果不存在，也视为成功

        del self.emotions_data[character_name][emotion_name]
        if not self.emotions_data[character_name]:
            del self.emotions_data[character_name]

        return self._save_emotions_to_file()
