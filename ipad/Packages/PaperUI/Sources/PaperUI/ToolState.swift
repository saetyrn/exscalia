import Observation
import PaperCore
import PencilKit
import UIKit

/// A colour with a name, because "the blue one" is not something a VoiceOver
/// user can see and colour alone is never allowed to carry meaning. The lasso's
/// Colour menu offers these.
public struct NamedInkColor: Equatable, Sendable {
    public let name: String
    public let red: CGFloat
    public let green: CGFloat
    public let blue: CGFloat

    var uiColor: UIColor { UIColor(red: red, green: green, blue: blue, alpha: 1) }
}

/// The window's tools: PaperCore's `ToolSettings`, observable, saved on every
/// change through `ToolSettingsStore`, and translated into PencilKit here.
///
/// The toolbar and the tool menu read `settings` and call the methods below;
/// the notebook controller listens with `addObserver` and applies the result
/// to its canvases. Nothing else writes the tool keys.
@MainActor
@Observable
public final class ToolState {
    public private(set) var settings: ToolSettings

    private let defaults: UserDefaults
    @ObservationIgnored private var observers: [UUID: (ToolSettings, ToolSettings) -> Void] = [:]

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        var loaded = ToolSettingsStore.load(defaults)
        #if DEBUG
            // `-debugToolDock leading` puts the tool menu on a side for a
            // screenshot without dragging it there.
            let arguments = ProcessInfo.processInfo.arguments
            if let index = arguments.firstIndex(of: "-debugToolDock"),
                arguments.indices.contains(index + 1),
                let dock = ToolMenuDock(rawValue: arguments[index + 1])
            {
                loaded.dock = dock
                loaded.menuVisible = true
            }
        #endif
        settings = loaded
    }

    // MARK: Reading

    public var active: PaperTool { settings.active }

    /// The tool Pencil double-tap "switch to previous" returns to.
    public var previousTool: PaperTool { settings.previous }

    /// What the live canvases are given for the active tool.
    public var pkTool: PKTool { Self.pkTool(for: settings.canvasTool) }

    /// Radius of the Pencil hover circle, in page points.
    public var hoverRadius: CGFloat { CGFloat(settings.hoverRadius) }

    /// The eight named swatches, which the lasso's Colour menu also offers.
    public static let penColors: [NamedInkColor] = ToolSettings.swatches.compactMap { swatch in
        ToolSettings.rgb(swatch.hex).map {
            NamedInkColor(
                name: swatch.name, red: CGFloat($0.red), green: CGFloat($0.green),
                blue: CGFloat($0.blue))
        }
    }

    public static func pkTool(for tool: CanvasTool) -> PKTool {
        switch tool {
        case .ink(let kind, let color, let width):
            return PKInkingTool(
                PKInk.InkType(rawValue: kind.rawValue) ?? .pen, color: uiColor(color),
                width: CGFloat(width))
        case .eraser(.partial, let width):
            return PKEraserTool(.fixedWidthBitmap, width: CGFloat(width))
        case .eraser(.stroke, _):
            return PKEraserTool(.vector)
        }
    }

    /// `#rrggbb` as an opaque colour; black for anything that is not one.
    public static func uiColor(_ hex: String) -> UIColor {
        guard let rgb = ToolSettings.rgb(hex) else { return .black }
        return UIColor(
            red: CGFloat(rgb.red), green: CGFloat(rgb.green), blue: CGFloat(rgb.blue), alpha: 1)
    }

    /// How a stored ink colour shows: as itself on light paper, and as
    /// PencilKit maps it on dark paper, where black ink draws light.
    public static func displayColor(_ hex: String, onDarkPaper dark: Bool) -> UIColor {
        let color = uiColor(hex)
        return dark ? PKInkingTool.convertColor(color, from: .light, to: .dark) : color
    }

    /// `#rrggbb` for a colour from the system picker, flattened to sRGB.
    public static func hex(_ color: UIColor) -> String? {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        let srgb =
            color.cgColor.converted(
                to: CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB(),
                intent: .defaultIntent, options: nil
            ).map(UIColor.init(cgColor:)) ?? color
        guard srgb.getRed(&red, green: &green, blue: &blue, alpha: &alpha) else { return nil }
        func byte(_ value: CGFloat) -> Int { Int((min(max(value, 0), 1) * 255).rounded()) }
        return String(format: "#%02x%02x%02x", byte(red), byte(green), byte(blue))
    }

    // MARK: Observing

    /// Called after every change with the settings before and after it, on
    /// the main actor. Returns the token `removeObserver` takes.
    @discardableResult
    public func addObserver(_ observer: @escaping (ToolSettings, ToolSettings) -> Void) -> UUID {
        let id = UUID()
        observers[id] = observer
        return id
    }

    public func removeObserver(_ id: UUID) {
        observers[id] = nil
    }

    /// The one way settings change: applied, saved, then announced. A change
    /// that changes nothing is neither saved nor announced.
    @discardableResult
    public func update<Value>(_ change: (inout ToolSettings) -> Value) -> Value {
        let old = settings
        var next = settings
        let result = change(&next)
        guard next != old else { return result }
        settings = next
        ToolSettingsStore.save(next, defaults)
        for observer in observers.values { observer(old, next) }
        return result
    }

    // MARK: Choosing tools

    /// A toolbar button: selects its tool, or shows and hides the tool menu
    /// when it is already selected.
    public func tap(_ slot: ToolbarSlot) { update { $0.tap(slot) } }

    public func select(_ tool: PaperTool) { update { $0.select(tool) } }

    public func selectPenStyle(_ style: PenStyle) { update { $0.selectPenStyle(style) } }

    public func switchToPrevious() { update { $0.switchToPrevious() } }

    public func toggleEraser() { update { $0.toggleEraser() } }

    /// An insert tool has placed its object: back to the lasso.
    public func finishInsert() { update { $0.finishInsert() } }

    public func setMenuVisible(_ visible: Bool) { update { $0.menuVisible = visible } }

    public func setDock(_ dock: ToolMenuDock) { update { $0.dock = dock } }

    public func setEraserMode(_ mode: EraserMode) { update { $0.eraserMode = mode } }

    public func setShapeKind(_ kind: ShapeKind) { update { $0.shapeKind = kind } }

    public func setShapeFill(_ fill: Bool) { update { $0.shapeFill = fill } }

    // MARK: Slots

    /// Selects a stroke slot; `true` when it was already selected and the
    /// width slider should open instead.
    @discardableResult
    public func selectWidth(_ index: Int, for tool: PaperTool) -> Bool {
        update { $0.selectWidth(index, for: tool) }
    }

    public func setWidth(_ width: Double, for tool: PaperTool, at index: Int? = nil) {
        update { $0.setWidth(width, for: tool, at: index) }
    }

    /// Selects a colour slot; `true` when it was already selected and the
    /// colour picker should open instead.
    @discardableResult
    public func selectColor(_ index: Int, for tool: PaperTool) -> Bool {
        update { $0.selectColor(index, for: tool) }
    }

    @discardableResult
    public func setColor(_ hex: String, for tool: PaperTool, at index: Int? = nil) -> Bool {
        update { $0.setColor(hex, for: tool, at: index) }
    }

    @discardableResult
    public func addColor(_ hex: String? = nil, for tool: PaperTool) -> Bool {
        update { $0.addColor(hex, for: tool) }
    }

    @discardableResult
    public func removeColor(at index: Int, for tool: PaperTool) -> Bool {
        update { $0.removeColor(at: index, for: tool) }
    }

    public func moveColor(from source: Int, to destination: Int, for tool: PaperTool) {
        update { $0.moveColor(from: source, to: destination, for: tool) }
    }
}
