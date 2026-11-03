import CoreFoundation
import Foundation

// The editor's tools, their slots and how they persist. Pure data and pure
// rules: the UI layer wraps `ToolSettings` in an observable object, calls the
// mutating methods here, and saves through `ToolSettingsStore`. Nothing in this
// file imports PencilKit; `InkKind` carries PencilKit's own raw values instead.

/// Every tool the editor can hold. The raw values `pen`, `highlighter`,
/// `eraser` and `lasso` are the ones older builds wrote to `paper.tool`, so a
/// launch argument such as `-paper.tool lasso` keeps working.
public enum PaperTool: String, Codable, CaseIterable, Sendable {
    case lasso, pen, pencil, highlighter, drawShape, eraser, text, image, shape

    /// Pen, pencil, highlighter and Draw Shape: the tools that make ink.
    public var isWriting: Bool {
        switch self {
        case .pen, .pencil, .highlighter, .drawShape: return true
        default: return false
        }
    }

    /// Text, image and shape: placed once, then the tool falls back to lasso.
    public var isInsert: Bool {
        switch self {
        case .text, .image, .shape: return true
        default: return false
        }
    }

    /// Whether the tool keeps stroke and colour slots. Lasso and image do not.
    public var hasSlots: Bool { self != .lasso && self != .image }

    /// Whether tapping the selected tool shows its tool menu. Lasso and image
    /// have nothing to show.
    public var hasToolMenu: Bool { hasSlots }

    /// The writing tools in the order the tool menu lists them.
    public static let writingTools: [PaperTool] = [.pen, .pencil, .highlighter, .drawShape]

    /// The tools that keep slots, in the order `ToolSettings.slots` holds them.
    public static let slotTools: [PaperTool] = [
        .pen, .pencil, .highlighter, .drawShape, .eraser, .text, .shape,
    ]

    /// The toolbar button this tool sits under.
    public var toolbarSlot: ToolbarSlot {
        switch self {
        case .lasso: return .lasso
        case .pen, .pencil, .highlighter, .drawShape: return .writing
        case .eraser: return .eraser
        case .text: return .text
        case .image: return .image
        case .shape: return .shape
        }
    }

    /// The accessibility label. UI tests find toolbar buttons by it.
    public var label: String {
        switch self {
        case .lasso: return "Lasso"
        case .pen: return "Pen"
        case .pencil: return "Pencil"
        case .highlighter: return "Highlighter"
        case .drawShape: return "Draw Shape"
        case .eraser: return "Eraser"
        case .text: return "Text"
        case .image: return "Image"
        case .shape: return "Shapes"
        }
    }

    /// The monochrome SF Symbol for the toolbar and the tool menu.
    public var symbolName: String {
        switch self {
        case .lasso: return "lasso"
        case .pen: return "pencil.tip"
        case .pencil: return "pencil"
        case .highlighter: return "highlighter"
        case .drawShape: return "scribble.variable"
        case .eraser: return "eraser"
        case .text: return "character.textbox"
        case .image: return "photo"
        case .shape: return "square.on.circle"
        }
    }
}

/// The toolbar's middle group, left to right. The same for notebooks and
/// whiteboards; the `writing` button shows `ToolSettings.lastWritingTool`.
public enum ToolbarSlot: String, CaseIterable, Sendable {
    case lasso, writing, eraser, text, image, shape
}

public enum PenStyle: String, Codable, CaseIterable, Sendable {
    case fountain, ball, brush

    public var label: String {
        switch self {
        case .fountain: return "Fountain Pen"
        case .ball: return "Ball Pen"
        case .brush: return "Brush Pen"
        }
    }
}

public enum EraserMode: String, Codable, CaseIterable, Sendable {
    /// Removes only what lies under the eraser.
    case partial
    /// Removes every stroke it touches.
    case stroke

    public var label: String {
        switch self {
        case .partial: return "Partial"
        case .stroke: return "Whole Stroke"
        }
    }
}

/// What the Shapes insert tool places.
public enum ShapeKind: String, Codable, CaseIterable, Sendable {
    case rectangle, ellipse, triangle, line, arrow

    public var label: String {
        switch self {
        case .rectangle: return "Rectangle"
        case .ellipse: return "Ellipse"
        case .triangle: return "Triangle"
        case .line: return "Line"
        case .arrow: return "Arrow"
        }
    }

