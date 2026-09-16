"""渠道实时执行步骤卡片流式器（飞书/钉钉共用基类）。

本模块管理"正在执行"过程卡片（trace card）的完整生命周期：AgentLoop
执行会话期间，通过 event_sink 钩子（:meth:`TraceStreamer.on_event`）持续
接收 Harness v2 事件，把事件渲染为终端用户可读的步骤行，并在后台
worker 线程中节流调用渠道适配器创建/更新卡片。

整体数据流::

    AgentLoop.handle_turn(event_sink=streamer.on_event)
        └─ on_event(event_type, payload)          # 主线程，必须快速返回
             ├─ _ingest_event                     # 事件 → trace 行（锁内累积）
             └─ _maybe_enqueue_patch              # 节流后入队更新任务
                  └─ worker 线程执行 _Task
                       ├─ _CreateCardTask → 适配器 create_card（幂等键防重）
                       └─ _PatchCardTask → 适配器 update_card（全量替换）

关键设计约束：

- **不阻塞会话**：所有渠道 HTTP I/O（含重试 sleep）都在 daemon worker
  线程执行；start/on_event/finish/abort 仅操作内存状态与任务队列后
  立即返回，绝不 join worker，避免渠道网络异常拖住会话锁。
- **失败隔离**：卡片创建/更新失败只记日志、绝不上抛——卡片是 turn 的
  附属展示，正文回复走 outbox 有独立重试，卡片失败不能影响 turn 成功。
- **幂等创建**：卡片创建携带幂等键（子类 ``_idempotency_key``），
  渠道侧据此对重复创建去重。
- **节流更新**：增量更新间隔不小于 ``_MIN_UPDATE_INTERVAL`` 秒，规避
  渠道卡片更新限流；行数超过 ``_MAX_LINES`` 时截断尾部历史。
"""

from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import UTC, datetime
from typing import Any

from app.db.models import ChannelBinding, GeneralSkill, Skill, Tool

logger = logging.getLogger(__name__)

# 卡片更新最小间隔（秒）：规避渠道卡片更新限流。
_MIN_UPDATE_INTERVAL = 1.0
# 单张卡片最多展示的步骤行数，超出截断尾部历史。
_MAX_LINES = 60
# 卡片创建/最终定格的瞬时失败重试：网络抖动一次就放弃会导致整轮无卡片
# （正文回复走 outbox 有重试，卡片没有就出现"有回复无卡片"的不对称）。
# 重试判据为异常的 retryable 属性（Feishu/DingTalk 错误类的共同约定）；
# 中间内容更新不重试——下一个事件的全量推送天然自愈。
_CARD_RETRY_DELAY = 3.0
_CARD_MAX_ATTEMPTS = 4

# SOP 紧凑展示模式：匹配 SOP（进入流程）后隐藏中间步骤，仅展示一行
# "翻书动画 + 正在推进SOP"。渠道卡片不支持动效，翻书动画通过节流更新
# 轮换下列 emoji 帧模拟（每 _MIN_UPDATE_INTERVAL 秒翻一帧）。
# 等待用户补充信息（awaiting_user / SOP 挂起）时定格为"📖 流程已暂停"，
# SOP 结束定格为"✅ 流程已结束"，异常定格为"❌ 流程未完成"。
_SOP_FLIP_FRAMES = ("📖", "📗", "📘", "📙", "📕")
# 紧凑模式合成行 id（不在事件行中存在，渲染时动态追加）。
_SOP_PROGRESS_LINE_ID = "__sop_compact_progress__"

# 无 SOP 匹配（闲聊/普通咨询）轮次，Router 的 reason/user_intent 可能夹带
# 内部英文枚举（如 conversation、answer_only），这些文本会原样展示在终端
# 用户的渠道卡片上。已知内部枚举映射为中文说法，其余英文词直接删除；
# 「SOP」与用户原文中的英文（被复述）保留。
_NO_SOP_TRACE_TERM_LABELS = {
    "conversation": "普通对话",
    "answer_only": "直接回答",
    "chitchat": "闲聊",
    "smalltalk": "闲聊",
    "clarify": "澄清",
    "handoff_human": "转人工",
    "continue_active": "继续当前流程",
    "start_new_task": "启动新任务",
    "switch_to_pending": "切换待办任务",
    "create_pending": "新建待办任务",
    "update_pending": "更新待办任务",
    "complete_task": "结束任务",
    "router": "路由",
    "task_frame": "任务",
    "task_frames": "任务",
}
_TRACE_ENGLISH_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
_TRACE_CJK_CLASS = r"\u4e00-\u9fff，。；、：！？"


class _SinkEvent:
    """轻量 AgentEvent 替身，仅供 _event_trace_lines 渲染使用。

    EventLog.record 的 sink 收到的是 (event_type, payload_dict)，而
    _event_trace_lines 读取 event.event_type / event.payload_json / event.id /
    event.created_at 四个字段。这里用一个最小对象补齐，避免构造完整 ORM 行。
    """

    __slots__ = ("created_at", "event_type", "id", "payload_json")

    def __init__(self, event_type: str, payload: dict[str, Any]) -> None:
        self.event_type = event_type
        self.payload_json = payload
        self.id = str(payload.get("turn_id") or payload.get("user_message_id") or "")
        self.created_at = datetime.now(tz=UTC)


