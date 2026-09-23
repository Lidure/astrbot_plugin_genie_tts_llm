"""Resolve automatic TTS state for both message hooks and the management UI."""

from typing import Any, Dict, List


MAX_KNOWN_SESSIONS = 500


def group_ids(value: Any) -> set:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item or "").strip()}


def group_state(plugin: Any, group_id: str) -> Dict[str, Any]:
    group_id = str(group_id or "")
    config = plugin.config
    if not group_id:
        return {"enabled": False, "blocked": False, "sources": []}
    if group_id in group_ids(config.get("group_blacklist", [])):
        return {"enabled": False, "blocked": True, "sources": ["配置群黑名单"]}
    if group_id in getattr(plugin, "inactive_groups", set()):
        return {"enabled": False, "blocked": False, "sources": ["群单独关闭"]}
    sources: List[str] = []
    if group_id in group_ids(config.get("group_whitelist", [])):
        sources.append("配置群白名单")
    if group_id in getattr(plugin, "active_groups", set()):
        sources.append("已保存群开关")
    if config.get("enable_group_tts_by_default", False):
        sources.append("配置全部群默认开启")
    return {"enabled": bool(sources), "blocked": False, "sources": sources or ["群未开启"]}


def effective_state(plugin: Any, session_id: str, group_id: str = "") -> Dict[str, Any]:
    group = group_state(plugin, group_id)
    if group["blocked"]:
        return {**group, "mode": "off"}
    sources = []
    mode = "off"
    if session_id in getattr(plugin, "w_active_sessions", set()):
        sources.append("会话自动情感 /tts-w")
        mode = "w"
    elif session_id in getattr(plugin, "active_sessions", set()):
        sources.append("会话普通配音 /tts-llm")
        mode = "tts"
    if group["enabled"]:
        sources.extend(group["sources"])
        if mode == "off":
            mode = "tts"
    return {
        "enabled": mode != "off", "blocked": False, "mode": mode,
        "sources": sources or group["sources"] or ["会话未开启"],
    }


def restore_known_sessions(value: Any) -> Dict[str, Dict[str, Any]]:
    """Keep only bounded event metadata, never message text or audio paths."""
    rows: Dict[str, Dict[str, Any]] = {}
    if not isinstance(value, dict):
        return rows
    for sid, row in value.items():
        if not isinstance(sid, str) or not sid or not isinstance(row, dict):
            continue
        try:
            seen = max(0, int(row.get("last_seen", 0)))
        except (ValueError, TypeError, OverflowError):
            seen = 0
        rows[sid] = {"group_id": str(row.get("group_id") or ""), "last_seen": seen}
    return dict(sorted(rows.items(), key=lambda item: item[1]["last_seen"])[-MAX_KNOWN_SESSIONS:])


def session_payload(plugin: Any) -> Dict[str, Any]:
    known = getattr(plugin, "known_sessions", {})
    normal = getattr(plugin, "active_sessions", set())
    automatic = getattr(plugin, "w_active_sessions", set())
    profiles = getattr(plugin, "session_emotions", {})
    w_profiles = getattr(plugin, "session_w_settings", {})
    ids = set(known) | set(normal) | set(automatic) | set(profiles) | set(w_profiles)
    rows = []
    for sid in sorted(ids, key=lambda s: (-known.get(s, {}).get("last_seen", 0), s)):
        meta = known.get(sid, {})
        gid = meta.get("group_id", "")
        status = effective_state(plugin, sid, gid)
        profile, w_profile = profiles.get(sid, {}), w_profiles.get(sid, {})
        rows.append({
            "session_id": sid, "group_id": gid, "last_seen": meta.get("last_seen", 0),
            "observed": sid in known, "tts_active": sid in normal, "w_active": sid in automatic,
            "effective": status,
            "character": profile.get("character", ""), "emotion": profile.get("emotion", ""),
            "w_character": w_profile.get("character", ""), "w_emotion": w_profile.get("emotion", ""),
            "has_last_audio": bool(getattr(plugin, "has_sent_voice", lambda _: False)(sid)),
            "sent_voices": getattr(plugin, "sent_voice_count", lambda _: 0)(sid),
        })
    whitelist = group_ids(plugin.config.get("group_whitelist", []))
    blacklist = group_ids(plugin.config.get("group_blacklist", []))
    explicit_on = set(getattr(plugin, "active_groups", set()))
    explicit_off = set(getattr(plugin, "inactive_groups", set()))
    gids = whitelist | blacklist | explicit_on | explicit_off | {r["group_id"] for r in rows if r["group_id"]}
    groups = [{"group_id": gid, **group_state(plugin, gid)} for gid in sorted(gids)]
    return {
        "sessions": rows, "group_rules": groups,
        "groups": {"active": sorted(whitelist | explicit_on), "inactive": sorted(explicit_off),
                   "whitelist": sorted(whitelist), "blacklist": sorted(blacklist)},
        "persistence": bool(plugin.config.get("enable_state_persistence", True)),
        "group_default": bool(plugin.config.get("enable_group_tts_by_default", False)),
        "known_limit": MAX_KNOWN_SESSIONS,
        "counts": {"known": len(rows), "effective": sum(r["effective"]["enabled"] for r in rows),
                   "groups_enabled": sum(g["enabled"] for g in groups)},
    }
