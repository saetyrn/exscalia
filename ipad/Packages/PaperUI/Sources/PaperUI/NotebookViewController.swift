import PDFKit
import PaperCore
import PencilKit
import PhotosUI
import UIKit
import UniformTypeIdentifiers

/// The notebook canvas.
///
/// UIKit rather than SwiftUI because every hard requirement here -- a
/// `PKCanvasView` per visible page, zoom without a parent transform, hover,
/// Pencil interactions, keyboard commands, virtualised recycling -- is a UIKit
/// API, and wrapping each one individually would cost more than owning the
/// controller.
public final class NotebookViewController: UIViewController {
    /// The live window: the visible page and one either side. Three canvases
    /// is what keeps a 200-page notebook scrolling; more is memory spent on
    /// pages nobody is looking at.
    private static let liveRadius = 1

    let session: NotebookSession
    var controller: PaperController { session.controller }
    var tools: ToolState { session.tools }
    let scanDocument: (@MainActor (UIViewController) async throws -> Data)?
    let onNavigateOnWeb: ((String) -> Void)?

    let scrollView = PageScrollView()
    let stack = PageStackView()

    private(set) var pageViews: [PageView] = []
    var zoom: CGFloat = 1
    var pinchStartZoom: CGFloat = 1
    /// Decided once, when a pinch begins: a rejected pinch stays rejected
    /// after the stroke that rejected it ends, rather than zooming from a
    /// start it never recorded.
    var pinchAccepted = false
    /// The page a stroke is in progress on, between `DidBeginUsingTool` and
    /// `DidEndUsingTool`. Cleared as well when that page loses its canvas,
    /// which takes the delegate with it, so the end is never reported.
    private weak var drawingPage: PageView?
    /// True while a stroke is in progress: a pinch that starts mid-stroke is a
    /// palm, not a gesture, and is ignored until its fingers lift.
    var isDrawing: Bool { drawingPage != nil }
    /// A rebuild asked for mid-stroke, run when the pen lifts. The stroke is
    /// not in `canvas.drawing` until then, so nothing may touch its canvas.
    private var rebuildPending = false
    var persistWork: DispatchWorkItem?
    private var hasRestoredPosition = false
    /// One recogniser for the whole notebook rather than one per page: the
    /// scroll view's pan is told to wait for exactly this, and a per-page
    /// recogniser would mean a failure requirement per recycled canvas.
    var lassoGesture: LassoGestureRecognizer?
    /// Fingers get a recogniser of their own, which takes one only on the
    /// selection. A palm resting on the glass is a finger as far as UIKit
    /// knows: it may fail this one, and the Pencil's is untouched.
    var fingerLassoGesture: LassoGestureRecognizer?
    /// The overlay a loop in progress belongs to.
    weak var activeLasso: LassoOverlayView?
    /// This window's registration as the source of its canvases' ink. It
    /// lives as long as the controller does, not as long as it is on screen:
    /// a full-screen scanner or camera takes the view off screen and brings
    /// the same controller back, and an unregistered canvas makes autosave
    /// write the bytes already on disk over the live ink.
    private var drawingProviderID: UUID?
    /// What the other windows on this device do to pages. Registered and
    /// removed with the drawing provider.
    private var localPageObserverID: UUID?
    /// Pages this window has edited since its own last write of them. All
    /// windows share one dirty set, so this is the only record of which of
    /// them has ink on a page that is not on disk yet.
    private var locallyDirty: Set<String> = []
    /// Pages another window dirtied or wrote while a stroke was in progress
    /// here. The stroke is not in `canvas.drawing` until the pen lifts, so the
    /// page catches up then rather than under it.
    private var changedUnderStroke: Set<String> = []
    /// An export is being rendered. Export is disabled meanwhile rather than
    /// shown as progress: nothing in the chrome spins.
    var isExporting = false
    /// The double-tap zoom, the undo and redo taps and the Text tool's tap.
    var editorGestures: EditorGestures?
    /// This controller's registration with the window's `ToolState`.
    var toolObserverID: UUID?
    /// The page a Draw Shape stroke is going down on, and how many strokes it
    /// had before, so the lift knows which stroke to clean up.
    var drawShapeStart: (pageID: String, strokeCount: Int)?
    /// A one-finger double-tap on the scroll view. Added with no target: the
    /// tool runtime gives it one.
    let zoomDoubleTap = UITapGestureRecognizer(target: nil, action: nil)
    /// A one-finger tap on the scroll view, once the double-tap has failed.
    /// Added with no target: selection gives it one.
    let objectTap = UITapGestureRecognizer(target: nil, action: nil)

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
    required init?(coder: NSCoder) { fatalError("NotebookViewController is created in code") }

