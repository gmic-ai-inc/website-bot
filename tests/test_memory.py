"""Memory-management sanity checks — no API keys required. Run: python tests/test_memory.py"""
import os
os.environ["MAX_TURNS_IN_MEMORY"] = "4"
os.environ["MAX_SESSIONS"] = "3"
os.environ["SESSION_TTL_SECONDS"] = "1800"
os.environ["LLM_HISTORY_TURNS"] = "3"

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core import sessions  # noqa: E402


def main():
    S = sessions.SessionStore()
    ok = True

    S.get_or_create("u1", {"page_url": "/"})
    for i in range(6):
        S.append_turn("u1", "user", f"m{i}")
    turns = [t["text"] for t in S.snapshot("u1")["turns"]]
    ok &= _check("turn cap keeps newest 4", turns == ["m2", "m3", "m4", "m5"], turns)

    win = [t["text"] for t in S.window("u1")]
    ok &= _check("sliding window = last 3", win == ["m3", "m4", "m5"], win)

    S.set_entry_intent("u1", "odm")
    # 必填 = need + 一种联系方式。先只给 need → 应还缺 contact。
    S.update_lead("u1", {"need": "recorder"})
    ok &= _check("missing=contact when only need given",
                 S.snapshot("u1")["lead"]["missing"] == ["contact"], S.snapshot("u1")["lead"])
    # 再补上 email → need + contact 都齐 → missing 应为空(name 不是必填,不进 missing)。
    S.update_lead("u1", {"email": "a@x.com"})
    lead = S.snapshot("u1")["lead"]
    ok &= _check("missing empty once need+contact present", lead["missing"] == [] and lead["email"] == "a@x.com", lead)

    S.set_entry_intent("u1", "products")
    # entry_intents 现在是【累积列表】:按到达顺序追加,第一个(odm)为主归因、不被冲掉。
    ok &= _check("entry_intents accumulate in arrival order",
                 S.snapshot("u1")["entry_intents"] == ["odm", "products"], S.snapshot("u1")["entry_intents"])
    S.set_entry_intent("u1", "odm")   # 重复触发同一入口 → 去重,不再追加
    ok &= _check("entry_intents dedupe repeat entry",
                 S.snapshot("u1")["entry_intents"] == ["odm", "products"], S.snapshot("u1")["entry_intents"])

    # ---- 问卷答案【按 Tab 分桶】(多份问卷并存)+ 推荐【按 Tab 分桶】----
    S.set_questionnaire("u1", "odm", {"product": "Recorder / microphone"}, None)
    S.set_questionnaire("u1", "help-me-choose", {"usage": "To answer phone calls"},
                        {"products": ["Telalive"], "link": "https://gmic.ai/telalive/", "hint": "call-answering"})
    snap = S.snapshot("u1")
    ok &= _check("answers bucketed per tab (both kept)",
                 set(snap["answers"]) == {"odm", "help-me-choose"}
                 and snap["answers"]["odm"] == {"product": "Recorder / microphone"}, snap["answers"])
    ok &= _check("recommendation stored only for tabs that produce one",
                 list(snap["recommendations"]) == ["help-me-choose"], snap["recommendations"])
    S.set_questionnaire("u1", "odm", {"product": "Speaker or headset"}, None)   # 同 Tab 再提交 → 覆盖该桶
    ok &= _check("re-submit same tab overwrites only its bucket",
                 S.snapshot("u1")["answers"]["odm"] == {"product": "Speaker or headset"}
                 and set(S.snapshot("u1")["answers"]) == {"odm", "help-me-choose"},
                 S.snapshot("u1")["answers"])

    # ---- messengers(列表联系方式,按平台去重、同平台留最新)----
    # 只给一个 IM(没 email/phone)→ 也算有联系方式 → missing 不含 contact。
    S.get_or_create("m1")
    S.update_lead("m1", {"need": "mic", "messengers": ["WeChat: wx_old"]})
    lead = S.snapshot("m1")["lead"]
    ok &= _check("messenger alone satisfies contact",
                 lead["missing"] == [] and lead["messengers"] == ["WeChat: wx_old"], lead)
    # 补一个【不同平台】→ 并集,两个都留。
    S.update_lead("m1", {"messengers": ["WhatsApp: +1 (669) 900-0008"]})
    plats = {sessions.messenger_platform(m) for m in S.snapshot("m1")["lead"]["messengers"]}
    ok &= _check("different platform kept (union)", plats == {"wechat", "whatsapp"},
                 S.snapshot("m1")["lead"]["messengers"])
    # 【同平台】再报(纠错)→ 覆盖旧的,只留最新;总数不变(仍是 wechat+whatsapp 两条)。
    S.update_lead("m1", {"messengers": ["WeChat: wx_new"]})
    msgr = S.snapshot("m1")["lead"]["messengers"]
    wx = [m for m in msgr if sessions.messenger_platform(m) == "wechat"]
    ok &= _check("same platform keeps latest only", wx == ["WeChat: wx_new"] and len(msgr) == 2, msgr)

    # ---- 甩直连链接的两个匹配器(不碰 LLM/Slack)----
    from core import widget_config as WC
    # 路A:用户留自己的号 → match_our_channels(按平台匹配我们有入口的渠道,Line 不甩)
    hit = [c["id"] for c in WC.match_our_channels(["WhatsApp: +1..", "Line: abc", "WhatsApp: +1.."])]
    ok &= _check("match_our_channels: WA hit, Line skip, dedup", hit == ["whatsapp"], hit)
    # 路B:用户问起我们渠道 → contact_for_channel(口头说法归一到 contacts id)。
    # 注:只打印 id(contacts 配置里含 emoji 图标,直接 print 整个 dict 会在 Windows GBK 控制台炸)。
    ok &= _check("contact_for_channel: phone->call", (WC.contact_for_channel("phone") or {}).get("id") == "call",
                 (WC.contact_for_channel("phone") or {}).get("id"))
    ok &= _check("contact_for_channel: unknown->None", WC.contact_for_channel("line") is None and WC.contact_for_channel("") is None,
                 "line->None, ''->None")

    # ---- recommend_for:多选题(musthave 列表)参与选型 ----
    # 选了"防水"(多选列表)→ 命中更具体的 desk+waterproof 规则 → 只推 SPK01(IPX7)。
    rec = WC.recommend_for({"usage": "On a desk / in a room",
                            "musthave": ["Long battery life", "Rugged / waterproof"]})
    ok &= _check("multi-select musthave narrows desk → SPK01", rec.get("products") == ["HA-SPK01"], rec.get("products"))
    # 没选防水 → 落回宽泛 desk 规则 → SPK01+SPK03(证明多选"包含"匹配没误伤单选路径)。
    rec = WC.recommend_for({"usage": "On a desk / in a room", "musthave": ["Long battery life"]})
    ok &= _check("desk without waterproof → general rule",
                 rec.get("products") == ["HA-SPK01", "HA-SPK03"], rec.get("products"))

    # ---- 推荐链接改成【列表】(一条推荐可能对应多个型号详情页)----
    # 穿戴 + 诊疗 → 推 MIC06A/MIC05 两款 → 必须两个详情页链接都给(改动前只有一个 link,
    # 推荐里第一款写 MIC06A、链接却指向 MIC05 页,客户点进去得自己找)。
    rec = WC.recommend_for({"usage": "Worn hands-free (badge, clip, lanyard)", "where": "Clinic / healthcare"})
    urls = [l["url"] for l in rec.get("links") or []]
    ok &= _check("wearable+clinic → MIC06 & MIC05 两个链接",
                 any("mic06" in u for u in urls) and any("mic05" in u for u in urls), urls)
    ok &= _check("旧的单值 link 字段已彻底移除", "link" not in rec, sorted(rec.keys()))

    # ---- 行业落地页:按"在哪用/什么行业"那题的选项映射(按值查,不认题 id)----
    ind = [l["url"] for l in WC.industry_links_for({"where": "Clinic / healthcare"})]
    ok &= _check("industry_links: 诊疗 → healthcare 页", len(ind) == 1 and "healthcare" in ind[0], ind)
    ok &= _check("industry_links: odm 的 industry 题同样命中",
                 len(WC.industry_links_for({"industry": "Field service / on-site work"})) == 1,
                 WC.industry_links_for({"industry": "Field service / on-site work"}))
    ok &= _check("industry_links: 官网没有对应行业页就不硬凑",
                 WC.industry_links_for({"where": "Retail / front desk"}) == [], "Retail → []")

    # ---- MOQ:低于起订量的量级要能被【确定性】判出来(不让模型比大小)----
    ok &= _check("below_moq: Prototype / under 500 → True", WC.below_moq({"add-branding": {"qty": "Prototype / under 500"}}) is True, True)
    ok &= _check("below_moq: 2,000 – 10,000 → False", WC.below_moq({"odm": {"qty": "2,000 – 10,000"}}) is False, False)
    ok &= _check("below_moq: 任一 Tab 透露过小量级就守住口径",
                 WC.below_moq({"odm": {"qty": "10,000+"}, "add-branding": {"qty": "500 – 2,000"}}) is True, True)
    ok &= _check("below_moq: 没做过问卷 → False", WC.below_moq(None) is False, False)
    # MOQ 口径必须真的拼进提示词(以前 moq_note 是死配置、从没注入,bot 才会对小批量说"完美契合")
    from ai import prompts
    ml = prompts.moq_line({"note": WC.MOQ_NOTE, "below": True})
    ok &= _check("moq_line: 低于起订量时给出硬约束",
                 "2,000" in ml and "great fit" in ml and "BELOW" in ml, len(ml))
    ok &= _check("moq_line: 量级够时只给口径、不提门槛",
                 "great fit" not in prompts.moq_line({"note": WC.MOQ_NOTE, "below": False}), "no warning")
    ok &= _check("moq_line: 没配 note → 整块不放", prompts.moq_line({"note": "", "below": True}) == "", '""')

    # ---- 归因题("你从哪知道我们的"):固定选项不信前端传的文字、Other 收自由文本并截断 ----
    ok &= _check("source_value: 固定选项取配置里的 label",
                 WC.source_value("linkedin", "前端乱传的东西") == "LinkedIn", WC.source_value("linkedin", "x"))
    ok &= _check("source_value: Other 收自由文本", WC.source_value("other", "a colleague") == "Other: a colleague",
                 WC.source_value("other", "a colleague"))
    ok &= _check("source_value: Other 留空也算答了", WC.source_value("other", "") == "Other", WC.source_value("other", ""))
    ok &= _check("source_value: 自由文本截断到上限",
                 len(WC.source_value("other", "x" * 500)) == len("Other: ") + WC.SOURCE_TEXT_MAX,
                 len(WC.source_value("other", "x" * 500)))
    ok &= _check("source_value: 未知选项 id → None", WC.source_value("wechat-moments", "x") is None, None)

    # 会话里的 source / source_asked:非必填(不进 missing)、只问一次、非空才覆盖
    S.get_or_create("src1")
    ok &= _check("source 初始为空且未问过",
                 S.snapshot("src1")["source"] is None and S.snapshot("src1")["source_asked"] is False, "None/False")
    S.mark_source_asked("src1")
    ok &= _check("mark_source_asked 只打标记、不写值",
                 S.snapshot("src1")["source_asked"] is True and S.snapshot("src1")["source"] is None, "True/None")
    S.set_source("src1", "LinkedIn")
    ok &= _check("set_source 写值", S.snapshot("src1")["source"] == "LinkedIn", S.snapshot("src1")["source"])
    S.set_source("src1", "")
    ok &= _check("set_source 空值不冲掉已有", S.snapshot("src1")["source"] == "LinkedIn", S.snapshot("src1")["source"])
    S.update_lead("src1", {"need": "recorder", "email": "a@b.com"})
    ok &= _check("source 不进 missing(非必填)", S.snapshot("src1")["lead"]["missing"] == [],
                 S.snapshot("src1")["lead"]["missing"])
    ok &= _check("source 不混进 lead(免被 LLM 每轮重抽覆盖)", "source" not in S.snapshot("src1")["lead"],
                 sorted(S.snapshot("src1")["lead"].keys()))

    S.get_or_create("u2"); S.get_or_create("u3"); S.get_or_create("u4")
    ok &= _check("LRU evicts u1", S.snapshot("u1") is None and S.snapshot("u4") is not None, S.stats())

    # backdate u2 past the TTL (>1800s idle); fresh u3/u4 should survive the sweep
    S._data["u2"]["last_seen"] -= 4000
    n = S.sweep_expired()
    ok &= _check("TTL sweep drops idle only", S.snapshot("u2") is None and S.snapshot("u4") is not None and n == 1, f"evicted={n}")

    print("\nALL PASS" if ok else "\nSOME FAILED")
    sys.exit(0 if ok else 1)


def _check(name, cond, detail):
    print(f"[{'OK' if cond else 'FAIL'}] {name}: {detail}")
    return cond


if __name__ == "__main__":
    main()
