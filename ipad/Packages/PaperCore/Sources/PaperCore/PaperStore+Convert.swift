import CoreGraphics
import Foundation
import GRDB
import ImageIO
import PDFKit
import PencilKit
import UniformTypeIdentifiers

#if canImport(Darwin)
    import Darwin
#endif

/// The points `convertToWhiteboard` passes, in the order it passes them. A
/// test stops it at one to see what an app killed there leaves behind.
enum WhiteboardConversionStep: Equatable, Sendable {
    /// Every page's ink is in the snapshot ring; nothing else has changed.
    case snapshotsTaken
    /// Every PDF page's picture is stored as an asset and queued for upload.
    case picturesStored
    /// The page at this position has been swapped for its board.
    case pageSwapped(Int)
    /// ... and its PUT queued.
    case pageJournalled(Int)
    /// The notebook's patch is queued; `notebook.json` is not written yet.
    case notebookJournalled
    /// `notebook.json` says whiteboard; the index rows are not written yet.
    case notebookWritten
}

/// What a test's stop looks like to the caller: a failure at that step.
struct WhiteboardConversionInterrupted: Error, Equatable {
    let step: WhiteboardConversionStep
}

extension PaperStore {
    /// The boards converting `notebookID` would make, measured from its pages
    /// and their ink, without changing anything.
    ///
    /// Throws what `convertToWhiteboard` would refuse with:
    /// `WhiteboardConversionError.tooLarge` for a page that needs a side
    /// longer than 32,768 points, `PaperStoreError.notFound` for a page whose
    /// files cannot be read or a PDF page whose document is not on this
    /// device, and PencilKit's error for ink that does not decode. Pages that
    /// are boards already plan nothing. Reads every drawing, so call it off
    /// the main actor.
    public func planWhiteboardConversion(notebookID: String) throws -> [BoardPlan] {
        let notebook = try notebook(id: notebookID)
        return try conversionWork(for: notebook).compactMap(\.plan)
    }

    /// Whether a whiteboard still holds a page that is not a board -- one a
    /// conversion did not reach, or another device's sheet kept as the
    /// original of a conflict. Convert to Whiteboard is offered on it, and
    /// converts just those pages.
    public func holdsSheet(notebookID: String) -> Bool {
        let count = try? database.queue.read { db in
            try Int.fetchOne(
                db,
                sql: "SELECT COUNT(*) FROM pages WHERE notebook_id = ? AND paper_size <> ?",
                arguments: [notebookID, PaperSize.board.rawValue])
        }
        return (count ?? 0) > 0
    }

    /// Finishes every conversion an interrupted run left: a notebook that
    /// holds a board is one whose conversion stopped before its
    /// `notebook.json` said whiteboard -- and before its patch was queued, so
    /// the server holds a notebook of boards and sheets that nothing else
    /// would ever finish. Each is converted again, which is idempotent: no
    /// board moves twice, and every board still owed a PUT gets one. Run at
    /// launch after the reconcile, with the sync engine held. Returns the
    /// ids finished; one that cannot be finished is left as it is.
    @discardableResult
    public func resumeInterruptedConversions() -> [String] {
        let marked = Set(markedConversions())
        let unfinished = Set(interruptedConversions())
        var finished: [String] = []
        for id in marked.union(unfinished).sorted() {
            if unfinished.contains(id), (try? convertToWhiteboard(notebookID: id)) != nil {
                finished.append(id)
            } else if !unfinished.contains(id) {
                // Marked, and holding no board: stopped before its first swap,
                // or after its last write. Nothing to finish.
                try? setSyncValue(nil, for: Self.conversionMark(id))
            }
        }
        return finished
    }

    /// The documents a conversion may have left half done, at once and from
    /// the index alone, so a launch can keep every editor off them before
    /// any window opens one: those whose conversion was marked as started
    /// and never finished, and the notebooks the index says hold a board.
    /// The index rows of a run killed half way still say sheet until the
    /// launch reconcile; the mark is what finds that one.
    public func conversionsToFinish() -> [String] {
        Set(interruptedConversions()).union(markedConversions()).sorted()
    }