def _load_skill_trace_names(
    db, tenant_id: str
) -> tuple[dict[str, str], dict[str, dict[str, str]], dict[str, str]]:
    """加载租户维度的技能/步骤/工具显示名映射，用于卡片行文案本地化。

    返回三元组：

    - ``skill_names``: ``skill_id → 技能名``
    - ``step_names``: ``skill_id → {node_id → 节点名}``（取自 SOP
      ``content_json.nodes``，仅保留 node_id 与 name 均非空的节点）
    - ``tool_names``: 工具名 / ``general_skill.{slug}`` → 展示名

    查询异常不在此处捕获，由调用方 ``_ensure_trace_names`` 统一记日志。
    """
    from sqlmodel import select

    rows = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    skill_names: dict[str, str] = {}
    step_names: dict[str, dict[str, str]] = {}
    for row in rows:
        skill_names[row.skill_id] = row.name
        content = row.content_json if isinstance(row.content_json, dict) else {}
        steps: dict[str, str] = {}
        for node in content.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("node_id") or "").strip()
            node_name = str(node.get("name") or "").strip()
            if node_id and node_name:
                steps[node_id] = node_name
        if steps:
            step_names[row.skill_id] = steps
    tool_names = _load_tool_display_names(db, tenant_id)
    return skill_names, step_names, tool_names


def _load_tool_display_names(db, tenant_id: str) -> dict[str, str]:
    """加载工具与通用技能的展示名映射。

    - ``Tool`` 表：``display_name`` 优先，缺省回退 ``description``；
    - ``GeneralSkill`` 表：以 ``general_skill.{slug}`` 为键（与工具调用
      事件中的工具名命名约定对齐），值为技能名。
    """
    from sqlmodel import select

    tool_names: dict[str, str] = {}
    for row in db.exec(select(Tool).where(Tool.tenant_id == tenant_id)).all():
        display = str(row.display_name or "").strip()
        if not display:
            display = str(row.description or "").strip()
        name = str(row.name or "").strip()
        if name and display:
            tool_names[name] = display
    for row in db.exec(select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id)).all():
        slug = str(row.slug or "").strip()
        name = str(row.name or "").strip()
        if slug and name:
            tool_names[f"general_skill.{slug}"] = name
    return tool_names


