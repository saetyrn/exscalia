import Observation
import PaperCore
import PencilKit
import SwiftUI
import UIKit

/// What one window did to a page, for the other windows on this device.
/// Remote changes arrive from the server; these never leave the device.
public enum LocalPageEvent: Sendable, Equatable {
    /// Marked dirty. `source` is the window whose canvas holds the edit; nil
    /// when the edit claimed no canvas.
    case dirtied(PageRef, source: UUID?)
    /// Written to disk. `owner` is the window whose canvas the ink was read
    /// from, nil when no open window has edited the page.
    case flushed(PageRef, owner: UUID?)
}

/// The Paper surface's single owner of state.
///
/// One store, one autosave coordinator, one renderer, one tool state, for the
/// whole section. Views hold this and nothing else, which is what keeps the
/// library and the canvas from disagreeing about what exists.
@MainActor
@Observable
public final class PaperController {
    public let store: PaperStore
    public let snapshots: SnapshotRing
    public let renderer: PageRenderer
    /// Legacy default tool state. Live notebook sessions own their own
    /// `ToolState`; keeping this property avoids breaking embedders that used
    /// the original controller API without making windows share selection.
    public let tools: ToolState

    /// Notebooks, newest activity first. Recomputed rather than mutated so a
    /// view never shows a list the store disagrees with.
    public private(set) var notebooks: [Notebook] = []
    public private(set) var folders: [String] = []
    public private(set) var tags: [String] = []

    /// What the pill shows.
    public private(set) var saveState: SaveState = .savedLocally
    /// Bumped every few seconds so "Synced 3s ago" keeps counting. The pill
    /// reads it so the text re-renders without the state itself changing.
    public private(set) var saveStateVersion = 0
    /// Set when the server kept a sibling of a page this device wrote.
    public private(set) var conflict: ConflictTarget?
    /// Open notebook screens subscribe independently. A single callback made
    /// the most recently opened window silence every older window.
    @ObservationIgnored private var remoteChangeObservers:
        [UUID: @MainActor (Set<String>) -> Void] = [:]
    /// The same, for what the windows on this device do to pages. Every
    /// window shares this controller, so nothing else would tell one of them
    /// that the page it is showing was just written by another.
    @ObservationIgnored private var localPageObservers:
        [UUID: @MainActor (LocalPageEvent) -> Void] = [:]

    /// Every live notebook canvas registers independently. `drawingOwners`
    /// records which window most recently edited each page, so two windows on
    /// the same page can never make autosave fetch stale ink from the wrong
    /// canvas.
    @ObservationIgnored private var drawingProviders:
        [UUID: @MainActor (PageRef) -> Data?] = [:]
    @ObservationIgnored private var drawingOwners: [String: UUID] = [:]

    @ObservationIgnored public private(set) var autosave: AutosaveCoordinator!
    /// Reads pages with Vision once they have been still for five seconds.
    @ObservationIgnored public private(set) var recognizer: HandwritingRecognizer!
    @ObservationIgnored private let dirtyPages = DirtyPageSet()

    /// Nil until `configureSync` runs: Paper is fully usable with no server,
    /// and everything in M2 works before this exists.
    @ObservationIgnored public private(set) var sync: SyncEngine?
    @ObservationIgnored public private(set) var api: PaperAPI?
    @ObservationIgnored var apiSession: URLSession?
    /// The thread of a lasso action the client stopped waiting for, by
    /// notebook id: the notebook's next action joins it.
    @ObservationIgnored var selectionThreads: [String: String] = [:]
    @ObservationIgnored private var monitor: NetworkMonitor?
    @ObservationIgnored private var tickTask: Task<Void, Never>?
    /// A page's ink did not reach the disk. That outranks anything the sync
    /// engine has to say, so the pill keeps it until a write succeeds.
    @ObservationIgnored private var autosaveNeedsAttention = false
    /// The launch reconcile, run once per controller and off the main actor.
    /// Kept so the first-notebook check can wait for what it finds.
    @ObservationIgnored private var initialReconcile: Task<Void, Never>?
    /// The PDF import running last. Imports run one after another: each page
    /// written rewrites its notebook's page order, and two imports
    /// interleaving there would each drop the other's pages from it.
    @ObservationIgnored var pdfImportTail: Task<PDFImportResult?, Never>?