    // MARK: - Lifecycle

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor.secondarySystemBackground

        scrollView.delegate = self
        scrollView.alwaysBounceVertical = true
        scrollView.contentInsetAdjustmentBehavior = .always
        scrollView.showsHorizontalScrollIndicator = false
        scrollView.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(scrollView)
        NSLayoutConstraint.activate([
            scrollView.topAnchor.constraint(equalTo: view.topAnchor),
            scrollView.bottomAnchor.constraint(equalTo: view.bottomAnchor),
            scrollView.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            scrollView.trailingAnchor.constraint(equalTo: view.trailingAnchor),
        ])
        scrollView.addSubview(stack)

        let pinch = UIPinchGestureRecognizer(target: self, action: #selector(handlePinch))
        pinch.delegate = self
        scrollView.addGestureRecognizer(pinch)

        lassoGesture = makeLassoGesture()
        fingerLassoGesture = makeLassoGesture()

        // Neither holds back, ends late or cancels a touch the pages and
        // objects get, and each may recognise alongside anything but the
        // lasso, so with no target they change nothing.
        zoomDoubleTap.numberOfTapsRequired = 2
        objectTap.require(toFail: zoomDoubleTap)
        for tap in [zoomDoubleTap, objectTap] {
            tap.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
            tap.cancelsTouchesInView = false
            tap.delaysTouchesEnded = false
            tap.delegate = self
            scrollView.addGestureRecognizer(tap)
        }
        editorGestures = EditorGestures(controller: self)
        installSelection()

        let pencil = UIPencilInteraction()
        pencil.delegate = self
        view.addInteraction(pencil)

        session.onFlush = { [weak self] in
            guard let self else { return }
            self.flushEverything()
            self.persistPosition()
        }
        session.onScrollTo = { [weak self] index in self?.scrollTo(index: index, animated: true) }
        session.onPagesChanged = { [weak self] in self?.rebuildPages() }
        session.onPagedModeChanged = { [weak self] in
            guard let self else { return }
            self.scrollView.isPagingEnabled = self.session.isPagedMode
        }
        scrollView.isPagingEnabled = session.isPagedMode
        session.onReveal = { [weak self] index, box in self?.reveal(index: index, box: box) }
        session.commands = self
        ensureDrawingProviderRegistered()

        rebuildPages()
        updateLassoGesture()
        observeTools()
        applySettings()
        applyReadOnly()
    }

