import PaperCore
import PencilKit
import UIKit

/// The five things the capsule can be doing. Fixed order, fixed set: the
/// research calls this the constitutional toolbar.
public enum PaperTool: String, CaseIterable, Sendable {
    case pen
    case highlighter
    case eraser
    case lasso

    var symbolName: String {
        switch self {
        case .pen: return "pencil.tip"
        case .highlighter: return "highlighter"
        case .eraser: return "eraser"
        case .lasso: return "lasso"
        }
    }

    /// Filled variant, used for the active tool so state is a shape and not
    /// only a colour.
    var filledSymbolName: String {
        switch self {
        case .pen: return "pencil.tip.crop.circle.fill"
        case .highlighter: return "highlighter"
        case .eraser: return "eraser.fill"
        case .lasso: return "lasso.badge.sparkles"
        }
    }

    var label: String {
        switch self {
        case .pen: return "Pen"
        case .highlighter: return "Highlighter"
        case .eraser: return "Eraser"
        case .lasso: return "Lasso"
        }
    }
}

/// A colour with a name, because "the blue one" is not something a VoiceOver
/// user can see and colour alone is never allowed to carry meaning.
public struct NamedInkColor: Equatable, Sendable {
    public let name: String
    public let red: CGFloat
    public let green: CGFloat
    public let blue: CGFloat

    var uiColor: UIColor { UIColor(red: red, green: green, blue: blue, alpha: 1) }

    static func rgb(_ name: String, _ hex: UInt32) -> NamedInkColor {
        NamedInkColor(
            name: name,
            red: CGFloat((hex >> 16) & 0xff) / 255,
            green: CGFloat((hex >> 8) & 0xff) / 255,
            blue: CGFloat(hex & 0xff) / 255)
    }
}

/// Which tool is selected and how it is configured, persisted so the pen a
/// person picked is the pen they get back tomorrow.
@MainActor
public final class ToolState {
    /// Eight named chips: enough range, few enough to hit without looking.
    public static let penColors: [NamedInkColor] = [
        .rgb("Black", 0x1c1c1e),
        .rgb("Graphite", 0x6b6f76),
        .rgb("Blue", 0x1f6feb),
        .rgb("Indigo", 0x6c5ce7),
        .rgb("Teal", 0x0f8b8d),
        .rgb("Green", 0x2f7d32),
        .rgb("Amber", 0xb26a00),
        .rgb("Red", 0xc0392b),
    ]
    public static let highlighterColors: [NamedInkColor] = [
        .rgb("Yellow", 0xffe066),
        .rgb("Green", 0xa8e6a1),
        .rgb("Blue", 0x9ecbff),
        .rgb("Pink", 0xffb3c7),
    ]

    /// Continuous ranges rather than three presets: a pen has a size, not a
    /// small number of sizes, and the slider is what lets a person find the
    /// one they write with.
    public static let penWidthRange: ClosedRange<CGFloat> = 0.5...12
    public static let highlighterWidthRange: ClosedRange<CGFloat> = 6...40
    public static let eraserWidthRange: ClosedRange<CGFloat> = 4...60

    /// The width range for a tool, and the width it starts at.
    public static func widthRange(for tool: PaperTool) -> ClosedRange<CGFloat> {
        switch tool {
        case .pen, .lasso: return penWidthRange
        case .highlighter: return highlighterWidthRange
        case .eraser: return eraserWidthRange
        }
    }

    private enum Key {
        static let tool = "paper.tool"
        static let penWidth = "paper.tool.penWidth"
        static let penColor = "paper.tool.penColor"
        static let highlighterWidth = "paper.tool.highlighterWidth"
        static let highlighterColor = "paper.tool.highlighterColor"
        static let eraserBitmap = "paper.tool.eraserBitmap"
        static let eraserWidth = "paper.tool.eraserWidth"
        static let capsuleEdge = "paper.tool.capsuleEdge"
        static let capsuleOffset = "paper.tool.capsuleOffset"
    }

    private let defaults: UserDefaults

    public private(set) var tool: PaperTool
    public var penWidth: CGFloat {
        didSet {
            penWidth = Self.clamp(penWidth, to: Self.penWidthRange, from: oldValue)
            defaults.set(Double(penWidth), forKey: Key.penWidth)
        }
    }
    public var penColorIndex: Int { didSet { defaults.set(penColorIndex, forKey: Key.penColor) } }
    public var highlighterWidth: CGFloat {
        didSet {
            highlighterWidth = Self.clamp(
                highlighterWidth, to: Self.highlighterWidthRange, from: oldValue)
            defaults.set(Double(highlighterWidth), forKey: Key.highlighterWidth)
        }
    }
    public var highlighterColorIndex: Int {
        didSet { defaults.set(highlighterColorIndex, forKey: Key.highlighterColor) }
    }
    public var eraserIsBitmap: Bool {
        didSet { defaults.set(eraserIsBitmap, forKey: Key.eraserBitmap) }
    }
    public var eraserWidth: CGFloat {
        didSet {
            eraserWidth = Self.clamp(eraserWidth, to: Self.eraserWidthRange, from: oldValue)
            defaults.set(Double(eraserWidth), forKey: Key.eraserWidth)
        }
    }

