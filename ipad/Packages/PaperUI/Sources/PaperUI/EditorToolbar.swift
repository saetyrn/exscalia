import PaperCore
import SwiftUI

/// The middle group of the navigation bar: Lasso, the writing tool, Eraser,
/// Text, Image and Shapes, left to right, the same for notebooks and
/// whiteboards.
///
/// Tapping a tool selects it; tapping the selected one shows or hides the tool
/// menu. The writing button shows whichever writing tool was used last. It
/// only changes `ToolState`; the canvas follows through its observer.
public struct EditorToolbar: View {
    let tools: ToolState

    public init(tools: ToolState) {
        self.tools = tools
    }

    public var body: some View {
        HStack(spacing: 6) {
            ForEach(ToolbarSlot.allCases, id: \.self) { slot in
                ToolbarButton(tools: tools, slot: slot)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("Toolbar")
    }
}

private struct ToolbarButton: View {
    let tools: ToolState
    let slot: ToolbarSlot

    var body: some View {
        let tool = tools.settings.tool(for: slot)
        let selected = tools.settings.isSelected(slot)
        Button {
            tools.tap(slot)
        } label: {
            Image(systemName: tool.symbolName)
                .font(.system(size: 19, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(selected ? PaperTokens.toolSelectedGlyph : Color.white)
                .frame(width: 36, height: 36)
                .background(
                    RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(selected ? PaperTokens.toolSelected : Color.clear))
                .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(tool.label)
        .accessibilityLabel(tool.label)
        .accessibilityValue(selected ? "selected" : "")
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("toolbar.\(slot.rawValue)")
    }
}
