// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json.Nodes;

namespace Provide.Uterm.Tests;

public class SharedCorpusParityTests
{
    [Fact]
    public void CSharp_Goldens_Match_Reference_Copies()
    {
        var localRoot = Path.GetFullPath(TestData.PathTo());
        var repoRoot = FindRepoRoot();
        var fallbackRoot = Path.Combine(repoRoot, "packages", "provide-uterm-csharp", "tests", "Provide.Uterm.Tests", "testdata");

        var localGoldenFiles = ReadLocalGoldenFiles(localRoot).ToList();
        if (localGoldenFiles.Count == 0 && Directory.Exists(fallbackRoot))
        {
            localRoot = fallbackRoot;
            localGoldenFiles = ReadLocalGoldenFiles(localRoot).ToList();
        }

        Assert.NotEmpty(localGoldenFiles);
        var tsRoot = Path.Combine(repoRoot, "packages", "provide-uterm-ts", "testdata");
        var sourceRoots = new[]
        {
            tsRoot,
            Path.Combine(repoRoot, "packages", "provide-uterm-go"),
        };

        foreach (var localPath in localGoldenFiles)
        {
            var localRelative = Normalize(Path.GetRelativePath(localRoot, localPath));
            var hasDirectory = localRelative.Contains('/');

            var localNode = JsonNode.Parse(File.ReadAllText(localPath));
            Assert.NotNull(localNode);

            var references = FindReferences(localPath, localRelative, hasDirectory, sourceRoots, tsRoot);
            Assert.NotEmpty(references);

            foreach (var reference in references)
            {
                var referenceNode = JsonNode.Parse(File.ReadAllText(reference));
                Assert.True(
                    JsonNode.DeepEquals(localNode, referenceNode),
                    $"golden mismatch: {localRelative} vs {reference}");
            }
        }
    }

    private static string FindRepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (Directory.Exists(Path.Combine(dir.FullName, "packages")))
            {
                return dir.FullName;
            }

            dir = dir.Parent;
        }

        return AppContext.BaseDirectory;
    }

    private static IReadOnlyList<string> FindReferences(
        string localPath,
        string localRelative,
        bool hasDirectory,
        string[] sourceRoots,
        string tsRoot)
    {
        var file = Path.GetFileName(localPath);
        var found = new HashSet<string>(StringComparer.Ordinal);

        var orderedRoots = hasDirectory
            ? sourceRoots
            : new[] { tsRoot }.Concat(sourceRoots.Where(r => r != tsRoot)).ToArray();

        foreach (var sourceRoot in orderedRoots)
        {
            if (!Directory.Exists(sourceRoot))
            {
                continue;
            }

            foreach (var candidate in Directory.EnumerateFiles(sourceRoot, file, SearchOption.AllDirectories))
            {
                var candidateRelative = RelativeToTestdata(sourceRoot, candidate);
                if (candidateRelative is null)
                {
                    continue;
                }

                var match = hasDirectory
                    ? candidateRelative.EndsWith(localRelative, StringComparison.Ordinal)
                    : !candidateRelative.Contains('/') && candidateRelative == file;
                if (!match)
                {
                    continue;
                }

                found.Add(candidate);
            }
        }

        if (found.Count == 0 && !hasDirectory && Directory.Exists(tsRoot))
        {
            var fallback = Directory.EnumerateFiles(tsRoot, file, SearchOption.AllDirectories);
            foreach (var candidate in fallback)
            {
                var candidateRelative = RelativeToTestdata(tsRoot, candidate);
                if (candidateRelative is not null && !candidateRelative.Contains('/') && candidateRelative == file)
                {
                    found.Add(candidate);
                }
            }
        }

        return found.OrderBy(v => v).ToList();
    }

    private static IEnumerable<string> ReadLocalGoldenFiles(string localRoot) =>
        Directory.EnumerateFiles(localRoot, "*_golden.json", SearchOption.AllDirectories)
            .Where(p => Path.GetFileName(p) != "vectors.json")
            .OrderBy(p => p);

    private static string? RelativeToTestdata(string sourceRoot, string path)
    {
        var normalizedRoot = Normalize(sourceRoot);
        var normalizedPath = Normalize(path);
        var marker = "/testdata/";
        var rootWithSlash = normalizedRoot.EndsWith('/') ? normalizedRoot : normalizedRoot + "/";
        var rootLen = rootWithSlash.Length;
        var markerAt = normalizedPath.IndexOf(marker, rootLen, StringComparison.Ordinal);
        if (markerAt == -1)
        {
            var localRoot = normalizedRoot.EndsWith('/') ? normalizedRoot : normalizedRoot + "/";
            if (!normalizedPath.StartsWith(localRoot, StringComparison.Ordinal))
            {
                return null;
            }

            var noRoot = normalizedPath[localRoot.Length..];
            return noRoot.Replace('\\', '/');
        }

        var afterMarker = normalizedPath[(markerAt + marker.Length)..];
        var between = normalizedPath[rootLen..markerAt];
        var directoryPrefix = between.Trim('/');
        return directoryPrefix.Length == 0
            ? afterMarker
            : directoryPrefix.Replace('\\', '/') + "/" + afterMarker;
    }

    private static string Normalize(string value) => value.Replace('\\', '/');
}
