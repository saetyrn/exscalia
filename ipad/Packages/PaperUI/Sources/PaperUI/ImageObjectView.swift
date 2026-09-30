import PaperCore
import UIKit

/// An image object: a photo, a pasted picture, or something dropped onto the
/// page.
///
/// The bytes live once in `assets/` under their content hash; the object only
/// references them, plus the crop rectangle that says which part of them shows.
/// Cropping never rewrites the asset, so undoing a crop is free and the same
/// picture used on two pages is still one file.
final class ImageObjectView: ObjectView {
    private let imageView = UIImageView()
    private var source: UIImage?

    /// While true the handles move the crop rather than the frame.
    var isCropping = false {
        didSet {
            guard isCropping != oldValue else { return }
            delegate?.objectViewDidChangeCropMode(self)
        }
    }

    init(object: PageObject, image: UIImage?) {
        self.source = image
        super.init(object: object)
        imageView.contentMode = .scaleToFill
        imageView.clipsToBounds = true
        imageView.isUserInteractionEnabled = false
        imageView.accessibilityIgnoresInvertColors = true
        content.addSubview(imageView)
        applyImage()
    }

    override func layoutSubviews() {
        super.layoutSubviews()
        imageView.frame = content.bounds
    }

    override func contentDidChange() {
        applyImage()
    }

    func setImage(_ image: UIImage?) {
        source = image
        applyImage()
    }

    private func applyImage() {
        guard let source else {
            imageView.image = nil
            imageView.backgroundColor = UIColor.secondarySystemFill
            imageView.accessibilityLabel = "Image, not available on this device yet"
            return
        }
        imageView.backgroundColor = .clear
        imageView.accessibilityLabel = "Image"
        guard let crop = object.crop, let cgImage = source.cgImage else {
            imageView.image = source
            return
        }
        let pixels = CGSize(width: cgImage.width, height: cgImage.height)
        let rect = CGRect(
            x: crop.x * Double(pixels.width), y: crop.y * Double(pixels.height),
            width: crop.width * Double(pixels.width), height: crop.height * Double(pixels.height)
        ).integral
        guard rect.width >= 1, rect.height >= 1, let cropped = cgImage.cropping(to: rect) else {
            imageView.image = source
            return
        }
        imageView.image = UIImage(cgImage: cropped, scale: source.scale, orientation: source.imageOrientation)
    }

    /// In crop mode a handle drag changes which part of the source is shown,
    /// leaving the object's place on the page exactly where it was.
    override func handleSpecialResize(_ handle: Handle, translation: CGSize) -> Bool {
        guard isCropping else { return false }
        var crop = object.crop ?? PageObject.Frame(x: 0, y: 0, width: 1, height: 1)
        let width = max(object.frame.width, 1)
        let height = max(object.frame.height, 1)
        let dx = Double(translation.width) / width * crop.width
        let dy = Double(translation.height) / height * crop.height
        let minimum = 0.05

        if handle.movesLeft {
            let delta = min(max(dx, -crop.x), crop.width - minimum)
            crop.x += delta
            crop.width -= delta
        }
        if handle.movesRight {
            crop.width = min(max(crop.width + dx, minimum), 1 - crop.x)
        }
        if handle.movesTop {
            let delta = min(max(dy, -crop.y), crop.height - minimum)
            crop.y += delta
            crop.height -= delta
        }
        if handle.movesBottom {
            crop.height = min(max(crop.height + dy, minimum), 1 - crop.y)
        }
        update({ $0.crop = crop }, actionName: "Crop Image")
        return true
    }

    override func endEditingContent() {
        isCropping = false
    }

    /// The size an image of `pixels` should take on a page of `pageSize`,
    /// fitted into half the page and never upscaled.
    static func fittedFrame(
        pixels: CGSize, pageSize: CGSize, centre: CGPoint
    ) -> PageObject.Frame {
        let maximum = CGSize(width: pageSize.width * 0.6, height: pageSize.height * 0.5)
        guard pixels.width > 0, pixels.height > 0 else {
            return PageObject.Frame(
                x: Double(centre.x - 100), y: Double(centre.y - 75), width: 200, height: 150)
        }
        let factor = min(1, min(maximum.width / pixels.width, maximum.height / pixels.height))
        let size = CGSize(width: pixels.width * factor, height: pixels.height * factor)
        return PageObject.Frame(
            CGRect(
                x: centre.x - size.width / 2, y: centre.y - size.height / 2, width: size.width,
                height: size.height))
    }
}
