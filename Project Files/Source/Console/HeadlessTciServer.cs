//=================================================================
// HeadlessTciServer.cs
//=================================================================
// Multi-Port TCI Server and Manager for Headless Slices (Release F).
// Provides TCI listeners on Ports 50002..50007 for digital apps
// (WSJT-X, JTDX, etc.) mapping to DDCs 2 through 7 (RX3 through RX8).
// Each pair exposes 2 TRX (TRX 0 and TRX 1):
//   - Ports 50002 & 50003: TRX 0 -> RX3 (DDC 2), TRX 1 -> RX4 (DDC 3)
//   - Ports 50004 & 50005: TRX 0 -> RX5 (DDC 4), TRX 1 -> RX6 (DDC 5)
//   - Ports 50006 & 50007: TRX 0 -> RX7 (DDC 6), TRX 1 -> RX8 (DDC 7)
//=================================================================

using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.RegularExpressions;
using System.Security.Cryptography;
using System.Threading;
using System.Collections.Generic;

namespace Thetis
{
    public sealed class HeadlessTciManager : ITciTxAudioSource
    {
        public static HeadlessTciManager Instance { get; } = new HeadlessTciManager();

        private readonly List<HeadlessTciServer> _servers = new List<HeadlessTciServer>();
        private bool _isRunning = false;
        private readonly object _lock = new object();

        private HeadlessTciManager() { }

        public bool IsRunning => _isRunning;
        public Console Console { get; private set; }

        public TCITxStereoInputMode TXStereoInputMode => TCITxStereoInputMode.Both;

        public bool UsesActiveTCITxAudio()
        {
            if (!_isRunning) return false;
            int activeRx = TxArbiter.Instance.ActiveDigitalRx;
            if (activeRx == -1) return false;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    if (_servers[i].BaseRxIndex == activeRx)
                    {
                        return _servers[i].UsesActiveTCITxAudio();
                    }
                }
            }
            return false;
        }

        public bool TryGetTxAudioRequestSettings(out int sampleRate, out int samples, out int bufferingMs)
        {
            sampleRate = 48000;
            samples = 2048;
            bufferingMs = 100;
            if (!_isRunning) return false;

            int activeRx = TxArbiter.Instance.ActiveDigitalRx;
            if (activeRx == -1) return false;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    if (_servers[i].BaseRxIndex == activeRx)
                    {
                        return _servers[i].TryGetTxAudioRequestSettings(out sampleRate, out samples, out bufferingMs);
                    }
                }
            }
            return false;
        }

        public void SendTxChrono(int receiver)
        {
            if (!_isRunning) return;
            int activeRx = TxArbiter.Instance.ActiveDigitalRx;
            if (activeRx == -1) return;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    if (_servers[i].BaseRxIndex == activeRx)
                    {
                        _servers[i].SendTxChrono(receiver);
                        return;
                    }
                }
            }
        }

        public bool TryDequeueTxAudio(out TCIQueuedTxAudio queuedAudio)
        {
            queuedAudio = null;
            if (!_isRunning) return false;

            int activeRx = TxArbiter.Instance.ActiveDigitalRx;
            if (activeRx == -1) return false;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    if (_servers[i].BaseRxIndex == activeRx)
                    {
                        return _servers[i].TryDequeueTxAudio(out queuedAudio);
                    }
                }
            }
            return false;
        }

        public void StartAll(IPAddress bindAddress, Console console)
        {
            lock (_lock)
            {
                if (_isRunning) StopAll();

                Console = console;
                TxArbiter.Instance.Initialize(console);
                TxArbiter.Instance.DigitalSlicePreempted += OnSlicePreempted;

                HeadlessSliceManager.Instance.SliceFrequencyChanged += OnSliceFreqChanged;
                HeadlessSliceManager.Instance.SliceModeChanged += OnSliceModeChanged;
                HeadlessSliceManager.Instance.SliceFilterChanged += OnSliceFilterChanged;

                // Bind ports 50002..50008 matching RX2..RX8 directly (Port = 50000 + RX#)
                // 50002 -> RX2 (DDC 1)
                // 50003 -> RX3 (DDC 2)
                // 50004 -> RX4 (DDC 3)
                // 50005 -> RX5 (DDC 4)
                // 50006 -> RX6 (DDC 5)
                // 50007 -> RX7 (DDC 6)
                // 50008 -> RX8 (DDC 7)
                int[] ports = new int[] { 50002, 50003, 50004, 50005, 50006, 50007, 50008 };
                int[] baseRxs = new int[] { 1, 2, 3, 4, 5, 6, 7 };

                for (int i = 0; i < ports.Length; i++)
                {
                    var s = new HeadlessTciServer(bindAddress, ports[i], baseRxs[i]);
                    _servers.Add(s);
                    s.Start();
                }

                cmaster.HeadlessAudioPublisher = PublishRxAudio;
                // Branch G: wire IQ streaming for RX3-RX8
                cmaster.HeadlessIQPublisher = PublishIQ;
                cmaster.HeadlessIQWantsIQ = IsAnyClientStreamingIQ;
                _isRunning = true;
            }
        }

        public void StopAll()
        {
            lock (_lock)
            {
                if (!_isRunning) return;

                cmaster.HeadlessAudioPublisher = null;
                cmaster.HeadlessIQPublisher = null;
                cmaster.HeadlessIQWantsIQ = null;

                foreach (var s in _servers)
                {
                    try { s.Stop(); } catch { }
                }
                _servers.Clear();

                TxArbiter.Instance.DigitalSlicePreempted -= OnSlicePreempted;
                HeadlessSliceManager.Instance.SliceFrequencyChanged -= OnSliceFreqChanged;
                HeadlessSliceManager.Instance.SliceModeChanged -= OnSliceModeChanged;
                HeadlessSliceManager.Instance.SliceFilterChanged -= OnSliceFilterChanged;

                HeadlessSliceManager.Instance.DeactivateAll();
                TxArbiter.Instance.Shutdown();

                Console = null;
                _isRunning = false;
            }
        }

        public void PublishRxAudio(int rx, int sampleRate, float[] left, float[] right, int nsamples)
        {
            if (!_isRunning) return;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.PublishAudio(0, sampleRate, left, right, nsamples);
                    }
                }
            }
        }

        // Branch G: publish an IQ block from the native DSP pipeline to the headless
        // server that owns 'rx'. Called from cmaster.OnTCIRxIQOutSamples.
        public void PublishIQ(int rx, int sampleRate, float[] interleavedIQ, int complexSamples)
        {
            if (!_isRunning || interleavedIQ == null) return;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.PublishIQ(0, sampleRate, interleavedIQ, complexSamples);
                    }
                }
            }
        }

        // Branch G: does any client on 'rx' want an IQ stream? Used by cmaster as a
        // cheap gate before it copies/forwards IQ samples.
        public bool IsAnyClientStreamingIQ(int rx)
        {
            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        if (s.IsTrxStreamingIQ(0)) return true;
                    }
                }
            }
            return false;
        }

        public bool IsAnyClientStreaming(int rx)
        {
            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        if (s.IsTrxStreaming(0)) return true;
                    }
                }
            }
            return false;
        }

        private void OnSlicePreempted(int rx)
        {
            if (!_isRunning) return;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.OnSlicePreempted();
                        s.BroadcastTrxState(0, false);
                    }
                }
            }
        }

        private void OnSliceFreqChanged(int rx, double freqMHz)
        {
            if (!_isRunning) return;

            long freqHz = (long)(freqMHz * 1e6);
            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.BroadcastVfo(0, freqHz);
                    }
                }
            }
        }

        private void OnSliceModeChanged(int rx, DSPMode mode)
        {
            if (!_isRunning) return;

            string modeStr = ModeToString(mode);
            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.BroadcastMode(0, modeStr);
                    }
                }
            }
        }

        private void OnSliceFilterChanged(int rx, int lowHz, int highHz)
        {
            if (!_isRunning) return;

            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx == s.BaseRxIndex)
                    {
                        s.BroadcastFilter(0, lowHz, highHz);
                    }
                }
            }
        }

        public static string ModeToString(DSPMode mode)
        {
            switch (mode)
            {
                case DSPMode.DIGU: return "DIGU";
                case DSPMode.DIGL: return "DIGL";
                case DSPMode.USB: return "USB";
                case DSPMode.LSB: return "LSB";
                case DSPMode.CWU: return "CWU";
                case DSPMode.CWL: return "CWL";
                case DSPMode.AM: return "AM";
                case DSPMode.SAM: return "SAM";
                case DSPMode.FM: return "NFM";
                default: return "USB";
            }
        }

        public static DSPMode ParseDSPMode(string str)
        {
            if (string.IsNullOrWhiteSpace(str)) return DSPMode.DIGU;
            switch (str.Trim().ToUpperInvariant())
            {
                case "DIGU": return DSPMode.DIGU;
                case "DIGL": return DSPMode.DIGL;
                case "USB": return DSPMode.USB;
                case "LSB": return DSPMode.LSB;
                case "CWU": return DSPMode.CWU;
                case "CWL": return DSPMode.CWL;
                case "AM": return DSPMode.AM;
                case "SAM": return DSPMode.SAM;
                case "NFM":
                case "FM": return DSPMode.FM;
                default: return DSPMode.DIGU;
            }
        }

        // Branch G: map TCI agc_mode string to WDSP AGCMode
        public static AGCMode AGCModeFromTciString(string s)
        {
            if (string.IsNullOrWhiteSpace(s)) return AGCMode.MED;
            switch (s.Trim().ToLowerInvariant())
            {
                case "off":
                case "fixd":
                case "fixed": return AGCMode.FIXD;
                case "long": return AGCMode.LONG;
                case "slow": return AGCMode.SLOW;
                case "fast": return AGCMode.FAST;
                case "custom": return AGCMode.CUSTOM;
                case "normal":
                case "med":
                case "medium":
                default: return AGCMode.MED;
            }
        }
    }

    public sealed class HeadlessTciServer
    {
        public int Port { get; }
        public int BaseRxIndex { get; } // 2 for 50002/50003, 4 for 50004/50005, 6 for 50006/50007

        private readonly IPAddress _address;
        private TcpListener _listener;
        private Thread _listenerThread;
        private volatile bool _stopServer = false;
        private readonly List<HeadlessTciClientHandler> _clients = new List<HeadlessTciClientHandler>();
        private readonly object _clientsLock = new object();

        public HeadlessTciServer(IPAddress address, int port, int baseRxIndex)
        {
            _address = address;
            Port = port;
            BaseRxIndex = baseRxIndex;
        }

        public void Start()
        {
            _stopServer = false;
            try
            {
                _listener = new TcpListener(_address, Port);
                _listener.Start();
                _listenerThread = new Thread(ListenLoop)
                {
                    IsBackground = true,
                    Name = "HeadlessTciServer_" + Port
                };
                _listenerThread.Start();
            }
            catch (Exception ex)
            {
                System.Diagnostics.Debug.WriteLine($"[HeadlessTciServer] Failed to start on port {Port}: {ex.Message}");
            }
        }

        public void Stop()
        {
            _stopServer = true;
            try { _listener?.Stop(); } catch { }

            lock (_clientsLock)
            {
                foreach (var client in _clients)
                {
                    client.Close();
                }
                _clients.Clear();
            }
        }

        private void ListenLoop()
        {
            while (!_stopServer)
            {
                try
                {
                    TcpClient client = _listener.AcceptTcpClient();
                    client.NoDelay = true;
                    client.SendBufferSize = 131072;
                    client.ReceiveBufferSize = 65536;
                    var handler = new HeadlessTciClientHandler(this, client);
                    lock (_clientsLock)
                    {
                        _clients.Add(handler);
                    }
                    handler.Start();
                }
                catch
                {
                    if (_stopServer) break;
                }
            }
        }

        public bool IsTrxStreaming(int trx)
        {
            if (trx < 0 || trx > 1) return false;
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].WantsAudio(trx)) return true;
                }
            }
            return false;
        }

        // Branch G: does any connected client want an IQ stream on this TRX?
        public bool IsTrxStreamingIQ(int trx)
        {
            if (trx < 0 || trx > 1) return false;
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].WantsIQ(trx)) return true;
                }
            }
            return false;
        }

        public void RemoveClient(HeadlessTciClientHandler client)
        {
            lock (_clientsLock)
            {
                _clients.Remove(client);
            }

            int rx = BaseRxIndex;
            if (!HeadlessTciManager.Instance.IsAnyClientStreaming(rx))
            {
                HeadlessSliceManager.Instance.DeactivateAudio(rx);
            }
        }

        public void BroadcastText(string text)
        {
            lock (_clientsLock)
            {
                foreach (var c in _clients)
                {
                    c.SendTextFrame(text);
                }
            }
        }

        public void BroadcastVfo(int trx, long freqHz)
        {
            BroadcastText($"vfo:0,0,{freqHz};");
            BroadcastText($"vfo:0,1,{freqHz};");
        }

        public void BroadcastMode(int trx, string modeStr)
        {
            BroadcastText($"modulation:{trx},{modeStr};");
        }

        public void BroadcastFilter(int trx, int lowHz, int highHz)
        {
            BroadcastText($"rx_filter_band:{trx},{lowHz},{highHz};");
        }

        public void BroadcastTrxState(int trx, bool isTx)
        {
            BroadcastText($"trx:{trx},{isTx.ToString().ToLowerInvariant()};");
        }

        public void OnSlicePreempted()
        {
            lock (_clientsLock)
            {
                foreach (var c in _clients)
                {
                    c.OnPreempted();
                }
            }
        }

        public bool UsesActiveTCITxAudio()
        {
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].IsTransmitting) return true;
                }
            }
            return false;
        }

        public bool TryGetTxAudioRequestSettings(out int sampleRate, out int samples, out int bufferingMs)
        {
            sampleRate = 48000;
            samples = 2048;
            bufferingMs = 100;
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].IsTransmitting)
                    {
                        sampleRate = _clients[i].AudioSampleRate;
                        samples = _clients[i].AudioStreamSamples;
                        bufferingMs = _clients[i].TxStreamAudioBufferingMs;
                        return true;
                    }
                }
            }
            return false;
        }

        public void SendTxChrono(int receiver)
        {
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].IsTransmitting)
                    {
                        _clients[i].SendTxChrono(receiver);
                    }
                }
            }
        }

        public bool TryDequeueTxAudio(out TCIQueuedTxAudio queuedAudio)
        {
            queuedAudio = null;
            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    if (_clients[i].IsTransmitting && _clients[i].TryDequeueTxAudio(out queuedAudio))
                    {
                        return true;
                    }
                }
            }
            return false;
        }

        public void PublishAudio(int trx, int sampleRate, float[] left, float[] right, int nsamples)
        {
            if (trx < 0 || trx > 1 || left == null || right == null || nsamples <= 0) return;

            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    var c = _clients[i];
                    if (c.WantsAudio(trx))
                    {
                        c.PublishAudio(trx, sampleRate, left, right, nsamples);
                    }
                }
            }
        }

