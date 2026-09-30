import PDFKit
import PaperCore
import PencilKit
import PhotosUI
import UIKit
import UniformTypeIdentifiers

/// The vertical scroller the pages live in.
///
/// Its only special behaviour is the input policy: when "Draw with finger" is
/// on, a single finger draws and panning takes two, because otherwise every
/// attempt to scroll would leave a line across the page.
final class PageScrollView: UIScrollView {
    var requiresTwoFingerPan = false {
        didSet {
            panGestureRecognizer.minimumNumberOfTouches = requiresTwoFingerPan ? 2 : 1
        }
    }

    override init(frame: CGRect) {
        super.init(frame: frame)
        // Pencil is creation, fingers are navigation -- the research principle,
        // enforced here once for every tool rather than tool by tool. Without
        // this a Pencil drag drives the pan like any other touch, which is why
        // the lasso only ever scrolled the page on hardware.
        panGestureRecognizer.allowedTouchTypes = [NSNumber(value: UITouch.TouchType.direct.rawValue)]
        // No 150ms hesitation before a touch reaches the page. Cancellation is
        // deliberately left on: a scroll view that may not cancel its content's
        // touches is a scroll view that stops scrolling the moment a finger
        // lands on a page, and the lasso no longer needs it -- it is a gesture
        // recogniser now, and the pan waits for it.
        delaysContentTouches = false
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("PageScrollView is created in code") }
}

/// Lays the pages out in one column with 24pt gutters, at whatever the current
/// zoom makes each page.
final class PageStackView: UIView {
    static let gutter: CGFloat = 24

    private(set) var pageViews: [PageView] = []
    var zoom: CGFloat = 1

    /// Removes the pages that went and adds the ones that came. Every other
    /// view stays where it is: taking a live `PKCanvasView` out of the
    /// hierarchy, even to put it straight back, ends its stroke and its
    /// responder state.
    func setPageViews(_ views: [PageView]) {
        let kept = Set(views.map(ObjectIdentifier.init))
        for view in pageViews where !kept.contains(ObjectIdentifier(view)) && view.superview === self {
            view.removeFromSuperview()
        }
        pageViews = views
        for view in views where view.superview !== self { addSubview(view) }
        setNeedsLayout()
    }

    /// Page origins in stack coordinates, which is what the scroll offset and
    /// the live window are computed from.
    func frame(ofPageAt index: Int) -> CGRect {
        guard pageViews.indices.contains(index) else { return .zero }
        return pageViews[index].frame
    }

    var contentHeight: CGFloat {
        guard let last = pageViews.last else { return 0 }
        return last.frame.maxY + Self.gutter
    }

