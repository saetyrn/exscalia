import CoreGraphics
import Foundation
import ImageIO
import PencilKit
import Vision

/// One line Vision read off a page.
///
/// `box` is in **page points with y running down from the top-left corner** --
/// the same coordinate space as `PageObject.Frame`, the renderer and every view
/// in the app. Vision hands back a normalised rectangle whose origin is the
/// bottom-left, so the conversion happens once, here, and nothing downstream
/// has to remember which way up the page is.
public struct RecognizedLine: Codable, Equatable, Sendable {
    public var text: String
    public var box: PageObject.Frame
    public var confidence: Double

    public init(text: String, box: PageObject.Frame, confidence: Double) {
        self.text = text
        self.box = box
        self.confidence = confidence
    }
}

/// An explicit correction made by a person after OCR.  `before`/`after` are
/// always retained for auditability; offsets and lineIndex are optional so a
/// correction can be authored by either a text editor or the line-based Paper
/// UI. Offsets are UTF-16 offsets in the raw transcript when present.
public struct TextCorrection: Codable, Equatable, Sendable, Identifiable {
    public var id: String
    public var before: String
    public var after: String
    public var start: Int?
    public var end: Int?
    public var lineIndex: Int?

    public init(
        id: String = PaperID.make(),
        before: String,
        after: String,
        start: Int? = nil,
        end: Int? = nil,
        lineIndex: Int? = nil
    ) {
        self.id = id
        self.before = before
        self.after = after
        self.start = start
        self.end = end
        self.lineIndex = lineIndex
    }

    /// Vocabulary used by integrations that call this an original/replacement
    /// edit rather than a before/after correction.
    public init(
        id: String = PaperID.make(),
        original: String,
        replacement: String,
        start: Int? = nil,
        end: Int? = nil,
        lineIndex: Int? = nil
    ) {
        self.init(
            id: id, before: original, after: replacement, start: start, end: end,
            lineIndex: lineIndex)
    }

    public var original: String {
        get { before }
        set { before = newValue }
    }

    public var replacement: String {
        get { after }
        set { after = newValue }
    }
}

public typealias PageTextCorrection = TextCorrection

/// An AI-generated reading attached by the server. It is intentionally
/// separate from OCR and user corrections: editing OCR must never discard the
/// AI transcript, and changing ink invalidates it by revision on the server.
public struct AITranscript: Codable, Equatable, Sendable {
    public var text: String
    public var model: String
    public var at: Date?
    public var revision: Int

    public init(text: String, model: String = "", at: Date? = nil, revision: Int = 0) {
        self.text = text
        self.model = model
        self.at = at
        self.revision = revision
    }
}

public typealias TranscriptAI = AITranscript

/// A correction whose ink has changed since it was made. It no longer
/// describes the page, so nothing searches it, but it is the user's typing
/// and is kept rather than dropped when a fresh reading replaces the old one.
public struct StaleCorrection: Codable, Equatable, Sendable {
    public var correctedTranscript: String?
    public var corrections: [TextCorrection]
    /// The ink revision it was made for.
    public var correctionsRevision: Int?

    public init(
        correctedTranscript: String?, corrections: [TextCorrection] = [],
        correctionsRevision: Int? = nil
    ) {
        self.correctedTranscript = correctedTranscript
        self.corrections = corrections
        self.correctionsRevision = correctionsRevision
    }

    private enum CodingKeys: String, CodingKey {
        case correctedTranscript = "corrected_transcript", corrections
        case correctionsRevision = "corrections_revision"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        correctedTranscript = try container.decodeIfPresent(
            String.self, forKey: .correctedTranscript)
        corrections =
            try container.decodeIfPresent([TextCorrection].self, forKey: .corrections) ?? []
        correctionsRevision = try container.decodeIfPresent(Int.self, forKey: .correctionsRevision)
    }
}