    public override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let width = max(scrollView.bounds.width, stack.contentWidth)
        stack.frame = CGRect(x: 0, y: 0, width: width, height: max(stack.contentHeight, 1))
        scrollView.contentSize = CGSize(width: width, height: stack.contentHeight)
        if !hasRestoredPosition, !pageViews.isEmpty {
            hasRestoredPosition = true
            restorePosition()
            // After the reading position, never before: a search result is a
            // deliberate destination and must win over where the page was left.
            if let deferred = session.consumeDeferredReveal() {
                reveal(index: deferred.index, box: deferred.box)
            }
            // Pages first, so the debug content lands on the page list it
            // will be screenshotted with.
            runDebugPagesIfRequested()
            runDebugStrokeIfRequested()
            runDebugObjectsIfRequested()
            runDebugHandwritingIfRequested()
            runDebugGrowIfRequested()
            runDebugLassoLoopIfRequested()
            runDebugDrawShapeIfRequested()
        }
        updateLiveWindow()
    }

    public override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        ensureDrawingProviderRegistered()
    }

    public override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        flushEverything()
        persistPosition()
    }

    public override func didMove(toParent parent: UIViewController?) {
        super.didMove(toParent: parent)
        // Only leaving the parent means going away; disappearing does not.
        // Flush first, while this window can still answer for its pages.
        guard parent == nil else { return }
        flushEverything()
        unregisterDrawingProvider()
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
        // deinit is nonisolated; UIKit releases its controllers on the main
        // thread. The provider holds self weakly, so this is hygiene only.
        MainActor.assumeIsolated {
            unregisterDrawingProvider()
            stopObservingTools()
        }
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
        locallyDirty.removeAll()
        changedUnderStroke.removeAll()
        guard let drawingProviderID else { return }
        controller.removeDrawingProvider(drawingProviderID)
        self.drawingProviderID = nil
    }

    /// The source every edit is marked dirty with. Registering here as well
    /// means an edit can never report a nil source, even if UIKit skipped an
    /// appearance callback on the way back.
    private var drawingSource: UUID? {
        ensureDrawingProviderRegistered()
        return drawingProviderID
    }

    /// Every edit this window makes to a page is marked through here or
    /// `setObjects(_:on:)`, so `locallyDirty` knows about all of them.
    func markDirty(_ ref: PageRef) {
        controller.markPageDirty(ref, source: claim(ref))
    }

    func setObjects(_ objects: [PageObject], on ref: PageRef) {
        controller.setObjects(objects, on: ref, source: claim(ref))
    }

    /// The source an edit is marked with. A page another window is writing
    /// on is only ever written from that window's canvas: an edit that still
    /// reaches it here -- an undo, a template -- claims nothing, and the ink
    /// on this canvas is replaced when that window's lands.
    private func claim(_ ref: PageRef) -> UUID? {
        if pageViews.first(where: { $0.page.id == ref.pageID })?.isExternallyEditing == true {
            return nil
        }
        locallyDirty.insert(ref.pageID)
        return drawingSource
    }

    /// Dirty, but not with anything this window wrote: another window has ink
    /// on the page that is not on disk yet.
    private func isDirtyElsewhere(_ ref: PageRef) -> Bool {
        controller.autosave.isDirty(ref) && !locallyDirty.contains(ref.pageID)
    }

    private func livePageView(id: String) -> PageView? {
        pageViews.first { $0.page.id == id && $0.isLive }
    }

    // MARK: - Other windows

    /// Two windows on one notebook share the store and the dirty set but each
    /// holds its own canvas. The window writing a page owns it; the others
    /// stop taking input on it until its ink is on disk, then reload.
    private func localPageChanged(_ event: LocalPageEvent) {
        switch event {
        case .dirtied(let ref, let source):
            guard source != drawingProviderID, let view = livePageView(id: ref.pageID) else {
                return
            }
            if view === drawingPage || locallyDirty.contains(ref.pageID) {
                // Writing here too. The canvas is left alone; anything the
                // other window put in page.json is taken now.
                if view === drawingPage { changedUnderStroke.insert(ref.pageID) }
                reloadPage(view, ref: ref)
                return
            }
            view.setExternallyEditing(true)

        case .flushed(let ref, let owner):
            let mine = owner != nil && owner == drawingProviderID
            if mine { locallyDirty.remove(ref.pageID) }
            guard let view = livePageView(id: ref.pageID) else { return }
            if view === drawingPage {
                if !mine { changedUnderStroke.insert(ref.pageID) }
                return
            }
            if locallyDirty.contains(ref.pageID) {
                // Both windows wrote the page inside one debounce and the
                // other's copy is the one on disk. This window's ink is only
                // on its screen: claim the page again so it lands as well,
                // and the other window reloads it, rather than the two
                // disagreeing about the page until one of them writes again.
                // The page is already in `locallyDirty`, so no `claim`.
                controller.markPageDirty(ref, source: drawingSource)
                return
            }
            if !mine {
                reloadPage(view, ref: ref, drawing: true)
                refreshUndoState()
            }
            // Dirty again already if another window claimed it while this
            // write was being announced.
            view.setExternallyEditing(isDirtyElsewhere(ref))
        }
    }

    /// Brings a live page up to what is on disk: its objects, template and
    /// extent, and with `drawing` its ink.
    private func reloadPage(_ view: PageView, ref: PageRef, drawing: Bool = false) {
        let size = view.pageSize
        if drawing {
            // Through `applyDrawing`, so the reload is not reported as an
            // edit and written straight back.
            view.reloadDrawing(
                controller.store.drawingData(notebookPath: ref.notebookPath, pageID: ref.pageID))
        }
        if let page = try? controller.store.page(notebookPath: ref.notebookPath, id: ref.pageID) {
            view.update(page: page)
        }
        if view.pageSize != size {
            stack.setNeedsLayout()
            self.view.setNeedsLayout()
        }
    }

    /// The pen lifted on a page another window changed meanwhile. If the
    /// stroke was an edit, it claimed the page and this window's copy is the
    /// one written; otherwise the page catches up now.
    private func settleAfterStroke(_ view: PageView) {
        guard changedUnderStroke.remove(view.page.id) != nil, view.isLive,
            !locallyDirty.contains(view.page.id), let ref = ref(for: view)
        else { return }
        reloadPage(view, ref: ref, drawing: true)
        view.setExternallyEditing(isDirtyElsewhere(ref))
    }

    // MARK: - Pages

    /// Brings the page views in line with `session.pages`, by page id.
    ///
    /// A page that is still there keeps its view, and with it its canvas: the
    /// undo history, a lasso selection and text still being typed all survive
    /// a bookmark, a title or this device's own sync echo. Only pages that
    /// arrived get a view and only pages that left lose one.
    func rebuildPages() {
        guard !isDrawing else {
            rebuildPending = true
            return
        }
        rebuildPending = false
        // Writes whatever the pages about to go are still holding.
        flushEverything()

        let previous = pageViews
        var existing: [String: PageView] = [:]
        for view in previous where existing[view.page.id] == nil {
            existing[view.page.id] = view
        }
        pageViews = session.pages.map { page in
            guard let view = existing.removeValue(forKey: page.id) else {
                return makePageView(page)
            }
            let binding = view.page.pdf
            view.update(page: page)
            view.zoom = zoom
            // Another device, a restored version or a conflict may have
            // rewritten the ink underneath this canvas. Not a page whose write
            // just failed: it is still dirty, and this canvas holds the only
            // copy of its ink until the retry lands.
            if view.isLive, !(ref(for: view).map { controller.autosave.isDirty($0) } ?? false) {
                view.reloadDrawing(
                    controller.store.drawingData(
                        notebookPath: session.notebook.path, pageID: page.id))
            }
            if binding != page.pdf { loadPDFBackground(for: view) }
            return view
        }
        for view in previous where !pageViews.contains(where: { $0 === view }) {
            sendDormant(view, render: nil)
        }
        for (index, view) in pageViews.enumerated() {
            view.setAccessibility(index: index + 1, total: pageViews.count)
            if !view.isLive { view.setRender(renderImage(for: view.page.id)) }
        }
        stack.zoom = zoom
        stack.setPageViews(pageViews)
        // Frames first: the live window is worked out from them, and a stale
        // frame would recycle a canvas that is about to be on screen again.
        stack.layoutIfNeeded()
        view.setNeedsLayout()
        updateLiveWindow()
    }

    private func makePageView(_ page: Page) -> PageView {
        let view = PageView(page: page, notebookPath: session.notebook.path)
        view.delegate = self
        view.zoom = zoom
        view.isUserInteractionEnabled = !session.chrome.isReadOnly
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
        loadPDFBackground(for: view)
        return view
    }

    private func runPendingRebuild() {
        guard rebuildPending, !isDrawing else { return }
        rebuildPages()
    }

    /// Takes a page's canvas away. The canvas's delegate goes with it, so a
    /// stroke that was live on it will never report its end: it ends here.
    private func sendDormant(_ view: PageView, render: UIImage?) {
        // A canvas made later loads whatever is on disk by then.
        view.setExternallyEditing(false)
        changedUnderStroke.remove(view.page.id)
        view.goDormant(render: render)
        if drawingPage === view { drawingPage = nil }
    }

    /// The page's ref, looked up by id rather than by position: while a
    /// rebuild waits for the pen to lift, `pageViews` and `session.pages` can
    /// disagree about which page is where.
    func ref(for pageView: PageView) -> PageRef? {
        session.pages.firstIndex(where: { $0.id == pageView.page.id }).flatMap(session.ref(at:))
    }

    private func renderImage(for pageID: String) -> UIImage? {
        guard
            let data = try? Data(
                contentsOf: controller.store.paths.render(session.notebook.path, pageID))
        else { return nil }
        return UIImage(data: data)
    }

    private func loadPDFBackground(for view: PageView) {
        let directory = controller.store.paths.pageDirectory(
            session.notebook.path, view.page.id)
        view.loadPDFBackground(
            pageDirectory: directory,
            assetURL: view.page.pdf.map {
                controller.assetURL($0.asset, notebookPath: session.notebook.path)
            })
    }

    /// Gives canvases to the pages near the viewport and takes them away from
    /// the rest, flushing before each recycle so no ink lives only in a view.
    private func updateLiveWindow() {
        guard !pageViews.isEmpty else { return }
        let visible = currentPageIndex
        let lower = max(0, visible - Self.liveRadius)
        let upper = min(pageViews.count - 1, visible + Self.liveRadius)

        var dropped = false
        for (index, view) in pageViews.enumerated() {
            let shouldBeLive = index >= lower && index <= upper
            if shouldBeLive, !view.isLive {
                view.goLive(
                    drawingData: controller.store.drawingData(
                        notebookPath: session.notebook.path, pageID: view.page.id))
                configureTool(on: view)
                // Its ink on disk is about to be replaced by another window's.
                if let ref = ref(for: view) { view.setExternallyEditing(isDirtyElsewhere(ref)) }
            } else if !shouldBeLive, view.isLive {
                if let ref = ref(for: view) {
                    controller.autosave.flush(ref)
                    // The write failed and the page is dirty still: the canvas
                    // stays until the retry has its ink on disk.
                    if controller.autosave.isDirty(ref) { continue }
                }
                sendDormant(view, render: renderImage(for: view.page.id))
                dropped = true
            }
        }
        let moved = session.currentIndex != visible
        if moved { session.currentIndex = visible }
        // A canvas that went away took its history with it; Undo must not
        // offer what is no longer there.
        if moved || dropped { refreshUndoState() }
        if dropped { runPendingRebuild() }
    }

    private var currentPageIndex: Int {
        let midpoint = scrollView.contentOffset.y + scrollView.bounds.height / 2
        for (index, view) in pageViews.enumerated() where view.frame.maxY >= midpoint {
            return index
        }
        return max(pageViews.count - 1, 0)
    }

    func scrollTo(index: Int, animated: Bool) {
        guard pageViews.indices.contains(index) else { return }
        view.layoutIfNeeded()
        let target = max(0, stack.frame(ofPageAt: index).minY - PageStackView.gutter)
        scrollView.setContentOffset(
            CGPoint(x: scrollView.contentOffset.x, y: target),
            animated: animated && !UIAccessibility.isReduceMotionEnabled)
        updateLiveWindow()
    }

    /// Brings a page's region into the middle of the viewport and outlines it.
    ///
    /// The centre rather than the top of the page, because a search result
    /// promised a spot and scrolling to the page's header would make the
    /// reader hunt for it again.
    private func reveal(index: Int, box: CGRect?) {
        guard pageViews.indices.contains(index) else { return }
        view.layoutIfNeeded()
        let frame = stack.frame(ofPageAt: index)
        var target = frame.minY - PageStackView.gutter
        if let box {
            target = frame.minY + box.midY * zoom - scrollView.bounds.height / 2
        }
        let ceiling = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        scrollView.setContentOffset(
            CGPoint(x: scrollView.contentOffset.x, y: min(max(0, target), ceiling)),
            animated: !UIAccessibility.isReduceMotionEnabled)
        session.currentIndex = index
        updateLiveWindow()
        guard let box else { return }
        pageViews[index].flashHighlight(box)
        UIAccessibility.post(
            notification: .announcement, argument: "Page \(index + 1), showing the match")
    }

    /// Opens where the document was left: its zoom, its page, and the spot on
    /// that page, both ways.
    private func restorePosition() {
        let state = controller.store.viewState(notebookID: session.notebook.id)
        if state.zoom > 0, state.zoom != zoom {
            setZoom(state.zoom)
        }
        session.zoom = zoom
        if let pageID = state.lastPageID,
            let index = session.pages.firstIndex(where: { $0.id == pageID })
        {
            session.currentIndex = index
            view.layoutIfNeeded()
            scrollView.contentOffset = restoredOffset(state.offset, onPageAt: index)
        }
        updateLiveWindow()
    }

    /// Where the viewport's top-left sits relative to the top-left of the
    /// page showing, in page points: what the view state stores. Page points
    /// rather than the scroll offset, so the spot survives a page above it
    /// growing, a different zoom and a window of another width.
    func viewportOffsetOnCurrentPage() -> CGPoint {
        let index = session.currentIndex
        guard pageViews.indices.contains(index) else { return scrollView.contentOffset }
        let frame = stack.frame(ofPageAt: index)
        let scale = max(zoom, 0.01)
        return CGPoint(
            x: (scrollView.contentOffset.x - frame.minX) / scale,
            y: (scrollView.contentOffset.y - frame.minY) / scale)
    }

    /// The scroll offset that puts `offset` (page points from the page's
    /// top-left) at the viewport's top-left, inside the content.
    func restoredOffset(_ offset: CGPoint, onPageAt index: Int) -> CGPoint {
        let frame = stack.frame(ofPageAt: index)
        let scale = max(zoom, 0.01)
        var relative = offset
        // Before offsets were kept per page they were the scroll view's own,
        // which for any page but the first lies past the page's end. Such a
        // spot opens at the page's top.
        if relative.y > frame.height / scale || relative.y < -scrollView.bounds.height / scale {
            relative.y = -PageStackView.gutter / scale
        }
        if relative.x > frame.width / scale || relative.x < -scrollView.bounds.width / scale {
            relative.x = 0
        }
        let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
        let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        return CGPoint(
            x: min(max(0, frame.minX + relative.x * scale), ceilingX),
            y: min(max(0, frame.minY + relative.y * scale), ceilingY))
    }

    /// Saves the page showing, the zoom and the spot on the page. The zoom is
    /// the canvas's own, whichever way it got there.
    func persistPosition() {
        session.zoom = zoom
        session.persistViewState(offset: viewportOffsetOnCurrentPage())
    }

    private func drawingData(for ref: PageRef) -> Data? {
        pageViews.first(where: { $0.page.id == ref.pageID })?.drawingData
    }

    private func flushEverything() {
        controller.autosave.flushAll()
    }
}

