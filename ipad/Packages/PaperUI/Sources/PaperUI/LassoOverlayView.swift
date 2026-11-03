import PaperCore
import PencilKit
import UIKit

/// The focused OMNIA workflows offered for a lasso selection. These are
/// intents, not destructive drawing operations: selected ink stays exactly
/// where it is and the server owns approval/Undo for any vault mutation.
enum SelectionAIAction: String, CaseIterable, Sendable {
    case ask = "ask"
    case transcribe = "transcribe"
    case makeNote = "note"
    case createTask = "task"
    case makeFlashcards = "flashcards"

    var title: String {
        switch self {
        case .ask: return "Ask OMNIA"
        case .transcribe: return "Transcribe"
        case .makeNote: return "Make note"
        case .createTask: return "Create task"
        case .makeFlashcards: return "Make flashcards"
        }
    }

    var symbol: String {
        switch self {
        case .ask: return "sparkles"
        case .transcribe: return "text.viewfinder"
        case .makeNote: return "note.text.badge.plus"
        case .createTask: return "checkmark.circle"
        case .makeFlashcards: return "rectangle.stack.badge.plus"
        }
    }
}

/// One drag of a lasso selection, from the touch that picks it up to the one
/// that puts it down, or the cancel that takes it away.
///
/// Bookkeeping over the real `PKDrawing` the drag began with, so the tests can
/// drive it without a touch. `resting` is the page's ink for the whole of the
/// drag: what autosave writes meanwhile, what a cancel puts back, and what the
/// drop's single undo step returns to.
struct SelectionDrag {
    let resting: PKDrawing
    let selection: [Int]
    /// Where the touch picked the selection up, in overlay points.
    let origin: CGPoint
    let zoom: CGFloat
    /// Where the touch is now, in overlay points.
    private(set) var location: CGPoint

    init(resting: PKDrawing, selection: [Int], origin: CGPoint, zoom: CGFloat) {
        self.resting = resting
        self.selection = selection
        self.origin = origin
        self.zoom = zoom
        self.location = origin
    }

    mutating func move(to point: CGPoint) {
        location = point
    }

    /// How far the touch has carried the selection, in overlay points.
    var viewTranslation: CGSize {
        CGSize(width: location.x - origin.x, height: location.y - origin.y)
    }

    /// The same distance on the page, which is what the strokes move by.
    var pageTranslation: CGSize {
        let scale = zoom == 0 ? 1 : zoom
        return CGSize(width: viewTranslation.width / scale, height: viewTranslation.height / scale)
    }

    /// The page without the selection: what the canvas shows while the
    /// selection is carried above it.
    var remainder: PKDrawing {
        let chosen = Set(selection)
        return PKDrawing(
            strokes: resting.strokes.enumerated().filter { !chosen.contains($0.offset) }.map(\.element))
    }

    /// The selection alone, where it started.
    var carried: PKDrawing {
        let strokes = resting.strokes
        return PKDrawing(strokes: selection.filter(strokes.indices.contains).map { strokes[$0] })
    }

    /// The page with the selection put down where the touch is. The order of
    /// the strokes is kept, so the selection's indices name the same strokes.
    var dropped: PKDrawing {
        let move = CGAffineTransform(
            translationX: pageTranslation.width, y: pageTranslation.height)
        var strokes = resting.strokes
        for index in selection where strokes.indices.contains(index) {
            strokes[index].transform = strokes[index].transform.concatenating(move)
        }
        return PKDrawing(strokes: strokes)
    }
}

/// One drag of a corner handle of a lasso selection: the selection scaled
/// proportionally about the opposite corner, as the reference app's white
/// corner handles do.
///
/// Bookkeeping like `SelectionDrag`: `resting` is the page's ink for the whole
/// drag, and only the drop writes.
struct SelectionScale {
    let resting: PKDrawing
    let selection: [Int]
    /// The opposite corner of the selection's outline, in overlay points.
    let anchor: CGPoint
    /// The corner the touch took, in overlay points.
    let corner: CGPoint
    let zoom: CGFloat
    /// Where the touch is now, in overlay points.
    private(set) var location: CGPoint

    /// The smallest a selection is scaled to.
    static let minimumFactor: CGFloat = 0.1

    init(resting: PKDrawing, selection: [Int], anchor: CGPoint, corner: CGPoint, zoom: CGFloat) {
        self.resting = resting
        self.selection = selection
        self.anchor = anchor
        self.corner = corner
        self.zoom = zoom
        self.location = corner
    }

    mutating func move(to point: CGPoint) {
        location = point
    }

    /// How much bigger the selection is: the touch's travel along the
    /// diagonal through the anchor.
    var factor: CGFloat {
        let diagonal = CGSize(width: corner.x - anchor.x, height: corner.y - anchor.y)
        let length = diagonal.width * diagonal.width + diagonal.height * diagonal.height
        guard length > 0 else { return 1 }
        let now = CGSize(width: location.x - anchor.x, height: location.y - anchor.y)
        return max(
            Self.minimumFactor, (now.width * diagonal.width + now.height * diagonal.height) / length)
    }

    /// The scale in overlay points.
    var viewTransform: CGAffineTransform {
        Self.scale(factor, about: anchor)
    }

    /// The same scale on the page, which is what the strokes and objects take.
    var pageTransform: CGAffineTransform {
        let scale = zoom == 0 ? 1 : zoom
        return Self.scale(factor, about: CGPoint(x: anchor.x / scale, y: anchor.y / scale))
    }

    static func scale(_ factor: CGFloat, about point: CGPoint) -> CGAffineTransform {
        CGAffineTransform(translationX: -point.x, y: -point.y)
            .concatenating(CGAffineTransform(scaleX: factor, y: factor))
            .concatenating(CGAffineTransform(translationX: point.x, y: point.y))
    }

