"""供应商授权的导出接口连接器。

传输层通过 :class:`Transport` 协议注入，生产环境可替换为带鉴权的 HTTP 客户端；
解析层严格校验门户契约，字段缺失即判定页面结构变化并暂停该供应商流程。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Protocol

from ..errors import PageStructureChanged
from ..models import CollectedRow, CollectionBatch, ReceiptDraft

REQUIRED_ENTRY_FIELDS = {"reported_as", "report_kind", "quantity", "revision"}
VALID_KINDS = {"product", "process"}


class Transport(Protocol):
    """导出数据传输协议。实现方负责鉴权与拉取原始字节。"""

    def fetch(self, source_ref: str) -> str:
        """返回导出接口的原始文本；登录失效时应抛出 LoginExpired。"""


class FileTransport:
    """从本地文件读取导出内容（演练与测试用）。"""

    def fetch(self, source_ref: str) -> str:
        return Path(source_ref).read_text(encoding="utf-8")


class JSONExportConnector:
    """解析门户 JSON 导出的产能回报。"""

    def __init__(self, supplier: str, source_ref: str, transport: Transport | None = None):
        self.supplier = supplier
        self.source_ref = source_ref
        self.transport = transport or FileTransport()
        self.kind = "export-json"

    def collect(self) -> CollectionBatch:
        raw = self.transport.fetch(self.source_ref)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PageStructureChanged(
                self.supplier, f"合法 JSON（解析失败：{exc}）", self.source_ref
            ) from exc

        entries = payload.get("entries")
        if not isinstance(entries, list) or not entries:
            raise PageStructureChanged(self.supplier, "entries 列表", self.source_ref)

        rows: list[CollectedRow] = []
        for index, entry in enumerate(entries):
            missing = REQUIRED_ENTRY_FIELDS - set(entry)
            if missing:
                raise PageStructureChanged(
                    self.supplier, f"entries[{index}] 字段 {sorted(missing)}", self.source_ref
                )
            if entry["report_kind"] not in VALID_KINDS:
                raise PageStructureChanged(
                    self.supplier,
                    f"entries[{index}].report_kind 取值 {entry['report_kind']!r}",
                    self.source_ref,
                )
            quantity = entry["quantity"]
            revision = entry["revision"]
            if not isinstance(quantity, int) or not isinstance(revision, int) or quantity < 0:
                raise PageStructureChanged(
                    self.supplier, f"entries[{index}] 数量/版本必须为非负整数", self.source_ref
                )
            rows.append(
                CollectedRow(
                    window=payload.get("window", entry.get("window", "")),
                    reported_as=entry["reported_as"],
                    report_kind=entry["report_kind"],
                    quantity=quantity,
                    revision=revision,
                )
            )
            if not rows[-1].window:
                raise PageStructureChanged(self.supplier, "window", self.source_ref)

        receipt = ReceiptDraft(
            media="export-json",
            source_ref=self.source_ref,
            content=raw,
        )
        return CollectionBatch(supplier=self.supplier, rows=rows, receipts=[receipt])


def content_hash(content: str) -> str:
    """原始回执内容的 SHA-256 哈希。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