// MARK: - Scrolling

extension NotebookViewController: UIScrollViewDelegate {
    public func scrollViewDidScroll(_ scrollView: UIScrollView) {
        updateLiveWindow()
    }

    public func scrollViewDidEndDecelerating(_ scrollView: UIScrollView) {
        schedulePersist()
    }

    public func scrollViewDidEndDragging(_ scrollView: UIScrollView, willDecelerate: Bool) {
        if !willDecelerate { schedulePersist() }
    }
}

// MARK: - Pages

extension NotebookViewController: PageViewDelegate {
    func pageViewDrawingChanged(_ pageView: PageView) {
        guard let ref = ref(for: pageView) else { return }
        markDirty(ref)
        // Growth writes the extent and moves the objects on disk; a page
        // another window is writing is not this window's to reshape.
        if !pageView.isExternallyEditing { growPageIfNeeded(pageView) }
        refreshUndoState()
    }

    func pageViewDidBeginUsingTool(_ pageView: PageView) {
        drawingPage = pageView
        drawShapeStrokeBegan(on: pageView)
    }

    func pageViewDidEndUsingTool(_ pageView: PageView) {
        drawingPage = nil
        drawShapeStrokeEnded(on: pageView)
        settleAfterStroke(pageView)
        refreshUndoState()
        runPendingRebuild()
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
        if !pageView.isExternallyEditing { growPageIfNeeded(pageView) }
        refreshUndoState()
    }

    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? {
        controller.assetImage(assetPath, notebookPath: session.notebook.path)
    }

    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {
        guard !session.chrome.isReadOnly,
            let index = pageViews.firstIndex(where: { $0 === pageView })
        else { return }
        session.currentIndex = index
        insertImage(data: data, name: name, at: point)
    }

    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {
        guard !session.chrome.isReadOnly,
            let index = pageViews.firstIndex(where: { $0 === pageView })
        else { return }
        session.currentIndex = index
        Task { await insertPDF(data: data, name: name) }
    }
}

