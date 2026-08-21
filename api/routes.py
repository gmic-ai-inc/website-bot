"""
HTTP 路由 —— 用 FastAPI 的 APIRouter 组织(异步),方便以后加更多端点/版本。

端点一览:
  GET  /health            存活检查
  GET  /config            前端拉配置(问候语、4 个 Tab、问卷、FAQ、归因题…)
  POST /event             快捷按钮点击(topic 话题 / faq 常见问题 / link 跳转)
  POST /chat              一条打字消息 -> AI 回复
  POST /questionnaire     某个 Tab 的问卷答完一次性提交 -> 方案 + 链接
  POST /source            归因题("你从哪知道我们的")-> 记录客源渠道,不调 LLM
  POST /voice/transcribe  一段录音 -> 只转写(浮窗预览用)
  POST /voice/message     发一条语音留言(联系方式必填)-> Slack,不调 LLM

【FastAPI 白拿的好处】用 Pydantic 模型声明请求体,字段缺失/类型不对 FastAPI 会自动返回 422,
  不用再手写 `if not sid` 这种校验(回应你 review 里关心的参数校验)。
"""
import os
import logging

from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pydantic import BaseModel

from core import sessions
from core.widget_config import (CONFIG, ACTIONS, QUESTIONNAIRES, SOURCE_QUESTION, MOQ_NOTE,
                                recommend_for, match_our_channels, contact_for_channel,
                                industry_links_for, below_moq, source_value)
from ai import stt, llm
from integrations import slack

log = logging.getLogger(__name__)

router = APIRouter()
STORE = sessions.STORE
# 录音大小上限(字节)。粗略防超大文件;精确的按时长限制在前端做。
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", str(8_000_000)))

# 大模型挂了(quota 耗尽/key 失效/超时)时给用户的兜底回复。默认英文(见多语言策略)。
# 为什么要兜底:见记忆 openai-key-pool-emergency——6-29 全线 key 耗尽过。没兜底的话
# LLM 一抛异常整个 /chat 就 500,用户"发了没反应",线索也断在半路。
LLM_FALLBACK_REPLY = ("Thanks for reaching out! Leave your email or phone and a real member of "
                      "our team will personally get back to you shortly.")


# ======================== 请求体模型(Pydantic 自动校验) ========================
class EventReq(BaseModel):
    # 例:{"session_id":"sess_ab12","action":"topic","id":"odm"}
    #     {"session_id":"sess_ab12","action":"faq","index":1}
    session_id: str
    action: str                    # "topic" | "faq" | "link"
    id: str | None = None          # topic/link 用:按钮 id
    index: int | None = None       # faq 用:第几条问题
    page_url: str | None = None
    lang: str | None = None


class ChatReq(BaseModel):
    # 例:{"session_id":"sess_ab12","text":"你们支持防水吗","page_url":"/products/"}
    # 注:原来的"可选邮箱框"已删除(那个框是给语音兜底的拐杖,又和对话记忆打架会被覆盖,
    #     鸡肋)。现在联系方式一律走对话捕获,不再有 email 搭车字段。
    session_id: str
    text: str
    page_url: str | None = None
    lang: str | None = None


# ======================== 小工具 ========================
async def _run_llm(sid):
    """
    跑一轮大模型,并把结果落进内存(异步)。步骤:
      1) 取会话快照 + 最近 N 轮(滑动窗口)
      2) await llm.respond() 拿到 (回复, 线索更新)
      3) 有线索就 merge 进 lead;有回复就作为 assistant 追加进 turns
    例:用户刚说完"邮箱 a@x.com" → 模型回"好的已记录…" + lead={email:"a@x.com"} →
        这里把 email 合并进 lead(会先过格式校验),把回复追加进 turns,返回给上层发 Slack/前端。

    【兜底】模型调用失败(quota/key/超时)不让整个请求 500:吞掉异常、记 log、回一句兜底话术,
      线索置空。这样用户仍收到回复、Slack 卡照常刷,线索不断在半路。
    """
    snap = STORE.snapshot(sid)
    window = STORE.window(sid)
    # MOQ(起订量)口径:note = 官方原话;below = 访客选过的数量是否低于起订量(确定性判定,不让模型比大小)。
    # 以前 moq_note 是死配置从没注入 → bot 对 "Under 1,000" 说过"完美契合",见 prompts.moq_line 的注释。
    moq = {"note": MOQ_NOTE, "below": below_moq(snap.get("answers") if snap else None)}
    try:
        reply, lead, wants_channel = await llm.respond(snap, CONFIG.get("faq", []), window,
                                                       CONFIG.get("product_reference", ""), moq)
    except Exception:
        log.exception("llm.respond failed for session %s — using fallback reply", sid)
        reply, lead, wants_channel = LLM_FALLBACK_REPLY, {}, ""
    if lead:
        STORE.update_lead(sid, lead)
    if reply:
        STORE.append_turn(sid, "assistant", reply)
    return reply, lead, wants_channel


