# -*- coding: utf-8 -*-
"""日历数据有效性检测 — 每日定时运行，确保节假日/交易时段数据未过期"""

import logging

logger = logging.getLogger("signals.sync.calendar_validate")


def sync_calendar_validate() -> dict:
    """验证交易日历数据覆盖率，在盘前给出预警。"""
    try:
        from signals.core.calendar.engine import get_calendar
        cal = get_calendar()
        info = cal.validate()
        if info["warnings"]:
            for w in info["warnings"]:
                logger.warning("⚠️ 日历过期风险: %s", w)
        else:
            logger.info(
                "✅ 日历覆盖至 %s (%d天后), %d holidays, %d schedules, %d exchanges",
                info["coverage_end"], info["days_remaining"],
                info["holiday_count"], info["sessions_loaded"], info["exchange_count"],
            )
        return {"status": "ok", **info}
    except Exception as e:
        logger.error("日历验证失败: %s", e)
        return {"status": "error", "error": str(e)}
