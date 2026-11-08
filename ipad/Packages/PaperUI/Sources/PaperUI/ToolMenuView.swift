import PaperCore
import SwiftUI
import UniformTypeIdentifiers

/// Where the tool menu is shown: docked over the canvas, or as the Pencil's
/// palette in a popover at the tip.
enum ToolMenuStyle: Equatable {
    case docked(ToolMenuDock)
    case popover
}

/// The floating tool menu: the selected tool's options in one dark pill.
///
/// Writing tools show the four writing types, three stroke slots and the
/// colour slots; the eraser its two modes, three sizes and Clear Page; text
/// three sizes and colours; shapes the five kinds, widths, colours and Fill.
/// Tapping a selected slot opens its editor. It writes nothing itself: every
/// change goes through `ToolState`, which saves it.
struct ToolMenuView: View {
    let tools: ToolState
    let style: ToolMenuStyle
    var isDarkPaper = false
    var onClearPage: () -> Void = {}
    /// The grip's drag, in the docking layer's coordinate space. The grip is
    /// shown only when these are set.
    var onGripChanged: ((DragGesture.Value) -> Void)?
    var onGripEnded: ((DragGesture.Value) -> Void)?

    @State private var editor: SlotEditor?
    @State private var draggedColor: Int?
    @State private var isConfirmingClear = false

    enum SlotEditor: Hashable {
        case width(Int)
        case color(Int)
    }

    private var axis: Axis {
        switch style {
        case .popover: return .horizontal
        case .docked(let dock): return dock.isHorizontal ? .horizontal : .vertical
        }
    }

    /// The tool whose options are shown. The palette shows the last writing
    /// tool while the lasso or an image is active, which have none.
    private var tool: PaperTool {
        let active = tools.settings.active
        if style == .popover, !active.hasToolMenu { return tools.settings.lastWritingTool }
        return active
    }