/// Exactly the derived `text.json` of the frozen vault layout. Raw OCR and
/// explicit user corrections are local source/derived layers; `transcript_ai`
/// is carried through so a correction never discards the server's AI reading.
public struct PageText: Codable, Equatable, Sendable {
    /// Raw OCR lines. The previous schema called this field `lines`, which is
    /// kept as the canonical JSON key for migration compatibility.
    public var lines: [RecognizedLine]
    /// Raw OCR transcript. Never replaced by a user correction.
    public var transcript: String

    /// Optional text explicitly corrected by the user. When present this is
    /// the text used by `TextIndex`; raw OCR remains available above.
    public var correctedTranscript: String?
    public var corrections: [TextCorrection]
    /// AI reading retained alongside OCR and corrections.
    public var transcriptAI: AITranscript?
    /// Revision of the ink to which the corrections apply. Optional for
    /// compatibility with text.json written before correction metadata.
    public var correctionsRevision: Int?
    /// Revision of the ink the raw OCR was read from. `nil` in text.json
    /// written before it was recorded.
    public var forRevision: Int?
    /// The previous correction, once new ink made it describe strokes that
    /// are no longer the page. Never searched.
    public var staleCorrection: StaleCorrection?

    public init(
        lines: [RecognizedLine],
        transcript: String? = nil,
        correctedTranscript: String? = nil,
        corrections: [TextCorrection] = [],
        transcriptAI: AITranscript? = nil,
        correctionsRevision: Int? = nil,
        forRevision: Int? = nil,
        staleCorrection: StaleCorrection? = nil
    ) {
        self.lines = lines
        self.transcript = transcript ?? lines.map(\.text).joined(separator: "\n")
        self.correctedTranscript = correctedTranscript
        self.corrections = corrections
        self.transcriptAI = transcriptAI
        self.correctionsRevision = correctionsRevision
        self.forRevision = forRevision
        self.staleCorrection = staleCorrection
    }

    /// Explicit raw naming for new callers; `lines` and `transcript` remain
    /// source-compatible with the original PaperCore API.
    public init(
        rawLines: [RecognizedLine],
        rawTranscript: String? = nil,
        correctedTranscript: String? = nil,
        corrections: [TextCorrection] = [],
        transcriptAI: AITranscript? = nil,
        correctionsRevision: Int? = nil
    ) {
        self.init(
            lines: rawLines, transcript: rawTranscript,
            correctedTranscript: correctedTranscript, corrections: corrections,
            transcriptAI: transcriptAI, correctionsRevision: correctionsRevision)
    }

    public var rawLines: [RecognizedLine] {
        get { lines }
        set { lines = newValue }
    }

    public var rawTranscript: String {
        get { transcript }
        set { transcript = newValue }
    }

    /// Text that should be searchable and shown as the preferred transcript.
    /// A corrected transcript wins; otherwise explicit corrections are applied
    /// to raw OCR on demand.
    public var searchableTranscript: String {
        correctedTranscript ?? applying(corrections, to: transcript)
    }

    public var preferredTranscript: String { searchableTranscript }

    /// Whether a person has corrected this reading.
    public var hasCorrection: Bool { correctedTranscript != nil || !corrections.isEmpty }

    /// Lines paired with their original boxes, but with explicit corrections
    /// applied where possible. This lets a search result show corrected text
    /// without losing the raw OCR geometry.
    public var searchableLines: [RecognizedLine] {
        let corrected = searchableTranscript
            .split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        let count = max(lines.count, corrected.count)
        return (0..<count).map { index in
            let fallback = RecognizedLine(
                text: "", box: PageObject.Frame(x: 0, y: 0, width: 0, height: 0), confidence: 0)
            var copy = lines.indices.contains(index) ? lines[index] : fallback
            if corrected.indices.contains(index) { copy.text = corrected[index] }
            return copy
        }
    }

