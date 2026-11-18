import CoreMedia
import Foundation
import VideoToolbox

/// Decodifica il video H.264 (formato Annex B) inviato dal PC con il chip video dell'iPad.
/// Va usato sempre dalla stessa coda (non è thread-safe).
final class H264Decoder {
    private var sps: Data?
    private var pps: Data?
    private var format: CMVideoFormatDescription?
    private var session: VTDecompressionSession?

    deinit { reset() }

    func reset() {
        if let s = session {
            VTDecompressionSessionInvalidate(s)
        }
        session = nil
        format = nil
    }

    /// Divide un flusso Annex B (separatori 00 00 01 / 00 00 00 01) nelle singole unità NAL.
    private static func nalUnits(_ data: Data) -> [Data] {
        let b = [UInt8](data)
        var starts: [(pos: Int, len: Int)] = []
        var i = 0
        while i + 3 <= b.count {
            if b[i] == 0, b[i + 1] == 0 {
                if b[i + 2] == 1 { starts.append((i, 3)); i += 3; continue }
                if i + 3 < b.count, b[i + 2] == 0, b[i + 3] == 1 { starts.append((i, 4)); i += 4; continue }
            }
            i += 1
        }
        var units: [Data] = []
        for (k, s) in starts.enumerated() {
            let begin = s.pos + s.len
            let end = k + 1 < starts.count ? starts[k + 1].pos : b.count
            if end > begin { units.append(Data(b[begin..<end])) }
        }
        return units
    }

