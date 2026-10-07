import Observation
import PaperCore
import PencilKit
import UIKit

// MARK: - Selection first

extension NotebookViewController {
    /// Gives `objectTap` its target, follows Read Only, and runs the
    /// selection's DEBUG launch arguments. Called once, from `viewDidLoad`.
    func installSelection() {
        objectTap.addTarget(self, action: #selector(objectTapped(_:)))
        observeReadOnly()
        #if DEBUG
            runDebugSelectionIfRequested()
        #endif
    }

    @objc private func objectTapped(_ gesture: UITapGestureRecognizer) {
        guard gesture.state == .ended else { return }
        selectionTap(at: gesture.location(in: stack))
    }

    // MARK: - Read Only

    /// Read Only switches selection off through one flag,
    /// `session.chrome.isReadOnly` (what `EditorCommands.setReadOnly` sets).
    /// Every selection gesture checks it when it begins, and turning it on
    /// puts down whatever is selected through `setSelectionLocked`.
    private func observeReadOnly() {
        withObservationTracking {
            _ = session.chrome.isReadOnly
        } onChange: { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.setSelectionLocked(self.session.chrome.isReadOnly)
                self.observeReadOnly()
            }
        }
    }

    /// Switches finger selection, the lasso, insert taps and object editing
    /// off or back on. Locking puts every selection down, text being typed
    /// included, and drops a drag in progress where it was picked up.
    func setSelectionLocked(_ locked: Bool) {
        objectTap.isEnabled = !locked
        for view in pageViews {
            for gesture in [view.insertTap, view.shapeDrag, view.longPress] as [UIGestureRecognizer] {
                gesture.isEnabled = !locked
            }
        }
        guard locked else { return }
        activeLasso?.handleTouchCancelled()
        activeLasso = nil
        for view in pageViews {
            view.objects.cancelInteraction()
            view.putDownSelection()
        }
        view.endEditing(true)
    }
}

// MARK: - Shared by the notebook and the whiteboard

/// What selection needs of a canvas controller: its live pages, the view the
/// finger tap's and the lasso recogniser's points are measured in, and the
/// lasso touch in progress. The notebook and the whiteboard both conform, so
/// a tap, a loop and a drag take one path on a page and on a board.
protocol PageSelectionHost: UIViewController {
    var session: NotebookSession { get }
    var onNavigateOnWeb: ((String) -> Void)? { get }
    /// Every page view that may hold a selection.
    var selectablePages: [PageView] { get }
    /// The view `selectionTap`, `lassoShouldBegin` and `routeLassoTouch` take
    /// their points in.
    var selectionSpace: UIView { get }
    /// The overlay a loop or a drag in progress belongs to.
    var activeLasso: LassoOverlayView? { get set }
    /// A stroke is in progress.
    var isDrawing: Bool { get }
}

extension NotebookViewController: PageSelectionHost {
    var selectablePages: [PageView] { pageViews }
    var selectionSpace: UIView { stack }
}

extension PageSelectionHost {
    /// The page under a point of `selectionSpace`, live or not.
    private func page(at point: CGPoint) -> PageView? {
        selectablePages.first { $0.bounds.contains($0.convert(point, from: selectionSpace)) }
    }

    /// A one-finger tap at a point of `selectionSpace`, once the double-tap
    /// has failed: it selects what it lands on, and on bare paper it puts the
    /// selection down (`PageView.handleObjectTap`). The recogniser's action
    /// comes through here, and so do the DEBUG launch arguments and the tests.
    func selectionTap(at point: CGPoint) {
        guard !session.chrome.isReadOnly, !isDrawing else { return }
        guard let page = page(at: point), page.isLive else {
            putDownSelections()
            return
        }
        putDownSelections(except: page)
        page.handleObjectTap(at: page.convert(point, from: selectionSpace))
    }

    /// Puts down every page's selection but `keep`'s: one selection at a time
    /// in a document.
    func putDownSelections(except keep: PageView? = nil) {
        for view in selectablePages where view !== keep && view.hasSelection {
            view.putDownSelection()
        }
    }

    // MARK: Selection keys

    /// The page holding the selection, ink or objects.
    var pageWithSelection: PageView? { selectablePages.first { $0.hasSelection } }

