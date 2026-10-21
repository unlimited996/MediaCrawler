/**
 * 回复态度标签库管理页（P3）
 *
 * 标签与判定规则全部存库，这里提供编辑入口：
 * - 标签增删改查、启用/停用、排序与优先级
 * - 每个标签的判定描述与示例维护
 * - 规则（keyword / regex / sentiment / laughter / popular）增删改查与启停
 * - 试判定：输入一句话立即看命中哪个标签（改完下次判定即生效）
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import axios from 'axios'
import {
  Plus, Trash2, Power, Save, RefreshCw, Wand2, ChevronDown, ChevronRight,
} from 'lucide-react'
import { toast } from 'sonner'

const api = axios.create({ baseURL: '/api' })

interface TagRule {
  id: number
  tag_id: number
  match_type: string
  pattern: string
  weight: number
  enabled: boolean
}

interface StanceTag {
  id: number
  code: string
  name: string
  polarity: string
  color: string | null
  description: string
  examples: string[]
  enabled: boolean
  sort_order: number
  priority: number
  is_fallback: boolean
  rule_count: number
}

const MATCH_TYPES = [
  { value: 'keyword', label: '关键词（子串包含）' },
  { value: 'regex', label: '正则' },
  { value: 'sentiment', label: '情感映射（情感|帖子立场）' },
  { value: 'laughter', label: '纯笑声' },
  { value: 'popular', label: '高赞比例（≥阈值）' },
]

const POLARITIES = [
  { value: 'agree', label: '赞同' },
  { value: 'neutral', label: '中立' },
  { value: 'oppose', label: '反对' },
]

export function StanceTagsView() {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState<number | null>(null)
  const [editing, setEditing] = useState<StanceTag | null>(null)
  const [draft, setDraft] = useState<Partial<StanceTag>>({})
  const [newRule, setNewRule] = useState<Record<number, Partial<TagRule>>>({})
  const [previewText, setPreviewText] = useState('')
  const [previewPost, setPreviewPost] = useState('')

  const tagsQ = useQuery({
    queryKey: ['stanceTags'],
    queryFn: async () => (await api.get<StanceTag[]>('/stance-tags')).data,
  })
  const rulesQ = useQuery({
    queryKey: ['stanceRules', expanded],
    enabled: expanded !== null,
    queryFn: async () =>
      (await api.get<TagRule[]>(`/stance-tags/${expanded}/rules`)).data,
  })
  const versionQ = useQuery({
    queryKey: ['stanceVersion'],
    queryFn: async () => (await api.get<{ version: number }>('/stance-tags/version')).data,
  })

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['stanceTags'] })
    qc.invalidateQueries({ queryKey: ['stanceRules'] })
    qc.invalidateQueries({ queryKey: ['stanceVersion'] })
  }

  const createM = useMutation({
    mutationFn: async (p: Partial<StanceTag>) => (await api.post('/stance-tags', p)).data,
    onSuccess: () => { invalidate(); toast.success('标签已新增') },
    onError: (e: any) => toast.error(`新增失败：${e?.response?.data?.detail || e?.message}`),
  })
  const updateM = useMutation({
    mutationFn: async ({ id, p }: { id: number; p: Partial<StanceTag> }) =>
      (await api.put(`/stance-tags/${id}`, p)).data,
    onSuccess: () => { invalidate(); setEditing(null); toast.success('已保存，下次判定即生效') },
    onError: (e: any) => toast.error(`保存失败：${e?.response?.data?.detail || e?.message}`),
  })
  const toggleM = useMutation({
    mutationFn: async ({ id, enabled }: { id: number; enabled: boolean }) =>
      (await api.post(`/stance-tags/${id}/toggle`, { enabled })).data,
    onSuccess: () => { invalidate(); toast.success('已更新') },
  })
  const delM = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/stance-tags/${id}`)).data,
    onSuccess: (d: any) => {
      invalidate()
      toast.success(d?.status === 'disabled' ? '已有评论引用，已改为停用' : '已删除')
    },
    onError: (e: any) => toast.error(`删除失败：${e?.response?.data?.detail || e?.message}`),
  })
  const addRuleM = useMutation({
    mutationFn: async (p: Partial<TagRule>) =>
      (await api.post(`/stance-tags/${p.tag_id}/rules`, p)).data,
    onSuccess: (_d, v) => {
      invalidate()
      setNewRule((m) => ({ ...m, [v.tag_id as number]: {} }))
      toast.success('规则已新增')
    },
    onError: (e: any) => toast.error(`新增失败：${e?.response?.data?.detail || e?.message}`),
  })
  const delRuleM = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/stance-tags/rules/${id}`)).data,
    onSuccess: () => { invalidate(); toast.success('规则已删除') },
  })
  const toggleRuleM = useMutation({
    mutationFn: async ({ id, enabled }: { id: number; enabled: boolean }) =>
      (await api.put(`/stance-tags/rules/${id}`, { enabled })).data,
    onSuccess: () => invalidate(),
  })
  const previewM = useMutation({
    mutationFn: async () =>
      (await api.post('/stance-tags/preview',
        { text: previewText, post_title: previewPost })).data,
  })
  const refreshM = useMutation({
    mutationFn: async () => (await api.post('/stance-tags/refresh')).data,
    onSuccess: (d: any) => { invalidate(); toast.success(`缓存已刷新，version=${d?.version}`) },
  })

  const tags = tagsQ.data || []

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* 头部 */}
      <div className="flex flex-shrink-0 items-center gap-2 rounded-lg border border-cyber-border-subtle px-3 py-2">
        <Wand2 className="h-4 w-4 text-cyber-neon-purple" />
        <div className="min-w-0">
          <h2 className="text-sm font-mono font-bold text-cyber-text-primary">
            回复态度标签库
          </h2>
          <p className="text-[10px] font-mono text-cyber-text-muted">
            STANCE TAGS · version {versionQ.data?.version ?? '-'} · {tags.length} 个标签
          </p>
        </div>
        <div className="ml-auto flex flex-shrink-0 items-center gap-2">
          <button
            onClick={() => refreshM.mutate()}
            className="flex h-8 items-center gap-1 rounded-md border border-cyber-border-subtle px-2 text-[11px] font-mono text-cyber-text-secondary hover:text-cyber-text-primary"
            title="强制刷新判定缓存"
          >
            <RefreshCw className="h-3 w-3" /> 刷新缓存
          </button>
          <button
            onClick={() => {
              setEditing(null)
              setDraft({ polarity: 'neutral', priority: 100, enabled: true,
                         examples: [], is_fallback: false })
              setExpanded(null)
            }}
            className="flex h-8 items-center gap-1 rounded-md bg-cyber-neon-purple px-2 text-[11px] font-mono font-bold text-white"
          >
            <Plus className="h-3 w-3" /> 新增标签
          </button>
        </div>
      </div>

      <div className="flex min-h-0 flex-1 gap-3 overflow-hidden">
        {/* 左：标签列表 */}
        <div className="flex min-h-0 w-full flex-col gap-1.5 overflow-y-auto pr-1 lg:w-1/2">
          {tagsQ.isLoading ? (
            <div className="py-6 text-center text-xs font-mono text-cyber-text-muted">加载中…</div>
          ) : tags.length === 0 ? (
            <div className="py-6 text-center text-xs font-mono text-cyber-text-muted">
              暂无标签，点右上角「新增标签」
            </div>
          ) : tags.map((tag) => {
            const open = expanded === tag.id
            return (
              <div
                key={tag.id}
                className="rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel"
                style={{ borderLeft: `3px solid ${tag.color || '#888'}` }}
              >
                <div className="flex items-center gap-2 px-2.5 py-2">
                  <button
                    onClick={() => setExpanded(open ? null : tag.id)}
                    className="text-cyber-text-muted hover:text-cyber-text-primary"
                    title="展开/收起规则"
                  >
                    {open ? <ChevronDown className="h-3.5 w-3.5" />
                          : <ChevronRight className="h-3.5 w-3.5" />}
                  </button>
                  <span
                    className="rounded px-1.5 py-0.5 text-[11px] font-mono font-bold"
                    style={{ color: tag.color || '#888',
                             background: `${tag.color || '#888'}22` }}
                  >
                    {tag.name}
                  </span>
                  <span className="text-[10px] font-mono text-cyber-text-muted">
                    {tag.code} · {tag.polarity} · 优先级 {tag.priority} · {tag.rule_count} 条规则
                  </span>
                  {tag.is_fallback && (
                    <span className="rounded bg-cyber-neon-cyan/20 px-1 text-[10px] font-mono text-cyber-neon-cyan">
                      兜底
                    </span>
                  )}
                  <div className="ml-auto flex items-center gap-1">
                    <button
                      onClick={() => toggleM.mutate({ id: tag.id, enabled: !tag.enabled })}
                      title={tag.enabled ? '停用' : '启用'}
                      className={`flex h-6 w-6 items-center justify-center rounded border ${
                        tag.enabled
                          ? 'border-cyber-neon-green/50 text-cyber-neon-green'
                          : 'border-cyber-border-subtle text-cyber-text-muted'
                      }`}
                    >
                      <Power className="h-3 w-3" />
                    </button>
                    <button
                      onClick={() => { setEditing(tag); setDraft({ ...tag }); setExpanded(tag.id) }}
                      className="h-6 rounded border border-cyber-border-subtle px-1.5 text-[10px] font-mono text-cyber-text-secondary hover:text-cyber-text-primary"
                    >
                      编辑
                    </button>
                    <button
                      onClick={() => {
                        if (window.confirm(`确定删除标签「${tag.name}」？`)) delM.mutate(tag.id)
                      }}
                      className="flex h-6 w-6 items-center justify-center rounded border border-cyber-border-subtle text-cyber-text-muted hover:text-cyber-neon-pink"
                    >
                      <Trash2 className="h-3 w-3" />
                    </button>
                  </div>
                </div>

                {open && (
                  <div className="border-t border-cyber-border-subtle px-2.5 py-2">
                    <p className="mb-1.5 text-[11px] text-cyber-text-secondary">
                      {tag.description || '（无描述）'}
                    </p>
                    {!!(tag.examples || []).length && (
                      <div className="mb-2 flex flex-wrap gap-1">
                        {tag.examples.map((x, i) => (
                          <span key={i}
                            className="rounded bg-cyber-bg-tertiary px-1.5 py-0.5 text-[10px] text-cyber-text-muted">
                            {x}
                          </span>
                        ))}
                      </div>
                    )}

                    {/* 规则列表 */}
                    <div className="mb-1.5 text-[10px] font-mono uppercase text-cyber-text-muted">
                      判定规则
                    </div>
                    <div className="space-y-1">
                      {rulesQ.isLoading ? (
                        <div className="text-[11px] text-cyber-text-muted">加载中…</div>
                      ) : (rulesQ.data || []).length === 0 ? (
                        <div className="text-[11px] text-cyber-text-muted">暂无规则</div>
                      ) : (rulesQ.data || []).map((r) => (
                        <div key={r.id}
                          className="flex items-center gap-1.5 rounded bg-cyber-bg-tertiary px-1.5 py-1">
                          <span className="w-16 flex-shrink-0 text-[10px] font-mono text-cyber-neon-cyan">
                            {r.match_type}
                          </span>
                          <span className={`min-w-0 flex-1 truncate text-[11px] font-mono ${
                            r.enabled ? 'text-cyber-text-primary' : 'text-cyber-text-muted line-through'
                          }`} title={r.pattern}>
                            {r.pattern || '（空）'}
                          </span>
                          <span className="flex-shrink-0 text-[10px] font-mono text-cyber-text-muted">
                            {r.weight}
                          </span>
                          <button
                            onClick={() => toggleRuleM.mutate({ id: r.id, enabled: !r.enabled })}
                            className={`flex h-5 w-5 flex-shrink-0 items-center justify-center rounded border ${
                              r.enabled ? 'border-cyber-neon-green/50 text-cyber-neon-green'
                                        : 'border-cyber-border-subtle text-cyber-text-muted'
                            }`}
                          >
                            <Power className="h-2.5 w-2.5" />
                          </button>
                          <button
                            onClick={() => delRuleM.mutate(r.id)}
                            className="flex h-5 w-5 flex-shrink-0 items-center justify-center text-cyber-text-muted hover:text-cyber-neon-pink"
                          >
                            <Trash2 className="h-3 w-3" />
                          </button>
                        </div>
                      ))}
                    </div>

                    {/* 新增规则 */}
                    <div className="mt-2 flex items-center gap-1">
                      <select
                        value={(newRule[tag.id]?.match_type) || 'keyword'}
                        onChange={(e) => setNewRule((m) => ({
                          ...m, [tag.id]: { ...m[tag.id], match_type: e.target.value } }))}
                        className="h-7 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1 text-[10px] font-mono text-cyber-text-primary"
                      >
                        {MATCH_TYPES.map((x) => (
                          <option key={x.value} value={x.value}>{x.label}</option>
                        ))}
                      </select>
                      <input
                        value={(newRule[tag.id]?.pattern) || ''}
                        onChange={(e) => setNewRule((m) => ({
                          ...m, [tag.id]: { ...m[tag.id], pattern: e.target.value } }))}
                        placeholder="匹配内容（关键词/正则/阈值）"
                        className="h-7 min-w-0 flex-1 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 text-[11px] font-mono text-cyber-text-primary"
                      />
                      <input
                        type="number" step="0.1" defaultValue={1}
                        onChange={(e) => setNewRule((m) => ({
                          ...m, [tag.id]: { ...m[tag.id], weight: Number(e.target.value) } }))}
                        title="权重"
                        className="h-7 w-14 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1 text-[11px] font-mono text-cyber-text-primary"
                      />
                      <button
                        onClick={() => addRuleM.mutate({
                          tag_id: tag.id,
                          match_type: newRule[tag.id]?.match_type || 'keyword',
                          pattern: newRule[tag.id]?.pattern || '',
                          weight: newRule[tag.id]?.weight ?? 1,
                        })}
                        disabled={!newRule[tag.id]?.pattern}
                        className="flex h-7 flex-shrink-0 items-center gap-1 rounded bg-cyber-neon-cyan px-1.5 text-[10px] font-mono font-bold text-black disabled:opacity-40"
                      >
                        <Plus className="h-3 w-3" /> 规则
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )
          })}
        </div>

        {/* 右：编辑区 + 试判定 */}
        <div className="hidden min-h-0 w-1/2 flex-col gap-3 overflow-y-auto lg:flex">
          {/* 编辑 / 新增表单 */}
          <div className="rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel p-3">
            <div className="mb-2 text-xs font-mono font-bold text-cyber-text-primary">
              {editing ? `编辑标签：${editing.name}` : '新增标签'}
            </div>
            <div className="grid grid-cols-2 gap-2">
              <label className="text-[11px] text-cyber-text-secondary">
                编码 code
                <input
                  value={draft.code ?? ''}
                  disabled={!!editing}
                  onChange={(e) => setDraft({ ...draft, code: e.target.value })}
                  placeholder="如 sarcasm"
                  className="mt-0.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 font-mono text-cyber-text-primary disabled:opacity-50"
                />
              </label>
              <label className="text-[11px] text-cyber-text-secondary">
                名称
                <input
                  value={draft.name ?? ''}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  placeholder="如 阴阳"
                  className="mt-0.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 text-cyber-text-primary"
                />
              </label>
              <label className="text-[11px] text-cyber-text-secondary">
                极性（归入赞同度）
                <select
                  value={draft.polarity ?? 'neutral'}
                  onChange={(e) => setDraft({ ...draft, polarity: e.target.value })}
                  className="mt-0.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 text-cyber-text-primary"
                >
                  {POLARITIES.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}
                </select>
              </label>
              <label className="text-[11px] text-cyber-text-secondary">
                颜色
                <input
                  value={draft.color ?? ''}
                  onChange={(e) => setDraft({ ...draft, color: e.target.value })}
                  placeholder="#fb923c"
                  className="mt-0.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 font-mono text-cyber-text-primary"
                />
              </label>
              <label className="text-[11px] text-cyber-text-secondary">
                优先级（越小越先匹配）
                <input
                  type="number"
                  value={draft.priority ?? 100}
                  onChange={(e) => setDraft({ ...draft, priority: Number(e.target.value) })}
                  className="mt-0.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 font-mono text-cyber-text-primary"
                />
              </label>
              <label className="flex items-end gap-3 pb-1 text-[11px] text-cyber-text-secondary">
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={!!draft.enabled}
                    onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} />
                  启用
                </label>
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={!!draft.is_fallback}
                    onChange={(e) => setDraft({ ...draft, is_fallback: e.target.checked })} />
                  兜底标签
                </label>
              </label>
            </div>
            <label className="mt-2 block text-[11px] text-cyber-text-secondary">
              判定描述
              <textarea
                value={draft.description ?? ''}
                onChange={(e) => setDraft({ ...draft, description: e.target.value })}
                rows={2}
                className="mt-0.5 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 py-1 text-[11px] text-cyber-text-primary"
              />
            </label>
            <label className="mt-2 block text-[11px] text-cyber-text-secondary">
              示例（每行一条）
              <textarea
                value={(draft.examples || []).join('\n')}
                onChange={(e) => setDraft({
                  ...draft,
                  examples: e.target.value.split('\n').map((x) => x.trim()).filter(Boolean),
                })}
                rows={3}
                className="mt-0.5 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 py-1 text-[11px] text-cyber-text-primary"
              />
            </label>
            <div className="mt-2 flex items-center gap-2">
              <button
                onClick={() => {
                  if (!draft.code || !draft.name) { toast.error('编码与名称必填'); return }
                  if (editing) updateM.mutate({ id: editing.id, p: draft })
                  else createM.mutate(draft)
                }}
                className="flex h-8 items-center gap-1 rounded bg-cyber-neon-cyan px-2 text-[11px] font-mono font-bold text-black"
              >
                <Save className="h-3 w-3" /> {editing ? '保存' : '创建'}
              </button>
              {editing && (
                <button
                  onClick={() => { setEditing(null); setDraft({}) }}
                  className="h-8 rounded border border-cyber-border-subtle px-2 text-[11px] font-mono text-cyber-text-secondary"
                >
                  取消
                </button>
              )}
            </div>
          </div>

          {/* 试判定 */}
          <div className="rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel p-3">
            <div className="mb-2 text-xs font-mono font-bold text-cyber-text-primary">试判定</div>
            <input
              value={previewPost}
              onChange={(e) => setPreviewPost(e.target.value)}
              placeholder="帖子标题（可选，用于判定帖子立场）"
              className="mb-1.5 h-7 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 text-[11px] text-cyber-text-primary"
            />
            <textarea
              value={previewText}
              onChange={(e) => setPreviewText(e.target.value)}
              rows={3}
              placeholder="输入一条回复，看命中哪个标签"
              className="w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-1.5 py-1 text-[11px] text-cyber-text-primary"
            />
            <button
              onClick={() => previewM.mutate()}
              disabled={!previewText.trim()}
              className="mt-1.5 flex h-8 items-center gap-1 rounded bg-cyber-neon-purple px-2 text-[11px] font-mono font-bold text-white disabled:opacity-40"
            >
              <Wand2 className="h-3 w-3" /> 判定
            </button>
            {previewM.data && (
              <div className="mt-2 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary p-2">
                <div className="flex items-center gap-1.5">
                  <span
                    className="rounded px-1.5 py-0.5 text-[11px] font-mono font-bold"
                    style={{
                      color: (previewM.data as any).result?.tag_color || '#888',
                      background: `${(previewM.data as any).result?.tag_color || '#888'}22`,
                    }}
                  >
                    {(previewM.data as any).result?.tag_name}
                  </span>
                  <span className="text-[10px] font-mono text-cyber-text-muted">
                    {(previewM.data as any).result?.tag_code} ·
                    立场 {(previewM.data as any).result?.stance} ·
                    置信度 {(previewM.data as any).result?.confidence}
                  </span>
                </div>
                <div className="mt-1 text-[10px] font-mono text-cyber-text-muted">
                  命中：{(previewM.data as any).result?.reason}
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
