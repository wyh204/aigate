# aigate

一个 OpenAI 兼容的 LLM 网关：**把大模型调用成本砍下来，并用评测证明质量没掉。**

> 求职作品集项目。一句话定位：不是又一个聊天机器人，而是「让公司少花钱、并且说得清为什么没变差」的那一层。

## 为什么做这个

- 招聘市场上真实存在的岗位：AI 网关后端、AI 基础设施、LLM Platform Engineer。要求就是路由、缓存、限流、成本、可观测性 —— 这个项目一项不落。
- 能量化：成本降幅、P95 延迟、TTFT、缓存命中率、评测通过率。简历上一行数字比十行"负责某某模块"有用。
- 是正经后端题：并发、流式、重试边界、多租户隔离、幂等，面试官有得往下问。
- 一个人 6–8 周能做完，且可以开源。

刻意不做的：TODO List、商城、后台管理系统、又一个 RAG demo —— 这些在筛简历时区分不出工程能力。

## 当前进度

M0–M5 已完成并且跑得起来：

- OpenAI 兼容 `POST /v1/chat/completions`，**非流式 + SSE 流式**
- Provider 抽象：`mock`（离线、确定性、零成本）+ 任意 OpenAI 兼容上游（OpenAI / DeepSeek / Qwen / vLLM / Ollama，只改 `base_url`）
- 别名路由 + fallback 链；**首包之前失败会自动切下一个 provider**
- 每次调用落一条结构化 trace，并异步写入数据库：tokens、延迟、TTFT、成本、是否估算
- 计量层：SQLite（零配置）/ Postgres（`AIGATE_DATABASE_URL`）双支持，Alembic 迁移
- 租户 / API Key（只存 SHA-256）/ 月度配额（token 与美元双口径，超了返回 429）/ 成本看板聚合
- 管理端：建租户、发 key（明文只出现一次）、吊销、改配额、改价格表、按天/模型/provider 聚合成本
- 两级缓存：精确缓存（默认开）+ 语义缓存（`tenant + model + 参数` 分区，阈值 0.92），命中时 **cost 记 0、`saved_cost_usd` 记省下的钱**，配额检查在缓存之前
- 评测子系统：JSONL 题集、确定性 grader + LLM 裁判、CLI 与 `/admin/evals/*`、基线对比、**2pp 红线 gate**（不过就退出码 2 / 决策 BLOCKED）
- 59 个测试，`pytest -q` 全绿

## 快速开始

```powershell
cd aigate
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

不配任何 key 也能跑，走 mock provider：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock","messages":[{"role":"user","content":"你好"}]}'
```

流式（`-N` 关掉 curl 缓冲，能直接看到 SSE 分块）：

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

接真模型，两种方式任选（默认 `default` 路由指向 DeepSeek）：

```powershell
# 方式一：项目根目录的 .env（已在 .gitignore 里，推荐本地开发）
#   DEEPSEEK_API_KEY=sk-...
# 方式二：当前终端的临时环境变量（真实环境变量优先级高于 .env）
$env:DEEPSEEK_API_KEY = "sk-..."
```

key 从 https://platform.deepseek.com/api_keys 取，账户需要有余额（跑完整评测也就几分钱）。填好后重启服务，`/healthz` 里能看到 `deepseek` provider，`default` 路由会从 mock 切到 `deepseek:deepseek-chat`。

Docker：

```bash
docker compose up --build          # 只起网关（SQLite，数据在卷里）
docker compose --profile full up   # 再加 Postgres / Redis
```

换 Postgres（Rust 版驱动是 asyncpg，URL 前缀别写错）：

```powershell
$env:AIGATE_DATABASE_URL = "postgresql+asyncpg://aigate:aigate@localhost:5432/aigate"
.\.venv\Scripts\python.exe -m alembic upgrade head
```

SQLite 是开箱即用路径（启动时自动建表）；Postgres 用 Alembic 管 schema，`alembic check` 保证模型和迁移不漂移。

**从旧版本升级**：`create_all` 只补缺少的表，不会给已有表加列。本地 SQLite 开发库最省事的是直接删掉 `aigate.db`（里面只有 mock 数据）重启；Postgres 部署本来就该用 Alembic 建库，版本表可信，`alembic upgrade head` 即可。启动时如果检测到列缺失，日志里会有一条 `schema_drift` 警告，接口返回 503 `database_error`，而不是等第一次查成本时才抛 500。

## 计量与管理端（M3）

打开管理端只需要一个 token（不配就整段返回 503，**默认关闭而不是默认开放**）：

