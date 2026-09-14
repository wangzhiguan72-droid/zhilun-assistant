# H5 部署指南

面向「把智论助手放到公网，让学生用分享链接直接访问」的场景。

> **核心结论：后端零改动。** 同一套 Flask 应用，换个 WSGI 服务器 + 几个环境变量
> 就是 H5 服务。这是本项目「后端一次写、外壳 N 次包」设计的直接兑现。

---

## 一、先理解两件事

### 1. 本地直启 ≠ 可以对外服务

```bash
python app.py          # Flask 自带开发服务器，仅限本机内测
```

开发服务器**单进程单线程**：多个同学同时点「开始分析」会排队，
且没有优雅重启、没有超时管理、会打印 Werkzeug 警告。
对外服务请用 `wsgi.py`（见下）。

### 2. 必须绑 `0.0.0.0`

```bash
HOST=0.0.0.0           # 对外服务必须；绑 127.0.0.1 时映射出去的端口永远连不上
```

这是容器/PaaS 部署最常见的坑：容器内绑 `127.0.0.1`，宿主机端口映射形同虚设。

---

## 二、启动方式

### 生产入口（推荐）

```bash
# Linux / 容器 / 云服务器：gunicorn（多 worker）
gunicorn --config gunicorn.conf.py wsgi:application

# Windows 服务器：waitress（gunicorn 不支持 Windows）
python wsgi.py

# 也可以不带参数直接跑，自动按平台挑
python wsgi.py
```

`wsgi.py` 会按平台自动选择：Linux/macOS → gunicorn，Windows → waitress。

### 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `5000` | 监听端口。PaaS 通常自动注入 |
| `HOST` | `0.0.0.0`（wsgi.py） | 监听地址。**对外服务必须 `0.0.0.0`** |
| `TRUST_PROXY` | 关 | 置于 Nginx/云负载均衡后才设 `1`。否则限流器拿到的是代理 IP，会把所有用户算成同一个人；不设时 `X-Forwarded-For` 一律不采信（防伪造） |
| `DEBUG` | `0` | **生产必须保持 0**，开了会暴露堆栈 |
| `WEB_CONCURRENCY` | `2` | gunicorn worker 数。统计计算吃内存，小内存机器别调大 |
| `GUNICORN_THREADS` | `4` | 每 worker 线程数 |
| `GUNICORN_TIMEOUT` | `120` | 单请求超时。LLM 解读 + 大样本 ANOVA 可能超过默认 60s |
| `LLM_TIER` | `free` | `free` / `pro` |
| `<平台>_API_KEY` | 无 | 可选。不配也能完整运行，仅 AI 解读降级 |

> Key 一律通过**真实环境变量**注入。`wsgi.py` import 时不加载 `.env`
> （与 `app.py` 一致），避免把本机凭据打进镜像。

---

## 三、平台部署

### Render / Railway（最简单）

`Procfile` 已写好，平台会自动识别：

```
web: gunicorn --config gunicorn.conf.py wsgi:application
```

只需：
1. 连接仓库
2. 环境变量加 `HOST=0.0.0.0`、`TRUST_PROXY=1`（如果前面有平台代理）
3. Build Command：`pip install -r requirements.txt`
4. 健康检查路径填 `/health`

### Docker

```bash
docker compose up --build
```

`Dockerfile` 与 `docker-compose.yml` 已配好 `HOST=0.0.0.0`、健康检查与端口映射。

### 云服务器（Nginx 反代）

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # 统计 + LLM 可能较慢，给足读取超时
        proxy_read_timeout 180s;
        client_max_body_size 32m;   # 与 gunicorn.conf.py 的上传上限保持一致
    }
}
```

配了 Nginx 就要设 `TRUST_PROXY=1`，否则限流看到的是 Nginx 的 IP。

### 静态托管（Vercel / Netlify / 腾讯云静态托管）

⚠️ **本项目不能纯静态部署。** 统计计算在 Python 后端（pandas/numpy/scipy），
不是前端能算的。静态托管只能放前端壳子，必须同时有一个能跑 Python 的后端。
所以：

- ✅ 前端 + 同源后端（Render/Railway/云服务器）
- ❌ 只传 `templates/` + `static/` 到静态托管（接口会全 404）

如果确实想要 CDN 加速静态部分，可以让 Nginx 托管 `/static`、其余反代给 Python。

---

## 四、上线前的安全清单

| 项 | 为什么 |
| --- | --- |
| `DEBUG=0` | 开 debug 会暴露堆栈细节 |
| `TRUST_PROXY` 按实际情况设 | 不设 → 所有人算一个 IP（误伤）；乱设 → 可伪造 IP 绕过限流 |
| 限流阈值按预期并发调 | 默认值面向内测；公网可能需收紧 |
| 确认 `/health` 可达 | 平台探针靠它判活 |
| 上传体积上限 | gunicorn 与 Nginx 两处要一致，否则大文件被拒得莫名其妙 |
| 不配真实 Key 也要能跑 | 已保证：LLM 只做解释，不参与统计计算 |
| 数据合规 | 后端**全内存不落盘**，原始数据不写磁盘；副驾驶产物才显式写 workspace |

---

## 五、验证部署是否成功

```bash
# 1. 健康检查
curl -s http://<host>/health

# 2. 前端与 PWA 资源
curl -s -o /dev/null -w "%{http_code}\n" http://<host>/
curl -s -o /dev/null -w "%{http_code}\n" http://<host>/static/manifest.json

# 3. 真实跑一次统计（验收标准：结果与 CLI 逐字段一致）
curl -s -X POST http://<host>/api/upload -F "file=@examples/student_scores.csv"
# 拿到 file_id 后：
curl -s -X POST http://<host>/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"file_id":"<上一步的 file_id>","method":"independent_t","group_col":"gender","value_col":"score"}'
```

**验收硬标准**：同一个文件、同一个方法，网页端与 CLI 的统计量必须**逐字段相同**。

```bash
python cli.py analyze examples/student_scores.csv \
  -m independent_t -g gender -v score -f json
```

两者比 `summary` 的每一个字段（p / t / df / CI / 效应量…），必须完全一致。
本项目已实测通过（15 个字段 0 差异）。若不一致，说明部署的是旧代码。

---

## 六、常见问题

**Q：页面能开，但点「分析」报 500。**
多半是缺依赖（`pip install -r requirements.txt`）或内存不足被 OOM Killer 杀掉。
看平台日志；`WEB_CONCURRENCY` 调小试试。

**Q：所有人反应「操作太频繁」。**
`TRUST_PROXY` 没设，限流器把所有人都当成同一个 IP。设为 `1` 并确认代理传了
`X-Forwarded-For`。

**Q：静态资源 404，但 `/` 正常。**
反向代理只转发了 `/` 没转发 `/static`。检查 Nginx `location` 配置。

**Q：上传大文件被拒。**
两处上限都要改：`gunicorn.conf.py` 与 Nginx `client_max_body_size`。

**Q：装成 PWA 后是旧界面。**
service worker 的导航请求是 **network-first**，正常情况下刷新即最新。
若仍异常，检查 `static/sw.js` 的 `VERSION` 是否随着前端改动 bump 了 ——
不 bump 时 SW 可能继续复用旧静态资源。

> 统计接口（`/api/*`）**永远不走缓存**，这是硬约束：宁可报错，
> 也不能让用户看到陈旧数字却以为是新结果。
