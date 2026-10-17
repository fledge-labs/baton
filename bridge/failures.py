# SPDX-License-Identifier: Apache-2.0
"""Closed failure vocabulary: raw subprocess, model and transport text never crosses it."""

import errno
import json
import re
from dataclasses import dataclass

FAILURE_KEY = "baton_failure"
FAILURE_VERSION = 1

CATALOG = {
    "CODEX_AUTH_UNAUTHORIZED": ("authentication", "模型服务拒绝了 worker 认证；请核对或更新认证副本。"),
    "CODEX_AUTH_REFRESH_FAILED": ("authentication", "worker 认证刷新失败；需要重新认证。"),
    "CODEX_MODEL_FORBIDDEN": ("model_access", "worker 账号无法使用配置的模型；请核对模型权限。"),
    "CODEX_CAPACITY_LIMITED": ("capacity", "模型服务的额度、速率或容量限制阻止了本次执行。"),
    "MODEL_CAPACITY": ("capacity", "所选模型当前容量不足；请等待恢复或选择可用模型。"),
    "CODEX_SANDBOX_FAILED": ("sandbox", "worker 无法启动或使用任务沙箱；请核对沙箱权限。"),
    "CODEX_CONFIG_INVALID": ("configuration", "worker 命令或配置不可用；请核对部署配置。"),
    "WORKER_RESOURCE_EXHAUSTED": ("session_start", "worker 创建线程或进程失败；请核对 PID、线程及系统资源限制。"),
    "WORKER_FD_EXHAUSTED": ("session_start", "worker 文件描述符耗尽；请核对打开文件数与限制。"),
    "WORKER_MEMORY_EXHAUSTED": ("session_start", "worker 内存分配失败；请核对可用内存与容器限制。"),
    "CODEX_TIMEOUT": ("execution", "worker 执行超过时间限制，子进程已停止。"),
    "CODEX_OUTPUT_MISSING": ("execution", "worker 未产生可用的最终结果。"),
    "CODEX_EXECUTION_FAILED": ("execution", "worker 执行失败，现有诊断无法匹配已知错误；未保留原始错误正文。"),
    "CODEX_IDEMPOTENCY_CONFLICT": ("replay", "同一任务标识对应不同输入，已拒绝重新执行。"),
    "CODEX_PRIOR_EXECUTION_UNRESOLVED": ("replay", "已有执行未成功或被中断；必须人工核对，禁止自动重跑。"),
    "A2A_FAILED_UNCLASSIFIED": ("execution", "worker 报告失败，但未提供可接受的安全诊断。"),
    "WORKER_HEALTHY": ("health_check", "资源采样与最近一次真实执行检查均通过。"),
    "WORKER_HEALTH_UNKNOWN": ("health_check", "worker 健康状态尚未验证。"),
    "WORKER_HEALTH_UNAVAILABLE": ("health_check", "无法获得有效的 worker 健康检查结果。"),
    "WORKER_RESOURCE_STALE": ("health_check", "worker 资源采样已过期。"),
    "WORKER_PID_PRESSURE": ("session_start", "worker PID/线程使用量已接近限制。"),
    "WORKER_MEMORY_PRESSURE": ("session_start", "worker 内存使用量已接近限制。"),
    "WORKER_CANARY_BUSY": ("health_check", "worker 正在执行其它工作，本轮尚未执行健康探针。"),
    "WORKER_CANARY_RUNNING": ("health_check", "worker 健康探针正在执行，结果尚未确定。"),
    "WORKER_CANARY_STALE": ("health_check", "worker 上次真实执行检查已过期。"),
    "WORKER_CANARY_REQUIRED": ("health_check", "资源告警后尚未通过新的真实执行检查。"),
    "WORKER_CANARY_INTERRUPTED": ("health_check", "上次健康探针被中断；同一探针标识不会重跑。"),
    "WORKER_CANARY_INVALID_RESULT": ("health_check", "健康探针未返回规定结果，不能确认执行能力。"),
}
STAGES = frozenset(stage for stage, _ in CATALOG.values())
HEALTH_STATES = frozenset({"healthy", "degraded", "error", "unknown", "busy", "checking"})
# Only these codes may be produced by the worker and accepted by the bridge.
WORKER_CODES = frozenset(code for code in CATALOG if code.startswith("CODEX_")) | {
    "MODEL_CAPACITY", "WORKER_RESOURCE_EXHAUSTED", "WORKER_FD_EXHAUSTED", "WORKER_MEMORY_EXHAUSTED",
}
# A failed A2A task without an acceptable diagnostic is still a definite failure.
TASK_FAILURE_CODES = WORKER_CODES | {"A2A_FAILED_UNCLASSIFIED"}


