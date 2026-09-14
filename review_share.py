"""v2.15 · ⑨ 协作审阅：核查报告 → 分享链接 + 批注。

把「论文排查 / 数据体检」产出的报告变成一个可发给导师、同门的链接，
对方打开即可阅读并留下批注。

## 设计前提（与产品定位一致，改动前请先读）

1. **本地优先**：分享内容以 JSON 落盘在项目内 `.review_share/`，
   **不依赖任何云服务、数据库或第三方账号**。换台机器把目录拷走即可。
2. **链接不可猜**：token 用 `secrets.token_urlsafe`（≥96 bit 熵），
   **绝不用自增 ID**（那等于把所有报告按顺序公开）。
3. **报告只读、批注另存**：批注只能追加/删除，**绝不改写报告正文**。
   报告是证据，批注是意见 —— 两者物理分开，避免「改报告」变成新的造假通道。
4. **沿用文案红线**：只呈现数字与口径差异，**永不判「造假」**。

## 安全边界

* token 必须匹配 `TOKEN_RE` 才会拼进文件路径 —— 挡住 `../` 与绝对路径穿越。
* 所有进入 HTML 的内容一律 `html.escape`；报告正文用 `<pre>` 呈现，
  **不做 markdown → HTML 转换**，因此批注里的 `<script>` 永远只是文字。
* 批注与标题都有长度上限，且落盘前剥掉控制字符（避免 ANSI 转义污染终端/日志）。
* `NaN` / `Infinity` 会被写成 `None` —— 否则 `json.dump` 产出非法 JSON，
  下次读取直接崩，等于一次拒绝服务。

用法（与 `app.py` 的路由解耦，可独立测试）::

    doc = review_share.create_share("张三-实验一核查", markdown, comparisons=[...])
    review_share.add_annotation(doc["token"], text="这里建议补效应量", author="李老师")
    html = review_share.render_share_html(review_share.load_share(doc["token"]))
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any

__all__ = [
    "ShareError",
    "enabled",
    "share_dir",
    "create_share",
    "load_share",
    "list_shares",
    "add_annotation",
    "delete_annotation",
    "gc_expired",
    "render_share_html",
]

#: 环境变量：分享目录。设 `off` / `0` / `false` 关闭本功能（默认开启）。
SHARE_DIR_ENV = "REVIEW_SHARE_DIR"

#: 默认目录名（项目根下，`.gitignore` 建议忽略）
DEFAULT_DIRNAME = ".review_share"

#: token 形态：`secrets.token_urlsafe(9)` ≈ 12 个字符，只含 URL 安全字符
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAX_TITLE = 120
MAX_TEXT = 2000          # 单条批注
MAX_AUTHOR = 40
MAX_ANCHOR = 120         # 批注挂在哪个条目上
MAX_ANNOTATIONS = 300
MAX_MARKDOWN = 400_000   # 报告正文上限（约 40 万字符，够长）
MAX_SUGGESTIONS = 200
MAX_COMPARISONS = 400

DEFAULT_TTL_DAYS = 30
MAX_TTL_DAYS = 365

#: 状态 → 中文 + 图标（未知状态原样显示，不猜）
_STATUS_CN = {
    "ok": ("✅", "一致"),
    "match": ("✅", "一致"),
    "minor_diff": ("🟡", "轻微差异"),
    "mismatch": ("🔴", "不一致"),
    "missing": ("⚪", "数据缺失"),
    "error": ("⚪", "未能比对"),
}


class ShareError(ValueError):
    """分享/批注失败。

    故意继承 `ValueError`：`app.py` 的异常处理会把 ValueError 转成中文提示
    返回 400，不必在每个路由里重复写一遍错误文案。
    """


# ---------------------------------------------------------------------------
# 目录与 token
# ---------------------------------------------------------------------------
def share_dir() -> Path | None:
    """分享目录。返回 `None` = 功能关闭。

    不设环境变量 → 项目根下 `.review_share/`（本地优先，零配置可用）。
    """
    raw = (os.environ.get(SHARE_DIR_ENV) or "").strip()
    if not raw:
        return Path(__file__).resolve().parent / DEFAULT_DIRNAME
    if raw.lower() in ("0", "off", "none", "false", "disable"):
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    return p


def enabled() -> bool:
    """协作审阅是否可用。"""
    return share_dir() is not None


def new_token() -> str:
    """生成一个不可猜的分享 token（≈96 bit 熵）。"""
    return secrets.token_urlsafe(9)


def _path(token: str) -> Path:
    """token → 文件路径。**先过 TOKEN_RE**，挡住目录穿越。"""
    d = share_dir()
    if d is None:
        raise ShareError("协作审阅已关闭（环境变量 REVIEW_SHARE_DIR=off）。")
    if not TOKEN_RE.match(token or ""):
        raise ShareError("分享链接无效。")
    return d / f"{token}.json"


# ---------------------------------------------------------------------------
# 清洗
# ---------------------------------------------------------------------------
def _clean(value: Any, maxlen: int) -> str:
    """转字符串 + 去控制字符 + 截断。

    保留换行与制表符（报告正文和批注都要换行）；其余 C* 类字符
    （含 `\\x00`、ANSI 转义序列）一律剔除，避免污染页面与日志。
    """
    if value is None:
        return ""
    s = str(value)
    s = "".join(ch for ch in s
                if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    s = s.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(s) > maxlen:
        s = s[:maxlen].rstrip() + "…"
    return s


def _sanitize(obj: Any, depth: int = 0) -> Any:
    """把一个任意结构收敛成 JSON 安全且有限大小的结构。

    * `NaN` / `±Inf` → `None`（否则 `json.dump` 写出非法 JSON，下次读取直接崩）
    * 列表最多 200 项、字典最多 40 键、递归深度 4
    * 所有字符串过 `_clean`
    """
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, str):
        return _clean(obj, 2000)
    if depth >= 4:
        return None
    if isinstance(obj, (list, tuple)):
        return [_sanitize(x, depth + 1) for x in list(obj)[:200]]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in list(obj.items())[:40]:
            out[_clean(k, 60)] = _sanitize(v, depth + 1)
        return out
    return _clean(obj, 200)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def _save(doc: dict) -> None:
    """原子落盘（先写 `.tmp` 再 `replace`），避免读到写了一半的文件。"""
    p = _path(doc["token"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(p)


def create_share(title: str, markdown: str = "", *,
                 comparisons: Any = None,
                 suggestions: Any = None,
                 kind: str = "audit",
                 ttl_days: float = DEFAULT_TTL_DAYS) -> dict:
    """把一份报告变成分享。返回 `{token, url, expires_at, ...}`。

    `url` 给的是相对路径（`/s/<token>`），由调用方拼 host —— 本模块
    不该猜自己的部署地址（反代 / 内网 / 本地端口各不相同）。
    """
    d = share_dir()
    if d is None:
        raise ShareError("协作审阅已关闭（环境变量 REVIEW_SHARE_DIR=off）。")
    try:
        ttl = float(ttl_days)
    except (TypeError, ValueError):
        ttl = DEFAULT_TTL_DAYS
    ttl = max(1.0, min(float(MAX_TTL_DAYS), ttl))

    now = time.time()
    doc = {
        "v": 1,
        "token": None,
        "kind": _clean(kind, 32) or "audit",
        "title": _clean(title, MAX_TITLE) or "未命名核查报告",
        "markdown": _clean(markdown, MAX_MARKDOWN),
        "comparisons": (_sanitize(comparisons) or [])[:MAX_COMPARISONS]
        if isinstance(comparisons, list) else [],
        "suggestions": [_clean(s, 800)
                        for s in (list(suggestions) if suggestions else [])
                        [:MAX_SUGGESTIONS]],
        "created_at": now,
        "expires_at": now + ttl * 86400.0,
        "annotations": [],
    }
    doc["token"] = new_token()
    _save(doc)
    return {
        "ok": True,
        "token": doc["token"],
        "url": f"/s/{doc['token']}",
        "title": doc["title"],
        "created_at": doc["created_at"],
        "expires_at": doc["expires_at"],
    }


def load_share(token: str) -> dict | None:
    """读取分享。不存在 / 已过期 / 文件损坏 / **token 形态非法** → `None`。

    这里**故意不抛异常**：token 直接来自 URL（`/s/<token>`），用户手改、
    复制截断、搜索引擎乱爬都会产生非法值。查不到就是查不到，
    抛异常会把一个普通的 404 变成 500（路由层已踩过一次）。
    需要给用户明确文案的**写操作**（追加/删除批注）会自己转成 `ShareError`。
    """
    if not TOKEN_RE.match(token or ""):
        return None
    p = _path(token)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    if float(doc.get("expires_at") or 0) < time.time():
        try:
            p.unlink()
        except OSError:
            pass
        return None
    doc.setdefault("annotations", [])
    return doc


def list_shares(limit: int = 50) -> list[dict]:
    """列出最近创建的分享（只含元信息，不含正文）。"""
    d = share_dir()
    if d is None or not d.exists():
        return []
    out = []
    for p in d.glob("*.json"):
        if not TOKEN_RE.match(p.stem):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if float(doc.get("expires_at") or 0) < time.time():
            continue
        out.append({
            "token": doc.get("token") or p.stem,
            "title": doc.get("title") or "",
            "kind": doc.get("kind") or "",
            "created_at": doc.get("created_at") or 0,
            "expires_at": doc.get("expires_at") or 0,
            "n_annotations": len(doc.get("annotations") or []),
        })
    out.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
    return out[:limit]


def add_annotation(token: str, *, text: str, author: str = "",
                   anchor: str = "") -> dict:
    """追加一条批注。**只增不改**：绝不触碰报告正文。"""
    doc = load_share(token)
    if doc is None:
        raise ShareError("分享链接不存在或已过期。")
    body = _clean(text, MAX_TEXT)
    if not body:
        raise ShareError("批注内容不能为空。")
    anns = doc.setdefault("annotations", [])
    if len(anns) >= MAX_ANNOTATIONS:
        raise ShareError(f"批注已达上限（{MAX_ANNOTATIONS} 条）。")
    ann = {
        "id": secrets.token_hex(4),
        "author": _clean(author, MAX_AUTHOR) or "匿名",
        "anchor": _clean(anchor, MAX_ANCHOR),
        "text": body,
        "at": time.time(),
    }
    anns.append(ann)
    _save(doc)
    return ann


def delete_annotation(token: str, ann_id: str) -> bool:
    """删除一条批注，返回是否真的删掉了。"""
    doc = load_share(token)
    if doc is None:
        raise ShareError("分享链接不存在或已过期。")
    before = len(doc.get("annotations") or [])
    doc["annotations"] = [a for a in (doc.get("annotations") or [])
                          if str(a.get("id")) != str(ann_id)]
    if len(doc["annotations"]) == before:
        return False
    _save(doc)
    return True


def gc_expired() -> int:
    """清理过期分享，返回删除条数。"""
    d = share_dir()
    if d is None or not d.exists():
        return 0
    n = 0
    now = time.time()
    for p in list(d.glob("*.json")):
        if not TOKEN_RE.match(p.stem):
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            expired = float(doc.get("expires_at") or 0) < now
        except (OSError, ValueError):
            expired = True
        if expired:
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n


# ---------------------------------------------------------------------------
# 渲染（全部转义，报告正文用 <pre>，不做 markdown → HTML）
# ---------------------------------------------------------------------------
def _fmt_time(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))
    except (TypeError, ValueError, OSError, OverflowError):
        return "—"


def _fmt_cell(v: Any) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, float):
        if not math.isfinite(v):
            return "—"
        # 统计值统一 4 位有效小数，避免 0.30000000000000004 这种噪音
        return f"{v:.4g}"
    return html.escape(str(v))


_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:24px;background:#f6f7f9;color:#1f2328;
 font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:920px;margin:0 auto}
.card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;
 padding:20px 22px;margin-bottom:16px}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:15px;margin:0 0 12px;color:#374151}
.meta{color:#6b7280;font-size:13px;margin-bottom:2px}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;
 background:#eef2ff;color:#4338ca;margin-left:6px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{border:1px solid #e5e7eb;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#f9fafb;font-weight:600;color:#374151}
tr:nth-child(even) td{background:#fcfcfd}
pre.md{margin:0;white-space:pre-wrap;word-break:break-word;font:13px/1.75
 ui-monospace,Consolas,"Courier New",monospace;color:#24292f}
ul.sug{margin:0;padding-left:20px}
ul.sug li{margin:4px 0}
form{display:flex;flex-direction:column;gap:8px;margin-top:12px}
input,textarea{font:inherit;padding:8px 10px;border:1px solid #d1d5db;
 border-radius:8px;width:100%}
textarea{min-height:84px;resize:vertical}
button{font:inherit;padding:8px 16px;border:0;border-radius:8px;
 background:#4f46e5;color:#fff;cursor:pointer;align-self:flex-start}
button:hover{background:#4338ca}
.ann{border-left:3px solid #c7d2fe;padding:8px 0 8px 12px;margin:10px 0}
.ann .who{font-size:13px;color:#4338ca;font-weight:600}
.ann .when{font-size:12px;color:#9ca3af;margin-left:6px;font-weight:400}
.ann .anchor{font-size:12px;color:#6b7280;margin:2px 0}
.ann .body{white-space:pre-wrap;word-break:break-word}
.foot{color:#6b7280;font-size:12px;text-align:center;padding:8px 0 24px}
.err{background:#fef2f2;border-color:#fecaca;color:#991b1b;padding:10px 14px;
 border-radius:8px;font-size:13px;margin:10px 0}
"""


