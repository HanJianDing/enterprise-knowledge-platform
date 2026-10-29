"""分块质量诊断（排查用，不入库）：对 D:\\文档数据 全部文档跑真实分块管线，
检查条款/问答/表格/标题各模板的边缘问题。

验收批次E 升级：新增逐行内容覆盖检查——源文件每一非空行的内容都必须
出现在某一块中（正常化去空白后比对）。这是抓"结构行被解析器吃掉却不输出"
类 bug 的兜底网（曾抓住：laws 章标题丢失、faq 首问前标题丢失）。

用法：python scripts/diag_chunks.py
输出：每份文档的分块类型与异常标记（内容丢失/空路径/超长块/表头丢失/碎块等）
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipeline.loader import load_document, load_table
from app.pipeline.processor import _clean_text
from app.rag.chunker import (
    _CN_H1_RE,
    _CN_H2_RE,
    _MD_HEADING_RE,
    _QA_MARK_RE,
    chunk_by_strategy,
    chunk_table,
)

DOCS_DIR = Path("D:/文档数据")
FOLDERS = ["人事部", "财务部", "行政部", "IT部", "市场部", "研发部", "公共", "体验上传"]


def _norm(s: str) -> str:
    """去全部空白后比较（中文文本内部基本无空格，安全）"""
    return "".join(s.split())


def check_coverage(path: Path, chunks, content_type: str) -> list[str]:
    """逐行覆盖检查：源文件非空行内容必须出现在某块中。

    faq 问句标记行按"问题正文"比对（标记按设计剥离）；表格按单元格值比对
    （超长单元格截断，比对截断前缀）。
    """
    misses: list[str] = []
    joined = "".join(c.page_content for c in chunks)
    big = _norm(joined)

    if content_type == "table":
        for sheet in load_table(str(path)):
            for row in sheet.get("rows") or []:
                for cell in row:
                    text = str(cell).strip()
                    if not text:
                        continue
                    probe = _norm(text[:290])  # 截断上限 300 留余量
                    if probe and probe not in big:
                        misses.append(f"单元格丢失: {text[:30]}")
        return misses

    if path.suffix.lower() not in (".txt", ".md"):
        return misses  # 二进制格式（docx/pdf 等）不做逐行检查
    try:
        src = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return misses
    for line in src.splitlines():
        s = line.strip()
        if not s:
            continue
        if content_type == "faq":
            m = _QA_MARK_RE.match(s)
            if m:
                body = m.group(2).strip() or s
                if _norm(body) not in big:
                    misses.append(f"问句行丢失: {s[:40]}")
                continue
        if content_type == "long_text":
            # 标题行（Markdown #/##/### 或 一、/（一））按标题文字比对——
            # 标题模板按设计剥离标记、标题文字注入【路径】前缀，非内容丢失
            m = _MD_HEADING_RE.match(s)
            if m:
                if _norm(m.group(2)) not in big:
                    misses.append(f"标题行丢失: {s[:40]}")
                continue
            cn1 = _CN_H1_RE.match(s)
            if cn1:
                probe = f"{cn1.group(1)}、{cn1.group(2)}"
                if _norm(probe) not in big:
                    misses.append(f"标题行丢失: {s[:40]}")
                continue
            cn2 = _CN_H2_RE.match(s)
            if cn2:
                probe = f"（{cn2.group(1)}）{cn2.group(2)}"
                if _norm(probe) not in big:
                    misses.append(f"标题行丢失: {s[:40]}")
                continue
        if _norm(s) not in big:
            misses.append(f"源行丢失: {s[:40]}")
    return misses


def detect_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".csv", ".xlsx"):
        return "table"
    if ext in (".md", ".txt"):
        text = path.read_text(encoding="utf-8", errors="ignore")[:500]
        if "问：" in text or "Q:" in text:
            return "faq"
    return "long_text"


def analyze(path: Path, content_type: str) -> list[str]:
    flags = []
    try:
        if content_type == "table":
            sheets = load_table(str(path))
            chunks = chunk_table(sheets, {"document_id": "x"})
        else:
            raw = load_document(str(path))
            text = _clean_text(raw)
            chunks = chunk_by_strategy(text, content_type, {"document_id": "x"})
    except Exception as e:
        return [f"解析异常: {e}"]

    # 逐行覆盖检查（批次E 新增）：任何源行内容不得从分块结果中消失
    flags.extend(check_coverage(path, chunks, content_type))

    if not chunks:
        return ["无分块"]
    ctype = chunks[0].metadata.get("chunk_type", "?")

    for i, c in enumerate(chunks, 1):
        content = c.page_content
        if len(content) > 700:
            flags.append(f"块{i} 超长({len(content)}字)")
        if len(content) < 20 and len(chunks) > 1:
            flags.append(f"块{i} 碎块({len(content)}字)")

    if ctype == "laws":
        # 条款模板：源文有章节/条款时，含条款内容的块必须有路径前缀。
        # 批次0 修正：长"目录/前言"（如管理制度汇编的 84 行目录）超长切开后
        # 无条款路径属正常——目录块本就不是条款，豁免之，防误报
        src = path.read_text(encoding="utf-8", errors="ignore") if path.suffix in (".txt", ".md") else ""
        has_chapter = bool(re.search(r"第[一二三四五六七八九十]+[章节]", src))
        missing = [i for i, c in enumerate(chunks, 1)
                   if not c.metadata.get("section_path") and re.search(r"第[一二三四五六七八九十]+条", c.page_content)]
        if has_chapter and missing:
            flags.append(f"条款模板路径丢失（{len(missing)}/{len(chunks)} 块无 section_path）")
        if has_chapter and not any("【" in c.page_content for c in chunks):
            flags.append("章节标题未注入任何块")
    elif ctype == "faq":
        # 问答模板：问答对不应被拆断（问与答同块）
        for i, c in enumerate(chunks, 1):
            q_count = len(re.findall(r"问[：:]", c.page_content))
            a_count = len(re.findall(r"答[：:]", c.page_content))
            if q_count and a_count < q_count:
                flags.append(f"块{i} 疑似问答对被拆断（问{q_count}/答{a_count}）")
    elif ctype == "table":
        if not all("列：" in c.page_content for c in chunks):
            flags.append("表格块缺列头上下文")
    elif ctype == "long_text":
        src = path.read_text(encoding="utf-8", errors="ignore") if path.suffix in (".txt", ".md") else ""
        has_heading = bool(re.search(r"^#{1,3}\s|^[一二三四五六七八九十]+、", src, re.M))
        if has_heading and any(c.metadata.get("section_path") for c in chunks):
            pass  # 标题注入正常
    return flags


def main() -> None:
    total, flagged = 0, 0
    for folder in FOLDERS:
        folder_path = DOCS_DIR / folder
        if not folder_path.exists():
            continue
        for f in sorted(folder_path.iterdir()):
            if not f.is_file() or f.suffix.lower() not in (".txt", ".md", ".csv", ".xlsx", ".docx"):
                continue
            total += 1
            ct = detect_type(f)
            flags = analyze(f, ct)
            mark = "✅" if not flags else "⚠️"
            if flags:
                flagged += 1
                print(f"{mark} [{folder}] {f.name}（{ct}）")
                for flag in flags:
                    print(f"    - {flag}")
    print(f"\n诊断完成：共 {total} 份，异常 {flagged} 份")


if __name__ == "__main__":
    main()