@dataclass(frozen=True)
class Failure:
    code: str

    def __post_init__(self):
        if not isinstance(self.code, str) or self.code not in CATALOG:
            raise ValueError("Unknown failure code")

    @property
    def stage(self):
        return CATALOG[self.code][0]

    def display(self):
        return self.code + ": " + CATALOG[self.code][1]

    def payload(self):
        return {"version": FAILURE_VERSION, "code": self.code, "stage": self.stage}

    def report(self):
        return "任务失败。\n阶段：" + self.stage + "\n" + self.display()


class FailureError(RuntimeError):
    def __init__(self, failure):
        if not isinstance(failure, Failure):
            raise TypeError("Expected safe failure")
        self.failure = failure
        super().__init__(failure.display())


def _diagnostic_text(stdout, stderr):
    messages = []
    # --json separates CLI error events from prompts, model text and tool output.
    # Never classify arbitrary content from any other event type.
    for line in stdout[-262144:].splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        detail = event if event.get("type") == "error" else (
            event.get("error") if event.get("type") == "turn.failed" else None)
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            messages.append(detail["message"][:16384])
    stderr_text = stderr[-65536:].decode("utf-8", errors="replace")
    if re.search(r"(?m)^user\s*$", stderr_text):
        # A human transcript is not an authenticated CLI diagnostic source.
        # --json should never render one; retain only typed JSON errors.
        return "\n".join(messages).lower()
    panic_lines = 0
    for line in stderr_text.splitlines():
        lowered = line.lower()
        # A normal bundled-bubblewrap fallback is not a sandbox failure.
        if re.search(r"\bwarn(?:ing)?\b|falling back|fallback", lowered):
            continue
        own_error = re.match(r"\s*(?:error:|fatal:|failed to (?:create|initialize) session:|"
                             r"thread ['\"].{0,160} panicked at)", lowered)
        tracing_error = re.search(r"\berror\b\s+(?:codex[_:]|reqwest[_:]|tokio[_:])", lowered)
        if own_error or tracing_error or panic_lines:
            messages.append(line[:16384])
        if "panicked at" in lowered and own_error:
            panic_lines = 2
        elif panic_lines:
            panic_lines -= 1
    return "\n".join(messages).lower()