class TraceStreamer:
    """渠道实时执行步骤卡片流式器基类（飞书/钉钉共用）。

    生命周期：
      start()  → 后台创建"正在执行"卡片，保存卡片实例标识
      on_event → 累积 trace 行，节流后后台更新卡片
      finish() → 定格为完成状态，等待后台 worker 排空
      abort()  → 异常路径定格为失败状态

    所有 HTTP I/O 在后台 worker 线程执行，on_event 不阻塞调用方
    （即 AgentLoop 主线程）。start/finish/abort 同样非阻塞：
    finish/abort 仅入队最终状态任务后立即返回，不 join worker，
    避免渠道网络异常拖住会话锁。worker 为 daemon 线程，进程退出时
    自动结束；最终卡片更新由 worker 异步完成，失败仅记日志。

    全程 try/except 隔离：卡片创建/更新失败仅记日志，绝不抛出，不影响 turn
    成功与正文回复投递。

    子类需实现：
      channel_name           渠道标识（适配器注册名 + worker 线程名）
      _idempotency_key()     卡片创建幂等键（渠道侧据此派生卡片标识）
      _compact_sop_setting() SOP 紧凑展示的全局默认开关
      _render_card()         行列表 + 状态 → 渠道卡片数据结构

    适配器需提供与 FeishuAdapter 同签名的鸭子类型方法：
      create_card(binding, target, card, *, idempotency_key) -> 卡片标识
      update_card(binding, card_id, card) -> None
    """

    # 子类覆盖：渠道名（适配器注册名）。
    channel_name: str = ""

    def __init__(
        self,
        binding: ChannelBinding,
        target: dict[str, Any],
        turn_id: str,
        *,
        adapter: Any | None = None,
        skill_names: dict[str, str] | None = None,
        step_names: dict[str, dict[str, str]] | None = None,
        tool_names: dict[str, str] | None = None,
        db=None,
        user_message: str | None = None,
        min_update_interval: float = _MIN_UPDATE_INTERVAL,
        compact_sop: bool | None = None,
        card_retry_delay: float = _CARD_RETRY_DELAY,
    ) -> None:
        """初始化流式器。

        :param binding: 渠道绑定（租户/渠道配置来源）。
        :param target: 渠道投递目标（message_id / chat_id / receive_id_type
            等），原样透传给适配器 ``create_card``。
        :param turn_id: 本轮会话 ID（幂等键与日志追踪用）。
        :param adapter: 渠道适配器；``None`` 时首次使用经注册表懒加载。
        :param skill_names / step_names / tool_names: 显示名映射；
            ``None`` 时按需从 ``db`` 懒加载（见 ``_ensure_trace_names``）。
        :param db: 用于懒加载显示名的 SQLModel 会话（可选，测试可省略）。
        :param user_message: 用户原始消息文本；无 SOP 轮次的文案净化
            需要据此保留用户原文中出现过的英文。
        :param min_update_interval: 卡片更新最小间隔（秒），测试可调小。
        :param compact_sop: SOP 紧凑展示开关；``None`` 时按
            "显式传参 > binding 配置 > 子类全局默认" 优先级解析。
        :param card_retry_delay: 卡片瞬时失败重试间隔（秒），测试可调小。
        """
        self._binding = binding
        self._target = dict(target or {})
        self._turn_id = str(turn_id or "").strip()
        self._adapter = adapter
        self._skill_names = dict(skill_names or {})
        self._step_names = dict(step_names or {})
        self._tool_names = dict(tool_names or {})
        self._db = db
        self._user_message = str(user_message or "")
        self._min_update_interval = max(0.1, float(min_update_interval))
        self._card_retry_delay = max(0.0, float(card_retry_delay))
        self._message_id: str | None = None
        self._lines: list[dict] = []
        self._skill_hint: str | None = None
        self._names_loaded = False
        self._lock = threading.Lock()
        self._last_update_at = 0.0
        self._dirty = False
        self._finished = False
        self._started = False
        self._final_state: str | None = None
        self._draining = False
        self._card_created = False
        # SOP 紧凑展示：None 时按全局设置 + binding 配置解析。
        self._compact_sop = self._resolve_compact_sop(compact_sop)
        # SOP 生命周期标记（主线程写、worker 线程读，锁内更新）。
        self._sop_started = False
        self._sop_finished = False
        self._sop_paused = False
        # 翻书动画帧号：仅 worker 线程读写。
        self._animation_frame = 0

        # 后台 worker
        self._task_queue: queue.Queue[_Task | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._worker_started = False

    # ---- 子类钩子 ----

    def _idempotency_key(self) -> str:
        """返回卡片创建幂等键（子类必须实现）。

        通常由渠道标识 + binding_id + turn_id 派生；渠道侧据此识别
        重复的创建请求（如 worker 重启后重投）并返回同一张卡片。
        """
        raise NotImplementedError

    def _compact_sop_setting(self) -> bool:
        """返回 SOP 紧凑展示的系统级默认开关（子类必须实现）。"""
        raise NotImplementedError

    def _render_card(
        self,
        *,
        lines: list[dict] | None = None,
        state: str = "running",
    ) -> Any:
        """把行列表 + 状态渲染为渠道卡片数据结构（子类必须实现）。

        :param lines: 步骤行列表；``None`` 表示渲染时取流式器当前累积
            的全部行（子类需自行处理 hidden 过滤等渲染逻辑）。
        :param state: 卡片整体状态（running/completed/failed），通常
            映射到卡片头部的状态文案与颜色。
        """
        raise NotImplementedError

    def _resolve_compact_sop(self, compact_sop: bool | None) -> bool:
        """解析紧凑展示开关的最终取值。

        优先级：显式传参 > ``binding.config_json.compact_trace``
        （仅显式 ``False`` 关闭）> 子类全局默认 ``_compact_sop_setting``。
        """
        if compact_sop is not None:
            return bool(compact_sop)
        config = self._binding.config_json if isinstance(self._binding.config_json, dict) else {}
        if config.get("compact_trace") is False:
            return False
        return self._compact_sop_setting()

    # ---- 后台 worker ----

    def _start_worker(self) -> None:
        """启动后台 worker 线程（daemon，进程退出时自动结束）；幂等。"""
        if self._worker_started:
            return
        self._worker_started = True
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"{self.channel_name}-trace-worker",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        """worker 主循环：串行执行任务队列，空闲轮询驱动动画与退出。

        ``queue.get`` 带 0.05s 超时，超时（空闲）时检查两件事：

        1. draining 且队列已空 → 正常退出（finish/abort 后的排空路径）；
        2. SOP 翻书动画到期 → 入队一次补帧更新（紧凑模式下即使无新
           事件，卡片也在"动"，给用户"正在推进"的感知）。

        单个任务抛异常仅记日志，worker 不退出，后续任务继续执行——
        一次卡片更新失败不应中断最终定格。
        """
        while True:
            try:
                task = self._task_queue.get(timeout=0.05)
            except queue.Empty:
                # finish/abort 已调用且队列排空：worker 可退出
                if self._draining and self._task_queue.empty():
                    return
                # SOP 紧凑展示：无事件到达期间也周期性更新，推进翻书动画帧
                if self._should_animate():
                    self._enqueue_animation_tick()
                continue
            if task is None:
                return
            try:
                task.execute(self)
            except Exception:
                logger.exception(
                    "%s trace worker 任务执行失败 binding=%s turn=%s",
                    self.channel_name,
                    self._binding.id,
                    self._turn_id,
                )
            # 任务执行后再次检查：若已在 draining 且队列空，退出
            if self._draining and self._task_queue.empty():
                return

    def _stop_worker(self, *, timeout: float = 0.0) -> None:
        """标记 worker 进入 draining 状态。

        默认 timeout=0 表示不阻塞等待（用于 finish/abort 路径）：
        仅设置 _draining 标志，worker 在处理完已排队任务（含最终状态
        更新及 _do_create_card 的补发任务）后自行退出。
        timeout>0 时阻塞 join 指定秒数（仅测试场景使用）。
        """
        if not self._worker_started or self._worker is None:
            return
        self._draining = True
        if timeout > 0:
            self._worker.join(timeout=timeout)
            self._worker_started = False
            self._worker = None

    # ---- adapter / skill names ----

    def _ensure_adapter(self):
        """返回渠道适配器；未显式注入时按 ``channel_name`` 从注册表懒加载。"""
        if self._adapter is not None:
            return self._adapter
        from app.channels.adapters.base import get_channel_adapter

        self._adapter = get_channel_adapter(self.channel_name)
        return self._adapter

    def _ensure_skill_names(self) -> dict[str, str]:
        """返回 ``skill_id → 技能名`` 映射（首次调用时从 db 懒加载）。"""
        self._ensure_trace_names()
        return self._skill_names

    def _ensure_step_names(self) -> dict[str, dict[str, str]]:
        """返回 ``skill_id → {node_id → 节点名}`` 映射（首次调用时懒加载）。"""
        self._ensure_trace_names()
        return self._step_names

    def _ensure_tool_names(self) -> dict[str, str]:
        """返回工具/通用技能展示名映射（首次调用时懒加载）。"""
        self._ensure_trace_names()
        return self._tool_names

    def _ensure_trace_names(self) -> None:
        """一次性从 db 加载三类显示名映射。

        加载失败仅记日志并放弃（``_names_loaded`` 不置位，下次重试），
        后续渲染继续使用原始 skill_id/工具名，卡片功能不中断。
        显式传入的映射优先，懒加载结果不覆盖非空字典。
        """
        if self._db is None or self._names_loaded:
            return
        try:
            skill_names, step_names, tool_names = _load_skill_trace_names(
                self._db, self._binding.tenant_id
            )
        except Exception:
            logger.exception(
                "%s trace 流式器加载技能名称失败 tenant=%s",
                self.channel_name,
                self._binding.tenant_id,
            )
            return
        self._names_loaded = True
        if not self._skill_names:
            self._skill_names = skill_names
        if not self._step_names:
            self._step_names = step_names
        if not self._tool_names:
            self._tool_names = tool_names

    # ---- 生命周期 ----

    def start(self) -> None:
        """启动流式器：拉起 worker 并入队建卡任务后立即返回。

        建卡在后台执行；卡片就绪前已到达的事件行不会丢失，由
        ``_do_create_card`` 完成后统一补发。重复调用幂等。
        """
        if self._started:
            return
        self._started = True
        self._start_worker()
        self._task_queue.put(_CreateCardTask())

    def on_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """AgentEvent sink 入口，由 AgentLoop 主线程逐事件调用。

        必须快速返回：只做内存态的行累积与节流入队，不做任何 I/O。
        事件处理失败仅记日志、绝不上抛——卡片是 turn 的附属展示，
        不能因渲染问题中断会话。``_finished`` 后静默丢弃（防止定格
        状态被迟到事件覆盖）。
        """
        if self._finished:
            return
        try:
            self._ingest_event(event_type, payload)
            self._maybe_enqueue_patch()
        except Exception:
            logger.exception(
                "%s trace 事件处理失败 binding=%s turn=%s event=%s",
                self.channel_name,
                self._binding.id,
                self._turn_id,
                event_type,
            )

    def finish(self) -> None:
        """成功路径定格：running 行全部翻转为 completed，入队最终更新。

        最终更新带 ``force=True``（瞬时失败会重试），确保卡片不会
        永远停在"正在执行"。不 join worker（见类文档），worker 处理完
        已排队任务后自行退出；若此刻卡片尚未创建，由 ``_do_create_card``
        检测 ``_final_state`` 补发最终状态。重复调用幂等。
        """
        if self._finished:
            return
        self._finished = True
        self._final_state = "completed"
        with self._lock:
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "completed"
        if self._message_id:
            self._task_queue.put(_PatchCardTask(state="completed", force=True))
        # 不 join worker：避免渠道网络异常阻塞会话锁。
        # worker 为 daemon 线程，会处理完已排队任务（含最终状态更新）后退出。
        # 卡片尚未创建时，_do_create_card 检测 _final_state 会补发最终状态。
        self._stop_worker()

    def abort(self, reason: str | None = None) -> None:
        """异常路径定格：running 行全部翻转为 failed，入队最终更新。

        与 :meth:`finish` 完全对称，仅终态不同；``reason`` 只写日志、
        不上卡片（避免内部错误细节透出给终端用户）。重复调用幂等。
        """
        if self._finished:
            return
        self._finished = True
        self._final_state = "failed"
        with self._lock:
            for line in self._lines:
                if line.get("state") == "running":
                    line["state"] = "failed"
        if self._message_id:
            self._task_queue.put(_PatchCardTask(state="failed", force=True))
        self._stop_worker()
        logger.info(
            "%s trace 流式器中止 binding=%s turn=%s reason=%s",
            self.channel_name,
            self._binding.id,
            self._turn_id,
            reason,
        )

    # ---- 事件处理 ----

    def _ingest_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """摄入单个事件，转换为 trace 行并在锁内累积。

        处理步骤：

        1. ``router_decision_created``：记录目标技能提示；无 SOP 轮次
           先净化展示文案（见 ``_sanitize_no_sop_router_payload``）；
        2. 复用 Web 控制台同一渲染逻辑 ``_event_trace_lines`` 产出
           0..n 行，保证渠道卡片与控制台轨迹口径一致；
        3. 锁内更新 SOP 生命周期标记（激活/暂停/结束）、按紧凑模式给
           中间步骤行打 ``hidden`` 标记、按行 id upsert、超过
           ``_MAX_LINES`` 截断尾部、置脏；
        4. 锁外更新技能上下文提示（供后续步骤行的上下文解析）。

        无行产出的事件（多数生命周期事件）仅更新技能上下文后返回。
        """
        if event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                self._skill_hint = target_skill_id
            if _router_decision_without_sop(payload):
                payload = _sanitize_no_sop_router_payload(payload, self._user_message)

        from app.api.chat import _event_trace_lines

        sink_event = _SinkEvent(event_type, payload)
        lines = _event_trace_lines(
            sink_event,
            self._ensure_skill_names(),
            self._skill_hint,
            self._ensure_step_names(),
            self._ensure_tool_names(),
        )
        if not lines:
            skill_context = _skill_context_from_payload(event_type, payload, self._skill_hint)
            if skill_context:
                self._skill_hint = skill_context
            return
        with self._lock:
            if not self._sop_started and _sop_activation_event(event_type, payload):
                self._sop_started = True
            # 暂停检测：frame 以 awaiting_user 结束（等待用户补充信息）或 SOP
            # 挂起 → 暂停；frame 重启 / SOP 恢复推进 → 取消暂停。
            pause_update = _sop_pause_update(event_type, payload)
            if pause_update is not None:
                self._sop_paused = pause_update
            # 紧凑模式下 SOP 推进期（进入流程之后、skill_completed 之前）的
            # 中间步骤行打 hidden 标记：数据仍全量累积，渲染时过滤，回滚开关
            # 关闭后即可恢复逐行展示。
            hide = self._compact_sop and self._sop_started and not self._sop_finished
            if event_type == "skill_completed":
                self._sop_finished = True
                self._sop_paused = False
            for line in lines:
                if hide and _sop_line_hidden(event_type, payload, line):
                    line = {**line, "hidden": True}
                _upsert_line(self._lines, line)
            if len(self._lines) > _MAX_LINES:
                self._lines = self._lines[-_MAX_LINES:]
            self._dirty = True

        skill_context = _skill_context_from_payload(event_type, payload, self._skill_hint)
        if skill_context:
            self._skill_hint = skill_context

    def _maybe_enqueue_patch(self) -> None:
        """节流入队一次增量卡片更新（仅当有脏行且间隔已到）。

        满足以下全部条件才入队：卡片已创建（有 message_id）、行有变化
        （``_dirty``）、距上次更新不小于 ``min_update_interval``。
        入队前在锁内清脏并取行快照，快照交由 worker 渲染，确保 worker
        侧不做任何加锁操作。被节流跳过的更新由下一个事件天然补上
        （更新为全量行快照，非增量）。
        """
        if not self._message_id:
            return
        with self._lock:
            if not self._dirty:
                return
            now = time.monotonic()
            if (now - self._last_update_at) < self._min_update_interval:
                return
            self._dirty = False
            self._last_update_at = now
            lines_snapshot = list(self._lines)
        self._task_queue.put(_PatchCardTask(lines=lines_snapshot, state="running", force=False))

    # ---- 卡片操作（在 worker 线程执行）----

    def _retry_call(self, fn, description: str):
        """带瞬时失败重试的卡片调用（worker 线程内执行，不阻塞主线程）。

        仅对异常带 retryable=True（两渠道 Transient 错误类的约定）的失败
        重试，永久错误（凭证无效/权限缺失）立即抛出；重试间 sleep 也发生在
        worker 线程，最坏拖慢卡片出现但不影响 turn 与正文回复。
        """
        for attempt in range(1, _CARD_MAX_ATTEMPTS + 1):
            try:
                return fn()
            except Exception as exc:
                if not bool(getattr(exc, "retryable", False)) or attempt == _CARD_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "%s trace %s 瞬时失败(第 %d/%d 次) %.1fs 后重试: %s",
                    self.channel_name,
                    description,
                    attempt,
                    _CARD_MAX_ATTEMPTS,
                    self._card_retry_delay,
                    exc,
                )
                if self._card_retry_delay > 0:
                    time.sleep(self._card_retry_delay)
        raise AssertionError("unreachable")

    def _do_create_card(self) -> None:
        """worker 线程：创建卡片并补发建卡期间积压的内容。

        创建成功后按当前进度二选一补发：

        - ``finish``/``abort`` 已调用（``_final_state`` 非空）→ 直接入队
          最终定格任务（处理"turn 极快结束、卡片还没建好"的竞态）；
        - 仍有脏行 → 入队一次 running 更新（处理"事件先于卡片就绪
          到达"的积压）。

        创建失败（重试耗尽）仅记日志：本轮无卡片，正文回复不受影响。
        ``finally`` 保证 ``_card_created`` 置位——该标志表示"建卡尝试
        已结束"（无论成败），供测试等待建卡路径执行完毕后断言。
        """
        try:
            adapter = self._ensure_adapter()
            card = self._render_card(state="running")
            self._message_id = self._retry_call(
                lambda: adapter.create_card(
                    self._binding,
                    self._target,
                    card,
                    idempotency_key=self._idempotency_key(),
                ),
                description="卡片创建",
            )
        except Exception:
            logger.exception(
                "%s trace 卡片创建失败 binding=%s turn=%s",
                self.channel_name,
                self._binding.id,
                self._turn_id,
            )
            self._message_id = None
        finally:
            self._card_created = True
        # 卡片创建成功后，处理累积行或最终状态
        if self._message_id:
            if self._final_state is not None:
                # finish/abort 已被调用，直接发送最终状态
                with self._lock:
                    lines_snapshot = list(self._lines)
                self._task_queue.put(
                    _PatchCardTask(lines=lines_snapshot, state=self._final_state, force=True)
                )
            else:
                with self._lock:
                    if self._dirty and self._lines:
                        self._dirty = False
                        self._last_update_at = time.monotonic()
                        lines_snapshot = list(self._lines)
                    else:
                        lines_snapshot = None
                if lines_snapshot is not None:
                    self._task_queue.put(
                        _PatchCardTask(lines=lines_snapshot, state="running", force=False)
                    )

    def _do_patch_card(self, lines: list[dict] | None, *, state: str, force: bool) -> None:
        """worker 线程：渲染并更新卡片内容（全量替换）。

        :param lines: 行快照；``None`` 表示取流式器当前全部行
            （最终定格/动画补帧场景）。
        :param state: 目标展示状态（running/completed/failed）。
        :param force: ``True`` 为最终定格——失败必须重试，否则卡片
            永远停在"正在执行"；``False`` 为中间增量更新——失败不
            重试，下一个事件的全量推送天然自愈。
        """
        if not self._message_id:
            return
        try:
            adapter = self._ensure_adapter()
            if lines is None:
                with self._lock:
                    lines = list(self._lines)
            card = self._render_card(lines=lines, state=state)
            if force:
                # 最终定格失败会让卡片永远停在"正在执行"，必须重试；
                # 中间更新不重试，下一个事件的全量推送天然自愈。
                self._retry_call(
                    lambda: adapter.update_card(self._binding, self._message_id, card),
                    description="卡片定格",
                )
            else:
                adapter.update_card(self._binding, self._message_id, card)
        except Exception:
            logger.exception(
                "%s trace 卡片更新失败 binding=%s card_id=%s",
                self.channel_name,
                self._binding.id,
                self._message_id,
            )

    # ---- SOP 紧凑展示（翻书动画） ----

    def _should_animate(self) -> bool:
        """worker 空闲时是否需要推进翻书动画。

        需同时满足：紧凑模式开启、SOP 已激活且未结束、未暂停等待
        用户输入、轮次未定格、卡片已创建。任一条件不满足时空闲
        轮询直接跳过，不产生额外的卡片更新请求。
        """
        return (
            self._compact_sop
            and self._sop_started
            and not self._sop_finished
            and not self._sop_paused
            and not self._finished
            and self._message_id is not None
        )

    def _enqueue_animation_tick(self) -> None:
        """按节流间隔入队一次空行更新以翻动动画帧（worker 线程内调用）。

        复用 ``_last_update_at`` 节流闸门（与事件驱动的更新互斥），
        防止动画帧与事件更新叠加超出渠道限流；``_PatchCardTask``
        不带行快照，渲染时取当前行并由 ``_compact_lines`` 拼上
        当前帧号的 emoji。
        """
        now = time.monotonic()
        with self._lock:
            if (now - self._last_update_at) < self._min_update_interval:
                return
            self._last_update_at = now
        self._animation_frame += 1
        self._task_queue.put(_PatchCardTask(state="running", force=False))

    def _compact_lines(self, lines: list[dict]) -> list[dict]:
        """紧凑模式渲染：过滤 hidden 行后按生命周期追加合成行。

        合成行取值优先级：轮次失败 → "流程未完成"（state=failed）；
        SOP 已结束或轮次成功 → "流程已结束"（state=completed）；暂停
        等待补充信息 → "📖 流程已暂停"（state 为空，跟书不跟对号，
        区别于已结束）；其余（推进中）→ 翻书动画帧 + "正在推进SOP"，
        帧号由 worker 的动画 tick 推进。状态图标由渲染器按 state
        补充（见 ``_state_icon``）。
        """
        visible = [line for line in lines if not line.get("hidden")]
        if self._final_state == "failed":
            visible.append(
                {"id": _SOP_PROGRESS_LINE_ID, "text": "流程未完成", "state": "failed"}
            )
        elif self._sop_finished:
            visible.append(
                {"id": _SOP_PROGRESS_LINE_ID, "text": "流程已结束", "state": "completed"}
            )
        elif self._sop_paused:
            # 暂停等待用户补充信息：跟书不跟对号，区别于已结束。
            visible.append(
                {
                    "id": _SOP_PROGRESS_LINE_ID,
                    "text": "📖 流程已暂停",
                    "detail": "等待用户补充信息后继续",
                    "state": "",
                }
            )
        elif self._final_state == "completed":
            visible.append(
                {"id": _SOP_PROGRESS_LINE_ID, "text": "流程已结束", "state": "completed"}
            )
        else:
            icon = _SOP_FLIP_FRAMES[self._animation_frame % len(_SOP_FLIP_FRAMES)]
            visible.append(
                {"id": _SOP_PROGRESS_LINE_ID, "text": f"{icon} 正在推进SOP", "state": ""}
            )
        return visible


