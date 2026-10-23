"""OCR 助手（Agent 广场 ``ocr`` 能力）：请求构造、回包归一化与错误语义。

上游契约来自仓库根目录的 ``indra.md``。这组用例重点守三件事：

1. 参数是**原样透传**给 ``/v1/ocr`` 的，没传的布尔项不能被写成 ``false``
   （否则用户会莫名其妙丢掉行坐标）；
2. 上游回包是宽松 JSON，缺字段 / 单文件失败都不能把前端打挂；
3. 错误要能区分「文件问题 / 服务问题 / 超时」，并且这个能力**不解析模型**。
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.api import agent_apps
from app.core.artifacts import ocr as ocr_module
from app.core.artifacts.ocr import (
    OcrError,
    OcrOptions,
    normalize_ocr_response,
    run_ocr,
)

#: indra.md 里给出的示例回包（稍微补全成两页，覆盖 text / ocr 两种来源）。
SAMPLE_RESPONSE = {
    "model": "indra-ocr",
    "results": [
        {
            "filename": "scan.pdf",
            "n_pages": 2,
            "elapsed_ms": 812.4,
            "error": None,
            "pages": [
                {
                    "index": 0,
                    "source": "ocr",
                    "note": "",
                    "rotation": 0,
                    "skew": 0.0,
                    "vertical": False,
                    "n_lines": 31,
                    "markdown": "# 标题\n\n正文 …",
                    "text": "标题\n正文 …",
                    "lines": [{"box": [72, 96, 521, 130], "text": "标题", "conf": 0.987}],
                    "blocks": [{"label": "Section-Header", "bbox": [72, 96, 521, 130], "text": "标题"}],
                    "timings": {"total_ms": 402.1},
                },
                {
                    "index": 1,
                    "source": "text",
                    "n_lines": 3,
                    "markdown": "第二页",
                    "text": "第二页",
                },
            ],
        }
    ],
}


#: 真实 httpx.Client 的引用（import 时抓，避免被上面的 patch 污染）。
_REAL_CLIENT = httpx.Client


def _patch_transport(monkeypatch, handler) -> list[httpx.Request]:
    """用 MockTransport 替掉真实出站，同时记录发往上游的请求。

    必须用**模块导入时**抓到的原始 ``httpx.Client``：同一个用例里第二次调用本函数时，
    ``httpx.Client`` 已经是上一个替身，直接取会拿到替身自己（请求走旧 transport）。
    """

    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        ocr_module.httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(
            transport=httpx.MockTransport(wrapped), timeout=kwargs.get("timeout")
        ),
    )
    return seen


def _settings(monkeypatch, *, base_url: str = "http://ocr.local:8000", api_key: str = "k-1") -> None:
    monkeypatch.setattr(ocr_module, "get_settings", lambda: _FakeSettings(base_url, api_key))


class _FakeSettings:
    indra_ocr_base_url = "http://ocr.local:8000"
    indra_ocr_api_key = ""
    indra_ocr_timeout_seconds = 30.0
    indra_ocr_max_files = 3
    indra_ocr_max_file_mb = 5

    def __init__(self, base_url: str, api_key: str) -> None:
        self.indra_ocr_base_url = base_url
        self.indra_ocr_api_key = api_key


# ---------------------------------------------------------------------------
# 回包归一化
# ---------------------------------------------------------------------------


def test_normalize_keeps_page_fields_and_summarizes() -> None:
    result = normalize_ocr_response(SAMPLE_RESPONSE, filenames=["scan.pdf"])

    assert result["model"] == "indra-ocr"
    assert result["summary"] == {
        "file_count": 1,
        "page_count": 2,
        "elapsed_ms": 812.4,
        "error_count": 0,
    }
    first, second = result["results"][0]["pages"]
    assert first["source"] == "ocr"
    assert first["lines"][0]["box"] == [72, 96, 521, 130]
    assert first["blocks"][0]["label"] == "Section-Header"
    assert first["timings"] == {"total_ms": 402.1}
    # 缺字段的页要补默认值，前端不用防御
    assert second["source"] == "text"
    assert second["lines"] == []
    assert second["blocks"] == []
    assert second["rotation"] == 0
    assert second["vertical"] is False


def test_normalize_accepts_single_result_and_flags_file_error() -> None:
    body = {
        "model": "indra-ocr",
        "filename": "broken.pdf",
        "n_pages": 0,
        "error": "password required",
        "pages": [],
    }

    result = normalize_ocr_response(body, filenames=["broken.pdf"])

    assert len(result["results"]) == 1
    assert result["results"][0]["error"] == "password required"
    assert result["results"][0]["filename"] == "broken.pdf"
    assert result["summary"]["error_count"] == 1
    assert result["summary"]["page_count"] == 0


def test_normalize_rejects_non_object_body() -> None:
    with pytest.raises(OcrError) as excinfo:
        normalize_ocr_response(["not", "a", "dict"])
    assert excinfo.value.kind == "decode"


def test_normalize_falls_back_to_upload_name_and_unknown_source() -> None:
    body = {
        "results": [
            {
                "pages": [
                    {"index": 0, "source": "hybrid", "markdown": "x"},
                    {"index": 1, "source": "weird", "markdown": "y"},
                ]
            }
        ]
    }

    result = normalize_ocr_response(body, filenames=["a.png"])

    assert result["results"][0]["filename"] == "a.png"
    sources = [page["source"] for page in result["results"][0]["pages"]]
    assert sources == ["hybrid", "ocr"]  # 未知来源收敛成 ocr，不把脏值透给前端


def test_normalize_assigns_page_index_when_upstream_omits_it() -> None:
    """format=md/text 时上游可能不回页码：按数组顺序兜底，别整份都是「第 1 页」。"""

    body = {"results": [{"filename": "a.pdf", "pages": [{"markdown": "一"}, {"markdown": "二"}]}]}

    result = normalize_ocr_response(body)

    assert [page["index"] for page in result["results"][0]["pages"]] == [0, 1]


# ---------------------------------------------------------------------------
# 出站请求
# ---------------------------------------------------------------------------


def test_run_ocr_passes_only_given_options(monkeypatch) -> None:
    _settings(monkeypatch)
    seen = _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=SAMPLE_RESPONSE),
    )

    run_ocr(
        [("scan.pdf", b"%PDF-1.4", "application/pdf")],
        OcrOptions(format="md", mode="ocr", max_pages=5, math=True),
    )

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == "http://ocr.local:8000/v1/ocr"
    assert request.headers["X-API-Key"] == "k-1"
    body = request.read()
    # 明确给的参数要透传
    assert b'name="format"' in body and b"md" in body
    assert b'name="mode"' in body and b"ocr" in body
    assert b'name="max_pages"' in body and b"5" in body
    assert b'name="math"' in body and b"true" in body
    # 没给的布尔项不能出现（否则会覆盖上游默认值）
    assert b'name="furniture"' not in body
    assert b'name="include_lines"' not in body
    assert b'name="include_timings"' not in body
    # 密码为空时不传
    assert b'name="password"' not in body
    assert b'name="files"' in body


def test_run_ocr_rejects_oversized_and_too_many_files(monkeypatch) -> None:
    monkeypatch.setattr(ocr_module, "get_settings", lambda: _FakeSettings("http://ocr.local:8000", "k"))

    with pytest.raises(OcrError) as too_many:
        run_ocr([(f"f{i}.png", b"x", "") for i in range(4)], OcrOptions())
    assert too_many.value.kind == "payload"

    with pytest.raises(OcrError) as too_big:
        run_ocr([("big.pdf", b"x" * (5 * 1024 * 1024 + 1), "")], OcrOptions())
    assert too_big.value.kind == "payload"
    assert "MB" in str(too_big.value)


def test_run_ocr_requires_configured_base_url(monkeypatch) -> None:
    monkeypatch.setattr(ocr_module, "get_settings", lambda: _FakeSettings("", ""))

    with pytest.raises(OcrError) as excinfo:
        run_ocr([("a.png", b"x", "image/png")], OcrOptions())
    assert excinfo.value.kind == "config"


def test_run_ocr_maps_upstream_failures(monkeypatch) -> None:
    _settings(monkeypatch)

    _patch_transport(
        monkeypatch,
        lambda request: httpx.Response(401, json={"detail": "invalid api key"}),
    )
    with pytest.raises(OcrError) as unauthorized:
        run_ocr([("a.png", b"x", "image/png")], OcrOptions())
    assert unauthorized.value.kind == "http"
    assert unauthorized.value.status == 401
    assert "invalid api key" in str(unauthorized.value)

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _patch_transport(monkeypatch, _boom)
    with pytest.raises(OcrError) as unreachable:
        run_ocr([("a.png", b"x", "image/png")], OcrOptions())
    assert unreachable.value.kind == "network"


# ---------------------------------------------------------------------------
# HTTP 语义
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("payload", None, 400),
        ("config", None, 503),
        ("timeout", None, 504),
        ("http", 422, 400),
        ("http", 500, 502),
        ("network", None, 502),
        ("decode", None, 502),
    ],
)
def test_error_mapping(kind: str, status: int | None, expected: int) -> None:
    exc = OcrError("boom", kind=kind, status=status)
    assert agent_apps._ocr_http_error(exc).status_code == expected  # noqa: SLF001


def test_ocr_run_endpoint_needs_no_model(monkeypatch) -> None:
    """OCR 不解析模型：没有 ModelConfig 也能跑通整个接口。"""

    from app.db.models import Tenant, User

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    captured: dict = {}

    def _fake_run(files, options):  # noqa: ANN001
        captured["files"] = files
        captured["options"] = options
        return {"model": "indra-ocr", "results": [], "summary": {}}

    monkeypatch.setattr(agent_apps, "run_ocr", _fake_run)

    with Session(engine) as db:
        db.add(Tenant(id="tenant_ocr", name="Demo"))
        user = User(
            id="user_ocr",
            tenant_id="tenant_ocr",
            username="ocr",
            role="admin",
            password_hash="unused",
        )
        db.add(user)
        db.commit()

        upload = UploadFile(filename="scan.pdf", file=io.BytesIO(b"%PDF-1.4"))
        result = agent_apps.ocr_run(
            tenant_id="tenant_ocr",
            files=[upload],
            format="json",
            mode="",
            dpi=200,
            max_pages=None,
            password="",
            math=True,
            furniture=None,
            image_ocr=None,
            include_lines=True,
            include_blocks=None,
            include_timings=False,
            db=db,
            current_user=user,
        )

    assert result["model"] == "indra-ocr"
    assert captured["files"][0][0] == "scan.pdf"
    assert captured["options"].format == "json"
    assert captured["options"].dpi == 200
    assert captured["options"].math is True
    assert captured["options"].include_timings is False
    # 没给的项保持 None（不透传）
    assert captured["options"].furniture is None


def test_ocr_run_endpoint_validates_ranges() -> None:
    from app.db.models import Tenant

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)

    with Session(engine) as db:
        db.add(Tenant(id="t", name="Demo"))
        db.commit()
        with pytest.raises(HTTPException) as dpi_error:
            agent_apps.ocr_run(
                tenant_id="t",
                files=[],
                format="json",
                mode="",
                dpi=10,
                max_pages=None,
                password="",
                math=None,
                furniture=None,
                image_ocr=None,
                include_lines=None,
                include_blocks=None,
                include_timings=None,
                db=db,
                current_user=None,  # type: ignore[arg-type]
            )
    assert dpi_error.value.status_code == 400


def test_sample_response_matches_documented_shape() -> None:
    """守护 indra.md 的字段名：改名会让前端静默丢字段。"""

    payload = json.loads(json.dumps(SAMPLE_RESPONSE))
    page = payload["results"][0]["pages"][0]
    assert {"index", "source", "note", "rotation", "skew", "vertical", "n_lines"} <= set(page)
    assert {"markdown", "text", "lines", "blocks", "timings"} <= set(page)
