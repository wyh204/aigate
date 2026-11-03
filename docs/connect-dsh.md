# 把 aigate 接到 DSH（交接文档）

给别人用的时候，对方只需要知道三件事：**网关地址**、**一把 key**、**在 DSH 里加一个「自定义 provider」**。
不用改 DSH 代码，也不用装插件。

> 适用版本：DSH 0.2.x（Models 设置页自带 Create provider）。更老的版本没有这个入口，见文末「退路」。

---

## 1. 先把网关跑起来

### 方式 A：本机 Docker（推荐给「只想用」的人）

```bash
docker compose up -d
```

默认监听 `127.0.0.1:8000`，不用装 Python、不用建库（默认 SQLite 自动建表）。

### 方式 B：本机 Python

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -e .
copy .env.example .env      # 填 DEEPSEEK_API_KEY、AIGATE_ADMIN_TOKEN
.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

`.env` 里至少要有：

```ini
DEEPSEEK_API_KEY=sk-...
AIGATE_ADMIN_TOKEN=换成一串随机字符串
```

没有上游 key 也能起，但所有请求会打到 `mock`（回显模式，用来验证链路通不通）。

### 方式 C：局域网共享给同事

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
$env:AIGATE_API_KEYS="sk-share-1"   # 起服务前设，别裸奔
```

对方填的 Base URL 换成 `http://<你的局域网 IP>:8000/v1`，Windows 防火墙要放行 8000。

---

## 2. 发一把 key 给对方

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/admin/keys `
  -Headers @{"x-admin-token"="你的AIGATE_ADMIN_TOKEN"} `
  -ContentType application/json `
  -Body '{"tenant":"zhangsan","name":"dsh-laptop"}'
```

返回里的 `key` 字段就是 `sk-aigate-...`，**只返回这一次**，让对方立刻存好。
`tenant` 是这个人的名字/项目名，之后 `/admin/usage?tenant=zhangsan` 单独看他的成本。

---

## 3. 在 DSH 里加 provider

打开 DSH：**设置（Settings）→ 模型（Models）→ Create provider**，按下面填：

| 字段 | 填什么 | 说明 |
| --- | --- | --- |
| Route | `aigate` | 内部 id，随便起，不能和已有的重名 |
| Display name | `aigate` | 模型选择器里显示的名字 |
| Base URL | `http://127.0.0.1:8000/v1` | **必须带 `/v1`**，不要带 `/chat/completions` |
| API protocol | `OpenAI Chat Completions` | 选错另外两个会 404 |
| API key | `sk-aigate-...` | 网关没开鉴权时填任意值 |
| Models | 加一行，id 填 `default` | 至少一行，否则不让创建 |

点 **Create provider**，回到对话页，在输入框的模型选择里选 `aigate / default`，发一句话。

---

## 4. 验证真的接到了

三种看法，任选：

1. **网关日志**：出现一行 `{"event":"llm_call","tenant":"zhangsan","route":"default","provider":"deepseek",...}`
2. **管理端**：
   ```powershell
   Invoke-RestMethod "http://127.0.0.1:8000/admin/usage?tenant=zhangsan&group_by=model" -Headers @{"x-admin-token"="..."}
   ```
   能看到 requests / tokens / cost_usd。
3. **直接打接口**（排除 DSH 配置问题）：
   ```powershell
   curl.exe http://127.0.0.1:8000/v1/chat/completions `
     -H "Content-Type: application/json" `
     -H "Authorization: Bearer sk-aigate-..." `
     -d '{\"model\":\"default\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}'
   ```

---

## 5. 会踩的坑（按出现频率排序）

1. **Base URL 写成完整路径**：`http://127.0.0.1:8000/v1/chat/completions` 会变成 `/v1/chat/completions/chat/completions` → 404。只填到 `/v1`。
2. **协议选成 Responses / Anthropic Messages**：网关只实现了 OpenAI Chat Completions，另外两个协议 404。选错时 DSH 报的是「模型不可用」，看不出根因，先检查这一项。
3. **管理端全 503**：`AIGATE_ADMIN_TOKEN` 没设。这是故意的 fail closed，不是坏了。
4. **别人连不上**：服务绑的是 `127.0.0.1` 而不是 `0.0.0.0`，或者防火墙没放行。
5. **成本一直是 0**：请求走了 `mock`（上游 key 没配或没生效），或者这个模型在 `models` 价格表里没有单价（网关会只告警一次 `price_missing`，不瞎编价格）。
6. **缓存不命中**：DSH 这类 agent 不传 `temperature`（按 1.0 处理）、上下文每轮都变，缓存被策略主动跳过，属正常。

---

## 6. 对方要带走的东西（交接清单）

- [ ] 代码（git 仓库或压缩包，`migrations/` 和 `data/` 别漏）
- [ ] `.env.example`，以及他自己填好的 `.env`（含 `DEEPSEEK_API_KEY`、`AIGATE_ADMIN_TOKEN`）
- [ ] 一把 `sk-aigate-...` key + 上面那张 Base URL
- [ ] `docker compose up -d` 或 `uvicorn` 启动命令
- [ ] 一句话说明：`mock` 是离线回显，真模型要配上游 key；成本在 `/admin/usage` 看

---

## 退路：DSH 版本太老、没有 Create provider

两条路：

1. 用任意支持 OpenAI 兼容接口的客户端（Cline / Roo Code / Continue / Cherry Studio），配法见 README 的「接入其他 agent / 客户端」。
2. DSH 的自定义 provider 最终落在 `settings.yaml` 的 `llm-pi-ai.providers` 下（Models 页就是在写这一段）。手写形如：

   ```yaml
   llm-pi-ai:
     providers:
       aigate:
         displayName: aigate
         baseURL: http://127.0.0.1:8000/v1
         api: openai-completions
         models:
           - id: default
   ```

   字段名取自 DSH 内部 schema，**不同小版本可能变**；UI 能点就别手写。
