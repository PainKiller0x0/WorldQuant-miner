# WorldQuant Miner（Rust）

这是一个面向 WorldQuant Brain 的 Alpha 研究、回测、Check 和 Submit 自动化系统。

当前 `dev-rust` 分支的生产运行时已经切换到 Rust。仓库中的 Python、Ollama 和 Docker 文件仍保留作历史参考或迁移材料，但线上主流程不再依赖它们。

## 当前状态

- 运行时：Rust 1.85+，SQLite，Tokio，Axum
- Miner：`agnes-2.0-flash`
- Evolver：`glm-4.7-flash`
- WorldQuant：通过官方 API 完成 simulation、状态轮询、Check 和 Submit
- 数据库：`wq_miner.db`
- 生产 Dashboard：`http://<host>:5000/`
- 兼容入口：`http://<host>:8080/`
- 生产分支：`dev-rust`

## 工作流

```text
Miner / Evolver
      │
      ▼
模型输出 → 本地表达式校验 → WorldQuant simulation
                              │
                              ▼
                         SQLite Alpha 库
                              │
                              ▼
        7 PASS 队列 → 单飞 Check → 8 PASS → 自动 Submit
```

每个 Alpha 会保留表达式、模拟指标、Check 结果、提交阶段、失败理由和原始 WorldQuant 响应。

### Miner 和 Evolver

- Miner 负责提出不同经济假设的候选因子。
- Evolver 从 Successful Pool 中轮换选择父 Alpha，执行结构化变异。
- 已提交成功的 Alpha 会进入 Successful Pool，优先作为 Evolver 父代。
- 未提交且没有失败的 Alpha 属于 Candidate Pool。
- simulation、Check 或 Submit 的确定性失败会进入 Failure Pool，并保留权威失败原因。

## 自动回测与提交

Submitter 使用持久化单飞队列，避免同时操作多个 WorldQuant Check：

1. 匹配本地 Alpha 与官网未提交 Alpha。
2. 对 `7 PASS + 1 PENDING` 的 Alpha 排队执行 Check。
3. Check 结果变成 `8 PASS` 后自动 Submit。
4. `SELF_CORRELATION=ERROR` 会按特殊规则尝试 Submit，并记录官网最终响应。
5. `Cannot submit Alpha: 1 test failed` 等失败会写入数据库 Failure Pool，不会伪装成成功。

提交服务由 systemd timer 调度，默认执行：

```bash
worldquant-miner-rs submit --limit 10 --auto-submit
```

## 自适应 WorldQuant TPM

WorldQuant API 的请求速率由 Rust 自适应控制器管理，不需要手动填写固定 TPM：

- 当前控制范围：6–60 TPM
- 根据 API 延迟、simulation 完成时间、429 和超时自动升降速
- 429 会触发退避和冷却
- 当前速率会持久化到 `limiter_runtime.json`
- Submitter 使用独立的 `limiter_submitter_runtime.json`

这里的 TPM 是 API 请求数/分钟，不是每小时 Alpha 产量。实际产量还取决于 WorldQuant simulation 的计算时间和模型响应速度。

Dashboard 会显示当前 TPM、范围、API 延迟、回测均值、冷却状态和最近调整原因。

## 本地表达式预检

模型输出不会直接发送到 WorldQuant。提交 simulation 前会检查：

- Markdown 反引号、全角标点和其他非法字符
- 括号是否平衡
- 字段、算子和禁用标识符
- 算子参数个数
- 时间序列 lookback 是否为 2–252 的整数
- 指数参数是否为数字
- 外层算子错误时禁止误提取嵌套子表达式

历史待重试 Alpha 也会执行同样的预检。被拦截的记录会写入 `LOCAL_VALIDATION_ERROR`，不会继续消耗官网回测请求。

## Dashboard 页面

