"""采集环节的领域错误。

这类错误都意味着该供应商的流程必须暂停并保留可恢复位置，
而不是让整轮采集失败或盲目重试提交。
"""
from __future__ import annotations


class CapflowError(Exception):
    """项目内所有可预期错误的基类。"""


class CollectionError(CapflowError):
    """采集故障基类，携带供应商与恢复提示。"""

    def __init__(self, supplier: str, message: str, instruction: str = ""):
        super().__init__(message)
        self.supplier = supplier
        self.instruction = instruction or "请检查门户状态后从检查点恢复，勿重复提交。"


class LoginExpired(CollectionError):
    """登录态过期或被门户拒绝。"""

    def __init__(self, supplier: str, source_ref: str = ""):
        super().__init__(
            supplier,
            f"供应商 {supplier} 登录态过期（{source_ref}）",
            "重新完成授权登录后，从当前检查点恢复；已完成的提交不会重放。",
        )


class PageStructureChanged(CollectionError):
    """页面结构或导出契约变化，预期元素/字段缺失。"""

    def __init__(self, supplier: str, missing: str, source_ref: str = ""):
        self.missing = missing
        super().__init__(
            supplier,
            f"供应商 {supplier} 页面结构变化，缺少 {missing}（{source_ref}）",
            "更新选择器或导出字段映射后，从故障步骤恢复该供应商流程。",
        )


class SubmissionAmbiguous(CollectionError):
    """提交已发出但结果不明确，必须查询确认而非重新提交。"""

    def __init__(self, supplier: str, idempotency_key: str):
        self.idempotency_key = idempotency_key
        super().__init__(
            supplier,
            f"供应商 {supplier} 提交结果不明确，幂等键 {idempotency_key}",
            "恢复时只允许凭幂等键查询提交状态，禁止再次填写并提交表单。",
        )


class ConnectorConfigError(CapflowError):
    """连接器配置缺失或不合法。"""