    public var symbolName: String {
        switch self {
        case .rectangle: return "rectangle"
        case .ellipse: return "circle"
        case .triangle: return "triangle"
        case .line: return "line.diagonal"
        case .arrow: return "arrow.up.right"
        }
    }
}

/// The screen edge the tool menu is snapped to.
public enum ToolMenuDock: String, Codable, CaseIterable, Sendable {
    case top, bottom, leading, trailing

    /// `top` and `bottom` lay the menu out in a row, the sides in a column.
    public var isHorizontal: Bool { self == .top || self == .bottom }
}

/// A PencilKit ink, as data. Each raw value is `PKInk.InkType`'s own, so the UI
/// maps it with `PKInk.InkType(rawValue:)`; a PaperCore test pins every value
/// against PencilKit on the Mac.
public enum InkKind: String, CaseIterable, Sendable {
    case fountainPen = "com.apple.ink.fountainpen"
    case monoline = "com.apple.ink.monoline"
    case pen = "com.apple.ink.pen"
    case pencil = "com.apple.ink.pencil"
    case marker = "com.apple.ink.marker"
}

/// What the live canvases are given, as data. `.ink` becomes
/// `PKInkingTool(InkType(rawValue: kind.rawValue), color:, width:)`;
/// `.eraser(.partial, w)` becomes `PKEraserTool(.fixedWidthBitmap, width: w)`
/// and `.eraser(.stroke, _)` becomes `PKEraserTool(.vector)`.
public enum CanvasTool: Equatable, Sendable {
    case ink(InkKind, color: String, width: Double)
    case eraser(EraserMode, width: Double)
}

/// One tool's three stroke slots and its colour slots.
public struct ToolSlots: Codable, Equatable, Sendable {
    /// Exactly three widths, in points (font points for text).
    public var widths: [Double]
    /// The selected width, 0...2.
    public var widthIndex: Int
    /// `#rrggbb`, lowercase, 1...12 of them; empty for the eraser.
    public var colors: [String]
    /// The selected colour.
    public var colorIndex: Int

    /// How many stroke slots every tool has.
    public static let widthCount = 3
    /// The most colour slots a tool can grow to.
    public static let maximumColors = 12

    public init(widths: [Double], widthIndex: Int, colors: [String], colorIndex: Int) {
        self.widths = widths
        self.widthIndex = widthIndex
        self.colors = colors
        self.colorIndex = colorIndex
    }

    /// The selected width, or the first when the index is out of range.
    public var width: Double {
        widths.indices.contains(widthIndex) ? widths[widthIndex] : (widths.first ?? 1)
    }

    /// The selected colour, `nil` for the eraser.
    public var color: String? {
        colors.indices.contains(colorIndex) ? colors[colorIndex] : colors.first
    }

    private enum CodingKeys: String, CodingKey {
        case widths, widthIndex, colors, colorIndex
    }

    /// Never throws for a malformed field: a width that is not a number reads
    /// as NaN and an invalid colour is dropped, and `ToolSettings` then repairs
    /// both from the tool's factory slots.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let rawWidths = (try? container.decode([Lossy<Double>].self, forKey: .widths)) ?? []
        widths = rawWidths.map { $0.value ?? .nan }
        widthIndex = (try? container.decode(Int.self, forKey: .widthIndex)) ?? 0
        let rawColors = (try? container.decode([Lossy<String>].self, forKey: .colors)) ?? []
        colors = rawColors.compactMap(\.value)
        colorIndex = (try? container.decode(Int.self, forKey: .colorIndex)) ?? 0
    }
}

/// Everything about the tools that survives a relaunch: one value, stored as
/// JSON under `paper.tools.v2`.
public struct ToolSettings: Codable, Equatable, Sendable {
    public var version: Int
    public var active: PaperTool
    /// The tool Pencil double-tap "switch to previous" returns to.
    public var previous: PaperTool
    /// The writing tool the toolbar's writing button shows and selects.
    public var lastWritingTool: PaperTool
    public var penStyle: PenStyle
    public var eraserMode: EraserMode
    public var shapeKind: ShapeKind
    public var shapeFill: Bool
    /// One entry for each of `PaperTool.slotTools`.
    public var slots: [PaperTool: ToolSlots]
    public var dock: ToolMenuDock
    public var menuVisible: Bool

    public static let currentVersion = 2