    /// Keeps a width inside its range without ever re-entering `didSet`.
    private static func clamp(
        _ value: CGFloat, to range: ClosedRange<CGFloat>, from previous: CGFloat
    ) -> CGFloat {
        guard value.isFinite else { return previous }
        return min(max(value, range.lowerBound), range.upperBound)
    }

    /// The current width of a tool, and where a slider writes it back.
    public func width(for tool: PaperTool) -> CGFloat {
        switch tool {
        case .pen, .lasso: return penWidth
        case .highlighter: return highlighterWidth
        case .eraser: return eraserWidth
        }
    }

    public func setWidth(_ width: CGFloat, for tool: PaperTool) {
        switch tool {
        case .pen, .lasso: penWidth = width
        case .highlighter: highlighterWidth = width
        case .eraser: eraserWidth = width
        }
    }

    /// The tool the Pencil's double-tap "switch to previous" action returns to.
    public private(set) var previousTool: PaperTool = .pen

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        tool = PaperTool(rawValue: defaults.string(forKey: Key.tool) ?? "") ?? .pen
        penWidth = Self.clamp(
            defaults.object(forKey: Key.penWidth).map { CGFloat($0 as? Double ?? 3) } ?? 3,
            to: Self.penWidthRange, from: 3)
        penColorIndex = defaults.object(forKey: Key.penColor).map { $0 as? Int ?? 0 } ?? 0
        highlighterWidth = Self.clamp(
            defaults.object(forKey: Key.highlighterWidth).map { CGFloat($0 as? Double ?? 20) }
                ?? 20, to: Self.highlighterWidthRange, from: 20)
        highlighterColorIndex =
            defaults.object(forKey: Key.highlighterColor).map { $0 as? Int ?? 0 } ?? 0
        eraserIsBitmap = defaults.object(forKey: Key.eraserBitmap).map { $0 as? Bool ?? true } ?? true
        eraserWidth = Self.clamp(
            defaults.object(forKey: Key.eraserWidth).map { CGFloat($0 as? Double ?? 24) } ?? 24,
            to: Self.eraserWidthRange, from: 24)
    }

    public func select(_ next: PaperTool) {
        guard next != tool else { return }
        previousTool = tool
        tool = next
        defaults.set(next.rawValue, forKey: Key.tool)
    }

    /// The Pencil "switch to previous tool" action.
    public func switchToPrevious() {
        select(previousTool)
    }

    public var penColor: NamedInkColor { Self.penColors[min(penColorIndex, Self.penColors.count - 1)] }
    public var highlighterColor: NamedInkColor {
        Self.highlighterColors[min(highlighterColorIndex, Self.highlighterColors.count - 1)]
    }

    /// What the live canvases get. Lasso is ours, not PencilKit's, so it maps
    /// to the pen and the overlay swallows the touches instead.
    public var pkTool: PKTool {
        switch tool {
        case .pen:
            return PKInkingTool(.pen, color: penColor.uiColor, width: penWidth)
        case .highlighter:
            return PKInkingTool(.marker, color: highlighterColor.uiColor, width: highlighterWidth)
        case .eraser:
            return PKEraserTool(eraserIsBitmap ? .bitmap : .vector, width: eraserWidth)
        case .lasso:
            return PKInkingTool(.pen, color: penColor.uiColor, width: penWidth)
        }
    }

    /// Radius of the hover circle for the current tool, in page points.
    public var hoverRadius: CGFloat {
        switch tool {
        case .pen: return max(penWidth, 2) / 2 + 1
        case .highlighter: return highlighterWidth / 2
        case .eraser: return eraserWidth / 2
        case .lasso: return 6
        }
    }

    // MARK: - Capsule placement

    public enum CapsuleEdge: String, Sendable {
        case leading
        case trailing
        case bottom
    }

    public var capsuleEdge: CapsuleEdge {
        get { CapsuleEdge(rawValue: defaults.string(forKey: Key.capsuleEdge) ?? "") ?? .trailing }
        set { defaults.set(newValue.rawValue, forKey: Key.capsuleEdge) }
    }

    /// Position along the chosen edge, 0...1.
    public var capsuleOffset: CGFloat {
        get {
            let stored = defaults.object(forKey: Key.capsuleOffset) as? Double
            return CGFloat(stored ?? 0.5)
        }
        set { defaults.set(Double(min(max(newValue, 0), 1)), forKey: Key.capsuleOffset) }
    }
}
