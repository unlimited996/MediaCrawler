import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import {
  ZoomIn, ZoomOut, RotateCcw, X, ChevronLeft, ChevronRight, Loader2,
} from 'lucide-react'

/**
 * 点击图片后弹出全屏看图窗：支持滚轮 / 按钮缩放、拖拽平移、双击放大、
 * 多图左右翻页，且复用与 MediaImage 一致的「本地 → 平台远程 → S3 备份」候选源重试。
 *
 * 之前帖子图 / 评论图都是 `<a target="_blank">` 直接在新标签页打开平台远程原链，
 * 而远程链带时间签名常已 403，体验差还跳出去。这里改成应用内弹窗，不再跳新路径。
 *
 * 通过 Context 暴露 open()，各平台布局与评论配图只需把候选源数组交给它即可。
 */

export interface ImageViewerState {
  /**
   * 打开看图弹窗。
   * @param images 每张图的候选源数组列表（[本地, 远程, S3]）；多张时可翻页
   * @param index  起始下标
   */
  open: (images: string[][], index?: number) => void
}

const ImageViewerContext = React.createContext<ImageViewerState | null>(null)

/** 在应用内任意位置调用，拿到 open() 打开看图弹窗（没有 Provider 时返回 null） */
export function useImageViewer(): ImageViewerState | null {
  return React.useContext(ImageViewerContext)
}

const MIN_SCALE = 0.5
const MAX_SCALE = 6