    var body: some View {
        // The pill hugs its contents; past the length its caller allows, the
        // same contents scroll.
        if axis == .horizontal {
            ViewThatFits(in: .horizontal) {
                decorated(pill)
                decorated(ScrollView(.horizontal, showsIndicators: false) { pill })
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            ViewThatFits(in: .vertical) {
                decorated(pill)
                decorated(ScrollView(.vertical, showsIndicators: false) { pill })
                    .fixedSize(horizontal: true, vertical: false)
            }
        }
    }

    private func decorated(_ content: some View) -> some View {
        content
            .floatingPillStyle(cornerRadius: 14, floating: style != .popover)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Tool menu")
            .accessibilityIdentifier("ToolMenu")
            .onAppear(perform: openDebugEditor)
            // A slot editor belongs to the tool it was opened for.
            .onChange(of: tool) { _, _ in editor = nil }
    }

    private var pill: some View {
        let layout =
            axis == .horizontal
            ? AnyLayout(HStackLayout(spacing: 0)) : AnyLayout(VStackLayout(spacing: 0))
        return layout {
            if onGripChanged != nil { grip }
            contents
        }
        .padding(axis == .horizontal ? .horizontal : .vertical, 4)
    }

    @ViewBuilder
    private var contents: some View {
        switch tool {
        case .pen, .pencil, .highlighter, .drawShape:
            writingTypes
            divider
            widthSlots
            divider
            colorSlots
        case .eraser:
            eraserModes
            divider
            widthSlots
            divider
            clearPage
        case .text:
            widthSlots
            divider
            colorSlots
        case .shape:
            shapeKinds
            divider
            widthSlots
            divider
            colorSlots
            divider
            fill
        case .lasso, .image:
            EmptyView()
        }
    }

    /// In the palette a choice is also a choice of tool: it may be showing
    /// the last writing tool while the lasso is active.
    private func choose(_ change: () -> Void) {
        if style == .popover { tools.select(tool) }
        change()
    }

    // MARK: Grip

    private var grip: some View {
        Capsule()
            .fill(Color.white.opacity(0.35))
            .frame(width: axis == .horizontal ? 4 : 18, height: axis == .horizontal ? 18 : 4)
            .frame(
                width: axis == .horizontal ? 20 : PaperTokens.minimumTarget,
                height: axis == .horizontal ? PaperTokens.minimumTarget : 20)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 4, coordinateSpace: .named(FloatingToolLayer.space))
                    .onChanged { onGripChanged?($0) }
                    .onEnded { onGripEnded?($0) })
            .help("Move")
            .accessibilityElement()
            .accessibilityLabel("Move tool menu")
            .accessibilityAddTraits(.isButton)
            .accessibilityAction(named: "Top") { tools.setDock(.top) }
            .accessibilityAction(named: "Bottom") { tools.setDock(.bottom) }
            .accessibilityAction(named: "Left") { tools.setDock(.leading) }
            .accessibilityAction(named: "Right") { tools.setDock(.trailing) }
    }

    private var divider: some View {
        Rectangle()
            .fill(PaperTokens.floatingDivider)
            .frame(width: axis == .horizontal ? 1 : 24, height: axis == .horizontal ? 24 : 1)
            .padding(axis == .horizontal ? .horizontal : .vertical, 6)
            .accessibilityHidden(true)
    }

    // MARK: Writing tools

    private var writingTypes: some View {
        ForEach(PaperTool.writingTools, id: \.self) { writing in
            let selected = tool == writing
            if writing == .pen, selected {
                Menu {
                    Picker(
                        "Pen",
                        selection: Binding(
                            get: { tools.settings.penStyle },
                            set: { style in choose { tools.selectPenStyle(style) } })
                    ) {
                        ForEach(PenStyle.allCases, id: \.self) { style in
                            Text(style.label).tag(style)
                        }
                    }
                } label: {
                    WritingTypeLabel(tool: writing, selected: true, chevron: true)
                }
                .menuIndicator(.hidden)
                .buttonStyle(.plain)
                .help(writing.label)
                .accessibilityLabel(writing.label)
                .accessibilityValue("selected, \(tools.settings.penStyle.label)")
                .accessibilityIdentifier("toolMenu.\(writing.rawValue)")
            } else {
                Button {
                    tools.select(writing)
                } label: {
                    WritingTypeLabel(tool: writing, selected: selected, chevron: false)
                }
                .buttonStyle(.plain)
                .help(writing.label)
                .accessibilityLabel(writing.label)
                .accessibilityValue(selected ? "selected" : "")
                .accessibilityIdentifier("toolMenu.\(writing.rawValue)")
            }
        }
    }

    // MARK: Stroke slots

    private var widthSlots: some View {
        let slot = tools.settings.slots[tool]
        return ForEach(0..<ToolSlots.widthCount, id: \.self) { index in
            let width = slot.map { $0.widths.indices.contains(index) ? $0.widths[index] : 0 } ?? 0
            let selected = slot?.widthIndex == index
            Button {
                choose {
                    if tools.selectWidth(index, for: tool) { editor = .width(index) }
                }
            } label: {
                WidthSlotGlyph(tool: tool, width: width, index: index)
                    .frame(width: 36, height: 36)
                    .background(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(selected ? PaperTokens.floatingSelected : Color.clear))
                    .frame(width: 40, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .popover(isPresented: editorBinding(.width(index))) {
                WidthSlotEditor(tools: tools, tool: tool, index: index, isDarkPaper: isDarkPaper)
            }
            .help(Self.widthName(tool, index))
            .accessibilityLabel(Self.widthName(tool, index))
            .accessibilityValue(
                Self.formatted(width, tool: tool) + (selected ? ", selected" : ""))
            .accessibilityIdentifier("toolMenu.width.\(index)")
        }
    }

    static func widthName(_ tool: PaperTool, _ index: Int) -> String {
        switch tool {
        case .eraser, .text: return ["Small", "Medium", "Large"][min(max(index, 0), 2)]
        default: return ["Thin", "Medium", "Thick"][min(max(index, 0), 2)]
        }
    }

    static func formatted(_ width: Double, tool: PaperTool) -> String {
        "\(WidthSlotEditor.number(width)) \(width == 1 ? "point" : "points")"
    }

    // MARK: Colour slots

    @ViewBuilder
    private var colorSlots: some View {
        let slot = tools.settings.slots[tool]
        let colors = slot?.colors ?? []
        ForEach(Array(colors.enumerated()), id: \.offset) { index, hex in
            let selected = slot?.colorIndex == index
            Button {
                choose {
                    if tools.selectColor(index, for: tool) { editor = .color(index) }
                }
            } label: {
                ColorDot(
                    color: Color(uiColor: ToolState.displayColor(hex, onDarkPaper: isDarkPaper)),
                    hex: hex, selected: selected)
            }
            .buttonStyle(.plain)
            .popover(isPresented: editorBinding(.color(index))) {
                ColorSlotEditor(
                    tools: tools, tool: tool, index: index, isDarkPaper: isDarkPaper
                ) { editor = nil }
            }
            .onDrag {
                draggedColor = index
                return NSItemProvider(object: "\(index)" as NSString)
            }
            .onDrop(
                of: [UTType.text],
                delegate: ColorSlotDropDelegate(
                    tools: tools, tool: tool, destination: index, dragged: $draggedColor))
            .help(Self.colorName(hex))
            .accessibilityLabel(Self.colorName(hex))
            .accessibilityValue(selected ? "selected" : "")
            .accessibilityIdentifier("toolMenu.color.\(index)")
        }
        if tools.settings.canAddColor(for: tool) {
            Button {
                choose { tools.addColor(for: tool) }
            } label: {
                Image(systemName: "plus")
                    .font(.system(size: 15, weight: .medium))
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(Color.white)
                    .frame(width: 40, height: PaperTokens.minimumTarget)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Add Color")
            .accessibilityLabel("Add Color")
            .accessibilityIdentifier("toolMenu.addColor")
        }
    }

    static func colorName(_ hex: String) -> String {
        ToolSettings.swatchName(hex) ?? "Custom color"
    }

    // MARK: Eraser

    private var eraserModes: some View {
        ForEach(EraserMode.allCases, id: \.self) { mode in
            let selected = tools.settings.eraserMode == mode
            Button {
                choose { tools.setEraserMode(mode) }
            } label: {
                MenuGlyph(
                    symbol: mode == .partial ? "eraser" : "eraser.line.dashed",
                    selected: selected)
            }
            .buttonStyle(.plain)
            .help(mode.label)
            .accessibilityLabel(mode.label)
            .accessibilityValue(selected ? "selected" : "")
            .accessibilityIdentifier("toolMenu.eraser.\(mode.rawValue)")
        }
    }

    private var clearPage: some View {
        Button {
            isConfirmingClear = true
        } label: {
            MenuGlyph(symbol: "trash", selected: false)
        }
        .buttonStyle(.plain)
        .help("Clear Page")
        .accessibilityLabel("Clear Page")
        .accessibilityIdentifier("toolMenu.clearPage")
        .confirmationDialog("Clear Page", isPresented: $isConfirmingClear) {
            Button("Clear Page", role: .destructive, action: onClearPage)
        }
    }

    // MARK: Shapes

    private var shapeKinds: some View {
        ForEach(ShapeKind.allCases, id: \.self) { kind in
            let selected = tools.settings.shapeKind == kind
            Button {
                choose { tools.setShapeKind(kind) }
            } label: {
                MenuGlyph(symbol: kind.symbolName, selected: selected)
            }
            .buttonStyle(.plain)
            .help(kind.label)
            .accessibilityLabel(kind.label)
            .accessibilityValue(selected ? "selected" : "")
            .accessibilityIdentifier("toolMenu.shape.\(kind.rawValue)")
        }
    }

    private var fill: some View {
        let on = tools.settings.shapeFill
        return Button {
            choose { tools.setShapeFill(!on) }
        } label: {
            MenuGlyph(symbol: on ? "square.fill" : "square.dashed", selected: on)
        }
        .buttonStyle(.plain)
        .help("Fill")
        .accessibilityLabel("Fill")
        .accessibilityValue(on ? "on" : "off")
        .accessibilityIdentifier("toolMenu.fill")
    }

    // MARK: Editors

    private func editorBinding(_ target: SlotEditor) -> Binding<Bool> {
        Binding(
            get: { editor == target },
            set: { presented in
                if presented {
                    editor = target
                } else if editor == target {
                    editor = nil
                }
            })
    }

    /// `-debugSlotEditor width|color` opens the selected slot's editor, for a
    /// screenshot: the simulator cannot tap a slot twice from a script.
    private func openDebugEditor() {
        #if DEBUG
            guard case .docked = style else { return }
            let arguments = ProcessInfo.processInfo.arguments
            guard let position = arguments.firstIndex(of: "-debugSlotEditor"),
                arguments.indices.contains(position + 1),
                let slot = tools.settings.slots[tool]
            else { return }
            switch arguments[position + 1] {
            case "width": editor = .width(slot.widthIndex)
            case "color" where !slot.colors.isEmpty: editor = .color(slot.colorIndex)
            default: break
            }
        #endif
    }
}

// MARK: - Pieces

/// A writing type: its glyph, on the teal square when selected, with the
/// pen's style chevron.
private struct WritingTypeLabel: View {
    let tool: PaperTool
    let selected: Bool
    let chevron: Bool

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: tool.symbolName)
                .font(.system(size: 18, weight: .regular))
            if chevron {
                Image(systemName: "chevron.down")
                    .font(.system(size: 9, weight: .semibold))
            }
        }
        .symbolRenderingMode(.monochrome)
        .foregroundStyle(Color.white)
        .frame(width: chevron ? 52 : 36, height: 32)
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(selected ? PaperTokens.writingToolSelected : Color.clear))
        .frame(minWidth: PaperTokens.minimumTarget, minHeight: PaperTokens.minimumTarget)
        .contentShape(Rectangle())
    }
}

