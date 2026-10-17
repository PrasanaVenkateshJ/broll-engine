Read from the system on 2026-10-17 (PowerShell `Get-CimInstance Win32_Processor / Win32_ComputerSystem /
Win32_OperatingSystem / Win32_VideoController`, `Get-PhysicalDisk`); every test pack run used this PC.

| Part | Value |
|---|---|
| Laptop | Lenovo 83EM |
| CPU | 13th Gen Intel Core i7-13620H: 10 cores (6 performance + 4 efficient), 16 threads, base 2.4 GHz |
| RAM | 15.7 GiB (16,857,817,088 bytes) |
| GPU | Intel UHD Graphics (integrated); **not used** - every model runs on the CPU (torch 2.14.1+cpu, 10 threads) |
| Disk | Micron 512 GB NVMe SSD |
| OS | Windows 11 Home 10.0.26300, Python 3.11.9 (.venv), FFmpeg 9.0.2 (gyan.dev full build) |
| Models | Kokoro-82M (TTS), faster-whisper `small` int8 (CPU), open_clip ViT-B-32 laion2b_s34b_b79k (CPU) |
| Network | home internet (Groq, Pixabay and Wikimedia are called over it; their speed is part of every turnaround) |

The API, the worker, the memory sampler and the batch client all ran on this one machine. Other programs
(VS Code, the browser) were open as on a normal working day.