```powershell
$env:AIGATE_ADMIN_TOKEN = "dev-admin-token"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

```bash
# 发一把 key（明文只返回这一次，库里只有 SHA-256）
curl -X POST http://127.0.0.1:8000/admin/keys \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"tenant":"dev","name":"laptop"}'

# 看这个月花了多少钱、按天/模型/provider 分桶
curl "http://127.0.0.1:8000/admin/usage?group_by=model" -H "x-admin-token: dev-admin-token"

# 最近请求明细（含 cost_usd、ttft_ms、usage_estimated）
curl "http://127.0.0.1:8000/admin/requests?limit=20" -H "x-admin-token: dev-admin-token"

# 配额：超了返回 429 quota_exceeded，token 与美元两个口径取严
curl -X PUT http://127.0.0.1:8000/admin/quotas/dev \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"tokens_limit":1000000,"cost_limit_usd":5}'
```

计量的写入路径是**异步**的：请求结束把事件丢进有界队列，后台任务批量落库。队列满了丢事件并计数，绝不让数据库把每个请求拖慢。管理端读之前会先 flush，所以刚发生的调用立刻能看到，不用等下一个批次。

## 评测（M5）

调便宜模型、开缓存、上难度路由，都会动质量。**没有评测，所有"降本 X%"都是自说自话**。这一层的作用只有一件事：给每个策略一个可复现的通过率，并卡一条红线 —— **通过率比基线掉超过 2 个百分点（2pp）的策略不允许上线**。

题集就是 JSONL，一行一个 case：

```json
{"id": "math-1", "input": "计算 17 * 23，只输出数字", "expected": "391", "grading": {"kind": "numeric"}, "tags": ["math"]}
{"id": "style-1", "input": "用一句话解释什么是语义缓存", "grading": {"kind": "llm_judge", "rubric": "必须提到『相似』或『语义』，且不超过 40 字"}, "tags": ["explain"]}
```

`grading.kind` 支持 `exact` / `contains` / `regex` / `numeric` / `llm_judge`。前四种是确定性判分（不花钱、可复现）；只有规则写不出来的才用裁判模型，裁判本身也走网关，所以裁判的成本同样进账。

命令行跑一轮、再和基线比：

```powershell
# 跑一轮并存成基线（mock 全过，用来验证管线本身是通的）
.\.venv\Scripts\python.exe -m app.evals run --dataset data/eval_smoke.jsonl --name baseline --route mock --save eval_runs/baseline.json

# 跑一个策略，然后和基线比；BLOCKED 时退出码是 2，能直接串进 CI
.\.venv\Scripts\python.exe -m app.evals run --dataset data/eval_math.jsonl --name cheap-qwen --route cheap
.\.venv\Scripts\python.exe -m app.evals compare eval_runs/baseline.json eval_runs/cheap-qwen.json
```

输出长这样（真实跑出来的，候选策略故意让两个 case 不过）：

```
baseline   pass 100.0%  cost $0.000000
candidate  pass 66.7%  cost $0.000000
pass drop  33.3pp  (budget 2.0pp)
reason     pass rate dropped 33.3pp, budget is 2.0pp
decision   BLOCKED
```

HTTP 入口（同一套逻辑，给看板和 CI 用）：

```bash
curl -X POST http://127.0.0.1:8000/admin/evals/run \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"dataset":"eval_smoke.jsonl","name":"api-baseline","route":"mock"}'

curl http://127.0.0.1:8000/admin/evals/runs -H "x-admin-token: dev-admin-token"

curl -X POST http://127.0.0.1:8000/admin/evals/compare \
  -H "x-admin-token: dev-admin-token" -H "Content-Type: application/json" \
  -d '{"baseline_run":1,"candidate_run":2}'
