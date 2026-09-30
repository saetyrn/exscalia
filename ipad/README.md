# EXSCALIA for iPad

Native iPad shell for EXSCALIA. The root hosts the existing web app in a
`WKWebView` and adds **Paper**, a native Apple Pencil section (`ipad/Packages/PaperUI`,
built out from M2 onward). This directory is self-contained: nothing outside
`ipad/` belongs to it.

Milestone M1 ships the shell, the auth bridge and the JS-to-native bridge.
Milestone M2 ships Paper itself, offline: library, canvas, pages, templates,
the tool capsule with its own lasso, autosave with snapshots, and the save-state
pill. Nothing in M2 touches the network. Milestone M3 adds the network: the op
journal is drained to the server, other devices' work comes down, conflicts keep
both halves, and the trash and history are reachable. Milestone M5 reads the
handwriting on the device and makes it searchable; M6 finishes the round trip
to OMNIA and adds the native-to-web deep links.

## Layout

```
project.yml               xcodegen source of truth (EXSCALIA.xcodeproj is generated + gitignored)
Signing.example.xcconfig   copy to Signing.xcconfig (gitignored) for device builds
App/                       app target: shell, bridge, auth, settings
Packages/PaperCore/        models, API client, store (iOS + macOS, `swift test` on the Mac)
Packages/PaperUI/          SwiftUI views for Paper (iOS only)
Tests/                     app-target unit tests
UITests/                   XCUITest: input arbitration and page growth
RELEASE_GATE.md            simulator-safe diagnostics + physical iPad checklist
```

## Shortcuts, windows and scanning

The Shortcuts/Siri actions **New Paper page** and **Open last Paper notebook**
route directly to the native Paper surface. They use the shared app-owned
Paper store and sync engine, so they do not open the web route or create a
second sync writer. `WindowGroup` supports multiple iPad windows: each window
keeps its own web/Paper navigation and transient UI state while the Paper
controller, local storage and sync coordination remain shared.

To add camera pages, a notebook UI can present
`PaperScannerCoordinator.scan(from:)`. The returned `PaperScanResult.pdfData`
contains every VisionKit page in order; pass it to
`PaperController.importPDF(data:name:into:after:)`. Scanning is a standalone
UIKit/VisionKit service and does not mutate a notebook until the caller imports
the result.

Durable web links (`/paper/id/<notebook-id>/<page-id>?region=x,y,w,h`) are
carried over the bridge as `notebookID`, `page`, and `region`. The native
library resolves the stable notebook id locally, so a rename does not break a
shared link and the matching region can be revealed on the page.

## Build and run

```sh
brew install xcodegen           # once
cd ~/omnianote/ipad
xcodegen generate

# Package tests run on the Mac, no simulator needed.
cd Packages/PaperCore && swift test && cd ../..

xcodebuild -project EXSCALIA.xcodeproj -scheme EXSCALIA \
  -destination 'platform=iOS Simulator,name=iPad Air 13-inch (M3),OS=26.1' build

# EXSCALIATests are unit tests; EXSCALIAUITests drive real touches, which is
# the only way to prove which recogniser a drag on the page belongs to.
xcodebuild test -project EXSCALIA.xcodeproj -scheme EXSCALIA \
  -destination 'platform=iOS Simulator,name=iPad Air 13-inch (M3),OS=26.1'
```

Simulator builds need no signing identity. `Signing.xcconfig` may stay empty.

Useful launch arguments (Xcode scheme, or `xcrun simctl launch … --args`):

- `-surface paper` — start on the native Paper surface instead of the web view.
- `-server http://127.0.0.1:8600` — point at a dev server for this launch only.
- `-debugOpenNotebook` (DEBUG only) — open the first notebook instead of the
  library grid.
- `-debugStroke` (DEBUG only) — open the first notebook and put one real
  `PKStroke` on page 1. The simulator has no Apple Pencil and `simctl` cannot
  synthesise a drag, so this is the only scriptable way to exercise autosave,
  the render pipeline and the pill.
- `-debugTextObject` (DEBUG only) — put a text object and an image object on
  page 1, for the same reason.
- `-debugImportPDF [path]` (DEBUG only) — import a PDF and draw one stroke over
  its first page. With no path (or an unreadable one) it generates a three-page
  fixture at launch, so the import path is scriptable without a file on the
  device.
- `-debugExport [path]` (DEBUG only) — export the open notebook as a PDF a
  second and a half after launch, or after `-debugImportPDF` has finished.
  Falls back to `Documents/export.pdf` in the app container, and prints where
  it wrote.
