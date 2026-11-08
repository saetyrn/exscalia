import PaperCore
import PencilKit
import UIKit
import UniformTypeIdentifiers

/// What a selection holds, as far as the object menu is concerned.
enum ObjectMenuKind: String, Equatable, Sendable {
    case ink, text, image, shape, mixed

    /// The kind of a single object, or nil for one this build cannot edit.
    init?(_ type: PageObject.Kind) {
        switch type {
        case .text: self = .text
        case .image: self = .image
        case .shape: self = .shape
        case .unknown: return nil
        }
    }

    /// Every entry the menu offers for this kind, in the contract's order.
    var items: [ObjectMenuItem] {
        switch self {
        case .ink:
            return [.cut, .copy, .duplicate, .delete, .color, .convertToText, .copyLink, .askOMNIA]
        case .text:
            return [
                .edit, .cut, .copy, .duplicate, .delete, .color, .size, .bringToFront, .sendToBack,
            ]
        case .image:
            return [.cut, .copy, .duplicate, .delete, .crop, .share, .bringToFront, .sendToBack]
        case .shape:
            return [
                .cut, .copy, .duplicate, .delete, .strokeColor, .fill, .width, .bringToFront,
                .sendToBack,
            ]
        case .mixed:
            return [.cut, .copy, .duplicate, .delete, .askOMNIA]
        }
    }

    /// The entries shown as glyphs on the pill. Everything, these included,
    /// is under the overflow button with its name.
    var glyphs: [ObjectMenuItem] {
        switch self {
        case .ink: return [.cut, .copy, .duplicate, .delete, .color, .convertToText, .askOMNIA]
        case .text: return [.edit, .color, .size, .duplicate, .delete]
        case .image: return [.crop, .share, .duplicate, .delete]
        case .shape: return [.strokeColor, .fill, .width, .duplicate, .delete]
        case .mixed: return [.cut, .copy, .duplicate, .delete, .askOMNIA]
        }
    }

    /// Objects can be nudged and resized from the overflow, which is the way
    /// to do both without dragging: VoiceOver and a keyboard.
    var canResize: Bool { self == .text || self == .image || self == .shape }
}

/// One entry of the object menu.
enum ObjectMenuItem: String, CaseIterable, Sendable {
    case edit, cut, copy, duplicate, delete, color, size, convertToText, copyLink, askOMNIA
    case crop, share, strokeColor, fill, width, bringToFront, sendToBack

    var label: String {
        switch self {
        case .edit: return "Edit"
        case .cut: return "Cut"
        case .copy: return "Copy"
        case .duplicate: return "Duplicate"
        case .delete: return "Delete"
        case .color: return "Color"
        case .size: return "Size"
        case .convertToText: return "Convert to Text"
        case .copyLink: return "Copy Link"
        case .askOMNIA: return "Ask OMNIA"
        case .crop: return "Crop"
        case .share: return "Share"
        case .strokeColor: return "Stroke Color"
        case .fill: return "Fill"
        case .width: return "Width"
        case .bringToFront: return "Bring to Front"
        case .sendToBack: return "Send to Back"
        }
    }

    var symbol: String {
        switch self {
        case .edit: return "character.cursor.ibeam"
        case .cut: return "scissors"
        case .copy: return "doc.on.doc"
        case .duplicate: return "plus.square.on.square"
        case .delete: return "trash"
        case .color: return "paintpalette"
        case .size: return "textformat.size"
        case .convertToText: return "textformat"
        case .copyLink: return "link"
        case .askOMNIA: return "sparkles"
        case .crop: return "crop"
        case .share: return "square.and.arrow.up"
        case .strokeColor: return "paintbrush.pointed"
        case .fill: return "drop"
        case .width: return "lineweight"
        case .bringToFront: return "square.3.layers.3d.top.filled"
        case .sendToBack: return "square.3.layers.3d.bottom.filled"
        }
    }

    /// Entries that open a list of choices rather than acting at once.
    var hasChoices: Bool {
        switch self {
        case .color, .size, .askOMNIA, .strokeColor, .fill, .width: return true
        default: return false
        }
    }
}

