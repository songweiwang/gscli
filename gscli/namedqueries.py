# -*- coding: utf-8 -*-
"""命名查询（named queries）。

把常用 SQL 存起来，起个名字反复调用，命令是 ``\\n`` 系列：

    \\ns 名字 SQL          保存
    \\n                    列出全部
    \\n+                   列出并显示定义
    \\n 名字 [参数...]      执行（支持 $1/$2/$* 占位符）
    \\np 模式              只打印不执行
    \\nd 名字              删除

存储位置是 gscli 的配置文件（``~/.config/gscli/config``，Windows 下在
``%LOCALAPPDATA%\\dbcli\\gscli\\config``）里的 ``[named queries]`` 段，
一行一条 ``名字 = SQL``，跨会话保留。

注意：ConfigObj 是以 ``interpolation=False`` 加载的，所以 SQL 里的 ``$1`` 不会被当成插值。
"""

import logging
import re

_logger = logging.getLogger(__name__)

SECTION_NAME = "named queries"

USAGE = """Named queries save frequently used SQL under a short name.

  \\ns <name> <query>        save a query
  \\n [name [args...]]       list all, or run a saved query
  \\n+                       list with definitions
  \\np <pattern>             print a saved query
  \\nd <name>                delete a saved query

Placeholders in the query are replaced by the extra arguments:
$1, $2 ... positional; $* joins the rest with a space, $@ with a comma.
"""


class NamedQueries(object):
    """``[named queries]`` 段的读写封装。

    只做持久化，不关心输出格式；命令实现在 ``main.py`` 的 register_special_commands 里。
    """

    section_name = SECTION_NAME
    usage = USAGE

    # 单例，和 pgcli 保持一致：命令 handler 里直接用 NamedQueries.instance
    instance = None

    def __init__(self, config):
        self.config = config
        if self.section_name not in self.config:
            self.config[self.section_name] = {}

    @classmethod
    def from_config(cls, config):
        return cls(config)

    # ---- 查询 ----

    def list(self):
        """返回所有名字（已排序，输出稳定）。"""
        return sorted(self.config.get(self.section_name, {}))

    def get(self, name):
        return self.config.get(self.section_name, {}).get(name)

    def match(self, pattern):
        """按正则匹配名字，返回 [(名字, SQL), ...]。"""
        if not pattern:
            return []
        try:
            regex = re.compile(pattern)
        except re.error:
            regex = re.compile(re.escape(pattern))
        return [(n, self.get(n)) for n in self.list() if regex.search(n)]

    # ---- 修改 ----

    def save(self, name, query):
        self.config[self.section_name][name] = query
        self.write()

    def delete(self, name):
        try:
            del self.config[self.section_name][name]
        except KeyError:
            return False
        self.write()
        return True

    def write(self):
        try:
            self.config.write()
        except Exception as e:      # 只读文件系统、权限不足等；不影响本次会话使用
            _logger.warning("命名查询写盘失败: %r", e)


def subst_args(query, args):
    """替换 SQL 里的占位符。

    ``$1`` / ``$2`` ... 按位置替换；``$*`` 用剩余参数以空格连接，``$@`` 用逗号连接。

    返回 ``(新 SQL, 未用到的参数)``。
    """
    args = list(args or [])
    if not args or "$" not in query:
        return query, args

    used = 0
    for idx, val in enumerate(args, start=1):
        placeholder = "$%d" % idx
        if placeholder in query:
            query = query.replace(placeholder, val)
            used = idx
        else:
            break

    rest = args[used:]
    if "$*" in query or "$@" in query:
        query = query.replace("$*", " ".join(rest)).replace("$@", ", ".join(rest))
        rest = []

    return query, rest
