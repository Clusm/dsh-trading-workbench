
# -*- coding: utf-8 -*-
"""生成《交易工作台技术文档》DOCX。"""
from docx import Document
from docx.shared import Pt, RGBColor, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.enum.table import WD_TABLE_ALIGNMENT

DOC = r"C:\Users\Tao\Desktop\币安\trading-workbench\docs\交易工作台-技术文档.docx"

doc = Document()

# 中文字体（必须同时设置 eastAsia，否则中文回落成默认字形）
style = doc.styles["Normal"]
style.font.name = "Microsoft YaHei"
style.font.size = Pt(10.5)
style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

for section in doc.sections:
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.4)


def heading(text, level=1):
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.name = "Microsoft YaHei"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    return h


def para(text, bold=False):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.bold = bold
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    return p


def bullet(text):
    p = doc.add_paragraph(style="List Bullet")
    run = p.add_run(text)
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    return p


def code(text):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.font.name = "Consolas"
    run.font.size = Pt(9)
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Consolas")
    p.paragraph_format.left_indent = Cm(0.6)
    return p


def table(headers, rows):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Light Grid Accent 1"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(h)
        run.bold = True
        run.font.name = "Microsoft YaHei"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    for row in rows:
        cells = t.add_row().cells
        for i, value in enumerate(row):
            cells[i].text = ""
            run = cells[i].paragraphs[0].add_run(str(value))
            run.font.name = "Microsoft YaHei"
            run.font.size = Pt(9)
            run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    return t


# ---------------------------------------------------------------- 封面
title = doc.add_heading("交易工作台 · 技术文档", level=0)
for run in title.runs:
    run.font.name = "Microsoft YaHei"
    run._element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

para("版本：2026-11-15（Demo 收口 + 实盘基础设施 M2/M3 部分完成）", bold=True)
para("适用范围：单用户、单账户、单活跃环境的自动化交易工作台。")
para("性质说明：本文是工程说明，不是实盘操作授权；以 live 身份启动仍需用户明确同意。")

# ---------------------------------------------------------------- 1
heading("1. 系统概览", 1)
para("工作台由三层组成，边界清晰：Client 只展示与提交命令，Host 负责编排与模型调用，"
     "Service 负责确定性风控、执行与对账，交易所与本地账本是事实来源。")
code("Client（DSH Web 插件）\n"
     "   ↓ 只读状态 / 提交命令\n"
     "Host（`host：调度、模型、轮次、配置快照）\n"
     "   ↓ 内部控制协议\n"
     "Service（FastAPI：身份、风控、租约、Outbox、对账）\n"
     "   ↓ signed/unsigned HTTP\n"
     "交易所（Demo/Live）      SQLite（本地镜像 + 审计）")

heading("1.1 关键设计原则", 2)
bullet("交易所是真值，本地库是镜像 + 审计；对不上时先对账，不改数字。")
bullet("LLM 只提出结构化计划，不持有密钥、不直接调用写接口。")
bullet("执行前由确定性代码复核：限额、数量、租约、控制态、数据新鲜度。")
bullet("结果不确定时如实标「待确认」，只由交易所响应或查询事实收敛。")
bullet("取不到的数据显示「不可用」，绝不回退成 0、空仓或「一切正常」。")

# ---------------------------------------------------------------- 2
heading("2. 运行与运维", 1)
heading("2.1 启动", 2)
para("唯一入口是 scripts/start-service.ps1，显式指定数据目录与端口：")
code('powershell -NoProfile -ExecutionPolicy Bypass -File "C:/Users/Tao/Desktop/币安/trading-workbench/scripts/start-service.ps1"')
para("脚本会校验 python venv 与 dotenv 存在、必要时重出发布清单、启动后核对 "
     "Host 与 Service 的身份（数据目录、端口、ledger_id、venue）。")

heading("2.2 停止", 2)
para("顶栏「停止自动交易」= 关闭本机调度 + POST /api/control {state:pause}；"
     "只撤执行租约、不平仓、不撤交易所保护单。")
