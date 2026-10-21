import { Component, ErrorInfo, ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
  componentStack: string
}

/**
 * 全局错误边界兜底。
 *
 * 背景：React 渲染期抛错会卸载整棵树，页面直接白屏，用户看不到任何线索。
 * 至少要把错误信息和组件栈显示出来，并给一个恢复入口。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '' }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 控制台留完整现场，便于 issue 反馈
    console.error('[ErrorBoundary] 渲染崩溃:', error, info.componentStack)
    this.setState({ componentStack: info.componentStack || '' })
  }

  private handleReload = () => {
    this.setState({ error: null, componentStack: '' })
    window.location.reload()
  }

  render() {
    const { error, componentStack } = this.state
    if (!error) return this.props.children

    return (
      <div className="flex h-screen w-screen items-center justify-center bg-cyber-bg-primary p-6">
        <div className="w-full max-w-2xl rounded-lg border border-cyber-neon-pink/40 bg-cyber-bg-panel p-6 font-mono shadow-lg">
          <h1 className="mb-2 text-base font-bold text-cyber-neon-pink">
            页面渲染出错（已拦截，未白屏）
          </h1>
          <p className="mb-4 text-xs leading-relaxed text-cyber-text-secondary">
            界面遇到了一个未处理的错误。你可以直接刷新页面重试；如果反复出现，
            请把下面的错误信息反馈给开发者。
          </p>
          <div className="mb-4 max-h-48 overflow-auto rounded-md border border-cyber-border-subtle bg-cyber-bg-tertiary/50 p-3 text-[11px] leading-relaxed text-cyber-neon-orange">
            <div className="mb-2 font-bold text-cyber-text-primary">
              {error.name}: {error.message}
            </div>
            {componentStack && (
              <pre className="whitespace-pre-wrap text-cyber-text-muted">{componentStack}</pre>
            )}
          </div>
          <button
            onClick={this.handleReload}
            className="rounded-md bg-cyber-neon-cyan px-4 py-2 text-xs font-bold text-cyber-bg-primary transition-colors hover:bg-cyber-neon-cyan/90"
          >
            刷新页面
          </button>
        </div>
      </div>
    )
  }
}