/// What a tap in the object menu asks for.
enum ObjectMenuAction: Equatable, Sendable {
    case edit, cut, copy, duplicate, delete
    case color(String)
    case size(Double)
    case convertToText, copyLink
    case ai(SelectionAIAction)
    case crop, share
    case strokeColor(String)
    /// `nil` takes the fill away.
    case fill(String?)
    case width(Double)
    case bringToFront, sendToBack
    /// Nudge by page points.
    case move(CGSize)
    /// Scale about the centre.
    case scale(CGFloat)
}

/// The current values the menu's choices tick.
struct ObjectMenuContext: Equatable {
    /// The swatches every colour list offers, as `#rrggbb`.
    var swatches: [String] = ToolSettings.swatches.map(\.hex)
    var color: String?
    var sizes: [Double] = []
    var size: Double?
    var widths: [Double] = []
    var width: Double?
    var fill: String?
    var isCropping = false
}

/// The menu that floats over a selection: a dark pill of glyphs with an
/// overflow button that names every entry.
///
/// It sits above the selection, or below it when there is no room above, and
/// every action goes through `onAction`; the menu itself changes nothing.
final class ObjectMenu: UIView {
    var onAction: ((ObjectMenuAction) -> Void)?

    private(set) var kind: ObjectMenuKind?
    private(set) var context = ObjectMenuContext()
    private let stack = UIStackView()

