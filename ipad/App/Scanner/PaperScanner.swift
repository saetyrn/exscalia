import Foundation
import PDFKit
import PaperCore
import UIKit
import VisionKit

/// A completed multi-page camera scan. `pdfData` is ready for the existing
/// Paper PDF importer; the scanner does not write into a notebook itself.
struct PaperScanResult: Sendable {
    let pdfData: Data
    let pageCount: Int
}

enum PaperScannerError: LocalizedError, Equatable {
    case unavailable
    case alreadyScanning
    case cancelled
    case noPages
    case couldNotCreatePDF
    case failed(String)

    var errorDescription: String? {
        switch self {
        case .unavailable: return "Document scanning is not available on this iPad."
        case .alreadyScanning: return "A document scan is already in progress."
        case .cancelled: return "The document scan was cancelled."
        case .noPages: return "The scan did not contain any pages."
        case .couldNotCreatePDF: return "The scanned pages could not be made into a PDF."
        case .failed(let message): return message
        }
    }
}

/// Presents VisionKit's system scanner and converts every captured page into
/// one PDF. It is deliberately independent of PaperUI, so a notebook screen
/// can choose where to import the resulting bytes (and so tests can validate
/// its PDF conversion without constructing a Paper view).
@MainActor
final class PaperScannerCoordinator: NSObject {
    private weak var scanner: VNDocumentCameraViewController?
    private var continuation: CheckedContinuation<PaperScanResult, Error>?

    static var isSupported: Bool { VNDocumentCameraViewController.isSupported }

    /// Presents the scanner from the supplied view controller. The caller
    /// should pass the resulting bytes to `PaperController.importPDF(data:name:into:after:)`.
    func scan(from presenter: UIViewController) async throws -> PaperScanResult {
        guard Self.isSupported else { throw PaperScannerError.unavailable }
        guard continuation == nil else { throw PaperScannerError.alreadyScanning }

        let camera = VNDocumentCameraViewController()
        camera.delegate = self
        camera.modalPresentationStyle = .fullScreen
        scanner = camera

        return try await withCheckedThrowingContinuation { continuation in
            self.continuation = continuation
            presenter.present(camera, animated: true)
        }
    }

    private func finish(_ result: Result<PaperScanResult, Error>) {
        guard let continuation else { return }
        self.continuation = nil
        scanner = nil
        switch result {
        case .success(let value): continuation.resume(returning: value)
        case .failure(let error): continuation.resume(throwing: error)
        }
    }

    private func pdfData(from scan: VNDocumentCameraScan) -> Result<PaperScanResult, Error> {
        guard scan.pageCount > 0 else {
            return .failure(PaperScannerError.noPages)
        }

        let document = PDFDocument()
        for index in 0..<scan.pageCount {
            let image = scan.imageOfPage(at: index)
            // Left to itself a page takes the image's pixels as its points: a
            // camera scan becomes a ~3000x4000 pt page that renders at 2x into
            // 48 MP. Laid out on paper-sized points it imports, renders and
            // syncs like any page, and the image keeps its full resolution
            // inside the PDF. `setBounds` would crop the image, not scale it.
            let options: [PDFPage.ImageInitializationOption: Any] = [
                .mediaBox: NSValue(cgRect: Self.pageBox(for: image.size)),
                .upscaleIfSmaller: true,
            ]
            guard let page = PDFPage(image: image, options: options) else {
                return .failure(PaperScannerError.couldNotCreatePDF)
            }
            document.insert(page, at: index)
        }
        guard let data = document.dataRepresentation() else {
            return .failure(PaperScannerError.couldNotCreatePDF)
        }
        return .success(PaperScanResult(pdfData: data, pageCount: scan.pageCount))
    }

    /// The image's own shape, fitted inside whichever of A4 and Letter is
    /// closer to it, turned to match.
    static func pageBox(for imageSize: CGSize) -> CGRect {
        guard imageSize.width > 0, imageSize.height > 0 else {
            return CGRect(origin: .zero, size: PaperSize.a4.points)
        }
        let aspect = imageSize.width / imageSize.height
        let papers = [PaperSize.a4.points, PaperSize.letter.points].map { paper in
            aspect > 1 ? CGSize(width: paper.height, height: paper.width) : paper
        }
        let paper = papers.min { first, second in
            abs(first.width / first.height - aspect) < abs(second.width / second.height - aspect)
        } ?? papers[0]
        let fit = min(paper.width / imageSize.width, paper.height / imageSize.height)
        return CGRect(
            x: 0, y: 0, width: max(1, (imageSize.width * fit).rounded()),
            height: max(1, (imageSize.height * fit).rounded()))
    }
}

extension PaperScannerCoordinator: @preconcurrency VNDocumentCameraViewControllerDelegate {
    func documentCameraViewControllerDidCancel(
        _ controller: VNDocumentCameraViewController
    ) {
        controller.dismiss(animated: true) { [weak self] in
            self?.finish(.failure(PaperScannerError.cancelled))
        }
    }

    func documentCameraViewController(
        _ controller: VNDocumentCameraViewController, didFailWithError error: Error
    ) {
        let message = error.localizedDescription
        controller.dismiss(animated: true) { [weak self] in
            self?.finish(.failure(PaperScannerError.failed(message)))
        }
    }

    func documentCameraViewController(
        _ controller: VNDocumentCameraViewController, didFinishWith scan: VNDocumentCameraScan
    ) {
        let result = pdfData(from: scan)
        controller.dismiss(animated: true) { [weak self] in self?.finish(result) }
    }
}