    /// The eight named swatches the colour picker offers, at today's values.
    /// They are also the palette `paper.tool.penColor` indexed.
    public static let swatches: [(name: String, hex: String)] = [
        ("Black", "#1c1c1e"),
        ("Graphite", "#6b6f76"),
        ("Blue", "#1f6feb"),
        ("Indigo", "#6c5ce7"),
        ("Teal", "#0f8b8d"),
        ("Green", "#2f7d32"),
        ("Amber", "#b26a00"),
        ("Red", "#c0392b"),
    ]

    /// The highlighter's own swatches, light enough that writing under them
    /// still reads. Its factory colours and the palette
    /// `paper.tool.highlighterColor` indexed are among them.
    public static let highlighterSwatches: [(name: String, hex: String)] = [
        ("Yellow", "#ffe066"),
        ("Green", "#a8e6a1"),
        ("Pink", "#ffb3c7"),
        ("Blue", "#9ecbff"),
        ("Orange", "#ffc078"),
        ("Purple", "#d0b0ff"),
    ]

    /// What a tool's colour picker offers: the highlighter its own swatches,
    /// every other tool the eight above.
    public static func swatches(for tool: PaperTool) -> [(name: String, hex: String)] {
        tool == .highlighter ? highlighterSwatches : swatches
    }

    /// The allowed width of each tool's slots. Lasso and image have no slots
    /// and answer the pen's range.
    public static func widthRange(_ tool: PaperTool) -> ClosedRange<Double> {
        switch tool {
        case .highlighter: return 6...40
        case .eraser: return 4...60
        case .text: return 9...72
        case .pen, .pencil, .drawShape, .shape, .lasso, .image: return 0.5...12
        }
    }

    /// A tool's slots on a fresh install, or `nil` for lasso and image.
    public static func factorySlots(_ tool: PaperTool) -> ToolSlots? {
        let penColors = ["#1c1c1e", "#1f6feb", "#c0392b"]
        switch tool {
        case .pen, .drawShape, .shape:
            // The medium slot is measured against the reference app's medium
            // fountain pen at 100 %: a stroke there covers about 5 points
            // across, and PencilKit's fountain pen at width 2 covered 2.
            return ToolSlots(widths: [2.5, 5.0, 8.5], widthIndex: 1, colors: penColors, colorIndex: 0)
        case .pencil:
            // Scaled with the pen: PencilKit's pencil at a width draws about
            // that wide, like its fountain pen.
            return ToolSlots(
                widths: [4.0, 6.0, 10.0], widthIndex: 1,
                colors: ["#3a3a3c", "#1c1c1e", "#1f6feb"], colorIndex: 0)
        case .highlighter:
            // Widened with the pen, by less: a marker is already a band, and
            // the reference's medium highlighter covers about a line of text.
            return ToolSlots(
                widths: [14, 22, 32], widthIndex: 1,
                colors: ["#ffe066", "#a8e6a1", "#ffb3c7"], colorIndex: 0)
        case .eraser:
            return ToolSlots(widths: [8, 16, 32], widthIndex: 1, colors: [], colorIndex: 0)
        case .text:
            return ToolSlots(widths: [13, 17, 24], widthIndex: 1, colors: penColors, colorIndex: 0)
        case .lasso, .image:
            return nil
        }
    }

    public static let factory = ToolSettings(
        version: currentVersion, active: .pen, previous: .pen, lastWritingTool: .pen,
        penStyle: .fountain, eraserMode: .partial, shapeKind: .rectangle, shapeFill: false,
        slots: Dictionary(
            uniqueKeysWithValues: PaperTool.slotTools.compactMap { tool in
                factorySlots(tool).map { (tool, $0) }
            }),
        dock: .top, menuVisible: true)

    init(
        version: Int, active: PaperTool, previous: PaperTool, lastWritingTool: PaperTool,
        penStyle: PenStyle, eraserMode: EraserMode, shapeKind: ShapeKind, shapeFill: Bool,
        slots: [PaperTool: ToolSlots], dock: ToolMenuDock, menuVisible: Bool
    ) {
        self.version = version
        self.active = active
        self.previous = previous
        self.lastWritingTool = lastWritingTool
        self.penStyle = penStyle
        self.eraserMode = eraserMode
        self.shapeKind = shapeKind
        self.shapeFill = shapeFill
        self.slots = slots
        self.dock = dock
        self.menuVisible = menuVisible
    }

    // MARK: Colours