    public init(store: PaperStore, tools: ToolState = ToolState()) {
        self.store = store
        self.snapshots = SnapshotRing(paths: store.paths, database: store.database)
        self.tools = tools
        // The renderer must never draw a page that is still being written on:
        // the result would be stale the moment it landed. The renderer runs off
        // the main actor, so the set it consults is its own locked box.
        let dirty = dirtyPages
        self.renderer = PageRenderer(
            paths: store.paths, isDirty: { dirty.contains($0) })
        self.autosave = AutosaveCoordinator(
            store: store, snapshots: snapshots,
            drawingData: { [weak self] ref in self?.drawingData(for: ref) })
        // Once the sync engine exists it owns the pill: autosave only knows
        // that bytes reached the disk, which is a smaller claim than the one
        // the pill makes. A failed write, and the write that ends it, are the
        // exceptions.
        autosave.onSaveStateChange = { [weak self] state in
            guard let self else { return }
            var failed = false
            if case .attention = state { failed = true }
            if failed || self.autosaveNeedsAttention || self.sync == nil {
                self.saveState = state
            }
            self.autosaveNeedsAttention = failed
        }
        autosave.onFlushed = { [weak self] ref in self?.pageWasFlushed(ref) }

        // Recognition rides behind the render and never in front of the pen:
        // it starts five seconds after the last change and is cancelled the
        // moment the page turns dirty again.
        let recognizer = HandwritingRecognizer(
            store: store,
            isDirty: { dirty.contains($0) },
            languages: { PaperController.recognitionLanguages() })
        self.recognizer = recognizer
        let box = WeakPaperController(self)
        Task.detached(priority: .utility) {
            await recognizer.setOnRecognized { job in
                Task { @MainActor in box.value?.textWasRecognized(job) }
            }
        }
        // What the index already holds, so the library has something to show
        // while the launch reconcile reads the tree.
        refresh()
    }

    /// Recognition languages from the settings screen, falling back to the
    /// two English spellings the plan names.
    public nonisolated static func recognitionLanguages(defaults: UserDefaults = .standard)
        -> [String]
    {
        let stored = defaults.stringArray(forKey: ServerSettings.Key.recognitionLanguages) ?? []
        let cleaned = stored.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        return cleaned.isEmpty ? HandwritingRecognizer.defaultLanguages : cleaned
    }

    public convenience init(root: URL) throws {
        self.init(store: try PaperStore(root: root))
    }

    deinit {
        tickTask?.cancel()
        monitor?.cancel()
    }

    // MARK: - Sync

    /// Attaches the sync engine. Called once, by the app, when a server is
    /// configured; calling it again is a no-op rather than a second engine.
    public func configureSync(api: PaperAPI, clientID: String, session: URLSession) {
        guard sync == nil else { return }
        self.api = api
        self.apiSession = session
        let live = LiveEventStream(session: session, request: { await api.liveRequest() })
        let engine = SyncEngine(store: store, api: api, clientID: clientID, live: live)
        sync = engine

        let box = WeakPaperController(self)
        Task {
            await engine.setHandlers(
                onState: { state in
                    Task { @MainActor in box.value?.engineStateChanged(state) }
                },
                onConflict: { target in
                    Task { @MainActor in box.value?.conflict = target }
                },
                onNotebooksChanged: { paths in
                    Task { @MainActor in
                        guard let controller = box.value else { return }
                        controller.refresh()
                        controller.notifyRemoteChange(paths)
                    }
                })
            await engine.start()
        }

        let monitor = NetworkMonitor()
        monitor.start { online in
            Task { await engine.setOnline(online) }
        }
        self.monitor = monitor
        startPillTick()
    }

    /// Unsaved ink keeps the pill; only a conflict, which also needs the user,
    /// may replace it.
    private func engineStateChanged(_ state: SaveState) {
        if autosaveNeedsAttention {
            guard case .conflict = state else { return }
        }
        saveState = state
    }

    /// Asks for a cycle now: on foreground, and when the user pulls to refresh.
    public func syncNow() {
        guard let sync else { return }
        Task { await sync.syncNow() }
    }

