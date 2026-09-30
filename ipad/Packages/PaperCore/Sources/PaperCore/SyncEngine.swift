import Foundation
import os

/// Every failure the engine absorbs, and every state the pill is sent.
/// `log stream --predicate 'subsystem == "art.exscalia.ipad"'` reads it; add
/// `--level info` for the state transitions.
private let log = Logger(subsystem: "art.exscalia.ipad", category: "sync")

/// Where the pill sends someone who taps "conflict on page 4".
public struct ConflictTarget: Equatable, Sendable {
    public let notebookPath: String
    /// The page as the server kept it.
    public let originalPageID: String
    /// The page this device wrote, now living beside the original.
    public let siblingPageID: String
    /// 1-based position of the sibling in the notebook.
    public let pageNumber: Int

    public init(
        notebookPath: String, originalPageID: String, siblingPageID: String, pageNumber: Int
    ) {
        self.notebookPath = notebookPath
        self.originalPageID = originalPageID
        self.siblingPageID = siblingPageID
        self.pageNumber = pageNumber
    }
}

/// Time, injected. The backoff sequence is part of the contract, so a test has
/// to be able to watch it without waiting a minute for the last step.
public protocol SyncClock: Sendable {
    var now: Date { get }
    func sleep(seconds: TimeInterval) async throws
}

public struct SystemSyncClock: SyncClock {
    public init() {}
    public var now: Date { Date() }
    public func sleep(seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
    }
}

