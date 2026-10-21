import { ThumbsUp, MessageCircle, Share2, Globe, ExternalLink } from 'lucide-react'
import type { PlatformTheme } from './platformTheme'
import {
  buildContent, pickMedia, pickSourceUrl, fmtTs, fmtNum, RichText, AuthorLink,
  authorAnchor, TopicChips, CommentReplies, StanceBadge,
} from './shared'
import type { LayoutProps } from './XhsLayout'

interface FacebookLayoutProps extends LayoutProps {
  theme: PlatformTheme
}

/**
 * Facebook 详情页
 *
 * 对齐 FB 原生帖子卡：白底圆角卡 + 明显的三段式分隔线
 * （正文 → 互动统计行 → 赞/评论/分享 三等分按钮行）；
 * 评论为灰底圆角气泡 + 下方「赞 · 回复 · 时间」文字操作条。
 */
export function FacebookLayout({ d, comments, commentsTotal, commentsToolbar, t, theme }: FacebookLayoutProps) {
  const content = buildContent(d)
  const media = pickMedia(d)
  const sourceUrl = pickSourceUrl(d)
  const tags: string[] = d?.tags || []

  return (
    <div className="mx-auto max-w-2xl space-y-3">
      <article className="overflow-hidden rounded-lg border border-cyber-border-subtle bg-cyber-bg-panel shadow-sm">
        {/* 头部：圆头像 + 蓝字姓名 + 时间 + 公开图标 */}
        <header className="flex items-center gap-2.5 px-4 pt-3">
          <AuthorLink url={d?.author_url} name={d?.author} avatar={d?.author_avatar} theme={theme} size={40} />
          <div className="min-w-0">
            <div className="truncate text-[15px] font-semibold text-cyber-text-primary">
              {authorAnchor(d?.author_url, d?.author || t('manage.author'))}
            </div>
            <div className="flex items-center gap-1 text-[12px] text-cyber-text-muted">
              <span>{fmtTs(d?.publish_ts)}</span>
              <span>·</span>
              <Globe className="h-3 w-3" />
            </div>
          </div>
          <span
            className="ml-auto rounded-full px-2 py-[1px] text-[11px] font-medium"
            style={{ color: theme.accent, background: theme.accentSoft }}
          >
            {theme.label}
          </span>
        </header>

        {d?.title && (
          <h2 className="mt-2 px-4 text-[15px] font-semibold leading-snug text-cyber-text-primary">
            {d.title}
          </h2>
        )}

        {content && (
          <div className="mt-1 whitespace-pre-wrap px-4 text-[15px] leading-7 text-cyber-text-secondary">
            {content}
          </div>
        )}

        {/* 媒体：FB 单图整宽满边，多图走网格 */}
        {media.length > 0 && (
          <div className={`mt-3 grid gap-0.5 ${media.length === 1 ? 'grid-cols-1' : 'grid-cols-2'}`}>
            {media.slice(0, 4).map((m, i) => (
              <a key={i} href={m.url} target="_blank" rel="noopener noreferrer" className="block">
                <img
                  src={m.url}
                  alt=""
                  loading="lazy"
                  className={`w-full border-y border-cyber-border-subtle object-cover ${
                    media.length === 1 ? 'max-h-[420px]' : 'aspect-square'
                  }`}
                />
              </a>
            ))}
          </div>
        )}

        <div className="px-4">
          <TopicChips topics={tags} theme={theme} />
        </div>

        {/* 互动统计行 */}
        <div className="mt-2 flex items-center gap-2 border-t border-cyber-border-subtle px-4 py-2 text-[13px] text-cyber-text-muted">
          <span className="flex items-center gap-1">
            <ThumbsUp className="h-3.5 w-3.5" style={{ color: theme.accent }} />
            {fmtNum(d?.like_count)}
          </span>
          <span className="ml-auto">{fmtNum(d?.comment_count ?? commentsTotal)} {t('manage.comments')}</span>
          <span>{fmtNum(d?.share_count)} {t('manage.share')}</span>
        </div>

        {/* 三等分操作条 */}
        <div className="flex items-center border-t border-cyber-border-subtle text-[14px] font-semibold text-cyber-text-secondary">
          {[
            { icon: ThumbsUp, label: t('manage.like') },
            { icon: MessageCircle, label: t('manage.comments') },
            { icon: Share2, label: t('manage.share') },
          ].map(({ icon: Icon, label }) => (
            <span
              key={label}
              className="flex flex-1 items-center justify-center gap-1.5 py-2 transition-colors hover:bg-cyber-bg-tertiary/60"
            >
              <Icon className="h-4 w-4" />
              {label}
            </span>
          ))}
        </div>

        {/* 评论区：灰底圆角气泡 */}
        <section className="border-t border-cyber-border-subtle bg-cyber-bg-tertiary/30 px-4 py-3">
          <h3 className="text-[13px] font-semibold text-cyber-text-muted">
            {t('manage.comments')}（{commentsTotal}）
          </h3>
          {commentsToolbar}
          {comments.length === 0 ? (
            <p className="mt-3 text-[13px] text-cyber-text-muted">{t('manage.noComments')}</p>
          ) : (
            <ul className="mt-2 space-y-3">
              {comments.map((c: any) => (
                <li key={c.id} className="flex gap-2">
                  <AuthorLink url={c.author_url} name={c.author} avatar={c.author_avatar} theme={theme} size={32} />
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-1.5 rounded-2xl bg-cyber-bg-tertiary px-3 py-2">
                      <span className="text-[13px] font-semibold text-cyber-text-primary">
                        {authorAnchor(c.author_url, c.author || '-')}
                      </span>
                      <StanceBadge stance={c.stance} t={t} size="xs" tagCode={c.stance_tag_code} tagName={c.stance_tag_name} tagColor={c.stance_tag_color} comment={c} />
                      <div className="mt-0.5 w-full whitespace-pre-wrap text-[14px] leading-6 text-cyber-text-primary">
                        <RichText content={c.content} />
                      </div>
                    </div>
                    {/* FB 的文字操作条：赞 · 回复 · 时间 */}
                    <div className="mt-1 flex items-center gap-3 pl-3 text-[12px] font-semibold text-cyber-text-muted">
                      <span className="hover:underline">{t('manage.like')}</span>
                      <span className="hover:underline">{t('manage.reply')}</span>
                      <span className="font-normal">{fmtTs(c.publish_ts)}</span>
                      <span className="ml-auto flex items-center gap-1 font-normal">
                        <ThumbsUp className="h-3 w-3" style={{ color: theme.accent }} />
                        {fmtNum(c.like_count)}
                      </span>
                    </div>
                    <CommentReplies
                      replies={c.replies} replyCount={c.reply_count} commentId={c.id}
                      theme={theme} t={t}
                      containerClassName="mt-2 ml-3 space-y-2 border-l-2 border-cyber-border-subtle pl-3"
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </article>

      {sourceUrl && (
        <a
          href={sourceUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-[13px] text-cyber-text-muted hover:text-cyber-text-primary"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          {t('manage.originLink')}
        </a>
      )}
    </div>
  )
}