para("危险动作「平仓并停止」单独放置在持仓处置区，需二次确认并逐项返回结果。")

heading("2.3 环境切换（运维动作，不在界面）", 2)
code("node scripts/venue-preflight.mjs --target live      # 只读预检\n"
     "node scripts/switch-venue.mjs --target live --confirm  # 真正切换（默认 dry-run）")
para("预检在以下任一条件下拒绝切换：源账户有持仓、有在途挂单、目标凭据缺失、"
     "目标目录不可写、域名不在受控映射内。切换失败保持停止，不自动切回源环境。")

# ---------------------------------------------------------------- 3
heading("3. 交易执行链路", 1)
table(
    ["阶段", "职责", "关键约束"],
    [
        ["分析 / 辩论", "生成候选与多空论证", "证据一轮内冻结，评分卡确定性计算"],
        ["决策", "选出计划（区间、止损、止盈、失效时间）", "必须原样复制上游计划，只许降低风险"],
        ["风控", "限额、保证金、连亏、回撤", "确定性重算允许数量，模型数量只作上限"],
        ["执行前闸门", "区间/有效期/证据时效/价格位置", "缺字段即阻断，不补默认值"],
        ["执行", "P1 审批令牌 → P2 复核 → 提交", "提交前强制刷新价格与余额"],
        ["保护", "止损/止盈挂单与覆盖核对", "先查已有保护，增量成交重算覆盖"],
        ["对账", "成交、权益、保护状态收敛", "只由交易所事实收敛，不按重试次数判失败"],
    ],
)

heading("3.1 标识贯通", 2)
code("round_id → intent_id / business_key → client_order_id → exchange_order_id")
para("同一意图重试保留原标识；网络超时先按原标识查询，不生成新标识重发。")

heading("3.2 状态语义", 2)
bullet("SUBMITTING / UNKNOWN：显示为「待确认」，不是失败。")
bullet("FILLED / CANCELED / EXPIRED / REJECTED / TRIGGERED：终态。")
bullet("TRIGGERED 专用于条件单已触发并产生实际订单（带 actualOrderId）。")

# ---------------------------------------------------------------- 4
heading("4. 数据与账本口径", 1)
heading("4.1 四个权益口径（不合并）", 2)
table(
    ["字段", "含义", "用途"],
    [
        ["wallet_balance", "交易所钱包余额", "资金事实"],
        ["margin_equity", "保证金权益（含未实现盈亏）", "风控基线、权益曲线"],
        ["unrealized_pnl", "未实现盈亏", "展示"],
        ["available_balance", "可用余额", "下单可用量"],
    ],
)
para("本地「净盈亏」是第五个口径，属于策略账本，不等于账户权益。")

heading("4.2 本地四本账", 2)
table(
    ["科目", "来源", "说明"],
    [
        ["已实现盈亏", "fills.realized_pnl", "交易所成交流水"],
        ["手续费", "fills.commission", "永远是成本；非计价币无换算时标未知"],
        ["资金费", "income 流水 FUNDING_FEE", "未回填时净盈亏显示「不可用」，不当 0"],
        ["划转", "income 流水 TRANSFER_*", "资金流，不计入策略盈利"],
    ],
)

heading("4.3 对账", 2)
para("恒等式：交易所权益变动 ≈ 本地净盈亏 + 净划转。输出 matched / partial / unknown，"
     "附差额、容差、原因与最后成功时间；容差不放大，差额有解释比「全绿」可信。")

