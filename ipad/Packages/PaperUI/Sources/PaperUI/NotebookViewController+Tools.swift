import PaperCore
import PencilKit
import SwiftUI
import UIKit

// MARK: - Settings

extension NotebookViewController {
    /// The input policy and the tool, applied to every page. Tool changes
    /// after this arrive through `toolsDidChange`.
    func applySettings() {
        // A finger never inks: the Pencil draws, one finger pans. Only the
        // DEBUG `-debugFingerInk` lets a finger ink, and then panning takes two.
        scrollView.panGestureRecognizer.minimumNumberOfTouches = EditorInput.fingerInk ? 2 : 1
        let policy = EditorInput.drawingPolicy
        for view in pageViews { view.setDrawingPolicy(policy) }
        applyTool()
        refreshUndoState()
    }

    /// Follows the window's `ToolState` from now on.
    func observeTools() {
        guard toolObserverID == nil else { return }
        toolObserverID = tools.addObserver { [weak self] old, new in
            self?.toolsDidChange(from: old, to: new)
        }
    }

    func stopObservingTools() {
        guard let toolObserverID else { return }
        tools.removeObserver(toolObserverID)
        self.toolObserverID = nil
    }

    /// The toolbar, the tool menu or the Pencil changed the tools. What the
    /// canvases draw with follows; a change of tool also goes through
    /// `setLassoActive`, which puts a lasso drag in progress back.
    private func toolsDidChange(from old: ToolSettings, to new: ToolSettings) {
        if old.canvasTool != new.canvasTool {
            let tool = tools.pkTool
            for view in pageViews { view.setTool(tool) }
        }
        guard old.active != new.active else { return }
        for view in pageViews { configureTool(on: view) }
        updateLassoGesture()
        updateEditorGestures()
        if new.active == .image { pickImageForTool() }
    }

    /// Everything about the tool, on every page.
    func applyTool() {
        let tool = tools.pkTool
        for view in pageViews {
            view.setTool(tool)
            configureTool(on: view)
        }
        updateLassoGesture()
        updateEditorGestures()
    }

    /// One page's share of the tool: the lasso overlay, and no ink while an
    /// insert tool is waiting to place its object. In Read Only the page takes
    /// no touches at all -- no ink, lasso, selection, insert or object edit --
    /// and fingers scroll and zoom it from the scroll view.
    func configureTool(on view: PageView) {
        let readOnly = session.chrome.isReadOnly
        view.setLassoActive(tools.active == .lasso && !readOnly)
        view.canvas?.drawingGestureRecognizer.isEnabled = !tools.active.isInsert && !readOnly
        view.isUserInteractionEnabled = !readOnly
    }

    /// The loop recogniser exists only while the Lasso is selected, so every
    /// other tool leaves one-finger scrolling exactly as it was. The loop
    /// belongs to the Pencil; a finger on the selection carries it through
    /// its own recogniser, and anywhere else keeps scrolling.
    func updateLassoGesture() {
        guard let lassoGesture else { return }
        let lasso = tools.active == .lasso && !session.chrome.isReadOnly
        if !lasso {
            activeLasso?.handleTouchCancelled()
            activeLasso = nil
        }
        lassoGesture.isEnabled = lasso
        let pencil = NSNumber(value: UITouch.TouchType.pencil.rawValue)
        let direct = NSNumber(value: UITouch.TouchType.direct.rawValue)
        lassoGesture.allowedTouchTypes = EditorInput.fingerInk ? [direct, pencil] : [pencil]
        fingerLassoGesture?.isEnabled = lasso && !EditorInput.fingerInk
        fingerLassoGesture?.allowedTouchTypes = [direct]
    }

    /// Undo and redo for the chrome, written only when they change: this runs
    /// on every stroke, and every write redraws whatever shows it.
    func refreshUndoState() {
        let manager = liveCanvas?.undoManager
        let canUndo = manager?.canUndo ?? false
        let canRedo = manager?.canRedo ?? false
        if session.chrome.canUndo != canUndo { session.chrome.canUndo = canUndo }
        if session.chrome.canRedo != canRedo { session.chrome.canRedo = canRedo }
    }

    var liveCanvas: PKCanvasView? {
        guard pageViews.indices.contains(session.currentIndex) else {
            return pageViews.first(where: { $0.canvas != nil })?.canvas
        }
        return pageViews[session.currentIndex].canvas
            ?? pageViews.first(where: { $0.canvas != nil })?.canvas
    }

    var isCurrentPageDark: Bool {
        currentPageView.map { PaperAppearance.isDark(page: $0.page) } ?? false
    }
}

// MARK: - Insert tools