    init() {
        super.init(frame: .zero)
        // The pill is the same in light and dark, as the tool menu is, and so
        // are the lists it opens.
        overrideUserInterfaceStyle = .dark
        PaperTokens.applyFloatingPillStyle(to: self, cornerRadius: 22)
        accessibilityIdentifier = "ObjectMenu"
        accessibilityContainerType = .semanticGroup
        accessibilityLabel = "Selection"

        stack.axis = .horizontal
        stack.alignment = .center
        stack.spacing = 0
        stack.translatesAutoresizingMaskIntoConstraints = false
        addSubview(stack)
        NSLayoutConstraint.activate([
            stack.topAnchor.constraint(equalTo: topAnchor, constant: 2),
            stack.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -2),
            stack.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 6),
            stack.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -6),
        ])
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("ObjectMenu is created in code") }

    /// Rebuilds the buttons for a kind of selection. Building is cheap and a
    /// selection changes kind rarely, so it is rebuilt whole.
    func configure(kind: ObjectMenuKind, context: ObjectMenuContext = ObjectMenuContext()) {
        guard kind != self.kind || context != self.context else { return }
        self.kind = kind
        self.context = context
        for view in stack.arrangedSubviews { view.removeFromSuperview() }
        for item in kind.glyphs {
            stack.addArrangedSubview(button(for: item))
        }
        let more = glyphButton(symbol: "ellipsis", label: "More")
        more.showsMenuAsPrimaryAction = true
        more.menu = overflowMenu(kind)
        stack.addArrangedSubview(more)
        invalidateIntrinsicContentSize()
    }

    /// The labels of the glyphs on the pill, left to right. For the tests.
    var glyphLabels: [String] {
        stack.arrangedSubviews.compactMap { $0.accessibilityLabel }
    }

    /// The size the pill wants.
    var fittingSize: CGSize { systemLayoutSizeFitting(UIView.layoutFittingCompressedSize) }

    /// Puts the menu over `selection` (in the superview's coordinates): above
    /// it, or below it when there is no room above, kept inside `visible`.
    func place(around selection: CGRect, visible: CGRect) {
        let size = fittingSize
        bounds.size = size
        center = Self.center(size: size, around: selection, visible: visible)
    }

    /// Where a menu of `size` goes for a selection, as a centre point. Pure.
    static func center(size: CGSize, around selection: CGRect, visible: CGRect) -> CGPoint {
        let margin: CGFloat = 8
        let gap: CGFloat = 16
        let area = visible.isNull || visible.isEmpty ? selection.insetBy(dx: -2000, dy: -2000) : visible
        let minX = area.minX + size.width / 2 + margin
        let maxX = max(minX, area.maxX - size.width / 2 - margin)
        let x = min(max(selection.midX, minX), maxX)
        let above = selection.minY - gap - size.height / 2
        let below = selection.maxY + gap + size.height / 2
        let y: CGFloat
        if above - size.height / 2 >= area.minY + margin {
            y = above
        } else if below + size.height / 2 <= area.maxY - margin {
            y = below
        } else {
            // Neither fits: over the top of the selection, inside the view.
            y = max(area.minY + margin + size.height / 2, min(above, area.maxY - margin - size.height / 2))
        }
        return CGPoint(x: x, y: y)
    }

    /// The part of `view` the person can see: its bounds, cut to the scroll
    /// view it is in. A board's canvas is that scroll view; a page's is not.
    static func visibleBounds(of view: UIView) -> CGRect {
        var scroller: UIView? = view.superview
        while let current = scroller,
            !(current is UIScrollView && (current as? PKCanvasView)?.isScrollEnabled != false)
        {
            scroller = current.superview
        }
        guard let scroller else { return view.bounds }
        let visible = view.convert(scroller.bounds, from: scroller)
        let cut = view.bounds.intersection(visible)
        return cut.isNull ? view.bounds : cut
    }

    // MARK: - Buttons

    private func button(for item: ObjectMenuItem) -> UIButton {
        let button = glyphButton(symbol: item.symbol, label: item.label)
        if item.hasChoices {
            button.showsMenuAsPrimaryAction = true
            button.menu = UIMenu(title: item.label, children: choices(for: item))
        } else {
            button.addAction(
                UIAction { [weak self] _ in self?.perform(item) }, for: .touchUpInside)
        }
        if item == .crop, context.isCropping {
            button.tintColor = UIColor(PaperTokens.accent)
            button.accessibilityValue = "cropping"
        }
        return button
    }

    private func glyphButton(symbol: String, label: String) -> UIButton {
        let button = UIButton(type: .system)
        button.setImage(
            UIImage(
                systemName: symbol,
                withConfiguration: UIImage.SymbolConfiguration(pointSize: 16, weight: .regular)),
            for: .normal)
        button.tintColor = .white
        button.translatesAutoresizingMaskIntoConstraints = false
        button.accessibilityLabel = label
        button.largeContentTitle = label
        button.showsLargeContentViewer = true
        button.addInteraction(UILargeContentViewerInteraction())
        NSLayoutConstraint.activate([
            button.widthAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
            button.heightAnchor.constraint(equalToConstant: PaperTokens.minimumTarget),
        ])
        return button
    }

    private func perform(_ item: ObjectMenuItem) {
        switch item {
        case .edit: onAction?(.edit)
        case .cut: onAction?(.cut)
        case .copy: onAction?(.copy)
        case .duplicate: onAction?(.duplicate)
        case .delete: onAction?(.delete)
        case .convertToText: onAction?(.convertToText)
        case .copyLink: onAction?(.copyLink)
        case .crop: onAction?(.crop)
        case .share: onAction?(.share)
        case .bringToFront: onAction?(.bringToFront)
        case .sendToBack: onAction?(.sendToBack)
        case .color, .size, .askOMNIA, .strokeColor, .fill, .width: break
        }
    }

    private func action(_ title: String, _ symbol: String? = nil, on: Bool = false, _ value: ObjectMenuAction)
        -> UIAction
    {
        UIAction(
            title: title, image: symbol.flatMap { UIImage(systemName: $0) }, state: on ? .on : .off
        ) { [weak self] _ in self?.onAction?(value) }
    }

    private func choices(for item: ObjectMenuItem) -> [UIMenuElement] {
        switch item {
        case .color:
            return swatchActions(selected: context.color) { .color($0) }
        case .strokeColor:
            return swatchActions(selected: context.color) { .strokeColor($0) }
        case .fill:
            return [action("None", on: context.fill == nil, .fill(nil))]
                + swatchActions(selected: context.fill) { .fill($0) }
        case .size:
            return context.sizes.map { size in
                action(Self.points(size), on: context.size == size, .size(size))
            }
        case .width:
            return context.widths.map { width in
                action(Self.points(width), on: context.width == width, .width(width))
            }
        case .askOMNIA:
            return SelectionAIAction.allCases.map { action($0.title, $0.symbol, .ai($0)) }
        default:
            return []
        }
    }

    private func swatchActions(selected: String?, _ make: @escaping (String) -> ObjectMenuAction)
        -> [UIMenuElement]
    {
        context.swatches.map { hex in
            UIAction(
                title: ToolSettings.swatchName(hex) ?? hex,
                image: Self.swatchImage(hex),
                state: ToolSettings.normalizedColor(selected ?? "") == ToolSettings.normalizedColor(hex)
                    ? .on : .off
            ) { [weak self] _ in self?.onAction?(make(hex)) }
        }
    }

    /// The overflow: every entry by name, then Move and, for an object,
    /// Resize, which are the ways to do both without a drag.
    private func overflowMenu(_ kind: ObjectMenuKind) -> UIMenu {
        var children: [UIMenuElement] = kind.items.map { item in
            if item.hasChoices {
                return UIMenu(
                    title: item.label, image: UIImage(systemName: item.symbol),
                    children: choices(for: item))
            }
            return UIAction(title: item.label, image: UIImage(systemName: item.symbol),
                attributes: item == .delete ? .destructive : []
            ) { [weak self] _ in self?.perform(item) }
        }
        let step: CGFloat = 16
        var arrange: [UIMenuElement] = [
            UIMenu(
                title: "Move", image: UIImage(systemName: "arrow.up.and.down.and.arrow.left.and.right"),
                children: [
                    action("Move Up", "arrow.up", .move(CGSize(width: 0, height: -step))),
                    action("Move Down", "arrow.down", .move(CGSize(width: 0, height: step))),
                    action("Move Left", "arrow.left", .move(CGSize(width: -step, height: 0))),
                    action("Move Right", "arrow.right", .move(CGSize(width: step, height: 0))),
                ])
        ]
        if kind.canResize {
            arrange.append(
                UIMenu(
                    title: "Resize", image: UIImage(systemName: "arrow.up.left.and.arrow.down.right"),
                    children: [
                        action("Larger", "plus.magnifyingglass", .scale(1.25)),
                        action("Smaller", "minus.magnifyingglass", .scale(0.8)),
                    ]))
        }
        children.append(UIMenu(title: "", options: .displayInline, children: arrange))
        return UIMenu(title: "", children: children)
    }

    static func points(_ value: Double) -> String {
        value == value.rounded() ? "\(Int(value)) pt" : String(format: "%.1f pt", value)
    }

    /// A filled circle of the colour, for the colour lists.
    static func swatchImage(_ hex: String) -> UIImage? {
        let color = ToolState.uiColor(hex)
        let size = CGSize(width: 18, height: 18)
        return UIGraphicsImageRenderer(size: size).image { context in
            color.setFill()
            context.cgContext.fillEllipse(in: CGRect(origin: .zero, size: size).insetBy(dx: 1, dy: 1))
            UIColor.white.withAlphaComponent(0.4).setStroke()
            context.cgContext.strokeEllipse(in: CGRect(origin: .zero, size: size).insetBy(dx: 1, dy: 1))
        }.withRenderingMode(.alwaysOriginal)
    }
}

