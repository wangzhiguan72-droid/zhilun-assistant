"""会话表 / 图表缓存有界化（v2.26）· 单元测试

背景（2026-09-20 真实事故）：`_session_put` 收尾一行误写成递归调用自己，
任何上传直接 RecursionError → 8 个套件连环红、线上 /api/upload 全灭。
本套件把两条内存有界化路径的契约一次锁死：

  1. 上传链路真实可用（递归事故的回归断言）
  2. `_session_put`：写入 + TS 登记
  3. TTL 到期回收
  4. 总量超 `_SESSION_MAX` 按最久未写入淘汰
  5. `_chart_cache_put`：LRU 上限不增长
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import app as app_mod
from app import app

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  [{detail}]")


print("=" * 70)
print("会话表 / 图表缓存有界化测试")
print("=" * 70)

client = app.test_client()

# ---------- 1) 上传链路真实可用（递归事故回归断言） ----------
import io

csv = "group,value\n" + "".join(
    f"{'A' if i % 2 == 0 else 'B'},{10 + (i % 5)}\n" for i in range(12))
r = client.post("/api/upload", data={
    "file": (io.BytesIO(csv.encode()), "sb.csv"),
}, content_type="multipart/form-data")
up = r.get_json()
check("上传 /api/upload 返回 ok", r.status_code == 200 and up.get("ok") is True,
      f"rc={r.status_code} {str(up)[:120]}")
fid = up.get("file_id", "")
check("上传后数据确实可取回", fid in app_mod._SESSION, f"file_id={fid!r}")

# ---------- 2) _session_put 写入 + TS 登记 ----------
app_mod._session_put("sb_t2", app_mod._SESSION[fid])
check("_session_put 写入并登记 TS",
      "sb_t2" in app_mod._SESSION and "sb_t2" in app_mod._SESSION_TS, "")

# ---------- 3) TTL 到期回收 ----------
app_mod._SESSION["sb_old"] = app_mod._SESSION[fid]
app_mod._SESSION_TS["sb_old"] = 0.0  # 石器时代的时间戳
app_mod._session_put("sb_new", app_mod._SESSION[fid])
check("TTL 到期条目被回收", "sb_old" not in app_mod._SESSION
      and "sb_old" not in app_mod._SESSION_TS, "")

# ---------- 4) 总量上限：超 _SESSION_MAX 淘汰最久未写入 ----------
app_mod._SESSION.clear()
app_mod._SESSION_TS.clear()
df0 = app_mod._SESSION.get(fid)
for i in range(app_mod._SESSION_MAX):
    app_mod._session_put(f"sb_fill_{i:02d}", df0)
check("灌满后数量 == _SESSION_MAX",
      len(app_mod._SESSION) == app_mod._SESSION_MAX,
      f"len={len(app_mod._SESSION)} max={app_mod._SESSION_MAX}")
oldest = "sb_fill_00"
app_mod._SESSION_TS[oldest] = 0.5  # 把它标成最久未写入
app_mod._session_put("sb_fresh", df0)
check("新写入进得来", "sb_fresh" in app_mod._SESSION, "")
check("超限淘汰的是最久未写入者",
      oldest not in app_mod._SESSION and len(app_mod._SESSION) <= app_mod._SESSION_MAX,
      f"len={len(app_mod._SESSION)}")

# ---------- 5) 图表缓存 LRU 有界 ----------
app_mod._CHART_CACHE.clear()
for i in range(app_mod._CHART_CACHE_MAX + 5):
    app_mod._chart_cache_put(f"k{i}", "data:image/png;base64,x")
check("图表缓存不超上限",
      len(app_mod._CHART_CACHE) <= app_mod._CHART_CACHE_MAX,
      f"len={len(app_mod._CHART_CACHE)} max={app_mod._CHART_CACHE_MAX}")
check("淘汰的是最旧键（FIFO 头部）",
      "k0" not in app_mod._CHART_CACHE and f"k{app_mod._CHART_CACHE_MAX + 4}"
      in app_mod._CHART_CACHE, "")

# 清理，避免影响同进程后续套件
app_mod._SESSION.clear()
app_mod._SESSION_TS.clear()
app_mod._CHART_CACHE.clear()

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
if FAIL:
    raise SystemExit(1)
print("✅ 会话表 / 图表缓存有界化测试全部通过")
