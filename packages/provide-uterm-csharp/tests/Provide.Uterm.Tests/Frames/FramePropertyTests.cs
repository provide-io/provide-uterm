using System;
using System.Text.Json;
using FsCheck;
using FsCheck.Xunit;
using Provide.Uterm.Frames;
using Xunit;

namespace Provide.Uterm.Tests.Frames;

public class FramePropertyTests
{
    [Property(MaxTest = 1000)]
    public void ParseFrame_NeverCrashesWithGarbage(string randomJson)
    {
        if (randomJson == null) return;
        
        try 
        {
            FrameCodec.DecodeFrame(randomJson);
        }
        catch (JsonException)
        {
            // Invalid JSON is expected to fail safely with JsonException
        }
        catch (ArgumentException)
        {
            // Missing required fields like 'type'
        }
        catch (Exception ex)
        {
            Assert.Fail($"ParseFrame crashed with {ex.GetType().Name}: {ex.Message}");
        }
    }
}
