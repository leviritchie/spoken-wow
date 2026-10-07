using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace SpokenAudioBatch;

internal static class Program
{
    private const string AddOns = @"V:\Games\World of Warcraft\_classic_beta_\Interface\AddOns";
    private const string Stage = @"V:\Games\WoW-Addon-Tests\SpokenQuests-All-1.5x";
    private const string Pilot = @"V:\Games\WoW-Addon-Tests\SpokenQuests-AlteredBeings-1.5x";
    // User-authorized (2026-10-07) adaptive candidate run. Review placeholders are byte-identical originals and install at original speed.
    private const string AdaptiveRun = @"V:\Games\WoW-Addon-Tests\SpokenQuests-Adaptive-Full-20261007-01";
    private const int BatchSize = 32;
    private static readonly string[] Packs = ["Alliance", "Horde", "Shared", "Gossip"];
    private static readonly Regex TimingLine = new(
        "(?m)^(?<prefix>[ \\t]*\\[\"(?<key>[^\"]+)\"\\][ \\t]*=[ \\t]*)(?<value>(?:[0-9]+(?:\\.[0-9]*)?|\\.[0-9]+)(?:[eE][+-]?[0-9]+)?)(?<suffix>,[ \\t]*\\r?)$",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly Regex TimingKeyStart = new("(?m)^[ \\t]*\\[\"[^\"]+\"\\]", RegexOptions.CultureInvariant | RegexOptions.Compiled);
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    private sealed class RunManifest
    {
        public int Schema { get; set; } = 1;
        public string Status { get; set; } = "";
        public string Recipe { get; set; } = "ffmpeg atempo=1.5, libvorbis q5, one encoder thread";
        public string FfmpegVersion { get; set; } = "";
        public string CreatedAtUtc { get; set; } = "";
        public List<AudioItem> Audio { get; set; } = [];
        public List<TableItem> Tables { get; set; } = [];
    }

    private sealed class AudioItem
    {
        public string Pack { get; set; } = "";
        public string RelativePath { get; set; } = "";
        public string Key { get; set; } = "";
        public string OriginalSha256 { get; set; } = "";
        public long OriginalBytes { get; set; }
        public long OriginalGranule { get; set; }
        public double OriginalDuration { get; set; }
        public int SampleRate { get; set; }
        public int Channels { get; set; }
        public string? PilotFasterSha256 { get; set; }
        public string State { get; set; } = "Inventoried";
        public string? FasterSha256 { get; set; }
        public long? FasterBytes { get; set; }
        public long? FasterGranule { get; set; }
        public double? FasterDuration { get; set; }
    }

    private sealed class TableItem
    {
        public string Pack { get; set; } = "";
        public string RelativePath { get; set; } = "generated/sound_length_table.lua";
        public string OriginalSha256 { get; set; } = "";
        public string? InitialPilotSha256 { get; set; }
        public string? FasterSha256 { get; set; }
        public string State { get; set; } = "Inventoried";
    }

    private sealed class ChunkCheckpoint
    {
        public int Index { get; set; }
        public string Recipe { get; set; } = "";
        public string FfmpegVersion { get; set; } = "";
        public List<CandidateInfo> Items { get; set; } = [];
    }

    private sealed class CandidateInfo
    {
        public string Pack { get; set; } = "";
        public string RelativePath { get; set; } = "";
        public string Sha256 { get; set; } = "";
        public long Bytes { get; set; }
        public long Granule { get; set; }
        public double Duration { get; set; }
        public uint SerialNumber { get; set; }
    }

    private sealed record TableDocument(byte[] Bytes, string Text, bool HasBom, Dictionary<string, string> Values);
    private sealed record PilotClip(string OriginalPath, string FasterPath, string OriginalHash, string FasterHash, OggInfo OriginalInfo, OggInfo FasterInfo);
    private sealed record PilotSeed(Dictionary<string, PilotClip> Clips, byte[] OriginalTable, string OriginalTableHash, string FasterTableHash);
    private sealed record SourceFile(AudioItem Item, string CopyFrom);
    private sealed record ProcessResult(int ExitCode, string Output, string Error, TimeSpan CpuTime);

    private static string ManifestPath => Path.Combine(Stage, "manifest.json");
    private static string LockPath => Stage + ".lock";

    private static async Task<int> Main(string[] args)
    {
        using var cancel = new CancellationTokenSource();
        Console.CancelKeyPress += (_, eventArgs) => { eventArgs.Cancel = true; cancel.Cancel(); };
        try
        {
            if (args.Length == 0 || args[0] is "help" or "--help" or "-h")
            {
                PrintUsage();
                return args.Length == 0 ? 2 : 0;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(LockPath)!);
            using var runLock = new FileStream(LockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            var ffmpeg = FindExecutable("ffmpeg");
            var command = args[0].ToLowerInvariant();
            switch (command)
            {
                case "inventory":
                    await InventoryAsync(ffmpeg, cancel.Token);
                    break;
                case "benchmark":
                    await BenchmarkAsync(ffmpeg, ReadOption(args, "--count", 32), cancel.Token);
                    break;
                case "build":
                    await BuildAsync(ffmpeg, ReadOption(args, "--workers", 0), cancel.Token);
                    break;
                case "deploy":
                    await ApplyAsync(Target.Faster, cancel.Token);
                    break;
                case "deploy-adaptive":
                    await ApplyAsync(Target.Adaptive, cancel.Token);
                    break;
                case "restore":
                    await ApplyAsync(Target.Original, cancel.Token);
                    break;
                case "validate":
                    ValidateInstalled();
                    break;
                default:
                    throw new ArgumentException($"Unknown action: {args[0]}");
            }
            return 0;
        }
        catch (OperationCanceledException)
        {
            Console.Error.WriteLine("Cancelled. Owned child processes were stopped; staged evidence was retained.");
            return 2;
        }
        catch (Exception error)
        {
            Console.Error.WriteLine(error);
            return 1;
        }
    }

    private static void PrintUsage() => Console.WriteLine("""
SpokenAudioBatch inventory
SpokenAudioBatch benchmark [--count 32]
SpokenAudioBatch build --workers 1|2|4|8
SpokenAudioBatch deploy
SpokenAudioBatch deploy-adaptive
SpokenAudioBatch validate
SpokenAudioBatch restore
""");

    private static int ReadOption(string[] args, string name, int fallback)
    {
        var index = Array.IndexOf(args, name);
        if (index < 0) return fallback;
        if (index + 1 >= args.Length || !int.TryParse(args[index + 1], NumberStyles.None, CultureInfo.InvariantCulture, out var value))
            throw new ArgumentException($"{name} needs an integer value.");
        return value;
    }

    private static string FindExecutable(string name)
    {
        var extension = OperatingSystem.IsWindows() ? ".exe" : "";
        foreach (var directory in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            var candidate = Path.Combine(directory.Trim('"'), name + extension);
            if (File.Exists(candidate)) return candidate;
        }
        throw new FileNotFoundException($"Required executable {name}{extension} was not found on PATH.");
    }

    private static async Task<ProcessResult> RunProcessAsync(string executable, IEnumerable<string> arguments, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var start = new ProcessStartInfo(executable) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, RedirectStandardOutput = true };
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        using var process = new Process { StartInfo = start, EnableRaisingEvents = true };
        if (!process.Start()) throw new InvalidOperationException($"Could not start {Path.GetFileName(executable)}.");
        var errorTask = process.StandardError.ReadToEndAsync(cancellationToken);
        var outputTask = process.StandardOutput.ReadToEndAsync(cancellationToken);
        using var timeoutSource = new CancellationTokenSource(timeout);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeoutSource.Token);
        try
        {
            await process.WaitForExitAsync(linked.Token);
        }
        catch (OperationCanceledException)
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync(CancellationToken.None);
            try { await errorTask; } catch (OperationCanceledException) { }
            try { await outputTask; } catch (OperationCanceledException) { }
            if (cancellationToken.IsCancellationRequested) throw;
            throw new TimeoutException($"Timed out: {Path.GetFileName(executable)} {string.Join(' ', arguments)}");
        }
        var error = await errorTask;
        var output = await outputTask;
        return new ProcessResult(process.ExitCode, output, error, process.TotalProcessorTime);
    }

    private static string PackPath(string pack) => Path.Combine(AddOns, "SpokenQuestsAudio" + pack);
    private static string OriginalPath(string pack, string relative) => Under(Path.Combine(Stage, "original", pack), relative);
    private static string FasterPath(string pack, string relative) => Under(Path.Combine(Stage, "faster", pack), relative);
    private static string WorkingPath(string pack, string relative) => Under(Path.Combine(Stage, "working", pack), relative);
    private static string Under(string root, string relative)
    {
        var safe = relative.Replace('/', Path.DirectorySeparatorChar).Replace('\\', Path.DirectorySeparatorChar);
        var full = Path.GetFullPath(Path.Combine(root, safe));
        var prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException($"Path escapes its root: {relative}");
        return full;
    }

    private static string NormalizeHash(string hash) => hash.ToUpperInvariant();
    private static string HashBytes(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes));
    private static string HashFile(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read);
        return Convert.ToHexString(SHA256.HashData(stream));
    }

    private static byte[] ReadBytesNoReparse(string path, string root)
    {
        AssertNoReparse(path, root);
        return File.ReadAllBytes(path);
    }

    private static void AssertNoReparse(string path, string root)
    {
        var rootFull = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        var current = Path.GetFullPath(path);
        while (current.StartsWith(rootFull, StringComparison.OrdinalIgnoreCase))
        {
            if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) throw new IOException($"Reparse-point path is not allowed: {current}");
            if (string.Equals(current, rootFull, StringComparison.OrdinalIgnoreCase)) return;
            current = Path.GetDirectoryName(current)!;
        }
        throw new IOException($"Path is outside expected root: {path}");
    }

    private static TableDocument ParseTiming(byte[] bytes, string label)
    {
        var bom = bytes.AsSpan().StartsWith(new byte[] { 0xEF, 0xBB, 0xBF });
        var offset = bom ? 3 : 0;
        var text = new UTF8Encoding(false, true).GetString(bytes, offset, bytes.Length - offset);
        var values = new Dictionary<string, string>(StringComparer.Ordinal);
        var matches = TimingLine.Matches(text);
        foreach (Match match in matches)
        {
            var key = match.Groups["key"].Value;
            var literal = match.Groups["value"].Value;
            if (!values.TryAdd(key, literal)) throw new InvalidDataException($"Duplicate timing key {key} in {label}.");
            if (!double.TryParse(literal, NumberStyles.Float, CultureInfo.InvariantCulture, out var number) || !double.IsFinite(number) || number <= 0)
                throw new InvalidDataException($"Invalid timing number for {key} in {label}.");
        }
        if (TimingKeyStart.Matches(text).Count != matches.Count) throw new InvalidDataException($"Non-literal timing entry found in {label}.");
        if (values.Count == 0) throw new InvalidDataException($"No literal timing entries in {label}.");
        return new TableDocument(bytes, text, bom, values);
    }

    private static string RewriteTiming(TableDocument table, IReadOnlyDictionary<string, double> durations, string label)
    {
        var replaced = new HashSet<string>(StringComparer.Ordinal);
        var output = TimingLine.Replace(table.Text, match =>
        {
            var key = match.Groups["key"].Value;
            if (!durations.TryGetValue(key, out var duration)) return match.Value;
            if (!replaced.Add(key)) throw new InvalidDataException($"Duplicate candidate timing key {key} in {label}.");
            return match.Groups["prefix"].Value + duration.ToString("R", CultureInfo.InvariantCulture) + match.Groups["suffix"].Value;
        });
        if (replaced.Count != durations.Count) throw new InvalidDataException($"Timing rewrite did not cover each audio key in {label}.");
        var maskedOriginal = MaskTiming(table.Text, replaced);
        var maskedOutput = MaskTiming(output, replaced);
        if (!string.Equals(maskedOriginal, maskedOutput, StringComparison.Ordinal)) throw new InvalidDataException($"Timing rewrite changed unrelated text in {label}.");
        return output;
    }

    private static string MaskTiming(string text, IEnumerable<string> keys)
    {
        var set = keys.ToHashSet(StringComparer.Ordinal);
        return TimingLine.Replace(text, match => set.Contains(match.Groups["key"].Value)
            ? match.Groups["prefix"].Value + "<DURATION>" + match.Groups["suffix"].Value
            : match.Value);
    }

    private static void EnsureExactKeys(string pack, IEnumerable<string> audioKeys, IEnumerable<string> tableKeys)
    {
        var audio = audioKeys.ToHashSet(StringComparer.Ordinal);
        var table = tableKeys.ToHashSet(StringComparer.Ordinal);
        var missing = audio.Except(table, StringComparer.Ordinal).Take(8).ToArray();
        var extra = table.Except(audio, StringComparer.Ordinal).Take(8).ToArray();
        if (missing.Length != 0 || extra.Length != 0)
            throw new InvalidDataException($"{pack} audio/timing key mismatch; audio missing timing=[{string.Join(',', missing)}], timing missing audio=[{string.Join(',', extra)}].");
    }

    private static void SaveManifest(RunManifest manifest)
    {
        var temporary = ManifestPath + ".pending";
        if (File.Exists(temporary)) throw new IOException($"Manifest checkpoint temporary exists: {temporary}");
        var json = JsonSerializer.SerializeToUtf8Bytes(manifest, JsonOptions);
        using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        {
            stream.Write(json);
            stream.Flush(flushToDisk: true);
        }
        if (File.Exists(ManifestPath)) File.Replace(temporary, ManifestPath, null);
        else File.Move(temporary, ManifestPath);
    }

    private static RunManifest LoadManifest()
    {
        var manifest = JsonSerializer.Deserialize<RunManifest>(File.ReadAllBytes(ManifestPath)) ?? throw new InvalidDataException("Manifest is empty.");
        if (manifest.Schema != 1 || manifest.Audio.Count == 0 || manifest.Tables.Count != Packs.Length) throw new InvalidDataException("Unsupported or incomplete manifest.");
        var expectedCounts = new Dictionary<string, int> { ["Alliance"] = 2832, ["Horde"] = 2373, ["Shared"] = 3646, ["Gossip"] = 4112 };
        if (manifest.Recipe != new RunManifest().Recipe || manifest.Audio.Count != 12963)
            throw new InvalidDataException("Manifest recipe or frozen corpus size changed.");
        var paths = new HashSet<string>(StringComparer.Ordinal);
        var keys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in manifest.Audio)
        {
            if (!expectedCounts.ContainsKey(item.Pack) || !Regex.IsMatch(item.RelativePath, "^generated/sounds/(quests|gossip|followup)/[^/\\\\]+\\.ogg$") ||
                item.RelativePath.Contains("..", StringComparison.Ordinal) || item.Key != Path.GetFileNameWithoutExtension(item.RelativePath) ||
                !paths.Add(AudioKey(item.Pack, item.RelativePath)) || !keys.Add(AudioKey(item.Pack, item.Key)) || !IsSha256(item.OriginalSha256))
                throw new InvalidDataException("Invalid or duplicate manifest audio contract.");
        }
        if (!manifest.Tables.Select(table => table.Pack).ToHashSet(StringComparer.Ordinal).SetEquals(Packs) ||
            manifest.Tables.Any(table => table.RelativePath != "generated/sound_length_table.lua" || !IsSha256(table.OriginalSha256)) ||
            expectedCounts.Any(pair => manifest.Audio.Count(item => item.Pack == pair.Key) != pair.Value))
            throw new InvalidDataException("Manifest pack/table inventory changed.");
        return manifest;
    }

    private static FileStream AcquireRunLock() => new(LockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);

    private static async Task InventoryAsync(string ffmpeg, CancellationToken token)
    {
        if (Directory.Exists(Stage)) throw new IOException($"Stage already exists; refusing to overwrite: {Stage}");
        var pilot = LoadPilotSeed();
        var audio = new List<AudioItem>(13000);
        var tables = new List<TableItem>(4);
        var copySources = new Dictionary<AudioItem, string>();
        var tableSources = new Dictionary<string, (string Path, byte[] Bytes)>(StringComparer.Ordinal);
        foreach (var pack in Packs)
        {
            token.ThrowIfCancellationRequested();
            var packRoot = PackPath(pack);
            var soundsRoot = Path.Combine(packRoot, "generated", "sounds");
            var tablePath = Path.Combine(packRoot, "generated", "sound_length_table.lua");
            if (!Directory.Exists(soundsRoot) || !File.Exists(tablePath)) throw new DirectoryNotFoundException($"Missing audio tree or timing table for {pack}.");
            var tableBytes = pack == "Horde" ? pilot.OriginalTable : ReadBytesNoReparse(tablePath, packRoot);
            var table = ParseTiming(tableBytes, $"{pack} timing table");
            var files = Directory.EnumerateFiles(soundsRoot, "*.ogg", SearchOption.AllDirectories).Order(StringComparer.Ordinal).ToArray();
            if (files.Length == 0) throw new InvalidDataException($"No Ogg voice clips found in {pack}.");
            var packItems = new List<AudioItem>(files.Length);
            var packSources = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (var file in files)
            {
                AssertNoReparse(file, packRoot);
                var relative = Path.GetRelativePath(packRoot, file).Replace('\\', '/');
                var key = Path.GetFileNameWithoutExtension(file);
                var currentHash = HashFile(file);
                var isPilot = pack == "Horde" && (relative is "generated/sounds/quests/880-accept.ogg" or "generated/sounds/quests/880-complete.ogg");
                OggInfo info;
                string originalSource;
                PilotClip? seed = null;
                if (isPilot)
                {
                    if (!pilot.Clips.TryGetValue(Path.GetFileName(file), out seed)) throw new InvalidDataException($"Missing verified pilot seed for {file}.");
                    if (!HashEquals(currentHash, seed.OriginalHash) && !HashEquals(currentHash, seed.FasterHash)) throw new InvalidDataException($"Pilot target has an unknown hash: {file}");
                    info = seed.OriginalInfo;
                    originalSource = seed.OriginalPath;
                }
                else
                {
                    info = OggReader.Read(file);
                    if (!HashEquals(currentHash, info.Sha256)) throw new IOException($"Audio changed during inventory: {file}");
                    originalSource = file;
                }
                if (info.Duration <= 0 || info.SampleRate <= 0 || info.Channels <= 0) throw new InvalidDataException($"Invalid Vorbis metadata: {file}");
                var item = new AudioItem
                {
                    Pack = pack, RelativePath = relative, Key = key, OriginalSha256 = NormalizeHash(info.Sha256),
                    OriginalBytes = info.Bytes, OriginalGranule = info.FinalGranulePosition, OriginalDuration = info.Duration,
                    SampleRate = info.SampleRate, Channels = info.Channels, PilotFasterSha256 = seed?.FasterHash
                };
                if (!packSources.TryAdd(key, file)) throw new InvalidDataException($"Duplicate audio key {key} in {pack}.");
                packItems.Add(item);
                copySources.Add(item, originalSource);
            }
            EnsureExactKeys(pack, packItems.Select(item => item.Key), table.Values.Keys);
            if (pack == "Horde")
            {
                var liveTableHash = HashFile(tablePath);
                if (!HashEquals(liveTableHash, pilot.OriginalTableHash) && !HashEquals(liveTableHash, pilot.FasterTableHash))
                    throw new InvalidDataException("Horde timing table has an unknown pilot state.");
            }
            tables.Add(new TableItem { Pack = pack, OriginalSha256 = HashBytes(tableBytes), InitialPilotSha256 = pack == "Horde" ? pilot.FasterTableHash : null });
            tableSources.Add(pack, (pack == "Horde" ? PilotTableOriginalPath() : tablePath, tableBytes));
            audio.AddRange(packItems);
        }

        Directory.CreateDirectory(Stage);
        var manifest = new RunManifest
        {
            Status = "InventoryBuilding", FfmpegVersion = await GetFfmpegVersionAsync(ffmpeg, token),
            CreatedAtUtc = DateTime.UtcNow.ToString("O", CultureInfo.InvariantCulture), Audio = audio, Tables = tables
        };
        SaveManifest(manifest);
        var copied = 0;
        foreach (var item in audio)
        {
            token.ThrowIfCancellationRequested();
            var source = copySources[item];
            var destination = OriginalPath(item.Pack, item.RelativePath);
            CreateParent(destination);
            CopyCreateNew(source, destination);
            if (!HashEquals(HashFile(destination), item.OriginalSha256)) throw new IOException($"Original snapshot hash mismatch: {destination}");
            copied++;
            if (copied % 64 == 0) Console.WriteLine($"Snapshotted {copied}/{audio.Count} audio files");
        }
        foreach (var table in tables)
        {
            var source = tableSources[table.Pack];
            var destination = OriginalPath(table.Pack, table.RelativePath);
            CreateParent(destination);
            WriteCreateNew(destination, source.Bytes);
            if (!HashEquals(HashFile(destination), table.OriginalSha256)) throw new IOException($"Timing snapshot hash mismatch: {destination}");
        }
        manifest.Status = "Inventoried";
        SaveManifest(manifest);
        WriteSummary("Inventoried", manifest);
    }

    private static bool IsPilotAudio(AudioItem item) => item.Pack == "Horde" &&
        item.RelativePath is "generated/sounds/quests/880-accept.ogg" or "generated/sounds/quests/880-complete.ogg";

    private static string PilotFasterPath(AudioItem item) => Path.Combine(Pilot, "faster", item.RelativePath.Replace('/', Path.DirectorySeparatorChar));

    private static void CheckTempo(OggInfo original, OggInfo faster, string label)
    {
        if (faster.SampleRate != original.SampleRate || faster.Channels != original.Channels)
            throw new InvalidDataException($"Sample rate/channel change for {label}.");
        var expected = original.Duration / 1.5;
        var tolerance = 0.05 + expected * 0.01;
        if (Math.Abs(faster.Duration - expected) > tolerance)
            throw new InvalidDataException($"1.5x duration check failed for {label}: {original.Duration:R} -> {faster.Duration:R}.");
    }

    private static string CheckpointPath(int index) => Path.Combine(Stage, "checkpoints", $"chunk-{index:D5}.json");

    private static CandidateInfo CandidateFrom(AudioItem item, OggInfo info) => new()
    {
        Pack = item.Pack, RelativePath = item.RelativePath, Sha256 = NormalizeHash(info.Sha256), Bytes = info.Bytes,
        Granule = info.FinalGranulePosition, Duration = info.Duration, SerialNumber = info.SerialNumber
    };

    private static OggInfo ReadAndCheckCandidate(AudioItem item, string path, string? expectedHash = null)
    {
        var info = OggReader.Read(path);
        if (expectedHash is not null && !HashEquals(info.Sha256, expectedHash)) throw new InvalidDataException($"Candidate hash mismatch: {path}");
        if (!IsSha256(item.OriginalSha256)) throw new InvalidDataException($"Invalid source hash in manifest: {item.RelativePath}");
        var original = new OggInfo(item.OriginalDuration, item.SampleRate, item.Channels, item.OriginalSha256, item.OriginalBytes, item.OriginalGranule, 0);
        CheckTempo(original, info, item.RelativePath);
        return info;
    }

    private static void ApplyCandidate(AudioItem item, CandidateInfo candidate)
    {
        item.FasterSha256 = NormalizeHash(candidate.Sha256);
        item.FasterBytes = candidate.Bytes;
        item.FasterGranule = candidate.Granule;
        item.FasterDuration = candidate.Duration;
        item.State = "Built";
    }

    private static async Task BuildAsync(string ffmpeg, int workers, CancellationToken token)
    {
        if (workers is not (1 or 2 or 4 or 8)) throw new ArgumentOutOfRangeException(nameof(workers), "Use the worker count selected from benchmark (1, 2, 4, or 8).");
        var manifest = LoadManifest();
        if (manifest.Status is "InventoryBuilding") throw new InvalidDataException("Inventory did not finish; do not build from a partial source snapshot.");
        if (manifest.Status is "Built" or "Deployed" or "Restored")
        {
            VerifyBuiltStage(manifest);
            WriteSummary(manifest.Status, manifest);
            return;
        }
        if (manifest.Status is not ("Inventoried" or "Building" or "BuildInterrupted")) throw new InvalidDataException($"Cannot build from state {manifest.Status}.");
        var ffmpegVersion = await GetFfmpegVersionAsync(ffmpeg, token);
        if (!string.Equals(ffmpegVersion, manifest.FfmpegVersion, StringComparison.Ordinal)) throw new InvalidDataException("ffmpeg version changed since inventory; refusing to mix recipes.");
        VerifyOriginalSnapshots(manifest);
        manifest.Status = "Building";
        SaveManifest(manifest);
        try
        {
            Directory.CreateDirectory(Path.Combine(Stage, "checkpoints"));
            var chunks = manifest.Audio.Chunk(BatchSize).ToArray();
            for (var index = 0; index < chunks.Length; index++)
            {
                token.ThrowIfCancellationRequested();
                var checkpointPath = CheckpointPath(index);
                if (File.Exists(checkpointPath))
                {
                    var savedCheckpoint = JsonSerializer.Deserialize<ChunkCheckpoint>(File.ReadAllBytes(checkpointPath)) ?? throw new InvalidDataException($"Empty checkpoint: {checkpointPath}");
                    FinalizeCheckpoint(index, chunks[index], savedCheckpoint, manifest.Recipe, manifest.FfmpegVersion);
                    continue;
                }
                var chunk = chunks[index];
                foreach (var item in chunk)
                {
                    if (item.State == "Built") throw new InvalidDataException($"Built item lacks its immutable chunk checkpoint: {item.RelativePath}");
                    if (File.Exists(FasterPath(item.Pack, item.RelativePath)) || File.Exists(WorkingPath(item.Pack, item.RelativePath)))
                        throw new IOException($"Uncheckpointed build output exists; inspect it rather than overwriting: {item.RelativePath}");
                }
                var candidates = new ConcurrentDictionary<string, CandidateInfo>(StringComparer.Ordinal);
                await Parallel.ForEachAsync(chunk, new ParallelOptions { MaxDegreeOfParallelism = workers, CancellationToken = token }, async (item, ct) =>
                {
                    var source = OriginalPath(item.Pack, item.RelativePath);
                    if (!HashEquals(HashFile(source), item.OriginalSha256)) throw new InvalidDataException($"Immutable original changed: {source}");
                    var working = WorkingPath(item.Pack, item.RelativePath);
                    CreateParent(working);
                    if (IsPilotAudio(item))
                    {
                        var pilotCandidate = PilotFasterPath(item);
                        if (item.PilotFasterSha256 is null) throw new InvalidDataException($"Missing pilot candidate hash: {item.RelativePath}");
                        ReadAndCheckCandidate(item, pilotCandidate, item.PilotFasterSha256);
                        CopyCreateNew(pilotCandidate, working);
                        var copiedInfo = ReadAndCheckCandidate(item, working, item.PilotFasterSha256);
                        candidates[item.Pack + "/" + item.RelativePath] = CandidateFrom(item, copiedInfo);
                    }
                    else
                    {
                        await ConvertAsync(ffmpeg, source, working, ct);
                        var candidate = ReadAndCheckCandidate(item, working);
                        candidates[item.Pack + "/" + item.RelativePath] = CandidateFrom(item, candidate);
                    }
                });
                var workingPaths = chunk.Select(item => WorkingPath(item.Pack, item.RelativePath)).ToArray();
                await DecodeBatchAsync(ffmpeg, workingPaths, token);
                var resultItems = chunk.Select(item => candidates[item.Pack + "/" + item.RelativePath]).ToList();
                var checkpoint = new ChunkCheckpoint { Index = index, Recipe = manifest.Recipe, FfmpegVersion = manifest.FfmpegVersion, Items = resultItems };
                SaveImmutableJson(checkpointPath, checkpoint);
                foreach (var item in chunk)
                {
                    var info = candidates[item.Pack + "/" + item.RelativePath];
                    var working = WorkingPath(item.Pack, item.RelativePath);
                    var faster = FasterPath(item.Pack, item.RelativePath);
                    CreateParent(faster);
                    if (File.Exists(faster)) throw new IOException($"Refusing to overwrite staged candidate: {faster}");
                    File.Move(working, faster);
                    ApplyCandidate(item, info);
                }
                Console.WriteLine($"Validated and checkpointed {Math.Min((index + 1) * BatchSize, manifest.Audio.Count)}/{manifest.Audio.Count} clips");
            }
            BuildTimingCandidates(manifest);
            manifest.Status = "Built";
            SaveManifest(manifest);
            VerifyBuiltStage(manifest);
            WriteSummary("Built", manifest);
        }
        catch
        {
            manifest.Status = "BuildInterrupted";
            SaveManifest(manifest);
            throw;
        }
    }

    private static void FinalizeCheckpoint(int index, AudioItem[] chunk, ChunkCheckpoint checkpoint, string recipe, string ffmpegVersion)
    {
        if (checkpoint.Index != index || checkpoint.Recipe != recipe || checkpoint.FfmpegVersion != ffmpegVersion || checkpoint.Items.Count != chunk.Length)
            throw new InvalidDataException($"Checkpoint contract mismatch: {CheckpointPath(index)}");
        var byKey = checkpoint.Items.ToDictionary(item => item.Pack + "/" + item.RelativePath, StringComparer.Ordinal);
        foreach (var item in chunk)
        {
            var key = item.Pack + "/" + item.RelativePath;
            if (!byKey.TryGetValue(key, out var candidate)) throw new InvalidDataException($"Checkpoint missing {key}.");
            var final = FasterPath(item.Pack, item.RelativePath);
            var working = WorkingPath(item.Pack, item.RelativePath);
            var hasFinal = File.Exists(final);
            var hasWorking = File.Exists(working);
            if (hasFinal == hasWorking) throw new IOException($"Checkpoint outputs are missing or duplicated: {key}");
            var path = hasFinal ? final : working;
            var info = ReadAndCheckCandidate(item, path, candidate.Sha256);
            if (info.Bytes != candidate.Bytes || info.FinalGranulePosition != candidate.Granule || info.SampleRate != item.SampleRate || info.Channels != item.Channels)
                throw new InvalidDataException($"Checkpoint metadata mismatch: {key}");
            if (hasWorking)
            {
                CreateParent(final);
                File.Move(working, final);
            }
            ApplyCandidate(item, candidate);
        }
    }

    private static void BuildTimingCandidates(RunManifest manifest)
    {
        foreach (var table in manifest.Tables)
        {
            var destination = FasterPath(table.Pack, table.RelativePath);
            if (table.State == "Built")
            {
                if (!HashEquals(HashFile(destination), table.FasterSha256!)) throw new InvalidDataException($"Built timing candidate changed: {destination}");
                continue;
            }
            if (table.State is not ("Inventoried" or "CommitPending")) throw new InvalidDataException($"Unexpected timing state {table.State}: {table.Pack}");
            var originalPath = OriginalPath(table.Pack, table.RelativePath);
            var originalBytes = File.ReadAllBytes(originalPath);
            if (!HashEquals(HashBytes(originalBytes), table.OriginalSha256)) throw new InvalidDataException($"Timing original changed: {originalPath}");
            var document = ParseTiming(originalBytes, table.Pack + " original timing table");
            var durations = manifest.Audio.Where(item => item.Pack == table.Pack).ToDictionary(item => item.Key, item => item.FasterDuration ?? throw new InvalidDataException($"Missing faster duration for {item.Key}"), StringComparer.Ordinal);
            EnsureExactKeys(table.Pack, durations.Keys, document.Values.Keys);
            var text = RewriteTiming(document, durations, table.Pack);
            var encoded = EncodeUtf8(text, document.HasBom);
            var expectedHash = HashBytes(encoded);
            CreateParent(destination);
            if (table.State == "CommitPending")
            {
                if (!HashEquals(table.FasterSha256 ?? "", expectedHash)) throw new InvalidDataException($"Timing pending checkpoint mismatch: {table.Pack}");
                if (File.Exists(destination))
                {
                    if (!HashEquals(HashFile(destination), expectedHash)) throw new InvalidDataException($"Timing pending file hash mismatch: {destination}");
                }
                else WriteCreateNew(destination, encoded);
            }
            else
            {
                if (File.Exists(destination)) throw new IOException($"Uncheckpointed timing candidate exists: {destination}");
                table.FasterSha256 = expectedHash;
                table.State = "CommitPending";
                SaveManifest(manifest);
                WriteCreateNew(destination, encoded);
            }
            if (!HashEquals(HashFile(destination), expectedHash)) throw new IOException($"Timing candidate write mismatch: {destination}");
            table.State = "Built";
            SaveManifest(manifest);
        }
    }

    private static byte[] EncodeUtf8(string text, bool bom)
    {
        var body = new UTF8Encoding(false, true).GetBytes(text);
        if (!bom) return body;
        var result = new byte[body.Length + 3];
        result[0] = 0xEF; result[1] = 0xBB; result[2] = 0xBF;
        body.CopyTo(result, 3);
        return result;
    }

    private static void SaveImmutableJson<T>(string path, T value)
    {
        if (File.Exists(path)) throw new IOException($"Refusing to overwrite checkpoint: {path}");
        var bytes = JsonSerializer.SerializeToUtf8Bytes(value, JsonOptions);
        using var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        stream.Write(bytes);
        stream.Flush(flushToDisk: true);
    }

    private static async Task<ProcessResult> ConvertAsync(string ffmpeg, string source, string destination, CancellationToken token)
    {
        var args = new[] { "-hide_banner", "-nostdin", "-n", "-xerror", "-threads", "1", "-filter_threads", "1", "-i", source,
            "-map", "0:a:0", "-vn", "-filter:a", "atempo=1.5", "-c:a", "libvorbis", "-q:a", "5", "-threads", "1", destination };
        var result = await RunProcessAsync(ffmpeg, args, TimeSpan.FromMinutes(10), token);
        if (result.ExitCode != 0) throw new InvalidDataException($"ffmpeg conversion failed for {source}: {result.Error}");
        return result;
    }

    private static async Task<ProcessResult> DecodeBatchAsync(string ffmpeg, IReadOnlyList<string> paths, CancellationToken token)
    {
        if (paths.Count is < 1 or > BatchSize) throw new ArgumentOutOfRangeException(nameof(paths));
        var args = new List<string> { "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-filter_threads", "1" };
        foreach (var path in paths) { args.Add("-threads"); args.Add("1"); args.Add("-i"); args.Add(path); }
        for (var i = 0; i < paths.Count; i++) { args.Add("-map"); args.Add($"{i}:a:0"); }
        args.AddRange(["-c:a", "pcm_s16le", "-threads", "1", "-f", "null", "NUL"]);
        var result = await RunProcessAsync(ffmpeg, args, TimeSpan.FromMinutes(30), token);
        if (result.ExitCode != 0) throw new InvalidDataException($"Full decode validation failed for batch ({paths.Count} clips): {result.Error}");
        return result;
    }

    private static async Task BenchmarkAsync(string ffmpeg, int count, CancellationToken token)
    {
        if (count is < 1 or > 128) throw new ArgumentOutOfRangeException(nameof(count), "Benchmark count must be 1 through 128.");
        var manifest = LoadManifest();
        if (manifest.Status == "InventoryBuilding") throw new InvalidDataException("Inventory is incomplete.");
        VerifyOriginalSnapshots(manifest);
        var ordered = manifest.Audio.OrderBy(item => Array.IndexOf(Packs, item.Pack)).ThenBy(item => item.OriginalDuration).ToArray();
        var sample = Enumerable.Range(0, Math.Min(count, ordered.Length)).Select(i => ordered[(int)Math.Round(i * (ordered.Length - 1d) / Math.Max(1, Math.Min(count, ordered.Length) - 1))]).ToArray();
        var runId = DateTime.UtcNow.ToString("yyyyMMddTHHmmssfffZ", CultureInfo.InvariantCulture) + "-" + Guid.NewGuid().ToString("N")[..8];
        var runs = new List<object>();
        foreach (var workers in new[] { 1, 2, 4, 8 })
        {
            token.ThrowIfCancellationRequested();
            var outputRoot = Path.Combine(Stage, "benchmark", runId, $"workers-{workers}");
            if (Directory.Exists(outputRoot)) throw new IOException($"Benchmark output already exists: {outputRoot}");
            Directory.CreateDirectory(outputRoot);
            var completed = new ConcurrentBag<(AudioItem Item, string Path)>();
            long cpuTicks = 0;
            var timer = Stopwatch.StartNew();
            await Parallel.ForEachAsync(sample, new ParallelOptions { MaxDegreeOfParallelism = workers, CancellationToken = token }, async (item, ct) =>
            {
                var source = OriginalPath(item.Pack, item.RelativePath);
                var dest = Under(Path.Combine(outputRoot, item.Pack), item.RelativePath);
                CreateParent(dest);
                var run = await ConvertAsync(ffmpeg, source, dest, ct);
                var candidate = OggReader.Read(dest);
                CheckTempo(new OggInfo(item.OriginalDuration, item.SampleRate, item.Channels, item.OriginalSha256, item.OriginalBytes, item.OriginalGranule, 0), candidate, item.RelativePath);
                Interlocked.Add(ref cpuTicks, run.CpuTime.Ticks);
                completed.Add((item, dest));
            });
            var outputPaths = completed.Select(item => item.Path).Order(StringComparer.Ordinal).ToArray();
            foreach (var batch in outputPaths.Chunk(BatchSize))
            {
                var decode = await DecodeBatchAsync(ffmpeg, batch, token);
                Interlocked.Add(ref cpuTicks, decode.CpuTime.Ticks);
            }
            timer.Stop();
            runs.Add(new { Workers = workers, Files = completed.Count, WallSeconds = timer.Elapsed.TotalSeconds, CpuSeconds = TimeSpan.FromTicks(cpuTicks).TotalSeconds, FilesPerSecond = completed.Count / timer.Elapsed.TotalSeconds, Output = outputRoot });
        }
        var report = Path.Combine(Stage, "benchmark", $"{runId}.json");
        Directory.CreateDirectory(Path.GetDirectoryName(report)!);
        SaveImmutableJson(report, new { RunId = runId, CreatedAtUtc = DateTime.UtcNow, FfmpegVersion = manifest.FfmpegVersion, SampleCount = sample.Length, Runs = runs });
        Console.WriteLine(report);
    }

    private static void VerifyOriginalSnapshots(RunManifest manifest)
    {
        foreach (var item in manifest.Audio)
            if (!HashEquals(HashFile(OriginalPath(item.Pack, item.RelativePath)), item.OriginalSha256)) throw new InvalidDataException($"Original snapshot hash mismatch: {item.Pack}/{item.RelativePath}");
        foreach (var table in manifest.Tables)
            if (!HashEquals(HashFile(OriginalPath(table.Pack, table.RelativePath)), table.OriginalSha256)) throw new InvalidDataException($"Original timing snapshot hash mismatch: {table.Pack}");
    }

    private static void VerifyBuiltStage(RunManifest manifest)
    {
        VerifyOriginalSnapshots(manifest);
        if (manifest.Audio.Any(item => item.State != "Built" || item.FasterSha256 is null || item.FasterDuration is null) ||
            manifest.Tables.Any(table => table.State != "Built" || table.FasterSha256 is null)) throw new InvalidDataException("Build is not complete.");
        foreach (var item in manifest.Audio)
        {
            var path = FasterPath(item.Pack, item.RelativePath);
            if (!HashEquals(HashFile(path), item.FasterSha256!)) throw new InvalidDataException($"Faster staged clip hash mismatch: {item.Pack}/{item.RelativePath}");
        }
        foreach (var table in manifest.Tables)
        {
            var original = ParseTiming(File.ReadAllBytes(OriginalPath(table.Pack, table.RelativePath)), table.Pack + " original table");
            var fasterBytes = File.ReadAllBytes(FasterPath(table.Pack, table.RelativePath));
            if (!HashEquals(HashBytes(fasterBytes), table.FasterSha256!)) throw new InvalidDataException($"Faster timing hash mismatch: {table.Pack}");
            var faster = ParseTiming(fasterBytes, table.Pack + " faster table");
            var keys = manifest.Audio.Where(item => item.Pack == table.Pack).Select(item => item.Key).ToArray();
            EnsureExactKeys(table.Pack, keys, faster.Values.Keys);
            foreach (var item in manifest.Audio.Where(item => item.Pack == table.Pack))
                if (double.Parse(faster.Values[item.Key], CultureInfo.InvariantCulture) != item.FasterDuration)
                    throw new InvalidDataException($"Measured candidate timing mismatch: {table.Pack}/{item.Key}");
            if (!string.Equals(MaskTiming(original.Text, keys), MaskTiming(faster.Text, keys), StringComparison.Ordinal))
                throw new InvalidDataException($"Timing table has unrelated changes: {table.Pack}");
        }
    }

    private sealed record LiveFile(string Pack, string RelativePath, string Path, string Hash, bool IsTable);
    private sealed record StagedFile(string Sha256, string Path);
    private sealed record AdaptiveSet(Dictionary<string, StagedFile> Audio, Dictionary<string, StagedFile> Tables, int Placeholders);
    private enum Target { Original, Faster, Adaptive }

    private static string AdaptivePath(string relative) => Under(AdaptiveRun, relative);

    // Binds the adaptive run to this stage by content: every candidate's source hash must equal the stage original hash,
    // so a later legacy deploy/restore rewriting the stage manifest status does not break the binding.
    // Not validated here: full candidate decode; that is AdaptiveAudioBatch.py --validate and must pass before deploy-adaptive.
    // verifyFiles=false only recognises adaptive hashes from run-manifest.json so deploy/restore never depend on the candidate files.
    private static AdaptiveSet LoadAdaptiveSet(RunManifest manifest, bool verifyFiles)
    {
        using var document = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(AdaptiveRun, "run-manifest.json")));
        var root = document.RootElement;
        if (root.GetProperty("schema").GetInt32() != 3 || root.GetProperty("scope").GetString() != "full" ||
            root.GetProperty("status").GetString() != "candidate_only_review_required" ||
            root.GetProperty("validation").GetProperty("status").GetString() != "passed" ||
            !string.Equals(Path.GetFullPath(root.GetProperty("stage").GetString()!), Path.GetFullPath(Stage), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Adaptive run manifest is not the validated full-corpus run for this stage.");
        var stageItems = manifest.Audio.ToDictionary(item => AudioKey(item.Pack, item.RelativePath), StringComparer.Ordinal);
        var audio = new Dictionary<string, StagedFile>(StringComparer.Ordinal);
        var durations = new Dictionary<string, double>(StringComparer.Ordinal);
        var placeholders = 0;
        foreach (var candidate in root.GetProperty("candidate_items").EnumerateArray())
        {
            var pack = candidate.GetProperty("pack").GetString()!;
            var relative = candidate.GetProperty("relative_path").GetString()!;
            var key = AudioKey(pack, relative);
            if (!stageItems.TryGetValue(key, out var item)) throw new InvalidDataException($"Adaptive candidate is outside the frozen corpus: {key}");
            if (!HashEquals(candidate.GetProperty("source_sha256").GetString()!, item.OriginalSha256)) throw new InvalidDataException($"Adaptive candidate source hash differs from stage original: {key}");
            if (candidate.GetProperty("candidate_path").GetString() != $"candidate_packs/{pack}/{relative}") throw new InvalidDataException($"Adaptive candidate path contract violated: {key}");
            if (candidate.GetProperty("decode_status").GetString() != "passed") throw new InvalidDataException($"Adaptive candidate did not pass decode: {key}");
            var sha = NormalizeHash(candidate.GetProperty("candidate_sha256").GetString()!);
            var status = candidate.GetProperty("proposal_status").GetString();
            if (status == "review")
            {
                if (!HashEquals(sha, item.OriginalSha256)) throw new InvalidDataException($"Review placeholder is not the exact original: {key}");
                placeholders++;
            }
            else if (status != "ready") throw new InvalidDataException($"Unknown adaptive proposal status for {key}: {status}");
            var path = AdaptivePath(Path.Combine("candidate_packs", pack, relative));
            if (verifyFiles && !HashEquals(HashFile(path), sha)) throw new InvalidDataException($"Adaptive staged clip hash mismatch: {key}");
            if (!audio.TryAdd(key, new StagedFile(sha, path))) throw new InvalidDataException($"Duplicate adaptive candidate: {key}");
            durations[AudioKey(pack, item.Key)] = candidate.GetProperty("decode").GetProperty("duration").GetDouble();
        }
        if (audio.Count != stageItems.Count) throw new InvalidDataException("Adaptive run does not cover the exact frozen corpus.");
        var tables = new Dictionary<string, StagedFile>(StringComparer.Ordinal);
        foreach (var output in root.GetProperty("table_outputs").EnumerateArray())
        {
            var pack = output.GetProperty("pack").GetString()!;
            var tableRecord = manifest.Tables.Single(table => table.Pack == pack);
            if (output.GetProperty("relative_path").GetString() != $"candidate_packs/{pack}/{tableRecord.RelativePath}") throw new InvalidDataException($"Adaptive table path contract violated: {pack}");
            var path = AdaptivePath(Path.Combine("candidate_packs", pack, tableRecord.RelativePath));
            var sha = NormalizeHash(output.GetProperty("sha256").GetString()!);
            if (!tables.TryAdd(pack, new StagedFile(sha, path))) throw new InvalidDataException($"Duplicate adaptive table: {pack}");
            if (!verifyFiles) continue;
            var bytes = File.ReadAllBytes(path);
            if (!HashEquals(HashBytes(bytes), sha)) throw new InvalidDataException($"Adaptive timing hash mismatch: {pack}");
            var adaptive = ParseTiming(bytes, pack + " adaptive table");
            var original = ParseTiming(File.ReadAllBytes(OriginalPath(pack, tableRecord.RelativePath)), pack + " original table");
            var keys = manifest.Audio.Where(item => item.Pack == pack).Select(item => item.Key).ToArray();
            EnsureExactKeys(pack, keys, adaptive.Values.Keys);
            foreach (var key in keys)
                if (double.Parse(adaptive.Values[key], CultureInfo.InvariantCulture) != durations[AudioKey(pack, key)])
                    throw new InvalidDataException($"Adaptive timing differs from measured candidate duration: {pack}/{key}");
            if (!string.Equals(MaskTiming(original.Text, keys), MaskTiming(adaptive.Text, keys), StringComparison.Ordinal))
                throw new InvalidDataException($"Adaptive timing table has unrelated changes: {pack}");
        }
        if (!tables.Keys.ToHashSet(StringComparer.Ordinal).SetEquals(Packs)) throw new InvalidDataException("Adaptive run does not cover each timing table.");
        return new AdaptiveSet(audio, tables, placeholders);
    }

    private static StagedFile StagedFor(Target target, RunManifest manifest, Dictionary<string, AudioItem> audioByKey, AdaptiveSet adaptive, string pack, string relativePath, bool isTable)
    {
        if (isTable)
        {
            var table = manifest.Tables.Single(entry => entry.Pack == pack);
            return target switch
            {
                Target.Original => new StagedFile(table.OriginalSha256, OriginalPath(pack, table.RelativePath)),
                Target.Faster => new StagedFile(table.FasterSha256!, FasterPath(pack, table.RelativePath)),
                _ => adaptive.Tables[pack],
            };
        }
        var item = audioByKey[AudioKey(pack, relativePath)];
        return target switch
        {
            Target.Original => new StagedFile(item.OriginalSha256, OriginalPath(pack, relativePath)),
            Target.Faster => new StagedFile(item.FasterSha256!, FasterPath(pack, relativePath)),
            _ => adaptive.Audio[AudioKey(pack, relativePath)],
        };
    }

    private static List<LiveFile> ReadLiveInventory(RunManifest manifest, AdaptiveSet adaptive, bool checkTemporaries = false)
    {
        var records = manifest.Audio.ToDictionary(item => AudioKey(item.Pack, item.RelativePath), StringComparer.Ordinal);
        var live = new List<LiveFile>(manifest.Audio.Count + Packs.Length);
        foreach (var pack in Packs)
        {
            var root = PackPath(pack);
            var sounds = Path.Combine(root, "generated", "sounds");
            var actual = Directory.EnumerateFiles(sounds, "*.ogg", SearchOption.AllDirectories)
                .Select(path => (Pack: pack, RelativePath: Path.GetRelativePath(root, path).Replace('\\', '/'), Path: path)).ToArray();
            var actualSet = actual.Select(item => (item.Pack, item.RelativePath)).ToHashSet();
            var expected = manifest.Audio.Where(item => item.Pack == pack).Select(item => (item.Pack, item.RelativePath)).ToHashSet();
            if (!expected.SetEquals(actualSet)) throw new InvalidDataException($"Live audio inventory changed for {pack}; added or missing Ogg files.");
            foreach (var item in actual)
            {
                AssertNoReparse(item.Path, root);
                var hash = HashFile(item.Path);
                var key = AudioKey(pack, item.RelativePath);
                var record = records[key];
                var faster = record.FasterSha256;
                if (!HashEquals(hash, record.OriginalSha256) && (faster is null || !HashEquals(hash, faster)) && !HashEquals(hash, adaptive.Audio[key].Sha256))
                    throw new InvalidDataException($"Live clip has an unknown hash: {pack}/{item.RelativePath}");
                live.Add(new LiveFile(pack, item.RelativePath, item.Path, hash, false));
            }
            var tableRelative = "generated/sound_length_table.lua";
            var tablePath = Path.Combine(root, tableRelative.Replace('/', Path.DirectorySeparatorChar));
            AssertNoReparse(tablePath, root);
            var table = manifest.Tables.Single(entry => entry.Pack == pack);
            var tableHash = HashFile(tablePath);
            if (!HashEquals(tableHash, table.OriginalSha256) && (table.FasterSha256 is null || !HashEquals(tableHash, table.FasterSha256)) &&
                (table.InitialPilotSha256 is null || !HashEquals(tableHash, table.InitialPilotSha256)) && !HashEquals(tableHash, adaptive.Tables[pack].Sha256))
                throw new InvalidDataException($"Live timing table has an unknown hash: {pack}");
            live.Add(new LiveFile(pack, tableRelative, tablePath, tableHash, true));
        }
        if (checkTemporaries)
        {
            foreach (var item in live)
            {
                var temporary = item.Path + ".spoken-batch-tmp";
                if (File.Exists(temporary)) throw new IOException($"Target replacement temp already exists: {temporary}");
            }
        }
        return live;
    }

    private static string AudioKey(string pack, string relativePath) => pack + "/" + relativePath;

    private static void AtomicReplace(string staged, LiveFile target, string expectedHash)
    {
        var current = HashFile(target.Path);
        if (!HashEquals(current, target.Hash)) throw new IOException($"Live file changed after preflight: {target.Pack}/{target.RelativePath}");
        if (HashEquals(current, expectedHash)) return;
        var temporary = target.Path + ".spoken-batch-tmp";
        if (File.Exists(temporary)) throw new IOException($"Target replacement temp already exists: {temporary}");
        var ownsTemporary = false;
        try
        {
            File.Copy(staged, temporary, overwrite: false);
            ownsTemporary = true;
            if (!HashEquals(HashFile(temporary), expectedHash)) throw new IOException($"Replacement temp hash mismatch: {target.RelativePath}");
            if (!HashEquals(HashFile(target.Path), target.Hash)) throw new IOException($"Live file changed before replacement: {target.RelativePath}");
            File.Replace(temporary, target.Path, null);
            if (!HashEquals(HashFile(target.Path), expectedHash)) throw new IOException($"Replacement verification failed: {target.RelativePath}");
        }
        catch
        {
            if (ownsTemporary && File.Exists(temporary)) File.Delete(temporary);
            throw;
        }
    }

    private static async Task ApplyAsync(Target target, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        AssertGameStopped();
        var manifest = LoadManifest();
        VerifyBuiltStage(manifest);
        var adaptive = LoadAdaptiveSet(manifest, verifyFiles: target == Target.Adaptive);
        var live = ReadLiveInventory(manifest, adaptive, checkTemporaries: true);
        var audioByKey = manifest.Audio.ToDictionary(item => AudioKey(item.Pack, item.RelativePath), StringComparer.Ordinal);
        foreach (var pack in Packs)
        {
            token.ThrowIfCancellationRequested();
            foreach (var file in live.Where(item => item.Pack == pack && !item.IsTable))
            {
                token.ThrowIfCancellationRequested();
                var staged = StagedFor(target, manifest, audioByKey, adaptive, pack, file.RelativePath, isTable: false);
                AtomicReplace(staged.Path, file, staged.Sha256);
            }
            var table = live.Single(item => item.Pack == pack && item.IsTable);
            var stagedTable = StagedFor(target, manifest, audioByKey, adaptive, pack, table.RelativePath, isTable: true);
            AtomicReplace(stagedTable.Path, table, stagedTable.Sha256);
            Console.WriteLine($"{pack}: clips and timing table processed");
        }
        var after = ReadLiveInventory(manifest, adaptive);
        foreach (var item in after)
            if (!HashEquals(item.Hash, StagedFor(target, manifest, audioByKey, adaptive, item.Pack, item.RelativePath, item.IsTable).Sha256))
                throw new InvalidDataException($"Post-deployment {target} hash mismatch: {item.Pack}/{item.RelativePath}");
        if (target == Target.Adaptive)
        {
            // The frozen stage manifest is the adaptive run's immutable source; record adaptive deployments beside the run instead.
            var record = Path.Combine(AdaptiveRun, $"deployment-{DateTime.UtcNow:yyyyMMddTHHmmssZ}.json");
            WriteCreateNew(record, JsonSerializer.SerializeToUtf8Bytes(new
            {
                Status = "AdaptiveDeployed", AddOns, DeployedAtUtc = DateTime.UtcNow, AudioFiles = adaptive.Audio.Count,
                ReviewPlaceholdersAtOriginalSpeed = adaptive.Placeholders, TimingTables = adaptive.Tables.Count, VerifiedInstalledFiles = after.Count,
            }, JsonOptions));
        }
        else
        {
            manifest.Status = target == Target.Faster ? "Deployed" : "Restored";
            SaveManifest(manifest);
        }
        Console.WriteLine($"{target} set verified: {after.Count} installed files. In WoW, /reload refreshes addon metadata.");
        await Task.CompletedTask;
    }

    private static void AssertGameStopped()
    {
        foreach (var process in Process.GetProcessesByName("WowB"))
        {
            using (process)
            {
                var executable = process.MainModule?.FileName ?? throw new IOException("Cannot inspect running WowB process.");
                if (string.Equals(executable, Path.Combine(Path.GetDirectoryName(Path.GetDirectoryName(AddOns))!, "WowB.exe"), StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Close this WoW client before deploying or restoring audio.");
            }
        }
    }

    // Valid means every installed file matches one complete deployable set (Adaptive or Faster). Adaptive is checked first
    // because its factor-1 copies and placeholders share original hashes, so the sets overlap only through originals.
    private static void ValidateInstalled()
    {
        var manifest = LoadManifest();
        VerifyBuiltStage(manifest);
        var adaptive = LoadAdaptiveSet(manifest, verifyFiles: true);
        var live = ReadLiveInventory(manifest, adaptive);
        var audioByKey = manifest.Audio.ToDictionary(item => AudioKey(item.Pack, item.RelativePath), StringComparer.Ordinal);
        bool Matches(Target target) => live.All(item => HashEquals(item.Hash, StagedFor(target, manifest, audioByKey, adaptive, item.Pack, item.RelativePath, item.IsTable).Sha256));
        Target? installed = Matches(Target.Adaptive) ? Target.Adaptive : Matches(Target.Faster) ? Target.Faster : null;
        if (installed is null)
        {
            if (Matches(Target.Original)) throw new InvalidDataException("Installed files are original; Validate requires a complete Faster or Adaptive deployment.");
            throw new InvalidDataException("Known partial deployment detected; Validate fails until deploy, deploy-adaptive or restore completes all files.");
        }
        Console.WriteLine(JsonSerializer.Serialize(new { Status = "Valid", TargetState = installed.ToString(), AudioFiles = manifest.Audio.Count, TimingTables = manifest.Tables.Count, VerifiedInstalledFiles = live.Count }, JsonOptions));
    }

    private static PilotSeed LoadPilotSeed()
    {
        var manifestPath = Path.Combine(Pilot, "manifest.json");
        using var document = JsonDocument.Parse(File.ReadAllBytes(manifestPath));
        var root = document.RootElement;
        if (root.GetProperty("Status").GetString() != "Built" || root.GetProperty("QuestId").GetInt32() != 880) throw new InvalidDataException("Existing pilot manifest is not a completed quest 880 build.");
        var clipMap = new Dictionary<string, PilotClip>(StringComparer.Ordinal);
        foreach (var clip in root.GetProperty("Clips").EnumerateArray())
        {
            var name = clip.GetProperty("Name").GetString()!;
            if (name is not ("880-accept.ogg" or "880-complete.ogg")) continue;
            var originalHash = NormalizeHash(clip.GetProperty("OriginalSHA256").GetString()!);
            var fasterHash = NormalizeHash(clip.GetProperty("FasterSHA256").GetString()!);
            var relative = Path.Combine("generated", "sounds", "quests", name);
            var originalPath = Path.Combine(Pilot, "original", relative);
            var fasterPath = Path.Combine(Pilot, "faster", relative);
            var originalInfo = OggReader.Read(originalPath);
            var fasterInfo = OggReader.Read(fasterPath);
            if (!HashEquals(originalHash, originalInfo.Sha256) || !HashEquals(fasterHash, fasterInfo.Sha256)) throw new InvalidDataException($"Pilot clip hash mismatch: {name}");
            CheckTempo(originalInfo, fasterInfo, name);
            if (!clipMap.TryAdd(name, new PilotClip(originalPath, fasterPath, originalHash, fasterHash, originalInfo, fasterInfo))) throw new InvalidDataException($"Duplicate pilot record: {name}");
        }
        if (clipMap.Count != 2) throw new InvalidDataException("Pilot manifest must contain exactly the accept and complete clips.");
        var timing = root.GetProperty("Timing");
        var originalTablePath = Path.Combine(Pilot, "original", "generated", "sound_length_table.lua");
        var fasterTablePath = Path.Combine(Pilot, "faster", "generated", "sound_length_table.lua");
        var originalTable = File.ReadAllBytes(originalTablePath);
        var fasterTable = File.ReadAllBytes(fasterTablePath);
        var originalHashTable = NormalizeHash(timing.GetProperty("OriginalSHA256").GetString()!);
        var fasterHashTable = NormalizeHash(timing.GetProperty("FasterSHA256").GetString()!);
        if (!HashEquals(HashBytes(originalTable), originalHashTable) || !HashEquals(HashBytes(fasterTable), fasterHashTable)) throw new InvalidDataException("Pilot timing table hash mismatch.");
        var originalDoc = ParseTiming(originalTable, "pilot original timing table");
        var fasterDoc = ParseTiming(fasterTable, "pilot faster timing table");
        if (!string.Equals(MaskTiming(originalDoc.Text, ["880-accept", "880-complete"]), MaskTiming(fasterDoc.Text, ["880-accept", "880-complete"]), StringComparison.Ordinal))
            throw new InvalidDataException("Pilot timing table differs outside the two quest 880 values.");
        return new PilotSeed(clipMap, originalTable, originalHashTable, fasterHashTable);
    }

    private static string PilotTableOriginalPath() => Path.Combine(Pilot, "original", "generated", "sound_length_table.lua");

    private static bool HashEquals(string a, string b) => string.Equals(NormalizeHash(a), NormalizeHash(b), StringComparison.Ordinal);
    private static bool IsSha256(string value) => value.Length == 64 && value.All(Uri.IsHexDigit);

    private static void CopyCreateNew(string source, string destination)
    {
        using var input = new FileStream(source, FileMode.Open, FileAccess.Read, FileShare.Read);
        using var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        input.CopyTo(output);
        output.Flush(flushToDisk: true);
    }

    private static void WriteCreateNew(string destination, byte[] bytes)
    {
        using var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        output.Write(bytes);
        output.Flush(flushToDisk: true);
    }

    private static void CreateParent(string file) => Directory.CreateDirectory(Path.GetDirectoryName(file)!);

    private static async Task<string> GetFfmpegVersionAsync(string ffmpeg, CancellationToken token)
    {
        var result = await RunProcessAsync(ffmpeg, ["-version"], TimeSpan.FromMinutes(1), token);
        if (result.ExitCode != 0) throw new InvalidOperationException("ffmpeg -version failed: " + result.Error);
        return result.Output.Split('\n', StringSplitOptions.RemoveEmptyEntries).FirstOrDefault()?.Trim() ?? throw new InvalidDataException("ffmpeg version output is empty.");
    }

    private static void WriteSummary(string status, RunManifest manifest)
    {
        var summary = new
        {
            Status = status,
            AudioFiles = manifest.Audio.Count,
            OriginalBytes = manifest.Audio.Sum(item => item.OriginalBytes),
            Packs = Packs.Select(pack => new { Pack = pack, Files = manifest.Audio.Count(item => item.Pack == pack) }).ToArray(),
            Stage = Stage
        };
        Console.WriteLine(JsonSerializer.Serialize(summary, JsonOptions));
    }

}
