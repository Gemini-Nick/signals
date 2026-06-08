# -*- coding: utf-8 -*-
"""Render screenshot-training replay samples from structured facts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REFERENCE_DIR = Path(__file__).resolve().parent / "references"


def load_training_facts(name: str) -> dict[str, Any]:
    path = Path(name)
    if not path.exists():
        path = REFERENCE_DIR / f"{name}-facts.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _join(items: list[str], sep: str = "，") -> str:
    return sep.join(items)


def render_training_sample(facts: dict[str, Any]) -> str:
    market = facts["market"]
    opening = facts["opening_flow"]
    aerospace = facts["commercial_aerospace"]
    turn_0933 = facts["turn_0933"]
    turn_1030 = facts["turn_1030"]
    robots = facts["robots"]
    collapse = facts["collapse_1330"]
    tail = facts["tail_emotion"]
    cycle = facts["cycle"]
    validations = facts["validations"]

    paragraphs = [
        facts["date_title"],
        (
            "今天市场的真实结构是：盘面看着热闹，尾盘一锅端。给你一个最直接的结论—今天除了"
            f"{market['strong_directions'][0]}和{market['strong_directions'][1]}两个方向维持了全天强度，"
            f"其他所有方向都在尾盘被{market['dragged_by']}拖下水了。"
            + "，".join(f"{name}跌{pct}" for name, pct in market["indices"])
            + "。但数字掩盖了真实的杀伤力—"
            f"{market['pressure_core']['name']}单日成交{market['pressure_core']['amount_yi']}，"
            f"{market['pressure_core']['rank']}，{market['pressure_core']['effect']}。"
        ),
        (
            "先说资金流动的完整链条。开盘后光模块方向直接低开—"
            + "，".join(opening["optical_low_open"])
            + "。科技方向开盘就弱，资金立刻去攻击了消费。"
            + "，".join(opening["consumer_attack"])
            + f"。{opening['consumer_context']}。"
        ),
        (
            "这时有一条暗线开始露头—商业航天。"
            f"航天装备板块全天涨了{aerospace['board_pct']}，是{aerospace['rank']}。"
            + "。".join(aerospace["stocks"])
            + "。这条线的关键特征是内部有先后节奏—"
            f"{aerospace['rhythm']}。这种分层节奏说明不是单一资金控盘，而是板块共识在扩散。"
        ),
        (
            "9点33分是第一个关键转折点。"
            f"{turn_0933['tech_pull']}。但拉升的那个瞬间，资金被吸引回科技方向。"
            "这时候电力方向开始跳水—"
            + "。".join(turn_0933["power"][:2])
            + "。"
            + "，".join(turn_0933["power"][2:])
            + f"。电力板块内部严重分化—{turn_0933['split']}。这种情况就是撤退信号。"
        ),
        (
            "10点半是第二个关键转折点。这个时候易中天方向停止回拉了—"
            f"{turn_1030['yi_zhong_tian']}。几乎同一时间，锂电和锂矿开始直线拉升。"
            + "，".join(turn_1030["lithium"])
            + f"。{turn_1030['rhythm']}。"
        ),
        (
            "下午机器人方向被资金平铺买入。"
            f"机器人执行器概念涨{robots['executor_pct']}，华为机器人概念涨{robots['huawei_pct']}。"
            "这个是前一两天的竞价异动的延续，但机器人的问题在于缺少一个旗帜性的涨停聚焦点，"
            "属于平铺买入但没有封板龙头。"
        ),
        (
            "下午1点30分是整个盘面的崩塌点。"
            f"{collapse['core']}从日内低点再次尝试拉升，但拉着拉着抛压越来越大，"
            f"{collapse['amount']}成交里几乎没有价格承接。"
            f"这不是成交量的问题—成交巨大说明有人在接，但接不住就是最大的问题。"
            f"{collapse['amount']}成交无承接，这个信号比单纯跌5%严重一个数量级。"
            f"主力全天买入{collapse['main_buy']}卖出{collapse['main_sell']}，净流出{collapse['main_net']}，"
            f"散户买入{collapse['retail_buy']}卖出{collapse['retail_sell']}净接了{collapse['retail_net']}—主力在卖给散户。"
        ),
        (
            "尾盘情绪彻底崩溃。"
            + "。".join(tail["failed"])
            + f"。但{tail['repair']}。分不清这个区别就会踩坑—{tail['difference']}。"
        ),
        (
            "看一下今天盘面的卡位结构。全天至少三次卡位："
            + "，".join(facts["carding"])
            + "。三次卡位，三个不同的资金流向切换，但没有任何一个方向形成了压倒性的胜势。"
            + "，".join(facts["carding_outcomes"])
            + "。这种互相卡位但没有明显胜方的行情，盘中最正确的策略就是不动。"
            "明天再跟胜方，今天别参与两方打架。"
        ),
        (
            "关于情绪温度，今天属于典型的情绪绞杀—不是一路跌，是给你希望再掐灭。"
            "开盘有机会，9点33分有机会，10点半有机会，下午有机会，每天都有一个方向看着要起来，"
            "但每个方向最终都被其他方向抽血或者自己崩掉。这种行情比直接跌三天更伤士气，"
            "因为每一次上涨都变成下一次下跌的冲锋号。尾盘炸板潮就是情绪崩溃的标识。"
        ),
        (
            "最后说一个重要的时间周期维度。"
            f"距离{cycle['high_date']}的高点{cycle['high_point']}已经过去了{cycle['days']}，"
            f"上证从{cycle['high_point']}跌到周五收盘{cycle['friday_close']}，跌了{cycle['drop']}。"
            f"但如果市场情绪进一步恶化，向下回补{cycle['gap']}附近缺口只需要一个低开加一次急跌。"
            "从结构上看，底部位置的个股跳水的动作有但跳不动了—这是企稳的微弱特征，"
            "需要周末仔细梳理板块结构才能确认。"
        ),
        (
            "明日几个验证点。"
            f"第一，{validations[0]}。"
            f"第二，{validations[1]}。"
            f"第三，{validations[2]}。"
            f"第四，{validations[3]}。"
        ),
        f"明天的核心问题是：{facts['core_question']}。",
    ]
    return "\n\n".join(paragraphs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render a replay training sample from structured facts.")
    parser.add_argument("sample")
    args = parser.parse_args(argv)
    print(render_training_sample(load_training_facts(args.sample)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
