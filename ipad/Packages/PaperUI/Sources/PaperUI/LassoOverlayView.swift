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

/// Selection, built by hand.
///
/// PencilKit ships a lasso in `PKToolPicker` but exposes no selection API at
/// all, so a toolbar of our own has to draw the loop, decide what it caught,
/// and rebuild the drawing for every operation. Every one of those rebuilds is
/// registered on the canvas's own `undoManager`, so Cmd-Z behaves the same
/// whether the last thing you did was write a word or recolour a paragraph.
final class LassoOverlayView: UIView {
    /// A stroke counts as selected when at least this much of it is inside the
    /// loop. Half is forgiving enough for a quick circle round a word without
    /// dragging in the line below.
    static let insideRatio = 0.5

    private weak var canvas: PKCanvasView?
    private let loopLayer = CAShapeLayer()
    private let selectionLayer = CAShapeLayer()
    private let inspector: SelectionInspector

    private var loopPoints: [CGPoint] = []
    private(set) var selection: [Int] = []
    private var selectionBounds: CGRect = .zero
    private var dragOrigin: CGPoint?
    private var dragTotal: CGSize = .zero

    var zoom: CGFloat = 1
    /// How far growth has moved the page's content so far. Set by `PageView`.
    var growthOffset: (() -> CGSize)?
    var onSelectionChange: ((Bool) -> Void)?
    /// M5: the selection was read as text and should become a text object at
    /// the rectangle it occupied. Set by `PageView`.
    var onConvertToText: ((String, CGRect) -> Void)?
    /// A PNG crop and its page-space bounds, ready for a native Paper action.
    /// The callback never removes or rewrites the selected strokes.
    var onAIAction: ((SelectionAIAction, Data, CGRect) -> Void)?
    var onCopyLink: ((CGRect) -> Void)?
    /// True while Vision is reading the selection, so a second tap does not
    /// start a second read of ink that is about to disappear.
    private var isConverting = false

    init(canvas: PKCanvasView) {
        self.canvas = canvas
        self.inspector = SelectionInspector()
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

        inspector.isHidden = true
        inspector.delegate = self
        addSubview(inspector)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("LassoOverlayView is created in code") }

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
    /// The selection inspector, while it is on screen, so the page above can
    /// publish it to VoiceOver. A page is an accessibility element in its own
    /// right, which would otherwise hide every control inside it.
    var accessibleInspector: UIView? { inspector.isHidden ? nil : inspector }

    /// Whether a touch should reach the loop at all -- rather than the buttons
    /// of the selection inspector floating above it.
    func shouldCaptureTouch(at point: CGPoint) -> Bool {
        guard !inspector.isHidden else { return true }
        return !inspector.frame.insetBy(dx: -4, dy: -4).contains(point)
    }

    func handleTouchBegan(at point: CGPoint) {
        if !selection.isEmpty && selectionBounds.insetBy(dx: -12, dy: -12).contains(point) {
            dragOrigin = point
            dragTotal = .zero
            return
        }
        clearSelection()
        loopPoints = [point]
    }

    func handleTouchMoved(to point: CGPoint) {
        if dragOrigin != nil {
            guard let origin = dragOrigin else { return }
            let delta = CGSize(width: point.x - origin.x, height: point.y - origin.y)
            dragOrigin = point
            dragTotal = CGSize(
                width: dragTotal.width + delta.width, height: dragTotal.height + delta.height)
            selectionBounds = selectionBounds.offsetBy(dx: delta.width, dy: delta.height)
            drawSelectionBounds()
            layoutInspector()
            return
        }
        loopPoints.append(point)
        loopLayer.path = Self.path(through: loopPoints, closed: false)
    }

    func handleTouchEnded() {
        if dragOrigin != nil {
            dragOrigin = nil
            let delta = dragTotal
            dragTotal = .zero
            if delta != .zero {
                translateSelection(
                    by: CGSize(width: delta.width / zoom, height: delta.height / zoom))
            }
            return
        }
        loopLayer.path = nil
        defer { loopPoints = [] }
        guard loopPoints.count > 2 else { return }
        selectStrokes(inside: loopPoints)
    }