    /// The notebooks that hold a board: conversions a kill left half done.
    /// One query, so the launch asks before it holds anything.
    public func interruptedConversions() -> [String] {
        (try? database.queue.read { db in
            try String.fetchAll(
                db,
                sql: """
                    SELECT DISTINCT n.id FROM notebooks n JOIN pages p ON p.notebook_id = n.id
                    WHERE n.kind = ? AND p.paper_size = ?
                    """,
                arguments: [DocumentKind.notebook.rawValue, PaperSize.board.rawValue])
        }) ?? []
    }

    /// Set in `sync_state` before a conversion's first swap and cleared
    /// after its last write.
    static func conversionMark(_ notebookID: String) -> String { "convert.started.\(notebookID)" }

    private func markedConversions() -> [String] {
        let prefix = Self.conversionMark("")
        let keys =
            (try? database.queue.read { db in
                try String.fetchAll(
                    db, sql: "SELECT key FROM sync_state WHERE substr(key, 1, ?) = ?",
                    arguments: [prefix.count, prefix])
            }) ?? []
        return keys.map { String($0.dropFirst(prefix.count)) }
    }

    /// The boards with no `render.png`, in these documents or in every
    /// whiteboard: a conversion's boards until each is drawn again (the
    /// sheet's picture went with the sheet), and any whose drawing an app
    /// killed meanwhile never finished. Until one is drawn and sent, the
    /// server shows the old sheet's picture at the board's size.
    public func boardsOwingRenders(notebookIDs: [String]? = nil) -> [PageRef] {
        let rows =
            (try? database.queue.read { db in
                try Row.fetchAll(
                    db,
                    sql: """
                        SELECT n.id AS notebook, n.path AS path, p.id AS page FROM pages p
                        JOIN notebooks n ON p.notebook_id = n.id
                        WHERE p.paper_size = ? ORDER BY n.path, p.position
                        """,
                    arguments: [PaperSize.board.rawValue])
            }) ?? []
        let wanted = notebookIDs.map(Set.init)
        return rows.compactMap { row -> PageRef? in
            let ref = PageRef(notebookID: row["notebook"], notebookPath: row["path"], pageID: row["page"])
            if let wanted, !wanted.contains(ref.notebookID) { return nil }
            let render = paths.render(ref.notebookPath, ref.pageID).path(percentEncoded: false)
            return FileManager.default.fileExists(atPath: render) ? nil : ref
        }
    }

    /// Turns a notebook into a whiteboard in place, one board per page, and
    /// returns it.
    ///
    /// Every page keeps its id, `created`, revision, bookmark, objects and
    /// history; its title becomes the board's name ("Board N" when it has
    /// none). Its ink and objects move by the plan's translation, so the
    /// content sits round the centre of its board and every stroke keeps its
    /// distance to every other. A PDF page becomes a picture of itself, the
    /// bottom image object.
    ///
    /// The order, and why:
    ///
    /// 1. Measure and plan every page, and check every PDF page's document
    ///    opens. Any refusal is thrown here, before anything is written.
    /// 2. Push every page's ink into the snapshot ring, so History holds the
    ///    drawing as it was before the first write.
    /// 3. Render each PDF page at 2x and store it as an asset, queued for
    ///    upload. An interruption here leaves the pages as they were and at
    ///    most an unused picture.
    /// 4. Build every page's board in a staging directory outside the vault
    ///    -- the moved ink, the board's `page.json`, the transcript's boxes
    ///    moved, no stale render -- noting the files it was built from.
    /// 5. Under `writeLock`: a page whose files changed since step 1, or that
    ///    arrived since, is planned and built again, and any refusal is
    ///    thrown before the first swap. Then page by page, each board is
    ///    swapped for its page's directory in one rename and the page's PUT
    ///    queued. A page is therefore always wholly the page or wholly the
    ///    board, never moved ink under an unmoved `page.json`.
    /// 6. Queue the notebook patch (`kind`, `paperSize`, `template`), set the
    ///    reading position to the converted page centred at 100 %, then write
    ///    `notebook.json`. The patch carries its values, so a patch queued
    ///    before the file is written still says whiteboard upstream, and the
    ///    pull brings it back down.
    /// 7. Rebuild the index rows.
    ///
    /// Interrupted anywhere, the notebook stays a notebook whose pages are
    /// each either untouched or a finished board, and running this again
    /// finishes the job: boards are not moved twice, and every board still
    /// owed a PUT gets one. A notebook that is a whiteboard already, with
    /// every page a board, is returned as it is.
    ///
    /// Steps 1 to 4 -- the decoding, the PDF pictures, the moved ink, every
    /// expensive part -- run without `writeLock`, which autosave takes on the
    /// main thread: a save in any window waits only for the swaps. The lock
    /// is held from the check in step 5 to the rows, so no save and no pull
    /// lands between a page's check and its swap. The caller flushes every
    /// live canvas on the document first and keeps it closed while this
    /// runs, and holds the sync engine: a canvas still holding the old ink
    /// would write it back at the old coordinates.
    @discardableResult
    public func convertToWhiteboard(notebookID: String) throws -> Notebook {
        try convertToWhiteboard(notebookID: notebookID, stoppingAt: nil)
    }

