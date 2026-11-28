import PaperCore
import PencilKit
import PhotosUI
import SwiftUI
import UIKit

/// The whiteboard canvas: one board of a whiteboard at a time, a plane the
/// fingers pan and pinch from the zoom floor to 400 % and the Pencil writes
/// on, over its paper and grid.
///
/// The board is drawn by `SingleCanvasBoardHost` (`BoardCanvasHost.swift`):
/// one `PKCanvasView` that is its own scroll view. Its page is a `PageView` in
/// board mode, so selection, the lasso, text, images and shapes are the
/// notebook's own, and the selection gestures take the notebook's path
/// (`PageSelectionHost`). Saving, rendering, sync and export are the
/// notebook's too: a board is a page. The zoom pill, the minimap and the
/// limit message sit at the bottom left.
///
/// It takes exactly what `NotebookViewController` takes, so whatever hosts
/// one can host the other.
public final class BoardViewController: UIViewController {
    let session: NotebookSession
    var controller: PaperController { session.controller }
    var tools: ToolState { session.tools }
    let onNavigateOnWeb: ((String) -> Void)?
    let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    let host = SingleCanvasBoardHost()
    var canvas: BoardCanvasView { host.canvas }
    /// The board showing.
    private(set) var page: PageView?

    let zoomPill = ZoomPill()
    let minimap = MinimapView()
    let limitBanner = BoardLimitBanner()

    /// A one-finger double-tap: twice the zoom about the tap, and from 400 %
    /// back to 100 % (§4.7).
    let zoomDoubleTap = UITapGestureRecognizer()
    /// A one-finger tap once the double-tap has failed: it selects.
    let objectTap = UITapGestureRecognizer()
    let undoTap = UITapGestureRecognizer()
    let redoTap = UITapGestureRecognizer()
    /// The Pencil's lasso, and a finger's on the selection.
    private(set) var lassoGesture: LassoGestureRecognizer?
    private(set) var fingerLassoGesture: LassoGestureRecognizer?
    weak var activeLasso: LassoOverlayView?

    /// True between `DidBeginUsingTool` and `DidEndUsingTool`.
    private(set) var isDrawing = false
    var drawShapeStart: (pageID: String, strokeCount: Int)?

    private(set) var hasPositioned = false
    private var lastViewport: CGSize = .zero
    /// The canvas point in the middle of the screen, kept so that a rotation
    /// or the sidebar opening leaves the same place in the middle.
    private var middle: CGPoint?
    /// Where each board was left this session, for coming back to it.
    private var viewports: [String: (zoom: CGFloat, center: CGPoint)] = [:]
    private var persistWork: DispatchWorkItem?

    private var drawingProviderID: UUID?
    private var localPageObserverID: UUID?
    private var toolObserverID: UUID?
    /// This window edited the board since its own last write of it.
    private var locallyDirty = false
    /// Another window changed the board while a stroke was in progress.
    private var changedUnderStroke = false
    /// A board this window left whose write failed: its ink, until a write
    /// of it lands.
    private var strandedDrawings: [String: Data] = [:]

    /// `BoardLimits` for the board showing, measured at every flush.
    private(set) var limitState: BoardLimits.State = .normal
    private(set) var measuredBytes = 0
    private(set) var measuredStrokes = 0

    /// The ink encoded off the main thread after the last change, for the
    /// flush a second later to take: encoding a board of 25,000 strokes on the
    /// main thread took 436 ms in the prototype.
    private(set) var drawingGeneration = 0
    private(set) var encoded: (generation: Int, pageID: String, data: Data, strokes: Int)?
    private var encodeWork: DispatchWorkItem?
    static let encodeDelay: TimeInterval = 0.3
    #if DEBUG
        /// How long the last encode off the main thread took, in ms.
        private(set) var lastEncodeAheadMs: Double = 0
    #endif

    /// The board's ink and objects, measured half a second after an edit
    /// rather than on every frame of a pan.
    private(set) var contentBounds: CGRect?
    private var contentWork: DispatchWorkItem?
    private var isRenderingMinimap = false
    private var minimapPending = false
    /// `paper.board.minimapVisible`, true unless turned off.
    static let minimapVisibleKey = "paper.board.minimapVisible"
    private(set) var isMinimapVisible = true
    private var isExporting = false

    public init(
        session: NotebookSession,
        onNavigateOnWeb: ((String) -> Void)? = nil,
        scanDocument: (@MainActor (UIViewController) async throws -> Data)? = nil
    ) {
        self.session = session
        self.onNavigateOnWeb = onNavigateOnWeb
        self.scanDocument = scanDocument
        super.init(nibName: nil, bundle: nil)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("BoardViewController is created in code") }

    // MARK: - Lifecycle

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = PaperColor.cream.uiColor
        host.view.frame = view.bounds
        host.view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        view.addSubview(host.view)
        host.onViewportChanged = { [weak self] in self?.viewportDidChange() }
        buildChrome()
        installGestures()

        let pencil = UIPencilInteraction()
        pencil.delegate = self
        view.addInteraction(pencil)

        session.onFlush = { [weak self] in
            guard let self else { return }
            self.controller.autosave.flushAll()
            self.persistPosition()
        }
        session.onScrollTo = { [weak self] index in self?.show(index: index) }
        session.onPagesChanged = { [weak self] in self?.pagesChanged() }
        session.onReveal = { [weak self] index, box in self?.reveal(index: index, box: box) }
        session.commands = self
        ensureDrawingProviderRegistered()

