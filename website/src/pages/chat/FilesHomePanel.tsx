import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import { FileText, ExternalLink, TerminalSquare, PenLine, MoreHorizontal } from 'lucide-react'
import { useBranding } from '../../hooks/useBranding'
import { revealOrOpen, useRevealFailure, useRevealLabel } from '../../components/FilePathMenu'
import ErrorNotice from '../../components/ErrorNotice'
import {
  DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem,
} from '../../components/ui/dropdown-menu'
import { canOpenDirInEditor, openDirInEditor } from '../../lib/electron'
import FileBrowserRail, { useTreeState } from './FileBrowserRail'

/** Last path segment, trailing slashes ignored. */
function basename(p: string): string {
  return p.replace(/\/+$/, '').split('/').pop() || p
}

/**
 * The pinned Files tab: an empty preview pane on the left and the permanent
 * file-browser rail on the right, under one full-width header. Clicking a
 * file NEVER opens inline here — every open spawns a file tab (the same
 * primitive every other file-open path lands in), so this tab stays the
 * stable jumping-off point.
 *
 * The rail is deliberately not hideable in this state: without a file, the
 * tree IS the tab.
 *
 * The header is also the dashboard's PER-PROJECT quick-action surface (issue
 * #1142): it is the one always-present place that names the project directory,
 * so "open this project in my editor" and "open a terminal already `cd`'d into
 * it" live here, in a `Project actions` menu, instead of requiring the user to
 * know that a side-panel tab kind spawns a shell in the right directory.
 */
