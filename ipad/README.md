# EXSCALIA for iPad

The iPad app is the EXSCALIA web app in a `WKWebView`, plus **Paper**, a native
section for writing with the Apple Pencil. Paper keeps documents in a folder
library. A document is a **notebook** (pages, of a fixed size or, in older
notebooks, infinite) or a **whiteboard** (boards, one large canvas each).
Paper works offline; a sync engine sends the work to the OMNIA server, which
keeps it in the vault under `paper/`, and the web and desktop apps show it in
a read-only viewer. Everything in `ipad/` belongs to the app and nothing
outside it does.

## Layout

```
project.yml                xcodegen source of truth (EXSCALIA.xcodeproj is generated and gitignored)
Signing.example.xcconfig   copy to Signing.xcconfig (gitignored) for device builds
App/                       app target: RootView, the web shell and bridge, auth, settings, App Intents, the scanner, the release-gate report
Packages/PaperCore/        pure logic: models, store, op journal, sync, renderer, recognition, geometry, tools (iOS and macOS; `swift test` on the Mac)
Packages/PaperUI/          Paper's views and controllers (iOS only)
Tests/                     app-target unit tests (EXSCALIATests)
UITests/                   XCUITest with real touches (EXSCALIAUITests)
RELEASE_GATE.md            the simulator-safe report and the physical iPad checklist
```

## How it fits together

- **The shell.** `App/RootView` gives each window two surfaces: the web view,
  kept alive under Paper so coming back does not reload it, and Paper.
  `AppModel` owns one `PaperController` for the whole app; each window keeps
  its own surface and navigation ("The shell").
- **`PaperController`** (PaperUI) is the app's one Paper model: the
  `PaperStore`, the `SyncEngine`, the save state, the library's lists, and the
  one `ToolState`, so the pen is the same in every document and window.
- **`PaperWindowView`** is Paper in one window: the library at Home,
  otherwise the editor for the active tab ("Editor chrome").
- **PaperCore** holds everything that can be tested without a screen, and
  PaperUI wraps it. New logic goes in PaperCore where it can.
- **Storage** is a tree of files that mirrors the vault, with SQLite as an
  index over it. Autosave writes the tree and appends to the op journal; the
  sync engine drains the journal.

| Area | Files (PaperUI unless marked) |
|---|---|
| Window and tabs | `PaperWindowView`, `DocumentTabsModel`, `DocumentTabBar` |
| Library | `LibraryView`, `LibraryBrowserView`, `LibraryRow`, `LibraryItemMenu`, `MovePickerSheet`, `ItemInfoSheet`, `FolderExporter`, `SearchView`, `TrashView`, `PaperController+Library`; PaperCore `LibraryModel` |
| New document sheets | `NewDocumentSheets`, `FolderAppearancePicker`; PaperCore `NewDocumentOptions` |
| Editor chrome | `EditorScreen`, `EditorNavigationBar`, `EditorToolbar`, `EditorMenus`, `EditorSidebar`, `ToolMenuView`, `ColorSlotEditor`, `UndoRedoPill`, `SaveStatePill`, `Tokens` |
| Notebook canvas | `NotebookViewController` and its extensions, `PageScrollViews`, `PageView`, `EditorGestures`, `NotebookSession` |
| Selection and objects | `LassoOverlayView`, `ObjectsContainerView`, `ObjectView`, `TextObjectView`, `ImageObjectView`, `ShapeObjectView`, `ObjectMenu` |
| Whiteboard | `BoardViewController`; PaperCore `BoardGeometry`, `PaperStore+Board`, `WhiteboardConversion` |
| Tools | `ToolState`; PaperCore `ToolModel`, `ShapeRecognizer`, `HitTesting` |
| Model and store | PaperCore `Models`, `Folders`, `PaperAppearance`, `PaperStore`, `PaperStore+Folders`, `PaperStore+Pages`, `PaperDatabase`, `AutosaveCoordinator`, `SnapshotRing`, `AssetStore` |
| Sync | PaperCore `SyncEngine`, `SyncEngine+Folders`, `OpJournal`, `PaperAPI`, `PaperWire`, `SyncStoreWriter`, `LiveEvents` |
| Render, export, recognition | PaperCore `PageRenderer`, `TemplateGeometry`, `PDFExporter`, `PDFImporter`, `Recognition`, `TextIndex`, `InfiniteCanvas` |

## Build and test

```sh
brew install xcodegen           # once
cd ~/omnianote/ipad
xcodegen generate               # after a clone or an edit to project.yml

# PaperCore runs on the Mac, no simulator needed.
cd Packages/PaperCore && swift test && cd ../..

xcodebuild -project EXSCALIA.xcodeproj -scheme EXSCALIA \
  -destination 'platform=iOS Simulator,name=iPad Air 13-inch (M3),OS=26.1' build

# Unit tests and UI tests. UI tests drive real touches, the only way to prove
# which recogniser a drag on the page belongs to. Add
# -only-testing:EXSCALIAUITests/LibraryUITests (for example) to run one suite.
xcodebuild test -project EXSCALIA.xcodeproj -scheme EXSCALIA \
  -destination 'platform=iOS Simulator,name=iPad Air 13-inch (M3),OS=26.1'
```

Simulator builds need no signing identity, and `Signing.xcconfig` may stay
empty.

- **PaperCore** (`swift test`): models, store, journal, reconciler, sync
  against `MockPaperServer`, renderer pixels, board geometry, tools, shape
  recognition, hit testing, Convert to Whiteboard (`ConvertTests`,
  `ConvertSyncTests`), and the wire fixtures.
- **`Tests/`**: what needs UIKit. `PageViewObjectSelectionTests`,
  `LassoDragTests`, `EditorChromeTests`, `LibraryControllerTests`,
  `NewDocumentSheetsTests`, `ToolStateTests`, `BoardViewControllerTests`, the
  notebook controller's lifecycle and rebuild, the shell and the bridge.
- **`UITests/`**: `LassoInputUITests` and `SelectionUITests` (a finger under
  `-debugFingerInk` stands in for the Pencil), `ToolbarUITests`,
  `EditorChromeUITests`, `LibraryUITests`, `NewSheetsUITests`,
  `InfiniteCanvasUITests`, `BoardUITests`, `ConvertUITests`.
- **The wire is shared with the backend** through fixtures
  (`PaperCoreTests/Fixtures/*.json`), written by
  `backend/tests/test_paper_wire_fixtures.py`. After a wire change, run that
  test with `UPDATE_FIXTURES=1`, then `swift test` ("Sync").
- The simulator has no Pencil, `simctl` cannot synthesise a drag, and
  XCUITest draws only straight lines. The `-debug*` arguments below are how a
  script reaches those paths.
- **Keys are unit tests.** The simulator passes hardware keys to SwiftUI's
  keyboard shortcuts only now and then, so the library's keys
  (`LibraryState.searchFromKeyboard`, `activateKeyboardItem`, `escape`) and
  the tab keys (`DocumentTabsModel`) are tested in `LibraryStateTests` and
  `DocumentTabsModelTests`, the editors' `UIKeyCommand`s through
  `keyCommands` and `perform`, and the keys themselves on the iPad.
- **Scratch defaults.** A test that needs its own `UserDefaults` takes it
  from `scratchDefaults(_:)` or `ScratchDefaults` (`Tests/ScratchDefaults.swift`,
  and the same file in `PaperCoreTests`). The suite is named by a path in a
  temporary directory that goes when the test ends: a suite emptied with
  `removePersistentDomain(forName:)` alone is still written to
  `Library/Preferences/<name>.plist` when the test process exits.

## Launch arguments

Pass them in the Xcode scheme or after the bundle id:
`xcrun simctl launch <udid> art.exscalia.ipad -surface paper -debugStroke`.
Every `-debug*` argument works in DEBUG builds only.

| Argument | What it does |
|---|---|
| **Shell** | |
| `-surface paper` | Start on Paper instead of the web view. |
| `-server <url>` | Use this server for this launch only, such as `http://127.0.0.1:8600`. |
| `-debugReleaseGate` | Print the release-gate report (`RELEASE_GATE.md`). |
| **Documents and tabs** | |
| `-debugOpenNotebook` | Open the first notebook. |
| `-debugNewNotebook [size]` | Create and open a Ruled notebook, Infinite unless a preset (`a4`, `letter`, `iPad`, `infinite`) is named. It stays Infinite because `InfiniteCanvasUITests` start here. |
| `-debugTabs <n>` | Open the first n documents in tabs, making "Tab N" notebooks up to n, with the first showing. |
| `-debugReadOnly` | The open tab in Read Only. |
| `-debugSidebar [pages\|outline\|boards]` | The editor's sidebar open, on that tab. |
| `-debugNewWindow` | A second after the document opens, Open in New Window on it, through the tab menu's `NSUserActivity`. |
| `-debugPages <n>` | Pad the open notebook to n pages, so the scroll view has somewhere to go. |
| `-debugPaperColor <name>` | Colour every page of the open notebook (`white`, `cream`, `dark` or any other paper colour), for the dark rule. |
| `-debugConvert [seconds]` | Convert the document in the active tab to a whiteboard, without asking, after that many seconds (2 by default), through the path Convert takes. Prints what it converted and what it is now. Once per launch. |
| `-debugNewWhiteboard [grid\|dotted\|blank] [cream\|white\|dark\|...]` | Create and open a whiteboard, Square Grid on Cream unless named. |
| `-debugBoardStrokes [n]` | On the board: a block of handwriting round the centre, its first stroke level through it, and three clusters thousands of points away, for Fit Content and the minimap; with n, n more short strokes, for the limits. |
| `-debugBoardZoom <percent>` | The board's centre at that zoom, for screenshots. |
| `-debugBoardSaveBench [n,n,...]` | Times a save of a board of n strokes (15,000 and 25,000 by default), with the ink encoded ahead and on the main thread, and prints it. |
| **Ink, objects and pages** | |
| `-debugStroke` | Open the first notebook and put one real `PKStroke` on page 1: the scriptable way into autosave, the render pipeline and the save state. |
| `-debugTextObject` | A text object and an image object on page 1. |
| `-debugGrow` | Write near the bottom right of page 1, which an infinite page answers by growing. |
| `-debugImportPDF [path]` | Import a PDF and draw one stroke over its first page. Without a readable path it generates a three-page PDF. |
| `-debugExport [path]` | Export the open notebook as a PDF a second and a half after launch, or after `-debugImportPDF`. Falls back to `Documents/export.pdf` in the container and prints where it wrote. |
| `-debugHandwriting [phrase]` | Stamp page 1 with a large text object (default "quarterly review") and let render and recognition run, so Vision has something to read. |
| `-debugSearch <query>` | The library's search with the query typed. |
| `-debugReveal <query>` | Run that search, open the first hit at its page and hold the outline open for a screenshot. |
| **Tools and selection** | |
| `-debugFingerInk` | A finger inks and lassos like the Pencil, and panning takes two fingers. For XCUITest, which has no Pencil; it is not a setting. |
| `-debugLassoLoop` | One lasso loop round the ink on page 1, through the entry points the recogniser uses. |
| `-debugLassoDrag [cancel\|hold]` | The same loop, then a drag that puts the ink's top left at (200, 450). `cancel` picks it up again and ends with a cancel, which must leave it at (200, 450); `hold` never lets go, for a screenshot of the ink in the air. |
| `-debugDrawShape [rectangle\|ellipse\|triangle\|line\|arrow]` | Select Draw Shape, put one rough stroke of that shape on page 1 and send it through the clean-up a Pencil lift runs. Prints what it was recognised as. |
| `-debugToolDock top\|bottom\|leading\|trailing` | The tool menu docked on that side and shown. |
| `-debugSlotEditor width\|color` | The selected width or colour slot's editor open. |
| `-debugTapSelect [ink\|text\|image\|shape]` | Tap the first thing of that kind on page 1 through a finger tap's path, so the selection and its object menu show: the `-debugStroke` ink by default, the `-debugTextObject` text or image, or the `-debugShape` shape. |
| `-debugShape [rectangle\|ellipse\|triangle\|line\|arrow]` | The Shapes tool places one shape low on page 1 with a tap, then falls back to the lasso with the shape selected. |
| **Library and sheets** | |
| `-debugFolders` | Once per store, the folders of the owner's screenshot: Management (orange), Accounting (red) with twelve whiteboards, Econ (blue), and a Homework6 whiteboard at the top level; three of them opened, for Recents. |
| `-debugLibraryFolder <name>` | That top-level folder open. |
| `-debugLibrary sidebar\|new\|select\|favorites\|recents\|trash\|keyboard` | The library in that state. |
| `-debugLibraryMenu <name>`, `-debugLibraryMove <name>`, `-debugLibraryInfo <name>` | That item's menu, move picker or Info. |
| `-debugLibraryStar <name>` | Star that item first. |
| `-debugNewSheet notebook\|whiteboard\|folder` | That sheet at launch. It prints what the sheet hands back and shows it in a result line, which `NewSheetsUITests` reads. `-debugNewSheet folder red emoji:📒` edits a folder "Accounting" with that colour and icon. |
| `-debugResignFocus` | Drop the keyboard two seconds in, so a screenshot shows the whole sheet. |