def render_share_html(doc: dict, *, interactive: bool = True,
                      form_action: str = "") -> str:
    """渲染分享页。

    `interactive=False` → 导出用静态快照（无表单，适合邮件/USB 传阅）。

    **安全**：所有动态内容都过 `html.escape`；正文进 `<pre>`，
    所以即便批注里写了 `<script>` 也只会显示为文字。
    """
    e = html.escape
    title = e(_clean(doc.get("title"), MAX_TITLE) or "未命名核查报告")
    kind_raw = _clean(doc.get("kind"), 32) or "audit"
    kind_cn = {"audit": "论文排查报告", "datacheck": "数据体检报告"}.get(
        kind_raw, "核查报告")
    created = _fmt_time(doc.get("created_at"))
    expires = _fmt_time(doc.get("expires_at"))

    # —— 逐条比对 ——
    cmps = doc.get("comparisons") or []
    cmp_html = ""
    if isinstance(cmps, list) and cmps:
        rows = []
        for c in cmps[:MAX_COMPARISONS]:
            if not isinstance(c, dict):
                continue
            st = str(c.get("status") or "")
            icon, cn = _STATUS_CN.get(st, ("⚪", e(st or "—")))
            # v2.16：优先用 audit 挂好的中文名（summary.kind_cn）。
            # 直接用 kind 会把 "table_mean" 这种英文 key 显示到分享页上，
            # 而分享页是给导师看的 —— 出现一坨英文很出戏。
            sm = c.get("summary") if isinstance(c.get("summary"), dict) else {}
            stat = sm.get("kind_cn") or c.get("kind") or c.get("name")
            rows.append(
                "<tr>"
                f"<td>{icon} {e(cn)}</td>"
                f"<td>{_fmt_cell(stat)}</td>"
                f"<td>{_fmt_cell(c.get('paper'))}</td>"
                f"<td>{_fmt_cell(c.get('real'))}</td>"
                "</tr>")
        if rows:
            cmp_html = (
                '<div class="card"><h2>逐条比对</h2>'
                '<table><thead><tr><th style="width:120px">结论</th>'
                '<th style="width:160px">统计量</th><th>论文声称</th>'
                '<th>数据实算</th></tr></thead><tbody>'
                + "".join(rows) + "</tbody></table>"
                '<div class="meta" style="margin-top:10px">'
                "「不一致」只说明数字对不上，可能源于口径、舍入或版本差异，"
                "需要人工核对，<b>不代表造假</b>。</div></div>")

    # —— 建议清单 ——
    sugs = doc.get("suggestions") or []
    sug_html = ""
    if isinstance(sugs, list) and sugs:
        items = "".join("<li>" + e(_clean(s, 800)) + "</li>"
                        for s in sugs[:MAX_SUGGESTIONS])
        sug_html = f'<div class="card"><h2>改进建议</h2><ul class="sug">{items}</ul></div>'

    # —— 报告正文 ——
    md_text = _clean(doc.get("markdown"), MAX_MARKDOWN)
    md_html = ""
    if md_text:
        md_html = (f'<div class="card"><h2>{e(kind_cn)} · 全文</h2>'
                   f'<pre class="md">{e(md_text)}</pre></div>')

    # —— 批注 ——
    anns = doc.get("annotations") or []
    ann_html = ""
    if anns:
        blocks = []
        for a in anns:
            if not isinstance(a, dict):
                continue
            anchor = _clean(a.get("anchor"), MAX_ANCHOR)
            blocks.append(
                '<div class="ann">'
                f'<div class="who">{e(_clean(a.get("author"), MAX_AUTHOR) or "匿名")}'
                f'<span class="when">{_fmt_time(a.get("at"))}</span></div>'
                + (f'<div class="anchor">针对：{e(anchor)}</div>' if anchor else "")
                + f'<div class="body">{e(_clean(a.get("text"), MAX_TEXT))}</div>'
                + "</div>")
        ann_html = "".join(blocks)
    else:
        ann_html = '<div class="meta">还没有批注。</div>'

    if interactive:
        ann_block = (
            '<div class="card"><h2>批注（{n} 条）</h2>{body}'
            '<form method="post" action="{act}">'
            '<input name="author" maxlength="{ma}" placeholder="你的名字（可留空，默认匿名）">'
            '<input name="anchor" maxlength="{mn}" placeholder="针对哪一条？（可留空）">'
            '<textarea name="text" maxlength="{mt}" required '
            'placeholder="写下你的意见…"></textarea>'
            '<button type="submit">提交批注</button></form>'
            '<div class="meta" style="margin-top:10px">批注只追加、不修改报告正文；'
            '报告是证据，意见是意见。</div></div>'
        ).format(n=len(anns), body=ann_html, act=e(form_action),
                 ma=MAX_AUTHOR, mn=MAX_ANCHOR, mt=MAX_TEXT)
    else:
        ann_block = (
            f'<div class="card"><h2>批注（{len(anns)} 条）</h2>{ann_html}'
            '<div class="meta" style="margin-top:10px">这是离线快照，'
            '无法在线提交批注；请直接回复分享者。</div></div>')

    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{title} · 协作审阅</title><style>{_CSS}</style></head><body>"
        '<div class="wrap">'
        f'<div class="card"><h1>{title}</h1>'
        f'<div class="badge">{e(kind_cn)}</div>'
        f'<div class="meta" style="margin-top:8px">生成于 {created} · '
        f'有效期至 {expires}</div>'
        '<div class="meta">本报告由智论助手自动生成，只核对数字与统计口径，'
        '<b>不构成任何学术不端认定</b>。</div></div>'
        + cmp_html + sug_html + md_html + ann_block +
        '<div class="foot">智论助手 · 协作审阅 · 数据留在本地</div>'
        "</div></body></html>")


def render_error_html(message: str) -> str:
    """分享页的错误态（链接失效 / 过期 / 功能关闭）。**不回显 token 原文以外的内容**。"""
    e = html.escape
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        "<title>链接不可用 · 协作审阅</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
        '<div class="card"><h1>链接不可用</h1>'
        f'<div class="err">{e(_clean(message, 300))}</div>'
        '<div class="meta">常见原因：链接已过期、分享被清理，或链接复制不完整。'
        '请找分享者重新生成一份。</div></div>'
        "</div></body></html>")
