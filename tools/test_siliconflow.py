"""硅基流动连接性测试（v0.4）

用法：
    1. 在终端设置环境变量：
         export SILICONFLOW_API_KEY=你的Key
       或者：
         echo 'SILICONFLOW_API_KEY=你的Key' > .env && python -c "from dotenv import load_dotenv; load_dotenv()"
       （后者需要先 pip install python-dotenv）

    2. 运行：
         .venv/Scripts/python.exe test_siliconflow.py

    3. 应该看到：
         - 推荐方法（GLM-4-Flash, 免费）成功
         - 长文本解读（DeepSeek-V3）成功
         - 三次小规模调用成本可忽略（GLM 是免费的）

注意：
    - 本测试不会保存任何 Key 到文件
    - 测试本身不存储任何缓存结果
"""
import os
import sys

# 把项目根目录加进 sys.path，方便 import agents/
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from agents import Router, SiliconFlowAgent, AgentError


def _try(label: str, fn):
    print(f"\n--- {label} ---")
    try:
        out = fn()
        print(f"✓ 成功（前 300 字）：")
        print(out[:300] + ("..." if len(out) > 300 else ""))
        return True
    except AgentError as e:
        print(f"✗ AgentError: {e}")
        return False
    except Exception as e:
        print(f"✗ 未预期异常: {type(e).__name__}: {e}")
        return False


def test_recommend_with_flash():
    """GLM-4-Flash（免费）做方法推荐。"""
    router = Router()
    prompt = """你是一名统计方法顾问。用户上传了一份数据，有以下列：
- 性别（男/女）
- 成绩（连续变量）
- 学习时长（小时，连续变量）

请只回答推荐的方法（一个方法名 + 一句话理由，不要超过 50 字）。"""
    return router.complete("recommend", prompt,
                           system="你是统计方法顾问，回答简洁。")


def test_paper_check_with_deepseek():
    """DeepSeek-V3 做论文核查（长文本）。"""
    router = Router()
    prompt = """下面是一段论文节选，请用 3 句话指出统计方法层面的潜在问题：

"本研究对 30 名学生（男 15 / 女 15）进行了独立样本 T 检验，
结果显示男生成绩（M=71, SD=3.25）显著低于女生（M=87, SD=3.00），
t(28) = -14.09，P < 0.001。差异达到极其显著的水平。"
"""
    return router.complete("paper_check", prompt,
                           system="你是统计审稿人，回复专业、简洁。")


def test_write_text_with_deepseek():
    """DeepSeek-V3 写学术解读（中等长度）。"""
    router = Router()
    prompt = """基于下面的统计结果，帮我写一段 80-120 字的论文结果章节片段：

方法：独立样本 T 检验
因变量：成绩
分组：性别（男 n=15，女 n=15）
t(28) = -14.09, p < 0.001
Cohen's d = -5.15
男 M=71.00, SD=3.25; 女 M=87.07, SD=2.99

要求：使用学术语言、给出具体数字、不下空泛结论。"""
    return router.complete("write_text", prompt,
                           system="你是学术写作助手。")


def main():
    print("=" * 60)
    print("硅基流动 + Router 测试")
    print("=" * 60)
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        print("\n⚠️  环境变量 SILICONFLOW_API_KEY 未设置。")
        print("   请先在终端执行 `export SILICONFLOW_API_KEY=你的Key`")
        print("   或者创建 .env 文件（参考 .env.example）")
        print("   注意：本测试不会回显 Key 本身。\n")
    else:
        masked = key[:6] + "..." + key[-4:] if len(key) > 10 else "***"
        print(f"\nKey 已设置（脱敏：{masked}）\n")

    results = []
    results.append(("GLM-4-Flash 推荐方法", test_recommend_with_flash))
    results.append(("DeepSeek-V3 论文核查", test_paper_check_with_deepseek))
    results.append(("DeepSeek-V3 学术写作", test_write_text_with_deepseek))

    passed = sum(1 for label, fn in results if _try(label, fn))
    print("\n" + "=" * 60)
    print(f"测试结果：{passed}/{len(results)} 通过")
    if passed < len(results):
        print("\n常见原因：")
        print("1. SILICONFLOW_API_KEY 没设置或拼错")
        print("2. 余额不足（GLM-4-Flash 是免费的，其他模型要充值）")
        print("3. 模型名拼错（参考 agents/siliconflow_agent.py 的 COMMON_MODELS）")
        print("4. 网络问题（试试 curl https://api.siliconflow.cn/v1/models）")
    print("=" * 60)
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()