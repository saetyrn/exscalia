import PaperCore
import SwiftUI

/// The always-visible answer to "did it save?".
///
/// Text plus one small monochrome glyph, never a spinner: a spinner says
/// "something is happening", and the point of this pill is to say what.
public struct SaveStatePill: View {
    /// `.pill` is the glyph and the words in a capsule. `.glyph` is the glyph
    /// alone, for the editor's tab bar: it takes the bar's foreground colour,
    /// gives the words as its tooltip and to VoiceOver, and taps the same.
    public enum Style: Sendable {
        case pill, glyph
    }

    private let state: SaveState
    private let style: Style
    private let action: (() -> Void)?

    public init(state: SaveState, style: Style = .pill, action: (() -> Void)? = nil) {
        self.state = state
        self.style = style
        self.action = action
    }

    public var body: some View {
        if let action {
            Button(action: action) { content }
                .buttonStyle(.plain)
                .accessibilityLabel(state.title)
                .accessibilityHint(hint)
        } else {
            content
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(state.title)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch style {
        case .pill: label
        case .glyph: glyph
        }
    }

    private var glyph: some View {
        Image(systemName: state.symbolName)
            .font(.system(size: 17, weight: .regular))
            .symbolRenderingMode(.monochrome)
            // Quiet while all is well, full strength when a person must look.
            .opacity(state.deservesAnnouncement ? 1 : 0.75)
            .frame(minWidth: PaperTokens.minimumTarget, minHeight: 32)
            .contentShape(Rectangle())
            .help(state.title)
    }

    private var label: some View {
        HStack(spacing: 6) {
            Image(systemName: state.symbolName)
                .font(.system(size: 12, weight: .regular))
                .symbolRenderingMode(.monochrome)
            Text(state.title)
                .font(.footnote)
                .monospacedDigit()
        }
        .foregroundStyle(foreground)
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .frame(minHeight: 28)
        .background(
            Capsule(style: .continuous).fill(PaperTokens.panel)
        )
        .overlay(
            Capsule(style: .continuous).strokeBorder(PaperTokens.border, lineWidth: 1)
        )
    }

    private var foreground: Color {
        switch state {
        case .conflict, .signedOut, .attention: return PaperTokens.text
        case .offline: return PaperTokens.textMuted
        case .savedLocally, .syncing, .synced: return PaperTokens.textMuted
        }
    }

    private var hint: String {
        switch state {
        case .conflict: return "Opens the conflicting page"
        case .signedOut: return "Opens EXSCALIA so you can sign in"
        case .attention: return "Tries the change that was refused again"
        default: return "Shows sync details"
        }
    }
}

#Preview("Every save state") {
    VStack(alignment: .leading, spacing: 12) {
        SaveStatePill(state: .savedLocally)
        SaveStatePill(state: .syncing(count: 4))
        SaveStatePill(state: .synced(at: Date().addingTimeInterval(-3)))
        SaveStatePill(state: .offline)
        SaveStatePill(state: .conflict(pageNumber: 2))
        SaveStatePill(state: .signedOut)
        SaveStatePill(state: .attention(message: "a page was not sent — 400: bad request"))
        HStack(spacing: 0) {
            SaveStatePill(state: .synced(at: Date()), style: .glyph)
            SaveStatePill(state: .syncing(count: 2), style: .glyph)
            SaveStatePill(state: .offline, style: .glyph)
            SaveStatePill(state: .conflict(pageNumber: 2), style: .glyph)
        }
        .foregroundStyle(.white)
        .background(Color(red: 0x1F / 255, green: 0x33 / 255, blue: 0x5E / 255))
    }
    .padding()
}
