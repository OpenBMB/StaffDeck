### `POST /v1/ocr` — multipart 上传（推荐）

| 字段 | 类型 | 说明 |
|---|---|---|
| `files` | 文件，可多个 | PDF / PNG / JPEG / TIFF / BMP / WebP |
| `format` | `json`（默认）/ `md` / `text` | 返回结构或纯文本 |
| `mode` `dpi` `max_pages` `password` | | 覆盖默认 OCR 参数 |
| `math` `furniture` `image_ocr` | `true/false` | 开关 |
| `include_lines` `include_blocks` | 默认 `true` | 是否带行坐标 / 版面块 |
| `include_timings` | 默认 `false` | 是否带各阶段耗时 |

```bash
# JSON（默认）
curl -s -X POST http://127.0.0.1:8000/v1/ocr \
  -H "X-API-Key: 你的密钥" \
  -F "files=@scan.pdf" -F "include_timings=true" | jq .

# 只要 markdown
curl -s -X POST http://127.0.0.1:8000/v1/ocr -F "files=@scan.pdf" -F "format=md" -o out.md

# 强制 OCR、只跑前 5 页、带公式
curl -s -X POST http://127.0.0.1:8000/v1/ocr \
  -F "files=@paper.pdf" -F "mode=ocr" -F "max_pages=5" -F "math=true"
```

返回（`format=json`，已省略部分行）：

```json
{
  "model": "indra-ocr",
  "results": [
    {
      "filename": "scan.pdf",
      "n_pages": 2,
      "elapsed_ms": 812.4,
      "error": null,
      "pages": [
        {
          "index": 0,
          "source": "ocr",              // ocr | text | hybrid | error
          "note": "",
          "rotation": 0, "skew": 0.0, "vertical": false,
          "n_lines": 31,
          "markdown": "# 标题\n\n正文 …",
          "text": "标题\n正文 …",
          "lines": [{"box": [72, 96, 521, 130], "text": "标题", "conf": 0.987}],
          "blocks": [{"label": "Section-Header", "bbox": [72, 96, 521, 130], "text": "标题"}],
          "timings": {"total_ms": 402.1}
        }
      ]
    }
  ]
}
```

> `source=text` 表示这一页直接用了 PDF 文本层（最快最准），`hybrid` 是文本层 + 图片内 OCR，
> `error` 的页会在 `note` 里写明原因，其它页照常返回。

### `POST /v1/ocr/base64` — JSON + base64（不方便发 multipart 时用）

```bash
curl -s -X POST http://127.0.0.1:8000/v1/ocr/base64 \
  -H "Content-Type: application/json" -H "X-API-Key: 你的密钥" \
  -d "{\"filename\":\"a.pdf\",\"content\":\"$(base64 -w0 a.pdf)\",\"format\":\"md\"}"
```

Python 调用（requests）：

```python
import requests
r = requests.post("http://127.0.0.1:8000/v1/ocr",
                  files=[("files", ("scan.pdf", open("scan.pdf", "rb"), "application/pdf"))],
                  data={"format": "json", "include_blocks": "true"},
                  headers={"X-API-Key": KEY}, timeout=300)
md = r.json()["results"][0]["pages"][0]["markdown"]
```
