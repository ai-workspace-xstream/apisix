# 固定区域域名，按用户权益分配节点

本文件记录目标契约；Accounts 权益解析、订阅下发、Agent 用户过滤与区域前端接入
仍需在各自仓库实现和验收。当前扩展实现的是 PG 配置发布与 APISIX 文件同步。

## 用户可见行为

用户选择 `JP 区域`，VLESS 连接的 address 和 TLS SNI 始终是
`jp-xconnect.svc.plus`。区域下拉列表按 canonical region ID 去重，不能把多个
物理节点或套餐 pool 各渲染成一个 `JP 区域`。root 资源管理页仍可查看完整分布。

示例资源声明（用户给出的节点标签，不是本次创建的云实例）：

| 区域 | 权益 | 分配节点 |
|---|---|---|
| jp | plus | jp-xconnect-c6g-medium-01.svc.plus |
| jp | free，自然月 3GB | jp-xconnect-t4g-small-01.svc.plus |
| jp | free，自然月 3GB | jp-xconnect-t4g-micro-01.svc.plus |
| jp | team:<tenant_id> | 当前团队绑定的专属节点 |

VPS/Cloud 是供应商与服务等级属性，不应只凭实例名决定权益。AWS/GCP/Azure
均可加入合适的 pool，节点选择由 Accounts 的有效权益和资源声明共同决定。

## 传输机制

```text
用户选择 JP（配置中的域名仍为 jp-xconnect.svc.plus）
                ↓ HTTPS / XHTTP
        区域 TLS 前端 → APISIX Standalone
                ↓ 识别服务端下发的路由路径
        /xconnect/r/<opaque-binding>/*
                ↓ 对应一个稳定的后端节点
        plus 节点 / free 节点 / 当前团队专属节点
                ↓
        Xray 校验该 pool 下发的用户凭据
```

Accounts 在配置生成时分配节点，并生成不含物理节点名的 binding 路径。
多个用户可以使用同一节点 binding，不必为每个用户建立 APISIX Consumer。
binding 是调度标识，不是授权凭据；擅自修改路径不应获得其他 pool 的 Xray 权限。

APISIX 将 binding 前缀改写为后端的统一 XHTTP base path，并完整保留后面的
session ID、序号以及查询参数。不能把整个请求 URI 都改写成固定路径。
同一 binding 的上行、下行和 packet-up 请求固定到同一节点；不得在节点之间
对这些 HTTP 请求直接 round-robin。节点内的连接调度交由 Xray 处理。

新连接重新分配可生成新 binding。迁移时旧 binding 和节点先进入 draining，
已有会话不应被无提示迁到另一节点。节点故障后的恢复需要客户端重建 XHTTP 会话。
这不承诺现存 TCP 连接无损迁移。

此机制利用 XHTTP 的 HTTP path，不解析 VLESS UUID，不需要 Envoy，也不修改
Xray 协议。兼容性以实际支持 XHTTP 的订阅导入客户端验收；纯 VLESS/TCP 或
TLS passthrough 无法直接复用 HTTP path 路由。

## 权限与状态变更

- 用户只能获取当前有效模式的 binding 和凭据。
- plus 仅下发至 plus pool；free 仅下发至 free pool。
- 团队凭据限定 tenant，不能仅凭 team 角色匹配全部团队节点。
- free 额度耗尽由 Accounts 撤销 Agent/Xray 授权，下月恢复；APISIX 不计算流量账单。
- 套餐切换或过期时同步更新入口配置和节点凭据；旧路径的存在不代表继续有权使用。
- root 可管理全部 pool，不代表其账户在所有节点自动获得代理使用权益。

路由发布仍经 `管理 API → JSONB revision → 校验 → publication → 同步器 → YAML`。
GitOps 管理静态资源映射，PG 保存其运行时路由投影，禁止两者同时独立编辑同一资源。

## 容量与验收

同域名方案中的区域 APISIX 承载实际代理流量，需要按区域总吞吐规划带宽、连接数、
XHTTP 流式转发及超时。入口性能会限制后端优质节点收益；跨云转发也有传输成本。

上线前至少验证：区域列表仅一个 JP；free/plus/team 路由到正确后端；修改路径无法
越权；XHTTP 上下行保持同节点；订阅更新、额度断流、节点故障和 draining 行为。
本地配置同步测试不替代上述端到端业务验收。
