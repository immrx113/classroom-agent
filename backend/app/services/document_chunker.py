"""索引分块器（第四阶段：块级定位）

将解析块（ParsedBlock）切成索引单元。一个解析块保持其来源身份（block_id / 页码 /
段落 / 表格），正文超长时按 ``chunk_size`` 拆分，仍沿用同一 block_id（子块以 ``sub`` 区分），
使同源去重与证据 quote 匹配都落在唯一解析块上，不因切块丢失定位。
"""

import logging
from typing import Optional

from app.services.document_parser import ParsedBlock

logger = logging.getLogger(__name__)


class DocumentChunker:
    """解析块 → 索引单元"""

    def __init__(self, chunk_size: int = 1000):
        self.chunk_size = chunk_size

    def chunk_parsed_blocks(self, blocks: list[ParsedBlock]) -> list[dict]:
        """切分解析块为索引单元（保持来源身份），返回有序单元列表。

        每个单元：content / block_id / page_no / paragraph_index / table_index /
        heading / kind / seq（块内顺序）/ sub（超长块的子块序号）。
        """
        units: list[dict] = []
        for block in blocks:
            text = block.text
            if not text or not text.strip():
                continue
            if len(text) <= self.chunk_size:
                units.append(self._unit(block, text, None))
            else:
                # 超长块拆分，仍沿用同一 block_id（sub 区分子块，定位不变）
                sub = 0
                for i in range(0, len(text), self.chunk_size):
                    piece = text[i:i + self.chunk_size].strip()
                    if piece:
                        units.append(self._unit(block, piece, sub))
                        sub += 1
        logger.info("索引分块完成：%d 个单元", len(units))
        return units

    @staticmethod
    def _unit(block: ParsedBlock, content: str, sub: Optional[int]) -> dict:
        return {
            "content": content,
            "block_id": block.block_id,
            "page_no": block.page_no,
            "paragraph_index": block.paragraph_index,
            "table_index": block.table_index,
            "heading": block.heading,
            "kind": block.kind,
            "seq": block.paragraph_index if block.paragraph_index is not None else block.page_no,
            "sub": sub,
        }


# 全局实例（chunk_size 由 config 注入；缺省 1000）
document_chunker = DocumentChunker()