/// Drains the op journal to the server and brings other devices' work down.
///
/// The invariants, in the order they matter:
///
/// 1. **The journal is FIFO and nothing is ever dropped.** A push either
///    succeeds, becomes a conflict (which is also a success: both halves are on
///    disk), or is retried. A 4xx that is not a conflict parks the op and says
///    so in the pill, because a request the server will never accept must not
///    block every op behind it, and deleting it would be deleting the user's
///    work.
/// 2. **A pull never overwrites local work.** Pages with unpushed ops are
///    skipped entirely; a page whose server revision is not ahead of the local
///    one is not fetched, which is also what makes this device ignore the echo
///    of its own save. A page is written only once every part of it has
///    arrived, and the change cursor moves only past changes that landed: a
///    revision stamped onto ink that never came would let the next stroke
///    replace the other side's work without a conflict.
/// 3. **Nothing here is on the input path.** Autosave has already written the
///    ink before this actor hears about it.
public actor SyncEngine {
    /// How often a quiet, connected app asks the server whether anything moved.
    public static let pollInterval: TimeInterval = 300
    /// Ceiling on the retry backoff.
    public static let maximumBackoff: TimeInterval = 60
    /// Ops pushed before pausing to pull, so a long queue still shows progress.
    public static let pushBatch = 200
    /// How often a signed-out engine asks whether its credential is good again.
    public static let signedOutProbeInterval: TimeInterval = 60
    /// Entries kept in `backoffHistory`; the ceiling is reached by the seventh.
    static let backoffHistoryLimit = 32

    private enum StateKey {
        static let cursor = "changes.seq"
        static let epoch = "changes.epoch"
        static let assetBackfill = "assets.backfill.v1"
        static let lastError = "sync.last_error"
        static func etag(_ page: String, _ name: String) -> String { "etag.\(page).\(name)" }
    }

    private static let blobNames = ["drawing.pkdrawing", "render.png", "text.json"]

    private let store: PaperStore
    private let api: PaperAPI
    /// This install's id on the feed. Public so callers that talk to the API
    /// directly (a history restore from the UI) stamp the same origin.
    public nonisolated let clientID: String
    private let clock: SyncClock
    private let jitter: @Sendable (TimeInterval) -> TimeInterval
    private let live: LiveEventStream?

    private var onState: (@Sendable (SaveState) -> Void)?
    private var onConflict: (@Sendable (ConflictTarget) -> Void)?
    private var onNotebooksChanged: (@Sendable (Set<String>) -> Void)?

    public private(set) var state: SaveState = .savedLocally
    public private(set) var isOnline = true
    public private(set) var isSignedOut = false
    public private(set) var conflict: ConflictTarget?
    public private(set) var lastSyncedAt: Date?
    /// Consecutive transport failures; drives the backoff.
    public private(set) var failureCount = 0
    /// The backoff delays of the current run of failures, in order, up to
    /// `backoffHistoryLimit` of them. Read by tests.
    public private(set) var backoffHistory: [TimeInterval] = []
    /// The newest failure the engine absorbed -- which op or page, and what
    /// the server or the network said -- kept across launches. A diagnostic,
    /// not a state: a later success leaves it alone.
    public private(set) var lastError: String?
    public private(set) var lastErrorAt: Date?

    /// One drain and one pull at a time.
    ///
    /// An actor serialises calls but not *cycles*: every `await` inside the
    /// drain is a point where a second caller (the poll loop and a foreground
    /// `syncNow` arriving together) can walk in, see the same head-of-queue op
    /// and push it twice. That is how one notebook becomes two.
    private var isPushing = false
    private var isPulling = false
    /// Whether the last drain stopped on a transport failure. Kept apart from
    /// `failureCount`, which a failed pull raises too: gating the pull on that
    /// left a device with nothing queued never pulling again.
    private var lastPushFailed = false

    /// Notebooks to reconcile by manifest once nothing is queued for them: a
    /// pull passed them over because they had unpushed work, so their change
    /// is behind the cursor now, or a page in them did not come back after a
    /// conflict.
    private var deferredNotebooks: Set<String> = []

    private var loopTask: Task<Void, Never>?
    private var liveTask: Task<Void, Never>?
    private var sleepTask: Task<Void, Never>?

    public init(
        store: PaperStore,
        api: PaperAPI,
        clientID: String,
        clock: SyncClock = SystemSyncClock(),
        live: LiveEventStream? = nil,
        jitter: @escaping @Sendable (TimeInterval) -> TimeInterval = { delay in
            // Full jitter over the top half of the window: enough spread that
            // two iPads reconnecting together do not knock on the door in step,
            // never so little delay that a failing server is hammered.
            delay * Double.random(in: 0.5...1.0)
        }
    ) {
        self.store = store
        self.api = api
        self.clientID = clientID
        self.clock = clock
        self.live = live
        self.jitter = jitter
        if let saved = store.syncValue(StateKey.lastError) {
            let parts = saved.split(separator: " ", maxSplits: 1).map(String.init)
            if parts.count == 2, let at = try? Date(parts[0], strategy: .iso8601) {
                lastErrorAt = at
                lastError = parts[1]
            } else {
                lastError = saved
            }
        }
    }

    public func setHandlers(
        onState: (@Sendable (SaveState) -> Void)? = nil,
        onConflict: (@Sendable (ConflictTarget) -> Void)? = nil,
        onNotebooksChanged: (@Sendable (Set<String>) -> Void)? = nil
    ) {
        self.onState = onState
        self.onConflict = onConflict
        self.onNotebooksChanged = onNotebooksChanged
        updateState()
    }

    // MARK: - Lifecycle

    /// Starts the background loop and, when one was supplied, the live stream.
    public func start() {
        guard loopTask == nil else { return }
        loopTask = Task { [weak self] in await self?.runLoop() }
        if live != nil {
            liveTask = Task { [weak self] in await self?.runLive() }
        }
    }

    public func stop() {
        loopTask?.cancel()
        loopTask = nil
        liveTask?.cancel()
        liveTask = nil
        wake()
    }

    /// Network reachability, from `NWPathMonitor` in the app target.
    public func setOnline(_ online: Bool) {
        guard online != isOnline else { return }
        isOnline = online
        if online { failureCount = 0 }
        updateState()
        if online { kick() }
    }

    /// Something local changed, or the user asked. Wakes the loop -- except
    /// while signed out, when all the loop does is probe the credential on its
    /// own slow cadence, and waking it for every autosave would turn that into
    /// a request per stroke.
    public func kick() {
        updateState()
        guard !isSignedOut else { return }
        wake()
    }

    /// The credential is good again: the web shell saw a signed-in session.
    /// Lifts the signed-out latch and runs a cycle now. Nothing to lift on an
    /// engine that was not signed out: the web shell says this on every page
    /// load, and a server that is failing keeps its backoff.
    public func credentialsChanged() {
        guard isSignedOut else { return }
        isSignedOut = false
        failureCount = 0
        updateState()
        wake()
    }

    /// The tap target on the pill has been honoured; go back to a normal state.
    public func clearConflict() {
        conflict = nil
        updateState()
    }

    /// Puts parked ops back in the queue. The user asked, so the answer is yes.
    public func retryParked() {
        _ = try? store.journal.unparkAll()
        kick()
    }

    /// The web view has a session again. A 401 stops every push and pull and
    /// nothing else clears it, so this is the way back: forget the refusal
    /// and the backoff, and wake the loop. Returns false, and changes nothing,
    /// when the engine was not signed out -- cookies change for many reasons
    /// and a healthy engine keeps its own schedule.
    @discardableResult
    public func signedIn() -> Bool {
        guard isSignedOut else { return false }
        isSignedOut = false
        failureCount = 0
        kick()
        return true
    }

    /// One full cycle: drain what is queued, then take what is new.
    ///
    /// A push that just failed means the server is not answering, so the pull
    /// is skipped rather than failing a second time: one cycle, one strike, one
    /// step of backoff. Only this cycle's push counts; a failure left over from
    /// an earlier pull must not stop the retry that pull is waiting for.
    public func syncNow() async {
        backfillAssetsOnce()
        // Foreground, or a tap on the pill: the moment to find out whether a
        // sign-in has happened since the last rejection.
        if isSignedOut { await probeCredentials() }
        await pushPending()
        if isOnline, !isSignedOut, !lastPushFailed { await pull() }
        updateState()
    }

    // MARK: - The loop

    private func runLoop() async {
        while !Task.isCancelled {
            if isSignedOut {
                // One rejected credential parks the drain, not the engine: ask
                // now and then whether this device is welcome again, so a
                // sign-in nobody reported (or a cookie that was not readable
                // yet on a cold start) brings sync back without a relaunch.
                updateState()
                await wait(seconds: Self.signedOutProbeInterval)
                if !Task.isCancelled { await probeCredentials() }
                continue
            }
            if isOnline { await syncNow() } else { updateState() }
            let delay: TimeInterval
            if failureCount > 0 {
                delay = jitter(Self.backoffBase(attempt: failureCount))
                if backoffHistory.count < Self.backoffHistoryLimit { backoffHistory.append(delay) }
            } else {
                backoffHistory.removeAll()
                delay = Self.pollInterval
            }
            await wait(seconds: delay)
        }
    }

    /// 1, 2, 4, 8, 16, 32, 60, 60 … -- doubling until the ceiling.
    public static func backoffBase(attempt: Int) -> TimeInterval {
        guard attempt > 0 else { return 0 }
        let doubled = pow(2.0, Double(attempt - 1))
        return min(maximumBackoff, doubled)
    }

    /// Sleeps until `seconds` have passed or `wake()` cancels the sleep.
    ///
    /// Cancelling the sleep is the whole wake-up, so nothing is left behind to
    /// fire later. A continuation resumed by a separate waker task was: the
    /// waker of a wait cut short woke the *next* wait at once, and in the
    /// offline branch, which never suspends between two waits, that was a loop
    /// spinning flat out.
    private func wait(seconds: TimeInterval) async {
        guard seconds > 0 else { return }
        let task = Task<Void, Never> { [clock] in
            _ = try? await clock.sleep(seconds: seconds)
        }
        sleepTask = task
        await task.value
        if sleepTask == task { sleepTask = nil }
    }

    private func wake() {
        sleepTask?.cancel()
    }

    /// Asks the server whether this device's credential is good again, and
    /// lifts the signed-out latch when it is.
    private func probeCredentials() async {
        guard isSignedOut, isOnline, let status = try? await api.authStatus(), status.canSync
        else { return }
        credentialsChanged()
    }

    /// Reads `/api/live` and pulls when somebody else writes. An event this
    /// device caused carries its own `client_id` as `origin` and is ignored:
    /// pulling on it would be chasing an echo.
    private func runLive() async {
        guard let live else { return }
        // The drop last logged. A server that is down fails every reconnect
        // the same way, which is one line rather than one every five seconds,
        // until the stream is back and can drop again.
        var reported: String?
        while !Task.isCancelled {
            guard isOnline, !isSignedOut else {
                try? await clock.sleep(seconds: 15)
                continue
            }
            let drop: String
            do {
                for try await event in live.events() {
                    // The server opens every stream with `ready`.
                    if reported != nil { log.info("live stream reconnected") }
                    reported = nil
                    guard let paper = event.paperEvent, paper.type == "paper" else { continue }
                    guard paper.origin != clientID else { continue }
                    kick()
                }
                drop = "closed"
            } catch {
                // A dropped stream is ordinary (proxies, radios). The polling
                // loop keeps the app correct while this waits to reconnect,
                // so it is noted, not kept as `lastError`.
                drop = Self.status(of: error)
            }
            if !Task.isCancelled, drop != reported {
                log.notice("live stream dropped [\(drop, privacy: .public)]; polling meanwhile")
                reported = drop
            }
            try? await clock.sleep(seconds: 5)
        }
    }

    // MARK: - Push

    /// Drains the queue head-first. Returns the number of ops that left.
    @discardableResult
    public func pushPending() async -> Int {
        guard isOnline, !isSignedOut, !isPushing else { return 0 }
        isPushing = true
        defer { isPushing = false }
        lastPushFailed = false
        var sent = 0
        while sent < Self.pushBatch {
            guard let op = (try? store.journal.pending(limit: 1))?.first, let id = op.id else {
                break
            }
            do {
                try await push(op)
                try? store.journal.complete(id)
                failureCount = 0
                sent += 1
                updateState()
            } catch PaperAPIError.unauthorized {
                isSignedOut = true
                updateState()
                return sent
            } catch let error as PaperAPIError where error.isPermanentClientError {
                // The server will never accept this request. Keep the op, stop
                // retrying it, and say which one it is.
                recordFailure(push: op, parked: true, error: error)
                try? store.journal.park(id, reason: error.summary)
                updateState()
            } catch let missing as LocalFilesMissing {
                // Nothing on this device to send. Completing it would report a
                // push that never happened; parked, it is named in the pill.
                recordFailure(push: op, parked: true, error: missing)
                try? store.journal.park(id, reason: missing.reason)
                updateState()
            } catch {
                // Transport or 5xx: the op is still good, the moment is not.
                recordFailure(push: op, parked: false, error: error)
                failureCount += 1
                lastPushFailed = true
                updateState()
                return sent
            }
        }
        if sent > 0 { lastSyncedAt = clock.now }
        return sent
    }

    private func push(_ op: PaperOp) async throws {
        switch op.kind {
        case .notebookCreate: try await pushNotebookCreate(op)
        case .notebookRename, .notebookMove: try await pushNotebookRename(op)
        case .notebookPatch: try await pushNotebookPatch(op)
        case .notebookDelete: try await api.deleteNotebook(path: op.notebookPath)
        case .pageCreate: try await pushPageCreate(op)
        case .pagePut: try await pushPagePut(op)
        case .pageRender, .pageText: try await pushDerived(op)
        case .pageDelete: try await api.deletePage(notebook: op.notebookPath, pageID: op.pageID)
        case .assetPut: try await pushAsset(op)
        }
    }

    /// Why an op that needs local files is parked instead of completed.
    struct LocalFilesMissing: Error {
        var reason = "page missing on this device"
    }

    /// Where the op's notebook is on this device now.
    ///
    /// The op's path is where the *server* has it when the op arrives, which a
    /// rename or move queued behind the op has not changed yet -- but the
    /// files moved at once, so they are found by id. Rows queued before ops
    /// carried an id fall back to the path.
    private func localNotebook(for op: PaperOp) -> Notebook? {
        if !op.notebookID.isEmpty { return try? store.notebook(id: op.notebookID) }
        return try? store.notebook(path: op.notebookPath)
    }

    /// Where a page op's files are on this device. `nil` when a delete still
    /// in the queue covers the page: the op is moot and completes. Throws when
    /// the page is simply not here, which parks the op -- completing it would
    /// report a push that never happened.
    private func localPagePath(for op: PaperOp) throws -> String? {
        if let path = localNotebook(for: op)?.path,
            (try? store.page(notebookPath: path, id: op.pageID)) != nil
        {
            return path
        }
        if (try? store.journal.hasQueuedDelete(pageID: op.pageID, notebookID: op.notebookID))
            == true
        {
            return nil
        }
        throw LocalFilesMissing()
    }

    private func pushNotebookCreate(_ op: PaperOp) async throws {
        guard let notebook = localNotebook(for: op) else { return }
        // Idempotent by construction: a retry after a response that never
        // arrived must not leave two notebooks called "Ideas".
        if try await api.notebookIfPresent(path: notebook.path) != nil { return }
        let manifest = try await api.createNotebook(
            title: notebook.title, folder: notebook.folder, template: notebook.template,
            size: notebook.paperSize, id: notebook.id, clientID: clientID)
        // Read again: a rename while the request was out re-pointed every op
        // queued behind this one at the new path, and moved nothing upstream.
        let target = localNotebook(for: op)?.path ?? notebook.path
        if manifest.path != target {
            // The server's own slug rules put it somewhere else; move it to the
            // path this device already keys everything by, with this device's
            // title: a new name in the same folder with none is taken as a
            // retitle to that name.
            _ = try await api.renameNotebook(
                path: manifest.path, to: target,
                title: localNotebook(for: op)?.title ?? notebook.title, clientID: clientID)
        }
    }

    private func pushNotebookRename(_ op: PaperOp) async throws {
        guard let newPath = op.payload["new_path"], !newPath.isEmpty else { return }
        do {
            // A rename journals the verbatim title (the path is only its
            // slug); a move journals none, so the server keeps its own.
            let moved = try await api.renameNotebook(
                path: op.notebookPath, to: newPath, title: op.payload["title"],
                clientID: clientID)
            // The move re-stamped every page so other devices find them at the
            // new path. This one already has them there: without the new seqs
            // its next pull would fetch the whole notebook again.
            if let local = localNotebook(for: op)?.path {
                for page in moved.movedPages ?? [] {
                    guard let previous = page.previousSeq, previous > 0 else { continue }
                    try? store.recordMovedPage(
                        notebookPath: local, pageID: page.id, seq: page.seq,
                        previousSeq: previous)
                }
            }
        } catch let error as PaperAPIError where error.status == 404 {
            // Nothing at the old path. Done if the rename already landed (a
            // retry after a response that never arrived); anything else means
            // the notebook is nowhere upstream, and that is parked, not hidden.
            if try await api.notebookIfPresent(path: newPath) != nil { return }
            throw error
        }
    }

    private func pushNotebookPatch(_ op: PaperOp) async throws {
        var patch: [String: JSONValue] = [:]
        for (key, value) in op.payload {
            switch key {
            case "favorite": patch[key] = .bool(value == "true")
            case "tags", "pages":
                let items = value.split(separator: ",").map { JSONValue.string(String($0)) }
                patch[key] = .array(items)
            case "title", "cover", "template", "paperSize": patch[key] = .string(value)
            default: break
            }
        }
        guard !patch.isEmpty else { return }
        do {
            _ = try await api.patchNotebook(
                path: op.notebookPath, patch: patch, clientID: clientID)
        } catch let error as PaperAPIError where error.status == 404 {
            return
        }
    }

    private func pushPageCreate(_ op: PaperOp) async throws {
        guard let local = try localPagePath(for: op),
            let page = try? store.page(notebookPath: local, id: op.pageID)
        else { return }
        do {
            let created = try await api.createPage(
                notebook: op.notebookPath, pageID: op.pageID, after: op.payload["after"],
                template: page.template, size: page.size, clientID: clientID,
                customSize: page.customSize)
            try store.recordServerRevisions(
                notebookPath: localNotebook(for: op)?.path ?? local, pageID: op.pageID,
                revision: created.revision, seq: created.seq)
        } catch let error as PaperAPIError where error.status == 409 {
            // Already there, which is what this op wanted.
            return
        }
    }

    private func pushPagePut(_ op: PaperOp) async throws {
        let pageID = op.pageID
        guard let local = try localPagePath(for: op),
            let page = try? store.page(notebookPath: local, id: pageID)
        else { return }
        let base = page.revision
        let outcome = try await api.putPage(
            notebook: op.notebookPath,
            pageID: pageID,
            baseRevision: base,
            pageJSON: store.pagePartJSON(notebookPath: local, pageID: pageID),
            drawing: store.drawingData(notebookPath: local, pageID: pageID),
            render: store.renderData(notebookPath: local, pageID: pageID),
            text: store.textData(notebookPath: local, pageID: pageID),
            // The render and transcript were made from the ink being sent, so
            // they describe the revision this save is about to become.
            forRevision: base + 1,
            after: op.payload["after"],
            clientID: clientID)

        // Where the files are now: a rename while the request was out moved them.
        let landed = localNotebook(for: op)?.path ?? local
        switch outcome {
        case .saved(let result):
            try store.recordServerRevisions(
                notebookPath: landed, pageID: pageID, revision: result.revision,
                renderRevision: result.page.renderRevision, textRevision: result.page.textRevision,
                seq: result.page.seq)
        case .conflict(let conflict):
            await adopt(conflict, notebookPath: op.notebookPath, local: landed, pageID: pageID)
        }
    }

    /// Both halves survived; this device keeps writing on the sibling and gets
    /// the original back exactly as the server has it -- from `notebookPath`
    /// upstream into `local` here, which differ while a rename is queued.
    private func adopt(
        _ response: PageConflict, notebookPath: String, local: String, pageID: String
    ) async {
        let sibling = response.conflict.sibling
        try? store.adoptConflictSibling(
            notebookPath: local, originalID: pageID, sibling: sibling)
        // The original's directory is the sibling's now: its ETags vouch for
        // nothing on disk.
        forgetBlobTags(page: "\(local)/pages/\(pageID)")
        if !(await fetchPage(notebook: notebookPath, pageID: pageID, into: local)) {
            // Nothing on the feed will name the original again, so its
            // notebook is reconciled by manifest on a later pull -- by the path
            // it has once the queue, and any rename in it, has drained.
            deferredNotebooks.insert(local)
        }
        store.reindex(notebookPath: local)

        let number = store.pageNumber(notebookPath: local, pageID: sibling.id) ?? 0
        let target = ConflictTarget(
            notebookPath: local, originalPageID: pageID, siblingPageID: sibling.id,
            pageNumber: number)
        conflict = target
        onConflict?(target)
        onNotebooksChanged?([local])
        updateState()
    }

    /// The render, or the transcript, finished after the ink went up, so it
    /// goes on its own.
    ///
    /// No source part, which is what makes this safe: the server records the
    /// derived file against the revision it was made for and leaves the
    /// revision alone, so a picture -- or a reading of the page -- arriving
    /// late can never look like an edit.
    ///
    /// A page that is not here any more is simply done: derived data can
    /// legitimately be gone, after a conflict was adopted for one.
    private func pushDerived(_ op: PaperOp) async throws {
        let pageID = op.pageID
        guard let local = localNotebook(for: op)?.path,
            let page = try? store.page(notebookPath: local, id: pageID),
            page.revision > 0
        else { return }
        let render = store.renderData(notebookPath: local, pageID: pageID)
        let text = store.textData(notebookPath: local, pageID: pageID)
        guard render != nil || text != nil else { return }
        let outcome = try await api.putPage(
            notebook: op.notebookPath, pageID: pageID, baseRevision: page.revision, pageJSON: nil,
            drawing: nil, render: render, text: text, forRevision: page.revision, after: nil,
            clientID: clientID)
        if case .saved(let result) = outcome {
            // Unchecked against the base, the answer names the server's
            // current revision whatever ink that is. Past the ink here means
            // somebody else wrote the page meanwhile: stamping it on this copy
            // would let the next stroke replace their change without a
            // conflict. Only the derived parts this push sent are recorded
            // then, and the pull sees the server ahead and fetches the page.
            let current = result.revision == page.revision
            try store.recordServerRevisions(
                notebookPath: localNotebook(for: op)?.path ?? local, pageID: pageID,
                revision: current ? result.revision : nil,
                renderRevision: current || render != nil ? result.page.renderRevision : nil,
                textRevision: current || text != nil ? result.page.textRevision : nil,
                seq: current ? result.page.seq : nil)
        }
    }

    /// An image or an imported PDF, found where its notebook is now.
    ///
    /// Sent under its stored name less the hash: the server hashes the bytes
    /// and names the file by the same rule this device did, so the name it
    /// answers with is the one the page already holds. A name from before the
    /// stems matched comes back in the server's spelling instead, which the
    /// pull asks for by that spelling too, so nothing here rewrites the page.
    private func pushAsset(_ op: PaperOp) async throws {
        guard let relative = op.payload["path"], !relative.isEmpty else {
            throw LocalFilesMissing(reason: "attachment has no file to send")
        }
        guard
            let data = store.assets.data(
                forAssetPath: relative, inNotebook: localNotebook(for: op)?.path)
        else { throw LocalFilesMissing(reason: "attachment missing on this device") }
        let filename = op.payload["filename"] ?? AssetStore.uploadFilename(forAssetPath: relative)
        _ = try await api.uploadAsset(
            notebook: op.notebookPath, filename: filename, data: data,
            contentType: op.payload["content_type"]
                ?? AssetStore.contentType(forFilename: filename))
    }

    /// Once per install: an upload for every asset already here.
    ///
    /// Earlier builds completed an asset op without sending it, so every image
    /// and PDF stored before this one is on this device alone while the
    /// server's pages name it. Uploads are content-hashed: one the server
    /// already holds costs a request and changes nothing.
    private func backfillAssetsOnce() {
        guard store.syncValue(StateKey.assetBackfill) == nil else { return }
        do {
            for notebook in try store.notebooks() {
                let queued = Set(
                    try store.journal.unpushed(notebookPath: notebook.path)
                        .filter { $0.kind == .assetPut }.compactMap { $0.payload["path"] })
                for path in store.assets.assetPaths(forNotebook: notebook.path)
                where !queued.contains(path) {
                    try store.journal.append(
                        .init(
                            kind: .assetPut, notebookPath: notebook.path, notebookID: notebook.id,
                            payload: ["path": path]))
                }
            }
            try store.setSyncValue("done", for: StateKey.assetBackfill)
        } catch {
            // Asked again on the next cycle; what did queue is deduplicated then.
        }
    }

    // MARK: - Pull

    public func pull() async {
        guard !isPulling else { return }
        isPulling = true
        defer { isPulling = false }
        let cursor = Int(store.syncValue(StateKey.cursor) ?? "") ?? 0
        let epoch = store.syncValue(StateKey.epoch)
        let feed: ChangesFeed
        do {
            feed = try await api.changes(since: cursor, epoch: epoch)
        } catch PaperAPIError.unauthorized {
            isSignedOut = true
            updateState()
            return
        } catch {
            recordFailure("pull changes since \(cursor) failed", error: error)
            failureCount += 1
            updateState()
            return
        }

        // A full feed says the cursor is no good any more: rebuild from the
        // library rather than trusting a partial answer. Either way the old
        // cursor stays until everything the feed named has landed.
        let complete: Bool
        if feed.full {
            complete = await reconcileFromLibrary()
        } else {
            complete = await apply(feed)
        }
        if complete {
            try? store.setSyncValue(String(feed.seq), for: StateKey.cursor)
            try? store.setSyncValue(feed.epoch, for: StateKey.epoch)
            failureCount = 0
            lastSyncedAt = clock.now
        } else {
            // Something the feed named did not arrive. Stepping past it would
            // leave it behind the cursor for good; the next pull asks again,
            // one step of backoff later.
            failureCount += 1
        }
        await reconcileDeferredNotebooks()
        updateState()
    }

    /// True when everything the feed named landed or was deliberately left
    /// alone (unpushed work here, or nothing newer than this device has);
    /// false when something failed to arrive and the cursor has to stay.
    private func apply(_ feed: ChangesFeed) async -> Bool {
        let pendingPages = (try? store.journal.pendingPageKeys()) ?? []
        let pendingNotebooks = (try? store.journal.pendingNotebookPaths()) ?? []
        var touched: Set<String> = []
        var complete = true

        for summary in feed.notebooks {
            guard !pendingNotebooks.contains(summary.path) else {
                // Behind the cursor once this pull lands, so it is asked for
                // again by name when nothing is queued for it.
                deferredNotebooks.insert(summary.path)
                continue
            }
            do {
                // Gone since the feed was read -- a delete or a rename, whose
                // tombstone is on the feed: nothing to apply, nothing missing.
                guard let manifest = try await api.notebookIfPresent(path: summary.path) else {
                    continue
                }
                let before = notebookOnDisk(summary.path)
                try? store.applyRemoteNotebook(manifest)
                // A page this device created comes back as a notebook with a new
                // seq and nothing else new. Announcing it would rebuild the open
                // notebook under the pen for a change that is already on screen.
                if let before, let after = notebookOnDisk(summary.path),
                    before.hasSameContent(as: after)
                {
                    continue
                }
                touched.insert(summary.path)
            } catch {
                recordFailure("pull notebook failed", notebook: summary.path, error: error)
                complete = false
            }
        }

        for page in feed.pages {
            guard !pendingPages.contains(page.path) else { continue }
            // Not ahead of what is already here: either somebody else's change
            // already landed, or this is the echo of our own save.
            guard
                serverIsAhead(
                    notebook: page.notebook, pageID: page.id, revision: page.revision,
                    textRevision: page.textRevision, seq: page.seq)
            else { continue }
            if !(await fetchPage(notebook: page.notebook, pageID: page.id)) { complete = false }
            touched.insert(page.notebook)
        }

        for deletion in feed.deleted {
            guard let kind = TrashToken.Kind(rawValue: deletion.kind) else { continue }
            if kind == .notebook, pendingNotebooks.contains(deletion.path) { continue }
            if kind == .page, pendingPages.contains(deletion.path) { continue }
            let applied = (try? store.applyRemoteDelete(path: deletion.path, kind: kind)) != nil
            // The page's directory is in the device trash now, so its ETags
            // vouch for nothing on disk.
            if kind == .page { forgetBlobTags(page: deletion.path) }
            guard applied else { continue }
            touched.insert(
                kind == .notebook
                    ? deletion.path
                    : deletion.path.components(separatedBy: "/pages/").first ?? deletion.path)
        }

        for path in touched { store.reindex(notebookPath: path) }
        if !touched.isEmpty { onNotebooksChanged?(touched) }
        return complete
    }

    /// `notebook.json` as it is on disk now. Not `store.notebook(path:)`: the
    /// index row only catches up at `reindex`, which is what this decides.
    private func notebookOnDisk(_ path: String) -> Notebook? {
        try? AtomicFile.readJSON(Notebook.self, at: store.paths.notebookJSON(path))
    }

    /// Everything, from `GET /api/paper` plus one manifest each.
    ///
    /// Deliberately additive: a notebook this device has and the server does
    /// not is either still queued or was created offline, and a reconcile is
    /// not allowed to be the thing that deletes it. Deletions arrive as
    /// tombstones on the feed, where they can be told apart from absence.
    private func reconcileFromLibrary() async -> Bool {
        let library: PaperLibrary
        do {
            library = try await api.library()
        } catch {
            recordFailure("pull library failed", error: error)
            return false
        }
        let pendingPages = (try? store.journal.pendingPageKeys()) ?? []
        let pendingNotebooks = (try? store.journal.pendingNotebookPaths()) ?? []
        var touched: Set<String> = []
        var complete = true

        for summary in library.notebooks {
            guard !pendingNotebooks.contains(summary.path) else {
                deferredNotebooks.insert(summary.path)
                continue
            }
            let reconciled = await reconcileNotebook(
                summary.path, pendingPages: pendingPages, touched: &touched)
            if !reconciled { complete = false }
        }
        for path in touched { store.reindex(notebookPath: path) }
        if !touched.isEmpty { onNotebooksChanged?(touched) }
        return complete
    }

    /// Reconciles each deferred notebook that has no unpushed work any more.
    private func reconcileDeferredNotebooks() async {
        guard !deferredNotebooks.isEmpty else { return }
        let pendingNotebooks = (try? store.journal.pendingNotebookPaths()) ?? []
        let ready = deferredNotebooks.subtracting(pendingNotebooks)
        guard !ready.isEmpty else { return }
        let pendingPages = (try? store.journal.pendingPageKeys()) ?? []
        var touched: Set<String> = []
        for path in ready.sorted() {
            let reconciled = await reconcileNotebook(
                path, pendingPages: pendingPages, touched: &touched)
            if reconciled { deferredNotebooks.remove(path) }
        }
        for path in touched { store.reindex(notebookPath: path) }
        if !touched.isEmpty { onNotebooksChanged?(touched) }
    }

    /// One notebook from its manifest: the notebook itself, then every page in
    /// it the server has moved past this device's copy. False when something
    /// did not arrive.
    private func reconcileNotebook(
        _ path: String, pendingPages: Set<String>, touched: inout Set<String>
    ) async -> Bool {
        let manifest: NotebookManifest
        do {
            // Gone since it was listed: deleted or renamed, and the feed says
            // so. Nothing to apply, and nothing missing.
            guard let found = try await api.notebookIfPresent(path: path) else { return true }
            manifest = found
        } catch {
            recordFailure("reconcile notebook failed", notebook: path, error: error)
            return false
        }
        let before = notebookOnDisk(path)
        try? store.applyRemoteNotebook(manifest)
        // A deferred notebook is most often the echo of this device's own page
        // create, held back while the pen kept writing: as in `apply`, only a
        // notebook whose content moved, or a page that arrived, is announced.
        var changed = true
        if let before, let after = notebookOnDisk(path), before.hasSameContent(as: after) {
            changed = false
        }
        var complete = true
        for detail in manifest.pagesDetail ?? [] {
            guard !pendingPages.contains("\(path)/pages/\(detail.id)") else { continue }
            guard
                serverIsAhead(
                    notebook: path, pageID: detail.id, revision: detail.revision,
                    textRevision: detail.textRevision, seq: detail.seq)
            else { continue }
            if await fetchPage(notebook: path, pageID: detail.id) {
                changed = true
            } else {
                complete = false
            }
        }
        if changed { touched.insert(path) }
        return complete
    }

    /// Whether the server's copy of a page has moved past this device's.
    ///
    /// A higher ink revision, first of all. At the same ink revision, a newer
    /// transcript or simply a later write: the server's own AI reading never
    /// bumps the ink revision, and on a page that had already been read it
    /// lands at the text revision that was there, so only `seq` moves. A lower
    /// ink revision is never ahead, whatever else moved.
    private func serverIsAhead(
        notebook: String, pageID: String, revision: Int, textRevision: Int, seq: Int?
    ) -> Bool {
        let local = try? store.page(notebookPath: notebook, id: pageID)
        let localRevision = local?.revision ?? 0
        guard revision == localRevision else { return revision > localRevision }
        return textRevision > (local?.textRevision ?? 0) || (seq ?? 0) > (local?.seq ?? 0)
    }

    /// One page's metadata, ink, render and transcript -- all of it or none.
    ///
    /// `page.json` is what says which revision this device holds, so it is
    /// written only once every part it describes has arrived. False means
    /// nothing was written and the page is asked for again; a page that went
    /// away since it was listed counts as done.
    ///
    /// `into` is where the notebook is on this device when that differs from
    /// where the server has it, which it does only while a rename is queued.
    private func fetchPage(notebook: String, pageID: String, into target: String? = nil) async
        -> Bool
    {
        let path = target ?? notebook
        let response: RemotePageResponse
        do {
            response = try await api.page(notebook: notebook, pageID: pageID)
        } catch let error as PaperAPIError where error.status == 404 {
            return true
        } catch {
            recordFailure("pull page failed", notebook: notebook, page: pageID, error: error)
            return false
        }
        let remote = response.page
        let local = try? store.page(notebookPath: path, id: pageID)
        let drawing = await blob(
            notebook: notebook, pageID: pageID, name: "drawing.pkdrawing",
            revision: remote.revision, hasFile: remote.drawingURL != nil, into: path)
        guard drawing != .failed else { return false }
        let render = await blob(
            notebook: notebook, pageID: pageID, name: "render.png",
            revision: remote.renderRevision, hasFile: remote.renderURL != nil, into: path)
        guard render != .failed else { return false }
        // Somebody else's iPad read this page, or the server's own AI did. A
        // transcript is cheap to fetch and expensive to recompute, so take the
        // one that is already newer than ours rather than running Vision again
        // -- at the same text revision too, once the page has moved on, since
        // that is where the AI's reading lands.
        let localText = local?.textRevision ?? 0
        let wantsText =
            remote.textURL != nil
            && (remote.textRevision > localText
                || (remote.textRevision == localText && (remote.seq ?? 0) > (local?.seq ?? 0)))
        let text = await blob(
            notebook: notebook, pageID: pageID, name: "text.json",
            revision: remote.textRevision, hasFile: wantsText, into: path)
        guard text != .failed else { return false }
        let applied: Page?
        do {
            applied = try store.applyRemotePage(
                rawPageJSON: response.raw, notebookPath: path, pageID: pageID,
                drawing: drawing.bytes, render: render.bytes, text: text.bytes)
        } catch {
            recordFailure("write pulled page failed", notebook: path, page: pageID, error: error)
            return false
        }
        if let applied { await fetchMissingAssets(of: applied, notebook: notebook, into: path) }
        // Only now: an ETag vouches for bytes on disk, not for bytes in flight.
        for (name, outcome) in zip(Self.blobNames, [drawing, render, text]) {
            guard case .data(_, let etag?) = outcome else { continue }
            try? store.setSyncValue(etag, for: StateKey.etag("\(path)/pages/\(pageID)", name))
        }
        if text.bytes != nil {
            store.indexPageText(
                pageID: pageID, notebookPath: path, textRevision: remote.textRevision)
        }
        return true
    }

    /// The images and the PDF a pulled page names that this device does not
    /// have, written into the notebook's `assets/` under the name the page
    /// uses, where every reader looks.
    ///
    /// Asked for by that name, then by the server's spelling of it, which
    /// differs for a name stored before the stems matched. Best effort: the
    /// page is the source and has landed; an asset that did not come is asked
    /// for again the next time the page is.
    private func fetchMissingAssets(of page: Page, notebook: String, into path: String) async {
        var references = page.objects.compactMap(\.asset)
        if let pdf = page.pdf?.asset { references.append(pdf) }
        var seen: Set<String> = []
        for reference in references where seen.insert(reference).inserted {
            guard !store.assets.exists(assetPath: reference, inNotebook: path),
                let filename = reference.split(separator: "/").last.map(String.init),
                filename != "..", filename != "."
            else { continue }
            var candidates = [filename]
            let spelled = AssetStore.serverFilename(forAssetPath: filename)
            if spelled != filename { candidates.append(spelled) }
            for candidate in candidates {
                guard let data = try? await api.asset(notebook: notebook, filename: candidate)
                else { continue }
                // The name is a hash of the bytes; anything else is not this file.
                if let parts = AssetStore.nameParts(filename),
                    AssetStore.hash(data) != parts.hash.lowercased()
                {
                    continue
                }
                try? AtomicFile.write(
                    data, to: store.assets.url(forNotebook: path, filename: filename))
                break
            }
        }
    }

    /// What one blob GET came to. A failure is kept apart from the two
    /// answers that carry no bytes: it is the only one that means this device
    /// does not have what the page describes.
    private enum BlobOutcome: Equatable {
        case data(Data, etag: String?)
        /// 304: the file already here is the server's.
        case notModified
        /// No such file on the server, or the part was not asked for.
        case missing
        /// A transport error or a non-2xx answer.
        case failed

        var bytes: Data? {
            if case .data(let data, _) = self { return data }
            return nil
        }
    }

    /// Fetches a blob, conditionally when the file it would vouch for is on
    /// disk, so an unchanged file costs a request and no bytes.
    ///
    /// Only then: an ETag row outlives a page moved into the device trash or
    /// renamed into a conflict sibling, and a page the server restores from
    /// its trash keeps its revision and so its ETag -- a 304 for bytes this
    /// device no longer has. The transcript is never conditional: its ETag
    /// names only the text revision, which the server's AI reading leaves
    /// alone.
    private func blob(
        notebook: String, pageID: String, name: String, revision: Int, hasFile: Bool,
        into path: String
    ) async -> BlobOutcome {
        guard hasFile else { return .missing }
        let file = localFile(name, notebook: path, pageID: pageID)
        let known =
            name != "text.json" && Self.fileExists(file)
            ? store.syncValue(StateKey.etag("\(path)/pages/\(pageID)", name)) : nil
        let outcome = await requestBlob(
            notebook: notebook, pageID: pageID, name: name, revision: revision, etag: known)
        if outcome == .notModified, !Self.fileExists(file) {
            // The file it vouches for went while the request was out: ask
            // again, for the bytes this time.
            let retry = await requestBlob(
                notebook: notebook, pageID: pageID, name: name, revision: revision, etag: nil)
            return retry == .notModified ? .failed : retry
        }
        return outcome
    }

    private func requestBlob(
        notebook: String, pageID: String, name: String, revision: Int, etag: String?
    ) async -> BlobOutcome {
        let response: BlobResponse
        do {
            response = try await api.blob(
                notebook: notebook, pageID: pageID, name: name,
                revision: revision > 0 ? revision : nil, etag: etag)
        } catch {
            recordFailure("pull \(name) failed", notebook: notebook, page: pageID, error: error)
            return .failed
        }
        if response.notModified { return .notModified }
        if response.missing { return .missing }
        guard let data = response.data else { return .failed }
        return .data(data, etag: response.etag)
    }

    private func localFile(_ name: String, notebook: String, pageID: String) -> URL {
        switch name {
        case "render.png": return store.paths.render(notebook, pageID)
        case "text.json": return store.paths.text(notebook, pageID)
        default: return store.paths.drawing(notebook, pageID)
        }
    }

    private static func fileExists(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.path(percentEncoded: false))
    }

    /// Drops the ETags of a page (`<notebook>/pages/<id>`) whose directory
    /// just moved away.
    private func forgetBlobTags(page: String) {
        for name in Self.blobNames {
            try? store.setSyncValue(nil, for: StateKey.etag(page, name))
        }
    }

    // MARK: - State

    private func updateState() {
        let pending = (try? store.journal.pendingCount()) ?? 0
        let parked = (try? store.journal.parked()) ?? []

        let next: SaveState
        if let conflict {
            next = .conflict(pageNumber: conflict.pageNumber)
        } else if isSignedOut {
            next = .signedOut
        } else if let first = parked.first {
            next = .attention(message: Self.attentionMessage(for: first, count: parked.count))
        } else if !isOnline {
            next = .offline
        } else if pending > 0 {
            next = .syncing(count: pending)
        } else if let lastSyncedAt {
            next = .synced(at: lastSyncedAt)
        } else {
            next = .savedLocally
        }
        publish(next)
    }

    private func publish(_ next: SaveState) {
        guard next != state else { return }
        let from = Self.label(state)
        let to = Self.label(next)
        if from != to { log.info("state \(from, privacy: .public) -> \(to, privacy: .public)") }
        state = next
        onState?(next)
    }

    /// A state as the transition log names it: the case and its number, never
    /// the attention text, which can quote a path.
    static func label(_ state: SaveState) -> String {
        switch state {
        case .savedLocally: return "savedLocally"
        case .syncing(let count): return "syncing(\(count))"
        case .synced: return "synced"
        case .offline: return "offline"
        case .conflict(let pageNumber): return "conflict(page \(pageNumber))"
        case .signedOut: return "signedOut"
        case .attention: return "attention"
        }
    }

    // MARK: - Diagnostics

    /// A push that failed or was parked: which op, and for which page.
    private func recordFailure(push op: PaperOp, parked: Bool, error: any Error) {
        recordFailure(
            "push \(op.kind.rawValue) \(parked ? "parked" : "failed")",
            notebook: op.notebookPath, page: op.pageID.isEmpty ? nil : op.pageID, error: error)
    }

    /// Logs a failure the engine absorbs and keeps it as `lastError`, in
    /// memory and in the store for the next launch.
    ///
    /// What failed and its status are public, so Console can filter on them;
    /// the notebook, the page and the server's own words are private. The
    /// stored copy keeps all of it: it never leaves the device.
    private func recordFailure(
        _ what: String, notebook: String? = nil, page: String? = nil, error: any Error
    ) {
        // `stop()` cut the request short: nothing failed, and the error that
        // did is not to be overwritten by it.
        if Task.isCancelled { return }
        let status = Self.status(of: error)
        let detail = Self.describe(error)
        log.error(
            """
            \(what, privacy: .public) [\(status, privacy: .public)] \
            notebook=\(notebook ?? "-", privacy: .private) \
            page=\(page ?? "-", privacy: .private): \(detail, privacy: .private)
            """)
        var summary = what
        if let notebook { summary += " \(notebook)" }
        if let page { summary += " page \(page)" }
        summary += ": \(detail)"
        let now = clock.now
        lastError = summary
        lastErrorAt = now
        try? store.setSyncValue("\(now.formatted(.iso8601)) \(summary)", for: StateKey.lastError)
    }

    /// The part of an error a log filter can match on: an HTTP status, a
    /// `URLError` code, or the kind of error.
    static func status(of error: any Error) -> String {
        switch error {
        case let api as PaperAPIError:
            switch api {
            case .http(let status, _): return String(status)
            case .unauthorized: return "unauthorized"
            case .malformedResponse: return "malformed response"
            }
        case let url as URLError: return "URLError \(url.code.rawValue)"
        case is CancellationError: return "cancelled"
        default: return String(describing: type(of: error))
        }
    }

    static func describe(_ error: any Error) -> String {
        switch error {
        case let api as PaperAPIError: return api.summary
        case let missing as LocalFilesMissing: return missing.reason
        case let url as URLError:
            return "URLError \(url.code.rawValue): \(url.localizedDescription)"
        default: return String(describing: error)
        }
    }

    static func attentionMessage(for op: PaperOp, count: Int) -> String {
        let reason = op.parkedReason ?? "the server refused a change"
        let what: String
        switch op.kind {
        case .pagePut, .pageCreate, .pageDelete, .pageRender, .pageText: what = "a page"
        case .assetPut: what = "an attachment"
        default: what = "a notebook"
        }
        if count > 1 { return "\(count) changes not sent — \(reason)" }
        return "\(what) was not sent — \(reason)"
    }
}
