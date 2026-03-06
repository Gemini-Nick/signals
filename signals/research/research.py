# -*- coding: utf-8 -*-
"""
研究笔记模块 — 将研究所小作文纳入投研流程

支持多格式导入：
- Markdown (.md)：YAML frontmatter + 正文
- PDF (.pdf)：pdfplumber 提取文本
- 图片 (.png/.jpg/.jpeg)：pytesseract OCR 提取文本
- 纯文本 (.txt)：直接读取
- YAML (.yaml/.yml)：纯结构化元数据（无正文）

工作流程：
1. 用户将原始文件放入 notes/ 目录
2. 运行 `python run.py --mode import --file notes/xxx.pdf`
   → 提取文本 → 自动识别行业/股票/观点 → 生成 .meta.yaml
3. 用户可手动修正 .meta.yaml
4. 盘中/复盘模式自动加载 notes/ 下所有 .meta.yaml，融入评分

集成方式（双维度独立，不混合评分）：
- 候选池：笔记中的行业自动加入扫描池、标的加入白名单
- 展示层：技术面分数 + 研报观点 并列展示，共振时标星
- 用户自行判断：系统只呈现两个维度，不替用户做加权决策
"""
import os
import re
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Tuple


# ─────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────

@dataclass
class ResearchNote:
    """
    一篇研究笔记的结构化表示。

    字段说明：
    - title:      笔记标题（如"锂电池行业深度"）
    - date:       笔记日期（YYYY-MM-DD）
    - source:     来源机构（如"中信证券"、"华泰研究所"、"个人研究"）
    - author:     作者/分析师（如"李小加"、"张三"）
    - sectors:    涉及行业（AKShare 行业名称，如["锂电池", "有色金属"]）
    - stocks:     提到的标的（Futu 格式，如["SZ.002460", "SH.601958"]）
    - sentiment:  整体观点（"看多" / "看空" / "中性"）
    - confidence: 置信度 0.0~1.0（越高加分越多）
    - catalysts:  核心催化剂/逻辑（简短描述列表）
    - raw_text:   原始提取文本（供参考，不参与评分）
    - file_path:  原始文件路径
    - meta_path:  生成的 .meta.yaml 路径
    """
    title: str = ""
    date: str = ""
    source: str = ""
    author: str = ""
    sectors: List[str] = field(default_factory=list)
    stocks: List[str] = field(default_factory=list)
    sentiment: str = "中性"           # "看多" / "看空" / "中性"
    confidence: float = 0.5
    catalysts: List[str] = field(default_factory=list)
    raw_text: str = ""
    file_path: str = ""
    meta_path: str = ""

    @property
    def is_bullish(self) -> bool:
        return self.sentiment == "看多"

    @property
    def is_bearish(self) -> bool:
        return self.sentiment == "看空"

    @property
    def is_valid(self) -> bool:
        """至少有行业或标的才算有效"""
        return bool(self.sectors or self.stocks)

    @property
    def is_expired(self) -> bool:
        """超过 30 天的笔记视为过期"""
        if not self.date:
            return False
        try:
            note_date = datetime.strptime(self.date, "%Y-%m-%d")
            return (datetime.now() - note_date) > timedelta(days=30)
        except ValueError:
            return False

    @property
    def age_days(self) -> int:
        """笔记距今天数"""
        if not self.date:
            return 0
        try:
            return (datetime.now() - datetime.strptime(self.date, "%Y-%m-%d")).days
        except ValueError:
            return 0

    @property
    def decay_factor(self) -> float:
        """
        时效衰减因子：7天内=1.0，之后线性衰减，30天后=0.2
        确保老笔记影响力递减但不归零。
        """
        days = self.age_days
        if days <= 7:
            return 1.0
        if days >= 30:
            return 0.2
        return 1.0 - 0.8 * (days - 7) / 23

    @property
    def source_label(self) -> str:
        """来源标签：机构+作者"""
        parts = []
        if self.source:
            parts.append(self.source)
        if self.author:
            parts.append(self.author)
        return "/".join(parts) if parts else "未知来源"

    def summary(self) -> str:
        """简短摘要"""
        parts = []
        if self.title:
            parts.append(self.title)
        parts.append(f"[{self.source_label}]")
        if self.sectors:
            parts.append(f"行业: {'、'.join(self.sectors)}")
        if self.stocks:
            parts.append(f"标的: {'、'.join(self.stocks[:5])}")
        parts.append(f"观点: {self.sentiment}")
        if self.catalysts:
            parts.append(f"催化: {'、'.join(self.catalysts[:3])}")
        return "  |  ".join(parts)


