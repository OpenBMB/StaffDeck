"""pytest 全局配置。

测试环境禁用 Redis：.env 里配置了 REDIS_HOST 时，测试会连到真实实例，
节流标记/缓存会在用例之间串扰（前面的用例写入节流 key，后面的用例断言
隐式同步行为就会跳过云端）。这里用空 REDIS_HOST 环境变量覆盖 .env，
使所有 Redis 依赖点走「不可用回退」路径——这同时验证了回退语义。
"""

from __future__ import annotations

import os

os.environ["REDIS_HOST"] = ""
