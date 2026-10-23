import { Check, Code2, Copy, KeyRound } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';

import { fetchOcrApiAccess, type OcrApiAccess } from '@/api/ocr';
import { Button, Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, notify } from '@/components/ui';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { copyTextToClipboard } from '@/lib/clipboard';

import { buildOcrApiSamples } from './ocrApiSamples';

// 接口未就绪时的兜底值；后端 /api/enterprise/agent-apps/ocr:api-access 给的是权威值
const OPEN_API_SCOPE = 'ocr:run';
const OPEN_API_PATH = '/api/v1/ocr';

type SampleLanguage = 'curl' | 'python' | 'node';

const LANGUAGES: { id: SampleLanguage; label: string }[] = [
  { id: 'curl', label: 'cURL' },
  { id: 'python', label: 'Python' },
  { id: 'node', label: 'Node.js' },
];

/**
 * OCR 助手的「API 接入」弹窗：说明 + 三种语言的 multipart 示例。
 *
 * 与「决策助手」的接入弹窗同一套结构，差别只在请求体是 multipart 文件上传，
 * 示例由 `ocrApiSamples.ts` 生成（那里刻意不写中文，见文件头注释）。
 */
export default function OcrApiDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [access, setAccess] = useState<OcrApiAccess | null>(null);
  const [accessFailed, setAccessFailed] = useState(false);
  const [language, setLanguage] = useState<SampleLanguage>('curl');
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    if (!open || access || accessFailed) return;
    let active = true;
    fetchOcrApiAccess()
      .then((value) => {
        if (active) setAccess(value);
      })
      .catch(() => {
        if (active) setAccessFailed(true);
      });
    return () => {
      active = false;
    };
  }, [open, access, accessFailed]);

  useEffect(() => {
    if (open) setCopied(null);
  }, [open]);

  const baseUrl = (access?.public_base_url?.trim() || (typeof window === 'undefined' ? '' : window.location.origin))
    .replace(/\/+$/, '');
  const endpointUrl = `${baseUrl}${access?.endpoint_path || OPEN_API_PATH}`;
  const requiredScope = access?.required_scope || OPEN_API_SCOPE;

  const samples = useMemo(() => buildOcrApiSamples(endpointUrl), [endpointUrl]);
  const code = samples[language];

  async function copySample() {
    try {
      await copyTextToClipboard(code);
      setCopied(language);
      notify.success('已复制调用示例');
    } catch {
      setCopied(null);
      notify.error('复制失败，请手动选中代码复制');
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => { if (!next) onClose(); }}>
      <DialogContent
        aria-describedby="ocr-api-description"
        className="flex max-h-[calc(100dvh-3rem)] w-[calc(100%-2rem)] flex-col gap-0 overflow-hidden rounded-[18px] border-0 bg-[#f7f8fa] p-0 shadow-[0_28px_80px_rgba(24,31,46,0.20)] sm:max-w-[820px]"
      >
        <DialogHeader className="border-b border-[#e9ecf2] bg-white px-[26px] py-[22px]">
          <div className="flex items-center gap-[12px]">
            <span className="grid size-[38px] place-items-center rounded-[12px] bg-[#18181a] text-white">
              <Code2 className="size-[18px]" />
            </span>
            <div>
              <DialogTitle className="text-[16px] font-semibold text-[#18181a]">API 接入 · OCR 助手</DialogTitle>
              <DialogDescription id="ocr-api-description" className="mt-[5px] text-[12px] text-[#757f9c]">
                用账号 API 密钥直接调用同一个识别服务，无需打开这个页面。
              </DialogDescription>
            </div>
          </div>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-[18px] overflow-y-auto px-[26px] py-[22px]">
          <section className="rounded-[16px] border border-[#cfe7dc] bg-[#eef8f3] p-[18px]">
            <div className="flex items-center gap-[8px] text-[13px] font-semibold text-[#18181a]">
              <KeyRound className="size-[15px] text-[#207451]" />
              第一步 · 拿到账号 API 全量密钥
            </div>
            <ol className="mt-[10px] grid gap-[6px] text-[12px] leading-[19px] text-[#464c5e]">
              <li>1. 点击右上角头像，选择「API 全量密钥」。</li>
              <li>2. 创建密钥并复制明文（仅显示一次，页面关掉就看不到）。</li>
              <li>
                3. 账号级密钥默认已包含{' '}
                <code className="rounded-[4px] bg-white/70 px-[4px] font-mono text-[11px]">{requiredScope}</code>{' '}
                权限，无需额外授权。
              </li>
            </ol>
            <p className="mt-[10px] text-[11px] leading-[17px] text-[#7f897f]">
              密钥等同于你的账号身份，不要写进前端代码或提交到仓库；泄露后到同一个入口吊销即可。
            </p>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="text-[13px] font-semibold text-[#18181a]">第二步 · 调用端点</div>
            <div className="mt-[10px] flex flex-wrap items-center gap-[8px]">
              <span className="rounded-[6px] bg-[#18181a] px-[8px] py-[3px] font-mono text-[10px] font-semibold text-white">
                POST
              </span>
              <code className="min-w-0 flex-1 truncate rounded-[8px] bg-[#f4f5f8] px-[10px] py-[6px] font-mono text-[11.5px] text-[#18181a]">
                {endpointUrl}
              </code>
            </div>
            <dl className="mt-[12px] grid gap-[8px] text-[12px] sm:grid-cols-[84px_minmax(0,1fr)]">
              <dt className="text-[#757f9c]">请求头</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">Authorization: Bearer sd_live_…</code>
              </dd>
              <dt className="text-[#757f9c]">内容类型</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">multipart/form-data</code>
              </dd>
              <dt className="text-[#757f9c]">所需权限</dt>
              <dd>
                <code className="font-mono text-[11.5px] text-[#464c5e]">{requiredScope}</code>
              </dd>
              <dt className="text-[#757f9c]">请求体</dt>
              <dd className="text-[#464c5e]">
                <code className="font-mono text-[11.5px]">files</code> 可传多个，其余字段见下方「接口说明」。
              </dd>
            </dl>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="flex flex-wrap items-center justify-between gap-[10px]">
              <span className="text-[13px] font-semibold text-[#18181a]">第三步 · 复制示例代码</span>
              <div className="flex items-center gap-[6px]">
                <Button
                  type="button"
                  onClick={() => void copySample()}
                  className="h-[26px] gap-[5px] rounded-[8px] bg-[#1d2027] px-[10px] text-[11px] text-white hover:bg-[#2b2f38]"
                >
                  {copied === language ? <Check className="size-[12px]" /> : <Copy className="size-[12px]" />}
                  {copied === language ? '已复制' : '复制'}
                </Button>
              </div>
            </div>

            <Tabs value={language} onValueChange={(value) => setLanguage(value as SampleLanguage)} className="mt-[12px] gap-0">
              <TabsList variant="line" className="h-auto w-full justify-start gap-[4px] rounded-none p-0">
                {LANGUAGES.map((item) => (
                  <TabsTrigger
                    key={item.id}
                    value={item.id}
                    className="h-[28px] flex-none rounded-[8px] px-[12px] text-[12px] data-active:bg-[#f4f5f8] data-active:text-[#18181a]"
                  >
                    {item.label}
                  </TabsTrigger>
                ))}
              </TabsList>

              {LANGUAGES.map((item) => (
                <TabsContent key={item.id} value={item.id} className="mt-[10px]">
                  <pre
                    data-testid={`ocr-api-sample-${item.id}`}
                    className="max-h-[340px] overflow-auto rounded-[14px] bg-[#1d2027] p-[14px] font-mono text-[11.5px] leading-[18px] text-[#e7ebf3]"
                  >
                    {samples[item.id]}
                  </pre>
                </TabsContent>
              ))}
            </Tabs>
          </section>

          <section className="rounded-[16px] border-[0.5px] border-[#e3e7f1] bg-white p-[18px]">
            <div className="text-[13px] font-semibold text-[#18181a]">接口说明</div>
            <div className="mt-[10px] grid gap-[8px] text-[12px] leading-[18px] text-[#464c5e]">
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">files</code>
                ：PDF / PNG / JPEG / TIFF / BMP / WebP，单次多个，字段名重复即可。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">format</code>
                ：<code className="font-mono text-[11.5px]">json</code>（默认，带行坐标与版面块）/
                <code className="font-mono text-[11.5px]">md</code> /<code className="font-mono text-[11.5px]">text</code>。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">max_pages</code>
                ：只跑前 N 页；<code className="font-mono text-[11.5px]">password</code>：加密 PDF 的口令；
                <code className="font-mono text-[11.5px]">math</code>：公式转 LaTeX；
                <code className="font-mono text-[11.5px]">image_ocr</code>：识别插图里的文字。
              </p>
              <p>
                <code className="font-mono text-[11.5px] text-[#18181a]">dpi</code> 默认 200、
                <code className="font-mono text-[11.5px]">furniture</code> 默认过滤页眉页脚、
                <code className="font-mono text-[11.5px]">include_lines</code> /
                <code className="font-mono text-[11.5px]">include_blocks</code> 默认开启 —— 与工作台取值一致，一般不用传。
              </p>
              <p className="text-[11.5px] text-[#757f9c]">
                回包按文件、按页给出：<code className="font-mono text-[11px]">source</code> 说明这页走的文本层 / OCR / 混合，
                单页失败写在 <code className="font-mono text-[11px]">note</code> 里，其它页照常返回。
              </p>
            </div>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
