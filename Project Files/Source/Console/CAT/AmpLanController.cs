// H1: OM Power OM2000A+ remote control over the LAN.
//
// The amplifier carries a Lantronix XPort network interface. Its data port (10001)
// speaks a small mixed ASCII/binary protocol, recovered from the official OM Power
// manager (AmpMan 3.41, decompiled) and re-verified on the bench 2026-10-02:
//
//   'C' handshake    -> "CON;WA<nnn>;"   (WA = heating countdown, 1/s while heating)
//   '?'              -> "ST<flags>;..."  (status: band, antenna)
//   '1'              -> "INFO:...;"      (amplifier type and firmware version)
//   'T' poll, 1/s    -> "TE<nn>;"        (temperature; the manager's steady poll)
//   '#' poll         -> housekeeping batch "IP0;IG0;UP2952;UG123;US0;UH8.5;UM237;TE64;"
//                       (the manager sends this only in its Advanced view)
//   'F' poll         -> "FR<kHz>;"       (measured frequency)
//   'J'              -> "TCVR:IC7100;"   (transceiver configured in the amplifier)
//   'I' poll         -> "IP<n>;"         (input/plate reading; the manager sends 'I'
//                       at 4/s during PTT only to units whose firmware version is
//                       unreadable - legacy amplifiers that do not push meter frames)
//   "OPERATE;", "STBY;", "PTTON;", "PTTOFF;"   state reports, pushed on change
//   'O'              -> toggles stand-by / operate, amp replies with the new state
//
// The TX meter data is a BINARY frame the amplifier pushes of its own accord while
// it transmits: '=' then 8 data bytes then ';' (10 bytes on current firmware):
//
//   [=][PO_L][PO_H][PR/2][PI][IS][FR_L][FR_H][IP/20][;]
//
//   PO = PO_L + 256*PO_H   forward watts     PR = 2 * PRbyte    reflected watts
//   PI = PI byte           input watts       IS = signed byte   screen current
//   FR = FR_L + 256*FR_H   frequency units   IP = 20 * IPbyte   plate current
//
// Legacy units send a 9-byte variant without the IP byte; both shapes are accepted,
// the expected length follows the firmware version in the INFO frame. The official
// manager does NOT poll these values - it synthesizes the text "PO<..>,PR<..>,PI<..>;
// IS<..>;FR<..>;" from the binary frame and parses that internally. The ASCII
// "PO...,PR...,PI..." reply the first version of this class waited for never exists
// on the wire, which is why the meters stayed empty (user report 2026-10-02).
//
// Only ONE TCP client is accepted at a time, so the console holds the link only
// while one of the two H1 amp options is enabled; the OM Power manager software
// cannot be connected at the same time. After a client disconnects the board can
// refuse new sessions for roughly 10-15 seconds.
//
// This class owns the connection, the one-second poll, the receive parser (ASCII
// tokens AND the binary meter frame) and the stand-by / operate requests with
// confirmation and one retry. Everything is traced to amp_lan.log; the raw wire
// bytes (TX and RX) are traced to amp_lan_raw.log - kept while the amplifier
// integration is being verified, remove when the user says so.