def _new_channel_throwbacks(sid, before_messengers):
    """
    甩直连链接【触发路 A:用户主动留了自己的号】——只对"这一轮新增的平台"甩,实现"第一次留就甩、
    之后(补充/纠正)不再烦"。核心手法:比较跑大模型【前后】的 messengers,按【平台】取差集。

    为什么按平台而非整条字符串比:① 模型每轮可能重复抽出同一 handle;② 用户纠正同平台号码时
    handle 变了但平台没变——两种都不该重复甩。按平台集合比,这两种都会被判成"非新增"。

    ── 输入 ──
      sid:               会话 id。
      before_messengers: 跑大模型【之前】lead 里的 messengers 列表(由 /chat、/voice 在调 _run_llm 前快照好传进来)。
    ── 输出 ──
      要甩回给用户的 contacts 配置列表(可能为空 [])。

    ── 逐步逻辑(3 个场景对照)──
      步1  snap = 当前会话快照(跑完模型后的最新状态)
      步2  after = 现在 lead 里的 messengers        # update_lead 已把这轮新抽到的合并进去(同平台留最新)
      步3  before_plats = {before 各条的平台}        # 用集合,便于差集
      步4  fresh = after 里"平台不在 before_plats"的那些   # 只留【本轮新出现的平台】
      步5  return match_our_channels(fresh)          # 把新增平台对到我们的现成链接

      场景A 首次留(应甩): before=["WeChat: x"](plats={wechat});这轮报了 WhatsApp
        → after=["WeChat: x","WhatsApp: +1.."] → fresh=["WhatsApp: +1.."](whatsapp∉{wechat})→ 甩 WhatsApp ✅
      场景B 纠正同平台(不甩): before=["WeChat: old"](plats={wechat});这轮"微信改成 new"
        → after=["WeChat: new"] → fresh=[](wechat∈before_plats,handle 变了但平台没变)→ 不甩 ✅
      场景C 只是提到/问(不甩): 用户没留自己的号 → after==before → fresh=[] → 不甩 ✅
        (注:"问我们的号"是另一条路,见 wants_channel / contact_for_channel。)
    """
    snap = STORE.snapshot(sid)                                                       # 步1
    after = (snap["lead"].get("messengers") or []) if snap else []                   # 步2
    before_plats = {sessions.messenger_platform(m) for m in before_messengers}       # 步3
    fresh = [m for m in after if sessions.messenger_platform(m) not in before_plats] # 步4
    return match_our_channels(fresh)                                                 # 步5


def _throwbacks(sid, before_messengers, wants_channel):
    """
    汇总这一轮要甩给用户的直连渠道(合并两条触发路,按 contacts id 去重):
      路 A(用户留了自己的号):_new_channel_throwbacks —— 只对本轮新增平台甩一次。
      路 B(用户问我们的号):  wants_channel —— 每次问都甩(问了就答,天然不必跨轮去重)。
    例:用户说"我的 WhatsApp +1..、你们 Telegram 是啥?" → 路A 甩 WhatsApp、路B 甩 Telegram → 两条都回。
    """
    result = _new_channel_throwbacks(sid, before_messengers)     # 路 A
    wanted = contact_for_channel(wants_channel)                  # 路 B:问起就取我们现成的那条
    if wanted and wanted["id"] not in {c["id"] for c in result}:  # 按 id 去重(两路可能指向同一渠道)
        result.append(wanted)
    return result


async def _reply_and_archive(sid):
    """
    跑一轮大模型出【回复】+ 归档到 Slack + 算出这轮要甩的直连渠道。
    【前置】用户这轮说的话必须【已经】append 进 turns 了(打字在 /chat 里 append、语音在 /voice 里 append)。
    这样本函数只管"生成回复",不重复记用户输入——正是"语音两步走"能共用同一段逻辑的关键。

    步骤:
      1) 跑模型前先快照当前 messengers(用于第4步比出"这一轮新报的 IM")
      2) _run_llm:出回复 + 回填 lead/turns(内部带兜底,失败不 500)
      3) AI 回复发进 Slack thread 归档(🤖 前缀)
      4) 刷新 Slack 线索卡 + 算甩链(路A 用户留自己的号 / 路B 用户问我们的号)
    返回:(reply 回复文本, throwbacks 要甩回的直连渠道列表)。
    """
    before_msgr = list((STORE.snapshot(sid)["lead"].get("messengers") or []))  # 步1
    reply, _, wants = await _run_llm(sid)                                       # 步2
    await slack.post_detail(STORE, sid, f"🤖 {reply}")                          # 步3
    await slack.update_card(STORE, sid)                                         # 步4
    throwbacks = _throwbacks(sid, before_msgr, wants)
    return reply, throwbacks


