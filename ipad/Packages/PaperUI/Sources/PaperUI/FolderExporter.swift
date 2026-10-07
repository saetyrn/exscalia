import Foundation
import PaperCore

/// Export of a folder: one PDF per document, in directories that keep the
/// folder's own subfolders, zipped with `NSFileCoordinator(.forUploading)` --
/// the system's own archiver, so nothing is added to the app for it. A
/// whiteboard's PDF has one page per board.
enum FolderExporter {
    /// The folder as `<name>.zip` in a temporary directory, or nil when
    /// nothing could be written. Unsaved ink is flushed first; the PDFs are
    /// made off the main actor.
    @MainActor
    static func export(_ folder: PaperFolder, controller: PaperController) async -> URL? {
        controller.autosave.flushAll()
        let store = controller.store
        let folders = controller.libraryFolders.filter {
            PaperController.isInside($0.path, folder.path)
        }
        let notebooks = controller.notebooks.filter {
            PaperController.isInside($0.path, folder.path)
        }
        return await Task.detached(priority: .userInitiated) {
            guard
                let staged = stage(
                    folder: folder, folders: folders, notebooks: notebooks,
                    pdf: { notebook in
                        try? PDFExporter.pdf(PDFExporter.jobs(forNotebook: notebook, store: store))
                    })
            else { return nil }
            defer { try? FileManager.default.removeItem(at: staged.deletingLastPathComponent()) }
            return zip(staged)
        }.value
    }

    /// Writes the tree into a fresh temporary directory and returns its top
    /// folder: `<name>/<subfolder>/…/<title>.pdf`, every name made safe for a
    /// file system, and a title that appears twice in one folder numbered.
    /// A document whose PDF cannot be made is left out.
    nonisolated static func stage(
        folder: PaperFolder, folders: [PaperFolder], notebooks: [Notebook],
        pdf: (Notebook) -> Data?
    ) -> URL? {
        let manager = FileManager.default
        let base = manager.temporaryDirectory
            .appending(path: "PaperFolderExport-\(UUID().uuidString)", directoryHint: .isDirectory)
        let top = base.appending(path: safeName(folder.name, fallback: "Folder"), directoryHint: .isDirectory)
        do {
            try manager.createDirectory(at: top, withIntermediateDirectories: true)
            for sub in folders where sub.path != folder.path {
                try manager.createDirectory(
                    at: directory(for: sub.path, under: folder, folders: folders, top: top),
                    withIntermediateDirectories: true)
            }
            for notebook in notebooks.sorted(by: { $0.path < $1.path }) {
                guard let data = pdf(notebook) else { continue }
                let directory = directory(
                    for: notebook.folderPath, under: folder, folders: folders, top: top)
                try manager.createDirectory(at: directory, withIntermediateDirectories: true)
                try data.write(
                    to: freeFile(named: safeName(notebook.title, fallback: "Untitled"), in: directory),
                    options: .atomic)
            }
        } catch {
            try? manager.removeItem(at: base)
            return nil
        }
        return top
    }

    /// The staged directory of the folder at `path`, named by each folder's
    /// name rather than its directory's slug.
    private nonisolated static func directory(
        for path: String, under folder: PaperFolder, folders: [PaperFolder], top: URL
    ) -> URL {
        guard path != folder.path, path.hasPrefix(folder.path + "/") else { return top }
        var url = top
        var current = folder.path
        for component in path.dropFirst(folder.path.count + 1).split(separator: "/") {
            current += "/" + component
            let name = folders.first { $0.path == current }?.name ?? String(component)
            url = url.appending(path: safeName(name, fallback: String(component)), directoryHint: .isDirectory)
        }
        return url
    }

    private nonisolated static func freeFile(named stem: String, in directory: URL) -> URL {
        let manager = FileManager.default
        var url = directory.appending(path: "\(stem).pdf", directoryHint: .notDirectory)
        var number = 2
        while manager.fileExists(atPath: url.path(percentEncoded: false)) {
            url = directory.appending(path: "\(stem) \(number).pdf", directoryHint: .notDirectory)
            number += 1
        }
        return url
    }

    nonisolated static func safeName(_ name: String, fallback: String) -> String {
        let slug = PaperPaths.slug(name)
        return slug.isEmpty ? fallback : slug
    }

    /// `directory` as a ZIP beside it, through the file coordinator's
    /// "for uploading" read, which hands over a zipped copy.
    nonisolated static func zip(_ directory: URL) -> URL? {
        let manager = FileManager.default
        let outbox = manager.temporaryDirectory.appending(path: "PaperExport", directoryHint: .isDirectory)
        try? manager.createDirectory(at: outbox, withIntermediateDirectories: true)
        let destination = outbox.appending(
            path: "\(directory.lastPathComponent).zip", directoryHint: .notDirectory)
        var coordinationError: NSError?
        var copied = false
        NSFileCoordinator().coordinate(
            readingItemAt: directory, options: .forUploading, error: &coordinationError
        ) { zipped in
            try? manager.removeItem(at: destination)
            copied = (try? manager.copyItem(at: zipped, to: destination)) != nil
        }
        return coordinationError == nil && copied ? destination : nil
    }
}