    /// The page with the selection scaled. Stroke order is kept.
    var scaled: PKDrawing {
        let transform = pageTransform
        var strokes = resting.strokes
        for index in selection where strokes.indices.contains(index) {
            strokes[index].transform = strokes[index].transform.concatenating(transform)
        }
        return PKDrawing(strokes: strokes)
    }

    /// An object's frame under a page transform made of a uniform scale and a
    /// move. A line's direction -- a negative width or height -- is kept, and
    /// a text object's type grows with its box.
    static func transformed(_ object: PageObject, by transform: CGAffineTransform) -> PageObject {
        var next = object
        let origin = CGPoint(x: object.frame.x, y: object.frame.y).applying(transform)
        let factor = Double(sqrt(abs(transform.a * transform.d - transform.b * transform.c)))
        next.frame = PageObject.Frame(
            x: Double(origin.x), y: Double(origin.y), width: object.frame.width * factor,
            height: object.frame.height * factor)
        if object.type == .text, factor != 1 {
            next.fontSize = min(max((object.fontSize ?? PageObject.defaultFontSize) * factor, 4), 400)
        }
        return next
    }
}

/// Selection, built by hand.
///
/// PencilKit ships a lasso in `PKToolPicker` but exposes no selection API at
/// all, so a toolbar of our own has to draw the loop, decide what it caught,
/// and rebuild the drawing for every operation. Every one of those rebuilds is
/// registered on the canvas's own `undoManager`, so Cmd-Z behaves the same
/// whether the last thing you did was write a word or recolour a paragraph.
///
/// The overlay holds a selection of ink -- by a loop, or by a finger tap on a
/// word -- and of objects a loop caught along with it (a mixed selection). A
/// single object on its own is the objects layer's selection instead, with its
/// own handles. Either way the object menu floats over it.
final class LassoOverlayView: UIView {
    /// A stroke counts as selected when at least this much of it is inside the
    /// loop. Half is forgiving enough for a quick circle round a word without
    /// dragging in the line below.
    static let insideRatio = 0.5

    private weak var canvas: PKCanvasView?
    private let loopLayer = CAShapeLayer()
    private let selectionLayer = CAShapeLayer()
    private let handlesLayer = CAShapeLayer()
    let menu = ObjectMenu()

    private var loopPoints: [CGPoint] = []
    private(set) var selection: [Int] = []
    /// Objects a loop caught along with ink, or more than one of them.
    private(set) var selectedObjects: [String] = []
    private(set) var selectionBounds: CGRect = .zero

    /// The drag in progress, if a touch picked the selection up.
    private(set) var drag: SelectionDrag?
    /// A corner handle being dragged.
    private(set) var scale: SelectionScale?
    /// Whether that drag has taken the selection off the canvas yet. A touch
    /// that lands on the selection and lifts without travelling never does.
    private(set) var isLifted = false
    /// The selection's outline when the drag began, for a cancel to go back to.
    private var restingBounds: CGRect = .zero
    /// Where the carried ink sat when it was lifted, in overlay points.
    private var restingInkFrame: CGRect = .zero
    /// The selected ink while a drag carries it: drawn once when it is lifted,
    /// then moved with the touch. Moving an image keeps up with the Pencil;
    /// rewriting the whole canvas on every move would not.
    private let carriedInk = UIImageView()
    /// Clips the carried ink to the page, as the canvas does once it lands:
    /// ink carried past the edge of a fixed page is ink that will not show.
    private let carriedClip = UIView()
    /// How far the selection travels before it is lifted, in overlay points.
    /// Below this a touch on the selection is a tap that changes nothing.
    static let liftDistance: CGFloat = 2
    /// How close to a corner of the outline a touch takes that corner.
    static let handleReach: CGFloat = 22

    var zoom: CGFloat = 1 {
        didSet {
            guard zoom != oldValue, drag == nil, scale == nil, hasSelection else { return }
            recomputeBounds()
            settle(at: selectionBounds)
        }
    }
    /// The page is dark paper: the carried ink is drawn as the canvas draws it.
    var isDarkPaper = false
    /// How far growth has moved the page's content so far. Set by `PageView`.
    var growthOffset: (() -> CGSize)?
    /// Replaces the canvas's ink without reporting an edit. Set by `PageView`:
    /// lifting the selection off the canvas and putting it back are not edits,
    /// and only the drop is saved or undone.
    var setDrawingQuietly: ((PKDrawing) -> Void)?
    var onSelectionChange: ((Bool) -> Void)?
    /// M5: the selection was read as text and should become a text object at
    /// the rectangle it occupied. Set by `PageView`.
    var onConvertToText: ((String, CGRect) -> Void)?
    /// A PNG crop and its page-space bounds, ready for a native Paper action.
    /// The callback never removes or rewrites the selected strokes.
    var onAIAction: ((SelectionAIAction, Data, CGRect) -> Void)?
    var onCopyLink: ((CGRect) -> Void)?
    /// The page's objects, for a loop to catch and a mixed selection to read.
    var objectsProvider: (() -> [PageObject])?
    /// Shows objects carried by a page transform while a drag lasts; nil puts
    /// them back. Changes nothing.
    var previewObjects: (([String], CGAffineTransform?) -> Void)?
    /// Writes the page's objects as part of the current undo step.
    var commitObjects: (([PageObject], String) -> Void)?
    /// A loop caught one object and no ink: the objects layer selects it.
    var onSelectObject: ((String) -> Void)?
    /// Cut or Copy: the strokes, the objects and a picture of them.
    var onCopy: ((PKDrawing, [PageObject], UIImage?) -> Void)?
    /// A picture of the strokes and objects over `rect` (page points) on
    /// white, for OMNIA. Without it only the ink is drawn.
    var renderSelection: ((PKDrawing, [PageObject], CGRect) -> UIImage?)?
    /// True while Vision is reading the selection, so a second tap does not
    /// start a second read of ink that is about to disappear.
    private var isConverting = false

