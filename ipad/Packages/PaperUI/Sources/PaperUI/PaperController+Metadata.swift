import Foundation
import PaperCore

extension PaperController {
    /// Stable link target: ids survive a notebook rename, while the optional
    /// page-space rectangle returns the reader to the exact handwriting.
    public func durableLink(notebookID: String, pageID: String, rect: CGRect? = nil) -> URL? {
        guard let origin = api?.origin else { return nil }
        var components = URLComponents(url: origin, resolvingAgainstBaseURL: false)
        components?.percentEncodedPath =
            "/paper/id/\(PaperAPI.encode(segment: notebookID))/\(PaperAPI.encode(segment: pageID))"
        if let rect {
            let region = [rect.minX, rect.minY, rect.width, rect.height]
                .map { String(format: "%.4f", Double($0)) }
                .joined(separator: ",")
            components?.queryItems = [URLQueryItem(name: "region", value: region)]
        }
        return components?.url
    }

    /// Page labels and bookmarks are source metadata. They bypass the ink
    /// autosave debounce, so changing a label in one window can never ask a
    /// different window for drawing bytes.
    public func setPageTitle(_ title: String, notebookPath: String, pageID: String) {
        guard let notebook = try? store.notebook(path: notebookPath) else { return }
        let cleaned = title.trimmingCharacters(in: .whitespacesAndNewlines)
        _ = try? store.setPageTitle(
            notebookID: notebook.id,
            pageID: pageID,
            title: cleaned.isEmpty ? nil : cleaned)
        if let sync { Task { await sync.kick() } }
    }

    public func setPageBookmarked(_ bookmarked: Bool, notebookPath: String, pageID: String) {
        guard let notebook = try? store.notebook(path: notebookPath) else { return }
        _ = try? store.setPageBookmarked(
            notebookID: notebook.id, pageID: pageID, bookmarked)
        if let sync { Task { await sync.kick() } }
    }

    /// Writes only the correction layer in text.json, retaining raw OCR, its
    /// boxes and any server AI transcript. Search is refreshed immediately.
    public func setCorrectedTranscript(
        _ corrected: String?, notebookPath: String, pageID: String
    ) {
        guard let notebook = try? store.notebook(path: notebookPath),
            let text = store.pageText(notebookPath: notebookPath, pageID: pageID)
        else { return }
        let corrections = corrected.map {
            [
                TextCorrection(
                    before: text.rawTranscript,
                    after: $0,
                    start: 0,
                    end: text.rawTranscript.utf16.count)
            ]
        } ?? []
        _ = try? store.correctPageText(
            notebookID: notebook.id,
            pageID: pageID,
            correctedTranscript: corrected,
            corrections: corrections)
        if let sync { Task { await sync.kick() } }
    }

    enum SelectionActionError: LocalizedError {
        case unavailable
        case rejected(String)
        case malformed
        /// The client stopped waiting; the server has not stopped working.
        case stillRunning

        var errorDescription: String? {
            switch self {
            case .unavailable: return "OMNIA is not available for this server."
            case .rejected(let detail): return detail
            case .malformed: return "The server returned an unreadable OMNIA response."
            case .stillRunning: return "The reply will appear in OMNIA."
            }
        }
    }

    /// Sends a lasso crop as an ephemeral multipart body. The server embeds it
    /// in one OMNIA turn, but never writes the image into the notebook or
    /// attachment vault.
    ///
    /// Every action opens its own thread, named here before it is sent, so a
    /// turn the client stops waiting for still has a thread to open. That one
    /// is kept for the notebook's next action: trying again lands beside the
    /// turn that is still running rather than in a second thread.
    func runSelectionAction(
        action: SelectionAIAction,
        notebookID: String,
        pageID: String,
        rect: CGRect,
        image: Data,
        prompt: String? = nil
    ) async throws -> SelectionActionResponse {
        guard let api else { throw SelectionActionError.unavailable }
        let region = [rect.minX, rect.minY, rect.width, rect.height]
            .map { String(format: "%.4f", Double($0)) }
            .joined(separator: ",")
        func send(threadID: String?) async throws -> SelectionActionResponse {
            try await api.selectionAction(
                notebookID: notebookID, pageID: pageID, action: action.rawValue,
                region: region, prompt: prompt, threadID: threadID, image: image)
        }
        var threadID: String? = selectionThreads.removeValue(forKey: notebookID) ?? PaperID.make()
        do {
            do {
                return try await send(threadID: threadID)
            } catch let error as PaperAPIError where error.status == 404 {
                // A server that predates client-named threads knows no thread
                // by this id; it names its own. It checks the page before the
                // thread, so nothing ran.
                threadID = nil
                return try await send(threadID: nil)
            }
        } catch let error as PaperAPIError {
            switch error {
            case .unauthorized:
                // `PaperAPI` has already told the app, which is signing out.
                throw SelectionActionError.rejected("Signed out. Sign in to use OMNIA.")
            case .http(let status, let body):
                throw SelectionActionError.rejected(
                    PaperAPI.detailMessage(in: body) ?? "OMNIA request failed (\(status)).")
            case .malformedResponse:
                throw SelectionActionError.malformed
            }
        } catch let error as URLError where error.code == .timedOut {
            selectionThreads[notebookID] = threadID
            throw SelectionActionError.stillRunning
        }
    }
}
