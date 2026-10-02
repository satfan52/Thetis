// H1: OM Power OM2000A+ remote control over the LAN.
//
// The amplifier carries a Lantronix XPort network interface. Its data port (10001)
// speaks a small ASCII protocol, recovered from the official OM Power manager and
// verified live on the bench 2026-10-02:
//
//   'C' handshake    -> "CON;WA<nnn>;"   (WA = heating countdown, 1/s while heating)
//   '?'              -> "ST<flags>;"     (status: band, antenna)
//   '1'              -> "INFO:...;"      (amplifier type and firmware version)
//   '#' poll, 1/s    -> housekeeping batch ("IP0;IG0;UP2952;UG123;US0;UH8.5;UM237;TE64;")
//   'T' poll, each 5th -> "TE<nn>;"        (temperature, kept for the log)
//   'I' poll, 4/s while PTT on -> the power frame "PO<W>,PR<W>,PI<W>;IS..;FR..;"
//                       (forward / reflected / input power - feeds the TX meters)
//   "OPERATE;", "STBY;", "PTTON;", "PTTOFF;"   state reports
//   'O'              -> toggles stand-by / operate, amp replies with the new state
//
// Only ONE TCP client is accepted at a time, so the console holds the link only
// while one of the two H1 amp options is enabled; the OM Power manager software
// cannot be connected at the same time.
//
// This class owns the connection, the one-second poll, the receive parser and the
// stand-by / operate requests with confirmation and one retry. Everything is
// traced to amp_lan.log beside the exe.

using System;
using System.Diagnostics;
using System.Net.Sockets;
using System.Text;
using System.Threading;

namespace Thetis
{
    public sealed class AmpLanController : IDisposable
    {
        // milliseconds without the 24.9 day wrap of Environment.TickCount
        private static long NowMs()
        {
            return (long)(Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency * 1000.0);
        }

        #region H1: trace

        private static void LogText(string format, params object[] args)
        {
            try
            {
                string text = string.Format("[{0:yyyy-MM-dd HH:mm:ss.fff}] {1}" + Environment.NewLine, DateTime.Now,
                    (args != null && args.Length > 0) ? string.Format(format, args) : format);
                string path = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "amp_lan.log");
                if (System.IO.File.Exists(path) && new System.IO.FileInfo(path).Length > 200 * 1024)
                {
                    System.IO.File.Copy(path, path + ".old", true);
                    System.IO.File.Delete(path);
                }
                System.IO.File.AppendAllText(path, text);
            }
            catch
            {
                // never crash the caller
            }
        }

        #endregion

        private readonly Console console;

        private readonly object _sync = new object();
        private TcpClient _client;
        private NetworkStream _stream;
        private Thread _worker;
        private volatile bool _dispose;
        private volatile bool _enabled;

        private volatile string _address = "192.168.129.124";
        private volatile int _port = 10001;

        // measurements, "fresh" = seen within a short window
        private float _po = float.NaN;      // forward power, W
        private float _pr = float.NaN;      // reflected power, W
        private float _pi = float.NaN;      // input power, W
        private long _poStamp = 0;
        private long _prStamp = 0;

        // amplifier state: 0 unknown, 1 stand-by, 2 operate
        private volatile int _ampState = 0;
        private volatile bool _stateKnown = false;
        private long _stateStamp = 0;

        // requested state change: 0 none, 1 stand-by, 2 operate
        private volatile int _wantState = 0;
        private volatile bool _pttOn = false;

        private string _rx = string.Empty;
        private long _lastPoll = 0;
        private long _lastMeterTrace = 0;
        private bool _tracedPo = false;
        private bool _tracedTe = false;
        private int _lastWa = -1;
        private volatile bool _sawCon = false;
        private volatile bool _sawStatus = false;
        private volatile bool _sawInfo = false;
        private long _verboseUntil = 0;
        private int _pollCount = 0;
        private long _lastIFast = 0;
        private readonly System.Collections.Generic.List<long> _stateEventMs = new System.Collections.Generic.List<long>();
        private readonly System.Collections.Generic.List<bool> _stateEventOperate = new System.Collections.Generic.List<bool>();

