"""表格照片结构化识别（批次D 后续·用户拍板 V3 完整版）——现成模型集成

选型：rapid-table（PP-StructureV3 SLANet 表格结构模型的 ONNX 版，
纯 pip 无 paddle 依赖、与 RapidOCR 同族）——深度学习模型直接输出
结构化表格 HTML（复杂表格 95%+，不自研算法）。
流程：RapidOCR 取文字坐标 → RapidTable 结构识别 → HTML 表格 →
Markdown 表格文本入库（可检索、AI 可读）。

注意：rapid-table 的 ocr_results 参数是**批次列表**语义
（[(boxes, texts, scores), ...] 每图一个三元组）——单图也要包一层。
"""
import logging
from html.parser import HTMLParser

import numpy as np

logger = logging.getLogger(__name__)

_table_engine = None  # 进程级单例（模型加载一次）


def _get_table_engine():
    """RapidTable 惰性单例；失败返回 None 静默降级（无表格结构仍可 OCR）"""
    global _table_engine
    if _table_engine is None:
        try:
            from rapid_table import RapidTable

            _table_engine = RapidTable()
            logger.info("RapidTable 引擎已加载")
        except Exception as e:
            logger.warning("RapidTable 初始化失败（表格结构识别降级）: %s", e)
            _table_engine = False
    return _table_engine or None


class _TableHTMLToMarkdown(HTMLParser):
    """HTML 表格 → Markdown 表格（手写转换器，仅支持 table/tr/td/th）。

    colspan 简化处理：colspan=N 时该单元格内容占 N 列（右补空列）。
    """

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._cur_row: list[str] = []
        self._cur_cell: list[str] = []
        self._in_cell = False
        self._colspan = 1

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("td", "th"):
            self._in_cell = True
            self._cur_cell = []
            # 非数字 colspan（如 "abc"）按 1 兜底——不因单属性异常丢整表
            try:
                self._colspan = max(1, int(attrs.get("colspan", 1)))
            except (TypeError, ValueError):
                self._colspan = 1
        elif tag == "tr":
            self._cur_row = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._in_cell:
            text = "".join(self._cur_cell).strip()
            self._cur_row.append(text)
            for _ in range(self._colspan - 1):
                self._cur_row.append("")  # colspan 右补空列
            self._in_cell = False
        elif tag == "tr":
            if self._cur_row:
                self.rows.append(self._cur_row)
            self._cur_row = []

    def handle_data(self, data):
        if self._in_cell:
            self._cur_cell.append(data)

    @staticmethod
    def _escape_cell(text: str) -> str:
        """单元格文本转义（批次D后续质检修复）：OCR 文本含 | 会打破
        Markdown 列结构、换行会破行——转义后入库结构稳定。
        反引号替换为单引号（批次D后续质检低危#1）：单元格内单个反引号
        会开启行内代码区间并吞掉后续 | 分隔符（OCR 代码截图场景）"""
        return (text.replace("|", "\\|").replace("\n", " ")
                   .replace("\r", " ").replace("`", "'"))

    def to_markdown(self) -> str:
        if not self.rows:
            return ""
        width = max(len(r) for r in self.rows)
        norm = [
            [self._escape_cell(c) for c in r] + [""] * (width - len(r))
            for r in self.rows
        ]
        header = norm[0]
        body = norm[1:]
        lines = [
            "| " + " | ".join(header) + " |",
            "|" + "---|" * width,
        ]
        for row in body:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)


def html_table_to_markdown(html: str) -> str:
    """rapid-table 输出的 HTML 表格 → Markdown（识别失败/空返回空串）"""
    if not html:
        return ""
    try:
        parser = _TableHTMLToMarkdown()
        parser.feed(html)
        return parser.to_markdown()
    except Exception as e:
        logger.warning("表格 HTML 转换失败: %s", e)
        return ""


def recognize_table_markdown(img: np.ndarray, ocr_raw: list) -> str:
    """图片 → Markdown 表格文本（无表格/引擎不可用返回空串——静默降级）。

    ocr_raw 为 RapidOCR 的原始结果列表（[[box, text, score], ...]）。
    """
    engine = _get_table_engine()
    if engine is None:
        return ""
    try:
        boxes = np.array([item[0] for item in ocr_raw], dtype=np.float32)
        texts = tuple(str(item[1]) for item in ocr_raw)
        scores = tuple(float(item[2]) for item in ocr_raw)
        out = engine([img], [(boxes, texts, scores)])  # 批次列表语义
        htmls = getattr(out, "pred_htmls", None) or []
        if not htmls or not htmls[0]:
            return ""
        return html_table_to_markdown(htmls[0])
    except Exception as e:
        logger.warning("表格识别失败（降级纯 OCR 文本）: %s", e)
        return ""