# ---- 后台任务 ----


class _Task:
    """worker 线程执行的抽象任务。

    用任务对象而非裸函数，是为了把"快照参数"（行/状态/force）随任务
    封装入队，worker 取出后回调流式器对应的 ``_do_*`` 方法执行。
    """

    def execute(self, streamer: TraceStreamer) -> None:
        raise NotImplementedError


class _CreateCardTask(_Task):
    """建卡任务：调用 ``_do_create_card`` 创建"正在执行"卡片。"""

    def execute(self, streamer: TraceStreamer) -> None:
        streamer._do_create_card()


class _PatchCardTask(_Task):
    """卡片更新任务：调用 ``_do_patch_card`` 全量替换卡片内容。

    属性随任务快照化，避免 worker 执行时读到后续事件的新状态：
      lines  ``None`` 表示渲染时取流式器当前全部行（定格/动画补帧）；
      state  目标展示状态（running/completed/failed）；
      force  ``True`` 为最终定格（带瞬时失败重试），``False`` 为普通
             增量更新（失败不重试，靠后续事件自愈）。
    """

    __slots__ = ("force", "lines", "state")

    def __init__(self, *, lines: list[dict] | None = None, state: str = "running", force: bool = False) -> None:
        self.lines = lines
        self.state = state
        self.force = force

    def execute(self, streamer: TraceStreamer) -> None:
        streamer._do_patch_card(self.lines, state=self.state, force=self.force)