def _should_ask_source(sid, force=False):
    """
    该不该在这一轮把归因题("你从哪知道我们的")发给访客?——【确定性判断,不靠 LLM】。

    ── 产品口径 ──
      ① 【不设成必填】(Luna 8-19):这题对访客零价值(不像问卷答完能换来一个推荐),必填只会让人
         随手蒙一个或者直接关窗;逼来的数据比没数据更危险(会拿它做决策)。所以它是"发出去,不答就算了"。
      ② 【永远放在最后收尾,不抢戏】(Luna 8-20 改口径):以前是"问卷答完就问 / 一拿到联系方式就问",
         结果紧跟在访客留下邮箱之后马上追一句,很赶。现在:
           · 拿到联系方式的 → **再过 N 句才问**(ask_turns_after_contact,默认 1)。为什么是 1:
             真实流程是"用户打邮箱 → bot 逐字复述确认 → 用户说 yes" —— 联系方式在【打邮箱那一句】
             就进 lead 了,+1 正好落在【确认那一句】,也就是 Luna 要的"confirm 之后马上问"。
             设成 0 会变成"刚给邮箱就追问"(她否掉过);设成 2 会拖到确认之后还得再聊一句才问
             (8-20 实测她那通就是这样错过去的)。
           · 一直没留联系方式的 → 聊到第 M 句才问(ask_after_user_turns,默认 5)= 当收尾用。
           · 【问卷答完不再单独触发】:答完问卷正是方案刚出来、他最想接着聊的时刻,插这题最碍事。
             问卷那一步本身算进句数,所以他继续聊下去自然会走到上面两条。
           · 兜底:用户说了道谢/道别的话(farewell_words)→ 不再等轮数,立刻问 —— 他要走了,
             这是最后的机会。判据是【固定词表 + 确定性匹配】,不问大模型(同 MOQ 那条的思路:
             能用代码判的别交给模型)。
      ③ 【一个会话只问一次】:发出去就标记 source_asked(见 sessions.mark_source_asked),
         用户不理也不再弹。标记在服务端,所以刷新页面(session_id 从 localStorage 复用)也不会重复问。

    ── 输入 ──
      sid:   会话 id。
      force: 保留参数但**不再让它跳过时机判断**(见 ②:问卷答完不该抢在方案前面问);
             /questionnaire 仍然传它,只是现在只影响"允许问",不影响"必须现在问"。
    ── 输出 ──
      True = 这一轮把题发出去(调用方需自行调 mark_source_asked);False = 不发。

    例(默认参数):
        第 1 句、没联系方式        → False(太早,别打断)
        第 2 句留下邮箱            → False(刚给完就追问很赶)
        第 3 句说 "yes" 确认邮箱     → True(确认完就问,这是最自然的收尾点)
        一直没留联系方式,聊到第 5 句 → True(当收尾)
        第 2 句就说 "thanks, bye"  → True(他要走了,最后机会)
        已经问过一次               → False(不管答没答)
    """
    if not SOURCE_QUESTION.get("options"):
        return False                                  # 没配这道题 → 永不问(删配置即整个功能下线)
    snap = STORE.snapshot(sid)
    if not snap:
        return False
    if snap.get("source") or snap.get("source_asked"):
        return False                                  # 答过 / 问过 → 不再问

    turns = snap.get("turns", [])
    # ⚠️ 用累计计数,不是 len(turns):turns 有上限会被裁,数出来的会变小(见 sessions.user_turns 注释)
    user_turns = snap.get("user_turns", 0)

    # 收尾信号:用户这一句在道谢/道别 → 不再等轮数(再等就没机会了)
    last_user = next((t.get("text", "") for t in reversed(turns) if t.get("role") == "user"), "")
    low = last_user.lower()
    if any(w in low for w in SOURCE_QUESTION.get("farewell_words", [])):
        return True

    contact_turn = snap.get("contact_turn")
    if contact_turn is not None:
        # 已经拿到联系方式:从"拿到的那一句"往后再数 N 句
        gap = int(SOURCE_QUESTION.get("ask_turns_after_contact", 2))
        return user_turns >= contact_turn + gap

    # 一直没留联系方式:聊到第 M 句当收尾问
    return user_turns >= int(SOURCE_QUESTION.get("ask_after_user_turns", 5))


def _ask_source_flag(sid, force=False):
    """
    算出这一轮要不要问归因题,并【顺手把"已问"标记打上】,返回给前端的布尔值。
    合成一个函数是为了不让调用方忘记打标记(忘了就会每轮都弹,那才叫烦人)。
    """
    ask = _should_ask_source(sid, force=force)
    if ask:
        STORE.mark_source_asked(sid)   # "发出去"就算问过:不答也不再弹(见 sessions.mark_source_asked)
    return ask


# 语音留言的联系方式:平台 key → Slack/messengers 里显示的规范标签。
# 对齐 sessions.messenger_platform / widget_config 的平台判断(whatsapp/wechat/telegram)。
_MSGR_LABELS = {"whatsapp": "WhatsApp", "wechat": "WeChat", "telegram": "Telegram", "messenger": "Messenger"}


def _validate_contact(ctype, value):
    """
    校验语音留言浮窗里必填的【那一种】联系方式,并转成能写进 lead 的字段。
    这是"发语音前必须留联系方式"的服务端兜底(前端也 gate 一遍,但绝不能只信前端)。

    输入:ctype = "email"/"phone"/"whatsapp"/"wechat"/"telegram";value = 用户填的值。
    输出:能喂给 STORE.update_lead 的字段 dict;不合法(空/格式错/未知类型)→ None(调用方据此 400)。
    例:("email","a@b.com") → {"email":"a@b.com"};("whatsapp","+1650...") → {"messengers":["WhatsApp: +1650..."]};
        ("email","坏邮箱") → None。
    """
    value = (value or "").strip()
    if not value:
        return None
    if ctype == "email":
        return {"email": value} if sessions._valid_email(value) else None
    if ctype == "phone":
        return {"phone": value} if sessions._valid_phone(value) else None
    label = _MSGR_LABELS.get(ctype)
    if label:
        return {"messengers": [f"{label}: {value}"]}   # 写成 "Platform: value",messenger_platform 能解析
    return None


# ======================== 路由 ========================
@router.get("/health")
async def health():
    # 返回存活状态 + 当前活跃会话数
    return {"status": "ok", **STORE.stats()}


@router.get("/config")
async def config():
    # 前端从这里加载 问候语 + 按钮 + FAQ(团队改 config/widget.json 即可,不用动代码)
    return CONFIG


