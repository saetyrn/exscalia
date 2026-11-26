import PaperCore
import SwiftUI
import UIKit

/// The navy strip at the top of the editor: Home, one tab per open document,
/// and the save state at the right end.
///
/// The active tab is the navigation bar's blue and carries a chevron; tapping
/// its title opens the document's menu. Any other tab is a tap away, and every
/// tab closes with its cross. Only the active tab has an editor behind it.
struct DocumentTabBar: View {
    let tabs: DocumentTabsModel
    /// What the save state does when tapped.
    let onSaveStateTapped: () -> Void
    let onExport: () -> Void
    let onInfo: () -> Void
    let onMove: () -> Void
    let onOpenInNewWindow: (String) -> Void

    static let height: CGFloat = 44
    private static let homeWidth: CGFloat = 56
    private static let tabRange: ClosedRange<CGFloat> = 140...300

    var body: some View {
        HStack(spacing: 0) {
            Button(action: tabs.goHome) {
                Image(systemName: "house")
                    .font(.system(size: 17, weight: .regular))
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(PaperTokens.tabInactiveTitle)
                    .frame(width: Self.homeWidth, height: Self.height)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Home")
            .accessibilityLabel("Home")

            GeometryReader { proxy in
                let count = CGFloat(max(tabs.ids.count, 1))
                let width = min(
                    max(proxy.size.width / count, Self.tabRange.lowerBound), Self.tabRange.upperBound)
                ScrollViewReader { scroller in
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 0) {
                            ForEach(tabs.ids, id: \.self) { id in
                                DocumentTab(
                                    tabs: tabs, id: id, width: width,
                                    onExport: onExport, onInfo: onInfo, onMove: onMove,
                                    onOpenInNewWindow: onOpenInNewWindow
                                )
                                .id(id)
                            }
                        }
                    }
                    .scrollBounceBehavior(.basedOnSize, axes: .horizontal)
                    .onChange(of: tabs.activeID) { _, id in
                        guard let id else { return }
                        withAnimation(UIAccessibility.isReduceMotionEnabled ? nil : .default) {
                            scroller.scrollTo(id)
                        }
                    }
                }
            }
            .frame(height: Self.height)

            TabBarSaveState(controller: tabs.controller, action: onSaveStateTapped)
                .padding(.trailing, 6)
        }
        .frame(height: Self.height)
        .background(PaperTokens.tabStrip.ignoresSafeArea(edges: .top))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("DocumentTabs")
        #if DEBUG
            .onAppear { DebugConvert.runIfRequested(tabs: tabs) }
        #endif
    }
}

/// One open document.
private struct DocumentTab: View {
    let tabs: DocumentTabsModel
    let id: String
    let width: CGFloat
    let onExport: () -> Void
    let onInfo: () -> Void
    let onMove: () -> Void
    let onOpenInNewWindow: (String) -> Void

    @State private var isShowingMenu = false

    var body: some View {
        let active = tabs.activeID == id
        let title = tabs.title(of: id)
        let foreground = active ? Color.white : PaperTokens.tabInactiveTitle
        HStack(spacing: 0) {
            Button {
                if active {
                    isShowingMenu = true
                } else {
                    tabs.activate(id)
                }
            } label: {
                HStack(spacing: 6) {
                    Text(title.isEmpty ? "Untitled" : title)
                        .font(.system(size: 15, weight: active ? .medium : .regular))
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if active {
                        Image(systemName: "chevron.down")
                            .font(.system(size: 11, weight: .semibold))
                            .symbolRenderingMode(.monochrome)
                            .accessibilityHidden(true)
                    }
                    Spacer(minLength: 0)
                }
                .foregroundStyle(foreground)
                .padding(.leading, 14)
                .frame(maxHeight: .infinity)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(title.isEmpty ? "Untitled" : title)
            .accessibilityValue(active ? "selected" : "")
            .accessibilityAddTraits(active ? [.isSelected] : [])
            .accessibilityIdentifier("tab")
            .popover(isPresented: $isShowingMenu, arrowEdge: .bottom) {
                DocumentTabMenu(
                    tabs: tabs, id: id,
                    onExport: { isShowingMenu = false; onExport() },
                    onInfo: { isShowingMenu = false; onInfo() },
                    onMove: { isShowingMenu = false; onMove() },
                    onOpenInNewWindow: { isShowingMenu = false; onOpenInNewWindow(id) },
                    onDone: { isShowingMenu = false })
                .presentationCompactAdaptation(.popover)
                // Opaque: over the toolbar a translucent menu is hard to read.
                .presentationBackground(PaperTokens.background)
            }

            Button {
                tabs.close(id)
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .medium))
                    .symbolRenderingMode(.monochrome)
                    .foregroundStyle(foreground)
                    .frame(width: 40, height: DocumentTabBar.height)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Close")
            .accessibilityLabel("Close \(title.isEmpty ? "Untitled" : title)")
        }
        .frame(width: width, height: DocumentTabBar.height)
        .background(alignment: .bottom) {
            if active {
                UnevenRoundedRectangle(
                    topLeadingRadius: 10, bottomLeadingRadius: 0, bottomTrailingRadius: 0,
                    topTrailingRadius: 10, style: .continuous
                )
                .fill(PaperTokens.navigationBar)
                .padding(.top, 5)
            }
        }
        .accessibilityElement(children: .contain)
    }
}