/// One glyph in the pill, on the lighter square when it is the selected
/// choice.
private struct MenuGlyph: View {
    let symbol: String
    let selected: Bool

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 17, weight: .regular))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(Color.white)
            .frame(width: 36, height: 36)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(selected ? PaperTokens.floatingSelected : Color.clear))
            .frame(width: 40, height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
    }
}

/// A stroke slot: a bar as thick as the stroke for ink, a dot as wide as the
/// hole for the eraser, a letter for text.
private struct WidthSlotGlyph: View {
    let tool: PaperTool
    let width: Double
    let index: Int

    var body: some View {
        switch tool {
        case .eraser:
            let range = ToolSettings.widthRange(.eraser)
            let fraction = (width - range.lowerBound) / (range.upperBound - range.lowerBound)
            Circle()
                .fill(Color.white)
                .frame(width: 5 + 15 * fraction, height: 5 + 15 * fraction)
                .accessibilityHidden(true)
        case .text:
            Image(
                systemName: ["textformat.size.smaller", "textformat.size", "textformat.size.larger"][
                    min(max(index, 0), 2)]
            )
            .font(.system(size: 17, weight: .regular))
            .symbolRenderingMode(.monochrome)
            .foregroundStyle(Color.white)
            .accessibilityHidden(true)
        default:
            let range = ToolSettings.widthRange(tool)
            let fraction = (width - range.lowerBound) / (range.upperBound - range.lowerBound)
            Capsule()
                .fill(Color.white)
                .frame(width: 20, height: 1.5 + 7 * min(max(fraction, 0), 1))
                .accessibilityHidden(true)
        }
    }
}

