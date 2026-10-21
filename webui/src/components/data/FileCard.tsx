import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { FileJson, FileSpreadsheet, FileText, Download, Eye, FileDown, Loader2, Trash2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { toast } from 'sonner'
import { dataApi, exportApi } from '@/lib/api'
import { formatFileSize, formatDateTime } from '@/lib/utils'
import { DataPreviewDialog } from './preview/DataPreviewDialog'
import type { DataFile } from '@/types/crawler'

interface FileCardProps {
  file: DataFile
  /** 删除成功后回调，用于刷新文件列表 */
  onDeleted?: () => void
}

const fileIcons: Record<string, typeof FileJson> = {
  json: FileJson,
  jsonl: FileJson,
  csv: FileSpreadsheet,
  xlsx: FileSpreadsheet,
  xls: FileSpreadsheet,
}

/** 柔和的类型配色：低饱和底色 + 同色系图标，避免霓虹堆砌 */
const fileStyles: Record<string, { icon: string; tile: string; badge: string }> = {
  json: {
    icon: 'text-cyber-neon-yellow',
    tile: 'bg-cyber-neon-yellow/10',
    badge: 'border-cyber-neon-yellow/30 bg-cyber-neon-yellow/10 text-cyber-neon-yellow',
  },
  jsonl: {
    icon: 'text-cyber-neon-purple',
    tile: 'bg-cyber-neon-purple/10',
    badge: 'border-cyber-neon-purple/30 bg-cyber-neon-purple/10 text-cyber-neon-purple',
  },
  csv: {
    icon: 'text-cyber-neon-green',
    tile: 'bg-cyber-neon-green/10',
    badge: 'border-cyber-neon-green/30 bg-cyber-neon-green/10 text-cyber-neon-green',
  },
  xlsx: {
    icon: 'text-cyber-neon-cyan',
    tile: 'bg-cyber-neon-cyan/10',
    badge: 'border-cyber-neon-cyan/30 bg-cyber-neon-cyan/10 text-cyber-neon-cyan',
  },
  xls: {
    icon: 'text-cyber-neon-cyan',
    tile: 'bg-cyber-neon-cyan/10',
    badge: 'border-cyber-neon-cyan/30 bg-cyber-neon-cyan/10 text-cyber-neon-cyan',
  },
}

const ghostBtn = 'h-7 px-2 font-mono text-[11px] text-cyber-text-secondary hover:bg-cyber-bg-tertiary'

export function FileCard({ file, onDeleted }: FileCardProps) {
  const { t } = useTranslation('data')
  const [previewOpen, setPreviewOpen] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [deleting, setDeleting] = useState(false)

  const Icon = fileIcons[file.type] || FileText
  const styles = fileStyles[file.type] || {
    icon: 'text-cyber-text-secondary',
    tile: 'bg-cyber-bg-tertiary',
    badge: 'border-cyber-border-subtle bg-cyber-bg-tertiary text-cyber-text-secondary',
  }

  // 检查是否支持预览
  const isPreviewable = ['json', 'jsonl', 'csv', 'xlsx', 'xls'].includes(file.type.toLowerCase())

  // 导出 PDF 仅支持结构化文本格式（与后端 SUPPORTED_SUFFIX 保持一致）
  const canExportPdf = ['json', 'jsonl', 'csv'].includes(file.type.toLowerCase())

  const handleDownload = () => {
    const url = dataApi.getDownloadUrl(file.path)
    window.open(url, '_blank')
  }

  // 删除是危险操作：先确认，成功后刷新列表
  const handleDelete = async () => {
    if (!window.confirm(`${t('file.deleteConfirm')}\n${file.name}`)) return
    setDeleting(true)
    try {
      await dataApi.deleteFile(file.path)
      toast.success(t('file.deleteOk'))
      onDeleted?.()
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || ''
      toast.error(`${t('file.deleteFailed')}${detail ? ': ' + detail : ''}`)
    } finally {
      setDeleting(false)
    }
  }

  const handleExportPdf = async () => {
    if (!canExportPdf) {
      toast.error(t('file.exportUnsupported'))
      return
    }
    setExporting(true)
    try {
      const { data } = await exportApi.exportPdf({
        file_path: file.path,
        limit: 200,
        embed_images: true,
      })
      toast.success(t('file.exportSuccess'))
      window.open(exportApi.getDownloadUrl(data.name), '_blank')
    } catch (err: any) {
      const detail = err?.response?.data?.detail || err?.message || ''
      toast.error(`${t('file.exportFailed')}${detail ? ': ' + detail : ''}`)
    } finally {
      setExporting(false)
    }
  }

  return (
    <>
      <Card className="group transition-all duration-200 border-cyber-border-subtle bg-cyber-bg-panel hover:border-cyber-border-default hover:shadow-[0_2px_12px_rgb(0_0_0/0.06)]">
        <CardContent className="flex h-full flex-col p-4">
          {/* 头部：类型图标 + 名称 + 类型徽标 */}
          <div className="flex items-start gap-3">
            <span className={`flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg ${styles.tile} ${styles.icon}`}>
              <Icon className="h-5 w-5" />
            </span>
            <div className="min-w-0 flex-1">
              <h3 className="truncate text-sm font-medium text-cyber-text-primary" title={file.name}>
                {file.name}
              </h3>
              <p className="mt-1 truncate text-[11px] text-cyber-text-muted">
                {formatDateTime(file.modified_at)}
              </p>
            </div>
            <span className={`flex-shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-mono font-medium ${styles.badge}`}>
              .{file.type.toUpperCase()}
            </span>
          </div>

          {/* 元信息：体积 + 记录数 */}
          <div className="mt-3 flex items-center gap-3 text-[11px] text-cyber-text-secondary">
            <span className="font-mono">{formatFileSize(file.size)}</span>
            {file.record_count !== null && (
              <>
                <span className="h-3 w-px bg-cyber-border-subtle" />
                <span className="font-mono">{t('file.entries', { count: file.record_count })}</span>
              </>
            )}
          </div>

          {/* 操作区：底部对齐，hover 显现 */}
          <div className="mt-auto flex items-center justify-end gap-1 border-t border-cyber-border-subtle pt-3 opacity-70 transition-opacity group-hover:opacity-100">
            {canExportPdf && (
              <Button
                variant="ghost"
                size="sm"
                className={`${ghostBtn} text-cyber-neon-purple hover:bg-cyber-neon-purple/10`}
                onClick={handleExportPdf}
                disabled={exporting}
                title={t('file.exportPdf')}
              >
                {exporting ? (
                  <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                ) : (
                  <FileDown className="mr-1 h-3 w-3" />
                )}
                {exporting ? t('file.exporting') : t('file.exportPdf')}
              </Button>
            )}
            {isPreviewable && (
              <Button
                variant="ghost"
                size="sm"
                className={ghostBtn}
                onClick={() => setPreviewOpen(true)}
                title={t('file.preview')}
              >
                <Eye className="mr-1 h-3 w-3" />
                {t('file.preview')}
              </Button>
            )}
            <Button
              variant="ghost"
              size="sm"
              className={ghostBtn}
              onClick={handleDownload}
              title={t('file.extract')}
            >
              <Download className="mr-1 h-3 w-3" />
              {t('file.extract')}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className={`${ghostBtn} text-cyber-neon-pink hover:bg-cyber-neon-pink/10`}
              onClick={handleDelete}
              disabled={deleting}
              title={t('file.delete')}
            >
              {deleting ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Trash2 className="h-3 w-3" />
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* 预览对话框 */}
      {isPreviewable && (
        <DataPreviewDialog
          file={file}
          open={previewOpen}
          onOpenChange={setPreviewOpen}
        />
      )}
    </>
  )
}