        public AmpLanController(Console console)
        {
            this.console = console;
        }

        #region public surface used by the console

        public bool IsOpen
        {
            get
            {
                lock (_sync)
                {
                    return _client != null && _client.Connected;
                }
            }
        }

        public bool StateKnown
        {
            get { return _stateKnown; }
        }

        public bool IsOperate
        {
            get { return _stateKnown && _ampState == 2; }
        }

        public float AmpForwardWatts
        {
            get { lock (_sync) { return _po; } }
        }

        public float AmpReflectedWatts
        {
            get { lock (_sync) { return _pr; } }
        }

        public float AmpSwrRatio
        {
            get
            {
                float po, pr;
                lock (_sync) { po = _po; pr = _pr; }
                if (float.IsNaN(po) || float.IsNaN(pr) || po <= 0f || pr < 0f || pr > po) return 0f;
                double rho = Math.Sqrt(pr / po);
                if (rho >= 1.0) return 0f;
                return (float)((1.0 + rho) / (1.0 - rho));
            }
        }

        public bool FwdFresh(int maxAgeMs)
        {
            long stamp;
            lock (_sync) { stamp = _poStamp; }
            return stamp != 0 && NowMs() - stamp <= maxAgeMs;
        }

        public bool RefFresh(int maxAgeMs)
        {
            long stamp;
            lock (_sync) { stamp = _prStamp; }
            return stamp != 0 && NowMs() - stamp <= maxAgeMs;
        }

        /// <summary>Address / port from the setup fields. Takes effect on the next connect.</summary>
        public void Configure(string address, int port)
        {
            if (string.IsNullOrEmpty(address)) return;
            address = address.Trim();
            if (port <= 0 || port > 65535) port = 10001;
            bool changed = false;
            if (address != _address && _address != null) changed = true;
            if (port != _port) changed = true;
            _address = address;
            _port = port;
            if (changed)
            {
                LogText("TARGET set to {0}:{1}", _address, _port);
                if (IsOpen) CloseSocket("target changed");
            }
        }

        /// <summary>True while at least one of the two H1 options asks for the amp link.</summary>
        public void SetEnabled(bool enabled)
        {
            if (_enabled == enabled) return;
            _enabled = enabled;
            LogText(enabled ? "LINK enabled" : "LINK disabled");
            if (enabled) EnsureWorker();
            // when disabled the worker closes the socket on its next pass
        }

        /// <summary>Ask for stand-by. No-op when the state is already known to be stand-by.</summary>
        public void RequestStandby()
        {
            if (!IsOpen)
            {
                LogText("TUNE: standby skipped, amplifier link is not open");
                return;
            }
            _wantState = 1;
            LogText("TUNE: standby requested (amp state {0})", StateName(_ampState));
        }

        /// <summary>Ask for operate. Only ever called when the amp was operating before tune.</summary>
        public void RequestOperate()
        {
            if (!IsOpen)
            {
                LogText("TUNE: operate restore skipped, amplifier link is not open");
                return;
            }
            _wantState = 2;
            LogText("TUNE: operate restore requested (amp state {0})", StateName(_ampState));
        }

        /// <summary>The monotonic clock the state-frame windows are measured on.</summary>
        public long ClockMs
        {
            get { return NowMs(); }
        }

        /// <summary>The first state frame sent inside [sinceMs, sinceMs + windowMs], with the
        /// state it carried. After an 'O' request the amplifier reports the state it had
        /// before the switch first, which is how a state that was unknown at TUNE press can
        /// still be restored properly.</summary>
        public bool TryGetFirstStateAfter(long sinceMs, long windowMs, out bool wasOperate)
        {
            lock (_sync)
            {
                for (int i = 0; i < _stateEventMs.Count; i++)
                {
                    long t = _stateEventMs[i];
                    if (t >= sinceMs && t - sinceMs <= windowMs)
                    {
                        wasOperate = _stateEventOperate[i];
                        return true;
                    }
                }
            }
            wasOperate = false;
            return false;
        }