        show(index: session.currentIndex)
        observeTools()
        observeReadOnly()
        applyTool()
        applyReadOnly()
    }

    public override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let size = canvas.bounds.size
        guard size.width > 0, size.height > 0, page != nil else { return }
        if !hasPositioned {
            hasPositioned = true
            lastViewport = size
            host.viewportDidResize()
            if let board = currentBoard { position(for: board) }
            // After the reading position, never before: a search result is a
            // deliberate destination.
            if let deferred = session.consumeDeferredReveal() {
                reveal(index: deferred.index, box: deferred.box)
            }
            #if DEBUG
                runDebugArguments()
            #endif
        } else if size != lastViewport {
            let centre = middle ?? host.viewportCenter
            lastViewport = size
            host.viewportDidResize()
            host.place(center: centre, zoom: host.zoom)
        }
    }

    public override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        ensureDrawingProviderRegistered()
    }

    public override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        controller.autosave.flushAll()
        persistPosition()
    }

    public override func didMove(toParent parent: UIViewController?) {
        super.didMove(toParent: parent)
        guard parent == nil else { return }
        controller.autosave.flushAll()
        unregisterDrawingProvider()
    }

    deinit {
        MainActor.assumeIsolated {
            unregisterDrawingProvider()
            if let toolObserverID { tools.removeObserver(toolObserverID) }
        }
    }

    // MARK: - Chrome

    private func buildChrome() {
        let defaults = UserDefaults.standard
        isMinimapVisible =
            defaults.object(forKey: Self.minimapVisibleKey) == nil
            || defaults.bool(forKey: Self.minimapVisibleKey)
        zoomPill.onZoom = { [weak self] request in self?.zoom(request) }
        zoomPill.onToggleMinimap = { [weak self] in self?.toggleMinimap() }
        zoomPill.setMinimapVisible(isMinimapVisible)
        minimap.onMove = { [weak self] point in self?.host.scroll(toCenter: point, animated: false) }
        minimap.onFit = { [weak self] in self?.fitContent(animated: true) }
        minimap.isHidden = !isMinimapVisible
        limitBanner.onAddBoard = { [weak self] in self?.addPage(.last, template: nil) }

        for item in [minimap, zoomPill, limitBanner] as [UIView] {
            item.translatesAutoresizingMaskIntoConstraints = false
            view.addSubview(item)
        }
        let guide = view.safeAreaLayoutGuide
        NSLayoutConstraint.activate([
            zoomPill.leadingAnchor.constraint(equalTo: guide.leadingAnchor, constant: 16),
            zoomPill.bottomAnchor.constraint(equalTo: guide.bottomAnchor, constant: -16),
            minimap.leadingAnchor.constraint(equalTo: zoomPill.leadingAnchor),
            minimap.bottomAnchor.constraint(equalTo: zoomPill.topAnchor, constant: -8),
            minimap.widthAnchor.constraint(equalToConstant: MinimapView.size.width),
            minimap.heightAnchor.constraint(equalToConstant: MinimapView.size.height),
            limitBanner.leadingAnchor.constraint(equalTo: zoomPill.trailingAnchor, constant: 8),
            limitBanner.centerYAnchor.constraint(equalTo: zoomPill.centerYAnchor),
        ])
    }

    func toggleMinimap() {
        isMinimapVisible.toggle()
        UserDefaults.standard.set(isMinimapVisible, forKey: Self.minimapVisibleKey)
        zoomPill.setMinimapVisible(isMinimapVisible)
        minimap.isHidden = !isMinimapVisible
        if isMinimapVisible {
            minimap.setImage(nil, world: .null)
            updateMinimap()
        }
    }

    // MARK: - Gestures

    private func installGestures() {
        let direct = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        zoomDoubleTap.numberOfTapsRequired = 2
        zoomDoubleTap.addTarget(self, action: #selector(zoomTapped(_:)))
        objectTap.require(toFail: zoomDoubleTap)
        objectTap.addTarget(self, action: #selector(objectTapped(_:)))
        undoTap.numberOfTouchesRequired = 2
        redoTap.numberOfTouchesRequired = 3
        for tap in [undoTap, redoTap] { tap.numberOfTapsRequired = 2 }
        undoTap.addTarget(self, action: #selector(undoTapped(_:)))
        redoTap.addTarget(self, action: #selector(redoTapped(_:)))
        // None of them holds back, ends late or cancels a touch the board
        // and its objects get.
        for tap in [zoomDoubleTap, objectTap, undoTap, redoTap] {
            tap.allowedTouchTypes = direct
            tap.cancelsTouchesInView = false
            tap.delaysTouchesEnded = false
            tap.delegate = self
            host.view.addGestureRecognizer(tap)
        }
        lassoGesture = makeLassoGesture()
        fingerLassoGesture = makeLassoGesture()
    }

    /// A lasso recogniser on the board. The loop and the drag win over the
    /// canvas's pan, always.
    private func makeLassoGesture() -> LassoGestureRecognizer {
        let gesture = LassoGestureRecognizer(target: self, action: #selector(handleLasso(_:)))
        gesture.delegate = self
        gesture.isEnabled = false
        gesture.shouldBegin = { [weak self] point, type in
            self?.lassoShouldBegin(at: point, touchType: type) ?? false
        }
        host.view.addGestureRecognizer(gesture)
        canvas.panGestureRecognizer.require(toFail: gesture)
        return gesture
    }

    @objc private func handleLasso(_ gesture: LassoGestureRecognizer) {
        switch gesture.state {
        case .began:
            let start = gesture.startLocation(in: host.view)
            routeLassoTouch(.began(start))
            let now = gesture.trackedLocation(in: host.view)
            if now != start { routeLassoTouch(.moved(now)) }
        case .changed:
            routeLassoTouch(.moved(gesture.trackedLocation(in: host.view)))
        case .ended:
            routeLassoTouch(.ended)
        case .cancelled, .failed:
            routeLassoTouch(.cancelled)
        default:
            break
        }
    }

    @objc private func objectTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended else { return }
        selectionTap(at: gesture.location(in: host.view))
    }

    @objc private func zoomTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended else { return }
        doubleTapZoom(at: gesture.location(in: host.view))
    }

    /// Twice the zoom about the tap, and from 400 % back to 100 % (a point
    /// of the screen). A double-tap on a selected object belongs to it.
    func doubleTapZoom(at point: CGPoint, animated: Bool = true) {
        guard !isDrawing, let page, !page.hasSelectedObject else { return }
        host.zoom(
            to: BoardGeometry.doubleTapZoom(host.zoom), keeping: host.canvasPoint(atScreen: point),
            at: point, animated: animated)
    }

    @objc private func undoTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended, !isDrawing else { return }
        performUndo()
    }

    @objc private func redoTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended, !isDrawing else { return }
        performRedo()
    }

    // MARK: - Boards

    var currentBoard: Page? {
        session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex] : nil
    }

    func ref(for pageView: PageView) -> PageRef? {
        session.pages.firstIndex(where: { $0.id == pageView.page.id }).flatMap(session.ref(at:))
    }

    /// Shows the board at `index`: the one before is flushed and taken off,
    /// and this one opens where it was left, or centred.
    func show(index: Int) {
        guard session.pages.indices.contains(index) else { return }
        let board = session.pages[index]
        if let page, page.page.id == board.id {
            session.currentIndex = index
            return
        }
        leaveBoard()
        session.currentIndex = index
        let view = PageView(page: board, notebookPath: session.notebook.path, isBoard: true)
        view.delegate = self
        view.onConvertToText = { [weak self, weak view] text, rect in
            guard let self, let view else { return }
            self.replaceInkWithText(text, rect: rect, on: view)
        }
        view.onAIAction = { [weak self, weak view] action, data, rect in
            guard let self, let view else { return }
            self.performSelectionAIAction(action, image: data, rect: rect, on: view)
        }
        view.onCopyLink = { [weak self, weak view] rect in
            guard let self, let view else { return }
            self.copySelectionLink(rect: rect, on: view)
        }
        let data =
            strandedDrawings[board.id]
            ?? controller.store.drawingData(notebookPath: session.notebook.path, pageID: board.id)
        page = view
        host.attach(view, drawingData: data, gestureHost: host.view)
        view.setAccessibility(index: index + 1, total: session.pages.count)
        drawingGeneration &+= 1
        encoded = nil
        measuredBytes = data?.count ?? 0
        measuredStrokes = view.drawing?.strokes.count ?? 0
        applyLimits(announce: false)
        configureTool(on: view)
        setSelectionLocked(session.chrome.isReadOnly)
        if let ref = ref(for: view) { view.setExternallyEditing(isDirtyElsewhere(ref)) }
        minimap.setPaper(board.paperColor)
        refreshContentBounds()
        if hasPositioned { position(for: board) }
        zoomPill.setZoom(host.zoom)
        refreshUndoState()
    }

    /// Flushes the board showing and takes it off the canvas.
    private func leaveBoard() {
        guard let page else { return }
        activeLasso?.handleTouchCancelled()
        activeLasso = nil
        if let ref = ref(for: page) {
            controller.autosave.flush(ref)
            // The write failed: the ink is kept here until one lands.
            if controller.autosave.isDirty(ref), let data = page.drawingData {
                strandedDrawings[ref.pageID] = data
            }
        }
        if hasPositioned { viewports[page.page.id] = (host.zoom, host.viewportCenter) }
        host.detach()
        self.page = nil
        locallyDirty = false
        changedUnderStroke = false
        encodeWork?.cancel()
        contentWork?.cancel()
    }

    /// Where a board opens: where this window left it, else where the
    /// document was left if it was on this board, else its content fitted
    /// (no closer than 100 %), else its centre at 100 %.
    private func position(for board: Page) {
        if let saved = viewports[board.id] {
            host.place(center: saved.center, zoom: saved.zoom)
            return
        }
        let state = controller.store.viewState(notebookID: session.notebook.id)
        if state.lastPageID == board.id, state.zoom > 0, state.zoom.isFinite,
            isPlausible(center: state.offset, zoom: state.zoom)
        {
            host.place(center: state.offset, zoom: state.zoom)
            return
        }
        placeDefault()
    }

    /// A stored centre is a canvas point inside the board, and shows some of
    /// what is on it. An offset written by a notebook before it was converted
    /// is often neither.
    private func isPlausible(center: CGPoint, zoom: CGFloat) -> Bool {
        let board = CGRect(origin: .zero, size: host.extent)
        guard center.x.isFinite, center.y.isFinite, board.contains(center) else { return false }
        guard let content = contentBounds else { return true }
        let shown = BoardGeometry.visibleRect(
            center: center, zoom: host.clamp(zoom), viewport: host.viewport)
        return shown.intersects(content)
    }

    private func placeDefault() {
        guard let content = contentBounds else {
            host.place(center: BoardGeometry.center(of: host.extent), zoom: 1)
            return
        }
        let fit = BoardGeometry.fitContent(content, extent: host.extent, viewport: host.viewport)
        host.place(center: fit.center, zoom: min(fit.zoom, 1))
    }

    /// The page list changed: a board added, renamed, recoloured, restored
    /// or deleted, here or elsewhere.
    private func pagesChanged() {
        guard let page else {
            show(index: session.currentIndex)
            return
        }
        guard let index = session.pages.firstIndex(where: { $0.id == page.page.id }) else {
            // The board showing went.
            leaveBoard()
            show(index: min(session.currentIndex, max(session.pages.count - 1, 0)))
            return
        }
        if let ref = ref(for: page) {
            if let fresh = try? controller.store.page(notebookPath: ref.notebookPath, id: ref.pageID) {
                page.update(page: fresh)
                minimap.setPaper(fresh.paperColor)
                host.view.backgroundColor = fresh.paperColor.uiColor
            }
            // Another device, a restored version or a conflict may have
            // rewritten the ink; not ink this canvas still holds unwritten.
            if !isDrawing, !controller.autosave.isDirty(ref) { reloadDrawing(ref) }
        }
        page.setAccessibility(index: index + 1, total: session.pages.count)
        refreshContentBounds()
    }

    /// Brings the canvas up to the ink on disk, quietly.
    private func reloadDrawing(_ ref: PageRef) {
        guard let page else { return }
        page.reloadDrawing(
            controller.store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        // The reload is quiet; whatever was encoded may no longer be what is
        // on the canvas.
        drawingGeneration &+= 1
        encoded = nil
        measuredStrokes = page.drawing?.strokes.count ?? 0
        refreshContentBounds()
    }

    /// Search is navigation: the board, and the spot on it, outlined.
    private func reveal(index: Int, box: CGRect?) {
        show(index: index)
        guard hasPositioned, let page, let box, !box.isNull, box.width.isFinite else { return }
        let viewport = host.viewport
        let fits =
            box.width * host.zoom <= viewport.width * 0.8
            && box.height * host.zoom <= viewport.height * 0.8
        let zoom =
            fits
            ? host.zoom
            : BoardGeometry.fitContent(box, extent: host.extent, viewport: viewport, margin: 96).zoom
        host.place(center: CGPoint(x: box.midX, y: box.midY), zoom: zoom)
        page.flashHighlight(box)
        UIAccessibility.post(
            notification: .announcement, argument: "Board \(index + 1), showing the match")
    }

    // MARK: - Following the view

    private func viewportDidChange() {
        guard page != nil else { return }
        zoomPill.setZoom(host.zoom)
        if canvas.bounds.size == lastViewport { middle = host.viewportCenter }
        updateMinimap()
        if hasPositioned, !host.isPlacing { schedulePersist() }
    }

    private func schedulePersist() {
        persistWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.persistPosition() }
        }
        persistWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    /// The board showing, its zoom and the canvas point in the middle of the
    /// screen (§6.2).
    func persistPosition() {
        guard hasPositioned, page != nil else { return }
        persistWork?.cancel()
        session.zoom = host.zoom
        session.persistViewState(offset: host.viewportCenter)
    }

    // MARK: - Minimap

    /// The ink and the objects, measured again half a second after an edit.
    private func scheduleContentRefresh() {
        contentWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.refreshContentBounds() }
        }
        contentWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    func refreshContentBounds() {
        guard let page else {
            contentBounds = nil
            return
        }
        contentBounds = BoardGeometry.contentBounds(
            ink: page.drawing?.bounds, objects: page.page.objects)
        // The ink changed under whatever picture there was.
        minimap.setImage(nil, world: .null)
        updateMinimap()
    }

    private func updateMinimap() {
        guard isMinimapVisible, let page else { return }
        let objects = page.page.objects.map { $0.frame.rect.standardized }
        if minimap.update(content: contentBounds, viewport: host.visibleRect, objects: objects) {
            renderMinimap()
        }
    }

    /// Draws the ink over the minimap's world off the main thread, one
    /// picture at a time; asked again meanwhile, it draws once more after.
    private func renderMinimap() {
        guard isMinimapVisible, let page else { return }
        guard !isRenderingMinimap else {
            minimapPending = true
            return
        }
        let world = minimap.map.world
        let scale = minimap.map.imageScale
        let drawing = page.drawing ?? PKDrawing()
        let dark = page.isDarkPaper
        guard !drawing.strokes.isEmpty, scale > 0, !world.isEmpty else {
            minimap.setImage(nil, world: world)
            return
        }
        isRenderingMinimap = true
        Task { @MainActor [weak self] in
            let image = await Task.detached(priority: .utility) { () -> UIImage? in
                var image: UIImage?
                UITraitCollection(userInterfaceStyle: dark ? .dark : .light).performAsCurrent {
                    image = drawing.image(from: world, scale: scale)
                }
                return image
            }.value
            guard let self else { return }
            self.isRenderingMinimap = false
            self.minimap.setImage(image, world: world)
            if self.minimapPending || self.minimap.needsImage {
                self.minimapPending = false
                self.renderMinimap()
            }
        }
    }

    // MARK: - Fit

    /// Fit Content: all the ink and objects on screen, or the centre at
    /// 100 % on an empty board.
    func fitContent(animated: Bool) {
        guard page != nil else { return }
        refreshContentBounds()
        let fit = BoardGeometry.fitContent(
            contentBounds, extent: host.extent, viewport: host.viewport)
        host.zoom(
            to: fit.zoom, keeping: fit.center,
            at: CGPoint(x: host.viewport.width / 2, y: host.viewport.height / 2),
            animated: animated)
    }

    // MARK: - Limits

    /// What the board holds, as last measured: the bytes written and the
    /// strokes, with the objects on it. A full board takes no more ink.
    func applyLimits(announce: Bool = true) {
        let state = BoardLimits.state(
            drawingBytes: measuredBytes, strokes: measuredStrokes,
            objects: page?.page.objects.count ?? 0)
        guard state != limitState || !announce else { return }
        limitState = state
        limitBanner.show(state, announce: announce)
        if let page { configureTool(on: page) }
    }

    // MARK: - Autosave source

    private func ensureDrawingProviderRegistered() {
        guard drawingProviderID == nil else { return }
        drawingProviderID = controller.addDrawingProvider { [weak self] ref in
            self?.drawingData(for: ref)
        }
        localPageObserverID = controller.observeLocalPageChanges { [weak self] event in
            self?.localPageChanged(event)
        }
    }

    private func unregisterDrawingProvider() {
        if let localPageObserverID {
            controller.removeLocalPageChangeObserver(localPageObserverID)
            self.localPageObserverID = nil
        }
        guard let drawingProviderID else { return }
        controller.removeDrawingProvider(drawingProviderID)
        self.drawingProviderID = nil
    }

    private var drawingSource: UUID? {
        ensureDrawingProviderRegistered()
        return drawingProviderID
    }

    /// The bytes autosave writes for the board showing: what was encoded off
    /// the main thread since the last change, or encoded now when that has
    /// not finished. Measured for the limits on the way.
    func drawingData(for ref: PageRef) -> Data? {
        if let stranded = strandedDrawings[ref.pageID] { return stranded }
        guard let page, page.page.id == ref.pageID, page.isLive, let drawing = page.drawing else {
            return nil
        }
        let data: Data
        let strokes: Int
        if let encoded, encoded.generation == drawingGeneration, encoded.pageID == ref.pageID {
            data = encoded.data
            strokes = encoded.strokes
        } else {
            data = drawing.dataRepresentation()
            strokes = drawing.strokes.count
            encoded = (drawingGeneration, ref.pageID, data, strokes)
        }
        measuredBytes = data.count
        measuredStrokes = strokes
        applyLimits()
        return data
    }

    /// The ink changed: encode it once the pen has rested a moment.
    private func drawingDidChange() {
        drawingGeneration &+= 1
        encodeWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.encodeAhead() }
        }
        encodeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.encodeDelay, execute: work)
        scheduleContentRefresh()
        host.onDrawingChanged?()
    }

    /// Copies the drawing here and encodes it off the main thread.
    func encodeAhead() {
        guard !isDrawing, let page, let drawing = page.drawing else { return }
        let generation = drawingGeneration
        let pageID = page.page.id
        Task { @MainActor [weak self] in
            let started = Date()
            let result = await Task.detached(priority: .userInitiated) {
                (drawing.dataRepresentation(), drawing.strokes.count)
            }.value
            guard let self else { return }
            #if DEBUG
                self.lastEncodeAheadMs = Date().timeIntervalSince(started) * 1000
            #endif
            guard generation == self.drawingGeneration, self.page?.page.id == pageID else {
                return
            }
            self.encoded = (generation, pageID, result.0, result.1)
        }
    }

    /// Every edit this window makes to the board is marked through here or
    /// `setObjects`, so `locallyDirty` knows about all of them.
    func markDirty(_ ref: PageRef) {
        controller.markPageDirty(ref, source: claim(ref))
    }

    private func setObjects(_ objects: [PageObject], on ref: PageRef) {
        controller.setObjects(objects, on: ref, source: claim(ref))
    }

    /// A board another window is writing on is only written from that
    /// window's canvas.
    private func claim(_ ref: PageRef) -> UUID? {
        if page?.isExternallyEditing == true { return nil }
        if ref.pageID == page?.page.id { locallyDirty = true }
        return drawingSource
    }

    private func isDirtyElsewhere(_ ref: PageRef) -> Bool {
        controller.autosave.isDirty(ref) && !(locallyDirty && ref.pageID == page?.page.id)
    }

    // MARK: - Other windows

    /// Two windows on one whiteboard share the store and the dirty set but
    /// each holds its own canvas: the notebook's rules, for one page.
    private func localPageChanged(_ event: LocalPageEvent) {
        switch event {
        case .dirtied(let ref, let source):
            guard source != drawingProviderID, let page, ref.pageID == page.page.id else { return }
            if isDrawing || locallyDirty {
                if isDrawing { changedUnderStroke = true }
                if let fresh = try? controller.store.page(
                    notebookPath: ref.notebookPath, id: ref.pageID)
                {
                    page.update(page: fresh)
                }
                return
            }
            page.setExternallyEditing(true)

        case .flushed(let ref, let owner):
            let mine = owner != nil && owner == drawingProviderID
            if mine { strandedDrawings[ref.pageID] = nil }
            guard let page, ref.pageID == page.page.id else { return }
            if mine { locallyDirty = false }
            if isDrawing {
                if !mine { changedUnderStroke = true }
                return
            }
            if locallyDirty {
                // Both windows wrote inside one debounce and the other's copy
                // is on disk: claim the board again so this one's lands too.
                controller.markPageDirty(ref, source: drawingSource)
                return
            }
            if !mine {
                reloadDrawing(ref)
                if let fresh = try? controller.store.page(
                    notebookPath: ref.notebookPath, id: ref.pageID)
                {
                    page.update(page: fresh)
                }
                refreshUndoState()
            }
            page.setExternallyEditing(isDirtyElsewhere(ref))
        }
    }

    private func settleAfterStroke() {
        defer { changedUnderStroke = false }
        guard changedUnderStroke, let page, !locallyDirty, let ref = ref(for: page) else { return }
        reloadDrawing(ref)
        page.setExternallyEditing(isDirtyElsewhere(ref))
    }
}

