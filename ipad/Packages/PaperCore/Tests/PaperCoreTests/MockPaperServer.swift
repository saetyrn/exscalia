import Foundation
import XCTest

@testable import PaperCore

/// An in-memory OMNIA server behind `URLProtocol`.
///
/// It keeps notebooks, pages and revisions, applies the same conflict rule the
/// real store applies (a source write whose `base_revision` is not current is
/// parked as a sibling and answered 409), and records every request, so a test
/// can assert both what the client sent and what the server ended up holding.
final class MockServerState: @unchecked Sendable {
    struct Page {
        var id: String
        var revision: Int
        var renderRevision: Int = 0
        var textRevision: Int = 0
        var seq: Int = 0
        var conflictOf: String?
        var template = "ruled"
        var width = 595.0
        var height = 842.0
        /// The preset the page is, as the real store keeps it beside the
        /// dimensions. `nil` is a page no client has named -- what an older
        /// server holds.
        var sizeName: String?
        var sizePoints: (width: Double, height: Double)?
        var drawing: Data?
        var render: Data?
        /// The `text` part of the last PUT that carried one -- what M5's
        /// derived-only upload is asserted against.
        var text: Data?
        /// Which parts the last PUT for this page carried, and the
        /// `for_revision` it stamped them with. A derived-only push is exactly
        /// a PUT whose parts are neither `page` nor `drawing`.
        var lastPutParts: Set<String> = []
        var lastPutForRevision: Int?
        var lastPutBaseRevision: Int?
        /// `pdf` and `objects` as the last source write sent them, which is
        /// where a page names its assets.
        var pdf: [String: Any]?
        var objects: [[String: Any]] = []
    }

    struct Notebook {
        var id: String
        var title: String
        var template = "ruled"
        var paperSize = "a4"
        var favorite = false
        var tags: [String] = []
        var cover: String?
        var order: [String] = []
        var pages: [String: Page] = [:]
        var seq: Int = 0
    }

    private let lock = NSLock()

    // Guarded by `lock`.
    private var notebooks: [String: Notebook] = [:]
    private var deletions: [(path: String, kind: String, seq: Int)] = []
    private var counter = 0
    private var recorded: [(method: String, path: String)] = []
    private var notModified: [String] = []
    /// Asset bytes by notebook path, then by the name the server gave them.
    private var assetFiles: [String: [String: Data]] = [:]
    private var uploads: [(notebook: String, filename: String, contentType: String?)] = []

    var epoch = "epoch-1"
    /// While positive, every request is answered with `failStatus` instead.
    var failuresRemaining = 0
    var failStatus = 500
    /// Narrow `failuresRemaining` to some requests: only a path containing
    /// `failPathsContaining`, or equal to `failPathEquals` (for `/api/paper`,
    /// which every other route starts with), fails and counts down. Both nil
    /// fails every request, as above.
    var failPathsContaining: String?
    var failPathEquals: String?
    /// One-shot: the next page PUT is answered 400, whatever it says.
    var rejectNextPut = false
    /// What `GET /api/auth/status` answers.
    var authRequired = false
    var authenticated = false

    // MARK: - Test helpers

    func reset() {
        lock.lock()
        defer { lock.unlock() }
        notebooks = [:]
        deletions = []
        counter = 0
        recorded = []
        epoch = "epoch-1"
        failuresRemaining = 0
        failStatus = 500
        failPathsContaining = nil
        failPathEquals = nil
        rejectNextPut = false
        authRequired = false
        authenticated = false
        notModified = []
        assetFiles = [:]
        uploads = []
    }

    var requests: [(method: String, path: String)] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    func paths(matching fragment: String) -> [String] {
        requests.filter { $0.path.contains(fragment) }.map(\.path)
    }

    /// Blob paths answered 304 because `If-None-Match` matched.
    var notModifiedPaths: [String] {
        lock.lock()
        defer { lock.unlock() }
        return notModified
    }

    /// The assets a notebook holds, by the name the server gave them.
    func assets(inNotebook path: String) -> [String: Data] {
        lock.lock()
        defer { lock.unlock() }
        return assetFiles[path] ?? [:]
    }