// Branch G: publish an IQ block to all clients on this server that requested IQ.
        public void PublishIQ(int trx, int sampleRate, float[] interleavedIQ, int complexSamples)
        {
            if (trx < 0 || trx > 1 || interleavedIQ == null || complexSamples <= 0) return;

            lock (_clientsLock)
            {
                for (int i = 0; i < _clients.Count; i++)
                {
                    var c = _clients[i];
                    if (c.WantsIQ(trx))
                    {
                        c.PublishIQ(trx, sampleRate, interleavedIQ, complexSamples);
                    }
                }
            }
        }

        
        internal static byte[] EncodeAudioSamples(float[] samples, TCISampleType sampleType)
        {
            if (samples == null || samples.Length == 0) return Array.Empty<byte>();

            if (sampleType == TCISampleType.FLOAT32)
            {
                byte[] data = new byte[samples.Length * 4];
                Buffer.BlockCopy(samples, 0, data, 0, data.Length);
                return data;
            }

            if (sampleType == TCISampleType.INT16)
            {
                byte[] data = new byte[samples.Length * 2];
                int offset = 0;
                for (int i = 0; i < samples.Length; i++)
                {
                    float clipped = Math.Max(-1.0f, Math.Min(1.0f, samples[i]));
                    short s16 = (short)Math.Round(clipped * 32767.0f);
                    data[offset++] = (byte)(s16 & 0xFF);
                    data[offset++] = (byte)((s16 >> 8) & 0xFF);
                }
                return data;
            }

            if (sampleType == TCISampleType.INT24)
            {
                byte[] data = new byte[samples.Length * 3];
                int offset = 0;
                for (int i = 0; i < samples.Length; i++)
                {
                    float clipped = Math.Max(-1.0f, Math.Min(1.0f, samples[i]));
                    int s24 = (int)Math.Round(clipped * 8388607.0f);
                    data[offset++] = (byte)(s24 & 0xFF);
                    data[offset++] = (byte)((s24 >> 8) & 0xFF);
                    data[offset++] = (byte)((s24 >> 16) & 0xFF);
                }
                return data;
            }

            if (sampleType == TCISampleType.INT32)
            {
                byte[] data = new byte[samples.Length * 4];
                int offset = 0;
                for (int i = 0; i < samples.Length; i++)
                {
                    float clipped = Math.Max(-1.0f, Math.Min(1.0f, samples[i]));
                    int s32 = (int)Math.Round(clipped * 2147483647.0f);
                    data[offset++] = (byte)(s32 & 0xFF);
                    data[offset++] = (byte)((s32 >> 8) & 0xFF);
                    data[offset++] = (byte)((s32 >> 16) & 0xFF);
                    data[offset++] = (byte)((s32 >> 24) & 0xFF);
                }
                return data;
            }

            byte[] fallback = new byte[samples.Length * 4];
            Buffer.BlockCopy(samples, 0, fallback, 0, fallback.Length);
            return fallback;
        }

        internal static byte[] BuildAudioPayload(int trx, int sampleRate, TCISampleType sampleType, int length, int channels, byte[] samplePayload)
        {
            int payloadLen = samplePayload != null ? samplePayload.Length : 0;
            byte[] packet = new byte[64 + payloadLen];

            WriteUInt32(packet, 0, (uint)trx);
            WriteUInt32(packet, 4, (uint)sampleRate);
            WriteUInt32(packet, 8, (uint)sampleType);
            WriteUInt32(packet, 12, 0);
            WriteUInt32(packet, 16, 0);
            WriteUInt32(packet, 20, (uint)length);
            WriteUInt32(packet, 24, 1); // RX_AUDIO_STREAM = 1
            WriteUInt32(packet, 28, (uint)channels);

            if (payloadLen > 0)
            {
                Buffer.BlockCopy(samplePayload, 0, packet, 64, payloadLen);
            }

            return packet;
        }

        // Branch G: build a 64-byte-header IQ_STREAM binary payload (frame type 0).
        public static byte[] BuildIQPayload(int trx, int sampleRate, int complexSamples, byte[] samplePayload)
        {
            int payloadLen = samplePayload != null ? samplePayload.Length : 0;
            byte[] packet = new byte[64 + payloadLen];

            WriteUInt32(packet, 0, (uint)trx);            // receiver
            WriteUInt32(packet, 4, (uint)sampleRate);      // sample rate
            WriteUInt32(packet, 8, (uint)TCISampleType.FLOAT32); // sample type
            WriteUInt32(packet, 12, 0);
            WriteUInt32(packet, 16, 0);
            WriteUInt32(packet, 20, (uint)(complexSamples * 2)); // length (interleaved values)
            WriteUInt32(packet, 24, (uint)TCIStreamType.IQ_STREAM); // frame type 0
            WriteUInt32(packet, 28, 2);                    // channels (I/Q)

            if (payloadLen > 0)
            {
                Buffer.BlockCopy(samplePayload, 0, packet, 64, payloadLen);
            }

            return packet;
        }

        public static void WriteUInt32(byte[] buffer, int offset, uint value)
        {
            buffer[offset] = (byte)(value & 0xFF);
            buffer[offset + 1] = (byte)((value >> 8) & 0xFF);
            buffer[offset + 2] = (byte)((value >> 16) & 0xFF);
            buffer[offset + 3] = (byte)((value >> 24) & 0xFF);
        }

        public static byte[] MakeWebSocketBinaryFrame(byte[] payload)
        {
            int length = payload != null ? payload.Length : 0;
            int headerLen = 2;
            if (length > 125 && length <= 65535) headerLen = 4;
            else if (length > 65535) headerLen = 10;

            byte[] frame = new byte[headerLen + length];
            frame[0] = 0x82; // Binary, FIN set

            if (length <= 125)
            {
                frame[1] = (byte)length;
            }
            else if (length <= 65535)
            {
                frame[1] = 126;
                frame[2] = (byte)((length >> 8) & 0xFF);
                frame[3] = (byte)(length & 0xFF);
            }
            else
            {
                frame[1] = 127;
                for (int i = 0; i < 8; i++)
                {
                    frame[2 + i] = (byte)((length >> (56 - 8 * i)) & 0xFF);
                }
            }

            if (length > 0)
            {
                Buffer.BlockCopy(payload, 0, frame, headerLen, length);
            }

            return frame;
        }

        public static byte[] MakeWebSocketTextFrame(string message)
        {
            byte[] payload = Encoding.UTF8.GetBytes(message);
            int length = payload.Length;
            int headerLen = 2;
            if (length > 125 && length <= 65535) headerLen = 4;
            else if (length > 65535) headerLen = 10;

            byte[] frame = new byte[headerLen + length];
            frame[0] = 0x81; // Text, FIN set

            if (length <= 125)
            {
                frame[1] = (byte)length;
            }
            else if (length <= 65535)
            {
                frame[1] = 126;
                frame[2] = (byte)((length >> 8) & 0xFF);
                frame[3] = (byte)(length & 0xFF);
            }
            else
            {
                frame[1] = 127;
                for (int i = 0; i < 8; i++)
                {
                    frame[2 + i] = (byte)((length >> (56 - 8 * i)) & 0xFF);
                }
            }

            Buffer.BlockCopy(payload, 0, frame, headerLen, length);
            return frame;
        }

        public static byte[] BuildChronoPayload(int receiver, int sampleRate, TCISampleType sampleType, int length, int channels)
        {
            byte[] packet = new byte[64];
            WriteUInt32(packet, 0, (uint)receiver);
            WriteUInt32(packet, 4, (uint)sampleRate);
            WriteUInt32(packet, 8, (uint)sampleType);
            WriteUInt32(packet, 12, 0);
            WriteUInt32(packet, 16, 0);
            WriteUInt32(packet, 20, (uint)length);
            WriteUInt32(packet, 24, (uint)TCIStreamType.TX_CHRONO);
            WriteUInt32(packet, 28, (uint)channels);
            return packet;
        }

        public static int GetBytesPerSample(TCISampleType sampleType)
        {
            switch (sampleType)
            {
                case TCISampleType.INT16: return 2;
                case TCISampleType.INT24: return 3;
                case TCISampleType.INT32:
                case TCISampleType.FLOAT32:
                default:
                    return 4;
            }
        }

        public static float[] DecodeSamples(byte[] payload, int offset, int count, TCISampleType sampleType)
        {
            float[] samples = new float[count];
            for (int i = 0; i < count; i++)
            {
                switch (sampleType)
                {
                    case TCISampleType.INT16:
                        short s16 = BitConverter.ToInt16(payload, offset);
                        samples[i] = s16 / 32768.0f;
                        offset += 2;
                        break;
                    case TCISampleType.INT24:
                        int s24 = (payload[offset] | (payload[offset + 1] << 8) | (payload[offset + 2] << 16));
                        if ((s24 & 0x800000) != 0) s24 |= unchecked((int)0xFF000000);
                        samples[i] = s24 / 8388608.0f;
                        offset += 3;
                        break;
                    case TCISampleType.INT32:
                        samples[i] = BitConverter.ToInt32(payload, offset) / 2147483648.0f;
                        offset += 4;
                        break;
                    case TCISampleType.FLOAT32:
                    default:
                        samples[i] = BitConverter.ToSingle(payload, offset);
                        offset += 4;
                        break;
                }
            }
            return samples;
        }

        public static double[] ConvertStreamSamplesToComplex(float[] samples, int channels)
        {
            if (channels < 1) channels = 1;
            int complexSamples = channels <= 1 ? samples.Length : samples.Length / channels;
            double[] complex = new double[complexSamples * 2];
            if (channels == 1)
            {
                for (int i = 0; i < complexSamples; i++)
                {
                    double val = samples[i];
                    complex[2 * i] = val;
                    complex[2 * i + 1] = val;
                }
            }
            else
            {
                for (int i = 0, j = 0; i < complexSamples; i++, j += channels)
                {
                    complex[2 * i] = samples[j];
                    complex[2 * i + 1] = samples[j + 1];
                }
            }
            return complex;
        }
    }

    public sealed class HeadlessTciClientHandler
    {
        private readonly HeadlessTciServer _server;
        private readonly TcpClient _client;
        private NetworkStream _stream;
        private Thread _thread;
        private Thread _sendThread;
        private volatile bool _stop = false;
        private bool _handshakeDone = false;
        private readonly bool[] _wantsAudio = new bool[2];
        // Branch G: per-client IQ stream request state
        private readonly bool[] _wantsIQ = new bool[2];
        // Branch G: IQ rate cap for headless ports (configurable via iq_samplerate cmd).
        // 96000 keeps bandwidth at ~0.77 MB/s per streaming receiver.
        public int IQSampleRate { get; private set; } = 96000;
        // Branch G: S-meter/sensors state
        private bool _rxSensorsEnabled = false;
        private int _rxSensorsIntervalMs = 500;
        private double _sMeterAccumSquared = 0.0;
        private int _sMeterSampleCount = 0;
        private long _lastSensorSendTicks = 0;

        // Dedicated outbound frame queue and background sender thread.
        // Guarantees network TCP writes NEVER block the real-time DSP audio callback thread.
        private readonly Queue<byte[]> _outboundFrames = new Queue<byte[]>();
        private readonly object _outboundLock = new object();
        private readonly AutoResetEvent _outboundEvent = new AutoResetEvent(false);
        private readonly object _sendLock = new object();

        public int AudioStreamSamples { get; private set; } = 2048;
        public int AudioStreamChannels { get; private set; } = 2;
        internal TCISampleType AudioSampleType { get; private set; } = TCISampleType.FLOAT32;
        public int AudioSampleRate { get; private set; } = 48000;
        public long AudioPacketsSent { get; private set; } = 0;

        private volatile bool _isTransmitting = false;
        // Branch G: per-client mute
        private volatile bool _clientMuted = false;
        public bool IsTransmitting => _isTransmitting;
        public int TxStreamAudioBufferingMs { get; private set; } = 100;
        private bool _seenModernTxAudioNegotiation = false;

        private const int MAX_TX_AUDIO_QUEUE_BLOCKS = 32;
        private const int MAX_TX_AUDIO_QUEUE_COMPLEX_SAMPLES = 65536;
        private readonly Queue<TCIQueuedTxAudio> _txAudioQueue = new Queue<TCIQueuedTxAudio>();
        private readonly object _txQueueLock = new object();
        private int _txQueuedComplexSamples = 0;

        // Circular ring buffer for audio accumulation (zero heap allocations & zero array-copy shifts during streaming)
        private const int RING_BUFFER_SIZE = 32768;
        private readonly float[] _ringLeft = new float[RING_BUFFER_SIZE];
        private readonly float[] _ringRight = new float[RING_BUFFER_SIZE];
        private int _audioBufRead = 0;
        private int _audioBufWrite = 0;
        private int _audioBufCount = 0;
        private readonly object _audioLock = new object();

        public HeadlessTciClientHandler(HeadlessTciServer server, TcpClient client)
        {
            _server = server;
            _client = client;
        }

        public bool WantsAudio(int trx)
        {
            if (trx < 0 || trx > 1) return false;
            return _wantsAudio[trx];
        }

        // Branch G
        public bool WantsIQ(int trx)
        {
            if (trx < 0 || trx > 1) return false;
            return _wantsIQ[trx];
        }

        public void OnPreempted()
        {
            _isTransmitting = false;
            ClearQueuedTxAudio();
        }

        public void ClearQueuedTxAudio()
        {
            lock (_txQueueLock)
            {
                _txAudioQueue.Clear();
                _txQueuedComplexSamples = 0;
            }
        }

        // Branch G: send rx_sensors frame computed from accumulated audio RMS.
        // Level is reported in dBFS relative to full scale (0 dBFS = clipping),
        // matching the convention where stronger signal = higher (less negative) value.
        private void SendSMeterFrame()
        {
            double rms;
            int n;
            lock (_audioLock)
            {
                rms = _sMeterAccumSquared;
                n = _sMeterSampleCount;
                _sMeterAccumSquared = 0.0;
                _sMeterSampleCount = 0;
            }

            if (n <= 0) return;
            double meanSq = rms / n;
            double rmsLevel = Math.Sqrt(meanSq);
            double dbfs = 20.0 * Math.Log10(rmsLevel);
            if (double.IsNaN(dbfs) || double.IsInfinity(dbfs)) dbfs = -160.0;
            if (dbfs < -160.0) dbfs = -160.0;
            if (dbfs > 0.0) dbfs = 0.0;

            SendTextFrame(string.Format(System.Globalization.CultureInfo.InvariantCulture, "rx_sensors:0,{0:F1};", dbfs));
            SendTextFrame(string.Format(System.Globalization.CultureInfo.InvariantCulture, "rx_channel_sensors:0,0,{0:F1},{1:F1},{1:F1};", dbfs, dbfs));
        }

        public void SendTxChrono(int receiver)
        {
            if (_stop || !_handshakeDone) return;
            int sampleRate = AudioSampleRate;
            int samples = AudioStreamSamples;
            int channels = AudioStreamChannels;
            TCISampleType sampleType = AudioSampleType;
            int requestLength = _seenModernTxAudioNegotiation ? samples * Math.Max(1, channels) : samples;
            byte[] payload = HeadlessTciServer.BuildChronoPayload(receiver, sampleRate, sampleType, requestLength, channels);
            byte[] wsFrame = HeadlessTciServer.MakeWebSocketBinaryFrame(payload);
            SendRawBytes(wsFrame);
        }

        public bool TryDequeueTxAudio(out TCIQueuedTxAudio queuedAudio)
        {
            lock (_txQueueLock)
            {
                if (_txAudioQueue.Count > 0)
                {
                    queuedAudio = _txAudioQueue.Dequeue();
                    if (queuedAudio != null)
                    {
                        _txQueuedComplexSamples = Math.Max(0, _txQueuedComplexSamples - Math.Max(0, queuedAudio.ComplexSamples));
                    }
                    return true;
                }
            }
            queuedAudio = null;
            return false;
        }

        public void Start()
        {
            _stream = _client.GetStream();
            _thread = new Thread(ClientLoop)
            {
                IsBackground = true,
                Name = $"HeadlessTciClient_{_server.Port}"
            };
            _thread.Start();

            _sendThread = new Thread(SendLoop)
            {
                IsBackground = true,
                Name = $"HeadlessTciSend_{_server.Port}",
                Priority = ThreadPriority.AboveNormal
            };
            _sendThread.Start();
        }

        public void Close()
        {
            _stop = true;
            _isTransmitting = false;
            ClearQueuedTxAudio();
            _outboundEvent.Set();
            try { _stream?.Close(); } catch { }
            try { _client?.Close(); } catch { }
            lock (_outboundLock)
            {
                _outboundFrames.Clear();
            }
            lock (_audioLock)
            {
                _audioBufCount = 0;
                _audioBufRead = 0;
                _audioBufWrite = 0;
            }
            // Branch G: stop IQ streaming state on disconnect
            _wantsIQ[0] = false;
            _wantsIQ[1] = false;
        }

        private void SendLoop()
        {
            while (!_stop && _client.Connected)
            {
                byte[] frame = null;
                lock (_outboundLock)
                {
                    if (_outboundFrames.Count > 0)
                    {
                        frame = _outboundFrames.Dequeue();
                    }
                }

                if (frame != null)
                {
                    try
                    {
                        lock (_sendLock)
                        {
                            _stream.Write(frame, 0, frame.Length);
                        }
                    }
                    catch
                    {
                        Close();
                        break;
                    }
                }
                else
                {
                    _outboundEvent.WaitOne(100);
                }
            }
        }

        public void PublishAudio(int trx, int sampleRate, float[] left, float[] right, int nsamples)
        {
            if (_stop || !_handshakeDone || trx != 0 || _clientMuted || !_wantsAudio[0] || left == null || nsamples <= 0) return;

            int targetRate = AudioSampleRate > 0 ? AudioSampleRate : 48000;
            int packetSamples = AudioStreamSamples > 0 ? AudioStreamSamples : 2048;
            int channels = AudioStreamChannels == 1 ? 1 : 2;
            TCISampleType sampleType = AudioSampleType;

            lock (_audioLock)
            {
                // Discard excess if ring buffer would overflow to preserve low latency without memory growth
                if (_audioBufCount + nsamples > RING_BUFFER_SIZE)
                {
                    int overflow = (_audioBufCount + nsamples) - RING_BUFFER_SIZE;
                    _audioBufRead = (_audioBufRead + overflow) % RING_BUFFER_SIZE;
                    _audioBufCount -= overflow;
                }

                for (int i = 0; i < nsamples; i++)
                {
                    _ringLeft[_audioBufWrite] = left[i];
                    _ringRight[_audioBufWrite] = (right != null && i < right.Length) ? right[i] : left[i];
                    _audioBufWrite = (_audioBufWrite + 1) % RING_BUFFER_SIZE;
                    _audioBufCount++;
                }

                while (_audioBufCount >= packetSamples)
                {
                    int interleavedCount = packetSamples * channels;
                    float[] interleaved = new float[interleavedCount];

                    if (channels == 1)
                    {
                        for (int i = 0; i < packetSamples; i++)
                        {
                            int idx = (_audioBufRead + i) % RING_BUFFER_SIZE;
                            interleaved[i] = _ringLeft[idx];
                        }
                    }
                    else
                    {
                        for (int i = 0; i < packetSamples; i++)
                        {
                            int idx = (_audioBufRead + i) % RING_BUFFER_SIZE;
                            interleaved[2 * i] = _ringLeft[idx];
                            interleaved[2 * i + 1] = _ringRight[idx];
                        }
                    }

                    _audioBufRead = (_audioBufRead + packetSamples) % RING_BUFFER_SIZE;
                    _audioBufCount -= packetSamples;

                    byte[] encoded = HeadlessTciServer.EncodeAudioSamples(interleaved, sampleType);
                    byte[] payload = HeadlessTciServer.BuildAudioPayload(0, targetRate, sampleType, interleavedCount, channels, encoded);
                    byte[] wsFrame = HeadlessTciServer.MakeWebSocketBinaryFrame(payload);

                    SendRawBytes(wsFrame);
                    AudioPacketsSent++;

                    // Branch G: accumulate RMS for S-meter reporting
                    if (_rxSensorsEnabled)
                    {
                        double sumSq = 0;
                        for (int i = 0; i < packetSamples * 2; i++) { double v = interleaved[i]; sumSq += v * v; }
                        _sMeterAccumSquared += sumSq;
                        _sMeterSampleCount += interleavedCount;
                    }
                }
            }
        }

        // Branch G: publish an IQ block to this client if it has requested IQ streaming.
        // IQ is float32 interleaved I/Q at IQSampleRate. Frames are streamed directly
        // (no rebuffering) - the native pipeline already produces fixed-size blocks.
        public void PublishIQ(int trx, int sampleRate, float[] interleavedIQ, int complexSamples)
        {
            if (_stop || !_handshakeDone || trx != 0 || !_wantsIQ[0] || interleavedIQ == null || complexSamples <= 0) return;

            // Resample if the native rate differs from the negotiated IQ rate
            float[] payloadSamples = interleavedIQ;
            int outRate = sampleRate;
            if (sampleRate != IQSampleRate && IQSampleRate > 0)
            {
                // Simple linear resample of interleaved IQ (pairs)
                int inComplex = complexSamples;
                int outComplex = (int)((long)inComplex * IQSampleRate / sampleRate);
                if (outComplex <= 0) return;
                float[] rs = new float[outComplex * 2];
                double step = (double)inComplex / outComplex;
                for (int i = 0; i < outComplex; i++)
                {
                    double pos = i * step;
                    int idx = (int)pos;
                    if (idx >= inComplex - 1) idx = inComplex - 2;
                    if (idx < 0) idx = 0;
                    double frac = pos - idx;
                    rs[2 * i] = (float)(interleavedIQ[2 * idx] + (interleavedIQ[2 * (idx + 1)] - interleavedIQ[2 * idx]) * frac);
                    rs[2 * i + 1] = (float)(interleavedIQ[2 * idx + 1] + (interleavedIQ[2 * idx + 3] - interleavedIQ[2 * idx + 1]) * frac);
                }
                payloadSamples = rs;
                complexSamples = outComplex;
                outRate = IQSampleRate;
            }

            byte[] encoded = HeadlessTciServer.EncodeAudioSamples(payloadSamples, TCISampleType.FLOAT32);
            byte[] payload = HeadlessTciServer.BuildIQPayload(trx, outRate, complexSamples, encoded);
            byte[] wsFrame = HeadlessTciServer.MakeWebSocketBinaryFrame(payload);
            SendRawBytes(wsFrame);
        }

        public void SendRawBytes(byte[] bytes)
        {
            if (_stop || !_handshakeDone || bytes == null || bytes.Length == 0) return;
            lock (_outboundLock)
            {
                // Cap queue at 32 frames (~1.3s of audio) to prevent memory growth if client network lags
                if (_outboundFrames.Count >= 32)
                {
                    _outboundFrames.Dequeue(); // Drop oldest frame to maintain low real-time latency
                }
                _outboundFrames.Enqueue(bytes);
            }
            _outboundEvent.Set();
        }

        public void SendTextFrame(string message)
        {
            if (_stop || !_handshakeDone || string.IsNullOrEmpty(message)) return;
            byte[] frame = HeadlessTciServer.MakeWebSocketTextFrame(message);
            SendRawBytes(frame);
        }

        private void ClientLoop()
        {
            byte[] buffer = new byte[8192];
            var streamBuffer = new List<byte>();

            try
            {
                while (!_stop && _client.Connected)
                {
                    int bytesRead = _stream.Read(buffer, 0, buffer.Length);
                    if (bytesRead <= 0) break;

                    if (!_handshakeDone)
                    {
                        string request = Encoding.UTF8.GetString(buffer, 0, bytesRead);
                        if (request.Contains("Sec-WebSocket-Key"))
                        {
                            PerformHandshake(request);
                            _handshakeDone = true;
                            SendInitialBanner();
                        }
                    }
                    else
                    {
                        for (int i = 0; i < bytesRead; i++) streamBuffer.Add(buffer[i]);
                        ProcessWebSocketFrames(streamBuffer);
                    }

                    // Branch G: periodic S-meter reporting
                    if (_rxSensorsEnabled && _wantsAudio[0])
                    {
                        long now = DateTime.UtcNow.Ticks;
                        if (_lastSensorSendTicks == 0 || (now - _lastSensorSendTicks) >= _rxSensorsIntervalMs * TimeSpan.TicksPerMillisecond)
                        {
                            _lastSensorSendTicks = now;
                            SendSMeterFrame();
                        }
                    }
                }
            }
            catch { }
            finally
            {
                // Release any active TX if this client was transmitting
                int rx = _server.BaseRxIndex;
                if (TxArbiter.Instance.ActiveDigitalRx == rx)
                {
                    TxArbiter.Instance.ReleaseDigitalTx(rx);
                }

                _server.RemoveClient(this);
                Close();
            }
        }

        private void PerformHandshake(string request)
        {
            string key = Regex.Match(request, "Sec-WebSocket-Key: (.*)").Groups[1].Value.Trim();
            string fullKey = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
            byte[] hash = SHA1.Create().ComputeHash(Encoding.UTF8.GetBytes(fullKey));
            string acceptKey = Convert.ToBase64String(hash);

            byte[] response = Encoding.UTF8.GetBytes(
                "HTTP/1.1 101 Switching Protocols\r\n" +
                "Connection: Upgrade\r\n" +
                "Upgrade: websocket\r\n" +
                "Sec-WebSocket-Accept: " + acceptKey + "\r\n\r\n");

            lock (_sendLock)
            {
                _stream.Write(response, 0, response.Length);
            }
        }

        private void SendInitialBanner()
        {
            // Send discrete WebSocket text frames for each parameter, exactly matching TCIServer.cs
            SendTextFrame("protocol:ExpertSDR3,2.0;");
            SendTextFrame("device:SunSDR2PRO;");
            SendTextFrame("receive_only:false;");
            SendTextFrame("trx_count:1;");
            SendTextFrame("channels_count:1;");
            SendTextFrame("vfo_limits:0,61440000;");
            SendTextFrame("if_limits:-24000,24000;");
            SendTextFrame("modulations_list:AM,SAM,DSB,LSB,USB,CWL,CWU,NFM,DIGL,DIGU;");
            SendTextFrame($"iq_samplerate:{IQSampleRate};");
            SendTextFrame("audio_samplerate:48000;");
            SendTextFrame("audio_stream_sample_type:float32;");
            SendTextFrame("audio_stream_channels:2;");
            SendTextFrame("audio_stream_samples:2048;");
            SendTextFrame("tx_stream_audio_buffering:100;");

            int rx = _server.BaseRxIndex;
            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
            long freqHz = slice != null ? (long)(slice.FrequencyMHz * 1e6) : 14074000;
            string modeStr = slice != null ? HeadlessTciManager.ModeToString(slice.Mode) : "DIGU";
            int low = slice != null ? slice.FilterLow : 300;
            int high = slice != null ? slice.FilterHigh : 3000;

            SendTextFrame($"vfo:0,0,{freqHz};");
            SendTextFrame($"vfo:0,1,{freqHz};");
            SendTextFrame("if:0,0,0;");
            SendTextFrame("if:0,1,0;");
            SendTextFrame($"modulation:0,{modeStr};");
            SendTextFrame($"rx_filter_band:0,{low},{high};");
            SendTextFrame("rx_channel_enable:0,0,true;");
            SendTextFrame("rx_channel_enable:0,1,false;");
            SendTextFrame("rx_enable:0,true;");
            SendTextFrame("tx_enable:0,true;");
            SendTextFrame("split_enable:0,false;");
            SendTextFrame("rit_enable:0,false;");
            SendTextFrame("xit_enable:0,false;");
            SendTextFrame("lock:0,false;");
            SendTextFrame("sql_enable:0,false;");
            SendTextFrame("trx:0,false;");
            SendTextFrame("drive:0,100;");
            SendTextFrame("tune_drive:0,100;");

            SendTextFrame("mute:false;");
            SendTextFrame("start;");
            SendTextFrame("ready;");
        }

        private void ProcessWebSocketFrames(List<byte> bytes)
        {
            while (bytes.Count >= 2)
            {
                bool fin = (bytes[0] & 0x80) != 0;
                int opcode = bytes[0] & 0x0F;
                bool masked = (bytes[1] & 0x80) != 0;
                int payloadLen = bytes[1] & 0x7F;
                int headerLen = 2;

                if (payloadLen == 126)
                {
                    if (bytes.Count < 4) return;
                    payloadLen = (bytes[2] << 8) | bytes[3];
                    headerLen = 4;
                }
                else if (payloadLen == 127)
                {
                    if (bytes.Count < 10) return;
                    payloadLen = (int)BitConverter.ToInt64(new byte[] { bytes[9], bytes[8], bytes[7], bytes[6], bytes[5], bytes[4], bytes[3], bytes[2] }, 0);
                    headerLen = 10;
                }

                if (masked) headerLen += 4;
                if (bytes.Count < headerLen + payloadLen) return; // need more data

                byte[] payload = new byte[payloadLen];
                if (masked)
                {
                    int maskOffset = headerLen - 4;
                    byte[] maskKey = new byte[] { bytes[maskOffset], bytes[maskOffset + 1], bytes[maskOffset + 2], bytes[maskOffset + 3] };
                    for (int i = 0; i < payloadLen; i++)
                    {
                        payload[i] = (byte)(bytes[headerLen + i] ^ maskKey[i % 4]);
                    }
                }
                else
                {
                    for (int i = 0; i < payloadLen; i++)
                    {
                        payload[i] = bytes[headerLen + i];
                    }
                }

                bytes.RemoveRange(0, headerLen + payloadLen);

                // Handle Frame by Opcode
                if (opcode == 0x01) // Text
                {
                    string text = Encoding.UTF8.GetString(payload);
                    HandleClientTextCommands(text);
                }
                else if (opcode == 0x02) // Binary
                {
                    HandleClientBinaryFrame(payload);
                }
                else if (opcode == 0x08) // Close
                {
                    Close();
                    return;
                }
                else if (opcode == 0x09) // Ping
                {
                    byte[] pong = new byte[] { 0x8A, 0x00 };
                    SendRawBytes(pong);
                }
            }
        }

        private void HandleClientBinaryFrame(byte[] payload)
        {
            if (payload == null || payload.Length < 64) return;

            int receiver = BitConverter.ToInt32(payload, 0);
            int sampleRate = BitConverter.ToInt32(payload, 4);
            TCISampleType sampleType = (TCISampleType)BitConverter.ToUInt32(payload, 8);
            int length = BitConverter.ToInt32(payload, 20);
            TCIStreamType streamType = (TCIStreamType)BitConverter.ToUInt32(payload, 24);
            int headerChannels = BitConverter.ToInt32(payload, 28);

            if (streamType != TCIStreamType.TX_AUDIO_STREAM || length <= 0) return;

            int bytesPerSample = HeadlessTciServer.GetBytesPerSample(sampleType);
            int dataOffset = 64;
            int actualDataBytes = payload.Length - dataOffset;
            if (actualDataBytes < bytesPerSample) return;

            int actualValueCount = actualDataBytes / bytesPerSample;
            int channels;
            int decodedValueCount;
            bool modernHeader = (headerChannels == 1 || headerChannels == 2);

            if (modernHeader)
            {
                channels = headerChannels;
                decodedValueCount = Math.Min(length, actualValueCount);
                if (channels > 1) decodedValueCount -= decodedValueCount % channels;
            }
            else
            {
                if (actualValueCount >= length * 2) channels = 2;
                else channels = 1;
                decodedValueCount = Math.Min(length, actualValueCount);
                if (channels > 1) decodedValueCount -= decodedValueCount % channels;
            }

            if (decodedValueCount <= 0) return;

            float[] decoded = HeadlessTciServer.DecodeSamples(payload, dataOffset, decodedValueCount, sampleType);
            for (int i = 0; i < decoded.Length; i++)
            {
                float sample = decoded[i];
                if (float.IsNaN(sample) || float.IsInfinity(sample)) decoded[i] = 0.0f;
                else if (sample > 4.0f) decoded[i] = 4.0f;
                else if (sample < -4.0f) decoded[i] = -4.0f;
            }

            int complexSamples = channels <= 1 ? decoded.Length : decoded.Length / channels;

            TCIQueuedTxAudio queuedAudio = new TCIQueuedTxAudio()
            {
                Receiver = receiver,
                SampleRate = sampleRate,
                SampleType = sampleType,
                Channels = channels,
                ComplexSamples = complexSamples,
                Samples = HeadlessTciServer.ConvertStreamSamplesToComplex(decoded, channels)
            };

            lock (_txQueueLock)
            {
                while (_txAudioQueue.Count >= MAX_TX_AUDIO_QUEUE_BLOCKS ||
                       (_txQueuedComplexSamples + queuedAudio.ComplexSamples) > MAX_TX_AUDIO_QUEUE_COMPLEX_SAMPLES)
                {
                    if (_txAudioQueue.Count == 0) break;
                    TCIQueuedTxAudio dropped = _txAudioQueue.Dequeue();
                    if (dropped != null)
                        _txQueuedComplexSamples = Math.Max(0, _txQueuedComplexSamples - Math.Max(0, dropped.ComplexSamples));
                }

                _txAudioQueue.Enqueue(queuedAudio);
                _txQueuedComplexSamples += Math.Max(0, queuedAudio.ComplexSamples);
            }
        }

        private void HandleClientTextCommands(string fullText)
        {
            if (string.IsNullOrWhiteSpace(fullText)) return;
            string[] commands = fullText.Split(new char[] { ';' }, StringSplitOptions.RemoveEmptyEntries);

            foreach (var rawCmd in commands)
            {
                string cmd = rawCmd.Trim();
                if (string.IsNullOrEmpty(cmd)) continue;

                string[] parts = cmd.Split(new char[] { ':' }, 2);
                string name = parts[0].ToLowerInvariant().Trim();
                string[] args = parts.Length > 1 ? parts[1].Split(',') : Array.Empty<string>();

                switch (name)
                {
                    case "audio_start":
                        _wantsAudio[0] = true;
                        _wantsAudio[1] = true;
                        HeadlessSliceManager.Instance.ActivateAudio(_server.BaseRxIndex);
                        SendTextFrame("audio_start:0;");
                        break;

                    case "audio_stop":
                        _wantsAudio[0] = false;
                        _wantsAudio[1] = false;
                        lock (_audioLock)
                        {
                            _audioBufCount = 0;
                            _audioBufRead = 0;
                            _audioBufWrite = 0;
                        }
                        SendTextFrame("audio_stop:0;");
                        if (!HeadlessTciManager.Instance.IsAnyClientStreaming(_server.BaseRxIndex))
                        {
                            HeadlessSliceManager.Instance.DeactivateAudio(_server.BaseRxIndex);
                        }
                        break;

                    case "vfo":
                        if (args.Length >= 3)
                        {
                            if (double.TryParse(args[2], System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out double freqHz))
                            {
                                int rx = _server.BaseRxIndex;
                                double newFreqMHz = freqHz / 1e6;
                                HeadlessSliceManager.Instance.SetFrequency(rx, newFreqMHz);
                                if (TxArbiter.Instance.ActiveDigitalRx == rx)
                                {
                                    TxArbiter.Instance.UpdateDigitalTxFrequency(rx, newFreqMHz);
                                }
                                _server.BroadcastText($"vfo:0,0,{freqHz:0};");
                                _server.BroadcastText($"vfo:0,1,{freqHz:0};");
                            }
                        }
                        else if (args.Length >= 2)
                        {
                            int rx = _server.BaseRxIndex;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            long freqHz = slice != null ? (long)(slice.FrequencyMHz * 1e6) : 14074000;
                            SendTextFrame($"vfo:0,{args[1]},{freqHz};");
                        }
                        break;

                    case "if":
                        if (args.Length >= 2)
                        {
                            SendTextFrame($"if:0,{args[1]},0;");
                        }
                        break;

                    // Branch G: DDS (panadapter center) - sets the slice center frequency.
                    // CW Skimmer uses this to learn what frequency range the IQ stream covers.
                    case "dds":
                        if (args.Length >= 2 && double.TryParse(args[1], System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out double ddsHz))
                        {
                            int rxDDS = _server.BaseRxIndex;
                            double ddsMHz = ddsHz / 1e6;
                            HeadlessSliceManager.Instance.SetFrequency(rxDDS, ddsMHz);
                            _server.BroadcastText($"dds:0,{ddsHz:0};");
                        }
                        else if (args.Length >= 1)
                        {
                            int rxDDS = _server.BaseRxIndex;
                            var sliceDDS = HeadlessSliceManager.Instance.GetSlice(rxDDS);
                            long ddsHzQ = sliceDDS != null ? (long)(sliceDDS.FrequencyMHz * 1e6) : 14074000;
                            SendTextFrame($"dds:0,{ddsHzQ:0};");
                        }
                        break;

                    case "modulation":
                        if (args.Length >= 2)
                        {
                            int rx = _server.BaseRxIndex;
                            DSPMode mode = HeadlessTciManager.ParseDSPMode(args[1]);
                            HeadlessSliceManager.Instance.SetMode(rx, mode);
                            _server.BroadcastText($"modulation:0,{args[1].ToUpperInvariant()};");
                        }
                        else if (args.Length == 1)
                        {
                            int rx = _server.BaseRxIndex;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            string modeStr = slice != null ? HeadlessTciManager.ModeToString(slice.Mode) : "DIGU";
                            SendTextFrame($"modulation:0,{modeStr};");
                        }
                        break;

                    case "rx_filter_band":
                        if (args.Length >= 3)
                        {
                            if (int.TryParse(args[1], out int low) && int.TryParse(args[2], out int high))
                            {
                                int rx = _server.BaseRxIndex;
                                HeadlessSliceManager.Instance.SetFilter(rx, low, high);
                                _server.BroadcastText($"rx_filter_band:0,{low},{high};");
                            }
                        }
                        else if (args.Length == 1)
                        {
                            int rx = _server.BaseRxIndex;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            int low = slice != null ? slice.FilterLow : 300;
                            int high = slice != null ? slice.FilterHigh : 3000;
                            SendTextFrame($"rx_filter_band:0,{low},{high};");
                        }
                        break;

                    case "trx":
                        if (args.Length >= 2)
                        {
                            bool wantsTx = bool.TryParse(args[1], out bool b) && b;
                            int rx = _server.BaseRxIndex;

                            if (wantsTx)
                            {
                                var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                                double freq = slice != null ? slice.FrequencyMHz : 14.074;
                                DSPMode mode = slice != null ? slice.Mode : DSPMode.DIGU;

                                bool granted = TxArbiter.Instance.RequestDigitalTx(rx, freq, mode);
                                _isTransmitting = granted;
                                if (granted)
                                {
                                    ClearQueuedTxAudio();
                                    cmaster.SignalTciTxStream();
                                }
                                _server.BroadcastTrxState(0, granted);
                            }
                            else
                            {
                                _isTransmitting = false;
                                ClearQueuedTxAudio();
                                TxArbiter.Instance.ReleaseDigitalTx(rx);
                                _server.BroadcastTrxState(0, false);
                            }
                        }
                        else if (args.Length == 1)
                        {
                            int rx = _server.BaseRxIndex;
                            bool isTx = TxArbiter.Instance.ActiveDigitalRx == rx;
                            SendTextFrame($"trx:0,{isTx.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "tune":
                        if (args.Length >= 2)
                        {
                            bool wantsTune = bool.TryParse(args[1], out bool b) && b;
                            int rx = _server.BaseRxIndex;

                            if (wantsTune)
                            {
                                var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                                double freq = slice != null ? slice.FrequencyMHz : 14.074;
                                DSPMode mode = slice != null ? slice.Mode : DSPMode.DIGU;

                                bool granted = TxArbiter.Instance.RequestDigitalTx(rx, freq, mode);
                                _isTransmitting = granted;
                                if (granted)
                                {
                                    ClearQueuedTxAudio();
                                    cmaster.SignalTciTxStream();
                                }
                                SendTextFrame($"tune:0,{granted.ToString().ToLowerInvariant()};");
                                _server.BroadcastTrxState(0, granted);
                            }
                            else
                            {
                                _isTransmitting = false;
                                ClearQueuedTxAudio();
                                TxArbiter.Instance.ReleaseDigitalTx(rx);
                                SendTextFrame("tune:0,false;");
                                _server.BroadcastTrxState(0, false);
                            }
                        }
                        else if (args.Length == 1)
                        {
                            int rx = _server.BaseRxIndex;
                            bool isTx = TxArbiter.Instance.ActiveDigitalRx == rx;
                            SendTextFrame($"tune:0,{isTx.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "rx_channel_enable":
                        if (args.Length >= 2 && int.TryParse(args[1], out int ceChan))
                        {
                            bool en = ceChan == 0;
                            if (args.Length >= 3 && bool.TryParse(args[2], out bool b)) en = b;
                            SendTextFrame($"rx_channel_enable:0,{ceChan},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "rx_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int reTrx))
                        {
                            bool en = true;
                            if (args.Length >= 2 && bool.TryParse(args[1], out bool b)) en = b;
                            SendTextFrame($"rx_enable:{reTrx},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "tx_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int teTrx))
                        {
                            bool en = true;
                            if (args.Length >= 2 && bool.TryParse(args[1], out bool b)) en = b;
                            SendTextFrame($"tx_enable:{teTrx},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "split_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int seTrx))
                        {
                            bool en = false;
                            if (args.Length >= 2 && bool.TryParse(args[1], out bool b)) en = b;
                            SendTextFrame($"split_enable:{seTrx},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "rit_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int ritTrx))
                        {
                            bool en = args.Length >= 2 && bool.TryParse(args[1], out bool b) && b;
                            SendTextFrame($"rit_enable:{ritTrx},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "xit_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int xitTrx))
                        {
                            bool en = args.Length >= 2 && bool.TryParse(args[1], out bool b) && b;
                            SendTextFrame($"xit_enable:{xitTrx},{en.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "rit_offset":
                        if (args.Length > 0 && int.TryParse(args[0], out int roTrx))
                        {
                            int off = args.Length >= 2 && int.TryParse(args[1], out int o) ? o : 0;
                            SendTextFrame($"rit_offset:{roTrx},{off};");
                        }
                        break;

                    case "xit_offset":
                        if (args.Length > 0 && int.TryParse(args[0], out int xoTrx))
                        {
                            int off = args.Length >= 2 && int.TryParse(args[1], out int o) ? o : 0;
                            SendTextFrame($"xit_offset:{xoTrx},{off};");
                        }
                        break;

                    case "lock":
                        if (args.Length > 0 && int.TryParse(args[0], out int lkTrx))
                        {
                            bool lk = args.Length >= 2 && bool.TryParse(args[1], out bool b) && b;
                            SendTextFrame($"lock:{lkTrx},{lk.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "sql_enable":
                        if (args.Length > 0 && int.TryParse(args[0], out int sqlTrx))
                        {
                            bool sq = args.Length >= 2 && bool.TryParse(args[1], out bool b) && b;
                            SendTextFrame($"sql_enable:{sqlTrx},{sq.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "sql_level":
                        if (args.Length > 0 && int.TryParse(args[0], out int sqlLvlTrx))
                        {
                            int lvl = args.Length >= 2 && int.TryParse(args[1], out int l) ? l : -140;
                            SendTextFrame($"sql_level:{sqlLvlTrx},{lvl};");
                        }
                        break;

                    case "drive":
                        if (args.Length > 0 && int.TryParse(args[0], out int drvTrx))
                        {
                            int drv = args.Length >= 2 && int.TryParse(args[1], out int d) ? d : 100;
                            SendTextFrame($"drive:{drvTrx},{drv};");
                        }
                        break;

                    case "tune_drive":
                        if (args.Length > 0 && int.TryParse(args[0], out int tdrvTrx))
                        {
                            int tdrv = args.Length >= 2 && int.TryParse(args[1], out int d) ? d : 100;
                            SendTextFrame($"tune_drive:{tdrvTrx},{tdrv};");
                        }
                        break;

                    // Branch G: functional AGC control (Tier 2)
                    // agc_mode: off/long/slow/normal(fast alias)/custom -> WDSP AGC mode
                    case "agc_mode":
                        if (args.Length >= 2)
                        {
                            var sliceAgc = HeadlessSliceManager.Instance.GetSlice(_server.BaseRxIndex);
                            if (sliceAgc != null && sliceAgc.IsActive)
                            {
                                AGCMode agcMode = HeadlessTciManager.AGCModeFromTciString(args[1]);
                                WDSP.SetRXAAGCMode(sliceAgc.ChannelId, agcMode);
                            }
                            _server.BroadcastText($"agc_mode:0,{args[1].Trim().ToLowerInvariant()};");
                        }
                        else if (args.Length == 1)
                        {
                            SendTextFrame("agc_mode:0,normal;");
                        }
                        break;

                    // agc_auto_ex:0,false -> AGC fixed gain mode (manual)
                    case "agc_auto_ex":
                        if (args.Length >= 2 && bool.TryParse(args[1], out bool agcAuto))
                        {
                            var sliceAuto = HeadlessSliceManager.Instance.GetSlice(_server.BaseRxIndex);
                            if (sliceAuto != null && sliceAuto.IsActive)
                            {
                                // auto=true -> normal AGC; auto=false -> fixed gain (hang not used)
                                WDSP.SetRXAAGCMode(sliceAuto.ChannelId, agcAuto ? AGCMode.MED : AGCMode.FIXD);
                            }
                            _server.BroadcastText($"agc_auto_ex:0,{agcAuto.ToString().ToLowerInvariant()};");
                        }
                        else if (args.Length == 1)
                        {
                            SendTextFrame("agc_auto_ex:0,true;");
                        }
                        break;

                    // agc_gain:0,N -> manual AGC fixed gain when AGC is off (-20..+120 dB)
                    case "agc_gain":
                        if (args.Length >= 2 && int.TryParse(args[1], out int agcGainDb))
                        {
                            agcGainDb = Math.Max(-20, Math.Min(120, agcGainDb));
                            var sliceGain = HeadlessSliceManager.Instance.GetSlice(_server.BaseRxIndex);
                            if (sliceGain != null && sliceGain.IsActive)
                            {
                                WDSP.SetRXAAGCFixed(sliceGain.ChannelId, agcGainDb);
                            }
                            _server.BroadcastText($"agc_gain:0,{agcGainDb};");
                        }
                        else if (args.Length == 1)
                        {
                            SendTextFrame("agc_gain:0,0;");
                        }
                        break;

                    // Branch G: functional mute (Tier 2) - silences this client's audio stream
                    case "mute":
                        if (args.Length > 0 && bool.TryParse(args[0], out bool mreq))
                        {
                            _clientMuted = mreq;
                            SendTextFrame($"mute:{_clientMuted.ToString().ToLowerInvariant()};");
                        }
                        else
                        {
                            SendTextFrame("mute:false;");
                        }
                        break;

                    case "volume":
                        SendTextFrame("volume:0;");
                        break;

                    case "rx_volume":
                        if (args.Length >= 3 && int.TryParse(args[0], out int setVolTrx) && int.TryParse(args[1], out int setVolChan) && double.TryParse(args[2], System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out double volDb))
                        {
                            int rx = _server.BaseRxIndex + setVolTrx;
                            double gainFactor = Math.Pow(10.0, volDb / 20.0) * 0.05;
                            HeadlessSliceManager.Instance.SetSliceGain(rx, gainFactor);
                            SendTextFrame($"rx_volume:{setVolTrx},{setVolChan},{volDb:0.00};");
                        }
                        else if (args.Length >= 2 && int.TryParse(args[0], out int volTrx) && int.TryParse(args[1], out int volChan))
                        {
                            SendTextFrame($"rx_volume:{volTrx},{volChan},0.00;");
                        }
                        break;

                    case "cw_macros_speed":
                        SendTextFrame("cw_macros_speed:30;");
                        break;

                    case "cw_macros_delay":
                        SendTextFrame("cw_macros_delay:50;");
                        break;

                    case "cw_keyer_speed":
                        SendTextFrame("cw_keyer_speed:30;");
                        break;

                    // Branch G: IQ stream control (CW Skimmer, panadapter clients)
                    case "iq_start":
                        if (args.Length > 0 && int.TryParse(args[0], out int iqTrxStart))
                        {
                            if (iqTrxStart >= 0 && iqTrxStart <= 1) _wantsIQ[iqTrxStart] = true;
                            SendTextFrame($"iq_start:{iqTrxStart};");
                        }
                        break;

                    case "iq_stop":
                        if (args.Length > 0 && int.TryParse(args[0], out int iqTrxStop))
                        {
                            if (iqTrxStop >= 0 && iqTrxStop <= 1) _wantsIQ[iqTrxStop] = false;
                            SendTextFrame($"iq_stop:{iqTrxStop};");
                        }
                        break;

                    case "iq_samplerate":
                        // Branch G: functional IQ rate for headless ports.
                        // Clamped to 48k-384k; default 96k keeps per-receiver bandwidth ~0.77 MB/s.
                        if (args.Length > 0 && int.TryParse(args[0], out int isrReq))
                        {
                            int isr = Math.Max(48000, Math.Min(384000, isrReq));
                            // snap to supported rates
                            if (isr <= 48000) IQSampleRate = 48000;
                            else if (isr <= 96000) IQSampleRate = 96000;
                            else if (isr <= 192000) IQSampleRate = 192000;
                            else IQSampleRate = 384000;
                        }
                        SendTextFrame($"iq_samplerate:{IQSampleRate};");
                        break;

                    case "audio_samplerate":
                        if (args.Length > 0 && int.TryParse(args[0], out int asr)) AudioSampleRate = asr;
                        SendTextFrame($"audio_samplerate:{AudioSampleRate};");
                        break;


                    case "audio_stream_sample_type":
                        if (args.Length > 0)
                        {
                            switch (args[0].Trim().ToLowerInvariant())
                            {
                                case "int16": AudioSampleType = TCISampleType.INT16; break;
                                case "int24": AudioSampleType = TCISampleType.INT24; break;
                                case "int32": AudioSampleType = TCISampleType.INT32; break;
                                case "float32":
                                default: AudioSampleType = TCISampleType.FLOAT32; break;
                            }
                        }
                        SendTextFrame($"audio_stream_sample_type:{AudioSampleType.ToString().ToLowerInvariant()};");
                        break;

                    case "audio_stream_channels":
                        _seenModernTxAudioNegotiation = true;
                        if (args.Length > 0 && int.TryParse(args[0], out int asc) && (asc == 1 || asc == 2))
                        {
                            AudioStreamChannels = asc;
                        }
                        SendTextFrame($"audio_stream_channels:{AudioStreamChannels};");
                        break;

                    case "audio_stream_samples":
                        _seenModernTxAudioNegotiation = true;
                        if (args.Length > 0 && int.TryParse(args[0], out int asamp) && asamp >= 100 && asamp <= 2048)
                        {
                            AudioStreamSamples = asamp;
                        }
                        SendTextFrame($"audio_stream_samples:{AudioStreamSamples};");
                        break;

                    case "tx_stream_audio_buffering":
                        if (args.Length > 0 && int.TryParse(args[0], out int bufMs))
                        {
                            TxStreamAudioBufferingMs = Math.Max(20, Math.Min(500, bufMs));
                        }
                        SendTextFrame($"tx_stream_audio_buffering:{TxStreamAudioBufferingMs};");
                        break;

                    // Branch G: S-meter enable - reports rx_sensors/rx_channel_sensors
                    // computed from streaming audio RMS. Interval 100-2000ms, default 500ms.
                    case "rx_sensors_enable":
                        if (args.Length > 0 && bool.TryParse(args[0], out bool sen))
                        {
                            _rxSensorsEnabled = sen;
                            if (args.Length > 1 && int.TryParse(args[1], out int senInt))
                                _rxSensorsIntervalMs = Math.Max(100, Math.Min(2000, senInt));
                            if (!_rxSensorsEnabled)
                            {
                                _sMeterAccumSquared = 0;
                                _sMeterSampleCount = 0;
                            }
                            SendTextFrame($"rx_sensors_enable:{_rxSensorsEnabled.ToString().ToLowerInvariant()},{_rxSensorsIntervalMs};");
                        }
                        break;

                    case "start":
                        var cStart = HeadlessTciManager.Instance.Console;
                        if (cStart != null && !cStart.PowerOn)
                        {
                            try { cStart.PowerOn = true; } catch { }
                        }
                        SendTextFrame("start;");
                        break;

                    case "stop":
                        var cStop = HeadlessTciManager.Instance.Console;
                        if (cStop != null && cStop.PowerOn)
                        {
                            try { cStop.PowerOn = false; } catch { }
                        }
                        SendTextFrame("stop;");
                        break;

                    case "ready":
                        SendTextFrame("ready;");
                        break;
                }
            }
        }
    }
}
