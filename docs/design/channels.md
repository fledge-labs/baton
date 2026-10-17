<!-- SPDX-License-Identifier: Apache-2.0 -->
# 人机交互通道：可插拔聊天渠道，飞书优先

状态：设计，**计划中**。本文档的内容一项也没有实现；今天能运行什么，以 [space-replica.md](space-replica.md) 和 [README](../../README.md) 的能力表为准。本文对应 owner 决定（2026-10-17，Baton 任务 cdb4f826，总工程师已批准按 3a 至 3e 拆分）中的 B4-3a，只交付设计，不含代码。里程碑见 [roadmap.md](roadmap.md)（W4-1 飞书与 Slack 桥、W3-1 人的身份、W2-4 Ask 对象）。

与既有文档的关系：[roadmap.md](roadmap.md) 的“Owner onboarding”写的是飞书只发提醒、没有按钮，W3-1 写的是在 web 看板上批准；owner 2026-10-17 的决定改为“人通过聊天平台与 Baton 交互，看板保持只读运维视图”。roadmap 与 space-replica 的相应改写不在本次变更的范围内，列在第 10 章的后续事项里，3b 至 3e 的 PR 各自负责自己那部分文档。

核验日期：2026-10-17（北瓜调研与审查修订：2026-10-17；2026-10-18 对照 `bridge/space_store.py` 里 `_mutate` 的提交顺序、`_comment` 的 `thread_id` 默认值，以及回调到达顺序修订）。官方来源（本文对飞书行为的陈述只依据这些页面；没有从官方页面确认的，文中标为“待核实”或 **NOT_PROVEN**）：

- 飞书服务端 SDK（Node.js）“处理回调”：<https://open.feishu.cn/document/server-side-sdk/nodejs-sdk/handling-callbacks?lang=zh-CN>
- 飞书服务端 SDK（Python）“处理回调”（同一页面的 Python 版）：<https://open.feishu.cn/document/server-side-sdk/python--sdk/handle-callbacks>
- 卡片回传交互回调 `card.action.trigger`（回调与响应结构、3 秒限制、错误码）：<https://open.feishu.cn/document/feishu-cards/card-callback-communication?lang=zh-CN>
- 卡片 JSON 2.0 结构、全量更新（`update_multi`、组件上限、频控）：<https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/cardkit-components/introduction>
- 全量更新卡片实体（`update_multi=true`、200 组件、10 次/秒/卡）：<https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/cardkit-v1/card/update>
- 流式更新卡片（审批卡不得使用流式模式）：<https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/streaming-updates>
- 更新已发送的消息卡片（`update_multi`、14 天、频控）：<https://open.feishu.cn/document/server-docs/im-v1/message-card/patch?lang=zh-CN>
- 发送消息（权限、`uuid` 去重、频控、卡片 30 KB）：<https://open.feishu.cn/document/server-docs/im-v1/message/create?lang=zh-CN>
- 接收事件（Encrypt Key 签名校验、事件解密、Verification Token）：<https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/encrypt-key-encryption-configuration-case?lang=zh-CN>
- 获取事件出口 IP：<https://open.feishu.cn/document/server-docs/event-subscription-guide/list?lang=zh-CN>
- 机器人被移出群事件：<https://open.feishu.cn/document/server-docs/group/chat-member/event/deleted?lang=zh-CN>
- 官方 **lark-channel-sdk（Python）**（`FeishuChannel`、WebSocket、`cardAction`）：<https://github.com/larksuite/channel-sdk-python>（README、`docs/reference.md`）
- 官方 **lark-oapi（Python）**（OpenAPI，含按 `message_id` 更新卡片等 channel-sdk 未封装的路径）：<https://github.com/larksuite/oapi-sdk-python>；PyPI：<https://pypi.org/project/lark-oapi/>
- Standard Webhooks 规范（W2-5 已合入 main）：<https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md>
- HumanLayer（“声明式审批”模式参考，不假定其 API）：<https://github.com/humanlayer/humanlayer>

## 1. 目标与非目标

**目标。**

- 人通过聊天平台与 Baton 交互，而不是登录 web 看板。飞书先做；Slack、企业微信、邮件之后再做，所以核心与平台无关，平台差异只在适配器里。
- **卡片只放结论和操作按钮，细节是链接。** 结论是一两句话加几个关键字段；diff、日志、完整线程都在链接指向的只读看板对象里。
- **不需要操作的通知是带链接的纯文本**，不做成卡片。
- 现有只读 `/board` 保留，作为运维视图；它不会获得写能力。聊天里的链接是 `/board` 上单个对象的限时只读视图（第 6 章）。
- 人在聊天里点按钮，等价于这个人自己调用审批接口：权限来自点击者本人的身份和委托链，与服务自己的身份无关（第 4、5 章）。
- 同一对象永远更新同一张卡片；决定之后，按钮原位替换成“已由 XX 于 HH:MM 通过/拒绝/要求修改”。
- 飞书侧默认不新增任何对外可达的入口：用飞书长连接（Baton 主动向外连接）接收事件与卡片回调（第 7 章）。

**非目标。**

- 不做聊天机器人式的自由文本指令，也不解析群里的普通消息。唯一的自由文本是 `request_changes` 的意见输入框。
- 不替代 W2-4 的 Ask 对象。通道只是把 Ask 或 gate 呈现给人并把回答带回 Baton；Baton 的状态是唯一事实来源，聊天里的卡片只是它的投影。
- 不使用飞书自带的审批产品，不把审批状态存在飞书。
- 卡片不提供“带修改批准”（approve-with-edits）；需要改动结果的人用 `request_changes` 写下意见，由执行方修改后重新提交。
- 不把聊天记录当作 Baton 的数据，不抓取群消息，不订阅消息接收事件。
- 不通过飞书做网页登录，也不依赖飞书是否签发 OIDC ID Token；人的身份绑定见第 5 章，OIDC 路径仍按 [identity-card.md](identity-card.md) 第 3 章单独处理。
- 本阶段不实现 Slack、企业微信、邮件。第 2 章只保证接口对它们不是死路；它们各自的平台限制没有在这里核实，动手前必须各自核实。
- 不做通道服务的多实例高可用（飞书长连接本身是“集群模式”，同一应用多个客户端只有随机一个收到消息，见第 7 章）。
- 不把审批同步为飞书原生“审批”应用实例；Baton 的 gate 与 `x_gate` 是唯一事实来源（见第 7 章）。
- `lark-cli`（`@larksuite/cli`）与 `lark-openapi-mcp` 留给 agent 或人在本机操作飞书文档/日程，**不进** Baton 服务依赖。

总体结构：

```
Baton bridge ──(Standard Webhooks，之后是 /events 流)──► channels core ──► adapter ──► 飞书
     ▲                                                     │   ▲              │
     │                                                     │   └─ parse_action ◄── 卡片回调（长连接）
     └──(以点击者本人身份的 gate / comment 请求)────────────┘
```

## 1.1 审批策略与渠道解耦

**声明式审批（参考模式）。** 哪些操作需要人工放行、需要几位审批人、谁有权批准（根、被委托的 approver、两人确认），应声明在**任务/操作**一侧，与“从哪个聊天平台点按钮”无关。渠道只负责：把待办渲染成卡片或通知、把一次合规的点击带回 Baton；它不改变策略，也不保存审批状态。

**在 Baton 上的映射（已实现或计划中的字段，不新增第二套策略引擎）。**

| 声明式概念 | Baton 中的对应 |
| --- | --- |
| 是否需要人工 gate | 任务的 `gate_mode`、`reviewers`、`approvals_required`（1 或 2）、以及 owner 级策略 |
| 谁可以批准 | 根；或持有有效 **approver 委托链** 的人类（与 [api.md](../api.md) 中 `gate` 一致） |
| 两人确认 | `approvals_required=2`：两位**不同**审批人各计一票，第一票后任务仍为 `awaiting_gate`，卡片显示进度（第 4.2 节） |
| 权限类请求 | `request_permission` / `decide_permission`（X26）；卡片按钮映射为 `allow_once` / `deny`，不提供 `allow_always` |
| 通道呈现 | 核心按事件选模板（第 4.2 节）；`CardSpec` 与按钮回传值不含身份或策略 |