    /// How wide the column has to be. An infinite page grows sideways as well
    /// as downwards, so the stack is no longer always the width of the screen.
    var contentWidth: CGFloat {
        let widest = pageViews.map { $0.pageSize.width * zoom }.max() ?? 0
        return widest + Self.gutter * 2
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        var y = Self.gutter
        for view in pageViews {
            let size = CGSize(
                width: view.pageSize.width * zoom, height: view.pageSize.height * zoom)
            view.frame = CGRect(
                x: (bounds.width - size.width) / 2, y: y, width: size.width, height: size.height)
            y += size.height + Self.gutter
        }
    }
}

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

    private let session: NotebookSession
    private var controller: PaperController { session.controller }
    private var tools: ToolState { session.tools }
    private let scanDocument: (@MainActor (UIViewController) async throws -> Data)?
    private let onNavigateOnWeb: ((String) -> Void)?

    private let scrollView = PageScrollView()
    private let stack = PageStackView()
    private lazy var capsule = ToolCapsuleView(state: tools)

    private(set) var pageViews: [PageView] = []
    private var zoom: CGFloat = 1
    private var pinchStartZoom: CGFloat = 1
    /// Decided once, when a pinch begins: a rejected pinch stays rejected
    /// after the stroke that rejected it ends, rather than zooming from a
    /// start it never recorded.
    private var pinchAccepted = false
    /// The page a stroke is in progress on, between `DidBeginUsingTool` and
    /// `DidEndUsingTool`. Cleared as well when that page loses its canvas,
    /// which takes the delegate with it, so the end is never reported.
    private weak var drawingPage: PageView?
    /// True while a stroke is in progress: a pinch that starts mid-stroke is a
    /// palm, not a gesture, and is ignored until its fingers lift.
    private var isDrawing: Bool { drawingPage != nil }
    /// A rebuild asked for mid-stroke, run when the pen lifts. The stroke is
    /// not in `canvas.drawing` until then, so nothing may touch its canvas.
    private var rebuildPending = false
    private var persistWork: DispatchWorkItem?
    private var hasRestoredPosition = false
    /// One recogniser for the whole notebook rather than one per page: the
    /// scroll view's pan is told to wait for exactly this, and a per-page
    /// recogniser would mean a failure requirement per recycled canvas.
    private var lassoGesture: LassoGestureRecognizer?
    /// The overlay a loop in progress belongs to.
    private weak var activeLasso: LassoOverlayView?
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
    /// shown as progress: the capsule never spins.
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

        let lasso = LassoGestureRecognizer(target: self, action: #selector(handleLasso))
        lasso.delegate = self
        lasso.isEnabled = false
        lasso.shouldBegin = { [weak self] point in self?.lassoTarget(at: point) != nil }
        stack.addGestureRecognizer(lasso)
        // The loop wins over the scroll, always: this is the fix for a Pencil
        // drag in Lasso mode doing nothing but scrolling.
        scrollView.panGestureRecognizer.require(toFail: lasso)
        lassoGesture = lasso

        capsule.delegate = self
        view.addSubview(capsule)

        let pencil = UIPencilInteraction()
        pencil.delegate = self
        view.addInteraction(pencil)

        session.onFlush = { [weak self] in
            guard let self else { return }
            self.flushEverything()
            self.session.persistViewState(offset: self.scrollView.contentOffset)
        }
        session.onScrollTo = { [weak self] index in self?.scrollTo(index: index, animated: true) }
        session.onPagesChanged = { [weak self] in self?.rebuildPages() }
        session.onReveal = { [weak self] index, box in self?.reveal(index: index, box: box) }
        ensureDrawingProviderRegistered()

        rebuildPages()
        updateLassoGesture()
        applySettings()
        NotificationCenter.default.addObserver(
            self, selector: #selector(applySettings), name: UserDefaults.didChangeNotification,
            object: nil)
    }

    public override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let width = max(scrollView.bounds.width, stack.contentWidth)
        stack.frame = CGRect(x: 0, y: 0, width: width, height: max(stack.contentHeight, 1))
        scrollView.contentSize = CGSize(width: width, height: stack.contentHeight)
        capsule.reposition(in: view)
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
        session.persistViewState(offset: scrollView.contentOffset)
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
        MainActor.assumeIsolated { unregisterDrawingProvider() }
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
    private func markDirty(_ ref: PageRef) {
        controller.markPageDirty(ref, source: claim(ref))
    }

    private func setObjects(_ objects: [PageObject], on ref: PageRef) {
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
                refreshCapsule()
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
    private func rebuildPages() {
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
    private func ref(for pageView: PageView) -> PageRef? {
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
                view.setLassoActive(tools.tool == .lasso)
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
        if moved || dropped { refreshCapsule() }
        if dropped { runPendingRebuild() }
    }

    private var currentPageIndex: Int {
        let midpoint = scrollView.contentOffset.y + scrollView.bounds.height / 2
        for (index, view) in pageViews.enumerated() where view.frame.maxY >= midpoint {
            return index
        }
        return max(pageViews.count - 1, 0)
    }

    private func scrollTo(index: Int, animated: Bool) {
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

    /// The lasso read some handwriting: put the words on the page where the
    /// ink was, at roughly the size it was written.
    private func replaceInkWithText(_ text: String, rect: CGRect, on view: PageView) {
        let lineCount = max(1, text.split(separator: "\n").count)
        let size = min(48, max(12, rect.height / CGFloat(lineCount) * 0.72))
        let frame = rect.insetBy(dx: -6, dy: -6)
        let object = PageObject(
            type: .text,
            frame: PageObject.Frame(
                CGRect(
                    x: max(0, frame.minX), y: max(0, frame.minY),
                    width: max(40, frame.width), height: max(size + 8, frame.height))),
            text: text, fontSize: Double(size))
        view.insertObject(object)
        // No keyboard: the words are already what the user wrote, and the ink
        // vanishing under a caret is disconcerting.
        view.deselectObjects()
        UIAccessibility.post(notification: .announcement, argument: "Converted to text")
    }

    private func copySelectionLink(rect: CGRect, on view: PageView) {
        guard let page = session.pages.first(where: { $0.id == view.page.id }),
            let url = controller.durableLink(
                notebookID: session.notebook.id, pageID: page.id, rect: rect)
        else {
            UIAccessibility.post(notification: .announcement, argument: "Link unavailable")
            return
        }
        UIPasteboard.general.url = url
        UIAccessibility.post(notification: .announcement, argument: "Paper link copied")
    }

    private func performSelectionAIAction(
        _ action: SelectionAIAction, image: Data, rect: CGRect, on view: PageView
    ) {
        if action == .ask {
            let alert = UIAlertController(
                title: "Ask OMNIA", message: "What should OMNIA do with this handwriting?",
                preferredStyle: .alert)
            alert.addTextField { field in
                field.placeholder = "Ask a question"
                field.returnKeyType = .send
            }
            alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
            alert.addAction(
                UIAlertAction(title: "Ask", style: .default) { [weak self, weak alert] _ in
                    guard let prompt = alert?.textFields?.first?.text,
                        !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    else { return }
                    self?.sendSelectionAIAction(
                        action, image: image, rect: rect, pageID: view.page.id, prompt: prompt)
                })
            present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
            return
        }
        sendSelectionAIAction(action, image: image, rect: rect, pageID: view.page.id)
    }

    private func sendSelectionAIAction(
        _ action: SelectionAIAction,
        image: Data,
        rect: CGRect,
        pageID: String,
        prompt: String? = nil
    ) {
        UIAccessibility.post(notification: .announcement, argument: "Sending to OMNIA")
        let notebookID = session.notebook.id
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let result = try await self.controller.runSelectionAction(
                    action: action,
                    notebookID: notebookID,
                    pageID: pageID,
                    rect: rect,
                    image: image,
                    prompt: prompt)
                if let onNavigateOnWeb = self.onNavigateOnWeb {
                    onNavigateOnWeb(
                        result.navigate
                            ?? "/omnia?t=\(PaperAPI.encode(segment: result.threadID))")
                } else {
                    let alert = UIAlertController(
                        title: action.title,
                        message: result.reply.isEmpty ? "OMNIA finished." : result.reply,
                        preferredStyle: .alert)
                    alert.addAction(UIAlertAction(title: "OK", style: .default))
                    self.present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
                }
            } catch {
                var stillRunning = false
                if case .stillRunning? = error as? PaperController.SelectionActionError {
                    stillRunning = true
                }
                let alert = UIAlertController(
                    title: stillRunning ? "OMNIA is still working" : "OMNIA unavailable",
                    message: error.localizedDescription,
                    preferredStyle: .alert)
                // The turn carries on without us; its thread is where it lands.
                if stillRunning, let onNavigateOnWeb = self.onNavigateOnWeb,
                    let thread = self.controller.selectionThreads[notebookID]
                {
                    alert.addAction(
                        UIAlertAction(title: "Open", style: .default) { _ in
                            onNavigateOnWeb("/omnia?t=\(PaperAPI.encode(segment: thread))")
                        })
                }
                alert.addAction(UIAlertAction(title: "OK", style: .default))
                self.present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
            }
        }
    }

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
            let base = stack.frame(ofPageAt: index).minY - PageStackView.gutter
            scrollView.contentOffset = CGPoint(x: 0, y: max(0, base + state.offset.y))
        }
        updateLiveWindow()
    }

    private func drawingData(for ref: PageRef) -> Data? {
        pageViews.first(where: { $0.page.id == ref.pageID })?.drawingData
    }

    private func flushEverything() {
        controller.autosave.flushAll()
    }

    // MARK: - Settings

    @objc private func applySettings() {
        let defaults = UserDefaults.standard
        let drawWithFinger = defaults.bool(forKey: ServerSettings.Key.drawWithFinger)
        let systemPicker = defaults.bool(forKey: ServerSettings.Key.useSystemToolPicker)

        scrollView.requiresTwoFingerPan = drawWithFinger
        let policy: PKCanvasViewDrawingPolicy = drawWithFinger ? .anyInput : .pencilOnly
        for view in pageViews { view.setDrawingPolicy(policy) }
        updateLassoGesture()

        capsule.isCollapsed = systemPicker
        if systemPicker {
            showSystemToolPicker()
        } else {
            hideSystemToolPicker()
            applyTool()
        }
        refreshCapsule()
    }

    private var toolPicker: PKToolPicker?

    /// The advanced fallback: PencilKit's own picker, with the capsule reduced
    /// to `Undo | Redo | +` so undo never disappears.
    private func showSystemToolPicker() {
        let picker = toolPicker ?? PKToolPicker()
        toolPicker = picker
        for view in pageViews {
            guard let canvas = view.canvas else { continue }
            picker.setVisible(true, forFirstResponder: canvas)
            picker.addObserver(canvas)
        }
        pageViews.first(where: { $0.canvas != nil })?.canvas?.becomeFirstResponder()
    }

    private func hideSystemToolPicker() {
        guard let picker = toolPicker else { return }
        for view in pageViews {
            guard let canvas = view.canvas else { continue }
            picker.setVisible(false, forFirstResponder: canvas)
            picker.removeObserver(canvas)
        }
        toolPicker = nil
    }

    private func applyTool() {
        let tool = tools.pkTool
        for view in pageViews {
            view.setTool(tool)
            view.setLassoActive(tools.tool == .lasso)
        }
        updateLassoGesture()
    }

    /// The loop recogniser exists only while the Lasso is selected, so every
    /// other tool leaves one-finger scrolling exactly as it was.
    private func updateLassoGesture() {
        guard let lassoGesture else { return }
        if tools.tool != .lasso {
            activeLasso = nil
        }
        lassoGesture.isEnabled = tools.tool == .lasso
        let pencil = NSNumber(value: UITouch.TouchType.pencil.rawValue)
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)
        // Under "Draw with finger" a finger creates as well, so it may also
        // lasso; otherwise the loop belongs to the Pencil alone and a finger
        // keeps scrolling.
        lassoGesture.allowedTouchTypes =
            UserDefaults.standard.bool(forKey: ServerSettings.Key.drawWithFinger)
            ? [direct, pencil] : [pencil]
    }

    private func refreshCapsule() {
        let manager = liveCanvas?.undoManager
        capsule.refresh(
            canUndo: manager?.canUndo ?? false, canRedo: manager?.canRedo ?? false)
    }

    private var liveCanvas: PKCanvasView? {
        guard pageViews.indices.contains(session.currentIndex) else {
            return pageViews.first(where: { $0.canvas != nil })?.canvas
        }
        return pageViews[session.currentIndex].canvas
            ?? pageViews.first(where: { $0.canvas != nil })?.canvas
    }

    // MARK: - Infinite pages

    /// Extends an infinite page when the work approaches an edge.
    ///
    /// The rule itself is `InfiniteCanvas` in PaperCore, so the screen, the
    /// renderer and the exporter cannot disagree about it. What lives here is
    /// only the consequence: write the new extent, move the objects if the page
    /// grew backwards, and shift the scroll offset by exactly as much as the
    /// content moved, so the page under the pen does not jump.
    private func growPageIfNeeded(_ pageView: PageView) {
        guard pageView.page.size.isInfinite,
            let ref = ref(for: pageView),
            let content = pageView.contentBounds
        else { return }
        let scale = max(zoom, 0.01)
        let viewport = CGSize(
            width: scrollView.bounds.width / scale, height: scrollView.bounds.height / scale)
        guard
            let growth = InfiniteCanvas.growth(
                extent: pageView.pageSize, content: content, viewport: viewport,
                quantum: TemplateGeometry.growthQuantum(for: pageView.page.template))
        else { return }

        // Where a fixed point of the page sits before the page changes shape.
        let before = stack.convert(CGPoint.zero, from: pageView)
        let moved = pageView.applyGrowth(growth)
        stack.setNeedsLayout()
        view.setNeedsLayout()
        view.layoutIfNeeded()
        // ...and where that same material point sits afterwards.
        let after = stack.convert(
            CGPoint(x: growth.translation.width * scale, y: growth.translation.height * scale),
            from: pageView)
        let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
        let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
        scrollView.contentOffset = CGPoint(
            x: min(max(0, scrollView.contentOffset.x + after.x - before.x), ceilingX),
            y: min(max(0, scrollView.contentOffset.y + after.y - before.y), ceilingY))

        try? controller.store.setPageExtent(
            growth.extent, notebookPath: session.notebook.path, pageID: pageView.page.id)
        if let moved {
            setObjects(moved, on: ref)
        }
        markDirty(ref)
        session.reload()
    }

    // MARK: - Lasso

    /// The live page under a point in stack coordinates, and its overlay.
    private func lassoTarget(at point: CGPoint) -> (page: PageView, lasso: LassoOverlayView)? {
        guard tools.tool == .lasso else { return nil }
        for view in pageViews where view.frame.contains(point) {
            guard let lasso = view.lasso, !view.isExternallyEditing else { return nil }
            return lasso.shouldCaptureTouch(at: lasso.convert(point, from: stack))
                ? (view, lasso) : nil
        }
        return nil
    }

    @objc private func handleLasso(_ gesture: LassoGestureRecognizer) {
        switch gesture.state {
        case .began:
            guard let target = lassoTarget(at: gesture.location(in: stack)) else { return }
            activeLasso = target.lasso
            target.lasso.handleTouchBegan(at: gesture.location(in: target.lasso))
        case .changed:
            guard let lasso = activeLasso else { return }
            lasso.handleTouchMoved(to: gesture.location(in: lasso))
        case .ended:
            activeLasso?.handleTouchEnded()
            activeLasso = nil
        case .cancelled, .failed:
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
        default:
            break
        }
    }

    // MARK: - Zoom

    @objc private func handlePinch(_ gesture: UIPinchGestureRecognizer) {
        switch gesture.state {
        case .began:
            // A pinch that arrives mid-stroke is a palm landing, not a zoom.
            pinchAccepted = !isDrawing
            guard pinchAccepted else { return }
            pinchStartZoom = zoom
        case .changed:
            // An accepted pinch still holds still under a live stroke.
            guard pinchAccepted, !isDrawing else { return }
            let anchor = gesture.location(in: stack)
            let previous = zoom
            setZoom(pinchStartZoom * gesture.scale)
            guard previous > 0 else { return }
            // Keep the point under the fingers under the fingers, on both
            // axes: an infinite page scrolls sideways as well.
            let factor = zoom / previous
            let offset = scrollView.contentOffset
            let anchorInView = CGPoint(
                x: anchor.x - offset.x, y: anchor.y - offset.y)
            let ceilingX = max(0, scrollView.contentSize.width - scrollView.bounds.width)
            let ceilingY = max(0, scrollView.contentSize.height - scrollView.bounds.height)
            scrollView.contentOffset = CGPoint(
                x: min(max(0, anchor.x * factor - anchorInView.x), ceilingX),
                y: min(max(0, anchor.y * factor - anchorInView.y), ceilingY))
        case .ended, .cancelled, .failed:
            defer { pinchAccepted = false }
            guard pinchAccepted else { return }
            session.zoom = zoom
            schedulePersist()
        default:
            break
        }
    }

    private func setZoom(_ next: CGFloat) {
        let clamped = min(max(next, 0.5), 4)
        guard abs(clamped - zoom) > 0.001 else { return }
        zoom = clamped
        stack.zoom = clamped
        for view in pageViews { view.zoom = clamped }
        view.setNeedsLayout()
        view.layoutIfNeeded()
        scrollView.contentSize = CGSize(
            width: max(scrollView.bounds.width, stack.contentWidth), height: stack.contentHeight)
    }

    private func schedulePersist() {
        persistWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated {
                guard let self else { return }
                self.session.persistViewState(offset: self.scrollView.contentOffset)
            }
        }
        persistWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5, execute: work)
    }

    // MARK: - Keyboard

    public override var canBecomeFirstResponder: Bool { true }

    public override var keyCommands: [UIKeyCommand]? {
        [
            command("z", [.command], #selector(commandUndo), "Undo"),
            command("z", [.command, .shift], #selector(commandRedo), "Redo"),
            command("n", [.command, .shift], #selector(commandNewPage), "New page"),
            command("]", [.command], #selector(commandNextPage), "Next page"),
            command("[", [.command], #selector(commandPreviousPage), "Previous page"),
            command("p", [.command], #selector(commandExport), "Export"),
            command("v", [.command], #selector(commandPaste), "Paste"),
            command(UIKeyCommand.inputEscape, [], #selector(commandEscape), "Clear selection"),
            command("\u{8}", [], #selector(commandDeleteSelection), "Delete selection"),
        ]
    }

    private func command(
        _ input: String, _ modifiers: UIKeyModifierFlags, _ action: Selector, _ title: String
    ) -> UIKeyCommand {
        let command = UIKeyCommand(input: input, modifierFlags: modifiers, action: action)
        command.discoverabilityTitle = title
        command.wantsPriorityOverSystemBehavior = true
        return command
    }

    @objc private func commandUndo() { performUndo() }
    @objc private func commandRedo() { performRedo() }
    @objc private func commandNewPage() { session.addPage(after: session.currentIndex) }
    @objc private func commandNextPage() { scrollTo(index: session.currentIndex + 1, animated: true) }
    @objc private func commandPreviousPage() {
        scrollTo(index: session.currentIndex - 1, animated: true)
    }
    @objc private func commandEscape() {
        pageViews.forEach {
            $0.setLassoActive(tools.tool == .lasso)
            $0.deselectObjects()
        }
    }
    @objc private func commandExport() { presentExport(from: capsule) }
    @objc private func commandPaste() { pasteFromClipboard() }
    @objc private func commandDeleteSelection() {
        guard pageViews.indices.contains(session.currentIndex) else { return }
        let view = pageViews[session.currentIndex]
        if view.hasSelectedObject {
            view.objects.deleteSelected()
        } else {
            view.lasso?.deleteSelection()
        }
    }

    /// Nothing is marked dirty here: an undone stroke or lasso edit reports
    /// itself through the canvas and an undone object edit through the
    /// objects layer, each for the page it actually changed -- which, with a
    /// shared undo manager, need not be the page under the viewport.
    private func performUndo() {
        liveCanvas?.undoManager?.undo()
        historyDidMove()
    }

    private func performRedo() {
        liveCanvas?.undoManager?.redo()
        historyDidMove()
    }

    /// A lasso selection is a list of stroke positions and a box drawn round
    /// them; after an undo or a redo neither need describe the page any more.
    private func historyDidMove() {
        for view in pageViews where view.isLive { view.lasso?.clearSelection() }
        refreshCapsule()
    }

    // MARK: - Debug

    /// The simulator has no Pencil and `simctl` cannot synthesise a drag, so a
    /// DEBUG-only launch argument puts one real stroke on page 1. It is the
    /// only way to exercise autosave and the pill from a script.
    private func runDebugStrokeIfRequested() {
        #if DEBUG
            guard CommandLine.arguments.contains("-debugStroke"),
                let first = pageViews.first, let canvas = first.canvas
            else { return }
            let points = stride(from: 0.0, through: 320.0, by: 8.0).map { offset -> PKStrokePoint in
                let wave = sin(offset / 40) * 40
                return PKStrokePoint(
                    location: CGPoint(x: 90 + offset, y: 240 + wave),
                    timeOffset: offset / 320, size: CGSize(width: 5, height: 5), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            let stroke = PKStroke(
                ink: PKInk(.pen, color: UIColor(red: 0x1c / 255, green: 0x1c / 255, blue: 0x1e / 255, alpha: 1)),
                path: PKStrokePath(controlPoints: points, creationDate: Date()))
            canvas.drawing = PKDrawing(strokes: [stroke])
            if let ref = session.ref(at: 0) {
                markDirty(ref)
            }
        #endif
    }

    /// `-debugPages <n>` pads the notebook out so that the scroll view has
    /// somewhere to scroll to. The UI test needs a notebook that can scroll in
    /// order to prove that, in Lasso mode, it does not.
    private func runDebugPagesIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            guard let index = arguments.firstIndex(of: "-debugPages"),
                arguments.indices.contains(index + 1),
                let wanted = Int(arguments[index + 1])
            else { return }
            while session.pages.count < min(wanted, 12) {
                session.addPage(after: session.pages.count - 1)
            }
            scrollTo(index: 0, animated: false)
        #endif
    }

    /// `-debugGrow` writes near the bottom-right of page 1, which is the one
    /// thing an infinite page is supposed to answer by getting bigger.
    private func runDebugGrowIfRequested() {
        #if DEBUG
            guard CommandLine.arguments.contains("-debugGrow"),
                let first = pageViews.first, let canvas = first.canvas
            else { return }
            let extent = first.pageSize
            let baseX: CGFloat = extent.width - 260
            let baseY: CGFloat = extent.height - 90
            let points = stride(from: 0.0, through: 200.0, by: 8.0).map { offset -> PKStrokePoint in
                let location = CGPoint(x: baseX + CGFloat(offset), y: baseY + CGFloat(offset) / 6)
                return PKStrokePoint(
                    location: location,
                    timeOffset: offset / 200, size: CGSize(width: 5, height: 5), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            canvas.drawing = PKDrawing(
                strokes: canvas.drawing.strokes + [
                    PKStroke(
                        ink: PKInk(.pen, color: .black),
                        path: PKStrokePath(controlPoints: points, creationDate: Date()))
                ])
        #endif
    }

    /// Drives one loop through exactly the entry points
    /// `LassoGestureRecognizer` calls, because `simctl` cannot synthesise a
    /// curved drag and `XCUITest` only draws straight lines. The touch
    /// arbitration -- the actual bug -- is covered by the UI test; this covers
    /// what the loop then does with what it caught.
    private func runDebugLassoLoopIfRequested() {
        #if DEBUG
            guard CommandLine.arguments.contains("-debugLassoLoop") else { return }
            tools.select(.lasso)
            applyTool()
            capsule.refresh()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.scrollTo(index: 0, animated: false)
                    self.view.layoutIfNeeded()
                    guard let page = self.pageViews.first, let lasso = page.lasso,
                        let ink = page.drawing?.bounds, ink.width > 1, ink.height > 1
                    else { return }
                    // Round whatever is actually on the page: an infinite page
                    // may have grown and moved the ink since it was put there.
                    let centre = CGPoint(x: ink.midX * self.zoom, y: ink.midY * self.zoom)
                    let radius = (max(ink.width, ink.height) / 2 + 60) * self.zoom
                    func point(_ angle: Double) -> CGPoint {
                        CGPoint(
                            x: centre.x + cos(angle) * radius,
                            y: centre.y + sin(angle) * radius)
                    }
                    lasso.handleTouchBegan(at: point(0))
                    for step in 1...48 {
                        lasso.handleTouchMoved(to: point(Double(step) / 48 * 2 * .pi))
                    }
                    lasso.handleTouchEnded()
                }
            }
        #endif
    }

    /// M5's counterpart: the simulator has no Pencil, so the only scriptable
    /// way to give Vision something to read is to stamp the page with printed
    /// text and let the ordinary render-then-recognise pipeline run.
    private func runDebugHandwritingIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            guard arguments.contains("-debugHandwriting") else { return }
            guard let view = currentPageView else { return }
            let phrase =
                arguments.firstIndex(of: "-debugHandwriting").flatMap { index in
                    arguments.indices.contains(index + 1)
                        && !arguments[index + 1].hasPrefix("-")
                        ? arguments[index + 1] : nil
                } ?? "quarterly review"
            insertTextObject(
                text: phrase,
                at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.25),
                fontSize: 40)
            view.deselectObjects()
        #endif
    }

    /// The M4 counterparts of `-debugStroke`: the simulator has no Pencil, no
    /// camera and no Files app worth scripting, so text, an image, a PDF import
    /// and an export each get a launch argument.
    private func runDebugObjectsIfRequested() {
        #if DEBUG
            let arguments = CommandLine.arguments
            if arguments.contains("-debugTextObject") {
                guard let view = currentPageView else { return }
                insertTextObject(
                    text: "Quarterly review",
                    at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.22))
                view.deselectObjects()
                if let data = Self.debugImagePNG() {
                    insertImage(
                        data: data, name: "debug-swatch.png",
                        at: CGPoint(x: view.pageSize.width / 2, y: view.pageSize.height * 0.62))
                    view.deselectObjects()
                }
            }
            let importing = runDebugImportIfRequested()
            if let index = arguments.firstIndex(of: "-debugExport") {
                let path = arguments.indices.contains(index + 1) ? arguments[index + 1] : ""
                Task { @MainActor [weak self] in
                    // After the import, so the export has its pages in it.
                    await importing?.value
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    await self?.runDebugExport(to: path)
                }
            }
        #endif
    }

    #if DEBUG
        /// `-debugImportPDF [path]`: the file, or a built-in three-page
        /// fixture, imported after the current page and then annotated.
        private func runDebugImportIfRequested() -> Task<Void, Never>? {
            let arguments = CommandLine.arguments
            guard let index = arguments.firstIndex(of: "-debugImportPDF") else { return nil }
            let path = arguments.indices.contains(index + 1) ? arguments[index + 1] : ""
            let data =
                (path.isEmpty ? nil : try? Data(contentsOf: URL(fileURLWithPath: path)))
                ?? Self.debugFixturePDF()
            guard let data else { return nil }
            return Task { @MainActor [weak self] in
                await self?.insertPDF(data: data, name: "debug-fixture.pdf")
                if let self, let view = self.currentPageView {
                    self.annotate(view)
                }
            }
        }

        /// One stroke over whatever page is showing, so an imported PDF page
        /// can be screenshotted with an annotation on it.
        private func annotate(_ view: PageView) {
            guard let canvas = view.canvas else { return }
            let points = stride(from: 0.0, through: 300.0, by: 8.0).map { offset -> PKStrokePoint in
                PKStrokePoint(
                    location: CGPoint(x: 120 + offset, y: 420 + sin(offset / 40) * 30),
                    timeOffset: offset / 300, size: CGSize(width: 6, height: 6), opacity: 1,
                    force: 1, azimuth: 0, altitude: .pi / 2)
            }
            canvas.drawing = PKDrawing(strokes: [
                PKStroke(
                    ink: PKInk(.pen, color: UIColor(red: 0.75, green: 0.12, blue: 0.12, alpha: 1)),
                    path: PKStrokePath(controlPoints: points, creationDate: Date()))
            ])
            if let index = pageViews.firstIndex(where: { $0 === view }),
                let ref = session.ref(at: index)
            {
                markDirty(ref)
            }
        }

        private func runDebugExport(to path: String) async {
            guard let url = await controller.exportPDF(notebook: session.notebook) else {
                print("[paper] export failed")
                return
            }
            var destination = URL(fileURLWithPath: path.isEmpty ? "export.pdf" : path)
            if path.isEmpty || (try? Data(contentsOf: url).write(to: destination)) == nil {
                let documents = FileManager.default.urls(
                    for: .documentDirectory, in: .userDomainMask)
                destination =
                    (documents.first ?? FileManager.default.temporaryDirectory)
                    .appending(path: "export.pdf", directoryHint: .notDirectory)
                try? FileManager.default.removeItem(at: destination)
                try? FileManager.default.copyItem(at: url, to: destination)
            }
            print("[paper] exported to \(destination.path(percentEncoded: false))")
        }

        /// A red square, used as the image object in screenshots.
        private static func debugImagePNG() -> Data? {
            let renderer = UIGraphicsImageRenderer(size: CGSize(width: 240, height: 160))
            return renderer.image { context in
                UIColor(red: 0.85, green: 0.3, blue: 0.2, alpha: 1).setFill()
                context.fill(CGRect(x: 0, y: 0, width: 240, height: 160))
            }.pngData()
        }

        /// A three-page PDF built with the exporter, so the import path has
        /// something real to read without a file in the bundle.
        private static func debugFixturePDF() -> Data? {
            let sizes = [
                CGSize(width: 595, height: 842), CGSize(width: 612, height: 792),
                CGSize(width: 400, height: 400),
            ]
            let texts = ["Alpha page one", "Beta page two", "Gamma page three"]
            let jobs = zip(sizes, texts).map { size, text in
                PageRenderJob(
                    notebookPath: "paper/Fixture.paper", pageID: PaperID.make(), template: .blank,
                    size: .a4, customSize: SizePoints(size),
                    objects: [
                        PageObject(
                            type: .text,
                            frame: PageObject.Frame(
                                x: 40, y: 40, width: Double(size.width) - 80, height: 120),
                            text: text, fontSize: 24)
                    ])
            }
            return try? PDFExporter.pdf(jobs)
        }
    #endif
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

extension NotebookViewController: UIGestureRecognizerDelegate {
    public func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
        // The loop is exclusive: nothing else may act on the touch that is
        // drawing it.
        if gestureRecognizer === lassoGesture || other === lassoGesture { return false }
        return true
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
        refreshCapsule()
    }

    func pageViewDidBeginUsingTool(_ pageView: PageView) {
        drawingPage = pageView
    }

    func pageViewDidEndUsingTool(_ pageView: PageView) {
        drawingPage = nil
        settleAfterStroke(pageView)
        refreshCapsule()
        runPendingRebuild()
    }

    func pageViewToolState(_ pageView: PageView) -> ToolState { tools }

    func pageViewDrawingPolicy(_ pageView: PageView) -> PKCanvasViewDrawingPolicy {
        UserDefaults.standard.bool(forKey: ServerSettings.Key.drawWithFinger) ? .anyInput : .pencilOnly
    }

    func pageView(_ pageView: PageView, didChangeSelection isEmpty: Bool) {
        refreshCapsule()
    }

    func pageView(_ pageView: PageView, didChangeObjects objects: [PageObject]) {
        guard let ref = ref(for: pageView) else { return }
        setObjects(objects, on: ref)
        session.reload()
        if !pageView.isExternallyEditing { growPageIfNeeded(pageView) }
        refreshCapsule()
    }

    func pageView(_ pageView: PageView, imageForAsset assetPath: String) -> UIImage? {
        controller.assetImage(assetPath, notebookPath: session.notebook.path)
    }

    func pageView(_ pageView: PageView, didDropImage data: Data, name: String, at point: CGPoint) {
        guard let index = pageViews.firstIndex(where: { $0 === pageView }) else { return }
        session.currentIndex = index
        insertImage(data: data, name: name, at: point)
    }

    func pageView(_ pageView: PageView, didDropPDF data: Data, name: String) {
        guard let index = pageViews.firstIndex(where: { $0 === pageView }) else { return }
        session.currentIndex = index
        Task { await insertPDF(data: data, name: name) }
    }
}

// MARK: - Toolbar

extension NotebookViewController: ToolCapsuleDelegate {
    func capsule(_ capsule: ToolCapsuleView, didSelect tool: PaperTool) {
        tools.select(tool)
        applyTool()
        refreshCapsule()
    }

    func capsule(_ capsule: ToolCapsuleView, showPresetsFor tool: PaperTool, from source: UIView) {
        presentPresets(for: tool, from: source)
    }

    func capsuleDidTapUndo(_ capsule: ToolCapsuleView) { performUndo() }
    func capsuleDidTapRedo(_ capsule: ToolCapsuleView) { performRedo() }

    func capsuleMenu(_ capsule: ToolCapsuleView) -> UIMenu {
        let hasCamera = UIImagePickerController.isSourceTypeAvailable(.camera)
        let insert = UIMenu(
            title: "", options: .displayInline,
            children: [
                UIAction(title: "Text", image: UIImage(systemName: "textformat")) {
                    [weak self] _ in self?.insertTextObject()
                },
                UIAction(title: "Image", image: UIImage(systemName: "photo")) { [weak self] _ in
                    self?.presentPhotoPicker()
                },
                UIAction(
                    title: "Camera", image: UIImage(systemName: "camera"),
                    // The simulator has no camera; the entry stays where it is
                    // and says so rather than disappearing.
                    attributes: hasCamera ? [] : .disabled
                ) { [weak self] _ in self?.presentCamera() },
                UIAction(
                    title: "Scan document", image: UIImage(systemName: "doc.viewfinder"),
                    attributes: scanDocument == nil ? .disabled : []
                ) { [weak self] _ in self?.presentDocumentScanner() },
                UIAction(title: "PDF", image: UIImage(systemName: "doc")) { [weak self] _ in
                    self?.presentPDFPicker()
                },
                UIAction(title: "Link", image: UIImage(systemName: "link")) { [weak self] _ in
                    self?.presentLinkPrompt()
                },
            ])
        let pages = UIMenu(
            title: "Page", image: UIImage(systemName: "doc.on.doc"),
            children: [
                UIAction(title: "Add page", image: UIImage(systemName: "plus")) { [weak self] _ in
                    guard let self else { return }
                    self.session.addPage(after: self.session.currentIndex)
                },
                UIAction(
                    title: "Delete page", image: UIImage(systemName: "trash"),
                    attributes: session.pages.count > 1 ? [] : .disabled
                ) { [weak self] _ in
                    guard let self else { return }
                    self.session.deletePage(at: self.session.currentIndex)
                },
                templateMenu(),
                UIAction(
                    title: "Export", image: UIImage(systemName: "square.and.arrow.up"),
                    attributes: isExporting ? .disabled : []
                ) { [weak self] _ in
                    guard let self else { return }
                    self.presentExport(from: self.capsule)
                },
            ])
        let more = UIMenu(
            title: "More", image: UIImage(systemName: "ellipsis"),
            children: [
                UIAction(
                    title: "Page thumbnails",
                    state: session.isShowingThumbnails ? .on : .off
                ) { [weak self] _ in
                    self?.session.isShowingThumbnails.toggle()
                },
                UIAction(
                    title: "Paged mode", state: session.isPagedMode ? .on : .off
                ) { [weak self] _ in
                    guard let self else { return }
                    self.session.isPagedMode.toggle()
                    self.scrollView.isPagingEnabled = self.session.isPagedMode
                },
                UIAction(
                    title: "Use system tool picker",
                    state: UserDefaults.standard.bool(forKey: ServerSettings.Key.useSystemToolPicker)
                        ? .on : .off
                ) { _ in
                    let defaults = UserDefaults.standard
                    defaults.set(
                        !defaults.bool(forKey: ServerSettings.Key.useSystemToolPicker),
                        forKey: ServerSettings.Key.useSystemToolPicker)
                },
            ])
        return UIMenu(children: [insert, pages, more])
    }

    /// The current page's ruling. Changing it is a page-level edit, so it
    /// marks the page dirty like any other.
    private func templateMenu() -> UIMenu {
        let current = session.pages.indices.contains(session.currentIndex)
            ? session.pages[session.currentIndex].template : .ruled
        return UIMenu(
            title: "Template", image: UIImage(systemName: "square.grid.3x3"),
            children: Template.allCases.map { template in
                UIAction(title: template.displayName, state: template == current ? .on : .off) {
                    [weak self] _ in self?.setTemplate(template)
                }
            })
    }

    private func presentPresets(for tool: PaperTool, from source: UIView) {
        let picker = ToolPresetsViewController(tools: tools, tool: tool) { [weak self] in
            self?.applyTool()
            self?.refreshCapsule()
        }
        picker.modalPresentationStyle = .popover
        picker.popoverPresentationController?.sourceView = source
        picker.popoverPresentationController?.sourceRect = source.bounds
        picker.popoverPresentationController?.permittedArrowDirections = [.any]
        picker.popoverPresentationController?.delegate = picker
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }
}

// MARK: - Apple Pencil

extension NotebookViewController: UIPencilInteractionDelegate {
    /// Honours the system preference rather than inventing one: a person who
    /// set double-tap to "switch to eraser" everywhere else expects it here.
    public func pencilInteractionDidTap(_ interaction: UIPencilInteraction) {
        switch UIPencilInteraction.preferredTapAction {
        case .switchEraser:
            tools.select(tools.tool == .eraser ? tools.previousTool : .eraser)
        case .switchPrevious:
            tools.switchToPrevious()
        case .showColorPalette, .showInkAttributes, .showContextualPalette:
            presentPresets(for: tools.tool, from: capsule)
        case .ignore, .runSystemShortcut:
            // The system is already doing something with this tap; adding a
            // second meaning to it would be exactly the surprise the research
            // warns about.
            return
        @unknown default:
            tools.switchToPrevious()
        }
        applyTool()
        refreshCapsule()
    }

    public func pencilInteractionDidSqueeze(_ interaction: UIPencilInteraction) {
        guard UIPencilInteraction.preferredSqueezeAction != .ignore else { return }
        presentPresets(for: tools.tool, from: capsule)
    }
}

// MARK: - Objects, import and export

extension NotebookViewController {
    /// The page an insertion lands on: whatever is under the viewport.
    private var currentPageView: PageView? {
        pageViews.indices.contains(session.currentIndex) ? pageViews[session.currentIndex] : nil
    }

    private var currentRef: PageRef? { session.ref(at: session.currentIndex) }

    /// Puts a text object at the last hover point, or the middle of the page,
    /// and opens the keyboard on it. Scribble works because it is a plain
    /// `UITextView`.
    func insertTextObject(
        text: String = "", at insertion: CGPoint? = nil,
        fontSize: Double = PageObject.defaultFontSize
    ) {
        guard let view = currentPageView else { return }
        let point = insertion ?? view.insertionPoint
        let width = min(420, max(160, view.pageSize.width - point.x - 24))
        let object = PageObject(
            type: .text,
            frame: PageObject.Frame(
                x: Double(max(12, point.x - width / 2)), y: Double(max(12, point.y - 20)),
                width: Double(width), height: max(44, fontSize * 1.6)),
            text: text, fontSize: fontSize)
        view.insertObject(object)
        UIAccessibility.post(notification: .layoutChanged, argument: nil)
    }

    /// Stores the bytes as an asset and puts an image object on the page.
    func insertImage(data: Data, name: String, at point: CGPoint? = nil) {
        guard let view = currentPageView, let image = UIImage(data: data) else { return }
        guard
            let assetPath = controller.storeAsset(
                data, name: name, notebookPath: session.notebook.path)
        else {
            presentImageNotAdded(tooLarge: data.count > AssetStore.maxBytes)
            return
        }
        let centre = point ?? view.insertionPoint
        let pixels = CGSize(
            width: image.size.width * image.scale, height: image.size.height * image.scale)
        let frame = ImageObjectView.fittedFrame(
            pixels: pixels, pageSize: view.pageSize, centre: centre)
        view.insertObject(PageObject(type: .image, frame: frame, asset: assetPath))
    }

    /// A photo, paste or drop, as the picture the person saw. Turning it
    /// upright, capping its size and encoding it run off the main actor; only
    /// the insert comes back.
    func importImage(_ image: UIImage, name: String, at point: CGPoint? = nil) {
        guard let cgImage = image.cgImage else {
            presentImageNotAdded(tooLarge: false)
            return
        }
        let orientation = image.imageOrientation
        let scale = image.scale
        Task.detached(priority: .userInitiated) { [weak self] in
            let normalized = ImageImport.normalized(
                cgImage: cgImage, orientation: orientation, scale: scale)
            await self?.insertNormalizedImage(normalized, name: name, at: point)
        }
    }

    private func insertNormalizedImage(
        _ normalized: (data: Data, extension: String)?, name: String, at point: CGPoint?
    ) {
        guard let normalized else {
            presentImageNotAdded(tooLarge: false)
            return
        }
        insertImage(data: normalized.data, name: "\(name).\(normalized.extension)", at: point)
    }

    private func presentImageNotAdded(tooLarge: Bool) {
        let cap = AssetStore.maxBytes / (1024 * 1024)
        let alert = UIAlertController(
            title: "Image not added",
            message: tooLarge
                ? "Images larger than \(cap) MB cannot be added."
                : "The image could not be saved.",
            preferredStyle: .alert)
        alert.addAction(UIAlertAction(title: "OK", style: .default))
        present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func insertPDF(data: Data, name: String) async {
        guard
            let result = await controller.importPDF(
                data: data, name: name, into: session.notebook,
                after: session.pages.indices.contains(session.currentIndex)
                    ? session.pages[session.currentIndex].id : nil)
        else { return }
        session.reload()
        rebuildPages()
        if let first = result.pages.first,
            let index = session.pages.firstIndex(where: { $0.id == first.id })
        {
            session.currentIndex = index
            scrollTo(index: index, animated: false)
        }
        UIAccessibility.post(
            notification: .announcement,
            argument: "Imported \(result.pages.count) pages")
    }

    private func setTemplate(_ template: Template) {
        guard let ref = currentRef,
            var page = try? controller.store.page(notebookPath: ref.notebookPath, id: ref.pageID)
        else { return }
        page.template = template
        page.modified = Date()
        try? controller.store.save(page: page, notebookPath: ref.notebookPath)
        markDirty(ref)
        session.reload()
        currentPageView?.update(page: page)
    }

    // MARK: - Pickers

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

    func presentPDFPicker() {
        let picker = UIDocumentPickerViewController(forOpeningContentTypes: [.pdf])
        picker.allowsMultipleSelection = false
        picker.delegate = self
        present(picker, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    func presentDocumentScanner() {
        guard let scanDocument else { return }
        Task { @MainActor [weak self] in
            guard let self else { return }
            do {
                let data = try await scanDocument(self)
                await self.insertPDF(data: data, name: "Scanned document.pdf")
            } catch {
                // Cancellation needs no error UI; every other scanner failure
                // is useful and stays local to this sheet.
                let nsError = error as NSError
                if nsError.localizedDescription.localizedCaseInsensitiveContains("cancel") {
                    return
                }
                let alert = UIAlertController(
                    title: "Scan unavailable", message: nsError.localizedDescription,
                    preferredStyle: .alert)
                alert.addAction(UIAlertAction(title: "OK", style: .default))
                self.present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
            }
        }
    }

    /// A link is a text object holding a URL: one kind of object to move,
    /// resize and search rather than a second thing that looks like text.
    func presentLinkPrompt() {
        let alert = UIAlertController(title: "Link", message: nil, preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "https://"
            field.keyboardType = .URL
            field.autocapitalizationType = .none
            field.accessibilityLabel = "Link address"
        }
        alert.addAction(UIAlertAction(title: "Cancel", style: .cancel))
        alert.addAction(
            UIAlertAction(title: "Insert", style: .default) { [weak self, weak alert] _ in
                let text = alert?.textFields?.first?.text ?? ""
                guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
                self?.insertTextObject(text: text)
            })
        present(alert, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    /// Cmd-V. Images come off the pasteboard as pictures; anything else is
    /// pasted as text.
    func pasteFromClipboard() {
        let board = UIPasteboard.general
        if board.hasImages, let image = board.image {
            importImage(image, name: "pasted")
        } else if board.hasStrings, let text = board.string, !text.isEmpty {
            insertTextObject(text: text)
        }
    }

    // MARK: - Export

    /// Shares the notebook as a PDF, or the page as a PNG when there is only
    /// one page to share.
    func presentExport(from source: UIView) {
        guard !isExporting else { return }
        isExporting = true
        UIAccessibility.post(notification: .announcement, argument: "Exporting")
        let notebook = session.notebook
        let single = session.pages.count == 1 ? session.pages.first : nil
        Task { @MainActor [weak self] in
            guard let self else { return }
            let url: URL?
            if let single {
                url = await self.controller.exportPNG(page: single, notebook: notebook)
            } else {
                url = await self.controller.exportPDF(notebook: notebook)
            }
            self.isExporting = false
            guard let url else {
                UIAccessibility.post(notification: .announcement, argument: "Export failed")
                return
            }
            // The notebook may have been closed while the pages rendered.
            guard self.viewIfLoaded?.window != nil, self.presentedViewController == nil else {
                return
            }
            let activity = UIActivityViewController(
                activityItems: [url], applicationActivities: nil)
            activity.popoverPresentationController?.sourceView = source
            activity.popoverPresentationController?.sourceRect = source.bounds
            self.present(activity, animated: !UIAccessibility.isReduceMotionEnabled)
        }
    }
}

extension NotebookViewController: PHPickerViewControllerDelegate {
    public func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult])
    {
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
                self?.insertNormalizedImage(normalized, name: name, at: nil)
            }
        }
    }
}

extension NotebookViewController: UIImagePickerControllerDelegate, UINavigationControllerDelegate {
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

extension NotebookViewController: UIDocumentPickerDelegate {
    public func documentPicker(
        _ controller: UIDocumentPickerViewController, didPickDocumentsAt urls: [URL]
    ) {
        guard let url = urls.first else { return }
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard let data = try? Data(contentsOf: url) else { return }
        let name = url.lastPathComponent
        Task { await insertPDF(data: data, name: name) }
    }
}