- `-debugHandwriting [phrase]` (DEBUG only) — stamp page 1 with a large text
  object (default "quarterly review") and let the ordinary render-then-recognise
  pipeline run, so Vision has something to read without a Pencil.
- `-debugSearch <query>` (DEBUG only) — open the library with the search field
  already filled in, which is the only scriptable way onto the results screen.
- `-debugReveal <query>` (DEBUG only) — run that search, open the first hit at
  its page and hold the highlight open for a screenshot.
- `-debugNewNotebook [size]` (DEBUG only) — create and open a notebook the way
  the New notebook sheet does (Ruled + Infinite), or at a named preset (`a4`,
  `letter`, `iPad`, `infinite`). The UI tests start from this rather than from
  whatever an earlier run left in the container.
- `-debugPages <n>` (DEBUG only) — pad the open notebook out to `n` pages, so
  the scroll view has somewhere to scroll to.
- `-debugGrow` (DEBUG only) — write near the bottom-right of page 1, which is
  what an infinite page answers by getting bigger.
- `-debugLassoLoop` (DEBUG only) — draw one lasso loop round whatever ink is on
  page 1, through the same entry points the gesture recogniser uses. `simctl`
  cannot synthesise a drag and `XCUITest` can only draw a straight line, so
  this is the only scriptable way to see a selection.

Settings keys can be set for one launch the same way, because `UserDefaults`
reads the argument domain: `-paper.tool lasso`, `-paper.drawWithFinger YES`.

Debug builds also carry a small shell menu (top-right, "Shell menu") with
Open Paper / Server settings / Reload. It is compiled out of Release builds; the
shipping route into Paper is the web app's own Paper link.

## Objects, PDFs and export (M4)

- Text and images are `objects` in `page.json`, drawn *under* the ink by
  `ObjectsContainerView`: the pen always reaches the canvas above, and the
  objects layer only takes touches while one of them is selected.
- `TextObjectView` is a `UITextView` with scrolling off, so Scribble, selection
  and dictation are free and the box grows with the text. Font size is in page
  points and is scaled by the zoom.
- Image bytes and imported PDFs are stored once, under
  `assets/<stem>-<sha256[:16]>.<ext>`, exactly like the vault's attachments.
  Storing the same bytes twice writes one file; cropping changes a rectangle in
  the object, never the asset. Photos, pastes and drops go through
  `ImageImport` first, off the main actor: redrawn upright (the camera's
  orientation flag baked into the pixels), capped at 3000 px on the long edge,
  JPEG unless the source can be transparent (then PNG). An image the asset
  store refuses says so in an alert.
- A PDF import stores the document untouched and makes one page per PDF page
  bound to it by index (`pdf: {asset, pageIndex}`), with the media box in
  `size_points` beside the nearest preset in `size`. Annotations are ordinary
  ink in that page's `PKDrawing`, so the imported file keeps its hash forever.
  `PDFBackgroundLayer` rasterises the page once at 2x and caches it as the
  derived `pdf-2x.png` in the page directory.
- `PageRenderer.compose` draws template, PDF background, images, text and ink,
  and both `render.png` and the export call it, so an export is what the page
  looks like rather than a second interpretation of it. In the exported PDF the
  imported page and the text objects are vectors -- selectable text -- and only
  the ink is rasterised (at 3x); `PKDrawing` has no vector output.
- Export is `PDFExporter`: the notebook as a PDF, or a single-page notebook as a
  PNG, shared through `UIActivityViewController` (`+` -> Page -> Export, or
  Cmd-P). Rendering and PDF import run off the main actor (the flush before an
  export stays on it); Export is disabled while one runs, and imports queue.

## Handwriting recognition and search (M5)

- `HandwritingRecognizer` (PaperCore) runs `VNRecognizeTextRequest` over the
  page's `render.png` -- the picture the renderer has already made -- five
  seconds after the page stops changing. It is cancelled outright the moment
  the page turns dirty again, and it runs at background priority: recognition
  is never on the input path.
- Vision hands back normalised rectangles whose origin is the bottom-left. They
  are converted once, in `HandwritingRecognizer.pageBox`, into page points with
  y running down -- the same space as `PageObject.Frame`, the renderer and every
  view -- so nothing downstream has to remember which way up the page is.
- The result is written atomically as the derived `text.json`
  (`{lines: [{text, box, confidence}], transcript}`) beside the render, and
  upserted into `page_text` / `page_fts`. `transcript_ai` is never written from
  here: it belongs to the server, which merges its own copy back by revision.