@router.post("/event")
async def event(req: EventReq):
    """
    处理 widget 里【快捷按钮】的点击 —— 注意:不是打字、也不是语音(那两条分别走 /chat 和 /voice)。
    这里只管问候语下面那几个 shortcut(话题按钮 / FAQ / 跳转链接),多数分支不调大模型,秒回、省钱。
    action 有三种:topic(话题)/ faq(常见问题)/ link(跳转);具体每种干啥见下面各分支的注释。
    """
    # 不管哪种 action,先确保这个用户的会话存在(用户第一次点按钮,就在这一步建会话)。
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})

    # ── 话题按钮(如 🏭 ODM):种入口意图 + 建 Slack 卡 + 回一句写死的开场白,不调大模型 ──
    if req.action == "topic":
        # act = 这个按钮在 widget.json 里的整条配置(core.widget_config 已按 id 建好索引 ACTIONS)。
        #   例:req.id="odm" → act={"id":"odm","label":"🏭 Custom / ODM","type":"topic",
        #                          "entry_intent":"odm","opener":"We do full ODM/OEM..."}
        act = ACTIONS.get(req.id)
        if not act:   # id 查不到按钮(前端传错 / 配置漏)→ 400,别静默返回空 reply + 白建空卡
            raise HTTPException(status_code=400, detail="unknown action id")

        # 累积"入口意图":记下用户点过的话题按钮(如 "odm"),按到达顺序追加、去重(第一个为主归因)。
        STORE.set_entry_intent(req.session_id, act.get("entry_intent"))

        # opener = 开场白:点这个话题按钮后 bot 先说的那句【写死】的话(不调大模型,直接秒回)。
        #   例(odm):"We do full ODM/OEM custom hardware ... product, quantity, target launch date?"
        opener = act.get("opener", "")
        if opener:
            STORE.append_turn(req.session_id, "assistant", opener)  # 开场白也算一轮,后续对话能接上它

        # 话题按钮 = 高意向:立刻在 Slack 建线索卡,并把刚种的 entry_intent 刷进卡。
        await slack.ensure_card(STORE, req.session_id)
        await slack.update_card(STORE, req.session_id)
        return {"reply": opener}   # 产出:{"reply": 开场白} → 前端显示成 bot 的第一句

    # ── 常见问题(FAQ):按 index 取那条,回写死的标准答案(+可选链接),不调大模型 ──
    if req.action == "faq":
        faq = CONFIG.get("faq", [])
        if req.index is None or not (0 <= req.index < len(faq)):   # index 越界/缺失 → 400
            raise HTTPException(status_code=400, detail="bad faq index")
        item = faq[req.index]                                       # item = {"q":问题, "a":答案, "link":可选}
        STORE.append_turn(req.session_id, "user", item["q"])        # 把"问题"当用户说的记一轮(追问能接上)
        STORE.append_turn(req.session_id, "assistant", item["a"])   # 把"标准答案"当 bot 回的记一轮
        return {"reply": item["a"], "link": item.get("link")}       # 产出:{写死答案 + 可选的了解更多链接}

    # ── 跳转按钮(看产品 / 预约演示):真正的跳转在前端做,后端保活 + 记下点的是哪个链接 ──
    if req.action == "link":
        # 用 id 查出点的是哪个按钮(products / demo …),这样才能区分/统计,而不是"任何链接一视同仁"。
        act = ACTIONS.get(req.id)
        if not act:   # id 查不到(前端传错 / 配置漏)→ 400,和 topic 分支一致
            raise HTTPException(status_code=400, detail="unknown action id")
        STORE.touch(req.session_id)   # 只刷新活跃时间,防会话被 TTL 清掉(跳转本身在前端做)
        # 记一笔"点了哪个链接去哪":先落 log 便于观察;以后要正经统计可在此镜像进 Slack/DB。
        log.info("link click: session=%s id=%s url=%s", req.session_id, req.id, act.get("url"))
        return {"ok": True}                                         # 产出:{"ok": True}(没有 reply)

    raise HTTPException(status_code=400, detail="unknown action")   # 三种都不是 → 未知 action


@router.post("/chat")
async def chat(req: ChatReq):
    """
    用户【打字】发一条消息的入口(和 /event 按钮并列;语音已拆成独立的 /voice/message 留言)。会真正调大模型。
    具体每步干啥、产出什么见下面各 step 的注释。
    """
    # 空消息直接 400,别浪费一次大模型调用。
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")

    # 1) 确保会话存在(首次打字就在这建会话,并记下来源页 page_url、语言 lang 进 meta)
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})
    # 2) 把用户这句记进对话(turns),后续滑动窗口喂给模型时能带上
    STORE.append_turn(req.session_id, "user", text)
    # 3) 确保 Slack 有这通对话的线索卡(没有就建,根消息 ts 存进会话)
    await slack.ensure_card(STORE, req.session_id)
    # 4) 用户原话发进 Slack thread 做明细归档(👤 前缀标明是访客说的)
    await slack.post_detail(STORE, req.session_id, f"👤 {text}")

    # 5) 跑大模型出回复 + 归档 + 算甩链(打字是即时的,一步返回即可,不像语音要拆两步)。
    reply, throwbacks = await _reply_and_archive(req.session_id)
    # 6) 该不该问归因题("你从哪知道我们的")。聊天这条路按"拿到联系方式 或 聊够 N 轮"触发,
    #    一个会话只问一次(见 _should_ask_source);前端收到 true 才渲染那排选项。
    ask_source = _ask_source_flag(req.session_id)
    # 产出:{reply: AI 回复, contacts: 要甩回的直连渠道(可能为空), ask_source: 是否附上归因题}
    return {"reply": reply, "contacts": throwbacks, "ask_source": ask_source}


