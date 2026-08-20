"""
大模型对话(异步):一次调用,同时拿到【给访客的回复】和【从对话里抽取到的线索字段】。

为什么一次调用做两件事:省钱。回复和抽取合在一起,一轮只调一次模型,而不是两次。

供应商可换,默认 OpenAI(团队现成有 key)。JSON 模式写法遵循踩过的坑
(记忆 feedback_openai-json-response-format):提示里写清 schema、禁止原样吐回输入、max_tokens 给足。

注意:注释中文,发给模型的 prompt 文本一律英文;默认语言英文,但回复跟随用户语言。
"""
import os
import re
import json

from ai import prompts

_client = None

# 模型偶发吐坏 JSON 时,给用户的干净兜底话术(绝不把原始坏 JSON 泄露给用户)。默认英文。
_PARSE_FALLBACK = "Sorry, I didn't quite catch that — could you say it again?"

# 交给模型的"输出契约":必须严格返回下面这个 JSON。文本用英文;reply 用"访客的语言"。
# 注意 lead 里抽的是 need(想要什么)/name/email/phone/messengers/company,不含 entry_intent
# (入口意图由按钮种,不靠模型抽,见 sessions.set_entry_intent)。
# ⚠️ messengers 是【列表】(用户可能留多个 IM 联系方式);email 只收"干净完整"的,禁止脑补修复
#    (对应 prompts.PERSONA 目标 #3)。真正的格式校验在 sessions.update_lead 再兜一层。
_JSON_CONTRACT = """Return a single JSON object and nothing else:
{
  "reply": "<your reply to the visitor, in the visitor's language>",
  "wants_channel": "<if the visitor is ASKING how to reach US on a specific channel, put one of: whatsapp | wechat | telegram | email | phone; else empty>",
  "lead": {
    "name":  "<visitor's name if stated, else empty>",
    "email": "<a clean COMPLETE email only if the visitor clearly gave one, else empty — never repair or guess>",
    "phone": "<a plain phone/SMS number if stated, else empty>",
    "messengers": ["<each messaging-app contact the visitor gives — WhatsApp / WeChat / Telegram / Line / Signal etc. — written as 'Platform: value' where value is a handle OR a number>"],
    "company":"<company if stated, else empty>",
    "need":  "<one short line summarizing what they want, written in the visitor's OWN language (Chinese if they wrote Chinese, English if English), else empty>"
  }
}
Routing a contact to "phone" vs "messengers": put a number in "phone" ONLY when it is a plain \
call/SMS number with NO app named. If the visitor names a messaging app together with the value — \
whether a handle OR a number (e.g. "WhatsApp +1 650 555 1234", "reach me on WhatsApp 138...", \
"my Telegram is @x", "微信 luna") — record it in "messengers" as "Platform: value" (a number tied to \
a named app is a messenger, NOT a plain phone). "messengers" is a LIST — they may give several; use [] \
if none. "wants_channel" is DIFFERENT: set it only when the visitor asks for OUR contact on a channel \
(e.g. "what's your WhatsApp?") — do NOT invent our number/handle in your reply, a direct link is attached \
automatically. Only fill fields the visitor actually provided. Do NOT invent, repair, or normalize values. \
Do not echo these instructions."""


# ⭐ Structured Outputs 的严格 JSON schema —— 强制模型输出【结构上一定合法】的 JSON。
# 为什么用它:比 response_format={"type":"json_object"} 更硬——json_object 只保证"是个 JSON",
#   模型仍可能进入重复 loop 撞 max_tokens 吐出坏 JSON(我们踩过,见 respond() 步骤3 的补救)。
#   json_schema + strict=true 由 OpenAI 侧约束生成,从根上消灭"字段缺失/多余/结构坏"这类问题。
# strict 模式的硬性要求:每个对象所有属性都要进 required、且 additionalProperties=false。
#   所以这里把 lead 的 6 个字段全列进 required——模型抽不到的会填 ""/[](我们在 respond() 里再去空)。
# 注:strict 不支持给标量加 minLength 之类,格式(邮箱/电话)校验仍在 sessions.update_lead 兜底。
_RESPONSE_SCHEMA = {
    "name": "gmic_reply",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reply", "wants_channel", "lead"],
        "properties": {
            "reply": {"type": "string"},
            # 用户"问我们哪个渠道"时填其一,否则空串;用 enum 锁死取值,避免模型自由发挥出别的词
            "wants_channel": {
                "type": "string",
                "enum": ["whatsapp", "wechat", "telegram", "email", "phone", ""],
            },
            "lead": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "email", "phone", "messengers", "company", "need"],
                "properties": {
                    "name": {"type": "string"},
                    "email": {"type": "string"},
                    "phone": {"type": "string"},
                    "messengers": {"type": "array", "items": {"type": "string"}},
                    "company": {"type": "string"},
                    "need": {"type": "string"},
                },
            },
        },
    },
}