    func convertToWhiteboard(notebookID: String, stoppingAt stop: WhiteboardConversionStep?)
        throws -> Notebook
    {
        // A second conversion of the same notebook waits for the first, then
        // finds its work done: the two would share one staging directory,
        // and each clears it.
        conversions.enter(notebookID)
        defer { conversions.leave(notebookID) }
        func reached(_ step: WhiteboardConversionStep) throws {
            if step == stop { throw WhiteboardConversionInterrupted(step: step) }
        }
        let manager = FileManager.default
        let staging = conversionStaging(notebookID: notebookID)
        // An earlier run's staging, and a `~convert` an earlier build left in
        // the package: the page beside each is whole either way.
        try? manager.removeItem(at: staging)
        defer { try? manager.removeItem(at: staging) }
        #if DEBUG
        testHookConversionStarted?()
        #endif

        // 1-4, without the lock.
        let indexed = try notebook(id: notebookID)
        removeConversionLeftovers(notebookPath: indexed.path)
        let finishingBefore =
            ((try? AtomicFile.readJSON(Notebook.self, at: paths.notebookJSON(indexed.path)))
                ?? indexed).kind != .whiteboard
        let work = try conversionWork(for: indexed)
        guard finishingBefore || work.contains(where: { $0.plan != nil }) else { return indexed }
        try snapshot(work)
        try reached(.snapshotsTaken)
        let pictures = try storePictures(work, notebookPath: indexed.path)
        try reached(.picturesStored)
        let now = Date()
        var built: [String: StagedBoard] = [:]
        for item in work {
            guard let plan = item.plan else { continue }
            built[item.page.id] = try stageBoard(
                item, plan: plan, picture: pictures[item.page.id], modified: now,
                notebookPath: indexed.path, in: staging)
        }
        #if DEBUG
        testHookBeforeConversionLock?()
        #endif

        // 5-7, under the lock.
        writeLock.lock()
        defer { writeLock.unlock() }
        let current = try notebook(id: notebookID)
        let path = current.path
        // The file, not the row: it holds the keys this build does not know.
        var notebook =
            (try? AtomicFile.readJSON(Notebook.self, at: paths.notebookJSON(path))) ?? current
        notebook.path = path
        notebook.pages = current.pages
        let finishing = notebook.kind != .whiteboard

        // Every page as it is now. What changed since it was built, or came
        // since, is built again here; a refusal is still thrown before the
        // first swap.
        var boards: [String: StagedBoard] = [:]
        var again: [ConversionItem] = []
        for (index, pageID) in notebook.pages.enumerated() {
            let page = try page(notebookPath: path, id: pageID)
            guard page.size != .board else { continue }
            if let staged = built[pageID], staged.source == sourceStamp(notebookPath: path, pageID: pageID) {
                boards[pageID] = staged
            } else {
                again.append(try conversionItem(page, number: index + 1, notebookPath: path))
            }
        }
        if !again.isEmpty {
            try snapshot(again)
            let pictures = try storePictures(again, notebookPath: path)
            for item in again {
                guard let plan = item.plan else { continue }
                boards[item.page.id] = try stageBoard(
                    item, plan: plan, picture: pictures[item.page.id], modified: now,
                    notebookPath: path, in: staging)
            }
        }
        guard finishing || !boards.isEmpty else {
            try? setSyncValue(nil, for: Self.conversionMark(notebookID))
            return current
        }

        // From the first swap until the rows: a launch keeps every editor
        // off a document whose mark is still here, until it is finished.
        try setSyncValue("1", for: Self.conversionMark(notebookID))
        for (index, pageID) in notebook.pages.enumerated() {
            if let board = boards[pageID] {
                try swapInBoard(board, notebookPath: path)
                #if DEBUG
                testHookAfterConversionSwap?(index)
                #endif
                try reached(.pageSwapped(index))
            } else if !finishing {
                // A board already, on a whiteboard: nothing is owed for it.
                continue
            }
            // A board an interrupted run swapped in may never have had its
            // PUT queued; queueing it again replaces the one that was.
            try journal.append(
                .init(
                    kind: .pagePut, notebookPath: path, notebookID: notebook.id,
                    pageID: pageID, payload: ["metadata": "convert"]))
            try reached(.pageJournalled(index))
        }

        // 6. The notebook: the patch first, then the file.
        let firstBoard = notebook.pages.first.flatMap { try? page(notebookPath: path, id: $0) }
        notebook.kind = .whiteboard
        notebook.paperSize = .board
        notebook.template = firstBoard?.template ?? work.first?.plan?.template ?? .grid
        notebook.modified = now
        try journal.append(
            .init(
                kind: .notebookPatch, notebookPath: path, notebookID: notebook.id,
                payload: [
                    "kind": DocumentKind.whiteboard.rawValue,
                    "paperSize": PaperSize.board.rawValue,
                    "template": notebook.template.rawValue,
                ]))
        try reached(.notebookJournalled)
        // Where it opens, before the file says whiteboard: a board read with a
        // notebook's reading position would open far from its content.
        let state = viewState(notebookID: notebook.id)
        let shown =
            state.lastPageID.flatMap { id in notebook.pages.contains(id) ? id : nil }
            ?? notebook.pages.first
        let extent =
            shown.flatMap { try? page(notebookPath: path, id: $0) }?.pointSize
            ?? BoardGeometry.defaultExtent
        try setViewState(
            NotebookViewState(lastPageID: shown, zoom: 1, offset: BoardGeometry.center(of: extent)),
            notebookID: notebook.id)
        try writeNotebookJSON(notebook)
        try reached(.notebookWritten)

        // 7. The rows.
        try indexNotebook(at: path)
        try? setSyncValue(nil, for: Self.conversionMark(notebookID))
        return try self.notebook(id: notebookID)
    }

