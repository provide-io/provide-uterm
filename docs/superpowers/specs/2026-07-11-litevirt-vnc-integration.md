# Litevirt VNC Integration Design

## 1. Overview
This document outlines the architecture for connecting `litevirt`'s `ProxyVNC` gRPC stream to `uterm`'s `gui.GraphicalSession`. It expands upon the Graphical Session Runtime design by specifying exactly how `uterm` will act as a middleman between QEMU/litevirt, human viewers (noVNC), and AI agents.

## 2. The Dual-Stream Architecture
To provide humans with high-performance compressed video while giving AI simple, uncompressed pixels for the `FramebufferTracker`, `uterm` will utilize a **Dual-Stream Sidecar** approach. For every active `uterm` session, it will establish **two** separate gRPC `ProxyVNC` connections to `litevirt`.

### 2.1 Stream A: The Human Relay (WebSockets -> gRPC)
- **Role:** Transparently bridges noVNC in the human browser to QEMU.
- **Video (Server-to-Client):** `uterm` acts as a dumb pipe for Server-to-Client messages. This allows noVNC to negotiate complex, high-efficiency encodings (like Tight or ZRLE) directly with QEMU, minimizing bandwidth usage on the human's local network.
- **Input (Client-to-Server):** `uterm` actively parses the simple, fixed-size Client-to-Server RFB packets (PointerEvent, KeyEvent).
  - It checks the `HijackLeaseManager`.
  - If the human connection holds the lease, the packet is forwarded to QEMU over gRPC.
  - If the human connection does *not* hold the lease, the packet is dropped, preventing unauthorized input.

### 2.2 Stream B: The AI Headless Client (gRPC -> FramebufferTracker)
- **Role:** Feeds the `FramebufferTracker` to allow the AI to "see" the screen via MCP tools (`gui_screenshot`).
- **Video (Server-to-Client):** `uterm` implements a minimal headless RFB client. During the handshake with QEMU, it explicitly advertises that it **only** supports the `Raw` and `CopyRect` encodings. QEMU is forced to stream uncompressed pixels. These pixels are fed directly into the `FramebufferTracker`.
- **Input (Client-to-Server):** When the AI invokes an MCP tool (e.g., `gui_click`), `uterm` generates the raw RFB PointerEvent/KeyEvent packet and sends it up this gRPC stream.

## 3. Component Interactions

```mermaid
flowchart TD
    subgraph litevirt
        Q[QEMU VNC] <--> P[litevirt ProxyVNC gRPC]
    end

    subgraph uterm [uterm Go Server]
        P <-->|Stream A (Compressed)| HR[Human Relay]
        P <-->|Stream B (Raw)| HC[Headless AI Client]

        HR --> |Parse Input| HL[HijackLeaseManager]
        HL --> |Gated| HR

        HC --> |Raw Pixels| FT[FramebufferTracker]
    end

    subgraph Browser
        HR <--> |WebSocket| NV[noVNC]
    end

    subgraph AI
        FT --> |gui_screenshot| Agent[AI Agent]
        Agent --> |gui_click / type| HL
        HL --> |Gated| HC
    end
```

## 4. Implementation Details
- **gRPC Connection:** `uterm` will use a standard gRPC client to connect to `litevirt`, passing the VM name via the `x-vm-name` metadata header as required by the `ProxyVNC` RPC.
- **RFB Parsing:**
  - The Human Relay only needs a parser for Client-to-Server messages (which are standard and simple).
  - The Headless AI Client needs a minimal parser for the RFB Handshake, Initialization, and Server-to-Client `FramebufferUpdate` messages (supporting only `Raw` encoding).
- **Concurrency:** The two streams operate independently. QEMU natively supports multiple concurrent VNC connections and handles the multiplexing of input events from both streams.