# 注:原 POST /lead 端点已删除。它只服务于 widget 的"邮箱框 Save"按钮,那个框已连同一起去掉
#     (鸡肋 + 和对话记忆打架会被覆盖)。聊天里联系方式走对话捕获(/chat);语音留言走 /voice/message 必填框。


# ============================================================================
# 问卷(Guided questionnaire)
#   产品背景:官网 chatbot 顶部 4 个 Tab(ODM / Add your branding / Help me choose / Book a demo),
#   每个 Tab 点开 = 一句介绍 + 3 道选择题。问卷屏在【前端】渲染、翻页、收集答案,【全程不调 LLM】
#   (确定性、省钱、防幻觉)。用户答完最后一题,前端把整包答案 POST 到这里。
#
#   本端点做四件事(见下面各 step):
#     ① 把答案 + 入口 Tab 写进会话(结构化上下文,后续每轮注入 LLM → bot 绝不重复问答过的题);
#     ② (仅 Tab3 help-me-choose)按答案查 recommend_rules,算出推荐产品/链接/hint;
#     ③ 调一次 LLM,依答案 + 推荐生成一段【量身方案】回给用户(复用 /chat 那套出回复+归档逻辑);
#     ④ 把该 Tab 要甩的链接(方案链接 / Tab3 推荐链接 / Book-a-demo 日历)一并返回给前端展示。
#   产出:{reply: 方案文本, contacts: 要甩的直连渠道, links: 要展示的链接列表}。
# ============================================================================
class QuestionnaireReq(BaseModel):
    # 例:{"session_id":"sess_ab12","tab":"help-me-choose",
    #      "answers":{"usage":"To answer phone calls","where":"Office / meetings","musthave":["Security / encryption"]}}
    session_id: str
    tab: str                       # 哪个 Tab:odm / add-branding / help-me-choose / book-demo
    answers: dict = {}             # {题目id: 选中的选项(字符串) 或 多选列表};跳过的题不在里面
    page_url: str | None = None
    lang: str | None = None


def _sanitize_answers(q, answers):
    """
    只保留【问卷定义里真实存在的题】+【该题选项清单里真实存在的选项】,其余一律丢弃。

    为什么必须做(不是可选):问卷答案会被拼进 LLM 的【系统提示】(见 prompts.questionnaire_line),
    这是比 /chat 用户消息更高的信任位;而本端点是公开的(任何人可直接 POST,不只经我们那个"只会
    发合法选项"的确定性前端)。不校验 = 把"任意字符串注入系统提示"的口子敞开(prompt injection),
    也兜不住前端 bug 传来的脏值。校验后:未知题 id 被丢、非法选项被丢、多选里混进的非法项被逐个过滤。

    输入:q = 该 Tab 的问卷定义(取 questions 的 id + options 清单);answers = 前端提交的原始 dict。
    输出:清洗后的 answers(只含合法题 + 合法选项);某题非法/被清空 → 该题不出现在结果里。
    例:题 usage 选项含 "On a desk / in a room";answers={"usage":"On a desk / in a room","x":"注入串"}
        → {"usage":"On a desk / in a room"}(未知题 x 丢掉)。
        多选 musthave:["Rugged / waterproof","恶意串"] → 只留 ["Rugged / waterproof"]。
    """
    by_id = {question["id"]: question for question in q.get("questions", [])}
    valid = {}
    for qid, val in (answers or {}).items():
        question = by_id.get(qid)
        if not question:
            continue                                        # 未知题 id → 丢
        opts = question.get("options", [])
        if isinstance(val, list):                           # 多选题:逐项过滤,只留清单内的
            kept = [v for v in val if v in opts]
            if kept:
                valid[qid] = kept
        elif val in opts:                                   # 单选题:必须在选项清单内
            valid[qid] = val
    return valid


def _summarize_answers(q, answers):
    """
    把问卷答案渲染成一行【人类可读】的摘要(题目全文 → 选中的答案),用途有二:
      ① 作为一条 user turn 追加进对话(给 LLM 一个"用户刚提交了这些"的触发点,好生成方案);
      ② 发进 Slack thread 做明细归档,团队一眼看全用户选了什么。
    输入:q = 该 Tab 的问卷定义(取 questions 的题目全文);answers = 用户答案。
    产出:形如 "How will it be used? → To answer phone calls · Where...? → Office / meetings" 的字符串;
          一题都没答 → "(completed the questionnaire)"。
    """
    parts = []
    for question in q.get("questions", []):     # 按问卷定义的题序走(而非 answers 的字典序),读起来顺
        val = answers.get(question["id"])
        if not val:                             # 这题被 skip 了 → 不进摘要
            continue
        if isinstance(val, list):               # 多选题 → 逗号拼接
            val = ", ".join(val)
        parts.append(f"{question['q']} → {val}")
    return " · ".join(parts) if parts else "(completed the questionnaire)"