    func handleTouchCancelled() {
        dragOrigin = nil
        dragTotal = .zero
        loopPoints = []
        loopLayer.path = nil
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
        selection = hits
        recomputeBounds()
        drawSelectionBounds()
        layoutInspector()
        inspector.isHidden = hits.isEmpty
        onSelectionChange?(hits.isEmpty)
        if !hits.isEmpty {
            UIAccessibility.post(
                notification: .announcement, argument: "\(hits.count) strokes selected")
        }
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

    private func recomputeBounds() {
        guard let canvas, !selection.isEmpty else {
            selectionBounds = .zero
            return
        }
        let strokes = canvas.drawing.strokes
        var union: CGRect = .null
        for index in selection where strokes.indices.contains(index) {
            union = union.union(strokes[index].renderBounds)
        }
        selectionBounds = union.isNull ? .zero : union.applying(
            CGAffineTransform(scaleX: zoom, y: zoom))
    }

    private func drawSelectionBounds() {
        guard !selection.isEmpty, !selectionBounds.isEmpty else {
            selectionLayer.path = nil
            return
        }
        selectionLayer.path =
            UIBezierPath(
                roundedRect: selectionBounds.insetBy(dx: -6, dy: -6), cornerRadius: 4
            ).cgPath
    }

    private func layoutInspector() {
        guard !selection.isEmpty else { return }
        let size = inspector.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize)
        inspector.bounds.size = size
        let y = max(size.height / 2 + 8, selectionBounds.minY - 16 - size.height / 2)
        inspector.center = CGPoint(
            x: min(max(selectionBounds.midX, size.width / 2 + 8), bounds.width - size.width / 2 - 8),
            y: y)
    }

    func clearSelection() {
        guard !selection.isEmpty || !inspector.isHidden else { return }
        selection = []
        selectionBounds = .zero
        selectionLayer.path = nil
        inspector.isHidden = true
        onSelectionChange?(true)
    }

    // MARK: - Operations