    /// The newest failure the engine absorbed, kept across launches; for a
    /// diagnostics view. Nil with no server, or when nothing has failed.
    public func lastSyncError() async -> String? {
        await sync?.lastError
    }

    /// The web shell saw a signed-in session: lift the engine's signed-out
    /// latch and sync now.
    public func credentialsChanged() {
        guard let sync else { return }
        Task { await sync.credentialsChanged() }
    }

    /// The user has looked at the conflicting page.
    public func clearConflict() {
        conflict = nil
        guard let sync else { return }
        Task { await sync.clearConflict() }
    }

    public func retryParkedOperations() {
        if autosaveNeedsAttention { autosave.flushAll() }
        guard let sync else { return }
        Task { await sync.retryParked() }
    }

    /// "Synced 3s ago" is a clock, so something has to move it.
    private func startPillTick() {
        tickTask?.cancel()
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard let self else { return }
                if case .synced = self.saveState { self.saveStateVersion &+= 1 }
            }
        }
    }

    // MARK: - Trash, history and snapshots

    /// The server's Paper trash. Nil when there is no server configured.
    public func serverTrash() async -> [PaperTrashItem]? {
        guard let api else { return nil }
        return try? await api.trash().items
    }

    @discardableResult
    public func restoreFromServerTrash(_ item: PaperTrashItem) async -> Bool {
        guard let api else { return false }
        do {
            _ = try await api.restoreFromTrash(id: item.id)
        } catch {
            return false
        }
        // Take the restored files straight back down rather than waiting for
        // the next poll: the user is looking at the list they just acted on.
        if let sync { await sync.pull() }
        refresh()
        return true
    }

    public func serverHistory(notebookPath: String, pageID: String) async -> [PageVersion] {
        guard let api else { return [] }
        return (try? await api.history(notebook: notebookPath, pageID: pageID))?.versions ?? []
    }

    @discardableResult
    public func restoreServerVersion(notebookPath: String, pageID: String, sha: String) async
        -> Bool
    {
        guard let api, let sync else { return false }
        do {
            _ = try await api.restoreVersion(
                notebook: notebookPath, pageID: pageID, sha: sha, clientID: sync.clientID)
        } catch {
            return false
        }
        await sync.pull()
        refresh()
        return true
    }

    /// This device's own finer-grained history, from the snapshot ring.
    public func localSnapshots(pageID: String) -> [SnapshotRing.Snapshot] {
        (try? snapshots.snapshots(pageID: pageID)) ?? []
    }

    /// Restoring a local snapshot is an ordinary local flush: the bytes are
    /// written, snapshotted again, and queued for the server. Nothing is
    /// rewritten and nothing is lost.
    @discardableResult
    public func restoreLocalSnapshot(
        _ snapshot: SnapshotRing.Snapshot, notebookPath: String, pageID: String
    ) -> Bool {
        guard let data = try? Data(contentsOf: snapshot.url) else { return false }
        do {
            try store.writeDrawing(data, notebookPath: notebookPath, pageID: pageID)
            try snapshots.push(data, pageID: pageID)
            if var page = try? store.page(notebookPath: notebookPath, id: pageID) {
                page.modified = Date()
                try store.save(page: page, notebookPath: notebookPath)
            }
            try store.journal.append(
                .init(kind: .pagePut, notebookPath: notebookPath, pageID: pageID))
        } catch {
            return false
        }
        if let sync { Task { await sync.kick() } }
        return true
    }

    // MARK: - Lifecycle

    /// Repairs the index from the tree, once, and guarantees there is
    /// something to open. Returns the notebook the app should show on a first
    /// launch.
    ///
    /// The library calls this every time it appears -- every switch into
    /// Paper -- so the walk over every notebook package runs off the main
    /// actor and only the first call starts it.
    public func bootstrapIfNeeded() async -> Notebook? {
        await startInitialReconcile().value
        return createFirstNotebookIfEmpty()
    }

    /// The same for a caller that cannot wait (an App Intent). The reconcile
    /// still runs in the background; only an index with no notebooks at all is
    /// read synchronously.
    @discardableResult
    public func bootstrap() -> Notebook? {
        startInitialReconcile()
        return createFirstNotebookIfEmpty()
    }

    @discardableResult
    private func startInitialReconcile() -> Task<Void, Never> {
        if let initialReconcile { return initialReconcile }
        let store = store
        let box = WeakPaperController(self)
        let task = Task.detached(priority: .utility) {
            _ = try? store.reconcile()
            await MainActor.run { box.value?.refresh() }
        }
        initialReconcile = task
        return task
    }

    private func createFirstNotebookIfEmpty() -> Notebook? {
        refresh()
        guard notebooks.isEmpty else { return nil }
        // No rows at all: a first launch, where reading the tree is free, or an
        // index that lost its rows, where it is the only thing that stops a
        // second "Notebook 1" appearing beside the notebooks already on disk.
        _ = try? store.reconcile()
        refresh()
        guard notebooks.isEmpty else { return nil }
        // First launch lands on a page, not on an empty grid with a tour.
        let created = try? store.createNotebook(
            title: "Notebook 1", template: .ruled, size: .defaultForNewNotebooks)
        refresh()
        return created
    }

    public func refresh() {
        notebooks = (try? store.notebooks()) ?? []
        folders = (try? store.folders()) ?? []
        tags = (try? store.tags()) ?? []
    }

    // MARK: - Dirty tracking

    public func markPageDirty(_ ref: PageRef, source: UUID? = nil) {
        if let source, drawingProviders[source] != nil {
            drawingOwners[ref.pageID] = source
        }
        dirtyPages.insert(ref.pageID)
        autosave.markDirty(ref)
        // Whatever was queued describes ink that no longer exists.
        let recognizer = recognizer
        let pageID = ref.pageID
        Task { await recognizer?.cancel(pageID: pageID) }
        if saveState != .savedLocally, !autosaveNeedsAttention { saveState = .savedLocally }
        notifyLocalPageChange(.dirtied(ref, source: source))
    }

    // MARK: - Window-scoped callbacks

    /// Registers a live canvas and returns the token its owner must remove.
    /// The callback may return nil for pages that do not belong to that
    /// window.
    @discardableResult
    public func addDrawingProvider(
        _ provider: @escaping @MainActor (PageRef) -> Data?
    ) -> UUID {
        let id = UUID()
        drawingProviders[id] = provider
        return id
    }

    public func removeDrawingProvider(_ id: UUID) {
        drawingProviders[id] = nil
        drawingOwners = drawingOwners.filter { $0.value != id }
    }

    private func drawingData(for ref: PageRef) -> Data? {
        if let owner = drawingOwners[ref.pageID],
            let data = drawingProviders[owner]?(ref)
        {
            return data
        }
        for provider in drawingProviders.values {
            if let data = provider(ref) { return data }
        }
        return store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID)
    }

    @discardableResult
    public func observeRemoteChanges(
        _ observer: @escaping @MainActor (Set<String>) -> Void
    ) -> UUID {
        let id = UUID()
        remoteChangeObservers[id] = observer
        return id
    }

    public func removeRemoteChangeObserver(_ id: UUID) {
        remoteChangeObservers[id] = nil
    }

    private func notifyRemoteChange(_ paths: Set<String>) {
        for observer in remoteChangeObservers.values { observer(paths) }
    }

    /// Called synchronously, on the input path for `.dirtied`: observers must
    /// stay cheap.
    @discardableResult
    public func observeLocalPageChanges(
        _ observer: @escaping @MainActor (LocalPageEvent) -> Void
    ) -> UUID {
        let id = UUID()
        localPageObservers[id] = observer
        return id
    }

    public func removeLocalPageChangeObserver(_ id: UUID) {
        localPageObservers[id] = nil
    }

    private func notifyLocalPageChange(_ event: LocalPageEvent) {
        for observer in localPageObservers.values { observer(event) }
    }

    private func pageWasFlushed(_ ref: PageRef) {
        dirtyPages.remove(ref.pageID)
        // Before anything else: another window showing this page is still
        // holding the ink it loaded, and its next stroke would write that
        // whole page back over this one.
        notifyLocalPageChange(.flushed(ref, owner: drawingOwners[ref.pageID]))
        // The journal grew; tell the engine so the pill counts down and the
        // push starts without waiting for the next poll.
        if let sync { Task { await sync.kick() } }
        guard let page = try? store.page(notebookPath: ref.notebookPath, id: ref.pageID) else {
            return
        }
        let job = PageRenderJob(
            notebookPath: ref.notebookPath, pageID: ref.pageID, template: page.template,
            size: page.size, drawing: store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        let renderer = renderer
        let box = WeakPaperController(self)
        Task.detached(priority: .utility) {
            await renderer.render(job)
            await MainActor.run { box.value?.renderDidFinish(ref) }
        }
    }

    /// The picture of the page exists now.
    ///
    /// It almost always lands after the ink has already been pushed, so it gets
    /// its own derived-only push rather than waiting for the next stroke: a
    /// page whose render never reaches the server is a page the web viewer and
    /// OMNIA cannot see.
    private func renderDidFinish(_ ref: PageRef) {
        _ = try? store.journal.append(
            .init(kind: .pageRender, notebookPath: ref.notebookPath, pageID: ref.pageID))
        if let sync { Task { await sync.kick() } }
        // The picture is what Vision reads, so the queue starts here.
        guard let page = try? store.page(notebookPath: ref.notebookPath, id: ref.pageID),
            let recognizer
        else { return }
        let job = RecognitionJob(notebookPath: ref.notebookPath, page: page)
        Task.detached(priority: .background) { await recognizer.schedule(job) }
    }

    /// The page has been read. The transcript is already on disk and in the
    /// index; all that is left is to send it, which is a derived-only push
    /// exactly like a render.
    private func textWasRecognized(_ job: RecognitionJob) {
        _ = try? store.journal.append(
            .init(kind: .pageText, notebookPath: job.notebookPath, pageID: job.pageID))
        if let sync { Task { await sync.kick() } }
        onTextRecognized?(job)
    }

    /// Raised after a page's transcript changed, so an open search can refresh.
    @ObservationIgnored public var onTextRecognized: (@MainActor (RecognitionJob) -> Void)?

    // MARK: - Search

    /// Handwriting search over this device's index, grouped by notebook.
    public func search(_ query: String) -> [NotebookMatches] {
        store.textIndex.search(query)
    }

    public func notebook(atPath path: String) -> Notebook? {
        (try? store.notebook(path: path)) ?? notebooks.first { $0.path == path }
    }

    /// The piece of `render.png` a search result shows: the union of the
    /// matching boxes with twelve points of air around it, so the words are
    /// legible rather than clipped to their own ink.
    public func crop(for match: PageTextMatch, padding: CGFloat = 12) -> UIImage? {
        let url = store.paths.render(match.notebookPath, match.pageID)
        guard let data = try? Data(contentsOf: url), let image = UIImage(data: data),
            let cgImage = image.cgImage
        else { return nil }
        guard let page = try? store.page(notebookPath: match.notebookPath, id: match.pageID)
        else { return nil }
        let pointSize = page.pointSize
        guard pointSize.width > 0, pointSize.height > 0 else { return nil }
        // The render is 2x, but read the factor off the file rather than
        // assuming it: a page pulled from another device may not be.
        let scale = CGFloat(cgImage.width) / pointSize.width
        let padded = match.unionBox.insetBy(dx: -padding, dy: -padding)
        let pixels = CGRect(
            x: padded.minX * scale, y: padded.minY * scale,
            width: padded.width * scale, height: padded.height * scale)
        let clamped = pixels.intersection(
            CGRect(x: 0, y: 0, width: cgImage.width, height: cgImage.height))
        guard clamped.width >= 1, clamped.height >= 1,
            let cropped = cgImage.cropping(to: clamped.integral)
        else { return nil }
        return UIImage(cgImage: cropped, scale: scale, orientation: .up)
    }

    /// Flush from `scenePhase` changes. Background flushes buy time with
    /// `beginBackgroundTask`, because the alternative is losing the last
    /// second of a sentence to the app being suspended.
    public func flushForBackground() {
        guard autosave.hasUnsavedChanges else { return }
        let application = UIApplication.shared
        var identifier = UIBackgroundTaskIdentifier.invalid
        identifier = application.beginBackgroundTask(withName: "Paper autosave") {
            application.endBackgroundTask(identifier)
            identifier = .invalid
        }
        autosave.flushAll()
        if identifier != .invalid {
            application.endBackgroundTask(identifier)
        }
    }

    // MARK: - Library operations

    public func createNotebook(title: String, folder: String, template: Template, size: PaperSize)
        -> Notebook?
    {
        let notebook = try? store.createNotebook(
            title: title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                ? "Untitled" : title, folder: folder, template: template, size: size)
        refresh()
        if let sync { Task { await sync.kick() } }
        return notebook
    }

    public func rename(_ notebook: Notebook, to title: String) {
        _ = try? store.rename(notebookID: notebook.id, to: title)
        refresh()
        if let sync { Task { await sync.kick() } }
    }

    public func move(_ notebook: Notebook, toFolder folder: String) {
        _ = try? store.move(notebookID: notebook.id, toFolder: folder)
        refresh()
        if let sync { Task { await sync.kick() } }
    }

    public func toggleFavorite(_ notebook: Notebook) {
        _ = try? store.setFavorite(notebookID: notebook.id, !notebook.favorite)
        refresh()
    }

    public func duplicate(_ notebook: Notebook) {
        _ = try? store.duplicate(notebookID: notebook.id)
        refresh()
        if let sync { Task { await sync.kick() } }
    }

    public func delete(_ notebook: Notebook) -> TrashToken? {
        let token = try? store.delete(notebookID: notebook.id)
        refresh()
        if let sync { Task { await sync.kick() } }
        return token
    }

    public func restore(_ token: TrashToken) {
        _ = try? store.restore(token)
        refresh()
        if let sync { Task { await sync.kick() } }
    }

    /// Notebooks in the order they were last opened -- what the switcher's
    /// Recents shows and what Control-Tab cycles through.
    public func recentNotebooks(limit: Int = 12) -> [Notebook] {
        (try? store.recentlyOpened(limit: limit)) ?? notebooks
    }

    public func coverImage(for notebook: Notebook) -> UIImage? {
        let automatic = (try? store.pages(ofNotebook: notebook.id))?
            .max { $0.modified < $1.modified }?.id
        guard let pageID = notebook.cover ?? automatic ?? notebook.pages.first else { return nil }
        if let data = try? Data(contentsOf: PageRenderer.coverURL(pageID: pageID)),
            let image = UIImage(data: data)
        {
            return image
        }
        guard
            let data = try? Data(
                contentsOf: store.paths.render(notebook.path, pageID))
        else { return nil }
        return UIImage(data: data)
    }
}