    /// Cmd-C: the selection on the pasteboard, as the object menu's Copy
    /// puts it there. The one selection key Read Only keeps.
    func copySelectionFromKeyboard() {
        if forwardToTextBeingEdited(#selector(UIResponderStandardEditActions.copy(_:))) { return }
        guard let page = pageWithSelection else { return }
        if page.hasSelectedObject {
            page.objects.perform(.copy)
        } else {
            page.lasso?.copySelection()
        }
    }

    /// Cmd-X: the selection on the pasteboard and off the page, one undo step.
    func cutSelectionFromKeyboard() {
        guard !session.chrome.isReadOnly else { return }
        if forwardToTextBeingEdited(#selector(UIResponderStandardEditActions.cut(_:))) { return }
        guard let page = pageWithSelection else { return }
        if page.hasSelectedObject {
            page.objects.perform(.cut)
        } else {
            page.lasso?.cutSelection()
        }
    }

    /// Escape: whatever is selected is put down, ink as well as objects.
    func putDownSelectionFromKeyboard() {
        guard !session.chrome.isReadOnly else { return }
        putDownSelections()
    }

    /// While a text box is being typed in, Cut, Copy and Paste are its own:
    /// the action goes to the text view, and `true` says it was taken.
    func forwardToTextBeingEdited(_ action: Selector) -> Bool {
        guard selectablePages.contains(where: { $0.objects.selectedView?.isEditingContent == true })
        else { return false }
        UIApplication.shared.sendAction(action, to: nil, from: nil, for: nil)
        return true
    }

    /// The live page under a point in `selectionSpace` that may take input.
    private func livePage(at point: CGPoint) -> PageView? {
        guard let view = page(at: point), view.isLive, !view.isExternallyEditing else {
            return nil
        }
        return view
    }

    /// Whether the lasso takes a touch that lands at `point` (`selectionSpace`
    /// coordinates). The Pencil always may: it draws a loop, or carries or
    /// resizes the selection when it lands on it or its handles. A finger is
    /// navigation, so it is taken only to carry or resize a selection --
    /// anywhere else it scrolls -- unless the DEBUG `-debugFingerInk` lets it
    /// draw loops as well. The object menu's buttons are never the lasso's.
    func lassoShouldBegin(at point: CGPoint, touchType: UITouch.TouchType) -> Bool {
        guard session.tools.active == .lasso, !session.chrome.isReadOnly,
            let page = livePage(at: point), let lasso = page.lasso
        else { return false }
        let local = page.convert(point, from: selectionSpace)
        guard !page.objects.menuContains(local), lasso.shouldCaptureTouch(at: local) else {
            return false
        }
        if page.objects.interaction(at: local) != nil { return true }
        if touchType != .direct || EditorInput.fingerInk {
            return true
        }
        return lasso.isOverSelection(local)
    }

    /// The page whose selected object a touch is carrying or resizing.
    private var interactingPage: PageView? { selectablePages.first { $0.objects.isInteracting } }

    /// Hands one step of a lasso touch to the page it began on. The
    /// recogniser's action comes through here, and so do the DEBUG launch
    /// arguments and the tests, so they all take the same path.
    ///
    /// A touch on a selected object or one of its handles moves or resizes
    /// it; on an ink selection or its corners it carries or scales that;
    /// anywhere else it draws a new loop, which puts every selection down.
    func routeLassoTouch(_ touch: LassoTouch) {
        switch touch {
        case .began(let point):
            // A touch that never ended -- the recogniser was switched off
            // under it -- is over.
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
            interactingPage?.objects.cancelInteraction()
            guard session.tools.active == .lasso, let page = livePage(at: point),
                let lasso = page.lasso
            else { return }
            let local = page.convert(point, from: selectionSpace)
            if let interaction = page.objects.interaction(at: local) {
                page.objects.beginInteraction(interaction, at: local)
                return
            }
            guard lasso.shouldCaptureTouch(at: local), !page.objects.menuContains(local) else {
                return
            }
            if !lasso.isOverSelection(local) {
                putDownSelections(except: page)
                page.objects.deselect()
            }
            activeLasso = lasso
            lasso.handleTouchBegan(at: lasso.convert(point, from: selectionSpace))
        case .moved(let point):
            if let page = interactingPage {
                page.objects.updateInteraction(to: page.convert(point, from: selectionSpace))
                return
            }
            guard let lasso = activeLasso else { return }
            lasso.handleTouchMoved(to: lasso.convert(point, from: selectionSpace))
        case .ended:
            if let page = interactingPage {
                page.objects.endInteraction(commit: true)
                return
            }
            activeLasso?.handleTouchEnded()
            activeLasso = nil
        case .cancelled:
            if let page = interactingPage {
                page.objects.endInteraction(commit: false)
                return
            }
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
        }
    }

    /// Ink and objects on white, drawn by the renderer the page's own render
    /// uses, so a copied picture or what OMNIA reads is what the page shows.
    func renderPicture(
        of drawing: PKDrawing, objects: [PageObject], in rect: CGRect, on pageView: PageView
    ) -> UIImage? {
        guard !objects.isEmpty, !rect.isNull, rect.width > 0, rect.height > 0 else { return nil }
        // The objects through the renderer, on white paper with no ruling;
        // the ink over them for the rectangle alone, under the light
        // appearance, rather than for the whole page.
        let job = PageRenderJob(
            notebookPath: session.notebook.path, pageID: pageView.page.id, template: .blank,
            size: pageView.page.size == .board ? .a4 : pageView.page.size,
            customSize: SizePoints(pageView.pageSize), objects: objects,
            vaultURL: session.controller.store.paths.vault, paperColor: .white)
        let scale = min(3, 4096 / max(rect.width, rect.height, 1))
        let format = UIGraphicsImageRendererFormat()
        format.scale = scale
        format.opaque = true
        var ink: UIImage?
        if !drawing.strokes.isEmpty {
            UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
                ink = drawing.image(from: rect, scale: scale)
            }
        }
        return UIGraphicsImageRenderer(size: rect.size, format: format).image { context in
            context.cgContext.saveGState()
            context.cgContext.translateBy(x: -rect.minX, y: -rect.minY)
            PageRenderer.compose(job, in: context.cgContext, inkScale: scale)
            context.cgContext.restoreGState()
            ink?.draw(in: CGRect(origin: .zero, size: rect.size))
        }
    }
}

// MARK: - Lasso

extension NotebookViewController {
    /// A lasso recogniser on the page stack. The loop and the drag win over
    /// the scroll, always: this is the fix for a Pencil drag in Lasso mode
    /// doing nothing but scrolling.
    func makeLassoGesture() -> LassoGestureRecognizer {
        let gesture = LassoGestureRecognizer(target: self, action: #selector(handleLasso))
        gesture.delegate = self
        gesture.isEnabled = false
        gesture.shouldBegin = { [weak self] point, type in
            self?.lassoShouldBegin(at: point, touchType: type) ?? false
        }
        stack.addGestureRecognizer(gesture)
        scrollView.panGestureRecognizer.require(toFail: gesture)
        return gesture
    }

    @objc private func handleLasso(_ gesture: LassoGestureRecognizer) {
        switch gesture.state {
        case .began:
            // A finger begins only after it has travelled: start where it
            // landed, then catch up with where it is.
            let start = gesture.startLocation(in: stack)
            routeLassoTouch(.began(start))
            let now = gesture.trackedLocation(in: stack)
            if now != start { routeLassoTouch(.moved(now)) }
        case .changed:
            routeLassoTouch(.moved(gesture.trackedLocation(in: stack)))
        case .ended:
            routeLassoTouch(.ended)
        case .cancelled, .failed:
            routeLassoTouch(.cancelled)
        default:
            break
        }
    }

}

extension NotebookViewController: UIGestureRecognizerDelegate {
    public func gestureRecognizer(
        _ gestureRecognizer: UIGestureRecognizer,
        shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer
    ) -> Bool {
        // The loop is exclusive: nothing else may act on the touch that is
        // drawing it, nor on the one carrying the selection.
        if gestureRecognizer is LassoGestureRecognizer || other is LassoGestureRecognizer {
            return false
        }
        return true
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
        while let current = view, current !== scrollView {
            if current is ObjectMenu { return false }
            if let text = current as? UITextView, text.isFirstResponder { return false }
            view = current.superview
        }
        return true
    }
}

// MARK: - What pages ask for

extension NotebookViewController {
    /// A tap selected something: the toolbar goes to the Lasso, as the
    /// reference app's does, and the other pages put theirs down.
    func pageView(_ pageView: PageView, didSelect kind: ObjectMenuKind) {
        putDownSelections(except: pageView)
        if tools.active != .lasso { tools.select(.lasso) }
    }

