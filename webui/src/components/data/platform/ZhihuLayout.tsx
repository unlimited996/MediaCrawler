import { ThumbsUp, MessageSquare, Bookmark, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface ZhihuLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 知乎详情页
 * 「问题」徽标 + 标题 + 回答正文 + 赞同（蓝色实心）/评论/收藏操作栏 + 简洁评论列表
 */
export function ZhihuLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: ZhihuLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5 shadow-sm">
        {/* 问题头 */}
        <div className="flex items-center gap-2">
          <span
            className="rounded px-1.5 py-0.5 text-[11px] font-medium text-white"
            style={{ background: theme.accent }}
          >
            {t('manage.question')}
          </span>
          <span className="text-[11px] text-cyber-text-muted">{fmtTs(d?.publish_ts)}</span>
          <span className="ml-auto text-[11px] text-cyber-text-muted">{theme.label}</span>
        </div>

        <h2 className="mt-3 text-lg font-bold leading-snug text-cyber-text-primary">
          {d?.title || '-'}
        </h2>

        {/* 回答正文 */}
        <div className="mt-4 border-t border-cyber-border-subtle pt-4">
          <div className="flex items-center gap-2">
            <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={28} />
            <span className="text-sm font-medium text-cyber-text-primary">
              {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
            </span>
            <span
              className="rounded px-1.5 py-0.5 text-[10px]"
              style={{ color: theme.accent, background: theme.accentSoft }}
            >
              {t('manage.answer')}
            </span>
          </div>
          {content && (
            <div className="mt-3 whitespace-pre-wrap text-sm leading-8 text-cyber-text-secondary">
              {content}
            </div>
          )}
          {media.length > 0 && (
            <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
              {media.map((m, i) => (
                <a key={i} href={m.url} target="_blank" rel="noopener noreferrer">
                  <img
                    src={m.url}
                    alt=""
                    loading="lazy"
                    className="aspect-video w-full rounded-lg border border-cyber-border-subtle object-cover"
                  />
                </a>
              ))}
            </div>
          )}
          <TopicChips topics={tags} theme={theme} />
        </div>

        {/* 操作栏：赞同为蓝色实心主按钮 */}
        <footer className="mt-4 flex items-center gap-2 border-t border-cyber-border-subtle pt-3">
          <span
            className="flex items-center gap-1.5 rounded-full px-3.5 py-1.5 text-xs font-medium text-white"
            style={{ background: theme.accent }}
          >
            <ThumbsUp className="h-3.5 w-3.5" />
            {t('manage.agree')} {fmtNum(d?.like_count)}
          </span>
          <span className="flex items-center gap-1.5 rounded-full border border-cyber-border-subtle px-3 py-1.5 text-xs text-cyber-text-secondary">
            <MessageSquare className="h-3.5 w-3.5" />
            {t('manage.comment')} {fmtNum(d?.comment_count ?? commentsTotal)}
          </span>
          <span className="flex items-center gap-1.5 rounded-full border border-cyber-border-subtle px-3 py-1.5 text-xs text-cyber-text-secondary">
            <Bookmark className="h-3.5 w-3.5" />
            {t('manage.collect')}
          </span>
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

      {/* 评论列表 */}
      <section className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
        <h3 className="text-sm font-medium text-cyber-text-primary">
          {t('manage.comments')}（{commentsTotal}）
        </h3>
        {commentsToolbar}
        {comments.length === 0 ? (
          <p className="mt-4 text-xs text-cyber-text-muted">{t('manage.noComments')}</p>
        ) : (
          <ul className="mt-3 divide-y divide-cyber-border-subtle">
            {comments.map((c: any) => (
              <li key={c.id} className="flex gap-2.5 py-3 first:pt-0">
                <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={26} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-cyber-text-primary">
                      {authorAnchor(c.author_url, c.author || '-')}
                    </span>
                    <span className="text-[11px] text-cyber-text-muted">{fmtTs(c.publish_ts)}</span>
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  </div>
                  <div className="mt-1 text-sm leading-6 text-cyber-text-secondary"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span className="flex items-center gap-1">
                      <ThumbsUp className="h-3 w-3" />
                      {t('manage.agree')} {fmtNum(c.like_count)}
                    </span>
                    <span>{t('manage.reply')}</span>
                  </div>
                  <CommentReplies
                    replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                    theme={theme}
                    t={t}
                    containerClassName="mt-2 space-y-1.5 border-l-2 border-cyber-border-subtle pl-3"
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