// MARK: - Selection

extension BoardViewController: PageSelectionHost {
    var selectablePages: [PageView] { page.map { [$0] } ?? [] }
    var selectionSpace: UIView { host.view }
}

extension BoardViewController: UIGestureRecognizerDelegate {
    public func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
        // The loop is exclusive: nothing else may act on the touch that is
        // drawing it, nor on the one carrying the selection.
        !(gestureRecognizer is LassoGestureRecognizer || other is LassoGestureRecognizer)
    }

    /// A tap on the object menu's buttons, or in text being typed, belongs
    /// to them: it neither selects nor zooms.
    public func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch
    ) -> Bool {
        guard gestureRecognizer === objectTap || gestureRecognizer === zoomDoubleTap else {
            return true
        }
        var view = touch.view
        while let current = view, current !== host.view {
            if current is ObjectMenu { return false }
            if let text = current as? UITextView, text.isFirstResponder { return false }
            view = current.superview
        }
        return true
    }
}

// MARK: - Read Only

extension BoardViewController {
    private func observeReadOnly() {
        withObservationTracking {
            _ = session.chrome.isReadOnly
        } onChange: { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.applyReadOnly()
                self.observeReadOnly()
            }
        }
    }

    /// Both halves of the lock at once: no ink, lasso, selection, insert or
    /// object edit, no undo taps. One finger still pans and a pinch zooms.
    func applyReadOnly() {
        let readOnly = session.chrome.isReadOnly
        if readOnly {
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
            page?.deselectObjects()
            view.endEditing(true)
            if let palette = presentedViewController as? ToolPaletteController {
                palette.dismiss(animated: false)
            }
        }
        if let page { configureTool(on: page) }
        updateLassoGesture()
        updateEditorGestures()
        setSelectionLocked(readOnly)
        refreshUndoState()
    }

    /// Finger selection, the lasso, insert taps and object editing, off or
    /// back on.
    func setSelectionLocked(_ locked: Bool) {
        objectTap.isEnabled = !locked
        if let page {
            for gesture in [page.insertTap, page.shapeDrag, page.longPress] as [UIGestureRecognizer] {
                gesture.isEnabled = !locked
            }
        }
        guard locked else { return }
        activeLasso?.handleTouchCancelled()
        activeLasso = nil
        page?.objects.cancelInteraction()
        page?.putDownSelection()
        view.endEditing(true)
    }

    func updateEditorGestures() {
        let readOnly = session.chrome.isReadOnly
        undoTap.isEnabled = !readOnly
        redoTap.isEnabled = !readOnly
    }
}

