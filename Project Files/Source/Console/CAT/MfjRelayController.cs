// ---------------------------------------------------------------------------
// MfjRelayController.cs
//
// H1 (user 2026-10-04): the station controller on a USB Arduino - the MFJ-998R
// power relay and the PTT out line. Sketch:
// C:\Thetis\tools\mfj_power_cycle\mfj_power_cycle.ino
//
// Commands are ONE PER LINE at 115200 8N1, each answered with exactly one
// "OK ..." line; the board also speaks unprompted when the operator's PTT input
// moves ("EVT PTT IN 0/1 ..."):
//
//     ON / OFF      the MFJ998R power relay (energised = tuner power cut)
//     1 / 0         PTT OUT to ground / released  (short form - PTT is the one
//                   path where the line length on the wire IS latency)
//     STATUS / PING
//
// PTT OUT follows MOX or Tune. It is NOT polled and the worker NEVER touches a
// Thetis control: the console computes the state on the UI thread at the moment
// the control changes and hands the bool in with PttSet(), which also wakes the
// worker. Reading chkMOX/chkTUN from the worker instead costs Control.Invoke on
// every read - measured at 500-900 ms of PTT delay while the UI thread was busy
// handling MOX itself. The worker still wakes every 2 ms on its own, which is
// what covers the relay and the serial input.
//
// The port is opened on the first need and then HELD OPEN. Opening it asserts
// DTR and resets the board (about 1.2 s of bootloader), so closing it when idle
// would put that cost in front of the next MOX press.
//
// FAIL-SAFE: the tuner is fed through the relay's NORMALLY-CLOSED contact, so
// DE-ENERGISED means POWERED, and an idle or dead board leaves PTT OUT HIGH =
// un-keyed. Never restore relay or PTT state after a reconnect.
// ---------------------------------------------------------------------------

using System;
using System.Diagnostics;
using System.IO.Ports;
using System.Text;
using System.Threading;

namespace Thetis
{
    public sealed class MfjRelayController : IDisposable
    {
        /// <summary>milliseconds on a monotonic clock (no 24.9 day wrap).</summary>
        private static long NowMs()
        {
            return (long)(Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency * 1000.0);
        }