    /// `#rrggbb` in lowercase, or `nil` when `hex` is not a six-digit colour.
    /// Surrounding whitespace and upper case are accepted.
    public static func normalizedColor(_ hex: String) -> String? {
        let trimmed = hex.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard trimmed.count == 7, trimmed.first == "#" else { return nil }
        let digits = trimmed.dropFirst()
        guard digits.allSatisfy({ $0.isHexDigit && $0.isASCII }) else { return nil }
        return trimmed
    }

    /// The colour's components in 0...1, for `UIColor(red:green:blue:alpha:)`.
    public static func rgb(_ hex: String) -> (red: Double, green: Double, blue: Double)? {
        guard let color = normalizedColor(hex), let value = UInt32(color.dropFirst(), radix: 16)
        else { return nil }
        return (
            Double((value >> 16) & 0xff) / 255, Double((value >> 8) & 0xff) / 255,
            Double(value & 0xff) / 255
        )
    }

    /// The swatch name for a colour, for VoiceOver; `nil` for a custom colour.
    public static func swatchName(_ hex: String) -> String? {
        guard let color = normalizedColor(hex) else { return nil }
        return (swatches + highlighterSwatches).first { $0.hex == color }?.name
    }

    // MARK: Reading

    /// The toolbar slot's tool: `writing` is the last writing tool.
    public func tool(for slot: ToolbarSlot) -> PaperTool {
        switch slot {
        case .lasso: return .lasso
        case .writing: return lastWritingTool
        case .eraser: return .eraser
        case .text: return .text
        case .image: return .image
        case .shape: return .shape
        }
    }

    /// Whether the toolbar shows this slot as selected.
    public func isSelected(_ slot: ToolbarSlot) -> Bool { active.toolbarSlot == slot }

    /// Whether the floating tool menu is on screen.
    public var showsToolMenu: Bool { menuVisible && active.hasToolMenu }

    /// The selected width of a tool, `nil` for lasso and image.
    public func width(for tool: PaperTool) -> Double? { slots[tool]?.width }

    /// The selected colour of a tool, `nil` for the eraser, lasso and image.
    public func color(for tool: PaperTool) -> String? { slots[tool]?.color }

    /// The PencilKit ink a writing tool draws with; `nil` for other tools.
    public func ink(for tool: PaperTool) -> InkKind? {
        switch tool {
        case .pen:
            switch penStyle {
            case .fountain: return .fountainPen
            case .ball: return .monoline
            case .brush: return .pen
            }
        case .pencil: return .pencil
        case .highlighter: return .marker
        case .drawShape: return .monoline
        default: return nil
        }
    }

    /// What the canvases get for the active tool. Lasso and the insert tools
    /// leave the canvas holding the last writing tool's ink; their touches
    /// never reach it.
    public var canvasTool: CanvasTool { canvasTool(for: active) }

    public func canvasTool(for tool: PaperTool) -> CanvasTool {
        if tool == .eraser {
            return .eraser(eraserMode, width: width(for: .eraser) ?? 16)
        }
        let writing = tool.isWriting ? tool : lastWritingTool
        let kind = ink(for: writing) ?? .fountainPen
        let factory = Self.factorySlots(writing)
        return .ink(
            kind, color: color(for: writing) ?? factory?.color ?? "#1c1c1e",
            width: width(for: writing) ?? factory?.width ?? 2)
    }

    /// Radius of the Pencil hover preview, in page points.
    public var hoverRadius: Double {
        let width = self.width(for: active) ?? 0
        switch active {
        case .pen, .pencil, .drawShape: return max(width, 2) / 2 + 1
        case .highlighter, .eraser: return width / 2
        case .lasso, .text, .image, .shape: return 6
        }
    }

    // MARK: Choosing tools

    /// A tap on a toolbar button. An unselected tool is selected; the selected
    /// one shows or hides the tool menu.
    public mutating func tap(_ slot: ToolbarSlot) {
        let tool = self.tool(for: slot)
        if tool == active {
            if tool.hasToolMenu { menuVisible.toggle() }
        } else {
            select(tool)
        }
    }

    /// Makes `tool` active, remembering the one it replaces as `previous`.
    /// Selecting the active tool does nothing.
    public mutating func select(_ tool: PaperTool) {
        guard tool != active else { return }
        previous = active
        active = tool
        if tool.isWriting { lastWritingTool = tool }
    }

    /// Picks a pen style in the tool menu, which also selects the pen.
    public mutating func selectPenStyle(_ style: PenStyle) {
        penStyle = style
        select(.pen)
    }

