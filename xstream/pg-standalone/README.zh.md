# PostgreSQL JSONB → APISIX Standalone

此目录是 fork 的增量扩展。保留全部上游代码，不增加任何 etcd 服务、连接或配置。
上游 `apisix/core/config_yaml.lua` 的加载代码不作修改。

## 职责和范围

- GitOps：节点、资源池、域名、制品版本、`config.yaml` 和启用插件清单。
- Playbook roles：安装运行环境、同步器和 systemd，注入 Vault 运行时凭据。
- 管理 API / PG：完整配置快照、不可变 revision、原子发布指针和审计。
- 同步器：通过 HTTPS 获取已发布版本，不需要数据库凭据；生成 `apisix.yaml`，
  原子替换，执行真实路由探测并回报。默认每 5 秒轮询。
- APISIX：现有 YAML 文件模式，每秒检查 mtime，热加载而不重启 worker。

首版仅管理 HTTP `routes`、`upstreams`、`services`、`plugin_configs`。
不复制 Accounts 的用户、订阅、团队、额度数据，不增加网关 Consumer 数据库。
配置 API 为受信管理面服务，使用服务令牌；最终用户 RBAC 由 Accounts/BFF 校验。
不同 gateway 使用不同 API 实例、PG gateway_id 和独立令牌。

## 数据库

用迁移账号执行 `schema.sql`。不要执行上游 Accounts 的全量初始化脚本。
版本创建后内容不可改；发布和回滚都增加 `generation`，回滚可以指向旧 revision。
`expected_generation` 防止两个管理员同时发布时覆盖对方。

生产运行账号不得拥有 schema，不赋予 DDL、TRUNCATE 或绕过触发器的权限。
最低权限示例（角色由数据库 Playbook 提前创建）：

```sql
GRANT USAGE ON SCHEMA gateway_config TO gateway_config_api;
GRANT SELECT, INSERT ON gateway_config.revisions, gateway_config.audit TO gateway_config_api;
GRANT SELECT, INSERT, UPDATE ON gateway_config.publications, gateway_config.node_status TO gateway_config_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA gateway_config TO gateway_config_api;
```

敏感内容由 APISIX 支持的 secret/env 引用处理，不将上游 Key 明文放入 JSONB。
APISIX 及原生校验器必须使用同一版本、插件与必要的运行时环境。

## 启动

安装 `requirements.txt`。将 `config.yaml` 放入 APISIX conf 目录；仅首次部署复制
`empty-apisix.yaml` 为 `apisix.yaml`。之后 Playbook 不再覆盖动态文件。
同步器需要 conf 目录写权限和独占的状态目录，运行用户应与 APISIX 读权限匹配。

通过 Vault 注入以下环境变量，不把值写到仓库：

- API：`GATEWAY_PG_DSN`、`GATEWAY_ADMIN_TOKEN`、`GATEWAY_NODE_TOKENS_FILE`。
- 节点：`GATEWAY_NODE_TOKEN`。
- 原生校验器：`APISIX_HOME`（默认 `/usr/local/apisix`）、可选 `RESTY_BIN`。

节点令牌文件为 `{"node-id":"<至少32字符的独立随机令牌>"}`，权限 0600。
管理令牌必须不同于全部节点令牌。节点身份从认证令牌解析，不能通过请求体指定。
API 固定监听 loopback，经现有 TLS 反代向授权节点提供服务。不要公开管理写接口。

```bash
python gateway_config.py serve \
  --gateway selfhost-uat --bootstrap /usr/local/apisix/conf/config.yaml \
  --validator /opt/apisix-pg-standalone/validate-native.sh

python gateway_config.py sync \
  --bootstrap /usr/local/apisix/conf/config.yaml \
  --api https://gateway-config.example.net \
  --validator /opt/apisix-pg-standalone/validate-native.sh \
  --probe /opt/site/bin/verify-gateway-routes
```

`--validator` 必填，candidate JSON 文件路径作为最后一个参数传入。
提供的 Lua 校验器复用当前安装的上游 schema 及 plugin.check_schema。
结构/引用校验通过后才执行原生校验，创建草稿、发布和节点写入前均校验。
插件中需要网络运行环境的检查必须在对应 APISIX 环境内执行。

`--probe` 必填，由部署环境提供；接收 YAML 路径及 `GATEWAY_CONFIG_METADATA`
JSON 环境变量（generation/revision/checksum）。它必须检查候选配置特有的路由行为，
在合理超时内重试并返回 0 才算验证成功。普通 `/healthz` 或进程存活不能证明版本生效。
配置写入后等待 2 秒再探测；完整命令超时 30 秒。APISIX 文件模式不提供全 worker
原子应用确认，所以探测成功仍只代表已探测请求，不承诺所有路由和 worker 同时切换。

## API

请求均携带 Bearer token；POST 使用 JSON，最大 4 MiB。

| 方法 | 路径 | 权限 | 请求 |
|---|---|---|---|
| POST | `/drafts` | 管理 | `{"config":{"routes":[],"upstreams":[],"services":[],"plugin_configs":[]}}` |
| POST | `/publish` | 管理 | `{"revision":1,"expected_generation":0}` |
| GET | `/snapshot` | 管理或节点 | 已发布版本、checksum、generation、完整配置 |
| POST | `/status` | 节点 | 当前 generation/revision/checksum/verified_at 或 error |
| GET | `/status` | 管理 | 节点身份、最近回报及回报时间 |

草稿本身也是不可变快照，修改草稿生成新 revision。`/publish` 在事务中更新指针和审计。
审计 actor 为管理 API 服务身份，用户级操作审计需要由 Accounts/BFF 留存和关联。
API 不接受直接 SQL、上游可执行代码或任意文件路径，启用插件由静态 bootstrap 限定。
管理员仍属于受信配置作者，应在上层 RBAC 限制上游地址等业务策略。

## 文件和恢复

- 临时文件位于目标同一目录，fsync 后 `os.replace`，文件权限 0640。
- 输出始终含全部四类资源；遗漏集合被写成空数组以删除旧配置。
- `#END` 结尾，mtime 严格递增，避免上游按秒检查遗漏连续发布。
- 单节点文件锁防止同步器并发写入。
- 配置 checksum 校验；拒绝过期 generation；允许新 generation 回退到旧 revision。
- 写入前保存旧文件和 pending 日志；探测失败恢复旧文件；进程崩溃后恢复未提交部署。
- 数据库/API 不可达时保持现有配置。节点回报失败会重试，不会重置发布指针。
- API 只保存配置，不安装 APISIX，不触发 CI，不进行 DNS 切换。

## 测试

```bash
python -m pytest tests
# 集成测试只对独立测试库执行 schema.sql：
GATEWAY_TEST_PG_DSN='postgresql://...' python -m pytest tests
```

上线前还需在目标固定版本运行 validate-native.sh，并用真实 APISIX 验证路由、插件、
多节点版本收敛、断库继续服务及探测失败恢复。不要把 Python 测试等同于 UAT 验收。

## 可复用部署角色

`deploy/roles/apisix_pg_standalone` 安装同步器、Python venv 和 systemd；APISIX 原生
安装由已有基础角色负责。角色不创建云资源，不执行数据库迁移，不创建 etcd。
`config.yaml` 属于启动配置，改变时校验并重启 APISIX；日常动态配置仅热加载。
首次以 deny-all 文件启动，之后角色不覆盖动态文件。Vault 环境文件和真实路由探测
脚本由环境集成提供。GitOps inventory 指定 `apisix_pg_mode: serve` 或 `sync`。