    /// Every op goes through here: rebuild the drawing, register the inverse on
    /// the canvas's own undo manager, hand it back to PencilKit.
    ///
    /// Handing it back reports the edit, and an edit near the top or left edge
    /// of an infinite page grows the page before this returns, moving all of
    /// its ink. `previous` is from before that move, so the inverse is
    /// registered with the growth it was captured at.
    private func replaceStrokes(_ strokes: [PKStroke], actionName: String) {
        guard let canvas else { return }
        let previous = canvas.drawing
        let base = growthOffset?() ?? .zero
        canvas.drawing = PKDrawing(strokes: strokes)
        registerRestore(of: previous, capturedAt: base, on: canvas, actionName: actionName)
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

    func translateSelection(by offset: CGSize) {
        guard let canvas, !selection.isEmpty else { return }
        var strokes = canvas.drawing.strokes
        let transform = CGAffineTransform(translationX: offset.width, y: offset.height)
        for index in selection where strokes.indices.contains(index) {
            strokes[index].transform = strokes[index].transform.concatenating(transform)
        }
        replaceStrokes(strokes, actionName: "Move Selection")
        recomputeBounds()
        drawSelectionBounds()
        layoutInspector()
    }

    func duplicateSelection() {
        guard let canvas, !selection.isEmpty else { return }
        var strokes = canvas.drawing.strokes
        let offset = CGAffineTransform(translationX: 16, y: 16)
        var copies: [PKStroke] = []
        for index in selection where strokes.indices.contains(index) {
            var copy = strokes[index]
            copy.transform = copy.transform.concatenating(offset)
            copies.append(copy)
        }
        let firstNew = strokes.count
        strokes.append(contentsOf: copies)
        replaceStrokes(strokes, actionName: "Duplicate Selection")
        // The copy becomes the selection, so a duplicate can be dragged away
        // immediately.
        selection = Array(firstNew..<strokes.count)
        recomputeBounds()
        drawSelectionBounds()
        layoutInspector()
    }

    func recolorSelection(to color: NamedInkColor) {
        guard let canvas, !selection.isEmpty else { return }
        var strokes = canvas.drawing.strokes
        for index in selection where strokes.indices.contains(index) {
            strokes[index].ink = PKInk(strokes[index].ink.inkType, color: color.uiColor)
        }
        replaceStrokes(strokes, actionName: "Recolour Selection")
    }

    /// Reads the selected strokes and turns them into a text object.
    ///
    /// The recognition runs off the main actor -- it is Vision, not a lookup --
    /// and the two edits it produces (the object appearing, the ink going
    /// away) both land in the same run-loop turn afterwards, so `UndoManager`
    /// groups them and one Cmd-Z puts the handwriting back.
    func convertSelectionToText() {
        guard let canvas, !selection.isEmpty, !isConverting else { return }
        let chosen = Set(selection)
        let strokes = canvas.drawing.strokes.enumerated()
            .filter { chosen.contains($0.offset) }
            .map(\.element)
        guard !strokes.isEmpty else { return }
        let drawing = PKDrawing(strokes: strokes)
        let bounds = drawing.bounds
        guard bounds.width > 1, bounds.height > 1 else { return }

        // Three times, because a lassoed word is a small part of the page and
        // the recogniser wants pixels rather than points.
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

    /// Renders only the chosen strokes against white paper. Page coordinates
    /// travel separately, so links can return to the exact region even when
    /// the image is later resized by an AI provider.
    func performAIAction(_ action: SelectionAIAction) {
        guard let canvas, !selection.isEmpty else { return }
        let chosen = Set(selection)
        let strokes = canvas.drawing.strokes.enumerated()
            .filter { chosen.contains($0.offset) }
            .map(\.element)
        guard !strokes.isEmpty else { return }
        let drawing = PKDrawing(strokes: strokes)
        let rawBounds = drawing.bounds
        guard rawBounds.width > 1, rawBounds.height > 1 else { return }
        let bounds = rawBounds.insetBy(dx: -12, dy: -12)
        var ink: UIImage?
        UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
            ink = drawing.image(from: bounds, scale: 3)
        }
        guard let ink else { return }
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
        guard !selection.isEmpty, !selectionBounds.isEmpty else { return }
        let scale = max(zoom, 0.01)
        onCopyLink?(
            CGRect(
                x: selectionBounds.minX / scale,
                y: selectionBounds.minY / scale,
                width: selectionBounds.width / scale,
                height: selectionBounds.height / scale))
    }

    private func replaceSelectionWithText(_ text: String, bounds: CGRect) {
        guard let canvas, !selection.isEmpty else { return }
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
        guard let canvas, !selection.isEmpty else { return }
        let doomed = Set(selection)
        let strokes = canvas.drawing.strokes.enumerated()
            .filter { !doomed.contains($0.offset) }
            .map(\.element)
        replaceStrokes(strokes, actionName: "Delete Selection")
        clearSelection()
    }
}

extension LassoOverlayView: SelectionInspectorDelegate {
    func selectionInspectorDidTapDuplicate(_ inspector: SelectionInspector) {
        duplicateSelection()
    }

    func selectionInspectorDidTapDelete(_ inspector: SelectionInspector) {
        deleteSelection()
    }

    func selectionInspectorDidTapConvert(_ inspector: SelectionInspector) {
        convertSelectionToText()
    }

    func selectionInspector(_ inspector: SelectionInspector, didChooseAIAction action: SelectionAIAction) {
        performAIAction(action)
    }

    func selectionInspectorDidRequestLink(_ inspector: SelectionInspector) {
        copySelectionLink()
    }

    func selectionInspector(_ inspector: SelectionInspector, didPick color: NamedInkColor) {
        recolorSelection(to: color)
    }