    /// Pencil double-tap "switch to previous": swaps the active tool with
    /// `previous`.
    public mutating func switchToPrevious() {
        select(previous)
    }

    /// Pencil double-tap "switch to eraser": the eraser, or back from it to
    /// the tool before.
    public mutating func toggleEraser() {
        if active == .eraser {
            select(previous == .eraser ? lastWritingTool : previous)
        } else {
            select(.eraser)
        }
    }

    /// An insert tool has placed its object (text when editing ends, an image
    /// on insert, a shape once drawn): fall back to the lasso. `previous` is
    /// left alone, so "switch to previous" still returns to the tool used
    /// before the insert.
    public mutating func finishInsert() {
        guard active.isInsert else { return }
        active = .lasso
    }

    // MARK: Slots

    /// Selects a stroke slot. Returns `true` when it was already selected,
    /// which is when the UI opens the width slider instead.
    @discardableResult
    public mutating func selectWidth(_ index: Int, for tool: PaperTool) -> Bool {
        guard var slot = slots[tool], slot.widths.indices.contains(index) else { return false }
        if slot.widthIndex == index { return true }
        slot.widthIndex = index
        slots[tool] = slot
        return false
    }

    /// Sets the width of a stroke slot (the selected one when `index` is nil),
    /// clamped to the tool's range. A non-finite width is ignored.
    public mutating func setWidth(_ width: Double, for tool: PaperTool, at index: Int? = nil) {
        guard width.isFinite, var slot = slots[tool] else { return }
        let target = index ?? slot.widthIndex
        guard slot.widths.indices.contains(target) else { return }
        let range = Self.widthRange(tool)
        slot.widths[target] = min(max(width, range.lowerBound), range.upperBound)
        slots[tool] = slot
    }

    /// Selects a colour slot. Returns `true` when it was already selected,
    /// which is when the UI opens the colour picker instead.
    @discardableResult
    public mutating func selectColor(_ index: Int, for tool: PaperTool) -> Bool {
        guard var slot = slots[tool], slot.colors.indices.contains(index) else { return false }
        if slot.colorIndex == index { return true }
        slot.colorIndex = index
        slots[tool] = slot
        return false
    }

    /// Replaces a colour slot (the selected one when `index` is nil). Returns
    /// `false` and changes nothing for an invalid colour.
    @discardableResult
    public mutating func setColor(_ hex: String, for tool: PaperTool, at index: Int? = nil) -> Bool {
        guard let color = Self.normalizedColor(hex), var slot = slots[tool] else { return false }
        let target = index ?? slot.colorIndex
        guard slot.colors.indices.contains(target) else { return false }
        slot.colors[target] = color
        slots[tool] = slot
        return true
    }

    /// Whether the trailing "+" is offered: fewer than twelve colours, and
    /// never on the eraser.
    public func canAddColor(for tool: PaperTool) -> Bool {
        guard tool != .eraser, let slot = slots[tool] else { return false }
        return slot.colors.count < ToolSlots.maximumColors
    }

    /// Whether "Remove" is offered: more than one colour remains.
    public func canRemoveColor(for tool: PaperTool) -> Bool {
        guard let slot = slots[tool] else { return false }
        return slot.colors.count > 1
    }

    /// The trailing "+": appends `hex`, or the current colour when it is nil,
    /// and selects the new slot.
    @discardableResult
    public mutating func addColor(_ hex: String? = nil, for tool: PaperTool) -> Bool {
        guard canAddColor(for: tool), var slot = slots[tool],
            let color = hex.map(Self.normalizedColor) ?? slot.color
        else { return false }
        slot.colors.append(color)
        slot.colorIndex = slot.colors.count - 1
        slots[tool] = slot
        return true
    }

    /// Removes a colour slot while more than one remains. The selection stays
    /// on the same colour, or moves to its neighbour when that one goes.
    @discardableResult
    public mutating func removeColor(at index: Int, for tool: PaperTool) -> Bool {
        guard canRemoveColor(for: tool), var slot = slots[tool],
            slot.colors.indices.contains(index)
        else { return false }
        slot.colors.remove(at: index)
        if index < slot.colorIndex {
            slot.colorIndex -= 1
        } else if slot.colorIndex >= slot.colors.count {
            slot.colorIndex = slot.colors.count - 1
        }
        slots[tool] = slot
        return true
    }