def _questionnaire_links(tab, q, recommendation, answers):
    """
    算出这个 Tab 答完后要给用户展示的链接列表(前端渲染成按钮)。

    ── 四个来源,按"最贴他这次选择"的顺序拼 ──
      ① 推荐型号的详情页(仅 help-me-choose,来自 recommend_rules 命中那条的 links);
      ② 【行业落地页】——按他在"在哪用 / 什么行业"那题选的,给一条"你这行我们专门做"的页面
         (见 widget_config.industry_links_for)。为什么加:官网 8 月上线了一批行业页,只给一个通用
         产品页说服力差很多;客户看到"他们专门做医疗场景的"信任度完全不同。odm / add-branding /
         help-me-choose 三个 Tab 都吃这条(它们的行业题选项都在 industry_links 表里)。
      ③ book-demo 的日历预约链接(result_link);
      ④ 该 Tab 配好的固定链接(questionnaire.links)。

    ── links 是【列表】不是单值 ──
      一条推荐可能对应【多个】型号详情页:比如"穿戴 + 诊疗"推 MIC06A 和 MIC05,官网两款各有自己的页
      (MIC06 系列页 + MIC05 页),必要时两个链接都给。改动前 recommend_rules 只有一个 `link` 字段,
      推荐里第一款写着 MIC06A、链接却指向 MIC05 页 —— 客户点进去得自己找。

    输入:tab / q(该 Tab 的问卷定义)/ recommendation(仅 Tab3 有)/ answers(【清洗后的】本次答案,用来查行业页)。
    产出:[{"label":..,"url":..}, ...],按 URL 去重保序;没有就 []。
    """
    extra = q.get("links", [])          # Tab 级固定链接(知识库/佐证页,widget.json 里配)
    industry = industry_links_for(answers)   # 行业落地页(按"在哪用/什么行业"那题的选项映射;没对应页就是 [])
    out = []
    if tab == "book-demo":
        rl = q.get("result_link")
        if rl:
            out.append(rl)
        out += industry + extra
    elif tab == "help-me-choose":
        # 推荐型号的详情页:标签写死在 widget.json 的 links 里(如 "See MIC06"),不再按 products 拼——
        # 写死才不会出现"标签列了两个型号、链接只有一个"的错配。
        out += (recommendation or {}).get("links") or []
        out += industry + extra
    else:
        out = industry + extra   # odm / add-branding:行业页 + 问卷里配好的链接(标签本就有描述性)
    # 按 URL 去重(推荐链接/行业页可能与某条 Tab 链接指向同一页),保序。
    seen, deduped = set(), []
    for l in out:
        u = (l or {}).get("url")
        if u and u not in seen:
            seen.add(u)
            deduped.append(l)
    return deduped


@router.post("/questionnaire")
async def questionnaire(req: QuestionnaireReq):
    """
    用户答完某个 Tab 的问卷后的提交入口(和 /event 按钮、/chat 打字并列)。会调一次大模型出方案。
    每一步干啥 / 产出什么见下面的 step 注释。
    """
    # 0) 校验 Tab 合法:tab 必须是 widget.json 里定义过的 4 个之一,否则 400(别拿未知 tab 建脏会话)。
    #    产出:q = 该 Tab 的问卷定义(intro/题目/链接),后面用它渲染摘要、取链接。
    q = QUESTIONNAIRES.get(req.tab)
    if not q:
        raise HTTPException(status_code=400, detail="unknown questionnaire tab")

    # 1) 清洗答案:只留问卷定义里真实存在的题 + 合法选项(防 prompt injection / 前端脏值,见 _sanitize_answers)。
    #    之后所有步骤(查推荐 / 存会话 / 摘要)都用这份【干净的】answers,不再碰 req.answers 原始值。
    answers = _sanitize_answers(q, req.answers)

    # 2) 确保会话存在(首次就在这建),并记来源页 page_url、语言 lang 进 meta。
    STORE.get_or_create(req.session_id, {"page_url": req.page_url, "lang": req.lang})

    # 3) 累积"入口意图" = 这个 Tab(按到达顺序追加、去重;连做多份问卷会记下走过的每个 Tab,
    #    第一个仍是主归因)。Slack 卡的"入口"行、LLM 的开场上下文都看它。
    STORE.set_entry_intent(req.session_id, req.tab)

    # 4) (仅 Tab3 help-me-choose)按【清洗后的】答案查推荐规则,算出推荐产品/链接/hint;其它 Tab 无推荐(None)。
    #    产出:recommendation dict 或 None。它会存进会话对应 Tab 的桶,供 LLM 出方案 + 前端取链接。
    recommendation = recommend_for(answers) if req.tab == "help-me-choose" else None

    # 5) 把答案 + 推荐写进会话【对应 Tab 的桶】(同 Tab 覆盖、换 Tab 新增,多份问卷并存)。之后每轮由
    #    llm._system → prompts.questionnaire_line 把所有做过的 Tab 一起注入系统提示,所以 GPT 后续全程
    #    看得到这些选择 → 顺着出方案、且绝不重复问答过的题。
    STORE.set_questionnaire(req.session_id, req.tab, answers, recommendation)

    # 6) 把答案渲染成人类可读摘要,并作为一条 user turn 追加进对话:
    #    ── 为什么当 user turn:LLM 的 respond() 是"看着对话窗口回话",需要一个触发点。这条摘要
    #       就是那个触发(相当于用户说"我选了这些,给我方案")。它只进服务器内存(前端不靠它渲染,
    #       前端自己有选择的显示副本),几轮后随滑动窗口自然滚掉,而结构化答案已在 step5 长期留在会话里。
    summary = _summarize_answers(q, answers)
    STORE.append_turn(req.session_id, "user", summary)

    # 7) Slack:确保有线索卡 + 把问卷摘要发进 thread 归档(📋 前缀标明是问卷提交)。
    await slack.ensure_card(STORE, req.session_id)
    await slack.post_detail(STORE, req.session_id, f"📋 {summary}")

    # 8) 跑大模型出【方案】+ 归档 AI 回复到 thread + 刷新线索卡 + 算这轮要甩的直连渠道。
    #    直接复用 /chat 那套 _reply_and_archive(前置条件"用户输入已 append"在 step6 已满足)。
    #    产出:reply = 量身方案文本;throwbacks = 要甩回的直连渠道(通常问卷这步为空)。
    reply, throwbacks = await _reply_and_archive(req.session_id)

    # 8b) 确定性追加 follow-up:模型只可靠地出"推荐"那一段(实测它会忽略 prompt 里"再追问一句"的要求),
    #     所以这句"想多说点需求 + 留个联系方式,真人尽快跟进"由后端补,保证【每次问卷后一定出现】。
    #     用空行分隔 → 前端 splitReply 会渲染成【第二个气泡】(方案一个、追问一个)。文案在 widget.json,可改。
    followup = CONFIG.get("questionnaire_followup")
    if followup:
        reply = (reply + "\n\n" + followup) if reply else followup

    # 9) 算出这个 Tab 要展示给用户的链接(推荐型号页 / 行业落地页 / Book-a-demo 日历 / Tab 固定链接)。
    links = _questionnaire_links(req.tab, q, recommendation, answers)

    # 10) 归因题:问卷方案给完就问(force=True,不看聊了几轮)。一个会话只问一次。
    ask_source = _ask_source_flag(req.session_id, force=True)

    # 产出:{reply: 方案, contacts: 甩链, links: 展示链接, ask_source: 是否附上归因题}
    return {"reply": reply, "contacts": throwbacks, "links": links, "ask_source": ask_source}