- Recognition languages come from the settings screen
  (`paper.recognitionLanguages`), defaulting to `en-GB` and `en-US`.
- `page_fts` is FTS5 with the **trigram** tokeniser, so a fragment inside a
  misread word still hits. Queries under three characters fall back to a scan of
  the same column rather than answering "nothing here".
- `SearchView` is reached from the library's search field, from Cmd-F anywhere
  in Paper, and from `⋯ -> Search` inside a notebook. Results are grouped by
  notebook and carry the page number, the matching line, and a crop of
  `render.png` at the union of the matching boxes with twelve points of air
  round it. Notebook titles match too, and appear as a group of their own.
- Tapping a result opens the notebook at that page, scrolls the region to the
  middle of the viewport and outlines it in the accent colour for a second and a
  half. Under Reduce Motion the outline appears and vanishes rather than fading,
  and the scroll is not animated.
- The lasso's **Convert to text** reads the selected strokes
  (`PKDrawing(strokes:).image(from:scale:3)`), puts a text object where the ink
  was at roughly the size it was written, and removes the strokes. Both edits
  land in the same run-loop turn, so one Cmd-Z puts the handwriting back.
- A `text.json` on disk is the truth: `reconcile()` reads it back into the index
  at launch (skipping files it has already seen, by modification date), so a
  restored backup or a pulled page is searchable without re-running Vision.

## OMNIA and deep links (M6)

- Every page PUT already carried `render`; it now carries `text` as well. A
  transcript that finishes after the ink has gone up gets its own `pageText`
  op -- a **derived-only** PUT with no `page` and no `drawing` part, stamped
  `for_revision` -- which the server files without bumping the revision. It
  coalesces exactly like `pageRender`, and a queued `pagePut` supersedes it,
  because that push reads `text.json` off the disk as it goes.
- A pulled page whose `text_revision` is ahead of the local one fetches
  `text.json` and refreshes the index, so a page read by another iPad (or
  transcribed by OMNIA) is searchable here without a second Vision run.
- **Web to native**: the bridge message `{type:'open', section:'paper',
  notebook, page}` opens that notebook and page natively. The web route names a
  notebook without the vault's `paper/` root; `PaperTarget.deepLink` puts it
  back and rejects anything that is not a `.paper` package -- the message is
  page data, not a command.
- **Native to web**: `⋯ -> Open on web` in a notebook flushes, switches to the
  web surface and calls `window.__exscaliaNavigate('/paper/<nb>#<page>')`
  through `evaluateJavaScript`. The path is built by the shell from a notebook
  this device owns and is passed as a JSON string literal; a web view that has
  not loaded yet is sent to the URL instead.

## Sideloading onto an iPad (free Apple ID)

1. Xcode → Settings → Accounts → add your Apple ID. Select the resulting
   "Personal Team" row and copy its team id into `ipad/Signing.xcconfig`
   (`EXSCALIA_DEVELOPMENT_TEAM = ABCDE12345`). Start from
   `Signing.example.xcconfig`.
2. On the iPad: connect over USB-C once and trust the Mac; then Settings →
   Privacy & Security → Developer Mode on. After the first install, trust the
   developer certificate under Settings → General → VPN & Device Management.
3. Build and install:

   ```sh
   xcodebuild -project EXSCALIA.xcodeproj -scheme EXSCALIA \
     -destination 'platform=iOS,name=<iPad name>' -allowProvisioningUpdates build
   xcrun devicectl device install app --device <udid> <path>/EXSCALIA.app
   xcrun devicectl device process launch --device <udid> art.exscalia.ipad
   ```

   Wi-Fi installs work after the first wired pairing.
4. Free-account provisioning profiles expire after **7 days**; rebuild and
   reinstall to renew. Reinstalling keeps app data as long as the bundle id and
   team are unchanged.

The bundle id `art.exscalia.ipad` must never change: free accounts get only ten
app ids per seven days.

## Authentication

There is no native sign-in screen and no native credential entry. You sign in
inside the web view; the shell then reads the `__Host-omnia_session` cookie out
of `WKHTTPCookieStore` and replays it as `Authorization: Bearer <token>`, which
`token_from_request` in `backend/app/api/auth.py` accepts. `GET /api/auth/status`
is the check the shell runs after each page load.

The native `URLSession` is ephemeral with `httpShouldSetCookies = false`, so API
traffic can never alter the web view's login state. A native 401 or 403 drops the
cached token and moves the save-state pill to "Sign in to sync".