def classify_codex_exit(stderr, *, stdout=b"", returncode, has_output):
    """Bounded heuristic over typed CLI errors; the matched text is never retained."""
    text = _diagnostic_text(stdout, stderr)
    if re.search(r"too many open files|\bemfile\b|\benfile\b|os error 24\b", text):
        return Failure("WORKER_FD_EXHAUSTED")
    if re.search(r"cannot allocate memory|out of memory|\benomem\b|os error 12\b", text):
        return Failure("WORKER_MEMORY_EXHAUSTED")
    if (re.search(r"resource temporarily unavailable|\beagain\b|os error 11\b", text)
            and re.search(r"(?:spawn|create|start|fork).{0,60}(?:thread|process|session)|"
                          r"(?:thread|process|session).{0,60}(?:spawn|create|start)|"
                          r"failed to (?:load agents\.md|initialize session)", text)):
        return Failure("WORKER_RESOURCE_EXHAUSTED")
    if re.search(r"refresh[_ -]?token.{0,100}(?:used|expired|invalid|revoked|failed)|"
                 r"(?:failed|unable) to refresh|token_refresh_failed", text):
        return Failure("CODEX_AUTH_REFRESH_FAILED")
    if re.search(r"\b401\b|\bunauthorized\b|invalid_api_key", text):
        return Failure("CODEX_AUTH_UNAUTHORIZED")
    if re.search(r"model_not_found|unsupported_model|model_access_denied|"
                 r"model.{0,100}(?:not supported|not found|not available|do not have access)|"
                 r"(?:no access|not have access).{0,60}model|\b403\b.{0,30}forbidden", text):
        return Failure("CODEX_MODEL_FORBIDDEN")
    if re.search(r"(?:selected )?model is at capacity|model.{0,40}capacity exceeded", text):
        return Failure("MODEL_CAPACITY")
    if re.search(r"\b429\b|too many requests|rate[_ -]?limit|insufficient_quota|"
                 r"usage limit|quota exceeded|overloaded|capacity limit|resource_exhausted", text):
        return Failure("CODEX_CAPACITY_LIMITED")
    if re.search(r"(?:bwrap|bubblewrap|sandbox|landlock|seccomp).{0,100}(?:failed|error|denied|cannot)|"
                 r"(?:failed|error|denied|cannot).{0,100}(?:bwrap|bubblewrap|sandbox|landlock|seccomp)", text):
        return Failure("CODEX_SANDBOX_FAILED")
    if re.search(r"error parsing (?:configuration|config)|invalid (?:configuration|config|toml)|"
                 r"failed to (?:load|read).{0,40}config|unknown (?:permissions profile|flag|option)|"
                 r"unexpected argument", text):
        return Failure("CODEX_CONFIG_INVALID")
    if returncode == 0 and not has_output:
        return Failure("CODEX_OUTPUT_MISSING")
    return Failure("CODEX_EXECUTION_FAILED")


def worker_exception_failure(error):
    if isinstance(error, FailureError):
        return error.failure
    if isinstance(error, TimeoutError):
        return Failure("CODEX_TIMEOUT")
    if isinstance(error, OSError):
        if error.errno == errno.EAGAIN:
            return Failure("WORKER_RESOURCE_EXHAUSTED")
        if error.errno in (errno.EMFILE, errno.ENFILE):
            return Failure("WORKER_FD_EXHAUSTED")
        if error.errno == errno.ENOMEM:
            return Failure("WORKER_MEMORY_EXHAUSTED")
        if error.errno in (errno.EPERM, errno.EACCES):
            return Failure("CODEX_SANDBOX_FAILED")
        if error.errno == errno.ENOENT:
            return Failure("CODEX_CONFIG_INVALID")
    return Failure("CODEX_EXECUTION_FAILED")


def failure_from_a2a(status):
    """Accept only our versioned data part; descriptions and text are never trusted."""
    fallback = Failure("A2A_FAILED_UNCLASSIFIED")
    message = status.get("message") if isinstance(status, dict) else None
    parts = message.get("parts") if isinstance(message, dict) else None
    if not isinstance(parts, list):
        return fallback
    accepted = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "data":
            continue
        data = part.get("data")
        if not isinstance(data, dict) or FAILURE_KEY not in data:
            continue
        payload = data[FAILURE_KEY]
        if (set(data) != {FAILURE_KEY} or not isinstance(payload, dict)
                or set(payload) != {"version", "code", "stage"}
                or type(payload["version"]) not in (int, float) or payload["version"] != FAILURE_VERSION
                or not isinstance(payload["code"], str) or payload["code"] not in WORKER_CODES):
            return fallback
        failure = Failure(payload["code"])
        if payload["stage"] != failure.stage:
            return fallback
        accepted.append(failure)
    return accepted[0] if len(accepted) == 1 else fallback
