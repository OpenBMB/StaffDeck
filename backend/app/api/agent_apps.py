"""Agent 广场：开箱即用的定制 Agent 的后端能力。

Agent 广场里的 Agent（清单由前端 `src/data/agent-catalog.json` 管理）对普通成员是**只读**的：
只能「使用」，不能编辑、不能增删。每个 Agent 对应一个 `capability`，服务端只为这些能力
提供执行接口，Agent 自身的名字 / 描述 / 作者 / 更新时间等元信息不落库。

当前能力：

- ``slides`` —— AI 幻灯片生成。把「模板标题 + 页面文字 + 正文口述」整理成一套结构化 deck，
  前端负责按固定主题渲染成预览与 HTML 源码；`slides:export` 再把同一份 deck 落成 pptx。

- ``doc-review`` —— AI 文档审阅。上传 .docx 解析成段落块，支持一键审阅（问题清单）与
  对话式修改（错别字/归纳总结/润色），导出时在原文档上做段落级文本替换（保留原格式）。
  核心逻辑在 `app.core.artifacts.doc_review`。

- ``ocr`` —— OCR 助手。把 PDF / 图片转成 Markdown、纯文本或带行坐标与版面块的结构化
  JSON，上游是 Indra OCR 服务（契约见仓库根目录 `indra.md`）。**不消耗模型额度**，
  也不需要用户配置模型：识别完全在 OCR 服务里完成，这里只做代理与归一化。

模型归属是本模块的硬约束：生成必须使用**当前用户自己**在「模型配置」里配置并启用的模型，
缺省不回落租户/管理员的默认模型（与 `model_for_agent` 的宽松策略不同），否则同一个 Agent 会
用别人的额度出内容。

deck 的结构、收敛与提示词都在 `app.core.artifacts.slides` —— 这里是「取参数 → 打模型 → 收敛」的薄壳。
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.core.artifacts.doc_review import (
    CHAT_SYSTEM_PROMPT,
    DOCX_MEDIA_TYPE,
    DocBlock,
    REVIEW_SYSTEM_PROMPT,
    apply_blocks_to_document,
    document_to_bytes,
    get_stored_document,
    parse_docx,
    review_batch_size,
    sanitize_actions,
    sanitize_issues,
    store_document,
)
from app.core.artifacts.ocr import (
    ALLOWED_FORMATS,
    OcrError,
    OcrOptions,
    run_ocr,
)
from app.core.artifacts.slides import (
    SLIDES_OPERATION,
    SlidesDeck,
    build_slides_system_prompt,
    content_page_target,
    finalize_deck,
    sanitize_pages,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import ModelConfig, User
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.observability.spans import llm_operation
from app.security.auth import get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/agent-apps",
    tags=["enterprise:agent-apps"],
    dependencies=[Depends(get_current_user)],
)

#: pptx 的 MIME 类型（python-pptx 没有官方常量，这里写死标准值）。
PPTX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

#: 导出文件名上限：太长的标题会把 Content-Disposition 撑爆，也没人看。
_MAX_FILE_NAME_CHARS = 80

#: 与 `SlidesDeck` 一起复用的栏目页数上限（与生成端一致的 1-30 口径）。
_MIN_PAGE_COUNT = 1
_MAX_PAGE_COUNT = 30


class SlidesGenerateRequest(BaseModel):
    tenant_id: str
    model_config_id: str
    narrative: str
    template_title: str = ""
    page_label: str = ""
    page_count_mode: Literal["custom", "auto"] = "custom"
    #: **总页数**（含勾选的骨架页）—— 页面上的「页数」框填的就是它。
    #: 内容页张数由 `content_page_target()` 推导（总页数 - 骨架页，至少 1）。
    page_count: int = 5
    skeleton_pages: list[str] = Field(default_factory=list)
    #: Agent 自带的口径/样式 prompt（如「旅文汇报样式」）。由清单 JSON 提供、用户勾选后带上；
    #: 为空时不追加任何样式约束。
    style_prompt: str = ""


class SlidesExportRequest(BaseModel):
    """导出的是**前端正在展示的那一份** deck —— 所见即所得。

    因此这里不再接收左侧选项（页数/骨架页），只接收 deck 本身；即便如此也会再过一遍
    `sanitize_pages`，防止有人直接打接口塞进越界版式或超长文本。
    """

    deck: SlidesDeck
    #: 可选文件名（不含扩展名）。为空时按 deck 标题推导。
    file_name: str = ""


def _resolve_own_model(db: Session, tenant_id: str, model_config_id: str, user: User):
    """只接受「当前用户自己的、已启用的」模型配置。

    与 `model_for_agent` 的「用户模型缺失就回落租户默认」不同：Agent 广场的生成动作是
    用户显式触发的，用它人的模型代跑会串额度，所以这里缺失即报错、不回落。
    """
    if not model_config_id:
        raise HTTPException(status_code=400, detail="请先在「模型配置」里配置一个属于你的模型")
    row = db.get(ModelConfig, model_config_id)
    if row is None or row.tenant_id != tenant_id or row.user_id != user.id:
        raise HTTPException(status_code=403, detail="只能使用你在「模型配置」里配置的模型")
    if not row.enabled:
        raise HTTPException(status_code=400, detail="该模型已停用，请换一个模型或先在「模型配置」里启用")
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


@router.post("/slides:generate", response_model=SlidesDeck)
def generate_slides(
    request: SlidesGenerateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SlidesDeck:
    ensure_tenant(db, request.tenant_id)
    narrative = (request.narrative or "").strip()
    if not narrative:
        raise HTTPException(status_code=400, detail="请先填写正文口述")
    if request.page_count_mode == "custom" and not (_MIN_PAGE_COUNT <= request.page_count <= _MAX_PAGE_COUNT):
        raise HTTPException(status_code=400, detail=f"页数需要在 {_MIN_PAGE_COUNT}-{_MAX_PAGE_COUNT} 之间")

    model = _resolve_own_model(db, request.tenant_id, request.model_config_id, current_user)

    skeleton_pages = list(request.skeleton_pages or [])
    system_prompt = build_slides_system_prompt(
        page_count_mode=request.page_count_mode,
        page_count=request.page_count,
        skeleton_pages=skeleton_pages,
        style_prompt=request.style_prompt,
    )
    target = content_page_target(request.page_count_mode, request.page_count, skeleton_pages)
    payload = {
        "模板标题": (request.template_title or "").strip(),
        "页面文字": (request.page_label or "").strip(),
        "正文口述": narrative,
        "内容页数": "由你按内容自动决定（4~8 页）" if target is None else str(target),
        "总页数": "由你决定" if target is None else str(target + len(skeleton_pages)),
        "需要额外生成的骨架页": "、".join(skeleton_pages) or "无",
    }

    try:
        with llm_operation(SLIDES_OPERATION):
            raw = LLMClient(model).generate_json(system_prompt, payload)
    except LLMError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"模型调用失败：{exc}。可以换一个模型或稍后重试。",
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 统一翻译成前端可读文案
        raise HTTPException(status_code=502, detail=f"幻灯片生成失败：{exc}") from exc

    # 「选了首页就得有个首页」：收敛时按左侧选项双向对齐（漏了补、没勾的丢、超页截断）。
    return finalize_deck(
        raw,
        template_title=request.template_title,
        page_label=request.page_label,
        skeleton_pages=skeleton_pages,
        page_count_mode=request.page_count_mode,
        page_count=request.page_count,
    )


def _fallback_file_name(title: str) -> str:
    """把标题收拾成一个安全的文件名主干（不含扩展名）。"""
    cleaned = " ".join(str(title or "").split())
    # 路径分隔符与引号会破坏 Content-Disposition，统一替换掉
    for bad in ('/', "\\", '"', "'", "\r", "\n", "\t"):
        cleaned = cleaned.replace(bad, "_")
    cleaned = cleaned.strip(" ._")
    return cleaned[:_MAX_FILE_NAME_CHARS] or "slides"


def _ensure_pptx(name: str) -> str:
    return name if name.lower().endswith(".pptx") else f"{name}.pptx"


def _content_disposition(file_name: str) -> str:
    """中文文件名必须走 RFC 5987 的 ``filename*``，否则部分浏览器会截断成乱码。

    同时给一个纯 ASCII 的 ``filename`` 兜底：老客户端只认这个字段，纯中文名会被削成空串，
    干脆回落成 ``slides.pptx`` 而不是 ``.pptx``。
    """
    stem = file_name[: -len(".pptx")] if file_name.lower().endswith(".pptx") else file_name
    ascii_stem = "".join(
        char for char in stem if char.isascii() and (char.isalnum() or char in "-_ ")
    ).strip(" -_")
    ascii_fallback = _ensure_pptx(ascii_stem or "slides")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


@router.post("/slides:export")
def export_slides(request: SlidesExportRequest) -> Response:
    """把 deck 渲染成 .pptx 下载。

    只做「已生成内容的再渲染」，不调模型，所以不校验模型归属，也不消耗额度。
    """
    pages = sanitize_pages([page.model_dump() for page in (request.deck.pages or [])])
    if not pages:
        raise HTTPException(status_code=400, detail="还没有可导出的幻灯片，请先生成")

    deck = SlidesDeck(
        deck_title=request.deck.deck_title,
        page_label=request.deck.page_label,
        pages=pages,
    )

    # 延迟导入：python-pptx 只在导出路径上用到，缺少它也不该让整个 Agent 广场 500。
    try:
        from app.core.artifacts.slides_pptx import build_slides_pptx
    except ImportError as exc:  # pragma: no cover - 依赖缺失属于部署问题
        raise HTTPException(status_code=500, detail="服务端缺少 PPTX 导出依赖（python-pptx）") from exc

    try:
        data = build_slides_pptx(deck)
    except Exception as exc:  # noqa: BLE001 - 渲染失败要给出可读文案而不是 500 堆栈
        raise HTTPException(status_code=500, detail=f"生成 PPTX 失败：{exc}") from exc

    file_name = _ensure_pptx(_fallback_file_name(request.file_name or deck.deck_title))

    return Response(
        content=data,
        media_type=PPTX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(file_name),
            "Content-Length": str(len(data)),
            # 前端要读文件名时不必再解析 Content-Disposition
            "X-File-Name": quote(file_name),
        },
    )


# ---------------------------------------------------------------------------
# doc-review：AI 文档审阅（解析 / 审阅 / 对话修改 / 导出）
# ---------------------------------------------------------------------------

#: 审阅最多跑多少批（每批 40 段）：控制长文档的 LLM 调用次数与等待时间。
_MAX_REVIEW_BATCHES = 10


class DocReviewRequest(BaseModel):
    tenant_id: str
    model_config_id: str
    doc_id: str
    #: blocks 的唯一事实来源在前端（用户可能已应用/撤销过修改），随请求带回
    blocks: list[DocBlock] = Field(default_factory=list)


class DocChatRequest(BaseModel):
    tenant_id: str
    model_config_id: str
    doc_id: str
    message: str
    #: 最近几轮对话（不含本轮），让 AI 知道之前做过什么
    history: list[dict[str, str]] = Field(default_factory=list)
    blocks: list[DocBlock] = Field(default_factory=list)


class DocExportRequest(BaseModel):
    tenant_id: str
    doc_id: str
    blocks: list[DocBlock] = Field(default_factory=list)
    file_name: str = ""


def _load_stored_doc(doc_id: str) -> dict:
    entry = get_stored_document(doc_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="文档已过期，请重新上传")
    return entry


def _validate_blocks(blocks: list[DocBlock]) -> list[DocBlock]:
    if not blocks:
        raise HTTPException(status_code=400, detail="文档内容为空")
    for block in blocks:
        if len(block.text) > 20000:
            raise HTTPException(status_code=400, detail="单段文本过长，请检查文档")
        # 批注超长直接截断（核心层 MAX_COMMENT_CHARS 兜底，这里提前收敛）
        if block.comment:
            block.comment = block.comment.strip()[:2000]
    return blocks


@router.post("/doc:parse")
def parse_doc(
    tenant_id: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """上传 .docx 并解析成段落块。文档对象留在服务端内存（doc_id 寻址），导出时在其上回写。"""
    ensure_tenant(db, tenant_id)
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件为空")
    original_name = file.filename or "document.docx"
    if not original_name.lower().endswith(".docx"):
        raise HTTPException(status_code=400, detail="目前只支持 .docx 文件（老 .doc 请先用 Word 另存为 .docx）")

    try:
        title, blocks, document, paragraphs = parse_docx(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    doc_id = store_document(original_name, document, paragraphs)
    return {
        "doc_id": doc_id,
        "doc_name": original_name,
        "title": title,
        "blocks": [block.model_dump() for block in blocks],
    }


@router.post("/doc:review")
def review_doc(request: DocReviewRequest, db: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    """一键审阅：分段调模型找问题，返回问题清单（可带逐段 fixes）。"""
    ensure_tenant(db, request.tenant_id)
    blocks = _validate_blocks(request.blocks)
    model = _resolve_own_model(db, request.tenant_id, request.model_config_id, current_user)

    batch_size = review_batch_size()
    batches = [blocks[i : i + batch_size] for i in range(0, len(blocks), batch_size)][:_MAX_REVIEW_BATCHES]
    issues = []
    try:
        for batch in batches:
            payload = {
                "文档块": [{"id": block.id, "text": block.text} for block in batch],
            }
            with llm_operation("doc_review"):
                raw = LLMClient(model).generate_json(REVIEW_SYSTEM_PROMPT, payload)
            issues.extend(sanitize_issues(raw, blocks))
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}。可以换一个模型或稍后重试。") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"文档审阅失败：{exc}") from exc

    reviewed_count = sum(len(batch) for batch in batches)
    notes: list[str] = []
    if len(batches) * batch_size < len(blocks):
        notes.append(f"文档较长，本次已审阅前 {reviewed_count} 段，其余部分未覆盖。")
    return {"issues": [issue.model_dump() for issue in issues], "reviewed_blocks": reviewed_count, "notes": notes}


@router.post("/doc:chat")
def chat_doc(request: DocChatRequest, db: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> dict:
    """对话式修改：模型只产出「段落替换」操作，前端应用后成为新的 blocks 事实来源。"""
    ensure_tenant(db, request.tenant_id)
    blocks = _validate_blocks(request.blocks)
    message = (request.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="请输入修改需求")
    model = _resolve_own_model(db, request.tenant_id, request.model_config_id, current_user)

    # 全量块文本可能很长：单块截断，整体超限再按比例收紧（保住 AI 的寻址能力）
    rows = [{"id": block.id, "text": block.text[:300]} for block in blocks]
    if sum(len(row["text"]) for row in rows) > 24000:
        rows = [{"id": block.id, "text": block.text[:120]} for block in blocks]

    history = [
        {"role": str(row.get("role") or "user"), "content": str(row.get("content") or "")[:1500]}
        for row in (request.history or [])[-8:]
    ]
    payload = {
        "修改指令": message,
        "历史对话": history,
        "文档块": rows,
    }
    try:
        with llm_operation("doc_review_chat"):
            raw = LLMClient(model).generate_json(CHAT_SYSTEM_PROMPT, payload)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=f"模型调用失败：{exc}。可以换一个模型或稍后重试。") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"文档修改失败：{exc}") from exc

    outcome = sanitize_actions(raw, blocks)
    return {
        "reply": outcome.reply,
        "actions": [action.model_dump() for action in outcome.actions],
    }


def _fallback_doc_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())
    for bad in ('/', "\\", '"', "'", "\r", "\n", "\t"):
        cleaned = cleaned.replace(bad, "_")
    cleaned = cleaned.strip(" ._")
    return cleaned[:_MAX_FILE_NAME_CHARS] or "document"


def _ensure_docx(name: str) -> str:
    return name if name.lower().endswith(".docx") else f"{name}.docx"


@router.post("/doc:export")
def export_doc(request: DocExportRequest, db: Session = Depends(get_session), current_user: User = Depends(get_current_user)) -> Response:
    """把当前 blocks 回写进原始 docx 并下载 —— 不调模型，不消耗额度。"""
    ensure_tenant(db, request.tenant_id)
    blocks = _validate_blocks(request.blocks)
    entry = _load_stored_doc(request.doc_id)

    try:
        changed = apply_blocks_to_document(entry, blocks)
        data = document_to_bytes(entry)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"生成 Word 失败：{exc}") from exc

    base_name = request.file_name or entry["name"] or "document"
    file_name = _ensure_docx(_fallback_doc_name(base_name[:-5] if base_name.lower().endswith(".docx") else base_name))

    return Response(
        content=data,
        media_type=DOCX_MEDIA_TYPE,
        headers={
            "Content-Disposition": _content_disposition(file_name),
            "Content-Length": str(len(data)),
            "X-File-Name": quote(file_name),
            "X-Doc-Changed": str(changed),
        },
    )


# ---------------------------------------------------------------------------
# OCR 助手（capability: ``ocr``）
# ---------------------------------------------------------------------------
#
# 与前面几个能力不同，这里**不解析模型**：识别在 Indra OCR 服务里完成，
# 既不需要 model_config_id，也不消耗大模型额度。没有配置模型的成员同样可以用。
#
# 开放 API（`app/public_api/ocr.py`）与企业端共用 `execute_ocr_upload`：同一套校验、
# 同一套上游调用，只是换一种认证。路径与 scope 单点维护在这里，前端「API 接入」示例跟着变。

OCR_OPEN_API_SCOPE = "ocr:run"
OCR_OPEN_API_ENDPOINT_PATH = "/api/v1/ocr"
OCR_OPEN_API_DOCS_PATH = "/api/v1/docs"


class OcrApiAccessResponse(BaseModel):
    """「API 接入」弹窗要展示的接入信息（路径与对外基址都由后端给，前端不再自己拼）。"""

    required_scope: str
    endpoint_path: str
    docs_path: str
    # 来自 PUBLIC_BASE_URL；为空表示未配置，前端回退到当前 origin
    public_base_url: str


@router.get("/ocr:status")
def ocr_status(current_user: User = Depends(get_current_user)) -> dict:
    """前端用来在上传前判断服务是否可用，避免「传完才发现没配置」。

    只回配置状态，不回上游地址（内网地址不该出现在前端产物里）。
    """

    settings = get_settings()
    return {
        "configured": bool((settings.indra_ocr_base_url or "").strip()),
        "formats": list(ALLOWED_FORMATS),
        "max_files": settings.indra_ocr_max_files,
        "max_file_mb": settings.indra_ocr_max_file_mb,
    }


@router.get("/ocr:api-access", response_model=OcrApiAccessResponse)
def ocr_api_access(current_user: User = Depends(get_current_user)) -> OcrApiAccessResponse:
    """给前端「API 接入」弹窗用的接入信息：对外基址取自 PUBLIC_BASE_URL。"""

    settings = get_settings()
    return OcrApiAccessResponse(
        required_scope=OCR_OPEN_API_SCOPE,
        endpoint_path=OCR_OPEN_API_ENDPOINT_PATH,
        docs_path=OCR_OPEN_API_DOCS_PATH,
        public_base_url=(settings.public_base_url or "").rstrip("/"),
    )


def _ocr_http_error(exc: OcrError) -> HTTPException:
    """把 OcrError 翻译成对用户有意义的状态码。

    上游 4xx（密钥错、文件损坏、密码错）原样保留 400/502 语义，让用户知道
    「是你这个文件的问题」还是「服务的问题」。
    """

    if exc.kind == "payload":
        return HTTPException(status_code=400, detail=str(exc))
    if exc.kind == "config":
        return HTTPException(status_code=503, detail=str(exc))
    if exc.kind == "timeout":
        return HTTPException(status_code=504, detail=str(exc))
    if exc.kind == "http" and exc.status and 400 <= exc.status < 500:
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=502, detail=str(exc))


def execute_ocr_upload(
    files: list[UploadFile],
    *,
    format: str = "json",
    mode: str = "",
    dpi: int | None = None,
    max_pages: int | None = None,
    password: str = "",
    math: bool | None = None,
    furniture: bool | None = None,
    image_ocr: bool | None = None,
    include_lines: bool | None = None,
    include_blocks: bool | None = None,
    include_timings: bool | None = None,
) -> dict:
    """企业端工作台与开放 API 共用的执行体：校验 → 读文件 → 打上游 → 归一化。

    参数名与 indra.md 的 `/v1/ocr` 完全一致；布尔项传 ``None`` 时不透传，
    由上游用默认值（``include_lines`` / ``include_blocks`` 默认 true）。
    """

    if dpi is not None and not 72 <= dpi <= 600:
        raise HTTPException(status_code=400, detail="dpi 需在 72~600 之间")
    if max_pages is not None and not 1 <= max_pages <= 2000:
        raise HTTPException(status_code=400, detail="max_pages 需在 1~2000 之间")

    # 先按 UploadFile.size 挡掉超大文件，再读进内存：10 个 50MB 全读进来是 500MB，
    # 没必要为了给用户报一句「文件太大」先把内存占满。
    settings = get_settings()
    max_bytes = max(1, settings.indra_ocr_max_file_mb) * 1024 * 1024
    if len(files) > settings.indra_ocr_max_files:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多识别 {settings.indra_ocr_max_files} 个文件，当前 {len(files)} 个",
        )
    for upload in files:
        if upload.size and upload.size > max_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"文件「{upload.filename or 'upload'}」超过 {settings.indra_ocr_max_file_mb}MB 上限",
            )

    payload: list[tuple[str, bytes, str]] = []
    for upload in files:
        raw = upload.file.read()
        payload.append((upload.filename or "upload", raw, upload.content_type or ""))

    options = OcrOptions(
        format=format,
        mode=(mode or "").strip(),
        dpi=dpi,
        max_pages=max_pages,
        password=(password or "").strip(),
        math=math,
        furniture=furniture,
        image_ocr=image_ocr,
        include_lines=include_lines,
        include_blocks=include_blocks,
        include_timings=include_timings,
    )

    try:
        return run_ocr(payload, options)
    except OcrError as exc:
        raise _ocr_http_error(exc) from exc


@router.post("/ocr:run")
def ocr_run(
    tenant_id: str = Form(...),
    files: list[UploadFile] = File(...),
    format: str = Form("json"),
    mode: str = Form(""),
    dpi: int | None = Form(None),
    max_pages: int | None = Form(None),
    password: str = Form(""),
    math: bool | None = Form(None),
    furniture: bool | None = Form(None),
    image_ocr: bool | None = Form(None),
    include_lines: bool | None = Form(None),
    include_blocks: bool | None = Form(None),
    include_timings: bool | None = Form(None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """上传 PDF / 图片做 OCR，返回按页的 markdown、纯文本与（可选）行坐标/版面块。"""

    ensure_tenant(db, tenant_id)
    return execute_ocr_upload(
        files,
        format=format,
        mode=mode,
        dpi=dpi,
        max_pages=max_pages,
        password=password,
        math=math,
        furniture=furniture,
        image_ocr=image_ocr,
        include_lines=include_lines,
        include_blocks=include_blocks,
        include_timings=include_timings,
    )
