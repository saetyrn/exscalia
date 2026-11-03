import Foundation
import PencilKit
import XCTest

@testable import PaperCore

/// The tool model is what the toolbar and the tool menu read: these pin the
/// contract's defaults and the rules for choosing tools and editing slots.
final class ToolModelTests: XCTestCase {
    func testFactorySettingsMatchTheContract() {
        let factory = ToolSettings.factory
        XCTAssertEqual(factory.version, 2)
        XCTAssertEqual(factory.active, .pen)
        XCTAssertEqual(factory.previous, .pen)
        XCTAssertEqual(factory.lastWritingTool, .pen)
        XCTAssertEqual(factory.penStyle, .fountain)
        XCTAssertEqual(factory.eraserMode, .partial)
        XCTAssertEqual(factory.shapeKind, .rectangle)
        XCTAssertFalse(factory.shapeFill)
        XCTAssertEqual(factory.dock, .top)
        XCTAssertTrue(factory.menuVisible)
        XCTAssertEqual(Set(factory.slots.keys), Set(PaperTool.slotTools))
        XCTAssertNil(factory.slots[.lasso])
        XCTAssertNil(factory.slots[.image])

        let penColors = ["#1c1c1e", "#1f6feb", "#c0392b"]
        for tool in [PaperTool.pen, .drawShape, .shape] {
            XCTAssertEqual(factory.slots[tool], ToolSlots(widths: [2.5, 5, 8.5], widthIndex: 1, colors: penColors, colorIndex: 0), "\(tool)")
        }
        XCTAssertEqual(
            factory.slots[.pencil],
            ToolSlots(widths: [4, 6, 10], widthIndex: 1, colors: ["#3a3a3c", "#1c1c1e", "#1f6feb"], colorIndex: 0))
        XCTAssertEqual(
            factory.slots[.highlighter],
            ToolSlots(widths: [14, 22, 32], widthIndex: 1, colors: ["#ffe066", "#a8e6a1", "#ffb3c7"], colorIndex: 0))
        XCTAssertEqual(factory.slots[.eraser], ToolSlots(widths: [8, 16, 32], widthIndex: 1, colors: [], colorIndex: 0))
        XCTAssertEqual(factory.slots[.text], ToolSlots(widths: [13, 17, 24], widthIndex: 1, colors: penColors, colorIndex: 0))
        XCTAssertEqual(factory, factory.normalized())
    }

    func testWidthRanges() {
        for tool in [PaperTool.pen, .pencil, .drawShape, .shape] {
            XCTAssertEqual(ToolSettings.widthRange(tool), 0.5...12)
        }
        XCTAssertEqual(ToolSettings.widthRange(.highlighter), 6...40)
        XCTAssertEqual(ToolSettings.widthRange(.eraser), 4...60)
        XCTAssertEqual(ToolSettings.widthRange(.text), 9...72)
        for tool in PaperTool.slotTools {
            let slots = try? XCTUnwrap(ToolSettings.factorySlots(tool))
            for width in slots?.widths ?? [] { XCTAssertTrue(ToolSettings.widthRange(tool).contains(width)) }
        }
    }

    func testToolsAndTheToolbar() {
        XCTAssertEqual(ToolbarSlot.allCases, [.lasso, .writing, .eraser, .text, .image, .shape])
        XCTAssertEqual(PaperTool.writingTools.filter(\.isWriting), PaperTool.writingTools)
        XCTAssertEqual(PaperTool.allCases.filter(\.isWriting), [.pen, .pencil, .highlighter, .drawShape])
        XCTAssertEqual(PaperTool.allCases.filter(\.isInsert), [.text, .image, .shape])
        XCTAssertFalse(PaperTool.lasso.hasToolMenu)
        XCTAssertFalse(PaperTool.image.hasToolMenu)
        // The names earlier builds wrote, and UI tests pass, are unchanged.
        XCTAssertEqual(PaperTool(rawValue: "lasso"), .lasso)
        XCTAssertEqual(PaperTool(rawValue: "pen"), .pen)
        XCTAssertEqual(PaperTool(rawValue: "highlighter"), .highlighter)
        XCTAssertEqual(PaperTool(rawValue: "eraser"), .eraser)
        for tool in PaperTool.allCases {
            XCTAssertFalse(tool.label.isEmpty)
            XCTAssertFalse(tool.symbolName.isEmpty)
        }
        XCTAssertEqual(PaperTool.drawShape.symbolName, "scribble.variable")
        XCTAssertEqual(PaperTool.shape.label, "Shapes")
    }

