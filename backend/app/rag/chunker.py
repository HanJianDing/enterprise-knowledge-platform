"""文本分块策略 — 按文档内容形态选择分块方式（产品化 A1：模板化分块）

三种新模板（借鉴 RAGFlow 思路，规则检测、不烧 token）：
- 条款模板（laws）：制度类文档按"第X条"切分并注入章节路径，由 long_text 自动检测
- 问答对模板（qa）：FAQ 按 Q/A 标记成对切分，一个问答对一块；识别不到回落旧双换行切分
- 表格模板（table）：xlsx/csv 按行组切分，每块重复注入列头上下文（结构化加载，见 loader.load_table）
"""
import re

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

# ── 结构检测正则 ──
_LAW_ITEM_RE = re.compile(r"^第[一二三四五六七八九十百千0-9]+条")        # 第X条
_LAW_SECTION_RE = re.compile(r"^第[一二三四五六七八九十百千0-9]+[章节]")  # 第X章/第X节
_QA_MARK_RE = re.compile(r"^[（(]?\s*([Qq問问])\s*[）).、:：]?\s*(.*)$")
_MD_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+)$")
_CN_H1_RE = re.compile(r"^([一二三四五六七八九十]+)、\s*(.*)$")
_CN_H2_RE = re.compile(r"^（([一二三四五六七八九十]+)）\s*(.*)$")

# 单块上限：条款/问答对/表格合并都以此为界（嵌入模型上下文安全余量）
_MAX_CHUNK = 600


def _get_text_splitter(
    chunk_size: int,
    chunk_overlap: int,
    separators: list[str],
) -> RecursiveCharacterTextSplitter:
    """创建递归文本分块器"""
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=separators,
        length_function=len,
        is_separator_regex=False,
        add_start_index=True,
    )


def _split_with_prefix(text: str, prefix: str) -> list[str]:
    """把超长段落递归切分，每块注入章节路径前缀（上下文不丢）"""
    splitter = _get_text_splitter(
        _MAX_CHUNK, 100, ["\n\n", "\n", "。", "！", "？", "；"]
    )
    subs = splitter.split_text(text)
    return [f"【{prefix}】\n{s}" if prefix else s for s in subs]


# ── 策略 1: 结构化数据 — 不分块，整个文档作为一块 ──

def chunk_structured(text: str, base_metadata: dict) -> list[Document]:
    """
    结构化数据不分块，整个文档作为一个整体块存储。
    chunk_count 始终为 1。
    """
    if not text or not text.strip():
        return []
    meta = {**base_metadata, "chunk_type": "structured"}
    return [Document(page_content=text.strip(), metadata=meta)]


# ── 策略 2: FAQ/问答对 — Q/A 成对切分，识别不到回落双换行 ──