// MARK: - Tools

extension BoardViewController {
    private func observeTools() {
        guard toolObserverID == nil else { return }
        toolObserverID = tools.addObserver { [weak self] old, new in
            self?.toolsDidChange(from: old, to: new)
        }
    }

    private func toolsDidChange(from old: ToolSettings, to new: ToolSettings) {
        if old.canvasTool != new.canvasTool { host.setTool(tools.pkTool) }
        guard old.active != new.active else { return }
        if let page { configureTool(on: page) }
        updateLassoGesture()
        updateEditorGestures()
        if new.active == .image { pickImageForTool() }
    }

    func applyTool() {
        host.setTool(tools.pkTool)
        if let page { configureTool(on: page) }
        updateLassoGesture()
        updateEditorGestures()
    }

    /// Whether the Pencil inks now: a writing tool or the eraser, not in Read
    /// Only, and no writing tool on a full board -- the eraser still erases.
    var inkAllowed: Bool {
        let active = tools.active
        guard !session.chrome.isReadOnly, !active.isInsert, active != .lasso else { return false }
        return limitState.allowsInking || active == .eraser
    }

    /// The board's share of the tool: the lasso overlay, and whether its
    /// canvas inks. Picking another tool puts a selection down, through
    /// `setLassoActive`, as on a page.
    func configureTool(on view: PageView) {
        let readOnly = session.chrome.isReadOnly
        view.setLassoActive(tools.active == .lasso && !readOnly)
        view.allowsInk = inkAllowed
        view.isUserInteractionEnabled = !readOnly
    }