    /// Every asset upload, with the filename and content type it was sent under.
    var assetUploads: [(notebook: String, filename: String, contentType: String?)] {
        lock.lock()
        defer { lock.unlock() }
        return uploads
    }

    func seedAsset(notebook path: String, name: String, data: Data) {
        lock.lock()
        defer { lock.unlock() }
        assetFiles[path, default: [:]][name] = data
    }

    func withState<T>(_ body: (inout [String: Notebook]) -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body(&notebooks)
    }

    func seed(
        notebook path: String, id: String, title: String, paperSize: String = "a4", pages: [Page]
    ) {
        lock.lock()
        defer { lock.unlock() }
        counter += 1
        var record = Notebook(id: id, title: title, paperSize: paperSize, seq: counter)
        for page in pages {
            counter += 1
            var stored = page
            stored.seq = counter
            record.order.append(page.id)
            record.pages[page.id] = stored
        }
        record.cover = record.order.first
        notebooks[path] = record
    }

    /// Changes a page the way the server's own writers do: whatever `body`
    /// does, the page takes the next seq, so the change is on the feed.
    func updatePage(notebook path: String, id: String, _ body: (inout Page) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard var page = notebooks[path]?.pages[id] else { return }
        body(&page)
        counter += 1
        page.seq = counter
        notebooks[path]?.pages[id] = page
    }

    /// The same for a notebook's own metadata.
    func updateNotebook(path: String, _ body: (inout Notebook) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard var notebook = notebooks[path] else { return }
        body(&notebook)
        counter += 1
        notebook.seq = counter
        notebooks[path] = notebook
    }

    /// Somebody else changed a notebook: `body` edits it and it gets a fresh
    /// seq, so the next incremental feed lists it.
    func editNotebook(_ path: String, _ body: (inout Notebook) -> Void) {
        lock.lock()
        defer { lock.unlock() }
        guard var record = notebooks[path] else { return }
        body(&record)
        notebooks[path] = record
        restampNotebook(path)
    }

    /// What the backend's `_write_notebook` does on every manifest write --
    /// including `_insert_page` after a page create -- so a create comes back
    /// on the feed as a notebook change too, exactly as it does in production.
    /// Callers hold `lock`.
    private func restampNotebook(_ path: String) {
        counter += 1
        notebooks[path]?.seq = counter
    }

    func recordDeletion(path: String, kind: String) {
        lock.lock()
        defer { lock.unlock() }
        counter += 1
        deletions.append((path, kind, counter))
        if kind == "notebook" {
            notebooks.removeValue(forKey: path)
        } else if let separator = path.range(of: "/pages/") {
            let notebook = String(path[path.startIndex..<separator.lowerBound])
            let page = String(path[separator.upperBound...])
            notebooks[notebook]?.pages.removeValue(forKey: page)
            notebooks[notebook]?.order.removeAll { $0 == page }
        }
    }

    // MARK: - Routing

