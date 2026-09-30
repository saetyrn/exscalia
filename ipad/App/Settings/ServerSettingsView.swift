import PaperCore
import PaperUI
import SwiftUI

/// Server + Paper preferences. Values live in `UserDefaults`; the two Paper
/// behaviour toggles and the language list are wired to storage now and read by
/// the canvas in M2.
struct ServerSettingsView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.dismiss) private var dismiss

    @State private var serverText = ""
    @State private var serverError: String?
    @State private var isConfirmingReset = false
    @State private var resetMessage: String?

    @AppStorage(ServerSettings.Key.drawWithFinger) private var drawWithFinger = false
    @AppStorage(ServerSettings.Key.useSystemToolPicker) private var useSystemToolPicker = false
    @AppStorage(ServerSettings.Key.recognitionLanguages) private var recognitionLanguages = "en-US"

    /// Languages `VNRecognizeTextRequest` is asked for in M5. Kept short and
    /// explicit rather than offering every tag the OS knows.
    private static let languageOptions: [(code: String, name: String)] = [
        ("en-US", "English"),
        ("de-DE", "German"),
        ("fr-FR", "French"),
        ("es-ES", "Spanish"),
        ("it-IT", "Italian"),
        ("pt-BR", "Portuguese"),
        ("nl-NL", "Dutch"),
    ]

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://notes.exscalia.art", text: $serverText)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                        .submitLabel(.done)
                        .onSubmit(applyServer)
                        .accessibilityLabel("Server address")
                        .accessibilityHint("The EXSCALIA server this iPad talks to")
                    if let serverError {
                        Text(serverError)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                    Button("Use this server", action: applyServer)
                        .disabled(ServerSettings.parse(serverText) == nil)
                        .accessibilityLabel("Use this server")
                } header: {
                    Text("Server")
                } footer: {
                    Text(
                        "Each server keeps its own notebooks on this iPad. Sign in happens in the web view; a plain http server has no session cookie, so it must run with authentication off."
                    )
                }

                Section("Paper") {
                    Toggle("Draw with finger", isOn: $drawWithFinger)
                        .accessibilityLabel("Draw with finger")
                        .accessibilityHint("When on, a finger draws and panning needs two fingers")
                    Toggle("Use system tool picker", isOn: $useSystemToolPicker)
                        .accessibilityLabel("Use system tool picker")
                        .accessibilityHint("Replaces the EXSCALIA tool capsule with the PencilKit palette")
                }
                .tint(PaperTokens.accent)

                Section {
                    ForEach(Self.languageOptions, id: \.code) { option in
                        Button {
                            toggleLanguage(option.code)
                        } label: {
                            HStack {
                                Text(option.name)
                                    .foregroundStyle(PaperTokens.text)
                                Spacer()
                                if selectedLanguages.contains(option.code) {
                                    Image(systemName: "checkmark")
                                        .symbolRenderingMode(.monochrome)
                                        .foregroundStyle(PaperTokens.accent)
                                }
                            }
                            .contentShape(Rectangle())
                        }
                        .accessibilityLabel(option.name)
                        .accessibilityAddTraits(
                            selectedLanguages.contains(option.code) ? [.isSelected] : []
                        )
                    }
                } header: {
                    Text("Handwriting recognition")
                } footer: {
                    Text("Recognition runs on this iPad. Nothing is sent anywhere for it.")
                }

                Section {
                    Button("Reset local Paper cache", role: .destructive) {
                        isConfirmingReset = true
                    }
                    .accessibilityLabel("Reset local Paper cache")
                    .accessibilityHint("Removes and rebuilds renders and search rows; ink and transcripts are kept")
                    if let resetMessage {
                        Text(resetMessage)
                            .font(.footnote)
                            .foregroundStyle(PaperTokens.textMuted)
                    }
                } footer: {
                    Text("Renders, PDF page caches and search rows for this server are removed and rebuilt; ink and transcripts are kept.")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .accessibilityLabel("Close settings")
                }
            }
            .confirmationDialog(
                "Reset the local Paper cache?", isPresented: $isConfirmingReset,
                titleVisibility: .visible
            ) {
                Button("Reset cache", role: .destructive, action: resetCache)
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Renders are redrawn from your ink and search rows from the saved transcripts.")
            }
            .onAppear { serverText = model.server?.originString ?? "" }
        }
    }

    private var selectedLanguages: [String] {
        recognitionLanguages.split(separator: ",").map(String.init)
    }

    private func toggleLanguage(_ code: String) {
        var languages = selectedLanguages
        if let index = languages.firstIndex(of: code) {
            // Never leave recognition with nothing to work with.
            guard languages.count > 1 else { return }
            languages.remove(at: index)
        } else {
            languages.append(code)
        }
        recognitionLanguages = languages.joined(separator: ",")
    }

    private func applyServer() {
        guard let settings = ServerSettings(urlString: serverText) else {
            serverError = "Enter an address such as notes.exscalia.art or http://127.0.0.1:8600"
            return
        }
        serverError = nil
        serverText = settings.originString
        model.setServer(settings)
        NotificationCenter.default.post(name: .webShellReload, object: nil)
    }

    /// Removes this server's renders, PDF page caches, covers and search rows
    /// through the Paper store, which then rebuilds them.
    private func resetCache() {
        guard let controller = model.paperController else {
            resetMessage = "Paper storage is unavailable."
            return
        }
        do {
            let removed = try controller.resetDerivedFiles()
            resetMessage = "Removed \(removed) derived \(removed == 1 ? "file" : "files")."
        } catch {
            resetMessage = "Could not clear the cache: \(error.localizedDescription)"
        }
    }
}