    func updateLassoGesture() {
        let lasso = tools.active == .lasso && !session.chrome.isReadOnly
        if !lasso {
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
        }
        let pencil = NSNumber(value: UITouch.TouchType.pencil.rawValue)
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)
        lassoGesture?.isEnabled = lasso
        lassoGesture?.allowedTouchTypes = EditorInput.fingerInk ? [direct, pencil] : [pencil]
        fingerLassoGesture?.isEnabled = lasso && !EditorInput.fingerInk
        fingerLassoGesture?.allowedTouchTypes = [direct]
    }

    /// Undo and redo for the chrome, written only when they change.
    func refreshUndoState() {
        let manager = page?.canvas?.undoManager
        let readOnly = session.chrome.isReadOnly
        let canUndo = !readOnly && (manager?.canUndo ?? false)
        let canRedo = !readOnly && (manager?.canRedo ?? false)
        if session.chrome.canUndo != canUndo { session.chrome.canUndo = canUndo }
        if session.chrome.canRedo != canRedo { session.chrome.canRedo = canRedo }
    }

    func performUndo() {
        guard !session.chrome.isReadOnly else { return }
        page?.canvas?.undoManager?.undo()
        historyDidMove()
    }

    func performRedo() {
        guard !session.chrome.isReadOnly else { return }
        page?.canvas?.undoManager?.redo()
        historyDidMove()
    }

    /// A lasso selection is a list of stroke positions; after an undo it need
    /// not describe the board any more.
    private func historyDidMove() {
        page?.lasso?.clearSelection()
        refreshUndoState()
    }

    /// The Image tool is the photo picker; the toolbar is back on the lasso
    /// as soon as it is up.
    private func pickImageForTool() {
        if view.window?.isKeyWindow == true, presentedViewController == nil,
            !session.chrome.isReadOnly
        {
            presentPhotoPicker()
        }
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.tools.finishInsert() }
        }
    }

    // MARK: Draw Shape and replacing ink

    private func drawShapeStrokeBegan(on view: PageView) {
        guard tools.active == .drawShape, let canvas = view.canvas else {
            drawShapeStart = nil
            return
        }
        drawShapeStart = (view.page.id, canvas.drawing.strokes.count)
    }

    /// One run-loop turn after the lift, once PencilKit has committed the
    /// stroke and its own undo, the clean shape replaces it as an undo step
    /// of its own.
    private func drawShapeStrokeEnded(on view: PageView) {
        guard let start = drawShapeStart, start.pageID == view.page.id else { return }
        drawShapeStart = nil
        DispatchQueue.main.async { [weak self, weak view] in
            MainActor.assumeIsolated {
                guard let self, let view else { return }
                self.cleanUpShape(on: view, strokesBefore: start.strokeCount)
            }
        }
    }

    @discardableResult
    func cleanUpShape(on view: PageView, strokesBefore count: Int) -> RecognizedShape? {
        guard let canvas = view.canvas, !view.isExternallyEditing else { return nil }
        let drawing = canvas.drawing
        guard drawing.strokes.count == count + 1, let rough = drawing.strokes.last else {
            return nil
        }
        let points = rough.path.interpolatedPoints(by: .distance(2)).map {
            $0.location.applying(rough.transform)
        }
        guard let shape = ShapeRecognizer.recognize(points: points, zoom: host.zoom) else {
            return nil
        }
        let first = rough.path.first
        let size = first?.size ?? CGSize(width: 2, height: 2)
        let clean = shape.splinePaths().map { path in
            PKStroke(
                ink: rough.ink,
                path: PKStrokePath(
                    controlPoints: path.enumerated().map { index, point in
                        PKStrokePoint(
                            location: point, timeOffset: Double(index) * 0.005, size: size,
                            opacity: first?.opacity ?? 1, force: first?.force ?? 1,
                            azimuth: first?.azimuth ?? 0, altitude: first?.altitude ?? .pi / 2)
                    },
                    creationDate: rough.path.creationDate))
        }
        var strokes = drawing.strokes
        strokes.removeLast()
        strokes.append(contentsOf: clean)
        replaceInk(on: view, with: PKDrawing(strokes: strokes), actionName: "Draw Shape")
        UIAccessibility.post(notification: .announcement, argument: shape.kind.rawValue.capitalized)
        return shape
    }

    /// Every stroke on the board goes, as one undo step. Objects stay.
    func clearInk() {
        guard let page, let canvas = page.canvas, !page.isExternallyEditing,
            !canvas.drawing.strokes.isEmpty
        else { return }
        page.lasso?.clearSelection()
        replaceInk(on: page, with: PKDrawing(), actionName: "Clear Board")
    }

    /// Replaces the ink as a user edit: reported like a stroke, so it is
    /// saved, and undone by putting the old ink back.
    func replaceInk(on view: PageView, with drawing: PKDrawing, actionName: String) {
        guard let canvas = view.canvas else { return }
        registerInkRestore(of: canvas.drawing, on: canvas, actionName: actionName)
        canvas.drawing = drawing
    }

    private func registerInkRestore(
        of drawing: PKDrawing, on canvas: PKCanvasView, actionName: String
    ) {
        guard let manager = canvas.undoManager else { return }
        manager.registerUndo(withTarget: canvas) { [weak self] target in
            MainActor.assumeIsolated {
                let replaced = target.drawing
                self?.page?.lasso?.clearSelection()
                target.drawing = drawing
                self?.registerInkRestore(of: replaced, on: target, actionName: actionName)
            }
        }
        manager.setActionName(actionName)
    }

    /// A selection was put down; the Text tool is done with its box.
    private func selectionWasPutDown() {
        if tools.active == .text { tools.finishInsert() }
    }
}

// MARK: - The board's page

extension BoardViewController: PageViewDelegate {
    func pageViewDrawingChanged(_ pageView: PageView) {
        guard pageView === page, let ref = ref(for: pageView) else { return }
        markDirty(ref)
        drawingDidChange()
        refreshUndoState()
    }

    func pageViewDidBeginUsingTool(_ pageView: PageView) {
        isDrawing = true
        // A palm or a resting finger that lands mid-stroke must not move the
        // board under the pen.
        canvas.panGestureRecognizer.isEnabled = false
        canvas.pinchGestureRecognizer?.isEnabled = false
        zoomDoubleTap.isEnabled = false
        drawShapeStrokeBegan(on: pageView)
    }

