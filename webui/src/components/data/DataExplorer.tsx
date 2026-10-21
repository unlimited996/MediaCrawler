import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { FolderOpen, RefreshCw, Search, HardDrive, FileStack, Database as DatabaseIcon } from 'lucide-react'
import { dataApi } from '@/lib/api'
import { FileCard } from './FileCard'
import { Button } from '@/components/ui/button'
import { formatFileSize } from '@/lib/utils'

// 从文件名提取类别
function extractCategory(filename: string): string {
  // 文件名格式: search_comments_xxx, search_creators_xxx, search_videos_xxx 等
  const match = filename.match(/^(search_\w+?)_/)
  if (match) {
    return match[1]
  }
  // 其他格式尝试提取前缀
  const parts = filename.split('_')
  if (parts.length >= 2) {
    return `${parts[0]}_${parts[1]}`
  }
  return 'other'
}

// 类别显示名称
function getCategoryLabel(category: string): string {
  const labels: Record<string, string> = {
    'search_comments': '评论',
    'search_creators': '创作者',
    'search_videos': '视频',
    'search_contents': '内容',
    'search_notes': '笔记',
    'other': '其他',
  }
  return labels[category] || category.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
}

/** 顶部统计小卡 */
function StatChip({
  icon: Icon, value, label,
}: {
  icon: typeof HardDrive
  value: string
  label: string
}) {
  return (
    <div className="flex items-center gap-2.5 rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel px-4 py-2.5">
      <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-cyber-neon-cyan/10 text-cyber-neon-cyan">
        <Icon className="h-4 w-4" />
      </span>
      <div className="leading-tight">
        <div className="text-sm font-semibold text-cyber-text-primary">{value}</div>
        <div className="text-[11px] text-cyber-text-muted">{label}</div>
      </div>
    </div>
  )
}

export function DataExplorer() {
  const { t } = useTranslation('data')
  const [activeTab, setActiveTab] = useState<string>('all')
  const [search, setSearch] = useState('')

  const { data, isLoading, refetch, isRefetching } = useQuery({
    queryKey: ['dataFiles'],
    queryFn: async () => {
      const { data } = await dataApi.getFiles()
      return data.files
    },
  })

  const files = data || []

  // 汇总统计
  const summary = useMemo(() => {
    const totalSize = files.reduce((sum, f) => sum + (f.size || 0), 0)
    const totalRecords = files.reduce((sum, f) => sum + (f.record_count || 0), 0)
    return { count: files.length, totalSize, totalRecords }
  }, [files])

  // 按类别分组
  const { categories, groupedFiles } = useMemo(() => {
    const grouped: Record<string, typeof files> = {}
    files.forEach(file => {
      const category = extractCategory(file.name)
      if (!grouped[category]) grouped[category] = []
      grouped[category].push(file)
    })
    const sortedCategories = Object.keys(grouped).sort((a, b) =>
      grouped[b].length - grouped[a].length
    )
    return { categories: sortedCategories, groupedFiles: grouped }
  }, [files])

  // 类别 + 搜索关键词双重过滤
  const displayFiles = useMemo(() => {
    let list = activeTab === 'all' ? files : (groupedFiles[activeTab] || [])
    const kw = search.trim().toLowerCase()
    if (kw) list = list.filter((f) => f.name.toLowerCase().includes(kw))
    return list
  }, [files, groupedFiles, activeTab, search])

  const pillBase =
    'px-3 py-1.5 rounded-full text-xs font-mono border transition-all whitespace-nowrap'
  const pillIdle =
    'bg-cyber-bg-panel text-cyber-text-secondary border-cyber-border-subtle hover:text-cyber-text-primary hover:border-cyber-neon-cyan/40'
  const pillActive =
    'bg-cyber-neon-cyan/10 text-cyber-neon-cyan border-cyber-neon-cyan/40 font-medium'

  return (
    <div className="flex h-full flex-col gap-4">
      {/* 页头 */}
      <div className="flex flex-shrink-0 items-center justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-cyber-neon-purple/10 text-cyber-neon-purple">
            <FolderOpen className="h-4 w-4" />
          </span>
          <div className="leading-tight">
            <h2 className="text-base font-semibold text-cyber-text-primary">
              {t('explorer.title')}
            </h2>
            <p className="text-[11px] text-cyber-text-muted">
              {t('manage.searchKeyword')} · {t('explorer.records', { count: files.length })}
            </p>
          </div>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isRefetching}
          className="font-mono"
        >
          <RefreshCw className={`h-4 w-4 ${isRefetching ? 'animate-spin' : ''}`} />
          {t('explorer.rescan')}
        </Button>
      </div>

      {/* 汇总统计 */}
      {files.length > 0 && (
        <div className="flex flex-shrink-0 flex-wrap gap-2.5">
          <StatChip icon={FileStack} value={String(summary.count)} label={t('manage.filesCount')} />
          <StatChip icon={HardDrive} value={formatFileSize(summary.totalSize)} label={t('manage.totalSize')} />
          <StatChip
            icon={DatabaseIcon}
            value={summary.totalRecords.toLocaleString()}
            label={t('manage.recordsCount')}
          />
        </div>
      )}

      {/* 工具栏：搜索 + 类别筛选 */}
      <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-cyber-text-muted" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder={t('manage.searchFile')}
            className="h-8 w-52 rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel pl-8 pr-2 text-xs text-cyber-text-primary placeholder:text-cyber-text-muted focus:border-cyber-neon-cyan/50 focus:outline-none"
          />
        </div>
        {files.length > 0 && categories.length > 1 && (
          <div className="flex flex-wrap items-center gap-1.5">
            <button
              onClick={() => setActiveTab('all')}
              className={`${pillBase} ${activeTab === 'all' ? pillActive : pillIdle}`}
            >
              {t('explorer.allCategories')} ({files.length})
            </button>
            {categories.map(category => (
              <button
                key={category}
                onClick={() => setActiveTab(category)}
                className={`${pillBase} ${activeTab === category ? pillActive : pillIdle}`}
              >
                {getCategoryLabel(category)} ({groupedFiles[category].length})
              </button>
            ))}
          </div>
        )}
      </div>

      {/* 文件区 */}
      {isLoading ? (
        <div className="flex flex-1 items-center justify-center">
          <div className="animate-pulse font-mono text-cyber-text-muted">
            {t('explorer.loading')}
          </div>
        </div>
      ) : files.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center text-center">
          <span className="mb-4 flex h-16 w-16 items-center justify-center rounded-2xl bg-cyber-bg-panel text-cyber-text-muted">
            <FolderOpen className="h-7 w-7" />
          </span>
          <h3 className="mb-1.5 text-base font-medium text-cyber-text-primary">
            {t('explorer.noData')}
          </h3>
          <p className="max-w-md text-xs text-cyber-text-muted">
            {t('explorer.noDataHint')}
          </p>
        </div>
      ) : displayFiles.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center text-center">
          <span className="mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-cyber-bg-panel text-cyber-text-muted">
            <Search className="h-5 w-5" />
          </span>
          <p className="text-xs text-cyber-text-muted">{t('manage.empty')}</p>
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {displayFiles.map((file) => (
              <FileCard key={file.path} file={file} onDeleted={() => refetch()} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