    /// Whether there is no text to search. A correction-only payload is still
    /// meaningful, even when an older/imported source had no raw transcript.
    public var isEmpty: Bool {
        searchableTranscript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private enum CodingKeys: String, CodingKey {
        case lines, transcript, correctedTranscript = "corrected_transcript"
        case corrections, transcriptAI = "transcript_ai"
        case correctionsRevision = "corrections_revision"
        case forRevision = "for_revision", staleCorrection = "stale_correction"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        lines = try container.decodeIfPresent([RecognizedLine].self, forKey: .lines) ?? []
        transcript = try container.decodeIfPresent(String.self, forKey: .transcript)
            ?? lines.map(\.text).joined(separator: "\n")
        correctedTranscript = try container.decodeIfPresent(String.self, forKey: .correctedTranscript)
        corrections = try container.decodeIfPresent([TextCorrection].self, forKey: .corrections) ?? []
        transcriptAI = try container.decodeIfPresent(AITranscript.self, forKey: .transcriptAI)
        correctionsRevision = try container.decodeIfPresent(Int.self, forKey: .correctionsRevision)
        forRevision = try container.decodeIfPresent(Int.self, forKey: .forRevision)
        staleCorrection = try container.decodeIfPresent(
            StaleCorrection.self, forKey: .staleCorrection)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(lines, forKey: .lines)
        try container.encode(transcript, forKey: .transcript)
        // Always written, null and empty when there is none: the server keeps
        // a correction the upload says nothing about, so "Use original" has to
        // say so out loud.
        if let correctedTranscript {
            try container.encode(correctedTranscript, forKey: .correctedTranscript)
        } else {
            try container.encodeNil(forKey: .correctedTranscript)
        }
        try container.encode(corrections, forKey: .corrections)
        try container.encodeIfPresent(transcriptAI, forKey: .transcriptAI)
        try container.encodeIfPresent(correctionsRevision, forKey: .correctionsRevision)
        try container.encodeIfPresent(forRevision, forKey: .forRevision)
        try container.encodeIfPresent(staleCorrection, forKey: .staleCorrection)
    }

    private func applying(_ corrections: [TextCorrection], to raw: String) -> String {
        guard !corrections.isEmpty else { return raw }
        var result = raw

        // Line-index edits are easiest to understand and preserve line boxes.
        var lines = result.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        var appliedLineCorrection = false
        for correction in corrections {
            guard let lineIndex = correction.lineIndex, lines.indices.contains(lineIndex) else { continue }
            lines[lineIndex] = replace(correction, in: lines[lineIndex])
            appliedLineCorrection = true
        }
        if appliedLineCorrection { result = lines.joined(separator: "\n") }

        // Full-transcript UTF-16 offsets are stable even when a correction's
        // spelling occurs more than once.
        let positioned = corrections
            .filter { $0.lineIndex == nil && $0.start != nil }
            .sorted { ($0.start ?? 0) > ($1.start ?? 0) }
        for correction in positioned {
            guard let start = correction.start, start >= 0 else { continue }
            let end = correction.end ?? (start + correction.before.utf16.count)
            guard end >= start else { continue }
            let utf16 = result.utf16
            guard end <= utf16.count else { continue }
            let from = String.Index(utf16Offset: start, in: result)
            let to = String.Index(utf16Offset: end, in: result)
            guard String(result[from..<to]) == correction.before else { continue }
            result.replaceSubrange(from..<to, with: correction.after)
        }

        // A correction without a position is still useful for simple line
        // editor clients. Replace one occurrence, never every matching word.
        for correction in corrections where correction.lineIndex == nil && correction.start == nil {
            if let range = result.range(of: correction.before) {
                result.replaceSubrange(range, with: correction.after)
            }
        }
        return result
    }

    private func replace(_ correction: TextCorrection, in line: String) -> String {
        if let start = correction.start, start >= 0 {
            let end = correction.end ?? (start + correction.before.utf16.count)
            let utf16 = line.utf16
            if end >= start, end <= utf16.count {
                let from = String.Index(utf16Offset: start, in: line)
                let to = String.Index(utf16Offset: end, in: line)
                guard String(line[from..<to]) == correction.before else { return line }
                var copy = line
                copy.replaceSubrange(from..<to, with: correction.after)
                return copy
            }
        }
        guard let range = line.range(of: correction.before) else { return line }
        var copy = line
        copy.replaceSubrange(range, with: correction.after)
        return copy
    }
}

public enum RecognitionError: Error, Equatable, Sendable {
    case imageUnavailable
}

/// Stable seam for recognition engines. PaperCore ships the Vision fallback;
/// an iOS app may later provide a PencilKit/native recognizer without making
/// this package import an SDK symbol that is unavailable to the macOS test
/// target. Selection recognition uses the same image/page-space contract.
public protocol HandwritingRecognitionBackend: Sendable {
    func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText
}

/// Current offline fallback, available on both iOS and macOS through Vision.
public struct VisionRecognitionBackend: HandwritingRecognitionBackend {
    public init() {}