    func handle(_ request: URLRequest, body: Data) -> (Int, [String: String], Data) {
        let method = request.httpMethod ?? "GET"
        let path = request.url?.path(percentEncoded: false) ?? ""
        lock.lock()
        recorded.append((method, path))
        if failuresRemaining > 0, failureApplies(to: path) {
            failuresRemaining -= 1
            let status = failStatus
            lock.unlock()
            return (status, [:], Data(#"{"detail":"nope"}"#.utf8))
        }
        lock.unlock()

        let query = URLComponents(url: request.url ?? URL(fileURLWithPath: "/"), resolvingAgainstBaseURL: false)?
            .queryItems ?? []

        lock.lock()
        defer { lock.unlock() }

        switch true {
        case path == "/api/auth/status":
            return json(200, ["auth_required": authRequired, "authenticated": authenticated])
        case path == "/api/paper":
            return json(200, ["folders": [], "notebooks": notebooks.keys.sorted().map(summary)])
        case path == "/api/paper/changes":
            return changesFeed(query: query)
        case path == "/api/paper/trash":
            return json(200, ["items": []])
        case path == "/api/paper/notebooks" && method == "POST":
            return createNotebook(body)
        case path.hasPrefix("/api/paper/notebooks/"):
            return notebookRoute(method: method, path: path, body: body)
        case path.hasPrefix("/api/paper/pages/"):
            return pageRoute(method: method, path: path, request: request, body: body)
        case path.hasPrefix("/api/paper/blob/"):
            return blobRoute(path: path, request: request)
        case path.hasPrefix("/api/paper/history/"):
            return json(200, ["path": path, "versions": []])
        case path.hasPrefix("/api/paper/assets/"):
            return assetRoute(method: method, path: path, request: request, body: body)
        default:
            return json(404, ["detail": "no route for \(path)"])
        }
    }

    private func failureApplies(to path: String) -> Bool {
        if let failPathEquals, path != failPathEquals { return false }
        if let failPathsContaining, !path.contains(failPathsContaining) { return false }
        return true
    }

    // MARK: - Routes

    private func createNotebook(_ body: Data) -> (Int, [String: String], Data) {
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        let title = (object["title"] as? String) ?? "Untitled"
        let folder = (object["folder"] as? String) ?? ""
        var path = folder.isEmpty ? "paper/\(title).paper" : "paper/\(folder)/\(title).paper"
        var suffix = 1
        while notebooks[path] != nil {
            suffix += 1
            let stem = folder.isEmpty ? "paper/\(title)" : "paper/\(folder)/\(title)"
            path = "\(stem) \(suffix).paper"
        }
        counter += 1
        notebooks[path] = Notebook(
            id: (object["id"] as? String) ?? UUID().uuidString,
            title: title,
            template: (object["template"] as? String) ?? "ruled",
            paperSize: (object["paperSize"] as? String) ?? "a4",
            seq: counter)
        return json(200, manifest(path))
    }

    private func notebookRoute(method: String, path: String, body: Data) -> (
        Int, [String: String], Data
    ) {
        var rest = String(path.dropFirst("/api/paper/notebooks/".count))
        var action: String?
        for candidate in ["/rename", "/pages"] where rest.hasSuffix(candidate) {
            action = String(candidate.dropFirst())
            rest = String(rest.dropLast(candidate.count))
        }
        let notebookPath = rest
        guard notebooks[notebookPath] != nil else {
            return json(404, ["detail": "Notebook not found: \(notebookPath)"])
        }
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]

        switch (action, method) {
        case ("rename", "POST"):
            guard let destination = object["new_path"] as? String else {
                return json(400, ["detail": "new_path required"])
            }
            if notebooks[destination] != nil, destination != notebookPath {
                return json(409, ["detail": "Notebook already exists"])
            }
            // As the real store does: the title changes only when one is sent,
            // and a rename onto its own path is a retitle, not a delete.
            var record = notebooks.removeValue(forKey: notebookPath)
            if let title = object["title"] as? String, !title.isEmpty { record?.title = title }
            // A move re-stamps every page, so another device finds them at the
            // new path, and names them in the answer for the device that asked.
            var moved: [[String: Any]] = []
            if destination != notebookPath, var pages = record?.pages {
                for id in pages.keys.sorted() {
                    let previous = pages[id]?.seq ?? 0
                    counter += 1
                    pages[id]?.seq = counter
                    moved.append(["id": id, "seq": counter, "previous_seq": previous])
                }
                record?.pages = pages
            }
            counter += 1
            record?.seq = counter
            notebooks[destination] = record
            // The package moves whole, assets and all.
            if let assets = assetFiles.removeValue(forKey: notebookPath) {
                assetFiles[destination] = assets
            }
            var body = manifest(destination)
            if destination != notebookPath {
                deletions.append((notebookPath, "notebook", counter))
                body["moved_pages"] = moved
            }
            return json(200, body)

        case ("pages", "POST"):
            let id = (object["id"] as? String) ?? UUID().uuidString
            if notebooks[notebookPath]?.pages[id] != nil {
                return json(409, ["detail": "Page already exists"])
            }
            counter += 1
            var page = Page(id: id, revision: 1, seq: counter)
            // The real store's rule: a named size, else a string size, else the
            // notebook's preset when no size came at all.
            if let dimensions = object["size"] as? [String: Any] {
                page.width = (dimensions["width"] as? Double) ?? page.width
                page.height = (dimensions["height"] as? Double) ?? page.height
                page.sizeName = object["size_name"] as? String
            } else {
                let preset = (object["size"] as? String) ?? notebooks[notebookPath]?.paperSize ?? "a4"
                if let points = Self.presetPoints(preset) { (page.width, page.height) = points }
                page.sizeName = (object["size_name"] as? String) ?? preset
            }
            notebooks[notebookPath]?.pages[id] = page
            if let after = object["after"] as? String,
                let index = notebooks[notebookPath]?.order.firstIndex(of: after)
            {
                notebooks[notebookPath]?.order.insert(id, at: index + 1)
            } else {
                notebooks[notebookPath]?.order.append(id)
            }
            if notebooks[notebookPath]?.cover == nil { notebooks[notebookPath]?.cover = id }
            restampNotebook(notebookPath)
            return json(200, pageJSON(notebookPath, page))

        case (nil, "GET"):
            var body = manifest(notebookPath)
            body["pages_detail"] =
                (notebooks[notebookPath]?.order ?? []).compactMap { id in
                    (notebooks[notebookPath]?.pages[id]).map { pageJSON(notebookPath, $0) }
                }
            return json(200, body)

        case (nil, "PATCH"):
            counter += 1
            if let title = object["title"] as? String { notebooks[notebookPath]?.title = title }
            if let favorite = object["favorite"] as? Bool {
                notebooks[notebookPath]?.favorite = favorite
            }
            if let tags = object["tags"] as? [String] { notebooks[notebookPath]?.tags = tags }
            if let order = object["pages"] as? [String] {
                let known = Set(notebooks[notebookPath]?.pages.map(\.key) ?? [])
                var next = order.filter { known.contains($0) }
                for id in notebooks[notebookPath]?.order ?? [] where !next.contains(id) {
                    next.append(id)
                }
                notebooks[notebookPath]?.order = next
            }
            notebooks[notebookPath]?.seq = counter
            return json(200, manifest(notebookPath))

        case (nil, "DELETE"):
            counter += 1
            notebooks.removeValue(forKey: notebookPath)
            // The real store trashes the package whole, assets and all.
            assetFiles.removeValue(forKey: notebookPath)
            deletions.append((notebookPath, "notebook", counter))
            return json(200, ["id": UUID().uuidString, "kind": "notebook"])

        default:
            return json(405, ["detail": "method not allowed"])
        }
    }

    private func pageRoute(method: String, path: String, request: URLRequest, body: Data) -> (
        Int, [String: String], Data
    ) {
        let rest = String(path.dropFirst("/api/paper/pages/".count))
        guard let (notebookPath, pageID) = splitPage(rest) else {
            return json(400, ["detail": "bad page path"])
        }
        guard notebooks[notebookPath] != nil else {
            return json(404, ["detail": "Notebook not found"])
        }

        switch method {
        case "GET":
            guard let page = notebooks[notebookPath]?.pages[pageID] else {
                return json(404, ["detail": "Page not found"])
            }
            return json(200, pageJSON(notebookPath, page))

        case "DELETE":
            guard notebooks[notebookPath]?.pages[pageID] != nil else {
                return json(404, ["detail": "Page not found"])
            }
            counter += 1
            notebooks[notebookPath]?.pages.removeValue(forKey: pageID)
            notebooks[notebookPath]?.order.removeAll { $0 == pageID }
            deletions.append(("\(notebookPath)/pages/\(pageID)", "page", counter))
            return json(200, ["id": UUID().uuidString, "kind": "page"])

        case "PUT":
            return savePage(
                notebookPath: notebookPath, pageID: pageID,
                parts: MultipartParser.parse(
                    body: body,
                    contentType: request.value(forHTTPHeaderField: "Content-Type") ?? ""))

        default:
            return json(405, ["detail": "method not allowed"])
        }
    }

    private func savePage(
        notebookPath: String, pageID: String, parts: MultipartParser.Parsed
    ) -> (Int, [String: String], Data) {
        if rejectNextPut {
            rejectNextPut = false
            return json(400, ["detail": "that page is not acceptable"])
        }
        let base = Int(parts.fields["base_revision"] ?? "0") ?? 0
        let forRevision = parts.fields["for_revision"].flatMap(Int.init)
        let existing = notebooks[notebookPath]?.pages[pageID]
        let hasSource = parts.files["page"] != nil || parts.files["drawing"] != nil

        if hasSource, (base == 0 && existing != nil) || (base != 0 && base != (existing?.revision ?? 0)) {
            return conflict(notebookPath: notebookPath, pageID: pageID, parts: parts)
        }
        if !hasSource, existing == nil {
            return json(404, ["detail": "Page not found"])
        }

        counter += 1
        var page = existing ?? Page(id: pageID, revision: 0, seq: counter)
        if hasSource { page.revision += 1 }
        page.seq = counter
        page.lastPutParts = Set(parts.files.keys)
        page.lastPutForRevision = forRevision
        page.lastPutBaseRevision = base
        if let part = parts.files["page"],
            let patch = (try? JSONSerialization.jsonObject(with: part)) as? [String: Any]
        {
            if let size = patch["size"] as? [String: Any] {
                page.width = (size["width"] as? Double) ?? page.width
                page.height = (size["height"] as? Double) ?? page.height
            }
            if let name = patch["size_name"] as? String { page.sizeName = name }
            if let pdf = patch["pdf"] as? [String: Any] { page.pdf = pdf }
            if let objects = patch["objects"] as? [[String: Any]] { page.objects = objects }
            if let points = patch["size_points"] as? [String: Any],
                let width = points["width"] as? Double, let height = points["height"] as? Double
            {
                page.sizePoints = (width, height)
            }
        }
        if let drawing = parts.files["drawing"] { page.drawing = drawing }
        if let render = parts.files["render"] {
            page.render = render
            page.renderRevision = forRevision ?? page.revision
        }
        if let text = parts.files["text"] {
            page.text = text
            page.textRevision = forRevision ?? page.revision
        }
        notebooks[notebookPath]?.pages[pageID] = page
        if !(notebooks[notebookPath]?.order.contains(pageID) ?? true) {
            if let after = parts.fields["after"],
                let index = notebooks[notebookPath]?.order.firstIndex(of: after)
            {
                notebooks[notebookPath]?.order.insert(pageID, at: index + 1)
            } else {
                notebooks[notebookPath]?.order.append(pageID)
            }
            restampNotebook(notebookPath)
        }
        return json(
            200,
            [
                "page": pageJSON(notebookPath, page), "revision": page.revision,
                "stale": (forRevision ?? page.revision) < page.revision,
            ])
    }

    /// The frozen rule: the original is untouched, the incoming version becomes
    /// a sibling listed directly after it, and both come back in the body.
    private func conflict(notebookPath: String, pageID: String, parts: MultipartParser.Parsed) -> (
        Int, [String: String], Data
    ) {
        let base = pageID.components(separatedBy: ".conflict-").first ?? pageID
        let siblingID = "\(base).conflict-20260902120000"
        counter += 1
        var sibling = Page(id: siblingID, revision: 1, seq: counter, conflictOf: pageID)
        sibling.drawing = parts.files["drawing"]
        if let render = parts.files["render"] {
            sibling.render = render
            sibling.renderRevision = 1
        }
        if let text = parts.files["text"] {
            sibling.text = text
            sibling.textRevision = 1
        }
        notebooks[notebookPath]?.pages[siblingID] = sibling
        if let index = notebooks[notebookPath]?.order.firstIndex(of: pageID) {
            notebooks[notebookPath]?.order.insert(siblingID, at: index + 1)
        } else {
            notebooks[notebookPath]?.order.append(siblingID)
        }
        let original = notebooks[notebookPath]?.pages[pageID] ?? Page(id: pageID, revision: 1)
        return json(
            409,
            [
                "detail": "The page changed since this edit started",
                "conflict": [
                    "original": pageJSON(notebookPath, original),
                    "sibling": pageJSON(notebookPath, sibling),
                ],
            ])
    }

    private func blobRoute(path: String, request: URLRequest) -> (Int, [String: String], Data) {
        let rest = String(path.dropFirst("/api/paper/blob/".count))
        var components = rest.components(separatedBy: "/")
        guard let name = components.popLast(), let pageID = components.popLast() else {
            return json(400, ["detail": "bad blob path"])
        }
        let notebookPath = components.joined(separator: "/")
        guard let page = notebooks[notebookPath]?.pages[pageID] else {
            return json(404, ["detail": "Page not found"])
        }
        let data: Data?
        switch name {
        case "render.png": data = page.render
        case "text.json": data = page.text
        default: data = page.drawing
        }
        guard let data else { return json(404, ["detail": "Page file not found"]) }
        // The real server's ETag names the revision the file belongs to
        // (`blob_revision`): the ink's for the drawing, the derived part's own
        // for the render and the transcript.
        let revision: Int
        let contentType: String
        switch name {
        case "render.png": (revision, contentType) = (page.renderRevision, "image/png")
        case "text.json": (revision, contentType) = (page.textRevision, "application/json")
        default: (revision, contentType) = (page.revision, "application/octet-stream")
        }
        let etag = "\"\(pageID)-\(name)-\(revision)\""
        if request.value(forHTTPHeaderField: "If-None-Match") == etag {
            notModified.append(path)
            return (304, ["ETag": etag], Data())
        }
        return (200, ["Content-Type": contentType, "ETag": etag], data)
    }

    /// `POST /api/paper/assets/<notebook>` stores the `file` part under the
    /// real store's name for it; `GET .../<notebook>/<filename>` reads it back.
    private func assetRoute(method: String, path: String, request: URLRequest, body: Data) -> (
        Int, [String: String], Data
    ) {
        let rest = String(path.dropFirst("/api/paper/assets/".count))
        if method == "POST" {
            guard notebooks[rest] != nil else {
                return json(404, ["detail": "Notebook not found: \(rest)"])
            }
            let parts = MultipartParser.parse(
                body: body, contentType: request.value(forHTTPHeaderField: "Content-Type") ?? "")
            let filename = parts.filenames["file"] ?? ""
            uploads.append((rest, filename, parts.contentTypes["file"]))
            let suffix = Self.pathlibSuffix(filename).lowercased()
            guard [".png", ".jpg", ".jpeg", ".webp", ".pdf", ".heic"].contains(suffix) else {
                let named = suffix.isEmpty ? "none" : suffix
                return json(400, ["detail": "Unsupported asset type: \(named)"])
            }
            guard let data = parts.files["file"], !data.isEmpty else {
                return json(400, ["detail": "Empty file"])
            }
            let name = "\(Self.serverSafeStem(filename))-\(AssetStore.hash(data))\(suffix)"
            assetFiles[rest, default: [:]][name] = data
            return json(
                200,
                [
                    "path": "\(rest)/assets/\(name)", "name": name, "size": data.count,
                    "url": "/api/paper/assets/\(rest)/\(name)",
                ])
        }
        var components = rest.components(separatedBy: "/")
        guard method == "GET", let filename = components.popLast() else {
            return json(405, ["detail": "method not allowed"])
        }
        guard let data = assetFiles[components.joined(separator: "/")]?[filename] else {
            return json(404, ["detail": "Asset not found: \(filename)"])
        }
        return (200, ["Content-Type": "application/octet-stream"], data)
    }

    /// `safe_stem` from backend/app/vault/notes.py, written from the Python
    /// rather than borrowed from `AssetStore`, so a test compares two spellings.
    static func serverSafeStem(_ filename: String) -> String {
        let stem = pathlibStem(filename)
        let cleaned = stem.replacingOccurrences(
            of: "[^A-Za-z0-9._-]+", with: "-", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: "-._"))
        let cut = String(String.UnicodeScalarView(cleaned.unicodeScalars.prefix(60)))
        return (cut.isEmpty ? "file" : cut).lowercased()
    }

