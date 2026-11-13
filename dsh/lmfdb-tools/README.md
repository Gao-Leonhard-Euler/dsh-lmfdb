# dsh-plugin-lmfdb

把 LMFDB 镜像接进 DeepSeek Harness 的**原生工具**插件：9 个 `lmfdb_*` 工具，
**不走 MCP**，全部用结构化参数、不需要模型写 SQL。

> 本项目基于 [AndrewVSutherland/lmfdb-mcp](https://github.com/AndrewVSutherland/lmfdb-mcp)。
> 基础的 `server.py` 与 `Dockerfile` 直接来自该项目（`server.py` 未做任何修改，
> 所以 MCP 版与 DSH 版行为一致）；本仓库新增的只是 DSH 原生工具层
> ——`agent_tools.py` 与 `dsh/lmfdb-tools/`。许可证沿用上游 GPL-3.0。

## 为什么是原生插件

原来的 `@local/lmfdb-mcp` 通过 MCP 连 `https://mcp.lmfdb.org/mcp`，那个端点是
Google 托管的，本机冷启动首次 TCP 连接经常超过 MCP 客户端写死的 10s 超时。
现在改成：

```
DSH host (node)  ──stdio JSONL──▶  python agent_tools.py --serve
                                          └─ import server.py（原样复用）
                                                └─ psycopg2 ──▶ devmirror.lmfdb.xyz:5432
```

* 没有 MCP 协议层、没有 HTTPS 到 Google，延迟从「秒级冷启动 + 可能超时」降到
  单次调用 ~0.3–1s（worker 常驻，连接池复用）。
* `server.py` **一行未改**：SQL 白名单、120s statement timeout、行数/字节上限、
  策展元数据（`lmfdb_tables`）、knowl 文档全部复用，MCP 版与 DSH 版不会漂移。
* 只有 `lmfdb_run_sql` 的 `sql` 参数需要模型写 SQL；`count_rows` / `table_stats`
  的 `where` 传 JSON，插件翻译成安全谓词。

## 目录结构

```
D:\MathProver\lmfdb-mcp\
├── server.py               # 原 MCP 服务器
├── agent_tools.py          # 本插件的 Python 层：9 个工具 + JSONL 协议
└── dsh\lmfdb-tools\        # DSH bundle
    ├── index.js            # 插件：注册工具、管理常驻 worker
    ├── cordis.patch.yml    # bundle patch（含本机路径配置）
    └── package.json
```

## 安装

```powershell
# 依赖：Python 3.14 + psycopg2（已装 2.9.13）
C:\Python314\python.exe -m pip install psycopg2-binary
```

```
plugin_manager install_bundle  target=D:\MathProver\lmfdb-mcp\dsh\lmfdb-tools
```

安装后 profile 的 `cordis.patch.yml` 会出现 `lmfdb-tools` 条目，`dsh.profile.bundles`
会加上 `dsh-plugin-lmfdb`。

## 9 个工具

| 工具 | 需要 SQL | 说明 |
|---|---|---|
| `lmfdb_overview` | 否 | 按数学分支列出生产表、hub 表、粒度/连接提示 |
| `lmfdb_list_tables` | 否 | 按前缀列表 + 行数估计 + 策展说明 |
| `lmfdb_search_knowls` | 否 | 全文检索 LMFDB 文档，找「哪个列存的是 X」 |
| `lmfdb_describe_table` | 否 | 列名/类型/可空/knowl 说明/数组典型长度 |
| `lmfdb_sample_rows` | 否 | 随机样本，看真实取值格式 |
| `lmfdb_count_rows` | 否 | 结构化 `where` 计数 |
| `lmfdb_table_stats` | 否 | count/distinct/min/max/avg/stddev（结构化 `where`） |
| `lmfdb_find` | 否 | `table` + `where` + `columns` + `limit` → 直接返回 rows |
| `lmfdb_export_query` | 否* | `table` + `where` + `columns` → 本地 CSV/JSONL 文件 |

\* `lmfdb_export_query` 也接受可选 `sql` 参数（原 MCP 行为，用于 JOIN / GROUP BY），
但正常用法不需要。

> `lmfdb_run_sql` 已下线：代码仍在（`server.py` 里的实现 + `agent_tools.py` 中被注释掉的
> manifest 条目），但不再注册给 agent。需要数据集的话用 `lmfdb_find`，需要落盘用
> `lmfdb_export_query`，确需自定义 SQL 时传给后者的 `sql` 参数。

## `where` 过滤器语法

`lmfdb_find`、`lmfdb_count_rows`、`lmfdb_table_stats`、`lmfdb_export_query` 的 `where` 接受 JSON：

```json
{"rank": 2}                                      // = 2
{"conductor": {"min": 16, "max": 128}}           // BETWEEN
{"st_group": {"in": ["USp(4)", "SU(2)xSU(2)"]}}  // IN
{"label": {"like": "%.a1"}}                      // LIKE
{"sha": null}                                    // IS NULL
{"torsion_primes": {"includes": 2}}              // 2 = ANY(col)  —— 数组列
{"rank": {"gt": 1}, "cm": true}                  // 多列 AND
```

支持的算子：`eq ne gt gte lt lte min max in like ilike isnull notnull includes not`。
列名会校验为合法标识符，字符串值会转义，然后拼成安全的 AND 谓词。
**任何以 `{` 开头的字符串都按 JSON 解析**；其它字符串按原始 SQL 谓词透传（向后兼容）。

## 导出

`lmfdb_export_query` 把结果写到本地文件（默认 `D:\MathProver\lmfdb-exports`，
可用 `exportDir` 配置或 `out_dir` 参数覆盖），返回：

```json
{"path": "D:\\MathProver\\lmfdb-exports\\lmfdb-20261113-100942.csv",
 "row_count": 3, "columns": [...], "bytes": 351, "truncated": false}
```

然后用 pwsh / pandas 读文件，不要让大表结果穿过对话上下文。

## 配置（`cordis.patch.yml`）

```yaml
- insert:
    - id: lmfdb-tools
      name: dsh-plugin-lmfdb
      inject: [ systemPrompt, tools ]
      config:
        python: 'C:\Python314\python.exe'   # 必须是带 psycopg2 的解释器
        root: 'D:\MathProver\lmfdb-mcp'     # 含 agent_tools.py / server.py
        exportDir: 'D:\MathProver\lmfdb-exports'
        timeoutMs: 300000                   # 单次调用上限，超时会重启 worker
```

数据库连接默认 `devmirror.lmfdb.xyz:5432 / lmfdb / lmfdb`（公开只读镜像），
由 `server.py` 的环境变量覆盖（`LMFDB_HOST` 等）。

## 排查

* **`ImportError: DLL load failed while importing _psycopg`**
  pip 刚写完 wheel 时 Windows 杀软还在扫描 DLL，隔几秒重试即可；确认
  `site-packages\psycopg2_binary.libs\libpq-*.dll` 存在。
* **工具返回 `lmfdb worker exited`**
  worker 崩了会自动重启；错误里带 Python 的 stderr 尾部（例如 import 失败）。
* **`column "x" does not exist`**
  列名和数学名词不一致（`ec_curvedata` 用 `lmfdb_label`），先
  `lmfdb_describe_table`。