    public func recognize(image: CGImage, pageSize: CGSize, languages: [String]) throws -> PageText {
        try HandwritingRecognizer.recognizeWithVision(
            image: image, pageSize: pageSize, languages: languages)
    }
}

/// A region passed to a future selection recognizer. No PencilKit types cross
/// this boundary; callers can render their selected strokes to a CGImage.
public struct RecognitionSelection: Codable, Equatable, Sendable {
    public var pageRect: PageObject.Frame

    public init(pageRect: PageObject.Frame) { self.pageRect = pageRect }
}

/// What a recognition run needs to know. Small and `Sendable` so it can cross
/// to a background priority without dragging the store behind it.
public struct RecognitionJob: Sendable, Equatable {
    public var notebookPath: String
    public var pageID: String
    /// The page's real size in points, which is what the boxes are expressed in.
    public var pageSize: CGSize
    /// The revision the recognised text describes, stamped onto the upload.
    public var forRevision: Int

    public init(notebookPath: String, pageID: String, pageSize: CGSize, forRevision: Int) {
        self.notebookPath = notebookPath
        self.pageID = pageID
        self.pageSize = pageSize
        self.forRevision = forRevision
    }

    public init(notebookPath: String, page: Page) {
        self.init(
            notebookPath: notebookPath, pageID: page.id, pageSize: page.pointSize,
            forRevision: max(page.revision, 1))
    }
}

/// Reads a page's handwriting, on the device, and files the result.
///
/// Three rules shape it, all from §1 of the plan:
///
/// 1. **Derived, never source.** Losing `text.json` costs a re-read, never
///    ink, so a failed run is simply not retried until the next flush.
/// 2. **Never on the input path.** A run starts five seconds after the last
///    change and is cancelled outright the moment the page turns dirty again:
///    somebody who is still writing gets the pen, not the CPU.
/// 3. **Search is navigation.** The boxes are kept, not just the words,
///    because a result has to be able to point at the spot on the page.
public actor HandwritingRecognizer {
    /// How long a page has to sit still before it is worth reading. Long
    /// enough that a pause mid-sentence does not trigger a run, short enough
    /// that "write, then search" works in one breath.
    public static let idleDelay: TimeInterval = 5

    /// What Vision is asked for when the user has expressed no preference.
    /// Both spellings, because the recognisers differ and a British writer's
    /// notes should not come back Americanised.
    public static let defaultLanguages = ["en-GB", "en-US"]

    private let store: PaperStore
    private let index: TextIndex
    private let backend: any HandwritingRecognitionBackend
    private let isDirty: @Sendable (String) -> Bool
    private let languages: @Sendable () -> [String]
    private let idleDelay: TimeInterval
    private var pending: [String: Task<Void, Never>] = [:]
    /// The newest pass over each page. A board pass yields between tiles, so
    /// a newer pass can start before an older one ends; only the newest may
    /// file what it read.
    private var passes: [String: Int] = [:]
    private var lastPass = 0
    private var onRecognized: (@Sendable (RecognitionJob) -> Void)?

    public init(
        store: PaperStore,
        isDirty: @escaping @Sendable (String) -> Bool = { _ in false },
        languages: @escaping @Sendable () -> [String] = { HandwritingRecognizer.defaultLanguages },
        idleDelay: TimeInterval = HandwritingRecognizer.idleDelay,
        backend: any HandwritingRecognitionBackend = VisionRecognitionBackend()
    ) {
        self.store = store
        self.index = TextIndex(database: store.database)
        self.backend = backend
        self.isDirty = isDirty
        self.languages = languages
        self.idleDelay = idleDelay
    }

    /// Called after a page's text has been written and indexed, so the caller
    /// can journal the derived-only upload.
    public func setOnRecognized(_ handler: @escaping @Sendable (RecognitionJob) -> Void) {
        onRecognized = handler
    }

    /// The render for `job` has landed. Read the page once it has been still
    /// for `idleDelay`; a second call for the same page replaces the first.
    public func schedule(_ job: RecognitionJob) {
        pending[job.pageID]?.cancel()
        let seconds = idleDelay
        pending[job.pageID] = Task(priority: .background) { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.run(job)
        }
    }

    /// The page changed again; whatever was queued describes ink that no
    /// longer exists.
    public func cancel(pageID: String) {
        pending[pageID]?.cancel()
        pending.removeValue(forKey: pageID)
    }

    public func cancelAll() {
        for task in pending.values { task.cancel() }
        pending.removeAll()
    }

    /// Reads the page now and files the result. Returns what it read, so a
    /// test (and the DEBUG launch argument) does not have to wait five seconds
    /// to find out.
    @discardableResult
    public func recognizeNow(_ job: RecognitionJob) async -> PageText? {
        await run(job)
    }

    @discardableResult
    private func run(_ queued: RecognitionJob) async -> PageText? {
        pending.removeValue(forKey: queued.pageID)
        // Where the page is now: a folder another device moved, or a rename,
        // takes it along, and a reading filed at the old path would make the
        // old directories again.
        guard let path = store.currentNotebookPath(ofPage: queued.pageID, near: queued.notebookPath)
        else { return nil }
        var job = queued
        job.notebookPath = path
        lastPass += 1
        let pass = lastPass
        passes[job.pageID] = pass
        defer { if passes[job.pageID] == pass { passes.removeValue(forKey: job.pageID) } }
        // Reading a page that is being written on would file a transcript of
        // ink that is already out of date.
        guard !isDirty(job.pageID) else { return nil }
        var read: PageText?
        if let page = try? store.page(notebookPath: job.notebookPath, id: job.pageID),
            page.size == .board
        {
            read = await recognizeBoard(job, page: page, pass: pass)
        } else {
            guard let image = Self.renderImage(at: store.paths.render(job.notebookPath, job.pageID))
            else { return nil }
            read = try? backend.recognize(
                image: image, pageSize: job.pageSize, languages: languages())
        }
        guard var text = read, passes[job.pageID] == pass else { return nil }
        guard !isDirty(job.pageID) else { return nil }
        text.forRevision = job.forRevision

        do {
            try store.writePageText(text, notebookPath: job.notebookPath, pageID: job.pageID)
            // Indexed as filed: a correction the store carried forward is
            // what search should find, one it set aside as stale is not.
            let filed = store.pageText(notebookPath: job.notebookPath, pageID: job.pageID) ?? text
            try index.upsert(
                notebookPath: job.notebookPath, pageID: job.pageID, text: filed,
                forRevision: job.forRevision)
        } catch {
            // Derived data: the next flush tries again.
            return nil
        }
        onRecognized?(job)
        return text
    }

    // MARK: - Boards

    /// Reads a board without its `render.png`, which shows the board at
    /// whatever scale its content allowed: the ink is cut into 1,024-point
    /// tiles (`BoardGeometry.recognitionTiles`), each drawn from the drawing
    /// at 2x, ink only, and read by itself. A line is kept by the tile whose
    /// core holds the middle of its box, and its box is moved into canvas
    /// space. Nil when the page turns dirty, a newer pass starts or a tile
    /// cannot be read, so a half-read board never replaces the last whole
    /// reading.
    private func recognizeBoard(_ job: RecognitionJob, page: Page, pass: Int) async -> PageText? {
        guard let data = store.drawingData(notebookPath: job.notebookPath, pageID: job.pageID),
            !data.isEmpty
        else { return PageText(lines: []) }
        guard let drawing = try? PKDrawing(data: data) else { return nil }
        let tiles = BoardGeometry.recognitionTiles(content: drawing.strokes.map(\.renderBounds))
        let dark = PaperAppearance.isDark(page: page)
        let languages = languages()
        var lines: [RecognizedLine] = []
        for tile in tiles {
            // One tile at a time, letting a newer schedule or a cancel in
            // between: a board is up to 64 Vision passes.
            await Task.yield()
            guard !Task.isCancelled, !isDirty(job.pageID), passes[job.pageID] == pass else {
                return nil
            }
            guard
                let image = PageRenderer.recognitionImage(drawing, rect: tile.rect, dark: dark),
                let read = try? backend.recognize(
                    image: image, pageSize: tile.rect.size, languages: languages)
            else { return nil }
            lines += Self.boardLines(read.lines, in: tile)
        }
        lines.sort { ($0.box.y, $0.box.x) < ($1.box.y, $1.box.x) }
        return PageText(lines: lines)
    }

    /// `lines` read off `tile`, in canvas space, less those another tile
    /// answers for.
    nonisolated static func boardLines(
        _ lines: [RecognizedLine], in tile: BoardGeometry.RecognitionTile
    ) -> [RecognizedLine] {
        lines.compactMap { line in
            var moved = line
            moved.box.x += Double(tile.rect.minX)
            moved.box.y += Double(tile.rect.minY)
            return tile.keeps(moved.box.rect) ? moved : nil
        }
    }

    // MARK: - Vision

    /// Runs `VNRecognizeTextRequest` over `image` and returns the lines in page
    /// points. Synchronous and free of the store, so the tests can hand it a
    /// bitmap and assert the geometry directly.
    public nonisolated static func recognize(
        image: CGImage, pageSize: CGSize, languages: [String] = defaultLanguages
    ) throws -> PageText {
        try recognizeWithVision(image: image, pageSize: pageSize, languages: languages)
    }

    /// The implementation behind the shipped backend. Kept separate from the
    /// public static convenience so custom backends can coexist with the
    /// existing API and tests.
    public nonisolated static func recognizeWithVision(
        image: CGImage, pageSize: CGSize, languages: [String] = defaultLanguages
    ) throws -> PageText {
        guard pageSize.width > 0, pageSize.height > 0 else {
            throw RecognitionError.imageUnavailable
        }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = true
        request.automaticallyDetectsLanguage = true
        let supported = (try? request.supportedRecognitionLanguages()) ?? []
        let requested = languages.filter { supported.isEmpty || supported.contains($0) }
        if !requested.isEmpty { request.recognitionLanguages = requested }

        let handler = VNImageRequestHandler(cgImage: image, options: [:])
        try handler.perform([request])

        var lines: [RecognizedLine] = []
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty else { continue }
            lines.append(
                RecognizedLine(
                    text: text,
                    box: pageBox(observation.boundingBox, pageSize: pageSize),
                    confidence: Double(candidate.confidence)))
        }
        // Vision returns observations in reading order already; sorting by the
        // top edge keeps a transcript sane when it does not.
        lines.sort { $0.box.y < $1.box.y }
        return PageText(lines: lines)
    }

    /// Vision's normalised, bottom-left rectangle in page points, y down.
    static func pageBox(_ normalized: CGRect, pageSize: CGSize) -> PageObject.Frame {
        PageObject.Frame(
            x: Double(normalized.minX * pageSize.width),
            y: Double((1 - normalized.maxY) * pageSize.height),
            width: Double(normalized.width * pageSize.width),
            height: Double(normalized.height * pageSize.height))
    }

    /// The page's `render.png`, decoded. `ImageIO` rather than UIKit so this
    /// file builds and runs on the Mac, where `swift test` lives.
    public nonisolated static func renderImage(at url: URL) -> CGImage? {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    }
}