    /// Long-press and drag: moves a colour slot. The selection follows the
    /// colour it was on.
    public mutating func moveColor(from source: Int, to destination: Int, for tool: PaperTool) {
        guard var slot = slots[tool], slot.colors.indices.contains(source),
            slot.colors.indices.contains(destination), source != destination
        else { return }
        let selected = slot.colorIndex
        let color = slot.colors.remove(at: source)
        slot.colors.insert(color, at: destination)
        if selected == source {
            slot.colorIndex = destination
        } else if source < selected, destination >= selected {
            slot.colorIndex = selected - 1
        } else if source > selected, destination <= selected {
            slot.colorIndex = selected + 1
        }
        slots[tool] = slot
    }

    // MARK: Repair

    /// The same settings with every rule applied: a slot for each slot tool
    /// and none for the others; three finite widths clamped to the tool's
    /// range (fewer than three take the factory widths and selection); valid
    /// lowercase colours, at most twelve, none on the eraser and
    /// at least one elsewhere; indices in range (an out-of-range one resets to
    /// 0); and `lastWritingTool` a writing tool, equal to `active` when that
    /// is one.
    public func normalized() -> ToolSettings {
        var result = self
        result.version = Self.currentVersion
        var repaired: [PaperTool: ToolSlots] = [:]
        for tool in PaperTool.slotTools {
            guard let factory = Self.factorySlots(tool) else { continue }
            repaired[tool] = Self.repair(slots[tool], tool: tool, factory: factory)
        }
        result.slots = repaired
        if !result.lastWritingTool.isWriting { result.lastWritingTool = .pen }
        if result.active.isWriting { result.lastWritingTool = result.active }
        return result
    }

    private static func repair(_ stored: ToolSlots?, tool: PaperTool, factory: ToolSlots) -> ToolSlots {
        guard var slot = stored else { return factory }
        let range = widthRange(tool)
        if slot.widths.count >= ToolSlots.widthCount {
            slot.widths = (0..<ToolSlots.widthCount).map { index in
                let width = slot.widths[index]
                guard width.isFinite else { return factory.widths[index] }
                return min(max(width, range.lowerBound), range.upperBound)
            }
        } else {
            slot.widths = factory.widths
            slot.widthIndex = factory.widthIndex
        }
        if !slot.widths.indices.contains(slot.widthIndex) { slot.widthIndex = 0 }
        if tool == .eraser {
            slot.colors = []
            slot.colorIndex = 0
        } else {
            slot.colors = Array(slot.colors.compactMap(normalizedColor).prefix(ToolSlots.maximumColors))
            if slot.colors.isEmpty {
                slot.colors = factory.colors
                slot.colorIndex = factory.colorIndex
            }
            if !slot.colors.indices.contains(slot.colorIndex) { slot.colorIndex = 0 }
        }
        return slot
    }

    // MARK: Codable

    private enum CodingKeys: String, CodingKey {
        case version, active, previous, lastWritingTool, penStyle, eraserMode, shapeKind,
            shapeFill, slots, dock, menuVisible
    }

    /// Reads whatever is there and repairs the rest: an unknown or missing
    /// value takes the factory one, and the result is `normalized()`. Throws
    /// only when the JSON is not an object at all.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        func value<T: RawRepresentable>(_ key: CodingKeys, _ fallback: T) -> T
        where T.RawValue == String {
            (try? container.decode(String.self, forKey: key)).flatMap(T.init(rawValue:)) ?? fallback
        }
        let factory = Self.factory
        var slots: [PaperTool: ToolSlots] = [:]
        if let stored = try? container.decode([String: Lossy<ToolSlots>].self, forKey: .slots) {
            for (name, slot) in stored {
                if let tool = PaperTool(rawValue: name), let value = slot.value { slots[tool] = value }
            }
        }
        self.init(
            version: Self.currentVersion,
            active: value(.active, factory.active),
            previous: value(.previous, factory.previous),
            lastWritingTool: value(.lastWritingTool, factory.lastWritingTool),
            penStyle: value(.penStyle, factory.penStyle),
            eraserMode: value(.eraserMode, factory.eraserMode),
            shapeKind: value(.shapeKind, factory.shapeKind),
            shapeFill: (try? container.decode(Bool.self, forKey: .shapeFill)) ?? factory.shapeFill,
            slots: slots,
            dock: value(.dock, factory.dock),
            menuVisible: (try? container.decode(Bool.self, forKey: .menuVisible)) ?? factory.menuVisible)
        self = normalized()
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(version, forKey: .version)
        try container.encode(active, forKey: .active)
        try container.encode(previous, forKey: .previous)
        try container.encode(lastWritingTool, forKey: .lastWritingTool)
        try container.encode(penStyle, forKey: .penStyle)
        try container.encode(eraserMode, forKey: .eraserMode)
        try container.encode(shapeKind, forKey: .shapeKind)
        try container.encode(shapeFill, forKey: .shapeFill)
        let keyed = Dictionary(uniqueKeysWithValues: slots.map { ($0.key.rawValue, $0.value) })
        try container.encode(keyed, forKey: .slots)
        try container.encode(dock, forKey: .dock)
        try container.encode(menuVisible, forKey: .menuVisible)
    }
}