    // MARK: - Measuring

    /// One page of a conversion: as it is, its ink, its plan -- `nil` for a
    /// page that is a board already -- and the files it was read from.
    private struct ConversionItem {
        var page: Page
        var drawing: Data?
        var plan: BoardPlan?
        var source: SourceStamp?
    }

    /// Reads, measures and plans every page of `notebook`, in order, and
    /// checks what the writes will need. Throws before anything is written.
    private func conversionWork(for notebook: Notebook) throws -> [ConversionItem] {
        var work: [ConversionItem] = []
        for (index, pageID) in notebook.pages.enumerated() {
            // A page whose page.json cannot be read is refused rather than
            // passed over: it would stay a page in a whiteboard.
            let page = try page(notebookPath: notebook.path, id: pageID)
            guard page.size != .board else {
                work.append(ConversionItem(page: page, drawing: nil, plan: nil, source: nil))
                continue
            }
            work.append(try conversionItem(page, number: index + 1, notebookPath: notebook.path))
        }
        return work
    }

    /// One sheet, read and planned. Its files are noted before they are
    /// read, so a write after the note is a change the swap sees.
    private func conversionItem(_ read: Page, number: Int, notebookPath: String) throws
        -> ConversionItem
    {
        let source = sourceStamp(notebookPath: notebookPath, pageID: read.id)
        // Read again after the note: what the note describes is what is used.
        let page = (try? self.page(notebookPath: notebookPath, id: read.id)) ?? read
        let drawing = drawingData(notebookPath: notebookPath, pageID: page.id)
        var ink: CGRect?
        if let drawing, !drawing.isEmpty {
            // Decoded here, not with `try?`: ink that does not decode
            // cannot be moved, and the page would end up without it.
            let decoded = try PKDrawing(data: drawing)
            if !decoded.strokes.isEmpty, !decoded.bounds.isNull, !decoded.bounds.isEmpty {
                ink = decoded.bounds
            }
        }
        let plan = try WhiteboardConversion.plan(page: page, number: number, inkBounds: ink)
        if let binding = page.pdf, pdfPage(binding, notebookPath: notebookPath) == nil {
            throw PaperStoreError.notFound(binding.asset)
        }
        return ConversionItem(page: page, drawing: drawing, plan: plan, source: source)
    }

