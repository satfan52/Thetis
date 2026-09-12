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
    public sealed class HeadlessTciManager
    {
        public static HeadlessTciManager Instance { get; } = new HeadlessTciManager();

        private readonly List<HeadlessTciServer> _servers = new List<HeadlessTciServer>();
        private bool _isRunning = false;
        private readonly object _lock = new object();

        private HeadlessTciManager() { }

        public bool IsRunning => _isRunning;

        public void StartAll(IPAddress bindAddress, Console console)
        {
            lock (_lock)
            {
                if (_isRunning) StopAll();

                TxArbiter.Instance.Initialize(console);
                TxArbiter.Instance.DigitalSlicePreempted += OnSlicePreempted;

                HeadlessSliceManager.Instance.SliceFrequencyChanged += OnSliceFreqChanged;
                HeadlessSliceManager.Instance.SliceModeChanged += OnSliceModeChanged;
                HeadlessSliceManager.Instance.SliceFilterChanged += OnSliceFilterChanged;

                // Bind ports 50003..50008 matching RX3..RX8 directly (Port = 50000 + RX#)
                // 50003 -> RX3 (DDC 2)
                // 50004 -> RX4 (DDC 3)
                // 50005 -> RX5 (DDC 4)
                // 50006 -> RX6 (DDC 5)
                // 50007 -> RX7 (DDC 6)
                // 50008 -> RX8 (DDC 7)
                int[] ports = new int[] { 50003, 50004, 50005, 50006, 50007, 50008 };
                int[] baseRxs = new int[] { 2, 3, 4, 5, 6, 7 };

                for (int i = 0; i < ports.Length; i++)
                {
                    var s = new HeadlessTciServer(bindAddress, ports[i], baseRxs[i]);
                    _servers.Add(s);
                    s.Start();
                }

                cmaster.HeadlessAudioPublisher = PublishRxAudio;
                _isRunning = true;
            }
        }

        public void StopAll()
        {
            lock (_lock)
            {
                if (!_isRunning) return;

                cmaster.HeadlessAudioPublisher = null;

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
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        s.PublishAudio(rx - s.BaseRxIndex, sampleRate, left, right, nsamples);
                    }
                }
            }
        }

        public bool IsAnyClientStreaming(int rx)
        {
            lock (_lock)
            {
                for (int i = 0; i < _servers.Count; i++)
                {
                    var s = _servers[i];
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        if (s.IsTrxStreaming(rx - s.BaseRxIndex)) return true;
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
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        s.BroadcastTrxState(rx - s.BaseRxIndex, false);
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
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        s.BroadcastVfo(rx - s.BaseRxIndex, freqHz);
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
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        s.BroadcastMode(rx - s.BaseRxIndex, modeStr);
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
                    if (rx >= s.BaseRxIndex && rx < s.BaseRxIndex + 2)
                    {
                        s.BroadcastFilter(rx - s.BaseRxIndex, lowHz, highHz);
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

        public void RemoveClient(HeadlessTciClientHandler client)
        {
            lock (_clientsLock)
            {
                _clients.Remove(client);
            }

            for (int trx = 0; trx < 2; trx++)
            {
                int rx = BaseRxIndex + trx;
                if (!HeadlessTciManager.Instance.IsAnyClientStreaming(rx))
                {
                    HeadlessSliceManager.Instance.DeactivateAudio(rx);
                }
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
            BroadcastText($"vfo:{trx},0,{freqHz};");
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
    }

    public sealed class HeadlessTciClientHandler
    {
        private readonly HeadlessTciServer _server;
        private readonly TcpClient _client;
        private NetworkStream _stream;
        private Thread _thread;
        private volatile bool _stop = false;
        private bool _handshakeDone = false;
        private readonly bool[] _wantsAudio = new bool[2];
        private readonly object _sendLock = new object();

        public int AudioStreamSamples { get; private set; } = 2048;
        public int AudioStreamChannels { get; private set; } = 2;
        internal TCISampleType AudioSampleType { get; private set; } = TCISampleType.FLOAT32;
        public int AudioSampleRate { get; private set; } = 48000;
        public long AudioPacketsSent { get; private set; } = 0;

        private readonly List<float>[] _pendingLeft = new List<float>[] { new List<float>(), new List<float>() };
        private readonly List<float>[] _pendingRight = new List<float>[] { new List<float>(), new List<float>() };
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

        public void Start()
        {
            _stream = _client.GetStream();
            _thread = new Thread(ClientLoop)
            {
                IsBackground = true,
                Name = $"HeadlessTciClient_{_server.Port}"
            };
            _thread.Start();
        }

        public void Close()
        {
            _stop = true;
            try { _stream?.Close(); } catch { }
            try { _client?.Close(); } catch { }
            lock (_audioLock)
            {
                _pendingLeft[0].Clear();
                _pendingRight[0].Clear();
                _pendingLeft[1].Clear();
                _pendingRight[1].Clear();
            }
        }

        public void PublishAudio(int trx, int sampleRate, float[] left, float[] right, int nsamples)
        {
            if (_stop || !_handshakeDone || trx < 0 || trx > 1 || !_wantsAudio[trx] || left == null || nsamples <= 0) return;

            int targetRate = AudioSampleRate > 0 ? AudioSampleRate : 48000;
            int packetSamples = AudioStreamSamples > 0 ? AudioStreamSamples : 2048;
            int channels = AudioStreamChannels == 1 ? 1 : 2;
            TCISampleType sampleType = AudioSampleType;

            lock (_audioLock)
            {
                var pl = _pendingLeft[trx];
                var pr = _pendingRight[trx];

                for (int i = 0; i < nsamples; i++)
                {
                    pl.Add(left[i]);
                    pr.Add(right != null && i < right.Length ? right[i] : left[i]);
                }

                while (pl.Count >= packetSamples)
                {
                    int interleavedCount = packetSamples * channels;
                    float[] interleaved = new float[interleavedCount];

                    if (channels == 1)
                    {
                        pl.CopyTo(0, interleaved, 0, packetSamples);
                    }
                    else
                    {
                        for (int i = 0; i < packetSamples; i++)
                        {
                            interleaved[2 * i] = pl[i];
                            interleaved[2 * i + 1] = pr[i];
                        }
                    }

                    pl.RemoveRange(0, packetSamples);
                    pr.RemoveRange(0, packetSamples);

                    byte[] encoded = HeadlessTciServer.EncodeAudioSamples(interleaved, sampleType);
                    byte[] payload = HeadlessTciServer.BuildAudioPayload(trx, targetRate, sampleType, interleavedCount, channels, encoded);
                    byte[] wsFrame = HeadlessTciServer.MakeWebSocketBinaryFrame(payload);

                    SendRawBytes(wsFrame);
                    AudioPacketsSent++;
                }
            }
        }

        public void SendRawBytes(byte[] bytes)
        {
            if (_stop || !_handshakeDone || bytes == null || bytes.Length == 0) return;
            try
            {
                lock (_sendLock)
                {
                    _stream.Write(bytes, 0, bytes.Length);
                }
            }
            catch
            {
                Close();
            }
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
                }
            }
            catch { }
            finally
            {
                // Release any active TX if this client was transmitting
                for (int trx = 0; trx < 2; trx++)
                {
                    int rx = _server.BaseRxIndex + trx;
                    if (TxArbiter.Instance.ActiveDigitalRx == rx)
                    {
                        TxArbiter.Instance.ReleaseDigitalTx(rx);
                    }
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
            SendTextFrame("trx_count:2;");
            SendTextFrame("channels_count:2;");
            SendTextFrame("vfo_limits:0,61440000;");
            SendTextFrame("if_limits:-24000,24000;");
            SendTextFrame("modulations_list:AM,SAM,DSB,LSB,USB,CWL,CWU,NFM,DIGL,DIGU;");
            SendTextFrame("iq_samplerate:48000;");
            SendTextFrame("audio_samplerate:48000;");
            SendTextFrame("audio_stream_sample_type:float32;");
            SendTextFrame("audio_stream_channels:2;");
            SendTextFrame("audio_stream_samples:2048;");
            SendTextFrame("tx_stream_audio_buffering:100;");

            for (int trx = 0; trx < 2; trx++)
            {
                int rx = _server.BaseRxIndex + trx;
                var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                long freqHz = slice != null ? (long)(slice.FrequencyMHz * 1e6) : 14074000;
                string modeStr = slice != null ? HeadlessTciManager.ModeToString(slice.Mode) : "DIGU";
                int low = slice != null ? slice.FilterLow : 300;
                int high = slice != null ? slice.FilterHigh : 3000;

                SendTextFrame($"vfo:{trx},0,{freqHz};");
                SendTextFrame($"vfo:{trx},1,{freqHz};");
                SendTextFrame($"if:{trx},0,0;");
                SendTextFrame($"if:{trx},1,0;");
                SendTextFrame($"modulation:{trx},{modeStr};");
                SendTextFrame($"rx_filter_band:{trx},{low},{high};");
                SendTextFrame($"rx_channel_enable:{trx},0,true;");
                SendTextFrame($"rx_channel_enable:{trx},1,false;");
                SendTextFrame($"rx_enable:{trx},true;");
                SendTextFrame($"tx_enable:{trx},true;");
                SendTextFrame($"split_enable:{trx},false;");
                SendTextFrame($"rit_enable:{trx},false;");
                SendTextFrame($"xit_enable:{trx},false;");
                SendTextFrame($"lock:{trx},false;");
                SendTextFrame($"sql_enable:{trx},false;");
                SendTextFrame($"trx:{trx},false;");
                SendTextFrame($"drive:{trx},100;");
                SendTextFrame($"tune_drive:{trx},100;");
            }

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
                        if (args.Length > 0 && int.TryParse(args[0], out int startTrx) && startTrx >= 0 && startTrx <= 1)
                        {
                            _wantsAudio[startTrx] = true;
                            int rx = _server.BaseRxIndex + startTrx;
                            HeadlessSliceManager.Instance.ActivateAudio(rx);
                            SendTextFrame($"audio_start:{startTrx};");
                        }
                        break;

                    case "audio_stop":
                        if (args.Length > 0 && int.TryParse(args[0], out int stopTrx) && stopTrx >= 0 && stopTrx <= 1)
                        {
                            _wantsAudio[stopTrx] = false;
                            lock (_audioLock)
                            {
                                _pendingLeft[stopTrx].Clear();
                                _pendingRight[stopTrx].Clear();
                            }
                            SendTextFrame($"audio_stop:{stopTrx};");

                            int rx = _server.BaseRxIndex + stopTrx;
                            if (!HeadlessTciManager.Instance.IsAnyClientStreaming(rx))
                            {
                                HeadlessSliceManager.Instance.DeactivateAudio(rx);
                            }
                        }
                        break;

                    case "vfo":
                        if (args.Length >= 3 && int.TryParse(args[0], out int vfoTrx) && vfoTrx >= 0 && vfoTrx <= 1)
                        {
                            if (double.TryParse(args[2], System.Globalization.NumberStyles.Any, System.Globalization.CultureInfo.InvariantCulture, out double freqHz))
                            {
                                int rx = _server.BaseRxIndex + vfoTrx;
                                HeadlessSliceManager.Instance.SetFrequency(rx, freqHz / 1e6);
                                _server.BroadcastText($"vfo:{vfoTrx},{args[1]},{freqHz:0};");
                            }
                        }
                        else if (args.Length == 2 && int.TryParse(args[0], out int qTrx) && qTrx >= 0 && qTrx <= 1)
                        {
                            int rx = _server.BaseRxIndex + qTrx;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            long freqHz = slice != null ? (long)(slice.FrequencyMHz * 1e6) : 14074000;
                            SendTextFrame($"vfo:{qTrx},{args[1]},{freqHz};");
                        }
                        break;

                    case "if":
                        if (args.Length >= 2 && int.TryParse(args[0], out int ifTrx) && ifTrx >= 0 && ifTrx <= 1)
                        {
                            SendTextFrame($"if:{ifTrx},{args[1]},0;");
                        }
                        break;

                    case "modulation":
                        if (args.Length >= 2 && int.TryParse(args[0], out int modTrx) && modTrx >= 0 && modTrx <= 1)
                        {
                            int rx = _server.BaseRxIndex + modTrx;
                            DSPMode mode = HeadlessTciManager.ParseDSPMode(args[1]);
                            HeadlessSliceManager.Instance.SetMode(rx, mode);
                            _server.BroadcastText($"modulation:{modTrx},{args[1].ToUpperInvariant()};");
                        }
                        else if (args.Length == 1 && int.TryParse(args[0], out int qModTrx) && qModTrx >= 0 && qModTrx <= 1)
                        {
                            int rx = _server.BaseRxIndex + qModTrx;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            string modeStr = slice != null ? HeadlessTciManager.ModeToString(slice.Mode) : "DIGU";
                            SendTextFrame($"modulation:{qModTrx},{modeStr};");
                        }
                        break;

                    case "rx_filter_band":
                        if (args.Length >= 3 && int.TryParse(args[0], out int filTrx) && filTrx >= 0 && filTrx <= 1)
                        {
                            if (int.TryParse(args[1], out int low) && int.TryParse(args[2], out int high))
                            {
                                int rx = _server.BaseRxIndex + filTrx;
                                HeadlessSliceManager.Instance.SetFilter(rx, low, high);
                                _server.BroadcastText($"rx_filter_band:{filTrx},{low},{high};");
                            }
                        }
                        else if (args.Length == 1 && int.TryParse(args[0], out int qFilTrx) && qFilTrx >= 0 && qFilTrx <= 1)
                        {
                            int rx = _server.BaseRxIndex + qFilTrx;
                            var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                            int low = slice != null ? slice.FilterLow : 300;
                            int high = slice != null ? slice.FilterHigh : 3000;
                            SendTextFrame($"rx_filter_band:{qFilTrx},{low},{high};");
                        }
                        break;

                    case "trx":
                        if (args.Length >= 2 && int.TryParse(args[0], out int trx) && trx >= 0 && trx <= 1)
                        {
                            bool wantsTx = bool.TryParse(args[1], out bool b) && b;
                            int rx = _server.BaseRxIndex + trx;

                            if (wantsTx)
                            {
                                var slice = HeadlessSliceManager.Instance.GetSlice(rx);
                                double freq = slice != null ? slice.FrequencyMHz : 14.074;
                                DSPMode mode = slice != null ? slice.Mode : DSPMode.DIGU;

                                bool granted = TxArbiter.Instance.RequestDigitalTx(rx, freq, mode);
                                _server.BroadcastTrxState(trx, granted);
                            }
                            else
                            {
                                TxArbiter.Instance.ReleaseDigitalTx(rx);
                                _server.BroadcastTrxState(trx, false);
                            }
                        }
                        else if (args.Length == 1 && int.TryParse(args[0], out int qTrxTrx) && qTrxTrx >= 0 && qTrxTrx <= 1)
                        {
                            int rx = _server.BaseRxIndex + qTrxTrx;
                            bool isTx = TxArbiter.Instance.ActiveDigitalRx == rx;
                            SendTextFrame($"trx:{qTrxTrx},{isTx.ToString().ToLowerInvariant()};");
                        }
                        break;

                    case "rx_channel_enable":
                        if (args.Length >= 2 && int.TryParse(args[0], out int ceTrx) && int.TryParse(args[1], out int ceChan))
                        {
                            bool en = ceChan == 0;
                            if (args.Length >= 3 && bool.TryParse(args[2], out bool b)) en = b;
                            SendTextFrame($"rx_channel_enable:{ceTrx},{ceChan},{en.ToString().ToLowerInvariant()};");
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

                    case "mute":
                        SendTextFrame("mute:false;");
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

                    case "audio_samplerate":
                        if (args.Length > 0 && int.TryParse(args[0], out int asr)) AudioSampleRate = asr;
                        SendTextFrame($"audio_samplerate:{AudioSampleRate};");
                        break;

                    case "iq_samplerate":
                        if (args.Length > 0) SendTextFrame($"iq_samplerate:{args[0]};");
                        else SendTextFrame("iq_samplerate:48000;");
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
                        if (args.Length > 0 && int.TryParse(args[0], out int asc) && (asc == 1 || asc == 2))
                        {
                            AudioStreamChannels = asc;
                        }
                        SendTextFrame($"audio_stream_channels:{AudioStreamChannels};");
                        break;

                    case "audio_stream_samples":
                        if (args.Length > 0 && int.TryParse(args[0], out int asamp) && asamp >= 100 && asamp <= 2048)
                        {
                            AudioStreamSamples = asamp;
                        }
                        SendTextFrame($"audio_stream_samples:{AudioStreamSamples};");
                        break;

                    case "tx_stream_audio_buffering":
                        if (args.Length > 0) SendTextFrame($"tx_stream_audio_buffering:{args[0]};");
                        else SendTextFrame("tx_stream_audio_buffering:100;");
                        break;

                    case "start":
                        SendTextFrame("start;");
                        break;

                    case "stop":
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