def _detect_qa_pairs(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """按问答标记成对解析：Q:/问： 开头到下一个 Q 之间为一个问答对。

    至少 2 对才算结构化 FAQ（1 对视为巧合，回落旧策略保证不劣化）。
    返回 (首问前的标题/前言文本, 问答对列表)；前言并入首对，避免文档
    标题/章节名从索引中丢失（验收批次E 分块体检发现的同款内容丢失）。
    """
    preamble: list[str] = []
    pairs: list[tuple[str, str]] = []
    cur_q: str | None = None
    cur_a: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m = _QA_MARK_RE.match(line)
        if m:
            if cur_q is not None:
                pairs.append((cur_q, "\n".join(cur_a)))
            cur_q = m.group(2).strip() or line  # 问题正文（去标记）
            cur_a = []
        elif cur_q is not None:
            cur_a.append(raw)
        else:
            preamble.append(raw)  # 首问之前的标题/说明行，并入首个问答对
    if cur_q is not None:
        pairs.append((cur_q, "\n".join(cur_a)))
    return ("\n".join(preamble), pairs) if len(pairs) >= 2 else None


def chunk_faq(text: str, base_metadata: dict) -> list[Document]:
    """FAQ/政策类分块（产品化 A1 升级）。

    优先按问答标记成对切分（一个问答对一块，小对合并至目标尺寸）；
    识别不到结构化问答时回落原双换行切分（行为与升级前一致）。
    """
    if not text or not text.strip():
        return []
    meta = {**base_metadata, "chunk_type": "faq"}

    parsed = _detect_qa_pairs(text)
    if parsed is None:
        splitter = _get_text_splitter(200, 20, ["\n\n", "\n"])
        return splitter.create_documents(texts=[text], metadatas=[meta])

    preamble, pairs = parsed
    if preamble:
        # 验收批次E：文档标题等首问前内容并入第一对——旧实现直接丢弃，
        # 标题（常含主题名）从索引中消失，按主题名检索搜不到该文档
        first_q, first_a = pairs[0]
        pairs[0] = (f"{preamble}\n{first_q}", first_a)

    chunks: list[Document] = []
    acc = ""

    def flush() -> None:
        nonlocal acc
        if acc.strip():
            chunks.append(Document(page_content=acc.strip(), metadata=meta))
        acc = ""

    for q, a in pairs:
        pair_text = f"{q}\n{a}" if a.strip() else q
        if len(pair_text) > _MAX_CHUNK:
            # 单个问答对超长：递归切分，问题正文作为每块前缀
            flush()
            for sub in _split_with_prefix(pair_text, q):
                chunks.append(Document(page_content=sub, metadata=meta))
            continue
        if acc and len(acc) + len(pair_text) + 2 <= _MAX_CHUNK:
            acc = acc + "\n\n" + pair_text
        else:
            flush()
            acc = pair_text
    flush()
    return chunks


# ── 策略 3: 长文本 — 条款模板 / 标题层级 / 递归兜底 ──

def _detect_law_structure(text: str) -> bool:
    """条款密度检测：平均每 500 字至少一条"第X条"且总数 ≥ 2 才走条款模板"""
    law_items = [
        l for l in text.split("\n") if _LAW_ITEM_RE.match(l.strip())
    ]
    if len(law_items) < 2:
        return False
    return len(text) / len(law_items) <= 500


def _chapter_of(path: str) -> str:
    """条款路径的首段章节名；无章节结构的条款路径不含" → "，返回空串"""
    return path.split(" → ")[0] if " → " in path else ""


def chunk_laws(text: str, base_metadata: dict) -> list[Document]:
    """条款模板：按"第X章/节 → 第X条"两级边界切分。

    每条条款内容注入章节路径前缀（如【第五章 假期管理 → 第十二条 婚假】），
    检索时条款编号与章节名天然参与匹配。

    验收批次E 两处修复：
    - 章/节标题并入其下首条条款正文（旧版标题只进路径不进正文，小文档合并
      成一块时二~末章章名从索引中彻底消失，搜章名永远搜不到）；
    - 合并按章分块：同章条款合并至目标尺寸，跨章不合并（块标签与内容一致，
      不再出现"标着第一章实际装五章"的名不副实块）；无章结构文档按尺寸合并，
      行为与旧版一致。
    """
    sections: list[tuple[str, str]] = []  # (章节路径, 条款正文)
    current_section = ""
    buf: list[str] = []
    preamble: list[str] = []  # 新章标题 + 章下散文，并入本章首条条款正文
    path = ""

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if _LAW_SECTION_RE.match(line):
            # 章/节标题：上一章的末条先落定；标题暂存，随下一条条款并入正文
            if buf and path:
                sections.append((path, "\n".join(buf)))
                buf = []
            current_section = line
            preamble = [line]
            continue
        if _LAW_ITEM_RE.match(line):
            if buf:
                sections.append((path, "\n".join(buf)))
                buf = []
            # 路径只取"条号 + 短标题"（截到首个标点，上限 15 字）——
            # 不整条内容入路径，否则【】前缀与正文重复且冗长
            m = _LAW_ITEM_RE.match(line)
            item_no = m.group(0)
            title = re.split(r"[，。；：、]", line[len(item_no):], maxsplit=1)[0][:15].strip()
            path = f"{current_section} → {item_no} {title}".strip() if current_section else f"{item_no} {title}".strip()
            if preamble:
                buf = buf + preamble  # 章标题并入本章首条条款（标题等前言在前）
                preamble = []
        elif preamble:
            # 章标题后的散文（无条号）内容：先把章标题并入正文
            buf.extend(preamble)
            preamble = []
        buf.append(raw)
    if preamble:
        # 文末孤章标题/散文：并入最后一段（正常文档不会出现）
        buf.extend(preamble)
    if buf:
        sections.append((path, "\n".join(buf)))

    # 合并小条至目标尺寸（保留每条条款的独立路径，路径取块首条款）
    chunks: list[Document] = []
    acc_text, acc_path = "", ""

    def flush() -> None:
        nonlocal acc_text, acc_path
        if acc_text.strip():
            content = f"【{acc_path}】\n{acc_text}" if acc_path else acc_text
            chunks.append(Document(
                page_content=content.strip(),
                metadata={**base_metadata, "chunk_type": "laws", "section_path": acc_path},
            ))
        acc_text, acc_path = "", ""

    for path, content in sections:
        if len(content) > _MAX_CHUNK:
            flush()
            for sub in _split_with_prefix(content, path):
                chunks.append(Document(
                    page_content=sub,
                    metadata={**base_metadata, "chunk_type": "laws", "section_path": path},
                ))
            continue
        if not acc_text:
            acc_path, acc_text = path, content
            continue
        if acc_path and _chapter_of(acc_path) != _chapter_of(path):
            flush()
            acc_path, acc_text = path, content
            continue
        # 验收批次G：块内后续条款注入自己的路径行（旧合并只留块首路径，
        # 其余条款的路径被丢弃——块标签"名不副实"，来源展示无法定位条号）
        part = f"【{path}】\n{content}" if (path and acc_path) else content
        if len(acc_text) + len(part) + 1 > _MAX_CHUNK:
            flush()
            acc_path, acc_text = path, content
            continue
        if not acc_path and path:
            # 修复：acc_path 只在累加区为空时设置——文档开头有标题/前言时
            # 第一个 section 的 path 是空串，之后合并新条款若不更新路径，
            # 整块会丢失章节前缀（章标题只存在于路径中，随之彻底消失）
            acc_path = path
        acc_text = acc_text + "\n" + part
    flush()

    # 验收批次1·1f（RAPTOR-lite 决策）：章节 ≥10 的条款文档追加"目录摘要块"——
    # 宏观问题（"制度体系覆盖哪些方面/白皮书有哪些章节"）靠完整目录作答。
    # 实测：60+ 行目录被分块切碎后，宏观问题 top6 只能捞到目录碎片；
    # 目录摘要块把全貌聚成一块，BM25/向量均可命中（零 LLM 成本，0 层摘要）
    # 批次11a：toc_summary_enabled 开关（默认开）——RAPTOR 量化对比档③
    # "纯 RAPTOR"时关闭目录块，隔离两者效果
    from app.config import settings

    if not settings.toc_summary_enabled:
        return chunks
    chapter_names = []
    for _path, _content in sections:
        ch = _chapter_of(_path)
        if ch and ch not in chapter_names:
            chapter_names.append(ch)
    if len(chapter_names) >= 10:
        # 章名来自原文（已含"第一章 总则"式编号），直接罗列即目录；
        # 描述头带"整体/涵盖/章节"等宏观问法常用词，保证
        # "整体覆盖哪些方面/有哪些章节"类问题能命中本块
        doc_name = (base_metadata.get("filename") or "").rsplit(".", 1)[0]
        summary = (
            f"【文档目录】《{doc_name}》共 {len(chapter_names)} 章，"
            f"整体涵盖以下方面：\n" + "\n".join(chapter_names)
        )
        chunks.append(Document(
            page_content=summary,
            metadata={**base_metadata, "chunk_type": "doc_summary"},
        ))
    return chunks


def _parse_headings(text: str) -> list[tuple[str, str]] | None:
    """按标题（Markdown #/##/### 或 一、/（一））分段，返回 [(标题路径, 正文)]。

    少于 2 个标题视为无结构（返回 None，回落递归分块）。
    """
    sections: list[tuple[str, str]] = []
    h1 = h2 = ""
    buf: list[str] = []
    heading_count = 0

    for raw in text.split("\n"):
        line = raw.strip()
        m = _MD_HEADING_RE.match(line)
        cn1 = _CN_H1_RE.match(line)
        cn2 = _CN_H2_RE.match(line)
        if m or cn1 or cn2:
            if buf and "".join(buf).strip():
                path = " → ".join(p for p in (h1, h2) if p)
                sections.append((path, "\n".join(buf)))
            buf = []
            heading_count += 1
            if m:
                level = len(m.group(1))
                title = m.group(2)
                if level == 1:
                    h1, h2 = title, ""
                else:  # ## / ### 都并入二级
                    h2 = title
            elif cn1:
                h1, h2 = f"{cn1.group(1)}、{cn1.group(2)}", ""
            else:
                h2 = f"（{cn2.group(1)}）{cn2.group(2)}"
        else:
            buf.append(raw)

    if buf and "".join(buf).strip():
        path = " → ".join(p for p in (h1, h2) if p)
        sections.append((path, "\n".join(buf)))
    return sections if heading_count >= 2 else None


def _make_sub_chunks(content: str, meta: dict) -> list[Document]:
    """父子双层分块（验收批次1·1c）：父块 ≤300 字原样返回；更长则按句边界
    切成子块（累计 ≤240 字发车、单句超长可到 600 上限）——检索用子块匹配
    （小块向量更聚焦、语义不稀释），每个子块 metadata 携带 parent_content
    （父块全文，返回时用它给 LLM 完整上下文）与 parent_id（同父块子块去重用）。

    父块序号通过临时键 _parent_seq 传入（processor 稍后统一重编
    chunk_index，子块化前的序号在此保留为 parent_id 的一部分）；
    临时键不进最终 metadata。
    """
    sub_meta_base = {k: v for k, v in meta.items() if k != "_parent_seq"}
    if len(content) <= 300:
        return [Document(page_content=content.strip(), metadata=sub_meta_base)]

    parent_seq = meta.get("_parent_seq", 0)
    parent_id = f"{meta.get('document_id', '')}#{parent_seq}"

    sentences = re.split(r"(?<=[。！？；])\s*|\n+", content)
    sentences = [s.strip() for s in sentences if s.strip()]
    pieces: list[str] = []
    acc = ""
    for s in sentences:
        if acc and len(acc) + len(s) > 240:
            pieces.append(acc)
            acc = s
        else:
            acc = acc + s
    if acc:
        # 收尾短句并入前一片（防碎块）
        if pieces and len(acc) < 60:
            pieces[-1] = pieces[-1] + acc
        else:
            pieces.append(acc)

    subs = []
    for piece in pieces:
        if not piece.strip():
            continue
        subs.append(Document(
            page_content=piece.strip(),
            metadata={
                **sub_meta_base,
                "chunk_type": "sub_chunk",
                "parent_content": content.strip(),
                "parent_id": parent_id,
            },
        ))
    return subs if subs else [Document(page_content=content.strip(), metadata=sub_meta_base)]


def chunk_long_text(text: str, base_metadata: dict) -> list[Document]:
    """长文本分块（产品化 A1 升级：条款自动识别 → 标题层级 → 递归兜底；
    验收批次1·1c：long_text 分块结果再过父子双层——大块拆小块匹配、
    子块带父块全文供返回上下文）。条款/问答/表格模板为结构化块，不做子块化。
    """
    if not text or not text.strip():
        return []

    if _detect_law_structure(text):
        return chunk_laws(text, base_metadata)

    headings = _parse_headings(text)
    if headings is not None:
        chunks: list[Document] = []
        for path, content in headings:
            if len(content) > _MAX_CHUNK:
                for sub in _split_with_prefix(content, path):
                    chunks.append(Document(
                        page_content=sub,
                        metadata={**base_metadata, "chunk_type": "long_text", "section_path": path},
                    ))
            else:
                body = f"【{path}】\n{content}" if path else content
                chunks.append(Document(
                    page_content=body.strip(),
                    metadata={**base_metadata, "chunk_type": "long_text", "section_path": path},
                ))
    else:
        splitter = _get_text_splitter(
            600, 100, ["\n\n", "\n", "。", "！", "？", "；"]
        )
        chunks = splitter.create_documents(
            texts=[text],
            metadatas=[{**base_metadata, "chunk_type": "long_text"}],
        )

    # 父子双层：原块作为"父块"再过子块化（父块序号临时注入）。
    # 验收批次1·1c 激活定案：只对厚文档（>settings.retrieval_subchunk_min_chars）
    # 自动启用——厚文档才是语义稀释重灾区；600 字级文档切块只添噪音
    # （全局开实测 MRR 双降即此因）。总闸 settings.retrieval_subchunk_enabled
    from app.config import settings

    if not settings.retrieval_subchunk_enabled or len(text) <= settings.retrieval_subchunk_min_chars:
        return chunks
    expanded: list[Document] = []
    for i, c in enumerate(chunks):
        m = dict(c.metadata)
        m["_parent_seq"] = i
        expanded.extend(_make_sub_chunks(c.page_content, m))
    return expanded


# ── 策略 4: 表格模板 — 结构化加载（表头 + 行），按行组切分 ──

_ROWS_PER_CHUNK = 20
# 单单元格截断上限：防畸形表格（单元格塞了整篇文章）撑爆嵌入 token 上限
_CELL_MAX_LEN = 300


def _cell_text(value) -> str:
    """单元格转文本并截断超长内容（尾部加省略号标记截断）"""
    text = str(value).strip() if value is not None else ""
    if len(text) > _CELL_MAX_LEN:
        return text[:_CELL_MAX_LEN] + "…"
    return text


def chunk_table(
    sheets: list[dict],
    base_metadata: dict,
) -> list[Document]:
    """表格模板：每个 sheet 按行组切分（约 20 行/块），每块开头注入列头上下文。

    sheets 结构（loader.load_table 产出）：
    [{"name": 表名, "headers": [列头...], "rows": [[单元格...], ...]}]
    行组边界整行切，不切半行；表头随每块重复，检索时"列名+行值"天然共现。
    """
    chunks: list[Document] = []
    for sheet in sheets:
        headers = sheet.get("headers") or []
        header_text = "、".join(str(h).strip() for h in headers if str(h).strip())
        rows = sheet.get("rows") or []
        for start in range(0, len(rows), _ROWS_PER_CHUNK):
            group = rows[start:start + _ROWS_PER_CHUNK]
            lines = [f"【表格：{sheet['name']}】列：{header_text}"]
            for i, row in enumerate(group, start=start + 1):
                cells = [_cell_text(c) for c in row]
                lines.append(f"行{i}：{' | '.join(cells)}")
            content = "\n".join(lines)
            if not content.strip():
                continue
            chunks.append(Document(
                page_content=content,
                metadata={
                    **base_metadata,
                    "chunk_type": "table",
                    "table_header": header_text,
                    "sheet_name": sheet["name"],
                },
            ))
    return chunks


# ── 策略 5: 默认兜底 ──

def _chunk_default(text: str, base_metadata: dict) -> list[Document]:
    """
    默认递归分块（兜底策略）。
    chunk_size=500, overlap=80, 全中文标点 separators。
    """
    if not text or not text.strip():
        return []
    splitter = _get_text_splitter(
        chunk_size=500,
        chunk_overlap=80,
        separators=["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""],
    )
    chunks = splitter.create_documents(
        texts=[text],
        metadatas=[{**base_metadata, "chunk_type": "default"}],
    )
    return chunks


# ── 策略路由器 ──

def chunk_by_strategy(
    text: str,
    content_type: str,
    base_metadata: dict,
) -> list[Document]:
    """
    根据 content_type 选择分块策略（与父子分类无关）。

    | content_type | 策略 | 说明 |
    |-------------|------|------|
    | structured  | 不分块，整个文档一块 | N/A |
    | faq         | 问答对成对切（回落双换行） | A1 升级 |
    | long_text   | 条款自动识别 → 标题层级 → 递归 | A1 升级 |
    | table       | 表格模板（结构化加载，见 processor 分支） | A1 新增 |
    | chat_log / 默认 | 递归分块（兜底） | 500/80 |
    """
    strategies = {
        "structured": chunk_structured,
        "faq": chunk_faq,
        "long_text": chunk_long_text,
    }
    chunk_fn = strategies.get(content_type, _chunk_default)
    return chunk_fn(text, base_metadata)
