"""生成「带已知页码 + 已知标记文字」的 2 页 PDF 试用课件（无第三方写库依赖）。

供阶段 2 复验项 3 使用：验证课件「可定位到对应原页」（page_no）。
第 1 页标记 PAGE_ONE_MARKER_NEWTON_LAW_2026；第 2 页标记 PAGE_TWO_MARKER_F_EQ_MA_2026。
仅使用标准 Type1 Helvetica + WinAnsiEncoding（ASCII），pypdf 可稳定提取文本。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]  # backend/
OUT = ROOT.parent / "docs" / "classroom-agent" / "trial-materials" / "newton-lesson.pdf"

PAGE1 = [
    "PAGE_ONE_MARKER_NEWTON_LAW_2026",
    "Newton's second law: introduction.",
    "Force equals mass times acceleration.",
]
PAGE2 = [
    "PAGE_TWO_MARKER_F_EQ_MA_2026",
    "Formula: F = m * a.",
    "Homework: derive the formula and list units.",
]


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[str]], out_path: Path) -> None:
    n = len(pages)
    font_num = 3 + 2 * n
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs: dict[int, bytes] = {}

    objs[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode("latin-1")

    for i, lines in enumerate(pages):
        page_num = 3 + 2 * i
        content_num = 4 + 2 * i
        cmds = ["BT", "/F1 18 Tf", "72 720 Td"]
        for j, line in enumerate(lines):
            if j > 0:
                cmds.append("0 -28 Td")
            cmds.append(f"({_esc(line)}) Tj")
        cmds.append("ET")
        stream = "\n".join(cmds).encode("latin-1")
        objs[content_num] = (
            b"<< /Length " + str(len(stream)).encode("latin-1")
            + b" >>\nstream\n" + stream + b"\nendstream"
        )
        objs[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/Contents {content_num} 0 R >>"
        ).encode("latin-1")

    objs[font_num] = (
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, font_num + 1):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode("latin-1")
        out += objs[num] + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {font_num + 1}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for num in range(1, font_num + 1):
        out += f"{offsets[num]:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {font_num + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("latin-1")
    out_path.write_bytes(bytes(out))


def verify(pdf_path: Path) -> None:
    from pypdf import PdfReader
    reader = PdfReader(str(pdf_path))
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        print(f"page {i + 1}: {text!r}")


if __name__ == "__main__":
    make_pdf([PAGE1, PAGE2], OUT)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    verify(OUT)
    if "--check" in sys.argv:
        from pypdf import PdfReader
        pages = PdfReader(str(OUT)).pages
        ok = ("PAGE_ONE_MARKER_NEWTON_LAW_2026" in (pages[0].extract_text() or "")
              and "PAGE_TWO_MARKER_F_EQ_MA_2026" in (pages[1].extract_text() or ""))
        print("check_ok=", ok)
        sys.exit(0 if ok else 1)