```

几个刻意的设计：

- **评测走的是线上同一条 `Router` + `Provider`**，不是另写一条"评测专用"调用链。否则测出来的分数不代表线上。
- **一个 case 失败不会中断整轮**：记下 `error`，继续跑完，`80% 通过` 才不是"一次爆炸被四舍五入"。
- **每轮结果连同策略参数一起落库**（`eval_runs.strategy`），否则过两周没人能复现那个通过率。
- **成本只警告、不拦截**：质量掉 2pp 是红线，成本涨了是"值得看一眼"，不是"不许上线"。
- `llm_judge` 的裁判噪声是真实存在的；默认 2pp 预算就是为了容忍这部分噪声，同时还能抓住真回归。

三套题集：`data/eval_smoke.jsonl`（6 条，**管线自检**，mock 必全过）；`data/eval_math.jsonl`（9 条，真模型小样，用来验证 key 通不通）；`data/eval_quality.jsonl`（**50 条正式题集**：数值 15 / 事实 10 / 结构化 8 / 指令遵循 7 / 中文表达 10，含 5 条 LLM 裁判）。正式对比跑第三套，配好 `DEEPSEEK_API_KEY` 后用 `--route default`：

```powershell
.\.venv\Scripts\python.exe -m app.evals run --dataset data/eval_quality.jsonl --name ds-q50 --route default --save eval_runs/ds-q50.json
```

参考值（DeepSeek `deepseek-chat`，50 条）：`50/50 passed`、`1315 tokens`、`$0.000626`、`avg 847ms`。

## 缓存（M4）

两层，命中先在响应头告诉你，再在计量表里记一行「这次本该花多少、实际花了 0」：

| 层 | 键 | 默认 | 命中头 |
| --- | --- | --- | --- |
| 精确 | `sha256(tenant + provider + model + 归一化后的请求体)` | 开 | `x-aigate-cache: exact` |
| 语义 | `tenant + provider + model + system + 生成参数` 分区内做向量近邻 | 开，阈值 0.92 | `x-aigate-cache: semantic` + `x-aigate-cache-similarity` |

**什么进缓存是设计决定，不是默认行为**：只有 `temperature <= 0.2`、没有 `tools` / `response_format` / `seed`、`n` 为 1、全文本消息的请求才缓存。创作类请求缓存会撒谎；带工具的请求工具结果可能变了；**流式一律 bypass**（把缓存 completion 重新切片成 SSE 是另一套代码，M4 不做，宁可少省一点也不出错）。

三个容易说错、但面试官会追的点：

1. **配额在缓存之前。** 否则缓存变成绕过计数的后门：被限流的租户只要重复问同一个问题就还能白拿答案。
2. **命中行也落库，而且记两笔。** `cost_usd = 0`、`saved_cost_usd = 按价格表本该花的钱`。不记这笔，「省了 X%」就没有数据来源；先记成本再减去，报表会互相打架。
3. **语义缓存默认阈值 0.92，不是拍脑袋。** 内置的哈希 embedding 只是零依赖占位：实测 `capital of france` 和 `capital of germany` 相似度 **0.833**，只有把阈值抬到 0.9 以上才不把「法国」的答案发给问「德国」的人。接真 embedding 模型时这个数要重新测，别沿用。

关闭或调参：

```bash
AIGATE_CACHE_ENABLED=0                      # 整体关，退化成直连
AIGATE_SEMANTIC_CACHE=0                     # 只留精确层
AIGATE_SEMANTIC_THRESHOLD=0.95              # 更保守
AIGATE_REDIS_URL=redis://127.0.0.1:6379/0   # 换成 Redis（需多进程共享时）
```

命中率、省下的钱、各层计数：`GET /admin/cache/stats`；清空：`POST /admin/cache/purge`。单次请求想跳过缓存（调试用）：带 `x-aigate-cache: off` 或 `cache-control: no-cache`。

## 配置

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIGATE_LOG_LEVEL` | `INFO` | 日志级别，输出是一行一个 JSON |
| `AIGATE_API_KEYS` | 空 | 逗号分隔；**留空 = 关闭鉴权**，只适合本地开发 |
| `AIGATE_REQUEST_TIMEOUT_S` | `60` | 单次上游调用超时 |
| `AIGATE_DATABASE_URL` | `sqlite+aiosqlite:///./aigate.db` | 计量库；Postgres 用 `postgresql+asyncpg://...` |
| `AIGATE_ADMIN_TOKEN` | 空 | 打开 `/admin/*`；空 = 管理端 503 |
| `AIGATE_USAGE_FLUSH_INTERVAL_S` | `0.5` | 批量落库间隔 |
| `AIGATE_USAGE_QUEUE_SIZE` | `2000` | 队列上限，满了丢事件而不是阻塞请求 |
| `AIGATE_EVAL_DATA_DIR` | `<项目>/data` | 评测题集目录，`/admin/evals/run` 只接受这个目录下的文件 |
| `AIGATE_CACHE_ENABLED` | `1` | 缓存总开关 |
| `AIGATE_SEMANTIC_CACHE` | `0` | 语义层开关；默认关，要开必须同时注入真 embedding 模型（见缓存小节） |
| `AIGATE_SEMANTIC_THRESHOLD` | `0.92` | 语义命中阈值；换 embedding 模型必须重测 |
| `AIGATE_CACHE_TTL_S` | `3600` | 缓存过期时间，秒 |
| `AIGATE_CACHE_MAX_ENTRIES` | `2048` | 内存缓存 LRU 上限 |
| `AIGATE_SEMANTIC_MAX_PARTITION` | `256` | 每个语义分区的候选上限 |
| `AIGATE_REDIS_URL` | 空 | 留空用进程内内存缓存；多进程共享才需要 Redis |
| `AIGATE_ROUTES` | 内置 | JSON：别名 → `["provider:model", ...]`，整体覆盖默认路由表 |
| `AIGATE_PROVIDERS` | 内置 | JSON 数组，自定义 provider（`name` / `type` / `base_url` / `api_key`） |
| `AIGATE_DEFAULT_ROUTE` | `default` | 未知模型兜底时用的路由 |
| `AIGATE_UNKNOWN_MODEL` | `route` | `route`=未知模型走默认路由（兼容硬编码模型名的 agent）；`error`=严格 404 |
| `AIGATE_MOCK_DELAY_S` | `0` | mock 分块之间的延时，演示流式用 |

