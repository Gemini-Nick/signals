# -*- coding: utf-8 -*-
"""
产业链映射 — 20 条核心产业链 + 上中下游定位

用途:
1. 个股分析时标注产业链位置
2. 主题追踪时关联上下游标的
3. 轮动分析时判断产业链传导方向
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ChainNode:
    """产业链节点"""
    position: str       # "上游" / "中游" / "下游"
    role: str           # 简要角色描述
    symbols: List[str] = field(default_factory=list)  # 代表标的代码


@dataclass
class ChainPosition:
    """个股在产业链中的位置"""
    chain_name: str     # 所属产业链名称
    position: str       # "上游" / "中游" / "下游"
    role: str           # 角色描述
    related_chains: List[str] = field(default_factory=list)  # 关联产业链


# ── 20 条核心产业链定义 ──────────────────────────────
CHAIN_MAP: Dict[str, Dict[str, ChainNode]] = {
    "算力/AI": {
        "上游": ChainNode("上游", "芯片/GPU/光模块", ["603019", "002281", "300502"]),
        "中游": ChainNode("中游", "服务器/IDC/云计算", ["000977", "603236", "300738"]),
        "下游": ChainNode("下游", "AI应用/大模型/AIGC", ["300433", "002230", "688111"]),
    },
    "昇腾/华为": {
        "上游": ChainNode("上游", "芯片设计/IP核", ["688256", "688521"]),
        "中游": ChainNode("中游", "整机/服务器/操作系统", ["000977", "600756"]),
        "下游": ChainNode("下游", "行业应用/解决方案", ["002415", "300496"]),
    },
    "机器人": {
        "上游": ChainNode("上游", "减速器/伺服/传感器", ["002472", "688339", "300124"]),
        "中游": ChainNode("中游", "本体制造/控制系统", ["300024", "002747", "688169"]),
        "下游": ChainNode("下游", "系统集成/应用场景", ["002527", "300607"]),
    },
    "新能源车": {
        "上游": ChainNode("上游", "锂矿/钴/正极材料", ["002460", "300750", "002371"]),
        "中游": ChainNode("中游", "电池/电机/电控", ["300750", "002074", "300014"]),
        "下游": ChainNode("下游", "整车/充电桩/运营", ["002594", "601238", "300001"]),
    },
    "光伏": {
        "上游": ChainNode("上游", "多晶硅/硅片", ["601012", "600438", "002129"]),
        "中游": ChainNode("中游", "电池片/组件", ["002459", "601615", "688223"]),
        "下游": ChainNode("下游", "电站/逆变器/EPC", ["300274", "688390", "601877"]),
    },
    "储能": {
        "上游": ChainNode("上游", "电芯/材料/BMS", ["300750", "002709", "300207"]),
        "中游": ChainNode("中游", "储能系统集成", ["300014", "688390"]),
        "下游": ChainNode("下游", "电力调度/用户侧", ["601877", "600905"]),
    },
    "风电": {
        "上游": ChainNode("上游", "叶片/轴承/铸件", ["002202", "688100", "603355"]),
        "中游": ChainNode("中游", "整机/齿轮箱", ["601016", "600875"]),
        "下游": ChainNode("下游", "风电场运营", ["601016", "600905"]),
    },
    "半导体": {
        "上游": ChainNode("上游", "设备/材料/EDA", ["002371", "688981", "688041"]),
        "中游": ChainNode("中游", "晶圆代工/封测", ["688981", "600584", "002156"]),
        "下游": ChainNode("下游", "芯片设计/应用", ["603019", "688521", "300782"]),
    },
    "消费电子": {
        "上游": ChainNode("上游", "面板/芯片/被动元件", ["000725", "600183", "603160"]),
        "中游": ChainNode("中游", "模组/结构件/代工", ["002475", "002241"]),
        "下游": ChainNode("下游", "品牌/渠道/应用", ["000651", "600690"]),
    },
    "医药/创新药": {
        "上游": ChainNode("上游", "原料药/CRO/CDMO", ["300759", "603259", "300347"]),
        "中游": ChainNode("中游", "创新药研发/器械", ["600276", "300760", "300003"]),
        "下游": ChainNode("下游", "流通/零售/医疗服务", ["601607", "603939"]),
    },
    "白酒": {
        "上游": ChainNode("上游", "粮食/包装/原材料", []),
        "中游": ChainNode("中游", "酿造/品牌", ["600519", "000858", "000568"]),
        "下游": ChainNode("下游", "经销/零售/电商", []),
    },
    "军工": {
        "上游": ChainNode("上游", "材料/元器件", ["002179", "688122"]),
        "中游": ChainNode("中游", "分系统/配套", ["600893", "000768"]),
        "下游": ChainNode("下游", "总装/主机厂", ["600760", "601989", "600118"]),
    },
    "汽车零部件": {
        "上游": ChainNode("上游", "原材料/铸件", ["600507", "002126"]),
        "中游": ChainNode("中游", "零部件/系统", ["601799", "002920", "603305"]),
        "下游": ChainNode("下游", "整车/4S/后市场", ["601238", "600104"]),
    },
    "数据要素": {
        "上游": ChainNode("上游", "数据采集/标注", ["300229", "300766"]),
        "中游": ChainNode("中游", "数据交易/治理", ["600845", "300378"]),
        "下游": ChainNode("下游", "数据应用/分析", ["002230", "300496"]),
    },
    "低空经济": {
        "上游": ChainNode("上游", "电机/飞控/材料", ["002097", "688169"]),
        "中游": ChainNode("中游", "eVTOL/无人机制造", ["002547", "688007"]),
        "下游": ChainNode("下游", "运营/空管/基建", ["600115", "688665"]),
    },
    "卫星互联网": {
        "上游": ChainNode("上游", "卫星制造/火箭", ["600118", "600879"]),
        "中游": ChainNode("中游", "地面设备/终端", ["002025", "300101"]),
        "下游": ChainNode("下游", "通信服务/应用", ["600640", "002115"]),
    },
    "智能驾驶": {
        "上游": ChainNode("上游", "激光雷达/芯片/传感器", ["300496", "688256"]),
        "中游": ChainNode("中游", "算法/系统集成", ["002405", "601127"]),
        "下游": ChainNode("下游", "运营/出行服务", ["601238", "600104"]),
    },
    "钢铁/基建": {
        "上游": ChainNode("上游", "铁矿/焦煤", ["000898", "600188"]),
        "中游": ChainNode("中游", "钢铁冶炼", ["600019", "000709", "600010"]),
        "下游": ChainNode("下游", "基建/地产/制造", ["601668", "601390"]),
    },
    "有色金属": {
        "上游": ChainNode("上游", "矿产资源/采选", ["601899", "603993", "000630"]),
        "中游": ChainNode("中游", "冶炼/加工", ["601600", "002460"]),
        "下游": ChainNode("下游", "合金/材料应用", ["600456", "600219"]),
    },
    "游戏/传媒": {
        "上游": ChainNode("上游", "IP/内容创作", ["603444", "300251"]),
        "中游": ChainNode("中游", "研发/发行", ["002602", "300418", "002555"]),
        "下游": ChainNode("下游", "渠道/平台/直播", ["300413", "002624"]),
    },
}

# ── 反向索引: symbol → (chain_name, position, role) ──
_SYMBOL_INDEX: Dict[str, List[ChainPosition]] = {}


def _build_index():
    """构建反向索引（首次调用时懒加载）"""
    if _SYMBOL_INDEX:
        return
    for chain_name, nodes in CHAIN_MAP.items():
        for pos_key, node in nodes.items():
            for sym in node.symbols:
                if sym not in _SYMBOL_INDEX:
                    _SYMBOL_INDEX[sym] = []
                _SYMBOL_INDEX[sym].append(ChainPosition(
                    chain_name=chain_name,
                    position=node.position,
                    role=node.role,
                ))


def get_chain_position(symbol: str) -> Optional[ChainPosition]:
    """
    查询个股所属产业链及位置。

    :param symbol: 股票代码（纯数字，如 "300750"，或含市场前缀 "SZ.300750"）
    :return: ChainPosition 或 None
    """
    _build_index()
    # 清理代码格式
    clean = symbol.split(".")[-1] if "." in symbol else symbol
    positions = _SYMBOL_INDEX.get(clean)
    if positions:
        return positions[0]  # 返回第一个匹配
    return None


def get_all_chain_positions(symbol: str) -> List[ChainPosition]:
    """查询个股所有关联的产业链位置（可能属于多条链）"""
    _build_index()
    clean = symbol.split(".")[-1] if "." in symbol else symbol
    return _SYMBOL_INDEX.get(clean, [])


def get_chain(chain_name: str) -> Optional[Dict[str, ChainNode]]:
    """获取完整产业链定义"""
    return CHAIN_MAP.get(chain_name)


def list_chains() -> List[str]:
    """列出所有产业链名称"""
    return list(CHAIN_MAP.keys())


def get_chain_symbols(chain_name: str, position: str = None) -> List[str]:
    """获取产业链中某位置（或所有位置）的代表标的"""
    chain = CHAIN_MAP.get(chain_name)
    if not chain:
        return []
    symbols = []
    for pos_key, node in chain.items():
        if position is None or node.position == position:
            symbols.extend(node.symbols)
    return symbols
