import { afterEach, describe, it, expect, vi } from 'vitest'
import { canOpenDirInEditor, openDirInEditor } from '../lib/electron'

/**
 * The renderer gate for the per-project "open this folder" launch (#1142).
 *
 * It has to agree with the main process on BOTH of that channel's conditions, or
 * the UI offers a row the channel refuses:
 *
 *  - the `fileOpenAPI.openDir` bridge exists (the shell loads the gateway's SPA,
 *    so an updated dashboard can run inside a shell whose preload predates it);
 *  - the SHELL runs Linux, the only platform where the native open resolves the
 *    path inside the `shell.openPath()` call — so the handler's realpath and
 *    not-a-bundle checks bind the object that actually opens. macOS and Windows
 *    defer the open and re-resolve afterwards, which for a directory is an
 *    arbitrary-code-execution window (a macOS bundle IS a directory).
 *
 * Both are read lazily from `window`, which is what lets this drive them
 * per-case rather than at import.
 */

type TestWindow = {
  fileOpenAPI?: { open?: unknown; openDir?: unknown }
  kirocrew?: { isElectron?: boolean; platform?: string }
}

const w = window as unknown as TestWindow

/** Put the window in the state a given shell + preload combination produces. */
function shell({ platform, openDir = true }: { platform?: string; openDir?: boolean }) {
  if (platform === undefined) delete w.kirocrew
  else w.kirocrew = { isElectron: true, platform }
  w.fileOpenAPI = openDir
    ? { open: vi.fn(), openDir: vi.fn().mockResolvedValue({ ok: true }) }
    : { open: vi.fn() }
}

afterEach(() => {
  delete w.kirocrew
  delete w.fileOpenAPI
})

describe('canOpenDirInEditor', () => {
  it('is true only on a Linux shell that exposes openDir', () => {
    shell({ platform: 'linux' })
    expect(canOpenDirInEditor()).toBe(true)
  })

  it('is false on macOS and Windows shells, whose native open is deferred', () => {
    // Not a capability gap in the bridge — the bridge is right there. The main
    // process refuses these platforms because a validate-then-launch check
    // cannot bind a path the launch re-resolves later, so offering the row would
    // promise a launch that answers `unsupported platform`.
    for (const platform of ['darwin', 'win32']) {
      shell({ platform })
      expect(canOpenDirInEditor()).toBe(false)
    }
  })

  it('is false on an unrecognised platform, matching the channel failing closed', () => {
    shell({ platform: 'freebsd' })
    expect(canOpenDirInEditor()).toBe(false)
  })

  it('is false in a browser tab, which has no shell platform at all', () => {
    // A plain tab and the PWA expose no `window.kirocrew`, so there is nothing
    // to launch on even if some other page had left a bridge object behind.
    shell({ platform: undefined })
    expect(canOpenDirInEditor()).toBe(false)
  })

  it('is false when the shell predates openDir, even on Linux', () => {
    shell({ platform: 'linux', openDir: false })
    expect(canOpenDirInEditor()).toBe(false)
  })
})

describe('openDirInEditor', () => {
  it('hands the path to the bridge and returns its verdict', async () => {
    shell({ platform: 'linux' })
    await expect(openDirInEditor('/home/me/project')).resolves.toEqual({ ok: true })
    expect(w.fileOpenAPI?.openDir).toHaveBeenCalledWith('/home/me/project')
    // Never the FILE half: that channel admits a path by extension, which a
    // directory does not have.
    expect(w.fileOpenAPI?.open).not.toHaveBeenCalled()
  })

  it('answers a definite negative rather than throwing with no bridge', async () => {
    shell({ platform: 'linux', openDir: false })
    await expect(openDirInEditor('/home/me/project')).resolves.toEqual({
      ok: false, error: 'unavailable',
    })
  })

  it('reports a rejected bridge call as its error message', async () => {
    // The launch crosses an IPC boundary, so a rejection is reachable; a thrown
    // error here would leave the caller with no failure line to render.
    shell({ platform: 'linux' })
    w.fileOpenAPI!.openDir = vi.fn().mockRejectedValue(new Error('channel gone'))
    await expect(openDirInEditor('/home/me/project')).resolves.toEqual({
      ok: false, error: 'channel gone',
    })
  })

  it('does NOT re-check the platform: the main process is the authority', async () => {
    // Deliberate split. `canOpenDirInEditor` decides what to RENDER; the refusal
    // itself belongs to the handler, whose answer the caller shows. Duplicating
    // the rule here would give two places to keep in step, and a stale copy
    // would silently swallow a verdict the user should see.
    shell({ platform: 'darwin' })
    w.fileOpenAPI!.openDir = vi.fn().mockResolvedValue({ ok: false, error: 'unsupported platform' })
    await expect(openDirInEditor('/home/me/project')).resolves.toEqual({
      ok: false, error: 'unsupported platform',
    })
  })
})
