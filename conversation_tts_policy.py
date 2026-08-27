from typing import Iterable, Mapping, Optional, Set


def is_private_tts_active(
    *,
    enable_by_default: bool,
    session_id: str,
    active_sessions: Set[str],
    w_active_sessions: Set[str],
    inactive_private_sessions: Set[str],
) -> bool:
    """Return whether automatic TTS is active for one private session."""
    if session_id in inactive_private_sessions:
        return False
    if session_id in active_sessions or session_id in w_active_sessions:
        return True
    return bool(enable_by_default)


def parse_character_default_emotions(values: object) -> dict[str, str]:
    """Parse `character=emotion` entries from plugin configuration."""
    if not isinstance(values, (list, tuple, set)):
        return {}

    parsed: dict[str, str] = {}
    for item in values:
        text = str(item or "").strip()
        if not text or "=" not in text:
            continue
        character, emotion = text.split("=", 1)
        character = character.strip()
        emotion = emotion.strip()
        if character and emotion:
            parsed[character] = emotion
    return parsed


def resolve_default_emotion(
    character_name: str,
    emotion_names: Iterable[str],
    global_default_emotion: Optional[str],
    character_defaults: Mapping[str, str],
) -> Optional[str]:
    """Resolve a safe default emotion for the selected character."""
    available = [str(name).strip() for name in emotion_names if str(name).strip()]
    if not available:
        return None

    character_default = str(character_defaults.get(character_name, "") or "").strip()
    if character_default in available:
        return character_default

    global_default = str(global_default_emotion or "").strip()
    if global_default in available:
        return global_default

    return available[0]
