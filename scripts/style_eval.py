# -*- coding: utf-8 -*-
"""三臂风格评测：重复试验 + 磁盘响应缓存 + 方差与置信区间。

为什么另起一个脚本（而不是改 verify_arms.py）：
    旧 runner 单次运行、无缓存、score() 跳过 llm is None，导致不同臂分母不同
    也能算出「严格 −21.4」这种无意义数字。本脚本把这三件事一次性堵死：

    1. **分母硬断言**：任一臂任一条拿不到合法判词，该次重复整体标记 incomplete
       并排除出统计，绝不静默缩小分母。
    2. **重复试验**：--repeats N，每次独立真调，给出均值/标准差/极差与 Wilson 区间。
       28 条语料上 1 条 = 3.6 个百分点，单次结果落在噪声里，必须靠重复才能判读。
    3. **响应缓存落盘**：key 含 trial 序号，所以「重跑同一次重复」命中缓存零成本，
       而「跑新的重复」一定真调——缓存不会把方差抹平。只存模型响应，不存原文。

缓存与隐私：
    缓存目录 _style_eval_cache/ 已被 .gitignore 的 `_*/` 排除。key 是 sha256，
    盘上只有响应文本与用量计数，不写 prompt、不写摘录原文。

成本：
    generic/vertical 每条都调模型；guarded 只有本地门不豁免的条目才调（实测约 35%）。
    提高缓存命中的手段是「同一次重复可无损续跑」，而不是跨重复复用响应。
    供应商侧前缀缓存折扣以 usage 里的 cached_tokens 实测为准，不假设。

用法：
    .venv/Scripts/python.exe scripts/style_eval.py --repeats 5 --workers 8
    .venv/Scripts/python.exe scripts/style_eval.py --corpus synthetic --repeats 3
    .venv/Scripts/python.exe scripts/style_eval.py --aggregate-only
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import env_loader  # noqa: E402
env_loader.load_dotenv()

import style_triage as st  # noqa: E402

OSS = BASE / '开源赛道'
PASSAGES = OSS / '06-佐证材料/原始证据/_复核抽样_风格段落.json'
LEDGER = OSS / '06-佐证材料/匿名汇总/复核台账-2026-10-21.csv'
R6 = OSS / '_工作/llm_vertical_matrix_r6.py'

HERE = BASE / '_style_eval'
CACHE = BASE / '_style_eval_cache'
HERE.mkdir(exist_ok=True)
CACHE.mkdir(exist_ok=True)


def use_corpus_dir(corpus: str) -> Path:
    """结果目录按语料分开。

    文件名是 <model>__<arm>__r<trial>.json，不含语料名——共用一个目录会让
    synthetic 直接覆盖 real28 的结果。缓存目录可以共用：key 含 prompt，
    不同语料天然不同 key，共用反而提高命中。
    """
    global HERE
    HERE = BASE / ('_style_eval' if corpus == 'real28' else f'_style_eval_{corpus}')
    HERE.mkdir(exist_ok=True)
    return HERE

GW = os.environ.get('ZHILUN_EVAL_GATEWAY', 'http://127.0.0.1:3425/v1/chat/completions')
GW_KEY = os.environ.get('ZHILUN_EVAL_GATEWAY_KEY', 'magpie')
TEMPERATURE = 0.2
MAX_TOKENS = 2000
VALID = ('真', '假', '存疑')
#: 判定解析器版本。改了解析口径**或本地门判定逻辑**就 bump，旧结果自动重算
#: （响应走缓存，不重复烧钱）。v6：门 v3 的引语未闭合守卫。
PARSER_VERSION = 'v6-quote-boundary'

# 网关模型：与 verify_arms.MODELS 同一批 ID，便于与既有 raw 对照
GATEWAY_MODELS = [
    ('GLM-5.3-Flash', 'workbuddy-ai/glm-5.3-flash'),
    ('Gemini-3.1-Flash-Lite', 'antigravity/gemini-3.1-flash-lite'),
    ('Gemini-3.5-Flash-Lite', 'antigravity-tools/gemini-3.5-flash-lite'),
    ('MiniMax-M3.1-Flash', 'workbuddy-ai/minimax-m3.1-flash'),
    ('GPT-OSS-120B', 'auto/gpt-oss-120b-medium'),
    ('DeepSeek-V4.1-Flash', 'workbuddy/deepseek-v4.1-flash'),
    ('Kimi-K3.1', 'workbuddy/kimi-k3-1'),
    ('Step-5', 'workbuddy/step-5-preview'),
    ('MiMo-V2.6-Flash(网关)', 'opencode2api/opencode/mimo-v2.6-flash-free'),
    # 2026-11 实测存活的新路由。workbuddy/ 前缀全线「额度已用尽」，
    # workbuddy-ai/ 是**另一条通道**，不受同一额度包约束。
    ('DeepSeek-V4.1-Flash-SG', 'workbuddy-ai/deepseek-v4.1-flash-sg'),
    ('Kimi-K3(网关)', 'workbuddy-ai/kimi-k3'),
]
# 官方平台：原矩阵最差案例 MiMo-V2.6-Pro 在网关上不存在，只能走官方
OFFICIAL_MODELS = [
    ('MiMo-V2.6-Pro(官方)', 'mimo-v2.6-pro'),
    ('MiMo-V2.6-Flash(官方)', 'mimo-v2.6-flash'),
]

#: Kimi 官方平台实测约束（2026-11，凭真实 Key 打通）：
#:   - temperature 只接受 1；传 0.2 直接 400 "only 1 is allowed for this model"
#:   - **组织级** RPM=3，跨模型共享（429 "organization max RPM: 3"），
#:     所以限流必须是全局的，按模型各算各的会叠加超限
#:   - 思考型模型：reasoning_tokens 计入 completion，2000 会被思考吃光
#: 温度与其余模型的 0.2 不同 → **不可直接混池比较**，汇总里逐模型声明。
#: 平台另有 kimi-k2.7-code / kimi-k2.7-code-highspeed / kimi-k3 三个现役 ID
#: （均已探活），但 RPM=3 下每加一个模型约多 1.4 小时，默认矩阵只跑两个代表档。
KIMI_MODELS = [
    ('Kimi-K3.1-Pro(官方)', 'kimi-k3.1-pro'),
    ('Kimi-K2.6(官方)', 'kimi-k2.6'),
]
KIMI_TEMPERATURE = 1.0
KIMI_MAX_TOKENS = 4096
KIMI_RPM = 3
#: 组织级**并发**上限，与 RPM 是两道独立的闸。实测 429 正文：
#: "request reached max organization concurrency: 1, please try again after 1 seconds"。
#: 只控 RPM 不控并发时，3 个 worker 各自合规发起、却在途重叠，照样被打回；
#: real28 的 K2.6 vertical r1 第 25 条就是这样耗尽 9 次重试后整单元作废。
KIMI_CONCURRENCY = 1

_KIMI_LOCK = threading.Lock()
_KIMI_STAMPS: list[float] = []
_KIMI_INFLIGHT = threading.BoundedSemaphore(KIMI_CONCURRENCY)


def call_params(backend):
    """该后端实际生效的 (temperature, max_tokens)。缓存 key 与汇总都用它，
    否则 Kimi 跑的是 1.0 却被记成 0.2，等于把不可比的数字伪装成可比。"""
    if backend == 'kimi':
        return KIMI_TEMPERATURE, KIMI_MAX_TOKENS
    return TEMPERATURE, MAX_TOKENS


def kimi_acquire():
    """滚动 60s 窗口内最多发起 KIMI_RPM 次；满了就等到最旧那次滑出窗口。"""
    while True:
        with _KIMI_LOCK:
            now = time.monotonic()
            while _KIMI_STAMPS and now - _KIMI_STAMPS[0] >= 60.0:
                _KIMI_STAMPS.pop(0)
            if len(_KIMI_STAMPS) < KIMI_RPM:
                _KIMI_STAMPS.append(now)
                return
            wait = 60.0 - (now - _KIMI_STAMPS[0]) + 0.5
        time.sleep(max(0.5, wait))

_lock = threading.Lock()
_cache_stats = Counter()
_usage = Counter()


def log(msg):
    with _lock:
        print(msg, flush=True)


def slug(s):
    return re.sub(r'[^A-Za-z0-9._-]+', '__', s.replace('/', '__'))


# ---------------------------------------------------------------- 旧两臂提示词
def load_old_prompts():
    """从第六轮原文件导入，避免抄写漂移。缺文件就明说，不偷偷换提示词。"""
    if not R6.exists():
        raise SystemExit(f'[FAIL] 找不到旧提示词原文件 {R6} —— 无法保证两臂逐字一致')
    spec = importlib.util.spec_from_file_location('r6', R6)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SYSTEM_GENERIC, mod.SYSTEM_VERTICAL, mod.TEMPLATE


SYSTEM_GENERIC, SYSTEM_VERTICAL, TEMPLATE = load_old_prompts()


# ---------------------------------------------------------------- 响应缓存
def cache_key(backend, model, system, prompt, trial, temp=None, max_tokens=None):
    payload = json.dumps({'b': backend, 'm': model, 's': system, 'p': prompt,
                          't': TEMPERATURE if temp is None else temp,
                          'mt': MAX_TOKENS if max_tokens is None else max_tokens,
                          'trial': trial},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def cache_get(key):
    p = CACHE / (key + '.json')
    if not p.exists():
        with _lock:
            _cache_stats['miss'] += 1
        return None
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        with _lock:
            _cache_stats['miss'] += 1
        return None
    with _lock:
        _cache_stats['hit'] += 1
        u = d.get('usage') or {}
        _usage['cached_tokens'] += int(u.get('cached_tokens') or 0)
        _usage['prompt_tokens'] += int(u.get('prompt_tokens') or 0)
    return d.get('response')


def cache_put(key, response, usage=None):
    p = CACHE / (key + '.json')
    tmp = p.with_suffix('.tmp')
    tmp.write_text(json.dumps({'response': response, 'usage': usage or {},
                               'ts': time.time()}, ensure_ascii=False), encoding='utf-8')
    tmp.replace(p)
    with _lock:
        _cache_stats['write'] += 1
        u = usage or {}
        _usage['cached_tokens'] += int(u.get('cached_tokens') or 0)
        _usage['prompt_tokens'] += int(u.get('prompt_tokens') or 0)


def _extract_usage(data):
    """供应商前缀缓存折扣只在 usage 里可见；取不到就记 0，不假设。"""
    u = data.get('usage') or {}
    out = {'prompt_tokens': u.get('prompt_tokens'),
           'completion_tokens': u.get('completion_tokens')}
    det = u.get('prompt_tokens_details') or {}
    out['cached_tokens'] = det.get('cached_tokens', u.get('cached_tokens'))
    return {k: v for k, v in out.items() if v is not None}


def raw_call(backend, model, system, prompt, trial, timeout=180, use_cache=True):
    """带缓存的单次调用。缓存命中零成本；未命中真调并落盘。

    use_cache=False 用于探活：探针 prompt 恒定，若走缓存则首次成功后
    永远 0.0s 命中，额度耗尽/模型下线全都探不出来——探活就成了假绿。
    """
    temp, mt = call_params(backend)
    key = cache_key(backend, model, system, prompt, trial, temp, mt)
    if use_cache:
        hit = cache_get(key)
        if hit is not None:
            return hit
    if backend == 'gateway':
        body = json.dumps({'model': model,
                           'messages': [{'role': 'system', 'content': system},
                                        {'role': 'user', 'content': prompt}],
                           'temperature': temp, 'max_tokens': mt},
                          ).encode('utf-8')
        req = urllib.request.Request(GW, data=body, headers={
            'Authorization': f'Bearer {GW_KEY}', 'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          'Chrome/126.0 Safari/537.36'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode('utf-8'))
        out = data['choices'][0]['message']['content'] or ''
        usage = _extract_usage(data)
    elif backend == 'kimi':
        from agents import KimiAgent
        # 先占并发位再记 RPM 戳：反过来的话戳会在等位期间变旧，白白浪费额度。
        # 缓存命中在上面就 return 了，重放不占位、零 API。
        with _KIMI_INFLIGHT:
            kimi_acquire()
            out = KimiAgent(model=model).complete(
                prompt, system=system, temperature=temp, max_tokens=mt) or ''
        usage = {}
    else:
        from agents import MimoAgent
        agent = MimoAgent(model=model)
        out = agent.complete(prompt, system=system, temperature=temp,
                             max_tokens=mt) or ''
        usage = {}
    if use_cache:
        cache_put(key, out, usage)
    return out


#: 额度耗尽类错误：重试不会好转，必须立刻放弃并声明不可用。
#: 实测 WorkBuddy 通道回 429 + 「额度已用尽，请购买加量包」——
#: 与真限流同样是 429，但等 30s×3 纯属白烧，50 个单元能拖死半小时。
#: Kimi 官方零余额实测回 429 + 「is suspended due to insufficient balance …
#: check your plan and billing details」/ type=exceeded_current_quota_error。
QUOTA_PAT = ('额度已用尽', 'credit package', 'insufficient_quota',
             'quota exceeded', '余额不足', 'billing',
             'insufficient balance', 'exceeded_current_quota', 'suspended')


class BackendUnavailable(RuntimeError):
    """后端不可用（额度耗尽 / 模型下线），非瞬时限流。"""


#: 供应商报错正文里会带账号标识（Kimi 429 实测回
#: "Your account org-<32位hex><ak-...> request reached organization max RPM"）。
#: 这类 reason 会落进汇总 JSON，擦掉标识再存，避免把账号 ID 带进任何产物。
_ACCOUNT_ID_RE = re.compile(r'org-[0-9a-f]{8,}|ak-[0-9a-z]{8,}')


def scrub_reason(text):
    return _ACCOUNT_ID_RE.sub('<账号标识已擦除>', str(text))


#: 服务端在正文里明说了等多久：Kimi 实测
#: "request reached organization max RPM: 3, please try again after 1 seconds"。
#: 一律睡 30s 是把 1s 的等待放大 30 倍——上一轮 real28 因此空转数小时。
_RETRY_AFTER_BODY_RE = re.compile(r'try again after\s+(\d+(?:\.\d+)?)\s*second',
                                  re.I)


def retry_wait(text):
    """该睡多久再重试。优先服务端正文提示，其次 429 兜底 30s，其它 4s。"""
    m = _RETRY_AFTER_BODY_RE.search(str(text))
    if m:
        # +0.5s 余量：滚动窗口边界上按字面秒数重试仍可能撞限流
        return max(float(m.group(1)), 0.5) + 0.5
    low = str(text).lower()
    return 30 if ('429' in low or 'too many' in low or 'rate' in low) else 4


#: 三臂共用的 API 重试预算（外层）。内层 make_complete 另有 3 次，
#: 故每条最多 3×3=9 次真调。
#:
#: 为什么必须共用：曾经 judge_old 自己包了 3 次外层重试（共 9 次尝试），
#: 而 guarded_one 只调 complete 一次（共 3 次）。Kimi 组织级 RPM=3 被打满时
#: 护栏臂预算只有旧双臂的 1/3，先耗尽就回落纯本地、整单元被排除——
#: **装置本身让护栏臂看起来更差**，real28 两个官方 Kimi 的 guarded 因此全灭。
#: 预算写在一处，两臂都走它，就不会再各自漂移。
API_OUTER_ATTEMPTS = 3


def call_with_retry(complete, prompt, system):
    """三臂统一的调用入口：只管 API 失败重试，不管判词解析。

    欠费/停用（BackendUnavailable）直接上抛，不重试——重试不会好转。
    """
    last = None
    for _ in range(API_OUTER_ATTEMPTS):
        try:
            return complete(prompt, system)
        except BackendUnavailable:
            raise
        except Exception as e:  # noqa: BLE001
            last = scrub_reason(repr(e))[:600]
            time.sleep(retry_wait(last))
    raise RuntimeError(last or 'call failed')


def make_complete(backend, model, trial):
    def complete(prompt, system):
        last = None
        for attempt in range(3):
            try:
                out = raw_call(backend, model, system, prompt, trial)
                if out and out.strip():
                    return out
                last = 'empty-content'
            except Exception as e:  # noqa: BLE001
                body = ''
                try:
                    if hasattr(e, 'read'):
                        body = e.read()[:400].decode('utf-8', 'replace')
                except Exception:  # noqa: BLE001
                    pass
                # 必须先擦账号标识再截断：Kimi 的 org-<32hex>/ak-<id> 正好占满
                # 原来的 160 字符预算，把后面的 billing/suspended 判定词全切掉，
                # 于是零余额被当成瞬时限流，28 条 ×3×30s 空转了 9.5 小时。
                last = scrub_reason(repr(e) + ' ' + body)[:600]
                if any(p in last.lower() for p in QUOTA_PAT):
                    raise BackendUnavailable(last) from e
                time.sleep(retry_wait(last))
        raise RuntimeError(last or 'call failed')
    return complete


def preflight(specs, timeout=45, attempts=4, retry_wait=20):
    """逐后端探一次活；探不通的**声明为不可用**并剔除，但必须写进汇总。

    为什么需要：网关的 mimo-v2.6-flash-free 实测已下线，连「回一个 JSON」
    都超时。不探活的话每个单元都要烧 3×180s 才判失败，15 个单元把整轮
    拖死二十多分钟，日志看着像卡死。

    探活**必须绕过缓存**（use_cache=False）：探针 prompt 恒定，走缓存的话
    首次成功后永远 0.0s 命中，额度耗尽与模型下线一个都探不出来。

    瞬时失败必须重试后才判死：Kimi 官方实测会间歇回 429
    engine_overloaded_error（引擎过载，稍后即好）。一次异常就剔除，会把
    活着的模型静默踢出矩阵 —— 本轮 K3.1-Pro 正是这样被误剔的。
    欠费/停用则立刻判死，重试无意义。
    """
    alive, dead = [], []
    for b, m, lab in specs:
        t0 = time.time()
        reason = None
        for k in range(attempts):
            try:
                raw_call(b, m, '你是连通性探针。', '只回 JSON：{"ok":"是"}',
                         -1, timeout=timeout, use_cache=False)
                log(f'探活 OK   {lab}  {time.time() - t0:.1f}s'
                    + (f'（第 {k + 1} 次尝试）' if k else ''))
                alive.append((b, m, lab))
                reason = None
                break
            except Exception as e:  # noqa: BLE001
                body = ''
                try:
                    if hasattr(e, 'read'):
                        body = e.read()[:400].decode('utf-8', 'replace')
                except Exception:  # noqa: BLE001
                    pass
                reason = scrub_reason((repr(e) + ' ' + body).strip())[:300]
                if any(p in reason.lower() for p in QUOTA_PAT):
                    break
                if k < attempts - 1:
                    log(f'探活 瞬时失败 {lab}（第 {k + 1}/{attempts} 次）'
                        f'→ 重试：{reason[:120]}')
                    time.sleep(retry_wait)
        if reason is not None:
            dead.append({'label': lab, 'model': m, 'backend': b,
                         'reason': reason})
            log(f'探活 失败 {lab} → 声明不可用，本轮不纳入：{reason[:160]}')
    return alive, dead


# ---------------------------------------------------------------- 语料
def real28_available():
    """真实语料是否存在。

    real28 的段落与台账含人工复核信息，经 .git/info/exclude 排除在仓库外，
    新克隆上必然不存在。依赖它的测试必须**显式跳过并说明原因**，不能报错把
    全量回归打红，也不能静默当成通过。
    """
    return PASSAGES.exists() and LEDGER.exists()


def load_real28():
    import csv
    if not real28_available():
        raise SystemExit(
            '[FAIL] 真实语料 real28 不在本机（仓库外私有数据，见 .git/info/exclude）。'
            '只跑 synthetic / holdout 即可，不得把缺语料当成通过。')
    with open(LEDGER, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    style = [r for r in rows if r['类型'] == 'AI风格提示']
    if len(style) != 28:
        raise SystemExit(f'[FAIL] 台账风格条数 {len(style)} != 28，语料已变，口径不可比')

    def norm(v):
        return '真' if v.startswith('真') else ('假' if v.startswith('假') else '存疑')
    labels = [norm(r['判定']) for r in style]
    with open(PASSAGES, encoding='utf-8') as f:
        passages = json.load(f)
    if len(passages) != 28:
        raise SystemExit(f'[FAIL] 段落数 {len(passages)} != 28')
    items = []
    for i, (x, lab) in enumerate(zip(passages, labels), 1):
        pa = x['passage']
        items.append({'序号': i, 'case': x['case'], 'layer': x['layer'],
                      'human': lab, 'reasons': pa.get('reasons', []),
                      'excerpt': pa.get('excerpt') or ''})
    return items


def load_synthetic():
    import style_eval_corpus as corp
    return corp.items()


def load_holdout():
    """留出集：条目在两条新规则定稿之后才写，用于检验泛化而非调参。"""
    import style_eval_holdout as ho
    return ho.items()


CORPORA = {'real28': load_real28, 'synthetic': load_synthetic,
           'holdout': load_holdout}


# ---------------------------------------------------------------- 单臂单次
# ---------------------------------------------------------------- 截断抢救
# 实测：思考型模型（Gemini-Flash-Lite 系）把 max_tokens=2000 的预算花在推理上，
# 输出 JSON 的**最后一个长文本字段**被切断（旧双臂是 reason，护栏臂是 basis）。
# 但判定字段都在 JSON 前部、完好可读。旧解析器要求整个对象完整，等于因为辅助
# 字段被切就丢掉一个有效判定——这是测量 bug，不是模型失败。
#
# 安全性：reconcile 里唯一能产出「假」的模型路径要求 needs_revision=="否" 且
# evidence_sufficient=="是" 两字段都显式存在；抢救时缺失字段一律给空串，
# 只会落到「存疑」，绝不会凭空造出「假」或「真」。visible 数组要求闭合括号，
# 截断的数组不会被当成有效确认。
OLD_VERDICT_RE = re.compile(r'"verdict"\s*:\s*"(真|假|存疑)"')
OLD_REASON_RE = re.compile(r'"reason"\s*:\s*"([^"]*)')
T_SECTION_RE = re.compile(r'"section_type"\s*:\s*"([^"]*)"')
T_VISIBLE_RE = re.compile(r'"visible"\s*:\s*\[(.*?)\]', re.S)
T_NEEDS_RE = re.compile(r'"needs_revision"\s*:\s*"([^"]*)"')
T_SUFF_RE = re.compile(r'"evidence_sufficient"\s*:\s*"([^"]*)"')
T_BASIS_RE = re.compile(r'"basis"\s*:\s*"([^"]*)')


def salvage_old(out):
    """从截断输出里抢救 verdict。门槛与严格解析一致：verdict 必须精确合法。"""
    if not out:
        return None
    m = OLD_VERDICT_RE.search(out)
    if not m:
        return None
    r = OLD_REASON_RE.search(out)
    return m.group(1), (r.group(1) if r else '')[:60]


def salvage_triage(out):
    """从截断输出里抢救识别结果。门槛与 parse_triage 一致：必须有 section_type。"""
    if not out:
        return None
    sec = T_SECTION_RE.search(out)
    if not sec:
        return None
    vis = T_VISIBLE_RE.search(out)
    needs = T_NEEDS_RE.search(out)
    suff = T_SUFF_RE.search(out)
    basis = T_BASIS_RE.search(out)
    return {'section_type': sec.group(1),
            'visible': re.findall(r'"([^"]*)"', vis.group(1)) if vis else [],
            'needs_revision': needs.group(1) if needs else '',
            'evidence_sufficient': suff.group(1) if suff else '',
            'basis': basis.group(1) if basis else ''}


def guarded_one(reasons, excerpt, complete):
    """与 st.triage_passage 同流程，只在严格解析失败时多做一步抢救。

    不直接改产品：产品侧 parse_triage 的截断缺陷单独报告，不在本轮偷偷改行为。
    严格解析成功时本函数与 triage_passage 逐字段一致（由 style_eval_test 断言）。
    """
    rs = list(reasons or [])
    text = excerpt or ''
    sig = st.local_signals(text)
    gate = st.local_gate(text, rs)
    if gate['exempt']:
        res = st.reconcile(None, rs, text, sig)
        res['model_called'] = False
        res['parse_mode'] = 'exempt'
        return res
    if complete is None:
        res = st.reconcile(None, rs, text, sig, model_available=False)
        res['model_called'] = False
        res['parse_mode'] = 'no_model'
        return res
    prompt = st.build_triage_prompt(rs, text)
    raw = call_with_retry(complete, prompt, st.STYLE_TRIAGE_SYSTEM) or ''
    parsed = st.parse_triage(raw)
    mode = 'strict'
    if parsed is None:
        parsed = salvage_triage(raw)
        mode = 'salvaged' if parsed else 'unparseable'
    res = st.reconcile(parsed, rs, text, sig)
    res['model_called'] = True
    res['parse_mode'] = mode
    res['raw'] = raw[:600]
    return res


def judge_old(complete, system, reasons, excerpt):
    """与 r6 同口径，另加截断抢救。返回 (verdict, reason, err, parse_mode)。

    外层 3 次只对「答了但没判词」重试；API 失败重试统一走 call_with_retry，
    与护栏臂同预算（3 外层 × make_complete 3 内层 = 9 次真调）。
    """
    prompt = TEMPLATE.format(reasons='；'.join(reasons), excerpt=excerpt[:500])
    last = None
    for _ in range(3):
        try:
            out = call_with_retry(complete, prompt, system)
        except BackendUnavailable:
            # 后端已停用/欠费：再重试只是白烧，且会把「没测到」写成「测到了但失败」。
            raise
        except Exception as e:  # noqa: BLE001
            return None, '', scrub_reason(repr(e))[:600], 'failed'
        m = re.search(r'\{[^{}]*\}', out, re.S)
        if m:
            try:
                j = json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                j = None
            if isinstance(j, dict) and j.get('verdict', '') in VALID:
                return j['verdict'], str(j.get('reason', ''))[:60], None, 'strict'
        sv = salvage_old(out)
        if sv:
            return sv[0], sv[1], None, 'salvaged'
        last = 'no-verdict:' + out[:60]
    return None, '', last, 'failed'


def run_one(backend, model, label, arm, trial, items):
    path = HERE / f'{slug(model)}__{arm}__r{trial}.json'
    if path.exists():
        d = json.loads(path.read_text(encoding='utf-8'))
        # 解析器换代必须重算：旧护栏臂把截断当成合法「存疑」，complete=True，
        # 只看 complete 会把这批静默 SKIP 掉。响应已进缓存，重算不烧 API。
        # 条目数同样必须比对：语料扩容后旧单元格只有 30 条，complete 仍为 True，
        # 不比对就会让旧模型停在 n=30、新模型跑 n=40，分母悄悄不齐。
        # 回落条数同样必须比对：回落走纯本地也给出合法判词，complete 仍为 True，
        # 不比对就会把「通道坏了没测到」当成已完成而永久 SKIP。计数从 results
        # 现算，不信 fallbacks 字段（旧进程根本不写它，读出来是 None）。
        fb = sum(1 for r in d.get('results', [])
                 if r.get('parse_mode') == 'error_fallback')
        if (d.get('complete') and d.get('parser_version') == PARSER_VERSION
                and d.get('items') == len(items) and not fb):
            log(f'SKIP {label} {arm} r{trial} (done)')
            return d
    complete = make_complete(backend, model, trial)
    results = []
    for it in items:
        reasons, excerpt = it['reasons'], it['excerpt']
        if arm == 'guarded':
            try:
                r = guarded_one(reasons, excerpt, complete)
                v, reason, err = r['verdict'], r['reason'], None
                extra = {'source': r['source'], 'guards': r['guards'],
                         'model_called': r['model_called'],
                         'model_opinion': r['model_opinion'],
                         'parse_mode': r['parse_mode']}
            except BackendUnavailable:
                # 欠费/停用不是「模型这一条没答好」，不能回落成本地判定冒充已测。
                raise
            except Exception as e:  # noqa: BLE001
                r = st.triage_passage(reasons, excerpt, None)
                # 先擦账号标识再截断：Kimi 的 429 正文带 org-/ak-，
                # 原来 [:110] 恰好把它们切掉才没泄露，那是运气不是设计。
                v, reason, err = r['verdict'], r['reason'], scrub_reason(repr(e))[:600]
                extra = {'source': r['source'] + '+fallback', 'guards': r['guards'],
                         'model_called': False, 'model_opinion': None,
                         'parse_mode': 'error_fallback'}
        else:
            system = SYSTEM_GENERIC if arm == 'generic' else SYSTEM_VERTICAL
            v, reason, err, mode = judge_old(complete, system, reasons, excerpt)
            extra = {'model_called': True, 'parse_mode': mode}
        rec = {'序号': it['序号'], 'case': it['case'], 'human': it['human'],
               'llm': v, 'llm_reason': reason, 'err': err}
        rec.update(extra)
        results.append(rec)
        time.sleep(0.2)
    bad = [r['序号'] for r in results if r['llm'] not in VALID]
    fb = sum(1 for r in results if r.get('parse_mode') == 'error_fallback')
    payload = {'model': model, 'label': label, 'backend': backend, 'arm': arm,
               'trial': trial, 'items': len(results), 'results': results,
               'complete': not bad, 'bad_items': bad,
               'parser_version': PARSER_VERSION,
               'salvaged': sum(1 for r in results if r.get('parse_mode') == 'salvaged'),
               'fallbacks': fb,
               'model_calls': sum(1 for r in results if r.get('model_called'))}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    log(f'{"OK  " if not bad else "BAD "} {label} {arm} r{trial} '
        f'calls={payload["model_calls"]}/{len(results)}'
        f' 抢救={payload["salvaged"]}'
        + (f' 缺判词={bad}' if bad else '')
        + (f' ⚠回落={fb}（未测到该臂，汇总将排除本单元）' if fb else ''))
    return payload


# ---------------------------------------------------------------- 指标
def score(results):
    """分母 = 全部条数。调用方必须已确认 complete，否则不该进来。"""
    n = len(results)
    strict = directional = spec_num = spec_den = fn = ff_num = ff_den = 0
    for r in results:
        v, lab = r['llm'], r['human']
        if v == lab:
            strict += 1
        ht, lt = lab == '真', v == '真'
        if ht == lt:
            directional += 1
        if not ht:
            spec_den += 1
            if not lt:
                spec_num += 1
            if lab == '假':
                ff_den += 1
                if v == '假':
                    ff_num += 1
        elif v == '假':
            fn += 1
    pct = lambda a, b: round(100.0 * a / b, 4) if b else None  # noqa: E731
    return {'n': n, '严格一致率': pct(strict, n), '方向一致率': pct(directional, n),
            '特异度': pct(spec_num, spec_den), '漏真FN': fn,
            '假族覆盖': pct(ff_num, ff_den),
            '_counts': {'strict': strict, 'directional': directional,
                        'spec_num': spec_num, 'spec_den': spec_den,
                        'ff_num': ff_num, 'ff_den': ff_den, 'fn': fn}}


def wilson(k, n, z=1.96):
    """比例的 Wilson 95% 区间。n 小时比正态近似可靠。"""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * (c - h), 2), round(100 * (c + h), 2)]


def fallback_count(cell):
    """该单元里有多少条是「模型调用失败 → 回落纯本地」判出来的。

    回落条目**不是模型输出**，护栏臂里它根本没被测量。一律从 results 现算：
    旧单元格没有计数字段，而落盘计数一旦与 results 不一致，信字段就等于
    给污染开了个后门。results 是唯一真源。
    """
    return sum(1 for r in cell.get('results') or []
               if r.get('parse_mode') == 'error_fallback')


def partial_arms(table, arms):
    """点名「只缺部分臂」的模型。

    no_data 只抓全空的，degraded_fallback 只抓「跑过但回落被排除」的；
    两者都不覆盖「这一臂根本没跑过」——表里它只显示一个 0，
    读者会以为测过且结果为零。缺臂模型不进「三臂齐全」均值，但必须显式声明。
    """
    out = []
    for r in table:
        missing = [a for a in arms if not r['arms'][a].get('available')]
        if missing and len(missing) < len(arms):
            out.append({
                'label': r['label'], 'missing_arms': missing,
                'reason': ('跑过但回落被排除'
                           if any(r['arms'][a].get('degraded_fallback_cells')
                                  for a in missing) else '未执行'),
                'measured_arms': [a for a in arms if a not in missing]})
    return out


def summarize(runs):
    """runs = 同一 (model, arm) 的多次重复。只纳入 complete 且**无回落**的重复。

    complete 只保证「每条都有合法判词」。额度耗尽时护栏会按产品红线回落纯本地
    （判存疑，绝不判假），判词合法 → complete=True → 整个单元被当成正常测量。
    实测 Kimi-K3(网关) 在 holdout 上 5 个护栏单元共 43 条回落（占 36%），
    汇总却把它当成一个正常模型报出去，还让它成了「护栏方向输给旧垂直」的唯一负例。
    那是通道死了，不是护栏保守。**回落单元必须排除并点名，绝不静默混入均值。**
    """
    good = [r for r in runs if r.get('complete') and not fallback_count(r)]
    degraded = [r for r in runs if r.get('complete') and fallback_count(r)]
    bad = [r for r in runs if not r.get('complete')]
    # 声明必须在「全军覆没」分支之前算好：那条分支提前 return，
    # 漏掉条数就会让汇总打印「5 个重复共 0 条回落」——实测真值是 43 条。
    decl = {}
    if degraded:
        n_fb = sum(fallback_count(r) for r in degraded)
        decl = {'degraded_fallback_cells': len(degraded),
                'degraded_fallback_items': n_fb,
                'degraded_fallback_note': (
                    ('另有' if good else '全部') + f' {len(degraded)} 个重复（r'
                    + ','.join(str(r.get('trial')) for r in degraded)
                    + f'）共 {n_fb} 条因模型调用失败回落纯本地，'
                    '未测到该臂，已排除出均值；这不是护栏保守，是通道问题。')}
    if not good:
        why = ('全部重复分母不齐' if not degraded else
               f'全部 {len(degraded)} 个重复都有模型调用失败回落，未测到该臂')
        return dict({'available': 0, 'incomplete': len(bad), 'error': why}, **decl)
    scores = [score(r['results']) for r in good]
    out = {'available': len(good), 'incomplete': len(bad), 'n': scores[0]['n'],
           'model_calls_mean': round(statistics.mean(r['model_calls'] for r in good), 2)}
    out.update(decl)
    for metric in ('严格一致率', '方向一致率', '特异度', '假族覆盖'):
        vals = [s[metric] for s in scores if s[metric] is not None]
        if not vals:
            out[metric] = None
            continue
        entry = {'mean': round(statistics.mean(vals), 2),
                 'sd': round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0,
                 # min/max 与 per_trial 同精度：否则「最小值」对不上任何一轮实测值
                 'min': round(min(vals), 4), 'max': round(max(vals), 4),
                 'per_trial': vals}
        out[metric] = entry
    # 合并全部重复的计数给 Wilson 区间（把重复当独立观测）
    tot = Counter()
    for s in scores:
        tot.update(s['_counts'])
    n_all = sum(s['n'] for s in scores)
    out['wilson95'] = {
        '严格': wilson(tot['strict'], n_all),
        '方向': wilson(tot['directional'], n_all),
        '特异度': wilson(tot['spec_num'], tot['spec_den']),
        '假族覆盖': wilson(tot['ff_num'], tot['ff_den'])}
    # 上面那个区间把 5×28 当 140 个独立观测，但**同一批条目被反复问**，
    # 重复之间高度相关 → 它系统性低估不确定性，不能拿来宣称精度。
    # 重复真正测的是「模型随机性」，条目抽样误差要看单轮 n 的区间。
    out['wilson95_per_trial'] = [
        {'trial': i, 'n': s['n'], '严格': wilson(s['_counts']['strict'], s['n'])}
        for i, s in enumerate(scores)]
    ns = '/'.join(str(v) for v in sorted({s['n'] for s in scores}))
    out['wilson95_caveat'] = (
        'wilson95 把重复当独立观测，条目重复出现使其偏窄（反保守）；'
        f'条目抽样误差以 wilson95_per_trial(单轮 n={ns}) 为准，'
        '模型随机性以各指标 sd 为准。')
    out['漏真FN_total'] = tot['fn']
    stale = sum(g.get('replay_misses', 0) for g in good)
    if stale:
        out['stale_replay_items'] = stale
        out['stale_replay_note'] = (
            f'{stale} 条判定来自缓存缺失时原样保留的旧判词，未按当前规则重算；'
            '该臂数字是新旧混合，补齐缓存后重跑 --replay 才是纯当前规则结果。')
    out['sources'] = dict(Counter(r.get('source', '') for g in good for r in g['results']))
    return out


def constant_baselines(items):
    """恒定预测基线（一律真 / 一律假 / 一律存疑）。

    【为什么必须摆出来】real28 的标签是 假8/存疑19/真1，极偏。
    「一律存疑」这种零信息预测器就能拿 方向 96.43% / 严格 67.86%，
    严格分比任何一臂都高。不把基线并排放，100% 方向一致率会被读成
    「模型很强」，而实际增益只有那 1 条真标签的 3.57 个点。
    语料偏斜时指标会奖励恒定预测——这是评测地基问题，不是模型问题。
    """
    out = {}
    for v in ('真', '假', '存疑'):
        s = score([{'llm': v, 'human': it['human']} for it in items])
        out[v] = {k: s[k] for k in ('严格一致率', '方向一致率', '特异度',
                                    '假族覆盖', '漏真FN')}
    return out


# ---------------------------------------------------------------- 主流程
def replay_guarded(backend, model, label, trial, items):
    """只用缓存里的模型响应重算护栏判定——迭代判定逻辑不重复烧 API。

    缓存 key 含 prompt，所以能由 items 反推出 key 并取回原始响应。
    取不到的条目**原样保留**并计数，绝不静默降级成「未调用模型」。
    """
    path = HERE / f'{slug(model)}__guarded__r{trial}.json'
    if not path.exists():
        return None
    d = json.loads(path.read_text(encoding='utf-8'))
    if d.get('items') != len(items):
        # 回放只能重算已有条目，变不出新条目的模型响应。语料扩容后旧单元格
        # 条目数不足，继续回放会把 n=30 的旧结果盖上新解析器版本戳冒充 n=40。
        log(f'REPLAY 跳过 {label} guarded r{trial}：单元格 {d.get("items")} 条 '
            f'≠ 当前语料 {len(items)} 条，需真跑补齐（回放零 API，补不出新条目）')
        return None
    by_no = {it['序号']: it for it in items}
    miss = 0
    for rec in d['results']:
        if rec.get('parse_mode') == 'error_fallback':
            continue
        it = by_no.get(rec['序号'])
        if it is None:
            miss += 1
            continue
        if not rec.get('model_called'):
            # 豁免条目当初根本没调模型，缓存里必然没有它。先查缓存再判豁免，
            # 会把这种结构性「无此条目」记成 miss——real28 每轮 18 条豁免，
            # 50 个单元格凭空多出 900 次「失效」，命中率看着像大面积失败。
            new = guarded_one(it['reasons'], it['excerpt'], None)
        else:
            prompt = st.build_triage_prompt(it['reasons'], it['excerpt'])
            temp, mt = call_params(backend)
            raw = cache_get(cache_key(backend, model, st.STYLE_TRIAGE_SYSTEM,
                                      prompt, trial, temp, mt))
            if raw is None:
                miss += 1
                continue
            new = guarded_one(it['reasons'], it['excerpt'], lambda p, s: raw)
        rec.update({'llm': new['verdict'], 'llm_reason': new['reason'],
                    'source': new['source'], 'guards': new['guards'],
                    'model_opinion': new['model_opinion'],
                    'model_called': new['model_called'],
                    'parse_mode': new['parse_mode']})
    bad = [r['序号'] for r in d['results'] if r['llm'] not in VALID]
    d.update({'complete': not bad, 'bad_items': bad,
              'parser_version': PARSER_VERSION,
              # 缓存缺失的条目保留的是**旧判词**。不记下来，汇总就会把
              # 「v3 重算」和「v2 旧值」混在一个 parser_version 下报出去。
              'replay_misses': miss,
              'salvaged': sum(1 for r in d['results'] if r.get('parse_mode') == 'salvaged'),
              'fallbacks': sum(1 for r in d['results']
                               if r.get('parse_mode') == 'error_fallback'),
              'model_calls': sum(1 for r in d['results'] if r.get('model_called'))})
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    log(f'REPLAY {label} guarded r{trial} → 方向 {score(d["results"])["方向一致率"]}%'
        + (f'（{miss} 条缓存缺失，原样保留）' if miss else ''))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--corpus', default='real28', choices=sorted(CORPORA))
    ap.add_argument('--repeats', type=int, default=5)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--arms', default='generic,vertical,guarded')
    ap.add_argument('--only', default='', help='按 label 逗号分隔筛选')
    ap.add_argument('--backend', default='all',
                    choices=['all', 'gateway', 'official', 'kimi'])
    ap.add_argument('--aggregate-only', action='store_true')
    ap.add_argument('--replay', action='store_true',
                    help='只用缓存响应重算护栏判定，零 API 调用')
    args = ap.parse_args()

    items = CORPORA[args.corpus]()
    outdir = use_corpus_dir(args.corpus)
    unavailable = []
    labels = Counter(i['human'] for i in items)
    log(f'语料 {args.corpus}：{len(items)} 条，人工/构造标签 {dict(labels)}')
    log(f'结果目录 {outdir}（按语料隔离，绝不与另一份语料共用）')
    if args.corpus != 'real28':
        log('注意：本语料标签为**构造式**，只能与真实语料并列报告，不得相加或混算')
    if labels.get('真', 0) < 2:
        log(f'⚠ 真标签只有 {labels.get("真", 0)} 条 → 灵敏度分母极小，'
            '「漏真FN=0」不能解读为灵敏度高')
    log(f'重复次数 {args.repeats}，并发 {args.workers}，缓存目录 {CACHE}')

    specs = []
    if args.backend in ('all', 'gateway'):
        specs += [('gateway', mid, lab) for lab, mid in GATEWAY_MODELS]
    if args.backend in ('all', 'official'):
        if not os.environ.get('MIMO_API_KEY'):
            log('⚠ 未设 MIMO_API_KEY，官方两模型跳过（不静默当成通过）')
        else:
            specs += [('official', mid, lab) for lab, mid in OFFICIAL_MODELS]
    if args.backend in ('all', 'kimi'):
        if not os.environ.get('KIMI_API_KEY'):
            log('⚠ 未设 KIMI_API_KEY，Kimi 官方模型跳过（不静默当成通过）')
        else:
            specs += [('kimi', mid, lab) for lab, mid in KIMI_MODELS]
            log(f'⚠ Kimi 官方温度恒为 {KIMI_TEMPERATURE}（平台强制），'
                f'max_tokens={KIMI_MAX_TOKENS}，组织级 RPM={KIMI_RPM}、'
                f'并发={KIMI_CONCURRENCY} 全局限流；'
                '与其余 temp=0.2 的模型**不可直接混池**，汇总逐模型声明')
    if args.only:
        want = {s.strip() for s in args.only.split(',') if s.strip()}
        specs = [s for s in specs if s[2] in want]
    arms = [a.strip() for a in args.arms.split(',') if a.strip()]

    if args.replay:
        n = 0
        for b, m, lab in specs:
            for t in range(args.repeats):
                if replay_guarded(b, m, lab, t, items) is not None:
                    n += 1
        log(f'重算 {n} 个护栏单元，API 调用 0 次')
    elif not args.aggregate_only:
        specs, unavailable = preflight(specs)
        if not specs:
            raise SystemExit('[FAIL] 所有后端都探不通 —— 没有任何可跑的任务，不报绿')
        jobs = [(b, m, lab, a, t) for b, m, lab in specs
                for a in arms for t in range(args.repeats)]
        log(f'总任务 {len(jobs)} 个 (模型×臂×重复)')
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(run_one, b, m, lab, a, t, items): (b, lab, a, t)
                    for b, m, lab, a, t in jobs}
            done = 0
            dead_mid = set()
            for f in as_completed(futs):
                b, lab, a, t = futs[f]
                done += 1
                try:
                    f.result()
                except BackendUnavailable as e:
                    # 探活只在开跑前做一次；账号中途欠费探不出来。此处必须停掉
                    # 该后端剩余任务并如实声明不可用，否则每个单元都白烧 3×30s。
                    if b not in dead_mid:
                        dead_mid.add(b)
                        reason = scrub_reason(str(e))[:300]
                        for sb, sm, slab in list(specs):
                            if sb == b:
                                specs.remove((sb, sm, slab))
                                unavailable.append({'label': slab, 'model': sm,
                                                    'backend': sb, 'reason': reason})
                        log(f'后端不可用 {lab} → 声明不可用并停掉该后端剩余任务：'
                            f'{reason[:160]}')
                        for g, (gb, glab, ga, gt) in futs.items():
                            if gb == b and g.cancel():
                                log(f'取消 {glab} {ga} r{gt}（后端已不可用）')
                except Exception as e:  # noqa: BLE001
                    log(f'FAIL {lab} {a} r{t}: {e!r}')
                if done % 25 == 0:
                    log(f'--- 进度 {done}/{len(jobs)} 缓存 {_cache_stats["hit"]}命中'
                        f'/{_cache_stats["miss"]}未命中 ---')

    # 汇总
    table = []
    for b, m, lab in specs:
        temp, mt = call_params(b)
        row = {'label': lab, 'model': m, 'backend': b,
               'temperature': temp, 'max_tokens': mt, 'arms': {}}
        for a in arms:
            runs = []
            for t in range(args.repeats):
                p = HERE / f'{slug(m)}__{a}__r{t}.json'
                if p.exists():
                    runs.append(json.loads(p.read_text(encoding='utf-8')))
            row['arms'][a] = summarize(runs) if runs else {'available': 0}
        table.append(row)

    out = {'corpus': args.corpus, 'items': len(items), 'labels': dict(labels),
           'repeats': args.repeats, 'temperature': TEMPERATURE,
           'max_tokens': MAX_TOKENS, 'gate_version': st.GATE_VERSION,
           'cache': dict(_cache_stats), 'usage': dict(_usage), 'models': table}
    out['baselines'] = constant_baselines(items)
    odd = sorted({(r['temperature'], r['max_tokens']) for r in table}
                 - {(TEMPERATURE, MAX_TOKENS)})
    if odd:
        out['temperature_caveat'] = (
            '以下 (temperature, max_tokens) 与主口径 '
            f'({TEMPERATURE}, {MAX_TOKENS}) 不同：{odd}。'
            '平台强制所致（Kimi 只接受 temperature=1）。这些模型的数字只能'
            '与**同模型**的三臂互比，不得与主口径模型横向排名或求平均。')
        log('⚠ 温度/额度口径不一致的模型已单独声明，不得与主口径混池：'
            + ', '.join(r['label'] for r in table
                        if (r['temperature'], r['max_tokens']) != (TEMPERATURE, MAX_TOKENS)))
    best = max(out['baselines'].items(), key=lambda kv: kv[1]['严格一致率'] or 0)
    log(f'恒定预测基线（零信息）：最佳「一律{best[0]}」严格 {best[1]["严格一致率"]:.2f}% '
        f'/ 方向 {best[1]["方向一致率"]:.2f}%')
    beaten = [r['label'] for r in table
              if r['arms'].get('guarded', {}).get('available')
              and (r['arms']['guarded'].get('严格一致率') or {}).get('mean', -1)
              < (best[1]['严格一致率'] or 0)]
    if beaten:
        out['baseline_beats_guarded_on_strict'] = beaten
        log(f'⚠ 护栏严格分低于「一律{best[0]}」基线的模型 {len(beaten)} 个 '
            '→ 该语料上严格口径不具区分力，不得据此宣称提升')
    if unavailable:
        out['unavailable'] = unavailable
        log('本轮声明不可用（未执行，不是通过）：'
            + ', '.join(d['label'] for d in unavailable))
    # 一条数据都没有的模型必须单独点名。--aggregate-only 不探活，拿不到
    # unavailable 的原因，但「无数据」本身绝不能混在表里看着像跑过了。
    no_data = [r['label'] for r in table
               if all(not r['arms'][a].get('available') for a in arms)]
    if no_data:
        out['no_data'] = no_data
        log('⚠ 无任何重复数据的模型（未执行，不是通过）：' + ', '.join(no_data))
    # 只缺**部分**臂的模型同样要点名：no_data 只抓全空的，degraded_fallback 只抓
    # 「跑过但回落被排除」的。两者都不覆盖「这一臂根本没跑过」——表里它只显示
    # 一个 0，读者会以为测过且结果为零。实测 synthetic 的 Kimi-K3.1 正是如此
    # （generic/vertical 各 5 个重复，guarded 一个单元格都没有）。
    partial = partial_arms(table, arms)
    if partial:
        out['partial_arms'] = partial
        for p in partial:
            log(f'⚠ {p["label"]} 缺臂 {"/".join(p["missing_arms"])}（{p["reason"]}，'
                f'不是测到零）；已测 {"/".join(p["measured_arms"])} '
                '→ 不进入「三臂齐全」均值')
    # 回落单元被排除后，模型可能只剩两条臂齐全 → 它会悄悄退出「三臂齐全」集合。
    # 不点名就等于静默缩小矩阵，与 unavailable/no_data 同一条纪律。
    degraded = [{'label': r['label'], 'arm': a,
                 'cells': r['arms'][a].get('degraded_fallback_cells', 0),
                 'items': r['arms'][a].get('degraded_fallback_items', 0),
                 'available': r['arms'][a].get('available', 0)}
                for r in table for a in arms
                if r['arms'].get(a, {}).get('degraded_fallback_cells')]
    if degraded:
        out['degraded_fallback'] = degraded
        for d in degraded:
            log(f'⚠ {d["label"]} {d["arm"]}：{d["cells"]} 个重复共 {d["items"]} 条'
                f'模型调用失败回落纯本地，已排除（该臂剩 {d["available"]} 个有效重复）'
                '——通道问题，不是护栏保守')
    (HERE / f'汇总_{args.corpus}.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')

    print('\n=== 三臂重复试验汇总 ===', flush=True)
    print('模型 | 臂 | 可用重复 | 方向 均值±sd [min,max] | 严格 均值±sd | 调用/条', flush=True)
    for r in table:
        for a in arms:
            s = r['arms'].get(a) or {}
            if not s.get('available'):
                print(f'{r["label"]} | {a} | 0 | 不可用 {s.get("error","")}', flush=True)
                continue
            d, st_ = s.get('方向一致率'), s.get('严格一致率')
            fmt = lambda e: (f'{e["mean"]:.2f}±{e["sd"]:.2f} [{e["min"]:.1f},{e["max"]:.1f}]'
                             if e else 'n/a')  # noqa: E731
            print(f'{r["label"]} | {a} | {s["available"]}/{args.repeats} | '
                  f'{fmt(d)} | {fmt(st_)} | {s["model_calls_mean"]}/{s["n"]}', flush=True)

    print('\n=== 护栏 vs 旧垂直 / 裸模型（均值差，含重复内极差）===', flush=True)
    for metric in ('方向一致率', '严格一致率', '特异度', '假族覆盖'):
        for base in ('vertical', 'generic'):
            deltas = []
            for r in table:
                g, v = r['arms'].get('guarded') or {}, r['arms'].get(base) or {}
                if not (g.get('available') and v.get('available')):
                    continue
                gm, vm = g.get(metric), v.get(metric)
                if not (gm and vm):
                    continue
                deltas.append((r['label'], round(gm['mean'] - vm['mean'], 2),
                               gm['sd'], vm['sd']))
            if not deltas:
                continue
            up = sum(1 for _, d, _, _ in deltas if d > 0)
            flat = sum(1 for _, d, _, _ in deltas if d == 0)
            down = sum(1 for _, d, _, _ in deltas if d < 0)
            print(f'{metric} 护栏 vs {base}: 上升{up} 持平{flat} 下降{down}', flush=True)
            for lab, d, gs, vs in sorted(deltas, key=lambda x: x[1]):
                print(f'    {lab}: {d:+.2f}  (护栏sd={gs:.2f} 基线sd={vs:.2f})', flush=True)

    tot = sum(r['arms'].get(a, {}).get('incomplete', 0) for r in table for a in arms)
    print(f'\n分母不齐被排除的重复：{tot}', flush=True)
    print(f'缓存：{dict(_cache_stats)}', flush=True)
    print(f'供应商用量（可取到的部分）：{dict(_usage)}', flush=True)
    print(f'汇总已写入 {HERE / ("汇总_" + args.corpus + ".json")}', flush=True)


if __name__ == '__main__':
    main()