/// A colour slot. The selected one wears a ring and the chevron that says a
/// second tap edits it.
private struct ColorDot: View {
    let color: Color
    let hex: String
    let selected: Bool

    var body: some View {
        ZStack {
            if selected {
                Circle()
                    .strokeBorder(PaperTokens.floatingSelected, lineWidth: 3)
                    .frame(width: 32, height: 32)
            }
            Circle()
                .fill(color)
                .frame(width: selected ? 22 : 24, height: selected ? 22 : 24)
                .overlay(Circle().strokeBorder(Color.white.opacity(0.18), lineWidth: 0.5))
            if selected {
                Image(systemName: "chevron.down")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Self.isLight(hex) ? Color.black : Color.white)
            }
        }
        .frame(width: 40, height: PaperTokens.minimumTarget)
        .contentShape(Rectangle())
    }

    private static func isLight(_ hex: String) -> Bool {
        guard let rgb = ToolSettings.rgb(hex) else { return false }
        return 0.299 * rgb.red + 0.587 * rgb.green + 0.114 * rgb.blue > 0.6
    }
}

/// Long-press a colour and drop it on another to move it there.
private struct ColorSlotDropDelegate: DropDelegate {
    let tools: ToolState
    let tool: PaperTool
    let destination: Int
    @Binding var dragged: Int?