**Dev server over http:** `__Host-` cookies only exist over https, so a plaintext
LAN server has no session cookie to read. Run such a server with authentication
off (no `OMNIA_AUTH_PASSWORD`). ATS here allows local networking only; every
other host must be https.

## Not in the shell

- **No web push and no APNs.** Web push does not work in `WKWebView` (the web
  app's `push.ts` already reports it as unsupported), and a free Apple ID cannot
  provision APNs. Notifications are a web-browser-only feature of EXSCALIA.
- **No iCloud**, for the same free-account reason. Paper syncs through the OMNIA
  server, not through iCloud.

## Conventions

Swift 6 with strict concurrency, no force unwraps in app code, an
`accessibilityLabel` on every control, monochrome SF Symbols, no emoji anywhere.

## Where Paper keeps its files (M2)

Everything lives under the app container, keyed by the server so two servers can
never mix notebooks:

```
Application Support/Paper/<sha1(origin)>/
  vault/paper/<Folder>/…/<Title>.paper/     # mirrors the vault layout exactly
    notebook.json
    pages/<pageId>/{page.json, drawing.pkdrawing, render.png, text.json}
  paper.sqlite                              # index, op journal, snapshot ring,
                                              # handwriting index (page_text/page_fts)
  snapshots/<pageId>/<timestamp>.pkdrawing  # 10 per page, 200 MB store cap
  .trash/<token>/                           # deletes, undone by moving back
```

The tree is the source of truth; `paper.sqlite` is an index over it that
`Reconciler` rebuilds at every launch. `drawing.pkdrawing` is the only file
whose loss is unrecoverable, so it is written first, atomically (tmp + `fsync` +
`replaceItemAt`), and snapshotted on every flush.

Autosave flushes one second after the last stroke, when a page leaves the live
window, and on resign-active/background inside `beginBackgroundTask`. It never
touches the network: the op journal fills up and nothing drains it until M3.

## Paper UI notes (M2)

- The capsule is `Pen | Highlighter | Eraser | Lasso | Undo | Redo | +`, in that
  order, always. It can be dragged to an edge (remembered) but never reorders,
  and undo/redo are dimmed rather than hidden. `+` carries the M4 insert entries
  disabled, a Page submenu and `⋯`.
- The lasso is ours: PencilKit exposes no selection API, so `LassoOverlayView`
  captures the loop, selects strokes that are at least half inside it, and
  registers every operation on the canvas's own `undoManager`.
- **Input arbitration.** The loop is captured by `LassoGestureRecognizer`: one
  recogniser on the page stack that the scroll view's `panGestureRecognizer`
  must `require(toFail:)`. Raw `touchesBegan` on the overlay was not enough --
  a `UIScrollView` cancels the touches it has delivered to its content the
  moment its own pan recognises, and on hardware an Apple Pencil drives that
  pan like any other touch, so a Pencil loop only ever scrolled the page.
  `PageScrollView`'s pan now takes `.direct` touches only: **Pencil is
  creation, fingers are navigation**, for every tool and not only the lasso.
  The loop recogniser takes the Pencil, and a finger as well when "Draw with
  finger" is on. A second finger fails it, so two-finger scrolling and
  pinch-to-zoom keep working with the Lasso selected.
- Tool widths are a continuous `UISlider` rather than three buttons -- pen
  0.5-12pt, highlighter 6-40pt, eraser 4-60pt -- with a stroke drawn at the
  width and colour about to be used, the number in points beside it, and the
  value persisted per tool in `ToolState`.
- A page is an accessibility *container*: one element for the canvas and, while
  there is a selection, the `SelectionInspector` beside it. As a plain element
  the page hid the inspector's own buttons from VoiceOver.
- **Notebook switcher.** The `books.vertical` glyph in the notebook toolbar
  (Cmd-O, and Control-Tab / Control-Shift-Tab to cycle Recents) opens a popover
  of Recents and every notebook, with a search field and covers. Choosing one
  flushes the ink and the reading position of the notebook being left, then
  puts a new `NotebookSession` in the same screen: nothing is popped, the
  toolbar and the tool selection are the same objects afterwards, and the new
  notebook opens at its own last position. The back button is unchanged.
- Zoom resizes frames and sets each canvas's `zoomScale`; the scroll view never
  applies a transform, and a pinch arriving mid-stroke is ignored.
- Every `PKCanvasView` and every render is forced to a light appearance.
  Without it PencilKit renders black ink white under a dark system appearance.
- Only the visible page and one either side get a real canvas (three at most);
  the rest show `render.png`.
- "Use system tool picker" swaps in `PKToolPicker` and collapses the capsule to
  `Undo | Redo | +`.

## Infinite pages

`PaperSize.infinite` (`"infinite"`, label "Infinite") is the default for new
notebooks: the New notebook sheet preselects Ruled + Infinite, and so does the
first-launch notebook. An infinite page starts at 1024x1366 and grows.

- The rule is `InfiniteCanvas` in PaperCore -- pure geometry, covered by
  `swift test`, so the screen, the renderer and the exporter cannot disagree
  about it. When the ink or the object frames come within 160pt of the bottom
  or the right, the page is extended by half a screen in that direction. When
  they come within 160pt of the top or the left, the page is extended *and* the
  drawing, the objects and the scroll offset are all moved by the same amount,
  so nothing appears to shift under the pen. Writing in a ruled page's left
  margin therefore extends the page leftwards once, and then stops. The
  template is drawn from the page's origin, so a top or left growth is rounded
  up to a whole number of rule, grid or dot spacings
  (`TemplateGeometry.growthQuantum`) and the ink stays on the line it was
  written on.
- The current extent lives in `page.json`'s `size_points`, which already rides
  the page PUT; `size` stays `infinite`. On the wire `size` is the extent in
  points (what the web viewer reads), so the preset travels beside it as
  `size_name`, and a page pulled with nothing local to go by -- a second
  device, a reinstall, the original of a conflict -- still comes back
  infinite. A page the server has no name for is infinite when its notebook
  makes infinite pages, it is at least the starting extent and it is not a PDF
  page; otherwise its dimensions pick the preset.
- The template rules the whole extent, and the page column scrolls sideways as
  well as down once a page is wider than the screen.
- Renders, covers and exports use the extent, with the scale capped so that no
  render is longer than `PageRenderer.maximumPixelEdge` (8192px) on its long
  edge; beyond that the picture is drawn at fewer pixels per point rather than
  refused.
- Zoom and "restore last position" are unchanged: the reading position is still
  the page id plus an offset, and a page that has grown restores to the same
  place on it.

## Sync (M3)

`SyncEngine` is an actor in `PaperCore`. It never sits on the input path:
autosave has already written the ink and appended an op before the engine hears
anything.

- **Push** drains `OpJournal` head-first. An op either succeeds, becomes a
  conflict, or is retried with a jittered backoff of 1, 2, 4 … 60 seconds. A 4xx
  that is not a 409 *parks* the op -- kept, not retried, named in the pill --
  because a request the server will never accept must not block the queue behind
  it and deleting it would be deleting the user's work.
- **Conflict**: the server keeps the original untouched and parks the incoming
  version as `<pageId>.conflict-<stamp>` right after it. The device adopts that
  sibling id for its own page directory and pulls the original back down, so
  both halves exist on both sides. The pill names the page and tapping it jumps
  there.
- **Pull** reads `/api/paper/changes?since=&epoch=`. Pages with unpushed local
  ops are skipped entirely, and a page whose server revision is not *ahead* of
  the local one is not fetched -- which is also how the device ignores the echo
  of its own save. `full: true` drops the cursor and reconciles from
  `GET /api/paper` plus one manifest per notebook; that path is additive, since
  a notebook the server has not seen yet is one that is still queued.
  Deletions arrive as tombstones and are mirrored into the device `.trash/`.
- **Live**: `/api/live` is read with `URLSession.bytes`; a `paper` event whose
  `origin` is not this install's `client_id` triggers a pull. Otherwise the
  engine pulls on foreground, after each push batch and every five minutes.
- **Renders** ride the page PUT as a part with `for_revision`. A render that
  finishes after the ink has gone up gets its own derived-only push, which the
  server accepts without bumping the revision.
- `client_id` is per install (`paper.clientID` in `UserDefaults`), because it
  identifies the iPad rather than the account.

`swift test` covers the whole of it against `MockPaperServer`, a `URLProtocol`
server that keeps notebooks and revisions in memory and applies the same
conflict rule the real store does.

**Trash** is the sidebar entry in the library and the `⋯` menu in a notebook:
the server's thirty-day trash, with Restore. The six-second undo toast is still
the fast path, and it cancels the delete before it is ever pushed.

**History** is the `⋯` menu: this iPad's snapshot ring (ten per page, with a
preview drawn from the `.pkdrawing`) above the server's git versions. Restoring
either is a new version, never a rewrite. Server versions list their commit
rather than a preview image -- the API exposes an old page's bytes only through
a restore, so there is nothing honest to draw yet.
