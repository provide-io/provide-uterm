using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Json;
using Provide.Uterm.ControlChannel;

namespace ControlChannelDecoderBench;

internal static class Program
{
    private static readonly NumberStyles IntStyles = NumberStyles.Integer;
    private static readonly NumberStyles FloatStyles = NumberStyles.Float;
    private static readonly IFormatProvider Invariant = CultureInfo.InvariantCulture;

    private sealed class XorShift32
    {
        private uint _state;

        public XorShift32(int seed)
        {
            _state = unchecked((uint)seed);
            if (_state == 0)
            {
                _state = 0x9E3779B9u;
            }
        }

        public uint NextUInt32()
        {
            var x = _state;
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            _state = x;
            return x;
        }

        public double NextDouble()
        {
            return NextUInt32() / 4294967296d;
        }
    }

    private record struct Arguments(
        int FrameCount,
        double ControlRatio,
        int ChunkSize,
        int DataSize,
        int ControlSize,
        int Seed,
        int Passes)
    {
        public static Arguments Parse(string[] args)
        {
            var frameCount = 200_000;
            var controlRatio = 0.25;
            var chunkSize = 4_096;
            var dataSize = 256;
            var controlSize = 128;
            var seed = 1337;
            var passes = 5;

            for (var i = 0; i < args.Length; i += 1)
            {
                var arg = args[i];
                if (arg is "--help" or "-h")
                {
                    PrintUsage();
                    Environment.Exit(0);
                }

                if (i + 1 >= args.Length)
                {
                    throw new ArgumentException($"missing value for {arg}");
                }

                var value = args[i + 1];
                switch (arg)
                {
                    case "--frame-count":
                        frameCount = int.Parse(value, IntStyles, Invariant);
                        break;
                    case "--control-ratio":
                        controlRatio = double.Parse(value, FloatStyles, Invariant);
                        break;
                    case "--chunk-size":
                        chunkSize = int.Parse(value, IntStyles, Invariant);
                        break;
                    case "--data-size":
                        dataSize = int.Parse(value, IntStyles, Invariant);
                        break;
                    case "--control-size":
                        controlSize = int.Parse(value, IntStyles, Invariant);
                        break;
                    case "--seed":
                        seed = int.Parse(value, IntStyles, Invariant);
                        break;
                    case "--passes":
                        passes = int.Parse(value, IntStyles, Invariant);
                        break;
                    default:
                        throw new ArgumentException($"unknown argument: {arg}");
                }

                i += 1;
            }

            if (frameCount < 1)
            {
                throw new ArgumentOutOfRangeException(nameof(frameCount), "frame count must be >= 1");
            }

            if (passes < 1)
            {
                throw new ArgumentOutOfRangeException(nameof(passes), "passes must be >= 1");
            }

            if (chunkSize < 1)
            {
                throw new ArgumentOutOfRangeException(nameof(chunkSize), "chunk size must be >= 1");
            }

            if (dataSize < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(dataSize), "data size must be >= 0");
            }

            if (controlSize < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(controlSize), "control size must be >= 0");
            }

            if (controlRatio <= 0.0 || controlRatio >= 1.0)
            {
                throw new ArgumentOutOfRangeException(nameof(controlRatio), "control ratio must be > 0 and < 1");
            }

