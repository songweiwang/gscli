1.0.0 (2026/05/15)
==================

Features:
---------

* 基于 pgcli v3.4.1 深度改造，专为华为云 GaussDB 数据库设计
* SQL 自动补全（关键字、表名、列名、函数、Schema）
* 语法高亮（基于 Pygments）
* 多行编辑、命令历史、可配置提示符
* 多种输出格式（psql、csv、vertical 等）
* 兼容模式自动检测（Oracle A / MySQL B,M / PostgreSQL C）
* 分布式部署支持（节点信息、数据分片查询）
* 工作负载管理（资源池查看）
* 性能诊断快捷命令（基于 ``dbe_perf`` 视图）
* 22 个反斜杠命令，含 7 个 GaussDB 专属命令
* GaussDB SSL 连接（sslmode/sslcert/sslkey/sslrootcert）
* SSH 隧道支持
* 输入校验防止 SQL 注入
* PyInstaller 独立二进制打包，支持 KylinV10
* GaussDB 官方 psycopg2 驱动自动加载，支持 SHA256 认证
