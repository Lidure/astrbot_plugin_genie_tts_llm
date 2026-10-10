import asyncio, json, sys, types, wave, io, os, shutil
from pathlib import Path

# 让 "from . import audio_compat" 能在裸测试里跑起来：造一个假包。
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
pkg = types.ModuleType("gpkg")
pkg.__path__ = [str(ROOT)]
sys.modules["gpkg"] = pkg
import importlib
audio_compat = importlib.import_module("gpkg.audio_compat")
voice_vault = importlib.import_module("gpkg.voice_vault")

TMP = ROOT.parent / "_vault_test"
if TMP.exists():
    shutil.rmtree(TMP)
TMP.mkdir(parents=True)

def make_wav(path, seconds=1.0, rate=24000):
    frames = int(rate * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(rate)
        wf.writeframes(b"\x01\x00" * frames)
    return path

a = make_wav(TMP / "a.wav", 1.5)
b = make_wav(TMP / "b.wav", 0.4)

assert audio_compat.sniff_format(a) == "wav", audio_compat.sniff_format(a)
info = audio_compat.probe_wav(a)
assert info and abs(info["duration_ms"] - 1500) <= 2, info
assert audio_compat.probe_duration_ms(b) in range(395, 406)
assert audio_compat.format_duration(1500) == "1.5s"
assert audio_compat.format_duration(95000) == "1:35", audio_compat.format_duration(95000)
assert audio_compat.format_size(2048) == "2.0 KB", audio_compat.format_size(2048)
assert len(audio_compat.sha256_file(a)) == 64
print("audio_compat ok")

async def main():
    vault = voice_vault.VoiceVault(TMP / "vault", limit=3)
    await vault.load()
    assert vault.count() == 0
    r1 = await vault.add(a, alias="打招呼", character="爱乃", emotion="开心", text="早上好呀", session_id="s1")
    assert not r1["duplicate"] and r1["entry"]["duration_ms"] > 1400, r1
    r2 = await vault.add(a, alias="打招呼")  # 同文件 -> 去重
    assert r2["duplicate"], r2
    r3 = await vault.add(b, alias="嗯", character="爱乃", emotion="平静", text="嗯。")
    assert not r3["duplicate"]
    assert vault.count() == 2
    rows = vault.search()
    assert [x["index"] for x in rows] == [1, 2]
    assert rows[0]["alias"] == "嗯", rows[0]
    e, err = vault.resolve("1")
    assert e and e["alias"] == "嗯", (e, err)
    e, err = vault.resolve("打招呼")
    assert e and e["character"] == "爱乃", (e, err)
    e, err = vault.resolve("不存在")
    assert e is None and "没找到" in err, err
    e, err = vault.resolve("9")
    assert e is None and "超出范围" in err, err
    # 关键词搜索
    assert len(vault.search(keyword="早上")) == 1
    assert len(vault.search(character="爱乃")) == 2
    # 改名唯一化
    ent = await vault.rename(rows[0]["id"], "打招呼")
    assert ent["alias"] == "打招呼-2", ent
    # 置顶 + 容量淘汰
    await vault.update(ent["id"], pinned=True)
    for i in range(4):
        p = make_wav(TMP / f"c{i}.wav", 0.2 + i * 0.05)
        await vault.add(p, alias=f"c{i}")
    assert vault.count() == 3, vault.count()
    ids = {x["id"] for x in vault.search()}
    assert ent["id"] in ids, "置顶条目不该被淘汰"
    assert vault.search()[0]["id"] == ent["id"], "置顶应排最前"
    # 播放计数
    await vault.touch(ent["id"])
    assert vault.get(ent["id"])["play_count"] == 1
    # 导出 / 导入
    bundle = TMP / "out.zip"
    rep = await vault.export_bundle(bundle)
    assert rep["count"] == 3, rep
    v2 = voice_vault.VoiceVault(TMP / "vault2", limit=50)
    await v2.load()
    ir = await v2.import_bundle(bundle, "merge")
    assert ir["added"] == 3 and ir["total"] == 3, ir
    assert v2.get(ent["id"])["alias"] == "打招呼-2"
    ir2 = await v2.import_bundle(bundle, "merge")
    assert ir2["skipped"] == 3 and ir2["added"] == 0, ir2
    ir3 = await v2.import_bundle(bundle, "overwrite")
    assert ir3["updated"] == 3, ir3
    ir4 = await v2.import_bundle(bundle, "replace")
    assert ir4["added"] == 3 and ir4["removed"] == 3, ir4
    print("import report:", voice_vault.describe_import(ir4))
    # 索引损坏后从目录重建
    (TMP / "vault2" / "index.json").write_text("{ broken", encoding="utf-8")
    v3 = voice_vault.VoiceVault(TMP / "vault2", limit=50)
    await v3.load()
    assert v3.count() == 3, v3.count()
    assert all(x["source"] == "plugin" for x in v3.search())
    # 清空保留置顶
    n = await v2.clear(keep_pinned=True)
    print("cleared", n, "left", v2.count())
    # 同一秒连收藏多条：顺序必须稳定（旧实现会退到随机 uuid 上抛硬币）
    v4 = voice_vault.VoiceVault(TMP / "vault4", limit=50)
    await v4.load()
    for i in range(6):
        p = make_wav(TMP / f"s{i}.wav", 0.1 + i * 0.01)
        await v4.add(p, alias=f"s{i}", text=f"第{i}条")
    order = [x["alias"] for x in v4.search()]
    assert order == ["s5", "s4", "s3", "s2", "s1", "s0"], order
    seqs = [int(x["seq"]) for x in v4.search()]
    assert seqs == sorted(seqs, reverse=True) and min(seqs) >= 1, seqs
    same_second = len({int(x["created_at"]) for x in v4.search()}) == 1
    for _ in range(4):
        v5 = voice_vault.VoiceVault(TMP / "vault4", limit=50)
        await v5.load()
        assert [x["alias"] for x in v5.search()] == order, (
            "重载后顺序变了",
            [x["alias"] for x in v5.search()],
        )
    print("stable order ok (same_second=%s):" % same_second, " ".join(order))
    # stats
    st = vault.stats()
    assert st["count"] == 3 and st["limit"] == 3, st
    print("stats:", json.dumps(st, ensure_ascii=False))
    print("voice_vault ok")

asyncio.run(main())
shutil.rmtree(TMP)
print("ALL OK")
