import { ConfirmDialog } from '@/components/ConfirmDialog';
import EmployeeAvatar from '@/components/EmployeeAvatar';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { employeeDisplayNameWithCreator, employeeProfile } from '@/employee';

import {
  CHAT_CITATION_DETAIL_CLASS,
  CHAT_CITATION_DETAIL_EYEBROW_CLASS,
  CHAT_CITATION_DETAIL_GRID_CLASS,
  CHAT_CITATION_DETAIL_MARKDOWN_CLASS,
  CHAT_CITATION_DETAIL_NOTE_CLASS,
  CHAT_CITATION_DETAIL_SECTION_CLASS,
  CHAT_CITATION_DETAIL_TITLE_CLASS,
  CHAT_HANDOFF_ACTIONS_CLASS,
  CHAT_HANDOFF_BLOCK_CLASS,
  CHAT_HANDOFF_CARD_CLASS,
  CHAT_HANDOFF_EMPTY_CLASS,
  CHAT_HANDOFF_HEAD_CLASS,
  CHAT_HANDOFF_LIST_CLASS,
} from '../chatPageStyles';
import {
  citationDisplayTitle,
  citationKindLabel,
  citationSectionLabel,
  citationSourceLabel,
  MarkdownMessage,
} from '../chatHelpers';
import type { UseChatSession } from '../useChatSession';

