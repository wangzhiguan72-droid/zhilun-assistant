# gunicorn 配置（Linux / 容器 H5 部署）
#
# 用法：
#   gunicorn --config gunicorn.conf.py wsgi:application
#
# 为什么单开一个文件而不是全写在命令行：
#   超时、worker 数这些参数和「统计计算可能很慢」这个业务事实强相关，
#   写在文件里带上注释，比一串命令行参数更容易被后来的人理解。

import multiprocessing
import os

# 绑 0.0.0.0：容器/PaaS 必须，否则映射端口连不上
bind = "%s:%s" % (os.environ.get("HOST", "0.0.0.0"),
                  os.environ.get("PORT", "5000"))

# worker 数：默认 2×CPU，但统计计算吃内存（pandas DataFrame 常驻），
# 小内存容器上开太多会被 OOM Killer 杀掉，可用 WEB_CONCURRENCY 覆盖。
workers = int(os.environ.get("WEB_CONCURRENCY", "2"))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

# worker 类型：gthread（线程池）。不用 sync 是因为 sync 一个 worker 同时只能
# 处理一个请求，而本项目有 LLM 调用这种长耗时 IO，会堵死其他用户。
worker_class = "gthread"

# 超时：LLM 解读 + 大样本 ANOVA 都可能跑过 60s，默认 30s 会误杀。
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# 日志给平台收集（stdout/stderr），不要写文件（容器里文件会随实例消失）
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")

# 只在明确置于可信代理后才信任 X-Forwarded-*，
# 与 security_guard 的 XFF 防伪造策略保持一致。
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")

# 请求体上限：上传 Excel/PDF 用。给 32MB，够用又不至于被塞爆内存。
limit_request_field_size = 8190
