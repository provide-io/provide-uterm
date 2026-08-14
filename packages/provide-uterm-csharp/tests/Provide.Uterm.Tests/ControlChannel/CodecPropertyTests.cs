using System;
using System.IO;
using System.Linq;
using FsCheck;
using FsCheck.Xunit;
using Provide.Uterm.ControlChannel;
using Xunit;

namespace Provide.Uterm.Tests.ControlChannel;

public class CodecPropertyTests
{
    [Property(MaxTest = 1000)]
    public void Decoder_FeedBytes_NeverCrashesWithGarbage(byte[] randomBytes)
    {
        if (randomBytes == null) return;

        var decoder = new Decoder(new DecoderOptions { MaxBufferBytes = 10 * 1024 * 1024 });
        try
        {
            decoder.FeedBytes(randomBytes);
            decoder.Finish();
        }
        catch (ProtocolException)
        {
            // Protocol exceptions are expected for garbage data
        }
        catch (Exception ex)
        {
            // Any other exception (like IndexOutOfRangeException, ArgumentException, etc.) 
            // is considered a crash/panic and should fail the property test!
            Assert.Fail($"Decoder crashed with {ex.GetType().Name}: {ex.Message}");
        }
    }

    [Property]
    public void EncodeTerminalData_RoundTrips(string terminalData)
    {
        if (terminalData == null) return;

        var encoded = ControlChannelCodec.EncodeTerminalData(terminalData);
        var decoder = new Decoder();
        
        var chunks = decoder.Feed(encoded);
        var final = decoder.Finish();
        
        var allChunks = chunks.Concat(final).ToList();
        var decodedString = string.Join("", allChunks.OfType<DataChunk>().Select(c => c.Data));
        
        Assert.Equal(terminalData, decodedString);
    }
    
    [Property(MaxTest = 1000)]
    public void IsControlFrame_NeverCrashes(byte[] randomBytes)
    {
        if (randomBytes == null) return;
        
        try 
        {
            ControlChannelCodec.IsControlFrame(randomBytes);
        }
        catch (ProtocolException) 
        {
            // valid
        }
        catch (Exception ex)
        {
            Assert.Fail($"IsControlFrame crashed with {ex.GetType().Name}: {ex.Message}");
        }
    }

    [Fact]
    public void Decoder_FuzzCorpus_NeverCrashes()
    {
        var basePath = AppDomain.CurrentDomain.BaseDirectory;
        // From bin/Debug/netX.0 to Provide.Uterm.Tests is 3 levels up
        // From Provide.Uterm.Tests to repo root is 4 levels up
        var corpusPath = Path.GetFullPath(Path.Combine(basePath, "..", "..", "..", "..", "..", "..", "..", "tests", "fuzz_corpus"));
        
        if (!Directory.Exists(corpusPath)) return;

        foreach (var file in Directory.GetFiles(corpusPath, "*.bin"))
        {
            var bytes = File.ReadAllBytes(file);
            var decoder = new Decoder(new DecoderOptions { MaxBufferBytes = 10 * 1024 * 1024 });
            try
            {
                decoder.FeedBytes(bytes);
                decoder.Finish();
            }
            catch (ProtocolException)
            {
                // Expected
            }
            catch (Exception ex)
            {
                Assert.Fail($"Decoder crashed on fuzz corpus file {Path.GetFileName(file)} with {ex.GetType().Name}: {ex.Message}");
            }
        }
    }
}
