# 注册平台适配器

在平台自己的模块实现适配器，并在 `platforms.py` 的 `registry` 注册；重复 ID 会被拒绝。`description` 描述指标、单位、地区、来源与支持的 `modules`，用于 `/api/v1/platforms`。目前关联词和画像只支持抖音，其他平台只接入趋势；占位平台设为不可用。

适配器负责查询校验、结果投影、存储及读取、CSV、worker、暂停与恢复，以及桥接命令；可参考 `Douyin` 和离线合成平台测试。需要私有表时在 `initialize(store)` 创建；可通过 `interrupted`、`repoint_dataset`、`status_counts` 处理重启、数据集去重与统计，通过 `async_status`、`mount_routes`、`openapi_paths` 和 `bridge_commands` 接入状态、路由和浏览器操作。平台独立处理预算和认证，不记录或传递凭据，不自动重试。

使用合成数据测试校验、缺失、存储、预算和桥接，并运行现有平台的离线回归。真实登录和页面数据须单独验证。