# ─────────────────────────────────────────────────────────
# 文本提取器（多格式）
# ─────────────────────────────────────────────────────────

def _extract_text_markdown(file_path: str) -> Tuple[str, dict]:
    """
    从 Markdown 文件提取文本和 YAML frontmatter。
    返回 (正文文本, frontmatter字典)
    """
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    frontmatter = {}
    body = content

    # 解析 YAML frontmatter（--- 包裹）
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
    if fm_match:
        try:
            frontmatter = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            pass
        body = fm_match.group(2)

    return body.strip(), frontmatter


def _extract_text_pdf(file_path: str) -> str:
    """从 PDF 文件提取文本。依赖 pdfplumber。"""
    try:
        import pdfplumber
    except ImportError:
        print("  [!] 需要安装 pdfplumber: pip install pdfplumber")
        return ""

    text_parts = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                text_parts.append(text)
    return "\n".join(text_parts)


def _extract_text_image(file_path: str) -> str:
    """
    从图片提取文本。
    优先使用 pytesseract OCR，若不可用则提示用户创建同名 .txt 文件。
    """
    # 方案 1：尝试 pytesseract OCR
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(file_path)
        text = pytesseract.image_to_string(img, lang="chi_sim+eng")
        if text.strip():
            return text.strip()
    except ImportError:
        pass
    except Exception as e:
        print(f"  [!] OCR 失败: {e}")

    # 方案 2：查找同名 .txt 伴随文件
    txt_path = Path(file_path).with_suffix(".txt")
    if txt_path.exists():
        with open(txt_path, "r", encoding="utf-8") as f:
            return f.read().strip()

    print(f"  [!] 图片 OCR 不可用，且未找到伴随文件 {txt_path.name}")
    print(f"      请安装 pytesseract (pip install pytesseract) + tesseract-ocr")
    print(f"      或手动创建 {txt_path.name} 写入文字内容")
    return ""


def _extract_text_plain(file_path: str) -> str:
    """直接读取纯文本文件。"""
    with open(file_path, "r", encoding="utf-8") as f:
        return f.read().strip()


# 扩展名 → 提取函数映射
_EXTRACTORS = {
    ".md":   lambda p: _extract_text_markdown(p)[0],
    ".pdf":  _extract_text_pdf,
    ".png":  _extract_text_image,
    ".jpg":  _extract_text_image,
    ".jpeg": _extract_text_image,
    ".txt":  _extract_text_plain,
}

_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


def extract_text(file_path: str) -> Tuple[str, dict]:
    """
    统一文本提取入口。
    返回 (提取文本, frontmatter元数据)。
    非 Markdown 文件 frontmatter 为空字典。
    """
    ext = Path(file_path).suffix.lower()

    if ext == ".md":
        return _extract_text_markdown(file_path)

    extractor = _EXTRACTORS.get(ext)
    if extractor is None:
        print(f"  [!] 不支持的文件格式: {ext}")
        return "", {}

    return extractor(file_path), {}


# ─────────────────────────────────────────────────────────
# 关键信息自动提取
# ─────────────────────────────────────────────────────────

# A 股代码模式：6位数字，可带市场前缀
_STOCK_CODE_RE = re.compile(
    r"(?:"
    r"(?:SH|SZ|BJ|sh|sz|bj)[.\s]?(\d{6})"   # SH.601958 / SZ002460
    r"|"
    r"(\d{6})[.\s](?:SH|SZ|BJ|sh|sz|bj)"     # 601958.SH
    r"|"
    r"(?<![0-9])([036]\d{5})(?![0-9])"         # 裸 6 位代码（0/3/6 开头）
    r")"
)

