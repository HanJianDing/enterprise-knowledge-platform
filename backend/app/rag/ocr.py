"""OCR 服务（批次D·3a）——扫描件/照片型文档的文本提取

分流不替换（用户拍板）：数字文档走现有轻解析（pypdf 等），检测到
"照片型 PDF"（提取文本近乎为空）时丢给本模块 OCR 转规整文本后
正常分块入库——"损坏扫描件"从正确报错升级为正确解析。

技术选型：RapidOCR（ONNX 版 PaddleOCR 模型，CPU 可跑、纯 pip 无系统
依赖）+ PyMuPDF（PDF 页渲染，Windows wheel 免 poppler）。
模型惰性加载（首次调用 ~1s），进程级缓存。
"""
import logging

logger = logging.getLogger(__name__)

# 扫描件判定阈值：pypdf 提取文本每页平均不足该字符数 → 视为照片型 PDF
_SCANNED_PAGE_CHAR_THRESHOLD = 50

_ocr_engine = None  # 进程级单例（模型加载一次）


def is_scanned_pdf(text: str, page_count: int = 1) -> bool:
    """判定"照片型 PDF"：提取文本近乎为空（每页平均字符数低于阈值）。
    page_count<=0 时按 1 页算（防除零）。"""
    pages = max(1, page_count)
    return len(text.strip()) / pages < _SCANNED_PAGE_CHAR_THRESHOLD


def _get_engine():
    """RapidOCR 惰性单例（模型加载 ~1s；失败返回 None 静默降级）"""
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
            logger.info("RapidOCR 引擎已加载")
        except Exception as e:
            logger.warning("RapidOCR 初始化失败（OCR 功能降级）: %s", e)
            _ocr_engine = False
    return _ocr_engine or None


def _ocr_image_bytes(img_bytes: bytes) -> str:
    """单张图片字节 → OCR 文本；引擎不可用返回空串（静默降级）。
    （批次D后续质检低危#4：主体逻辑与 ocr_image_raw 重复——改为复用
    ocr_image_raw 的文本路，避免两处漂移）"""
    return ocr_image_raw(img_bytes)[0]


def ocr_image_raw(img_bytes: bytes) -> tuple[str, list]:
    """单张图片 → (OCR 文本, 原始结果列表 [[box, text, score], ...])。
    原始结果供表格结构识别使用（批次D 后续：rapid-table 需要文字坐标）；
    引擎不可用返回 ("", [])。"""
    engine = _get_engine()
    if engine is None:
        return "", []
    try:
        result, _ = engine(img_bytes)
        if not result:
            return "", []
        lines = [item[1] for item in result if item and len(item) > 1]
        return "\n".join(lines).strip(), result
    except Exception as e:
        logger.warning("OCR 识别失败（返回空）: %s", e)
        return "", []


def ocr_pdf_file(file_path: str, max_pages: int | None = None) -> str:
    """扫描版 PDF → OCR 文本：PyMuPDF 逐页渲染为图片后 OCR。
    页数超过 max_pages（默认 settings.ocr_max_pages）时只处理前 N 页
    （批次E P3：上限配置化，防超大文件拖垮处理）。"""
    from app.config import settings

    max_pages = max_pages or settings.ocr_max_pages
    try:
        import pymupdf  # PyMuPDF（fitz 旧名已弃用）

        # 批次E P3：with 管理文件句柄（替代 finally close，异常路径同样关闭）
        with pymupdf.open(file_path) as doc:
            pages_text: list[str] = []
            for i, page in enumerate(doc):
                if i >= max_pages:
                    logger.warning("PDF 超过 %d 页，仅 OCR 前 %d 页", max_pages, max_pages)
                    break
                # 渲染 2x 缩放（150 DPI 级，OCR 识别率与速度平衡）
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))
                text = _ocr_image_bytes(pix.tobytes("png"))
                if text:
                    pages_text.append(text)
            return "\n\n".join(pages_text)
    except Exception as e:
        logger.warning("PDF OCR 失败（返回空）: %s", e)
        return ""