    func performDrop(info: DropInfo) -> Bool {
        guard let source = dragged else { return false }
        tools.moveColor(from: source, to: destination, for: tool)
        dragged = nil
        return true
    }

    func dropUpdated(info: DropInfo) -> DropProposal? { DropProposal(operation: .move) }
}

/// The slider a selected stroke slot opens, over a stroke drawn at the width
/// and colour it will be.
struct WidthSlotEditor: View {
    let tools: ToolState
    let tool: PaperTool
    let index: Int
    var isDarkPaper = false

    private var width: Double {
        guard let slot = tools.settings.slots[tool], slot.widths.indices.contains(index) else {
            return ToolSettings.widthRange(tool).lowerBound
        }
        return slot.widths[index]
    }

    private var step: Double {
        switch tool {
        case .highlighter, .eraser, .text: return 1
        default: return 0.25
        }
    }

    var body: some View {
        VStack(spacing: 14) {
            WidthPreview(
                tool: tool, width: width,
                color: Color(
                    uiColor: ToolState.displayColor(
                        tools.settings.color(for: tool) ?? "#1c1c1e", onDarkPaper: isDarkPaper)),
                paper: isDarkPaper ? PaperColor.dark : PaperColor.white)
            .frame(height: 64)
            HStack(spacing: 12) {
                Slider(
                    value: Binding(
                        get: { width },
                        set: { tools.setWidth($0, for: tool, at: index) }),
                    in: ToolSettings.widthRange(tool), step: step)
                .accessibilityLabel(ToolMenuView.widthName(tool, index))
                .accessibilityValue(ToolMenuView.formatted(width, tool: tool))
                Text(Self.readout(width))
                    .font(.system(size: 13).monospacedDigit())
                    .foregroundStyle(PaperTokens.textMuted)
                    .frame(width: 56, alignment: .trailing)
                    .accessibilityHidden(true)
            }
        }
        .padding(16)
        .frame(width: 300)
    }

    static func readout(_ width: Double) -> String { number(width) + " pt" }

    /// 2, 2.5, 2.25: at most two decimals, none when whole.
    static func number(_ width: Double) -> String {
        String(format: "%g", (width * 100).rounded() / 100)
    }
}

/// A sample of what the slot draws: a stroke, the eraser's hole, or text at
/// its size.
private struct WidthPreview: View {
    let tool: PaperTool
    let width: Double
    let color: Color
    let paper: PaperColor

    var body: some View {
        Canvas { context, size in
            let rect = CGRect(origin: .zero, size: size)
            switch tool {
            case .eraser:
                let radius = min(width / 2, Double(size.height) / 2 - 4)
                context.fill(
                    Path(
                        ellipseIn: CGRect(
                            x: rect.midX - radius, y: rect.midY - radius, width: radius * 2,
                            height: radius * 2)),
                    with: .color(.gray.opacity(0.45)))
            case .text:
                context.draw(
                    Text("Aa").font(.system(size: min(width, Double(size.height) - 8)))
                        .foregroundStyle(color),
                    at: CGPoint(x: rect.midX, y: rect.midY))
            default:
                var path = Path()
                let inset = max(width / 2 + 6, 12)
                path.move(to: CGPoint(x: inset, y: rect.midY))
                path.addCurve(
                    to: CGPoint(x: rect.maxX - inset, y: rect.midY),
                    control1: CGPoint(x: rect.width * 0.35, y: rect.midY - rect.height * 0.32),
                    control2: CGPoint(x: rect.width * 0.65, y: rect.midY + rect.height * 0.32))
                context.stroke(
                    path, with: .color(tool == .highlighter ? color.opacity(0.5) : color),
                    style: StrokeStyle(
                        lineWidth: min(width, Double(size.height) - 8), lineCap: .round))
            }
        }
        .background(
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(Color(red: paper.rgb.red, green: paper.rgb.green, blue: paper.rgb.blue)))
        .accessibilityHidden(true)
    }
}