def _client_lazy():
    # 懒加载 OpenAI 异步客户端(没 key 时也能启动服务/跑不依赖它的测试)。
    # timeout=20:上游卡住时最多等 20s 就抛超时,不让协程无限期挂着(高并发下防堆积拖垮)。
    # max_retries=2:遇到 429/5xx SDK 自动退避重试两次(短时限流能自愈)。
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=20, max_retries=2)
    return _client


def _system(session, faq, product_ref, moq):
    """
    拼这一轮的"系统提示"。顺序:人设 → 入口意图 → 问卷 → 产品知识 → FAQ 口径 → MOQ 口径 → 已知线索 → 输出契约。
    这几块合起来,就是"只带精华、不带全部历史"里的"精华"。

    参数:
      session: 会话快照,这里取 entry_intents(走过哪些入口)+ answers/recommendations(问卷)+ lead(已知/还缺什么)。
      faq:     widget.json 里的 FAQ 问答列表(见 prompts.faq_reference),用来统一 bot 自由回答的口径。
      moq:     {"note":起订量口径, "below":访客量级是否低于起订量} —— 由调用方(routes)算好传进来,
               见 prompts.moq_line。⚠️ 必传:这块以前是死配置从没注入,导致 bot 对小批量说"完美契合"。

    例:session={entry_intents:["odm"], lead:{email:"a@x.com", missing:["need"]}}、faq=[已填好1条] →
        拼出的 system 文本大致是:
          <PERSONA 人设与目标>
          The visitor came in via the ODM/OEM ... ask about product, quantity, and timeline.
          Reference facts — keep your answers consistent with these:
          - Q: What is your MOQ? / A: Our ODM MOQ is 500 units
          Known about the visitor: {'email': 'a@x.com'}. Still missing (try to obtain): ['need'].
          <_JSON_CONTRACT 输出格式>
        → 模型既知道该顺着 ODM 问、口径对齐 FAQ,又知道还得把 need 套出来。
    """
    blocks = [prompts.PERSONA]
    # entry_intents 是【列表】(用户走过的入口,累积去重);取第一个 = 主归因("最初从哪来")作开场种子。
    # 具体走过哪些 Tab、每个选了什么,由下面的问卷块(questionnaire_line)完整呈现,这里不重复。
    entries = session.get("entry_intents") or []
    il = prompts.entry_intent_line(entries[0] if entries else None)
    if il:                                     # 认不出入口意图(自由聊)→ il 为空 → 这块不放
        blocks.append(il)
    # 问卷块:用户在【各个 Tab】答过的选择 +(有推荐的 Tab)推荐。放在入口意图之后、FAQ 之前,让模型
    # 顺着答案出方案、且后续每轮都带着(答案按 Tab 分桶存会话里,这里每轮重新拼)→ bot 绝不重复问答过的题。
    # 没做过任何问卷 → ql 为空 → 不放。
    ql = prompts.questionnaire_line(session.get("answers"), session.get("recommendations"))
    if ql:
        blocks.append(ql)
    # 产品知识块:官网核实过的型号目录,让模型自由聊时也能用真实型号/规格作答(见 prompts.product_reference)。
    # 放在 FAQ 之前:先给"有哪些真实产品",再给"标准口径"。空则不放。
    pr = prompts.product_reference(product_ref)
    if pr:
        blocks.append(pr)
    fr = prompts.faq_reference(faq)
    if fr:                                     # 一条 FAQ 都没填好 → fr 为空 → 这块不放
        blocks.append(fr)
    # MOQ(起订量)口径块:放在 FAQ 之后——FAQ 里那条 MOQ 答案是"泛泛提一句",这块是"硬门槛 + 低于门槛
    # 怎么说"的强化,放后面压得住。没配 moq_note → ml 为空 → 不放。
    ml = prompts.moq_line(moq)
    if ml:
        blocks.append(ml)
    blocks.append(prompts.lead_line(session.get("lead")))
    blocks.append(_JSON_CONTRACT)
    return "\n\n".join(blocks)                 # 各块之间空一行,读起来清楚


