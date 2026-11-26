import UIKit

/// Photos, pastes and drops, turned into the bytes a page stores.
///
/// A camera photo is a sensor-oriented bitmap plus a rotation flag, and
/// `pngData()` writes the bitmap and drops the flag: a portrait photo landed on
/// the page sideways, as a 15-30 MB PNG. Redrawing bakes the rotation into the
/// pixels -- which is also what keeps `ImageObjectView`'s crop, done in bitmap
/// space, lined up with what the user sees -- and the long edge is capped
/// while it is drawn. Photos are stored as JPEG; only an image that can be
/// transparent stays PNG.
///
/// Callers take the `CGImage`, orientation and scale off the `UIImage` on the
/// main actor and run this off it: `UIImage` is not `Sendable`, and a 12 MP
/// redraw is the freeze this exists to remove.
enum ImageImport {
    static let jpegQuality: CGFloat = 0.85

    /// The encoded image and the file extension that names it, or nil when
    /// the image has no pixels to draw.
    nonisolated static func normalized(
        cgImage: CGImage,
        orientation: UIImage.Orientation,
        scale: CGFloat,
        maxLongEdge: CGFloat = 3000
    ) -> (data: Data, extension: String)? {
        let image = UIImage(cgImage: cgImage, scale: max(scale, 1), orientation: orientation)
        // Pixels as displayed: a quarter turn swaps the axes.
        let pixels = CGSize(width: image.size.width * image.scale, height: image.size.height * image.scale)
        let longEdge = max(pixels.width, pixels.height)
        guard longEdge >= 1 else { return nil }
        let factor = min(1, maxLongEdge / longEdge)
        let target = CGSize(
            width: max(1, (pixels.width * factor).rounded()),
            height: max(1, (pixels.height * factor).rounded()))

        let hasAlpha = ![.none, .noneSkipFirst, .noneSkipLast].contains(cgImage.alphaInfo)
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        format.opaque = !hasAlpha
        format.preferredRange = .standard
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        let draw: (UIGraphicsImageRendererContext) -> Void = { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
        if hasAlpha {
            return (renderer.pngData(actions: draw), "png")
        }
        return (renderer.jpegData(withCompressionQuality: jpegQuality, actions: draw), "jpg")
    }
}
