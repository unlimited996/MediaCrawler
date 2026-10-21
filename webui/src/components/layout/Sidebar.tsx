import { Bug, Wifi, AlertTriangle, Flame, Database, FolderOpen, CalendarClock } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Badge } from '@/components/ui/badge'
import { useCrawlerStore } from '@/store/crawlerStore'
import { useCrawlerStatus } from '@/hooks/useCrawler'
import { LanguageSwitch } from './LanguageSwitch'
import { ThemeToggle } from './ThemeToggle'

export type MainView = 'crawl' | 'discover' | 'data' | 'browser' | 'tasks'

interface SidebarProps {
  onShowDisclaimer?: () => void
  view: MainView
  onViewChange: (view: MainView) => void
}

export function Sidebar({ onShowDisclaimer, view, onViewChange }: SidebarProps) {
  const { t } = useTranslation()
  const { t: tLicense } = useTranslation('license')
  const status = useCrawlerStore((state) => state.status)

  // Poll status
  useCrawlerStatus()

  const isRunning = status === 'running'

  return (
    <header className="h-14 flex-shrink-0 glass-panel border-b border-cyber-border-subtle relative z-10">
      <div className="h-full px-4 flex items-center justify-between">
        {/* Left: Logo and GitHub Star */}
        <div className="flex items-center gap-3">
          <Bug className="w-5 h-5 text-cyber-neon-cyan" />
          <span className="font-mono font-bold text-cyber-text-primary tracking-wider text-sm">
            MediaCrawler
          </span>

          {/* 主导航：抓取 / 热搜时光机 / 数据管理 / 数据浏览 */}
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
              onClick={() => onViewChange('data')}
              className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-mono transition-all ${
                view === 'data'
                  ? 'bg-cyber-neon-green text-black font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-green/50'
              }`}
            >
              <Database className="w-3.5 h-3.5" />
              <span className="hidden sm:inline">{t('nav.data')}</span>
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

        {/* Center: Warning Text */}
        <button
          onClick={onShowDisclaimer}
          className="flex items-center gap-3 px-4 py-1.5 rounded-lg border border-cyber-neon-orange/50 bg-cyber-neon-orange/10 hover:bg-cyber-neon-orange/20 transition-all cursor-pointer"
        >
          <AlertTriangle className="w-4 h-4 text-cyber-neon-orange flex-shrink-0" />
          <div className="flex items-center gap-4 text-xs font-mono">
            <span className="text-cyber-neon-orange">
              <span className="text-cyber-neon-pink font-bold">1.</span> {tLicense('content.line1')}
            </span>
            <span className="text-cyber-neon-orange">
              <span className="text-cyber-neon-pink font-bold">2.</span> {tLicense('content.line2')}
            </span>
          </div>
        </button>

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
    </header>
  )
}