    func testTappingAToolSelectsItAndTappingAgainTogglesTheMenu() {
        var settings = ToolSettings.factory
        settings.tap(.eraser)
        XCTAssertEqual(settings.active, .eraser)
        XCTAssertEqual(settings.previous, .pen)
        XCTAssertTrue(settings.isSelected(.eraser))
        XCTAssertTrue(settings.showsToolMenu)
        settings.tap(.eraser)
        XCTAssertEqual(settings.active, .eraser)
        XCTAssertFalse(settings.menuVisible)
        XCTAssertFalse(settings.showsToolMenu)
        settings.tap(.eraser)
        XCTAssertTrue(settings.menuVisible)

        // The lasso has no menu: a second tap does nothing.
        settings.tap(.lasso)
        XCTAssertFalse(settings.showsToolMenu)
        settings.tap(.lasso)
        XCTAssertEqual(settings.active, .lasso)
        XCTAssertTrue(settings.menuVisible)
    }

    func testTheWritingButtonRemembersTheLastWritingTool() {
        var settings = ToolSettings.factory
        settings.select(.highlighter)
        XCTAssertEqual(settings.lastWritingTool, .highlighter)
        settings.tap(.lasso)
        XCTAssertEqual(settings.tool(for: .writing), .highlighter)
        XCTAssertFalse(settings.isSelected(.writing))
        settings.tap(.writing)
        XCTAssertEqual(settings.active, .highlighter)
        XCTAssertTrue(settings.isSelected(.writing))
        settings.select(.drawShape)
        XCTAssertEqual(settings.tool(for: .writing), .drawShape)
        settings.selectPenStyle(.ball)
        XCTAssertEqual(settings.active, .pen)
        XCTAssertEqual(settings.penStyle, .ball)
    }

    func testInsertToolsFallBackToTheLasso() {
        var settings = ToolSettings.factory
        settings.select(.text)
        settings.finishInsert()
        XCTAssertEqual(settings.active, .lasso)
        // "Switch to previous" still goes back to the pen.
        XCTAssertEqual(settings.previous, .pen)
        settings.switchToPrevious()
        XCTAssertEqual(settings.active, .pen)

        for tool in [PaperTool.image, .shape] {
            settings.select(tool)
            settings.finishInsert()
            XCTAssertEqual(settings.active, .lasso)
        }
        settings.select(.pencil)
        settings.finishInsert()
        XCTAssertEqual(settings.active, .pencil, "a writing tool is sticky")
    }

    func testPencilDoubleTapActions() {
        var settings = ToolSettings.factory
        settings.select(.pencil)
        settings.switchToPrevious()
        XCTAssertEqual(settings.active, .pen)
        settings.switchToPrevious()
        XCTAssertEqual(settings.active, .pencil)

        settings.toggleEraser()
        XCTAssertEqual(settings.active, .eraser)
        settings.toggleEraser()
        XCTAssertEqual(settings.active, .pencil)

        // An eraser whose previous tool is itself returns to writing.
        settings.active = .eraser
        settings.previous = .eraser
        settings.toggleEraser()
        XCTAssertEqual(settings.active, .pencil)
    }

    func testTheInkMappingIsData() {
        var settings = ToolSettings.factory
        XCTAssertEqual(settings.ink(for: .pen), .fountainPen)
        settings.penStyle = .ball
        XCTAssertEqual(settings.ink(for: .pen), .monoline)
        settings.penStyle = .brush
        XCTAssertEqual(settings.ink(for: .pen), .pen)
        XCTAssertEqual(settings.ink(for: .pencil), .pencil)
        XCTAssertEqual(settings.ink(for: .highlighter), .marker)
        XCTAssertEqual(settings.ink(for: .drawShape), .monoline)
        for tool in [PaperTool.lasso, .eraser, .text, .image, .shape] { XCTAssertNil(settings.ink(for: tool)) }

        settings.select(.highlighter)
        XCTAssertEqual(settings.canvasTool, .ink(.marker, color: "#ffe066", width: 22))
        // Lasso and the insert tools leave the last writing tool's ink in place.
        for tool in [PaperTool.lasso, .text, .image, .shape] {
            settings.select(tool)
            XCTAssertEqual(settings.canvasTool, .ink(.marker, color: "#ffe066", width: 22), "\(tool)")
        }
        settings.select(.eraser)
        XCTAssertEqual(settings.canvasTool, .eraser(.partial, width: 16))
        settings.eraserMode = .stroke
        XCTAssertEqual(settings.canvasTool, .eraser(.stroke, width: 16))
    }

    /// The raw values are PencilKit's own, so the UI layer can map them with
    /// `PKInk.InkType(rawValue:)` and no switch of its own.
    func testInkKindsArePencilKitsInkTypes() {
        let expected: [InkKind: PKInk.InkType] = [
            .fountainPen: .fountainPen, .monoline: .monoline, .pen: .pen, .pencil: .pencil, .marker: .marker,
        ]
        for kind in InkKind.allCases {
            XCTAssertEqual(PKInk.InkType(rawValue: kind.rawValue), expected[kind], kind.rawValue)
        }
    }