- **Documents.** `PaperWindowView` runs the arguments that open a document
  (`-debugNewNotebook`, `-debugNewWhiteboard`, `-debugOpenNotebook`,
  `-debugStroke`, `-debugHandwriting`, `-debugReveal`, `-debugTabs`). A window launched with
  one restores no tabs, so a test starts from what it asked for. A second
  window never runs them again.
- **Settings keys** can be set for one launch, because `UserDefaults` reads
  the argument domain: `-paper.tool lasso` (any `PaperTool` raw value, such as
  `highlighter`, `drawShape` or `eraser`), `-paper.library.layout grid`, and
  `-paper.newNotebook.options '"{\"cover\":true,\"orientation\":\"landscape\"}"'`
  (likewise `-paper.newWhiteboard.options`). An options value must be a quoted
  property-list string: a bare `{...}` is read as a dictionary and ignored.
- `LibraryUITests` and `ConvertUITests` add `-server http://127.0.0.1:8799`,
  where nothing listens: a store of their own, offline.
- **The shell menu.** Debug builds put a "Shell menu" button at the top right
  of the web surface, with Open Paper, Server settings and Reload web app. It
  is compiled out of Release builds, where the way into Paper is the web
  app's Paper link.

## Invariants

Every change keeps these. The sections named say why, and `HANDOFF.md` §7
has the history.

- **The Pencil draws; a finger navigates and selects.** `drawingPolicy` is
  `.pencilOnly`. The scroll view's pan takes `.direct` touches only and
  `require(toFail:)`s the lasso recogniser, which takes the Pencil; a second
  finger fails the lasso. Only `-debugFingerInk` changes this ("Input and the
  lasso").
- **No transform zoom.** A notebook zooms by resizing frames and setting each
  canvas's `zoomScale`. A pinch that begins mid-stroke is ignored.
- **Guard `canvas.drawing =`** with `isApplyingDrawing`; otherwise every load
  marks the page dirty and takes a snapshot.
- **At most three live canvases**: the visible page and one either side; the
  rest show `render.png`. Flush before a canvas is recycled, on disappear, and
  on a tab switch.
- **The dark rule.** A page whose paper colour is dark draws and renders with
  a dark appearance, every other page with a light one, and
  `PaperAppearance.isDark(page:)` is the only test. Stroke colours are stored
  as their light values ("Documents, covers and paper colours").
- **The tree is the truth.** `drawing.pkdrawing` is written first, atomically,
  and snapshotted on every flush. `paper.sqlite` is an index the reconciler
  rebuilds at launch.
- **Autosave never touches the network.** It writes files and appends ops;
  only `SyncEngine` talks to the server, and never on the input path.
- **Chrome never touches PencilKit.** The bars, menus and pills act through
  `session.commands` (`EditorCommands`) and read `session.chrome`.
- **Tools change only through `ToolState.update`**, which saves through
  `ToolSettingsStore`.
- **One editor per window.** Only the active tab has a session and a canvas.
- **A document's thumbnail is the render of `pages[0]`**, on the device and on
  the server.
- **Canvas space is page space**, on boards too. A board's centre never moves
  and its extent never changes.
- **Wire changes are additive, and the server ships first.** It answers 400 to
  a template, size, colour or kind it does not know.
- **Code and design.** Swift 6 with strict concurrency, no force unwraps in
  app code, an `accessibilityLabel` on every control, monochrome SF Symbols.
  Colour only where it is content (folder colours, covers, ink) and in the
  bar tokens of `Tokens.swift`. No helper copy. No emoji, except a folder's
  icon.

## Hardware notes

The simulator cannot show the Pencil: pressure, tilt, hover, double-tap,
squeeze, palm rejection, or the arbitration between the Pencil and the
scroll view (on hardware a Pencil drives a scroll view's pan like a finger,
which the simulator never shows). It cannot cancel a touch either. Run
`RELEASE_GATE.md` on the iPad before shipping; a simulator pass is not a
sign-off.

**Sideloading with a free Apple ID.**

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

## The shell

- **Windows.** `WindowGroup` allows several iPad windows. Each keeps its own
  surface, web or Paper navigation and transient UI, while the Paper
  controller, the local store and sync stay shared. A second window needs
  `UIApplicationSupportsMultipleScenes` inside `UIApplicationSceneManifest`
  (`project.yml`); at the top level of Info.plist UIKit ignores it.
- **Into and out of Paper.** The web app's Paper link opens the native
  section ("Web links" below). The way back is the globe in the library's
  top-right pill, "Back to EXSCALIA"; the editor has none, so a person goes
  Home first. Share → Open on Web in a document also switches to the web
  surface.
- **Shortcuts.** The App Intents **New Paper page** (a page after the last
  page of the most recently opened notebook, else the first notebook, else a
  new one) and **Open last Paper notebook** (at its last page) go straight to
  Paper. They use the shared store and sync engine, so they open no web route
  and start no second sync writer.
- **Scanning.** `PaperScannerCoordinator.scan(from:)` (App) returns a
  `PaperScanResult` whose `pdfData` holds every VisionKit page in order;
  `PaperController.importPDF(data:name:into:after:)` imports it. The scanner
  changes no notebook until the caller imports the result. `RootView` hands
  it to Paper as `scanDocument`, which the library's Scan Documents and the
  editor's Add → Scan Document call.
- **Settings** (`App/Settings/ServerSettingsView`): the server address,
  the handwriting recognition languages, and Reset local Paper cache. It opens
  from Settings at the foot of the library's sidebar (`RootView` hands the
  library `\.paperOpenSettings`), from Paper's placeholder (no server or no
  store), from the screen shown when the web app cannot load, and from the
  DEBUG shell menu. The editor has no entry for it.

### Web links

- **Web to native.** The bridge message `{type:'open', section:'paper', …}`
  carries a `notebook` (and `page`), a durable link's `notebook_id`, `page`
  and `region`, or a `folder`. The web route names a notebook without the
  vault's `paper/` root; `PaperTarget.deepLink` puts it back and rejects
  anything that is not a `.paper` package, because the message is page data,
  not a command. A durable link (`/paper/id/<notebook-id>/<page-id>?region=x,y,w,h`)
  resolves by the stable id, so a rename does not break it, and the region is
  revealed on the page. A folder (`/paper?folder=Work` or `paper/Work`)
  becomes `PaperDeepLink.folder`, and the window opens the library there.
- **Native to web.** Share → Open on Web flushes, switches to the web surface
  and calls `window.__exscaliaNavigate('/paper/<nb>#<page>')` through
  `evaluateJavaScript`. The shell builds the path from a notebook this device
  owns and passes it as a JSON string literal; a web view that has not loaded
  yet is sent to the URL instead.

### Authentication

There is no native sign-in screen and no native credential entry. You sign in
inside the web view; the shell then reads the `__Host-omnia_session` cookie out
of `WKHTTPCookieStore` and replays it as `Authorization: Bearer <token>`, which
`token_from_request` in `backend/app/api/auth.py` accepts. `GET /api/auth/status`
is the check the shell runs after each page load.

The native `URLSession` is ephemeral with `httpShouldSetCookies = false`, so API
traffic can never alter the web view's login state. A native 401 or 403 drops
the cached token and puts the save state at "Sign in to sync".

**Dev server over http:** `__Host-` cookies only exist over https, so a plaintext
LAN server has no session cookie to read. Run such a server with authentication
off (no `OMNIA_AUTH_PASSWORD`). ATS here allows local networking only; every
other host must be https.

### Not in the shell