    init(canvas: PKCanvasView) {
        self.canvas = canvas
        super.init(frame: .zero)
        backgroundColor = .clear
        isUserInteractionEnabled = false

        loopLayer.fillColor = UIColor(PaperTokens.accent).withAlphaComponent(0.06).cgColor
        loopLayer.strokeColor = UIColor(PaperTokens.accent).cgColor
        loopLayer.lineWidth = 1.5
        loopLayer.lineDashPattern = [4, 4]
        layer.addSublayer(loopLayer)

        selectionLayer.fillColor = UIColor.clear.cgColor
        selectionLayer.strokeColor = UIColor(PaperTokens.accent).cgColor
        selectionLayer.lineWidth = 1.5
        selectionLayer.lineDashPattern = [6, 4]
        layer.addSublayer(selectionLayer)

        handlesLayer.fillColor = UIColor.white.cgColor
        handlesLayer.strokeColor = UIColor(PaperTokens.accent).cgColor
        handlesLayer.lineWidth = 1
        layer.addSublayer(handlesLayer)

        // Under the outline, which is drawn round it.
        carriedInk.isHidden = true
        carriedInk.isAccessibilityElement = false
        carriedClip.clipsToBounds = true
        carriedClip.isUserInteractionEnabled = false
        carriedClip.addSubview(carriedInk)
        insertSubview(carriedClip, at: 0)

        menu.isHidden = true
        menu.onAction = { [weak self] action in self?.perform(action) }
        addSubview(menu)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("LassoOverlayView is created in code") }

    override func layoutSubviews() {
        super.layoutSubviews()
        carriedClip.frame = bounds
    }

    var hasSelection: Bool { !selection.isEmpty || !selectedObjects.isEmpty }

    /// What the menu offers: ink alone, or ink and objects together.
    var menuKind: ObjectMenuKind? {
        guard hasSelection else { return nil }
        return selectedObjects.isEmpty ? .ink : .mixed
    }

    // MARK: - Loop capture

    /// The loop is captured by `LassoGestureRecognizer`, which the notebook's
    /// scroll view is made to wait for, rather than by raw `touchesBegan`.
    ///
    /// This is the whole of the lasso bug: a `UIScrollView` cancels the touches
    /// it delivers to its content as soon as its own pan recognises, and on
    /// hardware an Apple Pencil drives that pan like any other touch. The
    /// overlay was therefore handed a `touchesBegan`, then a `touchesCancelled`
    /// a moment later, and the page scrolled instead.
    ///
    /// The object menu, while it is on screen, so the page above can publish
    /// it to VoiceOver. A page is an accessibility element in its own right,
    /// which would otherwise hide every control inside it.
    var accessibleInspector: UIView? { menu.isHidden ? nil : menu }

    /// Whether a touch should reach the loop at all -- rather than the buttons
    /// of the object menu floating above it.
    func shouldCaptureTouch(at point: CGPoint) -> Bool {
        !menuContains(point)
    }

    func menuContains(_ point: CGPoint) -> Bool {
        !menu.isHidden && menu.frame.insetBy(dx: -4, dy: -4).contains(point)
    }

    /// Whether a touch at `point` picks the selection up, rather than starting
    /// a new loop. A finger that lands anywhere else is the scroll view's.
    func isOverSelection(_ point: CGPoint) -> Bool {
        hasSelection
            && (selectionBounds.insetBy(dx: -12, dy: -12).contains(point) || handle(at: point) != nil)
    }

    /// The corners of the outline, which scale the selection.
    private var handleCorners: [CGPoint] {
        guard hasSelection, !selectionBounds.isNull else { return [] }
        let outline = selectionBounds.insetBy(dx: -6, dy: -6)
        return [
            CGPoint(x: outline.minX, y: outline.minY), CGPoint(x: outline.maxX, y: outline.minY),
            CGPoint(x: outline.maxX, y: outline.maxY), CGPoint(x: outline.minX, y: outline.maxY),
        ]
    }

    /// The index of the corner handle under `point`, nearest first. A touch
    /// inside the selection carries it, however small it is.
    func handle(at point: CGPoint) -> Int? {
        guard !selectionBounds.contains(point) else { return nil }
        var best: (Int, CGFloat)?
        for (index, corner) in handleCorners.enumerated() {
            let distance = hypot(point.x - corner.x, point.y - corner.y)
            if distance <= Self.handleReach, distance < (best?.1 ?? .infinity) {
                best = (index, distance)
            }
        }
        return best?.0
    }

    /// The four entry points `LassoGestureRecognizer` drives, through
    /// `NotebookViewController.routeLassoTouch`. A touch that lands on a
    /// corner scales the selection, on the selection carries it, and anywhere
    /// else draws a new loop.
    func handleTouchBegan(at point: CGPoint) {
        // A drag the last touch never finished goes back where it started.
        cancelDrag()
        if let corner = handle(at: point), let canvas {
            let corners = handleCorners
            let opposite = corners[(corner + 2) % 4]
            let grabbed = corners[corner]
            restingBounds = selectionBounds
            scale = SelectionScale(
                resting: canvas.drawing, selection: selection, anchor: opposite, corner: grabbed,
                zoom: zoom)
            return
        }
        if isOverSelection(point), let canvas {
            drag = SelectionDrag(
                resting: canvas.drawing, selection: selection, origin: point, zoom: zoom)
            restingBounds = selectionBounds
            return
        }
        clearSelection()
        loopPoints = [point]
    }

    func handleTouchMoved(to point: CGPoint) {
        if var scale {
            scale.move(to: point)
            self.scale = scale
            if !isLifted {
                guard hypot(point.x - scale.corner.x, point.y - scale.corner.y) >= Self.liftDistance
                else { return }
                lift(carried: carriedDrawing(scale.resting, scale.selection), resting: scale.resting)
            }
            showScale(scale)
            return
        }
        if var drag {
            drag.move(to: point)
            self.drag = drag
            if !isLifted {
                let travel = drag.viewTranslation
                guard max(abs(travel.width), abs(travel.height)) >= Self.liftDistance else { return }
                lift(carried: drag.carried, resting: drag.resting)
            }
            carry(drag)
            return
        }
        loopPoints.append(point)
        loopLayer.path = Self.path(through: loopPoints, closed: false)
    }

