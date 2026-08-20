"""
提示词(prompt)相关:AI 的人设,以及把上下文(入口意图/线索/FAQ)拼进系统提示的辅助函数。

注意:注释是中文(方便维护),但真正发给模型的 prompt 文本一律英文(默认语言英文)。
多语言策略:让模型"跟随用户最后一条消息的语言"回复,识别不出时兜底用英文。

【本文件产出的东西最后拼成什么样】见 llm._system():
  PERSONA(人设+目标) → entry_intent_line(从哪进来) → faq_reference(标准答案口径)
  → lead_line(已知/还缺什么) → _JSON_CONTRACT(输出格式) → 再接最近 N 轮对话。
"""

# AI 的人设与目标。这是每次对话都放在最前面的"系统提示"。文本用英文;默认语言英文。
#
# 目标 #2 = "什么算一条合格线索(required)":只要 need + 一种联系方式(email/phone/任一 IM 都算)。
# 目标 #3 = 联系方式的准确性:禁止 bot 脑补/自动修复联系方式,拿到后必须 readback 复述让用户确认。
# ⚠️ 目标 #2 的口径必须和 core/sessions.py update_lead() 里算 missing 的逻辑一致
#    (那里决定 Slack 卡片/lead_line 标什么缺,这里教 bot 去追什么;两边不一致就会一个追一套)。
PERSONA = """You are the website assistant for GMIC AI — an AI-voice-hardware ODM/OEM \
company that designs and manufactures custom AI voice devices (AI voice recorders, \
wearable microphones, smart badges, AI scribe devices, Bluetooth/Wi-Fi audio hardware, \
and fully custom voice-capture devices).

Your goals, in order:
1. Help the visitor concisely, warmly, and professionally.
2. A usable lead REQUIRES exactly two things before the chat ends:
   (a) NEED — one clear line on what product or help they want; and
   (b) at least ONE contact method. ANY single contact counts — an email, a phone number, \
OR a messaging-app contact (WhatsApp / WeChat / Telegram / Line / Signal ...). You MAY gently \
suggest email as the most reliable way to send a detailed proposal, but you MUST accept \
whatever the visitor gives: the moment you have ANY one contact, treat contact as DONE — \
never insist on email and never ask for a second channel.
   If the need or a contact is still missing, ask for it naturally in context — never interrogate.
   NAME and company are NICE-TO-HAVE: record them if offered, but never push for them.
   Whenever you ask for or accept a contact, reassure them that a REAL member of our team — \
not a bot — will personally follow up with them shortly. Never imply the assistant itself does the follow-up.
3. CONTACT ACCURACY — this is critical:
   - NEVER guess, repair, auto-correct, or complete a contact. Do NOT turn "(at)"/"at" into \
"@" or "dot" into "."; do NOT invent a domain or missing digits.
   - If a contact is obviously incomplete or malformed — e.g. an email with no "@" or no real \
domain such as "david(at)acme", or a number with too few digits — DO NOT read it back as if \
noted. Say PLAINLY that it doesn't look like a valid email/number and ask them to type it again \
(e.g. "That email doesn't look complete — could you type the full address?").
   - ONLY when you capture a clean, complete contact: READ IT BACK VERBATIM and ask them to \
confirm, e.g. "Just to confirm, your email is a@b.com — if that's wrong, just type it again." \
Never claim you've noted a contact you are not sure is complete and correct.
   - Read a given contact back only ONCE. If the visitor then confirms it, or already tells you \
it is correct / final / "100% sure", simply acknowledge it warmly and MOVE ON — e.g. "Perfect, \
got it — someone from our team will personally follow up at a@b.com shortly." Do NOT ask them to confirm the same contact again. \
Repeating "just to confirm" after the visitor has confirmed is annoying: confirm once, then trust it.
4. LANGUAGE: reply in the SAME language as the visitor's latest message. If the language \
is unclear or the message is empty, default to English.

Note: the visitor may be SPEAKING (their speech is transcribed to text for you) or typing; \
either way you ALWAYS reply in text, never audio. Because speech-to-text often mishears \
emails and spelled-out letters, the read-back-and-confirm rule in goal #3 matters most \
for contacts captured by voice.

Style: short (2-4 sentences), friendly, professional, no markdown headers, no long bullet lists. \
Group your reply into AT MOST two short paragraphs of flowing sentences; never put each sentence on \
its own line. Avoid em dashes (—); use commas, periods, or colons instead."""


