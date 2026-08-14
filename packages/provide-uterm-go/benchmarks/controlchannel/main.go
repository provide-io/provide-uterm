// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

type arguments struct {
	frameCount   int
	controlRatio float64
	chunkSize    int
	dataSize     int
	controlSize  int
	seed         int
	passes       int
}

type summary struct {
	Backend        string  `json:"backend"`
	GeneratedBytes int     `json:"generated_bytes"`
	FrameCount     int     `json:"frame_count"`
	ChunkSize      int     `json:"chunk_size"`
	MedianSeconds  float64 `json:"median_seconds"`
	MeanSeconds    float64 `json:"mean_seconds"`
	MinSeconds     float64 `json:"min_seconds"`
	Events         int     `json:"events"`
	MiBPerS        float64 `json:"mib_per_s"`
}

type xorshift32 struct {
	state uint32
}

func (r *xorshift32) nextUint32() uint32 {
	x := r.state
	x ^= x << 13
	x ^= x >> 17
	x ^= x << 5
	r.state = x
	return x
}

func (r *xorshift32) nextFloat64() float64 {
	return float64(r.nextUint32()) / float64(uint64(1)<<32)
}

func parseArguments() arguments {
	args := arguments{
		frameCount:   200000,
		controlRatio: 0.25,
		chunkSize:    4096,
		dataSize:     256,
		controlSize:  128,
		seed:         1337,
		passes:       5,
	}

	flag.IntVar(&args.frameCount, "frame-count", args.frameCount, "Number of frames to synthesize")
	flag.Float64Var(&args.controlRatio, "control-ratio", args.controlRatio, "Ratio of control frames in stream")
	flag.IntVar(&args.chunkSize, "chunk-size", args.chunkSize, "Chunk size in bytes")
	flag.IntVar(&args.dataSize, "data-size", args.dataSize, "Terminal payload size")
	flag.IntVar(&args.controlSize, "control-size", args.controlSize, "Control payload size")
	flag.IntVar(&args.seed, "seed", args.seed, "Deterministic stream seed")
	flag.IntVar(&args.passes, "passes", args.passes, "Benchmark passes per variant")
	flag.Parse()

	if args.frameCount < 1 {
		panic("frame-count must be >= 1")
	}
	if args.passes < 1 {
		panic("passes must be >= 1")
	}
	if args.chunkSize < 1 {
		panic("chunk-size must be >= 1")
	}
	if args.controlRatio <= 0.0 || args.controlRatio >= 1.0 {
		panic("control-ratio must be >0 and <1")
	}
	return args
}

func buildStream(args arguments) string {
	seed := uint32(args.seed)
	state := seed
	if state == 0 {
		state = 0x9e3779b9
	}
	rng := &xorshift32{state: state}

	dataSegment := strings.Repeat("x", maxInt(args.dataSize, 0))
	controlPayload := strings.Repeat("x", maxInt(args.controlSize, 0))
	var builder strings.Builder
	// Pre-size to avoid reallocation; benchmark workload is mostly payload-heavy.
	estimated := maxInt(1, args.frameCount*maxInt(1, minInt(args.dataSize, 128)))
	builder.Grow(estimated)

	for i := 0; i < args.frameCount; i += 1 {
		if rng.nextFloat64() < args.controlRatio {
			payload := map[string]any{
				"type":    "bench",
				"id":      i,
				"seed":    args.seed,
				"payload": controlPayload,
			}
			frame, err := controlchannel.EncodeControlFrame(payload)
			if err != nil {
				panic(err)
			}
			builder.WriteString(frame)
			continue
		}

		segment := dataSegment
		if dataSize := len(dataSegment); dataSize > 0 && rng.nextFloat64() < 0.01 {
			midpoint := minInt(64, dataSize/2)
			escaped := segment[:midpoint] + controlchannel.DLE + "DLE_ESC" + segment[midpoint:]
			if len(escaped) > dataSize {
				segment = escaped[:dataSize]
			} else {
				segment = escaped
			}
		}
		builder.WriteString(controlchannel.EncodeTerminalData(segment))
	}
	return builder.String()
}

func chunkStream(stream string, chunkSize int) []string {
	raw := []byte(stream)
	chunks := make([]string, 0, maxInt(1, len(raw)/chunkSize+1))
	for offset := 0; offset < len(raw); offset += chunkSize {
		end := minInt(offset+chunkSize, len(raw))
		chunks = append(chunks, string(raw[offset:end]))
	}
	return chunks
}

func benchmarkDecode(chunks []string, passes int) (float64, float64, float64, int) {
	samples := make([]float64, passes)
	events := 0

	for pass := 0; pass < passes; pass += 1 {
		decoder := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
		start := time.Now()
		count := 0

		for _, chunk := range chunks {
			eventsOut, err := decoder.Feed(chunk)
			if err != nil {
				panic(err)
			}
			count += len(eventsOut)
		}

		finishEvents, err := decoder.Finish()
		if err != nil {
			panic(err)
		}
		count += len(finishEvents)
		samples[pass] = time.Since(start).Seconds()
		events = count
	}

	sorted := make([]float64, len(samples))
	copy(sorted, samples)
	sort.Float64s(sorted)
	min := sorted[0]
	var total float64
	for _, sample := range samples {
		total += sample
	}
	median := sorted[len(sorted)/2]
	mean := total / float64(len(samples))
	return median, mean, min, events
}

func printUsage() {
	fmt.Println("Usage: go run ./benchmarks/controlchannel -- --frame-count 200000 --control-ratio 0.25 --chunk-size 4096 --data-size 256 --control-size 128 --passes 5")
	fmt.Println("  --frame-count N      Number of frames to generate")
	fmt.Println("  --control-ratio F    Control frame ratio in stream (0 < F < 1)")
	fmt.Println("  --chunk-size N       Chunk size in bytes")
	fmt.Println("  --data-size N        Terminal payload size")
	fmt.Println("  --control-size N     Control payload size")
	fmt.Println("  --seed N             PRNG seed")
	fmt.Println("  --passes N           Repeat benchmark passes")
}

func main() {
	args := parseArguments()
	stream := buildStream(args)
	chunks := chunkStream(stream, args.chunkSize)

	medianSeconds, meanSeconds, minSeconds, events := benchmarkDecode(chunks, args.passes)
	payloadMiB := float64(len(stream)) / (1024 * 1024)
	mibPerS := payloadMiB / medianSeconds

	if math.IsNaN(payloadMiB) || math.IsNaN(medianSeconds) {
		panic("invalid benchmark values")
	}

	fmt.Printf("Generated stream: %d bytes, %d frames, chunk size %d\n", len(stream), args.frameCount, args.chunkSize)
	fmt.Printf("Go: %0.4fs median, %0.4f min, %0.4f mean, %0.2f MiB/s\n", medianSeconds, minSeconds, meanSeconds, mibPerS)
	fmt.Printf("Events emitted: %d\n", events)

	summaryData := summary{
		Backend:        "go",
		GeneratedBytes: len(stream),
		FrameCount:     args.frameCount,
		ChunkSize:      args.chunkSize,
		MedianSeconds:  medianSeconds,
		MeanSeconds:    meanSeconds,
		MinSeconds:     minSeconds,
		Events:         events,
		MiBPerS:        mibPerS,
	}
	encoded, err := json.Marshal(summaryData)
	if err != nil {
		panic(err)
	}
	fmt.Println(string(encoded))
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}