// MARK: - Clipboard

/// A selection on the pasteboard: Paper's own copy for pasting back onto a
/// page, beside a plain picture or string other apps can take.
struct PaperClip: Codable, Equatable {
    /// `PKDrawing.dataRepresentation()` of the strokes, where they lay.
    var drawing: Data?
    var objects: [PageObject]
    /// The notebook the objects' assets live in.
    var notebookPath: String

    static let type = "art.exscalia.paper.selection"

    /// The strokes and the objects together, in page points.
    var bounds: CGRect {
        var union = CGRect.null
        if let drawing, let ink = try? PKDrawing(data: drawing), !ink.strokes.isEmpty {
            union = union.union(ink.bounds)
        }
        for object in objects { union = union.union(object.frame.rect.standardized) }
        return union
    }

    /// Puts the selection on the general pasteboard.
    @MainActor
    func write(image: UIImage?, string: String?) {
        guard let data = try? JSONEncoder().encode(self) else { return }
        var item: [String: Any] = [Self.type: data]
        if let png = image?.pngData() { item[UTType.png.identifier] = png }
        if let string { item[UTType.utf8PlainText.identifier] = string }
        UIPasteboard.general.setItems([item])
    }

    /// Paper's own copy on the general pasteboard, if there is one.
    @MainActor
    static func read() -> PaperClip? {
        guard UIPasteboard.general.contains(pasteboardTypes: [type]),
            let data = UIPasteboard.general.data(forPasteboardType: type)
        else { return nil }
        return try? JSONDecoder().decode(PaperClip.self, from: data)
    }

    /// Whether anything on the pasteboard can be pasted onto a page.
    @MainActor
    static var hasPasteable: Bool {
        let board = UIPasteboard.general
        return board.contains(pasteboardTypes: [type]) || board.hasImages || board.hasStrings
    }
}
