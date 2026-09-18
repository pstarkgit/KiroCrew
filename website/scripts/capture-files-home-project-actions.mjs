/**
 * Real-browser evidence for the per-project quick actions (#1142).
 *
 * Drives the isolated capture entry (website/capture/files-home-project-actions.html),
 * which mounts the REAL components with the desktop shell's `fileOpenAPI` bridge
 * and its Linux platform stubbed at the window boundary — the desktop-app state
 * where the editor rows are available. A plain dashboard tab (and a macOS or
 * Windows shell, which the launch channel refuses) correctly withholds them, so a
 * browser capture without the stub could only ever show half the feature.
 *
 * Frames, per theme:
 *   01-header-closed          the Files header: Reveal, then `Project actions`
 *   02-project-actions-open   the menu: Open project in editor + Open terminal
 *   03-editor-launch-refused  the editor failure line, separate from the reveal's
 *   04-directory-chip-menu    a transcript folder chip's new editor row
 *
 * Assertions, so a frame cannot silently photograph the wrong thing:
 *   - the header carries exactly two controls, in order (the row cap the two new
 *     actions were collapsed into one trigger to respect);
 *   - the open menu carries exactly the two new items, in order;
 *   - the refused arm renders the editor error notice and NOT the reveal one;
 *   - the directory chip's menu offers the FOLDER wording, never the file one;
 *   - the applied palette matches the filename's theme.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort    # in another shell (website/)
 *   node scripts/capture-files-home-project-actions.mjs http://127.0.0.1:6841 ../temp-screenshots/files-home-project-actions-1142
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/files-home-project-actions-1142'
mkdirSync(OUT, { recursive: true })

/** The panel's own dock width, so the capture IS the panel. */
const PANEL_VIEWPORT = { width: 460, height: 720 }
const CHIP_VIEWPORT = { width: 560, height: 420 }
const HEADER_BUTTONS = ['Show in file manager', 'Project actions']
const MENU_ITEMS = ['Open project in editor', 'Open terminal in the project directory']

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

const fail = (label, message) => { console.error(`[${label}] ${message}`); failures++ }

/** Assert the palette rather than trust the filename: an unset preference
 *  resolves to the HOST's mode, so a frame can otherwise claim a theme it does
 *  not carry. */
async function assertTheme(page, theme, label) {
  const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  if (applied !== (theme === 'light' ? 'kiro-light' : 'kiro-dark')) {
    fail(label, `data-theme=${applied} does not match the frame name`)
  }
}

for (const theme of ['dark', 'light']) {
  // ── The Files header and its new overflow ─────────────────────────────────
  const page = await browser.newPage({ viewport: PANEL_VIEWPORT })
  page.on('pageerror', e => fail(theme, `pageerror: ${e.message}`))
  await page.goto(`${BASE}/capture/files-home-project-actions.html?theme=${theme}`, { waitUntil: 'networkidle' })

  // Wait for the header's settled shape rather than a fixed sleep: Reveal
  // appears only once the branding read has answered `direct_local`.
  const trigger = page.getByRole('button', { name: 'Project actions' })
  await trigger.waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Show in file manager' }).waitFor({ timeout: 15000 })
  await assertTheme(page, theme, theme)

  // The header ROW is the parent of the tab's own `Files` label. Reached that way
  // rather than by a class chain, so this driver encodes none of the component's
  // internals — and rather than by an ancestor `hasText` filter, which matches
  // the whole panel and would count the rail's controls too.
  const header = page.getByText('Files', { exact: true }).locator('xpath=..')
  const names = await header.getByRole('button').evaluateAll(
    els => els.map(el => el.getAttribute('aria-label')),
  )
  if (JSON.stringify(names) !== JSON.stringify(HEADER_BUTTONS)) {
    fail(theme, `header buttons ${JSON.stringify(names)} != ${JSON.stringify(HEADER_BUTTONS)}`)
  }
  await page.screenshot({ path: `${OUT}/${theme}-01-header-closed.png` })

  await trigger.click()
  const items = page.getByRole('menuitem')
  await items.first().waitFor({ timeout: 15000 })
  const itemNames = await items.evaluateAll(els => els.map(el => el.textContent?.trim() || ''))
  if (JSON.stringify(itemNames) !== JSON.stringify(MENU_ITEMS)) {
    fail(theme, `menu items ${JSON.stringify(itemNames)} != ${JSON.stringify(MENU_ITEMS)}`)
  }
  await page.screenshot({ path: `${OUT}/${theme}-02-project-actions-open.png` })
  console.log(`[${theme}] header=${JSON.stringify(names)} menu=${JSON.stringify(itemNames)}`)
  await page.close()

  // ── A launch the shell declined reports on its OWN line ───────────────────
  const refusedPage = await browser.newPage({ viewport: PANEL_VIEWPORT })
  refusedPage.on('pageerror', e => fail(`${theme}/refused`, `pageerror: ${e.message}`))
  await refusedPage.goto(`${BASE}/capture/files-home-project-actions.html?theme=${theme}&refused=on`, { waitUntil: 'networkidle' })
  await refusedPage.getByRole('button', { name: 'Project actions' }).click()
  await refusedPage.getByRole('menuitem', { name: 'Open project in editor' }).click()
  await refusedPage.getByTestId('files-home-editor-error').waitFor({ timeout: 15000 })
  if (await refusedPage.getByTestId('files-home-reveal-error').count()) {
    fail(`${theme}/refused`, 'the gateway reveal error line rendered for an editor failure')
  }
  await assertTheme(refusedPage, theme, `${theme}/refused`)
  await refusedPage.screenshot({ path: `${OUT}/${theme}-03-editor-launch-refused.png` })
  console.log(`[${theme}/refused] editor error line rendered alone`)
  await refusedPage.close()

  // ── The transcript directory chip's right-click menu ──────────────────────
  const chipPage = await browser.newPage({ viewport: CHIP_VIEWPORT })
  chipPage.on('pageerror', e => fail(`${theme}/chip`, `pageerror: ${e.message}`))
  await chipPage.goto(`${BASE}/capture/files-home-project-actions.html?scene=chip&theme=${theme}`, { waitUntil: 'networkidle' })
  // The chip only becomes a folder once its own kind probe has answered, so wait
  // for the classification the menu depends on rather than for a timer.
  const chip = chipPage.locator('[data-path-kind="dir"]').first()
  await chip.waitFor({ timeout: 15000 })
  await chip.click({ button: 'right' })
  const chipItems = chipPage.getByRole('menuitem')
  await chipItems.first().waitFor({ timeout: 15000 })
  const chipNames = await chipItems.evaluateAll(els => els.map(el => el.textContent?.trim() || ''))
  if (!chipNames.includes('Open folder in editor')) {
    fail(`${theme}/chip`, `folder editor row missing: ${JSON.stringify(chipNames)}`)
  }
  // The file wording would over-promise for a folder (its handler may well be
  // the file manager), so the two rows are deliberately different strings.
  if (chipNames.includes('Open in editor')) {
    fail(`${theme}/chip`, 'the FILE editor wording rendered on a directory chip')
  }
  await assertTheme(chipPage, theme, `${theme}/chip`)
  await chipPage.screenshot({ path: `${OUT}/${theme}-04-directory-chip-menu.png` })
  console.log(`[${theme}/chip] menu=${JSON.stringify(chipNames)}`)
  await chipPage.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT}`)