        public void Dispose()
        {
            _dispose = true;
            _enabled = false;
            try
            {
                if (_worker != null && _worker.IsAlive) _worker.Join(500);
            }
            catch { }
            CloseSocket("dispose");
        }

        #endregion

        #region worker

        private void EnsureWorker()
        {
            if (_worker != null && _worker.IsAlive) return;
            _worker = new Thread(WorkerLoop);
            _worker.IsBackground = true;
            _worker.Name = "H1AmpLan";
            _worker.Start();
        }

        private void WorkerLoop()
        {
            while (!_dispose)
            {
                if (!_enabled)
                {
                    CloseSocket(null);
                    Thread.Sleep(200);
                    continue;
                }

                if (!IsOpen)
                {
                    if (TryOpen())
                    {
                        _lastPoll = 0;
                        _lastWa = -1;
                        _tracedPo = false;
                        _tracedTe = false;
                    }
                    else
                    {
                        CloseSocket(null);
                        Thread.Sleep(5000);
                        continue;
                    }
                }

                // connected: poll and read
                try
                {
                    ServiceRequest();
                    PumpReads(100);

                    long now = NowMs();
                    if (now - _lastPoll >= 1000)
                    {
                        _lastPoll = now;
                        // H1: '#' is the batch query the official manager sends once a second;
                        // it returns the power frame (PO<fwd>,PR<ref>,PI<input>) that feeds the
                        // TX meters. 'T' (temperature) rides along every fifth poll.
                        Send("#");
                        PumpReads(120);
                        _pollCount++;
                        if (_pollCount % 5 == 0)
                        {
                            Send("T");
                            PumpReads(80);
                        }
                        if (_pollCount % 300 == 0) LogText("link alive, amp state {0}", StateName(_ampState));
                    }

                    // H1: while the amplifier is transmitting, ask for the power snapshot four
                    // times a second - the same 'I' query the official manager uses during PTT;
                    // it answers with PO<fwd>,PR<ref>,PI<input>, which feeds the TX meters.
                    if (_pttOn && now - _lastIFast >= 250)
                    {
                        _lastIFast = now;
                        Send("I");
                        PumpReads(60);
                    }
                }
                catch (Exception ex)
                {
                    CloseSocket("io error: " + ex.Message);
                    Thread.Sleep(1500);
                }
            }

            CloseSocket(null);
        }

        private bool TryOpen()
        {
            string address = _address;
            int port = _port;
            try
            {
                TcpClient client = new TcpClient();
                client.NoDelay = true;
                var task = client.ConnectAsync(address, port);
                if (!task.Wait(1500) || !client.Connected)
                {
                    try { client.Close(); } catch { }
                    LogText("CONNECT to {0}:{1} timed out", address, port);
                    return false;
                }

                lock (_sync)
                {
                    _client = client;
                    _stream = client.GetStream();
                    _rx = string.Empty;
                    _poStamp = 0;
                    _prStamp = 0;
                    _stateKnown = false;
                    _pttOn = false;
                    _lastIFast = 0;
                }

                LogText("CONNECTED to {0}:{1}", address, port);
                _verboseUntil = NowMs() + 15000;
                if (!Handshake())
                {
                    LogText("HANDSHAKE failed, link dropped, retrying");
                    CloseSocket("handshake failed");
                    return false;
                }

                LogText("HANDSHAKE ok");
                return true;
            }
            catch (Exception ex)
            {
                LogText("CONNECT to {0}:{1} failed: {2}", address, port, ex.Message);
                return false;
            }
        }