    func handleTouchEnded() {
        if let scale {
            dropScale(scale)
            return
        }
        if let drag {
            drop(drag)
            return
        }
        loopLayer.path = nil
        defer { loopPoints = [] }
        guard loopPoints.count > 2 else { return }
        selectStrokes(inside: loopPoints)
    }

    /// The system took the touch away -- a notification pulled down, a call,
    /// the app sent to the background. The person never let go, so no drop
    /// point was chosen: the selection goes back where it started, with its
    /// outline. That is also what the page held on disk throughout.
    func handleTouchCancelled() {
        cancelDrag()
        loopPoints = []
        loopLayer.path = nil
    }

    // MARK: - Drag

    private func carriedDrawing(_ resting: PKDrawing, _ selection: [Int]) -> PKDrawing {
        let strokes = resting.strokes
        return PKDrawing(strokes: selection.filter(strokes.indices.contains).map { strokes[$0] })
    }

    private func remainder(_ resting: PKDrawing, _ selection: [Int]) -> PKDrawing {
        let chosen = Set(selection)
        return PKDrawing(
            strokes: resting.strokes.enumerated().filter { !chosen.contains($0.offset) }.map(\.element))
    }

    /// Takes the selection off the canvas and draws it here instead, at the
    /// same place, so that it can follow the touch.
    private func lift(carried: PKDrawing, resting: PKDrawing) {
        isLifted = true
        // Out of the way while the selection moves; back when it lands.
        menu.isHidden = true
        handlesLayer.path = nil
        let rect = carried.bounds
        guard !carried.strokes.isEmpty, !rect.isNull, !rect.isEmpty else { return }
        carriedClip.frame = bounds
        carriedInk.transform = .identity
        carriedInk.image = Self.image(
            of: carried, in: rect, zoom: zoom, displayScale: displayScale, dark: isDarkPaper)
        restingInkFrame = rect.applying(CGAffineTransform(scaleX: zoom, y: zoom))
        carriedInk.frame = restingInkFrame
        carriedInk.isHidden = false
        writeQuietly(remainder(resting, selection))
    }

    private func carry(_ drag: SelectionDrag) {
        guard isLifted else { return }
        let travel = drag.viewTranslation
        carriedInk.transform = CGAffineTransform(translationX: travel.width, y: travel.height)
        selectionBounds = restingBounds.offsetBy(dx: travel.width, dy: travel.height)
        drawSelectionBounds()
        if !selectedObjects.isEmpty {
            let move = drag.pageTranslation
            previewObjects?(selectedObjects, CGAffineTransform(translationX: move.width, y: move.height))
        }
    }

    private func showScale(_ scale: SelectionScale) {
        guard isLifted else { return }
        let transform = scale.viewTransform
        carriedInk.transform = .identity
        carriedInk.frame = restingInkFrame.applying(transform)
        selectionBounds = restingBounds.applying(transform)
        drawSelectionBounds()
        if !selectedObjects.isEmpty { previewObjects?(selectedObjects, scale.pageTransform) }
    }

    /// Puts the selection down where the touch left it: one write to the
    /// canvas, reported like any other edit so autosave and page growth see
    /// it, and one undo step back to where it was picked up.
    private func drop(_ drag: SelectionDrag) {
        self.drag = nil
        let wasLifted = isLifted
        isLifted = false
        if !selectedObjects.isEmpty { previewObjects?(selectedObjects, nil) }
        guard wasLifted, let canvas else {
            // Touched and let go without travelling: nothing moved.
            hideCarriedInk()
            settle(at: restingBounds)
            return
        }
        let move = drag.pageTranslation
        guard move != .zero else {
            writeQuietly(drag.resting)
            settle(at: restingBounds)
            putDownCarriedInk(immediately: false)
            return
        }
        let base = growthOffset?() ?? .zero
        commitMovedObjects(CGAffineTransform(translationX: move.width, y: move.height), "Move Selection")
        if !drag.selection.isEmpty {
            // Registered before the write, so the toolbar's Undo, refreshed by
            // the write, already sees it.
            registerRestore(of: drag.resting, capturedAt: base, on: canvas, actionName: "Move Selection")
            canvas.drawing = drag.dropped
        }
        // A drop near the top or left of an infinite page grows the page and
        // moves everything on it, so measure the selection again.
        recomputeBounds()
        settle(at: selectionBounds)
        putDownCarriedInk(immediately: (growthOffset?() ?? .zero) != base)
    }

    /// The scale lands as one undo step, like a move.
    private func dropScale(_ scale: SelectionScale) {
        self.scale = nil
        let wasLifted = isLifted
        isLifted = false
        if !selectedObjects.isEmpty { previewObjects?(selectedObjects, nil) }
        guard wasLifted, let canvas, abs(scale.factor - 1) > 0.001 else {
            if wasLifted { writeQuietly(scale.resting) }
            hideCarriedInk()
            settle(at: restingBounds)
            return
        }
        let base = growthOffset?() ?? .zero
        commitMovedObjects(scale.pageTransform, "Resize Selection")
        if !scale.selection.isEmpty {
            registerRestore(of: scale.resting, capturedAt: base, on: canvas, actionName: "Resize Selection")
            canvas.drawing = scale.scaled
        }
        recomputeBounds()
        settle(at: selectionBounds)
        putDownCarriedInk(immediately: true)
    }

    /// The selected objects moved or scaled by a page transform, written as
    /// part of the same undo step as the ink.
    private func commitMovedObjects(_ transform: CGAffineTransform, _ actionName: String) {
        guard !selectedObjects.isEmpty, let all = objectsProvider?() else { return }
        let chosen = Set(selectedObjects)
        let moved = all.map { chosen.contains($0.id) ? SelectionScale.transformed($0, by: transform) : $0 }
        commitObjects?(moved, actionName)
    }

