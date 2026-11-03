import AppIntents
import Foundation

/// Creates a page in the most recently opened local notebook and opens the
/// native Paper surface. The action intentionally has no web URL: shortcuts
/// and Siri must remain useful when the web shell is offline.
struct NewPaperPageIntent: AppIntent {
    static let title: LocalizedStringResource = "New Paper page"
    static let description = IntentDescription(
        "Add a page to the most recently opened Paper notebook and open it for writing."
    )
    static let openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        await MainActor.run { AppModel.shared?.performNewPaperPage() }
        return .result()
    }
}

/// Opens the last Paper notebook at its locally remembered page and zoom.
/// Storage and sync are owned by AppModel; an intent only asks the foreground
/// scene to navigate there.
struct OpenLastPaperNotebookIntent: AppIntent {
    static let title: LocalizedStringResource = "Open last Paper notebook"
    static let description = IntentDescription(
        "Open the most recently opened Paper notebook in EXSCALIA."
    )
    static let openAppWhenRun = true

    func perform() async throws -> some IntentResult {
        await MainActor.run { AppModel.shared?.performOpenLastNotebook() }
        return .result()
    }
}

struct EXSCALIAAppShortcuts: AppShortcutsProvider {
    static let shortcutTitles = ["New Paper page", "Open last notebook"]

    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: NewPaperPageIntent(),
            phrases: [
                "New Paper page in \(.applicationName)",
                "Add a Paper page in \(.applicationName)",
            ],
            shortTitle: "New Paper page",
            systemImageName: "square.and.pencil"
        )
        AppShortcut(
            intent: OpenLastPaperNotebookIntent(),
            phrases: [
                "Open my last Paper notebook in \(.applicationName)",
                "Open last Paper notebook in \(.applicationName)",
            ],
            shortTitle: "Open last notebook",
            systemImageName: "book"
        )
    }
}
