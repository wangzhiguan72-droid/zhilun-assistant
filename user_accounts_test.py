"""独立账号系统（v2.27）· 端到端测试

覆盖（每条都对应一个安全/产品承诺）：
  1. 关闭模式（默认）：行为与 v2.26 完全一致 —— 不登录也能用，桌面端零干扰
  2. 开启后：未登录访问页面 → 跳 /login；API → 401 need_login
  3. 注册是邀请制：口令错 → 401；用户名/密码不合规 → 400；成功种 cookie
  4. 重复用户名 → 400
  5. 登录后页脚显示账号名；上传正常且归属本人
  6. **file_id 归属**：A 上传的数据 B 访问 → 403
  7. **按人限流**：A 打满 LLM 配额不影响 B（各人各桶）
  8. **防爆破锁定**：连续错 5 次后，正确密码也被拒（429）
  9. cookie 篡改 → 会话失效
 10. /s/ 分享链接对账号系统豁免（设计如此）
 11. 改口令 → 旧账号会话全部失效（签名随口令派生）
 12. 退出账号 → cookie 清除

跑法：.venv/Scripts/python.exe user_accounts_test.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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


TMP = tempfile.mkdtemp(prefix="zhilun_acct_test_")
os.environ["ACCOUNTS_DIR"] = TMP

CODE = "测试口令2026账号"
CSV = "group,value\n" + "".join(
    f"{'A' if i % 2 == 0 else 'B'},{10 + (i % 5)}\n" for i in range(12))


def upload(client):
    r = client.post("/api/upload", data={
        "file": (io.BytesIO(CSV.encode()), "t.csv"),
    }, content_type="multipart/form-data")
    return r.get_json()


print("=" * 70)
print("独立账号系统 · 端到端测试")
print("=" * 70)

# ---------------------------------------------------------------------------
# 1) 关闭模式（默认）：不登录也能用 —— 桌面端/老部署零干扰
# ---------------------------------------------------------------------------
os.environ.pop("ACCOUNTS_ENABLED", None)
os.environ.pop("ACCESS_CODE", None)

from app import app
import security_guard

c0 = app.test_client()
r = c0.get("/")
check("1a 关闭模式：首页直接可进", r.status_code == 200, str(r.status_code))
up = upload(c0)
check("1b 关闭模式：未登录也能上传", up.get("ok") is True, str(up)[:120])

# ---------------------------------------------------------------------------
# 2) 开启账号系统：分层正确 —— 外层口令门禁先拦，过了门禁再看账号
# ---------------------------------------------------------------------------
os.environ["ACCOUNTS_ENABLED"] = "1"
os.environ["ACCESS_CODE"] = CODE

# 2a-0 两层都没过：先看到的是口令登录页（外层门禁优先，分层正确）
r = app.test_client().get("/")
check("2a-0 未解锁未登录 → 先见口令页（外层门禁）",
      r.status_code == 200 and "需要访问口令" in r.get_data(as_text=True),
      str(r.status_code))

# c0 过口令门禁：账号开启时，解锁成功直接被引导去 /login
r = c0.post("/unlock", data={"code": CODE})
check("2a-1 解锁成功 → 引导去 /login",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")
# 已过门禁、未登录个人账号：页面跳 /login
r = c0.get("/")
check("2a 已解锁未登录访问首页 → 跳 /login",
      r.status_code == 302 and "/login" in r.headers.get("Location", ""),
      f"{r.status_code} {r.headers.get('Location')}")
# 已过门禁、未登录：API 回 401 need_login（账号层语义，区别于门禁的 need_unlock）
r = c0.post("/api/analyze", json={"file_id": "x", "method": "independent_t"})
body = r.get_json() or {}
check("2b 已解锁未登录调 API → 401 need_login",
      r.status_code == 401 and body.get("need_login") is True,
      f"{r.status_code} {str(body)[:80]}")

# 登录/注册页本身可达（豁免）
check("2c /login 页可达", c0.get("/login").status_code == 200, "")
check("2d /register 页可达且含口令框",
      b"code" in c0.get("/register").data or
      "口令" in c0.get("/register").data.decode("utf-8"), "")

# ---------------------------------------------------------------------------
# 3) 注册：邀请制 + 规则校验
# ---------------------------------------------------------------------------
r = c0.post("/register", data={"code": "错口令", "username": "张三",
                               "password": "password123"})
check("3a 口令错 → 401", r.status_code == 401, str(r.status_code))
r = c0.post("/register", data={"code": CODE, "username": "a",
                               "password": "password123"})
check("3b 用户名过短 → 400", r.status_code == 400, str(r.status_code))
r = c0.post("/register", data={"code": CODE, "username": "张三",
                               "password": "短密码"})
check("3c 密码过短 → 400", r.status_code == 400, str(r.status_code))
r = c0.post("/register", data={"code": CODE, "username": "张三",
                               "password": "password123"})
check("3d 注册成功 → 302 并种 cookie",
      r.status_code == 302 and c0.get_cookie("zhilun_user") is not None,
      f"{r.status_code} cookie={c0.get_cookie('zhilun_user')}")
r = c0.post("/register", data={"code": CODE, "username": "张三",
                               "password": "password456"})
check("3e 重复用户名 → 400", r.status_code == 400, str(r.status_code))

# ---------------------------------------------------------------------------
# 4) 登录后可用；页脚显示账号；上传归属本人
# ---------------------------------------------------------------------------
r = c0.get("/")
check("4a 登录后进首页", r.status_code == 200, str(r.status_code))
check("4b 页脚显示当前账号", "账号：张三" in r.get_data(as_text=True), "")
up_a = upload(c0)
check("4c 登录后上传正常", up_a.get("ok") is True, str(up_a)[:120])
fid_a = up_a.get("file_id", "")
from app import _SESSION_OWNER
check("4d file_id 归属已登记", _SESSION_OWNER.get(fid_a) == "张三",
      str(_SESSION_OWNER.get(fid_a)))

# ---------------------------------------------------------------------------
# 5) file_id 归属：B（李四）拿 A 的 file_id → 403
# ---------------------------------------------------------------------------
cB = app.test_client()
cB.post("/unlock", data={"code": CODE})            # B 也过了口令门禁
cB.post("/register", data={"code": CODE, "username": "李四",
                           "password": "password456"})
r = cB.post("/api/analyze", json={"file_id": fid_a, "method": "independent_t",
                                  "group_col": "group", "value_col": "value"})
check("5 B 访问 A 的数据 → 403", r.status_code == 403,
      f"{r.status_code} {r.get_data(as_text=True)[:80]}")
up_b = upload(cB)
check("5b B 上传自己的数据正常", up_b.get("ok") is True, str(up_b)[:120])

# ---------------------------------------------------------------------------
# 6) 按人限流：A 打满 LLM 桶不影响 B
# ---------------------------------------------------------------------------
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "2"
security_guard.limiter.reset()
chat = lambda c: c.post("/api/audit_chat",
                        json={"summary": {"p": 0.03, "t": 2.2},
                              "question": "为什么？"})
codes_a = [chat(c0).status_code for _ in range(3)]
check("6a A 第 3 次 LLM 请求被限（429）", codes_a == [200, 200, 429], str(codes_a))
check("6b B 自己的配额不受影响", chat(cB).status_code == 200, "")
os.environ["RATE_LIMIT_LLM_PER_MIN"] = "8"
security_guard.limiter.reset()

# ---------------------------------------------------------------------------
# 7) 防爆破锁定：连续错 5 次 → 正确密码也 429
# ---------------------------------------------------------------------------
security_guard.login_lockout.reset()
cL = app.test_client()
cL.post("/unlock", data={"code": CODE})
for _ in range(5):
    cL.post("/login", data={"username": "张三", "password": "wrong-pass"})
r = cL.post("/login", data={"username": "张三", "password": "password123"})
check("7 连续失败 5 次后正确密码也被锁（429）", r.status_code == 429,
      str(r.status_code))
security_guard.login_lockout.reset()
r = cL.post("/login", data={"username": "张三", "password": "password123"})
check("7b 锁定清空后可正常登录", r.status_code == 302, str(r.status_code))

# ---------------------------------------------------------------------------
# 8) cookie 篡改 → 会话失效
# ---------------------------------------------------------------------------
ctamper = app.test_client()
ctamper.post("/unlock", data={"code": CODE})
ctamper.post("/login", data={"username": "张三", "password": "password123"})
val = ctamper.get_cookie("zhilun_user").value
ctamper.set_cookie("zhilun_user", val + "x", domain="localhost")
r = ctamper.get("/")
check("8 篡改 cookie → 打回登录页", r.status_code == 302
      and "/login" in r.headers.get("Location", ""), str(r.status_code))

# ---------------------------------------------------------------------------
# 9) /s/ 分享链接对账号系统豁免（给没账号的导师看，设计如此）
# ---------------------------------------------------------------------------
r = app.test_client().get("/s/does-not-exist")
check("9 /s/ 不被账号层拦截（404 而非 401/跳登录）",
      r.status_code not in (401, 302), f"{r.status_code}")

# ---------------------------------------------------------------------------
# 10) 改口令 → 旧账号会话全部失效（签名密钥随口令派生）。
# 端到端表现：旧用户连首页都进不去（先被同样失效的外层口令拦下，200 口令页）。
# 这里直接验证账号层本征行为：同一 token 在新口令下签名不再有效。
# ---------------------------------------------------------------------------
import user_accounts as ua


class _Req:
    def __init__(self, cookies):
        self.cookies = cookies


old_token = ua.make_token("张三")            # 旧口令下签发
check("10a 旧口令下签名有效", ua.current_user(_Req({"zhilun_user": old_token})) == "张三", "")
os.environ["ACCESS_CODE"] = CODE + "-换新"
check("10b 改口令后同一 token 被拒",
      ua.current_user(_Req({"zhilun_user": old_token})) is None, "")
r = c0.get("/")
check("10c 端到端：改口令后旧会话进不去首页",
      r.status_code == 200 and "需要访问口令" in r.get_data(as_text=True),
      f"{r.status_code}")
os.environ["ACCESS_CODE"] = CODE

# ---------------------------------------------------------------------------
# 11) 退出账号
# ---------------------------------------------------------------------------
r = c0.get("/logout-user")
check("11a 退出 → 重定向登录页", r.status_code == 302
      and "/login" in r.headers.get("Location", ""), str(r.status_code))
r = c0.get("/")
check("11b 退出后首页回到登录墙", r.status_code == 302, str(r.status_code))

# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------
os.environ.pop("ACCOUNTS_ENABLED", None)
os.environ.pop("ACCESS_CODE", None)
os.environ.pop("RATE_LIMIT_LLM_PER_MIN", None)
security_guard.limiter.reset()
security_guard.login_lockout.reset()
shutil.rmtree(TMP, ignore_errors=True)

print()
print(f"结果：{PASS} 通过 / {FAIL} 失败")
if FAIL:
    raise SystemExit(1)
print("✅ 独立账号系统端到端测试全部通过")
