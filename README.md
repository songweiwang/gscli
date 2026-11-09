# gscli

> **⚠️ 警告：gscli 仅适用于华为云 GaussDB 数据库，不兼容 PostgreSQL 及其他数据库。**

GaussDB 命令行客户端，支持自动补全、语法高亮和 GaussDB 特有功能。

gscli 基于 [pgcli](https://github.com/dbcli/pgcli) (v3.4.1) 深度改造，专为
[华为云 GaussDB](https://support.huaweicloud.com/productdesc-gaussdb/gaussdb-01-0001.html)
数据库设计。使用 GaussDB 官方 psycopg2 驱动，支持 SSL 安全连接。

## 功能特性

**核心功能：**

- SQL 自动补全（关键字、表名、列名、函数、Schema）
- 语法高亮（基于 Pygments）
- 多行编辑、命令历史、可配置提示符
- 多种输出格式（grid、csv、vertical 等）

**GaussDB 专属功能：**

- 兼容模式自动检测（Oracle A / MySQL B,M / PostgreSQL pg）
- 分布式部署支持（节点信息、数据分片查询）
- 工作负载管理（资源池查看）
- 性能诊断快捷命令（基于 `dbe_perf` 视图）
- 20+ 个反斜杠命令，含 6 个 GaussDB 专属命令

**连接与安全：**

- GaussDB SSL 连接（sslmode/sslcert/sslkey/sslrootcert）
- SSH 隧道支持
- 输入校验防止 SQL 注入

## 下载so
- [GaussDB_driver.zip](https://dbs-download.obs.cn-north-1.myhuaweicloud.com/GaussDB/1772024234272/GaussDB_driver.zip)

> **驱动版本要与服务端匹配，且必须和 Python 版本对应。** 驱动请从 GaussDB 官方驱动包
> 获取（`psycopg2` + `libpq`），不要用 PyPI 上的 psycopg2-binary 代替。
> 其中的 `_psycopg.so` 与 Python 版本强绑定，用 CPython 3.11 编译的包在 py3.7/3.8 上加载会报
> `ImportError: undefined symbol: PyObject_CheckBuffer`。
> 因此构建 gscli 二进制时统一用 Python 3.11；若在别的 Python 版本上从源码安装，
> 需换成该版本对应的驱动包。

## 安装

```bash
pip install gscli
```

## 快速开始

连接 GaussDB 数据库：

```bash
# 非 SSL 连接
gscli -h <host> -p <port> -U <user> -d <database>

# SSL 连接（verify-ca 模式）
gscli -h <host> -p <port> -U <user> -d <database> \
    --sslmode verify-ca \
    --sslrootcert /path/to/ca.pem

# 带客户端证书的 SSL 连接
gscli -h <host> -p <port> -U <user> -d <database> \
    --sslmode verify-ca \
    --sslcert /path/to/client-cert.pem \
    --sslkey /path/to/client-key.pem \
    --sslrootcert /path/to/ca.pem

# 使用 DSN（URI）连接
gscli 'postgresql://user:password@host:port/database'

# 使用 DSN（key=value）连接
gscli 'host=host port=port dbname=database user=user password=password'
```

### 多主机连接

gscli 底层使用 GaussDB 自带的 psycopg2 / libpq，原生支持按顺序逐个尝试多个主机：

```bash
# 方式一：多个主机、同一端口（-h 支持逗号分隔，-p 也可写主机数相等的端口列表）
gscli -h "10.0.0.1,10.0.0.2,10.0.0.3" -p 5432 -U user -d mydb
gscli -h "h1,h2,h3" -p "5432,5433,5434" -U user -d mydb

# 方式二：URI（可同时指定 target_session_attrs 与 connect_timeout）
gscli "postgresql://user:pwd@h1:5432,h2:5433,h3:5434/mydb?target_session_attrs=read-only&connect_timeout=10"

# 方式三：key=value DSN
gscli "host=h1,h2,h3 port=5432,5433,5434 dbname=mydb user=user password=pwd target_session_attrs=read-only connect_timeout=10"
```

- `--target-session-attrs` 可选值：`any`、`read-write`、`read-only`、`primary`、`standby`、`prefer-standby`（默认不限定，由 libpq 决定）。
  `read-write` 只会连到主节点，`read-only` 只会连到备节点，适合读写分离场景。
- 端口列表元素个数需与主机列表一致；只给一个端口时对所有主机生效。
- 连接成功后，`\H` / `\p` 显示的是实际命中的那个节点，不是整串地址。
- 注意：URI 的 scheme 必须是 `postgresql://` 或 `postgres://`，`gaussdb://` 不被 libpq 识别。

连接后自动识别服务端版本与兼容模式，用 `\compat` 查看兼容模式：

```
Database   | Compatibility Mode
-----------+--------------------
mydb       | PostgreSQL (pg)
```

兼容模式取值：`Oracle (A)`、`MySQL (B)`、`MySQL (M)`、`PostgreSQL (pg)`（服务端 `sql_compatibility`
为 `C` 或 `PG` 时都显示为 `pg`）。

## 反斜杠命令

**通用命令：**

| 命令 | 说明 |
|------|------|
| `\l` | 列出所有数据库 |
| `\dt [+] [模式]` | 列出表 |
| `\di [+] [模式]` | 列出索引 |
| `\dv [+] [模式]` | 列出视图 |
| `\ds [+] [模式]` | 列出序列 |
| `\df [+] [模式]` | 列出函数 |
| `\dn [+]` | 列出 Schema |
| `\du [+]` | 列出用户/角色 |
| `\d [+] [表名]` | 查看表结构 |
| `\encoding` | 显示客户端编码 |
| `\c [db [user [host port]]]` | 切换数据库连接 |
| `\x` | 切换扩展/垂直显示模式 |
| `\o [文件名]` | 将查询结果输出到文件 |
| `\conninfo` | 显示当前连接信息 |
| `\T [格式]` | 更改输出表格格式 |
| `\i 文件名` | 从文件执行 SQL |
| `\e [文件名]` | 在外部编辑器中编辑查询 |
| `\watch [秒数]` | 重复执行查询（默认间隔 2s） |
| `\q` | 退出 gscli |
| `\?` | 显示所有命令帮助 |
| `\#` | 刷新自动补全 |

**命名查询（把常用 SQL 存下来反复用）：**

| 命令 | 说明 |
|------|------|
| `\n` | 列出所有命名查询 |
| `\n+` | 列出并显示各自的 SQL |
| `\n 名字 [参数...]` | 执行一条命名查询（支持 `$1`、`$2`…、`$*`、`$@` 占位符） |
| `\n+ 名字` | 只显示这条的 SQL，不执行 |
| `\np 模式` | 按正则打印匹配的命名查询 |
| `\ns 名字 SQL` | 保存一条命名查询 |
| `\nd 名字` | 删除命名查询（可一次给多个名字） |

存在 `~/.config/gscli/config` 的 `[named queries]` 段里，跨会话保留。示例：

```bash
\ns slow_query SELECT pid, query, now()-query_start AS dur FROM pg_stat_activity WHERE state='active';
\n+                                  # 看都存了哪些
\n slow_query                        # 跑它
\ns by_oid SELECT * FROM tbl WHERE oid = $1::oid;
\n by_oid 16384                      # 占位符用参数替换
\np slow                             # 只看 SQL
\nd slow_query                       # 删掉
```

**GaussDB 专属命令：**

| 命令 | 说明 |
|------|------|
| `\version` | 显示 GaussDB 版本信息 |
| `\compat` | 显示当前数据库兼容模式 |
| `\nodes` | 显示分布式节点信息 |
| `\perf` | 显示性能统计（pg_stat_database） |
| `\workload` | 显示工作负载/资源池信息 |
| `\dist <表名>` | 显示表分布信息 |

## 配置文件

配置文件位置：

- Linux/macOS: `~/.config/gscli/config`
- Windows: `%USERPROFILE%\AppData\Local\dbcli\gscli\config`

配置文件支持自定义：

- 关键字大小写策略（`keyword_casing`）
- 表格式（`table_format`）
- 多行模式（`multi_line`）
- 滚动条、颜色主题
- DSN 别名（`[alias_dsn]` 段）
- SSL 默认参数

## SSL 连接

gscli 支持 GaussDB SSL 连接，详见
[华为云 GaussDB 开发指南](https://support.huaweicloud.com/centralized-devg-v8-gaussdb/gaussdb-42-0176.html)。

SSL 参数可通过以下方式指定：

- 命令行选项：`--sslmode`, `--sslcert`, `--sslkey`, `--sslrootcert`
- 环境变量：`GSSLMODE`, `GSSLCERT`, `GSSLKEY`, `GSSLROOTCERT`
- 配置文件：`~/.config/gscli/config`

SSL 模式说明：

- `disable` - 不使用 SSL
- `allow` - 服务器要求时尝试 SSL
- `prefer` - 优先使用 SSL（默认行为）
- `require` - 强制 SSL，不验证服务器证书
- `verify-ca` - 强制 SSL，验证服务器 CA 证书
- `verify-full` - 强制 SSL，验证服务器身份（GaussDB 暂不支持）

## 环境变量

**连接参数：**

- `GSHOST` - 数据库主机
- `GSPORT` - 数据库端口
- `GSDATABASE` - 数据库名
- `GSUSER` - 用户名
- `GSPASSWORD` - 密码

**SSL 参数：**

- `GSSLMODE` - SSL 模式
- `GSSLCERT` - 客户端证书路径
- `GSSLKEY` - 客户端密钥路径
- `GSSLROOTCERT` - 根 CA 证书路径

**其他：**

- `GSLIRC` - 配置文件路径
- `GSSERVICE` - 服务名（gs_service.conf 风格）
- `GSROWLIMIT` - 行数限制
- `EDITOR` - 外部编辑器（用于 `\e` 命令）

## 与 pgcli 的区别

- **仅支持 GaussDB**：gscli **不兼容** PostgreSQL、Redshift 等数据库
- **深度 GaussDB 适配**：移除 pgspecial 依赖，内置 GaussDB 专属反斜杠命令
- **兼容模式检测**：连接时自动识别 Oracle/MySQL/PostgreSQL 兼容模式
- **分布式支持**：查询分布式节点、数据分片信息
- **性能诊断**：内置 `\perf`、`\workload` 等快捷命令
- **GaussDB 关键字**：224 个 SQL 关键字、231 个函数、92 种数据类型
- **环境变量**：`GS` 前缀替代 `PG`
- **配置路径**：`~/.config/gscli/` 替代 `~/.config/pgcli/`

## 依赖

- click >= 4.1
- Pygments >= 2.0
- prompt_toolkit >= 2.0.6
- sqlparse >= 0.3.0
- configobj >= 5.0.6
- pendulum >= 2.1.0
- cli_helpers >= 2.2.1

## 许可证

BSD License。详见 `LICENSE.txt`。

## 致谢

gscli 基于 [pgcli](https://github.com/dbcli/pgcli) 项目（v3.4.1）深度改造，专为华为云 GaussDB 数据库设计。

作者：songweiwang@126.com
