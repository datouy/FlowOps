# -*- coding: utf-8 -*-
"""生成《桂林地接社 · 自媒体矩阵自动化推广执行手册》docx"""
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

DOC_PATH = r"E:\project\Dtou\flowOps\桂林地接社-自媒体矩阵自动化推广执行手册.docx"

# 配色
C_DARK = RGBColor(0x1F, 0x3A, 0x5F)   # 深蓝
C_ACCENT = RGBColor(0x0B, 0x6E, 0x4F)  # 绿（桂林山水）
C_GRAY = RGBColor(0x55, 0x55, 0x55)
C_CODE_BG = "F2F4F7"

def set_cjk(run, name_cn="微软雅黑", name_lat="Microsoft YaHei"):
    run.font.name = name_lat
    r = run._element.rPr
    rFonts = r.find(qn('w:rFonts'))
    if rFonts is None:
        rFonts = OxmlElement('w:rFonts')
        r.append(rFonts)
    rFonts.set(qn('w:eastAsia'), name_cn)
    rFonts.set(qn('w:ascii'), name_lat)
    rFonts.set(qn('w:hAnsi'), name_lat)

def shade(cell_or_par, color):
    el = cell_or_par._p if hasattr(cell_or_par, '_p') else cell_or_par._tc
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:fill'), color)
    el.get_or_add_pPr() if hasattr(el, 'get_or_add_pPr') else None
    if hasattr(cell_or_par, '_p'):
        cell_or_par._p.get_or_add_pPr().append(shd)
    else:
        cell_or_par._tc.get_or_add_tcPr().append(shd)

doc = Document()

# 默认样式
normal = doc.styles['Normal']
normal.font.name = 'Microsoft YaHei'
normal.font.size = Pt(10.5)
normal._element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

# 页边距
for s in doc.sections:
    s.left_margin = Inches(0.85)
    s.right_margin = Inches(0.85)
    s.top_margin = Inches(0.75)
    s.bottom_margin = Inches(0.75)

def h1(text):
    p = doc.add_heading(level=1)
    r = p.add_run(text)
    set_cjk(r)
    r.font.color.rgb = C_DARK
    r.font.size = Pt(16)
    return p

def h2(text):
    p = doc.add_heading(level=2)
    r = p.add_run(text)
    set_cjk(r)
    r.font.color.rgb = C_ACCENT
    r.font.size = Pt(13)
    return p

def h3(text):
    p = doc.add_heading(level=3)
    r = p.add_run(text)
    set_cjk(r)
    r.font.color.rgb = C_DARK
    r.font.size = Pt(11.5)
    return p

def para(text, bold=False, size=10.5, color=None, italic=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    set_cjk(r)
    r.font.bold = bold
    r.font.italic = italic
    r.font.size = Pt(size)
    if color:
        r.font.color.rgb = color
    return p

def bullet(text, bold_prefix=None):
    p = doc.add_paragraph(style='List Bullet')
    if bold_prefix:
        r = p.add_run(bold_prefix)
        set_cjk(r)
        r.font.bold = True
        r.font.color.rgb = C_ACCENT
    r = p.add_run(text)
    set_cjk(r)
    r.font.size = Pt(10.5)
    return p

def code(lines):
    for i, line in enumerate(lines):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Inches(0.25)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0 if i < len(lines)-1 else 8)
        p.paragraph_format.line_spacing = 1.0
        r = p.add_run(line if line else ' ')
        set_cjk(r, name_cn="微软雅黑", name_lat="Consolas")
        r.font.name = 'Consolas'
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x22, 0x33, 0x44)
        shade(p, C_CODE_BG)

def table(headers, rows, widths=None):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = 'Table Grid'
    hdr = t.rows[0].cells
    for i, htext in enumerate(headers):
        hdr[i].text = ''
        p = hdr[i].paragraphs[0]
        r = p.add_run(htext)
        set_cjk(r)
        r.font.bold = True
        r.font.size = Pt(9.5)
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        shade(hdr[i], "1F3A5F")
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ''
            p = cells[i].paragraphs[0]
            r = p.add_run(str(val))
            set_cjk(r)
            r.font.size = Pt(9.5)
    if widths:
        for i, w in enumerate(widths):
            for row in t.rows:
                row.cells[i].width = Inches(w)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t

# ============ 封面标题 ============
title = doc.add_heading(level=0)
tr = title.add_run("桂林地接社 · 自媒体矩阵自动化推广执行手册")
set_cjk(tr)
tr.font.size = Pt(22)
tr.font.color.rgb = C_DARK

