using SpokenAudioBatch;
using System.Text.Json;

// Fixture files belong on V:, never in the installed game packs.
if (args.Length != 2) throw new ArgumentException("Supply AddOns root and fixture directory.");
Directory.CreateDirectory(args[1]);
int count = 0;
foreach (string pack in new[] { "Alliance", "Horde", "Shared", "Gossip" })
{
    var files = Directory.GetFiles(Path.Combine(args[0], "SpokenQuestsAudio" + pack), "*.ogg", SearchOption.AllDirectories);
    foreach (var path in files)
    {
        var info = OggReader.Read(path);
        if (++count <= 4 || count % 2000 == 0) Console.WriteLine(JsonSerializer.Serialize(new { path, info }));
    }
}
var sample = Directory.GetFiles(Path.Combine(args[0], "SpokenQuestsAudioHorde"), "880-accept.ogg", SearchOption.AllDirectories).Single();
var source = File.ReadAllBytes(sample);
var corrupt = (byte[])source.Clone(); corrupt[^1] ^= 1;
var cases = new Dictionary<string, byte[]> {
    ["truncated"] = source[..^1], ["crc-corrupt"] = corrupt,
    ["chained"] = source.Concat(source).ToArray(), ["empty"] = []
};
foreach (var fixture in cases)
{
    var path = Path.Combine(args[1], fixture.Key + ".ogg");
    if (File.Exists(path)) throw new IOException("Refusing to overwrite fixture: " + path);
    File.WriteAllBytes(path, fixture.Value);
    bool rejected = false;
    try { OggReader.Read(path); }
    catch (InvalidDataException ex) { rejected = true; Console.WriteLine(fixture.Key + " rejected: " + ex.Message); }
    if (!rejected) throw new Exception("Invalid fixture accepted: " + fixture.Key);
}
Console.WriteLine($"PASS: {count} installed OGG files parsed; all four invalid fixtures rejected.");