    private func makeSession() -> Bool {
        guard let sps, let pps else { return false }
        var fmt: CMVideoFormatDescription?
        let status: OSStatus = sps.withUnsafeBytes { spsBuf in
            pps.withUnsafeBytes { ppsBuf in
                let pointers: [UnsafePointer<UInt8>] = [
                    spsBuf.bindMemory(to: UInt8.self).baseAddress!,
                    ppsBuf.bindMemory(to: UInt8.self).baseAddress!,
                ]
                let sizes = [sps.count, pps.count]
                return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                    allocator: kCFAllocatorDefault, parameterSetCount: 2,
                    parameterSetPointers: pointers, parameterSetSizes: sizes,
                    nalUnitHeaderLength: 4, formatDescriptionOut: &fmt)
            }
        }
        guard status == noErr, let fmt else { return false }
        reset()
        format = fmt
        let attrs = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA] as CFDictionary
        var s: VTDecompressionSession?
        let st = VTDecompressionSessionCreate(allocator: kCFAllocatorDefault, formatDescription: fmt,
                                              decoderSpecification: nil, imageBufferAttributes: attrs,
                                              outputCallback: nil, decompressionSessionOut: &s)
        guard st == noErr, let s else { return false }
        session = s
        return true
    }

    /// Prepara un fotogramma per AVSampleBufferDisplayLayer (che lo decodifica e lo mostra
    /// direttamente nel chip grafico, senza copie dell'immagine).
    func sampleBuffer(_ annexB: Data) -> CMSampleBuffer? {
        var avcc = Data()
        for nal in H264Decoder.nalUnits(annexB) {
            guard let first = nal.first else { continue }
            switch first & 0x1f {
            case 7:
                if nal != sps { sps = nal; format = nil }
            case 8:
                if nal != pps { pps = nal; format = nil }
            case 9:
                break
            default:
                var len = UInt32(nal.count).bigEndian
                avcc.append(Data(bytes: &len, count: 4))
                avcc.append(nal)
            }
        }
        if format == nil, let sps, let pps {
            var fmt: CMVideoFormatDescription?
            let st: OSStatus = sps.withUnsafeBytes { a in
                pps.withUnsafeBytes { b in
                    let ptrs: [UnsafePointer<UInt8>] = [a.bindMemory(to: UInt8.self).baseAddress!,
                                                        b.bindMemory(to: UInt8.self).baseAddress!]
                    return CMVideoFormatDescriptionCreateFromH264ParameterSets(
                        allocator: kCFAllocatorDefault, parameterSetCount: 2, parameterSetPointers: ptrs,
                        parameterSetSizes: [sps.count, pps.count], nalUnitHeaderLength: 4,
                        formatDescriptionOut: &fmt)
                }
            }
            if st == noErr { format = fmt }
        }
        guard let format, !avcc.isEmpty else { return nil }
        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: avcc.count,
            blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0,
            dataLength: avcc.count, flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &block) == kCMBlockBufferNoErr, let block else { return nil }
        let ok = avcc.withUnsafeBytes { buf in
            CMBlockBufferReplaceDataBytes(with: buf.baseAddress!, blockBuffer: block,
                                          offsetIntoDestination: 0, dataLength: avcc.count)
        }
        guard ok == kCMBlockBufferNoErr else { return nil }
        var sample: CMSampleBuffer?
        var size = avcc.count
        guard CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: format,
            sampleCount: 1, sampleTimingEntryCount: 0, sampleTimingArray: nil,
            sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample) == noErr,
              let sample else { return nil }
        // mostra subito, senza orologio di riproduzione
        if let atts = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: true),
           CFArrayGetCount(atts) > 0 {
            let dict = unsafeBitCast(CFArrayGetValueAtIndex(atts, 0), to: CFMutableDictionary.self)
            CFDictionarySetValue(dict, Unmanaged.passUnretained(kCMSampleAttachmentKey_DisplayImmediately).toOpaque(),
                                 Unmanaged.passUnretained(kCFBooleanTrue).toOpaque())
        }
        return sample
    }

    /// Decodifica un fotogramma (unità di accesso) e restituisce l'immagine, se pronta.
    func decode(_ annexB: Data) -> CGImage? {
        var avcc = Data()
        var newParams = false
        for nal in H264Decoder.nalUnits(annexB) {
            guard let first = nal.first else { continue }
            switch first & 0x1f {
            case 7:
                if nal != sps { sps = nal; newParams = true }
            case 8:
                if nal != pps { pps = nal; newParams = true }
            case 9:
                break  // delimitatore: non serve
            default:
                var len = UInt32(nal.count).bigEndian
                avcc.append(Data(bytes: &len, count: 4))
                avcc.append(nal)
            }
        }
        if newParams || session == nil {
            guard makeSession() else { return nil }
        }
        guard let session, let format, !avcc.isEmpty else { return nil }

        var block: CMBlockBuffer?
        guard CMBlockBufferCreateWithMemoryBlock(
            allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: avcc.count,
            blockAllocator: kCFAllocatorDefault, customBlockSource: nil, offsetToData: 0,
            dataLength: avcc.count, flags: kCMBlockBufferAssureMemoryNowFlag,
            blockBufferOut: &block) == kCMBlockBufferNoErr, let block else { return nil }
        let copied = avcc.withUnsafeBytes { buf in
            CMBlockBufferReplaceDataBytes(with: buf.baseAddress!, blockBuffer: block,
                                          offsetIntoDestination: 0, dataLength: avcc.count)
        }
        guard copied == kCMBlockBufferNoErr else { return nil }

        var sample: CMSampleBuffer?
        var size = avcc.count
        guard CMSampleBufferCreateReady(
            allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: format,
            sampleCount: 1, sampleTimingEntryCount: 0, sampleTimingArray: nil,
            sampleSizeEntryCount: 1, sampleSizeArray: &size, sampleBufferOut: &sample) == noErr,
              let sample else { return nil }

        var result: CGImage?
        // senza il flag "asincrono" il risultato arriva prima che la funzione ritorni
        let status = VTDecompressionSessionDecodeFrame(session, sampleBuffer: sample, flags: [],
                                                       infoFlagsOut: nil) { status, _, imageBuffer, _, _ in
            guard status == noErr, let imageBuffer else { return }
            var cg: CGImage?
            VTCreateCGImageFromCVPixelBuffer(imageBuffer, options: nil, imageOut: &cg)
            result = cg
        }
        if status != noErr {
            reset()  // al prossimo fotogramma chiave si riparte da zero
        }
        return result
    }
}