# ---------------------------------------------------------------- 5
heading("5. 实盘接入现状（M2 完成，M3 部分）", 1)
heading("5.1 已完成", 2)
table(
    ["能力", "行为"],
    [
        ["环境启动时固定", "WORKBENCH_VENUE=demo|live；未知值拒绝启动"],
        ["受控域名映射", "demo→demo-fapi，live→fapi；跨环境基址一律拒绝"],
        ["每环境独立数据目录", "demo 用 data/，live 用 data/；互不共享"],
        ["凭据按环境取", "live 只读 BINANCE_LIVE_* 或 live.env，绝不借用 demo 凭据"],
        ["账本身份绑定", "库已绑某环境/某 key 指纹时，换身份写入被拒绝"],
        ["界面接入入口", "设置→连接与账户→实盘接入：填 key 只写本机 0600 文件，不回显明文"],
    ],
)
heading("5.2 尚未完成", 2)
bullet("界面内的环境切换（当前是脚本 + 重启，属运维动作）。")
bullet("目标账户存量仓位的「只观察不认领」完整流程。")
bullet("切换失败的自动回滚——刻意不做：失败应保持停止并报警。")

# ---------------------------------------------------------------- 6
heading("6. 稳定性：已踩过的坑与加固", 1)
table(
    ["问题", "现象", "加固"],
    [
        ["代理白名单非法条目", "NO_PROXY 含 [::1] 时 httpx 构造即崩，服务起不来",
         "包导入时清理 NO_PROXY，并记 warning（workbench_service/proxyenv.py）"],
        ["PowerShell 环境重复键", "NO_PROXY 与 no_proxy 并存时 Start-Process 抛异常",
         "启动脚本回退 cmd start，不枚举环境块"],
        ["宿主重载带走服务", "DSH 重启后子进程树被杀，界面显示连接失败",
         "用独立窗口/脚本启动；进程不挂在 DSH 名下"],
        ["只读接口被交易所拖死", "交易所不可达时 /api/status 挂 12s+",
         "读超时 3s + 单次预算 4s + 失败冷却 15s，故障返回 200 + stale"],
        ["暂停后账户视图空白", "ready=false 被误判为服务不可用",
         "按 service_ready 判定；暂停只影响开新仓"],
        ["条件单已触发仍显示在途", "algoStatus=FINISHED 被当成活动态",
         "映射为终态 TRIGGERED（带 actualOrderId 才成立）"],
    ],
)

heading("6.1 网络出口注意事项", 2)
para("交易所访问依赖本机代理（HTTP(S)_PROXY）。不走代理时域名直接不可达；"
     "代理不稳定会表现为间歇性 E_EXCHANGE_UNAVAILABLE。判断方法："
     "看 /api/status 的 exchange.reachable 与 last_success_at，不要只看一次 curl 结果。")

# ---------------------------------------------------------------- 7
heading("7. 测试与验收", 1)
table(
    ["套件", "命令", "当前结果"],
    [
        ["Service", "pytest -q", "447 passed + 5 subtests"],
        ["Client 静态检查", "node client/checks.mjs", "155 passed"],
        ["渲染断言", "node client/render-smoke.mjs", "300 passed"],
        ["Host/协议测试", "node --test test/*.test.mjs", "185 passed"],
    ],
)
para("验收纪律：计数必须绑定源码清单摘要与运行锚点；跨版本不比较；"
     "跳过（skipped）的用例如果正好覆盖某条验收，该验收记 blocked。")

# ---------------------------------------------------------------- 8
heading("8. 命令速查", 1)
code("启动服务      scripts/start-service.ps1\n"
     "健康检查      curl http://127.0.0.1:8791/health\n"
     "资金口径      GET /api/pnl?range=1d\n"
     "切换预检      node scripts/venue-preflight.mjs --target live\n"
     "运行态核对    scripts/verify-runtime.mjs --host <url> --service <url> --data-dir <dir>\n"
     "重出发布清单  node scripts/build-release.mjs\n"
     "测试          cd service && pytest -q ；cd plugin && node --test test/*.test.mjs")

heading("9. 已知限制", 1)
bullet("真实成交链路验收受交易所网络可达性影响，不可达时一律记 blocked。")
bullet("历史订单缺少 cycle_id（修复前的行不重写），归因覆盖率有限。")
bullet("失败调用的原始回复全文（raw_response_text）尚未取得样本，需要一次真实失败。")
bullet("Live 端到端切换未在真实账户上验证过。")

doc.save(DOC)
print("saved:", DOC)