def _state_icon(state: str) -> str:
    """步骤行状态 → 图标：completed ✅ / failed ❌ / running ⏳ / 其他空。"""
    if state == "completed":
        return "✅"
    if state == "failed":
        return "❌"
    if state == "running":
        return "⏳"
    return ""


def _upsert_line(lines: list[dict], line: dict) -> None:
    """按行 id 就地合并更新；无 id 或未命中时追加到列表尾部。

    合并语义为浅合并（``{**existing, **line}``）：后续事件可对同一行
    补充 detail 字段或翻转 state（如步骤从 running → completed），
    而不丢失已有字段。行 id 相同视为同一逻辑步骤。
    """
    line_id = str(line.get("id") or "").strip()
    if line_id:
        for index, existing in enumerate(lines):
            if str(existing.get("id") or "") == line_id:
                lines[index] = {**existing, **line}
                return
    lines.append(line)


def _skill_context_from_payload(
    event_type: str,
    payload: dict[str, Any],
    skill_hint: str | None,
) -> str | None:
    """从技能生命周期事件中提取当前技能上下文（skill_id）。

    优先 ``to_skill_id``（切换目标），缺省回退 ``from_skill_id``，再
    回退现有 ``skill_hint``；非技能事件返回 ``None``（调用方据此保持
    上下文不变）。该上下文用于步骤行渲染时解析"属于哪个技能"。
    """
    if event_type in {"skill_started", "skill_resumed", "skill_step_changed"}:
        to_skill_id = str(payload.get("to_skill_id") or "").strip()
        from_skill_id = str(payload.get("from_skill_id") or "").strip()
        return to_skill_id or from_skill_id or skill_hint or None
    return None


