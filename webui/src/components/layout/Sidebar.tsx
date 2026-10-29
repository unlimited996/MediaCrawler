import { Bug, Wifi, Flame, Database, FolderOpen, CalendarClock, Tags, DatabaseBackup, Eye, SlidersHorizontal } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { useCrawlerStore } from '@/store/crawlerStore'
import { useCrawlerStatus, usePlatforms } from '@/hooks/useCrawler'
import { getPlatformTheme } from '@/components/data/platform/platformTheme'
import { PlatformBrandMark } from '@/components/data/platform/PlatformBrand'
import { LanguageSwitch } from './LanguageSwitch'
import { ThemeToggle } from './ThemeToggle'

export type MainView = 'crawl' | 'discover' | 'data' | 'browser' | 'tasks' | 'stance' | 'backup' | 'config' | 'guest'

interface SidebarProps {
  view: MainView
  onViewChange: (view: MainView, platform?: string) => void
  /** 数据管理当前选中的平台（'' = 全部） */
  dataPlatform: string
}

export function Sidebar({ view, onViewChange, dataPlatform }: SidebarProps) {
  const { t } = useTranslation()
  const status = useCrawlerStore((state) => state.status)
  const { data: platforms } = usePlatforms()

  // Poll status
  useCrawlerStatus()

  const isRunning = status === 'running'
  const list = (platforms || []) as { value: string; label: string }[]

  return (
    <header className="flex-shrink-0 glass-panel border-b border-cyber-border-subtle relative z-10">
      {/* 第一行：主导航 */}
      <div className="h-14 px-4 flex items-center justify-between">
        {/* Left: Logo and GitHub Star */}
        <div className="flex items-center gap-3">
          <Bug className="w-5 h-5 text-cyber-neon-cyan" />
          <span className="font-mono font-bold text-cyber-text-primary tracking-wider text-sm">
            MediaCrawler
          </span>

          {/* 主导航：抓取 / 热搜时光机 / 定时任务 / 数据浏览 */}
          <div className="flex items-center gap-1 ml-1">
            <button
              onClick={() => onViewChange('crawl')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'crawl'
                  ? 'bg-cyber-neon-cyan text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-cyan/50'
              }`}
            >
              <Bug className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.crawl')}</span>
            </button>
            <button
              onClick={() => onViewChange('discover')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'discover'
                  ? 'bg-cyber-neon-orange text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-orange/50'
              }`}
            >
              <Flame className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.discover')}</span>
            </button>
            <button
              onClick={() => onViewChange('tasks')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'tasks'
                  ? 'bg-cyber-neon-purple text-white font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-purple/50'
              }`}
            >
              <CalendarClock className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.tasks')}</span>
            </button>
            <button
              onClick={() => onViewChange('stance')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'stance'
                  ? 'bg-cyber-neon-pink text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-pink/50'
              }`}
            >
              <Tags className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.stanceTags', { defaultValue: '态度标签库' })}</span>
            </button>
            <button
              onClick={() => onViewChange('browser')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'browser'
                  ? 'bg-cyber-neon-purple text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-purple/50'
              }`}
            >
              <FolderOpen className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.dataBrowser')}</span>
            </button>
            <button
              onClick={() => onViewChange('backup')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'backup'
                  ? 'bg-cyber-neon-green text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-green/50'
              }`}
            >
              <DatabaseBackup className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.backup', { defaultValue: '图片备份' })}</span>
            </button>
            <button
              onClick={() => onViewChange('config')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'config'
                  ? 'bg-cyber-neon-cyan text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-cyan/50'
              }`}
            >
              <SlidersHorizontal className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.config', { defaultValue: '配置' })}</span>
            </button>
            {/* 访客视图：只读筛选 + 导出，可整页分享（URL 即 #/guest） */}
            <button
              onClick={() => onViewChange('guest')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'guest'
                  ? 'bg-cyber-neon-cyan text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-cyan/50'
              }`}
            >
              <Eye className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.guest', { defaultValue: '访客浏览' })}</span>
            </button>
          </div>

          {isRunning && (
            <Badge variant="running" className="text-[10px]">
              {t('status.active')}
            </Badge>
          )}
          {isRunning && (
            <span className="w-2 h-2 bg-cyber-neon-green rounded-full shadow-glow-green-sm animate-pulse-fast" />
          )}
        </div>

        {/* Right: Actions and Status */}
        <div className="flex items-center gap-3">
          {/* Theme Toggle */}
          <ThemeToggle />
          {/* Language Switch */}
          <LanguageSwitch />

          {/* Status Info */}
          <div className="hidden lg:flex items-center gap-2 text-xs font-mono">
            <span className="text-cyber-text-muted">{t('sidebar.api')}:</span>
            <span className="text-cyber-neon-green">v1.0.0</span>
            <div className="flex items-center gap-1.5">
              <Wifi className="w-3 h-3 text-cyber-text-secondary" />
              <span className="text-cyber-text-secondary">{t('sidebar.local')}</span>
              <span className="status-dot status-dot-online" />
            </div>
          </div>
        </div>
      </div>

      {/* 第二行：数据管理按平台拆分入口（品牌色区分，点击直达该平台的数据） */}
      <div className="flex items-center gap-1 px-4 pb-2 -mt-1 overflow-x-auto">
        <button
          onClick={() => onViewChange('data', '')}
          className="flex flex-shrink-0 items-center gap-1 rounded-md border px-2 py-[3px] text-[11px] font-mono transition-all"
          style={{
            color: view === 'data' && !dataPlatform ? '#06B6D4' : undefined,
            background: view === 'data' && !dataPlatform ? 'rgba(6,182,212,0.12)' : undefined,
            borderColor: view === 'data' && !dataPlatform ? 'rgba(6,182,212,0.45)' : 'rgba(120,120,140,0.25)',
          }}
          title={t('nav.data')}
        >
          <Database className="w-3 h-3" />
          {t('nav.data')}
        </button>
        {list.map((p) => {
          const theme = getPlatformTheme(p.value)
          // 后端下发的是英文平台名，优先用平台主题里的中文名（未知平台才退回）
          const label = theme.label === p.value ? p.label : theme.label
          const active = view === 'data' && dataPlatform === p.value
          return (
            <button
              key={p.value}
              onClick={() => onViewChange('data', p.value)}
              className="flex flex-shrink-0 items-center gap-1 rounded-md border px-2 py-[3px] text-[11px] font-mono transition-all"
              style={{
                color: active ? theme.accent : undefined,
                background: active ? theme.accentSoft : 'transparent',
                borderColor: active ? `${theme.accent}66` : `${theme.accent}33`,
              }}
              title={label}
            >
              <PlatformBrandMark platform={p.value} size={14} />
              {label}
            </button>
          )
        })}
      </div>
    </header>
  )
}