extension NotebookViewController {
    /// The Image tool is the photo picker: it places nothing by hand, so the
    /// toolbar is back on the lasso as soon as the picker is up. Only the
    /// window in front opens one; the others share the tool and stay put.
    private func pickImageForTool() {
        if view.window?.isKeyWindow == true, presentedViewController == nil,
            !session.chrome.isReadOnly
        {
            presentPhotoPicker()
        }
        // After every window has heard about the image tool.
        DispatchQueue.main.async { [weak self] in
            MainActor.assumeIsolated { self?.tools.finishInsert() }
        }
    }

    /// A selection was put down. When that was the text box the Text tool
    /// placed, the tool is done and falls back to the lasso.
    func selectionWasPutDown() {
        if tools.active == .text { tools.finishInsert() }
    }
}

// MARK: - Draw Shape

extension NotebookViewController {
    /// The Pencil went down with Draw Shape: how many strokes the page had.
    func drawShapeStrokeBegan(on view: PageView) {
        guard tools.active == .drawShape, let canvas = view.canvas else {
            drawShapeStart = nil
            return
        }
        drawShapeStart = (view.page.id, canvas.drawing.strokes.count)
    }

    /// The Pencil lifted. The stroke and PencilKit's own undo for it are
    /// committed by the next turn of the run loop; the clean shape replaces it
    /// then, as an undo step of its own, so one undo brings the rough stroke
    /// back.
    func drawShapeStrokeEnded(on view: PageView) {
        guard let start = drawShapeStart, start.pageID == view.page.id else { return }
        drawShapeStart = nil
        DispatchQueue.main.async { [weak self, weak view] in
            MainActor.assumeIsolated {
                guard let self, let view else { return }
                self.cleanUpShape(on: view, strokesBefore: start.strokeCount)
            }
        }
    }

    /// Runs the newest stroke through the recogniser and, on a match, swaps
    /// it for clean strokes with the same ink: one per closed outline, two for
    /// an arrow. A stroke that is no shape stays as drawn.
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
        guard let shape = ShapeRecognizer.recognize(points: points, zoom: zoom) else { return nil }
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

    /// The eraser's Clear Page: every stroke on the page goes, as one undo
    /// step. Objects stay.
    func clearInk(onPageAt index: Int) {
        guard pageViews.indices.contains(index), let canvas = pageViews[index].canvas,
            !pageViews[index].isExternallyEditing, !canvas.drawing.strokes.isEmpty
        else { return }
        // Puts a lasso drag in progress back before the strokes are read.
        pageViews[index].lasso?.clearSelection()
        replaceInk(on: pageViews[index], with: PKDrawing(), actionName: "Clear Page")
    }

    /// Replaces a page's ink as a user edit: reported like a stroke, so it is
    /// saved, and undone by putting the old ink back.
    func replaceInk(on view: PageView, with drawing: PKDrawing, actionName: String) {
        guard let canvas = view.canvas else { return }
        registerInkRestore(
            of: canvas.drawing, capturedAt: view.growthTranslation, on: view,
            actionName: actionName)
        canvas.drawing = drawing
    }

    /// Registers putting `drawing` back, and makes putting it back register
    /// the way forward again. An infinite page that grew since moves it by the
    /// growth, as the lasso's own undo does.
    private func registerInkRestore(
        of drawing: PKDrawing, capturedAt base: CGSize, on view: PageView, actionName: String
    ) {
        guard let canvas = view.canvas, let manager = canvas.undoManager else { return }
        manager.registerUndo(withTarget: canvas) { [weak self, weak view] target in
            MainActor.assumeIsolated {
                let now = view?.growthTranslation ?? base
                let replaced = target.drawing
                let delta = CGSize(width: now.width - base.width, height: now.height - base.height)
                view?.lasso?.clearSelection()
                target.drawing =
                    delta == .zero
                    ? drawing
                    : drawing.transformed(
                        using: CGAffineTransform(translationX: delta.width, y: delta.height))
                if let self, let view {
                    self.registerInkRestore(
                        of: replaced, capturedAt: now, on: view, actionName: actionName)
                }
            }
        }
        manager.setActionName(actionName)
    }