    /// An insert tool placed its object. A shape is done once drawn; a text
    /// box is done when it is put down (`selectionWasPutDown`).
    func pageView(_ pageView: PageView, didPlace type: PageObject.Kind) {
        putDownSelections(except: pageView)
        if type == .shape { tools.finishInsert() }
    }

    func pageViewIsReadOnly(_ pageView: PageView) -> Bool { session.chrome.isReadOnly }

    func pageView(_ pageView: PageView, pasteAt point: CGPoint) {
        paste(onto: pageView, at: point)
    }

    /// The object menu's Share for an image.
    func pageView(_ pageView: PageView, share image: UIImage, from rect: CGRect) {
        let sheet = UIActivityViewController(activityItems: [image], applicationActivities: nil)
        if let popover = sheet.popoverPresentationController {
            popover.sourceView = pageView
            popover.sourceRect = rect
        }
        present(sheet, animated: !UIAccessibility.isReduceMotionEnabled)
    }

    /// Ink and objects on white, for Copy and for OMNIA (`renderPicture`).
    func pageView(
        _ pageView: PageView, render drawing: PKDrawing, objects: [PageObject], in rect: CGRect
    ) -> UIImage? {
        renderPicture(of: drawing, objects: objects, in: rect, on: pageView)
    }
}

// MARK: - Selection actions

extension PageSelectionHost {
    /// The lasso read some handwriting: put the words on the page where the
    /// ink was, at roughly the size it was written.
    func replaceInkWithText(_ text: String, rect: CGRect, on view: PageView) {
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
        // No keyboard: the words are already what the user wrote, and the ink
        // vanishing under a caret is disconcerting.
        view.insertObject(object, edit: false)
        view.deselectObjects()
        UIAccessibility.post(notification: .announcement, argument: "Converted to text")
    }