export default function ChatDialogs({ chat }: { chat: UseChatSession }) {
  const {
    showHandoffInbox,
    setShowHandoffInbox,
    handoffs,
    handoffsLoading,
    handoffReplies,
    setHandoffReplies,
    submitHandoffReply,
    agents,
    displayedAgent,
    displayedProfile,
    openSession,
    activeCitation,
    setActiveCitation,
    renameSession,
    setRenameSession,
    renameTitle,
    setRenameTitle,
    saveRename,
    pendingDelete,
    setPendingDelete,
    confirmDeleteSession,
  } = chat;

  return (
    <>
      <Dialog open={showHandoffInbox} onOpenChange={(open) => !open && setShowHandoffInbox(false)}>
        <DialogContent className="max-w-[min(920px,calc(100vw-40px))] sm:max-w-[920px]">
          <DialogHeader>
            <DialogTitle>待回答</DialogTitle>
          </DialogHeader>
          {handoffs.length === 0 ? (
            <div className={CHAT_HANDOFF_EMPTY_CLASS}>
              {handoffsLoading ? '正在加载待回答消息' : '暂无待回答消息'}
            </div>
          ) : (
            <div className={CHAT_HANDOFF_LIST_CLASS}>
              {handoffs.map((handoff) => {
                const handoffAgent = handoff.agent_id
                  ? agents.find((item) => item.id === handoff.agent_id) || null
                  : displayedAgent;
                const profile = handoffAgent ? employeeProfile(handoffAgent) : displayedProfile;
                return (
                  <article className={CHAT_HANDOFF_CARD_CLASS} key={handoff.id}>
                    <div className={CHAT_HANDOFF_HEAD_CLASS}>
                      {profile ? <EmployeeAvatar profile={profile} size={36} radius={10} /> : null}
                      <div>
                        <strong>{handoffAgent ? employeeDisplayNameWithCreator(handoffAgent) : '数字员工'}</strong>
                        <span>需要人工接续</span>
                      </div>
                    </div>
                    <div className={CHAT_HANDOFF_BLOCK_CLASS}>
                      <span>上下文摘要</span>
                      <p>{handoff.context_summary || '暂无上下文摘要'}</p>
                    </div>
                    <div className={CHAT_HANDOFF_BLOCK_CLASS}>
                      <span>这一步需要你处理</span>
                      <p>{handoff.pending_question || '请根据当前会话补充人工回复。'}</p>
                    </div>
                    <Textarea
                      rows={3}
                      value={handoffReplies[handoff.id] || ''}
                      placeholder="以当前数字员工的口吻回复。提交后，原会话会继续推进 SOP 流程。"
                      onChange={(event) => setHandoffReplies((prev) => ({
                        ...prev,
                        [handoff.id]: event.target.value,
                      }))}
                    />
                    <div className={CHAT_HANDOFF_ACTIONS_CLASS}>
                      <Button
                        variant="outline"
                        onClick={() => {
                          setShowHandoffInbox(false);
                          openSession(handoff.session_id);
                        }}
                      >
                        打开原会话
                      </Button>
                      <Button onClick={() => submitHandoffReply(handoff)}>回复并恢复</Button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(activeCitation)} onOpenChange={(open) => !open && setActiveCitation(null)}>
        <DialogContent className="max-w-[min(1160px,calc(100vw-40px))] sm:max-w-[1160px]">
          <DialogHeader>
            <DialogTitle>引用详情</DialogTitle>
          </DialogHeader>
          {activeCitation && (
            <div className={CHAT_CITATION_DETAIL_CLASS}>
              <div className={CHAT_CITATION_DETAIL_EYEBROW_CLASS}>{citationKindLabel(activeCitation)}</div>
              <h3 className={CHAT_CITATION_DETAIL_TITLE_CLASS}>{citationDisplayTitle(activeCitation)}</h3>
              {citationDetailExcerpt(activeCitation) && (
                <div className={CHAT_CITATION_DETAIL_SECTION_CLASS}>
                  <span>引用片段</span>
                  <CitationMarkdown content={citationDetailExcerpt(activeCitation)} />
                </div>
              )}
              {citationDetailSummary(activeCitation) && (
                <div className={CHAT_CITATION_DETAIL_SECTION_CLASS}>
                  <span>片段摘要</span>
                  <CitationMarkdown content={citationDetailSummary(activeCitation)} />
                </div>
              )}
              {(activeCitation.source_path || activeCitation.section_path || activeCitation.concept_id) && (
                <div className={CHAT_CITATION_DETAIL_GRID_CLASS}>
                  {activeCitation.source_path && (
                    <div>
                      <span>来源</span>
                      <strong>{citationSourceLabel(activeCitation)}</strong>
                    </div>
                  )}
                  {activeCitation.section_path && (
                    <div>
                      <span>章节</span>
                      <strong>{citationSectionLabel(activeCitation)}</strong>
                    </div>
                  )}
                  {activeCitation.concept_id && (
                    <div>
                      <span>知识图谱</span>
                      <strong>{activeCitation.concept_id}</strong>
                    </div>
                  )}
                </div>
              )}
              {activeCitation.confidence_reason && (
                <div className={CHAT_CITATION_DETAIL_NOTE_CLASS}>{activeCitation.confidence_reason}</div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(renameSession)}
        onOpenChange={(open) => {
          if (!open) {
            setRenameSession(null);
            setRenameTitle('');
          }
        }}
      >
        <DialogContent className="gap-0 overflow-hidden rounded-[16px] p-0">
          <DialogHeader className="px-[16px] pt-[16px] pb-[12px]">
            <DialogTitle className="text-[14px] leading-[normal] font-medium text-[#18181a]">
              重命名
            </DialogTitle>
          </DialogHeader>
          <div className="px-[16px] pb-[4px]">
            <Input
              autoFocus
              maxLength={80}
              value={renameTitle}
              onChange={(event) => setRenameTitle(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void saveRename();
                }
              }}
              placeholder="输入会话名称"
            />
          </div>
          <div className="flex items-center justify-end gap-[8px] pt-[12px] pr-[16px] pb-[16px] pl-[12px]">
            <Button
              variant="outline"
              className="h-[32px] w-[80px] rounded-[10px] border-[#e3e7f1] bg-white px-[12px] py-[8px] text-[14px] font-normal text-[#464c5e] hover:border-[#e3e7f1] hover:bg-[#f6f6f6] hover:text-[#18181a]"
              onClick={() => {
                setRenameSession(null);
                setRenameTitle('');
              }}
            >
              取消
            </Button>
            <Button
              className="h-[32px] w-[80px] rounded-[10px] bg-[#18181a] px-[12px] py-[8px] text-[14px] font-normal text-white hover:bg-[#303030]"
              onClick={() => void saveRename()}
            >
              保存
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(pendingDelete)}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title="删除会话"
        description="删除后无法恢复该会话及其消息记录，确定继续吗？"
        onConfirm={() => void confirmDeleteSession()}
      />
    </>
  );
}

function CitationMarkdown({ content }: { content: string }) {
  return (
    <div className={CHAT_CITATION_DETAIL_MARKDOWN_CLASS}>
      <MarkdownMessage content={content} />
    </div>
  );
}

function citationDetailExcerpt(citation: NonNullable<UseChatSession['activeCitation']>): string {
  return String(citation.content || citation.excerpt || citation.summary || '').trim();
}

function citationDetailSummary(citation: NonNullable<UseChatSession['activeCitation']>): string {
  const summary = String(citation.summary || '').trim();
  const content = String(citation.content || citation.excerpt || '').trim();
  return summary && summary !== content ? summary : '';
}