def entry_intent_line(entry_intent):
    """
    根据"入口意图"(用户进来时点的按钮)给 AI 一句英文上下文提示,让它顺着话题开场。
    这只是开场种子——之后聊到哪,模型看对话窗口和 lead.need,不受这句约束。

    输入:entry_intent 字符串,取值为某个 Tab id(odm/add-branding/help-me-choose/book-demo)或
          "voice-message";或 None/其它(自由聊)。传进来的通常是 entry_intents[0](主归因,见 llm._system)。
    输出:一句英文提示;认不出的 entry_intent(含 None / voice-message)→ 返回空字符串 ""(不加任何提示)。

    这只是"开场行为种子"——具体选了哪些题/哪些型号由 questionnaire_line 详列;这里只给一句"该往哪个
    方向接话"的行为提示(如 ODM 就主动问 产品/数量/周期),避免泛泛问 "what can I help you with"。

    例:entry_intent="odm" → "...ask about product type, quantity, and timeline."
    例:entry_intent=None(直接打字自由聊)→ 返回 "" → 系统提示里不放这一块。
    """
    labels = {
        "odm": "The visitor came in via the ODM/OEM custom-build flow. Assume they want a fully "
               "custom device; proactively ask about product type, quantity, and timeline.",
        "add-branding": "The visitor came in via the 'Add your branding' (private-label) flow. Assume "
                        "they want to brand an existing device; focus on which product, how deep the "
                        "branding goes, and quantity.",
        "help-me-choose": "The visitor came in via the 'Help me choose' selector — they're unsure which "
                          "device fits. Land them on the right product, then naturally capture a contact.",
        "book-demo": "The visitor wants to book a demo. Confirm what they'd like to see, then capture a "
                     "contact and point them to the booking link.",
    }
    return labels.get(entry_intent, "")   # 默认值 "":entry_intent 不在表里(None / voice-message / 其它)就不加提示


def questionnaire_line(answers_by_tab, recommendations_by_tab):
    """
    把"用户在【各个】问卷 Tab 里选了什么 + (Tab3)我们据此算出的推荐"渲染成一段英文系统提示,
    让 LLM 顺着答案出方案、并且【绝不把答过的题再问一遍】。

    这是"问卷答案传给 GPT"的落点:答案按 Tab 分桶存在会话里(sessions.set_questionnaire),每轮由
    llm._system() 调本函数把【所有做过的 Tab】一起拼进系统提示 → 所以用户连做多份问卷时,后续每一轮
    对话 GPT 都能看到全部选择,不会重复问、也能综合出方案。

    ── 输入(注意都是【按 Tab 分桶】的 dict,对应 sessions 里的新结构)──
      answers_by_tab:         {tab: {题目id: 选项(字符串) 或 多选列表}};没做过任何问卷/为空 → 返回 ""(不加这块)。
      recommendations_by_tab: {tab: 推荐dict};目前只有 help-me-choose 这个键(其它 Tab 不产生推荐)。
                              推荐 dict 含 products/link/hint。可能为 None/空。
    ── 输出 ──
      一段英文提示(按 Tab 分段列选择,带上对应 Tab 的推荐);没有任何选择则返回 ""。

    ── 推荐渲染 ──
      products 非空 → 直接把这些型号 + hint 给模型,让它据此出方案(不给型号让 LLM 自由发挥会幻觉出不存在的型号)。
      products 为空(兜底)→ 告诉模型没有明显对口的单品,引导看总览页 + 提出让真人帮选。
      (注:哪些映射我们内部拿不太准、需要人工二次确认,由【我们自己记着】,不再作为字段喂给模型;
       若某条想让 bot 主动说"让专家确认",把这层意思写进该规则的 hint 文案即可——hint 是自然语言,会自然带进回复。)

    例:answers_by_tab={"help-me-choose":{"usage":"To answer phone calls",...}}、
        recommendations_by_tab={"help-me-choose":{products:["Telalive","HA-TEL02"],hint:"call-answering ..."}}
        → 输出大致:
          "The visitor completed one or more guided questionnaires. Their selections:
           [help-me-choose]
           - usage: To answer phone calls
           Based on this, recommend: Telalive, HA-TEL02 (call-answering ...).
           Give a brief, tailored recommendation NOW ... do not ask these questions again ..."
    """
    if not answers_by_tab:
        return ""   # 没做过任何问卷(自由聊/全跳过)→ 不加这一块

    recommendations_by_tab = recommendations_by_tab or {}
    body = []                                         # 各 Tab 的选择 + 推荐;为空则整块不加(见末尾判断)
    for tab, answers in answers_by_tab.items():
        if not answers:
            continue                                  # 这个 Tab 的桶是空的(理论上不会,防御)→ 跳过
        body.append(f"[{tab}]")                        # 分段标出是哪个 Tab 的选择(多份问卷时能区分)
        for k, v in answers.items():
            val = ", ".join(v) if isinstance(v, list) else v   # 多选题的值是列表 → 拼成一行
            if val:
                body.append(f"- {k}: {val}")

        # (仅有推荐的 Tab,目前是 help-me-choose)把型号 + hint 给模型据此出方案。
        # 有型号就直接推(型号来自确定性映射,不让 LLM 自己编);没型号(兜底)则引导看总览页 + 找真人帮选。
        rec = recommendations_by_tab.get(tab)
        if rec:
            prods = ", ".join(rec.get("products") or [])
            hint = rec.get("hint", "")
            if prods:
                body.append(f"Based on this, recommend: {prods} ({hint}).")
            else:
                body.append(f"No single device is an obvious fit ({hint}); point them to the full product "
                            f"range and offer to help narrow it down.")

    if not body:
        return ""   # 所有桶都空 → 不加这一块

    header = "The visitor completed one or more guided questionnaires. Their selections:"
    # 收尾指令:立刻出方案 + 别重复问 + 自然引向索样和留联系方式(承接 PERSONA 目标 #2)
    trailer = ("Give a brief, tailored recommendation NOW using these selections, as ONE flowing "
               "paragraph: which product and why it fits, folding in any must-haves it meets. Do NOT "
               "ask these questions again, and do NOT put each sentence on its own line. (A follow-up "
               "invitation to share more requirements and leave a contact is appended automatically, "
               "so you do NOT need to add one here.)")
    return "\n".join([header] + body + [trailer])


