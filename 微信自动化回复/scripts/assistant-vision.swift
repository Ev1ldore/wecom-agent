import Foundation
import Vision
import AppKit

guard CommandLine.arguments.count == 2,
      let image = NSImage(contentsOfFile: CommandLine.arguments[1]),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fputs("Image could not be read\n", stderr)
    exit(1)
}
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = true
do {
    try VNImageRequestHandler(cgImage: cgImage, options: [:]).perform([request])
    let results: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let best = observation.topCandidates(1).first else { return nil }
        return ["text": best.string, "confidence": best.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: results)
    print(String(data: data, encoding: .utf8)!)
} catch {
    fputs("OCR failed: \(error)\n", stderr)
    exit(1)
}