**参考实现（模式层面）。** [HumanLayer](https://github.com/humanlayer/humanlayer) 把“需要人批准的操作”与“通知/收集批准的具体通道”分开；Baton 采用同一分离：bridge 里的 gate 与委托链是权威，Slack/飞书/邮件只是未来的 `ChannelAdapter`。本文不复述 HumanLayer 的 API 细节。

## 2. `ChannelAdapter` 接口

适配器只做三件事：格式转换（Baton 中立结构与平台载荷互转）、签名与解密校验、传输（发送与接收）。其余全部在核心：事件订阅、模板选择、对象到消息的映射、去重、授权、调用 Baton、审计。适配器没有 Baton 凭据，不读写绑定表，不判断谁有权限。

中立类型放在 `adapters/channels/api.py`，只用标准库。

```python
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Literal, Mapping, Optional, Sequence

CardStatus = Literal["pending", "approved", "rejected", "changes_requested", "info", "alert"]
ActionName = Literal["approve", "reject", "request_changes"]


@dataclass(frozen=True)
class Target:               # 一个会话：飞书的 chat_id（群）或 open_id（私聊）
    channel: str            # "feishu"
    kind: Literal["chat", "user"]
    id: str


@dataclass(frozen=True)
class ObjectRef:            # Baton 对象；同一个 ObjectRef 永远对应同一张卡片
    type: str               # "task"（之后 "permission_request"、"ask"）
    id: str                 # UUID
    def __str__(self) -> str:
        return f"{self.type}:{self.id}"


@dataclass(frozen=True)
class Principal:            # resolve_identity 的结果：Baton 里一个确切的人
    principal: str
    account: str
    kind: Literal["human"]
    display_name: str
    space_id: str


@dataclass(frozen=True)
class RawInbound:           # 平台送来的原始请求或事件，适配器自己校验
    transport: Literal["longconn", "https"]
    headers: Mapping[str, str]      # 长连接下为空
    body: bytes                     # https 下是未经改动的原始正文；长连接下是 SDK 递交事件的 JSON
    received_at: datetime


@dataclass(frozen=True)
class ActionEvent:          # 只有校验与解密通过后才会被构造
    channel: str
    instance: str                   # 如 feishu:<app_id>，与绑定表一致
    external_user_id: str           # 飞书 operator.open_id
    action: ActionName
    value: Mapping[str, object]     # 按钮绑定的回传对象，见下文“按钮回传值”
    comment: Optional[str]          # request_changes 的输入框文字；其他动作为 None
    event_id: str                   # 平台每次回调唯一的 id（飞书 header.event_id）；幂等权威键
    dedupe_token: str               # SHA-256(event_id) 十六进制；与 event_id 一一对应，写入审计
    external_message_id: str        # 被点击的消息（飞书 context.open_message_id）
    external_chat_id: Optional[str] = None   # 附加：被点击消息所在会话，供核心校验
    event_time: Optional[datetime] = None    # 附加：平台给出的事件时间，供核心判断过期
    card_update_token: Optional[str] = None  # 飞书 event.token：仅用于（可选）延时更新卡片，不参与幂等


class AdapterError(Exception): ...
class BadSignature(AdapterError): ...      # 签名、token、app_id、租户任一不符
class Undecodable(AdapterError): ...       # 无法解密或解析，或不是受支持的回调
class TargetGone(AdapterError): ...        # 群已解散、机器人已被移出
class RateLimited(AdapterError):
    retry_after: float
class TooLarge(AdapterError): ...          # 超过平台卡片大小限制
class Transient(AdapterError): ...         # 网络或平台临时错误，可重试


@dataclass(frozen=True)
class ActionOutcome:        # 核心交给 ack_response 的中立结果；不含平台载荷
    kind: Literal["success", "denied", "unbound", "stale", "invalid", "deferred"]
    toast_type: Literal["success", "error", "warning", "info"]
    message: str
    card: Optional["CardSpec"] = None       # 飞书审批回调必须留空。共享卡片只走 upsert_card


@dataclass(frozen=True)
class Capabilities:
    in_place_update: bool           # 能否原位更新同一条消息
    buttons: bool
    text_input: bool                # request_changes 需要
    max_card_bytes: int             # 飞书为 30 * 1024


class ChannelAdapter(ABC):
    channel: str
    capabilities: Capabilities

    @abstractmethod
    def send_notice(self, target: Target, text: str, links: Sequence[tuple[str, str]] = ()) -> str:
        """发送纯文本通知，links 是 (label, url)；返回 external_message_id。"""

    @abstractmethod
    def upsert_card(self, target: Target, object_ref: ObjectRef, card: "CardSpec",
                    *, existing_message_id: Optional[str] = None) -> str:
        """existing_message_id 为 None 时创建，否则原位更新；返回 external_message_id。
        适配器无状态：对象到消息的映射由核心保存并传入。"""

    @abstractmethod
    def parse_action(self, raw: RawInbound) -> ActionEvent:
        """校验签名并解密后转成 ActionEvent；校验失败抛 BadSignature，其他无法识别的抛 Undecodable。"""

    def resolve_identity(self, channel: str, instance: str, external_user_id: str,
                         space_id: str) -> Optional[Principal]:
        """基类的具体实现：转发给核心注入的 Baton 绑定查询；适配器不得覆盖。
        space_id 由核心从 object_ref 解析出的任务所在 Space 决定，调用方不得传入任意 space 冒充。
        没有有效绑定返回 None，不区分“未绑定”“已撤销”“已过期”。"""

    @abstractmethod
    def ack_response(self, event: Optional[ActionEvent], outcome: "ActionOutcome") -> Mapping[str, object]:
        """把核心的处理结果转成平台要求的同步应答（飞书：toast 与可选的 card）。"""

    @abstractmethod
    def start(self, handler: Callable[[RawInbound], Mapping[str, object]]) -> None:
        """建立传输（飞书长连接，或 HTTPS 监听），收到的每个原始回调交给 handler，
        并把 handler 返回的应答送回平台。必须在非阻塞线程中运行。"""

    @abstractmethod
    def stop(self) -> None: ...
```

与 owner 列出的四个方法相比，这里有三处补充，理由都是飞书的硬约束：

1. `ack_response`、`start`、`stop`：飞书要求在 3 秒内通过回调的**响应体**返回 toast 或更新后的卡片（见第 7 章），所以同步应答也属于格式转换，必须由适配器生成；不需要同步应答的平台让它返回空映射。
2. `ActionEvent` 增加 `external_chat_id` 与 `event_time`（均可空）：核心用它们识别“被转发到别处的卡片”和过期点击，不改变 owner 给出的字段。
3. `resolve_identity` 放在接口里，但只有基类的一个具体实现：它转发到 Baton，适配器不能保存绑定。这样接口形状与 owner 的描述一致，同时保证身份判断不在适配器里。

按钮回传值（`ActionEvent.value`）。卡片按钮的回传对象由核心生成、适配器原样放进平台组件；飞书 SDK 只支持对象类型的回传值，不支持字符串（见 Python 版“处理回调”页的注意事项）。它不含任何身份：

```json
{"v": 1, "a": "approve", "o": "task:6f1c2d9e-0000-4000-8000-000000000001", "e": 3, "k": "q7Wm2p0cX1"}
```

`v` 是格式版本，`a` 动作，`o` 对象，`e` 是**这一次渲染**所展示结果的 epoch，`k` 是这张卡片这次渲染的随机键。核心在 `card` 表里同时保存 `card_key` 与 `epoch`（第 4.3 节）。收到点击时，`value.k` 必须等于该行的 `card_key`，`value.e` 必须等于该行的 `epoch`；两处都相符之后，`channel_act` 的 `expected_epoch` 才取这个已经核对过的 epoch，它这时才对应 `gate` 的 `expected_epoch`。回传值可以被改写。留下旧的 `k`、把 `e` 改成当前 epoch（旧值加一）时，核心必须拒绝，并且不得把这个 `e` 当作 `expected_epoch`（第 4.4 节第 3 步）。点击者是谁**只**来自平台自己校验过的 `operator`，不来自 `value`。

其他渠道的适配方式（只说明接口不会挡路，平台能力尚未核实）：Slack 与企业微信需要各自的签名校验、卡片与原位更新能力、按钮回传、文本输入，落在同一组方法上；邮件没有按钮和原位更新，`Capabilities` 里 `buttons` 与 `in_place_update` 为假，核心退化为只发 `send_notice`，动作改由签名链接进入的只读视图加平台之外的途径完成，这需要单独设计，本文不展开。

## 3. `CardSpec` 中立结构

```python
@dataclass(frozen=True)
class CardAction:
    name: ActionName
    label: str
    value: Mapping[str, object]          # 即上面的按钮回传值
    needs_text: bool = False             # request_changes 为 True：渲染输入框，文字进 ActionEvent.comment


@dataclass(frozen=True)
class TerminalState:
    by_display_name: str                 # Baton 里点击者的 display_name，不是平台昵称
    at: datetime                         # 带时区；渲染时转成配置的时区 HH:MM
    decision: Literal["approved", "rejected", "changes_requested"]


@dataclass(frozen=True)
class CardSpec:
    title: str
    status: CardStatus
    fields: tuple[tuple[str, str], ...] = ()        # (label, value)
    links: tuple[tuple[str, str], ...] = ()         # (label, url)
    actions: tuple[CardAction, ...] = ()
    terminal_state: Optional[TerminalState] = None
    version: int = 0                                 # 每次内容变化加一，用于去重幂等的发送键
```

不变量，由 `CardSpec` 构造时检查：

- `status == "pending"`：至少一个动作，且没有 `terminal_state`。
- `status in {approved, rejected, changes_requested}`：没有动作，必须有 `terminal_state`，且 `terminal_state.decision` 与 `status` 一致。
- `status in {info, alert}`：没有动作、没有 `terminal_state`。它们给不走纯文本的状态通知用（见第 4 章模板表的“形式”列，默认是纯文本）。
- 整张卡片只放结论：`fields` 最多 6 项，每个值最多 200 字符，超出由核心截断并加省略号；详情只出现在 `links`。序列化后的大小必须不超过 `Capabilities.max_card_bytes`，否则核心再截断一次，仍超出就抛 `TooLarge` 并改发纯文本通知加链接。
- 字段值不得含明文手机号、邮箱：飞书发送接口对含明文电话、邮箱的内容可能返回“数据防泄漏审查未通过”（错误码 230028，见“更新已发送的消息卡片”页的错误码表），所以核心在渲染前把这类内容替换成占位。

终态文字（owner 规定的格式，时间是配置时区的 `HH:MM`）：

| `decision` | 卡片上替代按钮的文字 |
| --- | --- |
| `approved` | 已由 {by_display_name} 于 {HH:MM} 通过 |
| `rejected` | 已由 {by_display_name} 于 {HH:MM} 拒绝 |
| `changes_requested` | 已由 {by_display_name} 于 {HH:MM} 要求修改 |

终态之后服务端依然对 **`event_id`** 幂等：重复到达的同一次回调得到与第一次相同的 **toast**，不会再次写 Baton。飞书回调体里始终没有卡片，共享卡片只由核心的更新队列写入（第 4.4 节）。

两个实例（示意，值为占位）：

```json
{
  "title": "评审结论待放行：PR 修复重试逻辑",
  "status": "pending",
  "fields": [["结论", "建议合并，无阻塞问题"], ["评审人", "reviewer-agent"], ["验收", "全部通过"], ["进度", "已通过 0/1"]],
  "links": [["查看详情", "https://baton.example.com/board/o/task/6f1c2d9e-0000-4000-8000-000000000001?exp=1792000000&kv=2&sig=<base64url>"]],
  "actions": [
    {"name": "approve", "label": "通过", "value": {"v": 1, "a": "approve", "o": "task:6f1c2d9e-0000-4000-8000-000000000001", "e": 3, "k": "q7Wm2p0cX1"}},
    {"name": "reject", "label": "拒绝", "value": {"v": 1, "a": "reject", "o": "task:6f1c2d9e-0000-4000-8000-000000000001", "e": 3, "k": "q7Wm2p0cX1"}},
    {"name": "request_changes", "label": "要求修改", "needs_text": true, "value": {"v": 1, "a": "request_changes", "o": "task:6f1c2d9e-0000-4000-8000-000000000001", "e": 3, "k": "q7Wm2p0cX1"}}
  ],
  "terminal_state": null,
  "version": 1
}
```

```json
{
  "title": "评审结论待放行：PR 修复重试逻辑",
  "status": "approved",
  "fields": [["结论", "建议合并，无阻塞问题"], ["评审人", "reviewer-agent"]],
  "links": [["查看详情", "https://baton.example.com/board/o/task/6f1c2d9e-0000-4000-8000-000000000001?exp=1792000900&kv=2&sig=<base64url>"]],
  "actions": [],
  "terminal_state": {"by_display_name": "张三", "at": "2026-10-20T10:42:00+08:00", "decision": "approved"},
  "version": 2
}
```

飞书上的呈现（示意）：

```
┌ 评审结论待放行：PR 修复重试逻辑 ───────┐        ┌ 评审结论待放行：PR 修复重试逻辑 ───────┐
│ 结论  建议合并，无阻塞问题             │        │ 结论  建议合并，无阻塞问题             │
│ 评审人 reviewer-agent  验收 全部通过   │  ───►  │ 评审人 reviewer-agent                  │
│ [通过] [拒绝] [要求修改 ▸ 意见输入框]  │        │ 已由 张三 于 10:42 通过                │
│ 查看详情                               │        │ 查看详情                               │
└────────────────────────────────────────┘        └────────────────────────────────────────┘
```

## 4. 通道核心（`adapters/channels/core.py`）

核心是一个独立进程，不进入 bridge 镜像，也不进入 bridge 的依赖锁。核心只用标准库（状态存在标准库 `sqlite3`、HTTP 用标准库）；第三方依赖只可能出现在某个适配器的可选依赖里（第 7 章）。核心以自己的 Baton principal 运行，下称“核心主体”：`kind=agent`、`role=client`，由离线管理员登记，令牌按现有方式只存哈希；它对工作 Space 只有 view 权限，**不是** reviewer，不持有任何审批人委托，更不是根。按 [identity-card.md](identity-card.md) 的规则，它作为 agent 必须有一个负责的人（运营者）。

### 4.1 事件来源

核心通过 `EventSource` 接收 Baton 事件，有两种实现：

- `StandardWebhookSource`（3c）：W2-5 已合入 main 的 Standard Webhooks 订阅。核心验证 `webhook-id`、`webhook-timestamp`、`webhook-signature`（五分钟容差、常数时间比较、轮换宽限期内两个 `v1` 签名任一匹配即可，与 [api.md](../api.md) “接收方校验与时间戳容差”一致），按 `webhook-id` 去重，把事件**持久化到本地队列后立刻返回 2xx**（bridge 每次尝试有 10 秒超时，失败最多重试 8 次后进入 dead-letter），再由工作线程处理。Baton 在并行弃用期内仍发送旧 `X-Baton-*` 头至 **2027-01-31**；通道核心**只**实现 Standard Webhooks 校验，不依赖已弃用的 `X-Baton-Signature`（无时间戳、无重放保护）。
- `EventsStreamSource`（之后）：`/events` 流，由核心主动拉取，届时可以去掉下面的内部监听端口。

**“不新增入口”的准确含义。** bridge 的 webhook 只允许 https，并拒绝私有地址，除非运营者用 `BATON_WEBHOOK_ALLOW_HOSTS` 精确豁免主机名、用 `BATON_WEBHOOK_CA_BUNDLE` 提供内部 CA（见 [security-model.md](../security-model.md)）。所以在 `/events` 流出现之前，核心需要一个**只在内部网络上、带内部 CA 证书的 TLS 监听端口**给 bridge 投递，不发布到宿主机，也不经过公共入口。飞书方向则没有任何入站：长连接由核心主动外连。

**一个已发现的缺口（需要在 3c 里解决，见第 12 章问题 1）。** 今天的 webhook 只投递**订阅者自己**作为收件人的通知（见 [api.md](../api.md) “订阅只投递调用者自己的”）。核心主体不是 reviewer，也不是 owner，所以收不到 `task.awaiting_gate`。方案是给 bridge 增加一种“观察者订阅”：只有被离线管理员标记为通道核心的主体、且对该 Space 有 view 权限时，才能订阅整个 Space 的事件，投递前照旧重新检查它的 view 权限。这是对 webhook 语义的扩展，需要 owner 与总工程师确认后在 3c 实现。

### 4.2 事件、模板与形式

核心把事件映射到模板。读取对象内容时用核心主体调用 `get_task` 等只读接口；webhook 默认只含元数据（`id, seq, event, object_type, object_id, thread_id, created_at, recipient`），所以卡片内容总是从 Baton 现取，不放进事件。

| 模板 | 触发 | 形式 | 目标 | 按钮 |
| --- | --- | --- | --- | --- |
| `approval.pr_review`：PR 评审结论 | `task.awaiting_gate`，任务是评审类 | 卡片，原位更新 | 工作群，@ reviewers | 通过 / 拒绝 / 要求修改 |
| `approval.gate`：一般放行 | `task.awaiting_gate`，其他任务 | 同上 | 同上 | 同上 |
| `approval.permission`：权限请求 | `task.blocked` 中的权限请求（X26） | 卡片，原位更新 | 工作群，@ 被点名的 approvers | 通过（映射为 `allow_once`）/ 拒绝（`deny`） |
| `status.dispatched`：任务已派发 | `task.assigned` | 纯文本加链接 | 工作群 | 无 |
| `status.done`：任务完成 | `gate.decided` 且任务 done | 纯文本加链接 | 工作群 | 无 |
| `status.sla_breached`：响应超时 | `task.overdue` | 纯文本加链接，@ `escalate_to` 与 owner | 工作群 | 无 |
| `status.budget`：预算告警 | 80% 与 100% 预算事件（W2-2，**计划中**） | 纯文本加链接，@ approvers | 工作群 | 无 |

说明：

- 评审类任务与一般放行任务如何区分（`task_kind`、reviewer 配置或页面约定）在 3c 里按当时已合入的字段决定；本文不假定字段。
- 权限请求（`request_permission`、`decide_permission`，规格 X26）在撰写本文时在分支 `cursor/permission-loop`，未合入 main，所以 `approval.permission` 模板在它合入前不启用。卡片**不提供** `allow_always`：长期放行只能走看板之外的显式路径，这是有意的收紧。
- `status.*` 默认是纯文本，符合“无需操作的通知是带链接的纯文本”。模板配置里有 `form: "text" | "card"`，owner 想让一个任务只对应一张原位更新的状态卡时可以改成 `card`，此时用 `status=info/alert` 的 `CardSpec`。
- 两人确认策略下，第一位批准后任务仍在 `awaiting_gate`：卡片保持 `pending`，`fields` 里“进度”改为“已通过 1/2”，仍显示按钮；同一个人再点会被 Baton 拒绝（不能重复通过）。直到完成或任何一次 fail，卡片才进入终态。
- 重新提交结果（epoch 加一）时，同一张卡片原位回到 `pending`，**同时**换新的 `k` 与映射里的 `epoch`（回传值里的 `e` 跟着这张新卡片走）。旧渲染上的按钮因 `k` 或 `e` 与映射不符，被核心拒绝、不调用 Baton。映射还没刷新时，保留旧 `k` 只把 `e` 改成新 epoch，同样拒绝：`expected_epoch` 不能超前于这张卡片已经保存的 `epoch`。真正过期、且与映射一致的旧 epoch 仍由 Baton 返回 `stale_claim`，并触发一次刷新。

### 4.3 对象到消息的映射与状态

核心在本地 `sqlite3`（WAL，放在核心自己的数据卷）保存：

```sql
CREATE TABLE card (
  object_ref TEXT NOT NULL,            -- "task:<uuid>"
  channel TEXT NOT NULL, target_id TEXT NOT NULL,
  external_message_id TEXT NOT NULL,
  card_key TEXT NOT NULL,              -- 当前渲染的 k
  epoch INTEGER, version INTEGER NOT NULL, state TEXT NOT NULL,   -- pending|approved|...|dead
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  PRIMARY KEY (object_ref, channel, target_id)
);
CREATE TABLE seen_event  (webhook_id TEXT PRIMARY KEY, at INTEGER NOT NULL);
CREATE TABLE seen_action (
  event_id TEXT PRIMARY KEY,           -- 飞书 header.event_id；重复投递 id 不变
  dedupe_token TEXT NOT NULL UNIQUE,   -- sha256(event_id)，供审计与 Baton x_channel_action 对齐
  toast TEXT NOT NULL,                 -- 只存 toast（type + message）。不存卡片快照，重放不得把它写回共享卡片
  card_version INTEGER,                -- 首次应答时 card.version；只作对照，重放不凭它回放旧卡片
  baton_committed INTEGER NOT NULL,    -- 1 仅当该 event_id 的 x_channel_action 已提交（含拒绝与未绑定，不只是 gate 成功）
  at INTEGER NOT NULL
);
CREATE TABLE pending_click (
  event_id TEXT PRIMARY KEY,           -- 每一次 channel_act 在调用前写入；Baton 已提交但卡片未写完时也留着
  payload TEXT NOT NULL,               -- 调 channel_act 所需字段（含已核对的 expected_epoch）
  started_at INTEGER NOT NULL, tries INTEGER NOT NULL
);
CREATE TABLE queue (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, due INTEGER NOT NULL, tries INTEGER NOT NULL);
```

这些是缓存与投递状态，不是事实来源。丢失本地库的后果是：旧卡片无法再被原位更新（核心会为仍待办的对象发一张新卡片），已经写入 Baton 的成功或拒绝靠 `x_channel_action` 挡住，不会按新的绑定或委托重判。授权、绑定和动作去重的权威都在 Baton（第 5 章）：拒绝和未绑定也算一次决定，不只成功。`seen_action` 只缓存 toast。`seen_event` 保留 24 小时（覆盖约 64 分钟的重试窗口），`seen_action` 保留 7 天。每一次要通过 `channel_act` 的点击，都在调用之前把 `pending_click` 提交到本地库，不只是超时那一条。行一直留到 `seen_action` 已提交，并且（这次决定会改共享卡片时）按当前任务状态的 `upsert_card` 已经成功。进程每次启动、在 `start` 接受新回调之前，若本地库还在，必须先排空这张表（第 4.4 节）。内存里的后台队列随进程消失，不能代替这张表。

创建卡片时用 `uuid = hash(object_ref, version)` 作为发送去重键：核心在“已发送”和“已写映射”之间崩溃后重试，飞书的 `uuid` 去重（相同 `uuid` 一小时内至多成功发送一条）可避免重复卡片；超过一小时后重试仍可能出现一张重复卡，接受这个限度。

向同一群发消息的频控是群内机器人共享的 5 QPS，更新同一条消息的频控是 5 QPS（见上述两个官方页面）；核心对每个目标与每个对象各用令牌桶，对同一对象的多次更新合并为最新一次。这个合并只约束核心自己的 `upsert_card` 队列，约束不到已经返回的回调体。数据库写锁只把两次 `channel_act` 的提交排成先后，不决定两条回调谁先到达飞书。因此回调体不带卡片（第 4.4 节）。同一对象同时最多一个在途 `upsert_card`；它返回之后，队列里若有更新的 version 再发那一版，不把“1/2”和终态并发发出去。更旧的 version 入队之后不得再发，失败后也不得重试。更新只在发送后 14 天内有效，超过则收到错误 230031：核心对仍待办的对象改发新卡片并把映射换成新消息。

### 4.4 点击处理流程

适配器的 `start(handler)` 把每个回调交给核心的 `handle_inbound(raw)`；总预算 2.0 秒（给飞书的 3 秒留 1 秒余量，可配置），Baton 调用本身的超时 1.5 秒。

1. `adapter.parse_action(raw)`：失败抛 `BadSignature` 或 `Undecodable` → 核心写一条核心侧元数据审计 `channel.action_refused`（不含原始载荷），应答 error toast，**不调用 Baton**。
2. 查 `seen_action[event_id]`：仅当 `baton_committed=1` 时命中，**不再调用 Baton**。回调**只有**当时保存的 toast，`card` 为空。`seen_action` 不保存卡片快照。若这个 `event_id` 的 `pending_click` 还在，说明决定已经提交、共享卡片可能还没写完：把**当前**任务状态排进第 6 步的更新队列，不把这次点击当时的“1/2”或终态快照 PATCH 回去。toast 仍然与第一次相同。
3. 校验 `value`：`v == 1`；`o` 在映射里，且 `external_message_id`、`k` 与该行的 `card_key` 一致；**`e` 与该行的 `epoch` 一致**（空的 `epoch` 视为不符）；`external_chat_id` 等于映射里的目标会话（被转发或合并转发到别处的卡片因此无效）；`event_time` 距今不超过 10 分钟。任一不符 → error toast“卡片无效或已过期”，记 `channel.action_refused`，**不调用 Baton**。通过之后，`channel_act` 的 `expected_epoch` 只使用这一行里的 `epoch`，不使用未核对的客户端 `e`。
4. **先**提交 `pending_click`（同一 `event_id`、第 3 步核对过的 `expected_epoch`，以及调用所需的其余字段），**再**调用 Baton 的 `channel_act`（3b，见 4.5）：`operation_id = uuid5(命名空间, "channel-act:" + event_id)`。幂等查询在 bridge **这一次写事务内部、点击者授权之前**完成。现有 `_mutate` 做不到这个顺序，核心不能先在另一个事务里查表再调用它（4.5）。飞书对**同一次**回调的重复投递携带相同 `event_id`。这一步对快速路径和超时路径相同，不只是预算用完的时候才落盘。
5. 按 `channel_act` 结果生成应答。下表“提交 `x_channel_action`”为“是”的结果，bridge 在返回前已经提交；核心再写 `seen_action`（`baton_committed=1`，只存 toast 与当时的 `card_version`）。本地 `seen_action` 还没落盘时，同一 `event_id` 的重投会再次进入 `channel_act`，bridge 返回**第一次**的决定，不会因为其间补上了绑定或委托而改判。飞书回调体**一律不带卡片**。共享卡片是否变化，只由第 6 步的队列决定。

| `channel_act` 结果 | 对点击者 | 共享卡片 | `seen_action` | 提交 `x_channel_action` |
| --- | --- | --- | --- | --- |
| 首次成功 | success toast，回调无卡片 | 第 6 步按**当前**任务状态入队（终态，或两人确认时“已通过 1/2”且仍有按钮） | 是 | 是 |
| 命中已有 `x_channel_action`（成功或拒绝） | 第一次的 toast，回调无卡片 | 不 PATCH 历史快照。`pending_click` 还在时，把**当前**任务状态入队 | 是（补写缓存） | 已有行，不新增 |
| 未绑定 | error toast“无权放行”，附“尚未绑定，请联系空间所有者” | **不变** | 是 | 是 |
| 无委托（`delegation_required`）或无权（`forbidden`） | error toast“无权放行” | **不变** | 是 | 是 |
| 同一人重复 `pass`（任务仍 `awaiting_gate`） | error toast“无权放行”或“您已投过票” | **不变**。这一票**不**用来补写上一票还没写出的卡片 | 是 | 是（`forbidden`，不是 `stale_claim`） |
| `stale_claim`（映射中的 epoch 已与任务不符，或任务已终态） | warning toast“已有结论，已刷新”，回调无卡片 | 按**当前**任务状态入队 | 是 | 是 |
| 超过 2.0 秒预算（`channel_act` 尚未返回） | info toast“已收到，处理中”，回调无卡片 | `pending_click` 留着，后台用同一 `event_id` 补完后再入队 | **否** | **否**（还没有决定） |

`pending_click` 在第 4 步已经提交，所以超时只是不再等 `channel_act` 返回：不写 `seen_action`，把 toast 交给飞书，行继续留着。**不**假定飞书会重试（收到 toast 即视为这次投递已经结束）。会改卡片的行，要等第 6 步把**当前**任务状态发送成功之后才删除；若队列已经用更新的 version 盖过这张卡，旧 version 不再发，这一行随之删除。不要求当初那张“1/2”快照自己 PATCH 成功。不改卡片的拒绝，在 `seen_action` 提交后删除。同一个人用新的 `event_id` 再点，若任务仍在 `awaiting_gate`，是 `forbidden`，卡片不变，不能靠它把上一票的卡片补上。

“原位提示”的含义：owner 要求未绑定或无权的点击者得到原位的“无权放行”提示。这里把它实现为**只对点击者显示的 toast**，而不是改写共享卡片：卡片对所有群成员是同一张（`update_multi`），若把它替换成“无权放行”，群里任何一个没有权限的人都能让有权限的人看不到按钮，等于一个无成本的拒绝服务。任务不变与卡片不变是同一个意思。这是对 owner 措辞的一种解释，列在第 12 章问题 3 里等确认。

6. 共享卡片**只**通过 `upsert_card` 更新。首次成功、`stale_claim`、超时补完，以及 `pending_click` 还在时的重放，都读取**此刻**的任务状态再入队，不使用该 `event_id` 首次决定时渲染过的卡片。两人确认时，先提交的那一票在自己的事务里看到的是“已通过 1/2”；后一票提交之后任务已经终态。写锁只串行这两次数据库提交（`_transaction(write=True)` 里的 `pg_advisory_xact_lock`），两条回调谁先到飞书没有保证。若回调里带着刚渲染的“1/2”，而终态更新先到，迟到的回调会把按钮写回去（`update_multi`，第 7.5 节）。所以回调不带卡片。队列按对象只保留最新 version，同一对象同时最多一个在途 PATCH：在途的那次返回之后才发送更新的 version，避免“1/2”和终态并发、后到的旧 PATCH 盖住终态。更旧的 version 不得再发，失败后也不得重试。3d 不得改成用回调体更新全群。

**启动恢复。** `start` 在把回调交给 `handle_inbound` 之前，若本地库还在，先逐行处理 `pending_click`（以及队列里同一 `event_id` 的未完成项）。这一步不套用 2.0 秒的回调预算。对每一行用保存的载荷调用 `channel_act`，幂等键仍是该 `event_id`：Baton 若已有行，返回首次决定，不会第二次 gate；若还没有行，这一次才执行并提交。然后写 `seen_action`。这次决定会改变共享卡片时（包括 Baton **已经**提交、但进程在 `upsert_card` 之前退出的那一行），按**当前**任务状态入队，成功之后才删除 `pending_click`。删除不得早于这次入队成功，否则再次崩溃就找不到要补的卡片。同一人的新 `event_id` 不是这条补写路径。本地库丢失、并且 toast 已经发出、Baton 又还没有该行时，没有可恢复的记录，飞书也不会重投：这次点击补不回来，记为本地库丢失的残余（C16）。

### 4.5 在点击者本人的权限下调用 Baton（3b 的 bridge 变更）

核心**不**用自己的权限做决定，也不持有任何人的令牌。bridge 新增一个窄接口：

```
channel_act(operation_id, channel, instance, external_user_id, event_id, dedupe_token, event_time,
            object_ref, expected_epoch, action, comment=None)
```

**3b 必须新增的存储入口，不能套现有 `_mutate`。** 今天 `SpaceStore._mutate`（`bridge/space_store.py`）每次调用 `_transaction(write=True)`：那个上下文自己 `psycopg.connect`，写路径在交出连接之前先取 `pg_advisory_xact_lock`。连接交出来之后的顺序是固定的：先 `authorize(conn)`，再按 `(principal, operation_id)` 读 `baton.operations`，命中则走重放，**最后**才 `effect(conn)`。`gate` 的 `authorize` 会调用 `_approver_chain`；委托在链上任一环被撤销或过期时，这里抛 `delegation_required`。`gate` 自己的注释写明：这次授权先于重放，所以撤销之后的重试会被拒绝，operations 里保存的结果读不到。因此：

- 在调用 `_mutate` **之前**查 `x_channel_action`，是另一次连接、另一个事务。两次之间绑定或委托可以变化；查表与决定不原子。
- 把查询放进 `effect`，则撤销委托后的重放仍然先经过 `authorize`，调用方先得到 `delegation_required`。
- “整次 `channel_act` 放进一次 `_mutate` 的 effect”和“同一写事务里、授权之前按 `event_id` 返回已有行”不能同时成立。3b 不得假设给 `_mutate` 换一个 `authorize` 参数就能把查询挪到授权前面：现有助手没有这个顺序，改 `_mutate` 会影响所有已经先授权再重放的写路径（`gate` 依赖这一点）。

3b 新增 `SpaceStore.channel_act`。它**不**调用 `_mutate`、`gate` 或 `comment`（后两个各自再进一次 `_mutate`）。它只调用**一次** `_transaction(write=True)`，在同一个 `with` 块里按下面的顺序做完，然后**正常返回**，让连接提交。`_transaction` 在异常离开 `with` 时回滚（`StoreError` 会原样抛出）。所以未绑定、`delegation_required`、`forbidden`、`stale_claim`、`invalid` 都是返回值，不是逃出事务的异常：先插入 `x_channel_action`，再返回，这一行才会留下。

1. **调用者。** 必须是被离线管理员标记为“通道核心”的主体（登记表新字段，只有离线管理员能设置，与 `kind`、`account` 同级）；否则 `403 forbidden`，**不**写 `x_channel_action`。这是调用方身份，不是点击者的委托检查。失败时事务里还没有任何插入，抛 `StoreError` 可以。
2. **幂等，先于点击者授权。** 仍在这个写事务里：`SELECT response FROM baton.x_channel_action WHERE event_id = %s FOR UPDATE`。有行则返回该 JSON，结束。不再查绑定，不调用 `_approver_chain`，不读 `baton.operations`，不跑 gate 的 effect。委托在首次决定之后被撤销时，重放得到的是首次 outcome，不是 `delegation_required`。返回的 JSON 是决定（toast 语义、`result`、gate 摘要），不是要回写到共享卡片上的卡片快照；核心按 4.4 第 2 步处理重放。
3. **任务与绑定，仍在本事务内。** 从 `object_ref` 解析任务，`space_id` 只从该任务所在页面读取，**不得**由调用方传入。用 `(channel, instance, external_user_id, space_id)` 查绑定：`revoked_at IS NULL` 且 (`expires_at IS NULL OR expires_at > now()`)。没有、已过期，或 principal/account/kind 与登记表不一致：插入 `x_channel_action`（`result` 为 `unbound` 或 `denied`，`principal` 在未绑定时为空），写审计，**返回并提交**。不写评论，不写 `x_gate`。未绑定的审计事件是 `channel.unbound_click`；身份不符是 `channel.action_denied`。
4. **点击者授权与 gate 的前置条件，仍在写评论和 `x_gate` 之前。** 用绑定行构造点击者的 `Actor`，在本连接上做 `gate` 今天在 `authorize` 里做的检查（`_manual`、是否根、`_approver_chain`、不能自批、须是 reviewer），以及 effect 里会变成拒绝的检查（任务不在 `awaiting_gate`、epoch 不符、同一人已经 `pass`、`request_changes` 的文字为空或超过 1000 字、点击者没有 comment 权限）。`_approver_chain` 今天以 `StoreError` 抛出 `delegation_required`；3b 必须在这个 `with` 块**内部**接住它（以及上述 `forbidden` / `stale_claim` / `invalid`），插入 `x_channel_action` 后正常返回。`result` 取 `denied`（含 `delegation_required`、`forbidden`、重复 `pass`）、`stale` 或 `invalid`。审计：无委托或无权为 `channel.action_denied`。这样拒绝和未绑定的权威在 Baton，而不只在核心的 `seen_action`。同一 `event_id` 再来时走第 2 步，即使其间完成了绑定或补上了委托，也返回首次拒绝，不会变成一次新的通过。
5. **只有第 4 步通过才写业务行。** 在同一个 `conn` 上、以点击者的身份写入。`gate` 的 effect 今天是 `gate()` 里面的嵌套函数，没有可复用的 conn 级入口；3b 要把它抽出来再在这里调用，**不得**调用 `gate()`。`request_changes` 先写评论，再写入与失败的 gate 相同的 `x_gate` 行（`decision=fail`，`reason` 为该文字）。意见必须落在任务线程上，调用已有的 `_comment` 时 **`thread_id` 必须是任务 id**，`page_id` 必须是任务的 `page_id`，作者和 `requested_by` 都是点击者：

```python
_comment(conn, clicker, operation_id, str(task["page_id"]), body,
         thread_id=str(task["id"]), requested_by=clicker.principal)
```

这与 `_notify_task` 把 `thread_id` 设成 `str(task["id"])` 相同。`_comment` 在 `thread_id` 省略时执行 `thread_id = thread_id or str(identity)`，线程 id 变成这条评论自己的 id。`list_comments(task_id=…)` 用任务 id 当线程 id 去查（`page_id, thread_id = str(task["page_id"]), str(task["id"])`）。省略 `thread_id` 会写成孤立评论，C6 的任务线程上看不到意见。不得调用 `comment()`：它再进一次 `_mutate`；线程上还没有评论时，它在插入之前就抛 `not_found`。任务线程经常是空的，第一条意见只能走 `_comment`。同一 `conn` 上再写 `comment.created`：用 `_audit`，带上 `task_id` 与 `thread_id`。不要用 `_task_audit`，它把 `requested_by` 固定成任务所有者。并按 `comment()` 在找到任务之后的做法，把 `comment.created` 放进 outbox。今天 `gate()` 的 `gate.reviewed` 不把 `reason` 放进审计 metadata（只有 `gate.result_overridden` 会带 `reason`），所以修改意见以这条评论的正文为准；`x_gate.reason` 仍写同一段文字，但不能代替任务线程。权限请求同样只在这个 `conn` 上映射到 `decide_permission` 的 `allow_once` / `deny`，不得再进一次 `_mutate`（该分支合入之后；合入前不启用）。规则与现在的 `gate` 一致：委托链、不能自批、同一人不能重复 pass、两人确认等。根自批需要 `self_approval=true`，卡片不提供。然后插入 `x_channel_action`（`result=success`）。评论、`x_gate`、`x_channel_action`、审计同一次提交。这一步若抛错，整笔回滚：没有半截评论，也没有动作行。那是意外失败，不是已记录的决定，重试会重新判定。无委托的人留不下评论，因为评论只出现在本步。
6. **不写 `baton.operations`，也不靠它重放。** `operation_id` 只放进 `x_channel_action.operation_id` 和审计，用来关联。operations 的重放发生在 `authorize` 之后，实现不了第 2 步。写锁把写事务串行化，后到的调用会在自己的第 2 步看见已提交的行；不要在一次 `_read` 里记“没有行”，再开另一次事务去插入。
7. **审计。** 成功时除了 `gate.reviewed` 等，再写 `channel.action`（含 `event_id`、`dedupe_token`）。requested_by 为点击者，performed_by 为核心主体。今天 `_audit` 的 metadata 白名单没有 `event_id`、`dedupe_token`、`channel`、`instance`、`external_user_id`；不在白名单里的键会变成 `invalid`。3b 要把这些键加进白名单，不能假定现有 `_audit` 会收下它们。

关键不变量，3b 的测试逐条覆盖：核心主体自己调用 `gate` 仍然被拒（“没有任何服务写入的 gate”这条规则不变，`channel_act` 是唯一的例外，且必须经过一个有效绑定）；核心主体拿到的任何成功响应，其授权都来自被绑定的那个人。

诚实的残余：bridge 信任核心如实转述“谁点了什么”。核心被攻破时，攻击者可以替**已绑定且本来就有权限**的人伪造点击；它不能让没有权限的人通过，也不能越过两人确认与根必需策略。收敛手段：绑定按 Space 隔离、只允许 pass/fail 两类决定、每主体限速、全部审计，并保留“高风险范围不允许经通道决定”的可选策略（第 12 章问题 7）。

## 5. 身份绑定

聊天平台里的点击者只带一个平台账号标识。把它变成 Baton 里的人，靠一张**只能由 Space 根创建**的绑定表。它与 [identity-card.md](identity-card.md) 第 3 章的规则一致：键是（发行方，主体）这一对，不用昵称或邮箱；不存任何密码、令牌或平台密钥；绑定要审计；一对未绑定的（发行方，主体）没有任何访问权。

```sql
CREATE TABLE baton.x_channel_binding (
  id uuid PRIMARY KEY,
  space_id uuid NOT NULL REFERENCES baton.spaces(id),
  channel text NOT NULL,              -- 'feishu'
  instance text NOT NULL,             -- 'feishu:<app_id>'，应用或租户变了，旧绑定自然失效
  external_user_id text NOT NULL,     -- 飞书 open_id
  principal text NOT NULL, account text NOT NULL,   -- 登记表里确切的人类身份
  bound_by text NOT NULL, bound_via text NOT NULL CHECK (bound_via IN ('api','operator')),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz,
  revoked_at timestamptz, revoked_by text, revoke_reason text
);
CREATE UNIQUE INDEX channel_binding_active
  ON baton.x_channel_binding(channel, instance, external_user_id, space_id)
  WHERE revoked_at IS NULL;
CREATE TABLE baton.x_channel_action (
  event_id text PRIMARY KEY,          -- 平台回调 id（飞书 header.event_id）
  dedupe_token text NOT NULL UNIQUE,  -- sha256(event_id)
  channel text NOT NULL, instance text NOT NULL, external_user_id text NOT NULL,
  principal text, task_id uuid, action text NOT NULL, result text NOT NULL,
  response jsonb NOT NULL,              -- 首次决定（toast 语义与 result）；重放原样返回，不含要回写的卡片快照
  operation_id uuid NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
```

两张表都在 `baton_owner` 名下，`baton_app` 只有读写、没有 DDL，与其余业务表一致；`x_channel_action` 追加后不改。`result` 为 `success`、`unbound`、`denied`、`stale`、`invalid` 之一；拒绝和未绑定也插入，不只有成功。`principal` 在未绑定时为空。具体 DDL 在 3b 定稿。3b 还要把这两张表的列加进 `SpaceStore.check_schema` 的必需列表。该检查在已列出的表缺列时返回 `storage_unavailable`；只建表、不更新列表，则缺表不会被这次检查发现。

规则：

- **谁能绑定。** 只有该 Space 的根（`owner_principal`、`owner_account`、`owner_kind` 记录的精确身份）。同一路径与委托一致：根自己有 token 后走 API；根尚未有 Baton 身份时，走离线管理员路径 `python -m bridge.channels bind --space <uuid> --channel feishu --instance feishu:<app_id> --external-id <open_id> --principal <p> --account <a> --operator <name> --reason <text>`，审计里 requested_by 是根、performed_by 是操作员，和 `bridge.delegations create` 的做法相同。
- **被绑定的必须是人。** 目标必须是登记表里已连接的人类主体，principal、account、kind 三者与登记表完全一致，并且对该 Space 有成员身份。这样根 A 不能把一个平台账号绑到一个只在别的 Space 有权限的人身上。绑定带 `space_id`，只对该 Space 内的任务有效。
- **怎么拿到对方的 open_id。** 飞书的回调不带姓名，只带 `open_id`（需要 `user_id` 还要额外权限，本设计不申请）。流程：根请这个人在工作群里点一下“绑定确认”卡片按钮（或者任意卡片上的按钮）；此时点击被判定为未绑定，核心写入审计 `channel.unbound_click`（含 `external_user_id`，同一账号每小时最多一条）；根从 `list_audit` 看到刚刚出现的那条，与对方当面或在别的渠道核对后，用上面的命令绑定。绑定不由点击者自己发起，所以不存在“自己认领身份”。
- **解绑。** 根执行 `unbind`（API 或操作员路径）：设置 `revoked_at`、`revoked_by`、`revoke_reason`，行保留作历史。每次点击都重新查绑定，所以下一次点击立即生效；已经提交的决定不受影响。解绑写审计。
- **可选过期。** `expires_at` 用于外部人员或临时授权；过期等同撤销。PostgreSQL 部分唯一索引**不能**用 `now()`（非 IMMUTABLE），故索引只排除 `revoked_at IS NOT NULL` 的行（与 [space_schema.sql](../bridge/space_schema.sql) 既有模式一致）。自然过期的行仍占唯一槽，直到根 **`bind`**：`bind` 在同一事务内把匹配键上“`revoked_at` 为空且 `expires_at <= now()`”的旧行写上 `revoked_at` 与 `revoke_reason='expired'`，再插入新行。运行时“有效绑定”仍按 `expires_at > now()` 判断（查询时过滤，非索引谓词）。
- **与 Identity Card 对齐。** W2-3 落地后，同一对（发行方，主体）作为人类卡片 `external_identities` 的一项出现，`issuer` 为 `feishu-channel:<app_id>`，用不同的 `issuer` 字符串与 OIDC 登录绑定区分开，两者不会互相冒充；绑定表在迁移时成为这些条目的来源，规则不变。在那之前绑定表是唯一记录。
- **平台账号是应用内标识。** 飞书的 `open_id` 按应用区分（来自飞书的用户身份概述，**待核实**官方原文），所以 `instance` 里带 `app_id`；owner 重建应用后，所有旧绑定自然不再匹配，需要重新绑定，这是设计上的安全行为。适配器还要求回调里的 `tenant_key` 与 `app_id` 等于配置的值，其他租户（例如外部群里的外部成员）的点击一律当作未绑定。
- **人离开群。** 绑定的是“人与 Baton 身份”，不是“人与群”。人被移出群后看不到也点不了那个群里的卡片；若有人保留了转发过的卡片，核心的会话校验（4.4 第 3 步）会拒绝来自别的会话的点击。真正撤销权限靠的是 Baton 侧：根解绑、撤销或让委托过期，这些对**新的** `event_id` 在下一次点击就生效。已经写入 `x_channel_action` 的那一次决定不改。飞书是否提供“成员被移出群”的事件、需要什么权限，**没有从官方页面核实**，本设计不订阅也不依赖它；如果以后采用，它只能用来**收紧**（例如把绑定标记为暂停），不能用来放行。人离职时的清单是：根解绑，撤销其审批人委托，必要时从群里移除。
- **机器人被移出群。** 官方有 `im.chat.member.bot.deleted_v1` 事件（权限 `im:chat.members:bot_access`、`im:chat` 或 `im:chat:readonly` 之一，见上面的页面），但那是第四个权限，默认不申请；默认靠发送失败（群已解散或机器人不在群内）检测：核心把目标标记为 `dead`、停止更新，并通过既有告警中继提醒运维。要不要多申请这个权限由 owner 决定。

## 6. 带签名的只读链接

聊天里的“查看详情”指向 `/board` 上**一个对象**的只读视图，链接本身带签名。

形式：

```
https://baton.example.com/board/o/task/6f1c2d9e-0000-4000-8000-000000000001?exp=1792000900&kv=2&sig=<base64url>
```

- **范围。** 一个对象、只读。链接对应两个路由：页面 `GET /board/o/{type}/{id}` 与同一对象的 `GET /board/o/{type}/{id}/data`；其他任何路径、方法、对象都不接受这个签名。链接模式的页面没有 token 输入框，也不发 MCP 或写请求；服务端靠路由而不是靠前端保证只读。
- **有效期。** 默认 15 分钟，`exp` 是 Unix 秒，运营者设置上限（默认不超过 24 小时）。
- **不是一次性的。** 聊天平台会为链接生成预览，预览抓取会消耗一次性链接，所以链接在有效期内可重复打开。
- **默认摘要、导航才全文（3e）。** 带同一签名的 `GET /board/o/{type}/{id}` 与 `GET /board/o/{type}/{id}/data` **默认**只返回摘要：HTML 页或 JSON（`/data`）仅含对象**标题**与“在浏览器中打开以查看详情”提示，**不含**任务说明、结果正文、评论线程或 diff。只有浏览器**顶层导航**才返回全文：同时满足 `Sec-Fetch-Mode: navigate` 与 `Sec-Fetch-Dest: document`（3e 测试用例覆盖；其他组合一律摘要）。不依赖 User-Agent 白名单（未识别的飞书预览也只会拿到标题）。两种响应均 `Referrer-Policy: no-referrer`、`Cache-Control: private, no-store`。
- **签名。** `sig = HMAC-SHA256(key[kv], "baton-link-v1\nGET\n" + path + "\n" + object_type + "\n" + object_id + "\n" + exp + "\n" + kv)`，`path` 是对象的规范路径 `/board/o/{type}/{id}`（`/data` 子资源按同一规范路径验证）。签名同时覆盖路径、对象 id、过期时间和密钥版本，所以改任何一项都会使它失效。服务端用常数时间比较；所有失败（签名、对象、格式）返回同样的 404，不泄漏对象是否存在；只有签名**有效**但已过期时才显示“链接已过期，请回到群里刷新卡片”，因为这时已经证明是我们签的。
- **签发与密钥。** 密钥只在 bridge 里：bridge 新增 `link_sign(object_type, object_id, ttl_seconds=900)`，调用者必须对该对象有 view 权限，返回 `{url, expires_at}`；核心通过它取链接，适配器和核心都碰不到密钥。密钥表 `x_link_key(version, key, created_at, retired_at)`，同一时刻最多两把有效。**轮换沿用 W2-5 的 webhook 密钥轮换办法**：`python -m bridge.links rotate` 生成新密钥（只由 bridge 使用，不返回、不记录），旧密钥继续验证到宽限期结束（默认 24 小时，运营者用 `BATON_LINK_ROTATION_GRACE_SECONDS` 设置 0 到 30 天；这个名字沿用 `BATON_WEBHOOK_ROTATION_GRACE_SECONDS` 的格式，3e 定稿），宽限期内再次轮换立即丢弃最旧的一把。怀疑泄漏时把宽限期设为 0，所有已发出的链接立即失效。宽限期不应小于链接上限，否则轮换会让合法链接提前失效。
- **URL 里没有令牌。** URL 不含 Baton 令牌、webhook 密钥、会话标识或任何能换取别的访问的东西。`sig` 只证明“bridge 为这个对象在这个时间窗口签过”，换不到其他对象，也换不到写权限。它仍然是不记名的：拿到链接的人在有效期内都能读这个对象，第 11 章按这个事实建模。
- **响应头。** `Referrer-Policy: no-referrer`、`Cache-Control: private, no-store`、`X-Robots-Tag: noindex`，页面里的外链不带来源。
- **审计。** `link_sign` 写一条 `x_audit`：`link.minted`（主体、对象、`ttl`、`kv`）。打开链接是未认证的 GET，不写数据库审计（会被预览抓取与扫描刷爆）；只写 bridge 的元数据 JSONL（对象、`kv`、结果，不含签名），沿用 `common.audit` 的白名单。边缘与网关的访问日志里会出现完整 URL，日志要按敏感数据对待。
- **部署影响（3e）。** 现在网关对 `/board` 前缀要求 bearer，`/board/data` 也一样。3e 要为 `/board/o/` 单独加一条不要求 bearer 的路由，并用测试证明 `/board`、`/board/data` 仍然需要令牌。
- **与卡片寿命的张力。** 一张待办卡片可能存在几小时甚至几天，15 分钟的链接会过期。第 12 章问题 2 列出方案：待办卡片的链接取 `min(任务响应时限, 运营者上限)` 的有效期；或者终态与每次更新时重新签发；或者增加一个“刷新链接”按钮，它只做一次 `upsert_card`。默认值仍是 owner 规定的 15 分钟，这里不替 owner 改它。

## 7. 飞书适配器（`adapters/channels/feishu.py`）

### 7.1 实现基础：`lark-channel-sdk`（Python）

**决定：** 飞书适配器基于官方 **[lark-channel-sdk（Python）](https://github.com/larksuite/channel-sdk-python)**（包名 `lark_channel`，MIT AND BSD-3-Clause），**不**自行实现 WebSocket 长连接帧协议。主入口 **`FeishuChannel`**，默认 `transport="websocket"`（与文档中的 longconn 同义）；可选 `transport="webhook"` 作后备（第 7.7 节）。

| 官方组件 | Baton 中的角色 |
| --- | --- |
| `lark-channel-sdk` / `FeishuChannel` | 长连接、订阅 `cardAction`（`CardActionEvent`）、SDK 层 dedup（`DedupStore` + `ICache`）、安全模式（`SecurityConfig`: `compat` / `audit` / **`strict`**）、可选 `BotLoopGuardConfig`（默认关） |
| `lark-oapi` | **仅** channel-sdk 未封装的 OpenAPI（例如按 `message_id` 全量 PATCH 卡片）；旧版 `lark_oapi.channel` 仅兼容，新 Channel 能力在 channel-sdk |
| `lark-cli`、`lark-openapi-mcp` | agent/人本机工具，**不进** Baton 服务（第 1 节） |

控制台须订阅 **`card.action.trigger`** 并选长连接（或 webhook 后备），否则按钮无效。收其他 bot @ 本 bot 需 `im:message.group_at_msg.include_bot:readonly`，否则**静默收不到**（本设计默认不依赖该路径）。

官方 Node/Java“处理回调”页与卡片回传页仍说明：`card.action.trigger`（schema `2.0`）可走长连接，**3 秒内**须返回 toast 或更新后的卡片，长连接会**重复投递**（见上列 GitHub 与 <https://open.feishu.cn/document/feishu-cards/card-callback-communication?lang=zh-CN>）。

**CardKit 硬约束（审批卡必须遵守）：**

- 卡片 **JSON 2.0** 要求客户端 **≥ 7.20**，低版本提示升级。
- 全量更新须 `update_multi=true`；单卡最多 **200** 组件；每张卡 **10 次/秒** 操作上限（见 CardKit 全量更新文档）。
- **审批类卡片禁止流式模式**：流式卡用户点击后服务端**无法立即**更新该卡（官方“流式更新卡片”页）；流式仅用于进度类 `status.*` 可选模板。
- 回调响应中的卡片结构须保持 2.0，不得退回 1.0（错误码 200830）。

**安全与去重（两层，权威在 Baton）：**

1. SDK：`SecurityConfig(mode="strict")` 生产默认；上线前可用 `audit` 观察。显式设置 `max_concurrent_ws_handlers` 等并发上限（SDK 默认不限）。
2. SDK **`DedupStore` 必须按 `header.event_id` 建键**（3d 对照 channel-sdk 配置/API 核实）；**禁止**按消息 id、`event.token` 等合并不同点击。若 SDK 无法保证“不同 `event_id` 永不合并”，则**关闭** SDK 去重，只保留核心 + `x_channel_action`。权威幂等：`dedupe_token = sha256(event_id)`；`event.token` → `card_update_token`，不参与审批去重。
3. `BotLoopGuardConfig` 仅为启发式兜底；权威防循环仍由 Baton B4-2（轮次/花费）负责。

**可选依赖隔离：** `requirements-feishu.in` / 带哈希的 `requirements-feishu.txt` 锁定 `lark-channel-sdk` 及按需的 `lark-oapi` 版本；只装进通道服务镜像。核心与 bridge 零第三方依赖。3d 更新 [THIRD_PARTY.md](../THIRD_PARTY.md)。

**NOT_PROVEN（留 3d PR 对照官方源码/文档核实）：** channel-sdk 要求的 Python 最低版本、是否硬依赖 `lark-oapi`、`cardAction` 处理器返回 toast/卡片的确切 API（README、`docs/webhook-server.md`、`docs/cardkit-streaming.md`）；2026-07–10 飞书 changelog 动态页未抓取；`lark-openapi-mcp` 版本与工具数（镜像站信息）。

### 7.2 传输：WebSocket 默认

沿用官方限制（与旧版 7.1 相同，来源见 Node/Python“处理回调”页）：

- 企业自建应用；只用新版 `card.action.trigger`，不用旧版卡片回传；
- 每应用最多 50 连接；**集群模式、不广播** → 每环境**一个**通道实例，测试/生产**不同**飞书应用；
- **3 秒**回调预算（第 4.4、7.4 节）；禁止 3xx 应答；
- WebSocket 建连后推送明文，纵深靠 `app_id`/`tenant_key` 字段校验与第 11 章威胁模型。

### 7.3 `card.action.trigger` 到 `ActionEvent`

字段来自官方回调结构体（见上面的“卡片回传交互回调”页）：

| `ActionEvent` | 来源 |
| --- | --- |
| `channel` | 固定 `"feishu"` |
| `instance` | 配置中的 `feishu:<app_id>` |
| `external_user_id` | `event.operator.open_id` |
| `action`、`value` | `event.action.value`（对象；`a` 给出动作） |
| `comment` | `event.action.form_value[<输入框 name>]`（表单容器内的输入框；不在表单里的输入框用 `action.input_value`） |
| `event_id` | `header.event_id`（每次回调唯一；重复投递不变） |
| `dedupe_token` | `sha256(event_id)` 十六进制，供审计与 `x_channel_action` |
| `card_update_token` | `event.token`（CardKit 更新凭证；可选保存，**不参与**幂等；原始值不出适配器、不写日志） |
| `external_message_id` | `event.context.open_message_id` |
| `external_chat_id` | `event.context.open_chat_id` |
| `event_time` | `header.create_time`（微秒级时间戳） |

适配器还要拒绝：`schema` 不是 `2.0`；`header.event_type` 不是 `card.action.trigger`；`header.app_id` 或 `header.tenant_key` 与配置不符；`event.operator.tenant_key` 与配置租户不符；出现 `delivery_type`（链接预览卡片，本设计不注册 `url.preview.get`）。长连接下由 SDK 在建连时鉴权，这些字段检查仍然执行，作为纵深防御。

### 7.4 三秒预算与重复投递

- 处理函数只做：解析、查 `seen_action`、提交 `pending_click`、**一次** `channel_act`（超时 1.5 秒）、生成应答。总预算 2.0 秒。`pending_click` 在调用 `channel_act` **之前**就落盘，快速路径和超时路径一样。若超时：返回“已收到，处理中” toast，不写 `seen_action`；后台队列用同一 `event_id` 重试。飞书收到 toast 后**不会**再投递同一回调（见 4.4）。进程重启且本地库还在时，`start` 先排空 `pending_click`：补完尚未提交的 `channel_act`，也对已经提交但卡片还没写上的行按当前状态 `upsert_card`。只靠活进程里的队列，崩溃后这张卡片就停在旧的待办上。慢速 `upsert_card` 亦入队，不阻塞回调线程。
- 飞书这条回调的响应体**只有** `toast`（`type` 为 `info`、`success`、`error`、`warning`；官方例子同时给出 `content` 与 `i18n`），**没有** `card`。`ActionOutcome.card` 在这条路径上留空。共享更新走 `upsert_card`（PATCH，`update_multi`）。回调里若带上刚渲染的“1/2”，而另一票的终态 PATCH 先到，迟到的回调会把按钮写回共享卡片；数据库写锁排不了这个到达顺序。例如：

```json
{
  "toast": {"type": "error", "content": "无权放行", "i18n": {"zh_cn": "无权放行", "en_us": "Not authorized"}}
}
```

- 长连接下**同 `event_id`** 的重复投递会发生。SDK 去重键只能是 `event_id`（7.1）。权威是 `x_channel_action`：`channel_act` 在点击者授权之前返回已有行（4.5，新的写事务，不是 `_mutate`）。`seen_action` 只缓存 toast。重放的回调也只有 toast。`pending_click` 还在时，补写走更新队列里的当前状态，不走回调体。
- **同 `event_id` 重复投递** → 与首次相同的 toast，仍然没有卡片。**同一人两个 `event_id`** → 第二票 `forbidden`（仍 `awaiting_gate` 时），toast“无权放行/已投过票”，**不是**“已有结论”，也不补写卡片。**两人确认、两个不同 `event_id`** → 两票都计（C5b；3d 冒烟确认 SDK 不吞第二 `event_id`）；两条回调都只有 toast，卡片只由队列按最新 version 更新（C5c）。**任务已终态或 epoch 变了** → `stale_claim`，刷新走队列。

### 7.5 发送与更新

- 发送：`POST /open-apis/im/v1/messages?receive_id_type=chat_id`，消息类型 `interactive`；带 `uuid` 去重。权限：官方页面列出 `im:message`、`im:message:send_as_bot`、`im:message:send` 三者开其一即可，本设计申请**最小的** `im:message:send_as_bot`。
- 更新：`PATCH /open-apis/im/v1/messages/:message_id`。官方限制：调用身份必须与发送者一致；只能更新未撤回的卡片；更新前后卡片的 `config` 里都要显式写 `"update_multi": true`（共享卡片，更新对所有接收者可见）；不支持批量发送的消息和仅特定人可见的卡片；14 天内；单条消息 5 QPS；卡片不超过 30 KB。权限三者开其一，仍然是 `im:message:send_as_bot`，无需额外申请 `im:message:update`。
- 卡片只放中立结构里的内容，按下表转成飞书 JSON 2.0 组件。**具体字段名在 3d 对照官方“卡片 JSON 2.0 结构”页确定**，本文不抄写未核实的组件字段：

| `CardSpec` | 飞书卡片 |
| --- | --- |
| `title`、`status` | 标题与标题颜色模板（pending 蓝、approved 绿、rejected 红、changes_requested 橙、info 灰、alert 红） |
| `fields` | 正文里的键值行 |
| `links` | 带链接的文字按钮或文本链接 |
| `actions` | 按钮，`behaviors` 里 `type: "callback"`，回传值是对象 |
| `needs_text` | 表单容器内的输入框加提交按钮，输入值从 `form_value` 取回 |
| `terminal_state` | 去掉按钮，替换为一行终态文字 |

### 7.6 凭据

- 使用**专用的 Baton 飞书应用**，由 owner 创建；**不是**告警中继应用。只申请机器人发消息能力（`im:message:send_as_bot`）并启用机器人能力；卡片回传交互回调在官方页面里的权限要求是“暂无”。不申请通讯录权限（`contact:user.employee_id:readonly` 只为拿 `user_id`，本设计只用 `open_id`）。
- 凭据文件，每个一个文件，**按路径读取**，在部署宿主机上属主 `root:root`、模式 `0600`，**从不进入仓库**，不放环境变量或命令行（它们能在进程列表和容器元数据里看到），**不打印、不写日志**：

| 文件 | 内容 | 何时读取 |
| --- | --- | --- |
| `app_id` | 应用 App ID | 始终 |
| `app_secret` | 应用 App Secret | 始终 |
| `encrypt_key` | 事件加密策略的 Encrypt Key | 仅 HTTPS 回调后备方式 |
| `verification_token` | Verification Token | 仅 HTTPS 回调后备方式 |

长连接模式只在建连时鉴权，用 `app_id` 与 `app_secret`；`encrypt_key` 与 `verification_token` 只有在 `transport = "https"` 时才加载，长连接模式不读取它们（暴露面越小越好）。配置文件只写路径，格式示意（宿主机上的部署文件，示例值全是占位）：

```json
{
  "channel": "feishu",
  "transport": "longconn",
  "credentials": {
    "app_id_file": "/run/credentials/baton-channels/feishu_app_id",
    "app_secret_file": "/run/credentials/baton-channels/feishu_app_secret"
  },
  "baton": {"url": "https://baton.example.com", "token_file": "/run/credentials/baton-channels/baton_token", "ca_file": "<CA_FILE>"},
  "timezone": "Asia/Shanghai",
  "targets": {"work": {"kind": "chat", "id": "oc_xxxxxxxx"}, "ops": {"kind": "chat", "id": "oc_yyyyyyyy"}},
  "callback_budget_ms": 2000
}
```

- 进程读到的凭据放进一个 `Secret` 包装类型：`__repr__` 与 `__str__` 只输出 `<redacted>`，不能被格式化进日志。SDK 的日志级别固定在 INFO 以上，不使用官方示例里的 DEBUG 与 `print(lark.JSON.marshal(data))`（它们会把载荷与令牌打到日志里）；沿用 bridge 的做法，关闭第三方库日志，只写白名单元数据审计。
- `root:root 0600` 的文件如何让服务进程读到，由部署侧决定：例如 systemd 的 `LoadCredential=`（宿主机文件保持 `root:root 0600`，服务只拿到临时副本；具体语义部署时对照 systemd 文档核对），或容器以 root 启动、读完后降权。要求只有一条：服务用户不需要比读更多的权限，文件不被复制进镜像或仓库。
- 轮换：`app_secret` 在飞书开发者后台重置后更新文件并重启服务；`encrypt_key`、`verification_token` 同理。Baton 令牌按现有令牌轮换流程。怀疑泄漏的响应步骤见第 11 章。

### 7.7 后备方式：HTTPS 回调

长连接被禁止或不可用时，改用 `FeishuChannel(transport="webhook")` 或等价 HTTPS 监听：飞书向配置的地址 POST。地址经**既有的 JB 443 公共入口**（部署侧概念，本仓库不记录其地址）转发到通道服务的内部监听端口，路径 `/channels/feishu/callback`。官方页面把两种订阅方式作为二选一的配置项列出，本设计按“不能同时使用”理解（**待核实**）；切换在开发者后台手工完成。

**后备方式启动条件：** `encrypt_key` 与 `verification_token` 文件**必须**存在且非空；缺少任一项则**拒绝启动** webhook 传输（Verification Token  alone 不绑定正文，泄漏后可伪造 `operator`，不可作为唯一认证）。

这条路径必须**同时**做来源 IP 白名单与 **Encrypt Key 正文签名校验**，任何一项都不能单独成立：

1. **IP 白名单**（入口层）：官方“获取事件出口 IP”接口 `GET /open-apis/event/v1/outbound_ip` 返回开放平台推送所用的 IP，页面提醒 IP 可能变化、应定期拉取并更新防火墙规则。它需要额外权限 `event:ip_list`，只有启用后备方式时才申请。该接口列出的是“事件”出口，是否也覆盖卡片回调**待核实**，3d 在启用后备方式前确认。白名单只是缩小面，不是认证。
2. **签名校验**（通道服务，**强制**）：`X-Lark-Signature` 应等于 `sha256(X-Lark-Request-Timestamp + X-Lark-Request-Nonce + encrypt_key + 原始正文)` 的十六进制（注意这是对拼接内容的 SHA-256，不是 HMAC，且要用原始正文字节，不要先解析再计算）；用常数时间比较；再对时间戳加五分钟容差（飞书页面没有写容差，这是本设计自己的规则，与 W2-5 一致，**待用真实流量核实**）。未通过则 **400**，不解析正文、不调用 Baton。
3. **解密**：正文加密时密钥为 `sha256(encrypt_key)`，AES-256-CBC，前 16 字节是 IV，整体 base64。官方页面给了测试向量（密钥 `test key`、密文 `P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk=`、明文 `hello world`），3d 的单元测试直接用它。
4. 解密后再检查 `header.token` 等于 Verification Token（**附加**一致性检查，不能替代第 2 步）、`header.app_id` 与 `tenant_key` 等于配置、`schema == "2.0"`。
5. 其余：只接受 `POST` 与 `application/json`，正文上限 64 KiB，同样的 2.0 秒预算，永远不返回 3xx，响应头不带内部信息。URL 校验请求（配置回调地址时的握手）按官方文档处理，3d 里补全。

多实例在 HTTPS 方式下技术上可行，但本阶段仍然只跑一个实例；若以后跑多个，`seen_action`、`pending_click` 与队列需要共享存储，否则启动恢复看不到另一进程已经应答的点击。

## 8. 群

- **运维告警群**：现有的告警中继（`BatonBoardDown`、`BatonMcpCallFailed`）保持不变，仍由中继应用发送。Baton 的飞书应用不加入这个群。通道服务自己的健康告警（长连接断开、连续更新失败、目标失效）也走既有中继，告警名在 3d 定为 `BatonChannelDisconnected`、`BatonChannelActionFailed`（保留名，尚未定义）。
- **Baton 工作群**：审批卡片与状态通知都发这里；响应超时（SLA）与预算告警也发这里并 @approvers。**由 owner 创建这个群并把机器人加进去**；群成员由 owner 管理，建议群里不放其他机器人（第 11 章“卡片仿冒”）。
- `@` 的对象来自绑定表：reviewers、`escalate_to`、owner 有有效绑定的才能 @（用 `open_id`），没有绑定的只显示名字。
- 群 id 是部署数据，写在宿主机上的配置文件里，不进仓库；仓库里只有带占位符的示例。

## 9. 契约测试计划

假适配器 `adapters/channels/fake.py`：纯内存，记录所有出站调用（`send_notice`、`upsert_card`），并允许测试注入入站点击（`RawInbound`）。它有一个可开关的“签名有效”标志，关闭时 `parse_action` 抛 `BadSignature`。契约测试与具体平台无关，写成参数化的 `AdapterContract`，任何适配器都要通过；飞书适配器再加自己的单元测试。测试放在 `tests/` 里，随完整套件在 PostgreSQL 上运行（`sh scripts/test_with_postgres.sh`，0 skipped）。

端到端的核心用例（3c；用真实的 bridge 存储与绑定夹具，依赖 3b）：

| 编号 | 场景 | 期望 |
| --- | --- | --- |
| C1 | 完整审批流：提交任务并派发 → 事件到达 → 核心发出一张 `pending` 卡片 → 已绑定且有委托的批准人点 `approve` → gate 通过 → 卡片原位变为 `approved` 终态 | 同一 `external_message_id` 被更新；终态文字含批准人 display_name 与 HH:MM；`x_gate` 一行；审计里有 `channel.action`（含 `channel`、`external_user_id`、`dedupe_token`）；任务 done |
| C2 | 未授权点击：已绑定但没有委托的人点 `approve` | toast“无权放行”；任务状态不变、没有 `x_gate` 行；卡片不变；审计 `channel.action_denied`；`x_channel_action` **提交**一行 `denied`。其后补上委托，同一 `event_id` 仍返回该拒绝 |
| C3 | 未绑定点击 | 同上，toast 附“尚未绑定”；审计 `channel.unbound_click`；`x_channel_action` 提交一行 `unbound`。其后完成绑定，同一 `event_id` 仍返回该拒绝 |
| C4 | 重复投递：同一 `event_id` 两次 | 只有一次 gate；两次都只有相同的 toast，回调都不带卡片。`seen_action` 与 `x_channel_action` 各一行 |
| C4b | 两人确认：第一票的 `event_id` 在终态之后再次投递 | toast 仍是第一票的成功提示；回调不带卡片；共享卡片保持终态 |
| C5 | 同一人两个不同 `event_id`（仍 `awaiting_gate`） | 第二票 `403 forbidden`；toast 不含“已有结论”；卡片仍 pending |
| C5b | 两人确认：两位审批人并发两个 `event_id` | 两票都计入；第一票后队列里的卡片是“1/2”；第二票后是终态。两条回调都只有 toast |
| C5c | 第一票的回调晚于第二票的终态更新到达飞书 | 共享卡片保持终态，按钮不会被写回。两条回调都没有卡片。同一对象同时最多一个在途 PATCH，终态在“1/2”那次返回之后再发，不并发 |
| C6 | `request_changes`：带文字 | 评论的 `thread_id` 等于任务 id，`list_comments(task_id=…)` 能读到，作者是点击者；不是 `thread_id` 等于评论自身 id 的孤立行。`x_gate.reason` 为该文字；`gate.reviewed` 的 metadata 不含这段文字。任务回到 `queued`；卡片终态“要求修改” |
| C7 | `request_changes`：文字为空或超过 1000 字符 | 被拒绝（`invalid`），任务不变，不写评论；`x_channel_action` 提交该拒绝 |
| C8 | 坏签名：`parse_action` 抛 `BadSignature` | 核心不调用 Baton；写 `channel.action_refused`；error toast |
| C9 | 伪造的 `value`：`k` 不匹配、`e` 与 `card.epoch` 不一致（含保留旧 `k`、把 `e` 改成更新的 epoch）、对象不在映射、`external_chat_id` 不符、`event_time` 太旧 | 全部拒绝，不调用 Baton；`expected_epoch` 不会采用未核对的 `e` |
| C10 | epoch 过期：映射里的 `epoch` 仍与点击的 `e` 一致，但任务已被重新提交 | `stale_claim` → warning toast，按当前状态刷新。映射若已经换成新的 `k` 与 `epoch`，旧按钮走 C9，不调用 Baton |
| C11 | 两人确认：第一位批准 | 卡片进度“已通过 1/2”来自 `upsert_card` 队列，回调无卡片；同一人再点被拒绝且不改卡片；第二位批准后队列把卡片推进终态 |
| C12 | 预算：`channel_act` 慢于 2 秒 | `pending_click` 在调用前已经提交（不是超时才创建）；返回“处理中” toast；**不**写 `seen_action`，Baton 此时还没有该行；后台队列用同一 `event_id` 完成 gate 并按当前状态刷新卡片；只决定一次 |
| C12b | 委托在首次成功后撤销，同 `event_id` 再次进入核心 | 仍返回首次 `x_channel_action` outcome，**不是** `delegation_required`。查询发生在 `channel_act` 自己的写事务里、点击者授权之前，不经过 `_mutate` |
| C12c | 拒绝或未绑定尚未被核心写入 `seen_action` 时，同一 `event_id` 重投；其间绑定或委托已经补上 | 仍是首次拒绝；不会按新状态通过 |
| C13 | 核心主体自己调用 `gate` | 被拒绝（`forbidden`），`channel_act` 是唯一路径 |
| C14 | 绑定在点击前被撤销、或已过期 | 视为未绑定，并提交 `x_channel_action` 的 `unbound` 行 |
| C15 | webhook 事件重复（相同 `webhook-id`）、签名坏、时间戳超出五分钟、两个密钥轮换期 | 重复只处理一次；坏签名与过期被拒；两个密钥都能通过 |
| C16 | 核心重启后（本地库保留 / 丢失） | **保留：** 启动时、接受新回调之前，排空每一条 `pending_click`。含两类：已应答“处理中”、Baton 尚未提交；以及 Baton **已经**提交、`upsert_card` 还没成功。同一 `event_id` 只决定一次，卡片按**当前**任务状态补写。同一个人用新 `event_id` 再点是 `forbidden`，不会把这张卡片补上。**丢失：** 待办对象得到新卡；已写入 `x_channel_action` 的成功或拒绝按 `event_id` 原样返回。残余：toast 已发出、本地库丢失、Baton 尚无该行时，这次点击无法补完 |

3b 另有桥侧测试：绑定/`bind` 撤销过期行后可重绑；`channel_act` 在**自己的**一次 `_transaction(write=True)` 里先查 `x_channel_action` 再做点击者授权（C12b），并且不调用 `_mutate`；拒绝与未绑定会**提交**动作行，异常逃出事务则该行不留（C2、C3、C12c）；`request_changes` 的评论与 gate 只在授权通过后写在同一个 `conn` 上，同提交，授权失败时两者都不写；`_comment` 的 `thread_id` 必须是任务 id，省略时不得依赖默认（会变成评论自身 id），也不得改调 `comment()`；各授权分支与 `gate` 一致；`event_id` 唯一；不写 `baton.operations`。3d：`DedupStore` 仅 `event_id` 或关闭 SDK dedup。3e：链接默认摘要（含 `/data` JSON）；仅 `Sec-Fetch-Mode: navigate` + `Sec-Fetch-Dest: document` 得全文；签名/过期/轮换/只读等同前。

飞书适配器单元测试（3d）用官方样例载荷，不联网：

- 官方“卡片回传交互回调”页的回调样例 → `ActionEvent` 的字段映射；
- 官方响应样例（`toast` 与 `card`）与本适配器生成的应答结构逐字段对照；
- 官方“接收事件”页的签名公式与 `test key` 解密向量；
- 拒绝路径：`schema` 不是 `2.0`、`app_id` 或 `tenant_key` 不符、`delivery_type` 存在、`token` 不符、时间戳过期、正文被改动；
- 卡片大小、字段截断、明文邮箱与手机号替换。

样例夹具放在 `tests/fixtures/feishu/`，每个文件顶部注明来源页面，只含官方页面里的掩码样例 id；夹具要通过 `scripts/check_public_tree.py`（不含真实 id、邮箱、非示例 IP）。真机冒烟（点击到达、3 秒内应答、原位更新、重复投递、令牌行为）不进 CI，写成 3d 的手工验收清单，由 owner 提供的测试应用执行。

## 10. PR 计划

| PR | 内容 | 依赖 |
| --- | --- | --- |
| **3a** | 本设计文档，加 `docs/architecture.md` 的一行链接 | 无 |
| **3b** | 身份绑定与代点击授权（bridge）：`x_channel_binding`、`x_channel_action`；离线管理员 `python -m bridge.channels bind/unbind/list`；**新的** `channel_act`（一次 `_transaction(write=True)`，不经过 `_mutate`：先按 `event_id` 返回已有行，再授权；拒绝与未绑定提交动作行；评论与 gate 仅在授权通过后写在同一 `conn` 上）；登记表“通道核心”标志；`_audit` 白名单与 `check_schema`；审计事件；更新 `docs/api.md`、`docs/security-model.md`；测试见第 9 章 | 审批人委托与 gate（已合入）；权限请求分支合入后才接 `decide_permission` |
| **3c** | 核心加假适配器与契约测试：`adapters/channels/core.py`、`fake.py`、事件源、映射与队列、模板、本地状态；bridge 的“观察者订阅”；本核心的内部 TLS 监听 | 3b；W2-5 已合入 main；观察者订阅的决定（第 12 章问题 1） |
| **3d** | 飞书适配器：`adapters/channels/feishu.py`，可选依赖锁文件，`THIRD_PARTY.md`，compose 里的通道服务（不发布端口），凭据加载，告警名，后备 HTTPS 回调，单元测试与真机冒烟清单 | 3c；owner 创建的专用飞书应用、测试应用与工作群 |
| **3e** | 带签名的只读链接：`link_sign`、`x_link_key`、`python -m bridge.links rotate`、`/board/o/` 路由与链接模式页面、网关与 nginx 路由、轮换与测试 | 无（只动 bridge、board、deploy）；密钥轮换沿用 W2-5 的做法 |

顺序：3b 与 3e 互相独立，可以并行；3c 在 3b 之后（3c 里的链接先用占位，3e 合入后换成真实签名）；3d 在 3c 之后。**生产启用**需要 3b、3c、3d、3e 全部合入，且真机冒烟通过。依赖图：

```
3a ─► 3b ─► 3c ─► 3d ─► 生产启用
                        ▲
3e ─────────────────────┘
```

每个 PR 保持卫生检查（提交身份与尾注，`scripts/check_public_tree.py`）为绿，并以 `sh scripts/test_with_postgres.sh` 跑完整套件，0 skipped。各 PR 的验收是第 9 章里对应的测试全部通过。

后续文档事项（不属于 3a 的范围，由对应 PR 一并修改）：[roadmap.md](roadmap.md) 的 W3-1、W4-1 与“Owner onboarding”按本设计改写，[space-replica.md](space-replica.md) 的“Owner integration”（飞书只给 owner 发保留事项提醒）与能力表更新，README 能力表，THIRD_PARTY。

## 11. 威胁模型

| 威胁 | 做法 | 控制 | 覆盖与残余 |
| --- | --- | --- | --- |
| **伪造回调** | 攻击者直接向回调地址 POST 一个看起来像 `card.action.trigger` 的请求，冒充某人点击 | 默认 WebSocket：无入站回调；建连用 `app_id`、`app_secret`。后备 HTTPS：**必须**配置 Encrypt Key；IP 白名单 + `X-Lark-Signature`（正文绑定）+ 解密 + `app_id`/`tenant_key`；缺 Encrypt Key 的配置拒绝启动；Verification Token 仅作附加检查 | 默认路径没有可伪造的入口。残余：信任飞书与 TLS；`app_secret` 泄漏见下行 |
| **重放** | 重放截获的回调或 webhook | **`event_id`** → `x_channel_action`（`channel_act` 的写事务在点击者授权之前返回已有行，含拒绝与未绑定）+ 只缓存 toast 的 `seen_action`；飞书回调始终不带卡片，共享更新只走按 version 合并的 `upsert_card` 队列；`pending_click` 在调用 `channel_act` 前落盘，启动时补完未提交的决定，也补写已提交但尚未 PATCH 的卡片；webhook Standard Webhooks | 已提交点击在委托撤销、或事后补上绑定/委托后，仍返回首次 outcome。迟到的“1/2”回调不能把终态卡片写回。残余：新 `event_id` 仍是新一次 gate；toast 已发出且本地库丢失、Baton 尚无该行时，慢路径补不完 |
| **点击者冒名** | 甲想以乙的权限放行；群里的人使用别人的卡片；卡片被转发 | 点击者只来自平台认证后的 `operator.open_id`，`value` 里没有身份；绑定只能由根创建、按 Space 隔离、绑定的人必须与登记表一致；新的 `event_id` 现查绑定与权限，同一 `event_id` 返回已提交的决定；`k`、`e` 与映射中的 `epoch`、`o`、会话校验挡住伪造和转发；租户校验挡住外部租户 | 覆盖“冒名”与“越权”。残余：他人使用乙已解锁的设备；根绑错了人（靠绑定审计与确认流程发现）；平台账号被盗；**核心被攻破**时可替已绑定且有权限的人伪造点击（4.5 的残余与收敛手段） |
| **链接泄漏** | 带签名的链接被转发、被预览抓取、留在浏览器历史或访问日志里 | 默认**仅标题**（`/data` 亦同）；顶层浏览器导航才全文；15 分钟 TTL；`no-referrer`/`no-store`/`noindex`；轮换 | 未识别 UA 的预览也只会摘要。残余：导航模式下有效期内可读全文 |
| **群聊里的卡片仿冒** | 群成员或别的机器人发一张长得一样的卡片，附上钓鱼链接，或发“已批准”的文字 | 只有我们的应用能让按钮触发我们的回调，仿冒卡片改变不了 Baton；真正的状态以 Baton 为准；卡片里的链接只指向配置的 `PUBLIC_URL`；卡片从不要求输入凭据；只有核心能更新我们的卡片；群成员与机器人由 owner 管理，群里不放其他机器人 | 仿冒不能产生任何 Baton 效果。残余：用户被外观骗去点钓鱼链接、被假文字误导；缓解靠群管理和统一的机器人名称与头像。群是否有“限制添加机器人”的设置**未核实**，3d 的群设置清单里核对 |
| **凭据暴露 / 卡片篡改** | `app_secret` 等泄漏；攻击者用同一应用凭据 PATCH 消息卡片，把真卡改成“已通过”，与 Baton `x_gate` 不一致 | 凭据 `root:root 0600` 文件、不进仓库/环境变量/日志；长连接不加载 encrypt 凭据；专用最小权限应用；channel-sdk `strict`；**泄漏响应**：立即轮换 `app_secret`、重启、用 `get_task`/`x_gate` **对照**群内卡片与 Baton 事实，必要时强制 `upsert_card` 刷新；审计 `channel.action` 与 gate 以 Baton 为准；用户以终态 toast + 刷新链接为准 | 不能伪造被 Baton 接受的点击（仍要飞书 `operator` + 绑定）。残余：`app_secret` 泄漏可**视觉上**篡改卡片，直到刷新；缓解靠轮换、对照 gate、短 TTL 链接 |
| **拒绝服务与骚扰** | 群里有人狂点按钮；事件风暴；被限频 | 令牌桶与按对象合并更新、审计限速、预算超时后异步完成；错误按平台限频退避；单点击幂等 | 覆盖基本的放大。残余：飞书限频是群内机器人共享的，其他机器人刷屏会拖慢更新 |
| **内容外泄到第三方平台** | 卡片内容留在飞书 | 卡片只放结论，细节是链接；字段长度上限；明文邮箱、手机号替换；工作群只有 owner 管理的成员 | 减小暴露面。残余：结论文字本身由平台保存，不适合放进卡片的内容不要写进任务结论 |

## 12. 需要 owner 或总工程师确认的问题

1. **观察者订阅。** webhook 只投递订阅者自己是收件人的通知，核心主体收不到 reviewers 的 `task.awaiting_gate`。提议：给 bridge 增加“Space 范围的观察者订阅”，仅限被标记的通道核心主体并要求 view 权限。这扩展了 webhook 语义，需确认后在 3c 实现。
2. **15 分钟与长寿命卡片。** 默认 15 分钟的链接对放几个小时的待办卡片会过期。选项：待办卡片的链接取 `min(响应时限, 运营者上限)`；每次更新重新签发；增加“刷新链接”按钮。默认值不动。
3. **“原位无权放行提示”的解释。** 实现为只对点击者可见的 toast，而不是改写共享卡片（理由见 4.4）。请确认。
4. **根自批不经卡片。** 卡片永远不带 `self_approval`；根批准自己提交或执行的工作只能走显式路径。请确认。
5. **`event_id` 权威幂等**已写入全文；3d 真机确认重复投递 `event_id` 不变、不同点击 `event_id` 不同；`event.token` 行为记入 NOT_PROVEN（7.1）。
6. **`lark-channel-sdk` + CardKit 真机冒烟**（点击、3 秒应答、原位更新、并发两票）是启用生产的前置条件（7.1、7.4）。
7. **高风险范围是否允许经通道决定。** 例如在两人确认策略之外，再给 `x_approval_policy` 增加“不允许通道决定”的开关。
8. **结论的来源。** 卡片“结论”暂定取结果文本的首段并截断；若 W2-4 的 Ask 带有结构化的 `message`，应改用它。
9. **`im.chat.member.bot.deleted_v1` 事件是否多申请一项权限**（5 章最后一条）。
10. **“成员被移出群”事件**是否值得核实并订阅，只用于收紧（5 章“人离开群”）。
