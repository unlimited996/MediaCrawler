/**
 * 帖子编辑记录（P2）
 *
 * 抓取检测到内容变化时后端会落一条 story_edit_log，这里做前后比对：
 * 左侧按时间列出历次编辑，右侧双栏展示 before / after 差异。
 */

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import api from '@/lib/api'
import { X, History } from 'lucide-react'


interface EditItem {
  id: number
  edit_ts: number
  changed_fields: string[]
  source: string | null
  chapter_id: number | null
}

interface DiffRow {
  field: string
  before: string
  after: string
  kind: 'text' | 'number'
}

interface EditDetail {
  id: number
  edit_ts: number
  changed_fields: string[]
  before: Record<string, any>
  after: Record<string, any>
  diff: DiffRow[]
  source: string | null
}

const FIELD_LABEL: Record<string, string> = {
  title: '标题',
  content_text: '正文',
  like_count: '点赞数',
  comment_count: '评论数',
}

function fmtTs(ms?: number) {
  if (!ms) return '-'
  const d = new Date(ms)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

export function StoryEditLogDialog({
  storyId, open, onClose,
}: {
  storyId: number
  open: boolean
  onClose: () => void
}) {
  const [activeId, setActiveId] = useState<number | null>(null)

  const listQ = useQuery({
    queryKey: ['storyEdits', storyId],
    enabled: open,
    queryFn: async () =>
      (await api.get<{ items: EditItem[] }>(`/stories/${storyId}/edits`)).data.items,
  })

  const detailQ = useQuery({
    queryKey: ['storyEdit', storyId, activeId],
    enabled: open && activeId !== null,
    queryFn: async () =>
      (await api.get<EditDetail>(`/stories/${storyId}/edits/${activeId}`)).data,
  })

  // 打开时默认选中最新一条
  useEffect(() => {
    if (open && listQ.data?.length && activeId === null) {
      setActiveId(listQ.data[0].id)
    }
  }, [open, listQ.data, activeId])

  if (!open) return null

  const items = listQ.data || []
  const detail = detailQ.data

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
         onClick={onClose}>
      <div
        className="flex h-[70vh] w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel"
        onClick={(e) => e.stopPropagation()}
      >
        {/* 头部 */}
        <div className="flex flex-shrink-0 items-center gap-2 border-b border-cyber-border-subtle px-3 py-2">
          <History className="h-4 w-4 text-cyber-neon-cyan" />
          <span className="text-sm font-mono font-bold text-cyber-text-primary">编辑记录</span>
          <span className="text-[10px] font-mono text-cyber-text-muted">
            {items.length} 条
          </span>
          <button onClick={onClose}
            className="ml-auto flex h-6 w-6 items-center justify-center text-cyber-text-muted hover:text-cyber-text-primary">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          {/* 左：历次编辑 */}
          <div className="w-44 flex-shrink-0 overflow-y-auto border-r border-cyber-border-subtle">
            {listQ.isLoading ? (
              <div className="p-3 text-[11px] font-mono text-cyber-text-muted">加载中…</div>
            ) : items.length === 0 ? (
              <div className="p-3 text-[11px] font-mono text-cyber-text-muted">
                暂无编辑记录（内容未发生变化）
              </div>
            ) : items.map((e) => (
              <button
                key={e.id}
                onClick={() => setActiveId(e.id)}
                className={`block w-full border-l-2 px-2 py-1.5 text-left transition-colors ${
                  activeId === e.id
                    ? 'border-cyber-neon-cyan bg-cyber-bg-tertiary'
                    : 'border-transparent hover:bg-cyber-bg-tertiary/60'
                }`}
              >
                <div className="text-[11px] font-mono text-cyber-text-primary">
                  {fmtTs(e.edit_ts)}
                </div>
                <div className="truncate text-[10px] text-cyber-text-muted">
                  {(e.changed_fields || []).map((f) => FIELD_LABEL[f] || f).join('、')}
                </div>
              </button>
            ))}
          </div>

          {/* 右：前后比对 */}
          <div className="min-w-0 flex-1 overflow-y-auto p-3">
            {detailQ.isLoading ? (
              <div className="text-[11px] font-mono text-cyber-text-muted">加载中…</div>
            ) : !detail ? (
              <div className="text-[11px] font-mono text-cyber-text-muted">
                选择左侧一条记录查看差异
              </div>
            ) : (
              <div className="space-y-2">
                <div className="text-[10px] font-mono text-cyber-text-muted">
                  {fmtTs(detail.edit_ts)} · 来源 {detail.source || '-'}
                </div>
                {(detail.diff || []).length === 0 ? (
                  <div className="text-[11px] text-cyber-text-muted">无字段差异</div>
                ) : (detail.diff || []).map((d) => (
                  <div key={d.field}
                       className="overflow-hidden rounded border border-cyber-border-subtle">
                    <div className="bg-cyber-bg-tertiary px-2 py-1 text-[11px] font-mono text-cyber-text-secondary">
                      {FIELD_LABEL[d.field] || d.field}
                    </div>
                    <div className="grid grid-cols-2 divide-x divide-cyber-border-subtle">
                      <div className="p-2">
                        <div className="mb-1 text-[10px] font-mono text-cyber-text-muted">
                          修改前
                        </div>
                        <div className="max-h-40 overflow-y-auto whitespace-pre-wrap break-all text-[11px] text-cyber-text-secondary">
                          {d.before || '（空）'}
                        </div>
                      </div>
                      <div className="p-2">
                        <div className="mb-1 text-[10px] font-mono text-cyber-text-muted">
                          修改后
                        </div>
                        <div className="max-h-40 overflow-y-auto whitespace-pre-wrap break-all text-[11px] text-cyber-text-primary">
                          {d.after || '（空）'}
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