上游 key 按约定读取：`OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`。

## 路由

客户端 `model` 字段既可以是别名，也可以直接写 `provider:model`：

```bash
AIGATE_ROUTES='{"default":["deepseek:deepseek-chat"],"cheap":["qwen:qwen-turbo"],"safe":["deepseek:deepseek-chat","qwen:qwen-plus"]}'
```

链上第一个成功的目标会被使用；只有可重试的失败（429、5xx、超时）才切下一个，`4xx` 参数错误直接返回、不做无意义重试。响应头带 `x-aigate-route` / `x-aigate-provider` / `x-aigate-model`，排查和演示都靠它。

路由目标在启动时校验：引用了没注册的 provider 会直接启动失败，而不是等到线上第一个请求才 500。

默认 `AIGATE_UNKNOWN_MODEL=route`：客户端请求表里没有的模型名（`gpt-4o`、`claude-3-5-sonnet`……）会兜底到 `default` 路由，响应头 `x-aigate-route` 仍是客户端请求的那个名字。想严格校验就设成 `error`，未知模型返回 404 `model_not_found`。

## 接入其他 agent / 客户端

对 agent 来说它就是一个 OpenAI 兼容的服务端，改 `base_url` 就行：

| 客户端 | 怎么配 |
| --- | --- |
| DSH（DeepSeek Harness） | 设置 → 模型 → Create provider：Base URL `http://127.0.0.1:8000/v1`、协议 **OpenAI Chat Completions**、Models 加一行 `default`。完整交接步骤见 [docs/connect-dsh.md](docs/connect-dsh.md) |
| Cline / Roo Code（VS Code） | Provider 选 **OpenAI Compatible**，Base URL 填 `http://127.0.0.1:8000/v1`，API Key 填任意值（或 `sk-aigate-…`），Model ID 填 `default` |
| Continue | `config.yaml` 里 `provider: openai`、`apiBase: http://127.0.0.1:8000/v1`、`model: default` |
| Cherry Studio / NextChat | 自定义 OpenAI 服务商，接口地址填 `http://127.0.0.1:8000/v1` |
| Aider | `OPENAI_API_BASE=http://127.0.0.1:8000/v1`，`OPENAI_API_KEY=…`，`aider --model default` |
| OpenAI SDK / LangChain | `OpenAI(base_url=..., api_key=...)` / `ChatOpenAI(base_url=...)` |
| Claude Code 等 Anthropic 协议客户端 | **不行**：它们打 `/v1/messages`，当前只实现 OpenAI 协议 |

- **模型名不用对齐**：默认兜底开着，客户端写 `gpt-4o` 也能跑（实测走 `default` 路由、响应头保留 `gpt-4o`）。想按名字分流就用 `AIGATE_ROUTES` 建同名的别名。
- **每个 agent 发一个 key**：`/admin/keys` 建 key 后，租户字段就是 agent 名，`/admin/usage?tenant=<名字>` 单独看它的成本与命中率。
- **工具调用原样透传**：`tools` / `tool_choice` 等字段直送上游，能否用取决于上游模型（DeepSeek 支持 function calling，已实测 agent 风格请求返回 `tool_calls`）。
- **缓存基本不会命中**：agent 通常不传 `temperature`（按 1.0 处理）且上下文每轮都变，精确缓存和语义缓存都会主动跳过 —— 这是有意的缓存策略，不是 bug。

## 七个设计取舍（面试会聊）