    func testHoverRadius() {
        var settings = ToolSettings.factory
        XCTAssertEqual(settings.hoverRadius, 3.5, accuracy: 1e-9)  // max(5, 2) / 2 + 1
        settings.select(.highlighter)
        XCTAssertEqual(settings.hoverRadius, 11, accuracy: 1e-9)
        settings.select(.eraser)
        XCTAssertEqual(settings.hoverRadius, 8, accuracy: 1e-9)
        settings.select(.lasso)
        XCTAssertEqual(settings.hoverRadius, 6, accuracy: 1e-9)
    }

    // MARK: Slots

    func testWidthSlots() {
        var settings = ToolSettings.factory
        XCTAssertFalse(settings.selectWidth(2, for: .pen))
        XCTAssertEqual(settings.width(for: .pen), 8.5)
        XCTAssertTrue(settings.selectWidth(2, for: .pen), "tapping the selected slot opens the slider")
        settings.setWidth(7.25, for: .pen)
        XCTAssertEqual(settings.slots[.pen]?.widths, [2.5, 5, 7.25])
        settings.setWidth(900, for: .pen, at: 0)
        XCTAssertEqual(settings.slots[.pen]?.widths[0], 12)
        settings.setWidth(-4, for: .eraser)
        XCTAssertEqual(settings.width(for: .eraser), 4)
        settings.setWidth(.nan, for: .pen)
        settings.setWidth(.infinity, for: .pen)
        XCTAssertEqual(settings.width(for: .pen), 7.25)
        XCTAssertFalse(settings.selectWidth(3, for: .pen))
        XCTAssertEqual(settings.slots[.pen]?.widthIndex, 2)
        settings.setWidth(5, for: .lasso)
        XCTAssertNil(settings.width(for: .lasso))
    }

    func testColourSlotsGrowFromThreeToTwelve() {
        var settings = ToolSettings.factory
        XCTAssertEqual(settings.slots[.pen]?.colors.count, 3)
        XCTAssertTrue(settings.addColor(for: .pen), "the + adds the current colour")
        XCTAssertEqual(settings.slots[.pen]?.colors.last, "#1c1c1e")
        XCTAssertEqual(settings.slots[.pen]?.colorIndex, 3)
        while settings.canAddColor(for: .pen) { XCTAssertTrue(settings.addColor("#ABCDEF", for: .pen)) }
        XCTAssertEqual(settings.slots[.pen]?.colors.count, 12)
        XCTAssertEqual(settings.color(for: .pen), "#abcdef")
        XCTAssertFalse(settings.addColor("#000000", for: .pen))
        XCTAssertEqual(settings.slots[.pen]?.colors.count, 12)
        XCTAssertFalse(settings.addColor("red", for: .pencil), "an invalid colour is not added")
        XCTAssertEqual(settings.slots[.pencil]?.colors.count, 3)
        XCTAssertFalse(settings.canAddColor(for: .eraser))
        XCTAssertFalse(settings.addColor("#000000", for: .eraser))
        XCTAssertFalse(settings.canAddColor(for: .lasso))
        // Every tool keeps its own colours.
        XCTAssertEqual(settings.slots[.drawShape]?.colors.count, 3)
    }

    func testSelectingAndReplacingColours() {
        var settings = ToolSettings.factory
        XCTAssertFalse(settings.selectColor(1, for: .pen))
        XCTAssertEqual(settings.color(for: .pen), "#1f6feb")
        XCTAssertTrue(settings.selectColor(1, for: .pen), "tapping the selected colour opens the picker")
        XCTAssertTrue(settings.setColor(" #6C5CE7 ", for: .pen))
        XCTAssertEqual(settings.color(for: .pen), "#6c5ce7")
        XCTAssertFalse(settings.setColor("#12345", for: .pen))
        XCTAssertFalse(settings.setColor("#gggggg", for: .pen))
        XCTAssertFalse(settings.setColor("#00ff00", for: .pen, at: 9))
        XCTAssertFalse(settings.selectColor(5, for: .pen))
        XCTAssertEqual(settings.color(for: .pen), "#6c5ce7")
        XCTAssertNil(settings.color(for: .eraser))
    }

    func testRemovingColoursKeepsOneAndTheSelection() {
        var settings = ToolSettings.factory
        settings.selectColor(2, for: .pen)  // red
        XCTAssertTrue(settings.removeColor(at: 0, for: .pen))
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1f6feb", "#c0392b"])
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        XCTAssertTrue(settings.removeColor(at: 1, for: .pen), "removing the selected colour")
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1f6feb"])
        XCTAssertEqual(settings.slots[.pen]?.colorIndex, 0)
        XCTAssertFalse(settings.canRemoveColor(for: .pen))
        XCTAssertFalse(settings.removeColor(at: 0, for: .pen))
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1f6feb"])
    }