sub = doc.add_paragraph()
sr = sub.add_run("用 social-auto-upload 多平台自动发布工具，把《桂林地接社破局方案》跑成每天可执行的推广动作")
set_cjk(sr)
sr.font.size = Pt(12)
sr.font.color.rgb = C_ACCENT
sr.font.bold = True

meta = doc.add_paragraph()
mr = meta.add_run("依据文件：桂林地接社方案.pdf（内部讨论稿 V1.0，2026-10）  ×  项目 social-auto-upload（sau CLI）\n适用范围：仅限桂林一地地接业务；文中价格/账号为示例，落地前替换为实际底价与真实账号。")
set_cjk(mr)
mr.font.size = Pt(9)
mr.font.color.rgb = C_GRAY

doc.add_paragraph()

# ============ 一、这份手册怎么用 ============
h1("一、这份手册怎么用（30 秒看懂）")
para("《桂林地接社破局方案》回答了“做什么、为什么”：客人已经自己坐高铁来了，钱花在了柜台之外，所以要把“等组团社送团”改成“在客人落地 2 小时里什么都能搞定的人”，并给出 6 个动作、90 天节奏、6 个考核指标。")
para("但它没有回答“谁来每天发内容、怎么批量发、怎么定时发、怎么多平台同时铺开”。这正是 social-auto-upload 工具（sau CLI）的活儿。")
para("本手册的任务，就是把原方案里最吃“内容分发”的三件事——动作 3 内容前置、动作 4 上平台当供应商、以及动作 1/2/6 的内容侧——翻译成可直接执行的发布任务和命令。", bold=True)
table(
    ["原方案（PDF）", "本手册（新增）"],
    [
        ["6 个动作、90 天节奏、6 个指标", "账号矩阵 + 选题库 + 发布 SOP + 命令实操 + 30 天日历 + 数据闭环"],
        ["讲“做什么、为什么”", "讲“用哪台工具、怎么发、哪天发、发什么、怎么考核”"],
        ["面向老板和运营", "面向执行运营 + 内容运营 + 电商运营"],
    ],
    widths=[2.6, 4.2],
)
para("一句话结论：把“内容种草 → 团购/私信 → 加微信 → 行中复购 → 行后联程”这条链路，用一套工具定时、批量、多平台自动铺出去，1 个人干过去 5 个人的活。", bold=True, color=C_ACCENT)

# ============ 二、工具能力盘点 ============
h1("二、我们手里有什么：工具能力盘点")
para("项目 social-auto-upload 提供一个统一 CLI 入口 sau，一条命令完成“登录 → 校验 → 发布 → 定时”。当前主线已接入 10 个平台，覆盖原方案要打的所有主阵地。")
table(
    ["平台", "CLI 子命令", "视频", "图文", "定时", "特色能力"],
    [
        ["抖音", "sau douyin", "√", "√", "√", "可挂商品链接（--product-link）"],
        ["快手", "sau kuaishou", "√", "√", "√", "多图图文"],
        ["小红书", "sau xiaohongshu", "√", "√", "√", "图文笔记吃搜索长尾"],
        ["B 站", "sau bilibili", "√", "—", "√", "需 --tid 分区；自动调用 biliup"],
        ["视频号", "sau tencent", "√", "—", "√", "草稿/合集/横竖双封面"],
        ["百家号", "sau baijiahao", "√", "—", "—", "百度搜索生态"],
        ["支付宝生活号", "sau alipay", "√", "—", "—", "生活号内容"],
        ["微博", "sau weibo", "√", "—", "—", "话题传播"],
        ["虎扑", "sau hupu", "√", "—", "—", "垂直社区"],
        ["YouTube", "sau youtube", "√", "—", "—", "入境游外宣（可选）"],
    ],
    widths=[1.15, 1.55, 0.55, 0.55, 0.55, 2.45],
)
h3("四个对推广最关键的能力")
bullet("多账号：一个 account_name 对应一个账号文件，可准备多个账号并发使用（攻略号、管家 IP、企业号各自登录，互不干扰）。", "")
bullet("定时发布：抖音/快手/小红书/视频号/B 站支持 --schedule，可把一周内容一次排好，按黄金时段自动发。", "")
bullet("视频 + 图文双形态：攻略号走图文笔记（小红书吃搜索），管家 IP 走真人视频（抖音/视频号养信任）。", "")
bullet("抖音挂商品链接：--product-link 直接对应“抖音本地生活团购”，把人从内容拉到下单核销。", "")