    func pageViewDidEndUsingTool(_ pageView: PageView) {
        isDrawing = false
        canvas.panGestureRecognizer.isEnabled = true
        canvas.pinchGestureRecognizer?.isEnabled = true
        zoomDoubleTap.isEnabled = true
        drawShapeStrokeEnded(on: pageView)
        settleAfterStroke()
        refreshUndoState()
    }

    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }

    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {
        refreshUndoState()
        if isEmpty { selectionWasPutDown() }
    }

    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {
        guard let ref = ref(for: pageView) else { return }
        setObjects(objects, on: ref)
        session.reload()
        refreshUndoState()
        applyLimits()
        scheduleContentRefresh()
    }

    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? {
        controller.assetImage(assetPath, notebookPath: session.notebook.path)
    }

    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {
        guard !session.chrome.isReadOnly else { return }
        insertImage(data: data, name: name, at: point)
    }

    /// A PDF is pages, and a board has none to give it.
    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {}

    func pageView(_ pageView: PageView, didSelect kind: ObjectMenuKind) {
        if tools.active != .lasso { tools.select(.lasso) }
    }

    func pageView(_ pageView: PageView, didPlace type: PageObject.Kind) {
        if type == .shape { tools.finishInsert() }
    }

    func pageViewIsReadOnly(_ pageView: PageView) -> Bool { session.chrome.isReadOnly }

    func pageView(_ pageView: PageView, pasteAt point: CGPoint) {
        paste(at: point)
    }

    func pageView(_ pageView: PageView, share image: UIImage, from rect: CGRect) {
        let sheet = UIActivityViewController(activityItems: [image], applicationActivities: nil)
        if let popover = sheet.popoverPresentationController {
            popover.sourceView = pageView
            popover.sourceRect = rect
        }
        present(sheet, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func pageView(
        _ pageView: PageView, render drawing: PKDrawing, objects: [PageObject], in rect: CGRect
    ) -> UIImage? {
        renderPicture(of: drawing, objects: objects, in: rect, on: pageView)
    }
}

// MARK: - Editor commands

extension BoardViewController: EditorCommands {
    public func undo() { performUndo() }

    public func redo() { performRedo() }

    /// Add Board adds one at the end and shows it, centred at 100 %.
    public func addPage(_ position: PagePosition, template: Template?) {
        guard !session.chrome.isReadOnly else { return }
        let existing = Set(session.pages.map(\.id))
        switch position {
        case .before where session.currentIndex == 0:
            session.addPage(after: nil)
            session.movePage(from: session.pages.count - 1, to: 0)
            show(index: 0)
        case .before:
            session.addPage(after: session.currentIndex - 1)
        case .after:
            session.addPage(after: session.currentIndex)
        case .last:
            session.addPage(after: session.pages.count - 1)
        }
        guard let template,
            let index = session.pages.firstIndex(where: { !existing.contains($0.id) })
        else { return }
        setPageStyle(template: template, paperColor: nil, at: index)
    }

    /// The board showing, as one undo step; another one on disk, with its ink
    /// kept in its snapshots. Objects stay.
    public func clearPage(at index: Int) {
        guard !session.chrome.isReadOnly, session.pages.indices.contains(index) else { return }
        if session.pages[index].id == page?.page.id {
            clearInk()
            return
        }
        guard let ref = session.ref(at: index),
            (try? controller.store.clearPage(notebookID: ref.notebookID, pageID: ref.pageID)) != nil
        else { return }
        markDirty(ref)
    }

    /// A board's background (Blank, Square Grid, Dot Grid), its paper colour,
    /// or both.
    public func setPageStyle(template: Template?, paperColor: PaperColor?, at index: Int) {
        guard !session.chrome.isReadOnly, template != nil || paperColor != nil,
            let ref = session.ref(at: index),
            let fresh = try? controller.store.setPageStyle(
                notebookID: ref.notebookID, pageID: ref.pageID, template: template,
                paperColor: paperColor)
        else { return }
        markDirty(ref)
        session.reload()
        guard let page, page.page.id == ref.pageID else { return }
        page.update(page: fresh)
        host.view.backgroundColor = fresh.paperColor.uiColor
        minimap.setPaper(fresh.paperColor)
        refreshContentBounds()
    }

    public func export(_ scope: ExportScope, from source: UIView?) {
        presentExport(scope, from: source ?? zoomPill)
    }

    /// Photo and Camera place a picture on the board; a PDF or a scan is
    /// pages, which a board has none of.
    public func presentInsert(_ kind: InsertKind, from source: UIView?) {
        guard !session.chrome.isReadOnly else { return }
        switch kind {
        case .photo: presentPhotoPicker()
        case .camera: presentCamera()
        case .files, .pdf, .scan: break
        }
    }

    /// Fit Content and Cmd-9; a percentage, Cmd-0 being 100 %; Cmd-+ and
    /// Cmd-minus by a factor of 1.25. About the middle of the screen, and
    /// never under a stroke in progress.
    public func zoom(_ request: ZoomRequest) {
        zoom(request, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func zoom(_ request: ZoomRequest, animated: Bool) {
        guard !isDrawing, page != nil else { return }
        let centre = host.viewportCenter
        switch request {
        case .fitContent, .fitWidth:
            fitContent(animated: animated)
        case .percent(let percent):
            host.setZoom(CGFloat(percent / 100), anchor: centre, animated: animated)
        case .stepIn:
            host.setZoom(BoardGeometry.steppedZoom(host.zoom, by: 1), anchor: centre, animated: animated)
        case .stepOut:
            host.setZoom(
                BoardGeometry.steppedZoom(host.zoom, by: -1), anchor: centre, animated: animated)
        }
    }

    /// Today's Ask flow, with what is on screen of the board as the picture
    /// and the visible rectangle, in board points, as the region.
    public func askOMNIA(from source: UIView?) {
        guard let page, let region = visibleRegion() else {
            UIAccessibility.post(notification: .announcement, argument: "Nothing to send")
            return
        }
        performSelectionAIAction(.ask, image: region.image, rect: region.rect, on: page)
    }

    public func setReadOnly(_ readOnly: Bool) {
        session.chrome.isReadOnly = readOnly
        applyReadOnly()
    }

    /// What is on screen: its picture at the screen's scale (2x at most), and
    /// where it is on the board.
    func visibleRegion() -> (rect: CGRect, image: Data)? {
        host.view.layoutIfNeeded()
        let bounds = host.view.bounds
        let rect = host.visibleRect.intersection(CGRect(origin: .zero, size: host.extent))
        guard bounds.width >= 1, bounds.height >= 1, !rect.isNull, !rect.isEmpty else { return nil }
        let format = UIGraphicsImageRendererFormat()
        format.scale = min(view.window?.screen.scale ?? 2, 2)
        format.opaque = true
        let board = host.view
        let data = UIGraphicsImageRenderer(bounds: bounds, format: format).pngData { _ in
            board.drawHierarchy(in: bounds, afterScreenUpdates: false)
        }
        return (rect, data)
    }
}

// MARK: - Export

extension BoardViewController {
    /// This board as a one-page PDF over its render rectangle, or the whole
    /// whiteboard, one page per board.
    func presentExport(_ scope: ExportScope, from source: UIView) {
        guard !isExporting else { return }
        let notebook = session.notebook
        let board = currentBoard
        if scope == .currentPage, board == nil { return }
        isExporting = true
        UIAccessibility.post(notification: .announcement, argument: "Exporting")
        Task { @MainActor [weak self] in
            guard let self else { return }
            var url: URL?
            switch scope {
            case .currentPage:
                if let board { url = await self.exportBoardPDF(board: board, notebook: notebook) }
            case .all:
                url = await self.controller.exportPDF(notebook: notebook)
            }
            self.isExporting = false
            guard let url else {
                UIAccessibility.post(notification: .announcement, argument: "Export failed")
                return
            }
            guard self.viewIfLoaded?.window != nil, self.presentedViewController == nil else {
                return
            }
            let activity = UIActivityViewController(activityItems: [url], applicationActivities: nil)
            activity.popoverPresentationController?.sourceView = source
            activity.popoverPresentationController?.sourceRect = source.bounds
            self.present(activity, animated: !UIAccessibility.isReduceMotionEnabled)
        }
    }

    /// One board as a one-page PDF, named after the whiteboard and the board.
    /// The flush comes first so the export has the last stroke.
    func exportBoardPDF(board: Page, notebook: Notebook) async -> URL? {
        controller.autosave.flushAll()
        let store = controller.store
        let fresh = (try? store.page(notebookPath: notebook.path, id: board.id)) ?? board
        let number = (session.pages.firstIndex(where: { $0.id == board.id }) ?? 0) + 1
        let label = (fresh.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let name = "\(notebook.title) " + (label.isEmpty ? "board \(number)" : label)
        return await Task.detached(priority: .userInitiated) {
            guard
                let data = try? PDFExporter.pdf([
                    PDFExporter.job(for: fresh, notebook: notebook, store: store)
                ])
            else { return nil }
            return try? PDFExporter.temporaryFile(named: name, extension: "pdf", data: data)
        }.value
    }
}

// MARK: - Insert and paste

extension BoardViewController {
    /// A text box at a board point, typing at once.
    func insertTextObject(text: String = "", at point: CGPoint? = nil) {
        guard let page else { return }
        page.insertObject(page.textObject(at: point ?? page.insertionPoint, text: text))
        UIAccessibility.post(notification: .layoutChanged, argument: nil)
    }

    /// Stores the bytes as an asset and puts the picture on the board, fitted
    /// to what is on screen.
    func insertImage(data: Data, name: String, at point: CGPoint? = nil) {
        guard let page, let image = UIImage(data: data) else { return }
        guard
            let assetPath = controller.storeAsset(
                data, name: name, notebookPath: session.notebook.path)
        else {
            presentImageNotAdded(tooLarge: data.count > AssetStore.maxBytes)
            return
        }
        let centre = point ?? page.insertionPoint
        let pixels = CGSize(
            width: image.size.width * image.scale, height: image.size.height * image.scale)
        let frame = ImageObjectView.fittedFrame(
            pixels: pixels, pageSize: host.visibleRect.size, centre: centre)
        page.insertObject(PageObject(type: .image, frame: frame, asset: assetPath))
    }

    private func importImage(_ image: UIImage, name: String, at point: CGPoint? = nil) {
        guard let cgImage = image.cgImage else {
            presentImageNotAdded(tooLarge: false)
            return
        }
        let orientation = image.imageOrientation
        let scale = image.scale
        Task { @MainActor [weak self] in
            let normalized = await Task.detached(priority: .userInitiated) {
                ImageImport.normalized(cgImage: cgImage, orientation: orientation, scale: scale)
            }.value
            guard let self else { return }
            guard let normalized else {
                self.presentImageNotAdded(tooLarge: false)
                return
            }
            self.insertImage(
                data: normalized.data, name: "\(name).\(normalized.extension)", at: point)
        }
    }

    private func presentImageNotAdded(tooLarge: Bool) {
        let cap = AssetStore.maxBytes / (1024 * 1024)
        let alert = UIAlertController(
            title: "Image not added",
            message: tooLarge
                ? "Images larger than \(cap) MB cannot be added." : "The image could not be saved.",
            preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentPhotoPicker() {
        var configuration = PHPickerConfiguration()
        configuration.filter = .images
        configuration.selectionLimit = 1
        let picker = PHPickerViewController(configuration: configuration)
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentCamera() {
        guard UIImagePickerController.isSourceTypeAvailable(.camera) else { return }
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    /// Paper's own copy comes back as ink and objects, selected; another
    /// picture as an image; anything else as text. `point` is in board
    /// points: the long press's Paste lands there, Cmd-V a little down and
    /// right of where it was copied.
    func paste(at point: CGPoint?) {
        guard !session.chrome.isReadOnly, let page, !page.isExternallyEditing else { return }
        if let clip = PaperClip.read() {
            paste(clip, onto: page, at: point)
            return
        }
        let board = UIPasteboard.general
        if board.hasImages, let image = board.image {
            importImage(image, name: "pasted", at: point)
        } else if board.hasStrings, let text = board.string, !text.isEmpty {
            insertTextObject(text: text, at: point)
        }
    }

    private func paste(_ clip: PaperClip, onto view: PageView, at point: CGPoint?) {
        guard let canvas = view.canvas else { return }
        let bounds = clip.bounds
        guard !bounds.isNull else { return }
        let offset =
            point.map { CGSize(width: $0.x - bounds.midX, height: $0.y - bounds.midY) }
            ?? CGSize(width: 16, height: 16)
        view.putDownSelection()
        var objects = clip.objects.map { object -> PageObject in
            var copy = object
            copy.id = PaperID.make()
            copy.frame.x += Double(offset.width)
            copy.frame.y += Double(offset.height)
            return copy
        }
        if clip.notebookPath != session.notebook.path {
            for index in objects.indices where objects[index].type == .image {
                guard let asset = objects[index].asset,
                    let data = controller.store.assets.data(
                        forAssetPath: asset, inNotebook: clip.notebookPath),
                    let stored = controller.storeAsset(
                        data, name: (asset as NSString).lastPathComponent,
                        notebookPath: session.notebook.path)
                else { continue }
                objects[index].asset = stored
            }
        }
        if !objects.isEmpty {
            view.objects.commit(view.objects.objects + objects, actionName: "Paste")
        }
        var strokes: [Int] = []
        if let data = clip.drawing, let ink = try? PKDrawing(data: data), !ink.strokes.isEmpty {
            let move = CGAffineTransform(translationX: offset.width, y: offset.height)
            let pasted = ink.strokes.map { stroke -> PKStroke in
                var copy = stroke
                copy.transform = copy.transform.concatenating(move)
                return copy
            }
            let first = canvas.drawing.strokes.count
            replaceInk(
                on: view, with: PKDrawing(strokes: canvas.drawing.strokes + pasted),
                actionName: "Paste")
            strokes = Array(first..<(first + pasted.count))
        }
        if tools.active != .lasso { tools.select(.lasso) }
        if strokes.isEmpty, objects.count == 1, let only = objects.first {
            view.selectObject(only.id)
        } else {
            view.lasso?.select(strokes: strokes, objects: objects.map(\.id))
        }
        UIAccessibility.post(notification: .announcement, argument: "Pasted")
    }
}

extension BoardViewController: PHPickerViewControllerDelegate {
    public func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
        guard let provider = results.first?.itemProvider,
            provider.canLoadObject(ofClass: UIImage.self)
        else { return }
        let name = provider.suggestedName ?? "photo"
        // Already off the main actor here, so the image is made upright and
        // encoded before the hop back.
        provider.loadObject(ofClass: UIImage.self) { [weak self] object, _ in
            guard let image = object as? UIImage, let cgImage = image.cgImage else { return }
            let normalized = ImageImport.normalized(
                cgImage: cgImage, orientation: image.imageOrientation, scale: image.scale)
            Task { @MainActor in
                guard let self, let normalized else { return }
                self.insertImage(data: normalized.data, name: "\(name).\(normalized.extension)")
            }
        }
    }
}

extension BoardViewController: UIImagePickerControllerDelegate, UINavigationControllerDelegate {
    public func imagePickerController(
        _ picker: UIImagePickerController,
        didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]
    ) {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
        guard let image = info[.originalImage] as? UIImage else { return }
        importImage(image, name: "photo")
    }

    public func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
        picker.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
    }
}

// MARK: - Apple Pencil

extension BoardViewController: UIPencilInteractionDelegate {
    /// Double-tap follows the system preference, as on a page.
    public func pencilInteraction(
        _ interaction: UIPencilInteraction, didReceiveTap tap: UIPencilInteraction.Tap
    ) {
        switch UIPencilInteraction.preferredTapAction {
        case .switchEraser: tools.toggleEraser()
        case .switchPrevious: tools.switchToPrevious()
        case .showColorPalette, .showInkAttributes, .showContextualPalette:
            presentToolMenu(at: tap.hoverPose?.location)
        case .ignore, .runSystemShortcut: return
        @unknown default: return
        }
    }

    public func pencilInteraction(
        _ interaction: UIPencilInteraction, didReceiveSqueeze squeeze: UIPencilInteraction.Squeeze
    ) {
        guard squeeze.phase == .ended else { return }
        switch UIPencilInteraction.preferredSqueezeAction {
        case .ignore, .runSystemShortcut: return
        default: presentToolMenu(at: squeeze.hoverPose?.location)
        }
    }
}

extension BoardViewController: UIPopoverPresentationControllerDelegate {
    /// The tool menu in a popover at a point of this view, or the docked menu
    /// back when there is no point. A second call closes it.
    func presentToolMenu(at point: CGPoint?) {
        if let open = presentedViewController as? ToolPaletteController {
            open.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
            return
        }
        guard !session.chrome.isReadOnly else { return }
        guard let point, presentedViewController == nil else {
            tools.setMenuVisible(true)
            return
        }
        let palette = ToolPaletteController(
            rootView: ToolMenuView(
                tools: tools, style: .popover, isDarkPaper: page?.isDarkPaper ?? false,
                onClearPage: { [weak self] in self?.clearInk() }))
        palette.sizingOptions = .preferredContentSize
        palette.modalPresentationStyle = .popover
        palette.view.backgroundColor = UIColor(PaperTokens.floatingPill)
        if let popover = palette.popoverPresentationController {
            popover.sourceView = view
            popover.sourceRect = CGRect(x: point.x - 2, y: point.y - 2, width: 4, height: 4)
            popover.permittedArrowDirections = [.up, .down]
            popover.backgroundColor = UIColor(PaperTokens.floatingPill)
            popover.delegate = self
        }
        present(palette, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    public func adaptivePresentationStyle(
        for controller: UIPresentationController, traitCollection: UITraitCollection
    ) -> UIModalPresentationStyle {
        .none
    }
}

// MARK: - Keyboard

extension BoardViewController {
    public override var canBecomeFirstResponder: Bool { true }

    public override var keyCommands: [UIKeyCommand]? {
        var commands = [
            command("=", [.command], #selector(commandZoomIn), "Zoom In"),
            command("+", [.command], #selector(commandZoomIn), "Zoom In"),
            command("-", [.command], #selector(commandZoomOut), "Zoom Out"),
            command("0", [.command], #selector(commandActualSize), "Zoom to 100%"),
            command("9", [.command], #selector(commandFit), "Fit Content"),
            command("]", [.command], #selector(commandNextBoard), "Next board"),
            command("[", [.command], #selector(commandPreviousBoard), "Previous board"),
            command("p", [.command], #selector(commandExport), "Export"),
            command("c", [.command], #selector(commandCopy), "Copy"),
        ]
        // Read Only keeps the keys that only move about, copy and share.
        guard !session.chrome.isReadOnly else { return commands }
        commands += [
            command("z", [.command], #selector(commandUndo), "Undo"),
            command("z", [.command, .shift], #selector(commandRedo), "Redo"),
            command("n", [.command, .shift], #selector(commandNewBoard), "New board"),
            command("x", [.command], #selector(commandCut), "Cut"),
            command("v", [.command], #selector(commandPaste), "Paste"),
            command(UIKeyCommand.inputEscape, [], #selector(commandEscape), "Clear selection"),
            command("\u{8}", [], #selector(commandDeleteSelection), "Delete selection"),
        ]
        return commands
    }

    private func command(
        _ input: String, _ modifiers: UIKeyModifierFlags, _ action: Selector, _ title: String
    ) -> UIKeyCommand {
        let command = UIKeyCommand(input: input, modifierFlags: modifiers, action: action)
        command.discoverabilityTitle = title
        command.wantsPriorityOverSystemBehavior = true
        return command
    }

    @objc private func commandZoomIn() { zoom(.stepIn) }
    @objc private func commandZoomOut() { zoom(.stepOut) }
    @objc private func commandActualSize() { zoom(.percent(100)) }
    @objc private func commandFit() { zoom(.fitContent) }
    @objc private func commandNextBoard() { session.show(pageAt: session.currentIndex + 1) }
    @objc private func commandPreviousBoard() { session.show(pageAt: session.currentIndex - 1) }
    @objc private func commandExport() { presentExport(.all, from: zoomPill) }
    @objc private func commandUndo() { performUndo() }
    @objc private func commandRedo() { performRedo() }
    @objc private func commandNewBoard() { addPage(.last, template: nil) }
    @objc private func commandCopy() { copySelectionFromKeyboard() }
    @objc private func commandCut() { cutSelectionFromKeyboard() }
    @objc private func commandPaste() {
        if forwardToTextBeingEdited(#selector(UIResponderStandardEditActions.paste(_:))) { return }
        paste(at: nil)
    }
    @objc private func commandEscape() { putDownSelectionFromKeyboard() }
    @objc private func commandDeleteSelection() {
        guard let page else { return }
        if page.hasSelectedObject {
            page.objects.deleteSelected()
        } else {
            page.lasso?.deleteSelection()
        }
    }
}

// MARK: - Debug

#if DEBUG
    extension BoardViewController {
        /// The simulator has no Pencil and `simctl` cannot drag, so a script
        /// reaches a board through these, run once the board is placed:
        ///
        /// - `-debugBoardStrokes [n]` writes a block of handwriting round the
        ///   centre and three clusters thousands of points away, for Fit
        ///   Content and the minimap; with `n`, n more short strokes beside
        ///   them, for the limits.
        /// - `-debugBoardZoom <percent>` shows the board's centre at that zoom.
        /// - `-debugBoardSaveBench <n,n,...>` times a save at each count of
        ///   strokes, with the ink encoded ahead and on the main thread.
        func runDebugArguments() {
            let arguments = ProcessInfo.processInfo.arguments
            func value(after flag: String) -> String? {
                guard let index = arguments.firstIndex(of: flag) else { return nil }
                let next = index + 1
                return arguments.indices.contains(next) && !arguments[next].hasPrefix("-")
                    ? arguments[next] : ""
            }
            if let strokes = value(after: "-debugBoardStrokes") {
                debugWriteStrokes(extra: Int(strokes) ?? 0)
            }
            if let percent = value(after: "-debugBoardZoom").flatMap(Double.init) {
                host.place(center: BoardGeometry.center(of: host.extent), zoom: CGFloat(percent / 100))
                print("[paper] board zoom \(BoardGeometry.zoomLabel(host.zoom))")
            }
            if let counts = value(after: "-debugBoardSaveBench") {
                let list = counts.split(separator: ",").compactMap { Int($0) }
                Task { @MainActor in await self.debugSaveBench(list.isEmpty ? [15_000, 25_000] : list) }
            }
        }

        /// A wavy stroke, like a written word, `width` long at `origin`.
        static func debugWord(
            at origin: CGPoint, width: CGFloat, color: UIColor = UIColor(
                red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1),
            amplitude: CGFloat = 9
        ) -> PKStroke {
            let points = stride(from: 0.0, through: Double(width), by: 4).map { x -> PKStrokePoint in
                let wave = CGFloat(sin(x / 9)) * amplitude
                return PKStrokePoint(
                    location: CGPoint(x: origin.x + CGFloat(x), y: origin.y + wave),
                    timeOffset: x / 400, size: CGSize(width: 3, height: 3), opacity: 1, force: 1,
                    azimuth: 0, altitude: .pi / 2)
            }
            return PKStroke(
                ink: PKInk(.pen, color: color),
                path: PKStrokePath(controlPoints: points, creationDate: Date()))
        }

        /// The first stroke runs level through the board's centre, so a tap
        /// there lands on ink; the rest are lines of words round it and
        /// clusters far away.
        func debugStrokes(extra: Int) -> [PKStroke] {
            let centre = BoardGeometry.center(of: host.extent)
            var strokes = [
                Self.debugWord(
                    at: CGPoint(x: centre.x - 200, y: centre.y), width: 400, amplitude: 2)
            ]
            let green = UIColor(red: 0.18, green: 0.62, blue: 0.27, alpha: 1)
            for line in 0..<5 {
                var x = centre.x - 420
                let y = centre.y + 60 + CGFloat(line) * 56
                for word in 0..<6 {
                    let width = CGFloat(50 + (line * 7 + word * 13) % 70)
                    strokes.append(
                        Self.debugWord(
                            at: CGPoint(x: x, y: y), width: width,
                            color: word == 0 ? green : UIColor(
                                red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1)))
                    x += width + 26
                }
            }
            for offset in [CGPoint(x: -3200, y: -2400), CGPoint(x: 3600, y: 2800), CGPoint(x: 2400, y: -3000)] {
                for line in 0..<3 {
                    strokes.append(
                        Self.debugWord(
                            at: CGPoint(x: centre.x + offset.x, y: centre.y + offset.y + CGFloat(line) * 50),
                            width: 260))
                }
            }
            // Short strokes in rows below the handwriting, for the limits.
            let columns = 120
            for index in 0..<max(extra, 0) {
                let x = centre.x - 2400 + CGFloat(index % columns) * 40
                let y = centre.y + 600 + CGFloat(index / columns) * 30
                strokes.append(Self.debugWord(at: CGPoint(x: x, y: y), width: 24, amplitude: 4))
            }
            return strokes
        }

        private func debugWriteStrokes(extra: Int) {
            guard let canvas = page?.canvas else { return }
            let strokes = debugStrokes(extra: extra)
            // A user edit, so autosave, the render and the limits see it.
            canvas.drawing = PKDrawing(strokes: canvas.drawing.strokes + strokes)
            refreshContentBounds()
            print("[paper] board strokes \(canvas.drawing.strokes.count)")
        }

        /// For each count: a board of that many strokes, then how long a save
        /// takes on the main thread with the ink encoded ahead (what autosave
        /// does now), and how long encoding it there would take (what it did).
        private func debugSaveBench(_ counts: [Int]) async {
            guard let canvas = page?.canvas, let ref = page.flatMap(ref(for:)) else { return }
            for count in counts {
                let strokes = debugStrokes(extra: max(count - 40, 0))
                canvas.drawing = PKDrawing(strokes: Array(strokes.prefix(count)))
                // What the pen resting does: the encode ahead, then the flush.
                encodeWork?.cancel()
                encodeAhead()
                let deadline = Date().addingTimeInterval(30)
                while encoded?.generation != drawingGeneration, Date() < deadline {
                    try? await Task.sleep(for: .milliseconds(20))
                }
                let ahead = lastEncodeAheadMs
                if !controller.autosave.isDirty(ref) { markDirty(ref) }
                let flushStart = Date()
                controller.autosave.flush(ref)
                let flushMs = Date().timeIntervalSince(flushStart) * 1000
                let mainStart = Date()
                let bytes = canvas.drawing.dataRepresentation().count
                let mainMs = Date().timeIntervalSince(mainStart) * 1000
                print(
                    String(
                        format:
                            "[paper] board save %d strokes, %.1f MB: flush on main %.0f ms with the ink encoded ahead in %.0f ms; encoding on main %.0f ms",
                        canvas.drawing.strokes.count, Double(bytes) / 1_048_576, flushMs, ahead,
                        mainMs))
            }
        }
    }
#endif

/// The board canvas, wrapped.
struct BoardCanvas: UIViewControllerRepresentable {
    let session: NotebookSession
    let onNavigateOnWeb: ((String) -> Void)?
    let scanDocument: (@MainActor (UIViewController) async throws -> Data)?

    func makeUIViewController(context: Context) -> BoardViewController {
        BoardViewController(
            session: session,
            onNavigateOnWeb: onNavigateOnWeb,
            scanDocument: scanDocument)
    }

    func updateUIViewController(_ controller: BoardViewController, context: Context) {}
}
