# -*- coding: utf-8 -*-
"""
深度社交爬虫框架

提供股吧/贴吧等社交平台的帖子数量、互动量、标题情绪等数据。
"""
from signals.data.scrapers.base import BaseScraper, ScrapedPost
from signals.data.scrapers.tieba import TiebaScraper
