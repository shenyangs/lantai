---
# id: lantai-web-data (migrated to name)
name: lantai-web-data
description: 澜台 Web 与数据查询，负责网页读取、联网搜索、行情、公司与股票研究、天气和 IP 归属地等轻量事实核查。用户需要最新公开数据、指定 URL 内容、股票行情或公司基本面/技术面快照时使用；需要多轮证据综合时交给 desk-research。
kind: vertical_tool
domain: research_runtime
version: 1.0.0
source: builtin
enabled: true
locked: false
permission_profile: network_read_with_citation
recommended_agents:
- coconut
- latte
inputs:
- query
- url
- location
- ticker
- ip_address
outputs:
- sourced_summary
- data_snapshot
- citation_list
memory_writeback:
- reliable_source
- data_boundary
tags:
- 澜台内置
- Web
- 数据查询
---

# 澜台 Web 与数据查询

适用场景：需要抓取网页、网络搜索、查询行情、天气、IP 归属地或做轻量数据核查时触发。
输出格式：来源标注 + 数据快照 + 待核边界

## 澜台 工具映射
- `web_fetch`：抓取指定 URL 的网页内容。
- `web_search`：通过搜索引擎查询最新信息。
- `stock_price`：实时股价和历史 K 线。
- `stock_analysis`：SAR/MACD/KDJ/RSI/BOLL/EMA 等技术指标分析。
- `weather`：当前天气和多日预报。
- `ip_location`：查询 IP 地址归属城市、经纬度和 ISP。

## 基线与增强层级

- `web_search` / `web_fetch` 是澜台自身的基础 Web 能力，不依赖观澜或任何 MCP。
- 观澜可作为官方增强层，提供中文互联网召回、信源路由、质量审查和研究证据组织，但不取代基线工具。
- 观澜搜索不可用、超时、报错或结果不足时，降级到 `web_search`；已知 URL 的读取失败时，降级到 `web_fetch`。
- `web_fetch` 会区分“HTTP 成功”和“正文可用”。当它返回 `retry_recommended=true` 且 `recommended_route=guanlan_read` 时，在宣告页面不可读前恰好调用一次 `guanlan_read`；同一 URL 在同一后端最多尝试一次，并保留已有 partial 证据。
- `recommended_route=jina_reader` 由 `web_fetch` 内部完成一次抽取修复；`recommended_route=browser` 表示页面需要浏览器可见态、登录或用户授权，不能继续盲目抓取。
- 任何单一增强集成失败都不等于“公网没有资料”。

## 股票与公司研究

1. 先确认交易所、代码、公司实体、币种、时区和数据截止时间。
2. `stock_price` 只回答行情和历史价格；`stock_analysis` 用于技术指标，不能替代公司基本面。
3. 基本面研究使用公开财报、监管披露、公司公告和可核对的行业资料，分开历史事实、管理层指引和分析推断。
4. 比较公司时统一会计期间、币种、一次性项目和指标定义。
5. 输出至少包含数据时间、来源、计算口径、主要风险和“非投资建议”提示。
6. 不根据单一技术指标给买卖指令，不编造目标价或实时数据。

## WorkflowGraph 节点
- classify
- route_search_backend
- retrieve_sources
- normalize_data
- cite
- risk_note

## 权限规则
- 网络读取可开关。
- 股票分析必须声明数据来源和非投资建议。
- IP 查询不得用于个人追踪或敏感识别。

## 失败模式
- 无可靠来源时只输出待核方向。
- 时间敏感信息必须保留查询时间。