    /// Ends a drag without moving anything: the canvas gets the selection
    /// back where it started, and the outline goes with it.
    func cancelDrag() {
        let resting = drag?.resting ?? scale?.resting
        guard let resting else { return }
        drag = nil
        scale = nil
        if !selectedObjects.isEmpty { previewObjects?(selectedObjects, nil) }
        if isLifted {
            isLifted = false
            writeQuietly(resting)
            carriedInk.transform = .identity
            putDownCarriedInk(immediately: false)
        }
        settle(at: restingBounds)
    }

    private func settle(at bounds: CGRect) {
        selectionBounds = bounds
        drawSelectionBounds()
        if let kind = menuKind {
            menu.configure(kind: kind)
            menu.isHidden = false
            layoutMenu()
        } else {
            menu.isHidden = true
        }
    }

    /// The canvas has the ink again. The copy stays a moment, exactly over
    /// it, because PencilKit can take a frame to draw a replaced drawing and
    /// the selection would otherwise blink.
    private func putDownCarriedInk(immediately: Bool) {
        guard !immediately else {
            hideCarriedInk()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { [weak self] in
            MainActor.assumeIsolated {
                guard let self, !self.isLifted else { return }
                self.hideCarriedInk()
            }
        }
    }

    private func hideCarriedInk() {
        carriedInk.isHidden = true
        carriedInk.image = nil
        carriedInk.transform = .identity
    }

    /// Whether the carried copy is on screen, and where. For the tests.
    var carriedInkFrame: CGRect? { carriedInk.isHidden ? nil : carriedInk.frame }

    private func writeQuietly(_ drawing: PKDrawing) {
        if let setDrawingQuietly {
            setDrawingQuietly(drawing)
        } else {
            canvas?.drawing = drawing
        }
    }

    private var displayScale: CGFloat {
        let scale = traitCollection.displayScale
        return scale > 0 ? scale : 2
    }

    /// The carried copy is a picture of the selection at the scale it is
    /// shown, capped so that a whole page picked up at high zoom does not
    /// cost hundreds of megabytes for the length of a drag. Drawn under the
    /// page's appearance, as the canvas draws it (§3.4).
    private static func image(
        of drawing: PKDrawing, in rect: CGRect, zoom: CGFloat, displayScale: CGFloat, dark: Bool
    ) -> UIImage? {
        let longest = max(rect.width, rect.height, 1)
        let scale = min(max(zoom, 0.01) * displayScale, 4096 / longest)
        var image: UIImage?
        UITraitCollection(userInterfaceStyle: dark ? .dark : .light).performAsCurrent {
            image = drawing.image(from: rect, scale: scale)
        }
        return image
    }

    private static func path(through points: [CGPoint], closed: Bool) -> CGPath? {
        guard let first = points.first else { return nil }
        let path = UIBezierPath()
        path.move(to: first)
        for point in points.dropFirst() { path.addLine(to: point) }
        if closed { path.close() }
        return path.cgPath
    }

    // MARK: - Selection

    private func selectStrokes(inside points: [CGPoint]) {
        guard let canvas else { return }
        // The loop is captured in view points; strokes live in page points.
        let scale = zoom == 0 ? 1 : zoom
        let pagePoints = points.map { CGPoint(x: $0.x / scale, y: $0.y / scale) }
        guard let loop = Self.path(through: pagePoints, closed: true) else { return }

        var hits: [Int] = []
        for (index, stroke) in canvas.drawing.strokes.enumerated() {
            if Self.stroke(stroke, isInside: loop) { hits.append(index) }
        }
        // An object is caught when the middle of it is inside the loop.
        let objects = (objectsProvider?() ?? []).filter { object in
            guard ObjectMenuKind(object.type) != nil else { return false }
            let frame = object.frame.rect.standardized
            return loop.contains(CGPoint(x: frame.midX, y: frame.midY))
        }
        if hits.isEmpty, objects.count == 1, let only = objects.first {
            // One object alone is that object's selection, handles and all.
            onSelectObject?(only.id)
            return
        }
        setSelection(strokes: hits, objects: objects.map(\.id))
        if !hits.isEmpty || !objects.isEmpty {
            let count = hits.count + objects.count
            UIAccessibility.post(
                notification: .announcement,
                argument: objects.isEmpty ? "\(hits.count) strokes selected" : "\(count) items selected")
        }
    }

    /// Selects strokes by index, as a finger tap on a word does, with the
    /// menu at once.
    func select(strokes: [Int]) {
        cancelDrag()
        guard let canvas else { return }
        let valid = strokes.filter(canvas.drawing.strokes.indices.contains)
        setSelection(strokes: valid, objects: [])
        if !valid.isEmpty {
            UIAccessibility.post(notification: .announcement, argument: "Handwriting selected")
        }
    }

    /// Selects ink and objects together, as a paste does.
    func select(strokes: [Int], objects: [String]) {
        cancelDrag()
        setSelection(strokes: strokes, objects: objects)
    }

    private func setSelection(strokes: [Int], objects: [String]) {
        selection = strokes
        selectedObjects = objects
        recomputeBounds()
        settle(at: selectionBounds)
        onSelectionChange?(!hasSelection)
    }

    private static func stroke(_ stroke: PKStroke, isInside loop: CGPath) -> Bool {
        // Cheap test first: a stroke whose whole bounding box is in the loop is
        // in the loop.
        let bounds = stroke.renderBounds
        let corners = [
            CGPoint(x: bounds.minX, y: bounds.minY), CGPoint(x: bounds.maxX, y: bounds.minY),
            CGPoint(x: bounds.minX, y: bounds.maxY), CGPoint(x: bounds.maxX, y: bounds.maxY),
        ]
        if corners.allSatisfy({ loop.contains($0) }) { return true }
        if !loop.boundingBox.intersects(bounds) { return false }

        var total = 0
        var inside = 0
        for point in stroke.path.interpolatedPoints(by: .distance(6)) {
            total += 1
            if loop.contains(point.location.applying(stroke.transform)) { inside += 1 }
        }
        guard total > 0 else { return false }
        return Double(inside) / Double(total) >= insideRatio
    }

    /// The selected objects as they are now.
    private var currentSelectedObjects: [PageObject] {
        guard !selectedObjects.isEmpty else { return [] }
        let chosen = Set(selectedObjects)
        return (objectsProvider?() ?? []).filter { chosen.contains($0.id) }
    }

    /// The selection's outline, in page points.
    private var pageBounds: CGRect {
        var union: CGRect = .null
        if let canvas {
            let strokes = canvas.drawing.strokes
            for index in selection where strokes.indices.contains(index) {
                union = union.union(strokes[index].renderBounds)
            }
        }
        for object in currentSelectedObjects { union = union.union(object.frame.rect.standardized) }
        return union
    }

    private func recomputeBounds() {
        guard hasSelection else {
            selectionBounds = .zero
            return
        }
        let union = pageBounds
        selectionBounds = union.isNull ? .zero : union.applying(
            CGAffineTransform(scaleX: zoom, y: zoom))
    }

    private func drawSelectionBounds() {
        guard hasSelection, !selectionBounds.isNull, selectionBounds != .zero else {
            selectionLayer.path = nil
            handlesLayer.path = nil
            return
        }
        selectionLayer.path =
            UIBezierPath(
                roundedRect: selectionBounds.insetBy(dx: -6, dy: -6), cornerRadius: 4
            ).cgPath
        guard drag == nil, scale == nil else {
            handlesLayer.path = nil
            return
        }
        let handles = UIBezierPath()
        for corner in handleCorners {
            handles.append(
                UIBezierPath(
                    roundedRect: CGRect(x: corner.x - 5, y: corner.y - 5, width: 10, height: 10),
                    cornerRadius: 2))
        }
        handlesLayer.path = handles.cgPath
    }

    private func layoutMenu() {
        guard hasSelection, !menu.isHidden else { return }
        menu.place(
            around: selectionBounds.insetBy(dx: -6, dy: -6), visible: ObjectMenu.visibleBounds(of: self))
    }

    /// Undo, a reload, another tool, the page leaving the screen: whatever
    /// clears the selection puts a drag in progress back first.
    func clearSelection() {
        cancelDrag()
        guard hasSelection || !menu.isHidden else { return }
        selection = []
        selectedObjects = []
        selectionBounds = .zero
        selectionLayer.path = nil
        handlesLayer.path = nil
        menu.isHidden = true
        onSelectionChange?(true)
    }

    // MARK: - Operations

    private func perform(_ action: ObjectMenuAction) {
        switch action {
        case .cut: cutSelection()
        case .copy: copySelection()
        case .duplicate: duplicateSelection()
        case .delete: deleteSelection()
        case .color(let hex): recolorSelection(to: ToolState.uiColor(hex))
        case .convertToText: convertSelectionToText()
        case .copyLink: copySelectionLink()
        case .ai(let action): performAIAction(action)
        case .move(let offset): translateSelection(by: offset)
        default: break
        }
    }

    /// Every op goes through here: rebuild the drawing, register the inverse on
    /// the canvas's own undo manager, hand it back to PencilKit.
    ///
    /// Handing it back reports the edit, and an edit near the top or left edge
    /// of an infinite page grows the page before this returns, moving all of
    /// its ink. `previous` is from before that move, so the inverse is
    /// registered with the growth it was captured at.
    ///
    /// The inverse is registered before the write, so the toolbar's Undo,
    /// which the write refreshes, already sees it.
    private func replaceStrokes(_ strokes: [PKStroke], actionName: String) {
        guard let canvas else { return }
        let previous = canvas.drawing
        let base = growthOffset?() ?? .zero
        registerRestore(of: previous, capturedAt: base, on: canvas, actionName: actionName)
        canvas.drawing = PKDrawing(strokes: strokes)
    }

    /// The canvas, with any drag in progress put back first: every operation
    /// reads the page's strokes, and mid-drag the canvas holds the page
    /// without the selection.
    private func settledCanvas() -> PKCanvasView? {
        cancelDrag()
        return canvas
    }

    /// Registers putting `drawing` back, and makes putting it back register
    /// the way forward again, so Redo works as well as Undo.
    private func registerRestore(
        of drawing: PKDrawing, capturedAt base: CGSize, on canvas: PKCanvasView,
        actionName: String
    ) {
        let manager = canvas.undoManager
        manager?.registerUndo(withTarget: canvas) { [weak self] target in
            MainActor.assumeIsolated {
                let now = self?.growthOffset?() ?? base
                let replaced = target.drawing
                let delta = CGSize(width: now.width - base.width, height: now.height - base.height)
                // The strokes a selection points at are about to change.
                self?.clearSelection()
                target.drawing =
                    delta == .zero
                    ? drawing
                    : drawing.transformed(
                        using: CGAffineTransform(translationX: delta.width, y: delta.height))
                self?.registerRestore(
                    of: replaced, capturedAt: now, on: target, actionName: actionName)
            }
        }
        manager?.setActionName(actionName)
    }

    /// Moves the selection by page points: the menu's Move, and the keys.
    func translateSelection(by offset: CGSize) {
        guard let canvas = settledCanvas(), hasSelection else { return }
        let transform = CGAffineTransform(translationX: offset.width, y: offset.height)
        commitMovedObjects(transform, "Move Selection")
        if !selection.isEmpty {
            var strokes = canvas.drawing.strokes
            for index in selection where strokes.indices.contains(index) {
                strokes[index].transform = strokes[index].transform.concatenating(transform)
            }
            replaceStrokes(strokes, actionName: "Move Selection")
        }
        recomputeBounds()
        settle(at: selectionBounds)
    }

    func duplicateSelection() {
        guard let canvas = settledCanvas(), hasSelection else { return }
        let offset = CGAffineTransform(translationX: 16, y: 16)
        var newObjects: [String] = []
        if !selectedObjects.isEmpty, let all = objectsProvider?() {
            let copies = currentSelectedObjects.map { object -> PageObject in
                var copy = SelectionScale.transformed(object, by: offset)
                copy.id = PaperID.make()
                return copy
            }
            newObjects = copies.map(\.id)
            commitObjects?(all + copies, "Duplicate Selection")
        }
        var newStrokes: [Int] = []
        if !selection.isEmpty {
            var strokes = canvas.drawing.strokes
            var copies: [PKStroke] = []
            for index in selection where strokes.indices.contains(index) {
                var copy = strokes[index]
                copy.transform = copy.transform.concatenating(offset)
                copies.append(copy)
            }
            let firstNew = strokes.count
            strokes.append(contentsOf: copies)
            replaceStrokes(strokes, actionName: "Duplicate Selection")
            newStrokes = Array(firstNew..<strokes.count)
        }
        // The copy becomes the selection, so a duplicate can be dragged away
        // immediately.
        selection = newStrokes
        selectedObjects = newObjects
        recomputeBounds()
        settle(at: selectionBounds)
    }

    func recolorSelection(to color: NamedInkColor) {
        recolorSelection(to: color.uiColor)
    }

    /// Colours are given as their light-appearance values, as the pen
    /// stores them; on dark paper PencilKit shows them mapped.
    func recolorSelection(to color: UIColor) {
        guard let canvas = settledCanvas(), !selection.isEmpty else { return }
        var strokes = canvas.drawing.strokes
        for index in selection where strokes.indices.contains(index) {
            strokes[index].ink = PKInk(strokes[index].ink.inkType, color: color)
        }
        replaceStrokes(strokes, actionName: "Recolour Selection")
    }

    /// The selection on the pasteboard.
    func copySelection() {
        guard let canvas = settledCanvas(), hasSelection else { return }
        let ink = carriedDrawing(canvas.drawing, selection)
        let bounds = pageBounds
        var image: UIImage?
        if !bounds.isNull, bounds.width > 0, bounds.height > 0 {
            image = renderSelection?(ink, currentSelectedObjects, bounds.insetBy(dx: -4, dy: -4))
                ?? Self.image(of: ink, in: bounds, zoom: 1, displayScale: 2, dark: false)
        }
        onCopy?(ink, currentSelectedObjects, image)
        UIAccessibility.post(notification: .announcement, argument: "Copied")
    }

    func cutSelection() {
        copySelection()
        removeSelection(actionName: "Cut")
    }

    /// Reads the selected strokes and turns them into a text object.
    ///
    /// The recognition runs off the main actor -- it is Vision, not a lookup --
    /// and the two edits it produces (the object appearing, the ink going
    /// away) both land in the same run-loop turn afterwards, so `UndoManager`
    /// groups them and one Cmd-Z puts the handwriting back.
    func convertSelectionToText() {
        guard let canvas = settledCanvas(), !selection.isEmpty, !isConverting else { return }
        let chosen = Set(selection)
        let strokes = canvas.drawing.strokes.enumerated()
            .filter { chosen.contains($0.offset) }
            .map(\.element)
        guard !strokes.isEmpty else { return }
        let drawing = PKDrawing(strokes: strokes)
        let bounds = drawing.bounds
        guard bounds.width > 1, bounds.height > 1 else { return }

        // Three times, because a lassoed word is a small part of the page and
        // the recogniser wants pixels rather than points. Always light: Vision
        // reads dark ink on white.
        var image: UIImage?
        UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
            image = drawing.image(from: bounds, scale: 3)
        }
        guard let cgImage = image?.cgImage else { return }
        isConverting = true

        Task { [weak self] in
            let text = await Task.detached(priority: .userInitiated) { () -> String? in
                let recognised = try? HandwritingRecognizer.recognize(
                    image: cgImage, pageSize: bounds.size,
                    languages: PaperController.recognitionLanguages())
                guard let recognised, !recognised.isEmpty else { return nil }
                return recognised.transcript
            }.value
            guard let self else { return }
            self.isConverting = false
            guard let text else {
                UIAccessibility.post(
                    notification: .announcement, argument: "No text found in the selection")
                return
            }
            self.replaceSelectionWithText(text, bounds: bounds)
        }
    }