/// A live notebook: which page is showing, what the strip lists, and the two
/// display switches from the `⋯` menu.
///
/// The UIKit canvas and the SwiftUI chrome both hold this, which is how the
/// thumbnail strip can move a page and the scroll view can follow.
@MainActor
@Observable
public final class NotebookSession {
    public let controller: PaperController
    public let tools: ToolState
    public private(set) var notebook: Notebook
    public private(set) var pages: [Page] = []
    public var currentIndex: Int = 0
    public var isShowingThumbnails = false
    public var isPagedMode = false
    public var zoom: CGFloat = 1

    /// Deleting a page shows the same six-second toast the library uses.
    public var pendingUndo: UndoToastModel?

    /// Set by the view controller: writes the ink and the reading position
    /// straight away, for the moment another notebook takes this one's place.
    @ObservationIgnored public var onFlush: (() -> Void)?
    /// Set by the view controller.
    @ObservationIgnored public var onScrollTo: ((Int) -> Void)?
    @ObservationIgnored public var onPagesChanged: (() -> Void)?
    /// Set by the view controller: bring a page's region into the middle of
    /// the viewport and flash the accent outline round it.
    @ObservationIgnored public var onReveal: ((Int, CGRect?) -> Void)?
    /// Held when a reveal arrives before the canvas has laid out -- opening a
    /// notebook straight onto a search result. The controller drains it after
    /// restoring the reading position, so the two do not fight over the
    /// scroll offset.
    @ObservationIgnored private var deferredReveal: (index: Int, box: CGRect?)?