/// Decodes one element without failing its container.
private struct Lossy<Value: Decodable>: Decodable {
    let value: Value?

    init(from decoder: Decoder) throws {
        value = try? decoder.singleValueContainer().decode(Value.self)
    }
}

// MARK: - Persistence

/// Reads and writes `ToolSettings` in `UserDefaults`, and migrates the keys
/// earlier builds wrote.
public enum ToolSettingsStore {
    /// JSON of `ToolSettings`, as a UTF-8 string.
    public static let key = "paper.tools.v2"
    /// Mirrors `active` on every save. A value here, or a launch argument such
    /// as `-paper.tool lasso`, wins on load.
    public static let activeKey = "paper.tool"

    /// The keys earlier builds wrote besides `paper.tool`. Migration reads
    /// them once and then deletes them.
    enum LegacyKey {
        static let penWidth = "paper.tool.penWidth"
        static let penColor = "paper.tool.penColor"
        static let highlighterWidth = "paper.tool.highlighterWidth"
        static let highlighterColor = "paper.tool.highlighterColor"
        static let eraserWidth = "paper.tool.eraserWidth"
        static let eraserBitmap = "paper.tool.eraserBitmap"
        static let capsuleEdge = "paper.tool.capsuleEdge"
        static let capsuleOffset = "paper.tool.capsuleOffset"

        static let all = [
            penWidth, penColor, highlighterWidth, highlighterColor, eraserWidth, eraserBitmap,
            capsuleEdge, capsuleOffset,
        ]
    }

    /// The highlighter palette `paper.tool.highlighterColor` indexed.
    static let legacyHighlighterColors = ["#ffe066", "#a8e6a1", "#9ecbff", "#ffb3c7"]

    /// What the earlier build used when a key was missing or unreadable.
    enum LegacyDefault {
        static let penWidth = 3.0
        static let highlighterWidth = 20.0
        static let eraserWidth = 24.0
    }

    /// The settings to start with. Reads `paper.tools.v2`; when it is absent
    /// or unreadable, migrates the earlier keys (factory settings when there
    /// are none), saves the result and deletes those keys. Either way a valid
    /// tool name under `paper.tool` then becomes the active tool. Safe to call
    /// any number of times.
    public static func load(_ defaults: UserDefaults) -> ToolSettings {
        var settings: ToolSettings
        if let stored = decode(defaults.object(forKey: key)) {
            settings = stored
        } else {
            settings = migrate(defaults)
            save(settings, defaults)
        }
        for legacy in LegacyKey.all { defaults.removeObject(forKey: legacy) }
        if let name = defaults.string(forKey: activeKey), let tool = PaperTool(rawValue: name) {
            settings.select(tool)
        }
        return settings
    }

    /// Writes the settings and mirrors `active` under `paper.tool`.
    public static func save(_ settings: ToolSettings, _ defaults: UserDefaults) {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(settings.normalized()),
            let json = String(data: data, encoding: .utf8)
        else { return }
        defaults.set(json, forKey: key)
        defaults.set(settings.active.rawValue, forKey: activeKey)
    }

    static func decode(_ stored: Any?) -> ToolSettings? {
        let data: Data?
        switch stored {
        case let string as String: data = string.data(using: .utf8)
        case let raw as Data: data = raw
        default: data = nil
        }
        guard let data else { return nil }
        return try? JSONDecoder().decode(ToolSettings.self, from: data)
    }

    // MARK: Migration