    /// The ink as it was, before the first write.
    private func snapshot(_ work: [ConversionItem]) throws {
        let ring = SnapshotRing(paths: paths, database: database)
        for item in work where item.plan != nil {
            guard let ink = item.drawing, !ink.isEmpty else { continue }
            try ring.push(ink, pageID: item.page.id)
        }
    }

    /// The PDF pages' pictures, stored and queued, by page id.
    private func storePictures(_ work: [ConversionItem], notebookPath: String) throws
        -> [String: String]
    {
        var pictures: [String: String] = [:]
        for item in work {
            guard let plan = item.plan, let binding = item.page.pdf else { continue }
            let picture = try pdfPicture(
                binding, notebookPath: notebookPath, size: item.page.pointSize,
                paper: plan.paperColor)
            pictures[item.page.id] = try storeAsset(
                picture, preferredName: "page-\(binding.pageIndex + 1).png",
                notebookPath: notebookPath)
        }
        return pictures
    }

    // MARK: - Writing a board

    /// What a page's files were when its board was built: each file's size,
    /// date and inode, which every write through `AtomicFile` changes.
    struct SourceStamp: Equatable {
        var files: [FileStamp?]
    }

    struct FileStamp: Equatable {
        var size: Int64
        var modified: Int64
        var inode: UInt64
    }

    private func sourceStamp(notebookPath: String, pageID: String) -> SourceStamp {
        let urls = [
            paths.pageJSON(notebookPath, pageID), paths.drawing(notebookPath, pageID),
            paths.text(notebookPath, pageID),
        ]
        return SourceStamp(
            files: urls.map { url in
                var info = stat()
                guard stat(url.path(percentEncoded: false), &info) == 0 else { return nil }
                return FileStamp(
                    size: Int64(info.st_size),
                    modified: Int64(info.st_mtimespec.tv_sec) * 1_000_000_000
                        + Int64(info.st_mtimespec.tv_nsec),
                    inode: UInt64(info.st_ino))
            })
    }

    /// A board built and waiting to be swapped in, and what it was built from.
    struct StagedBoard {
        var pageID: String
        var directory: URL
        var source: SourceStamp?
    }

    /// Where a conversion builds its boards: outside the vault, so nothing
    /// that walks it meets one, and on the same volume, so a board swaps in
    /// with one rename. A run removes what an earlier one left.
    func conversionStaging(notebookID: String) -> URL {
        paths.root.appending(path: "converting", directoryHint: .isDirectory)
            .appending(path: notebookID, directoryHint: .isDirectory)
    }

    /// Removes what conversions a kill stopped left in `converting/`: a
    /// copy of every page's directory, staged and never swapped in, for
    /// ever otherwise. Run at launch, before an interrupted conversion is
    /// finished; one running now is left alone.
    public func removeConversionStaging() {
        let root = paths.root.appending(path: "converting", directoryHint: .isDirectory)
        let names =
            (try? FileManager.default.contentsOfDirectory(atPath: root.path(percentEncoded: false)))
            ?? []
        for name in names where !conversions.isBusy(name) {
            try? FileManager.default.removeItem(
                at: root.appending(path: name, directoryHint: .isDirectory))
        }
    }

    /// Where an earlier build staged a board, inside the package.
    private func legacyStagingDirectory(_ notebookPath: String, _ pageID: String) -> URL {
        paths.pagesDirectory(notebookPath)
            .appending(path: "\(pageID)~convert", directoryHint: .isDirectory)
    }

