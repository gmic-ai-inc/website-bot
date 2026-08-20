"""
会话存储 + 对话内存管控。

【两层记忆模型】
  - 工作记忆(本模块,放进程内存/RAM):只保存"当前正在进行"的对话,尽量小。
  - 归档(Slack):每一轮对话一产生就实时转发到 Slack,永久保存。
  因为 Slack 里什么都有,所以内存这层可以随时裁剪/清理,丢了也不丢数据。

【四道闸门控制内存】① 单会话轮数上限 ② TTL 超时清理 ③ 全局数量上限+LRU ④ 单条大小限制(在 api 层)

【主键 session_id】
  每个用户对应一个 session_id,它同时是:①本字典的 key ②Slack thread 的归属 ③前端浏览器身份。
  一个进程、一个字典、很多用户,靠 key 隔开,永远不会串。

【并发说明】后端是 FastAPI 异步(单进程事件循环)。本模块的方法都是"纯同步、内部不 await",
  所以在事件循环里天然是原子的(不会执行到一半被别的协程插进来)。那把 threading.Lock 因此
  基本用不到,但留着无害(万一以后引入线程池还能兜底)。
"""
import os
import re
import time
import asyncio
import threading
from collections import OrderedDict

# ---- 可调参数(从 .env 读,给了默认值)----
TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))     # 闲置多久算过期(默认 30 分钟)
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "500"))           # 全局最多同时保留几个会话
HISTORY_TURNS = int(os.getenv("LLM_HISTORY_TURNS", "8"))        # 每轮喂给大模型的最近几轮
# 内存里最多留几轮 = 默认等于喂给 LLM 的轮数。
#   为什么不多留:多出来的既不发给模型、前端也不靠服务器回滚(前端自己存显示副本),
#   而老对话又已经在 Slack 归档,所以"多留"没有意义。约束:必须 >= HISTORY_TURNS。
MAX_TURNS = int(os.getenv("MAX_TURNS_IN_MEMORY", str(HISTORY_TURNS)))

# lead(线索)里我们会存的【标量】字段(单值、非空覆盖)。missing 单独算,不在这里。
# messengers 是【列表】字段(用户可能留多个 IM),单独处理(做并集去重),不放这个元组。
_LEAD_FIELDS = ("name", "email", "phone", "company", "need")

# 【改动③】写入前的格式校验:光靠 prompt 说"别编造"不够稳,代码这里再兜一层。
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now():
    # 当前时间戳(秒)。单独包一层方便测试。
    return time.time()


def _valid_email(v):
    # 例:"a@x.com" -> True;"我的邮箱" / "a@x" -> False。不合格就不写进 lead。
    return bool(_EMAIL_RE.match(v.strip()))


def _valid_phone(v):
    # 抽出所有数字判断位数(容忍 +、空格、横杠)。例:"+1 (669) 900-0008" -> 11 位数字 -> True。
    digits = re.sub(r"\D", "", v)
    return 7 <= len(digits) <= 15


# messenger(IM 联系方式)平台识别:把一条 messenger 字符串归一到一个"平台 key",
# 用于 messengers 列表里【同平台只留最新一条】(见 update_lead 关键5)。
# canonical key 对齐 widget.json 的 contacts id(whatsapp/wechat/telegram),这样
# widget_config.match_our_channels 能用同一套平台判断找到我们现成的直连入口,不会两套逻辑漂移。
_PLATFORM_KEYWORDS = {
    "whatsapp":  ("whatsapp", "wa.me", "whats app"),
    "wechat":    ("wechat", "weixin", "微信"),
    "telegram":  ("telegram", "t.me"),
    "line":      ("line",),
    "signal":    ("signal",),
    "messenger": ("messenger", "facebook"),
    "viber":     ("viber",),
    "qq":        ("qq",),
    "imessage":  ("imessage",),
    "skype":     ("skype",),
    "kakao":     ("kakao",),
    "zalo":      ("zalo",),
}