/// The active tab's menu: its name, and what can be done with the document.
/// The name is saved on Return or when the menu closes.
struct DocumentTabMenu: View {
    let tabs: DocumentTabsModel
    let id: String
    let onExport: () -> Void
    let onInfo: () -> Void
    let onMove: () -> Void
    let onOpenInNewWindow: () -> Void
    let onDone: () -> Void

    @State private var name = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            TextField("Name", text: $name)
                .font(.headline)
                .textFieldStyle(.plain)
                .submitLabel(.done)
                .onSubmit {
                    rename()
                    onDone()
                }
                .padding(.horizontal, 16)
                .frame(height: 52)
                .accessibilityLabel("Name")
            Divider()
            row("Move", "folder", action: onMove)
            if let notebook = try? tabs.controller.store.notebook(id: id),
                PaperConvertDocumentAction.isOffered(notebook, store: tabs.controller.store)
            {
                ConvertToWhiteboardRow(notebook: notebook, inTab: true, onDone: onDone)
                    .environment(\.paperConvertDocument, PaperConvertDocumentAction(tabs: tabs))
            }
            row("Close Other Tabs", "xmark.square", disabled: tabs.ids.count < 2) {
                tabs.closeOthers(keeping: id)
                onDone()
            }
            row("Open in New Window", "macwindow.badge.plus", action: onOpenInNewWindow)
            row("Export", "square.and.arrow.up", action: onExport)
            row("Info", "info.circle", action: onInfo)
            Divider()
            // No question asked: the toast that follows offers it back.
            row("Move to Trash", "trash", role: .destructive) {
                onDone()
                tabs.trash(id)
            }
        }
        .frame(width: 300)
        .padding(.bottom, 6)
        .onAppear { name = tabs.title(of: id) }
        .onDisappear(perform: rename)
    }

    private func rename() {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != tabs.title(of: id),
            let notebook = try? tabs.controller.store.notebook(id: id)
        else { return }
        tabs.controller.rename(notebook, to: trimmed)
        tabs.session?.reload()
    }

    private func row(
        _ title: String, _ symbol: String, role: ButtonRole? = nil, disabled: Bool = false,
        action: @escaping () -> Void
    ) -> some View {
        Button(role: role, action: action) {
            HStack(spacing: 12) {
                Text(title)
                Spacer(minLength: 0)
                Image(systemName: symbol)
                    .symbolRenderingMode(.monochrome)
                    .frame(width: 22)
                    .accessibilityHidden(true)
            }
            .font(.body)
            .foregroundStyle(role == .destructive ? Color.red : PaperTokens.text)
            .padding(.horizontal, 16)
            .frame(height: PaperTokens.minimumTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.4 : 1)
        .accessibilityLabel(title)
    }
}

/// The save state as one glyph at the end of the tab strip: its words are the
/// tooltip and what VoiceOver reads, and a tap does what the pill's does.
private struct TabBarSaveState: View {
    let controller: PaperController
    let action: () -> Void

    var body: some View {
        SaveStatePill(state: controller.saveState, style: .glyph, action: action)
            .foregroundStyle(Color.white)
            .frame(height: DocumentTabBar.height)
            // Re-read on the controller's tick, so "Synced 3s ago" keeps
            // counting.
            .id(controller.saveStateVersion)
    }
}