1. **流式与重试的边界。** 流式请求先拿到第一个 chunk 才返回 200；拿不到就还能切下一个 provider、还能返回正常错误码。一旦已经吐出字节，状态行就发出去了，中途失败只能用 SSE 里的 `data: {"error": ...}` 事件收尾 —— 客户端必须知道这一点。
2. **不用 `BaseHTTPMiddleware`。** 它会包裹、缓冲 `StreamingResponse`，SSE 的延迟能直接看出来。这里用纯 ASGI 中间件，只改 `http.response.start` 并补 `x-request-id`。
3. **成本口径先统一。** 上游返回的 `usage` 优先；流式拿不到 usage 时用本地估算，并在 trace 里标 `usage_estimated: true`。不标的话，后面所有"降本 X%"都是假的。
4. **计量不在请求关键路径上。** 有界队列 + 后台批量落库，队列满了丢事件并计数，绝不让数据库把每个请求拖慢。代价是配额按已提交计数判断，最多滞后一个批次（0.5s）；要硬实时得把计数器前置到 Redis，那是 M4 的事 —— 这个取舍要自己先说出来。
5. **只存 Key 的摘要。** SHA-256，明文只在创建那一刻返回一次。`requests` 表里同时存了 `tenant_id` 和反范式的 `tenant` slug：删租户后成本报表仍然可读，审计和对账不会被级联删除搞丢。金额用 float 是明确取舍（亚分级、非账本系统）；真做计费要换整数微美元 + 只追加流水。
6. **红线判的是绝对百分点，不是相对百分比。** 90% → 85% 是掉 5pp（该拦），90% → 88% 是掉 2pp（可以容忍）；用相对比例的话这两者只差 2 个百分点却会被一起误判。代价是**题集太小时这个阈值没有意义**：10 道题错 1 道就是 10pp，直接误杀。所以题集至少 50 条，冒烟用的 6 条只负责验证管线通不通。
7. **缓存 key 里放什么，等于承诺什么。** key 不含 `tenant` 会把 A 公司的答案泄露给 B 公司；不含 `max_tokens`，5 token 的答案会被当成 10 token 请求的命中；不含生成参数，语义层会把「简短回答」和「详细回答」判成同一件事。反过来 key 放太细（比如 `created`、请求时间）命中率直接归零 —— 每一层都只在「答案真的一样」的维度上加字段。

## 目录

```
aigate/
├── app/
│   ├── main.py             # app factory；依赖都挂在 app.state 上
│   ├── config.py           # 环境变量 / .env → Settings（无必需配置文件）
│   ├── api/v1/             # chat / models / health
│   ├── admin/router.py     # 租户 / key / 配额 / 价格 / 成本聚合
│   ├── db/                 # base / models(ORM) / session / writer(异步落库) / seed
│   ├── protocol/openai.py  # 对外协议，当前也当内部协议用
│   ├── provider/           # base(ABC) / mock / openai_compat / registry
│   ├── router/simple.py    # 别名 → 目标链；Route 带 reason 字段
│   ├── evals/              # 题集 / grader / runner / gate / CLI
│   ├── cache/              # embedding(零依赖占位) / store(LRU·Redis) / service(两层查询与记账)
│   ├── quota.py            # 月度配额校验（429）
│   ├── pricing.py          # 价格表 + 成本计算（DB 覆盖内置价）
│   ├── middleware.py       # 纯 ASGI：request id + 访问日志
│   ├── trace.py            # 每次调用一条 trace
│   └── usage.py            # token 估算（兜底口径，不是主口径）
├── migrations/             # Alembic（初始迁移 + env.py）
├── data/                   # 评测题集（eval_smoke 6 / eval_math 9 / eval_quality 50）
├── tests/
├── Dockerfile
└── docker-compose.yml
```

## 路线图

- [x] **M0** 骨架：配置、provider 接口、错误体、结构化日志
- [x] **M1** `/v1/chat/completions` + SSE + 别名路由 + fallback + trace
- [ ] **M2** 难度路由（便宜模型先跑，不够再升级）、Anthropic 原生适配器、熔断与并发上限
- [x] **M3** 落库：租户 / API Key / 配额 / 每次请求的 tokens 与成本 + 成本聚合 API，作为看板数据源
- [x] **M4** 精确缓存（内存 LRU / Redis）+ 语义缓存（`tenant + model + 生成参数` 分区，阈值 0.92）；命中记 `saved_cost_usd`，`/admin/cache/stats` 给出命中率与省下的钱
- [x] **M5** 评测子系统：固定题集 + 裁判模型 + 回归报告；**红线：通过率下降超过 2% 的策略不允许上线**
- [ ] **M6** 压测（k6 / Locust）与 Grafana：P95、TTFT、每租户成本

M5 才是这个项目的护城河：多数人做得出网关、计量和缓存，做不出「降本同时证明质量没降」。

## 测试

```bash
.\.venv\Scripts\python.exe -m pytest -q
```

## License

MIT
