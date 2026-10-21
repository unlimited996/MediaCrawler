import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { SlidersHorizontal, ListPlus } from 'lucide-react'
import { CrawlerConfigPanel } from '@/components/config/CrawlerConfigPanel'
import { CrawlerQueuePanel } from '@/components/config/CrawlerQueuePanel'
import { useCrawlerQueue } from '@/hooks/useCrawler'

type LeftTab = 'config' | 'queue'

/**
 * 抓取视图的左侧栏：配置 / 队列 两个菜单互斥切换（不同时出现）。
 * 控制台独占右侧，不再被配置面板挤压。
 */
export function CrawlLeftPanel() {
  const { t } = useTranslation('common')
  const [tab, setTab] = useState<LeftTab>('config')

  // 队列待办数（pending + running）显示在菜单上，切到配置页也能看见还有多少没跑
  const { data: queue } = useCrawlerQueue()
  const todo = (queue?.pending ?? 0) + (queue?.running ?? 0)
  const failed = queue?.failed ?? 0

  const tabs: { key: LeftTab; label: string; icon: typeof SlidersHorizontal }[] = [
    { key: 'config', label: t('nav.config', '配置'), icon: SlidersHorizontal },
    { key: 'queue', label: t('nav.queue', '队列'), icon: ListPlus },
  ]

  return (
    <aside className="w-[400px] flex-shrink-0 flex flex-col rounded-lg glass-panel float-panel overflow-hidden">
      {/* 菜单：配置 / 队列 互斥切换 */}
      <div className="flex-shrink-0 flex gap-1 border-b border-cyber-border-subtle/50 bg-cyber-bg-tertiary/30 p-1.5">
        {tabs.map(({ key, label, icon: Icon }) => {
          const active = tab === key
          return (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`flex flex-1 items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-mono transition-all ${
                active
                  ? 'bg-cyber-neon-cyan/90 text-cyber-bg-primary font-bold'
                  : 'text-cyber-text-secondary hover:text-cyber-text-primary border border-cyber-border-subtle hover:border-cyber-neon-cyan/50'
              }`}
            >
              <Icon className="h-3.5 w-3.5" />
              {label}
              {key === 'queue' && todo > 0 ? (
                <span
                  className={`rounded px-1 text-[10px] font-bold ${
                    active ? 'bg-cyber-bg-primary/20' : 'bg-cyber-neon-orange/20 text-cyber-neon-orange'
                  }`}
                >
                  {todo}
                </span>
              ) : null}
              {key === 'queue' && todo === 0 && failed > 0 ? (
                <span
                  className={`rounded px-1 text-[10px] font-bold ${
                    active ? 'bg-cyber-bg-primary/20' : 'bg-cyber-neon-pink/20 text-cyber-neon-pink'
                  }`}
                >
                  !
                </span>
              ) : null}
            </button>
          )
        })}
      </div>

      {/* 内容区：两个页面都自己管理滚动（配置页表单滚、底部按钮磁吸；
          队列页内部列表滚），所以这里一律不套 overflow-y-auto */}
      {tab === 'config' ? (
        <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
          <CrawlerConfigPanel />
        </div>
      ) : (
        <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
          <CrawlerQueuePanel />
        </div>
      )}
    </aside>
  )
}