    /// Renders the chosen strokes -- and objects, for a mixed selection --
    /// against white paper. Page coordinates travel separately, so links can
    /// return to the exact region even when the image is later resized by an
    /// AI provider.
    func performAIAction(_ action: SelectionAIAction) {
        guard let canvas = settledCanvas(), hasSelection else { return }
        let drawing = carriedDrawing(canvas.drawing, selection)
        let rawBounds = pageBounds
        guard !rawBounds.isNull, rawBounds.width > 1 || rawBounds.height > 1 else { return }
        let bounds = rawBounds.insetBy(dx: -12, dy: -12)
        var picture = renderSelection?(drawing, currentSelectedObjects, bounds)
        if picture == nil {
            UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
                picture = drawing.image(from: bounds, scale: 3)
            }
        }
        guard let ink = picture else { return }
        let format = UIGraphicsImageRendererFormat()
        format.scale = ink.scale
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(size: ink.size, format: format)
        let flattened = renderer.image { context in
            UIColor.white.setFill()
            context.fill(CGRect(origin: .zero, size: ink.size))
            ink.draw(in: CGRect(origin: .zero, size: ink.size))
        }
        guard let data = flattened.pngData() else { return }
        onAIAction?(action, data, bounds)
    }

    func copySelectionLink() {
        cancelDrag()
        guard hasSelection, !selectionBounds.isEmpty else { return }
        let scale = max(zoom, 0.01)
        onCopyLink?(
            CGRect(
                x: selectionBounds.minX / scale,
                y: selectionBounds.minY / scale,
                width: selectionBounds.width / scale,
                height: selectionBounds.height / scale))
    }

    private func replaceSelectionWithText(_ text: String, bounds: CGRect) {
        guard let canvas = settledCanvas(), !selection.isEmpty else { return }
        // The object first, so that undoing puts the ink back over an empty
        // page rather than under a text box that is still there.
        onConvertToText?(text, bounds)
        let doomed = Set(selection)
        let remaining = canvas.drawing.strokes.enumerated()
            .filter { !doomed.contains($0.offset) }
            .map(\.element)
        replaceStrokes(remaining, actionName: "Convert to Text")
        clearSelection()
    }

    func deleteSelection() {
        removeSelection(actionName: "Delete Selection")
    }

    private func removeSelection(actionName: String) {
        guard let canvas = settledCanvas(), hasSelection else { return }
        if !selectedObjects.isEmpty, let all = objectsProvider?() {
            let doomed = Set(selectedObjects)
            commitObjects?(all.filter { !doomed.contains($0.id) }, actionName)
        }
        if !selection.isEmpty {
            let doomed = Set(selection)
            let strokes = canvas.drawing.strokes.enumerated()
                .filter { !doomed.contains($0.offset) }
                .map(\.element)
            replaceStrokes(strokes, actionName: actionName)
        }
        clearSelection()
    }
}