    func testReorderingColoursKeepsTheSelectionOnItsColour() {
        var settings = ToolSettings.factory
        settings.addColor("#111111", for: .pen)
        settings.addColor("#222222", for: .pen)  // [black, blue, red, 111, 222], 222 selected
        settings.selectColor(2, for: .pen)  // red
        settings.moveColor(from: 2, to: 4, for: .pen)
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1c1c1e", "#1f6feb", "#111111", "#222222", "#c0392b"])
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        settings.moveColor(from: 0, to: 4, for: .pen)
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        settings.moveColor(from: 4, to: 0, for: .pen)
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1c1c1e", "#1f6feb", "#111111", "#222222", "#c0392b"])
        XCTAssertEqual(settings.slots[.pen]?.colorIndex, 4)
        settings.moveColor(from: 3, to: 1, for: .pen)
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        settings.moveColor(from: 0, to: 9, for: .pen)
        XCTAssertEqual(settings.slots[.pen]?.colors.count, 5)
    }

    func testColourHelpers() {
        XCTAssertEqual(ToolSettings.normalizedColor("#ABCDEF"), "#abcdef")
        XCTAssertNil(ToolSettings.normalizedColor("abcdef"))
        XCTAssertNil(ToolSettings.normalizedColor("#abc"))
        XCTAssertNil(ToolSettings.normalizedColor("#abcdefff"))
        XCTAssertNil(ToolSettings.normalizedColor("#ab cde"))
        XCTAssertNil(ToolSettings.normalizedColor("#ａｂｃｄｅｆ"))
        let red = try? XCTUnwrap(ToolSettings.rgb("#ff8000"))
        XCTAssertEqual(red?.red ?? 0, 1, accuracy: 1e-9)
        XCTAssertEqual(red?.green ?? 0, 128.0 / 255, accuracy: 1e-9)
        XCTAssertEqual(red?.blue ?? 1, 0, accuracy: 1e-9)
        XCTAssertNil(ToolSettings.rgb("nope"))
        XCTAssertEqual(ToolSettings.swatches.map(\.name), ["Black", "Graphite", "Blue", "Indigo", "Teal", "Green", "Amber", "Red"])
        XCTAssertEqual(ToolSettings.swatchName("#C0392B"), "Red")
        XCTAssertNil(ToolSettings.swatchName("#123456"))
    }

    /// The highlighter's picker offers highlighter colours, not the pen's ink.
    func testTheHighlighterHasItsOwnSwatches() throws {
        let highlighter = ToolSettings.swatches(for: .highlighter)
        XCTAssertEqual(highlighter.map(\.name), ["Yellow", "Green", "Pink", "Blue", "Orange", "Purple"])
        for swatch in highlighter {
            XCTAssertEqual(ToolSettings.normalizedColor(swatch.hex), swatch.hex)
            XCTAssertEqual(ToolSettings.swatchName(swatch.hex.uppercased()), swatch.name)
            // Light: a highlighter over black ink must leave the ink legible.
            let rgb = try XCTUnwrap(ToolSettings.rgb(swatch.hex))
            XCTAssertGreaterThan(
                0.2126 * rgb.red + 0.7152 * rgb.green + 0.0722 * rgb.blue, 0.6, swatch.name)
        }
        let hexes = Set(highlighter.map(\.hex))
        XCTAssertTrue(Set(ToolSettings.factory.slots[.highlighter]?.colors ?? []).isSubset(of: hexes))
        XCTAssertTrue(Set(ToolSettingsStore.legacyHighlighterColors).isSubset(of: hexes))
        XCTAssertTrue(hexes.isDisjoint(with: ToolSettings.swatches.map(\.hex)))
        for tool in PaperTool.allCases where tool != .highlighter {
            XCTAssertEqual(
                ToolSettings.swatches(for: tool).map(\.hex), ToolSettings.swatches.map(\.hex), "\(tool)")
        }
    }

    /// A highlighter colour picked from the pen's swatches before it had its
    /// own stays as it was.
    func testAStoredHighlighterColourIsKept() throws {
        var settings = ToolSettings.factory
        XCTAssertTrue(settings.setColor("#1f6feb", for: .highlighter, at: 1))
        let data = try JSONEncoder().encode(settings)
        let decoded = try JSONDecoder().decode(ToolSettings.self, from: data)
        XCTAssertEqual(decoded.slots[.highlighter]?.colors, ["#ffe066", "#1f6feb", "#ffb3c7"])
        XCTAssertEqual(decoded, settings)
    }

    // MARK: JSON