        private static void LogText(string format, params object[] args)
        {
            try
            {
                string text = string.Format("[{0:yyyy-MM-dd HH:mm:ss.fff}] {1}" + Environment.NewLine, DateTime.Now,
                    (args != null && args.Length > 0) ? string.Format(format, args) : format);
                string path = System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "mfj_relay.log");
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

        private readonly Console _console;
        private readonly object _sync = new object();
        private readonly Thread _worker;
        private readonly AutoResetEvent _wake = new AutoResetEvent(false);
        private volatile bool _dispose;
        private volatile bool _enabled;              // the MFJ998R Power tick
        private volatile string _portName = "COM7";  // the USB controller
        private SerialPort _port;
        private readonly StringBuilder _rx = new StringBuilder();

        private bool _relayOn;                       // last state the board confirmed
        private bool _pttSentLow;                    // last PTT OUT we asked for
        private bool _pttHaveSent;
        private long _lastOpenAttemptMs;
        private int _sentDebounceMs = -1;             // DEB window the board has been told about
        private volatile bool _pttInEnabled;          // the external PTT line may key MOX
        private volatile int _pttInDebounceMs = 30;   // its debounce window, in the firmware
        private bool _hasPendingMox;                  // set on the worker, applied on the UI thread
        private bool _pendingMox;
        private long _pttNotifyMs = -1;              // when the console last called in

        /// <summary>MOX or Tune - set by the console.</summary>
        private volatile bool _wantPtt;               // computed by the console, never read here

        public MfjRelayController(Console owner)
        {
            _console = owner;
            _worker = new Thread(Worker);
            _worker.IsBackground = true;
            _worker.Name = "MfjRelay";
            _worker.Start();
            LogText("MFJ998R controller started (idle, port {0})", _portName);
        }

        /// <summary>Called from the console's MOX and Tune control events, ON THE UI
        /// THREAD, with the state already worked out:  MOX || _tuning || chkTUN.Checked.
        /// Passing the bool in is the whole point - the worker must not evaluate those
        /// controls itself, because each read marshals through Control.Invoke and blocks
        /// the pump for as long as the UI thread is busy.</summary>
        public void PttSet(bool transmitting)
        {
            _wantPtt = transmitting;
            _pttNotifyMs = NowMs();
            try { _wake.Set(); }
            catch { }
        }

        /// <summary>The external PTT option. Off by default: it gates TX from a wire, so
        /// the operator turns it on deliberately.</summary>
        public bool PttInEnabled
        {
            get { return _pttInEnabled; }
            set
            {
                if (_pttInEnabled == value) return;
                _pttInEnabled = value;
                LogText("external PTT line {0}", value ? "may key MOX" : "will not key MOX");
            }
        }

        /// <summary>How long the board's PTT-in pin must hold a new level before it is
        /// believed. The window runs in the firmware; this just pushes it over.</summary>
        public int PttInDebounceMs
        {
            get { return _pttInDebounceMs; }
            set
            {
                int v = value;
                if (v < 0) v = 0;
                if (v > 1000) v = 1000;
                if (v == _pttInDebounceMs) return;
                _pttInDebounceMs = v;
                LogText("external PTT debounce set to {0} ms", v);
            }
        }

        public string PortName
        {
            get { return _portName; }
            set
            {
                string v = (value ?? "").Trim();
                if (v.Length == 0) return;
                if (string.Equals(v, _portName, StringComparison.OrdinalIgnoreCase)) return;
                lock (_sync) { ClosePort("port changed"); }
                _portName = v;
                LogText("MFJ998R port set to {0}", v);
            }
        }

        /// <summary>The MFJ998R Power control. The relay is held energised while this
        /// is true and released when it clears; nothing survives the link.</summary>
        public bool Enabled
        {
            get { return _enabled; }
            set
            {
                if (_enabled == value) return;
                _enabled = value;
                LogText("MFJ998R relay {0} requested", value ? "ON" : "OFF");
            }
        }

        public bool RelayOn { get { return _relayOn; } }

        public bool LinkOpen
        {
            get { lock (_sync) { return _port != null && _port.IsOpen; } }
        }

        public void LogNote(string format, params object[] args)
        {
            LogText(format, args);
        }

        private void Worker()
        {
            while (!_dispose)
            {
                try
                {
                    Pump();
                }
                catch (Exception ex)
                {
                    LogText("MFJ998R worker error: {0}", ex.Message);
                    lock (_sync) { ClosePort("error"); }
                }
                // Woken by PttNotify the instant MOX or Tune changes; otherwise this
                // is the tick that carries the relay and the serial input.
                _wake.WaitOne(2);
            }
            lock (_sync) { ClosePort("dispose"); }
        }

        private void Pump()
        {
            // Apply a PTT-in edge OUTSIDE the lock: the console call marshals to the UI
            // thread (BeginInvoke, so it never blocks us) and must not be holding _sync
            // while the console may be waiting on something that wants it.
            if (_hasPendingMox)
            {
                bool wanted = _pendingMox;
                _hasPendingMox = false;
                try { if (_console != null) _console.H1MfjPttInSet(wanted); }
                catch (Exception ex) { LogText("external PTT apply failed: {0}", ex.Message); }
            }

            bool wantPtt = _wantPtt;   // set by the console on the UI thread; never polled here

            lock (_sync)
            {
                if (_port != null && _port.IsOpen) ReadLinesLocked();

                bool needPort = _enabled || wantPtt || !_pttHaveSent;
                if (needPort && (_port == null || !_port.IsOpen))
                {
                    if (NowMs() - _lastOpenAttemptMs < 2000) return;
                    _lastOpenAttemptMs = NowMs();
                    if (!OpenPortLocked()) return;
                    _pttHaveSent = false;   // a reset board starts un-keyed and relay off
                    _relayOn = false;
                }

                if (_port == null || !_port.IsOpen) return;

                // PTT out first: it is the time-critical one, and it goes on the wire
                // in its SHORT form so the frame is two bytes and not six.
                if (!_pttHaveSent || wantPtt != _pttSentLow)
                {
                    long notify = _pttNotifyMs;
                    long t0 = NowMs();
                    if (WriteLocked(wantPtt ? "1" : "0"))
                    {
                        long t1 = NowMs();
                        _pttSentLow = wantPtt;
                        _pttHaveSent = true;
                        LogText("PTT OUT -> {0} ({1})  write {2} ms{3}",
                            wantPtt ? "LOW" : "high",
                            wantPtt ? "MOX or Tune" : "idle",
                            t1 - t0,
                            notify >= 0 ? string.Format(", {0} ms after the console's event", t1 - notify) : "");
                    }
                }

                // push the debounce window once, and again after every reset
                if (_sentDebounceMs != _pttInDebounceMs)
                {
                    if (WriteLocked("DEB " + _pttInDebounceMs.ToString())) _sentDebounceMs = _pttInDebounceMs;
                }

                // relay
                if (_enabled && !_relayOn)
                {
                    if (WriteLocked("ON")) _relayOn = true;
                }
                else if (!_enabled && _relayOn)
                {
                    if (WriteLocked("OFF")) _relayOn = false;
                }
            }
        }

        private void ReadLinesLocked()
        {
            try
            {
                int n = _port.BytesToRead;
                if (n <= 0) return;
                _rx.Append(_port.ReadExisting());
            }
            catch (Exception ex)
            {
                LogText("MFJ998R read failed: {0}", ex.Message);
                ClosePort("read failed");
                return;
            }

            string all = _rx.ToString();
            int nl;
            while ((nl = all.IndexOf('\n')) >= 0)
            {
                string line = all.Substring(0, nl).Trim();
                all = all.Substring(nl + 1);
                if (line.Length > 0) HandleLine(line);
            }
            _rx.Length = 0;
            if (all.Length > 0) _rx.Append(all);
            if (_rx.Length > 512) _rx.Length = 0;   // garbage guard
        }

        private void HandleLine(string line)
        {
            if (line.StartsWith("OK READY", StringComparison.OrdinalIgnoreCase))
            {
                // A board that has just come up is un-keyed and de-energised whatever the
                // console last asked for - the reset threw that away. Force the pump to
                // re-assert both so the console's idea and the pins agree again.
                _pttHaveSent = false;
                _relayOn = false;
                _sentDebounceMs = -1;   // the board came up on its own default
                LogText("board ready: '{0}' - state to be re-sent", line);
                return;
            }
            // "OK PTT OUT 1 us=NNN" - the board's own processing time, its share of
            // the MOX -> pin-low path.
            if (line.StartsWith("OK PTT OUT", StringComparison.OrdinalIgnoreCase))
            {
                int at = line.IndexOf("us=", StringComparison.OrdinalIgnoreCase);
                string us = at >= 0 ? line.Substring(at + 3).Trim() : "?";
                LogText("PTT OUT confirmed by the board: '{0}' (board delay {1} us)", line, us);
                return;
            }
            if (line.StartsWith("EVT PTT IN", StringComparison.OrdinalIgnoreCase))
            {
                // The operator's PTT line moved, already debounced on the board. Hand it to
                // the console, which owns MOX and the power-off gate.
                LogText("external PTT line: '{0}'", line);
                if (_pttInEnabled)
                {
                    _pendingMox = line.IndexOf(" IN 0", StringComparison.OrdinalIgnoreCase) >= 0;
                    _hasPendingMox = true;
                    try { _wake.Set(); } catch { }
                }
                return;
            }
            if (line.StartsWith("OK RELAY", StringComparison.OrdinalIgnoreCase))
            {
                LogText("relay confirmed by the board: '{0}'", line);
                return;
            }
            LogText("board: '{0}'", line);
        }

        private bool OpenPortLocked()
        {
            if (_port != null && _port.IsOpen) return true;
            try
            {
                SerialPort sp = new SerialPort(_portName, 115200, Parity.None, 8, StopBits.One);
                sp.ReadTimeout = 1;
                sp.WriteTimeout = 400;
                sp.NewLine = "\n";
                sp.DtrEnable = true;
                sp.RtsEnable = false;
                sp.Open();
                // Opening the port asserts DTR and resets the board through the USB
                // bridge. Wait for the board's own READY line before writing anything:
                // the old bootloader holds the line for about a second after the reset
                // and eats whatever arrives in that window - that is how a relay request
                // went missing while the console believed it had been carried out.
                sp.ReadTimeout = 400;
                long deadline = NowMs() + 5000;
                while (NowMs() < deadline)
                {
                    try
                    {
                        string readyLine = sp.ReadLine().Trim();
                        if (readyLine.StartsWith("OK READY", StringComparison.OrdinalIgnoreCase))
                        {
                            LogText("board ready: '{0}'", readyLine);
                            break;
                        }
                    }
                    catch (TimeoutException) { }
                }
                sp.ReadTimeout = 1;
                try { sp.DiscardInBuffer(); } catch { }
                _rx.Length = 0;
                _port = sp;
                _relayOn = false;
                _pttSentLow = false;
                LogText("MFJ998R link open on {0} (board reset by the port open)", _portName);
                return true;
            }
            catch (Exception ex)
            {
                LogText("MFJ998R cannot open {0}: {1}", _portName, ex.Message);
                _port = null;
                return false;
            }
        }

        private void ClosePort(string reason)
        {
            if (_port == null) return;
            try { if (_port.IsOpen) _port.Close(); }
            catch { }
            try { _port.Dispose(); }
            catch { }
            _port = null;
            _relayOn = false;
            LogText("MFJ998R link closed ({0})", reason);
        }

        private bool WriteLocked(string command)
        {
            if (_port == null || !_port.IsOpen) return false;
            try
            {
                _port.Write(command + "\n");
                return true;
            }
            catch (Exception ex)
            {
                LogText("MFJ998R write '{0}' failed: {1}", command, ex.Message);
                ClosePort("write failed");
                return false;
            }
        }

        public void Dispose()
        {
            _dispose = true;
            _enabled = false;
            try { _wake.Set(); } catch { }
            lock (_sync) { ClosePort("dispose"); }
        }
    }
}
