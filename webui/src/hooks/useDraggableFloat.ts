import { useCallback, useEffect, useRef, useState } from 'react'

/** 右下角浮动按钮/浮窗的可拖拽位置（localStorage 持久化，避免遮挡其它按钮）。 */
export interface FloatPos {
  left: number
  top: number
}

const STORE_PREFIX = 'mc_float_pos_'

export function useDraggableFloat(key: string, fallback: FloatPos) {
  const [pos, setPos] = useState<FloatPos>(() => {
    try {
      const raw = localStorage.getItem(STORE_PREFIX + key)
      if (raw) {
        const v = JSON.parse(raw)
        if (typeof v.left === 'number' && typeof v.top === 'number') return v
      }
    } catch { /* 忽略损坏的本地数据 */ }
    return fallback
  })
  const posRef = useRef(pos)
  useEffect(() => { posRef.current = pos }, [pos])

  const drag = useRef<{ dx: number; dy: number; moved: boolean } | null>(null)
  const justDragged = useRef(false)

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLElement>) => {
    if (e.button !== 0) return
    // 关闭/输入等交互元素不启动拖拽
    if ((e.target as HTMLElement).closest('[data-no-drag]')) return
    const rect = e.currentTarget.getBoundingClientRect()
    drag.current = {
      dx: e.clientX - rect.left,
      dy: e.clientY - rect.top,
      moved: false,
    }
    justDragged.current = false
    try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* 忽略 */ }
  }, [])

  const onPointerMove = useCallback((e: React.PointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d) return
    const left = e.clientX - d.dx
    const top = e.clientY - d.dy
    if (Math.abs(left - posRef.current.left) > 3
        || Math.abs(top - posRef.current.top) > 3) {
      d.moved = true
    }
    setPos({
      left: Math.max(0, Math.min(window.innerWidth - 48, left)),
      top: Math.max(0, Math.min(window.innerHeight - 48, top)),
    })
  }, [])

  const onPointerUp = useCallback(() => {
    if (drag.current?.moved) {
      justDragged.current = true
      try {
        localStorage.setItem(STORE_PREFIX + key, JSON.stringify(posRef.current))
      } catch { /* 忽略 */ }
    }
    drag.current = null
  }, [key])

  /** 点击前调用：刚拖动过就吞掉这次 click，避免误触发打开/关闭。 */
  const consumeDragClick = useCallback(() => {
    if (justDragged.current) {
      justDragged.current = false
      return true
    }
    return false
  }, [])

  return {
    pos,
    dragHandlers: { onPointerDown, onPointerMove, onPointerUp },
    consumeDragClick,
  }
}