    /// A run of an earlier build that stopped half way left a `~convert`
    /// directory in the package: the board it had not swapped in yet, or the
    /// page it had swapped out. The page directory beside it is whole either
    /// way.
    private func removeConversionLeftovers(notebookPath: String) {
        let names =
            (try? FileManager.default.contentsOfDirectory(
                atPath: paths.pagesDirectory(notebookPath).path(percentEncoded: false))) ?? []
        for name in names where name.hasSuffix("~convert") {
            try? FileManager.default.removeItem(
                at: paths.pagesDirectory(notebookPath)
                    .appending(path: name, directoryHint: .isDirectory))
        }
    }

    /// Builds the board for `item` in `staging` -- every file the page has,
    /// the ink moved, the transcript's boxes moved, the stale render gone.
    private func stageBoard(
        _ item: ConversionItem, plan: BoardPlan, picture: String?, modified: Date,
        notebookPath: String, in staging: URL
    ) throws -> StagedBoard {
        let manager = FileManager.default
        let board = WhiteboardConversion.board(
            from: item.page, plan: plan, pdfImageAsset: picture, modified: modified)
        let live = paths.pageDirectory(notebookPath, board.id)
        let directory = staging.appending(path: board.id, directoryHint: .isDirectory)
        try manager.createDirectory(at: staging, withIntermediateDirectories: true)
        try? manager.removeItem(at: directory)
        try manager.copyItem(at: live, to: directory)

        let moved = try item.drawing.map {
            try WhiteboardConversion.translatedDrawing($0, by: plan.translation)
        }
        let stagedText = directory.appending(path: "text.json", directoryHint: .notDirectory)
        // A transcript newer than the ink is a reading of it, and goes up with
        // the board; one older than the ink is waiting to be read again. The
        // two are written in the order that keeps which one it is.
        let textIsCurrent = !transcriptPredatesInk(notebookPath: notebookPath, pageID: board.id)
        let text = (try? Data(contentsOf: stagedText)).flatMap {
            Self.translatedText($0, by: plan.translation)
        }
        var files: [(name: String, data: Data)] = []
        if let text, !textIsCurrent { files.append(("text.json", text)) }
        if let moved { files.append(("drawing.pkdrawing", moved)) }
        if let text, textIsCurrent { files.append(("text.json", text)) }
        files.append(("page.json", try PaperJSON.encode(board)))
        for file in files {
            try AtomicFile.write(
                file.data, to: directory.appending(path: file.name, directoryHint: .notDirectory))
        }
        // The render is a picture of the page; the board is drawn afresh.
        try? manager.removeItem(
            at: directory.appending(path: "render.png", directoryHint: .notDirectory))
        return StagedBoard(pageID: board.id, directory: directory, source: item.source)
    }

    /// Swaps a staged board for its page's directory in one rename. Throws,
    /// leaving the page as it was, where the volume cannot: writing into the
    /// live page file by file instead is what leaves moved ink under an
    /// unmoved `page.json` when the app is killed half way, and a run after
    /// that moves the ink a second time. Callers hold `writeLock`.
    private func swapInBoard(_ board: StagedBoard, notebookPath: String) throws {
        let manager = FileManager.default
        let live = paths.pageDirectory(notebookPath, board.pageID)
        #if DEBUG
        let swapped = !testSwapFails && Self.swap(board.directory, live)
        #else
        let swapped = Self.swap(board.directory, live)
        #endif
        guard swapped else {
            throw CocoaError(.fileWriteUnknown, userInfo: [NSFilePathErrorKey: live.path(percentEncoded: false)])
        }
        // The staging directory holds the page as it was now.
        try? manager.removeItem(at: board.directory)
        // Its ETags vouched for the page's files: a pull must not take a 304
        // for bytes that are the board's now.
        for key in SyncEngine.blobTagKeys(page: "\(notebookPath)/pages/\(board.pageID)") {
            try? setSyncValue(nil, for: key)
        }
        // The library's thumbnail was drawn from the page too.
        try? manager.removeItem(at: PageRenderer.coverURL(pageID: board.pageID))
    }