- **No web push and no APNs.** Web push does not work in `WKWebView` (the web
  app's `push.ts` already reports it as unsupported), and a free Apple ID cannot
  provision APNs. Notifications are a web-browser-only feature of EXSCALIA.
- **No iCloud**, for the same free-account reason. Paper syncs through the OMNIA
  server, not through iCloud.

## Where Paper keeps its files

Everything lives under the app container, keyed by the server so two servers can
never mix notebooks:

```
Application Support/Paper/<sha1(origin)>/
  vault/paper/<Folder>/…/<Title>.paper/     # mirrors the vault layout exactly
    notebook.json
    pages/<pageId>/{page.json, drawing.pkdrawing, render.png, text.json}
  vault/paper/<Folder>/folder.json          # a folder's record ("Sync", Folders)
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
window, on a tab switch, and on resign-active or background inside
`beginBackgroundTask`. It writes the files and appends an op to the journal;
the sync engine sends it later.

## Documents, covers and paper colours

The model is PaperCore and runs under `swift test`.

- **Kinds.** A document is a notebook or a whiteboard, `kind` in
  `notebook.json`, and an older `notebook.json` reads as a notebook. A
  whiteboard is a notebook record with `paperSize: board`, and each of its
  pages is a board: `size: board`, its extent in `board_extent` and never
  `size_points`, titled "Board N" until renamed. The extent is 1,024 to 32,768
  points a side, and 16,384 square when missing.
- **Creating.** `PaperStore.createDocument(title:folderPath:kind:options:)`
  makes both kinds. `folderPath` keeps the root, so `paper` is the top level.
  With Cover on, page 1 is a cover page (template `cover`, filled with the
  cover colour) and the first paper page follows it. A page that is coloured,
  a board or a cover goes up as a whole-page PUT, because a page create
  carries only the template and the size.
- **Defaults.** New notebooks are Ruled, White, iPad, portrait, no cover.
  Whiteboards are Square Grid on Cream. `NewDocumentOptions` keeps the last
  choices per kind as JSON in `paper.newNotebook.options` and
  `paper.newWhiteboard.options`. A stored value the sheet no longer offers,
  such as Infinite, falls back to the factory value for that field only.
- **The cover** is `pages[0]`, on the device as on the server, and
  `coverPage(ofNotebook:)` returns it. There is no cover picker. Deleting page
  1 removes the cover. The old `cover` field is kept as it is read, and
  nothing in the app sets it.
- **Orientation and paper colour** live on the notebook and apply to the pages
  it makes next. Landscape is the preset with its sides swapped into
  `size_points`, so the web viewer and older builds show it correctly; a page
  is landscape when it is wider than tall. `setPageStyle` changes one page's
  template or colour and journals a page PUT.
- **Paper colours.** `PaperColor` has eleven names: white, cream and dark for
  paper, and eight cover colours. `isDark` means relative luminance under
  0.4, which puts sage and ochre on the dark side and sand on the light one.
  Rules and dots are black at 12 % on light paper and white at 14 % on dark.
  A cover is its colour with one frame, 24 points in and 1 point wide.
- **The dark rule.** `PaperAppearance.isDark(page:)` is the one test. A dark
  page's `PKCanvasView` takes a dark `overrideUserInterfaceStyle`, and so do
  the ink a drag carries, its text and its shapes; every other page is forced
  light. Renders, covers, exports and board recognition follow the same rule
  (`.darkAqua` on the Mac). Stroke colours are stored as their light values
  and PencilKit maps them, so black ink shows light on dark paper and dark
  again if the paper turns light; without the rule PencilKit would turn black
  ink white on white paper under a dark system appearance. Text with no
  `color` is `#1C1C1E` on light paper and `#F2F2F7` on dark. On dark paper,
  shape strokes and fills, text colours and the tool menu's swatches go
  through PencilKit's light-to-dark mapping (`PageRenderer.displayColor`).
- **Folders.** `PaperFolder` is the `folder.json` record. `Folders.swift` also
  holds the legacy ids, the icon rules and `folderSlug`. The store and sync
  are under "Sync".
- **Library rules.** `LibraryArrangement` sorts by date, name or type, and
  filters to all, folders, notebooks or whiteboards. It also builds Favorites
  and Recents (the last 20 documents opened on this device, from
  `PaperStore.openedDates()`). Every sort ends on the item id, so equal items
  never swap places between refreshes.
- **Schema.** Migration `v4-documents` adds `kind`, `orientation` and
  `paper_color` to the `notebooks` table. The store reads notebooks from that
  table, so the last two have to be there for new pages to follow them.

## Library

The library is a folder browser laid out on the owner's screenshots. It
follows the system appearance: black with `PaperTokens.libraryChip`
(`#212121`) chips and `libraryHairline` (`#373737`) rules under dark, white
under light; "+ New" is `newButton` (`#127DC6`).

| File | Holds |
|---|---|
| `LibraryView.swift` | `LibraryView` (the initialiser the window uses), `PaperDeepLink`, `LibraryState` (section, open folder by id, search, select mode, the keyboard's row, the open menu and sheet), the sheets, imports, export, the keys, the sidebar, the DEBUG arguments |
| `LibraryBrowserView.swift` | the top bar, the large title, the control row, select mode's row, the list and the grid, the New menu |
| `LibraryRow.swift` | a row, a tile, the thumbnails, drag and drop |
| `LibraryItemMenu.swift` | the `chevron.down` menu (with Color and Icon pages for a folder) and the long-press menu |
| `MovePickerSheet.swift` | the folder tree, a new folder in place, Move |
| `ItemInfoSheet.swift` | Info, with tags |
| `FolderExporter.swift` | a folder as a ZIP of PDFs that keeps its subfolders |
| `PaperController+Library.swift` | everything the views ask of the store, and the pure rules behind it |
| `SearchView.swift`, `TrashView.swift` | search's five groups; the server's Trash, folders included |

- **Top bar.** The sidebar chip (Ctrl-Cmd-S) at the left; inside a folder a
  back chip beside it and the folder's name centred, in place of the large
  title. The large title names the section: Documents, Favorites, Recents or
  Trash. The pill at the right holds Search and the globe, "Back to
  EXSCALIA".
- **Sidebar.** Documents, Favorites, Recents and Trash; at its foot a
  `gearshape` Settings entry that opens the shell's settings sheet, shown
  while the environment carries `paperOpenSettings`. Tags are in Info.
- **Navigation.** Documents is the folder tree: tapping a folder opens it,
  and the back chip (or Cmd-[, or Left in the list) goes up. The open folder
  is held by id and followed when it is renamed or moved, here or on another
  device; when it goes, the library climbs to what is left. Favorites
  (starred folders and documents at any depth) and Recents (sort does not
  apply) are flat; a folder opens under Documents. Trash is the server's,
  with "Deletes in N days" and Restore, and no way to empty it; a folder
  restores through the server and a sync.
- **Folders before the reconcile.** `PaperController.libraryFolders` is the
  folder records plus, for any directory holding a document or a folder with
  no record yet, the legacy record (`PaperFolder.legacy`) the launch
  reconcile will write with the same id. The first launch over a tree an
  older build made shows every folder at once, nothing twice, and nothing
  moves when the reconcile lands. A write to such a folder reads the tree
  first, which writes its record.
- **Control row.** Filter (Show: All, Folders, Notebooks, Whiteboards; Sort
  By: Date, Name, Type, which Recents hides), "+ New", View (Grid View, List
  View, Select Items), then a hairline and the save-state glyph. Trash and
  search hide the row. The choices are `paper.library.sort`,
  `paper.library.filter` and `paper.library.layout` (`LibraryArrangement`,
  PaperCore).
- **Rows** are 84 points apart: a 70 x 54 thumbnail, the name over the date
  line, then the menu chevron and the star, and a small `chevron.right` on a
  folder. Grid tiles are at least 168 points wide. A document's thumbnail is
  its first page at the paper's shape (a board's render rectangle), from the
  renderer's cover JPEG or the render, decoded off the main actor and kept
  until that page renders again (`coverStamps`); until a page has a render,
  its paper and ruling.
- **New** offers Notebook (Option-Cmd-N, and Cmd-N), Whiteboard
  (Shift-Cmd-W), Quick Note (Shift-Cmd-N), Folder, then Import (PDFs), Scan
  Documents, Image and Take Photo. A picture becomes a one-page PDF and
  imports as a document. New things land in the open folder; from Favorites,
  Recents, the Trash or search they land at the top level.
- **Item menu** (`chevron.down`): a name field that renames on Return or when
  the menu closes, then Duplicate (documents), Move, Convert to Whiteboard
  (notebooks; "Convert to Whiteboard" below), Open in New Window,
  Export (a PDF; a folder as a ZIP), Info, Color and Icon (folders), and Move
  to Trash with the undo toast. The **long press** offers Open, Rename (the
  menu above), Duplicate, Move, Convert to Whiteboard (notebooks), Add to
  Favorites or Remove from Favorites,
  Open in New Window, Export, Info, Color, Icon and Move to Trash. The star
  toggles a favourite. A sheet asked for from a menu waits for the menu to
  go. The editor's tab menu uses the same Move picker and Info sheet.
- **Info** shows the name, kind, location, created and modified; pages, size
  and orientation for a notebook, boards for a whiteboard, counts for a
  folder; and the tags, with Add Tag and a remove button on each.
- **Move picker.** The folder tree from Documents, a `folder.badge.plus` New
  Folder that creates one in place, and Move. A folder never goes into itself
  or a folder inside it, or past the 900-byte path limit; the picker dims
  those.
- **Select mode** (View → Select Items, or Cmd-A, which also selects
  everything): Select All, Export, Duplicate, Move, Trash, Done. Dragging a
  row or tile onto a folder moves it (a selected one carries the selection);
  dropping on the back chip moves it up a folder.
- **Keys.** Cmd-F searches, Up and Down walk the rows, Left and Right go up
  and into folders in the list and walk the tiles in the grid, Return opens,
  Cmd-Delete trashes, Cmd-[ goes up, and Escape closes an open menu, then
  search, then select mode, then the sidebar. Every row reads "name, date,
  kind", and "Favorite" when starred; every control has a label.
- **Search** is under "Handwriting recognition and search".
- **Under the editor** the library keeps its `LibraryState` and draws no
  rows: XCUITest reads through `accessibilityHidden`, so the covered
  library's Sidebar and Search answered beside the editor's. Home rebuilds
  the list and scrolls back to the document that was opened. The library's
  key buttons are titled apart from every visible label for the same
  reason.

## New document sheets

`NewDocumentSheets.swift` and `FolderAppearancePicker.swift` hold the three
sheets of the library's New menu. They hand back what was chosen and write
nothing to the store; the library makes the document or the folder through
`PaperController.createDocument`. Focus starts in the title, Return creates,
Escape cancels. All three present at page size, because a form sheet hides
half of each below the fold.

- **New Notebook** (`NotebookSheet`): a cover preview and a paper preview side
  by side, drawn at the chosen size and orientation, then Title, Cover (a
  switch, and the eight cover colours while it is on), Paper (Blank, Ruled,
  Squared, Dotted), Color (White, Cream, Dark), Size (A4, Letter, iPad) and
  Orientation. It hands back the title and a `NewDocumentOptions`.
- **New Whiteboard** (`WhiteboardSheet`): a live preview of the board round
  its centre at 100 %, drawn by `BoardGeometry.drawBackground` as the board
  itself is, then Title, Background (Blank, Dot Grid, Square Grid) and Color.
- **New Folder** (`FolderSheet`, also `FolderSheet(editing:)`): the folder as
  it will look, Name, Color (the twelve system colours) and Icon: None, the 24
  symbols, or one emoji. The emoji field opens the emoji keyboard and holds
  exactly one emoji; text is refused as it is typed and a second emoji
  replaces the first (`FolderEmoji`). It remembers nothing.
- **Defaults.** "Create" saves the notebook or whiteboard choices and the
  next sheet of that kind opens with them; the title is not kept. An empty
  title or name becomes "Untitled", line breaks become spaces, and anything
  past 200 scalars is cut on a character boundary (`NewDocumentTitle`).
- `FolderThumbnail` draws a folder in two tones of its colour with its
  symbol (a darker shade of the folder) or its emoji on the front; a symbol
  the device lacks draws the plain folder. The folder is our own
  `FolderShape` (a back with its tab, 0.16 more saturated and 6 % darker,
  and the front over it with a light top edge), not `folder.fill`, whose
  iOS 26 drawing leaves a gap under the tab that shows as a dark line in
  dark mode. `TemplatePreview` takes a paper colour and a page size and
  draws covers.
- Template thumbnails show a corner of the page at about a third of real
  size, and board backgrounds tile at 56 %: a whole page at tile size turns
  the 5 mm squares and dots into a grey wash. The large previews show the
  whole page, for its size and orientation.

## Editor chrome

The frame the editor lives in. The bars are coloured as in the owner's
screenshot, the same under light and dark appearance: `PaperTokens.tabStrip`
(`#1F335E`), `navigationBar` (`#294C80`, also the active tab),
`tabInactiveTitle` (`#A1AECE`), `toolSelected` (`#C8D2DF`), `floatingPill`
(`#292C2D`) and `writingToolSelected` (`#3A8092`). Every floating pill (undo
and redo, the tool menu, the zoom pill, the minimap, the limit banner, the
object menu) wears one style from `Tokens.swift`: `floatingPillStyle` in
SwiftUI, `PaperTokens.applyFloatingPillStyle` on a `UIView`. It is the pill
colour, a white hairline at 14 % (dark paper, `#2A2B2E`, is nearly the
pill's colour) and a soft shadow.

| File | Holds |
|---|---|
| `PaperWindowView.swift` | the Paper root in each window: the library at Home, the editor otherwise; the tab list in `@SceneStorage("paper.window")`; deep links; the DEBUG arguments that open documents; the tab keys; Open in New Window |
| `DocumentTabsModel.swift` | the window's open documents: order, the active one and its `NotebookSession`, Read Only per tab, Quick Notes, the sidebar's state |
| `DocumentTabBar.swift` | the tab strip: Home, the tabs with their crosses, the active tab's menu, the save state |
| `EditorScreen.swift` | the editor: tab strip, navigation bar, sidebar, the canvas with `FloatingToolLayer` over it, the sheets |
| `EditorNavigationBar.swift` | Sidebar, Search, OMNIA, Read Only; `EditorToolbar`; Add, Share, More; the in-document search sheet |
| `EditorMenus.swift` | the Add, Share and More menus |
| `EditorSidebar.swift` | Pages (with All and Bookmarks), Outline, Boards |
| `NotebookMetadataViews.swift` | the transcript correction sheet |

- **To the bottom edge.** The canvas, notebook or board, runs under the home
  indicator to the bottom of the screen (`ignoresSafeArea` on the bottom
  edge, and on the sidebar's divider), so no strip of the window's background
  shows below the paper. The zoom pill and the minimap
  (`safeAreaLayoutGuide`) and the floating tools laid over the canvas keep to
  the safe area; the notebook's scroll view adds the inset to its content.
- **One editor per window.** Only the active tab has a session and a canvas.
  Switching flushes the ink and the reading position of the document being
  left (`session.onFlush`), then builds the next `NotebookSession` on the
  controller's `ToolState`, so the toolbar and the tool selection are the same
  objects afterwards. The canvas is keyed by the session and the document's
  path: a folder moved on another device rebuilds it on the new path. A
  document trashed or deleted anywhere loses its tab (`closeMissing`, on every
  change to the library's list).
- **Tabs.** `house` goes Home and keeps the tabs. Opening an open document
  shows its tab. Closing the active tab shows the one that took its place,
  else the one before, else Home. Keys: Cmd-1 to Cmd-9, Cmd-W,
  Option-Cmd-W (all, and Home), Control-Tab and Control-Shift-Tab, Cmd-O
  (Home with the library's search focused). The tab list, the active tab and
  the sidebar come back after a relaunch, minus documents that went
  meanwhile. The save-state glyph sits at the right end of the strip.
- **The tab menu** (the active tab's chevron): the name (saved on Return or
  when the menu closes), Move (the library's move picker), Convert to
  Whiteboard (notebooks; off while the tab is Read Only), Close Other
  Tabs, Open in New Window, Export, Info (the library's Info sheet), Move to
  Trash (undo toast).
- **Quick Notes.** `open(_:isQuickNote:)` remembers one; when its tab closes
  or the window goes Home it is deleted silently if it still has one page, no
  strokes, no objects and its title. In memory only.
- **The library** stays alive under the editor, hidden and disabled, so Home
  is where it was left. It opens documents through
  `@Environment(\.paperOpenDocument)`, focuses its search when
  `\.paperLibrarySearchRequest` changes, and finds a folder target in
  `\.paperLibraryFolderID`.
- **Navigation bar.** Labels: Sidebar (value shown or hidden), Search, OMNIA,
  Read Only (value on or off), the toolbar's tools, Add Page or Add Board,
  Share, More. When the window is too narrow for one row the toolbar
  takes a second row.
- **Add**, on a notebook: a position (Before, After, Last), the four papers
  (the notebook's own first), Import PDF, Photo, Camera, Scan Document, Text
  (the Text tool) and Link (a link typed into a text box). On a whiteboard,
  Add Board adds one at the end.
- **Share**: Export This Page (a one-page PDF), Export All, Copy Page Link,
  Open on Web. A whiteboard says Board.
- **More**: Go to Page, Change Template (Change Background on a whiteboard),
  Paper Color, Clear Page, Bookmark Page, Move Page to Trash; Paged Mode,
  Correct Recognized Text, Page History, Info; Trash, Sync Now.
- **Sidebar.** 280 points at the left, pushing the canvas aside. Pages: an
  All or Bookmarks filter, tap to go, drag to reorder, a bookmark on each,
  and a menu with Add Page After, Duplicate, Title, Bookmark, Change Template,
  Paper Color, Clear Page, Export Page and Move to Trash. Outline: titled
  pages in order, with Rename and Remove Title. Boards: Add Board, drag to
  reorder, and Rename, Duplicate, Export, Move to Trash.
  `PaperStore.duplicatePage` and `clearPage` (`PaperStore+Pages.swift`,
  PaperCore) are the page operations underneath.
- **Read Only** is per tab and in memory. The toolbar, the tool menu and the
  undo pill go; every page takes no touches (`isUserInteractionEnabled`), the
  canvases' `drawingGestureRecognizer`, the lasso recognisers, `objectTap`,
  the insert taps, the long press and the undo and redo taps are off, drops
  are refused, and the keys shrink to Cmd-[, Cmd-], Cmd-P and Cmd-C. One
  finger still scrolls, pinch still zooms, and the sidebar, search and
  Share work.
- **OMNIA** sends what is on screen of the page showing (paper, objects and
  ink, at the screen's scale) through the lasso's Ask flow, with its rectangle
  in page points as the region.
- **Open in New Window** sends an `NSUserActivity` of type
  `art.exscalia.ipad.paper.window` (declared in `project.yml`) with
  `targetContentIdentifier` `paper/window` and `userInfo["target"]`, the JSON
  of a `PaperWindowTarget`, through `requestSceneSessionActivation`.
  `RootView.onContinueUserActivity` switches the new window to Paper and
  opens the target. Two windows on one document keep the other-window rules
  (`localPageChanged`, `drawingOwners`).
- **Whiteboards** get the same frame: Boards in the sidebar, Add Board,
  Export This Board. `BoardCanvas` is hosted where `NotebookCanvas` is, and
  `BoardViewController` sets `session.commands`, so OMNIA, Change Background,
  Paper Color, Clear Board, the sidebar's Export, Share's exports, the tab
  menu's Export, undo and redo and the tool menu's Clear Page all act on the
  board ("Whiteboard editor"). More hides Paged Mode on a whiteboard: a board
  is one plane with nothing to page through.

## The notebook editor

`NotebookViewController` is one class spread over extensions, one file per
concern. Its stored properties all stay in the main file, because an
extension cannot hold one.

| File | Holds |
|---|---|
| `NotebookViewController.swift` | stored properties, init, lifecycle, the autosave source, other windows, page views and the live window, navigation, reveal, restore, the scroll and page delegates, `EditorCommands` |
| `PageScrollViews.swift` | `PageScrollView`, `PageStackView` |
| `NotebookViewController+Selection.swift` | `objectTap`'s target (`selectionTap`), Read Only for selection (`setSelectionLocked`), the lasso recognisers (`makeLassoGesture`, `lassoShouldBegin`, `routeLassoTouch`), the gesture delegate, what pages ask for (tool switch, share, paste, renders), the selection's convert, link and OMNIA actions |
| `NotebookViewController+Tools.swift` | the input policy, following `ToolState`, `updateLassoGesture`, `refreshUndoState`, the insert tools, Draw Shape, Clear Page, the Pencil's tool-menu popover |
| `NotebookViewController+Pencil.swift` | Pencil double-tap and squeeze |
| `EditorGestures.swift` | `EditorInput` (who may ink), the double-tap zoom, two- and three-finger undo and redo |
| `NotebookViewController+Zoom.swift` | pinch, `setZoom`, infinite-page growth |
| `NotebookViewController+Keys.swift` | key commands, undo and redo |
| `NotebookViewController+Insert.swift` | text (`placeText`), links, images, PDFs, the pickers, paste (Paper's own copy, pictures, words), a page's template |
| `NotebookViewController+Export.swift` | export |
| `NotebookViewController+Debug.swift` | the `-debug*` launch-argument drivers |
| `NotebookSession.swift` | `NotebookSession`, `UndoToastModel`, `EditorCommands`, `EditorChromeState` |
| `BoardViewController.swift` | the whiteboard's controller and its `BoardCanvas` wrapper ("Whiteboard editor") |

- **`session.commands`** (`EditorCommands`) is how chrome acts on the canvas;
  it never touches PencilKit itself. Undo, redo, adding a page (before, after,
  last, with a template), a page's template, export, the insert pickers and
  zoom. `setPageStyle` changes a page's template, its paper colour or both.
  `clearPage` clears the ink: as an undo step on a page with a canvas, on disk
  (with a snapshot) on one without. `export(.currentPage)` is a one-page PDF.
  `askOMNIA` runs the lasso's Ask flow on what is on screen. `setReadOnly` is
  Read Only.
- **`session.chrome`** (`EditorChromeState`) carries `canUndo` and `canRedo`,
  updated by `refreshUndoState()` after every stroke, undo and page change,
  and `isReadOnly`.
- **`zoomDoubleTap`** and **`objectTap`** sit on the scroll view: one finger,
  never cancelling, delaying or ending late the touches pages and objects
  get, and free to recognise alongside anything but the lasso. `objectTap`
  waits for `zoomDoubleTap` to fail; `+Selection` gives it its target and
  `EditorGestures` gives `zoomDoubleTap` one.
- **Zoom** resizes frames and sets each canvas's `zoomScale`; the scroll view
  never applies a transform. Paged Mode (More) turns on the scroll view's
  paging.
- **Reading position.** `notebook_state` keeps the page showing, the zoom and
  the viewport's top-left in page points from that page's top-left, both
  ways. An offset stored before this (the scroll view's own) opens at the
  page's top.
- **Keys.** Cmd-Z and Shift-Cmd-Z undo and redo, Shift-Cmd-N adds a page
  after this one, Cmd-] and Cmd-[ turn pages, Cmd-P exports (a one-page
  notebook as a PNG), Cmd-C copies the selection, Cmd-X cuts it, Cmd-V
  pastes, Escape puts the selection down (ink or objects), Delete deletes
  it, and Cmd-F searches the document. Copy, cut and Escape are
  `PageSelectionHost`'s (`copySelectionFromKeyboard`,
  `cutSelectionFromKeyboard`, `putDownSelectionFromKeyboard`), shared with
  the board; while a text box is being typed in, Cut, Copy and Paste go to
  its text view.

## Input and the lasso

- **The lasso is ours.** PencilKit exposes no selection API, so
  `LassoOverlayView` captures the loop, selects strokes that are at least half
  inside it, and registers every operation on the canvas's own `undoManager`.
- **Input arbitration.** The loop is captured by `LassoGestureRecognizer`: one
  recogniser on the page stack that the scroll view's `panGestureRecognizer`
  must `require(toFail:)`. Raw `touchesBegan` on the overlay was not enough:
  a `UIScrollView` cancels the touches it has delivered to its content the
  moment its own pan recognises, and on hardware an Apple Pencil drives that
  pan like any other touch, so a Pencil loop only ever scrolled the page.
  `PageScrollView`'s pan takes `.direct` touches only: **the Pencil creates,
  fingers navigate**, for every tool. The loop recogniser takes the Pencil at
  once (and a finger as well under `-debugFingerInk`); a palm resting beside
  the Pencil does not stop it.
- A second recogniser of the same class takes a finger, but only one that
  lands on the selection, to carry it. It begins after 6 points of travel, so
  a second finger that lands first (two-finger scroll, pinch) still fails it
  in time. There are two recognisers because a resting palm is a finger as far
  as UIKit knows: it may fail the finger one, never the Pencil's. A finger
  anywhere else is the scroll view's.
- **Moving a selection.** A touch that lands on the selection carries it
  (`SelectionDrag`). After 2 points of travel the selected strokes are lifted:
  the canvas is given the page without them (a quiet write, not an edit), and
  the overlay draws them as an image that follows the touch, so the ink
  follows the Pencil without PencilKit redrawing the page on every move. The
  drop is one write of the moved drawing: one "Move Selection" undo step,
  reported like any other edit, so autosave and page growth see it. While the
  selection is in the air `PageView.drawing` (what autosave writes) is still
  the page with the selection where it was picked up. A drag the system
  cancels (notification, call, backgrounding, a tool switch) puts the ink and
  its outline back where they started: the person never let go, so no drop
  point was chosen, and that is what the disk held all along. Every menu
  operation puts a drag in progress back before it reads the strokes.
  `NotebookViewController.routeLassoTouch` is the one entry point the
  recogniser, `-debugLassoLoop`/`-debugLassoDrag` and `LassoDragTests` share;
  it also carries and resizes a selected object ("Selection first").
- A page is an accessibility *container*: one element for the canvas and,
  while there is a selection, the object menu beside it. As a plain element
  the page hid the menu's own buttons from VoiceOver.

## Tools

PaperCore's `ToolModel.swift`, `ShapeRecognizer.swift` and `HitTesting.swift`
are pure logic under `swift test`. PaperUI's `ToolState`, `EditorToolbar`,
`ToolMenuView`, `ColorSlotEditor` and `UndoRedoPill` are the UI over them.

### The model (PaperCore)

- **Settings.** One `ToolSettings` value, stored as JSON under `paper.tools.v2`.
  `paper.tool` mirrors the active tool and wins on load, so `-paper.tool lasso`
  still works. Each tool keeps three widths and 1-12 colours. Decoding repairs
  rather than fails: widths clamp to the tool's range, invalid colours are
  dropped, and an out-of-range index resets to 0.
- **Migration.** When `paper.tools.v2` is absent or unreadable,
  `ToolSettingsStore.load` reads the v1 `paper.tool.*` keys with their old
  fallbacks (pen 3 pt, highlighter 20 pt, eraser 24 pt), saves the result and
  deletes the old keys. The pen keeps its brush ink, width and colour.
  `paper.tool.capsuleEdge` and `capsuleOffset` are dropped.
- **PencilKit as data.** `InkKind` raw values are `PKInk.InkType`'s, and
  `ToolSettings.canvasTool` says what the canvases get.
- **Draw Shape.** `ShapeRecognizer.recognize(points:zoom:)` returns a line,
  arrow, triangle, rectangle, ellipse (a circle when the axes are within 8 %)
  or a polygon of 4-8 sides, or `nil`. Strokes under 16 screen points are left
  alone. `splinePaths()` gives `PKStrokePath` control points with every inner
  corner tripled, so the spline keeps it sharp; a test checks this against
  PencilKit on the Mac. Every threshold is a named constant.
- **Tap to select.** `HitTesting.target(at:strokes:objects:zoom:)` tests
  objects first, then the nearest stroke within `max(12 / zoom, width / 2)`,
  then grows a word: strokes whose bounds come within 4 pt and share half the
  shorter one's height. Dots and accents join their letter.

### The runtime (PaperUI)

- **`ToolState`** is an `@Observable` wrapper over `ToolSettings`. Every
  change goes through `update(_:)`, which saves through `ToolSettingsStore`
  and then calls each observer with the settings before and after. The app
  keeps one, `PaperController.tools`. `pkTool` is the PencilKit tool for
  `settings.canvasTool`: fountain, ball and brush pens are `.fountainPen`,
  `.monoline` and `.pen`; the highlighter `.marker`; Draw Shape `.monoline`;
  the eraser `.fixedWidthBitmap` (Partial, with its width) or `.vector`
  (Whole Stroke).
- **`EditorToolbar`**, in the navigation bar's middle group: Lasso, the writing tool (whichever was used last: Pen,
  Pencil, Highlighter or Draw Shape), Eraser, Text, Image, Shapes. Labels are
  the tools' names; the selected one has the value `"selected"` and sits on
  the `toolSelected` square. Tapping it again shows or hides the tool menu.
  Identifiers are `toolbar.<slot>`.
- **`ToolMenuView`** is the dark pill: the writing types (the selected pen's
  chevron picks Fountain Pen, Ball Pen or Brush Pen), three width slots (Thin,
  Medium, Thick) and the colour slots with a trailing `+`; the eraser's
  Partial and Whole Stroke, three sizes and Clear Page (confirmed, one undo
  step); text sizes and colours; shape kinds, widths, colours and Fill. The
  lasso and Image have no menu. A selected width tapped again opens a slider
  (`WidthSlotEditor`); a selected colour opens `ColorSlotEditor`: the eight
  named swatches (the highlighter has its own six, `ToolSettings.swatches(for:)`:
  Yellow, Green, Pink, Blue, Orange and Purple), "Custom…" (the system colour
  picker, stored as sRGB hex) and Remove. Long-press a colour and drop it on another to reorder. On dark
  paper swatches preview as PencilKit will draw them.
- **Docking.** `FloatingToolLayer` floats the undo pill at the top left and
  the menu over the canvas. The menu starts at the top centre; dragging its
  grip carries it, and letting go snaps it to the nearest side
  (`settings.dock`, remembered): rows at the top and bottom, columns at the
  sides. It is never longer than the space that keeps it off the undo pill
  and inside the safe area, and scrolls beyond that. VoiceOver docks it with
  the grip's actions.
- **`UndoRedoPill`** reads `EditorChromeState` and acts through
  `session.commands`; a button that cannot act is dimmed.
- **The canvas follows** through `NotebookViewController.toolsDidChange`: a
  new PencilKit tool on every live canvas when `canvasTool` changes, and on a
  change of tool `setLassoActive`, which puts a lasso drag back, and
  `updateLassoGesture`. While an insert tool is active the canvas's
  `drawingGestureRecognizer` is off, so the Pencil cannot ink under it.
- **Insert tools.** Image opens the photo picker in the window in front and
  falls back to the lasso. Text and Shapes place their objects with a tap
  ("Selection first").
- **Draw Shape.** `pageViewDidBeginUsingTool` remembers the page's stroke
  count; on the lift, one run-loop turn later (once PencilKit has committed the
  stroke and its own undo), `cleanUpShape` runs the new stroke through
  `ShapeRecognizer` and swaps it for `splinePaths()` strokes with the same ink,
  as an undo step of its own: one undo brings the rough stroke back. A stroke
  that is no shape stays as drawn.
- **Gestures** (`EditorGestures`). A one-finger double-tap toggles between
  zoom 1 and 2 about the tap (not on a selected object); two fingers
  double-tapping undo and three redo. All `.direct`, none cancelling touches.
- **Input.** `drawingPolicy` is `.pencilOnly`; `EditorInput.fingerInk`
  (`-debugFingerInk`) is the only exception, for UI tests.
- **The Pencil.** `pencilInteraction(_:didReceiveTap:)` follows
  `preferredTapAction`: Switch Eraser toggles with the previous tool, Switch
  Previous swaps, the palette actions show the tool menu in a popover at the
  hover point (or bring the docked menu back when there is none), Ignore and
  system shortcuts do nothing. `didReceiveSqueeze` shows the same popover when
  a squeeze ends, unless squeeze is off or runs a system shortcut. A hovering
  Pencil shows a circle the size of the tool (`HoverPreviewLayer`).
- **Settings.** "Draw with finger" and "Use system tool picker" are gone, with
  their toggles; M1 deletes the stored keys. The recognition languages are
  written as `[String]`, which is what `PaperController.recognitionLanguages`
  reads.

## Selection first

A finger tap selects; the Pencil draws. The files are `PageView.swift`,
`LassoOverlayView.swift`, `ObjectsContainerView.swift`, the object views
(`ObjectView`, `TextObjectView`, `ImageObjectView`, `ShapeObjectView`),
`ObjectMenu.swift`, and `NotebookViewController+Selection.swift` and
`+Insert.swift`.

- **A tap selects.** `objectTap` (one finger, after the double-tap fails) goes
  to `selectionTap`, then `PageView.handleObjectTap`, which asks PaperCore's
  `HitTesting.target` what is under the finger: objects first, top-most first
  (a line or an arrow along its line), then the nearest stroke within
  `max(12 / zoom, width / 2)` and the rest of its word. Ink becomes the
  lasso's selection, an object the objects layer's; the toolbar goes to the
  Lasso and the object menu appears at once. A tap on bare paper puts the
  selection down; there is one selection in a notebook at a time.
- **The object menu** (`ObjectMenu`) is a dark pill above the selection, or
  below it when there is no room above, with glyphs for the common entries and
  `ellipsis` for all of them by name, plus Move and Resize for VoiceOver and
  the keyboard. Ink: Cut, Copy, Duplicate, Delete, Color, Convert to Text, Copy
  Link, Ask OMNIA. Text: Edit, Cut, Copy, Duplicate, Delete, Color, Size, Bring
  to Front, Send to Back. Image: Cut, Copy, Duplicate, Delete, Crop, Share,
  Bring to Front, Send to Back. Shape: Cut, Copy, Duplicate, Delete, Stroke
  Color, Fill, Width, Bring to Front, Send to Back. Ink and objects together
  (a mixed selection): Cut, Copy, Duplicate, Delete, Ask OMNIA. Every entry is
  one undo step on the canvas's `undoManager`; object edits go through
  `ObjectsContainerView.commit`, which is `PaperController.setObjects`, so
  autosave and sync are the same as for ink.
- **Dragging and handles.** A drag that starts on a selection (ink, an
  object, or one of their handles) is the lasso recogniser's, finger or
  Pencil, so the scroll view waits for it (`lassoShouldBegin`); a finger
  anywhere else scrolls. `routeLassoTouch` hands it to the object
  (`ObjectsContainerView.beginInteraction`) or to the overlay. A touch well
  inside an object moves it, near its edge a handle takes it. Corner handles
  scale proportionally, a text box's type with it; text and shapes also have
  edge handles that move one side; an image being cropped has all eight. A
  lasso selection has four corner handles that scale its ink and objects
  (`SelectionScale`). Each drag is one undo step, and a cancelled one puts
  everything back.
- **The lasso** still draws a loop with the Pencil. It catches strokes at
  least half inside and objects whose middle is inside; one object alone
  becomes that object's selection, anything more a mixed selection that
  moves, scales, copies and deletes as one.
- **Insert taps.** With Text or Shapes, a tap on bare paper places the object
  there: a finger tap through `objectTap`, a Pencil tap through
  `PageView.insertTap`. A text box starts at the tap, at the Text tool's size
  and colour (`color` in the object), and types at once; putting it down
  falls back to the lasso. A shape is 160 x 120, centred on the tap, of the
  tool's kind, width and colour, filled in its stroke colour when Fill is on;
  a Pencil drag (`PageView.shapeDrag`) draws its box instead, a line or an
  arrow in the drag's direction. The tool then falls back to the lasso with
  the shape selected.
- **Long press.** A finger held on the page offers Paste, when there is
  something to paste, and Insert Text.
- **Copy and paste.** Copy puts Paper's own copy of the selection
  (`PaperClip`: the strokes and the objects) on the pasteboard beside a
  picture or the words, so other apps get something too. Paste puts it back on
  the page, selected, as one undo step; an image from another notebook is
  stored again in this one.
- **Shapes on screen.** `ShapeObjectView` draws `PageRenderer.shapePath(for:)`
  with the object's stroke, width and 25 % fill, through
  `PageRenderer.displayColor`, so the page and its render agree.
  `ShapeStyle` reads and writes the payload in `additionalFields`.
- **Read Only.** `session.chrome.isReadOnly` is the one switch: every
  selection gesture checks it when it begins, and turning it on runs
  `setSelectionLocked(true)`, which puts every selection down, ends typing and
  switches off `objectTap`, the insert taps and the long press.
- **For the whiteboard.** A board's `PageView` gets all of this as it is: give
  `objectTap` a target that calls `PageView.handleObjectTap(at:)` in the
  page's coordinates, route the lasso recognisers' touches into
  `routeLassoTouch` (or the same four calls on the page's overlay and
  objects layer), and answer the `PageViewDelegate` calls `didSelect`,
  `didPlace`, `pasteAt`, `share`, `render` and `pageViewIsReadOnly`.
- **Tests.** `PageViewObjectSelectionTests` drives taps through `HitTesting`
  into a selection of each kind, the menu's actions and their undo, object
  drags and handles, the dark rule and shape placement, and, through a real
  notebook, the tool switch, mixed selections, Read Only and paste.
  `SelectionUITests` selects, moves and puts down ink and a text box with real
  finger taps, places a shape with a tap and draws one with a drag (a finger
  under `-debugFingerInk` standing in for the Pencil).

## Objects, PDFs and export

- Text, images and shapes are `objects` in `page.json`, drawn *under* the ink
  by `ObjectsContainerView`: the pen always reaches the canvas above, and the
  objects layer takes touches only while one of them is selected.
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
- `PageRenderer.compose` draws the paper, template, PDF background, objects and
  ink, and both `render.png` and the export call it, so an export is what the
  page looks like rather than a second interpretation of it. Objects are drawn
  in their `page.json` order (images, shapes and text interleaved) under the
  ink. In the exported PDF the imported page, shapes and text are vectors
  (selectable text) and only the ink is rasterised, at 3x; `PKDrawing` has no
  vector output.
- Export is `PDFExporter`: the notebook as a PDF, or a one-page notebook as a
  PNG, shared through `UIActivityViewController`. It runs from Share (Export
  This Page, Export All), the tab menu's Export, the library's item menu and
  Cmd-P. Rendering and PDF import run off the main actor (the flush before an
  export stays on it); Export is disabled while one runs, and imports queue.

## Infinite pages

`PaperSize.infinite` (`"infinite"`, label "Infinite") is no longer offered for
new notebooks; whiteboards replace it. Existing infinite pages keep working:
an infinite page starts at 1024 x 1366 and grows.

- The rule is `InfiniteCanvas` in PaperCore, pure geometry under `swift test`,
  so the screen, the renderer and the exporter cannot disagree about it. When
  the ink or the object frames come within 160 pt of the bottom or the right,
  the page is extended by half a screen in that direction. When they come
  within 160 pt of the top or the left, the page is extended *and* the
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
  `size_name`, and a page pulled with nothing local to go by (a second
  device, a reinstall, the original of a conflict) still comes back
  infinite. A page the server has no name for is infinite when its notebook
  makes infinite pages, it is at least the starting extent and it is not a PDF
  page; otherwise its dimensions pick the preset.
- The template rules the whole extent, and the page column scrolls sideways as
  well as down once a page is wider than the screen.
- Renders, covers and exports use the extent, with the scale capped so that no
  render is longer than `PageRenderer.maximumPixelEdge` (8192 px) on its long
  edge; beyond that the picture is drawn at fewer pixels per point rather than
  refused.
- Zoom and the reading position are unchanged: a page that has grown restores
  to the same place on it.

## Handwriting recognition and search

- `HandwritingRecognizer` (PaperCore) runs `VNRecognizeTextRequest` over the
  page's `render.png`, the picture the renderer has already made, five
  seconds after the page stops changing. It is cancelled outright the moment
  the page turns dirty again, and it runs at background priority: recognition
  is never on the input path. Boards are read differently ("Whiteboards").
- Vision hands back normalised rectangles whose origin is the bottom-left. They
  are converted once, in `HandwritingRecognizer.pageBox`, into page points with
  y running down (the same space as `PageObject.Frame`, the renderer and every
  view), so nothing downstream has to remember which way up the page is.
- The result is written atomically as the derived `text.json`
  (`{lines: [{text, box, confidence}], transcript}`) beside the render, and
  upserted into `page_text` / `page_fts`. `transcript_ai` is never written from
  here: it belongs to the server, which merges its own copy back by revision.
- Recognition languages come from Settings (`paper.recognitionLanguages`),
  defaulting to `en-GB` and `en-US`.
- `page_fts` is FTS5 with the **trigram** tokeniser, so a fragment inside a
  misread word still hits. Queries under three characters fall back to a scan
  of the same column rather than answering "nothing here".
- A `text.json` on disk is the truth: `reconcile()` reads it back into the
  index at launch (skipping files it has already seen, by modification date),
  so a restored backup or a pulled page is searchable without re-running
  Vision.
- **Library search** (`SearchView`, from the library's Search, Cmd-F, or
  Cmd-O in the editor) shows five groups: Folders and Documents by name (case
  and accents ignored), Outline (page titles, read off the main actor once
  per search), then the handwriting of Notebooks and of Whiteboards. Newest
  first, fifty at most each. A handwriting result carries the page number,
  the matching line, and a crop of `render.png` at the union of the matching
  boxes with twelve points of air round it.
- **In a document**, the navigation bar's Search (and Cmd-F) searches that
  document only, by page title and handwriting.
- Tapping a result opens the document at that page, scrolls the region to the
  middle of the viewport and outlines it in the accent colour for a second and
  a half. Under Reduce Motion the outline appears and vanishes rather than
  fading, and the scroll is not animated.
- The lasso's **Convert to Text** reads the selected strokes
  (`PKDrawing(strokes:).image(from:scale:3)`), puts a text object where the ink
  was at roughly the size it was written, and removes the strokes. Both edits
  land in the same run-loop turn, so one Cmd-Z puts the handwriting back.

## Transcripts

- Every page PUT carries `render` and `text`. A transcript that finishes after
  the ink has gone up gets its own `pageText` op, a **derived-only** PUT with
  no `page` and no `drawing` part, stamped `for_revision`, which the server
  files without bumping the revision. It coalesces exactly like `pageRender`,
  and a queued `pagePut` supersedes it, because that push reads `text.json`
  off the disk as it goes.
- A pulled page whose `text_revision` is ahead of the local one fetches
  `text.json` and refreshes the index, so a page read by another iPad (or
  transcribed by OMNIA) is searchable here without a second Vision run.

## Sync

`SyncEngine` is an actor in PaperCore. It never sits on the input path:
autosave has already written the ink and appended an op before the engine hears
anything.

- **Push** drains `OpJournal` head-first. An op either succeeds, becomes a
  conflict, or is retried with a jittered backoff of 1, 2, 4 … 60 seconds. Only
  429, 5xx and the network are retried. Every other 4xx, 409 included, that its
  handler does not resolve *parks* the op (kept, not retried, named in the save
  state), because a request the server will never accept must not block the
  queue behind it and deleting it would be deleting the user's work. The 409s
  that mean something are resolved where they are sent: a page conflict, a page
  that exists already, and a notebook name taken upstream (below).
- **Names.** A new, renamed or moved notebook takes the name the server would
  give it: one no folder or notebook beside it holds, compared after NFC and
  case folding, else "<name> 2" and on (`uniquePackagePath`, the server's
  `_taken`), and never a path over 900 bytes, which the server refuses
  (contract 2.2): such a create, rename or move throws `unsafePath`. A
  rename refused upstream anyway (409, by something this device has not
  pulled) takes the first name free there, and the device follows it with its
  files and the queue behind it.
- **Conflict**: the server keeps the original untouched and parks the incoming
  version as `<pageId>.conflict-<stamp>` right after it. The device adopts that
  sibling id for its own page directory and pulls the original back down, so
  both halves exist on both sides. Work queued for the page while that push
  was out was written into the directory that is the sibling's now, and goes
  up as the sibling's (`OpJournal.repointPage`). A page deleted here while
  that push was out is deleted with its sibling: a delete is not checked
  against revisions (sent a moment sooner it would have taken the server's
  page, whatever was on it), so nothing is adopted or fetched, the sibling
  gets a delete of its own, and both halves go to the server's Trash. The save state names the page and tapping
  it jumps there.
- **Pull** reads `/api/paper/changes?since=&epoch=`. Pages with unpushed local
  ops are skipped entirely, and a page whose server revision is not *ahead* of
  the local one is not fetched, which is also how the device ignores the echo
  of its own save. The same rule holds at the moment of the write:
  `applyRemotePage` checks the journal under the store's lock and throws
  `LocalWorkPending`, writing nothing, for a page with any op not yet pushed,
  so ink autosave wrote while the pull was out is never overwritten; the push
  then meets the server's version as a conflict. `full: true` drops the cursor and reconciles from
  `GET /api/paper` plus one manifest per notebook; that path is additive, since
  a notebook the server has not seen yet is one that is still queued.
  Deletions arrive as tombstones and are mirrored into the device `.trash/`.
- **Live**: `/api/live` is read with `URLSession.bytes`; a `paper` event whose
  `origin` is not this install's `client_id` triggers a pull. Otherwise the
  engine pulls on foreground, after each push batch and every five minutes.
  A wake that comes while a cycle is running, when there is no sleep to cut
  short, runs the loop once more straight after it: the cycle's pull may have
  read the feed before the change the event announced.
- **Hold.** `hold()` stops the engine while the store rewrites a document
  under it (Convert to Whiteboard): no push or pull starts, a pull already out
  lands nothing more (it checks after every wait, before it writes, and its
  changes come again), and `hold()` returns once a push already out has had
  its answer. `release()` ends it.
- **Renders** ride the page PUT as a part with `for_revision`. A render that
  finishes after the ink has gone up gets its own derived-only push, which the
  server accepts without bumping the revision.
- `client_id` is per install (`paper.clientID` in `UserDefaults`), because it
  identifies the iPad rather than the account.
- **Unknown op kinds.** A journal row whose kind this build cannot read (a
  newer build queued it) decodes as `.unknown`: `pending()` skips it,
  `parked()` lists it with the reason "unknown operation", and nothing ever
  sends it.
- **Document fields.** The notebook create sends `kind`, `orientation` and
  `paperColor`; a notebook patch carries them too; `applyRemoteNotebook` takes
  them from the manifest, keeping this device's value when a field is absent.
  `render_rect` rides the page PUT as a form field beside a render, on the full
  push and the derived-only one, and only when a render goes with it.
- **The log.** The engine logs every failure it absorbs and every state it
  sends the save state, under the subsystem `art.exscalia.ipad`
  (`RELEASE_GATE.md`); the newest failure is kept as `sync.last_error`.

### Folders

Folders are directories under `paper/` with their record, `folder.json`,
inside, indexed in the `folders` table (migration `v5-folders`).

- **Store** (`PaperStore+Folders.swift`): `folderRecords`, `folder(id:)`,
  `folder(path:)`, `contents(ofFolder:)`, `createFolder`, `renameFolder`,
  `moveFolder`, `setFolderAppearance`, `setFolderFavorite`, `deleteFolder`
  (undo through `restore(_:)`, which answers nil for a folder). A new folder
  whose name a sibling folder or notebook already has is called "<name> 2", so
  its create is never merged into that one; a rename onto a taken name keeps
  the name and suffixes the directory, as the server does. A change of case
  goes through `.rename-<id>`. Paths are capped at 900 bytes. A notebook made,
  renamed or moved by folder name (`createNotebook(folder:)`,
  `move(notebookID:toFolder:)`) goes into the folder as its directory is
  spelled on disk, matched exactly or after case folding as the server's
  `canonical_folder` does (`canonicalFolderPath`), never into a slugged twin;
  a folder made on the way gets its directory name from `folderSlug` and its
  legacy record.
- **Reconciler**: every directory that is not a package gets a record. A
  missing `folder.json` gets the legacy one (`PaperFolderID.legacy(path:)`, the
  id the server derives too), an unreadable one is set aside as
  `folder.json.corrupt-<stamp>`, a copied id is re-id'd, a `.rename-<id>` left
  by an interrupted rename is put back, and nothing is journalled. Empty
  directories become empty folders.
- **Journal**: `folderCreate`, `folderRename`, `folderPatch` and `folderDelete`
  keep the folder's path in `notebook_path` and its id in `notebook_id`.
  Patches merge by id. A delete drops the work it makes moot (ops at or
  under the folder, except those of something a queued move took out, and the
  edits of something a queued move brought in) but keeps the moves, which
  decide what the server's delete takes; it is not journalled when the
  folder's own create was among the dropped. A rename while the create is
  unpushed re-points the queue from the create on (`rewritePrefix`) instead.
- **Pull**, in order: `folder_moves`, each one directory rename with nothing
  downloaded and the queue re-pointed at the place in it where each op will
  reach the server (`followFolderMove`), or nothing at all for the echo of this
  device's own rename; then `folders`, adopting the server's id for a
  directory known by another, and making the folders this device lacks, empty
  or not; then notebooks and pages, at the place this device keeps them while a
  folder rename is queued here, and skipped under a folder delete queued here;
  then deletions, a folder's last and by id. A folder goes to the device trash
  only when no notebook package survives inside it; otherwise it stays, under
  the legacy record the server will make again once that work arrives. A full
  reconcile applies `folder_records` the same way, additively.
- **Push**: a create answered with another folder's id adopts it (two devices
  that made "Work" offline end with one); a rename answered at a suffixed path
  moves the local directory there. A rename, patch or delete that meets a 404
  because another device moved the folder (and a notebook patch whose folder
  moved) is held, not parked: the drain stops, the pull re-points it, and it
  goes again in the same sync. Only one that still finds nothing after a pull
  parks. A notebook or page delete that meets a 404 is done only when the
  notebook is nowhere upstream: one another device moved (its folder, or its
  own name) is found by id in the library and deleted where it is. A page
  PUT, page create, attachment or notebook patch that meets 404 because its
  notebook was deleted upstream -- by itself or with its folder -- while this
  device still had work for it (which the pull's rule kept here) sends the
  notebook up again as new (`requeueAsNew`), every page as it is here; that
  brings its folder back upstream under the legacy id, as contract 2.7 says,
  and the deleted copy stays in the server's Trash. The rest of its old queue
  is dropped as moot. Never while the notebook's own create is still queued:
  a create the server refused (an older server and a whiteboard, say) parks,
  and so does the op that met the 404, so the queue settles instead of
  sending the refused create again and again. A notebook create the server suffixed and then refused to rename (a
  folder has the name) takes the server's path rather than retrying forever.
- **An open notebook** follows its folder: `AutosaveCoordinator` finds the
  notebook by id under the store's lock when it flushes, so unsaved ink lands
  in the moved directory instead of recreating the old path. A render or a
  reading that finishes late does the same: the renderer writes through
  `storeRender` and the recogniser files where `currentNotebookPath(ofPage:)`
  finds the page by id, under the same lock, and the ops they queue name that
  path (`located`). A page that is nowhere gets nothing, and `writePageText`
  never makes a page directory.

`swift test` covers all of it against `MockPaperServer`, a `URLProtocol`
server that keeps notebooks, revisions and folders in memory and applies the
same conflict, folder and uniqueness rules the real store does.
`FolderSyncTests` runs two devices against it; `FolderJournalTests` holds the
journal, store, reconciler and migration rules. `SyncHoldTests` (the hold, a
pull landing over unpushed work), `SyncWakeTests` (a wake during a cycle),
`LateWriteTests` (renders and readings after a folder move) and
`NotebookFolderTests` (folder spelling and records) cover this round's
rules; `MockPaperServer.holdNext(pathContaining:)` holds one answer back
while a test does something else.

### Wire fixtures

`PaperCoreTests/Fixtures/*.json` are the live `/api/paper` answers, written by
`backend/tests/test_paper_wire_fixtures.py`, and `WireFixtureTests` decodes
them with the `PaperWire` types. `folders`, `folder-rename` and
`changes-folders` show a folder rename reaching the feed as one `folder_moves`
entry with no pages. `notebook-whiteboard` and `page-board` are a whiteboard
and its first board: the server reports a board's `size` as its `render_rect`
(the extent while it has no render) and never `size_points`, and a pull keeps
the extent from `board_extent`. After a wire change, run that backend test
with `UPDATE_FIXTURES=1`, then `swift test`.

### Trash and history

- **Trash** is the library's sidebar entry and More → Trash in a document: the
  server's thirty-day trash, folders included, with Restore. The six-second
  undo toast is the fast path, and it cancels the delete before it is ever
  pushed.
- **History** is More → Page History: this iPad's snapshot ring (ten per page,
  with a preview drawn from the `.pkdrawing`) above the server's git versions.
  Restoring either is a new version, never a rewrite. Server versions list
  their commit rather than a preview image: the API exposes an old page's
  bytes only through a restore, so there is nothing honest to draw yet.

## Whiteboards

The board model, its geometry and its renders are PaperCore, all under
`swift test`.

- **Board geometry** is `BoardGeometry`. Canvas space is page space, origin at
  the top-left of the extent. The extent's centre never moves, and the grid is
  anchored there. The zoom runs to 400 %, and its floor is
  `coveringZoom(extent:viewport:)`: the larger of 5 % and the zoom at which the
  board still covers the viewport, so the board's edge never shows (about 7 %
  on an 11-inch iPad and 8 % on a 13-inch one, for the default 16,384-point
  extent). `steppedZoom` (Cmd-+ and Cmd-minus) steps by 1.25, `zoomPresets`
  are 25, 50, 100, 200 and 400 %, and `doubleTapZoom` doubles, and from
  400 % returns to 100 %.
  `renderRect` is the content (ink and object frames) plus 48 points, in whole
  points, or 1,366 x 1,024 points round the centre of an empty board.
  `fitContent` is Fit Content. `Minimap` holds the minimap's world rectangle
  and its conversions.
- **Limits.** `BoardLimits` says "Board nearly full" at 16 MB or 15,000
  strokes and objects, and "Board full" at 24 MB or 25,000, where the Pencil
  stops inking that board while the eraser, the lasso and objects keep
  working.
- **The grid** comes in levels 25 x 2^k points apart, k >= 0: it never
  divides below 25 points, so zooming in makes the squares larger on screen,
  and zooming out brings in coarser levels so the lines do not merge. A
  level's opacity is `smoothstep(6, 24, spacing x zoom)`, and each line is
  drawn once, at the strength of the coarsest level it belongs to. A Square
  Grid line is two pixels wide (one screen point), black at 8.5 % (white at
  10.5 % on dark paper). A Dot Grid dot is 1.5 pixels across, black at 64 %
  (white at 72 %). The line is measured against the owner's screenshots of
  the reference at 56 % and 100 % on cream: at their scale (2,000 of the
  screen's 2,732 pixels) a line there is two pixels wide and a strong one
  darkens the paper by about 30 levels summed across its width, a fine one
  by about 14; ours match the width and the sums. Dots and dark paper keep
  their proportion to a line's summed darkness. `gridLevels(zoom:)` is the maths and
  `drawBackground` draws the visible part. The screen, the renderer and the
  exporter all call it: a render passes its scale over 2x as the zoom, an
  export its ink scale over 3x. At 56 % the lines fall 14 points apart on
  screen and every other one is stronger, as in the owner's screenshot.
- **A board's render** covers `renderRect`, not the extent, at
  `cappedScale(2, rect)`, and the 8 MB shrink loop still applies. A renderer
  made with `PageRenderer(store:)` records the rectangle in `page.json` as
  `render_rect` (`PaperStore.setRenderRect`) before the caller journals the
  render. That record changes neither `modified` nor the journal. Notebook
  pages keep whole-page renders and no rectangle. A whiteboard's cover is
  board 1's render.
- **Export.** A board is one PDF page covering its render rectangle, with the
  pattern, shapes and text as vectors and the ink at `cappedScale(3, rect)`.
  A board PNG is the same rectangle at 3x.
- **Recognition on boards** never reads `render.png`. The ink is cut into
  1,024-point tiles that overlap by 64 points, and only tiles holding ink
  count, read row by row from the top left, 64 at most. Each tile is drawn
  from the drawing at 2x, ink only on white; on dark paper the ink is drawn as
  it shows and the picture inverted. A line belongs to the tile whose core
  holds the centre of its box, and its box is moved to canvas space. A tile
  that fails to read drops the whole pass, so a half-read board never replaces
  the last full reading. Ink past 64 tiles is not indexed.

## Whiteboard editor

A whiteboard opens in the same editor frame as a notebook ("Editor chrome"),
one board at a time. The files are in `Packages/PaperUI/Sources/PaperUI/`.

| File | Holds |
|---|---|
| `BoardViewController.swift` | the controller and its `BoardCanvas` wrapper: boards, `EditorCommands`, tools, Draw Shape, the save path, limits, view state, other windows, the Pencil, keys, export, Ask OMNIA, insert and paste, the DEBUG arguments |
| `BoardCanvasHost.swift` | `BoardCanvasHost` (contract §6.7), `BoardCanvasView` and `SingleCanvasBoardHost`, its one implementation |
| `BoardBackgroundView.swift` | the paper and its Square Grid or Dot Grid, under the canvas |
| `ZoomPill.swift` | the zoom pill with the minimap button, and `BoardLimitBanner` |
| `MinimapView.swift` | the minimap |
| `PageView.swift` | board mode (`isBoard`) |

- **One canvas, its own scroll view.** `SingleCanvasBoardHost` is one
  `PKCanvasView` whose `contentSize` is the board's extent at 100 %; the zoom
  is the canvas's own `zoomScale` and pinch, from the floor
  (`BoardGeometry.coveringZoom`, so the board's edge never shows) to 400 %.
  Never a transform. The canvas's pan and pinch take fingers only (two for
  the pan under `-debugFingerInk`) and are off between
  `pageViewDidBeginUsingTool` and `DidEndUsingTool`, with the double-tap. The
  canvas reports every layout pass, which is every change of offset and
  zoom, and the grid, the page and its overlays follow from there in the
  same transaction as the ink.
- **The page, inside out.** The board's `PageView` is made with `isBoard`
  and lives inside the canvas as its content
  (`goLive(onBoard:gestureHost:drawingData:)`): the objects layer goes in
  under PencilKit's ink, and the page, holding the lasso overlay, the hover
  circle and the search outline, over it, both at the board's origin and
  `extent x zoom` in size. The page's points are still page points times
  `zoom`, so `HitTesting`, the lasso, the object views and `ObjectMenu` work
  unchanged. Bare touches pass through the page to the ink
  (`PageView.hitTest`); `BoardCanvasView.hitTest` asks the page first, then a
  selected object, then PencilKit. Because the canvas is the scroll view it
  is never switched off: ink is switched with `drawingGestureRecognizer`
  (`PageView.allowsInk`), so fingers pan and pinch whatever is selected. The
  insert tap, the shape drag, the long press, hover and drop move to the
  board's view. A board has no shadow, no template, no PDF and no stand-in
  render.
- **Background.** `BoardBackgroundView` is the paper colour plus one-pixel
  lines (or 1.5-pixel dots) repeated by `CAReplicatorLayer`s, with no backing
  store. Its levels, colours and sizes are `BoardGeometry`'s
  (`gridLevels(zoom:)`, `gridLineComponents(on:)`, `gridDotComponents(on:)`,
  `gridLinePixels`, `gridDotPixels`), the same that `drawBackground` gives
  renders and exports, so one constant moves all three. Each colour is mixed
  with the paper into a flat one, so lines of one level crossing do not
  darken, as one stroke of `drawBackground` does not.
- **Selection** is the notebook's: `PageSelectionHost`
  (`NotebookViewController+Selection.swift`) holds `selectionTap`,
  `lassoShouldBegin`, `routeLassoTouch`, `putDownSelections`, the selection's
  OMNIA, link and convert actions and `renderPicture`, and both controllers
  conform. The board's one-finger tap, after the double-tap fails, goes to
  `selectionTap`; two `LassoGestureRecognizer`s (the Pencil; a finger on the
  selection) sit on the board's view, and the canvas's pan waits for both.
  The object menu finds the screen through the canvas
  (`ObjectMenu.visibleBounds` stops at a canvas that scrolls).
- **Zoom.** The pill at the bottom left reads `BoardGeometry.zoomLabel`
  ("56%", also its accessibility label) and opens Fit Content, 25 %, 50 %,
  100 %, 200 % and 400 %. Cmd-9 fits, Cmd-0 is 100 %, Cmd-+ and Cmd-minus
  step by 1.25, all about the middle of the screen. A one-finger double-tap
  doubles the zoom under the finger and from 400 % returns to 100 %; not on a
  selected object. Fit Content shows all the ink and objects with 48 points
  round them, or the centre at 100 % on an empty board.
- **Minimap.** The `map` button beside the percentage shows or hides it,
  remembered in `paper.board.minimapVisible` (shown unless turned off). It is
  200 x 140 points above the pill: `BoardGeometry.Minimap`'s world, the ink as
  a picture drawn off the main thread under the dark rule, objects as grey
  boxes, the screen as an outline. The content is measured half a second
  after an edit, not on every frame; the picture is drawn again when the
  world changes, one at a time, and the last one is laid where its world
  falls meanwhile. A finger dragged in it moves the view there (the world
  holds still under the finger); a double-tap fits.
- **Boards.** `session.show(pageAt:)` (the Boards sidebar) shows a board: the
  one before is flushed and taken off the canvas, the canvas's history goes,
  and the board opens where this window left it, else where the document was
  left if that was this board, else its content fitted no closer than 100 %,
  else its centre at 100 %. A new board opens centred at 100 %. The view
  state is `notebook_state`: the board, the zoom, and the canvas point in the
  middle of the screen as `offset` (§6.2), written half a second after the
  view stops and on every flush. A stored centre outside the board, or one
  showing none of its content (a notebook's offset from before a
  conversion), is ignored.
- **Tools** follow `ToolState` as on a page. The Pencil inks with a writing
  tool or the eraser; the lasso and the insert tools switch the ink off, and
  a change of tool puts a selection down. Draw Shape cleans a stroke one
  run-loop turn after the lift, as an undo step of its own. Pencil
  double-tap and squeeze follow the system settings.
- **Saving.** Autosave, snapshots, the journal and sync are the notebook's: a
  board is a page. The board's drawing provider hands autosave bytes encoded
  off the main thread: 0.3 s after the last change the drawing is copied on
  the main thread and encoded in the background, and the flush a second
  after the change takes that copy, or encodes on the main thread if the
  drawing has changed since. The write itself stays in `AutosaveCoordinator`,
  on the main thread. In the simulator (a Mac, so several times an iPad's
  speed) a flush of 15,000 strokes (3.1 MB) took 20 ms on the main thread
  with the encode done ahead in 59 ms, against 43 ms to encode there; 25,000
  strokes (5.2 MB) 14 ms, 116 ms and 64 ms. A board's render waits until the
  board has rested five seconds (`PaperController.pageWasFlushed`: a later
  flush restamps `modified` and the earlier render gives way) and reads its
  ink off the main thread; recognition follows the render as on a page.
- **Limits.** Each flush measures the bytes written and the strokes, and the
  objects with them (`BoardLimits`). "Board nearly full" and "Board full",
  with Add Board, show beside the pill. On a full board the Pencil stops
  inking with a writing tool; the eraser, the lasso and objects keep working.
- **Read Only** is the notebook's lock: no ink, lasso, tap selection, insert
  taps, long press or object editing, no undo taps; one finger pans, a pinch
  zooms, and the keys shrink to the zoom, the boards, Export and Cmd-C.
- **Commands.** Add Board adds a board at the end and shows it. Clear Board
  is one undo step on the board showing. Change Background and Paper Color
  restyle a board and its canvas follows the dark rule. Export This Board is
  a one-page PDF over its render rectangle, Export All one page per board.
  OMNIA sends what is on screen with the visible rectangle, in board points,
  as the region. Photo and Camera place a picture fitted to the screen; a
  PDF, a scan and Paged Mode do not apply to a board (Add on a whiteboard
  offers only Add Board, and More hides Paged Mode). Paste and the long press
  are the notebook's. Two windows on one board keep the other-window rules.
- **Keys.** Cmd-Z, Shift-Cmd-Z, Cmd-+ (or Cmd-=), Cmd-minus, Cmd-0, Cmd-9,
  Cmd-] and Cmd-[ (next and previous board), Shift-Cmd-N (new board),
  Cmd-C, Cmd-X, Cmd-V, Cmd-P, Escape, Delete.
- **Tests.** `BoardViewControllerTests` (unit): opening, the zoom floor and
  ceiling and the steps, Fit Content, the double-tap, the view state round
  trip, the tools, a tap that selects and a finger that carries it, the
  limits, Read Only, Add Board and coming back to a board, background and
  paper, the save path, Clear Board's undo, the minimap toggle.
  `BoardUITests`: the pill's label and its menu, the minimap button, a
  finger pan that draws nothing, a pinch that starts on the board, a finger
  tap that selects.

## Convert to Whiteboard

`WhiteboardConversion` (PaperCore) plans the conversion.
`plan(pages:inkBounds:)` works out each page's content, extent (the default,
or the next multiple of 1,024 that leaves 1,024 points round the content, up
to 32,768), translation to the centre, template (Square Grid and Dot Grid
stay, everything else becomes Blank) and colour (a cover colour becomes
white). It throws `WhiteboardConversionError.tooLarge` when a page would need
more than 32,768 points. `board(from:plan:pdfImageAsset:)`,
`translatedDrawing` and `whiteboard(from:plans:)` rewrite a page, its ink and
the notebook in place, keeping every id. A PDF page's binding becomes the
bottom image object, framed at the page rect moved by the translation, and
the board's page part says `"pdf": null`: the server's page patch keeps a key
it is not sent, and clears `pdf` on null. Every page part without a binding
says so now; a part without the key, from the build on `main`, still leaves
the server's binding alone.

**The store** (`PaperStore+Convert.swift`). `planWhiteboardConversion(notebookID:)`
measures every page and its ink and throws what the conversion would refuse
with, changing nothing: `tooLarge`, `PaperStoreError.notFound` for a page
whose `page.json` cannot be read or a PDF page whose document is not on this
device, PencilKit's error for ink that does not decode.
`convertToWhiteboard(notebookID:)` runs once per notebook at a time, whoever
asks (`ConversionGate`): a second request -- another window, a double tap,
the launch finishing a killed one -- waits for the first and finds its work
done; the two would share one staging directory. It goes in this order. Steps 1 to 4, every
expensive part, run without `writeLock`, which autosave takes on the main
thread: a save in any window waits for the swaps only, never for a whole
conversion. Step 5 on holds it.

1. Measure, plan and check every page, noting each page's files (size, date,
   inode of `page.json`, the ink and `text.json`) before reading them. Any
   refusal is thrown here, before the first write, and leaves every file and
   the queue as they were.
2. Push every page's ink into the snapshot ring.
3. Draw each PDF page at 2x on its paper and store it as an asset
   (`page-<n>-<hash>.png`, queued for upload). Stopped here, the pages are
   untouched and at most an unused picture is left.
4. Build every board in `converting/<notebook id>/<page id>` beside the vault
   (the moved ink, the transcript's line boxes moved, the board's
   `page.json`, no stale render). Outside the vault nothing that walks it
   meets one; on the same volume a board swaps in with one rename. A run
   removes what an earlier one left, and a `pages/<id>~convert` an earlier
   build left; the launch reconcile removes what a run a kill stopped left
   (`removeConversionStaging`), except a conversion running then.
5. Under `writeLock`: a page whose files changed since step 1, or that arrived
   since, is planned, snapshotted and built again, and any refusal is still
   thrown before the first swap. Then page by page: swap the board for the
   page directory in one APFS `RENAME_SWAP`, forget the page's blob ETags,
   then queue a whole-page PUT (`["metadata": "convert"]`). A page is always
   wholly the page or wholly the board. A swap the volume refuses throws and
   leaves the page as it was; nothing is written into a live page file by
   file.
6. Queue the notebook patch (`kind`, `paperSize`, `template`), set the
   reading position to the converted page centred at 100 %, then write
   `notebook.json`. The patch carries its values, so one queued before the
   file is written still turns the server's copy into a whiteboard, and the
   pull brings that down.
7. Rebuild the index rows.

Stopped after any step, the notebook is a notebook whose pages are each
untouched or a finished board, and running the conversion again finishes it:
no board moves twice, and every board still owed a PUT gets one
(`ConvertTests` stop it after each step, relaunch and run it again;
`ConvertStagingTests` cover the lock, a page written meanwhile and a refused
swap). Every
page keeps its id, `created`, revision, bookmark, objects and history; its
title becomes the board's name, or "Board N". The PDF file stays in
`assets/`. Text keeps its search rows (by page id) and its boxes move with
the ink; Vision reads the board again after its render.

**The rules.** Queued work is allowed: a conversion's PUT replaces a PUT
queued for the same page, and a queued page create stays ahead of it. A sync
runs first (for at most eight seconds), so the pages converted are the
newest; then the engine is held until the boards are in, so no pull lands
on a page being converted, not even one still out from that sync; a page another device writes after that falls under the page
conflict rule, its version kept as the original and this device's board
beside it as the sibling. A conflict sibling converts like any page and
keeps `conflict_of`. A whiteboard that still holds a sheet -- the original of
such a conflict, or a page a conversion did not reach -- is offered Convert to
Whiteboard again (`holdsSheet`), which converts just its sheets. A notebook
that holds a board is a conversion killed before its `notebook.json` said
whiteboard, and before its patch was queued: the launch reconcile finishes it
(`resumeInterruptedConversions`, with the sync engine held), so the server is
never left with a notebook of boards. A conversion marks itself in
`sync_state` (`convert.started.<id>`) before its first swap and clears the
mark after its last write, because a run killed half way leaves index rows
that still say sheet; `conversionsToFinish()` is the marks plus the
notebooks the index says hold a board, read at once. The controller holds
each of them from its birth, before any window restores a tab, until the
launch has finished them (`holdLaunchConversions`, `endLaunchConversions`):
tab restore, the library and Recents all open documents through
`DocumentTabsModel.activate`, which refuses one held, and gets it once it is
a whiteboard. Every window is then told of the paths. The entry is off in a Read Only tab (that tab's own menu
only) and for a Quick Note still as it was made, which goes away with its
tab. Nothing turns a whiteboard back into a notebook; History restores pages
one at a time, and its snapshot from before the conversion holds each
page's ink as it was, at its old coordinates.

**The menus** (`PaperController+Convert.swift`). The tab menu, the item menu
and the long press each offer Convert to Whiteboard on a notebook, and on a
whiteboard that holds a sheet (`PaperConvertDocumentAction.isOffered`). Opening
a menu measures the document off the main actor (remembered until it
changes); one that cannot be converted shows "Too large to convert" or "PDF
missing on this iPad" in place of the name, disabled. Choosing it asks
"Convert to Whiteboard?" with Convert and Cancel. The window's
`PaperConvertDocumentAction` (`@Environment(\.paperConvertDocument)`, set by
`PaperWindowView`) then converts through the controller, which takes the
document down in every window first: each window showing it goes Home, which
writes its ink and reading position and takes its canvas down, and nothing
opens it again until the conversion is over (`DocumentTabsModel.takeDown`,
`isConverting`). The windows that were showing it, or asked for it
meanwhile, open it again on the whiteboard, centred at 100 %.
Board 1, the thumbnail, is drawn before the library hears of the change;
every other board straight after, unopened (`renderOwedBoards`): its
picture is queued for the server, which shows the old sheet's picture at the
board's size until then, and the board is read by Vision. A board with no
`render.png` is owed one (`boardsOwingRenders`), so the launch reconcile draws
any that an app killed before then left.

## Migration

`PaperMigrations.run(defaults:store:engine:)` (PaperCore) brings the
container an earlier build left on the device forward, once per install. It
is keyed by `paper.migrations.version` (absent until migration 1 finishes, then
the number of the last one finished; `currentVersion` is 2). The launch
reconcile task runs it first: `bootstrapIfNeeded`, `bootstrap()` and the
library's DEBUG arguments all wait on that task, and a failed run never stops
the launch. Only the steps of migrations the install has not finished run,
in order. Migration 2 is one step: pen, Draw Shape and Shapes slots still at
the first rebuilt build's factory widths (1, 2 and 3.5) take the calibrated
ones (2.5, 5 and 8.5, measured against the reference's medium fountain pen);
slots the owner changed are left alone. Migration 3 does the same for the
pencil (1.5, 2.5 and 4 become 4, 6 and 10) and the highlighter (10, 16 and
24 become 14, 22 and 32), scaled with the pen by eye rather than measured.
Migration 1, in order:

1. `ToolSettingsStore.load`, which turns the capsule's `paper.tool.*` keys
   into `paper.tools.v2` and deletes them ("Tools" above).
2. Deletes `paper.drawWithFinger` and `paper.useSystemToolPicker`.
3. A comma-joined String `paper.recognitionLanguages` becomes `[String]`;
   an array is left alone.
4. The change cursor goes back to 0 through `SyncEngine.forgetCursor()`, so
   the next pull ends in a full one. A cursor that is absent or 0 already is
   left alone. The full pull is additive and carries no tombstones, so the
   cursor it replaces is set aside as `changes.reset_from` in `sync_state`:
   the next pull first runs one ordinary incremental pull from there, landing
   the deletions and folder moves since this device last pulled, then clears
   the mark and runs the full one. A failure on the wire, a 5xx, or something
   that pull named and did not get keeps the mark: no full pull yet, and the
   next pull tries again, so a flaky first pull loses no deletion. A 4xx or a
   full answer clears it, and after five failures it goes all the same
   (`resetAttemptLimit`), so it never holds the full pull back for good. A pull already
   under way lands what it fetched but does not write its cursor over the
   reset; from that same cursor it counts as the incremental pull. With no
   engine the cursor is simply 0. On a real launch the engine is running
   before this step, and nothing here waits on the network.
5. `PaperStore.reconcile()`, then a check that every directory it visits has
   a folder record. Legacy folders get the id the server derives; nothing is
   journalled.

- **Safe to repeat.** Every step is idempotent and the version is written
  only after all five succeed, so a launch killed after any step is finished
  by the next. A run at the current version does nothing.
- **What it never touches.** A page's ink, renders, transcripts, manifests,
  snapshots and the device trash; any op in the journal (none is dropped,
  rewritten or reordered). It never waits on the network.
- **What reaches the server afterwards.** The queued ops first, as the earlier
  build left them and in its order. Then the feed since the old cursor, and
  `GET /api/paper/changes?since=0`, answered full: `GET /api/paper` and one manifest per notebook, a page only
  where the server is ahead of this device, and no folder write. A server
  from before folder records loses nothing and parks nothing. Its boot pass
  later sends the records on an ordinary pull, with the same ids.
- **Mirrored.** A notebook deleted on the server after the iPad's last pull
  goes to the device trash on the pull from the old cursor, before the full
  pull. After an epoch change or a cursor older than the 60-day prune the
  server answers that pull full, which has no deletions to give, as on
  `main`.
- **Folder dates.** The `folders` table keeps seconds since 2001, what a `Date`
  holds, so a row reads back exactly. A row in seconds since 1970, from an
  earlier build of this work, is rewritten by the next launch reconcile.
- **Seeing it run.** `log show --predicate 'subsystem == "art.exscalia.ipad"
  AND category == "migration"'` (through `xcrun simctl spawn <udid>`) prints
  "migration 1 finished" with counts, or the step it stopped at.
  `-paper.migrations.version 0` runs it again for one launch.

`MigrationTests` build a container as the earlier build wrote it and test the
run, a second run, a fresh install, a kill after each step, and the sync
afterwards against `MockPaperServer`: a current server, one from before
folder records, days offline, and a pull already under way.
