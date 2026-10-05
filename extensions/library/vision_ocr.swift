import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count == 2 else { exit(2) }
let url = URL(fileURLWithPath: CommandLine.arguments[1])
guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(3) }

let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
let supported = (try? request.supportedRecognitionLanguages()) ?? []
let preferred = ["zh-Hans", "zh-Hant", "en-US"].filter { supported.contains($0) }
if !preferred.isEmpty { request.recognitionLanguages = preferred }
let handler = VNImageRequestHandler(cgImage: image, options: [:])
do {
    try handler.perform([request])
    let lines = (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
    let bytes = Array(lines.joined(separator: "\n").utf8.prefix(200_000))
    FileHandle.standardOutput.write(Data(bytes))
} catch {
    let failure = error as NSError
    let fingerprint = "vision:\(failure.domain):\(failure.code)\n"
    FileHandle.standardError.write(Data(fingerprint.utf8.prefix(256)))
    exit(4)
}
