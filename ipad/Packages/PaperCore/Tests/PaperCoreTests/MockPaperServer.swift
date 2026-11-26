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
        /// The area the render shows, as the last render that landed said.
        var renderRect: [String: Any]?
        /// The page part's own fields, kept as the real store keeps them: a
        /// board has its extent and never `size_points`.
        var title: String?
        var paperColor: String?
        var boardExtent: [String: Any]?
        var bookmarked = false
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
        var kind = "notebook"
        var orientation = "portrait"
        var paperColor = "white"
    }

    /// A folder's `folder.json`, keyed by its path in `folders`.
    struct Folder {
        var id: String
        var name: String
        var color = "blue"
        var icon: String?
        var favorite = false
        var created = "2026-09-02T10:00:00.123456Z"
        var modified = "2026-09-02T10:00:00.123456Z"
        var seq = 0
    }

    struct Deletion {
        var path: String
        var kind: String
        var seq: Int
        var id: String?
    }

    struct Move {
        var id: String
        var from: String
        var to: String
        var seq: Int
    }

    /// A folder in the Trash, with everything it held.
    struct TrashedFolder {
        var id: String
        var originalPath: String
        var folderID: String
        var folders: [String: Folder]
        var directories: Set<String>
        var notebooks: [String: Notebook]
    }

    private let lock = NSLock()

    // Guarded by `lock`.
    private var notebooks: [String: Notebook] = [:]
    private var deletions: [Deletion] = []
    private var counter = 0
    private var recorded: [(method: String, path: String)] = []
    private var bodies: [(method: String, path: String, body: Data)] = []
    /// Folder records by path. A directory with none is either the parent of
    /// a notebook or folder, or one of `directories`: what the real server
    /// meets before its boot pass writes records.
    private var folders: [String: Folder] = [:]
    private var directories: Set<String> = []
    private var moves: [Move] = []
    private var trashedFolders: [String: TrashedFolder] = [:]
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
        holdFragment = nil
        counter = 0
        recorded = []
        bodies = []
        folders = [:]
        directories = []
        moves = []
        trashedFolders = [:]
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

    /// The JSON bodies of the requests to paths containing `fragment`, in
    /// order, with their method and path.
    func jsonRequests(matching fragment: String) -> [(method: String, path: String, body: [String: Any])] {
        lock.lock()
        defer { lock.unlock() }
        return bodies.filter { $0.path.contains(fragment) }.map {
            ($0.method, $0.path,
             (try? JSONSerialization.jsonObject(with: $0.body)) as? [String: Any] ?? [:])
        }
    }

    // MARK: - Holding a request

    private var holdFragment: String?
    private var holding = false
    private let holdGate = DispatchSemaphore(value: 0)

    /// The next request whose path contains `fragment` is answered, and the
    /// answer then held back until `releaseHeld()`: a request that is out
    /// while the test does something else.
    func holdNext(pathContaining fragment: String) {
        lock.lock()
        holdFragment = fragment
        lock.unlock()
    }

    var isHolding: Bool {
        lock.lock()
        defer { lock.unlock() }
        return holding
    }

    func waitUntilHeld() async throws {
        for _ in 0..<500 where !isHolding { try await Task.sleep(nanoseconds: 10_000_000) }
        XCTAssertTrue(isHolding, "nothing was held")
    }

    /// Lets the held answer go; nothing when nothing is held.
    func releaseHeld() {
        lock.lock()
        defer { lock.unlock() }
        if holding { holdGate.signal() }
    }

    /// Called by the protocol once the answer is ready.
    fileprivate func holdIfAsked(_ path: String) {
        lock.lock()
        guard let fragment = holdFragment, path.contains(fragment) else {
            lock.unlock()
            return
        }
        holdFragment = nil
        holding = true
        lock.unlock()
        holdGate.wait()
        lock.lock()
        holding = false
        lock.unlock()
    }

    // MARK: - Folder helpers

    func withFolders<T>(_ body: (inout [String: Folder]) -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body(&folders)
    }

    /// Every folder directory, records or not, sorted.
    var folderPaths: [String] {
        lock.lock()
        defer { lock.unlock() }
        return allDirectories()
    }

    var folderMoves: [Move] {
        lock.lock()
        defer { lock.unlock() }
        return moves
    }

    var tombstones: [Deletion] {
        lock.lock()
        defer { lock.unlock() }
        return deletions
    }

    /// A folder with its record, as another device or the server's boot
    /// pass wrote it.
    func seedFolder(
        path: String, id: String? = nil, name: String? = nil, color: String = "blue",
        icon: String? = nil, favorite: Bool = false
    ) {
        lock.lock()
        defer { lock.unlock() }
        counter += 1
        folders[path] = Folder(
            id: id ?? PaperFolderID.legacy(path: path),
            name: name ?? String(path.split(separator: "/").last ?? ""), color: color, icon: icon,
            favorite: favorite, seq: counter)
    }

    /// A directory with no record and nothing in it.
    func seedDirectory(_ path: String) {
        lock.lock()
        defer { lock.unlock() }
        directories.insert(path)
    }

    /// Sends a request as another client would, straight to the handler:
    /// how a test plays a device that has no store of its own.
    @discardableResult
    func call(_ method: String, _ path: String, json: [String: Any]? = nil) -> (Int, [String: Any]) {
        var request = URLRequest(url: URL(string: "https://paper.test\(path)")!)
        request.httpMethod = method
        let body = json.flatMap { try? JSONSerialization.data(withJSONObject: $0) } ?? Data()
        let (status, _, data) = handle(request, body: body)
        return (status, (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:])
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
        deletions.append(Deletion(path: path, kind: kind, seq: counter))
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
        bodies.append((method, path, body))
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
            return json(
                200,
                [
                    "folders": allDirectories(), "notebooks": notebooks.keys.sorted().map(summary),
                    "folder_records": folderListing(),
                ])
        case path == "/api/paper/changes":
            return changesFeed(query: query)
        case path == "/api/paper/trash":
            return json(200, ["items": trashedFolders.values.map(trashItem)])
        case path.hasPrefix("/api/paper/trash/") && path.hasSuffix("/restore") && method == "POST":
            let id = String(path.dropFirst("/api/paper/trash/".count).dropLast("/restore".count))
            return restoreFolder(id)
        case path == "/api/paper/folders" && method == "GET":
            return json(200, ["folders": folderListing()])
        case path == "/api/paper/folders" && method == "POST":
            return createFolder(body)
        case path.hasPrefix("/api/paper/folders/"):
            return folderRoute(method: method, path: path, body: body)
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

    /// The real store's rules: the folder as the disk spells it, a name no
    /// sibling folder or notebook holds after case folding, missing parents
    /// made with records at the notebook's seq, and their tombstones cleared.
    private func createNotebook(_ body: Data) -> (Int, [String: String], Data) {
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        let title = (object["title"] as? String) ?? "Untitled"
        guard let parent = folderPath(object["folder"] as? String).map(canonical) else {
            return json(400, ["detail": "Invalid folder path"])
        }
        let path = "\(parent)/\(freeEntryName(parent, PaperPaths.slug(title))).paper"
        counter += 1
        let kind = (object["kind"] as? String) ?? "notebook"
        notebooks[path] = Notebook(
            id: (object["id"] as? String) ?? UUID().uuidString,
            title: title,
            template: (object["template"] as? String) ?? "ruled",
            paperSize: kind == "whiteboard" ? "board" : (object["paperSize"] as? String) ?? "a4",
            seq: counter, kind: kind,
            orientation: (object["orientation"] as? String) ?? "portrait",
            paperColor: (object["paperColor"] as? String) ?? "white")
        ensureAncestorRecords(path, seq: counter)
        // The real store's `clear_tombstone`: a recreate must not leave the
        // feed reporting a delete.
        deletions.removeAll { $0.path == path }
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
            // The real store's rule: a name any other entry beside it holds
            // -- a folder by its name, a notebook by its name without
            // `.paper` -- after case folding, unless it is only a change of
            // case of this one.
            let caseOnly = fold(destination) == fold(notebookPath)
            if !caseOnly, destination != notebookPath {
                if notebooks[destination] != nil {
                    return json(409, ["detail": "Notebook already exists"])
                }
                let stem = { (path: String) in self.fold(String(self.leafOf(path).dropLast(".paper".count))) }
                let wanted = stem(destination)
                let parent = parentOf(destination)
                if folderChildren(parent).contains(where: { fold($0) == wanted })
                    || notebooks.keys.contains(where: {
                        $0 != notebookPath && parentOf($0) == parent && stem($0) == wanted
                    })
                {
                    return json(409, ["detail": "Notebook already exists"])
                }
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
            ensureAncestorRecords(destination, seq: counter)
            // The package moves whole, assets and all.
            if let assets = assetFiles.removeValue(forKey: notebookPath) {
                assetFiles[destination] = assets
            }
            var body = manifest(destination)
            if destination != notebookPath {
                deletions.append(Deletion(path: notebookPath, kind: "notebook", seq: counter))
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
            if let kind = object["kind"] as? String { notebooks[notebookPath]?.kind = kind }
            if let orientation = object["orientation"] as? String {
                notebooks[notebookPath]?.orientation = orientation
            }
            if let paperColor = object["paperColor"] as? String {
                notebooks[notebookPath]?.paperColor = paperColor
            }
            if let template = object["template"] as? String {
                notebooks[notebookPath]?.template = template
            }
            if let paperSize = object["paperSize"] as? String {
                notebooks[notebookPath]?.paperSize = paperSize
            }
            // A whiteboard's paper size is always `board`.
            if notebooks[notebookPath]?.kind == "whiteboard" {
                notebooks[notebookPath]?.paperSize = "board"
            }
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
            deletions.append(Deletion(path: notebookPath, kind: "notebook", seq: counter))
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
            deletions.append(
                Deletion(path: "\(notebookPath)/pages/\(pageID)", kind: "page", seq: counter))
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
        if let rect = parts.fields["render_rect"] {
            let object = (try? JSONSerialization.jsonObject(with: Data(rect.utf8))) as? [String: Any]
            let numbers = ["x", "y", "width", "height"].compactMap {
                (object?[$0] as? NSNumber)?.doubleValue
            }
            guard numbers.count == 4, numbers.allSatisfy(\.isFinite), numbers[2] > 0, numbers[3] > 0
            else { return json(400, ["detail": "Invalid render_rect"]) }
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
            if let pdf = patch["pdf"] as? [String: Any] {
                page.pdf = pdf
            } else if patch["pdf"] is NSNull {
                // The real store's rule: null clears it, absent keeps it.
                page.pdf = nil
            }
            if let objects = patch["objects"] as? [[String: Any]] { page.objects = objects }
            if let points = patch["size_points"] as? [String: Any],
                let width = points["width"] as? Double, let height = points["height"] as? Double
            {
                page.sizePoints = (width, height)
            }
            if let template = patch["template"] as? String { page.template = template }
            if patch.keys.contains("title") { page.title = patch["title"] as? String }
            if let color = patch["paper_color"] as? String { page.paperColor = color }
            if let bookmarked = patch["bookmarked"] as? Bool { page.bookmarked = bookmarked }
            if let extent = patch["board_extent"] as? [String: Any] { page.boardExtent = extent }
            if page.sizeName == "board" {
                // `_apply_page_patch`: a board drops the `size_points` it had
                // and always has an extent.
                page.sizePoints = nil
                if page.boardExtent == nil {
                    page.boardExtent = ["width": 16_384.0, "height": 16_384.0]
                }
            }
        }
        if let drawing = parts.files["drawing"] { page.drawing = drawing }
        if let render = parts.files["render"] {
            page.render = render
            page.renderRevision = forRevision ?? page.revision
            // Stored with the render it describes; a render without one
            // clears it.
            page.renderRect = parts.fields["render_rect"].flatMap {
                (try? JSONSerialization.jsonObject(with: Data($0.utf8))) as? [String: Any]
            }
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
            : deletions.filter { $0.seq > since }.map { row -> [String: Any] in
                var entry: [String: Any] = [
                    "path": row.path, "kind": row.kind, "seq": row.seq,
                    "at": "2026-09-02T10:00:00.123456Z",
                ]
                if let id = row.id { entry["id"] = id }
                return entry
            }
        let records = folderListing().filter { full || ($0["seq"] as? Int ?? 0) > since }
        let moved =
            full
            ? []
            : moves.filter { $0.seq > since }.sorted { $0.seq < $1.seq }.map {
                [
                    "id": $0.id, "from": $0.from, "to": $0.to, "seq": $0.seq,
                    "at": "2026-09-02T10:00:00.123456Z",
                ] as [String: Any]
            }
        return json(
            200,
            [
                "epoch": epoch, "seq": counter, "full": full, "notebooks": notebookRows,
                "pages": pageRows.sorted { ($0["seq"] as? Int ?? 0) < ($1["seq"] as? Int ?? 0) },
                "deleted": deleted, "folders": records, "folder_moves": moved,
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
            "kind": notebook.kind, "orientation": notebook.orientation,
            "paperColor": notebook.paperColor,
            "folder_id": recordID(of: parentOf(path)) as Any,
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
            "kind": notebook.kind, "orientation": notebook.orientation,
            "paperColor": notebook.paperColor,
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
        if let rect = page.renderRect { body["render_rect"] = rect }
        if let title = page.title { body["title"] = title }
        if let color = page.paperColor { body["paper_color"] = color }
        if let extent = page.boardExtent { body["board_extent"] = extent }
        if page.bookmarked { body["bookmarked"] = true }
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

// MARK: - Folders

/// The folder half of the real store (`backend/app/paper_folders.py`): records
/// by path, idempotent creates that merge by name, renames and moves as one
/// move record with nothing inside re-stamped, deletes that tombstone all
/// they held at one seq, restores beside a taken name. Every method here is
/// called with `lock` held.
extension MockServerState {
    fileprivate func folderPath(_ raw: String?) -> String? {
        let trimmed = (raw ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "/ "))
        guard !trimmed.isEmpty, trimmed != "paper" else { return "paper" }
        let rel = trimmed.hasPrefix("paper/") ? trimmed : "paper/\(trimmed)"
        let parts = rel.split(separator: "/", omittingEmptySubsequences: false)
        guard parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." && !$0.hasSuffix(".paper") })
        else { return nil }
        return rel
    }

    func parentOf(_ path: String) -> String {
        guard let slash = path.lastIndex(of: "/") else { return "paper" }
        return String(path[..<slash])
    }

    func leafOf(_ path: String) -> String { String(path.split(separator: "/").last ?? "") }

    func fold(_ name: String) -> String {
        name.precomposedStringWithCanonicalMapping.folding(options: [.caseInsensitive], locale: nil)
    }

    fileprivate func isUnder(_ path: String, _ prefix: String) -> Bool {
        path == prefix || path.hasPrefix(prefix + "/")
    }

    /// Every folder directory: records, bare directories, and the parents of
    /// every notebook and folder.
    func allDirectories() -> [String] {
        var all = Set(folders.keys).union(directories)
        for path in Array(notebooks.keys) + Array(folders.keys) + Array(directories) {
            var current = parentOf(path)
            while current != "paper", current.hasPrefix("paper/") {
                all.insert(current)
                current = parentOf(current)
            }
        }
        return all.filter { $0 != "paper" }.sorted()
    }

    func folderChildren(_ parent: String) -> [String] {
        allDirectories().filter { parentOf($0) == parent }.map(leafOf)
    }

    fileprivate func takenNames(_ parent: String, exclude: String? = nil) -> Set<String> {
        var names = folderChildren(parent)
        names += notebooks.keys.filter { parentOf($0) == parent }.map {
            String(leafOf($0).dropLast(".paper".count))
        }
        return Set(names.filter { $0 != exclude }.map(fold))
    }

    fileprivate func freeEntryName(_ parent: String, _ base: String, exclude: String? = nil) -> String {
        let taken = takenNames(parent, exclude: exclude)
        guard taken.contains(fold(base)) else { return base }
        var number = 2
        while taken.contains(fold("\(base) \(number)")) { number += 1 }
        return "\(base) \(number)"
    }

    /// `rel` spelled as the directories spell it, segment by segment.
    fileprivate func canonical(_ rel: String) -> String {
        let parts = rel.split(separator: "/").map(String.init)
        var out = [parts.first ?? "paper"]
        for (index, part) in parts.enumerated().dropFirst() {
            let candidates = folderChildren(out.joined(separator: "/"))
            if candidates.contains(part) {
                out.append(part)
            } else if let match = candidates.first(where: { fold($0) == fold(part) }) {
                out.append(match)
            } else {
                out += parts[index...]
                break
            }
        }
        return out.joined(separator: "/")
    }

    /// The stored record, or the legacy one a GET synthesises with seq 0.
    fileprivate func record(at path: String) -> Folder {
        folders[path] ?? Folder(id: PaperFolderID.legacy(path: path), name: leafOf(path))
    }

    func recordID(of path: String) -> String? {
        path == "paper" ? nil : record(at: path).id
    }

    fileprivate func recordJSON(_ path: String, _ folder: Folder) -> [String: Any] {
        [
            "id": folder.id, "name": folder.name, "color": folder.color,
            "icon": folder.icon as Any, "favorite": folder.favorite, "created": folder.created,
            "modified": folder.modified, "seq": folder.seq, "path": path, "parent": parentOf(path),
        ]
    }

    /// Every record, sorted by path; a second folder holding an id an
    /// earlier one holds is listed under its legacy id.
    func folderListing() -> [[String: Any]] {
        var seen: Set<String> = []
        return allDirectories().map { path in
            var folder = record(at: path)
            if seen.contains(folder.id) { folder.id = PaperFolderID.legacy(path: path) }
            seen.insert(folder.id)
            return recordJSON(path, folder)
        }
    }

    /// Records for every folder above `path` that has none, at `seq`; a
    /// directory made where a trashed folder was clears its tombstone.
    func ensureAncestorRecords(_ path: String, seq: Int) {
        var current = parentOf(path)
        var written: [String] = []
        while current != "paper", current.hasPrefix("paper/") {
            if folders[current] == nil {
                var folder = record(at: current)
                folder.seq = seq
                folders[current] = folder
                directories.remove(current)
                written.append(current)
            }
            current = parentOf(current)
        }
        clearFolderTombstones(written)
    }

    fileprivate func clearFolderTombstones(_ paths: [String]) {
        deletions.removeAll { $0.kind == "folder" && paths.contains($0.path) }
    }

    fileprivate func now() -> String { "2026-10-05T12:00:\(String(format: "%02d", counter % 60)).000000Z" }

    func createFolder(_ body: Data) -> (Int, [String: String], Data) {
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        let name = ((object["name"] as? String) ?? "").trimmingCharacters(in: .whitespaces)
        guard name.unicodeScalars.count <= 200 else { return json(400, ["detail": "Name too long"]) }
        let id = (object["id"] as? String) ?? PaperID.make()
        guard PaperFolderID.isValid(id) else { return json(400, ["detail": "Bad folder id"]) }
        let color = (object["color"] as? String) ?? "blue"
        guard FolderColor(rawValue: color) != nil else { return json(400, ["detail": "Bad colour"]) }
        let icon = (object["icon"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        if let icon, FolderIcon(wire: icon) == nil { return json(400, ["detail": "Bad icon"]) }
        guard let parent = folderPath(object["parent"] as? String).map(canonical) else {
            return json(400, ["detail": "Invalid folder path"])
        }
        let fresh = Folder(
            id: id, name: name.isEmpty ? "Untitled" : name, color: color, icon: icon,
            favorite: (object["favorite"] as? Bool) ?? false, created: now(), modified: now())

        // Idempotent by id, wherever the folder has gone since.
        if let found = folders.first(where: { $0.value.id == id }) {
            return json(200, recordJSON(found.key, found.value).merging(["existing": false]) { $1 })
        }
        let slug = PaperFolderID.folderSlug(fresh.name)
        if let match = folderChildren(parent).first(where: { fold($0) == fold(slug) }) {
            let rel = "\(parent)/\(match)"
            if let held = folders[rel], held.id != id {
                // Another device made it first: that one wins, unsuffixed.
                return json(200, recordJSON(rel, held).merging(["existing": true]) { $1 })
            }
            counter += 1
            var claimed = fresh
            claimed.seq = counter
            folders[rel] = claimed
            directories.remove(rel)
            clearFolderTombstones([rel])
            return json(200, recordJSON(rel, claimed).merging(["existing": false]) { $1 })
        }
        let rel = "\(parent)/\(freeEntryName(parent, slug))"
        guard rel.utf8.count <= 900 else { return json(400, ["detail": "Path too long"]) }
        counter += 1
        var created = fresh
        created.seq = counter
        folders[rel] = created
        ensureAncestorRecords(rel, seq: counter)
        clearFolderTombstones([rel])
        return json(200, recordJSON(rel, created).merging(["existing": false]) { $1 })
    }

    func folderRoute(method: String, path: String, body: Data) -> (Int, [String: String], Data) {
        var rest = String(path.dropFirst("/api/paper/folders/".count))
        let renaming = method == "POST" && rest.hasSuffix("/rename")
        if renaming { rest = String(rest.dropLast("/rename".count)) }
        guard let normalized = folderPath(rest), normalized != "paper" else {
            return json(400, ["detail": "A folder path is required"])
        }
        let src = canonical(normalized)
        guard allDirectories().contains(src) else {
            return json(404, ["detail": "Folder not found: \(src)"])
        }
        let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any] ?? [:]
        switch (renaming, method) {
        case (true, _): return renameFolder(src, object)
        case (false, "PATCH"): return patchFolder(src, object)
        case (false, "DELETE"): return deleteFolder(src)
        default: return json(405, ["detail": "method not allowed"])
        }
    }

    private func renameFolder(_ src: String, _ object: [String: Any]) -> (Int, [String: String], Data) {
        guard let wish = folderPath(object["new_path"] as? String), wish != "paper" else {
            return json(400, ["detail": "A folder path is required"])
        }
        let leaf = leafOf(wish)
        if fold(leaf) == "folder.json" || leaf.hasPrefix(".rename-") {
            return json(400, ["detail": "Invalid folder name"])
        }
        let title = (object["name"] as? String).map { $0.trimmingCharacters(in: .whitespaces) }
        let dstParent = canonical(parentOf(wish))
        if isUnder(fold(dstParent), fold(src)) {
            return json(400, ["detail": "A folder cannot move into itself"])
        }
        let hadRecord = folders[src] != nil
        var folder = record(at: src)
        if let title { folder.name = title.isEmpty ? "Untitled" : title }
        let sameParent = dstParent == parentOf(src)
        let dst = "\(dstParent)/\(freeEntryName(dstParent, leaf, exclude: sameParent ? leafOf(src) : nil))"
        if dst == src {
            guard title != nil, !(hadRecord && folders[src]?.name == folder.name) else {
                return json(200, ["folder": recordJSON(src, folder), "from": src, "path": src])
            }
            counter += 1
            folder.seq = counter
            folder.modified = now()
            folders[src] = folder
            return json(200, ["folder": recordJSON(src, folder), "from": src, "path": src])
        }
        counter += 1
        let seq = counter
        // Folders inside without a record keep the id they have now.
        for path in allDirectories() where isUnder(path, src) && path != src && folders[path] == nil {
            var inner = record(at: path)
            inner.seq = seq
            folders[path] = inner
        }
        func moved(_ path: String) -> String { dst + path.dropFirst(src.count) }
        for (path, value) in folders where isUnder(path, src) {
            folders.removeValue(forKey: path)
            folders[moved(path)] = value
        }
        directories = Set(directories.map { isUnder($0, src) ? moved($0) : $0 })
        for (path, value) in notebooks where isUnder(path, src) {
            notebooks.removeValue(forKey: path)
            notebooks[moved(path)] = value
        }
        for (path, value) in assetFiles where isUnder(path, src) {
            assetFiles.removeValue(forKey: path)
            assetFiles[moved(path)] = value
        }
        folder.seq = seq
        folder.modified = now()
        folders[dst] = folder
        if !sameParent { ensureAncestorRecords(dst, seq: seq) }
        moves.append(Move(id: folder.id, from: src, to: dst, seq: seq))
        return json(200, ["folder": recordJSON(dst, folder), "from": src, "path": dst])
    }

    private func patchFolder(_ rel: String, _ object: [String: Any]) -> (Int, [String: String], Data) {
        var folder = record(at: rel)
        var changed = folders[rel] == nil
        if let color = object["color"] as? String {
            guard FolderColor(rawValue: color) != nil else { return json(400, ["detail": "Bad colour"]) }
            changed = changed || color != folder.color
            folder.color = color
        }
        if object.keys.contains("icon") {
            let icon = (object["icon"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            if let icon, FolderIcon(wire: icon) == nil { return json(400, ["detail": "Bad icon"]) }
            changed = changed || icon != folder.icon
            folder.icon = icon
        }
        if let favorite = object["favorite"] as? Bool {
            changed = changed || favorite != folder.favorite
            folder.favorite = favorite
        }
        guard changed else { return json(200, recordJSON(rel, folder)) }
        counter += 1
        folder.seq = counter
        folder.modified = now()
        folders[rel] = folder
        directories.remove(rel)
        return json(200, recordJSON(rel, folder))
    }

    private func deleteFolder(_ rel: String) -> (Int, [String: String], Data) {
        counter += 1
        let seq = counter
        let inside = allDirectories().filter { isUnder($0, rel) }
        var held: [String: Folder] = [:]
        for path in inside {
            var folder = record(at: path)
            if folders[path] == nil { folder.seq = seq }
            held[path] = folder
            folders.removeValue(forKey: path)
        }
        let heldDirectories = directories.filter { isUnder($0, rel) }
        directories.subtract(heldDirectories)
        var heldNotebooks: [String: Notebook] = [:]
        for (path, notebook) in notebooks where isUnder(path, rel) {
            heldNotebooks[path] = notebook
            notebooks.removeValue(forKey: path)
            deletions.append(Deletion(path: path, kind: "notebook", seq: seq))
        }
        for path in inside {
            deletions.append(Deletion(path: path, kind: "folder", seq: seq, id: held[path]?.id))
        }
        let item = TrashedFolder(
            id: PaperID.make(), originalPath: rel, folderID: held[rel]?.id ?? "",
            folders: held, directories: heldDirectories, notebooks: heldNotebooks)
        trashedFolders[item.id] = item
        return json(200, trashItem(item))
    }

    func trashItem(_ item: TrashedFolder) -> [String: Any] {
        [
            "id": item.id, "kind": "paper_folder", "original_path": item.originalPath,
            "deleted_at": "2026-10-05T12:00:00.000000Z", "expires_at": "2026-11-04T12:00:00.000000Z",
            "note_count": 0, "folder_id": item.folderID, "notebook_count": item.notebooks.count,
        ]
    }

    /// Back where it was, or beside it as "<name> N"; everything inside is
    /// re-stamped, because devices threw their copies away.
    func restoreFolder(_ id: String) -> (Int, [String: String], Data) {
        guard let item = trashedFolders.removeValue(forKey: id) else {
            return json(404, ["detail": "Not in the trash"])
        }
        let parent = canonical(parentOf(item.originalPath))
        let target = "\(parent)/\(freeEntryName(parent, leafOf(item.originalPath)))"
        counter += 1
        ensureAncestorRecords(target, seq: counter)
        func moved(_ path: String) -> String { target + path.dropFirst(item.originalPath.count) }
        let live = Set(folders.values.map(\.id))
        for (path, var folder) in item.folders {
            counter += 1
            folder.seq = counter
            if live.contains(folder.id) { folder.id = PaperFolderID.legacy(path: moved(path)) }
            folders[moved(path)] = folder
        }
        for path in item.directories { directories.insert(moved(path)) }
        for (path, var notebook) in item.notebooks {
            for pageID in notebook.pages.keys {
                counter += 1
                notebook.pages[pageID]?.seq = counter
            }
            counter += 1
            notebook.seq = counter
            notebooks[moved(path)] = notebook
        }
        deletions.removeAll { isUnder($0.path, target) }
        return json(
            200,
            [
                "id": id, "kind": "paper_folder", "path": target, "notes": [] as [Any],
                "folder_id": folders[target]?.id ?? item.folderID,
            ])
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
        Self.state.holdIfAsked(request.url?.path(percentEncoded: false) ?? "")
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