    func testRoundTripAndShape() throws {
        var settings = ToolSettings.factory
        settings.select(.pencil)
        settings.penStyle = .brush
        settings.dock = .leading
        settings.menuVisible = false
        settings.shapeKind = .arrow
        settings.shapeFill = true
        settings.addColor("#0f8b8d", for: .pencil)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let data = try encoder.encode(settings)
        XCTAssertEqual(try JSONDecoder().decode(ToolSettings.self, from: data), settings)

        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(
            Set(object.keys),
            ["version", "active", "previous", "lastWritingTool", "penStyle", "eraserMode", "shapeKind",
             "shapeFill", "slots", "dock", "menuVisible"])
        XCTAssertEqual(object["version"] as? Int, 2)
        XCTAssertEqual(object["active"] as? String, "pencil")
        XCTAssertEqual(object["dock"] as? String, "leading")
        let slots = try XCTUnwrap(object["slots"] as? [String: Any])
        XCTAssertEqual(Set(slots.keys), ["pen", "pencil", "highlighter", "drawShape", "eraser", "text", "shape"])
        let pencil = try XCTUnwrap(slots["pencil"] as? [String: Any])
        XCTAssertEqual(pencil["colors"] as? [String], ["#3a3a3c", "#1c1c1e", "#1f6feb", "#0f8b8d"])
        XCTAssertEqual(pencil["colorIndex"] as? Int, 3)
        XCTAssertEqual(pencil["widths"] as? [Double], [4, 6, 10])
        XCTAssertEqual(pencil["widthIndex"] as? Int, 1)
    }

    func testDecodingRepairsWhatItCannotUse() throws {
        let json = """
            {"version": 7, "active": "laser", "previous": "eraser", "lastWritingTool": "lasso",
             "penStyle": "quill", "eraserMode": "partial", "shapeKind": "star", "shapeFill": "yes",
             "dock": "middle", "menuVisible": false, "futureField": [1, 2],
             "slots": {
               "pen": {"widths": [0.1, "thick", 99, 4], "widthIndex": 7,
                       "colors": ["#FFFFFF", "blue", 3, "#00ff00", "#000000", "#111111", "#222222",
                                  "#333333", "#444444", "#555555", "#666666", "#777777", "#888888",
                                  "#999999"],
                       "colorIndex": 40},
               "pencil": {"widths": [2, 3], "colors": []},
               "eraser": {"widths": [8, 16, 32], "colors": ["#ff0000"], "colorIndex": 2},
               "lasso": {"widths": [1, 2, 3], "colors": ["#000000"]},
               "highlighter": "broken",
               "sticker": {"widths": [1, 2, 3]}
             }}
            """
        let settings = try JSONDecoder().decode(ToolSettings.self, from: Data(json.utf8))
        XCTAssertEqual(settings.version, 2)
        XCTAssertEqual(settings.active, .pen)
        XCTAssertEqual(settings.previous, .eraser)
        XCTAssertEqual(settings.lastWritingTool, .pen)
        XCTAssertEqual(settings.penStyle, .fountain)
        XCTAssertEqual(settings.shapeKind, .rectangle)
        XCTAssertFalse(settings.shapeFill)
        XCTAssertEqual(settings.dock, .top)
        XCTAssertFalse(settings.menuVisible)
        XCTAssertEqual(Set(settings.slots.keys), Set(PaperTool.slotTools))

        let pen = try XCTUnwrap(settings.slots[.pen])
        XCTAssertEqual(pen.widths, [0.5, 5, 12], "clamped, and a word replaced by the factory width")
        XCTAssertEqual(pen.widthIndex, 0)
        XCTAssertEqual(pen.colors.count, 12)
        XCTAssertEqual(pen.colors.first, "#ffffff")
        XCTAssertEqual(pen.colors[1], "#00ff00", "invalid colours are dropped")
        XCTAssertEqual(pen.colorIndex, 0)
        XCTAssertEqual(settings.slots[.pencil], ToolSettings.factorySlots(.pencil))
        XCTAssertEqual(settings.slots[.eraser]?.colors, [])
        XCTAssertEqual(settings.slots[.highlighter], ToolSettings.factorySlots(.highlighter))
        XCTAssertNil(settings.slots[.lasso])

        XCTAssertThrowsError(try JSONDecoder().decode(ToolSettings.self, from: Data("[1]".utf8)))
        XCTAssertEqual(try JSONDecoder().decode(ToolSettings.self, from: Data("{}".utf8)), .factory)
    }

    func testAWritingToolActiveIsTheLastWritingTool() throws {
        let json = #"{"active": "highlighter", "lastWritingTool": "pencil"}"#
        let settings = try JSONDecoder().decode(ToolSettings.self, from: Data(json.utf8))
        XCTAssertEqual(settings.lastWritingTool, .highlighter)
    }
}

/// `ToolSettingsStore` against a scratch defaults suite, including the
/// one-time migration from the keys earlier builds wrote.
final class ToolSettingsStoreTests: XCTestCase {
    private var scratch: ScratchDefaults?
    private var defaults = UserDefaults.standard

