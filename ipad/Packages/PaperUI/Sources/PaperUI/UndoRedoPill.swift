import SwiftUI

/// Undo and redo, floating at the top left of the canvas. A button that cannot
/// act is dimmed, never hidden, so the pair never changes shape.
public struct UndoRedoPill: View {
    let chrome: EditorChromeState
    let onUndo: () -> Void
    let onRedo: () -> Void

    public init(chrome: EditorChromeState, onUndo: @escaping () -> Void, onRedo: @escaping () -> Void) {
        self.chrome = chrome
        self.onUndo = onUndo
        self.onRedo = onRedo
    }

    public var body: some View {
        HStack(spacing: 0) {
            button("arrow.uturn.backward", "Undo", enabled: chrome.canUndo, action: onUndo)
            button("arrow.uturn.forward", "Redo", enabled: chrome.canRedo, action: onRedo)
        }
        .padding(.horizontal, 2)
        .floatingPillStyle(cornerRadius: 14)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("UndoRedo")
    }

    private func button(
        _ symbol: String, _ label: String, enabled: Bool, action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 17, weight: .regular))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(Color.white.opacity(enabled ? 1 : 0.32))
                .frame(width: PaperTokens.minimumTarget, height: PaperTokens.minimumTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(!enabled)
        .help(label)
        .accessibilityLabel(label)
    }
}
