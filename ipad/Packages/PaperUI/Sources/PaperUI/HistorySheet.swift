import PaperCore
import PencilKit
import SwiftUI

/// Every version of one page that can still be got back.
///
/// Two lists, because there are honestly two: the server's git history, whose
/// grain is the ten-second autocommit debounce, and this iPad's own snapshot
/// ring underneath it, which keeps the last ten flushes of the page. The
/// research invariant is that a version is never silently destroyed, so both
/// are shown, both say when they are from, and restoring either is an ordinary
/// new version rather than a rewrite.
public struct HistorySheet: View {
    private let controller: PaperController
    private let notebookPath: String
    private let pageID: String
    private let pageNumber: Int
    private let onRestored: () -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var versions: [PageVersion] = []
    @State private var snapshots: [SnapshotRing.Snapshot] = []
    @State private var isLoading = true
    @State private var busySHA: String?
    @State private var failure: String?

    public init(
        controller: PaperController, notebookPath: String, pageID: String, pageNumber: Int,
        onRestored: @escaping () -> Void
    ) {
        self.controller = controller
        self.notebookPath = notebookPath
        self.pageID = pageID
        self.pageNumber = pageNumber
        self.onRestored = onRestored
    }

    public var body: some View {
        NavigationStack {
            List {
                if let failure {
                    Section {
                        Text(failure)
                            .font(.footnote)
                            .foregroundStyle(PaperTokens.textMuted)
                    }
                }
                Section("On this iPad") {
                    if snapshots.isEmpty {
                        emptyRow("No snapshots of this page yet")
                    } else {
                        ForEach(snapshots) { snapshot in
                            snapshotRow(snapshot)
                        }
                    }
                }
                Section("On the server") {
                    if isLoading {
                        emptyRow("Loading versions")
                    } else if versions.isEmpty {
                        emptyRow("No server versions of this page yet")
                    } else {
                        ForEach(versions) { version in
                            versionRow(version)
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("Page \(pageNumber) history")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                        .accessibilityHint("Closes the history without restoring anything")
                }
            }
        }
        .task { await load() }
    }

    // MARK: - Rows

    private func snapshotRow(_ snapshot: SnapshotRing.Snapshot) -> some View {
        HStack(spacing: 12) {
            SnapshotThumbnail(url: snapshot.url)
                .frame(width: 44, height: 60)
                .overlay(
                    RoundedRectangle(cornerRadius: 3).strokeBorder(PaperTokens.border, lineWidth: 1))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(SaveState.relative(since: snapshot.createdAt))
                    .font(.body)
                    .foregroundStyle(PaperTokens.text)
                Text(snapshot.createdAt.formatted(date: .abbreviated, time: .standard))
                    .font(.caption)
                    .foregroundStyle(PaperTokens.textMuted)
            }
            Spacer(minLength: 0)
            Button("Restore") {
                let restored = controller.restoreLocalSnapshot(
                    snapshot, notebookPath: notebookPath, pageID: pageID)
                if restored {
                    onRestored()
                    dismiss()
                } else {
                    failure = "That snapshot could not be read."
                }
            }
            .buttonStyle(.bordered)
            .tint(PaperTokens.accent)
            .accessibilityHint("Puts this version back as a new version")
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Snapshot from \(SaveState.relative(since: snapshot.createdAt))")
    }

    private func versionRow(_ version: PageVersion) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(version.message ?? "Version \(version.shortSHA ?? version.sha)")
                    .font(.body)
                    .foregroundStyle(PaperTokens.text)
                    .lineLimit(1)
                Text(
                    [version.shortSHA, version.committedAt.map(Self.readableDate)]
                        .compactMap { $0 }.joined(separator: " · ")
                )
                .font(.caption)
                .foregroundStyle(PaperTokens.textMuted)
                .monospacedDigit()
            }
            Spacer(minLength: 0)
            Button("Restore") {
                Task { await restore(version) }
            }
            .buttonStyle(.bordered)
            .tint(PaperTokens.accent)
            .disabled(busySHA != nil)
            .accessibilityHint("Brings this version back as a new version on the server")
        }
        .accessibilityElement(children: .contain)
    }

    private func emptyRow(_ text: String) -> some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(PaperTokens.textMuted)
    }

    // MARK: - Work

    private func load() async {
        snapshots = controller.localSnapshots(pageID: pageID)
        versions = await controller.serverHistory(notebookPath: notebookPath, pageID: pageID)
        isLoading = false
        if versions.isEmpty, controller.api == nil {
            failure = "No server is configured, so only this iPad's snapshots are listed."
        }
    }

    private func restore(_ version: PageVersion) async {
        busySHA = version.sha
        let restored = await controller.restoreServerVersion(
            notebookPath: notebookPath, pageID: pageID, sha: version.sha)
        busySHA = nil
        if restored {
            onRestored()
            dismiss()
        } else {
            failure = "That version could not be restored. The server may be unreachable."
        }
    }

    private static func readableDate(_ iso: String) -> String {
        guard let date = PaperDate.parse(iso) else { return iso }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
}

/// A snapshot drawn straight from its `.pkdrawing`.
///
/// Forced to a light appearance for the same reason every canvas is: PencilKit
/// renders black ink white under a dark system appearance, and a preview that
/// lies about what is on the page is worse than no preview.
struct SnapshotThumbnail: View {
    let url: URL

    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
            } else {
                Rectangle().fill(PaperTokens.paper)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .task { await render() }
    }

    private func render() async {
        let source = url
        let task = Task.detached(priority: .userInitiated) { () -> UIImage? in
            guard let data = try? Data(contentsOf: source),
                let drawing = try? PKDrawing(data: data)
            else { return nil }
            let bounds =
                drawing.bounds.isEmpty ? CGRect(x: 0, y: 0, width: 1, height: 1) : drawing.bounds
            var rendered: UIImage?
            UITraitCollection(userInterfaceStyle: .light).performAsCurrent {
                rendered = drawing.image(from: bounds, scale: 1)
            }
            return rendered
        }
        image = await task.value
    }
}