# ============================================================================
# 归因("你从哪知道我们的")—— 客源渠道指标
#   为什么要:Slack 卡上的"来源"是访客的落地页 URL(机器能看到的那半,GA4 也有),而展会、口碑、
#   朋友推荐、线下这些【机器看不到】的渠道只能问。这题补的正是那半盲区。
#   为什么不必填:见 _should_ask_source 的口径说明(必填 = 随手蒙 = 噪音数据)。
#   前端:收到 ask_source=true 才渲染那排选项;用户点了才 POST 到这里,不点就什么也不发生。
# ============================================================================
class SourceReq(BaseModel):
    # 例:{"session_id":"sess_ab12","option_id":"linkedin"}
    #     {"session_id":"sess_ab12","option_id":"other","text":"a colleague recommended you"}
    session_id: str
    option_id: str                 # 必须是 widget.json source_question.options 里定义过的 id
    text: str | None = None        # 只有 free_text 那个选项(Other)才用得上;其余忽略


@router.post("/source")
async def source(req: SourceReq):
    """
    记录访客自述的"从哪知道我们的"。不调大模型(纯确定性数据),秒回。

    步骤:
      1) 校验 + 归一成最终字符串:固定选项取【配置里的 label】(不信前端传的文字),Other 才收自由文本并截断
         —— 端点是公开的,而这个值会进 Slack 卡片(见 widget_config.source_value)。不认识的 option_id → 400。
      2) 会话不存在 → 400(归因必须挂在一段真实对话上,不给它建空会话)。
      3) 写进会话(顺带标记已问)+ 发进 Slack thread 归档 + 刷新线索卡(卡上多一行"获知渠道")。
    产出:{"ok":true, "value":最终存的字符串, "thanks":致谢话术} → 前端渲染成一句 bot 回应。
    """
    # 1) 校验 + 归一(不合法直接 400,别把脏值写进卡片)
    value = source_value(req.option_id, req.text)
    if not value:
        raise HTTPException(status_code=400, detail="unknown source option")

    # 2) 必须已有会话(归因是对话的附属信息,不单独建会话)
    if not STORE.snapshot(req.session_id):
        raise HTTPException(status_code=400, detail="unknown session")

    # 3) 落库 + 归档
    STORE.set_source(req.session_id, value)
    await slack.post_detail(STORE, req.session_id, f"📣 获知渠道: {value}")
    await slack.update_card(STORE, req.session_id)
    return {"ok": True, "value": value, "thanks": SOURCE_QUESTION.get("thanks", "")}


# ============================================================================
# 官网 contact 表单 → Slack
#   gmic.ai 的 /contact-gmic-ai/ 那张询盘表单(Meng 的静态页,以前只是假装成功、从不发送)
#   现在由 WordPress 侧的 mu-plugin 先存成一条记录,再【服务器对服务器】转发到这里,
#   由我们发进 #web-bot —— 于是表单、聊天、语音三个入口的线索都落在同一个频道。
#   为什么不让浏览器直接打这个端点:那样这个 token 就得写在页面里,等于公开。
# ============================================================================
CONTACT_FORM_TOKEN = os.getenv("CONTACT_FORM_TOKEN", "")


class ContactFormReq(BaseModel):
    # 例:{"token":"…","name":"Will de Hoon","contact_type":"WhatsApp","contact_value":"+15550001234",
    #      "company":"Enzover",
    #      "industry":"MedTech","volume":"2,000 - 10,000","project":"Need a branded recorder",
    #      "page_url":"https://gmic.ai/contact-gmic-ai/","referrer":"https://www.google.com/"}
    token: str
    name: str
    contact_type: str              # Email / WhatsApp / WeChat / Telegram / Phone(表单里选的类型)
    contact_value: str             # 对应的值。WP 侧已按类型校验过一遍,这里只查非空
    company: str | None = None
    industry: str | None = None
    volume: str | None = None
    project: str | None = None
    page_url: str | None = None
    referrer: str | None = None
    submitted: str | None = None


