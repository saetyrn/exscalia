import PDFKit
import PaperCore
import UIKit

/// The imported PDF page a page annotates, drawn behind everything else.
///
/// PDFKit is asked for the page once, at 2x, off the main thread, and the
/// result is cached beside the page as the derived file `pdf-2x.png`: scrolling
/// a fifty-page import must not re-rasterise a PDF page every time it comes
/// back into the live window. The cache is derived data -- deleting it costs one
/// redraw -- and the imported PDF itself is only ever read.
final class PDFBackgroundLayer: UIView {
    /// Same scale as `render.png`, for the same reason: legible at page width
    /// on a Retina display without holding a full-resolution bitmap per page.
    static let scale: CGFloat = 2
    static let cacheName = "pdf-2x.png"

    private let imageView = UIImageView()
    private var loadedKey: String?

    override init(frame: CGRect) {
        super.init(frame: frame)
        isUserInteractionEnabled = false
        imageView.contentMode = .scaleToFill
        addSubview(imageView)
        isHidden = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("PDFBackgroundLayer is created in code") }

    override func layoutSubviews() {
        super.layoutSubviews()
        imageView.frame = bounds
    }

    /// Shows the page `binding` points at. Cheap to call repeatedly: the same
    /// binding is loaded once.
    func load(binding: PDFBinding?, pageDirectory: URL, assetURL: URL?, pageSize: CGSize) {
        guard let binding, let assetURL else {
            isHidden = true
            imageView.image = nil
            loadedKey = nil
            return
        }
        isHidden = false
        let key = "\(binding.asset)#\(binding.pageIndex)"
        guard key != loadedKey else { return }
        loadedKey = key

        let cache = pageDirectory.appending(path: Self.cacheName, directoryHint: .notDirectory)
        if let data = try? Data(contentsOf: cache), let image = UIImage(data: data) {
            imageView.image = image
            return
        }

        let index = binding.pageIndex
        let scale = Self.scale
        Task.detached(priority: .userInitiated) {
            guard let png = Self.renderPNG(assetURL: assetURL, pageIndex: index, size: pageSize, scale: scale)
            else { return }
            try? AtomicFile.write(png, to: cache)
            let image = UIImage(data: png)
            await MainActor.run { [weak self] in
                guard let self, self.loadedKey == key else { return }
                self.imageView.image = image
            }
        }
    }

    /// Rasterises one page of a PDF. Off the main actor: a fifty-page import
    /// must never make the pen wait.
    private nonisolated static func renderPNG(
        assetURL: URL, pageIndex: Int, size: CGSize, scale: CGFloat
    ) -> Data? {
        guard let document = PDFDocument(url: assetURL), pageIndex >= 0,
            pageIndex < document.pageCount, let page = document.page(at: pageIndex)
        else { return nil }
        let pixelSize = CGSize(width: size.width * scale, height: size.height * scale)
        guard pixelSize.width >= 1, pixelSize.height >= 1 else { return nil }

        let format = UIGraphicsImageRendererFormat.preferred()
        format.scale = 1
        format.opaque = false
        let renderer = UIGraphicsImageRenderer(size: pixelSize, format: format)
        let image = renderer.image { context in
            let cgContext = context.cgContext
            cgContext.translateBy(x: 0, y: pixelSize.height)
            cgContext.scaleBy(x: scale, y: -scale)
            let bounds = page.bounds(for: .mediaBox)
            let rotated = page.rotation % 180 == 0
                ? bounds.size
                : CGSize(width: bounds.height, height: bounds.width)
            guard rotated.width > 0, rotated.height > 0 else { return }
            cgContext.scaleBy(x: size.width / rotated.width, y: size.height / rotated.height)
            page.draw(with: .mediaBox, to: cgContext)
        }
        return image.pngData()
    }
}