    public init(
        controller: PaperController,
        notebook: Notebook,
        tools: ToolState = ToolState()
    ) {
        self.controller = controller
        self.notebook = notebook
        self.tools = tools
        reload()
        try? controller.store.markOpened(notebookID: notebook.id)
    }

    public func reload() {
        notebook = (try? controller.store.notebook(id: notebook.id)) ?? notebook
        pages = (try? controller.store.pages(ofNotebook: notebook.id)) ?? []
        currentIndex = min(currentIndex, max(pages.count - 1, 0))
    }

    /// Search is navigation: go to the page, and to the spot on it.
    public func reveal(pageID: String, box: CGRect?) {
        guard let index = pages.firstIndex(where: { $0.id == pageID }) else { return }
        currentIndex = index
        if let onReveal {
            onReveal(index, box)
        } else {
            deferredReveal = (index, box)
        }
    }

    public func consumeDeferredReveal() -> (index: Int, box: CGRect?)? {
        defer { deferredReveal = nil }
        return deferredReveal
    }

    public func ref(at index: Int) -> PageRef? {
        guard pages.indices.contains(index) else { return nil }
        return PageRef(
            notebookID: notebook.id, notebookPath: notebook.path, pageID: pages[index].id)
    }

    public func addPage(after index: Int?) {
        let after = index.flatMap { pages.indices.contains($0) ? pages[$0].id : nil }
        _ = try? controller.store.createPage(notebookID: notebook.id, after: after)
        reload()
        onPagesChanged?()
        if let after, let position = pages.firstIndex(where: { $0.id == after }) {
            currentIndex = min(position + 1, pages.count - 1)
        } else {
            currentIndex = max(pages.count - 1, 0)
        }
        onScrollTo?(currentIndex)
    }

