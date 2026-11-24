"""capflow：供应商产能回报采集、运营复核与订单分配。

模块划分：

- ``models``       领域数据结构与状态枚举
- ``errors``       采集过程中可恢复的领域错误
- ``repository``   SQLite 持久化（回执、版本、审计、检查点、计划快照）
- ``collectors``   导出接口连接器与可审计 RPA 驱动
- ``operations``   运营复核与有效产能计算
- ``allocation``   订单分配引擎
- ``planning``     方案快照与新旧方案对比
- ``cli``          命令行入口
"""

__version__ = "1.0.0"