        /// <summary>
        /// 'C' -> CON;, retried like the official manager, then '?' -> status frame and
        /// '1' -> amplifier info. Returns false when the control board stays silent.
        /// </summary>
        private bool Handshake()
        {
            _sawCon = false;
            _sawStatus = false;
            _sawInfo = false;

            for (int attempt = 0; attempt < 3; attempt++)
            {
                Send(attempt == 1 ? " " : "C");
                Thread.Sleep(attempt == 1 ? 400 : 150);
                Send("C");
                if (WaitUntil(delegate { return _sawCon; }, 1500)) break;
                if (attempt == 2) return false;
            }

            // H1: the status and info frames must both arrive. A half handshake leaves the
            // amplifier deaf to every later command - states, tune toggles and the meter
            // queries - so a missing frame means: drop the link and try the whole thing again.
            for (int round = 0; round < 2; round++)
            {
                if (!_sawStatus) { Send("?"); WaitUntil(delegate { return _sawStatus; }, 1200); }
                if (!_sawInfo) { Send("1"); WaitUntil(delegate { return _sawInfo; }, 1200); }
                if (_sawStatus && _sawInfo) break;
            }
            if (!(_sawStatus && _sawInfo))
            {
                LogText("handshake incomplete (ST={0} INFO={1}) - dropping the link and trying again", _sawStatus, _sawInfo);
                return false;
            }

            // the state and the measurements arrive from the per-second polls from now on
            return true;
        }

        private bool WaitUntil(Func<bool> condition, int ms)
        {
            long deadline = NowMs() + ms;
            while (NowMs() < deadline && IsOpen)
            {
                PumpReads(50);
                if (condition()) return true;
                Thread.Sleep(20);
            }
            return condition();
        }

        /// <summary>Send 'O' when a request is pending and the tracked state does not match,
        /// with confirmation from the state frames and one retry.</summary>
        private void ServiceRequest()
        {
            int want = _wantState;
            if (want == 0) return;

            int target = want; // 1 standby, 2 operate
            if (_stateKnown && _ampState == target)
            {
                _wantState = 0;
                LogText("TUNE: amplifier already {0}, nothing sent", StateName(target));
                return;
            }

            for (int attempt = 0; attempt < 2; attempt++)
            {
                long stampBefore;
                lock (_sync) { stampBefore = _stateStamp; }
                Send("O");
                long deadline = NowMs() + 2000;
                while (NowMs() < deadline && IsOpen)
                {
                    PumpReads(50);
                    if (_stateKnown && _ampState == target)
                    {
                        _wantState = 0;
                        LogText("TUNE: amplifier is now {0}, confirmed", StateName(target));
                        return;
                    }
                    long stampNow;
                    lock (_sync) { stampNow = _stateStamp; }
                    if (stampNow != stampBefore) break; // state changed but not to target, toggle again
                    Thread.Sleep(20);
                }
            }

            _wantState = 0;
            LogText("TUNE: amplifier did NOT confirm {0} after two attempts", StateName(target));
        }

        private void Send(string s)
        {
            NetworkStream stream;
            lock (_sync) { stream = _stream; }
            if (stream == null) return;
            byte[] bytes = Encoding.ASCII.GetBytes(s);
            stream.Write(bytes, 0, bytes.Length);
        }

        /// <summary>Read whatever is pending and parse it, for up to ms milliseconds.</summary>
        private void PumpReads(int ms)
        {
            NetworkStream stream;
            lock (_sync) { stream = _stream; }
            if (stream == null) return;

            long deadline = NowMs() + ms;
            do
            {
                if (!stream.DataAvailable)
                {
                    Thread.Sleep(10);
                    continue;
                }
                byte[] buffer = new byte[4096];
                int n = stream.Read(buffer, 0, buffer.Length);
                if (n <= 0)
                {
                    CloseSocket("peer closed");
                    return;
                }
                lock (_sync)
                {
                    _rx += Encoding.ASCII.GetString(buffer, 0, n);
                }
                ParseFrames();
            }
            while (NowMs() < deadline && IsOpen);
        }

        private void ParseFrames()
        {
            while (true)
            {
                string rx;
                lock (_sync) { rx = _rx; }
                int semi = rx.IndexOf(';');
                if (semi < 0)
                {
                    // guard against runaway if the amp ever streams junk
                    if (rx.Length > 512) lock (_sync) { _rx = string.Empty; }
                    return;
                }
                string token;
                lock (_sync)
                {
                    token = _rx.Substring(0, semi);
                    _rx = _rx.Substring(semi + 1);
                }
                HandleToken(token.Trim());
            }
        }