    func selectionInspector(_ inspector: SelectionInspector, nudgeBy offset: CGSize) {
        translateSelection(by: offset)
    }
}

@MainActor
protocol SelectionInspectorDelegate: AnyObject {
    func selectionInspectorDidTapDuplicate(_ inspector: SelectionInspector)
    func selectionInspectorDidTapDelete(_ inspector: SelectionInspector)
    func selectionInspectorDidTapConvert(_ inspector: SelectionInspector)
    func selectionInspector(_ inspector: SelectionInspector, didChooseAIAction action: SelectionAIAction)
    func selectionInspectorDidRequestLink(_ inspector: SelectionInspector)
    func selectionInspector(_ inspector: SelectionInspector, didPick color: NamedInkColor)
    func selectionInspector(_ inspector: SelectionInspector, nudgeBy offset: CGSize)
}

/// The contextual capsule that appears above a selection: the progressive
/// disclosure the research asks for, instead of a permanently crowded toolbar.
final class SelectionInspector: UIView {
    weak var delegate: SelectionInspectorDelegate?

    private let stack = UIStackView()

    init() {
        super.init(frame: .zero)
        let background = UIVisualEffectView(effect: UIBlurEffect(style: .systemThickMaterial))
        background.translatesAutoresizingMaskIntoConstraints = false
        background.isUserInteractionEnabled = false
        addSubview(background)
        NSLayoutConstraint.activate([
            background.topAnchor.constraint(equalTo: topAnchor),
            background.bottomAnchor.constraint(equalTo: bottomAnchor),
            background.leadingAnchor.constraint(equalTo: leadingAnchor),
            background.trailingAnchor.constraint(equalTo: trailingAnchor),
        ])
        layer.cornerRadius = 22
        layer.cornerCurve = .continuous
        layer.borderWidth = 1
        layer.borderColor = UIColor.separator.cgColor
        clipsToBounds = true

        stack.axis = .horizontal
        stack.alignment = .center
        stack.spacing = 0
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 4),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -4),
        ])

        // Move: drag works with a finger or the Pencil, and this is the
        // keyboard-and-VoiceOver way to do the same thing.
        let move = button(symbol: "arrow.up.and.down.and.arrow.left.and.right", label: "Move")
        move.accessibilityHint = "Drag the selection, or use this menu to nudge it"
        move.showsMenuAsPrimaryAction = true
        move.menu = UIMenu(children: [
            nudge("Move up", "arrow.up", CGSize(width: 0, height: -16)),
            nudge("Move down", "arrow.down", CGSize(width: 0, height: 16)),
            nudge("Move left", "arrow.left", CGSize(width: -16, height: 0)),
            nudge("Move right", "arrow.right", CGSize(width: 16, height: 0)),
        ])
        stack.addArrangedSubview(move)

        let duplicate = button(symbol: "plus.square.on.square", label: "Duplicate")
        duplicate.accessibilityHint = "Copies the selection 16 points down and right"
        duplicate.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.selectionInspectorDidTapDuplicate(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(duplicate)

        let colour = button(symbol: "paintpalette", label: "Colour")
        colour.accessibilityHint = "Recolours the selected strokes"
        colour.showsMenuAsPrimaryAction = true
        colour.menu = UIMenu(
            children: ToolState.penColors.map { named in
                UIAction(title: named.name) { [weak self] _ in
                    guard let self else { return }
                    self.delegate?.selectionInspector(self, didPick: named)
                }
            })
        stack.addArrangedSubview(colour)

        let convert = button(symbol: "textformat", label: "Convert to text")
        convert.accessibilityHint = "Reads the selected handwriting and replaces it with text"
        convert.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.selectionInspectorDidTapConvert(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(convert)

        let omnia = button(symbol: "sparkles", label: "OMNIA")
        omnia.accessibilityHint = "Uses the selected handwriting without changing the ink"
        omnia.showsMenuAsPrimaryAction = true
        omnia.menu = UIMenu(
            children: SelectionAIAction.allCases.map { action in
                UIAction(title: action.title, image: UIImage(systemName: action.symbol)) {
                    [weak self] _ in
                    guard let self else { return }
                    self.delegate?.selectionInspector(self, didChooseAIAction: action)
                }
            })
        stack.addArrangedSubview(omnia)

        let link = button(symbol: "link", label: "Copy link")
        link.accessibilityHint = "Copies a durable link to this handwriting region"
        link.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.selectionInspectorDidRequestLink(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(link)

        let delete = button(symbol: "trash", label: "Delete")
        delete.accessibilityHint = "Deletes the selected strokes"
        delete.addAction(
            UIAction { [weak self] _ in
                guard let self else { return }
                self.delegate?.selectionInspectorDidTapDelete(self)
            }, for: .touchUpInside)
        stack.addArrangedSubview(delete)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("SelectionInspector is created in code") }

    private func nudge(_ title: String, _ symbol: String, _ offset: CGSize) -> UIAction {
        UIAction(title: title, image: UIImage(systemName: symbol)) { [weak self] _ in
            guard let self else { return }
            self.delegate?.selectionInspector(self, nudgeBy: offset)
        }
    }

    private func button(symbol: String, label: String) -> UIButton {
        let button = UIButton(type: .system)
        button.setImage(
            UIImage(
                systemName: symbol,
                withConfiguration: UIImage.SymbolConfiguration(pointSize: 16, weight: .regular)),
            for: .normal)
        button.tintColor = .label
        button.translatesAutoresizingMaskIntoConstraints = false
        button.accessibilityLabel = label
        NSLayoutConstraint.activate([
            button.widthAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
            button.heightAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
        ])
        return button
    }
}

/// The recogniser that owns a lasso loop.
///
/// A gesture recogniser rather than raw touches because that is the only thing
/// a `UIScrollView`'s pan can be told to wait for: `panGestureRecognizer
/// .require(toFail:)` this, and a drag in Lasso mode draws a loop instead of
/// scrolling the notebook. It begins on the first touch so the pan never gets
/// the chance, and fails the moment a second finger lands, so two-finger
/// scrolling and pinch-to-zoom keep working with the Lasso selected.
final class LassoGestureRecognizer: UIGestureRecognizer {
    /// Asked once per gesture, with the point in this recogniser's own view:
    /// false when there is no live page there, or when the selection
    /// inspector's buttons are what the finger is actually aiming at.
    var shouldBegin: ((CGPoint) -> Bool)?

    private var tracked: UITouch?
    private var origin: CGPoint = .zero
    private var hasTravelled = false

    override init(target: Any?, action: Selector?) {
        super.init(target: target, action: action)
        // Nothing below the overlay may be reached while the Lasso is active,
        // so there is no touch worth cancelling -- and leaving them alone is
        // what keeps the inspector's own buttons tappable.
        cancelsTouchesInView = false
        delaysTouchesBegan = false
        delaysTouchesEnded = false
    }

    override func reset() {
        super.reset()
        tracked = nil
        hasTravelled = false
    }

    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesBegan(touches, with: event)
        guard tracked == nil else {
            // A second finger on a loop that has barely started is somebody
            // reaching for the page, not drawing.
            if hasTravelled {
                for touch in touches { ignore(touch, for: event) }
            } else {
                state = .failed
            }
            return
        }
        guard let touch = touches.first, let view else {
            state = .failed
            return
        }
        guard (event.allTouches?.count ?? 1) == 1 else {
            state = .failed
            return
        }
        let point = touch.location(in: view)
        guard shouldBegin?(point) ?? true else {
            state = .failed
            return
        }
        tracked = touch
        origin = point
        state = .began
    }

    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesMoved(touches, with: event)
        guard let tracked, touches.contains(tracked), let view else { return }
        let point = tracked.location(in: view)
        if abs(point.x - origin.x) > 2 || abs(point.y - origin.y) > 2 { hasTravelled = true }
        state = .changed
    }

    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesEnded(touches, with: event)
        guard let tracked, touches.contains(tracked) else { return }
        state = (state == .began || state == .changed) ? .ended : .failed
    }

    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent) {
        super.touchesCancelled(touches, with: event)
        guard let tracked, touches.contains(tracked) else { return }
        state = .cancelled
    }
}
