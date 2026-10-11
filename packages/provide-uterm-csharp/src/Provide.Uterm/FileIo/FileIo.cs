//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.Ansi;
using Provide.Uterm.Platform;

namespace Provide.Uterm.FileIo;

/// <summary>
/// File I/O helpers for secure recording sinks, BBS screen files, and color palettes.
/// Port of provide.uterm.file_io / packages/provide-uterm-go/fileio.
/// </summary>
public static class FileIo
{
    /// <summary>
    /// Apply POSIX permissions to <paramref name="path"/> where the platform has
    /// them, and do nothing where it does not.
    ///
    /// <c>File.SetUnixFileMode</c> is annotated unsupported on Windows, so every
    /// unguarded call site is a CA1416. The guard is an explicit OS test rather
    /// than a caught <see cref="PlatformNotSupportedException"/> because the
    /// analyzer cannot see through the catch, and because catching hides the
    /// difference between "this platform has no POSIX modes" and "the mode could
    /// not be applied" — only the first is benign.
    /// </summary>
    public static void TrySetUnixFileMode(string path, UnixFileMode mode)
    {
        if (!OsPlatform.HasUnixFileModes)
        {
            return;
        }

        ApplyUnixFileMode(path, mode);
    }

    /// <summary>
    /// Test seam for the chmod itself, so a refused chmod (EPERM on a file
    /// another user owns) can be provoked even when the suite runs as root.
    /// Defaults to <see cref="File.SetUnixFileMode(string, UnixFileMode)"/>;
    /// its only caller is guarded by <see cref="OsPlatform.HasUnixFileModes"/>.
    /// </summary>
#pragma warning disable CA1416 // reached only behind OsPlatform.HasUnixFileModes
    internal static Action<string, UnixFileMode> ApplyUnixFileMode { get; set; } =
        static (path, mode) => File.SetUnixFileMode(path, mode);
#pragma warning restore CA1416

    private static void EnsureOwnerOnlyDir(string directory, UnixFileMode mode)
    {
        Directory.CreateDirectory(directory);
        TrySetUnixFileMode(directory, mode);
    }

    /// <summary>
    /// Create/open path for append with owner-only permissions when the platform supports it.
    /// </summary>
    public static FileStream SecureOpenAppend(string path) =>
        SecureOpenAppendMode(path, UnixFileMode.UserRead | UnixFileMode.UserWrite,
            UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);

    public static FileStream SecureOpenAppendMode(string path, UnixFileMode mode, UnixFileMode dirMode)
    {
        var dir = Path.GetDirectoryName(path);
        if (!string.IsNullOrEmpty(dir))
        {
            EnsureOwnerOnlyDir(dir, dirMode);
        }

        // Refuse to open through a symlink at the target path when possible.
        if (File.Exists(path))
        {
            // A symlink has a LinkTarget everywhere; the attribute also catches
            // Windows reparse points that are not links (junction-like targets).
            var info = new FileInfo(path);
            if (info.LinkTarget is not null || (info.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new IOException($"refusing to open symlink or reparse-point recording sink: {path}");
            }
        }

        var fs = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read);
        TrySetUnixFileMode(path, mode);
        return fs;
    }

    /// <summary>Load a .ans file as latin-1 text.</summary>
    public static string LoadAns(string path)
    {
        var raw = File.ReadAllBytes(path);
        var chars = new char[raw.Length];
        for (var i = 0; i < raw.Length; i++)
        {
            chars[i] = (char)raw[i];
        }

        return new string(chars);
    }

    /// <summary>Load a plain UTF-8 text file.</summary>
    public static string LoadTxt(string path) => File.ReadAllText(path, Encoding.UTF8);

    /// <summary>Load a JSON 256-color palette (list of 16 ints 0-255).</summary>
    public static int[] LoadPalette(string path)
    {
        if (string.IsNullOrEmpty(path))
        {
            return (int[])AnsiConstants.DefaultPalette.Clone();
        }

        var raw = File.ReadAllText(path);
        using var doc = JsonDocument.Parse(raw);
        if (doc.RootElement.ValueKind != JsonValueKind.Array || doc.RootElement.GetArrayLength() != 16)
        {
            throw new FormatException("palette map must be a JSON list of 16 integers");
        }

        var outArr = new int[16];
        var i = 0;
        foreach (var el in doc.RootElement.EnumerateArray())
        {
            if (el.ValueKind != JsonValueKind.Number || !el.TryGetInt32(out var v) || v is < 0 or > 255)
            {
                throw new FormatException("palette map values must be integers in 0..255");
            }

            outArr[i++] = v;
        }

        return outArr;
    }
}
