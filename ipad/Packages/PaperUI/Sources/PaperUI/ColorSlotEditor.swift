import PaperCore
import SwiftUI

/// What a second tap on the selected colour opens: the tool's named swatches
/// (the highlighter has its own six), "Custom…" for the system colour picker,
/// and "Remove" while the tool has more than one colour. Every choice
/// rewrites the slot in place.
struct ColorSlotEditor: View {
    let tools: ToolState
    let tool: PaperTool
    let index: Int
    var isDarkPaper = false
    var onClose: () -> Void = {}

    private var current: String? {
        guard let slot = tools.settings.slots[tool], slot.colors.indices.contains(index) else {
            return nil
        }
        return slot.colors[index]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            LazyVGrid(
                columns: Array(repeating: GridItem(.fixed(40), spacing: 12), count: 4), spacing: 12
            ) {
                ForEach(ToolSettings.swatches(for: tool), id: \.hex) { swatch in
                    swatchButton(name: swatch.name, hex: swatch.hex)
                }
            }
            Divider()
            ColorPicker(selection: custom, supportsOpacity: false) {
                Text("Custom…")
                    .foregroundStyle(PaperTokens.text)
            }
            .accessibilityLabel("Custom…")
            .accessibilityIdentifier("colorSlot.custom")
            if tools.settings.canRemoveColor(for: tool) {
                Divider()
                Button(role: .destructive) {
                    tools.removeColor(at: index, for: tool)
                    onClose()
                } label: {
                    Text("Remove")
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .frame(minHeight: 32)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.red)
                .accessibilityLabel("Remove")
                .accessibilityIdentifier("colorSlot.remove")
            }
        }
        .padding(16)
        .frame(width: 232)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("ColorSlotEditor")
    }

    private func swatchButton(name: String, hex: String) -> some View {
        let selected = current == hex
        return Button {
            tools.setColor(hex, for: tool, at: index)
        } label: {
            ZStack {
                if selected {
                    Circle()
                        .strokeBorder(PaperTokens.text.opacity(0.55), lineWidth: 2)
                        .frame(width: 40, height: 40)
                }
                Circle()
                    .fill(Color(uiColor: ToolState.displayColor(hex, onDarkPaper: isDarkPaper)))
                    .frame(width: 32, height: 32)
                    .overlay(Circle().strokeBorder(PaperTokens.border, lineWidth: 0.5))
            }
            .frame(width: 40, height: 40)
            .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .help(name)
        .accessibilityLabel(name)
        .accessibilityValue(selected ? "selected" : "")
        .accessibilityIdentifier("colorSlot.\(name)")
    }

    /// The system picker edits the slot live; whatever it hands back is
    /// flattened to sRGB `#rrggbb`, the only form a slot holds.
    private var custom: Binding<Color> {
        Binding(
            get: { Color(uiColor: ToolState.uiColor(current ?? "#1c1c1e")) },
            set: { color in
                guard let hex = ToolState.hex(UIColor(color)) else { return }
                tools.setColor(hex, for: tool, at: index)
            })
    }
}