# 常见行业关键词 → AKShare 行业名称映射
_SECTOR_KEYWORDS: Dict[str, str] = {
    # 新能源
    "锂电": "锂电池", "锂电池": "锂电池", "碳酸锂": "锂电池",
    "磷酸铁锂": "锂电池", "三元锂": "锂电池", "正极材料": "锂电池",
    "负极材料": "锂电池", "电解液": "锂电池", "隔膜": "锂电池",
    "动力电池": "锂电池", "储能": "储能",
    "光伏": "光伏设备", "太阳能": "光伏设备", "组件": "光伏设备",
    "风电": "风电设备", "风力发电": "风电设备",
    "新能源车": "汽车整车", "电动车": "汽车整车", "新能源汽车": "汽车整车",
    # 半导体
    "半导体": "半导体", "芯片": "半导体", "晶圆": "半导体",
    "封装": "半导体", "EDA": "半导体", "光刻": "半导体",
    # AI / 科技
    "人工智能": "人工智能", "AI": "人工智能", "大模型": "人工智能",
    "算力": "人工智能", "GPU": "人工智能",
    "消费电子": "消费电子", "手机": "消费电子",
    # 医药
    "医药": "医药商业", "创新药": "化学制药", "中药": "中药",
    "医疗器械": "医疗器械", "CXO": "化学制药",
    # 消费
    "白酒": "白酒", "食品饮料": "食品饮料", "调味品": "食品饮料",
    "家电": "家用电器", "消费": "商业百货",
    # 金融地产
    "银行": "银行", "券商": "证券", "保险": "保险",
    "地产": "房地产开发", "房地产": "房地产开发",
    # 周期
    "有色金属": "有色金属", "铜": "有色金属", "铝": "有色金属",
    "黄金": "贵金属", "白银": "贵金属",
    "钢铁": "钢铁行业", "煤炭": "煤炭行业",
    "化工": "化工行业", "石油": "石油行业",
    # 其他
    "军工": "国防军工", "国防": "国防军工",
    "基建": "工程建设", "建筑": "工程建设",
    "传媒": "传媒", "游戏": "游戏",
    "农业": "农牧饲渔", "养殖": "农牧饲渔",
}

# 多空关键词
_BULLISH_KEYWORDS = [
    "看多", "看好", "推荐", "买入", "增持", "强推",
    "利好", "景气", "向上", "拐点", "反转", "底部",
    "供不应求", "涨价", "量价齐升", "高增长", "超预期",
    "催化", "受益", "龙头",
]
_BEARISH_KEYWORDS = [
    "看空", "看淡", "减持", "卖出", "回避", "谨慎",
    "利空", "下行", "过剩", "降价", "萎缩", "低于预期",
    "风险", "见顶", "泡沫",
]


def _normalize_stock_code(raw: str) -> Optional[str]:
    """将原始代码标准化为 Futu 格式（SH.600xxx / SZ.000xxx / SZ.300xxx）"""
    code = raw.zfill(6)
    if code.startswith("6"):
        return f"SH.{code}"
    elif code.startswith(("0", "3")):
        return f"SZ.{code}"
    elif code.startswith(("8", "4")):
        return f"BJ.{code}"
    return None


def auto_extract_info(text: str) -> dict:
    """
    从原始文本中自动提取结构化信息。

    返回字典：
    {
        "sectors":    ["锂电池", "有色金属"],
        "stocks":     ["SZ.002460", "SH.601958"],
        "sentiment":  "看多",
        "catalysts":  ["碳酸锂价格见底", "下游需求回暖"],
    }
    """
    result = {"sectors": [], "stocks": [], "sentiment": "中性", "catalysts": []}

    if not text:
        return result

    # 1. 提取股票代码
    seen_codes = set()
    for m in _STOCK_CODE_RE.finditer(text):
        raw = m.group(1) or m.group(2) or m.group(3)
        if raw:
            code = _normalize_stock_code(raw)
            if code and code not in seen_codes:
                seen_codes.add(code)
                result["stocks"].append(code)

    # 2. 识别行业
    seen_sectors = set()
    for keyword, sector in _SECTOR_KEYWORDS.items():
        if keyword in text and sector not in seen_sectors:
            seen_sectors.add(sector)
            result["sectors"].append(sector)

    # 3. 判断多空
    bull_count = sum(1 for kw in _BULLISH_KEYWORDS if kw in text)
    bear_count = sum(1 for kw in _BEARISH_KEYWORDS if kw in text)
    if bull_count > bear_count + 2:
        result["sentiment"] = "看多"
    elif bear_count > bull_count + 2:
        result["sentiment"] = "看空"
    else:
        result["sentiment"] = "中性"

    # 4. 提取催化剂（简单：以句号分割，取包含关键词的短句）
    catalyst_keywords = ["催化", "逻辑", "核心", "驱动", "拐点", "景气", "供需",
                         "涨价", "降价", "政策", "需求", "产能"]
    sentences = re.split(r"[。！\n]", text)
    for sent in sentences:
        sent = sent.strip()
        if 10 < len(sent) < 80:
            if any(kw in sent for kw in catalyst_keywords):
                result["catalysts"].append(sent)
                if len(result["catalysts"]) >= 5:
                    break

    return result


# ─────────────────────────────────────────────────────────
# Meta YAML 读写
# ─────────────────────────────────────────────────────────

