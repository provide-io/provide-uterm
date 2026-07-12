//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Fanout;

/// <summary>
/// Divergence detection for fan-out sessions.
/// Port of packages/provide-uterm-go/fanout/divergence.go (SequenceMatcher.ratio).
/// </summary>
public static class Divergence
{
    public static bool[] ComputeDivergence(IReadOnlyList<string> outputs, double threshold)
    {
        var n = outputs.Count;
        if (n == 0)
        {
            return Array.Empty<bool>();
        }

        if (n == 1)
        {
            return [false];
        }

        var avgSim = new double[n];
        for (var i = 0; i < n; i++)
        {
            double sum = 0;
            for (var j = 0; j < n; j++)
            {
                if (j == i)
                {
                    continue;
                }

                sum += Ratio(outputs[i], outputs[j]);
            }

            avgSim[i] = sum / (n - 1);
        }

        var majorityIdx = 0;
        var best = avgSim[0];
        for (var i = 1; i < n; i++)
        {
            if (avgSim[i] > best)
            {
                best = avgSim[i];
                majorityIdx = i;
            }
        }

        var majority = outputs[majorityIdx];
        var simToMajority = new double[n];
        for (var i = 0; i < n; i++)
        {
            simToMajority[i] = Ratio(outputs[i], majority);
        }

        var hasSupporters = false;
        for (var i = 0; i < n; i++)
        {
            if (i != majorityIdx && simToMajority[i] >= threshold)
            {
                hasSupporters = true;
                break;
            }
        }

        var flags = new bool[n];
        for (var i = 0; i < n; i++)
        {
            flags[i] = i == majorityIdx ? !hasSupporters : simToMajority[i] < threshold;
        }

        return flags;
    }

    private static double Ratio(string aStr, string bStr)
    {
        var a = aStr.ToCharArray();
        var b = bStr.ToCharArray();
        var total = a.Length + b.Length;
        if (total == 0)
        {
            return 1.0;
        }

        var sm = new SeqMatcher(a, b);
        var matches = sm.TotalMatches();
        return 2.0 * matches / total;
    }

    private sealed class SeqMatcher
    {
        private readonly char[] _a;
        private readonly char[] _b;
        private readonly Dictionary<char, List<int>> _b2j = new();
        private readonly HashSet<char> _bjunk = new();
        private readonly HashSet<char> _bpopular = new();

        public SeqMatcher(char[] a, char[] b)
        {
            _a = a;
            _b = b;
            for (var i = 0; i < b.Length; i++)
            {
                if (!_b2j.TryGetValue(b[i], out var list))
                {
                    list = [];
                    _b2j[b[i]] = list;
                }

                list.Add(i);
            }

            var n = b.Length;
            if (n >= 200)
            {
                var ntest = (n / 100) + 1;
                foreach (var (elt, idxs) in _b2j)
                {
                    if (idxs.Count > ntest)
                    {
                        _bpopular.Add(elt);
                    }
                }

                foreach (var elt in _bpopular)
                {
                    _b2j.Remove(elt);
                }
            }
        }

        private (int I, int J, int Size) FindLongestMatch(int alo, int ahi, int blo, int bhi)
        {
            var besti = alo;
            var bestj = blo;
            var bestsize = 0;
            var j2len = new Dictionary<int, int>();
            for (var i = alo; i < ahi; i++)
            {
                var newj2len = new Dictionary<int, int>();
                if (_b2j.TryGetValue(_a[i], out var js))
                {
                    foreach (var j in js)
                    {
                        if (j < blo)
                        {
                            continue;
                        }

                        if (j >= bhi)
                        {
                            break;
                        }

                        j2len.TryGetValue(j - 1, out var prev);
                        var k = prev + 1;
                        newj2len[j] = k;
                        if (k > bestsize)
                        {
                            besti = i - k + 1;
                            bestj = j - k + 1;
                            bestsize = k;
                        }
                    }
                }

                j2len = newj2len;
            }

            while (besti > alo && bestj > blo && !_bjunk.Contains(_b[bestj - 1]) &&
                   _a[besti - 1] == _b[bestj - 1])
            {
                besti--;
                bestj--;
                bestsize++;
            }

            while (besti + bestsize < ahi && bestj + bestsize < bhi &&
                   !_bjunk.Contains(_b[bestj + bestsize]) &&
                   _a[besti + bestsize] == _b[bestj + bestsize])
            {
                bestsize++;
            }

            return (besti, bestj, bestsize);
        }

        public int TotalMatches()
        {
            var queue = new Stack<(int Alo, int Ahi, int Blo, int Bhi)>();
            queue.Push((0, _a.Length, 0, _b.Length));
            var total = 0;
            while (queue.Count > 0)
            {
                var (alo, ahi, blo, bhi) = queue.Pop();
                var m = FindLongestMatch(alo, ahi, blo, bhi);
                if (m.Size > 0)
                {
                    total += m.Size;
                    if (alo < m.I && blo < m.J)
                    {
                        queue.Push((alo, m.I, blo, m.J));
                    }

                    if (m.I + m.Size < ahi && m.J + m.Size < bhi)
                    {
                        queue.Push((m.I + m.Size, ahi, m.J + m.Size, bhi));
                    }
                }
            }

            return total;
        }
    }
}