using System;
using System.Collections.Generic;
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

        /// <summary>Raw wire trace, every byte sent and received (diagnostic build).</summary>
        private static void LogRaw(string line)
        {
            try
            {
                string text = string.Format("[{0:yyyy-MM-dd HH:mm:ss.fff}] {1}" + Environment.NewLine, DateTime.Now, line);
                string path = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "amp_lan_raw.log");
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

        // worker-thread private receive buffer - may hold raw binary frame bytes, so
        // it is a byte list, NOT the decoded string the first version used
        private readonly List<byte> _rxBytes = new List<byte>();
        // expected length of the binary meter frame (10 with the IP byte, 9 legacy);
        // chosen from the firmware version in the INFO frame
        private volatile int _binFrameLen = 10;
        private long _lastPoll = 0;
        private long _lastMeterTrace = 0;
        private bool _tracedPo = false;
        private bool _tracedTe = false;
        private int _lastWa = -1;
        private int _lastAutoLog = -1000; // H1: rate limit for the amplifier's AUTO: frames
        private volatile bool _autoTune = false; // H1: the amplifier's own autotune armed/running
        private int _autoTuneMs = 0;             // H1: tick of the last autotune frame
        private volatile bool _wantAutoTune = false; // H1: console Auto tune button asked for it
        private volatile int _wantAutoTuneMs = 0;    // H1: when the operate-first nudge started
        private volatile bool _wantAutoTuneStop = false; // H1: right-click on Auto tune, back to manual
        // H1 (2026-10-02 round 2): amplifier power, thermal state and the autotune
        // session surface, decoded from the amplifier's own frames:
        //   'Y' PA on (tube heating) -> WA<n>; countdown -> PAON; when up
        //   'Z' PA off (cooling)     -> COON; / CO<n>; / CT<n>;   -> PAOFF; when down
        //   'L' arm autotune         -> AUTO; then AUTO:<text>;   -> AUTOESC; / STOP;
        //   '8' stop the autotune    -> STOP;   'M' back to manual -> MAN;
        private volatile bool _paKnown = false;
        private volatile bool _paOn = false;
        private volatile bool _heating = false;
        private volatile int _heatSeconds = 0;
        private volatile bool _cooling = false;
        private volatile int _coolSeconds = 0;
        private volatile int _coolTemp = -1;
        private volatile bool _fault = false;
        private long _faultMs = 0;
        private volatile string _autoText = "";      // the amplifier's own AUTO: wording
        private volatile string _autoResult = "";    // DONE / ABORTED / FAILED / STOPPED / EXITED
        private long _autoResultMs = 0;
        private volatile bool _wantPaOn = false;     // PA ON request ('Y')
        private volatile bool _wantPaOff = false;    // PA OFF request ('Z')
        private volatile bool _wantAutoTuneAbort = false; // stop request ('8')
        private volatile bool _stopActive = false;   // an '8' stop is being chased
        private int _stopTries = 0;
        private long _stopNextMs = 0;
        private long _lastArmSentMs = 0;
        private volatile bool _autoStandby = false;  // return the amp to stand-by after a tuning session
        private long _autoStandbyMs = 0;
        private long _autoStandbyFireMs = 0;
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

        /// <summary>H1: the amplifier's own autotune is armed or running. While that state
        /// machine is active the amp ignores mode toggles, so the console runs the third
        /// tune type for it instead: carrier at the drive value, amplifier left untouched.
        /// A silent cancellation on the amp panel expires after 10 minutes without frames.</summary>
        public bool IsAutoTune
        {
            get
            {
                if (!_autoTune) return false;
                if (Environment.TickCount - _autoTuneMs > 600000) return false;
                return true;
            }
        }

        /// <summary>H1: true while an autotune episode is on - from the click (arming, the
        /// operate-first nudge) through the amplifier's WAITING / IN PROGRESS frames until
        /// the console stops chasing a stop request. Drives the Auto tune pill's lit state.</summary>
        public bool AutoTuneBusy
        {
            get
            {
                if (IsAutoTune) return true;
                if (_wantAutoTune || _wantAutoTuneMs != 0) return true;
                if (_stopActive) return true;
                if (_lastArmSentMs != 0 && NowMs() - _lastArmSentMs < 2000) return true;
                return false;
            }
        }

        /// <summary>H1: a stop was requested and the console is waiting for the amp to confirm.</summary>
        public bool AutoTuneStopping
        {
            get { return _stopActive; }
        }

        /// <summary>H1: the amplifier's own wording of its autotune state ("" when idle).</summary>
        public string AutoTuneText
        {
            get { return _autoText; }
        }

        /// <summary>H1: final autotune outcome text, fresh for two minutes.</summary>
        public string AutoTuneResult
        {
            get { return (NowMs() - _autoResultMs <= 120000) ? _autoResult : ""; }
        }

        public bool PaKnown { get { return _paKnown; } }
        public bool PaOn { get { return _paOn; } }
        public bool IsHeating { get { return _heating; } }
        public int HeatSeconds { get { return _heatSeconds; } }
        public bool IsCooling { get { return _cooling; } }
        public int CoolSeconds { get { return _coolSeconds; } }
        public int CoolTemp { get { return _coolTemp; } }
        public bool IsPtt { get { return _pttOn; } }
        public bool FaultFresh { get { return _fault && NowMs() - _faultMs <= 600000; } }

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

        /// <summary>H1: enable the amplifier's own autotune (the third tune type). 'L' is the
        /// command behind the official manager's "Automatic" menu; the amplifier answers AUTO;
        /// and reports it is waiting for input power, which the console's Tune press supplies
        /// at the drive level.</summary>
        public void RequestAutoTune()
        {
            if (!IsOpen)
            {
                LogText("AUTOTUNE: skipped, amplifier link is not open");
                return;
            }
            _wantAutoTune = true;
            LogText("AUTOTUNE: enable requested");
        }

        /// <summary>H1: back to the amplifier's manual tuning (right-click on Auto tune).
        /// 'M' is the official manager's "Manual" menu item; the amp answers MAN;.</summary>
        public void RequestAutoTuneStop()
        {
            if (!IsOpen)
            {
                LogText("AUTOTUNE: back to manual skipped, amplifier link is not open");
                return;
            }
            _wantAutoTuneStop = true;
            LogText("AUTOTUNE: back to manual requested");
        }

        /// <summary>H1: power the amplifier's PA on ('Y' - starts the tube heating) or off
        /// ('Z' - cools down and switches off). Both are the commands behind the official
        /// manager's PA ON / PA OFF button.</summary>
        public void RequestPaOn()
        {
            if (!IsOpen)
            {
                LogText("AMP: PA ON skipped, amplifier link is not open");
                return;
            }
            _wantPaOn = true;
            LogText("AMP: PA ON requested");
        }

        public void RequestPaOff()
        {
            if (!IsOpen)
            {
                LogText("AMP: PA OFF skipped, amplifier link is not open");
                return;
            }
            _wantPaOff = true;
            LogText("AMP: PA OFF requested");
        }

        /// <summary>H1: stop the amplifier's own autotune ('8' - the official manager's stop
        /// button on its automatic-tuning panel) and let the console return the amplifier to
        /// stand-by once the session ends. Retried, and finally exited with '*' (the
        /// manager's EXIT key), for remote operation without walking to the amplifier.</summary>
        public void RequestAutoTuneAbort()
        {
            if (!IsOpen)
            {
                LogText("AUTOTUNE: stop skipped, amplifier link is not open");
                return;
            }
            _wantAutoTuneAbort = true;
            LogText("AUTOTUNE: stop requested (Auto tune toggled off)");
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

        /// <summary>The LAST state frame sent inside [sinceMs, sinceMs + windowMs], with the
        /// state it carried. The tune-restore decision uses this: a window that ends in
        /// STANDBY means the amplifier was operating and the request switched it down
        /// (a hand-set stand-by ends in OPERATE once the toggle brought it up).</summary>
        public bool TryGetLastStateWithin(long sinceMs, long windowMs, out bool wasOperate)
        {
            lock (_sync)
            {
                for (int i = _stateEventMs.Count - 1; i >= 0; i--)
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

        /// <summary>One line into amp_lan.log on behalf of the console (forensics).</summary>
        public void LogNote(string format, params object[] args)
        {
            LogText("TUNE: " + format, args);
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
                        // H1: 'T' is the official manager's steady one-second poll; it
                        // returns the temperature frame "TE<n>;". The TX meter data is
                        // NOT polled - the amplifier pushes the binary '=' meter frame
                        // itself while it transmits.
                        Send("T");
                        PumpReads(120);
                        _pollCount++;
                        if (_pollCount % 300 == 0) LogText("link alive, amp state {0}", StateName(_ampState));
                    }

                    // H1: the amplifier's own autotune, requested from the console's Auto tune
                    // button. 'L' is the command behind the official manager's "Automatic" menu
                    // item - verified on the wire 2026-10-02: the amp answers AUTO; and then
                    // AUTO:WAITING FOR INPUT POWER, exactly like selecting A Tune on its panel.
                    // IMPORTANT (live-probed): 'L' sent while the amp is in STAND-BY is silently
                    // deferred - the user saw nothing. So bring a known stand-by amp to OPERATE
                    // first and wait for it to report, then send 'L'. Unknown state: send 'L'
                    // straight away (the amp itself defers it if needed).
                    if (_wantAutoTune)
                    {
                        bool oper = _stateKnown && _ampState == 2;
                        if (_stateKnown && !oper && _wantAutoTuneMs == 0)
                        {
                            _wantAutoTuneMs = Environment.TickCount;
                            LogText("AUTOTUNE: amplifier is in stand-by - requesting operate first");
                            RequestOperate();
                        }
                        else if (oper || _wantAutoTuneMs == 0 || Environment.TickCount - _wantAutoTuneMs > 6000)
                        {
                            _wantAutoTune = false;
                            _wantAutoTuneMs = 0;
                            Send("L");
                            _lastArmSentMs = NowMs();
                            PumpReads(150);
                            LogText("AUTOTUNE: 'L' sent to the amplifier (automatic tuning armed)");
                        }
                    }
                    if (_wantAutoTuneStop)
                    {
                        _wantAutoTuneStop = false;
                        Send("M");
                        PumpReads(150);
                        LogText("AUTOTUNE: 'M' sent to the amplifier (back to manual)");
                    }

                    // H1 round 2: the amplifier's PA power switch ('Y' / 'Z') - the two
                    // commands behind the official manager's PA ON / PA OFF button
                    if (_wantPaOn)
                    {
                        _wantPaOn = false;
                        Send("Y");
                        PumpReads(200);
                        LogText("AMP: 'Y' sent (PA ON, tube heating starts)");
                    }
                    if (_wantPaOff)
                    {
                        _wantPaOff = false;
                        Send("Z");
                        PumpReads(200);
                        LogText("AMP: 'Z' sent (PA OFF, cooling starts)");
                    }

                    // H1 round 2: the Auto tune pill toggled off - chase the stop until the
                    // amplifier leaves its tuning session ('8' is the manager's stop; three
                    // attempts, then 'M' back to manual as the last resort)
                    if (_wantAutoTuneAbort)
                    {
                        _wantAutoTuneAbort = false;
                        _stopActive = true;
                        _stopTries = 0;
                        _stopNextMs = 0;
                    }
                    if (_stopActive)
                    {
                        if (!IsAutoTune && _stopTries > 0)
                        {
                            _stopActive = false;
                            LogText("AUTOTUNE: amplifier confirmed the stop (left the tuning session)");
                        }
                        else if (NowMs() >= _stopNextMs)
                        {
                            if (_stopTries == 0)
                            {
                                // '8' = the manager's STOP button on the automatic tuning panel
                                // (acts on a running tune; the amplifier ignores it while it is
                                // only waiting for input power - probed live 2026-10-02)
                                _stopTries = 1;
                                Send("8");
                                PumpReads(200);
                                LogText("AUTOTUNE: '8' sent (stop the tuning process)");
                                _stopNextMs = NowMs() + 3000;
                            }
                            else if (_stopTries == 1)
                            {
                                // '*' = the manager's EXIT button (all three of its tuning
                                // panels) - the session exit, the remote equivalent of the
                                // amplifier panel's own EXIT key
                                _stopTries = 2;
                                Send("*");
                                PumpReads(250);
                                LogText("AUTOTUNE: '*' sent (exit the tuning session)");
                                _stopNextMs = NowMs() + 3000;
                            }
                            else if (_stopTries == 2)
                            {
                                _stopTries = 3;
                                Send("*");
                                PumpReads(250);
                                LogText("AUTOTUNE: '*' repeated (exit the tuning session)");
                                _stopNextMs = NowMs() + 6000;
                            }
                            else
                            {
                                _stopActive = false;
                                LogText("AUTOTUNE: stop NOT confirmed - the amplifier still reports its autotune");
                            }
                        }
                    }

                    // H1 round 2: a tuning episode ended - the amplifier must not linger in
                    // OPERATE. Return it to stand-by (hold while transmitting; give up after
                    // two minutes so a deaf amplifier cannot latch a stale intention).
                    if (_autoStandby)
                    {
                        if (NowMs() - _autoStandbyMs > 120000)
                        {
                            _autoStandby = false;
                            LogText("AMP: stand-by return gave up (timed out)");
                        }
                        else if (_stateKnown && _ampState != 2)
                        {
                            _autoStandby = false;
                            LogText("AMP: amplifier is in stand-by");
                        }
                        else if (_stateKnown && !_pttOn && NowMs() >= _autoStandbyFireMs)
                        {
                            _autoStandbyFireMs = NowMs() + 5000;
                            RequestStandby();
                            LogText("AMP: returning the amplifier to stand-by after the tuning session");
                        }
                    }

                    // H1: the 'I' fallback poll was REMOVED 2026-10-02. Confirmed live: this
                    // amplifier pushes the binary '=' meter frame on its own while it
                    // transmits, and its controller is command-load sensitive - during its
                    // internal AUTOTUNE episodes extra traffic delays the mode toggle (a
                    // stand-by request landed 13 s late, live 2026-10-02). The manager only
                    // ever sends 'I' to legacy units without a readable firmware version;
                    // do not reintroduce it without a legacy unit to test against.
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
                    // a fresh session starts clean - no request from a previous
                    // session may survive into it (a standby request left pending by a
                    // tune press during a half handshake must not fire minutes later:
                    // user hit this live 2026-10-02, the amp was parked in stand-by)
                    _wantState = 0;
                    _poStamp = 0;
                    _prStamp = 0;
                    _stateKnown = false;
                    _pttOn = false;
                    _lastIFast = 0;
                }
                _rxBytes.Clear();

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
            LogRaw(string.Format("TX: {0}", s == " " ? "[space]" : s));
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
                for (int i = 0; i < n; i++) _rxBytes.Add(buffer[i]);
                LogRaw(string.Format("RX {0,3} B: {1}", n, BitConverter.ToString(buffer, 0, n).Replace("-", " ")));
                ProcessFrames();
            }
            while (NowMs() < deadline && IsOpen);
        }

        /// <summary>
        /// Split the receive stream into the binary meter frame ('=' ... ';') and the
        /// ASCII ";"-terminated tokens, the way the official manager's reader does.
        /// </summary>
        private void ProcessFrames()
        {
            while (true)
            {
                if (_rxBytes.Count == 0) return;

                if (_rxBytes[0] == (byte)'=')
                {
                    if (_binFrameLen >= 10)
                    {
                        // modern firmware: '=' + 8 data bytes + ';'
                        if (_rxBytes.Count < 10) return; // wait for the rest of the frame
                        if (_rxBytes[9] == (byte)';')
                        {
                            byte[] frame = _rxBytes.GetRange(0, 10).ToArray();
                            _rxBytes.RemoveRange(0, 10);
                            ParseBinaryFrame(frame, 10);
                            continue;
                        }
                    }
                    else
                    {
                        // legacy firmware expected: accept both shapes, prefer the
                        // modern one once it is complete
                        if (_rxBytes.Count >= 10 && _rxBytes[9] == (byte)';')
                        {
                            byte[] frame = _rxBytes.GetRange(0, 10).ToArray();
                            _rxBytes.RemoveRange(0, 10);
                            ParseBinaryFrame(frame, 10);
                            continue;
                        }
                        if (_rxBytes.Count >= 9 && _rxBytes[8] == (byte)';')
                        {
                            byte[] frame = _rxBytes.GetRange(0, 9).ToArray();
                            _rxBytes.RemoveRange(0, 9);
                            ParseBinaryFrame(frame, 9);
                            continue;
                        }
                        if (_rxBytes.Count < 10) return; // wait for more bytes
                    }
                    // no ';' where the frame must end: not a meter frame after all -
                    // drop the byte and resync
                    LogText("bin frame lost sync, dropping a byte");
                    _rxBytes.RemoveAt(0);
                    continue;
                }

                int semi = _rxBytes.IndexOf((byte)';');
                if (semi < 0)
                {
                    // guard against runaway if the amp ever streams junk
                    if (_rxBytes.Count > 512)
                    {
                        _rxBytes.Clear();
                        LogText("rx overflow, buffer cleared");
                    }
                    return;
                }
                string token = Encoding.ASCII.GetString(_rxBytes.GetRange(0, semi).ToArray()).Trim();
                _rxBytes.RemoveRange(0, semi + 1);
                HandleToken(token);
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
                    _paKnown = true; _paOn = true; _heating = false; _heatSeconds = 0;
                    LogText("amplifier PA ON (heating complete, ready)");
                    return;
                case "PAOFF":
                    _paKnown = true; _paOn = false; _cooling = false; _coolSeconds = 0; _coolTemp = -1;
                    LogText("amplifier PA OFF (cooling complete)");
                    return;
                case "COON":
                    _cooling = true;
                    LogText("amp: cooling started");
                    return;
                case "STOP":
                    LogText("amp: STOP acknowledged");
                    return;
                case "AGAIN":
                    LogText("amp: AGAIN acknowledged");
                    return;
                case "MAN":
                    LogText("amp: MAN (manual tuning mode)");
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
                if (int.TryParse(token.Substring(2), out wa))
                {
                    _heating = wa > 0;
                    _heatSeconds = wa;
                    if (wa != _lastWa)
                    {
                        _lastWa = wa;
                        if (wa % 30 == 0 || wa < 10) LogText("heating, {0} to go", wa);
                    }
                }
                return;
            }
            if (token.StartsWith("CO", StringComparison.Ordinal) && token.Length > 2 && char.IsDigit(token[2]))
            {
                // "CO<nn>;" - the cooling countdown while the amplifier powers the PA down
                int co;
                if (int.TryParse(token.Substring(2), out co))
                {
                    _cooling = true;
                    _coolSeconds = co;
                    if (co % 30 == 0 || co < 10) LogText("cooling, {0} to go", co);
                }
                return;
            }
            if (token.StartsWith("CT", StringComparison.Ordinal) && token.Length > 2 && char.IsDigit(token[2]))
            {
                // "CT<nn>;" - the cooling temperature; the PA powers off below 40 C
                int ct;
                if (int.TryParse(token.Substring(2), out ct))
                {
                    _cooling = true;
                    _coolTemp = ct;
                    if (ct % 10 == 0) LogText("cooling, {0} C", ct);
                }
                return;
            }
            if (token.StartsWith("AUTO", StringComparison.Ordinal))
            {
                // H1: the amplifier's own automatics - "AUTO:AUTOTUNE IN PROGRESS;",
                // "AUTO:WAITING FOR INPUT POWER;", "AUTO:AUTOTUNE ABORTED;", "AUTOESC;".
                // While that state machine runs the controller ignores mode commands, so
                // the console leaves the amplifier alone and only supplies the carrier.
                // The amplifier's own wording is kept for the OM2000A+ block.
                int n2 = Environment.TickCount;
                if (n2 - _lastAutoLog >= 1000)
                {
                    _lastAutoLog = n2;
                    LogText("amp: {0}", token);
                }
                string autoText = token.StartsWith("AUTO:", StringComparison.Ordinal)
                    ? token.Substring(5).Trim() : "";
                if (autoText.Length > 0) _autoText = autoText;
                bool was = _autoTune;
                if (token.IndexOf("IN PROGRESS", StringComparison.OrdinalIgnoreCase) >= 0 ||
                    token.IndexOf("WAITING", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    _autoTune = true;
                    _autoTuneMs = n2;
                    if (!was)
                    {
                        _stopTries = 0;
                    }
                }
                else if (token.IndexOf("ABORTED", StringComparison.OrdinalIgnoreCase) >= 0 ||
                         token.IndexOf("FAILED", StringComparison.OrdinalIgnoreCase) >= 0 ||
                         token.IndexOf("DONE", StringComparison.OrdinalIgnoreCase) >= 0 ||
                         token.IndexOf("COMPLETE", StringComparison.OrdinalIgnoreCase) >= 0 ||
                         token.IndexOf("ESC", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    _autoTune = false;
                }
                if (_autoTune != was)
                {
                    LogText("amp: autotune {0}", _autoTune ? "active" : "finished");
                    if (!_autoTune)
                    {
                        // the session ended: name the outcome and return the amplifier to
                        // stand-by (user requirement 2026-10-02) - a tuning episode must
                        // not leave the amplifier parked in OPERATE
                        string res;
                        if (_autoText.IndexOf("FAILED", StringComparison.OrdinalIgnoreCase) >= 0) res = "AUTOTUNE FAILED";
                        else if (_stopTries > 0 || _stopActive) res = "AUTOTUNE STOPPED";
                        else if (_autoText.IndexOf("ABORTED", StringComparison.OrdinalIgnoreCase) >= 0) res = "AUTOTUNE ABORTED";
                        else if (_autoText.IndexOf("DONE", StringComparison.OrdinalIgnoreCase) >= 0 ||
                                 _autoText.IndexOf("COMPLETE", StringComparison.OrdinalIgnoreCase) >= 0) res = "AUTOTUNE DONE";
                        else res = "AUTOTUNE EXITED";
                        _autoResult = res;
                        _autoResultMs = NowMs();
                        _autoStandby = true;
                        _autoStandbyMs = NowMs();
                        _autoStandbyFireMs = 0;
                        LogText("AUTOTUNE: session ended - {0}", res);
                    }
                }
                return;
            }
            if (token.StartsWith("ST", StringComparison.Ordinal))
            {
                _sawStatus = true;
                // H1: the ST frame encodes the amplifier state directly
                // ("ST7100- 12 17M" = ST + [1 char] + PA-on digit + operate digit +
                // PTT digit + mode char + antenna text - the same mapping the official
                // manager's regex "ST([^;]{1})(\d)(\d)(\d)([01-])([^;]{0,51});" uses).
                // Parsing it settles the operate/stand-by state at every connect, which
                // the tune-restore logic depends on.
                if (token.Length >= 8 && (token[3] == '0' || token[3] == '1')
                    && (token[4] == '0' || token[4] == '1') && (token[5] == '0' || token[5] == '1'))
                {
                    _paKnown = true;
                    if (token[3] == '1')
                    {
                        _paOn = true;
                        if (_heating) { _heating = false; _heatSeconds = 0; LogText("amp: PA ready (heating complete)"); }
                    }
                    else if (!_heating)
                    {
                        _paOn = false;
                        if (_cooling) { _cooling = false; _coolSeconds = 0; _coolTemp = -1; }
                    }
                    SetState(token[4] == '1' ? 2 : 1);
                    LogText("ST: {0} -> PA {1}, {2}, PTT {3}", token,
                        token[3] == '1' ? "on" : "off",
                        token[4] == '1' ? "operate" : "standby",
                        token[5] == '1' ? "on" : "off");
                }
                else
                {
                    LogText("ST frame (not parsed): {0}", token);
                }
                return;
            }
            if (token.StartsWith("INFO", StringComparison.Ordinal))
            {
                _sawInfo = true;
                LogText("INFO frame: {0}", token);
                // the firmware version decides the binary meter frame length: a
                // readable version means the 8-data-byte frame ('=' PO PR PI IS FR IP
                // ';'), an unreadable one the legacy 7-data-byte frame
                try
                {
                    string[] parts = token.Split(',');
                    float v;
                    _binFrameLen = (parts.Length >= 4 && float.TryParse(parts[3],
                        System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out v)) ? 10 : 9;
                }
                catch
                {
                    _binFrameLen = 9;
                }
                return;
            }
            if (token.StartsWith("FR", StringComparison.Ordinal))
                return;
            if (token.StartsWith("FA", StringComparison.Ordinal))
            {
                // "FA<1-15>;" are the amplifier's fault codes (the manager's CFau table)
                int fa;
                if (token.Length <= 4 && int.TryParse(token.Substring(2), out fa))
                {
                    _fault = true;
                    _faultMs = NowMs();
                    LogText("FAULT from amplifier: FA{0}", fa);
                }
                return;
            }
            if (token.StartsWith("WG", StringComparison.Ordinal))
            {
                LogText("warning from amplifier: {0}", token);
                return;
            }
            if (token.StartsWith("FERROR", StringComparison.Ordinal) || token == "HVF" || token == "HF")
            {
                _fault = true;
                _faultMs = NowMs();
                LogText("fault frame: {0}", token);
                return;
            }
            // everything else: PSU readings, warnings and so on - ignored for now
        }

        /// <summary>
        /// The binary meter frame the amplifier pushes while transmitting:
        /// '=' PO_L PO_H PR/2 PI IS FR_L FR_H [IP/20] ';'. Offsets follow the official
        /// manager's decoder byte for byte.
        /// </summary>
        private void ParseBinaryFrame(byte[] f, int len)
        {
            int po = f[1] + f[2] * 256;
            int pr = f[3] * 2;
            int pi = f[4];
            int isVal = ((f[5] & 0x80) != 0) ? -((f[5] ^ 0xFF) + 1) : f[5];
            int fr = f[6] + f[7] * 256;
            string note;
            if (len >= 10)
            {
                int ip = f[8] * 20;
                note = string.Format("power frame (bin {0}B): PO={1}W PR={2}W PI={3}W IS={4} FR={5} IP={6}", len, po, pr, pi, isVal, fr, ip);
            }
            else
            {
                note = string.Format("power frame (bin {0}B): PO={1}W PR={2}W PI={3}W IS={4} FR={5}", len, po, pr, pi, isVal, fr);
            }
            StorePower(po, pr, pi, note);
        }

        /// <summary>Kept for compatibility - the wire protocol of this amplifier never
        /// carries the ASCII PO/PR/PI form (the official manager synthesizes it from the
        /// binary frame internally), so this path should stay silent.</summary>
        private void ParsePower(string token)
        {
            try
            {
                string[] parts = token.Split(',');
                if (parts.Length < 3) return;
                int po = (int)float.Parse(parts[0].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                int pr = (int)float.Parse(parts[1].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                int pi = (int)float.Parse(parts[2].Substring(2), System.Globalization.CultureInfo.InvariantCulture);
                StorePower(po, pr, pi, "power frame (ascii): " + token);
            }
            catch
            {
                // malformed frame, ignore
            }
        }

        private void StorePower(int po, int pr, int pi, string firstNote)
        {
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
                LogText(firstNote);
            }
            long now = NowMs();
            if (now - _lastMeterTrace >= 1000)
            {
                _lastMeterTrace = now;
                LogText("PWR PO={0} W  PR={1} W  PI={2} W", po, pr, pi);
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
                // the link died - any request that was never actioned is void and must
                // not fire after the next reconnect (user hit this live 2026-10-02:
                // a tune press during a half handshake parked the amp in stand-by
                // minutes later on an unrelated session)
                _wantState = 0;
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
