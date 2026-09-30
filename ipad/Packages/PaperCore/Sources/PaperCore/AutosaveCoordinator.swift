import Foundation

/// When the debounced flush happens. Injected so the tests can drive time
/// rather than wait for it.
@MainActor
public protocol AutosaveScheduling: AnyObject {
    func schedule(after seconds: TimeInterval, work: @escaping @MainActor () -> Void)
    func cancel()
}

/// The production scheduler: one pending main-queue work item, replaced on
/// every keystroke of the pen.
@MainActor
public final class DispatchAutosaveScheduler: AutosaveScheduling {
    private var pending: DispatchWorkItem?

    public init() {}

    public func schedule(after seconds: TimeInterval, work: @escaping @MainActor () -> Void) {
        cancel()
        let item = DispatchWorkItem { MainActor.assumeIsolated { work() } }
        pending = item
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: item)
    }

    public func cancel() {
        pending?.cancel()
        pending = nil
    }
}

/// A scheduler that never fires on its own. Tests call `fire()`.
@MainActor
public final class ManualAutosaveScheduler: AutosaveScheduling {
    public private(set) var scheduledDelay: TimeInterval?
    private var work: (@MainActor () -> Void)?

    public init() {}

    public var isScheduled: Bool { work != nil }

    public func schedule(after seconds: TimeInterval, work: @escaping @MainActor () -> Void) {
        scheduledDelay = seconds
        self.work = work
    }

    public func cancel() {
        work = nil
        scheduledDelay = nil
    }

    public func fire() {
        let pending = work
        work = nil
        pending?()
    }
}

/// Which page a change belongs to. Carries the notebook path because the file
/// layout is keyed by path, and the notebook id because the index is.
public struct PageRef: Hashable, Sendable {
    public let notebookID: String
    public let notebookPath: String
    public let pageID: String

    public init(notebookID: String, notebookPath: String, pageID: String) {
        self.notebookID = notebookID
        self.notebookPath = notebookPath
        self.pageID = pageID
    }
}

/// The thing that makes "never wonder whether it saved" true.
///
/// `canvasViewDrawingDidChange` calls `markDirty`; one second after the last
/// change (or immediately on background, on a page leaving the live window, or
/// before an export) the drawing is written, snapshotted, and journalled. It
/// never touches the network -- that is the M3 seam, and it is why a flush
/// cannot fail because of a server.
@MainActor
public final class AutosaveCoordinator {
    /// Debounce window. Short enough that a killed app loses nothing a person
    /// would notice, long enough that a fast writer is not doing disk IO per
    /// stroke.
    public static let debounce: TimeInterval = 1.0
    /// A write that failed (full disk, protected data while locked) is tried
    /// again after this long, doubling per failure up to `retryCap`.
    public static let retryDelay: TimeInterval = 2.0
    public static let retryCap: TimeInterval = 30.0

    private let store: PaperStore
    private let snapshots: SnapshotRing
    private let scheduler: AutosaveScheduling
    /// Hands back the current `PKDrawing.dataRepresentation()` for a page. The
    /// coordinator never sees PencilKit types, which is what lets it be tested
    /// without a canvas.
    private let drawingData: @MainActor (PageRef) -> Data?

    private var dirty: Set<PageRef> = []
    /// The next retry delay of every page whose last write failed.
    private var retryDelays: [PageRef: TimeInterval] = [:]

    /// What the pill shows. Set on the main actor, observed by the UI.
    public private(set) var saveState: SaveState = .savedLocally
    public var onSaveStateChange: (@MainActor (SaveState) -> Void)?
    /// Called after a page's bytes hit the disk, so the renderer can run.
    public var onFlushed: (@MainActor (PageRef) -> Void)?

    public init(
        store: PaperStore,
        snapshots: SnapshotRing,
        scheduler: AutosaveScheduling = DispatchAutosaveScheduler(),
        drawingData: @escaping @MainActor (PageRef) -> Data?
    ) {
        self.store = store
        self.snapshots = snapshots
        self.scheduler = scheduler
        self.drawingData = drawingData
    }

    public var hasUnsavedChanges: Bool { !dirty.isEmpty }
    public func isDirty(_ ref: PageRef) -> Bool { dirty.contains(ref) }

    /// The canvas changed. Cheap on purpose: this is on the input path.
    public func markDirty(_ ref: PageRef) {
        dirty.insert(ref)
        scheduler.schedule(after: Self.debounce) { [weak self] in
            self?.flushAll()
        }
    }

    /// Flushes one page now -- used when it leaves the live window, so the
    /// canvas can be recycled without the ink living only in a view.
    public func flush(_ ref: PageRef) {
        guard dirty.contains(ref) else { return }
        dirty.remove(ref)
        write(ref)
        if dirty.isEmpty {
            scheduler.cancel()
            publish(.savedLocally)
        }
    }

    /// Flushes everything. Called by the debounce, on resign-active and on
    /// background (the caller wraps it in `beginBackgroundTask`).
    public func flushAll() {
        scheduler.cancel()
        let pending = dirty
        dirty.removeAll()
        for ref in pending { write(ref) }
        if dirty.isEmpty { publish(.savedLocally) }
    }

    private func write(_ ref: PageRef) {
        // Drawing first, then page.json: a crash between the two leaves the ink
        // present and the metadata stale, which the reconciler can repair. The
        // other order would leave metadata claiming ink that is not there.
        if let data = drawingData(ref) {
            do {
                try store.writeDrawing(data, notebookPath: ref.notebookPath, pageID: ref.pageID)
            } catch {
                // The newest ink exists only in the canvas. Journalling a put
                // now would upload the previous drawing as if it were current.
                retry(ref, after: error)
                return
            }
            // The ink is on disk; a missing history entry does not unsave it.
            _ = try? snapshots.push(data, pageID: ref.pageID)
        }
        retryDelays[ref] = nil
        // One locked read-modify-write: a revision the sync engine records
        // meanwhile is not written back over with the one read here.
        _ = try? store.updatePage(notebookPath: ref.notebookPath, pageID: ref.pageID) {
            $0.modified = Date()
        }
        // Queued for M3; nothing in this milestone drains the journal.
        _ = try? store.journal.append(
            .init(kind: .pagePut, notebookPath: ref.notebookPath, pageID: ref.pageID))
        onFlushed?(ref)
    }

    /// Keeps the page dirty, so `hasUnsavedChanges` stays true for the
    /// background flush, and tells the pill.
    private func retry(_ ref: PageRef, after error: Error) {
        dirty.insert(ref)
        let delay = retryDelays[ref] ?? Self.retryDelay
        retryDelays[ref] = min(delay * 2, Self.retryCap)
        scheduler.schedule(after: delay) { [weak self] in
            self?.flushAll()
        }
        publish(.attention(message: "could not save page — \(error.localizedDescription)"))
    }

    private func publish(_ state: SaveState) {
        guard state != saveState else { return }
        saveState = state
        onSaveStateChange?(state)
    }
}
