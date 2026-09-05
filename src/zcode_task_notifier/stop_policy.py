"""将来源事实收敛为需要通知的实际停顿状态。

本模块不调用模型，也不把自然语言中的单个词当作状态。来源适配器应先
提供结构化状态和本回合是否存在真实用户任务的最小证据；正文只用于在
结构化事实不足时确认“明确等待/明确完成/明确失败”。
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Literal


StopStatus = Literal["completed", "error", "awaiting_approval", "awaiting_input"]


@dataclass(frozen=True)
class StopDecision:
    """一次来源记录的停顿判定；``status is None`` 表示推进游标但不通知。"""

    status: StopStatus | None
    reason: str
    stop_reason: str | None = None
    plan_fingerprint: str | None = None


_WAITING_STATES = frozenset(
    {"waiting", "awaiting", "paused", "blocked", "needs_input", "needs_approval"}
)
_RUNNING_STATES = frozenset(
    {"running", "in_progress", "working", "started", "queued", "pending"}
)
_COMPLETED_STATES = frozenset({"completed", "complete", "done", "success", "succeeded"})
_ERROR_STATES = frozenset({"error", "failed", "failure", "aborted", "cancelled", "canceled"})

_SYSTEM_ONLY_MARKERS = (
    "harness 规则",
    "harness规则",
    "agents.md",
    "copilot-instructions",
    "环境上下文",
    "推荐插件",
    "插件列表",
    "等待具体任务",
    "等待第一项任务",
    "等待任务",
    "已就绪",
    "就绪",
)
_APPROVAL_RE = re.compile(
    r"(?:等待|请|需要|待)(?:你的|你|用户的|用户|您)?(?:明确)?"
    r"(?:回复|给出)?[\s'\"“”‘’「」]*"
    r"(?:确认|同意|批准|审批|授权|许可)"
    r"(?:后|再|才|即可|方可)?"
)
_CHOICE_RE = re.compile(
    r"(?:请(?:你|您)?|需要(?:你|您|用户)|等待(?:你|您|用户))\s*(?:选择|选定|决定|择一)"
)
_INPUT_RE = re.compile(
    r"(?:请(?:你|您)?|需要你|等待你|请先)(?:提供|补充|输入|填写|回复|回答|说明|指定)"
    r"|(?:^|[。！？\n])\s*(?:当前)?(?:缺少|需要补充).{0,20}(?:输入|信息|参数)"
)
_FAILURE_RE = re.compile(
    r"^(?:任务|执行|操作|处理|请求|编译|测试|检查|安装|提交|保存|运行|构建)"
    r".{0,32}(?:失败|出错|错误|异常|无法|未能|中止|终止)"
)
_PROGRESS_RE = re.compile(
    r"^(?:我先|先(?:检查|查看|读取|分析)|正在|目前|接下来|稍后|继续|处理中|已开始)"
)
_FORMAL_PLAN_RE = re.compile(
    r"^\s*<proposed_plan>\s*[\s\S]+?</proposed_plan>\s*$",
    re.IGNORECASE,
)
_NEGATED_WAIT_RE = re.compile(
    r"(?:不|无需|无须|不用|不必).{0,5}(?:确认|同意|批准|审批|授权|选择|提供|补充|回复)"
)
_REFERENCE_PREFIX_RE = re.compile(
    r"^\s*(?:>|[`'\"“‘]|(?:文档|示例|例如|引用|计划中提到))"
)
_RECOVERED_OUTCOME_RE = re.compile(
    r"(?:已|已经|当前|最终|但|现在).{0,24}(?:完成|修复|通过|成功|交付|验证)"
)


def _normalise(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _state(value: object) -> str:
    return _normalise(value).casefold().replace("-", "_").replace(" ", "_")


def _actual_task(user_input: object, has_user_task: bool | None) -> bool | None:
    if has_user_task is not None:
        return bool(has_user_task)
    text = _normalise(user_input)
    if not text:
        # 没有输入字段的旧格式仍可按明确最终答案兼容判断；等待类事件
        # 在调用方会被保守地忽略，不能把结构化 task_complete 当作唯一证据。
        return None
    lowered = text.casefold()
    if any(marker.casefold() in lowered for marker in _SYSTEM_ONLY_MARKERS):
        return user_task_evidence(text)
    return True


def user_task_evidence(value: object) -> bool:
    """判断 role=user 文本是否是真实任务，而不是 Harness 封装上下文。"""
    text = _normalise(value)
    if not text:
        return False
    # rollout 中的系统封装可能嵌在 role=user 的 response_item 内容里；先去掉
    # 明确边界块，再判断剩余文本，避免 AGENTS 中的“修改/测试”动词误报。
    stripped = re.sub(
        r"<(?:INSTRUCTIONS|environment_context|recommended_plugins)>[\s\S]*?</(?:INSTRUCTIONS|environment_context|recommended_plugins)>",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(r"^\s*#\s*(?:AGENTS\.md|CLAUDE\.md|copilot-instructions)[^\n]*", " ", stripped, flags=re.IGNORECASE | re.MULTILINE)
    stripped = stripped.strip()
    if not stripped:
        return False
    lowered = stripped.casefold()
    if any(marker.casefold() in lowered for marker in _SYSTEM_ONLY_MARKERS):
        # 混合封装和真实请求时只接受明确的用户句首，不让 AGENTS 正文
        # 中的“修改/测试/完成”等普通动词变成任务证据。
        return bool(
            re.search(
                r"(?:^|\n)\s*(?:请|帮我|我要|希望|需要)(?:你|您)?\S*",
                stripped,
            )
        )
    return True


def _fingerprint(text: str) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text[-6000:].encode("utf-8")).hexdigest()[:24]


def _has_explicit_failure(text: str, structured: str) -> bool:
    if not text:
        return structured in _ERROR_STATES
    # 只接受以“任务/执行/编译...”为主语的明确结果，避免引用、计划和否定
    # 句中的“失败/错误”把仍在运行的回合误报为失败。
    if "不失败" in text or "未失败" in text or "没有失败" in text:
        return False
    failure = _FAILURE_RE.search(text)
    if failure is None:
        return structured in _ERROR_STATES
    # 计划/引用中可能先描述旧失败，最终又明确说明已经修复；最终结果
    # 优先，避免整段摘要中的“失败”覆盖完成状态。
    if _RECOVERED_OUTCOME_RE.search(text[failure.end() :]):
        return False
    return True


def _waiting_kind(text: str) -> tuple[StopStatus | None, str | None]:
    if _REFERENCE_PREFIX_RE.search(text):
        return None, None
    if _NEGATED_WAIT_RE.search(text):
        return None, None
    if _APPROVAL_RE.search(text):
        return "awaiting_approval", "approval_required"
    if _CHOICE_RE.search(text):
        return "awaiting_input", "choice_required"
    if _INPUT_RE.search(text):
        return "awaiting_input", "input_required"
    return None, None


def classify_stop(
    final_text: str = "",
    *,
    structured_status: str | None = None,
    has_user_task: bool | None = None,
    user_input: str | None = None,
    event_type: str | None = None,
    explicit_final: bool | None = None,
    resumed: bool = False,
) -> StopDecision:
    """分类一次来源回合。

    ``has_user_task=False`` 是适配器根据真实记录得出的强证据，优先于正文。
    没有该字段时只保留旧格式中明确的最终结果；不能仅凭
    ``task_complete`` 或“计划”一词生成通知。
    """

    text = _normalise(final_text)
    state = _state(structured_status)
    event = _state(event_type)
    actual_task = _actual_task(user_input, has_user_task)
    if actual_task is False:
        return StopDecision(None, "no_actual_task")

    if resumed or state in _RUNNING_STATES:
        return StopDecision(None, "running")

    is_waiting = state in _WAITING_STATES or event in {
        "task_waiting",
        "awaiting_input",
        "awaiting_approval",
        "task_paused",
        "request_user_input",
    }
    # 工具仍在运行是强证据；仅明确阻塞交互可在没有最终回复时通知。
    if explicit_final is False and not is_waiting:
        return StopDecision(None, "not_final")

    if _has_explicit_failure(text, state):
        return StopDecision("error", "error", "error")

    if is_waiting:
        if actual_task is None:
            return StopDecision(None, "no_actual_task")
        if event == "request_user_input":
            return StopDecision(
                "awaiting_input",
                "input_required",
                "input_required",
                None,
            )
        waiting_status, stop_reason = _waiting_kind(text)
        if waiting_status is None:
            return StopDecision(None, "not_explicit_wait")
        return StopDecision(
            waiting_status,
            stop_reason or "waiting",
            stop_reason,
            _fingerprint(text) if waiting_status == "awaiting_approval" else None,
        )

    has_system_marker = any(marker.casefold() in text.casefold() for marker in _SYSTEM_ONLY_MARKERS)
    if actual_task is not False and not (actual_task is None and has_system_marker) and _FORMAL_PLAN_RE.match(text):
        return StopDecision(
            "awaiting_approval",
            "approval_required",
            "approval_required",
            _fingerprint(text),
        )

    # ZCode 终态索引没有 waiting 状态；明确的“请确认/请选择”语义仍须
    # 优先于 completed 标签，但不接受否定句或普通计划叙述。
    if actual_task is not False and not (actual_task is None and has_system_marker):
        explicit_waiting, explicit_reason = _waiting_kind(text)
        if explicit_waiting is not None:
            return StopDecision(
                explicit_waiting,
                explicit_reason or "waiting",
                explicit_reason,
                _fingerprint(text) if explicit_waiting == "awaiting_approval" else None,
            )

    if state in _COMPLETED_STATES or event in {"task_complete", "completed", "turn_complete"}:
        if explicit_final is False:
            return StopDecision(None, "not_final")
        # 旧格式没有用户输入时仅接受明确结果性正文；不会因 task_complete
        # 这个结构化标签单独通过。
        if not text:
            return StopDecision(None, "empty_final")
        if _PROGRESS_RE.search(text):
            return StopDecision(None, "progress")
        # “计划如下/步骤如下”只是中间方案，即使旧索引已标成 completed，
        # 也不能在没有明确结果语义时当作已完成。
        if re.match(r"^(?:实施)?计划(?:如下|为)|^步骤如下", text) and not re.search(
            r"(?:已完成|已经完成|执行完毕|通过测试|修复完成|提交成功)", text
        ):
            return StopDecision(None, "progress")
        if has_user_task is None and (
            _PROGRESS_RE.search(text)
            or any(marker.casefold() in text.casefold() for marker in _SYSTEM_ONLY_MARKERS)
            or re.search(r"(?:等待具体任务|仍在运行|稍后继续|正在处理|处理中)", text)
        ):
            return StopDecision(None, "not_explicit_final")
        if any(marker.casefold() in text.casefold() for marker in _SYSTEM_ONLY_MARKERS):
            return StopDecision(None, "no_actual_task")
        return StopDecision("completed", "completed", "completed")

    if _waiting_kind(text)[0] is not None:
        return StopDecision(None, "missing_waiting_structure")
    return StopDecision(None, "progress")


__all__ = ["StopDecision", "StopStatus", "classify_stop", "user_task_evidence"]