    /// `-debugDrawShape [rectangle|ellipse|triangle|line|arrow]` selects Draw
    /// Shape, puts one rough stroke of that shape on page 1 and sends it
    /// through the same clean-up the Pencil's lift does. The simulator has
    /// no Pencil and `simctl` cannot draw.
    func runDebugDrawShapeIfRequested() {
        #if DEBUG
            let arguments = ProcessInfo.processInfo.arguments
            guard let position = arguments.firstIndex(of: "-debugDrawShape") else { return }
            let name =
                arguments.indices.contains(position + 1) && !arguments[position + 1].hasPrefix("-")
                ? arguments[position + 1] : "rectangle"
            tools.select(.drawShape)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.scrollTo(index: 0, animated: false)
                    guard let page = self.pageViews.first, let canvas = page.canvas else { return }
                    let ink = self.tools.pkTool as? PKInkingTool
                    let width = ink?.width ?? 2
                    let points = Self.debugRoughShape(name).enumerated().map { index, point in
                        PKStrokePoint(
                            location: point, timeOffset: Double(index) * 0.01,
                            size: CGSize(width: width, height: width), opacity: 1, force: 1,
                            azimuth: 0, altitude: .pi / 2)
                    }
                    let rough = PKStroke(
                        ink: PKInk(.monoline, color: ink?.color ?? .black),
                        path: PKStrokePath(controlPoints: points, creationDate: Date()))
                    let before = canvas.drawing.strokes.count
                    canvas.drawing = PKDrawing(strokes: canvas.drawing.strokes + [rough])
                    let shape = self.cleanUpShape(on: page, strokesBefore: before)
                    print("[paper] draw shape \(name): \(shape?.kind.rawValue ?? "kept as drawn")")
                }
            }
        #endif
    }

    #if DEBUG
        /// A shape as a hand draws it: wobbling sides, softened corners, and
        /// an end that stops short of the start. Page points.
        static func debugRoughShape(_ name: String) -> [CGPoint] {
            func wobble(_ step: Int) -> CGFloat {
                CGFloat(sin(Double(step) * 0.31) * 3 + sin(Double(step) * 0.83) * 1.5)
            }
            func walk(_ corners: [CGPoint], closed: Bool) -> [CGPoint] {
                var points: [CGPoint] = []
                let path = closed ? corners + [corners[0]] : corners
                for (start, end) in zip(path, path.dropFirst()) {
                    let length = hypot(end.x - start.x, end.y - start.y)
                    let steps = max(Int(length / 6), 1)
                    for step in 0..<steps {
                        let t = CGFloat(step) / CGFloat(steps)
                        let n = points.count
                        points.append(
                            CGPoint(
                                x: start.x + (end.x - start.x) * t + wobble(n),
                                y: start.y + (end.y - start.y) * t + wobble(n + 7)))
                    }
                }
                if closed, let first = points.first {
                    // Stops a little short of where it began.
                    points.append(CGPoint(x: first.x + 9, y: first.y + 5))
                } else if let last = corners.last {
                    points.append(last)
                }
                return points
            }
            // Inside an A4 sheet (595 x 842), the smallest page there is.
            switch name {
            case "ellipse":
                return (0...110).map { step in
                    let angle = Double(step) / 112 * 2 * .pi
                    let radius = 1 + sin(Double(step) * 0.5) * 0.015
                    return CGPoint(
                        x: 300 + cos(angle) * 170 * radius, y: 360 + sin(angle) * 110 * radius)
                }
            case "triangle":
                return walk(
                    [CGPoint(x: 300, y: 200), CGPoint(x: 480, y: 480), CGPoint(x: 120, y: 470)],
                    closed: true)
            case "line":
                return walk([CGPoint(x: 100, y: 300), CGPoint(x: 480, y: 360)], closed: false)
            case "arrow":
                return walk(
                    [
                        CGPoint(x: 100, y: 440), CGPoint(x: 440, y: 300), CGPoint(x: 393, y: 290),
                        CGPoint(x: 440, y: 300), CGPoint(x: 411, y: 341),
                    ], closed: false)
            default:
                return walk(
                    [
                        CGPoint(x: 110, y: 220), CGPoint(x: 470, y: 212), CGPoint(x: 478, y: 470),
                        CGPoint(x: 118, y: 478),
                    ], closed: true)
            }
        }
    #endif
}

// MARK: - The Pencil's tool menu

/// The tool menu as the Pencil's palette, in a popover at the tip.
final class ToolPaletteController: UIHostingController<ToolMenuView> {}

extension NotebookViewController: UIPopoverPresentationControllerDelegate {
    /// Shows the tool menu at a point of this view, or brings the docked menu
    /// back when there is no point to show it at. A second call closes it.
    func presentToolMenu(at point: CGPoint?) {
        if let open = presentedViewController as? ToolPaletteController {
            open.dismiss(animated: !UIAccessibility.isReduceMotionEnabled)
            return
        }
        // Read Only has no tools to set.
        guard !session.chrome.isReadOnly else { return }
        guard let point, presentedViewController == nil else {
            tools.setMenuVisible(true)
            return
        }
        let palette = ToolPaletteController(
            rootView: ToolMenuView(
                tools: tools, style: .popover, isDarkPaper: isCurrentPageDark,
                onClearPage: { [weak self] in
                    guard let self else { return }
                    self.clearInk(onPageAt: self.session.currentIndex)
                }))
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

    /// A popover on iPad stays a popover.
    public func adaptivePresentationStyle(
        for controller: UIPresentationController, traitCollection: UITraitCollection
    ) -> UIModalPresentationStyle {
        .none
    }
}