def _sop_activation_event(event_type: str, payload: dict[str, Any]) -> bool:
    """判断事件是否意味着本轮已匹配 SOP（紧凑模式自此激活）。

    覆盖四种信号：技能直接启动/恢复、kind=sop 的任务帧启动、
    skill_state 的 runtimeDecision 为 start_skill/start_new_task、
    Router 决策携带 target_skill_id。命中任一即认为进入 SOP 推进期。
    """
    if event_type in {"skill_started", "skill_resumed"}:
        return True
    if event_type == "task_frame_started":
        return str(payload.get("kind") or "").strip() == "sop"
    if event_type == "skill_state":
        decision = str(payload.get("runtimeDecision") or "").strip()
        return decision in {"start_skill", "start_new_task"}
    if event_type == "router_decision_created":
        return bool(str(payload.get("target_skill_id") or "").strip())
    return False


def _sop_pause_update(event_type: str, payload: dict[str, Any]) -> bool | None:
    """紧凑模式暂停检测：返回 True（暂停）/ False（恢复）/ None（无变化）。

    暂停：frame 以 awaiting_user 结束（等待用户补充信息）、SOP 挂起。
    恢复：frame 重新开始、SOP 启动/恢复/推进、skill_state 回到 active。
    """
    if event_type == "task_frame_finished":
        return str(payload.get("status") or "").strip() == "awaiting_user"
    if event_type == "skill_state":
        states = [
            str(entry.get("state") or "").strip()
            for entry in payload.get("currentSkills") or []
            if isinstance(entry, dict)
        ]
        if "suspended" in states:
            return True
        if "active" in states:
            return False
        return None
    if event_type in {"skill_started", "skill_resumed", "skill_step_changed", "task_frame_started"}:
        return False
    return None


