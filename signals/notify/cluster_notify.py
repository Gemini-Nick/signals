# -*- coding: utf-8 -*-
"""行业聚类结果 → 飞书卡片推送"""
import logging

logger = logging.getLogger(__name__)


def _build_card(result: dict) -> dict:
    """将聚类结果构建为飞书交互式卡片 JSON。"""
    meta = result.get("meta", {})
    top = result.get("top", [])
    date_str = meta.get("date", "")
    source = meta.get("source", "")

    # 标题
    header = {
        "template": "turquoise",
        "title": {"tag": "plain_text", "content": f"行业聚类 Top {len(top)} — {date_str}"},
    }

    elements = []

    # 元信息
    info_parts = []
    if source:
        info_parts.append(f"数据源: {source}")
    if meta.get("total_boards"):
        info_parts.append(f"{meta['total_boards']} 板块")
    if meta.get("n_clusters"):
        info_parts.append(f"{meta['n_clusters']} 簇")
    if meta.get("features"):
        info_parts.append(f"{len(meta['features'])}D 特征")

    if info_parts:
        elements.append({
            "tag": "markdown",
            "content": " | ".join(info_parts),
        })

    elements.append({"tag": "hr"})

    # Top N 聚类
    for i, cluster in enumerate(top):
        gain_sign = "+" if cluster["avg_gain"] >= 0 else ""
        breadth_pct = round(cluster["avg_breadth"] * 100)

        # 标题行
        elements.append({
            "tag": "markdown",
            "content": (
                f"**#{i + 1} {cluster['label']}**  "
                f"综合 {round(cluster['score'] * 100)}"
            ),
        })

        # 指标行
        elements.append({
            "tag": "markdown",
            "content": (
                f"涨幅 {gain_sign}{cluster['avg_gain']}%  |  "
                f"广度 {breadth_pct}%  |  "
                f"换手 {cluster['avg_turnover']}%  |  "
                f"{cluster['size']} 板块"
            ),
        })

        # 成员（前 5）
        members = cluster.get("members", [])[:5]
        if members:
            member_strs = []
            for m in members:
                ms = "+" if m["gain_pct"] >= 0 else ""
                leader = f"({m.get('leader', '')})" if m.get("leader") else ""
                member_strs.append(f"{m['name']} {ms}{m['gain_pct']}% {leader}")
            elements.append({
                "tag": "markdown",
                "content": "  ".join(member_strs),
            })

        if i < len(top) - 1:
            elements.append({"tag": "hr"})

    return {"header": header, "elements": elements}


def push_cluster_result(result: dict = None):
    """
    推送聚类结果到飞书。

    :param result: 聚类结果 dict，为 None 时自动执行聚类。
    """
    from signals.notify import send_card

    if result is None:
        from signals.core.clustering import cluster_industries
        result = cluster_industries(top_n=3)

    top = result.get("top", [])
    if not top:
        logger.warning("聚类无结果，跳过推送")
        return

    card = _build_card(result)
    send_card(card)
    logger.info("聚类结果已推送飞书 (Top %d)", len(top))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    push_cluster_result()
