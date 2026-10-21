import { useState } from 'react'
import { Toaster } from 'sonner'
import { Sidebar } from '@/components/layout/Sidebar'
import { MainContent } from '@/components/layout/MainContent'
import { CrawlLeftPanel } from '@/components/layout/CrawlLeftPanel'
import { LoginQrcodeFab } from '@/components/layout/LoginQrcodeFab'
import { EnvironmentCheck, isEnvChecked } from '@/components/env/EnvironmentCheck'
import { LicenseDisclaimer, isLicenseAccepted } from '@/components/license/LicenseDisclaimer'
import { HotTimelineView } from '@/views/HotTimelineView'
import { DataManagementView } from '@/views/DataManagementView'
import { ScheduledTasksView } from '@/views/ScheduledTasksView'
import { StanceTagsView } from '@/views/StanceTagsView'
import { S3BackupView } from '@/views/S3BackupView'
import { DataExplorer } from '@/components/data/DataExplorer'
import type { MainView } from '@/components/layout/Sidebar'

function App() {
  // 主视图：抓取 / 热搜时光机 / 数据浏览 / 数据管理（按平台拆分）
  const [view, setView] = useState<MainView>('crawl')
  /** 数据管理当前选中的平台（'' = 全部）；平台入口在顶部第二行 */
  const [dataPlatform, setDataPlatform] = useState('')
  // Initialize by checking localStorage if license has been accepted
  const [licenseAccepted, setLicenseAccepted] = useState(() => isLicenseAccepted())
  // Initialize by checking localStorage if env check has passed
  const [envChecked, setEnvChecked] = useState(() => isEnvChecked())

  const handleEnvCheckComplete = () => {
    setEnvChecked(true)
  }

  const handleLicenseAccept = () => {
    setLicenseAccepted(true)
  }

  /** 顶部导航切换；进数据管理时一并带上平台（平台入口按平台直达） */
  const handleViewChange = (next: MainView, platform?: string) => {
    setView(next)
    if (next === 'data') setDataPlatform(platform ?? '')
  }

  return (
    <div className="flex flex-col h-screen cyber-grid overflow-hidden relative">
      {/* License Disclaimer Modal - 首次使用时弹出（顶部横幅已移除） */}
      {!licenseAccepted && (
        <LicenseDisclaimer onAccept={handleLicenseAccept} />
      )}

      {/* Environment Check Modal - Shows after license accepted */}
      {licenseAccepted && !envChecked && (
        <EnvironmentCheck onCheckComplete={handleEnvCheckComplete} />
      )}

      {/* Header Bar */}
      <Sidebar
        view={view}
        onViewChange={handleViewChange}
        dataPlatform={dataPlatform}
      />

      {/* Main Area */}
      <div className="flex-1 flex flex-col gap-4 p-4 overflow-hidden min-h-0">
        {view === 'crawl' ? (
          <div className="flex-1 flex gap-4 overflow-hidden min-h-0">
            {/* 左侧：配置 / 队列 互斥切换（不同时出现） */}
            <CrawlLeftPanel />

            {/* 右侧：控制台独占，全屏 */}
            <MainContent />
          </div>
        ) : view === 'discover' ? (
          <HotTimelineView />
        ) : view === 'tasks' ? (
          <ScheduledTasksView />
        ) : view === 'browser' ? (
          <DataExplorer />
        ) : view === 'stance' ? (
          <StanceTagsView />
        ) : view === 'backup' ? (
          <S3BackupView />
        ) : (
          <DataManagementView platform={dataPlatform} />
        )}
      </div>

      {/* 扫码登录二维码：全局浮动可收纳，任何页面都能扫 */}
      <LoginQrcodeFab />

      {/* Toast notifications - Theme-aware style */}
      <Toaster
        position="top-right"
        toastOptions={{
          className: 'glass-panel font-mono text-cyber-text-primary',
          style: {
            fontFamily: 'JetBrains Mono, monospace',
          },
        }}
      />
    </div>
  )
}

export default App