def faq_reference(faq):
    """
    把 FAQ 的标准答案喂给模型,让它"自由回答"时口径和 ❓FAQ 按钮里的写死答案保持一致
    (避免按钮说"MOQ 500"、bot 聊天时随口说成"1000")。

    【输入 faq 长什么样】widget.json 里的一个列表,每条是一个问答对:
        [
          {"q": "What is your MOQ?",        # q = 问题
           "a": "Our ODM MOQ is 500 units", # a = 标准答案文本(marketing 写好的官方口径)
           "link": "https://gmic.ai/..."},  # link = 可选的"了解更多"链接(本函数不用)
          {"q": "Lead time for samples?",
           "a": "TODO: marketing fills in",  # a still a TODO placeholder = 这条还没写,跳过
           "link": null},
        ]
      注:"TODO..." 是占位符——widget.json 出厂时所有 a 都是 "TODO: marketing fills in...",
      等 marketing 填真答案。本函数只喂【已填好】的,TODO 占位的跳过(否则会把废话喂给模型)。

    【输出长什么样】把已填好的问答拼成一段英文参考事实;一条都没填好就返回空字符串 ""。
      上面的例子(第2条是 TODO,跳过)→ 输出:
        "Reference facts — keep your answers consistent with these:
        - Q: What is your MOQ?
          A: Our ODM MOQ is 500 units"
    """
    lines = []
    for item in faq or []:                       # faq 可能为 None → `or []` 兜底成空列表
        a = item.get("a", "")
        if a and not a.startswith("TODO"):        # 只要有答案、且不是 TODO 占位
            lines.append(f"- Q: {item['q']}\n  A: {a}")
    # 有内容才加抬头;一条都没填好就返回 ""(系统提示里干脆不放 FAQ 这块)
    return ("Reference facts — keep your answers consistent with these:\n" + "\n".join(lines)) if lines else ""


def product_reference(text):
    """
    把 widget.json 里的 product_reference(一段【官网核实过】的产品目录)喂给模型,让它在【自由聊天】
    (用户没走问卷、直接打字问需求)时也能用真实型号/规格作答,而不是空口瞎编或答得很泛。

    为什么需要:问卷走完会有 recommend 注入(见 questionnaire_line),但用户直接打字问"有没有防水的
    录音设备"时,模型手里只有 PERSONA(泛泛的品类)+ FAQ——没有具体型号知识,容易要么答得很虚、要么
    编出不存在的型号。这段目录补上"真实产品事实",并硬性要求:只推列出的型号、不编规格、不确定就引导看
    总览页 + 转真人。

    输入:text = widget.json 的 product_reference 字符串(可能为空)。
    输出:带抬头的一段英文提示;为空则返回 ""(系统提示里不放这块)。
    """
    text = (text or "").strip()
    return ("Product knowledge — ground ALL product answers in these real facts:\n" + text) if text else ""