# ============ 三、底层逻辑 ============
h1("三、推广的底层逻辑：在客人刷手机的那一刻出现")
para("原方案的核心判断是：问题不是没客人，是没入口。自由行客人订房、订票、看攻略全在手机上完成，出发前就结束了，我们在“决策现场”缺席。")
para("所以推广的战场不在车站、不在景区门口，而在三个“刷手机”的决策现场：", bold=True)
bullet("出发前 7–15 天：刷抖音/小红书查“桂林怎么玩、会不会被坑” → 攻略号承接，加微信领行程表。")
bullet("到店当晚：刷手机想“晚上干嘛” → 演出票、夜游、夜宵的内容加购。")
bullet("离店/返程：还想再去哪 → 北海、涠洲岛、德天联程内容，私域复购。")
para("工具的价值：这三个现场同时发生、每天都发生、且分散在多个平台。靠人一条条发根本跟不上；靠 sau 的“多账号 + 定时 + 批量”，1 个人即可把 3 个号、多平台的内容矩阵跑起来。", bold=True, color=C_ACCENT)

# ============ 四、账号矩阵 ============
h1("四、账号矩阵规划（先想清楚，再登录）")
para("对应原方案动作 3 的“只开 3 个号”。不要贪多，先把 3 类账号跑通。")
table(
    ["账号类型", "平台", "定位", "内容形式", "频次"],
    [
        ["攻略号", "小红书 + 抖音", "吃搜索长尾，回答“怎么玩不踩坑”", "图文笔记 + 短视频", "每周 3 更"],
        ["管家 IP 号", "抖音 + 视频号", "真人出镜养信任（司兼导/目的地管家）", "短视频为主", "每周 1–2 更"],
        ["企业号", "抖音 + 视频号小店", "挂团购/商品，承接下单", "短视频 + 团购挂载", "每周 2 更 + 1 播(人工)"],
    ],
    widths=[1.3, 1.5, 2.4, 1.5, 1.1],
)
para("登录账号时，给每个平台账号起清晰的名字，例如：", italic=True, color=C_GRAY)
code([
    "sau xiaohongshu login --account gl_guide        # 攻略号（小红书）",
    "sau douyin     login --account gl_guide        # 攻略号（抖音）",
    "sau douyin     login --account gl_butler       # 管家 IP（抖音）",
    "sau tencent    login --account gl_shop         # 企业号（视频号）",
    "sau tencent    login --account gl_butler       # 管家 IP（视频号）",
])
para("说明：登录会弹二维码，扫码即存 cookie；二次复用无需重复登录。发布前先跑一次 check 确认账号有效。", color=C_GRAY)

# ============ 五、六个动作 → 推广任务映射 ============
h1("五、把 PDF 的 6 个动作翻译成推广任务（核心映射）")
para("这是本手册最核心的一张表：原方案的每个动作，对应“发什么内容、用哪台工具、挂什么链接”。")
table(
    ["原方案动作", "推广目标", "内容产品", "工具动作", "关键命令/参数"],
    [
        ["动作 1｜单品货架化", "每个单品都有人搜、有人买", "30 个单品各配一条种草（竹筏/包车/旅拍/演出…）", "图文+视频铺量", "小红书图文、抖音视频、--tags"],
        ["动作 2｜民宿前台柜台", "内容把客人引导到店/私域", "民宿实拍、避坑卡、到店动线", "攻略号种草", "小红书图文、加微信钩子文案"],
        ["动作 3｜内容前置", "出发前就认识我们", "攻略号 + 管家 IP 短视频", "矩阵批量定时发布", "--schedule 定时、多账号"],
        ["动作 4｜上平台当供应商", "让搜索能找到我们", "抖音团购 + 视频号小店挂品", "挂商品链接/合集", "douyin --product-link、tencent --collection"],
        ["动作 5｜B 端批量客", "写生/研学/团建/疗休养获客", "面向机构/工会的 toB 内容", "视频号+百家号", "tencent/baijiahao 发布、定向标题"],
        ["动作 6｜联程 + 司兼导", "拉长停留、做厚毛利", "北海/涠洲岛/德天联程种草", "攻略号 + 管家 IP", "行后追更联程内容、私域复购"],
    ],
    widths=[1.55, 1.7, 2.0, 1.35, 1.9],
)