/// One step of a touch in Lasso mode, in the page stack's coordinates: what
/// `LassoGestureRecognizer`'s action hands `NotebookViewController
/// .routeLassoTouch`, and what the DEBUG launch arguments drive it with.
enum LassoTouch: Equatable {
    case began(CGPoint)
    case moved(CGPoint)
    case ended
    case cancelled
}

/// The recogniser that owns a lasso touch: a loop, or a drag of the selection.
///
/// A gesture recogniser rather than raw touches because that is the only thing
/// a `UIScrollView`'s pan can be told to wait for: `panGestureRecognizer
/// .require(toFail:)` this, and a drag in Lasso mode draws a loop or carries
/// the selection instead of scrolling the notebook.
///
/// Pencil is creation: a Pencil touch begins at once, so nothing else gets the
/// chance, and a palm resting on the glass beside it changes nothing. Fingers
/// are navigation: `shouldBegin` turns a finger away unless it lands on the
/// selection (or "Draw with finger" is on), so the pan has it straight away. A
/// finger that is taken begins only once it has travelled `fingerSlop`: a
/// second finger arriving first -- two-finger scrolling, a pinch -- fails it
/// while the scroll view is still waiting, and a finger that lifts without
/// travelling was a tap. The notebook keeps one for the Pencil and one for
/// fingers, so a palm resting on the glass can only ever fail the second.
final class LassoGestureRecognizer: UIGestureRecognizer {
    /// Asked once per gesture, with the point in this recogniser's own view
    /// and the kind of touch: false when there is no live page there, when
    /// the object menu's buttons are what the touch is aiming at, or
    /// when a finger lands away from the selection.
    var shouldBegin: ((CGPoint, UITouch.TouchType) -> Bool)?