| 页面 | 用途 |
|---|---|
| `/` | Miner、Evolver、指标和最新日志 |
| `/pending` | Candidate Pool、提交状态和失败理由 |
| `/queue` | Check/Submit 排队队列和实时阶段 |
| `/chart` | 产出、fitness 和质量趋势 |
| `/settings` | 配置查看与可调整设置 |
| `/healthz` | 服务健康检查 |
| `/api/status` | 当前运行状态 JSON |
| `/api/submission_queue` | 提交队列 JSON |

日志区域按最新记录优先显示。生产服务日志可通过以下方式查看：

```bash
journalctl -u worldquant-rust-worker.service -f
journalctl -u worldquant-rust-dashboard.service -f
journalctl -u worldquant-rust-submitter.service -f
```

## 配置

生产环境使用仓库根目录的 `.env`、`api_config.json` 和 `system_config.json`。敏感文件不应提交到 GitHub。

`.env` 至少需要：

```dotenv
WQ_USER_ID=your-worldquant-user-id
WQ_API_KEY=your-worldquant-api-key
WQ_DATABASE=wq_miner.db
WQ_API_CONFIG=api_config.json
WQ_SYSTEM_CONFIG=system_config.json
WQ_LISTEN=0.0.0.0:5000
WQ_COMPAT_LISTEN=0.0.0.0:8080
```

`api_config.json` 使用 OpenAI-compatible Chat Completions 配置：

```json
{
  "miner_config": {
    "model_name": "agnes-2.0-flash",
    "api_key": "...",
    "base_url": "https://..."
  },
  "evolver_config": {
    "model_name": "glm-4.7-flash",
    "api_key": "...",
    "base_url": "https://..."
  }
}
```

## Rust CLI

在 `rust/` 目录执行：

```bash
# 编译和测试
cargo fmt --check
cargo test
cargo build --release

# 健康检查
./target/release/worldquant-miner-rs health

# 同时运行 Miner 和 Evolver
./target/release/worldquant-miner-rs run both --interval-secs 60

# 启动 Dashboard
./target/release/worldquant-miner-rs dashboard

# 执行一次提交队列处理
./target/release/worldquant-miner-rs submit --limit 10 --auto-submit
```

## 生产部署

线上使用以下 systemd 单元：

- `worldquant-rust-worker.service`
- `worldquant-rust-dashboard.service`
- `worldquant-rust-submitter.service`
- `worldquant-rust-submitter.timer`

部署新版本的一般流程：

```bash
cargo fmt --check
cargo test
cargo build --release
install -m 0755 target/release/worldquant-miner-rs /usr/local/bin/worldquant-miner-rs
systemctl restart worldquant-rust-worker.service
systemctl restart worldquant-rust-dashboard.service
systemctl restart worldquant-rust-submitter.timer
```

Rust 构建缓存保留在 `rust/target/`，避免每次部署重新下载和编译全部依赖。

## 目录说明

```text
.
├── rust/
│   ├── src/
│   │   ├── domain.rs       # Alpha、表达式策略和校验
│   │   ├── gateway.rs      # WorldQuant API、轮询和自适应限速
│   │   ├── llm.rs          # Miner/Evolver 模型调用和表达式提取
│   │   ├── store.rs        # SQLite 持久化和 Alpha Pool
│   │   ├── workflow.rs     # 生成、模拟、Check、Submit 工作流
│   │   ├── dashboard.rs    # Dashboard 和 API
│   │   └── main.rs         # CLI 入口
│   └── deploy/             # systemd 单元模板
├── templates/              # Dashboard 页面
├── static/                 # Dashboard 静态资源
├── wq_miner.db             # 运行时 Alpha 数据库，不提交
├── api_config.json         # 模型配置，不提交敏感内容
├── system_config.json      # Dashboard 和策略配置
└── .env                    # WorldQuant 和运行时环境变量，不提交
```

## 安全注意事项

- 不要把 WorldQuant、模型 API key 或 GitHub token 写进 README、源码或 Git 历史。
- 生产环境优先使用仓库级 GitHub Deploy Key，不要把个人私钥复制到虚拟机。
- 数据库、运行时 limiter 状态、日志和备份都属于运行时文件。
- 修改提交逻辑前先备份 `wq_miner.db`，并运行完整 Rust 测试。