    func copySelectionLink(rect: CGRect, on view: PageView) {
        guard let page = session.pages.first(where: { $0.id == view.page.id }),
            let url = session.controller.durableLink(
                notebookID: session.notebook.id, pageID: page.id, rect: rect)
        else {
            UIAccessibility.post(notification: .announcement, argument: "Link unavailable")
            return
        }
        UIPasteboard.general.url = url
        UIAccessibility.post(notification: .announcement, argument: "Paper link copied")
    }

    func performSelectionAIAction(
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
                let result = try await self.session.controller.runSelectionAction(
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
                    let thread = self.session.controller.selectionThreads[notebookID]
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
}

// MARK: - Debug

#if DEBUG
    extension NotebookViewController {
        /// The simulator has no Pencil and `simctl` cannot tap a chosen
        /// stroke, so the selection has launch arguments of its own, run once
        /// the first page is live:
        ///
        /// - `-debugPaperColor <name>` colours every page (`white`, `cream`,
        ///   `dark` or any other `PaperColor`), for the dark rule.
        /// - `-debugShape <kind>` places a shape of that kind on page 1 as a
        ///   tap with the Shapes tool does (`+Insert`).
        /// - `-debugTapSelect [ink|text|image|shape]` taps the first thing of
        ///   that kind on page 1 through `selectionTap`, the path a finger
        ///   tap takes: the `-debugStroke` ink by default.
        func runDebugSelectionIfRequested() {
            let arguments = ProcessInfo.processInfo.arguments
            func value(after flag: String) -> String? {
                guard let index = arguments.firstIndex(of: flag) else { return nil }
                let next = index + 1
                return arguments.indices.contains(next) && !arguments[next].hasPrefix("-")
                    ? arguments[next] : ""
            }
            let colour = value(after: "-debugPaperColor")
            let shape = value(after: "-debugShape")
            let tap = value(after: "-debugTapSelect")
            guard colour != nil || shape != nil || tap != nil else { return }
            if let shape { tools.setShapeKind(ShapeKind(rawValue: shape) ?? .rectangle) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    if let colour { self.debugPaint(PaperColor(rawValue: colour) ?? .dark) }
                    self.scrollTo(index: 0, animated: false)
                    self.view.layoutIfNeeded()
                    if shape != nil { self.runDebugShape() }
                    if let tap { self.debugTap(tap.isEmpty ? "ink" : tap) }
                }
            }
        }

        /// Every page in one paper colour, through the store as the page menu
        /// will do it.
        private func debugPaint(_ colour: PaperColor) {
            for page in session.pages {
                _ = try? controller.store.setPageStyle(
                    notebookID: session.notebook.id, pageID: page.id, paperColor: colour)
            }
            session.reload()
            for view in pageViews {
                if let page = try? controller.store.page(
                    notebookPath: session.notebook.path, id: view.page.id)
                {
                    view.update(page: page)
                }
            }
            print("[paper] paper \(colour.rawValue)")
        }

        /// A finger tap on the first thing of a kind on page 1.
        private func debugTap(_ kind: String) {
            guard let page = pageViews.first, page.isLive else { return }
            var target: CGPoint?
            switch kind {
            case "text", "image", "shape":
                if let object = page.page.objects.first(where: { $0.type.rawValue == kind }) {
                    let frame = object.frame.rect.standardized
                    target = CGPoint(x: frame.midX, y: frame.midY)
                }
            default:
                // A point on the first stroke's path, not the middle of its
                // box, which a wave can miss by more than a finger.
                if let stroke = page.drawing?.strokes.first {
                    let points = stroke.path.interpolatedPoints(by: .distance(2)).map {
                        $0.location.applying(stroke.transform)
                    }
                    target = points.isEmpty ? nil : points[points.count / 4]
                }
            }
            guard let target else {
                print("[paper] tap select \(kind): nothing to tap")
                return
            }
            selectionTap(
                at: stack.convert(CGPoint(x: target.x * zoom, y: target.y * zoom), from: page))
            print("[paper] tap select \(kind): \(page.selectionKind?.rawValue ?? "nothing")")
        }
    }
#endif
