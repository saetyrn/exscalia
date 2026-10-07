import PaperCore
import SwiftUI

/// Raw recognition is evidence and remains read-only. The editable value is a
/// separate user-authored layer, so rerunning OCR or comparing candidates can
/// never erase what the recognizer originally returned.
struct TranscriptCorrectionSheet: View {
    let text: PageText?
    let onSave: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var corrected: String

    init(text: PageText?, onSave: @escaping (String?) -> Void) {
        self.text = text
        self.onSave = onSave
        _corrected = State(initialValue: text?.preferredTranscript ?? "")
    }

    var body: some View {
        NavigationStack {
            Group {
                if let text {
                    Form {
                        Section("Corrected text") {
                            TextEditor(text: $corrected)
                                .frame(minHeight: 180)
                                .textInputAutocapitalization(.sentences)
                                .accessibilityLabel("Corrected recognized text")
                        }
                        Section("Original recognition") {
                            Text(text.rawTranscript.isEmpty ? "No text recognized" : text.rawTranscript)
                                .foregroundStyle(PaperTokens.textMuted)
                                .textSelection(.enabled)
                        }
                    }
                } else {
                    ContentUnavailableView(
                        "No recognized text",
                        systemImage: "text.viewfinder",
                        description: Text("Write on the page and wait for on-device recognition."))
                }
            }
            .navigationTitle("Correct recognition")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                if text != nil {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Use original") {
                            onSave(nil)
                            dismiss()
                        }
                    }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") {
                            onSave(corrected)
                            dismiss()
                        }
                    }
                }
            }
        }
    }
}
