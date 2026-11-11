/**
 * S3 媒体备份管理页（P4）
 *
 * 把帖子关联的图片/视频自动备份到 S3 兼容存储（Garage），
 * 访问链：本地缓存 → 平台远程 → S3 备份。
 *
 * 本页提供：
 * - S3 配置的增删改查与启用状态管理（Secret 加密落库，前端只读掩码）
 * - 连通性测试（保存前试用 / 已保存配置直试）
 * - 备份任务面板：统计、立即执行、失败重试、历史媒体入队
 * - 备份记录明细：状态/平台筛选、单条重试、删除记录（连同 S3 对象）、
 *   一键跳转到原贴
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api from '@/lib/api'
import {
  CloudUpload, Crosshair, DatabaseBackup, HardDriveDownload, Layers, Play, Plus,
  Power, RefreshCw, RotateCcw, Trash2, Wifi, X, Zap,
  Smile, Sparkles, CheckSquare,
} from 'lucide-react'
import { toast } from 'sonner'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Badge } from '@/components/ui/badge'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog'
import {
  S3ConfigRow, S3BackupItem, S3BackupStats, emojiAssetsApi,
} from '@/lib/api'
import { useImageViewer } from '@/components/data/platform/ImageViewer'


const STATUS_LABEL: Record<string, { label: string; cls: string }> = {
  pending: { label: '待备份', cls: 'bg-amber-500/15 text-amber-300 border-amber-500/40' },
  uploading: { label: '上传中', cls: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40' },
  uploaded: { label: '已备份', cls: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40' },
  failed: { label: '失败', cls: 'bg-rose-500/15 text-rose-300 border-rose-500/40' },
  orphaned: { label: '待清理', cls: 'bg-orange-500/15 text-orange-300 border-orange-500/40' },
}

function fmtBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`
}

interface ConfigDraft {
  id?: number
  name: string
  endpoint: string
  access_key_id: string
  secret_key: string
  region: string
  bucket: string
  addressing_style: string
  enabled: boolean
}

const EMPTY_DRAFT: ConfigDraft = {
  name: '', endpoint: '', access_key_id: '', secret_key: '',
  region: 'garage', bucket: 'photos', addressing_style: 'path', enabled: true,
}

export function S3BackupView() {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<ConfigDraft | null>(null)
  const [testResult, setTestResult] = useState<string>('')
  const [status, setStatus] = useState('')
  const [platform, setPlatform] = useState('')
  const [page, setPage] = useState(1)

  const configsQ = useQuery({
    queryKey: ['s3Configs'],
    queryFn: async () => (await api.get<{ items: S3ConfigRow[] }>('/s3-backup/configs')).data.items,
  })
  const statsQ = useQuery({
    queryKey: ['s3BackupStats'],
    queryFn: async () => (await api.get<S3BackupStats>('/s3-backup/stats')).data,
    refetchInterval: 15000,
  })
  const itemsQ = useQuery({
    queryKey: ['s3BackupItems', status, platform, page],
    queryFn: async () => (await api.get<{ items: S3BackupItem[]; total: number }>(
      '/s3-backup/items', {
        params: { status: status || undefined, platform: platform || undefined,
                  page, page_size: 20 },
      })).data,
    refetchInterval: 15000,
  })
  const dedupQ = useQuery({
    queryKey: ['s3DedupReport'],
    queryFn: async () => (await api.get<{
      analyzed_rows: number; dup_groups: number; redundant_objects: number;
      reclaimable_bytes: number; groups: any[]; last_run?: any
    }>('/s3-backup/dedup/report')).data,
    // 不自动轮询：全表扫描较重，仅在「分析报告」/「执行去重」后按需刷新
  })
  const [dedupOpen, setDedupOpen] = useState(false)
  // 用户勾选「保留」、不参与清理的对象键（按 key 精确排除）
  const [excludedKeys, setExcludedKeys] = useState<Set<string>>(new Set())
  // 已展开的分组（按 checksum）
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  // 批量勾选的副本（用于「保留」/「转为表情包」）
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set())
  // 看图器：展开分组后点缩略图可放大查看（复用「本地→远程→S3」候选源）
  const viewer = useImageViewer()
  const emojiQ = useQuery({
    queryKey: ['emojiAssets'],
    queryFn: async () => (await emojiAssetsApi.list({ page_size: 60 })).data,
  })

  const toggleExpand = (c: string) =>
    setExpanded((prev) => {
      const n = new Set(prev)
      n.has(c) ? n.delete(c) : n.add(c)
      return n
    })
  const toggleKeyExclude = (k: string) =>
    setExcludedKeys((prev) => {
      const n = new Set(prev)
      n.has(k) ? n.delete(k) : n.add(k)
      return n
    })
  const toggleGroupExclude = (keys: string[], on: boolean) =>
    setExcludedKeys((prev) => {
      const n = new Set(prev)
      keys.forEach((k) => (on ? n.add(k) : n.delete(k)))
      return n
    })
  const toggleKeySelect = (k: string) =>
    setSelectedKeys((prev) => {
      const n = new Set(prev)
      n.has(k) ? n.delete(k) : n.add(k)
      return n
    })
  const keepSelected = () => {
    if (!selectedKeys.size) return
    setExcludedKeys((prev) => {
      const n = new Set(prev)
      selectedKeys.forEach((k) => n.add(k))
      return n
    })
    toast.success(`已保留 ${selectedKeys.size} 个副本，去重时不会删除`)
    setSelectedKeys(new Set())
  }

  const invalidateAll = () => {
    qc.invalidateQueries({ queryKey: ['s3Configs'] })
    qc.invalidateQueries({ queryKey: ['s3BackupStats'] })
    qc.invalidateQueries({ queryKey: ['s3BackupItems'] })
  }

  const saveM = useMutation({
    mutationFn: async (d: ConfigDraft) => {
      if (d.id) {
        return (await api.put(`/s3-backup/configs/${d.id}`, d)).data
      }
      return (await api.post('/s3-backup/configs', d)).data
    },
    onSuccess: () => { invalidateAll(); setEditing(null); toast.success('配置已保存') },
    onError: (e: any) => toast.error(`保存失败：${e?.response?.data?.detail || e?.message}`),
  })

  const delM = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/s3-backup/configs/${id}`)).data,
    onSuccess: () => { invalidateAll(); toast.success('配置已删除（S3 对象与备份记录保留）') },
    onError: (e: any) => toast.error(`删除失败：${e?.response?.data?.detail || e?.message}`),
  })

  const toggleM = useMutation({
    mutationFn: async ({ id, enabled }: { id: number; enabled: boolean }) =>
      (await api.post(`/s3-backup/configs/${id}/toggle`, { enabled })).data,
    onSuccess: (_d, v) => {
      invalidateAll()
      toast.success(v.enabled ? '已启用，历史媒体补队中' : '已停用')
    },
  })

  const testM = useMutation({
    mutationFn: async (id: number) => (await api.post(`/s3-backup/configs/${id}/test`)).data,
    onSuccess: (d: any) => {
      configsQ.refetch()
      toast.success(d?.ok ? `连接成功（${d.latency_ms}ms）` : `连接失败：${d?.error}`)
    },
  })

  const runM = useMutation({
    mutationFn: async () => (await api.post('/s3-backup/run', {}, { params: { limit: 50 } })).data,
    onSuccess: (d: any) => {
      toast.success(d?.started ? '备份任务已启动' : (d?.note || '启动失败'))
      setTimeout(invalidateAll, 3000)
      setTimeout(invalidateAll, 10000)
    },
  })

  const retryM = useMutation({
    mutationFn: async () => (await api.post('/s3-backup/retry', {})).data,
    onSuccess: (d: any) => { invalidateAll(); toast.success(`已重置 ${d?.reset ?? 0} 条失败项`) },
  })

  const enqueueM = useMutation({
    mutationFn: async () => (await api.post('/s3-backup/enqueue-missing')).data,
    onSuccess: () => { toast.success('历史媒体入队中（后台执行）'); setTimeout(invalidateAll, 5000) },
  })

  const retryItemM = useMutation({
    mutationFn: async (id: number) => (await api.post(`/s3-backup/items/${id}/retry`)).data,
    onSuccess: () => { invalidateAll(); toast.success('已加入重试队列') },
  })

  const deleteItemM = useMutation({
    mutationFn: async (id: number) => (await api.delete(`/s3-backup/items/${id}`)).data,
    onSuccess: (d: any) => {
      invalidateAll()
      if (d?.object_deleted) toast.success('备份记录与 S3 对象已删除')
      else if (d?.kept_for_refs) toast.success('对象仍被其它媒体引用，仅删除本条记录')
      else toast.success('备份记录已删除')
    },
    onError: (e: any) => toast.error(e?.response?.data?.detail || '删除失败'),
  })

  const purgeOrphanedM = useMutation({
    mutationFn: async () => (await api.post('/s3-backup/purge-orphaned')).data,
    onSuccess: (d: any) => {
      invalidateAll()
      toast.success(`清理完成：删除 ${d?.purged ?? 0} 个，仍待清理 ${d?.still ?? 0} 个`)
    },
  })

  const dedupAnalyzeM = useMutation({
    mutationFn: async () => (await api.post('/s3-backup/dedup/run', { execute: false })).data,
    onSuccess: () => {
      dedupQ.refetch()
      toast.success('分析完成：仅统计，未删除任何对象。要真正回收请点「执行去重」')
    },
  })
  const dedupRunM = useMutation({
    mutationFn: async (payload: any) => (await api.post('/s3-backup/dedup/run', payload)).data,
    onSuccess: (d: any) => {
      toast.success(d?.started ? '去重合并已在后台启动，稍后查看报告' : (d?.note || '未启动'))
      if (d?.started) {
        setExcludedKeys(new Set())  // 已提交，清空选择
        // 后台任务要跑一会儿，延时刷新两次让报告跟上
        setTimeout(() => dedupQ.refetch(), 5000)
        setTimeout(() => dedupQ.refetch(), 15000)
      }
    },
    onError: (e: any) => toast.error(`启动失败：${e?.response?.data?.detail || e?.message}`),
  })

  const emojiArchiveM = useMutation({
    mutationFn: async (payload: { keys: string[]; source: string }) =>
      (await emojiAssetsApi.archive(payload)).data,
    onSuccess: (d: any) => {
      const linked = d.linked_rows ? `，${d.linked_rows} 条备份改指向表情包` : ''
      const deleted = d.objects_deleted ? `，清理 ${d.objects_deleted} 个冗余对象` : ''
      toast.success(
        `已归档 ${d.archived} 个新表情包（更新 ${d.updated} 个）${linked}${deleted}`)
      qc.invalidateQueries({ queryKey: ['emojiAssets'] })
      dedupQ.refetch()
      setSelectedKeys(new Set())
    },
    onError: (e: any) => toast.error(`归档失败：${e?.response?.data?.detail || e?.message}`),
  })
  const emojiDetectM = useMutation({
    mutationFn: async () => (await emojiAssetsApi.detect({
      auto_archive: true, limit: 200,
    })).data,
    onSuccess: (d: any) => {
      toast.success(
        `识别到 ${d.total} 个候选，归档 ${d.archived} 个；` +
        `${d.linked_rows ?? 0} 条备份已改指向表情包，清理 ${d.objects_deleted ?? 0} 个冗余对象`)
      qc.invalidateQueries({ queryKey: ['emojiAssets'] })
      dedupQ.refetch()
    },
    onError: (e: any) => toast.error(`识别失败：${e?.response?.data?.detail || e?.message}`),
  })
  const emojiRemoveM = useMutation({
    mutationFn: async (id: number) => (await emojiAssetsApi.remove(id)).data,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['emojiAssets'] })
      dedupQ.refetch()
    },
  })

  const stats = statsQ.data
  const items = itemsQ.data?.items || []
  const total = itemsQ.data?.total || 0
  const configs = configsQ.data || []

  return (
    <div className="flex h-full flex-col gap-4 overflow-hidden">
      {/* 头部：标题 + 操作 */}
      <div className="flex flex-shrink-0 flex-wrap items-center gap-2 glass-panel rounded-lg px-4 py-3">
        <DatabaseBackup className="h-5 w-5 text-cyber-neon-cyan" />
        <div className="mr-auto">
          <h2 className="font-mono text-sm font-bold text-cyber-text-primary">图片自动备份（S3）</h2>
          <p className="text-[11px] text-cyber-text-muted">
            帖子图片/视频自动上传至 S3 兼容存储；访问顺序：本地缓存 → 平台远程 → S3 备份，帖子删除后备份仍可访问
          </p>
        </div>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={() => { configsQ.refetch(); statsQ.refetch(); itemsQ.refetch() }}>
          <RefreshCw className="mr-1 h-3.5 w-3.5" />刷新
        </Button>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={() => enqueueM.mutate()} disabled={enqueueM.isPending}>
          <HardDriveDownload className="mr-1 h-3.5 w-3.5" />历史媒体入队
        </Button>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={() => retryM.mutate()} disabled={retryM.isPending}>
          <RotateCcw className="mr-1 h-3.5 w-3.5" />重试失败项
        </Button>
        <Button size="sm" variant="outline" className="font-mono text-xs"
                onClick={() => purgeOrphanedM.mutate()}
                disabled={purgeOrphanedM.isPending || !stats?.orphaned}
                title="删帖时 S3 删除失败转来的孤立备份：重试删除对象与记录">
          <Trash2 className="mr-1 h-3.5 w-3.5" />清理孤立备份
          {stats?.orphaned ? ` (${stats.orphaned})` : ''}
        </Button>
        <Button size="sm" className="font-mono text-xs" onClick={() => runM.mutate()}
                disabled={runM.isPending}>
          <Play className="mr-1 h-3.5 w-3.5" />立即备份
        </Button>
        <Button size="sm" className="font-mono text-xs"
                onClick={() => { setEditing({ ...EMPTY_DRAFT }); setTestResult('') }}>
          <Plus className="mr-1 h-3.5 w-3.5" />新增配置
        </Button>
      </div>

      <div className="flex-1 grid min-h-0 gap-4 overflow-hidden lg:grid-cols-5">
        {/* 左列：统计 + 配置 */}
        <div className="flex min-h-0 flex-col gap-4 overflow-hidden lg:col-span-2">
          {/* 存储去重 */}
          <div className="glass-panel rounded-lg px-3 py-2.5">
            <div className="flex items-center gap-2">
              <Layers className="h-4 w-4 text-cyber-neon-cyan" />
              <span className="font-mono text-xs font-bold text-cyber-text-primary">存储去重</span>
              <span className="text-[11px] text-cyber-text-muted">
                按图片内容（sha256）合并完全相同的备份，释放重复存储
              </span>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-2">
              <StatCard label="重复组" value={dedupQ.data?.dup_groups ?? 0} tone="purple" />
              <StatCard label="冗余对象" value={dedupQ.data?.redundant_objects ?? 0} tone="orange" />
              <StatCard label="待回收" value={fmtBytes(dedupQ.data?.reclaimable_bytes ?? 0)} tone="cyan" />
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" className="font-mono text-xs"
                      onClick={() => dedupAnalyzeM.mutate()} disabled={dedupAnalyzeM.isPending}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" />分析报告
              </Button>
              <Button size="sm" className="font-mono text-xs"
                      disabled={dedupRunM.isPending
                                || !(dedupQ.data?.redundant_objects)}
                      onClick={() => {
                        const excl = excludedKeys.size
                        const msg = `确认执行去重合并？\n\n将把重复图片合并为一份、删除冗余 S3 对象并释放存储空间。` +
                          (excl ? `\n\n已勾选 ${excl} 个副本「保留」，本次不会被清理。` : '') +
                          `\n\n该操作不可撤销，建议先在分组里确认无异常、并勾掉想保留的副本。`
                        if (confirm(msg)) {
                          dedupRunM.mutate({ execute: true, exclude_keys: [...excludedKeys] })
                        }
                      }}>
                <Trash2 className="mr-1 h-3.5 w-3.5" />执行去重
                {excludedKeys.size > 0 && ` (保留${excludedKeys.size})`}
              </Button>
              {(dedupQ.data?.dup_groups ?? 0) > 0 && (
                <Button size="sm" variant="ghost" className="font-mono text-xs"
                        onClick={() => setDedupOpen((v) => !v)}>
                  {dedupOpen ? '收起分组' : `查看分组(${dedupQ.data?.dup_groups})`}
                </Button>
              )}
            </div>
            {dedupQ.data?.last_run && (
              <div className={`mt-2 rounded border px-2 py-1 text-[11px] font-mono ${
                dedupQ.data.last_run.ok
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                  : 'border-rose-500/30 bg-rose-500/10 text-rose-300'}`}>
                {dedupQ.data.last_run.ok
                  ? (dedupQ.data.last_run.execute
                      ? `上次执行：合并 ${dedupQ.data.last_run.groups ?? 0} 组，` +
                        `删除 ${dedupQ.data.last_run.objects_deleted ?? 0} 个对象，` +
                        `释放 ${fmtBytes(dedupQ.data.last_run.bytes_reclaimed ?? 0)}`
                      : `上次分析（未删除任何对象）：可合并 ${dedupQ.data.last_run.groups ?? 0} 组、` +
                        `预计删除 ${dedupQ.data.last_run.objects_deleted ?? 0} 个、` +
                        `可回收 ${fmtBytes(dedupQ.data.last_run.bytes_reclaimed ?? 0)}。` +
                        `点「执行去重」才会真正生效`)
                  : `上次运行失败：${dedupQ.data.last_run.error || ''}`}
                {dedupQ.data.last_run.errors?.length
                  ? `（${dedupQ.data.last_run.errors.length} 组跳过）` : ''}
                {dedupQ.data.last_run.delete_errors?.length
                  ? ` · ${dedupQ.data.last_run.delete_errors.length} 个对象删除失败` +
                    `（${dedupQ.data.last_run.delete_errors[0]?.error || ''}）` : ''}
                {dedupQ.data.last_run.delete_protected
                  ? ` · ${dedupQ.data.last_run.delete_protected} 个仍被引用跳过` : ''}
              </div>
            )}
            {dedupOpen && dedupQ.data?.groups?.length ? (
              <div className="mt-2">
                <div className="flex items-center justify-between px-1 text-[10px] text-cyber-text-muted">
                  <span>点整行展开：组内所有重复图片并排显示（点图可放大）；勾「保留」的副本不会被清理</span>
                  {excludedKeys.size > 0 && (
                    <button type="button" onClick={() => setExcludedKeys(new Set())}
                            className="text-cyber-neon-cyan hover:underline">
                      清空排除({excludedKeys.size})
                    </button>
                  )}
                </div>
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5 rounded border border-cyber-border-subtle bg-cyber-bg-tertiary/40 px-2 py-1.5">
                  <CheckSquare className="h-3.5 w-3.5 text-cyber-neon-cyan" />
                  <span className="text-[10px] font-mono text-cyber-text-secondary">
                    已选 {selectedKeys.size} 个副本
                  </span>
                  <Button size="sm" variant="outline" className="h-6 font-mono text-[10px]"
                          disabled={!selectedKeys.size}
                          onClick={() => {
                            const all = (dedupQ.data?.groups || []).slice(0, 300)
                              .flatMap((g: any) => g.keys as string[])
                            setSelectedKeys(new Set(all))
                          }}>
                    全选可见
                  </Button>
                  <Button size="sm" variant="outline" className="h-6 font-mono text-[10px]"
                          disabled={!selectedKeys.size} onClick={keepSelected}>
                    保留所选
                  </Button>
                  <Button size="sm" className="h-6 font-mono text-[10px]"
                          disabled={!selectedKeys.size || emojiArchiveM.isPending}
                          onClick={() => emojiArchiveM.mutate({
                            keys: [...selectedKeys], source: 'dedup',
                          })}>
                    <Smile className="mr-1 h-3 w-3" />转为表情包
                  </Button>
                  {selectedKeys.size > 0 && (
                    <button type="button" onClick={() => setSelectedKeys(new Set())}
                            className="text-[10px] text-cyber-text-muted hover:underline">
                      清空选择
                    </button>
                  )}
                </div>
                {/* 分组列表独立滚动：否则展开组多了会把下面的统计卡片挤出可视区 */}
                <div className="mt-1.5 max-h-[320px] space-y-1.5 overflow-y-auto pr-1">
                  {dedupQ.data.groups.slice(0, 300).map((g: any) => {
                    const exp = expanded.has(g.checksum)
                    const allKept = g.keys.length > 0 && g.keys.every((k: string) => excludedKeys.has(k))
                    // 组内所有副本的大图地址，交给看图器后可左右翻页对比
                    const groupSrcs: string[][] = g.keys.map(
                      (k: string) => [`/library/api/s3-backup/object/${k}`])
                    return (
                      <div key={g.checksum}
                           className="overflow-hidden rounded border border-cyber-border-subtle">
                        {/* 整行可点：不只是三角箭头；内部复选框/按钮单独 stopPropagation */}
                        <div className="flex cursor-pointer flex-wrap items-center gap-2 px-2 py-1.5 transition-colors hover:bg-cyber-bg-tertiary/60"
                             onClick={() => toggleExpand(g.checksum)}>
                          <span className="font-mono text-xs text-cyber-neon-cyan">
                            {exp ? '▾' : '▸'}
                          </span>
                          <span className="font-mono text-[10px] text-cyber-text-secondary" title={g.checksum}>
                            {g.checksum.slice(0, 12)}
                          </span>
                          <span className="text-[10px] text-cyber-text-muted">{g.media_type}</span>
                          <span className="text-[10px] text-cyber-text-secondary">×{g.count}</span>
                          <span className="text-[10px] text-cyber-text-secondary">
                            可收回 {fmtBytes(g.reclaimable_bytes)}
                          </span>
                          <span className="ml-auto flex items-center gap-1"
                                onClick={(e) => e.stopPropagation()}>
                            <input type="checkbox" checked={allKept}
                                   onChange={(e) => toggleGroupExclude(g.keys, e.target.checked)}
                                   className="h-3 w-3 accent-emerald-400" />
                            <span className="text-[10px] text-cyber-text-muted">整组保留</span>
                          </span>
                        </div>
                        {exp && (
                          <div className="border-t border-cyber-border-subtle/60 p-2">
                            <div className="mb-1 text-[10px] text-cyber-text-muted">
                              涉及 {g.content_ids?.length ?? 0} 个对象(content_id)：
                              <span className="ml-1 font-mono text-cyber-text-secondary">
                                {(g.content_ids || []).slice(0, 10).join('、')}
                                {(g.content_ids || []).length > 10 ? ' …' : ''}
                              </span>
                            </div>
                            {(g.story_titles || []).length > 0 && (
                              <div className="mb-1.5 truncate text-[10px] text-cyber-neon-cyan"
                                   title={(g.story_titles || []).join('、')}>
                                关联帖子：{(g.story_titles || []).slice(0, 3).join('、')}
                                {(g.story_titles || []).length > 3 ? ' …' : ''}
                              </div>
                            )}
                            {/* 所有副本横向并排、不换行、可左右滑动，方便直接比对；点图放大 */}
                            <div className="flex gap-2 overflow-x-auto pb-1">
                              {g.keys.map((k: string, i: number) => {
                                const size = (g.sizes && g.sizes[i]) || 0
                                const kept = excludedKeys.has(k)
                                const isCanon = k === g.canonical_key
                                return (
                                  <div key={k}
                                       className={`w-24 flex-shrink-0 rounded border p-1 sm:w-28 ${kept ? 'border-emerald-500/60 bg-emerald-500/5' : 'border-cyber-border-subtle'}`}>
                                    <div className="relative">
                                      <img src={`/library/api/s3-backup/object/${k}`} alt=""
                                           loading="lazy"
                                           onClick={() => viewer?.open(groupSrcs, i)}
                                           className="aspect-square w-full cursor-zoom-in rounded bg-cyber-bg-tertiary object-cover"
                                           onError={(e) => {
                                             (e.currentTarget as HTMLImageElement)
                                               .style.visibility = 'hidden'
                                           }} />
                                      {isCanon && (
                                        <span className="absolute left-0.5 top-0.5 rounded bg-cyber-neon-cyan/80 px-1 text-[8px] font-mono text-black">
                                          规范
                                        </span>
                                      )}
                                    </div>
                                    <div className="mt-1 flex items-center gap-1">
                                      <input type="checkbox" checked={selectedKeys.has(k)}
                                             onChange={() => toggleKeySelect(k)}
                                             title="勾选用于批量操作"
                                             className="h-3 w-3 accent-cyan-400" />
                                      <label className="flex items-center gap-0.5"
                                             title="保留：去重时不会删除该副本">
                                        <input type="checkbox" checked={kept}
                                               onChange={() => toggleKeyExclude(k)}
                                               className="h-3 w-3 accent-emerald-400" />
                                        <span className="text-[9px] text-cyber-text-muted">留</span>
                                      </label>
                                      <span className="truncate font-mono text-[9px] text-cyber-text-muted"
                                            title={k}>{k.split('/').pop()}</span>
                                    </div>
                                    <div className="font-mono text-[9px] text-cyber-text-muted">
                                      {fmtBytes(size)}
                                    </div>
                                  </div>
                                )
                              })}
                            </div>
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              </div>
            ) : null}
          </div>

          {/* 表情包库：内容寻址规范对象，所有帖子的同图备份都指向这里 */}
          <div className="glass-panel flex-shrink-0 rounded-lg px-3 py-2.5">
            <div className="flex flex-wrap items-center gap-2">
              <Smile className="h-4 w-4 text-cyber-neon-orange" />
              <span className="font-mono text-xs font-bold text-cyber-text-primary">表情包库</span>
              <span className="text-[11px] text-cyber-text-muted">
                共 {emojiQ.data?.total ?? 0} 个 · 归档即收敛引用，重复入库自动指向这里
              </span>
              <Button size="sm" variant="outline" className="ml-auto h-6 font-mono text-[10px]"
                      onClick={() => emojiDetectM.mutate()} disabled={emojiDetectM.isPending}>
                <Sparkles className="mr-1 h-3 w-3" />自动识别并归档
              </Button>
            </div>
            <div className="mt-2 flex max-h-[132px] gap-2 overflow-x-auto pb-1">
              {(emojiQ.data?.items || []).length === 0 ? (
                <div className="py-3 text-[11px] text-cyber-text-muted">
                  暂无表情包。勾选重复副本点「转为表情包」会把同图收敛成一份；也可「自动识别并归档」。
                </div>
              ) : (emojiQ.data?.items || []).map((a: any) => (
                <div key={a.id}
                     className="w-20 flex-shrink-0 rounded border border-cyber-border-subtle p-1">
                  <img src={a.preview_url} alt="" loading="lazy"
                       referrerPolicy="no-referrer"
                       onClick={() => viewer?.open([[a.preview_url]], 0)}
                       className="aspect-square w-full cursor-zoom-in rounded bg-cyber-bg-tertiary object-cover"
                       onError={(e) => {
                         (e.currentTarget as HTMLImageElement).style.visibility = 'hidden'
                       }} />
                  <div className="mt-0.5 font-mono text-[9px] text-cyber-neon-cyan">×{a.use_count}</div>
                  <div className="truncate font-mono text-[9px] text-cyber-text-muted"
                       title={(a.story_titles || []).join('、')}>
                    {a.name || (a.story_titles || [])[0] || '表情包'}
                  </div>
                  <button type="button" onClick={() => emojiRemoveM.mutate(a.id)}
                          className="text-[9px] text-rose-300 hover:underline">移出</button>
                </div>
              ))}
            </div>
          </div>

          {/* 统计卡片 */}
          <div className="grid flex-shrink-0 grid-cols-3 gap-2">
            <StatCard label="待备份" value={stats?.pending ?? 0} tone="amber" />
            <StatCard label="已备份" value={stats?.uploaded ?? 0} tone="emerald" />
            <StatCard label="失败（待手动重试）" value={stats?.failed ?? 0} tone="rose" />
            <StatCard label="待清理（原帖已删）" value={stats?.orphaned ?? 0} tone="orange" />
            <StatCard label="已备份体积" value={fmtBytes(stats?.uploaded_bytes ?? 0)} tone="cyan" />
            <StatCard label="当前配置" value={stats?.config_name || '未配置'} tone="purple" />
          </div>

          {stats?.last_error && (
            <button
              type="button"
              onClick={() => { setStatus('failed'); setPage(1) }}
              title="点击查看未解决的失败记录（连接恢复后此横幅自动隐藏）"
              className="flex-shrink-0 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-left text-[11px] text-rose-300 transition-colors hover:border-rose-500/60 hover:bg-rose-500/15"
            >
              最近错误：{stats.last_error}
              {stats.last_error_at ? `（${stats.last_error_at.replace('T', ' ').slice(0, 19)}）` : ''}
              <span className="ml-1 text-rose-300/70">· 点击查看失败记录</span>
            </button>
          )}

          {/* 配置列表 */}
          <div className="flex min-h-0 flex-1 flex-col glass-panel rounded-lg overflow-hidden">
            <div className="flex items-center gap-2 border-b border-cyber-border-subtle px-3 py-2">
              <CloudUpload className="h-4 w-4 text-cyber-neon-cyan" />
              <span className="font-mono text-xs font-bold text-cyber-text-primary">S3 配置</span>
              <span className="text-[11px] text-cyber-text-muted">
                （{configs.filter((c) => c.enabled).length} 个已启用，取第一个生效）
              </span>
            </div>
            <div className="flex-1 overflow-auto">
              {configs.length === 0 ? (
                <div className="p-6 text-center text-xs text-cyber-text-muted">
                  暂无配置，点右上角「新增配置」
                </div>
              ) : configs.map((c) => (
                <div key={c.id}
                     className="border-b border-cyber-border-subtle px-3 py-2.5 last:border-b-0">
                  <div className="flex items-center gap-2">
                    <span className={`h-2 w-2 flex-shrink-0 rounded-full ${
                      c.enabled ? 'bg-emerald-400 shadow-glow-green-sm' : 'bg-slate-500'}`} />
                    <span className="font-mono text-xs font-bold text-cyber-text-primary">{c.name}</span>
                    {c.last_check_ok === true && (
                      <Badge className="border-emerald-500/40 bg-emerald-500/15 text-[10px] text-emerald-300">连通</Badge>
                    )}
                    {c.last_check_ok === false && (
                      <Badge className="border-rose-500/40 bg-rose-500/15 text-[10px] text-rose-300">异常</Badge>
                    )}
                    <div className="ml-auto flex items-center gap-1">
                      <Button size="icon" variant="ghost" title="启用/停用"
                              className="h-6 w-6"
                              onClick={() => toggleM.mutate({ id: c.id, enabled: !c.enabled })}>
                        <Power className={`h-3.5 w-3.5 ${c.enabled ? 'text-emerald-400' : 'text-slate-500'}`} />
                      </Button>
                      <Button size="icon" variant="ghost" title="测试连接" className="h-6 w-6"
                              onClick={() => testM.mutate(c.id)} disabled={testM.isPending}>
                        <Wifi className="h-3.5 w-3.5 text-cyber-neon-cyan" />
                      </Button>
                      <Button size="icon" variant="ghost" title="编辑" className="h-6 w-6"
                              onClick={() => setEditing({
                                id: c.id, name: c.name, endpoint: c.endpoint,
                                access_key_id: c.access_key_id, secret_key: '',
                                region: c.region, bucket: c.bucket,
                                addressing_style: c.addressing_style, enabled: c.enabled,
                              })}>
                        <Zap className="h-3.5 w-3.5 text-cyber-text-secondary" />
                      </Button>
                      <Button size="icon" variant="ghost" title="删除" className="h-6 w-6"
                              onClick={() => { if (confirm(`删除配置 ${c.name}？已上传的 S3 对象不受影响`)) delM.mutate(c.id) }}>
                        <Trash2 className="h-3.5 w-3.5 text-rose-400" />
                      </Button>
                    </div>
                  </div>
                  <div className="mt-1 space-y-0.5 font-mono text-[11px] text-cyber-text-secondary">
                    <div className="truncate" title={c.endpoint}>{c.endpoint}</div>
                    <div className="flex flex-wrap gap-x-3">
                      <span>桶: {c.bucket}</span>
                      <span>区域: {c.region}</span>
                      <span>Key: {c.access_key_id}</span>
                      <span className="text-cyber-text-muted">Secret: {c.secret_masked || '（空）'}</span>
                    </div>
                    {c.last_error && (
                      <div className="truncate text-rose-300" title={c.last_error}>{c.last_error}</div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* 右列：备份记录 */}
        <div className="flex min-h-0 flex-col glass-panel rounded-lg overflow-hidden lg:col-span-3">
          <div className="flex flex-wrap items-center gap-2 border-b border-cyber-border-subtle px-3 py-2">
            <span className="font-mono text-xs font-bold text-cyber-text-primary">备份记录</span>
            <select value={status} onChange={(e) => { setStatus(e.target.value); setPage(1) }}
                    className="rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-2 py-1 font-mono text-[11px] text-cyber-text-primary">
              <option value="">全部状态</option>
              <option value="pending">待备份</option>
              <option value="uploading">上传中</option>
              <option value="uploaded">已备份</option>
              <option value="failed">失败</option>
              <option value="orphaned">待清理（原帖已删）</option>
            </select>
            <Input value={platform} placeholder="平台筛选，如 xhs"
                   onChange={(e) => { setPlatform(e.target.value); setPage(1) }}
                   className="h-7 w-32 font-mono text-[11px]" />
            <span className="ml-auto text-[11px] text-cyber-text-muted">共 {total} 条</span>
          </div>
          <div className="flex-1 overflow-auto">
            <table className="w-full font-mono text-[11px]">
              <thead className="sticky top-0 bg-cyber-bg-panel text-cyber-text-muted">
                <tr className="border-b border-cyber-border-subtle">
                  <th className="px-2 py-1.5 text-left">对象键</th>
                  <th className="px-2 py-1.5 text-left">平台</th>
                  <th className="px-2 py-1.5 text-left">状态</th>
                  <th className="px-2 py-1.5 text-left">大小</th>
                  <th className="px-2 py-1.5 text-left">重试</th>
                  <th className="px-2 py-1.5 text-left">操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => (
                  <tr key={it.id} className="border-b border-cyber-border-subtle/50 hover:bg-cyber-bg-tertiary/50">
                    <td className="max-w-[260px] px-2 py-1.5 text-cyber-text-secondary">
                      <div className="flex items-center gap-1.5">
                        <img src={it.object_url} alt="" loading="lazy"
                             referrerPolicy="no-referrer"
                             onClick={() => viewer?.open([[it.object_url]], 0)}
                             className="h-8 w-8 flex-shrink-0 cursor-zoom-in rounded border border-cyber-border-subtle bg-cyber-bg-tertiary object-cover"
                             onError={(e) => {
                               (e.currentTarget as HTMLImageElement).style.visibility = 'hidden'
                             }} />
                        <div className="min-w-0">
                          <button type="button" title={it.object_key}
                                  onClick={() => viewer?.open([[it.object_url]], 0)}
                                  className="block max-w-[190px] truncate text-left font-mono hover:text-cyber-neon-cyan hover:underline">
                            {it.object_key}
                          </button>
                          {it.story_title && (
                            <div className="max-w-[190px] truncate text-[10px] text-cyber-neon-cyan"
                                 title={it.story_title}>
                              {it.story_title}
                            </div>
                          )}
                        </div>
                      </div>
                    </td>
                    <td className="px-2 py-1.5 text-cyber-text-secondary">{it.platform || '-'}</td>
                    <td className="px-2 py-1.5">
                      <span className={`rounded border px-1.5 py-0.5 text-[10px] ${STATUS_LABEL[it.status]?.cls || ''}`}>
                        {STATUS_LABEL[it.status]?.label || it.status}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-cyber-text-secondary">{fmtBytes(it.size_bytes || 0)}</td>
                    <td className="px-2 py-1.5 text-cyber-text-muted">{it.retry_count}/{it.max_retries}</td>
                    <td className="px-2 py-1.5">
                      <div className="flex items-center gap-0.5">
                        {it.story_id ? (
                          <Button size="icon" variant="ghost" title="跳转到原贴" className="h-6 w-6"
                                  onClick={() => window.dispatchEvent(new CustomEvent(
                                    'mc:open-story', {
                                      detail: { storyId: it.story_id, platform: it.platform || '' },
                                    }))}>
                            <Crosshair className="h-3 w-3 text-cyber-neon-purple" />
                          </Button>
                        ) : null}
                        <Button size="icon" variant="ghost"
                                title="转为表情包（收敛同图为一份，帖子图片改指向表情包对象）"
                                className="h-6 w-6"
                                disabled={emojiArchiveM.isPending}
                                onClick={() => emojiArchiveM.mutate({
                                  keys: [it.object_key], source: 'manual',
                                })}>
                          <Smile className="h-3 w-3 text-cyber-neon-orange" />
                        </Button>
                        {(it.status === 'failed' || it.status === 'pending') && (
                          <Button size="icon" variant="ghost" title="重试" className="h-6 w-6"
                                  onClick={() => retryItemM.mutate(it.id)}>
                            <RotateCcw className="h-3 w-3 text-cyber-neon-cyan" />
                          </Button>
                        )}
                        <Button size="icon" variant="ghost"
                                title={it.status === 'orphaned'
                                  ? '原帖已删：删除 S3 对象与备份记录'
                                  : '删除备份记录（连同 S3 对象）'}
                                className="h-6 w-6"
                                onClick={() => {
                                  if (!confirm(`删除备份记录？\n${it.object_key}\n\nS3 中的对应对象将一并删除，删除后无法通过备份地址恢复。`)) return
                                  deleteItemM.mutate(it.id)
                                }}>
                          <Trash2 className="h-3 w-3 text-rose-400" />
                        </Button>
                        {it.last_error && (
                          <span className="ml-1 text-rose-300" title={it.last_error}>!</span>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
                {items.length === 0 && (
                  <tr><td colSpan={6} className="px-2 py-8 text-center text-cyber-text-muted">
                    暂无记录
                  </td></tr>
                )}
              </tbody>
            </table>
          </div>
          {total > 20 && (
            <div className="flex flex-shrink-0 items-center justify-end gap-2 border-t border-cyber-border-subtle px-3 py-2">
              <Button size="sm" variant="outline" className="h-6 text-[11px]"
                      disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>上一页</Button>
              <span className="text-[11px] text-cyber-text-muted">第 {page} 页</span>
              <Button size="sm" variant="outline" className="h-6 text-[11px]"
                      disabled={page * 20 >= total} onClick={() => setPage((p) => p + 1)}>下一页</Button>
            </div>
          )}
        </div>
      </div>

      {/* 配置编辑弹窗 */}
      {editing && (
        <Dialog open onOpenChange={(o) => { if (!o) setEditing(null) }}>
          <DialogContent className="max-w-md">
            <DialogHeader>
              <DialogTitle>{editing.id ? `编辑配置 · ${editing.name}` : '新增 S3 配置'}</DialogTitle>
            </DialogHeader>
            <div className="grid gap-3 py-2">
              <div className="grid grid-cols-2 gap-3">
                <div><Label className="text-xs">配置名</Label>
                  <Input value={editing.name} className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, name: e.target.value })} /></div>
                <div><Label className="text-xs">Endpoint</Label>
                  <Input value={editing.endpoint} placeholder="http://host:port"
                         className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, endpoint: e.target.value })} /></div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div><Label className="text-xs">Key ID</Label>
                  <Input value={editing.access_key_id} className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, access_key_id: e.target.value })} /></div>
                <div><Label className="text-xs">Secret Key（留空=不变）</Label>
                  <Input value={editing.secret_key} type="password" placeholder={editing.id ? '******（不修改请留空）' : ''}
                         className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, secret_key: e.target.value })} /></div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div><Label className="text-xs">Region</Label>
                  <Input value={editing.region} className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, region: e.target.value })} /></div>
                <div><Label className="text-xs">Bucket</Label>
                  <Input value={editing.bucket} className="mt-1 font-mono text-xs"
                         onChange={(e) => setEditing({ ...editing, bucket: e.target.value })} /></div>
                <div><Label className="text-xs">寻址方式</Label>
                  <select value={editing.addressing_style}
                          onChange={(e) => setEditing({ ...editing, addressing_style: e.target.value })}
                          className="mt-1 w-full rounded border border-cyber-border-subtle bg-cyber-bg-tertiary px-2 py-1.5 font-mono text-xs text-cyber-text-primary">
                    <option value="path">path</option>
                    <option value="virtual">virtual</option>
                  </select></div>
              </div>
              <label className="flex items-center gap-2 text-xs text-cyber-text-secondary">
                <input type="checkbox" checked={editing.enabled}
                       onChange={(e) => setEditing({ ...editing, enabled: e.target.checked })} />
                启用该配置（备份任务取第一个启用的配置）
              </label>
              <div className="flex items-center gap-2">
                <Button size="sm" variant="outline" className="font-mono text-xs"
                        disabled={!editing.endpoint || !editing.access_key_id || (!editing.secret_key && !editing.id)}
                        onClick={async () => {
                          try {
                            const r = await api.post('/s3-backup/configs/test', {
                              config_id: editing.id, endpoint: editing.endpoint,
                              access_key_id: editing.access_key_id,
                              secret_key: editing.secret_key || undefined,
                              region: editing.region, bucket: editing.bucket,
                              addressing_style: editing.addressing_style,
                            })
                            setTestResult(r.data?.ok
                              ? `连接成功（${r.data.latency_ms}ms）`
                              : `连接失败：${r.data?.error || '未知错误'}`)
                          } catch (e: any) {
                            setTestResult(`请求失败：${e?.response?.data?.detail || e?.message}`)
                          }
                        }}>
                  <Wifi className="mr-1 h-3.5 w-3.5" />测试连接
                </Button>
                {testResult && (
                  <span className={`text-[11px] font-mono ${testResult.startsWith('连接成功') ? 'text-emerald-300' : 'text-rose-300'}`}>
                    {testResult}
                  </span>
                )}
              </div>
            </div>
            <DialogFooter>
              <Button variant="outline" size="sm" onClick={() => setEditing(null)}>
                <X className="mr-1 h-3.5 w-3.5" />取消
              </Button>
              <Button size="sm" disabled={!editing.name || !editing.endpoint || !editing.access_key_id
                                        || (!editing.id && !editing.secret_key) || saveM.isPending}
                      onClick={() => saveM.mutate(editing)}>
                保存
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
    </div>
  )
}

function StatCard({ label, value, tone }: { label: string; value: number | string; tone: string }) {
  const toneMap: Record<string, string> = {
    amber: 'text-amber-300', emerald: 'text-emerald-300',
    rose: 'text-rose-300', cyan: 'text-cyan-300', orange: 'text-orange-300',
    purple: 'text-purple-300',
  }
  return (
    <div className="glass-panel rounded-lg px-3 py-2">
      <div className="text-[11px] text-cyber-text-muted">{label}</div>
      <div className={`font-mono text-lg font-bold ${toneMap[tone] || ''}`}>{value}</div>
    </div>
  )
}