export default function FilesHomePanel({ projectDir, onFileOpen, onAddToContext, onOpenTerminal }: {
  projectDir: string
  /** `opts.line` opens the file at that line — a rail content-search hit. */
  onFileOpen: (absPath: string, diff: boolean, opts?: { line?: number }) => void
  /** Right-click "Add to context" on a tree row — forwarded to the composer
   *  host so a file/folder becomes an `@`-mention. */
  onAddToContext?: (absPath: string, kind: 'file' | 'dir') => void
  /** Spawn a terminal tab whose cwd is this project directory. Omitted when the
   *  host cannot serve one (the terminal feature is off, or the host withdraws
   *  the terminal view), which withdraws the action rather than offering a shell
   *  that will not start. */
  onOpenTerminal?: () => void
}) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  // Reveal shells out on the gateway host, so it only makes sense when the
  // browser is on that same machine. On a remote/tunneled session the backend
  // degrades reveal to a clipboard copy, so hide the affordance to match every
  // other gated file-location surface (FilePathMenu, ReportProblemModal, …).
  const isLocal = useBranding().directLocal
  // The platform-aware wording every other file-location surface uses ("Open in
  // Finder" / "Open in File Explorer" / "Show in file manager"), read from the
  // gateway host that `/api/reveal` shells out on — not a static "file manager".
  const revealLabel = useRevealLabel()
  // A failed reveal (policy-blocked path, no file manager) renders under the
  // header; askAgent on — the Files panel holds no draft.
  const reveal = useRevealFailure(projectDir ?? undefined)
  // "Open project in editor" is a DESKTOP-SHELL launch (fileOpenAPI.openDir →
  // shell.openPath in the main process), so it is gated on the preload bridge,
  // not on `directLocal`: a browser tab and the PWA expose no bridge and the row
  // is withheld there, exactly as the file-path menu's editor row is.
  const canOpenInEditor = canOpenDirInEditor()
  const [editorError, setEditorError] = useState<string | null>(null)
  useEffect(() => { setEditorError(null) }, [projectDir])
  const openInEditor = async () => {
    setEditorError(null)
    const r = await openDirInEditor(projectDir)
    // The bridge always names a reason on failure (missing bridge, not a
    // directory, or the OS refusal string), so there is no English fallback.
    if (!r.ok) setEditorError(r.error || 'error')
  }
  const treeState = useTreeState(projectDir)
  const treeAvailable = treeState === 'ready'
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['project-tree', projectDir] })
    qc.invalidateQueries({ queryKey: ['git-status', projectDir] })
  }
  // The two per-project actions are a MENU, not two more header buttons.
  // `max-two-buttons-per-row` caps this row at two controls, and a
  // `DropdownMenu` trigger counts as one however many items it holds — so the
  // row is Reveal (which shipped as one click and keeps it) plus this trigger.
  //
  // The row had space for the trigger only because the header's old Refresh
  // button, which stood between them, was UNREACHABLE: with a project directory
  // set, `useTreeState` answers `ready` or `error` and nothing else — `ready`
  // covers the in-flight case on purpose (see its own doc comment) — so
  // `!treeAvailable && treeState !== 'error'` was never true here, and the two
  // existing header tests already asserted no Refresh renders. The reachable
  // refreshes are unaffected: the rail owns one, and the tree-error state below
  // owns the other, which is where the remedy belongs anyway.
  const hasMenu = canOpenInEditor || !!onOpenTerminal
  const iconBtn = 'flex items-center justify-center w-[26px] h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none shrink-0'
  return (
    <div className="flex flex-col h-full min-h-0">
      <div className="flex items-center gap-2 h-[38px] px-3 shrink-0 border-b border-border">
        <span className="text-[12px] font-semibold text-text-strong">{t('pages.chat.filesHome.title')}</span>
        {projectDir && <span className="text-[11.5px] text-muted truncate" title={projectDir}>{basename(projectDir)}</span>}
        <span className="flex-1" />
        {projectDir && (
          <>
            {isLocal && (
              <button onClick={() => { void revealOrOpen(projectDir, 'reveal', reveal) }} className={iconBtn} title={revealLabel} aria-label={revealLabel}>
                <ExternalLink size={14} />
              </button>
            )}
            {hasMenu && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button className={iconBtn} title={t('pages.chat.filesHome.project_actions')} aria-label={t('pages.chat.filesHome.project_actions')}>
                    <MoreHorizontal size={14} />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="min-w-[220px]">
                  {canOpenInEditor && (
                    <DropdownMenuItem onSelect={() => { void openInEditor() }}>
                      <PenLine size={13} className="shrink-0" />
                      <span>{t('pages.chat.filesHome.open_project_in_editor')}</span>
                    </DropdownMenuItem>
                  )}
                  {onOpenTerminal && (
                    <DropdownMenuItem onSelect={onOpenTerminal}>
                      <TerminalSquare size={13} className="shrink-0" />
                      <span>{t('pages.chat.filesHome.open_terminal')}</span>
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </>
        )}
      </div>
      {(reveal.error || editorError) && (
        <div className="px-3 py-2 border-b border-border flex flex-col gap-2">
          {reveal.error && (
            <ErrorNotice variant="inline" className="whitespace-normal" message={reveal.error} askAgent onDismiss={reveal.clear} testId="files-home-reveal-error" />
          )}
          {/* The editor launch happens entirely in the desktop shell and never
              touches /api/reveal, so it carries its own line rather than
              borrowing the gateway reveal's. */}
          {editorError && (
            <ErrorNotice variant="inline" className="whitespace-normal" message={editorError} askAgent onDismiss={() => setEditorError(null)} testId="files-home-editor-error" />
          )}
        </div>
      )}
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 flex flex-col items-center justify-center gap-2 text-muted px-6 text-center">
          <FileText size={22} className="opacity-40" />
          {treeState === 'error' ? (
            <>
              {/* A failed fetch is not a missing setting: the directory is set
                  (the header is naming it), the tree endpoint just would not
                  serve it. Retrying is the remedy, so the affordance sits with
                  the message instead of only as a header icon. The Files tab
                  holds no draft → hand-off on, beside the retry. */}
              <ErrorNotice message={t('pages.chat.filesHome.tree_error')} askAgent />
              <button
                onClick={refresh}
                className="text-[12px] px-2.5 h-[26px] rounded-md cursor-pointer transition-colors text-muted hover:text-text hover:bg-bg-hover bg-transparent border border-border"
              >{t('pages.chat.filesHome.refresh')}</button>
            </>
          ) : (
            <span className="text-[12.5px]">
              {treeAvailable ? t('pages.chat.filesHome.select_file_hint') : t('pages.chat.filesHome.no_project_dir')}
            </span>
          )}
        </div>
        {treeAvailable && (
          <FileBrowserRail projectDir={projectDir} onFileOpen={onFileOpen} onAddToContext={onAddToContext} />
        )}
      </div>
    </div>
  )
}
