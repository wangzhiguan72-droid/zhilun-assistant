# 智论助手 · 桌面端（内测版）

> 单文件 exe，双击即用，**无需安装 Python**。仅本机内测使用。

## 一、快速开始

1. 双击 `dist/智论助手.exe`
2. 控制台窗口出现后，浏览器会自动打开 `http://127.0.0.1:5000`
3. 用完关闭控制台窗口即可（服务随之停止）

**注意**：不要关闭那个黑色控制台窗口，它是后端服务本体。

## 二、它做了什么

启动器（`desktop_launcher.py`）在同一个进程里：

1. 检测 5000 端口
   - 如果已有本应用在跑 → 直接打开浏览器复用，不重复启动
   - 如果被别的程序占用 → 自动顺延到 5001、5002… 并提示实际地址
2. 拉起 Flask 后端（`app.py`）
3. 延迟 2 秒自动打开浏览器

两个 Tab 功能都在：

- **数据分析**：9 种统计方法（T 检验 / ANOVA / 相关 / 卡方 / 配对 T / Mann-Whitney / Wilcoxon / 线性回归 / Logistic 回归）
- **论文排查**：上传论文 + 数据 → 识别方法/统计量/变量 → 规则化建议 → 可导出 Word

## 三、重新打包

```bash
# 在项目根目录，用项目自带的 .venv
.venv/Scripts/python.exe build_desktop.py
```

流程：生成图标 → PyInstaller 打包 → 校验产物，产物落在 `dist/智论助手.exe`。

| 文件 | 作用 |
| --- | --- |
| `build_desktop.py` | 一键打包入口 |
| `desktop.spec` | PyInstaller 配置（资源清单 + 隐藏导入） |
| `desktop_launcher.py` | 桌面启动器（端口检测 + 拉起 Flask + 开浏览器） |
| `make_icon.py` | 生成 `assets/icon.ico` / `icon.png` |

## 四、LLM 功能说明

数据分析的「AI 增强」和论文排查的「AI 深度审计」需要 API Key（环境变量）：

```
SILICONFLOW_API_KEY / ZHIPU_API_KEY / DEEPSEEK_API_KEY
```

没有 Key 时会**静默降级**为规则化输出，其余功能不受影响。

> 桌面版目前读取系统环境变量；如需在 exe 内配置 Key，可在启动器里补一个设置界面（后续迭代）。

## 五、为什么不用 Tauri

最初计划用 Tauri（包体 5–10 MB）。但 Tauri 需要 MSVC 编译工具链，本机
Visual Studio Build Tools 安装失败，Rust 的 GNU 工具链又缺 `dlltool`，
导致构建卡住。

PyInstaller 方案包体 94 MB，但**零额外依赖、构建稳定**，对「本机内测」场景
完全够用。

## 六、已知问题

- 首次启动解压到临时目录，约需 5–10 秒才打开浏览器，属正常
- 未做代码签名，Windows SmartScreen 可能提示「未知发布者」，选择「仍要运行」
- 端口 5000 若被占用会自动顺延，请以控制台打印的地址为准