@router.post("/contact-form")
async def contact_form(req: ContactFormReq):
    """
    收 WordPress 转来的表单询盘 → 发一张 Slack 卡。不建会话、不调大模型。

    鉴权 = 一个共享 token(WP 侧存在 .env / WP option 里)。**没配 token 就一律拒绝**(fail closed):
    这个端点会往团队频道发消息,开着等于给人一个刷 Slack 的口子。
    """
    if not CONTACT_FORM_TOKEN or req.token != CONTACT_FORM_TOKEN:
        raise HTTPException(status_code=403, detail="bad token")
    if not req.name.strip() or not req.contact_value.strip():
        raise HTTPException(status_code=400, detail="name and contact_value required")

    await slack.post_form_card(req.model_dump())
    log.info("contact form forwarded to Slack: %s %s (%s)", req.contact_type, req.contact_value, req.company)
    return {"ok": True}


# ============================================================================
# 语音留言(独立功能,已从聊天里拆出来)
#   产品变更:聊天变【纯文字】,语音改成 contacts 行里的"🎙️ 语音留言"——先必填一种联系方式,
#   再长按录音发出。为什么这么设计:联系方式【打字】输入(可靠),语音只承载"需求描述",
#   于是彻底绕开"语音听错邮箱字母"这个老坑(见 [[feedback_phone-asr-letters]])。
#   两个端点:/voice/transcribe(录完预览用,只转写)+ /voice/message(真正发送:联系方式必填→Slack)。
# ============================================================================
@router.post("/voice/transcribe")
async def voice_transcribe(
    audio: UploadFile = File(...),
    lang: str | None = Form(None),
):
    """
    只做转写,不建会话、不发 Slack。给浮窗"录完 → 显示可编辑文字"用(微信式,用户可改错再发)。
    步骤:读音频(限读防 OOM)→ STT(失败吞异常当没转出)→ 回 {transcript}(可能空串)。
    真正入库归档在 /voice/message 那一步做。
    """
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)   # 限读:见 /voice/message 里同款说明
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")
    try:
        transcript = await stt.transcribe(audio_bytes, filename=audio.filename or "voice.webm",
                                          language=lang or None)
    except Exception:
        log.exception("voice/transcribe stt failed")
        transcript = ""
    return {"transcript": transcript}


@router.post("/voice/message")
async def voice_message(
    session_id: str = Form(...),
    contact_type: str = Form(...),     # email / phone / whatsapp / wechat / telegram
    contact_value: str = Form(...),    # 用户填的联系方式值(必填)
    audio: UploadFile = File(...),
    text: str | None = Form(None),     # 前端编辑后的最终留言文字(可空→服务端自己转一次)
    page_url: str | None = Form(None),
    lang: str | None = Form(None),
):
    """
    发送一条语音留言 = 一条高质量线索(联系方式打字保证可靠 + 语音需求)。
    步骤:
      0) 读音频 + 大小闸门(限读到上限+1,防超大上传先吃满 RAM 再拒的 OOM)
      1) 【服务端】校验联系方式必填 + 格式——绝不只信前端 gate(前端可被绕过)。不合格 → 400
      2) 最终留言文字:优先用前端编辑后的 text;没有就服务端自己转一次(容错,失败也不 500)
      3) 建/取会话 → 种 entry_intent="voice-message" → 回填 lead(联系方式 + need=留言)
      4) Slack:建卡 + 刷新 + 原始音频&文字进 thread 归档(和聊天线索卡同一套渲染)
      5) 回 {ok, transcript}——前端弹个简单确认即可(不调 LLM,它是留言不是对话)
    """
    # 0) 读音频 + 大小闸门
    audio_bytes = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")

    # 1) 校验联系方式(服务端兜底)
    lead_fields = _validate_contact(contact_type, contact_value)
    if not lead_fields:
        raise HTTPException(status_code=400, detail="a valid contact is required")

    # 2) 最终留言文字:优先前端编辑后的;缺了才自己转
    transcript = (text or "").strip()
    if not transcript:
        try:
            transcript = await stt.transcribe(audio_bytes, filename=audio.filename or "voice.webm",
                                              language=lang or None)
        except Exception:
            log.exception("voice/message stt failed for session %s", session_id)
            transcript = ""

    # 3) 建会话 + 回填线索。need 用转写原文(用户说什么语言就是什么语言,不翻译);转不出给中文占位。
    STORE.get_or_create(session_id, {"page_url": page_url, "lang": lang})
    STORE.set_entry_intent(session_id, "voice-message")
    lead_fields["need"] = transcript or "(语音留言 — 见附件录音)"
    STORE.update_lead(session_id, lead_fields)

    # 4) Slack:发线索卡 + 原音频进 thread。
    #    注:lead 已在上一步 update_lead 填好,ensure_card 发出来的卡就是完整的 → 不再 update_card(去冗余、
    #    也少一次 Slack 调用,对并发有利)。thread 里的音频【不再重复转写文字】(卡上"留言"已有),避免刷两遍。
    await slack.ensure_card(STORE, session_id)
    await slack.post_detail(STORE, session_id, "🎤 原始录音", audio_bytes=audio_bytes,
                            filename=audio.filename or "voice.webm")

    # audio_bytes 是本次请求局部变量,函数返回后自动释放,绝不长期驻留内存/磁盘
    return {"ok": True, "transcript": transcript}