# ============ 六、选题库 ============
h1("六、内容选题库（直接抄 PDF 里的搜索词）")
para("原方案已经点破了选题本质：全是搜索词。考核不看播放量，只看“加微信人数”和“加微信后 7 天成交率”。")
h3("6.1 攻略类选题（攻略号，吃搜索长尾）")
bullet("漓江竹筏到底选哪个码头")
bullet("阳朔住西街还是遇龙河")
bullet("桂林 3 天怎么排不累")
bullet("桂林旅游最容易被坑的 5 个地方")
bullet("两江四湖夜游值不值 / 怎么买票最划算")
h3("6.2 单品种草选题（每个单品一条）")
bullet("遇龙河竹筏：哪个码头不排队（避坑卡）")
bullet("印象·刘三姐：买哪个区位置最好")
bullet("桂林旅拍：哪家不宰客、底片给不给")
bullet("阳朔攀岩/骑行/桨板：给“为品类而来”的人")
h3("6.3 联程选题（拉长停留）")
bullet("桂林 2 天太短，加 1 天北海怎么走最顺")
bullet("涠洲岛 vs 德天：桂林返程顺路去哪")
bullet("龙脊梯田：住一晚看日出，值不值得绕路")

# ============ 七、命令实操 ============
h1("七、发布 SOP 与命令实操")
h3("7.1 安装与登录（一次性）")
code([
    "# 安装 CLI 入口（项目根目录，用 uv）",
    "uv pip install -e .",
    "",
    "# 安装浏览器内核（Windows，先指定镜像）",
    "$env:PLAYWRIGHT_DOWNLOAD_HOST=\"https://npmmirror.com/mirrors/playwright\"; patchright install chromium",
    "",
    "# 登录各平台（弹二维码扫码）",
    "sau douyin     login --account gl_guide",
    "sau kuaishou    login --account gl_guide",
    "sau xiaohongshu login --account gl_guide",
    "sau bilibili    login --account gl_guide",
    "sau tencent     login --account gl_shop",
    "",
    "# 发布前校验账号有效",
    "sau douyin check --account gl_guide",
])
h3("7.2 视频发布（攻略号 / 管家 IP）")
code([
    "# 抖音：攻略视频",
    "sau douyin upload-video --account gl_guide --file videos/lijiang_zhufa.mp4 --title \"漓江竹筏到底选哪个码头\" --desc \"本地司兼导实测：3 个码头怎么选，哪个不排队\" --tags 桂林,漓江,竹筏,旅游攻略",
    "",
    "# 视频号：企业号挂合集 + 定时",
    "sau tencent upload-video --account gl_shop --file videos/yanchu.mp4 --title \"印象刘三姐选哪个区最好\" --desc \"附接送+夜游套餐\" --tags 桂林,演出 --collection \"桂林竹筏演出\" --schedule \"2026-10-24 20:30\"",
    "",
    "# B 站（需 --tid 分区）",
    "sau bilibili upload-video --account gl_guide --file videos/gonglue.mp4 --title \"桂林3天怎么排不累\" --desc \"本地管家路线\" --tid 249 --tags 桂林,攻略",
])
h3("7.3 图文发布（小红书/抖音/快手，攻略号主战场）")
code([
    "# 小红书：图文攻略笔记",
    "sau xiaohongshu upload-note --account gl_guide --images videos/p1.png videos/p2.png videos/p3.png --title \"桂林3天怎么排不累（本地人版）\" --note \"D1 落地+两江四湖夜游；D2 遇龙河竹筏+西街；D3 龙脊梯田。评论区领完整行程表\" --tags 桂林,旅游攻略,自由行",
    "",
    "# 抖音：图文（可挂商品）",
    "sau douyin upload-note --account gl_guide --images videos/q1.png videos/q2.png --title \"遇龙河竹筏选哪个码头\" --note \"扫码即买，当天可核销\" --tags 桂林,竹筏",
    "",
    "# 快手：多图",
    "sau kuaishou upload-note --account gl_guide --images videos/k1.png videos/k2.png videos/k3.png --title \"桂林避坑指南\" --note \"本地人整理的5个坑\" --tags 桂林,避坑",
])
h3("7.4 定时发布 + 抖音团购挂载（动作 4 落地）")
code([
    "# 抖音：视频挂本地生活团购链接",
    "sau douyin upload-video --account gl_shop --file videos/zhufa.mp4 --title \"遇龙河竹筏 扫码即买\" --desc \"当天可核销\" --tags 桂林,竹筏 --product-link https://example.com/item --product-title \"遇龙河竹筏票\"",
    "",
    "# 定时：把一周内容一次排好",
    "sau douyin upload-video --account gl_guide --file videos/g1.mp4 --title \"龙脊梯田住一晚值不值\" --desc \"...\" --tags 桂林,龙脊 --schedule \"2026-10-25 19:00\"",
    "sau xiaohongshu upload-note --account gl_guide --images videos/r1.png --title \"桂林雨天怎么玩\" --note \"室内替代方案\" --tags 桂林,攻略 --schedule \"2026-10-26 12:00\"",
])
para("提示：直播（每周 1 播）工具不代跑，需人工；工具负责的是“每周 3 更”的图文/视频批量定时分发。", italic=True, color=C_GRAY)

