import Foundation
import PaperCore
import PaperUI
import SwiftUI
import UIKit

/// Root of the app: one of two surfaces, plus the settings sheet.
///
/// The web view is kept alive across surface switches (`ZStack` + `opacity`
/// rather than an `if`) so returning from Paper does not reload the web app or
/// lose its scroll position and in-page state.
struct RootView: View {
    /// Integration seam for PaperUI's Camera action. Keeping this closure on
    /// the shell makes VisionKit presentation a shell concern while the
    /// notebook remains responsible for importing the returned PDF bytes.
    static let scanDocument: @MainActor (UIViewController) async throws -> Data = { presenter in
        let scanner = PaperScannerCoordinator()
        return try await scanner.scan(from: presenter).pdfData
    }

    @Environment(AppModel.self) private var appModel
    @State private var sceneModel = SceneModel()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        @Bindable var sceneModel = sceneModel

        ZStack {
            WebShellView()
                .ignoresSafeArea()
                .opacity(sceneModel.surface == .web ? 1 : 0)
                .accessibilityHidden(sceneModel.surface != .web)

            if sceneModel.surface == .paper {
                if let controller = sceneModel.paperController {
                    LibraryView(
                        controller: controller,
                        deepLink: sceneModel.paperTarget.deepLink,
                        onOpenOnWeb: { notebookPath, pageID in
                            sceneModel.openOnWeb(notebookPath: notebookPath, pageID: pageID)
                        },
                        onNavigateOnWeb: { path in
                            sceneModel.openOnWeb(path: path)
                        },
                        scanDocument: Self.scanDocument
                    ) {
                        sceneModel.showWeb()
                    }
                    .transition(.identity)
                } else {
                    PaperPlaceholderView(
                        saveState: sceneModel.saveState,
                        message: appModel.paperControllerError ?? "No server is configured.",
                        onRetry: { appModel.retryPaperController() },
                        onSettings: { sceneModel.isShowingSettings = true }
                    ) {
                        sceneModel.showWeb()
                    }
                    .transition(.identity)
                }
            }

            if sceneModel.surface == .web, let failure = sceneModel.webLoadFailure {
                WebLoadFailureView(message: failure)
            }

            #if DEBUG
                if sceneModel.surface == .web {
                    DebugMenuButton()
                }
            #endif
        }
        .animation(nil, value: sceneModel.surface)
        .sheet(isPresented: $sceneModel.isShowingSettings) {
            ServerSettingsView()
                .environment(appModel)
        }
        .environment(sceneModel)
        .onAppear {
            sceneModel.attach(to: appModel)
            sceneModel.setActive(true)
            // Preserve the existing DEBUG `-surface paper` smoke path while
            // making the actual surface state scene-local.
            if appModel.consumeInitialPaperSurface() {
                sceneModel.openPaper(target: appModel.paperTarget)
            }
        }
        .onDisappear { sceneModel.detach() }
        .onChange(of: scenePhase) { _, phase in
            sceneModel.setActive(phase == .active)
            if phase != .active { appModel.paperController?.flushForBackground() }
        }
    }
}

/// Shown when the web app cannot be reached at all. Paper stays reachable from
/// here, which is the point: an offline launch must not trap the user.
struct WebLoadFailureView: View {
    @Environment(SceneModel.self) private var sceneModel
    let message: String

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "wifi.exclamationmark")
                .font(.system(size: 36, weight: .light))
                .symbolRenderingMode(.monochrome)
                .foregroundStyle(PaperTokens.textMuted)
                .accessibilityHidden(true)
            Text("Cannot reach EXSCALIA")
                .font(.title3)
                .foregroundStyle(PaperTokens.text)
            Text(message)
                .font(.callout)
                .foregroundStyle(PaperTokens.textMuted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)

            HStack(spacing: 12) {
                Button("Retry") {
                    NotificationCenter.default.post(name: .webShellReload, object: nil)
                }
                .accessibilityLabel("Retry loading EXSCALIA")

                Button("Server settings") {
                    sceneModel.isShowingSettings = true
                }
                .accessibilityLabel("Open server settings")

                Button("Open Paper") {
                    sceneModel.openPaper(target: PaperTarget())
                }
                .accessibilityLabel("Open Paper")
            }
            .buttonStyle(.bordered)
            .tint(PaperTokens.accent)
            .controlSize(.large)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PaperTokens.background)
    }
}

#if DEBUG
    /// Development-only way into Paper and settings before the web bridge
    /// exists. Compiled out of Release builds; the shipping route is the web
    /// app's own Paper link.
    struct DebugMenuButton: View {
        @Environment(SceneModel.self) private var sceneModel

        var body: some View {
            VStack {
                HStack {
                    Spacer()
                    Menu {
                        Button("Open Paper") { sceneModel.openPaper(target: PaperTarget()) }
                        Button("Server settings") { sceneModel.isShowingSettings = true }
                        Button("Reload web app") {
                            NotificationCenter.default.post(name: .webShellReload, object: nil)
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                            .font(.system(size: 18, weight: .regular))
                            .symbolRenderingMode(.monochrome)
                            .frame(width: 44, height: 44)
                            .contentShape(Rectangle())
                    }
                    .tint(PaperTokens.textMuted)
                    .accessibilityLabel("Shell menu")
                    .accessibilityHint("Development shortcuts to Paper and server settings")
                }
                Spacer()
            }
            .padding(.top, 2)
            .padding(.trailing, 2)
        }
    }
#endif
