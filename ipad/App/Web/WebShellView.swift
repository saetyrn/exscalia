import SwiftUI

/// SwiftUI face of `WebShellController`.
struct WebShellView: UIViewControllerRepresentable {
    @Environment(AppModel.self) private var appModel
    @Environment(SceneModel.self) private var sceneModel

    func makeUIViewController(context: Context) -> WebShellController {
        WebShellController(model: appModel, scene: sceneModel)
    }

    func updateUIViewController(_ controller: WebShellController, context: Context) {
        controller.loadHome(force: false)
        if let path = sceneModel.consumeWebNavigation() {
            controller.navigate(to: path)
        }
    }
}
