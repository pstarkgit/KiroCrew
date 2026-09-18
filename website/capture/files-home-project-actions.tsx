/**
 * Isolated capture entry for the per-project quick actions (#1142), both
 * surfaces the change adds.
 *
 * WHY ISOLATED: every new row is gated on something a browser cannot have — a
 * Linux desktop shell exposing the `fileOpenAPI.openDir` preload bridge, and a
 * host able to spawn a terminal. In a plain dashboard tab the editor rows are
 * correctly withheld, so a live-gateway capture could only ever photograph half
 * the feature. Here the bridge and the shell platform are stubbed at the window
 * boundary in exactly the shapes `preload.js` exposes, and the terminal callback
 * is supplied — the DESKTOP-APP state the change ships for.
 *
 * WHAT IS FAITHFUL: the REAL components, inside the real `BrandingProvider` (so
 * `directLocal` — and therefore the Reveal control each new row sits beside —
 * comes from the branding read rather than a prop), with the real
 * `FileBrowserRail` over a stubbed project tree. In the chip scene the DIRECTORY
 * classification comes from the same `X-Path-Kind` header the real
 * `/api/file-read` endpoint sends, so the chip decides its own kind exactly as it
 * does in production — which is what makes the `kind="dir"` menu real evidence.
 *
 * Query string: ?scene=header|chip&theme=dark&refused=off
 *   refused=on makes the stub bridge answer `{ ok: false, error }` — the frame
 *   for the editor-launch failure line, which is separate from the gateway
 *   reveal's on purpose.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// MarkdownRenderer's chip menu reaches for router context, so the chip scene
// needs one in scope even though nothing here navigates.
import { MemoryRouter } from 'react-router-dom'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI. Pinned to `en`
// because the driver asserts the English labels.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { BrandingProvider } from '../src/hooks/useBranding'
import FilesHomePanel from '../src/pages/chat/FilesHomePanel'
import MarkdownRenderer from '../src/components/MarkdownRenderer'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'chip' ? 'chip' : 'header'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const refused = params.get('refused') === 'on'

// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount (an unset
// preference resolves to `system`, which is LIGHT in headless Chromium). Seed the
// preference it reads, and set the attribute too so the first paint is already
// the right palette.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT_DIR = '/Users/me/workspace/KiroCrew'

/** The desktop shell's preload bridge and platform, in the shapes
 *  `electron/preload.js` exposes. `canOpenDirInEditor()` requires BOTH — the
 *  `openDir` key, and a Linux shell, the only platform whose native open resolves
 *  the path inside the call, so the main process will launch a directory there.
 *  Their absence is what correctly withholds the editor rows in a browser tab and
 *  on a macOS/Windows shell. */
;(window as unknown as { kirocrew: unknown }).kirocrew = { isElectron: true, platform: 'linux' }
;(window as unknown as { fileOpenAPI: unknown }).fileOpenAPI = {
  open: () => Promise.resolve({ ok: true }),
  openDir: () => Promise.resolve(
    refused ? { ok: false, error: 'not a directory' } : { ok: true },
  ),
}

/** Fetch stub at the API boundary, so nothing hangs on a gateway that does not
 *  exist here. Four reads matter: branding (`directLocal` gates Reveal), the
 *  project tree (mounts the rail), git status, and the chip's own kind probe. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  if (url.startsWith('/api/file-read')) {
    // The same three-outcome contract `api_file_read` implements: a directory is
    // a 404 carrying `X-Path-Kind: dir`, which is how a chip learns it is a
    // folder. Only the project directory is answered as one here.
    const probed = decodeURIComponent(new URLSearchParams(url.split('?')[1] || '').get('path') || '')
    const kind = probed.replace(/[/\\]$/, '') === PROJECT_DIR ? 'dir' : 'missing'
    return Promise.resolve(new Response(null, { status: 404, headers: { 'X-Path-Kind': kind } }))
  }
  const body = url.includes('/api/dashboard/branding')
    ? { bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: true }
    : url.includes('/api/project/tree')
      ? {
        root: PROJECT_DIR,
        paths: ['README.md', 'src/main.py', 'src/util.py', 'website/src/App.tsx'],
        directories: ['src', 'website', 'website/src'],
        repo: true,
      }
      : url.includes('/api/project/git/status')
        ? { repo: true, branch: 'main', ahead: 0, behind: 0, files: [] }
        : {}
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

/** The Files tab header: Reveal beside the new `Project actions` overflow. */
function HeaderScene() {
  return (
    // A right-dock-sized frame, the width the panel occupies in the chat.
    <div
      style={{ width: 460, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
      className="bg-bg text-text"
    >
      <FilesHomePanel
        projectDir={PROJECT_DIR}
        onFileOpen={() => {}}
        onOpenTerminal={() => {}}
      />
    </div>
  )
}

/** A transcript directory chip, whose right-click menu now carries the folder
 *  editor row. The chip classifies itself through the stubbed kind probe. */
function ChipScene() {
  return (
    <div data-capture-root className="bg-bg text-text p-5" style={{ width: 560 }}>
      <MarkdownRenderer
        content={`The project is checked out at \`${PROJECT_DIR}\`.`}
        onFileOpen={() => {}}
        onFolderOpen={() => {}}
      />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider>
      <BrandingProvider>
        <MemoryRouter>
          {scene === 'chip' ? <ChipScene /> : <HeaderScene />}
        </MemoryRouter>
      </BrandingProvider>
    </ThemeProvider>
  </QueryClientProvider>,
)