            return new Arguments(frameCount, controlRatio, chunkSize, dataSize, controlSize, seed, passes);
        }

        private static void PrintUsage()
        {
            Console.WriteLine(
                "Usage: dotnet run --project packages/provide-uterm-csharp/benchmarks/ControlChannelDecoderBench/"
                + "ControlChannelDecoderBench.csproj -- --frame-count 200000 --control-ratio 0.25 --chunk-size 4096 "
                + "--data-size 256 --control-size 128 --passes 5");
            Console.WriteLine("  --frame-count N      Number of frames to generate");
            Console.WriteLine("  --control-ratio F    Control frame ratio in stream (0 < F < 1)");
            Console.WriteLine("  --chunk-size N       Chunk size in bytes");
            Console.WriteLine("  --data-size N        Terminal payload size");
            Console.WriteLine("  --control-size N     Control payload size");
            Console.WriteLine("  --seed N             RNG seed");
            Console.WriteLine("  --passes N           Repeat benchmark passes");
        }
    }

    public static int Main(string[] args)
    {
        try
        {
            var parsed = Arguments.Parse(args);
            var stream = BuildBenchmarkStream(parsed);
            var chunks = ChunkStream(stream, parsed.ChunkSize);
            var (medianSeconds, meanSeconds, minSeconds, events) = Benchmark(chunks, parsed.Passes);

            var payloadMiB = stream.Length / (1024d * 1024d);
            var mibPerSec = payloadMiB / medianSeconds;

            Console.WriteLine(
                "Generated stream: {0} bytes, {1} frames, chunk size {2}",
                stream.Length,
                parsed.FrameCount,
                parsed.ChunkSize);
            Console.WriteLine(
                "After   (csharp):  {0:F4}s, {0:F4}s median, {1:F2} MiB/s",
                medianSeconds,
                mibPerSec);
            Console.WriteLine("Events emitted: {0}", events);
            Console.WriteLine(
                "Stability: {0} runs, min={1:F4}s, mean={2:F4}s",
                parsed.Passes,
                minSeconds,
                meanSeconds);

            var json = JsonSerializer.Serialize(
                new
                {
                    backend = "csharp",
                    generated_bytes = stream.Length,
                    frame_count = parsed.FrameCount,
                    chunk_size = parsed.ChunkSize,
                    median_seconds = medianSeconds,
                    mean_seconds = meanSeconds,
                    min_seconds = minSeconds,
                    events = events,
                    mib_per_s = mibPerSec,
                });

            Console.WriteLine(json);
            return 0;
        }
        catch (Exception ex) when (ex is ArgumentException or FormatException or OverflowException)
        {
            Console.Error.WriteLine(ex.Message);
            return 2;
        }
    }

    private static byte[] BuildBenchmarkStream(Arguments args)
    {
        var random = new XorShift32(args.Seed);
        var dataSegment = new string('x', args.DataSize);
        var controlPayload = new string('x', args.ControlSize);
        var builder = new StringBuilder(capacity: Math.Max(1, args.FrameCount * Math.Min(Math.Max(args.DataSize, 1), 128)));

        for (var i = 0; i < args.FrameCount; i += 1)
        {
            if (random.NextDouble() < args.ControlRatio)
            {
                var payload = new Dictionary<string, object?>(StringComparer.Ordinal)
                {
                    ["type"] = "bench",
                    ["id"] = i,
                    ["seed"] = args.Seed,
                    ["payload"] = controlPayload,
                };
                builder.Append(ControlChannelCodec.EncodeControlFrame(payload));
                continue;
            }

            var segment = dataSegment;
            if (random.NextDouble() < 0.01 && segment.Length > 0)
            {
                var midpoint = Math.Min(64, segment.Length / 2);
                var escaped = new StringBuilder(segment.Length + 8);
                escaped.Append(segment, 0, midpoint);
                escaped.Append(ControlChannelCodec.Dle);
                escaped.Append("DLE_ESC");
                escaped.Append(segment, midpoint, segment.Length - midpoint);
                segment = escaped.Length > dataSegment.Length
                    ? escaped.ToString(0, Math.Min(escaped.Length, dataSegment.Length))
                    : escaped.ToString();
            }

            builder.Append(ControlChannelCodec.EncodeTerminalData(segment));
        }

        return Encoding.UTF8.GetBytes(builder.ToString());
    }

    private static List<byte[]> ChunkStream(byte[] data, int chunkSize)
    {
        var chunks = new List<byte[]>(Math.Max(1, data.Length / chunkSize + 1));
        for (var offset = 0; offset < data.Length; offset += chunkSize)
        {
            var size = Math.Min(chunkSize, data.Length - offset);
            var chunk = new byte[size];
            Buffer.BlockCopy(data, offset, chunk, 0, size);
            chunks.Add(chunk);
        }

        return chunks;
    }

    private static (double MedianSeconds, double MeanSeconds, double MinSeconds, int Events) Benchmark(
        List<byte[]> chunks,
        int passes)
    {
        var samples = new double[passes];
        var eventsFromLastRun = 0;

        for (var i = 0; i < passes; i += 1)
        {
            var decoder = new ControlFrameDecoder();
            var sw = Stopwatch.StartNew();

            var events = 0;
            foreach (var chunk in chunks)
            {
                foreach (var _ in decoder.FeedBytes(chunk))
                {
                    events += 1;
                }
            }

            foreach (var _ in decoder.Finish())
            {
                events += 1;
            }

            sw.Stop();
            samples[i] = sw.Elapsed.TotalSeconds;
            eventsFromLastRun = events;
        }

        Array.Sort(samples);
        var min = samples[0];
        var mean = ComputeMean(samples);
        var median = samples[samples.Length / 2];

        return (median, mean, min, eventsFromLastRun);
    }

    private static double ComputeMean(double[] values)
    {
        var total = 0.0;
        for (var i = 0; i < values.Length; i += 1)
        {
            total += values[i];
        }

        return values.Length > 0 ? total / values.Length : 0.0;
    }
}