# ============ 八、30 天日历 ============
h1("八、30 天内容日历（示例，落地节奏对齐 90 天计划）")
table(
    ["周次", "平台/账号", "内容类型", "选题（示例）", "发布方式"],
    [
        ["第 1 周", "小红书 gl_guide", "图文 ×3", "竹筏码头 / 桂林3天 / 避坑", "定时发布"],
        ["第 1 周", "抖音 gl_guide", "视频 ×2", "漓江竹筏实测 / 行程表领取", "--schedule"],
        ["第 2 周", "抖音 gl_butler", "视频 ×2", "管家出镜：接站+包车的一天", "定时发布"],
        ["第 2 周", "视频号 gl_shop", "视频 ×2", "演出选座 / 夜游套餐", "--collection"],
        ["第 3 周", "抖音 gl_shop", "视频+团购", "遇龙河竹筏挂商品链接", "--product-link"],
        ["第 3 周", "快手 gl_guide", "图文 ×2", "桂林避坑指南", "定时发布"],
        ["第 4 周", "小红书+抖音", "联程种草", "桂林+北海 / 涠洲岛", "定时发布"],
    ],
    widths=[0.9, 1.55, 1.05, 2.6, 1.3],
)
para("节奏目标：每周稳定“3 更 + 1 播（人工）”；第 1 个月跑通内容→加微信→成交闭环，第 2–3 个月逐步上量。", color=C_GRAY)

# ============ 九、数据闭环 ============
h1("九、数据闭环：不看播放量，看成交")
para("原方案第 8 章给了 6 个经营指标；内容侧只看 3 个数，直接对齐到工具产出的内容上：")
table(
    ["内容侧指标", "看什么", "对应经营指标", "谁负责"],
    [
        ["加微信人数", "每条内容带来的私信/加微信", "指标 3 微信新增", "内容运营"],
        ["7 天成交率", "加微信后 7 天内下单占比", "指标 5 自由行客单承接额", "客服"],
        ["团购核销数", "抖音/视频号团购核销单量", "指标 1 单品日订单数", "电商运营"],
    ],
    widths=[1.5, 2.2, 2.3, 1.2],
)
para("原方案铁律：只看播放量 = 死亡。视频几万播放没加一个微信，等于没发。每周一晨会 15 分钟，只报 6 个数，红灯只问一句“卡在哪个动作上”。", bold=True)

# ============ 十、红线与合规 ============
h1("十、红线与合规（内容侧）")
para("推广内容同样要守住原方案第 7 章的五条红线，只是换成内容口径：")
bullet("不低价内卷：不打“桂林 3 日 399”红海价，内容只推非标品（包车/竹筏/旅拍/研学）。")
bullet("不虚假承诺：文案、避坑卡、攻略必须真实可核验，不夸大“最好”“第一”。")
bullet("不站外揽客：不在车站/景区门口揽客，内容引导走预约制与民宿/演出场口合作柜台。")
bullet("私域合规：预收款专户管理，不挪用客户预付款；个人微信承接需话术合规，不诱导私下转账。")
bullet("内容版权：图文/视频素材自产或取得授权，不用他人版权素材批量搬运。")

# ============ 十一、第一周启动 ============
h1("十一、第一周就能做的三件事")
para("对齐原方案结尾的“第一周三件事”，加上工具落地：")
bullet("① 打 10 个电话问合作民宿：客人到店最常问哪 3 个问题 → 答案就是选题库和单品清单。", "")
bullet("② 让计调出《30 个单品成本-定价-毛利表》→ 有底价才有定价，有定价才能上架挂团购。", "")
bullet("③ 拍第一条视频《漓江竹筏到底选哪个码头》，不用精修，用 sau 立即发出去，先跑通“登录→上传→定时”全链路。", "")
para("")
para("—— 手册完 · 结合《桂林地接社破局方案》与 social-auto-upload 项目生成 ——", italic=True, color=C_GRAY)

doc.save(DOC_PATH)
print("SAVED:", DOC_PATH)