export function ImageViewerProvider({ children }: { children: React.ReactNode }) {
  const [images, setImages] = React.useState<string[][]>([])
  const [imgIdx, setImgIdx] = React.useState(0)
  const [srcIdx, setSrcIdx] = React.useState(0)
  const [scale, setScale] = React.useState(1)
  const [tx, setTx] = React.useState(0)
  const [ty, setTy] = React.useState(0)
  const [dragging, setDragging] = React.useState(false)
  const [isOpen, setIsOpen] = React.useState(false)
  const [isLoading, setIsLoading] = React.useState(false)
  const [isBroken, setIsBroken] = React.useState(false)
  const dragRef = React.useRef<{ x: number; y: number; tx: number; ty: number } | null>(null)
  const movedRef = React.useRef(false)

  const open = React.useCallback((imgs: string[][], index = 0) => {
    if (!imgs || imgs.length === 0) return
    const i = Math.min(Math.max(0, index), imgs.length - 1)
    setImages(imgs)
    setImgIdx(i)
    setSrcIdx(0)
    setScale(1)
    setTx(0)
    setTy(0)
    setIsOpen(true)
  }, [])

  const close = React.useCallback(() => setIsOpen(false), [])

  const candidates = images[imgIdx] || []
  const currentSrc = candidates.length ? (candidates[srcIdx] || candidates[0]) : ''

  const resetView = React.useCallback(() => {
    setScale(1); setTx(0); setTy(0)
  }, [])

  const setZoom = React.useCallback((next: number) => {
    const ns = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next))
    if (ns <= 1.001) resetView()
    else setScale(ns)
  }, [resetView])

  const goTo = React.useCallback((next: number) => {
    if (images.length === 0) return
    const n = ((next % images.length) + images.length) % images.length
    setImgIdx(n)
    setSrcIdx(0)
    resetView()
  }, [images.length, resetView])

  // 换图 / 换候选源时重置加载态
  React.useEffect(() => {
    if (!isOpen) return
    setIsLoading(true)
    setIsBroken(false)
  }, [currentSrc, isOpen])

  // 键盘：← → 翻页，+ - 缩放，0/1 复位
  React.useEffect(() => {
    if (!isOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'ArrowLeft') goTo(imgIdx - 1)
      else if (e.key === 'ArrowRight') goTo(imgIdx + 1)
      else if (e.key === '+' || e.key === '=') setZoom(scale + 0.5)
      else if (e.key === '-' || e.key === '_') setZoom(scale - 0.5)
      else if (e.key === '0' || e.key === '1') resetView()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isOpen, imgIdx, scale, goTo, setZoom, resetView])

  const onImgError = () => {
    if (srcIdx + 1 < candidates.length) setSrcIdx(srcIdx + 1)
    else setIsBroken(true)
    setIsLoading(false)
  }

  const onWheel = (e: React.WheelEvent) => {
    const delta = e.deltaY < 0 ? 0.3 : -0.3
    setZoom(scale + delta)
  }

  const onPointerDown = (e: React.PointerEvent) => {
    movedRef.current = false
    if (scale <= 1) return
    ;(e.target as HTMLElement).setPointerCapture?.(e.pointerId)
    dragRef.current = { x: e.clientX, y: e.clientY, tx, ty }
    setDragging(true)
  }
  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current
    if (!d) return
    const dx = e.clientX - d.x
    const dy = e.clientY - d.y
    if (Math.abs(dx) > 4 || Math.abs(dy) > 4) movedRef.current = true
    setTx(d.tx + dx)
    setTy(d.ty + dy)
  }
  const onPointerUp = () => {
    dragRef.current = null
    setDragging(false)
  }

  const onDoubleClick = () => {
    if (scale > 1.001) resetView()
    else setZoom(2)
  }

  // 点图片区空白处（非图片本身）关闭；拖拽过则忽略，避免误关
  const onBackdropClick = (e: React.MouseEvent) => {
    if (e.target !== e.currentTarget) return
    if (movedRef.current) { movedRef.current = false; return }
    close()
  }

  return (
    <ImageViewerContext.Provider value={{ open }}>
      {children}
      <DialogPrimitive.Root open={isOpen} onOpenChange={(o) => { if (!o) close() }}>
        <DialogPrimitive.Portal>
          <DialogPrimitive.Overlay
            className="fixed inset-0 z-[60] bg-black/85 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0"
          />
          <DialogPrimitive.Content
            aria-label="图片查看器"
            className="fixed inset-0 z-[70] flex flex-col bg-transparent outline-none"
            onWheel={onWheel}
          >
            <DialogPrimitive.Title className="sr-only">图片查看器</DialogPrimitive.Title>

            {/* 顶部条：计数 + 关闭 */}
            <div className="flex items-center justify-between px-4 py-3 text-white/90">
              <span className="font-mono text-sm">
                {images.length > 1 ? `${imgIdx + 1} / ${images.length}` : ''}
              </span>
              <button
                type="button"
                onClick={close}
                className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 text-white transition-colors hover:bg-white/20"
                aria-label="关闭"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {/* 图片区 */}
            <div
              className="relative flex min-h-0 flex-1 items-center justify-center overflow-hidden"
              onClick={onBackdropClick}
            >
              {images.length > 1 && (
                <>
                  <button
                    type="button"
                    onClick={() => goTo(imgIdx - 1)}
                    className="absolute left-3 top-1/2 z-10 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/40 text-white transition-colors hover:bg-black/60"
                    aria-label="上一张"
                  >
                    <ChevronLeft className="h-6 w-6" />
                  </button>
                  <button
                    type="button"
                    onClick={() => goTo(imgIdx + 1)}
                    className="absolute right-3 top-1/2 z-10 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/40 text-white transition-colors hover:bg-black/60"
                    aria-label="下一张"
                  >
                    <ChevronRight className="h-6 w-6" />
                  </button>
                </>
              )}

              <div
                className="flex h-full w-full select-none items-center justify-center"
                style={{ cursor: scale > 1 ? (dragging ? 'grabbing' : 'grab') : 'zoom-in', touchAction: 'none' }}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={onPointerUp}
                onPointerLeave={onPointerUp}
                onDoubleClick={onDoubleClick}
              >
                {isLoading && (
                  <div className="absolute inset-0 flex items-center justify-center">
                    <Loader2 className="h-8 w-8 animate-spin text-white/70" />
                  </div>
                )}
                {isBroken ? (
                  <div className="text-sm text-white/70">图片加载失败（所有候选源均不可用）</div>
                ) : (
                  <img
                    key={currentSrc}
                    src={currentSrc}
                    alt=""
                    draggable={false}
                    onLoad={() => setIsLoading(false)}
                    onError={onImgError}
                    className="max-h-full max-w-full object-contain shadow-2xl"
                    style={{
                      transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
                      transition: dragging ? 'none' : 'transform 0.12s ease-out',
                    }}
                  />
                )}
              </div>
            </div>

            {/* 底部控制条：缩小 / 比例 / 放大 / 复位 */}
            <div className="flex items-center justify-center gap-2 px-4 py-3 text-white">
              <button
                type="button"
                onClick={() => setZoom(scale - 0.5)}
                className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 transition-colors hover:bg-white/20"
                aria-label="缩小"
              >
                <ZoomOut className="h-5 w-5" />
              </button>
              <span className="w-14 text-center font-mono text-sm">{Math.round(scale * 100)}%</span>
              <button
                type="button"
                onClick={() => setZoom(scale + 0.5)}
                className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 transition-colors hover:bg-white/20"
                aria-label="放大"
              >
                <ZoomIn className="h-5 w-5" />
              </button>
              <button
                type="button"
                onClick={resetView}
                className="flex h-9 w-9 items-center justify-center rounded-full bg-white/10 transition-colors hover:bg-white/20"
                aria-label="重置大小"
              >
                <RotateCcw className="h-4 w-4" />
              </button>
            </div>
          </DialogPrimitive.Content>
        </DialogPrimitive.Portal>
      </DialogPrimitive.Root>
    </ImageViewerContext.Provider>
  )
}