        private void HandleToken(string token)
        {
            if (token.Length == 0) return;

            if (NowMs() < _verboseUntil) LogText("frame: {0}", token);

            if (token == "CON")
            {
                _sawCon = true;
                return;
            }

            switch (token)
            {
                case "OPERATE":
                    SetState(2);
                    return;
                case "STBY":
                    SetState(1);
                    return;
                case "PTTON":
                    _pttOn = true;
                    LogText("PTT on");
                    return;
                case "PTTOFF":
                    _pttOn = false;
                    LogText("PTT off");
                    return;
                case "PAON":
                    LogText("amplifier PA ON");
                    return;
                case "PAOFF":
                    LogText("amplifier PA OFF");
                    return;
            }

            if (token.StartsWith("PO", StringComparison.Ordinal) && token.IndexOf(',') > 0)
            {
                ParsePower(token);
                return;
            }
            if (token.StartsWith("TE", StringComparison.Ordinal))
            {
                if (!_tracedTe)
                {
                    _tracedTe = true;
                    LogText("TE frame: {0}", token);
                }
                return;
            }
            if (token.StartsWith("WA", StringComparison.Ordinal))
            {
                int wa;
                if (int.TryParse(token.Substring(2), out wa) && wa != _lastWa)
                {
                    _lastWa = wa;
                    if (wa % 30 == 0 || wa < 10) LogText("heating, {0} to go", wa);
                }
                return;
            }
            if (token.StartsWith("ST", StringComparison.Ordinal))
            {
                _sawStatus = true;
                LogText("ST frame: {0}", token);
                return;
            }
            if (token.StartsWith("INFO", StringComparison.Ordinal))
            {
                _sawInfo = true;
                LogText("INFO frame: {0}", token);
                return;
            }
            if (token.StartsWith("FR", StringComparison.Ordinal) || token.StartsWith("FA", StringComparison.Ordinal))
                return;
            if (token.StartsWith("FERROR", StringComparison.Ordinal) || token == "HVF" || token == "HF")
            {
                LogText("fault frame: {0}", token);
                return;
            }
            // everything else: PSU readings, warnings and so on - ignored for now
        }

        private void ParsePower(string token)
        {
            // PO<watts>,PR<watts>,PI<watts with decimals>;
            try
            {
                string[] parts = token.Split(',');
                if (parts.Length < 3) return;
                float po = float.Parse(parts[0].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                float pr = float.Parse(parts[1].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                float pi = float.Parse(parts[2].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                lock (_sync)
                {
                    _po = po;
                    _pr = pr;
                    _pi = pi;
                    _poStamp = NowMs();
                    _prStamp = _poStamp;
                }
                if (!_tracedPo)
                {
                    _tracedPo = true;
                    LogText("power frame: {0}", token);
                }
                long now = NowMs();
                if (now - _lastMeterTrace >= 1000)
                {
                    _lastMeterTrace = now;
                    LogText("PWR PO={0:0} W  PR={1:0} W  PI={2:0.0} W", po, pr, pi);
                }
            }
            catch
            {
                // malformed frame, ignore
            }
        }

        private void SetState(int state)
        {
            bool changed;
            lock (_sync)
            {
                changed = !_stateKnown || _ampState != state;
                _ampState = state;
                _stateKnown = true;
                _stateStamp = NowMs();
                _stateEventMs.Add(_stateStamp);
                _stateEventOperate.Add(state == 2);
                if (_stateEventMs.Count > 16)
                {
                    _stateEventMs.RemoveAt(0);
                    _stateEventOperate.RemoveAt(0);
                }
            }
            if (changed) LogText("STATE {0}", StateName(state));
        }

        private static string StateName(int state)
        {
            switch (state)
            {
                case 1: return "STANDBY";
                case 2: return "OPERATE";
                default: return "unknown";
            }
        }

        private void CloseSocket(string reason)
        {
            bool was;
            lock (_sync)
            {
                was = _client != null;
                _stream = null;
                if (_client != null)
                {
                    try { _client.Close(); } catch { }
                    _client = null;
                }
            }
            if (was)
            {
                if (!string.IsNullOrEmpty(reason)) LogText("DISCONNECTED ({0})", reason);
                else LogText("DISCONNECTED");
            }
        }

        #endregion
    }
}