    override func setUp() {
        super.setUp()
        do {
            let scratch = try ScratchDefaults("ToolSettingsStoreTests")
            self.scratch = scratch
            defaults = scratch.defaults
        } catch {
            XCTFail("could not make a defaults suite")
        }
    }

    override func tearDown() {
        scratch?.remove()
        scratch = nil
        super.tearDown()
    }

    private func seedEveryV1Key() {
        defaults.set("highlighter", forKey: "paper.tool")
        defaults.set(7.25, forKey: "paper.tool.penWidth")
        defaults.set(3, forKey: "paper.tool.penColor")
        defaults.set(31.0, forKey: "paper.tool.highlighterWidth")
        defaults.set(2, forKey: "paper.tool.highlighterColor")
        defaults.set(55.0, forKey: "paper.tool.eraserWidth")
        defaults.set(false, forKey: "paper.tool.eraserBitmap")
        defaults.set("leading", forKey: "paper.tool.capsuleEdge")
        defaults.set(0.3, forKey: "paper.tool.capsuleOffset")
    }

    private var legacyKeysLeft: [String] {
        ToolSettingsStore.LegacyKey.all.filter { defaults.object(forKey: $0) != nil }
    }

    private func storedJSON() throws -> [String: Any] {
        let string = try XCTUnwrap(defaults.string(forKey: "paper.tools.v2"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(string.utf8)) as? [String: Any])
    }