    /// How far a finger travels before it is a drag, in points.
    static let fingerSlop: CGFloat = 6

    private var tracked: UITouch?
    private var origin: CGPoint = .zero

    override init(target: Any?, action: Selector?) {
        super.init(target: target, action: action)
        // Nothing below the overlay may be reached while the Lasso is active,
        // so there is no touch worth cancelling -- and leaving them alone is
        // what keeps the object menu's own buttons tappable.
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
    }

    /// Where the tracked touch landed. A finger begins only after it has
    /// travelled, and its drag starts from here so none of that is lost.
    func startLocation(in other: UIView?) -> CGPoint {
        guard let view else { return origin }
        return view.convert(origin, to: other)
    }

    /// Where the tracked touch is now.
    func trackedLocation(in other: UIView?) -> CGPoint {
        tracked?.location(in: other) ?? location(in: other)
    }

    override func reset() {
        super.reset()
        tracked = nil
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesBegan(touches, with: event)
        if let tracked {
            for touch in touches where touch !== tracked {
                // A second finger before the first has committed is somebody
                // reaching for the page, not dragging.
                if tracked.type == .direct, touch.type == .direct, state == .possible {
                    state = .failed
                    return
                }
                ignore(touch, for: event)
            }
            return
        }
        guard let view,
            let touch = touches.first(where: { $0.type == .pencil }) ?? touches.first
        else {
            state = .failed
            return
        }
        if touch.type == .direct {
            let fingers = (event.allTouches ?? touches).filter {
                $0.type == .direct && $0.phase != .ended && $0.phase != .cancelled
            }
            guard fingers.count == 1 else {
                state = .failed
                return
            }
        }
        let point = touch.location(in: view)
        guard shouldBegin?(point, touch.type) ?? true else {
            state = .failed
            return
        }
        tracked = touch
        origin = point
        for other in touches where other !== touch { ignore(other, for: event) }
        if touch.type != .direct { state = .began }
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesMoved(touches, with: event)
        guard let tracked, touches.contains(tracked), let view else { return }
        switch state {
        case .possible:
            let point = tracked.location(in: view)
            if max(abs(point.x - origin.x), abs(point.y - origin.y)) >= Self.fingerSlop {
                state = .began
            }
        case .began, .changed:
            state = .changed
        default:
            break
        }
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesEnded(touches, with: event)
        guard let tracked, touches.contains(tracked) else { return }
        state = (state == .began || state == .changed) ? .ended : .failed
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesCancelled(touches, with: event)
        guard let tracked, touches.contains(tracked) else { return }
        state = (state == .began || state == .changed) ? .cancelled : .failed
    }
}