    private static func pathlibName(_ filename: String) -> String {
        filename.split(separator: "/").last { $0 != "." }.map(String.init) ?? ""
    }

    private static func pathlibStem(_ filename: String) -> String {
        let name = pathlibName(filename)
        guard let dot = name.lastIndex(of: "."), dot > name.startIndex,
            name.index(after: dot) < name.endIndex
        else { return name }
        return String(name[..<dot])
    }

    private static func pathlibSuffix(_ filename: String) -> String {
        let name = pathlibName(filename)
        guard let dot = name.lastIndex(of: "."), dot > name.startIndex,
            name.index(after: dot) < name.endIndex
        else { return "" }
        return String(name[dot...])
    }

    private func changesFeed(query: [URLQueryItem]) -> (Int, [String: String], Data) {
        let since = Int(query.first { $0.name == "since" }?.value ?? "0") ?? 0
        let cursorEpoch = query.first { $0.name == "epoch" }?.value
        let full = since <= 0 || (cursorEpoch != nil && cursorEpoch != epoch)

        var notebookRows: [[String: Any]] = []
        var pageRows: [[String: Any]] = []
        for path in notebooks.keys.sorted() {
            guard let notebook = notebooks[path] else { continue }
            if full || notebook.seq > since { notebookRows.append(summary(path)) }
            for id in notebook.order {
                guard let page = notebook.pages[id] else { continue }
                guard full || page.seq > since else { continue }
                pageRows.append([
                    "path": "\(path)/pages/\(id)",
                    "notebook": path,
                    "id": id,
                    "revision": page.revision,
                    "render_revision": page.renderRevision,
                    "text_revision": page.textRevision,
                    "modified": "2026-09-02T10:00:00.123456Z",
                    "seq": page.seq,
                ])
            }
        }
        let deleted =
            full
            ? []
            : deletions.filter { $0.seq > since }.map {
                ["path": $0.path, "kind": $0.kind, "seq": $0.seq] as [String: Any]
            }
        return json(
            200,
            [
                "epoch": epoch, "seq": counter, "full": full, "notebooks": notebookRows,
                "pages": pageRows.sorted { ($0["seq"] as? Int ?? 0) < ($1["seq"] as? Int ?? 0) },
                "deleted": deleted,
            ])
    }