def moq_line(moq):
    """
    把起订量(MOQ)口径注入系统提示,并在访客量级【低于】起订量时给出硬约束。

    ── 为什么必须有这一块(踩过的坑)──
      widget.json 里一直写着 moq_note("典型起订量约 2,000 台"),但【从来没有任何代码注入它】,
      等于死配置:MOQ 这个数字唯一进到系统提示的路径,是藏在 FAQ 第 2 条答案的句子中间。
      模型只"瞄到过一次数字",没人告诉它这是硬门槛 → 2026-08-19 一条真实询盘里,访客选了
      "Under 1,000",bot 回了 "perfect for smaller quantities under 1,000 units, a great fit"。
      客户拿到一个我们兑现不了的预期,真人跟进时要往回改口。这块就是修这个。

    ── 输入 ──
      moq: {"note": 起订量口径原文(来自 widget.json 的 moq_note), "below": 访客量级是否低于起订量(bool)}。
           note 为空 → 返回 ""(这块不放,行为退回改动前)。below 由 widget_config.below_moq() 确定性判定,
           【不让模型自己比大小】——它既不可靠、也不知道 2000 是门槛。
    ── 输出 ──
      一段英文提示;note 为空时返回 ""。

    例:{"note":"Custom / private-label projects typically start around 2,000 units...", "below":True}
        → 除了口径本身,还追加"绝不能说 great/perfect fit,要说清典型起订量 + 给出样机/打样这条路 +
           让真人给准数"这条硬约束。
        {"note":"...","below":False} → 只给口径,不追加约束(别对本来量级够的人主动提门槛,扫兴)。
    """
    note = ((moq or {}).get("note") or "").strip()
    if not note:
        return ""      # 没配 MOQ 口径 → 这块不放
    lines = [f"Minimum order quantity (MOQ) — this is a commercial commitment, treat it as fact: {note}"]
    if (moq or {}).get("below"):
        # ⚠️ 这几句是防"说漏"的关键,改文案时别把"禁止说 great fit"这层意思弄丢。
        lines.append(
            "IMPORTANT: the visitor's stated quantity is BELOW that typical minimum. Do NOT tell them it "
            "is a 'great fit' or 'perfect' for their quantity, and do NOT imply we can produce a custom or "
            "private-label run at that volume. Instead: acknowledge their volume, state plainly that custom "
            "and private-label runs typically start around the minimum above, and offer the realistic path "
            "forward, a working sample or paid prototype first, or an off-the-shelf unit they can brand later, "
            "with our team confirming the exact figure for their specific build. Stay warm and encouraging, "
            "never dismissive, and never quote a lower minimum than the one above."
        )
    return "\n".join(lines)


def lead_line(lead):
    """
    把"目前已知访客哪些信息、还缺什么"用英文告诉模型,方便它决定要不要追问 need/联系方式。
    (缺什么由 sessions.update_lead 算好的 lead["missing"] 决定——必填只有 need + 联系方式。)

    输入:lead 字典,如 {"email":"a@x.com", "need":"recorder", "missing":["name"...]},可能为空 {}。
    输出:一两句英文陈述已知 + 还缺什么。

    例1:lead={} → "Known about the visitor: nothing yet."(还没套出任何信息)
    例2:lead={"email":"a@x.com", "missing":["need"]} →
         "Known about the visitor: {'email': 'a@x.com'}. Still missing (try to obtain): ['need']."
         → 模型看到还缺 need,就会追问"您具体想做什么产品?"。
    """
    if not lead:
        return "Known about the visitor: nothing yet."
    known = {k: v for k, v in lead.items() if k != "missing" and v}   # 去掉 missing 本身和空字段,剩下真已知的
    missing = lead.get("missing", [])
    parts = [f"Known about the visitor: {known or 'nothing yet'}."]
    if missing:
        parts.append(f"Still missing (try to obtain): {missing}.")
    return " ".join(parts)