def _sop_line_hidden(event_type: str, payload: dict[str, Any], line: dict) -> bool:
    """紧凑模式下 SOP 推进期内的行是否隐藏。

    保留：判断意图（含"匹配 xx SOP"理由）与失败行（错误必须透出）。
    隐藏：进入流程及其后的全部中间步骤（推进流程、当前步骤、工具调用、
    任务整理等）；skill_completed 行本身也隐藏，由合成行"流程已结束"替代。
    """
    if str(line.get("state") or "") == "failed":
        return False
    return event_type not in {"router_decision_created", "general_skill_intent_checked"}


def _router_decision_without_sop(payload: dict[str, Any]) -> bool:
    """判断 router_decision_created 事件是否未匹配任何 SOP。

    顶层 ``target_skill_id`` 与所有任务帧（``task_frames``）的
    ``target_skill_id`` 均为空时，视为闲聊/普通咨询轮次——此时
    Router 文案需经净化后才能上卡片。
    """
    if str(payload.get("target_skill_id") or "").strip():
        return False
    for frame in payload.get("task_frames") or []:
        if isinstance(frame, dict) and str(frame.get("target_skill_id") or "").strip():
            return False
    return True


def _sanitize_no_sop_router_payload(
    payload: dict[str, Any], user_message: str | None
) -> dict[str, Any]:
    """净化无 SOP 轮次的 Router 决策展示文案。

    仅处理 ``user_intent`` 与 ``reason`` 两个字段，返回浅拷贝——
    只影响卡片渲染，不修改入库的事件 payload（控制台轨迹仍可见原文）。
    """
    sanitized = dict(payload)
    for field in ("user_intent", "reason"):
        value = str(sanitized.get(field) or "").strip()
        if value:
            sanitized[field] = _sanitize_no_sop_trace_text(value, user_message)
    return sanitized