    // MARK: - Encoding

    private func summary(_ path: String) -> [String: Any] {
        guard let notebook = notebooks[path] else { return ["path": path] }
        return [
            "path": path, "folder": (path as NSString).deletingLastPathComponent,
            "id": notebook.id, "title": notebook.title,
            "created": "2026-09-02T10:00:00.123456Z",
            "modified": "2026-09-02T10:00:00.123456Z",
            "template": notebook.template, "paperSize": notebook.paperSize,
            "tags": notebook.tags, "favorite": notebook.favorite,
            "cover": notebook.cover as Any, "cover_url": NSNull(),
            "page_count": notebook.order.count, "seq": notebook.seq,
        ]
    }

    private func manifest(_ path: String) -> [String: Any] {
        guard let notebook = notebooks[path] else { return ["path": path] }
        return [
            "path": path, "id": notebook.id, "title": notebook.title,
            "created": "2026-09-02T10:00:00.123456Z",
            "modified": "2026-09-02T10:00:00.123456Z",
            "template": notebook.template, "paperSize": notebook.paperSize,
            "cover": notebook.cover as Any, "tags": notebook.tags,
            "favorite": notebook.favorite, "pages": notebook.order, "seq": notebook.seq,
        ]
    }

    /// The real store's `PAPER_SIZES`, matched case-insensitively.
    private static func presetPoints(_ name: String) -> (Double, Double)? {
        switch name.lowercased() {
        case "a4": return (595, 842)
        case "letter": return (612, 792)
        case "ipad", "infinite": return (1024, 1366)
        default: return nil
        }
    }

