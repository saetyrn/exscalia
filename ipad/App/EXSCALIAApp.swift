import SwiftUI

@main
struct EXSCALIAApp: App {
    @State private var model = AppModel()

    init() {
        ReleaseGateDiagnostics.runIfRequested()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
        }
        // WindowGroup gives every window its own RootView/SceneModel while
        // AppModel remains one shared storage and SyncEngine coordinator.
        .handlesExternalEvents(matching: ["paper", "paper/*"])
    }
}