    /// Exchanges two directories in one step (APFS's `RENAME_SWAP`). False
    /// where the volume cannot.
    private static func swap(_ first: URL, _ second: URL) -> Bool {
        #if canImport(Darwin)
            return renamex_np(
                first.path(percentEncoded: false), second.path(percentEncoded: false),
                UInt32(RENAME_SWAP)) == 0
        #else
            return false
        #endif
    }

    /// `text.json` with every recognised line's box moved, everything else
    /// byte for byte as it was read; `nil` when it is not a transcript.
    static func translatedText(_ data: Data, by translation: CGSize) -> Data? {
        guard var object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        if let lines = object["lines"] as? [[String: Any]] {
            object["lines"] = lines.map { line -> [String: Any] in
                guard var box = line["box"] as? [String: Any],
                    let x = (box["x"] as? NSNumber)?.doubleValue,
                    let y = (box["y"] as? NSNumber)?.doubleValue
                else { return line }
                var moved = line
                box["x"] = x + Double(translation.width)
                box["y"] = y + Double(translation.height)
                moved["box"] = box
                return moved
            }
        }
        return try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    // MARK: - PDF pages

    /// The PDF page a binding names, if its document is on this device, with
    /// the document: a page holds its document weakly, and one drawn after
    /// the document has gone draws nothing.
    private func pdfPage(_ binding: PDFBinding, notebookPath: String)
        -> (document: PDFDocument, page: PDFPage)?
    {
        let url = assets.url(forAssetPath: binding.asset, inNotebook: notebookPath)
        guard let document = PDFDocument(url: url), binding.pageIndex >= 0,
            binding.pageIndex < document.pageCount,
            let page = document.page(at: binding.pageIndex)
        else { return nil }
        return (document, page)
    }

    /// The PDF page as a PNG at 2x, on its paper, the way the page showed it:
    /// scaled to the page's size, turned as the page is turned.
    private func pdfPicture(
        _ binding: PDFBinding, notebookPath: String, size: CGSize, paper: PaperColor
    ) throws -> Data {
        guard let (document, pdf) = pdfPage(binding, notebookPath: notebookPath) else {
            throw PaperStoreError.notFound(binding.asset)
        }
        defer { withExtendedLifetime(document) {} }
        let scale = PageRenderer.cappedScale(PageRenderer.scale, for: size)
        let width = Int((size.width * scale).rounded())
        let height = Int((size.height * scale).rounded())
        guard width > 0, height > 0,
            let context = CGContext(
                data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)
        else { throw PageRenderError.contextUnavailable }
        let rgb = paper.rgb
        context.setFillColor(red: rgb.red, green: rgb.green, blue: rgb.blue, alpha: 1)
        context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        // PDF space is y-up, as is a bitmap context: no flip, only the scale.
        context.scaleBy(x: scale, y: scale)
        let bounds = pdf.bounds(for: .mediaBox)
        if bounds.width > 0, bounds.height > 0 {
            let turned =
                pdf.rotation % 180 == 0
                ? bounds.size : CGSize(width: bounds.height, height: bounds.width)
            context.scaleBy(x: size.width / turned.width, y: size.height / turned.height)
            pdf.draw(with: .mediaBox, to: context)
        }
        guard let image = context.makeImage() else { throw PageRenderError.contextUnavailable }
        let data = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                data as CFMutableData, UTType.png.identifier as CFString, 1, nil)
        else { throw PageRenderError.encodingFailed }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { throw PageRenderError.encodingFailed }
        return data as Data
    }
}

/// Notebooks being converted, in this process: `enter` waits while another
/// conversion of the same notebook runs.
final class ConversionGate: @unchecked Sendable {
    private let condition = NSCondition()
    private var busy: Set<String> = []

    func enter(_ id: String) {
        condition.lock()
        while busy.contains(id) { condition.wait() }
        busy.insert(id)
        condition.unlock()
    }

    func leave(_ id: String) {
        condition.lock()
        busy.remove(id)
        condition.broadcast()
        condition.unlock()
    }

    func isBusy(_ id: String) -> Bool {
        condition.lock()
        defer { condition.unlock() }
        return busy.contains(id)
    }
}
