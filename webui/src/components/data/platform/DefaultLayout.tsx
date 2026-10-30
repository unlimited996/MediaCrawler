import { Heart, MessageSquare, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {buildContent, pickMedia, MediaImage, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink, authorAnchor, TopicChips, CommentReplies, StanceBadge} from './shared'
import type { LayoutProps } from './XhsLayout'

interface DefaultLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * 通用详情页（未注册平台 / 兜底）
 * 中性配色、清晰的信息层级：标题 → 元信息 → 正文 → 媒体 → 标签 → 互动 → 评论
 */
export function DefaultLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: DefaultLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const gallery = media.map((m) => m.candidates)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5 shadow-sm">
        <div className="flex items-center gap-2.5">
          <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={32} />
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
        </div>

        {d?.title && (
          <h2 className="mt-4 text-base font-bold leading-snug text-cyber-text-primary">
            {d.title}
          </h2>
        )}

        {content && (
          <div className="mt-2 whitespace-pre-wrap text-sm leading-7 text-cyber-text-secondary">
            {content}
          </div>
        )}

        {media.length > 0 && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
            {media.map((m, i) => (
              <MediaImage
                key={i}
                media={m}
                gallery={gallery}
                galleryIndex={i}
                className="aspect-square w-full cursor-zoom-in rounded-lg border border-cyber-border-subtle object-cover"
              />
            ))}
          </div>
        )}

        <TopicChips topics={tags} theme={theme} />

        <footer className="mt-4 flex items-center gap-4 border-t border-cyber-border-subtle pt-3 text-xs text-cyber-text-secondary">
          <span className="flex items-center gap-1.5">
            <Heart className="h-3.5 w-3.5" style={{ color: theme.accent }} />
            {fmtNum(d?.like_count)}
          </span>
          <span className="flex items-center gap-1.5">
            <MessageSquare className="h-3.5 w-3.5" />
            {fmtNum(d?.comment_count ?? commentsTotal)}
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

      <section className="rounded-xl border border-cyber-border-subtle bg-cyber-bg-panel p-5">
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
                    <StanceBadge stance={c.stance} t={t} tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                  </div>
                  <div className="mt-0.5 text-sm leading-6 text-cyber-text-primary"><RichText content={c.content} /></div>
                  <div className="mt-1 flex items-center gap-3 text-[11px] text-cyber-text-muted">
                    <span>{fmtTs(c.publish_ts)}</span>
                    <span className="flex items-center gap-1">
                      <Heart className="h-3 w-3" />
                      {fmtNum(c.like_count)}
                    </span>
                  </div>
                  <CommentReplies replies={c.replies} replyCount={c.reply_count} commentId={c.id} theme={theme} t={t} />
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  )
}