    private func pageJSON(_ notebookPath: String, _ page: Page) -> [String: Any] {
        let directory = "\(notebookPath)/pages/\(page.id)"
        var body: [String: Any] = [
            "id": page.id, "path": directory, "notebook": notebookPath,
            "template": page.template,
            "size": ["width": page.width, "height": page.height],
            "created": "2026-09-02T10:00:00.123456Z",
            "modified": "2026-09-02T10:00:00.123456Z",
            "revision": page.revision, "render_revision": page.renderRevision,
            "text_revision": page.textRevision, "objects": page.objects,
            "pdf": page.pdf ?? NSNull(), "conflict_of": page.conflictOf as Any, "seq": page.seq,
            "drawing_url": page.drawing == nil
                ? NSNull() : "/api/paper/blob/\(directory)/drawing.pkdrawing?r=\(page.revision)",
            "render_url": page.render == nil
                ? NSNull() : "/api/paper/blob/\(directory)/render.png?r=\(page.renderRevision)",
            "text_url": page.text == nil
                ? NSNull() : "/api/paper/blob/\(directory)/text.json?r=\(page.textRevision)",
        ]
        if let name = page.sizeName { body["size_name"] = name }
        if let points = page.sizePoints {
            body["size_points"] = ["width": points.width, "height": points.height]
        }
        return body
    }

