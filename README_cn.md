# dsh-lmfdb —— DeepSeek Harness 的 LMFDB 原生工具

在 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 里以 9 个
原生 `lmfdb_*` 工具只读访问 [LMFDB](https://www.lmfdb.org)（L-函数与模形式数据库）。
Agent 用**结构化过滤器**查询镜像，**没有任何暴露的工具需要它写 SQL**。

本项目是 [AndrewVSutherland/lmfdb-mcp](https://github.com/AndrewVSutherland/lmfdb-mcp)
的 DSH 插件版本：MCP 服务器（`server.py`）与 Cloud Run 部署（`Dockerfile`）来自上游，且
`server.py` **未做任何修改**，所以本项目不会与上游行为漂移；本项目新增的是下面这套
Harness 原生工具层。

## 本项目新增了什么

| 路径 | 作用 |
|:--|:--|
| `agent_tools.py` | 9 个工具 + JSONL worker 协议。直接 import `server.py`，SQL 白名单、语句超时、行数上限、策展元数据全部复用，不重复实现。 |
| `dsh/lmfdb-tools/` | DeepSeek Harness bundle：注册工具、管理常驻 worker 进程。 |

设计要点：

- **托管 MCP 优先，镜像兜底。** 每次调用先走 `https://mcp.lmfdb.org/mcp`；该端点
  超时、拒绝连接或返回 MCP 级错误时，同一次调用自动改用只读 PostgreSQL 镜像
  （经由 `server.py`）。设 `primary: postgres` 可完全跳过远端。
- **不挡在 SQL 前面。** 8 个工具用普通参数，第 9 个（`lmfdb_find`）用 JSON 过滤器。
  `lmfdb_run_sql` 已下线：manifest 条目在 `agent_tools.py` 里被注释掉，实现仍留在
  `server.py`。
- **单一事实来源。** 所有安全相关逻辑都在未修改的 `server.py` 里。

## 9 个工具

| 工具 | 作用 |
|:--|:--|
| `lmfdb_overview` | 按数学分支列出生产表，给出每个分支的 hub 表与粒度/连接提示。 |
| `lmfdb_list_tables` | 列出表（行数估计、策展说明），可按前缀筛（`ec`、`nf`、`g2c`、`mf`、`gps` …）。 |
| `lmfdb_search_knowls` | 全文检索 LMFDB 文档：知道数学概念但不知道哪个列时用。 |
| `lmfdb_describe_table` | 列名/类型/可空/knowl 说明/数组典型长度。 |
| `lmfdb_sample_rows` | 若干随机行，看真实取值格式。 |
| `lmfdb_find` | 单表取行：`table` + `where` + `columns` + `limit`，免 SQL 的查询方式。 |
| `lmfdb_count_rows` | 同样结构化 `where` 的 `COUNT(*)`。 |
| `lmfdb_table_stats` | 单列的 count / distinct / min / max / avg / stddev，可带过滤。 |
| `lmfdb_export_query` | 批量导出到本地 CSV/JSONL；可选 `sql` 参数用于 JOIN 与聚合。 |

全部只读。

## 结构化过滤器（`where`）

`lmfdb_find`、`lmfdb_count_rows`、`lmfdb_table_stats`、`lmfdb_export_query`
共用同一个 JSON 过滤对象：

```json
{"rank": 2}                                      // = 2
{"conductor": {"min": 16, "max": 128}}           // BETWEEN
{"st_group": {"in": ["USp(4)", "SU(2)xSU(2)"]}}  // IN
{"label": {"like": "%.a1"}}                      // LIKE
{"sha": null}                                    // IS NULL
{"torsion_primes": {"includes": 2}}              // 2 = ANY(col)，数组列
{"rank": {"gt": 1}, "cm": true}                  // 多个键之间是 AND
```

算子：`eq ne gt gte lt lte min max in like ilike isnull notnull includes not`。
列名按标识符校验、字符串值转义后才拼成谓词。**不以 `{` 开头**的字符串会作为原始
SQL 谓词透传，保留原 MCP 行为。

## 批量导出

`lmfdb_export_query` 把结果写到磁盘而不是塞进对话：

```json
{"table": "g2c_curves",
 "where": "{\"st_group\": \"USp(4)\"}",
 "columns": "label,cond,mw_rank",
 "format": "csv",
 "max_rows": 50000}
```

```json
{"path": "D:\\MathProver\\lmfdb-exports\\20261113-100942.csv",
 "format": "csv", "row_count": 50000, "columns": [...], "bytes": 1234567,
 "truncated": false}
```

拿到路径后用 pandas / `head` 读文件，不要让大结果穿过模型上下文。

## 架构

```
DSH host (node)  ──stdio JSONL──▶  python agent_tools.py --serve
                                        ├─ 主：MCP over HTTPS ──▶ mcp.lmfdb.org/mcp
                                        └─ 备：import server.py（未改）
                                                  ├─ SELECT-only 白名单、120s 超时、行数上限
                                                  └─ psycopg2 ──▶ devmirror.lmfdb.xyz:5432
```

`lmfdb_find` / `lmfdb_export_query` 在本地拼好 SELECT，再通过主端点的 `run_sql`
工具执行，所以两条后端回答的是同一个问题；其余 7 个工具与托管工具 1:1 对应。
发生兜底时 stderr 会打一行 `primary MCP unavailable (...)`。

worker 首次调用时拉起，崩溃或超时后自动重启：数据库挂掉只会让单次调用报错，
不会让工具永久失效。`agent_tools.py` 也能单独运行：

```
python agent_tools.py --dump-tools            # 输出工具清单 JSON
python agent_tools.py --call lmfdb_find '{"table":"nf_fields","where":"{\"conductor\":64}"}'
python agent_tools.py --serve                 # JSONL 请求/响应循环
```

后端选择在 worker 启动时从环境变量读取：`LMFDB_PRIMARY`（`mcp` | `postgres`）、
`LMFDB_MCP_URL`、`LMFDB_MCP_TIMEOUT_MS`。

## 安装

前置：装好 `psycopg2` 的 Python（worker 用它连 PostgreSQL），以及一个 DeepSeek
Harness profile。

直接从 GitHub 安装——仓库根就是 bundle，pnpm 的 git 依赖可直接解析：

```powershell
pip install psycopg2-binary
plugin_manager install_bundle  target=github:Gao-Leonhard-Euler/dsh-lmfdb
```

从本地 checkout 安装（改完即生效，不需要发布）：

```powershell
pnpm pack                                        # 生成 dsh-lmfdb-tools-1.0.0.tgz
plugin_manager install_bundle  target=file:D:/MathProver/lmfdb-mcp/dsh-lmfdb-tools-1.0.0.tgz
```

两种方式都会在 profile 的 `cordis.patch.yml` 增加 `lmfdb-tools` 条目，
`dsh.profile.bundles` 增加 `dsh-lmfdb-tools`。调用 `lmfdb_overview` 或
`lmfdb_find` 即可验证。

> 如果此前用别的包名、以目录形式装过，**先重启 DeepSeek Harness 再装**：运行中的
> 进程会缓存「包名 → 入口」的解析结果，改名后的第一次 import 仍可能指向旧路径。

## 配置

仓库根的 `cordis.patch.yml`（括号内为默认值）：

| 键 | 含义 |
|:--|:--|
| `python`（`python`） | 带 `psycopg2` 的解释器。建议用绝对路径：GUI 进程的 `PATH` 不一定有 Python。 |
| `root`（包根） | 含 `agent_tools.py`、`server.py` 的目录。 |
| `exportDir`（`<home>/lmfdb-exports`） | `lmfdb_export_query` 的默认落盘目录；GitHub 安装落在 `node_modules` 里，所以默认写到用户主目录。 |
| `timeoutMs`（300000） | 单次调用上限；超时会杀掉并重启 worker。 |
| `primary`（`mcp`） | `mcp`：先走托管端点，失败自动回落镜像；`postgres`：只用镜像。 |
| `mcpUrl`（`https://mcp.lmfdb.org/mcp`） | 主端点地址。 |
| `mcpTimeoutMs`（15000） | 单次主端点尝试的建连+调用预算，超时即触发回落。 |

数据库默认连公开只读镜像 `devmirror.lmfdb.xyz:5432` / `lmfdb` / `lmfdb`，
可用 `server.py` 读取的同一批环境变量覆盖：`LMFDB_HOST`、`LMFDB_PORT`、
`LMFDB_DBNAME`、`LMFDB_USER`、`LMFDB_PASSWORD`、`LMFDB_MAX_ROWS`、
`LMFDB_DEFAULT_LIMIT`。

## 安全

- 只有 `SELECT` / `WITH` / `EXPLAIN` 能到数据库；这道闸门属于 `server.py`，
  对插件构造的每条查询都生效。
- 120 秒语句超时、行数上限与近似字节预算。
- 表名/列名按标识符语法校验，过滤值转义，结构化过滤器无法夹带 SQL。
- 镜像本身只读，写操作在数据库层无论如何都会失败。
- 没有暴露的工具需要 SQL；原始 SQL 只能通过 `lmfdb_export_query` 的可选
  `sql` 参数触达。

## 上游 MCP 服务器

`server.py` 仍然可以作为上游的 MCP 服务器使用：通过 Streamable HTTP 暴露同样这
9 个工具（`python server.py`，`PORT`，默认 8000），`Dockerfile` 用于构建
Google Cloud Run 镜像。上面的 Harness 插件不走这条路——它直接 import 模块——
但两边共用同一份工具实现，行为保持一致。Claude/ChatGPT 连接器、Cloud Run
部署、沙箱 allowlist 等上游部署说明见上游项目。

## 目录结构

```
lmfdb-mcp/                 # 仓库根 == 可安装的 bundle
├── package.json           # npm 元数据 + dsh.bundle.patch（GitHub 安装读这个）
├── cordis.patch.yml       # GitHub 安装时使用的 bundle patch
├── index.js               # 工具注册 + worker 管理（入口）
├── agent_tools.py         # 本 fork：9 个工具 + JSONL worker 协议
├── server.py              # 上游 MCP 服务器，未修改
├── Dockerfile             # 上游 Cloud Run 镜像
├── requirements.txt       # 上游 Python 依赖（mcp、psycopg2-binary、uvicorn）
└── dsh/lmfdb-tools/       # 本地 checkout 用的 bundle（转发到根入口）
    ├── index.js           # `export { name, inject, apply } from '../../index.js'`
    ├── cordis.patch.yml   # bundle patch（含本机路径）
    ├── package.json
    └── README.md
```

## 排查

| 现象 | 处理 |
|:--|:--|
| `ImportError: DLL load failed while importing _psycopg` | pip 刚写完 wheel、Windows 还在扫描 DLL，等几秒重试；确认 `site-packages\psycopg2_binary.libs\libpq-*.dll` 存在。 |
| `lmfdb worker exited` | worker 会自动重启，错误信息带 Python stderr 尾部；通常是 `python` 路径不对或缺 `psycopg2`。 |
| `column "x" does not exist` | 列名与数学名词不一致（`ec_curvedata` 用 `lmfdb_label`），先跑 `lmfdb_describe_table`。 |
| 改了 profile 后工具消失 | 重新应用 bundle：`plugin_manager set_plugin include:lmfdb-tools enabled=false`，再 `enabled=true`。 |

## 许可证

GPL-3.0（与 lmfdb-lite 一致），继承自
[AndrewVSutherland/lmfdb-mcp](https://github.com/AndrewVSutherland/lmfdb-mcp)。