    /// The settings the earlier keys describe. With none of them present this
    /// is the factory settings. With any present, the install is an earlier
    /// one, and every value is read the way that build read it: a missing or
    /// unreadable key means the value it fell back to (pen 3 pt, highlighter
    /// 20 pt, eraser 24 pt, the first colour, the pixel eraser), so the pen a
    /// person wrote with yesterday is the pen they get today. This build also
    /// writes `paper.tool`, so beside an unreadable blob that key alone is not
    /// an earlier install.
    static func migrate(_ defaults: UserDefaults) -> ToolSettings {
        var keys = LegacyKey.all
        if defaults.object(forKey: key) == nil { keys.append(activeKey) }
        guard keys.contains(where: { defaults.object(forKey: $0) != nil }) else {
            return ToolSettings.factory
        }
        var settings = ToolSettings.factory

        let active = defaults.string(forKey: activeKey).flatMap(PaperTool.init(rawValue:)) ?? .pen
        settings.active = active
        settings.previous = .pen
        settings.lastWritingTool = active == .highlighter ? .highlighter : .pen

        settings.penStyle = .brush
        settings.slots[.pen] = migratedSlots(
            .pen,
            width: number(defaults.object(forKey: LegacyKey.penWidth)) ?? LegacyDefault.penWidth,
            colorIndex: index(defaults.object(forKey: LegacyKey.penColor)),
            palette: ToolSettings.swatches.map(\.hex))
        settings.slots[.highlighter] = migratedSlots(
            .highlighter,
            width: number(defaults.object(forKey: LegacyKey.highlighterWidth))
                ?? LegacyDefault.highlighterWidth,
            colorIndex: index(defaults.object(forKey: LegacyKey.highlighterColor)),
            palette: legacyHighlighterColors)
        settings.slots[.eraser] = migratedSlots(
            .eraser,
            width: number(defaults.object(forKey: LegacyKey.eraserWidth)) ?? LegacyDefault.eraserWidth,
            colorIndex: nil, palette: [])
        let bitmap = flag(defaults.object(forKey: LegacyKey.eraserBitmap)) ?? true
        settings.eraserMode = bitmap ? .partial : .stroke
        return settings.normalized()
    }

    /// Widths `[max(low, w/2), w, min(high, w*1.75)]` with `w` clamped into the
    /// tool's range, the middle one selected; the palette's colour at the old
    /// index goes in front of the factory colours, de-duplicated, selected.
    static func migratedSlots(
        _ tool: PaperTool, width: Double, colorIndex: Int?, palette: [String]
    ) -> ToolSlots? {
        guard var slot = ToolSettings.factorySlots(tool) else { return nil }
        let range = ToolSettings.widthRange(tool)
        let w = min(max(width, range.lowerBound), range.upperBound)
        slot.widths = [max(range.lowerBound, w / 2), w, min(range.upperBound, w * 1.75)]
        slot.widthIndex = 1
        if !palette.isEmpty {
            // The earlier build clamped a too-large index to the last colour.
            let chosen = palette[min(max(colorIndex ?? 0, 0), palette.count - 1)]
            var colors = [chosen]
            for color in slot.colors where !colors.contains(color) { colors.append(color) }
            slot.colors = colors
            slot.colorIndex = 0
        }
        return slot
    }

    /// A finite number stored as a number or as text (the argument domain
    /// holds text). Booleans and anything else are not numbers.
    static func number(_ value: Any?) -> Double? {
        let result: Double?
        switch value {
        case let number as NSNumber:
            result = isBoolean(number) ? nil : number.doubleValue
        case let string as String:
            result = Double(string.trimmingCharacters(in: .whitespaces))
        default:
            result = nil
        }
        guard let result, result.isFinite else { return nil }
        return result
    }

    /// A whole number, or `nil` when the value is not one. A negative index
    /// never existed (the earlier build would have crashed on it) and reads as
    /// unreadable.
    static func index(_ value: Any?) -> Int? {
        guard let number = number(value), number >= 0, number == number.rounded(),
            number <= Double(Int32.max)
        else { return nil }
        return Int(number)
    }

    static func flag(_ value: Any?) -> Bool? {
        switch value {
        case let number as NSNumber:
            if isBoolean(number) { return number.boolValue }
            let double = number.doubleValue
            return double == 0 ? false : double == 1 ? true : nil
        case let string as String:
            switch string.trimmingCharacters(in: .whitespaces).lowercased() {
            case "yes", "true", "1": return true
            case "no", "false", "0": return false
            default: return nil
            }
        default:
            return nil
        }
    }

    private static func isBoolean(_ number: NSNumber) -> Bool {
        CFGetTypeID(number) == CFBooleanGetTypeID()
    }
}