    func testAFreshInstallGetsTheFactorySettings() throws {
        XCTAssertEqual(ToolSettingsStore.key, "paper.tools.v2")
        XCTAssertEqual(ToolSettingsStore.activeKey, "paper.tool")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings, .factory)
        XCTAssertEqual(try storedJSON()["version"] as? Int, 2)
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "pen")
    }

    func testSavedSettingsComeBack() {
        var settings = ToolSettingsStore.load(defaults)
        settings.select(.highlighter)
        settings.setWidth(31, for: .highlighter)
        settings.addColor("#9ecbff", for: .highlighter)
        settings.dock = .bottom
        ToolSettingsStore.save(settings, defaults)
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "highlighter")
        XCTAssertEqual(ToolSettingsStore.load(defaults), settings)
    }

    func testPaperToolWinsOnLoad() {
        var settings = ToolSettings.factory
        settings.select(.pencil)
        ToolSettingsStore.save(settings, defaults)
        defaults.set("lasso", forKey: "paper.tool")
        let loaded = ToolSettingsStore.load(defaults)
        XCTAssertEqual(loaded.active, .lasso)
        XCTAssertEqual(loaded.previous, .pencil)
        XCTAssertEqual(loaded.lastWritingTool, .pencil)

        defaults.set("highlighter", forKey: "paper.tool")
        XCTAssertEqual(ToolSettingsStore.load(defaults).lastWritingTool, .highlighter)

        defaults.set("banana", forKey: "paper.tool")
        XCTAssertEqual(ToolSettingsStore.load(defaults).active, .pencil, "an unknown name is ignored")
    }

    /// UI tests launch with `-paper.tool lasso`, which lands in the argument
    /// domain that every suite reads through.
    func testALaunchArgumentWinsOnLoad() {
        let standard = UserDefaults.standard
        let original = standard.volatileDomain(forName: UserDefaults.argumentDomain)
        defer { standard.setVolatileDomain(original, forName: UserDefaults.argumentDomain) }
        var arguments = original
        arguments["paper.tool"] = "lasso"
        standard.setVolatileDomain(arguments, forName: UserDefaults.argumentDomain)
        guard defaults.string(forKey: "paper.tool") == "lasso" else {
            return XCTFail("the argument domain did not reach the suite")
        }
        var settings = ToolSettings.factory
        settings.select(.eraser)
        ToolSettingsStore.save(settings, defaults)
        XCTAssertEqual(ToolSettingsStore.load(defaults).active, .lasso)
    }

    func testEveryV1KeyMigratesAndIsDeleted() throws {
        seedEveryV1Key()
        let settings = ToolSettingsStore.load(defaults)

        XCTAssertEqual(settings.active, .highlighter)
        XCTAssertEqual(settings.lastWritingTool, .highlighter)
        XCTAssertEqual(settings.previous, .pen)
        XCTAssertEqual(settings.penStyle, .brush)
        XCTAssertEqual(settings.dock, .top, "the capsule's edge and offset are ignored")
        XCTAssertEqual(
            settings.slots[.pen],
            ToolSlots(
                widths: [3.625, 7.25, 12], widthIndex: 1,
                colors: ["#6c5ce7", "#1c1c1e", "#1f6feb", "#c0392b"], colorIndex: 0))
        XCTAssertEqual(
            settings.slots[.highlighter],
            ToolSlots(
                widths: [15.5, 31, 40], widthIndex: 1,
                colors: ["#9ecbff", "#ffe066", "#a8e6a1", "#ffb3c7"], colorIndex: 0))
        XCTAssertEqual(settings.slots[.eraser], ToolSlots(widths: [27.5, 55, 60], widthIndex: 1, colors: [], colorIndex: 0))
        XCTAssertEqual(settings.eraserMode, .stroke)
        // Tools that did not exist keep their factory slots.
        for tool in [PaperTool.pencil, .drawShape, .text, .shape] {
            XCTAssertEqual(settings.slots[tool], ToolSettings.factorySlots(tool), "\(tool)")
        }
        // What the pen draws with is what it drew with before: brush ink,
        // the same width and the same colour.
        XCTAssertEqual(settings.canvasTool(for: .pen), .ink(.pen, color: "#6c5ce7", width: 7.25))
        XCTAssertEqual(settings.canvasTool(for: .highlighter), .ink(.marker, color: "#9ecbff", width: 31))
        XCTAssertEqual(settings.canvasTool(for: .eraser), .eraser(.stroke, width: 55))

        XCTAssertEqual(legacyKeysLeft, [])
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "highlighter")
        XCTAssertEqual(try storedJSON()["penStyle"] as? String, "brush")
    }

    func testMigrationIsSafeToRunTwice() {
        seedEveryV1Key()
        let pure = ToolSettingsStore.migrate(defaults)
        XCTAssertEqual(ToolSettingsStore.migrate(defaults), pure)
        let first = ToolSettingsStore.load(defaults)
        XCTAssertEqual(first, pure)
        XCTAssertEqual(ToolSettingsStore.load(defaults), first)
        XCTAssertEqual(ToolSettingsStore.load(defaults), first)
    }

    func testAnInterruptedMigrationFinishes() {
        // The new blob was written but the old keys were not yet deleted.
        seedEveryV1Key()
        let migrated = ToolSettingsStore.migrate(defaults)
        ToolSettingsStore.save(migrated, defaults)
        defaults.set(1.0, forKey: "paper.tool.penWidth")
        XCTAssertEqual(ToolSettingsStore.load(defaults), migrated, "the stored blob wins")
        XCTAssertEqual(legacyKeysLeft, [])
    }

    func testOnlyPaperToolIsSetAsUITestsDo() {
        defaults.set("lasso", forKey: "paper.tool")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.active, .lasso)
        XCTAssertEqual(settings.lastWritingTool, .pen)
        XCTAssertEqual(settings.previous, .pen)
        // The earlier build's pen: brush ink at 3 pt in black.
        XCTAssertEqual(settings.canvasTool(for: .pen), .ink(.pen, color: "#1c1c1e", width: 3))
        XCTAssertEqual(settings.slots[.pen]?.widths, [1.5, 3, 5.25])
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1c1c1e", "#1f6feb", "#c0392b"])
        XCTAssertEqual(settings.slots[.highlighter]?.widths, [10, 20, 35])
        XCTAssertEqual(settings.slots[.eraser]?.widths, [12, 24, 42])
        XCTAssertEqual(settings.eraserMode, .partial)
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "lasso")
    }

    func testPartialSetsOfV1Keys() {
        defaults.set(5.0, forKey: "paper.tool.penWidth")
        defaults.set(7, forKey: "paper.tool.penColor")
        var settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.active, .pen)
        XCTAssertEqual(settings.penStyle, .brush)
        XCTAssertEqual(settings.slots[.pen]?.widths, [2.5, 5, 8.75])
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#c0392b", "#1c1c1e", "#1f6feb"], "de-duplicated")
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        XCTAssertEqual(settings.slots[.highlighter]?.colors, ["#ffe066", "#a8e6a1", "#ffb3c7"])

        tearDown()
        setUp()
        defaults.set(12.0, forKey: "paper.tool.highlighterWidth")
        defaults.set(true, forKey: "paper.tool.eraserBitmap")
        settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.slots[.highlighter]?.widths, [6, 12, 21])
        XCTAssertEqual(settings.eraserMode, .partial)
        XCTAssertEqual(settings.slots[.pen]?.widths, [1.5, 3, 5.25])

        tearDown()
        setUp()
        defaults.set("bottom", forKey: "paper.tool.capsuleEdge")
        settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.penStyle, .brush, "any earlier key marks an earlier install")
        XCTAssertEqual(settings.dock, .top)
        XCTAssertEqual(legacyKeysLeft, [])
    }

    func testV1ValuesOutsideTheirRanges() {
        defaults.set(9_999.0, forKey: "paper.tool.penWidth")
        defaults.set(42, forKey: "paper.tool.penColor")
        defaults.set(2.0, forKey: "paper.tool.highlighterWidth")
        defaults.set(9, forKey: "paper.tool.highlighterColor")
        defaults.set(0.0, forKey: "paper.tool.eraserWidth")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.slots[.pen]?.widths, [6, 12, 12])
        // The earlier build showed the last colour for a too-large index.
        XCTAssertEqual(settings.color(for: .pen), "#c0392b")
        XCTAssertEqual(settings.slots[.highlighter]?.widths, [6, 6, 10.5])
        XCTAssertEqual(settings.color(for: .highlighter), "#ffb3c7")
        XCTAssertEqual(settings.slots[.eraser]?.widths, [4, 4, 7])

        tearDown()
        setUp()
        defaults.set(0.1, forKey: "paper.tool.penWidth")
        XCTAssertEqual(ToolSettingsStore.load(defaults).slots[.pen]?.widths, [0.5, 0.5, 0.875])
    }

    /// The argument domain holds text, so `-paper.tool.penWidth 4.5` arrives
    /// as a string.
    func testV1ValuesStoredAsText() {
        defaults.set("4.5", forKey: "paper.tool.penWidth")
        defaults.set("2", forKey: "paper.tool.penColor")
        defaults.set("NO", forKey: "paper.tool.eraserBitmap")
        defaults.set(" 30 ", forKey: "paper.tool.eraserWidth")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.slots[.pen]?.widths, [2.25, 4.5, 7.875])
        XCTAssertEqual(settings.color(for: .pen), "#1f6feb")
        XCTAssertEqual(settings.eraserMode, .stroke)
        XCTAssertEqual(settings.slots[.eraser]?.widths, [15, 30, 52.5])
    }

    func testGarbageInV1KeysFallsBackToWhatTheEarlierBuildUsed() throws {
        defaults.set("banana", forKey: "paper.tool")
        defaults.set("wide", forKey: "paper.tool.penWidth")
        defaults.set(-3, forKey: "paper.tool.penColor")
        defaults.set(Double.nan, forKey: "paper.tool.highlighterWidth")
        defaults.set(true, forKey: "paper.tool.highlighterColor")
        defaults.set([1, 2], forKey: "paper.tool.eraserWidth")
        defaults.set("maybe", forKey: "paper.tool.eraserBitmap")
        defaults.set(Data([0xde, 0xad]), forKey: "paper.tool.capsuleEdge")
        defaults.set(["x": 1], forKey: "paper.tool.capsuleOffset")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.active, .pen)
        XCTAssertEqual(settings.lastWritingTool, .pen)
        XCTAssertEqual(settings.slots[.pen]?.widths, [1.5, 3, 5.25])
        XCTAssertEqual(settings.slots[.pen]?.colors, ["#1c1c1e", "#1f6feb", "#c0392b"])
        XCTAssertEqual(settings.slots[.highlighter]?.widths, [10, 20, 35])
        XCTAssertEqual(settings.color(for: .highlighter), "#ffe066")
        XCTAssertEqual(settings.slots[.eraser]?.widths, [12, 24, 42])
        XCTAssertEqual(settings.eraserMode, .partial)
        XCTAssertEqual(settings, settings.normalized())
        XCTAssertEqual(legacyKeysLeft, [])
        XCTAssertEqual(defaults.string(forKey: "paper.tool"), "pen", "the mirror replaces the garbage")
        XCTAssertEqual(ToolSettingsStore.load(defaults), settings)
    }

    func testAnUnreadableBlobIsReplaced() throws {
        defaults.set("{not json", forKey: "paper.tools.v2")
        defaults.set(6.0, forKey: "paper.tool.penWidth")
        let settings = ToolSettingsStore.load(defaults)
        XCTAssertEqual(settings.slots[.pen]?.widths, [3, 6, 10.5], "migrated from what is still there")
        XCTAssertEqual(ToolSettingsStore.decode(defaults.object(forKey: "paper.tools.v2")), settings)

        // Only the mirror is left, which this build wrote: not an earlier install.
        XCTAssertNotNil(defaults.object(forKey: "paper.tool"))
        for garbage: Any in [42, ["a", "b"], "[]", "null", Data("{oops".utf8)] {
            defaults.set(garbage, forKey: "paper.tools.v2")
            let loaded = ToolSettingsStore.load(defaults)
            XCTAssertEqual(loaded.slots[.pen], ToolSettings.factorySlots(.pen), "\(garbage)")
            XCTAssertEqual(loaded.penStyle, .fountain, "\(garbage)")
        }
    }

    func testABlobStoredAsDataIsRead() throws {
        var settings = ToolSettings.factory
        settings.select(.drawShape)
        defaults.set(try JSONEncoder().encode(settings), forKey: "paper.tools.v2")
        XCTAssertEqual(ToolSettingsStore.load(defaults).active, .drawShape)
    }
}