    public func deletePage(at index: Int) {
        guard pages.indices.contains(index), pages.count > 1 else { return }
        let page = pages[index]
        let thumbnail = controller.store.paths.render(notebook.path, page.id)
        guard let token = try? controller.store.deletePage(notebookID: notebook.id, pageID: page.id)
        else { return }
        reload()
        onPagesChanged?()
        pendingUndo = UndoToastModel(
            message: "Page \(index + 1) deleted", token: token,
            thumbnailURL: FileManager.default.fileExists(
                atPath: thumbnail.path(percentEncoded: false)) ? thumbnail : nil)
    }

    public func movePage(from source: Int, to destination: Int) {
        guard pages.indices.contains(source) else { return }
        var order = pages.map(\.id)
        let moved = order.remove(at: source)
        order.insert(moved, at: min(max(destination, 0), order.count))
        try? controller.store.reorderPages(notebookID: notebook.id, order: order)
        reload()
        onPagesChanged?()
    }

    public func undoPendingDelete() {
        guard let pending = pendingUndo else { return }
        controller.restore(pending.token)
        pendingUndo = nil
        reload()
        onPagesChanged?()
    }

    /// Saves the reading position. Called on a 500ms debounce by the canvas and
    /// once more when the screen goes away.
    public func persistViewState(offset: CGPoint) {
        let state = NotebookViewState(
            lastPageID: pages.indices.contains(currentIndex) ? pages[currentIndex].id : nil,
            zoom: zoom, offset: offset)
        try? controller.store.setViewState(state, notebookID: notebook.id)
    }
}