def messenger_platform(s):
    """
    从一条 messenger 字符串里解析出"平台"(小写 key),用于同平台去重(留最新)。
    识别优先看【冒号前的标签】——我们让 LLM 按 "Platform: handle" 写(见 llm._JSON_CONTRACT),
    所以平台名通常就在冒号前;没冒号就拿整条来匹配关键词。
    例:"WeChat: luna_369" -> "wechat";"my whatsapp is +1.." -> "whatsapp";
        "Line: abc" -> "line";只有裸 handle、看不出平台 -> "other"。
    """
    s = (s or "").strip()
    if not s:
        return "other"
    head = s.split(":", 1)[0].lower() if ":" in s else s.lower()   # 平台名通常在冒号前
    for plat, kws in _PLATFORM_KEYWORDS.items():
        if any(k in head for k in kws):
            return plat
    # 没命中已知平台:把冒号前那段当自定义平台名(少见平台也能各占一格);实在没有 -> other
    label = s.split(":", 1)[0].strip().lower() if ":" in s else ""
    return label or "other"


class SessionStore:
    def __init__(self):
        # 用 OrderedDict 而非普通 dict:它记住顺序,能几乎零成本实现 LRU——
        # 刚用过的挪到末尾,要淘汰时从头部(最久没动的)删。
        self._data = OrderedDict()
        self._lock = threading.Lock()   # 见文件顶部"并发说明":基本用不到,留着兜底

    # ======================== 生命周期 ========================
    def get_or_create(self, sid, meta=None):
        """
        拿到某用户的会话;没有就新建。

        例:用户第一次点开 widget,前端带 session_id="sess_ab12" 进来 → 这里没有它 → 新建空会话。
            第二次说话,同一个 sid 再进来 → 直接返回上次那条,并刷新"最近活跃时间"。
        """
        with self._lock:
            s = self._data.get(sid)
            if s is None:
                s = {
                    "id": sid,
                    "created_at": _now(),
                    "last_seen": _now(),     # 相当于 updated_at:每次活动刷新,TTL/LRU 都看它
                    "entry_intents": [],     # 入口意图【列表】:用户每走一个入口(话题按钮 / 问卷 Tab / 语音留言)
                                             #   就累积一项;按到达顺序、去重(同一入口不重复记),见 set_entry_intent。
                                             #   entry_intents[0] = 主归因("最初从哪来",投放/转化用);后续项 = 走过的其它入口。
                                             #   注意:这只是"从哪些入口进来";对话中演变的真实诉求看 lead.need。
                    "answers": {},           # 问卷答案【按 Tab 分桶】:{tab: {题目id: 选中选项(字符串) 或 多选列表}}。
                                             #   由确定性前端收集、每个 Tab 一次性提交(见 /questionnaire、set_questionnaire):
                                             #   同一 Tab 再答 = 覆盖该桶;换个 Tab = 新增一桶 → 多份问卷并存、互不冲掉。
                                             #   之后每轮整体注入 LLM 系统提示 → bot 顺着答案出方案、绝不重复问答过的题。
                    "recommendations": {},   # 问卷推荐【按 Tab 分桶】:{tab: 推荐dict(products/link/hint)}。
                                             #   目前只有 help-me-choose(Tab3)按 recommend_rules 算推荐,故通常只有它一个键;
                                             #   其它 Tab 不产生推荐(不写入)。供 LLM 出方案 + 前端取链接。
                    "lead": {},              # 线索:边聊边回填的"一条"记录(不是每轮一条)
                    "source": None,          # 获知渠道(访客【自述】"你从哪知道我们的"):LinkedIn / Google search / 自由文本。
                                             #   ⚠️ 和 meta.page_url 不是一回事:page_url 是机器能看到的落地页(渠道的一半线索,
                                             #   GA4 那边也有),source 补的是机器【看不到】的那半——展会、口碑、朋友推荐、线下。
                                             #   刻意【不放进 lead】:lead 里的字段都是 LLM 每轮重抽的,放进去会被模型瞎猜覆盖;
                                             #   这个值只由用户点选/打字产生(见 set_source),是确定性数据。也【不进 missing】(非必填)。
                    "source_asked": False,   # 归因题问过没有(问一次就不再问,别烦人)。见 routes._should_ask_source。
                                             #   语义是"已经把题发出去了",不代表用户答了——答了才写 source。
                    "turns": [],             # 逐句对话:每说一句 append 一条,有上限
                    "slack_thread_ts": None, # 这通对话在 Slack 那条 thread 的根消息 ID(不是时间!)
                    "meta": meta or {},      # 附加信息:来源页面、语言等
                }
                self._data[sid] = s
                self._evict_over_cap_locked()   # 新增后可能超全局上限,顺手淘汰最久没用的
            else:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)     # 标记为"最近使用"(挪到末尾,LRU 用)
            return s

    def touch(self, sid):
        """只刷新活跃时间(比如用户点了跳转按钮,没产生对话,但人还在)。"""
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["last_seen"] = _now()
                self._data.move_to_end(sid)

    # ======================== 写入 ========================
    def set_entry_intent(self, sid, entry_intent):
        """
        累积一个"入口意图"(用户走过的一个入口:话题按钮 / 问卷 Tab / 语音留言)。
        规则:【按到达顺序追加、去重】——同一入口重复触发不重复记;第一个仍然排在最前(=主归因)。

        为什么改成累积(而非早期的"首次锁定单值"):一个用户可能先点 ODM 问卷、再点 Help me choose 问卷,
        每一个都是一次真实的入口/意图,应该都留下来(和 answers 按 Tab 分桶一一对应,不再出现
        "入口显示 odm 但答案却是 help-me-choose"的错位)。entry_intents[0] 依旧承担"最初从哪来"的
        归因语义(投放/转化),不会被后来的入口冲掉;后续项记录其它走过的入口。
        而"用户聊着聊着诉求变了"仍由 lead.need 承接(每轮 LLM 重抽、非空即覆盖):
        entry_intents=走过哪些入口(累积、去重),need=现在想要什么(随对话演变)。

        例:先点 [🏭 ODM] → entry_intents=["odm"];再点 [🧭 Help me choose] → ["odm","help-me-choose"];
            又点一次 ODM(重复)→ 仍是 ["odm","help-me-choose"](去重,不追加)。
        """
        with self._lock:
            s = self._data.get(sid)
            if s and entry_intent and entry_intent not in s["entry_intents"]:
                s["entry_intents"].append(entry_intent)   # 按到达顺序追加;已存在则不重复记(去重)

    def set_questionnaire(self, sid, tab, answers, recommendation=None):
        """
        存一次问卷的结果:确定性前端收集的【答案】+(仅 Tab3)按规则算出的【推荐】,【按 Tab 分桶】。

        为什么按 Tab 分桶(而非早期的"整包覆盖单份"):一个用户可能连做多份问卷(先 ODM、再选型),
        每份都该独立保留、互不冲掉。所以答案存进 answers[tab]、推荐存进 recommendations[tab]:
          - 同一个 Tab 再提交一次 → 覆盖该 Tab 那个桶(问卷是"答完一次性提交",单桶内不累加,覆盖即可);
          - 换一个 Tab 提交 → 新增一个桶,已有的桶原样保留。
        这和 set_entry_intent 的累积一一对应(走过哪些 Tab ↔ 每个 Tab 选了什么),不会再错位。

        和 entry_intents 的分工:entry_intents 记"走过哪些入口/Tab"(累积、去重),
        answers 记"在每个 Tab 里选了什么";两者一起构成后续对话的结构化上下文,喂给 LLM(见
        prompts.questionnaire_line),让 bot 顺着答案出方案、且绝不把答过的题再问一遍。

        输入:tab = 哪个 Tab(odm/add-branding/help-me-choose/book-demo);answers = {题目id: 选项};
              recommendation = 该 Tab 的推荐 dict(目前仅 help-me-choose 有),其它 Tab 传 None(不写入 recommendations)。
        产出:把答案/推荐写进对应 Tab 的桶,并刷新活跃时间(算一次活动,防被 TTL 清掉)。
        例:set_questionnaire("sess_x", "help-me-choose",
                             {"usage":"...phone calls","where":"..."}, {"products":["Telalive"],...})
            → answers={"help-me-choose":{...}}、recommendations={"help-me-choose":{...}}。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            s["answers"][tab] = dict(answers or {})    # 覆盖【该 Tab 的桶】(单桶一次性提交,不累加);其它桶不动
            if recommendation is not None:             # 有推荐(仅 Tab3)才写;其它 Tab 传 None → 不占桶
                s["recommendations"][tab] = recommendation
            s["last_seen"] = _now()
            self._data.move_to_end(sid)                # 标记最近使用(LRU)

    def append_turn(self, sid, role, text):
        """
        追加一轮对话(role = "user" 或 "assistant")。
        【改动②】每条 turn 只存 {role, text},不再存没用到的时间戳 ts。

        【闸门①:单会话轮数上限】只留最新 MAX_TURNS 轮,更早的已在 Slack 归档,内存里可丢。
        例:MAX_TURNS=4,已有 [m0,m1,m2,m3],又来 m4 →
            append 后 [m0,m1,m2,m3,m4](5 条)→ 超了 → 裁成 [m1,m2,m3,m4]。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            s["turns"].append({"role": role, "text": text})
            if len(s["turns"]) > MAX_TURNS:
                s["turns"] = s["turns"][-MAX_TURNS:]   # 只留末尾(最新)MAX_TURNS 条
            s["last_seen"] = _now()
            self._data.move_to_end(sid)

    def update_lead(self, sid, fields):
        """
        把新识别到的线索字段合并进"那一条" lead 记录,并重算 missing(还缺什么)。

        【关键1:lead 是一条记录、不断回填,不是每轮一条】
          初始    lead={} → 第1句 update_lead({need:"录音麦"}) → {need:"录音麦"}
          第2句 update_lead({email:"a@x.com"}) → {need:"录音麦", email:"a@x.com"}
        【关键2:非空才覆盖,空值不冲掉已有】所以支持"纠错"——
          用户先说 a@x.com,后说"写错了是 b@y.com" → LLM 提取新邮箱(非空)→ 覆盖成 b@y.com;
          某轮没提邮箱(提取为空)→ 跳过,不会把已存的 b@y.com 抹掉。
        【关键3:email/phone 写入前先校验格式】不合格就不写(代码兜底,不只信 prompt)。
          例:LLM 误把 "gmac" 当邮箱 → _valid_email 判 False → 不写 → missing 里仍标 contact → bot 继续追问。
        【关键4:什么算"必填(required)"→ 只有 need + 一种联系方式】missing 只追这两样;
          name/company 是"有就记、没有不追"的加分项,不进 missing。这套口径必须和 prompts.py
          PERSONA 目标 #2 一致(那里教 bot 追什么,这里决定卡片/lead_line 标什么缺),否则两边打架。
          联系方式 = email / phone / messengers(任一即可,不强制邮箱)。
          例:lead={need:"录音麦"} 但啥联系方式都没留 → missing=["contact"] → bot 追"留个联系方式吧";
              lead={email:"a@x.com"} 但没说想要啥 → missing=["need"] → bot 追"您具体想做什么产品?"。
        【关键5:messengers 是列表字段,按平台去重、同平台留最新】
          - 不同平台 → 并集都留:用户先报微信、后报 WhatsApp → 两条都在。
          - 同一平台再报 → 覆盖旧的只留最新:先说微信 wx_old、后纠正成 wx_new → 只留 wx_new。
          平台由 messenger_platform() 解析(见其注释)。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s or not fields:
                return
            for k in _LEAD_FIELDS:
                v = fields.get(k)
                if not v:
                    continue                       # 空值跳过(保护已有值,支持纠错)
                if k == "email" and not _valid_email(v):
                    continue                       # 邮箱格式不对,不写
                if k == "phone" and not _valid_phone(v):
                    continue                       # 电话位数不对,不写
                s["lead"][k] = v                   # 非空且合格 → 覆盖写入(取最新一次)
            # messengers(列表):按平台去重、同平台留最新。
            # 不同平台并集都留;同一平台再报则移除旧的、追加最新那条(纠错/补充都靠这)。
            incoming = fields.get("messengers")
            if incoming:
                existing = list(s["lead"].get("messengers", []))
                for m in incoming:
                    m = (m or "").strip()
                    if not m:
                        continue
                    plat = messenger_platform(m)
                    existing = [e for e in existing if messenger_platform(e) != plat]  # 去掉同平台旧记录
                    existing.append(m)                                                 # 追加最新一条
                if existing:
                    s["lead"]["messengers"] = existing
            # 重算"还缺哪些必填信息"。必填 = 【need(想要什么)】+【一种联系方式】。
            # 联系方式任一即可:email / phone / messengers(非空列表)。name/company 是加分项不进 missing。
            # 改这里务必同步 prompts.py PERSONA 目标 #2。
            has_contact = bool(s["lead"].get("email") or s["lead"].get("phone") or s["lead"].get("messengers"))
            missing = []
            if not s["lead"].get("need"):
                missing.append("need")
            if not has_contact:
                missing.append("contact")
            s["lead"]["missing"] = missing

    def mark_source_asked(self, sid):
        """
        标记"归因题已经发给这个访客了"(问一次就够,别每轮都弹)。

        为什么"发出去"就算问过、而不是"答了"才算:归因是 nice-to-have,发出去用户没理
        (Luna 的原话是"他不回答就不回咯")那就算了,不该下一轮又弹一次。用户刷新页面时
        session_id 从 localStorage 复用 → 这个标记在服务端,所以刷新也不会重复问。
        """
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["source_asked"] = True

    def set_source(self, sid, value):
        """
        写入访客自述的"获知渠道"(点了 LinkedIn / Google search / Other 后填的自由文本)。

        规则:【非空才覆盖】(同 update_lead 的口径,支持改口:先点 Other 又点 LinkedIn → 留后者);
              写入同时把 source_asked 置 True(答了当然算问过)。
        输入:value = 已经在路由层校验/截断过的字符串(选项 label 或自由文本)。
        例:set_source("sess_x", "LinkedIn") → session["source"]="LinkedIn"、source_asked=True。
        """
        with self._lock:
            s = self._data.get(sid)
            if not s:
                return
            value = (value or "").strip()
            if value:
                s["source"] = value
            s["source_asked"] = True          # 答了/点了 → 一定算问过
            s["last_seen"] = _now()
            self._data.move_to_end(sid)       # 算一次活动(LRU + 防 TTL 清掉)

    def set_slack_ts(self, sid, ts):
        """记住这通对话在 Slack 的 thread 根消息 ID(之后 chat.update 靠它精准改那张卡)。"""
        with self._lock:
            s = self._data.get(sid)
            if s:
                s["slack_thread_ts"] = ts

    # ======================== 读取 ========================
    def window(self, sid, n=None):
        """
        取"最近 n 轮"喂给大模型(滑动窗口)。

        为什么不喂全部:每轮都带整段历史会越聊越贵越慢。只带最近 n 轮;
        丢掉的老对话 Slack 里有,关键事实(entry_intent/lead)另存,不会丢。
        例:HISTORY_TURNS=8,对话 30 轮 → 只取最后 8 轮发给模型。
        """
        n = n or HISTORY_TURNS
        with self._lock:
            s = self._data.get(sid)
            return list(s["turns"][-n:]) if s else []

    def snapshot(self, sid):
        """返回会话的一份拷贝(只读用,避免外部直接改内部结构)。"""
        with self._lock:
            s = self._data.get(sid)
            return dict(s) if s else None

    def stats(self):
        """当前有多少个活跃会话(给 /health 用)。"""
        with self._lock:
            return {"sessions": len(self._data)}

    # ======================== 淘汰 ========================
    def _evict_over_cap_locked(self):
        """
        【闸门③:全局数量上限 + LRU】(调用方已持锁)
        活跃会话超过 MAX_SESSIONS 时,从头部(最久没动的)一直删到不超标。
        例:上限=500,第 501 个进来 → 删掉最久没人说话的那 1 个(它只是数据,不是线程,删了不影响别人)。
        """
        while len(self._data) > MAX_SESSIONS:
            self._data.popitem(last=False)     # last=False 删最旧的

    def sweep_expired(self):
        """
        【闸门②:TTL 超时清理】把闲置超过 TTL_SECONDS 的会话删掉。由后台协程定时调用。
        删了不丢数据(Slack 有归档)。返回这次清理掉的数量。
        例:TTL=1800(30分钟),某会话 last_seen 是 40 分钟前 → 判过期 → 删除。
        """
        cutoff = _now() - TTL_SECONDS
        with self._lock:
            dead = [sid for sid, s in self._data.items() if s["last_seen"] < cutoff]
            for sid in dead:
                self._data.pop(sid, None)
            return len(dead)


# 模块级单例:整个进程共用这一个存储(所有用户的会话都在这里)
STORE = SessionStore()


async def run_sweeper(interval=300):
    """
    后台 TTL 清理协程(async 版):每 interval 秒扫一次过期会话。
    由 app 启动时用 asyncio.create_task 拉起。

    用协程而不是线程的好处:跑在同一个事件循环里,不会被 debug/重载器杀掉
    (回避了之前 Flask 线程被杀的坑,见记忆 feedback_flask-debug-threads)。
    例:每 5 分钟醒一次 → 把 30 分钟没人理的会话从内存清掉。
    """
    while True:
        await asyncio.sleep(interval)
        try:
            STORE.sweep_expired()
        except Exception:
            pass   # 清理协程绝不能因一次异常把自己搞挂
