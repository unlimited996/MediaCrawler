import type { ReactNode } from 'react'
import {
  Heart, Star, MessageSquare, ExternalLink, Repeat2,
} from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'

export interface LayoutProps {
  d: any
  comments: any[]
  commentsTotal: number
  /** 评论区头部工具条（统计 + 筛选），由详情页注入 */
  commentsToolbar?: ReactNode
  t: (key: string, opts?: any) => string
}

interface XhsLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 小红书详情页
 * 白底圆角笔记卡 → 作者行 → 标题 → 正文 → 图片网格 → 话题标签 → 赞/收藏/评论互动栏 → 评论区
 */
export function XhsLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: XhsLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  const pill =
    'flex items-center gap-1.5 rounded-full border border-cyber-border-subtle bg-cyber-bg-tertiary/60 px-3 py-1.5 text-xs text-cyber-text-secondary'

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      {/* 笔记卡片 */}
      <article className="rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel p-5 shadow-sm">
        {/* 作者行 */}
        <header className="flex items-center gap-3">
          <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={40} />
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-cyber-text-primary">
              {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
            </div>
            <div className="text-[11px] text-cyber-text-muted">{fmtTs(d?.publish_ts)}</div>
          </div>
          <span
            className="ml-auto rounded-full px-2.5 py-0.5 text-[11px] font-medium"
            style={{ color: theme.accent, background: theme.accentSoft }}
          >
            {theme.label}
          </span>
        </header>

        {/* 标题 */}
        {d?.title && (
          <h2 className="mt-4 text-base font-bold leading-snug text-cyber-text-primary">
            {d.title}
          </h2>
        )}

        {/* 正文 */}
        {content && (
          <div className="mt-2 whitespace-pre-wrap text-sm leading-7 text-cyber-text-secondary">
            {content}
          </div>
        )}

        {/* 图片网格：≤4 两列，>4 三列 */}
        {media.length > 0 && (
          <div
            className={`mt-3 grid gap-1.5 ${media.length <= 4 ? 'grid-cols-2' : 'grid-cols-3'}`}
          >
            {media.map((m, i) => (
              <a key={i} href={m.url} target="_blank" rel="noopener noreferrer" className="block">
                <img
                  src={m.url}
                  alt=""
                  loading="lazy"
                  className="aspect-square w-full rounded-lg border border-cyber-border-subtle object-cover"
                />
              </a>
            ))}
          </div>
        )}

        {/* 话题标签：从标题/正文摘出来，用平台色展示在正文底部 */}
        <TopicChips topics={tags} theme={theme} />

        {/* 互动栏 */}
        <footer className="mt-4 flex items-center gap-2 border-t border-cyber-border-subtle pt-3">
          <span className={pill}>
            <Heart className="h-3.5 w-3.5" style={{ color: theme.accent }} />
            {fmtNum(d?.like_count)}
          </span>
          {d?.collect_count != null && (
            <span className={pill}>
              <Star className="h-3.5 w-3.5" />
              {fmtNum(d?.collect_count)}
            </span>
          )}
          <span className={pill}>
            <MessageSquare className="h-3.5 w-3.5" />
            {fmtNum(d?.comment_count ?? commentsTotal)}
          </span>
          {d?.share_count != null && (
            <span className={pill}>
              <Repeat2 className="h-3.5 w-3.5" />
              {fmtNum(d?.share_count)}
            </span>
          )}
          {sourceUrl && (
            <a
              href={sourceUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto flex items-center gap-1 text-[11px] text-cyber-text-muted hover:text-cyber-text-primary"
            >
              <ExternalLink className="h-3 w-3" />
              {t('manage.originLink')}
            </a>
          )}
        </footer>
      </article>

      {/* 评论区 */}
      <section className="rounded-2xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
        <h3 className="text-sm font-medium text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 space-y-4">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-2.5">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-1.5 text-xs text-cyber-text-muted">
                    {authorAnchor(c.author_url, c.author || '-')}
                    <StanceBadge stance={c.stance} t={t} />
                  </div>
                  <div className="mt-0.5 text-sm leading-6 text-cyber-text-primary">
                    <RichText content={c.content} />
                  </div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span>{fmtTs(c.publish_ts)}</span>
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                  </div>
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme}
                    t={t}
                    containerClassName="mt-2 space-y-2 rounded-lg bg-cyber-bg-tertiary/50 p-2.5"
                  />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