    private func json(_ status: Int, _ object: Any) -> (Int, [String: String], Data) {
        let data =
            (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
        return (status, ["Content-Type": "application/json"], data)
    }

    private func splitPage(_ rest: String) -> (String, String)? {
        var components = rest.components(separatedBy: "/")
        guard let pageID = components.popLast(), !components.isEmpty else { return nil }
        return (components.joined(separator: "/"), pageID)
    }
}

/// Reads a `multipart/form-data` body back apart, so the mock can behave like
/// the real endpoint and a test can assert what was actually sent.
enum MultipartParser {
    struct Parsed {
        var fields: [String: String] = [:]
        var files: [String: Data] = [:]
        var filenames: [String: String] = [:]
        var contentTypes: [String: String] = [:]
    }

    static func parse(body: Data, contentType: String) -> Parsed {
        var parsed = Parsed()
        guard let range = contentType.range(of: "boundary=") else { return parsed }
        let boundary = String(contentType[range.upperBound...])
        let separator = Data("--\(boundary)".utf8)
        for section in split(body, on: separator) {
            guard let headerEnd = section.range(of: Data("\r\n\r\n".utf8)) else { continue }
            let header = String(decoding: section[section.startIndex..<headerEnd.lowerBound], as: UTF8.self)
            var content = section[headerEnd.upperBound...]
            // Exactly one CRLF sits before the next boundary. The part's own
            // bytes may end in one too -- a PDF ends in a newline.
            if content.suffix(2) == Data("\r\n".utf8) { content = content.dropLast(2) }
            guard let name = value(of: "name", in: header) else { continue }
            if let filename = value(of: "filename", in: header) {
                parsed.files[name] = Data(content)
                parsed.filenames[name] = filename
                if let line = header.split(separator: "\r\n").first(where: {
                    $0.lowercased().hasPrefix("content-type:")
                }) {
                    parsed.contentTypes[name] = line.dropFirst("content-type:".count)
                        .trimmingCharacters(in: .whitespaces)
                }
            } else {
                parsed.fields[name] = String(decoding: content, as: UTF8.self)
            }
        }
        return parsed
    }