// MARK: - Docking

/// The undo pill and the tool menu, floating over the canvas.
///
/// The undo pill sits at the top left. The tool menu starts at the top centre;
/// dragging its grip carries it, and letting go snaps it to the nearest side,
/// which it remembers. It never covers the undo pill and keeps inside the
/// safe area, so it is never under the home indicator.
struct FloatingToolLayer: View {
    let tools: ToolState
    let chrome: EditorChromeState
    var isDarkPaper = false
    let onUndo: () -> Void
    let onRedo: () -> Void
    let onClearPage: () -> Void

    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var drag: CGSize = .zero

    static let space = "paper.floatingTools"
    static let inset: CGFloat = 8
    /// The undo pill: two 44 pt buttons and a little padding.
    static let undoPill = CGSize(width: 92, height: 44)

    var body: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                if !chrome.isReadOnly {
                    UndoRedoPill(chrome: chrome, onUndo: onUndo, onRedo: onRedo)
                        .padding(.leading, Self.inset)
                        .padding(.top, Self.inset)
                    if tools.settings.showsToolMenu {
                        menu(in: proxy.size)
                    }
                }
            }
            .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
        }
        .coordinateSpace(.named(Self.space))
    }

    private func menu(in size: CGSize) -> some View {
        let dock = tools.settings.dock
        let inset = Self.inset
        let belowUndo = inset + Self.undoPill.height + inset
        let maxLength: CGFloat
        let alignment: Alignment
        var padding = EdgeInsets()
        switch dock {
        case .top:
            // Centred, and short enough to stay clear of the undo pill.
            maxLength = size.width - 2 * (inset + Self.undoPill.width + inset)
            alignment = .top
            padding.top = inset
        case .bottom:
            maxLength = size.width - 2 * inset
            alignment = .bottom
            padding.bottom = inset
        case .leading:
            maxLength = size.height - belowUndo - inset
            alignment = .leading
            padding = EdgeInsets(top: belowUndo, leading: inset, bottom: inset, trailing: 0)
        case .trailing:
            maxLength = size.height - 2 * inset
            alignment = .trailing
            padding = EdgeInsets(top: inset, leading: 0, bottom: inset, trailing: inset)
        }
        let menu = ToolMenuView(
            tools: tools, style: .docked(dock), isDarkPaper: isDarkPaper,
            onClearPage: onClearPage,
            onGripChanged: { drag = $0.translation },
            onGripEnded: { end($0, in: size) }
        )
        return Group {
            if dock.isHorizontal {
                menu.frame(maxWidth: max(maxLength, 120))
            } else {
                menu.frame(maxHeight: max(maxLength, 120))
            }
        }
        .offset(drag)
        .padding(padding)
        .frame(width: size.width, height: size.height, alignment: alignment)
    }

    /// Snaps the menu to the side nearest where the finger let go.
    private func end(_ value: DragGesture.Value, in size: CGSize) {
        let point = value.location
        let distances: [(ToolMenuDock, CGFloat)] = [
            (.top, point.y), (.bottom, size.height - point.y),
            (.leading, point.x), (.trailing, size.width - point.x),
        ]
        let nearest = distances.min { $0.1 < $1.1 }?.0 ?? .top
        withAnimation(reduceMotion ? nil : .snappy(duration: 0.25)) {
            drag = .zero
            tools.setDock(nearest)
        }
    }
}
