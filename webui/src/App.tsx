import { useEffect, useState } from 'react'
import { Toaster } from 'sonner'
import { Sidebar } from '@/components/layout/Sidebar'
import { MainContent } from '@/components/layout/MainContent'
import { CrawlLeftPanel } from '@/components/layout/CrawlLeftPanel'
import { LoginQrcodeFab } from '@/components/layout/LoginQrcodeFab'
import { LoginPageDialog } from '@/components/layout/LoginPageDialog'
import { CaptchaDialog } from '@/components/layout/CaptchaDialog'
import { EnvironmentCheck, isEnvChecked } from '@/components/env/EnvironmentCheck'
import { LicenseDisclaimer, isLicenseAccepted } from '@/components/license/LicenseDisclaimer'
import { HotTimelineView } from '@/views/HotTimelineView'
import { DataManagementView } from '@/views/DataManagementView'
import { ScheduledTasksView } from '@/views/ScheduledTasksView'
import { StanceTagsView } from '@/views/StanceTagsView'
import { S3BackupView } from '@/views/S3BackupView'
import { ConfigAdminView } from '@/views/ConfigAdminView'
import { GuestView } from '@/views/GuestView'
import { DataExplorer } from '@/components/data/DataExplorer'
import { ImageViewerProvider } from '@/components/data/platform/ImageViewer'
import { useHashRoute } from '@/lib/useHashRoute'
import type { MainView } from '@/components/layout/Sidebar'

function App() {
  // 视图与平台由 URL hash 驱动（#/crawl、#/data/xhs、#/guest…）：
  // 菜单切换即改 URL，刷新/分享链接能恢复到对应视图
  const [route, navigate] = useHashRoute()
  const view = route.view as MainView
  const dataPlatform = route.platform

  // 监听全局「打开帖子详情」事件（S3 备份记录行 → 跳转到原贴）：
  // 直接改 URL（带 storyId），刷新/分享也能停在该帖子
  useEffect(() => {
    const onOpenStory = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        { storyId?: number; platform?: string } | undefined
      if (!detail?.storyId) return
      navigate({ view: 'data', platform: detail.platform || '', storyId: detail.storyId })
    }
    window.addEventListener('mc:open-story', onOpenStory)
    return () => window.removeEventListener('mc:open-story', onOpenStory)
  }, [navigate])
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
    navigate({ view: next, platform: platform ?? '' })
  }

  return (
    <ImageViewerProvider>
      <div className="flex flex-col h-screen cyber-grid overflow-hidden relative">
        {/* License Disclaimer Modal - 首次使用时弹出（顶部横幅已移除；访客视图跳过） */}
        {view !== 'guest' && !licenseAccepted && (
          <LicenseDisclaimer onAccept={handleLicenseAccept} />
        )}

        {/* Environment Check Modal - Shows after license accepted (访客视图跳过) */}
        {view !== 'guest' && licenseAccepted && !envChecked && (
          <EnvironmentCheck onCheckComplete={handleEnvCheckComplete} />
        )}

        {/* Header Bar：访客视图不显示管理菜单（页面自带精简头部） */}
        {view !== 'guest' && (
          <Sidebar
            view={view}
            onViewChange={handleViewChange}
            dataPlatform={dataPlatform}
          />
        )}

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
          ) : view === 'config' ? (
            <ConfigAdminView />
          ) : view === 'guest' ? (
            <GuestView onEnterFull={() => handleViewChange('crawl')}
                       storyId={route.storyId}
                       navigate={navigate} />
          ) : (
            <DataManagementView platform={dataPlatform}
                                storyId={route.storyId}
                                navigate={navigate} />
          )}
        </div>

        {/* 扫码登录二维码：全局浮动可收纳，任何页面都能扫（访客视图除外） */}
        {view !== 'guest' && <LoginQrcodeFab />}
        {view !== 'guest' && <LoginPageDialog />}
        {view !== 'guest' && <CaptchaDialog />}

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
    </ImageViewerProvider>
  )
}

export default App
