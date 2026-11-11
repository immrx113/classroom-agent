"""文档解析器（第四阶段：真实定位块）

产出带稳定 ``block_id``、正文与定位信息的 ``ParsedBlock``：
- PDF 保留页码（page_no，1-based）；无可提取文本 → ``needs_ocr``。
- DOCX 保留段落（paragraph_index）/表格（table_index）与标题（heading），不伪造页码。
- TXT / Markdown 按段落给出稳定定位（paragraph_index）。
- 旧 DOC（.doc）与 PPTX 明确不支持（提示转换 / 当前不支持），不伪装解析成功。

解析失败以 ``ParseError`` 携带稳定错误码区分，供文档服务持久化解析状态。
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


class ParseError(Exception):
    """文档解析失败，code 区分：needs_ocr / unsupported / empty / failed。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ParsedBlock:
    block_id: str
    kind: str  # paragraph / heading / table / text
    text: str
    heading: Optional[str] = None
    page_no: Optional[int] = None
    paragraph_index: Optional[int] = None
    table_index: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "block_id": self.block_id,
            "kind": self.kind,
            "text": self.text,
            "heading": self.heading,
            "page_no": self.page_no,
            "paragraph_index": self.paragraph_index,
            "table_index": self.table_index,
        }


class DocumentParser:
    """文档解析器"""

    @staticmethod
    def parse_pdf(file_path: str) -> list[ParsedBlock]:
        try:
            from pypdf import PdfReader
        except Exception as e:  # noqa: BLE001
            raise ParseError("failed", f"PDF 解析库不可用: {e}") from e

        try:
            reader = PdfReader(file_path)
        except Exception as e:  # noqa: BLE001
            raise ParseError("failed", f"PDF 无法读取: {e}") from e

        blocks: list[ParsedBlock] = []
        for i, page in enumerate(reader.pages):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001
                text = ""
            if text:
                blocks.append(
                    ParsedBlock(
                        block_id=f"p{i + 1}",
                        kind="text",
                        text=text,
                        page_no=i + 1,
                    )
                )

        if not blocks:
            raise ParseError("needs_ocr", "图片型 PDF 无可提取文本，需要 OCR")
        logger.info("PDF 解析完成：%d 页有文本", len(blocks))
        return blocks

    @staticmethod
    def parse_docx(file_path: str) -> list[ParsedBlock]:
        try:
            from docx import Document
        except Exception as e:  # noqa: BLE001
            raise ParseError("failed", f"DOCX 解析库不可用: {e}") from e

        try:
            doc = Document(file_path)
        except Exception as e:  # noqa: BLE001
            raise ParseError("failed", f"DOCX 无法读取: {e}") from e

        blocks: list[ParsedBlock] = []
        para_idx = 0
        for para in doc.paragraphs:
            text = (para.text or "").strip()
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            is_heading = style.lower().startswith("heading") or style.lower().startswith("标题")
            blocks.append(
                ParsedBlock(
                    block_id=f"para-{para_idx}",
                    kind="heading" if is_heading else "paragraph",
                    text=text,
                    heading=text if is_heading else None,
                    paragraph_index=para_idx,
                )
            )
            para_idx += 1

        for t_idx, table in enumerate(doc.tables):
            cell_texts: list[str] = []
            for row in table.rows:
                row_cells = [c.text.strip() for c in row.cells if (c.text or "").strip()]
                if row_cells:
                    cell_texts.append(" | ".join(row_cells))
            joined = "\n".join(cell_texts).strip()
            if joined:
                blocks.append(
                    ParsedBlock(
                        block_id=f"table-{t_idx}",
                        kind="table",
                        text=joined,
                        table_index=t_idx,
                    )
                )

        if not blocks:
            raise ParseError("empty", "DOCX 无可提取文本")
        logger.info("DOCX 解析完成：%d 段落/标题块 + %d 表格块",
                    para_idx, len(doc.tables))
        return blocks

    @staticmethod
    def parse_txt(file_path: str) -> list[ParsedBlock]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(file_path, "r", encoding="gb18030") as f:
                content = f.read()
        except Exception as e:  # noqa: BLE001
            raise ParseError("failed", f"文本文件无法读取: {e}") from e

        blocks = DocumentParser._paragraph_blocks(content, prefix="txt")
        if not blocks:
            raise ParseError("empty", "文本文件为空")
        return blocks

    @staticmethod
    def _paragraph_blocks(content: str, prefix: str) -> list[ParsedBlock]:
        # 按空行 / 换行切段，保留稳定段落序号（TXT/Markdown 定位）
        parts = [p.strip() for p in content.split("\n\n")]
        if len(parts) == 1:
            parts = [p.strip() for p in content.split("\n") if p.strip()]
        blocks: list[ParsedBlock] = []
        idx = 0
        for p in parts:
            if not p:
                continue
            blocks.append(
                ParsedBlock(
                    block_id=f"{prefix}-{idx}",
                    kind="paragraph",
                    text=p,
                    paragraph_index=idx,
                )
            )
            idx += 1
        return blocks

    @classmethod
    def parse(cls, file_path: str, filename: str = "") -> list[ParsedBlock]:
        """根据文件类型自动解析；不支持/需转换格式抛 ParseError。"""
        ext = filename.lower().split(".")[-1] if filename else file_path.lower().split(".")[-1]

        if ext == "pdf":
            return cls.parse_pdf(file_path)
        elif ext == "docx":
            return cls.parse_docx(file_path)
        elif ext == "doc":
            raise ParseError("unsupported_conversion", "旧 DOC 需先转换为 DOCX")
        elif ext in ("pptx", "ppt"):
            raise ParseError("unsupported", "PPTX 当前不支持")
        elif ext in ("txt", "md", "markdown"):
            return cls.parse_txt(file_path)
        else:
            raise ParseError("unsupported", f"不支持的文件类型: {ext}")


# 全局实例
document_parser = DocumentParser()
