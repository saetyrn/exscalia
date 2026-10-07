import PaperCore
import SwiftUI

/// Shown in place of the library when Paper's storage for this server could
/// not be opened: what failed, a way to try again, and the way back.
public struct PaperPlaceholderView: View {
    private let saveState: SaveState
    private let message: String
    private let onRetry: () -> Void
    private let onSettings: () -> Void
    private let onBack: () -> Void

    public init(
        saveState: SaveState = .savedLocally,
        message: String,
        onRetry: @escaping () -> Void,
        onSettings: @escaping () -> Void,
        onBack: @escaping () -> Void
    ) {
        self.saveState = saveState
        self.message = message
        self.onRetry = onRetry
        self.onSettings = onSettings
        self.onBack = onBack
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider().overlay(PaperTokens.border)
            Spacer(minLength: 0)
            VStack(spacing: 16) {
                VStack(spacing: 16) {
                    Image(systemName: "externaldrive.badge.exclamationmark")
                        .font(.system(size: 36, weight: .light))
                        .symbolRenderingMode(.monochrome)
                        .foregroundStyle(PaperTokens.textMuted)
                        .accessibilityHidden(true)
                    Text("Paper is unavailable")
                        .font(.title3)
                        .foregroundStyle(PaperTokens.text)
                    Text(message)
                        .font(.callout)
                        .foregroundStyle(PaperTokens.textMuted)
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 420)
                }
                .accessibilityElement(children: .combine)

                HStack(spacing: 12) {
                    Button("Retry", action: onRetry)
                        .accessibilityLabel("Retry opening Paper")

                    Button("Server settings", action: onSettings)
                        .accessibilityLabel("Open server settings")
                }
                .buttonStyle(.bordered)
                .tint(PaperTokens.accent)
                .controlSize(.large)
            }
            .padding(24)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PaperTokens.background)
    }

    private var header: some View {
        HStack(spacing: 12) {
            Button(action: onBack) {
                Image(systemName: "chevron.backward")
                    .font(.system(size: 17, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .frame(
                        width: PaperTokens.minimumTarget,
                        height: PaperTokens.minimumTarget
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(PaperTokens.text)
            .accessibilityLabel("Back to EXSCALIA")
            .accessibilityHint("Leaves Paper and returns to the web app")

            Spacer(minLength: 0)

            SaveStatePill(state: saveState)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(PaperTokens.panel)
    }
}

#Preview("Paper unavailable") {
    PaperPlaceholderView(
        saveState: .savedLocally,
        message: "The file “paper.sqlite” couldn’t be opened.",
        onRetry: {}, onSettings: {}, onBack: {})
}
