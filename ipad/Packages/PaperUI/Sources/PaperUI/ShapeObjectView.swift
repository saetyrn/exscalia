import PaperCore
import UIKit

/// A shape object's payload, read from and written to `additionalFields` the
/// way `PageRenderer` reads it: `shape`, `stroke`, `strokeWidth` and `fill`.
struct ShapeStyle: Equatable {
    var kind: ShapeKind
    /// `#rrggbb`, as stored: the light-appearance value.
    var stroke: String
    var strokeWidth: Double
    /// `#rrggbb`, drawn at `PageRenderer.shapeFillOpacity`, or nil for none.
    var fill: String?

    /// The defaults a shape object with no payload is drawn with.
    init(
        kind: ShapeKind = .rectangle, stroke: String = PageRenderer.defaultShapeStroke,
        strokeWidth: Double = 2, fill: String? = nil
    ) {
        self.kind = kind
        self.stroke = stroke
        self.strokeWidth = strokeWidth
        self.fill = fill
    }

    /// The same reading as the renderer's: an unknown kind is a rectangle, a
    /// width is held to 0.5-12, and a colour that is not one is the default.
    init(_ object: PageObject) {
        self.init()
        if case .string(let name)? = object.additionalFields["shape"],
            let kind = ShapeKind(rawValue: name.lowercased())
        {
            self.kind = kind
        }
        if case .string(let hex)? = object.additionalFields["stroke"],
            let color = ToolSettings.normalizedColor(hex)
        {
            stroke = color
        }
        switch object.additionalFields["strokeWidth"] {
        case .double(let value)? where value.isFinite: strokeWidth = min(max(value, 0.5), 12)
        case .int(let value)?: strokeWidth = min(max(Double(value), 0.5), 12)
        default: break
        }
        if case .string(let hex)? = object.additionalFields["fill"] {
            fill = ToolSettings.normalizedColor(hex)
        }
    }

    var isLine: Bool { kind == .line || kind == .arrow }

    /// Writes the payload into an object, leaving every other field alone.
    func apply(to object: inout PageObject) {
        object.additionalFields["shape"] = .string(kind.rawValue)
        object.additionalFields["stroke"] = .string(stroke)
        object.additionalFields["strokeWidth"] = .double(strokeWidth)
        object.additionalFields["fill"] = fill.map(JSONValue.string) ?? .null
    }

    /// A new shape object over `frame` (page points; a line runs from its
    /// origin to the opposite corner).
    func object(frame: PageObject.Frame) -> PageObject {
        var object = PageObject(type: .shape, frame: frame)
        apply(to: &object)
        return object
    }

    /// What the Shapes tool draws with now: its kind, the selected width and
    /// colour slots, and Fill in the stroke's colour when it is on.
    init(tools settings: ToolSettings) {
        let stroke = settings.color(for: .shape).flatMap(ToolSettings.normalizedColor)
            ?? PageRenderer.defaultShapeStroke
        self.init(
            kind: settings.shapeKind, stroke: stroke,
            strokeWidth: min(max(settings.width(for: .shape) ?? 2, 0.5), 12),
            fill: settings.shapeFill ? stroke : nil)
    }

    /// The size a tap places, centred on the tap.
    static let tapSize = CGSize(width: 160, height: 120)

    /// The frame a tap at `point` places for `kind`: 160 x 120 centred on it,
    /// a line or an arrow along the box's diagonal.
    static func tapFrame(at point: CGPoint) -> PageObject.Frame {
        PageObject.Frame(
            x: Double(point.x - tapSize.width / 2), y: Double(point.y - tapSize.height / 2),
            width: Double(tapSize.width), height: Double(tapSize.height))
    }

    /// The frame a drag from `start` to `end` draws (page points). A closed
    /// shape takes the box the drag spans; a line or an arrow keeps the
    /// drag's direction, so its width or height may be negative.
    static func dragFrame(from start: CGPoint, to end: CGPoint, kind: ShapeKind) -> PageObject.Frame {
        if kind == .line || kind == .arrow {
            return PageObject.Frame(
                x: Double(start.x), y: Double(start.y), width: Double(end.x - start.x),
                height: Double(end.y - start.y))
        }
        return PageObject.Frame(
            CGRect(
                x: min(start.x, end.x), y: min(start.y, end.y), width: abs(end.x - start.x),
                height: abs(end.y - start.y)))
    }
}

/// A shape object: the outline `PageRenderer.shapePath(for:)` gives, stroked
/// and filled the way the render draws it, so the page and its render agree.
///
/// Colours are stored as their light-appearance values; on dark paper they
/// show as PencilKit shows ink (`PageRenderer.displayColor`).
final class ShapeObjectView: ObjectView {
    private let shape = CAShapeLayer()

    override init(object: PageObject) {
        super.init(object: object)
        shape.lineCap = .round
        shape.lineJoin = .round
        content.layer.addSublayer(shape)
        content.clipsToBounds = false
        isAccessibilityElement = true
        applyShape()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        applyShape()
    }

    /// A shape may be drawn small; a line has no height at all and keeps none.
    override var minimumSize: CGSize { CGSize(width: 8, height: 8) }

    override func contentDidChange() { applyShape() }
    override func zoomChanged() { applyShape() }
    override func paperDidChange() { applyShape() }

    var style: ShapeStyle { ShapeStyle(object) }

    /// The path in the content view's coordinates, for the tests.
    var displayedPath: CGPath? { shape.path }

    private func applyShape() {
        let style = ShapeStyle(object)
        accessibilityLabel = "\(style.kind.label) shape"
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        defer { CATransaction.commit() }
        guard let path = PageRenderer.shapePath(for: object) else {
            shape.path = nil
            return
        }
        // Page points to the content view: the content is the frame scaled by
        // the zoom, its origin the frame's top-left however the line runs.
        let origin = object.frame.rect.standardized.origin
        var transform = CGAffineTransform(scaleX: zoom, y: zoom)
            .translatedBy(x: -origin.x, y: -origin.y)
        shape.path = path.copy(using: &transform)
        shape.lineWidth = CGFloat(style.strokeWidth) * zoom
        shape.strokeColor = PageRenderer.displayColor(hex: style.stroke, dark: isDarkPaper)
        if !style.isLine, let fill = style.fill,
            let color = PageRenderer.displayColor(hex: fill, dark: isDarkPaper)
        {
            shape.fillColor = color.copy(alpha: PageRenderer.shapeFillOpacity) ?? color
        } else {
            shape.fillColor = nil
        }
    }
}