/// What a six-second undo toast needs to know.
public struct UndoToastModel: Identifiable, Equatable {
    public let id = UUID()
    public let message: String
    public let token: TrashToken
    public var thumbnailURL: URL?

    public init(message: String, token: TrashToken, thumbnailURL: URL? = nil) {
        self.message = message
        self.token = token
        self.thumbnailURL = thumbnailURL
    }
}

/// The set of pages with unflushed ink, readable from the render queue.
/// A tiny locked box rather than an actor: the renderer's check happens once
/// per job and must not suspend.
final class DirtyPageSet: @unchecked Sendable {
    private let lock = NSLock()
    private var ids: Set<String> = []

    func insert(_ id: String) {
        lock.lock()
        ids.insert(id)
        lock.unlock()
    }

    func remove(_ id: String) {
        lock.lock()
        ids.remove(id)
        lock.unlock()
    }

    func contains(_ id: String) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return ids.contains(id)
    }
}


/// A weak handle on the controller for the sync engine's callbacks.
///
/// The engine is owned by the controller, so a strong callback would be a
/// retain cycle; the value is only ever touched on the main actor, which is
/// what makes the unchecked conformance honest.
final class WeakPaperController: @unchecked Sendable {
    weak var value: PaperController?

    init(_ value: PaperController) {
        self.value = value
    }
}