def _meta_path_for(file_path: str) -> str:
    """给定原始文件路径，返回对应的 .meta.yaml 路径"""
    p = Path(file_path)
    return str(p.with_suffix(".meta.yaml"))


def save_meta(note: ResearchNote, meta_path: str = None):
    """将 ResearchNote 保存为 .meta.yaml 文件"""
    path = meta_path or note.meta_path
    data = {
        "title": note.title,
        "date": note.date,
        "source": note.source,
        "author": note.author,
        "sectors": note.sectors,
        "stocks": note.stocks,
        "sentiment": note.sentiment,
        "confidence": note.confidence,
        "catalysts": note.catalysts,
        "file_path": note.file_path,
    }
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    print(f"  已保存元数据: {path}")


def load_meta(meta_path: str) -> ResearchNote:
    """从 .meta.yaml 文件加载 ResearchNote"""
    with open(meta_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return ResearchNote(
        title=data.get("title", ""),
        date=str(data.get("date", "")),  # YAML 可能解析为 datetime.date
        source=data.get("source", ""),
        author=data.get("author", ""),
        sectors=data.get("sectors", []),
        stocks=data.get("stocks", []),
        sentiment=data.get("sentiment", "中性"),
        confidence=data.get("confidence", 0.5),
        catalysts=data.get("catalysts", []),
        file_path=data.get("file_path", ""),
        meta_path=meta_path,
    )


# ─────────────────────────────────────────────────────────
# 导入：原始文件 → ResearchNote + .meta.yaml
# ─────────────────────────────────────────────────────────

def import_note(file_path: str, title: str = "", source: str = "", author: str = "") -> ResearchNote:
    """
    导入一篇研究笔记：
    1. 提取文本
    2. 自动识别行业/标的/观点
    3. 生成 .meta.yaml
    4. 返回 ResearchNote

    如果已有 .meta.yaml，则加载已有数据并跳过重新提取。
    """
    file_path = str(Path(file_path).resolve())
    meta_path = _meta_path_for(file_path)

    # 已有元数据文件则直接加载
    if os.path.exists(meta_path):
        print(f"  已有元数据文件，直接加载: {meta_path}")
        return load_meta(meta_path)

    ext = Path(file_path).suffix.lower()
    print(f"  导入: {file_path} (格式: {ext})")

    # 提取文本
    text, frontmatter = extract_text(file_path)
    if not text and ext not in (".yaml", ".yml"):
        print(f"  [!] 未能提取到文本内容")

    # 自动提取关键信息
    info = auto_extract_info(text)

    # 构建 ResearchNote（frontmatter 优先级高于自动提取）
    today = datetime.now().strftime("%Y-%m-%d")
    note = ResearchNote(
        title=frontmatter.get("title", title) or Path(file_path).stem,
        date=frontmatter.get("date", today),
        source=frontmatter.get("source", source),
        author=frontmatter.get("author", author),
        sectors=frontmatter.get("sectors", info["sectors"]),
        stocks=frontmatter.get("stocks", info["stocks"]),
        sentiment=frontmatter.get("sentiment", info["sentiment"]),
        confidence=frontmatter.get("confidence", 0.5),
        catalysts=frontmatter.get("catalysts", info["catalysts"]),
        raw_text=text[:2000],  # 截断，仅供参考
        file_path=file_path,
        meta_path=meta_path,
    )

    # 保存元数据
    save_meta(note, meta_path)
    return note


# ─────────────────────────────────────────────────────────
# 批量加载：从 notes/ 目录加载所有有效笔记
# ─────────────────────────────────────────────────────────

def load_all_notes(notes_dir: str) -> List[ResearchNote]:
    """
    递归加载 notes/ 下所有 .meta.yaml（支持 notes/YYYY/MM/ 子目录结构）。
    过滤掉过期和无效的笔记。
    """
    notes_path = Path(notes_dir)
    if not notes_path.exists():
        return []

    notes = []
    for meta_file in sorted(notes_path.rglob("*.meta.yaml")):
        try:
            note = load_meta(str(meta_file))
            if note.is_valid and not note.is_expired:
                notes.append(note)
            elif note.is_expired:
                print(f"  [跳过] {meta_file.name} — 已过期({note.age_days}天)")
        except Exception as e:
            print(f"  [!] 加载 {meta_file.name} 失败: {e}")

    return notes


# ─────────────────────────────────────────────────────────
# 双维度集成：研报维度独立于技术面维度
#
# 设计原则：
# - 不修改技术面分数，两个维度独立展示
# - 研报的价值 = 提供候选池 + 提供观点参考
# - 用"共振标记"表示技术面和研报方向一致
# ─────────────────────────────────────────────────────────

@dataclass
class NoteView:
    """某只标的在研报维度的视图"""
    symbol: str
    sentiment: str = "无覆盖"      # "看多" / "看空" / "中性" / "无覆盖"
    sources: List[str] = field(default_factory=list)  # 来源笔记标题
    catalysts: List[str] = field(default_factory=list)

    @property
    def has_coverage(self) -> bool:
        return self.sentiment != "无覆盖"

    @property
    def label(self) -> str:
        """简短标签，用于输出"""
        if not self.has_coverage:
            return ""
        src = self.sources[0] if self.sources else ""
        return f"{self.sentiment}({src})"


def match_notes_for_symbol(symbol: str, notes: List[ResearchNote]) -> NoteView:
    """
    查找某只标的在研究笔记中的覆盖情况。
    多篇笔记取最近一篇的观点。
    """
    view = NoteView(symbol=symbol)

    matching = [n for n in notes if symbol in n.stocks]
    if not matching:
        # 也检查行业覆盖（标的属于被研报覆盖的行业）
        return view

    # 按日期降序，取最近的观点
    matching.sort(key=lambda n: n.date, reverse=True)
    latest = matching[0]
    view.sentiment = latest.sentiment
    view.sources = [n.title for n in matching]
    view.catalysts = latest.catalysts[:3]
    return view


def check_resonance(tech_score: float, note_view: NoteView) -> str:
    """
    检查技术面和研报是否共振。

    返回标记：
    - "★共振" ：技术面买点 + 研报看多
    - "⚠冲突" ：技术面买点 + 研报看空（或反向）
    - ""       ：无覆盖或无信号
    """
    if not note_view.has_coverage:
        return ""

    tech_bullish = tech_score > 0
    tech_bearish = tech_score < 0

    if tech_bullish and note_view.sentiment == "看多":
        return "★共振"
    if tech_bearish and note_view.sentiment == "看空":
        return "★共振"
    if tech_bullish and note_view.sentiment == "看空":
        return "⚠冲突"
    if tech_bearish and note_view.sentiment == "看多":
        return "⚠冲突"
    return ""


def get_industry_sentiment(industry: str, notes: List[ResearchNote]) -> str:
    """
    获取某行业在研报中的总体观点。
    返回 "看多" / "看空" / "中性" / "无覆盖"
    """
    matching = [n for n in notes if industry in n.sectors]
    if not matching:
        return "无覆盖"
    # 取最近一篇
    matching.sort(key=lambda n: n.date, reverse=True)
    return matching[0].sentiment


def get_noted_industries(notes: List[ResearchNote]) -> List[str]:
    """从所有有效笔记中提取涉及的行业列表（去重）"""
    sectors = []
    seen = set()
    for note in notes:
        for s in note.sectors:
            if s not in seen:
                seen.add(s)
                sectors.append(s)
    return sectors


def get_noted_stocks(notes: List[ResearchNote]) -> List[str]:
    """从所有有效笔记中提取涉及的标的列表（去重）"""
    stocks = []
    seen = set()
    for note in notes:
        for s in note.stocks:
            if s not in seen:
                seen.add(s)
                stocks.append(s)
    return stocks


# ─────────────────────────────────────────────────────────
# 打印报告
# ─────────────────────────────────────────────────────────

def print_notes_summary(notes: List[ResearchNote]):
    """打印当前加载的研究笔记摘要"""
    if not notes:
        print("  📝 无有效研究笔记")
        return

    print(f"\n{'─'*50}")
    print(f"  📝 研究笔记（{len(notes)} 篇有效）")
    print(f"{'─'*50}")
    for note in notes:
        decay = f"衰减={note.decay_factor:.1f}" if note.decay_factor < 1.0 else "新鲜"
        print(f"  {note.date}  {note.title}  [{note.source_label}]")
        print(f"    行业: {'、'.join(note.sectors) or '未识别'}")
        print(f"    标的: {'、'.join(note.stocks[:5]) or '未识别'}")
        print(f"    观点: {note.sentiment}  置信度: {note.confidence}  {decay}")
        if note.catalysts:
            print(f"    催化: {'、'.join(note.catalysts[:2])}")
    print(f"{'─'*50}")

    # 汇总
    all_sectors = get_noted_industries(notes)
    all_stocks = get_noted_stocks(notes)
    if all_sectors:
        print(f"  → 涉及行业: {'、'.join(all_sectors)}")
    if all_stocks:
        print(f"  → 涉及标的: {'、'.join(all_stocks[:10])}")
    print()