def _sanitize_no_sop_trace_text(text: str, user_message: str | None) -> str:
    """删除文案中的英文词；保留「SOP」与用户原文中出现过的英文。

    背景：Router 的 reason/user_intent 可能夹带内部英文枚举
    （如 conversation、answer_only），会原样展示给终端用户。已知
    枚举映射为中文（``_NO_SOP_TRACE_TERM_LABELS``），未知英文词删除；
    用户原文出现过的词视为被复述的内容，原样保留。
    """
    if not text:
        return ""
    # 用户原文中的英文词白名单（小写化），复述内容不删。
    user_words = {
        word.lower() for word in _TRACE_ENGLISH_WORD.findall(user_message or "")
    }

    def _replace(match: re.Match) -> str:
        raw = match.group(0)
        key = raw.lower()
        # 「SOP」为产品术语恒保留；白名单词保留原文（保大小写）。
        if key == "sop" or key in user_words:
            return raw
        # 已知内部枚举 → 中文说法；未知英文词 → 删除。
        return _NO_SOP_TRACE_TERM_LABELS.get(key, "")

    sanitized = _TRACE_ENGLISH_WORD.sub(_replace, text)
    # 删除英文词后的残留清理：连续空白压一、中文字符前后的空格去掉、
    # 重复标点折叠（删除词后相邻逗号/句号会叠加）。
    sanitized = re.sub(r"[ \t]{2,}", " ", sanitized)
    sanitized = re.sub(rf"(?<=[{_TRACE_CJK_CLASS}]) +", "", sanitized)
    sanitized = re.sub(rf" +(?=[{_TRACE_CJK_CLASS}])", "", sanitized)
    sanitized = re.sub(rf"([{_TRACE_CJK_CLASS}])\1+", r"\1", sanitized)
    # 删除词首/词尾英文后可能残留悬挂标点：保留句尾句号，其余剥离。
    sanitized = sanitized.strip().lstrip("。，、；：！？").rstrip("，、；：！？")
    return sanitized