// MARK: - Editor commands

/// The chrome's way in. The navigation bar, its menus and the sidebar act on
/// the canvas only through these.
extension NotebookViewController: EditorCommands {
    public func undo() {
        guard !session.chrome.isReadOnly else { return }
        performUndo()
    }

    public func redo() {
        guard !session.chrome.isReadOnly else { return }
        performRedo()
    }

    public func addPage(_ position: PagePosition, template: Template?) {
        guard !session.chrome.isReadOnly else { return }
        let existing = Set(session.pages.map(\.id))
        switch position {
        case .before where session.currentIndex == 0:
            // The store adds a page after another one or at the end, so the
            // new first page is added at the end and moved to the front.
            session.addPage(after: nil)
            session.movePage(from: session.pages.count - 1, to: 0)
            scrollTo(index: 0, animated: true)
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

    /// A page's ink, as one undo step, when the page has a canvas; otherwise
    /// straight on disk, with the ink kept in its snapshots. Objects stay.
    public func clearPage(at index: Int) {
        guard !session.chrome.isReadOnly, pageViews.indices.contains(index) else { return }
        if pageViews[index].isLive {
            clearInk(onPageAt: index)
            return
        }
        guard let ref = session.ref(at: index),
            (try? controller.store.clearPage(notebookID: ref.notebookID, pageID: ref.pageID)) != nil
        else { return }
        pageViews[index].setRender(nil)
        // The render and the reading of the page follow, as after any edit.
        markDirty(ref)
    }

    /// A page's ruling, its paper colour or both, by index rather than
    /// whichever page is showing. The store journals the change; marking the
    /// page dirty renders it again.
    public func setPageStyle(template: Template?, paperColor: PaperColor?, at index: Int) {
        guard !session.chrome.isReadOnly, template != nil || paperColor != nil,
            let ref = session.ref(at: index),
            let page = try? controller.store.setPageStyle(
                notebookID: ref.notebookID, pageID: ref.pageID, template: template,
                paperColor: paperColor)
        else { return }
        markDirty(ref)
        session.reload()
        pageViews.first(where: { $0.page.id == ref.pageID })?.update(page: page)
    }

    /// The page showing, as a one-page PDF, or the whole document.
    public func export(_ scope: ExportScope, from source: UIView?) {
        presentExport(scope, from: source ?? view)
    }

    public func presentInsert(_ kind: InsertKind, from source: UIView?) {
        guard !session.chrome.isReadOnly else { return }
        switch kind {
        case .photo: presentPhotoPicker()
        case .camera: presentCamera()
        case .files, .pdf: presentPDFPicker()
        case .scan: presentDocumentScanner()
        }
    }

    /// Zooms about the middle of the viewport, as a pinch zooms about the
    /// fingers, and never under a stroke in progress.
    public func zoom(_ request: ZoomRequest) {
        guard !isDrawing else { return }
        let next: CGFloat
        switch request {
        case .fitWidth, .fitContent: next = 1
        case .percent(let percent): next = CGFloat(percent / 100)
        case .stepIn: next = zoom * 1.25
        case .stepOut: next = zoom / 1.25
        }
        let previous = zoom
        let size = scrollView.bounds.size
        let middle = CGPoint(
            x: scrollView.contentOffset.x + size.width / 2,
            y: scrollView.contentOffset.y + size.height / 2)
        setZoom(next)
        guard previous > 0 else { return }
        let factor = zoom / previous
        let ceilingX = max(0, scrollView.contentSize.width - size.width)
        let ceilingY = max(0, scrollView.contentSize.height - size.height)
        scrollView.contentOffset = CGPoint(
            x: min(max(0, middle.x * factor - size.width / 2), ceilingX),
            y: min(max(0, middle.y * factor - size.height / 2), ceilingY))
        session.zoom = zoom
        schedulePersist()
    }

    /// Today's Ask flow, with what is on screen of the page showing as the
    /// picture: its paper, objects and ink, cut to the viewport.
    public func askOMNIA(from source: UIView?) {
        guard let region = visiblePageRegion() else {
            UIAccessibility.post(notification: .announcement, argument: "Nothing to send")
            return
        }
        performSelectionAIAction(.ask, image: region.image, rect: region.rect, on: region.page)
    }

    /// Read Only: no ink, no lasso, no selecting, inserting or editing
    /// objects. Scrolling, zoom and everything that only reads still work.
    public func setReadOnly(_ readOnly: Bool) {
        session.chrome.isReadOnly = readOnly
        applyReadOnly()
    }
}

// MARK: - Read Only

extension NotebookViewController {
    /// Brings every page and recogniser in line with `chrome.isReadOnly`.
    func applyReadOnly() {
        let readOnly = session.chrome.isReadOnly
        if readOnly {
            // A selection, a drag or a text box being typed in is put down
            // where it is, as a tool switch puts it down.
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
            for page in pageViews { page.deselectObjects() }
            view.endEditing(true)
            if let palette = presentedViewController as? ToolPaletteController {
                palette.dismiss(animated: false)
            }
        }
        for page in pageViews { configureTool(on: page) }
        updateLassoGesture()
        updateEditorGestures()
        // Selection, insert taps and object editing, at once: the observer in
        // +Selection applies the same lock a run-loop turn later, which is too
        // late for anything that happens in between.
        setSelectionLocked(readOnly)
        // With nothing to ink, one finger pans even where a finger would ink.
        scrollView.panGestureRecognizer.minimumNumberOfTouches =
            EditorInput.fingerInk && !readOnly ? 2 : 1
        refreshUndoState()
    }

    /// The finger gestures that change the page -- the Text tool's taps and
    /// the two- and three-finger undo and redo -- are off in Read Only.
    func updateEditorGestures() {
        guard let editorGestures else { return }
        // The text and insert taps belong to the pages now; Read Only switches
        // them off through `setSelectionLocked` (+Selection).
        let readOnly = session.chrome.isReadOnly
        editorGestures.undoTap.isEnabled = !readOnly
        editorGestures.redoTap.isEnabled = !readOnly
    }

    /// The part of the page showing that is on screen: its picture as a PNG,
    /// and where it is on the page, in page points.
    func visiblePageRegion() -> (page: PageView, rect: CGRect, image: Data)? {
        guard let page = currentPageView else { return nil }
        view.layoutIfNeeded()
        let viewport = CGRect(origin: scrollView.contentOffset, size: scrollView.bounds.size)
        let shown = page.frame.intersection(viewport)
        guard !shown.isNull, shown.width >= 1, shown.height >= 1 else { return nil }
        let local = page.convert(shown, from: stack)
        let format = UIGraphicsImageRendererFormat()
        // The screen's own pixels at most: this is a picture of what is
        // showing, not an export.
        format.scale = min(view.window?.screen.scale ?? 2, 2)
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(bounds: local, format: format)
        let data = renderer.pngData { context in
            UIColor(PaperTokens.paper).setFill()
            context.fill(local)
            page.drawHierarchy(in: page.bounds, afterScreenUpdates: false)
        }
        let scale = max(zoom, 0.01)
        let rect = CGRect(
            x: local.minX / scale, y: local.minY / scale,
            width: local.width / scale, height: local.height / scale)
        return (page, rect, data)
    }
}