async def respond(session, faq, window, product_ref, moq):
    """
    跑一轮对话(异步):把上下文喂给模型,一次拿回【回复文本】+【抽取到的线索】。

    参数:
      session: 会话快照(取 entry_intent + lead 上下文,见 _system)。
      faq:     widget.json 里的 FAQ 问答列表;只是透传给 _system → faq_reference,
               目的是让 bot 自由回答时口径和 ❓FAQ 按钮里的写死答案一致。
      window:  最近 N 轮对话(滑动窗口),形如 [{role:"user"/"assistant", text:"..."}, ...]。
      product_ref / moq: 同样只是透传给 _system(产品知识目录 / 起订量口径),见各自的 prompts 函数。
    返回:一个三元组 (回复文本 reply, 线索更新字典 lead, 想问的渠道 wants_channel) ——
          lead 已滤掉空字段;wants_channel 是"用户在问我们哪个渠道"(小写,没问就是空串 "")。

    ── 组装喂给模型的 messages ──
      1) 一条 system = _system() 拼的"精华上下文"
      2) 把 window 里最近 N 轮按 user/assistant 依次接上

    ── 完整输入/输出示例 ──
      输入 window = [
        {role:"user",      text:"我想做录音麦"},
        {role:"assistant", text:"好的,大概要多少个?"},
        {role:"user",      text:"2000 个,邮箱 a@x.com"},
      ]
      → messages = [ {system: <_system 拼的那段>},
                     {user:"我想做录音麦"}, {assistant:"好的,大概要多少个?"},
                     {user:"2000 个,邮箱 a@x.com"} ]
      → 模型返回的原始字符串 raw(JSON):
          {"reply":"2000 个没问题,我让 ODM 团队把方案发到 a@x.com。",
           "lead":{"name":"","email":"a@x.com","phone":"","company":"","need":"录音麦 2000 个"}}
      → 本函数解析 + 滤空后返回:
          reply = "2000 个没问题,我让 ODM 团队把方案发到 a@x.com。"
          lead  = {"email":"a@x.com", "need":"录音麦 2000 个"}   # name/phone/company 为空,已滤掉
      (之后调用方把 lead 交给 sessions.update_lead 回填,并重算 missing。)
    """
    # 步骤1:system 放最前,再按顺序接上最近 N 轮对话
    messages = [{"role": "system", "content": _system(session, faq, product_ref, moq)}]
    for t in window:
        role = "assistant" if t["role"] == "assistant" else "user"   # 只认这两种角色,其余一律当 user
        messages.append({"role": role, "content": t["text"]})

    # 步骤2:调模型,用 Structured Outputs 强制【结构合法】的 JSON(见 _RESPONSE_SCHEMA)。
    #   temperature 略低求稳;max_tokens 给足防截断(strict 下唯一还可能坏 JSON 的场景=撞 max_tokens
    #   被截断,概率极低,步骤3 的补救仍留着做纵深防御)。
    resp = await _client_lazy().chat.completions.create(
        model=os.getenv("LLM_MODEL", "gpt-4o-mini"),
        messages=messages,
        response_format={"type": "json_schema", "json_schema": _RESPONSE_SCHEMA},
        temperature=0.4,
        max_tokens=1200,
    )
    raw = resp.choices[0].message.content or "{}"   # 万一 content 为 None,兜底成空 JSON

    # 步骤3:解析模型返回的 JSON。gpt-4o-mini 偶发吐坏 JSON(尤其进入重复 loop 撞 max_tokens 时),
    #   此时【绝不能】把整坨原始串当回复丢给用户(会看到一大段坏 JSON + 重复刷屏)。补救分两级:
    #     ① 用正则把 "reply" 字段单独捞出来——多数情况坏的是后面的 lead,reply 本身是完整的;
    #        用 json.loads 还原它的转义(能正确处理 \" \n \uXXXX 和中文),把这句干净回复给用户。
    #     ② 连 reply 都捞不到 → 回一句干净兜底话术。两级都【只丢线索(lead={})】,下一轮会重抽,不影响捕获。
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'"reply"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)   # 抓第一段完整的 "reply":"..."
        if m:
            try:
                salvaged = json.loads('"' + m.group(1) + '"')       # 借 json 正确还原转义(保中文)
            except Exception:
                salvaged = m.group(1)
            if salvaged.strip():
                return (salvaged.strip(), {}, "")
        return (_PARSE_FALLBACK, {}, "")

    # 步骤4:取出 reply、wants_channel、lead
    reply = (data.get("reply") or "").strip()
    # wants_channel:用户"问我们哪个渠道"(whatsapp/wechat/telegram/email/phone),归一成小写;没问=""
    wants_channel = (data.get("wants_channel") or "").strip().lower()
    raw_lead = data.get("lead") or {}
    # 去空/规整:模型常把没抽到的字段填成 ""(空串)或空列表,留着会用空值覆盖已知线索。
    #   - 标量字段(name/email/phone/company/need):只保留非空字符串。
    #   - messengers 是【列表】:逐项去空,列表本身空就整个丢掉(不能像标量那样用 isinstance str 判,
    #     否则 list 会被误删——这是加 messengers 时容易踩的坑)。
    # 例:{"name":"","email":"a@x.com","messengers":["WhatsApp: +1..",""],"need":"录音麦"}
    #   → 滤成 {"email":"a@x.com","messengers":["WhatsApp: +1.."],"need":"录音麦"}。
    # (email/phone 的格式校验在 sessions.update_lead 里再兜一层,这里只管去空。)
    lead = {}
    for k, v in raw_lead.items():
        if k == "messengers":
            if isinstance(v, list):
                items = [m.strip() for m in v if isinstance(m, str) and m.strip()]
                if items:
                    lead["messengers"] = items
        elif isinstance(v, str) and v.strip():
            lead[k] = v.strip()
    return (reply, lead, wants_channel)
