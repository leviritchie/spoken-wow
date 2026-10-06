using System.Buffers.Binary;
using System.Security.Cryptography;

namespace SpokenAudioBatch;

internal readonly record struct OggInfo(
    double Duration,
    int SampleRate,
    int Channels,
    string Sha256,
    long Bytes,
    long FinalGranulePosition,
    uint SerialNumber);

internal static class OggReader
{
    private const int PageHeaderLength = 27;
    private const uint CrcPolynomial = 0x04C11DB7;
    private static readonly uint[] CrcTable = BuildCrcTable();

    public static OggInfo Read(string path)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(path);

        var bytes = File.ReadAllBytes(path);
        if (bytes.Length == 0)
        {
            throw Invalid(path, "file is empty");
        }

        var sha256 = Convert.ToHexString(SHA256.HashData(bytes));
        return Parse(bytes, path, sha256);
    }

    private static OggInfo Parse(byte[] bytes, string path, string sha256)
    {
        var offset = 0;
        var pageIndex = 0;
        uint serialNumber = 0;
        uint expectedSequence = 0;
        var haveSerialNumber = false;
        var continuedFromPreviousPage = false;
        var sawEndOfStream = false;
        var sampleRate = 0;
        var channels = 0;
        var packetIndex = 0;
        var packetPrefixLength = 0;
        var packetPrefix = new byte[7];
        var hasAudioPacket = false;
        long lastGranule = -1;
        long finalGranule = -1;

        while (offset < bytes.Length)
        {
            if (sawEndOfStream)
            {
                throw Invalid(path, "data follows the EOS page; chained streams are unsupported");
            }

            var remaining = bytes.Length - offset;
            if (remaining < PageHeaderLength)
            {
                throw Invalid(path, $"truncated page header at byte {offset}");
            }

            var page = bytes.AsSpan(offset);
            if (!page[..4].SequenceEqual("OggS"u8))
            {
                throw Invalid(path, $"missing Ogg capture pattern at byte {offset}");
            }

            if (page[4] != 0)
            {
                throw Invalid(path, $"unsupported Ogg page version {page[4]} at byte {offset}");
            }

            var headerType = page[5];
            if ((headerType & ~0x07) != 0)
            {
                throw Invalid(path, $"reserved page flags are set at byte {offset}");
            }

            var isContinued = (headerType & 0x01) != 0;
            var isBeginning = (headerType & 0x02) != 0;
            var isEnd = (headerType & 0x04) != 0;
            var granulePosition = BinaryPrimitives.ReadInt64LittleEndian(page.Slice(6, 8));
            if (granulePosition < 0 && granulePosition != -1)
            {
                throw Invalid(path, $"invalid negative granule position {granulePosition} at byte {offset}");
            }

            var currentSerial = BinaryPrimitives.ReadUInt32LittleEndian(page.Slice(14, 4));
            var sequence = BinaryPrimitives.ReadUInt32LittleEndian(page.Slice(18, 4));
            var expectedCrc = BinaryPrimitives.ReadUInt32LittleEndian(page.Slice(22, 4));
            var segmentCount = page[26];
            var headerLength = PageHeaderLength + segmentCount;
            if (remaining < headerLength)
            {
                throw Invalid(path, $"truncated lacing table at byte {offset}");
            }

            var bodyLength = 0;
            var pageHasCompletedPacket = false;
            for (var i = 0; i < segmentCount; i++)
            {
                var lace = page[PageHeaderLength + i];
                bodyLength += lace;
                pageHasCompletedPacket |= lace < byte.MaxValue;
            }

            var pageLength = headerLength + bodyLength;
            if (remaining < pageLength)
            {
                throw Invalid(path, $"truncated page body at byte {offset}");
            }

            var fullPage = page[..pageLength];
            if (ComputePageCrc(fullPage) != expectedCrc)
            {
                throw Invalid(path, $"page CRC mismatch at byte {offset}");
            }

            if (!haveSerialNumber)
            {
                if (!isBeginning || isContinued || sequence != 0)
                {
                    throw Invalid(path, "first page must be an uncontinued BOS page with sequence zero");
                }

                serialNumber = currentSerial;
                haveSerialNumber = true;
                (sampleRate, channels) = ReadIdentificationHeader(fullPage, path, offset);
            }
            else
            {
                if (currentSerial != serialNumber)
                {
                    throw Invalid(path, "multiple logical streams are unsupported");
                }

                if (isBeginning)
                {
                    throw Invalid(path, "unexpected BOS page in a single logical stream");
                }

                if (sequence != expectedSequence)
                {
                    throw Invalid(path, $"unexpected page sequence {sequence}; expected {expectedSequence}");
                }

                if (isContinued != continuedFromPreviousPage)
                {
                    throw Invalid(path, $"continued-packet flag disagrees with lacing at byte {offset}");
                }
            }

            if (pageIndex == 0 && granulePosition != 0)
            {
                throw Invalid(path, "Vorbis identification page must have granule position zero");
            }

            if (granulePosition == -1 && pageHasCompletedPacket)
            {
                throw Invalid(path, $"page completes a packet but has unset granule position at byte {offset}");
            }

            if (granulePosition >= 0)
            {
                if (granulePosition < lastGranule)
                {
                    throw Invalid(path, $"granule positions decrease at byte {offset}");
                }

                lastGranule = granulePosition;
            }

            var bodyOffset = headerLength;
            var nextContinues = continuedFromPreviousPage;
            for (var i = 0; i < segmentCount; i++)
            {
                var lace = page[PageHeaderLength + i];
                var segmentLength = (int)lace;
                if (packetIndex < 3 && packetPrefixLength < packetPrefix.Length)
                {
                    var copyLength = Math.Min(packetPrefix.Length - packetPrefixLength, segmentLength);
                    fullPage.Slice(bodyOffset, copyLength).CopyTo(packetPrefix.AsSpan(packetPrefixLength));
                    packetPrefixLength += copyLength;
                }

                bodyOffset += segmentLength;
                nextContinues = lace == byte.MaxValue;
                if (lace < byte.MaxValue)
                {
                    if (packetIndex < 3)
                    {
                        ValidateHeaderPacket(packetPrefix, packetPrefixLength, packetIndex, path, offset);
                    }
                    else
                    {
                        hasAudioPacket = true;
                    }

                    packetIndex++;
                    packetPrefixLength = 0;
                }
            }

            if (segmentCount == 0)
            {
                nextContinues = continuedFromPreviousPage;
            }

            if (isEnd)
            {
                if (offset + pageLength != bytes.Length)
                {
                    throw Invalid(path, "EOS page is not the final page; chained streams are unsupported");
                }

                if (nextContinues || !pageHasCompletedPacket || !hasAudioPacket || granulePosition <= 0)
                {
                    throw Invalid(path, "EOS page lacks a complete Vorbis audio packet or positive final granule");
                }

                finalGranule = granulePosition;
                sawEndOfStream = true;
            }

            continuedFromPreviousPage = nextContinues;
            expectedSequence = unchecked(sequence + 1);
            offset += pageLength;
            pageIndex++;
        }

        if (!sawEndOfStream || !haveSerialNumber || sampleRate <= 0 || channels <= 0 || finalGranule <= 0)
        {
            throw Invalid(path, "stream is incomplete or has no valid final duration");
        }

        return new OggInfo(
            finalGranule / (double)sampleRate,
            sampleRate,
            channels,
            sha256,
            bytes.LongLength,
            finalGranule,
            serialNumber);
    }

    private static (int SampleRate, int Channels) ReadIdentificationHeader(ReadOnlySpan<byte> page, string path, int offset)
    {
        var segmentCount = page[26];
        if (segmentCount != 1 || page[PageHeaderLength] != 30 || page.Length != PageHeaderLength + 1 + 30)
        {
            throw Invalid(path, "first Ogg page is not a canonical single-packet Vorbis identification page");
        }

        var packet = page.Slice(PageHeaderLength + 1, 30);
        if (packet[0] != 1 || !packet.Slice(1, 6).SequenceEqual("vorbis"u8))
        {
            throw Invalid(path, $"first packet is not a Vorbis identification header at byte {offset}");
        }

        var version = BinaryPrimitives.ReadUInt32LittleEndian(packet.Slice(7, 4));
        var channels = packet[11];
        var sampleRate = BinaryPrimitives.ReadUInt32LittleEndian(packet.Slice(12, 4));
        var blockSizes = packet[28];
        var smallBlockExponent = blockSizes & 0x0F;
        var largeBlockExponent = blockSizes >> 4;
        var framingFlag = packet[29];

        if (version != 0 || channels == 0 || sampleRate == 0 || sampleRate > int.MaxValue
            || smallBlockExponent < 6 || largeBlockExponent < smallBlockExponent || largeBlockExponent > 13
            || framingFlag != 1)
        {
            throw Invalid(path, "Vorbis identification header has invalid version, channels, rate, block sizes, or framing bit");
        }

        return ((int)sampleRate, channels);
    }

    private static void ValidateHeaderPacket(byte[] prefix, int prefixLength, int packetIndex, string path, int offset)
    {
        ReadOnlySpan<byte> header = prefix.AsSpan(0, prefixLength);
        var expectedType = packetIndex switch
        {
            0 => 1,
            1 => 3,
            2 => 5,
            _ => throw new InvalidOperationException("Unexpected Vorbis header packet index."),
        };

        if (header.Length < 7 || header[0] != expectedType || !header.Slice(1, 6).SequenceEqual("vorbis"u8))
        {
            throw Invalid(path, $"invalid Vorbis header packet {packetIndex + 1} at byte {offset}");
        }
    }

    private static uint ComputePageCrc(ReadOnlySpan<byte> page)
    {
        uint crc = 0;
        for (var i = 0; i < page.Length; i++)
        {
            var value = i is >= 22 and < 26 ? (byte)0 : page[i];
            var tableIndex = (int)((crc >> 24) ^ value) & 0xFF;
            crc = (crc << 8) ^ CrcTable[tableIndex];
        }

        return crc;
    }

    private static uint[] BuildCrcTable()
    {
        var table = new uint[256];
        for (var i = 0; i < table.Length; i++)
        {
            var value = (uint)i << 24;
            for (var bit = 0; bit < 8; bit++)
            {
                value = (value & 0x80000000) != 0
                    ? (value << 1) ^ CrcPolynomial
                    : value << 1;
            }

            table[i] = value;
        }

        return table;
    }

    private static InvalidDataException Invalid(string path, string message) =>
        new($"Invalid Ogg/Vorbis file '{path}': {message}.");
}