    private static func value(of key: String, in header: String) -> String? {
        guard let start = header.range(of: "\(key)=\"") else { return nil }
        let rest = header[start.upperBound...]
        guard let end = rest.firstIndex(of: "\"") else { return nil }
        return String(rest[rest.startIndex..<end])
    }

    private static func split(_ data: Data, on separator: Data) -> [Data] {
        var pieces: [Data] = []
        var cursor = data.startIndex
        while let range = data[cursor...].range(of: separator) {
            if range.lowerBound > cursor { pieces.append(Data(data[cursor..<range.lowerBound])) }
            cursor = range.upperBound
        }
        // Everything before the first boundary is the preamble; the final "--"
        // tail is not a section either. Both are dropped by the header check.
        return pieces
    }
}

/// The `URLProtocol` face of `MockServerState`.
final class MockPaperServer: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static let state = MockServerState()

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.protocolClasses = [MockPaperServer.self]
        return URLSession(configuration: configuration)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body = request.httpBody ?? Self.readStream(request.httpBodyStream)
        let (status, headers, data) = Self.state.handle(request, body: body)
        guard let url = request.url,
            let response = HTTPURLResponse(
                url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    /// `URLProtocol` hands a body over as a stream, not as `httpBody`.
    private static func readStream(_ stream: InputStream?) -> Data {
        guard let stream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 64 * 1024)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(contentsOf: buffer[0..<read])
        }
        return data
    }
}
