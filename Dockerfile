# 智论助手 Docker 镜像
# 基于 python:3.12-slim，数据不出容器

FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（matplotlib 需要）+ curl（供 healthcheck 使用）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖清单
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建非 root 用户
RUN useradd -m -u 1000 zhilun && \
    chown -R zhilun:zhilun /app
USER zhilun

# 暴露端口（仅内网）
EXPOSE 5000

# 容器内必须绑 0.0.0.0，否则宿主机映射的端口连不上；
# debug 保持关闭（镜像里没有开发调试需求）。
ENV HOST=0.0.0.0 \
    PORT=5000 \
    DEBUG=0

# 健康检查：用 Python 标准库，避免依赖外部工具（curl 已装，但 Python 更可靠）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/health', timeout=4).status==200 else 1)"

# 启动命令：走生产 WSGI 入口（gunicorn 多 worker + 线程池），
# 不用 `python app.py` —— 那是 Flask 开发服务器，单进程单线程、
# 无优雅重启、无超时管理，对外提供 H5 服务时会排队并偶发卡死。
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]
