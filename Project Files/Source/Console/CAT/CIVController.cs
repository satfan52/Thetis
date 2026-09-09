//=================================================================
// CIVController.cs
//=================================================================
// Controller for native Icom CI-V serial communication in Thetis.
// Supports IC-7100 VFO A/B, PTT, Modulation Modes, IF Filter Width,
// Split Mode, and Full Duplex TX Frequency steering.
//=================================================================

using System;
using System.IO.Ports;
using System.Threading;
using System.Collections.Generic;
using System.Diagnostics;

namespace Thetis
{
    public class CIVController : IDisposable
    {
        #region Fields & State

        private readonly Console _console;
        private SerialPort _serialPort;
        private readonly object _portLock = new object();
        private readonly object _stateLock = new object();

        // Radio Addresses
        private byte _radioAddr = CIVProtocol.DEFAULT_RADIO_ADDR;
        private byte _hostAddr = CIVProtocol.DEFAULT_HOST_ADDR;

        // Port Parameters
        private string _portName = "COM10";
        private int _baudRate = 19200;

        // Features Configuration
        private bool _transceiveEnabled = true;
        private bool _syncSplitAndFullDuplex = true;
        private bool _syncFilterWidth = true;
        private bool _syncPTT = false;

        // Flood Control / Rate Limiting
        private Timer _floodTimer;
        private const int FLOOD_INTERVAL_MS = 50; // 20 Hz update rate for VFO tuning
        private bool _freqChangePending = false;
        private bool _vfoBChangePending = false;
        private bool _modeChangePending = false;
        private bool _splitChangePending = false;

        // Pending values to transmit
        private double _pendingVfoAFreq = 0.0;
        private double _pendingVfoBFreq = 0.0;
        private double _pendingTxFreq = 0.0;
        private DSPMode _pendingMode = DSPMode.USB;
        private int _pendingFilterWidth = 2700;
        private bool _pendingSplit = false;

        // Last confirmed sent states (Echo / Ping-Pong prevention)
        private double _lastSentVfoAFreq = -1.0;
        private double _lastSentVfoBFreq = -1.0;
        private CIVMode _lastSentCivMode = (CIVMode)0xFF;
        private CIVFilter _lastSentCivFilter = (CIVFilter)0xFF;
        private CIVDataMode _lastSentDataMode = (CIVDataMode)0xFF;
        private byte _lastSentFilterWidthCode = 0xFF;
        private bool _lastSentPtt = false;
        private bool _lastSentSplit = false;
        private bool _actualRadioSplit = false;
        private int _pttPollCounter = 0;
        private int _splitPollCounter = 0;
        private byte _currentRadioSelectedVfo = CIVProtocol.VFO_A;
        private long _lastTxReleaseTime = 0;
        private volatile bool _isSwappingVfo = false;
        private long _lastVfoSwapTime = 0;
        private readonly object _vfoSwapLock = new object();

        // Loop suppression when Thetis is being updated from the radio
        private bool _suppressOutgoingUpdates = false;

        // Serial reception buffer
        private readonly List<byte> _rxBuffer = new List<byte>(512);

        // Echo history for filtering out reflected TX frames on 1-wire buses
        private readonly Queue<byte[]> _recentSentFrames = new Queue<byte[]>();
        private readonly object _echoLock = new object();

        private bool _isDisposed = false;

        #endregion

        #region Properties

        public string PortName
        {
            get { return _portName; }
            set { _portName = value; }
        }

        public int BaudRate
        {
            get { return _baudRate; }
            set { _baudRate = value; }
        }

        public byte RadioAddress
        {
            get { return _radioAddr; }
            set { _radioAddr = value; }
        }

        public byte HostAddress
        {
            get { return _hostAddr; }
            set { _hostAddr = value; }
        }

        public bool TransceiveEnabled
        {
            get { return _transceiveEnabled; }
            set { _transceiveEnabled = value; }
        }

        public bool SyncSplitAndFullDuplex
        {
            get { return _syncSplitAndFullDuplex; }
            set { _syncSplitAndFullDuplex = value; }
        }

        public bool SyncFilterWidth
        {
            get { return _syncFilterWidth; }
            set { _syncFilterWidth = value; }
        }

        public bool SyncPTT
        {
            get { return _syncPTT; }
            set { _syncPTT = value; }
        }

        public bool IsOpen
        {
            get
            {
                lock (_portLock)
                {
                    return _serialPort != null && _serialPort.IsOpen;
                }
            }
        }

        #endregion

        #region Constructor

        public CIVController(Console console)
        {
            if (console == null) throw new ArgumentNullException("console");
            _console = console;
            _floodTimer = new Timer(OnFloodTimerTick, null, Timeout.Infinite, Timeout.Infinite);
        }

        #endregion

        #region Connection Management

        public bool Start(string portName, int baudRate = 19200, byte radioAddr = CIVProtocol.DEFAULT_RADIO_ADDR, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR, bool transceive = true, bool syncSplit = true, bool syncPTT = false)
        {
            _portName = portName;
            _baudRate = baudRate;
            _radioAddr = radioAddr;
            _hostAddr = hostAddr;
            _transceiveEnabled = transceive;
            _syncSplitAndFullDuplex = syncSplit;
            _syncPTT = syncPTT;

            lock (_portLock)
            {
                try
                {
                    Stop();

                    _serialPort = new SerialPort(_portName, _baudRate, Parity.None, 8, StopBits.One)
                    {
                        Handshake = Handshake.None,
                        DtrEnable = true,
                        RtsEnable = true,
                        ReadTimeout = 500,
                        WriteTimeout = 500
                    };

                    _serialPort.DataReceived += SerialPort_DataReceived;
                    _serialPort.Open();

                    _rxBuffer.Clear();

                    // Start flood control timer
                    _floodTimer.Change(FLOOD_INTERVAL_MS, FLOOD_INTERVAL_MS);

                    // Subscribe to Thetis Console events
                    SubscribeToThetisEvents();

                    // Perform initial sync of current state to IC-7100
                    SyncCurrentThetisState();

                    return true;
                }
                catch (Exception ex)
                {
                    Debug.WriteLine(string.Format("[CIVController] Error opening {0}: {1}", _portName, ex.Message));
                    Stop();
                    return false;
                }
            }
        }

        public void Stop()
        {
            lock (_portLock)
            {
                UnsubscribeFromThetisEvents();

                if (_floodTimer != null)
                {
                    _floodTimer.Change(Timeout.Infinite, Timeout.Infinite);
                }

                if (_serialPort != null)
                {
                    try
                    {
                        if (_serialPort.IsOpen)
                        {
                            // Ensure PTT is released before closing port
                            byte[] rxPttFrame = CIVProtocol.SetPttFrame(_radioAddr, _hostAddr, false);
                            _serialPort.Write(rxPttFrame, 0, rxPttFrame.Length);
                            _serialPort.Close();
                        }
                        _serialPort.DataReceived -= SerialPort_DataReceived;
                        _serialPort.Dispose();
                    }
                    catch (Exception ex)
                    {
                        Debug.WriteLine(string.Format("[CIVController] Error closing serial port: {0}", ex.Message));
                    }
                    _serialPort = null;
                }

                _rxBuffer.Clear();
                lock (_echoLock)
                {
                    _recentSentFrames.Clear();
                }
            }
        }

        public bool Start(string portName, int baudRate, byte radioAddr, bool transceive, bool syncSplit, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR)
        {
            return Start(portName, baudRate, radioAddr, hostAddr, transceive, syncSplit, _syncPTT);
        }

        public bool Start(string portName, int baudRate, byte radioAddr, bool transceive, bool syncSplit, bool syncPTT, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR)
        {
            return Start(portName, baudRate, radioAddr, hostAddr, transceive, syncSplit, syncPTT);
        }

        public bool Open(string portName, int baudRate = 19200, byte radioAddr = CIVProtocol.DEFAULT_RADIO_ADDR, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR, bool transceive = true, bool syncSplit = true, bool syncPTT = false)
        {
            return Start(portName, baudRate, radioAddr, hostAddr, transceive, syncSplit, syncPTT);
        }

        public bool Open(string portName, int baudRate, byte radioAddr, bool transceive, bool syncSplit, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR)
        {
            return Start(portName, baudRate, radioAddr, hostAddr, transceive, syncSplit, _syncPTT);
        }

        public bool Open(string portName, int baudRate, byte radioAddr, bool transceive, bool syncSplit, bool syncPTT, byte hostAddr = CIVProtocol.DEFAULT_HOST_ADDR)
        {
            return Start(portName, baudRate, radioAddr, hostAddr, transceive, syncSplit, syncPTT);
        }

        public void Close()
        {
            Stop();
        }

        #endregion

        #region Thetis Event Subscriptions

        private bool _eventsSubscribed = false;

        private void SubscribeToThetisEvents()
        {
            if (_eventsSubscribed || _console == null) return;

            _console.VFOAFrequencyChangeHandlers += OnVFOAFrequencyChanged;
            _console.VFOBFrequencyChangeHandlers += OnVFOBFrequencyChanged;
            _console.VFOASubFrequencyChangeHandlers += OnVFOASubFrequencyChanged;
            _console.TXFrequncyChangedHandlers += OnTXFrequencyChanged;
            _console.MoxChangeHandlers += OnMoxChanged;
            _console.SplitChangedHandlers += OnSplitChanged;
            _console.VFOTXChangedHandlers += OnVFOTXChanged;
            _console.RX2EnabledChangedHandlers += OnRX2EnabledChanged;
            _console.ModeChangeHandlers += OnModeChanged;
            _console.FilterEdgesChangedHandlers += OnFilterEdgesChanged;

            _eventsSubscribed = true;
        }

        private void UnsubscribeFromThetisEvents()
        {
            if (!_eventsSubscribed || _console == null) return;

            _console.VFOAFrequencyChangeHandlers -= OnVFOAFrequencyChanged;
            _console.VFOBFrequencyChangeHandlers -= OnVFOBFrequencyChanged;
            _console.VFOASubFrequencyChangeHandlers -= OnVFOASubFrequencyChanged;
            _console.TXFrequncyChangedHandlers -= OnTXFrequencyChanged;
            _console.MoxChangeHandlers -= OnMoxChanged;
            _console.SplitChangedHandlers -= OnSplitChanged;
            _console.VFOTXChangedHandlers -= OnVFOTXChanged;
            _console.RX2EnabledChangedHandlers -= OnRX2EnabledChanged;
            _console.ModeChangeHandlers -= OnModeChanged;
            _console.FilterEdgesChangedHandlers -= OnFilterEdgesChanged;

            _eventsSubscribed = false;
        }

        #endregion

        #region State Sync & Event Handlers

        private bool IsSplitRequired(double txFreq, double vfoAFreq)
        {
            if (!_syncSplitAndFullDuplex || _console == null) return false;

            return _console.VFOSplit || 
                   _console.FullDuplex || 
                   _console.VFOBTX || 
                   Math.Abs(txFreq - vfoAFreq) > 0.0000015;
        }

        private bool IsSplitRequired()
        {
            if (!_syncSplitAndFullDuplex || _console == null) return false;

            double txFreq;
            double vfoAFreq;
            lock (_stateLock)
            {
                txFreq = _pendingTxFreq > 0 ? _pendingTxFreq : _console.TXFreq;
                vfoAFreq = _pendingVfoAFreq > 0 ? _pendingVfoAFreq : _console.VFOAFreq;
            }
            return IsSplitRequired(txFreq, vfoAFreq);
        }

        public void SyncCurrentThetisState()
        {
            if (!IsOpen || _console == null) return;

            lock (_stateLock)
            {
                _pendingVfoAFreq = _console.VFOAFreq;
                _pendingVfoBFreq = _console.VFOBFreq;
                _pendingTxFreq = _console.TXFreq;
                _pendingMode = _console.RX1DSPMode;
                _pendingFilterWidth = Math.Abs(_console.RX1FilterHigh - _console.RX1FilterLow);
                bool splitRequired = IsSplitRequired();
                _pendingSplit = splitRequired;

                _freqChangePending = true;
                _vfoBChangePending = true;
                _modeChangePending = true;
                _splitChangePending = true;
            }

            // Query initial Split status from the radio
            byte[] readSplitFrame = CIVProtocol.ReadSplitFrame(_radioAddr, _hostAddr);
            SendFrame(readSplitFrame);
        }

        private void OnVFOAFrequencyChanged(Band oldBand, Band newBand, DSPMode oldMode, DSPMode newMode, Filter oldFilter, Filter newFilter, double oldFreq, double newFreq, double oldCentreF, double newCentreF, bool oldCTUN, bool newCTUN, int oldZoomSlider, int newZoomSlider, double offset, int rx)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            lock (_stateLock)
            {
                _pendingVfoAFreq = newFreq;
                _freqChangePending = true;

                bool splitRequired = IsSplitRequired(_pendingTxFreq, newFreq);
                if (splitRequired != _lastSentSplit || (splitRequired && !_actualRadioSplit) || (!splitRequired && _actualRadioSplit))
                {
                    _splitChangePending = true;
                    _vfoBChangePending = true;
                }

                if (oldMode != newMode || oldFilter != newFilter)
                {
                    _pendingMode = newMode;
                    _pendingFilterWidth = Math.Abs(_console.RX1FilterHigh - _console.RX1FilterLow);
                    _modeChangePending = true;
                }
            }
        }

        private void OnVFOBFrequencyChanged(Band oldBand, Band newBand, DSPMode oldMode, DSPMode newMode, Filter oldFilter, Filter newFilter, double oldFreq, double newFreq, double oldCentreF, double newCentreF, bool oldCTUN, bool newCTUN, int oldZoomSlider, int newZoomSlider, double offset, int rx)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            lock (_stateLock)
            {
                _pendingVfoBFreq = newFreq;
                _vfoBChangePending = true;
                if (_console != null && _console.VFOBTX)
                {
                    _pendingTxFreq = newFreq;
                    _splitChangePending = true;
                }
            }
        }

        private void OnVFOASubFrequencyChanged(Band oldBand, Band newBand, DSPMode newMode, Filter newFilter, double oldFreq, double newFreq, double newCentreF, bool newCTUN, int newZoomSlider, double offset, int rx)
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_stateLock)
            {
                double txFreq = (_console.RX2Enabled && _console.VFOSplit && !_console.VFOBTX) ? newFreq : _console.TXFreq;
                _pendingTxFreq = txFreq;
                _pendingVfoBFreq = txFreq;
                bool splitRequired = IsSplitRequired(txFreq, _pendingVfoAFreq);

                if (splitRequired != _lastSentSplit || splitRequired)
                {
                    _vfoBChangePending = true;
                    _splitChangePending = true;
                }
            }
        }

        private void OnTXFrequencyChanged(double old_frequency, double new_frequency, Band old_band, Band new_band, bool rx2_enabled, bool tx_vfob, double centre_freq)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            lock (_stateLock)
            {
                _pendingTxFreq = new_frequency;
                _pendingVfoBFreq = new_frequency;
                bool splitRequired = IsSplitRequired(new_frequency, _pendingVfoAFreq);

                if (splitRequired != _lastSentSplit || splitRequired)
                {
                    _vfoBChangePending = true;
                    _splitChangePending = true;
                }
            }
        }

        private void OnMoxChanged(int rx, bool oldMox, bool newMox)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            if (!newMox && oldMox)
            {
                _lastTxReleaseTime = Stopwatch.GetTimestamp();
            }

            // PTT changes bypass the flood control timer and are transmitted immediately
            SendImmediatePtt(newMox);
        }

        public void NotifySplitOrFullDuplexChanged()
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_stateLock)
            {
                _pendingTxFreq = _console.TXFreq;
                _pendingVfoAFreq = _console.VFOAFreq;
                _pendingVfoBFreq = _console.VFOBFreq;
                bool splitRequired = IsSplitRequired(_pendingTxFreq, _pendingVfoAFreq);
                _pendingSplit = splitRequired;

                _splitChangePending = true;
                _freqChangePending = true;
                _vfoBChangePending = true;
            }
        }

        private void OnSplitChanged(int rx, bool oldSplit, bool newSplit)
        {
            NotifySplitOrFullDuplexChanged();
        }

        private void OnVFOTXChanged(bool vfoB, bool oldState, bool newState)
        {
            NotifySplitOrFullDuplexChanged();
        }

        private void OnRX2EnabledChanged(bool enabled)
        {
            NotifySplitOrFullDuplexChanged();
        }

        private void OnModeChanged(int rx, DSPMode oldMode, DSPMode newMode, Band oldBand, Band newBand)
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_stateLock)
            {
                _pendingMode = newMode;
                _pendingFilterWidth = Math.Abs(_console.RX1FilterHigh - _console.RX1FilterLow);
                _modeChangePending = true;
            }
        }

        private void OnFilterEdgesChanged(int rx, Filter filter, Band band, int low, int high, string sName, int max_width, int max_shift)
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_stateLock)
            {
                _pendingFilterWidth = Math.Abs(high - low);
                _modeChangePending = true;
            }
        }

        #endregion

        #region Outgoing Flood Control & Dispatch

        private void OnFloodTimerTick(object state)
        {
            if (!IsOpen || _suppressOutgoingUpdates) return;

            double targetVfoAFreq = 0.0;
            double targetVfoBFreq = 0.0;
            bool doVfoA = false;
            bool doVfoB = false;
            bool doMode = false;
            bool doSplit = false;
            bool targetSplit = false;
            DSPMode targetMode = DSPMode.USB;
            int targetFilterWidth = 2700;

            lock (_stateLock)
            {
                bool splitRequired = IsSplitRequired();

                if (splitRequired != _lastSentSplit || (splitRequired && !_actualRadioSplit) || (!splitRequired && _actualRadioSplit) || _splitChangePending)
                {
                    doSplit = true;
                    targetSplit = splitRequired;
                    _splitChangePending = false;
                    if (splitRequired)
                    {
                        doVfoB = true;
                        if (_pendingTxFreq <= 0 && _console != null)
                        {
                            _pendingTxFreq = _console.TXFreq;
                        }
                        targetVfoBFreq = _pendingTxFreq;
                        _vfoBChangePending = false;
                    }
                }

                if (_freqChangePending)
                {
                    doVfoA = true;
                    targetVfoAFreq = _pendingVfoAFreq;
                    _freqChangePending = false;
                }

                if (_vfoBChangePending)
                {
                    doVfoB = true;
                    // If Split or Full Duplex is active (or TXFreq != VFOAFreq), unselected VFO mirrors Thetis TXFreq
                    if (splitRequired)
                    {
                        if (_pendingTxFreq <= 0 && _console != null)
                        {
                            _pendingTxFreq = _console.TXFreq;
                        }
                        targetVfoBFreq = _pendingTxFreq;
                    }
                    else
                    {
                        targetVfoBFreq = _pendingVfoBFreq;
                    }
                    _vfoBChangePending = false;
                }

                if (_modeChangePending)
                {
                    doMode = true;
                    targetMode = _pendingMode;
                    targetFilterWidth = _pendingFilterWidth;
                    _modeChangePending = false;
                }
            }

            // Dispatch pending commands sequentially
            if (doSplit)
            {
                SendSplit(targetSplit, force: true);
            }

            if (doVfoA)
            {
                SendVfoAFrequency(targetVfoAFreq);
            }

            if (doVfoB)
            {
                SendVfoBFrequency(targetVfoBFreq);
            }

            if (doMode)
            {
                SendModeAndFilter(targetMode, targetFilterWidth);
            }

            // Periodically poll transceiver condition / PTT state (every 100ms = 2 ticks)
            // so pressing the physical microphone PTT on the IC-7100 triggers Thetis PTT/MOX
            if (_syncPTT)
            {
                _pttPollCounter++;
                if (_pttPollCounter >= 2)
                {
                    _pttPollCounter = 0;
                    if (!_freqChangePending && !_vfoBChangePending)
                    {
                        PollPttCondition();
                    }
                }
            }

            // Periodically poll Split status (every ~1000ms = 20 ticks)
            if (_syncSplitAndFullDuplex)
            {
                _splitPollCounter++;
                if (_splitPollCounter >= 20)
                {
                    _splitPollCounter = 0;
                    if (!_isSwappingVfo && !_freqChangePending && !_vfoBChangePending && !_splitChangePending)
                    {
                        PollSplitCondition();
                    }
                }
            }
        }

        private void PollSplitCondition()
        {
            if (!IsOpen || _suppressOutgoingUpdates) return;
            byte[] frame = CIVProtocol.ReadSplitFrame(_radioAddr, _hostAddr);
            SendFrame(frame);
        }

        private void PollPttCondition()
        {
            if (!IsOpen || _suppressOutgoingUpdates) return;
            byte[] frame = CIVProtocol.ReadPttFrame(_radioAddr, _hostAddr);
            SendFrame(frame);
        }

        private void SendVfoAFrequency(double freqMHz)
        {
            if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoAFreq) < 0.0000005) return;

            byte[] frame = CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freqMHz);
            SendFrame(frame);
            _lastSentVfoAFreq = freqMHz;
        }

        private void SendVfoBFrequency(double freqMHz)
        {
            if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000005) return;

            lock (_vfoSwapLock)
            {
                if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000005) return;
                _lastSentVfoBFreq = freqMHz;

                // If VFO B is currently the active selected VFO on the radio, set it directly
                if (_currentRadioSelectedVfo == CIVProtocol.VFO_B)
                {
                    byte[] directFrame = CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freqMHz);
                    SendFrame(directFrame);
                    return;
                }

                // IC-7100 does not support command 0x25 (unselected VFO).
                // Set VFO B via the standard Icom sequence:
                // 1. Select VFO B (0x07 0x01)
                // 2. Set Frequency (0x05 [BCD])
                // 3. Reselect VFO A (0x07 0x00)
                byte[] selVfoB = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, true);
                byte[] setFreq = CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freqMHz);
                byte[] selVfoA = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false);

                _isSwappingVfo = true;
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                try
                {
                    SendFrame(selVfoB);
                    Thread.Sleep(20);
                    SendFrame(setFreq);
                    Thread.Sleep(20);
                    SendFrame(selVfoA);
                }
                finally
                {
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                    _lastVfoSwapTime = Stopwatch.GetTimestamp();
                    _isSwappingVfo = false;
                }
            }
        }

        private void SendImmediatePtt(bool tx)
        {
            lock (_vfoSwapLock)
            {
                if (tx == _lastSentPtt) return;

                if (tx)
                {
                    // Pre-TX Check: If split operation is required, ensure IC-7100 Split is enabled
                    // and VFO B is set to the current transmit frequency before keying PTT.
                    bool splitRequired;
                    double txFreq;
                    double vfoAFreq;
                    lock (_stateLock)
                    {
                        txFreq = _pendingTxFreq > 0 ? _pendingTxFreq : (_console != null ? _console.TXFreq : 0);
                        vfoAFreq = _pendingVfoAFreq > 0 ? _pendingVfoAFreq : (_console != null ? _console.VFOAFreq : 0);
                        splitRequired = IsSplitRequired(txFreq, vfoAFreq);
                    }

                    if (splitRequired)
                    {
                        if (!_actualRadioSplit || !_lastSentSplit)
                        {
                            SendSplit(true, force: true);
                        }
                        if (txFreq > 0 && Math.Abs(txFreq - _lastSentVfoBFreq) > 0.0000015)
                        {
                            SendVfoBFrequency(txFreq);
                        }
                    }
                    else if (_actualRadioSplit)
                    {
                        SendSplit(false, force: true);
                    }
                }

                byte[] frame = CIVProtocol.SetPttFrame(_radioAddr, _hostAddr, tx);
                SendFrame(frame);
                _lastSentPtt = tx;
            }
        }

        private void SendSplit(bool splitOn, bool force = false)
        {
            lock (_vfoSwapLock)
            {
                if (!force && splitOn == _lastSentSplit && splitOn == _actualRadioSplit) return;

                if (splitOn)
                {
                    // Ensure VFO A is the selected receiver VFO on the IC-7100 before enabling Split,
                    // so the IC-7100 transmits on VFO B (unselected) and receives on VFO A.
                    byte[] selVfoFrame = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false);
                    SendFrame(selVfoFrame);
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                    Thread.Sleep(25);
                    _lastSentVfoBFreq = 0; // Force refresh of VFO B frequency when Split is engaged
                }

                byte[] frame = CIVProtocol.SetSplitFrame(_radioAddr, _hostAddr, splitOn);
                SendFrame(frame);
                _lastSentSplit = splitOn;
                _actualRadioSplit = splitOn;
                Thread.Sleep(30);

                if (!splitOn)
                {
                    // Ensure VFO A is active receiver after exiting split
                    byte[] selVfoFrame = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false);
                    SendFrame(selVfoFrame);
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                }
            }
        }

        private void SendModeAndFilter(DSPMode mode, int filterWidthHz)
        {
            CIVMode civMode;
            CIVFilter civFilter;
            CIVDataMode dataMode;
            CIVProtocol.MapThetisMode(mode, filterWidthHz, out civMode, out civFilter, out dataMode);

            if (civMode != _lastSentCivMode || civFilter != _lastSentCivFilter)
            {
                byte[] modeFrame = CIVProtocol.SetModeFrame(_radioAddr, _hostAddr, civMode, civFilter);
                SendFrame(modeFrame);
                _lastSentCivMode = civMode;
                _lastSentCivFilter = civFilter;
            }

            if (dataMode != _lastSentDataMode)
            {
                byte[] dataModeFrame = CIVProtocol.SetDataModeFrame(_radioAddr, _hostAddr, dataMode, civFilter);
                SendFrame(dataModeFrame);
                _lastSentDataMode = dataMode;
            }

            if (_syncFilterWidth && (civMode == CIVMode.USB || civMode == CIVMode.LSB || civMode == CIVMode.CW || civMode == CIVMode.CW_R))
            {
                byte widthCode = CIVProtocol.CalculateSsbIfFilterWidthCode(filterWidthHz);
                if (widthCode != _lastSentFilterWidthCode)
                {
                    byte[] filterWidthFrame = CIVProtocol.SetFilterWidthFrame(_radioAddr, _hostAddr, widthCode);
                    SendFrame(filterWidthFrame);
                    _lastSentFilterWidthCode = widthCode;
                }
            }
        }

        private void SendFrame(byte[] frame)
        {
            if (frame == null || frame.Length == 0) return;

            lock (_portLock)
            {
                if (_serialPort == null || !_serialPort.IsOpen) return;

                try
                {
                    // Track recently sent frame for echo suppression
                    lock (_echoLock)
                    {
                        if (_recentSentFrames.Count >= 20)
                        {
                            _recentSentFrames.Dequeue();
                        }
                        _recentSentFrames.Enqueue(frame);
                    }

                    _serialPort.Write(frame, 0, frame.Length);
                }
                catch (Exception ex)
                {
                    Debug.WriteLine(string.Format("[CIVController] Error sending frame: {0}", ex.Message));
                }
            }
        }

        #endregion

        #region Incoming Serial Processing & Echo Suppression

        private void SerialPort_DataReceived(object sender, SerialDataReceivedEventArgs e)
        {
            lock (_portLock)
            {
                if (_serialPort == null || !_serialPort.IsOpen) return;

                try
                {
                    int bytesAvailable = _serialPort.BytesToRead;
                    if (bytesAvailable <= 0) return;

                    byte[] buffer = new byte[bytesAvailable];
                    int bytesRead = _serialPort.Read(buffer, 0, bytesAvailable);

                    for (int i = 0; i < bytesRead; i++)
                    {
                        byte b = buffer[i];
                        _rxBuffer.Add(b);

                        if (b == CIVProtocol.EOM)
                        {
                            ProcessRxBuffer();
                        }
                    }

                    // Guard against unbounded buffer growth from corrupted streams
                    if (_rxBuffer.Count > 1024)
                    {
                        _rxBuffer.Clear();
                    }
                }
                catch (Exception ex)
                {
                    Debug.WriteLine(string.Format("[CIVController] Error reading serial data: {0}", ex.Message));
                }
            }
        }

        private void ProcessRxBuffer()
        {
            int startIndex = -1;

            // Locate preamble FE FE
            for (int i = 0; i < _rxBuffer.Count - 1; i++)
            {
                if (_rxBuffer[i] == CIVProtocol.PREAMBLE && _rxBuffer[i + 1] == CIVProtocol.PREAMBLE)
                {
                    startIndex = i;
                    break;
                }
            }

            if (startIndex == -1)
            {
                _rxBuffer.Clear();
                return;
            }

            // Remove any garbage preceding the preamble
            if (startIndex > 0)
            {
                _rxBuffer.RemoveRange(0, startIndex);
            }

            // Find EOM FD
            int endIndex = _rxBuffer.IndexOf(CIVProtocol.EOM);
            if (endIndex == -1) return; // Incomplete packet, wait for more data

            int frameLen = endIndex + 1;
            byte[] frame = new byte[frameLen];
            _rxBuffer.CopyTo(0, frame, 0, frameLen);
            _rxBuffer.RemoveRange(0, frameLen);

            HandleCIVFrame(frame);
        }

        private void HandleCIVFrame(byte[] frame)
        {
            // Minimum valid frame: FE FE [to] [from] [cmd] FD (6 bytes)
            if (frame == null || frame.Length < 6) return;

            // Check for echo of our own sent command
            if (IsLocalEcho(frame))
            {
                return;
            }

            byte toAddr = frame[2];
            byte fromAddr = frame[3];
            byte cmd = frame[4];

            // Frame must be directed to Host (0xE0) or Broadcast (0x00)
            if (toAddr != _hostAddr && toAddr != CIVProtocol.BROADCAST_ADDR)
            {
                return;
            }

            // Acknowledgment or NAK
            if (cmd == CIVProtocol.ACK)
            {
                Debug.WriteLine("[CIVController] ACK received from IC-7100");
                return;
            }
            if (cmd == CIVProtocol.NAK)
            {
                Debug.WriteLine("[CIVController] NAK received from IC-7100 (unsupported command or PLL out-of-lock)");
                return;
            }

            // If transceive is disabled, ignore unsolicited status broadcasts from the radio
            if (!_transceiveEnabled) return;

            switch (cmd)
            {
                // Frequency report (0x00 or 0x03)
                case 0x00:
                case CIVProtocol.CMD_READ_FREQ:
                    if (frame.Length >= 10)
                    {
                        double freqMHz = CIVProtocol.DecodeFrequency(frame, 5);
                        HandleIncomingFrequency(freqMHz);
                    }
                    break;

                // Mode report (0x01 or 0x04)
                case 0x01:
                case CIVProtocol.CMD_READ_MODE:
                    if (frame.Length >= 7)
                    {
                        CIVMode mode = (CIVMode)frame[5];
                        CIVFilter filter = frame.Length >= 8 ? (CIVFilter)frame[6] : CIVFilter.FIL2;
                        HandleIncomingMode(mode, filter);
                    }
                    break;

                // VFO selection report (0x07)
                case CIVProtocol.CMD_VFO_SEL:
                    if (frame.Length >= 6)
                    {
                        byte vfoId = frame[5];
                        if (vfoId == CIVProtocol.VFO_A || vfoId == CIVProtocol.VFO_B)
                        {
                            if (_isSwappingVfo) return;
                            if (_lastVfoSwapTime > 0 && vfoId == CIVProtocol.VFO_B)
                            {
                                double msSinceSwap = (double)(Stopwatch.GetTimestamp() - _lastVfoSwapTime) / Stopwatch.Frequency * 1000.0;
                                if (msSinceSwap < 400.0) return;
                            }
                            _currentRadioSelectedVfo = vfoId;
                        }
                    }
                    break;

                // Data Mode report (0x1A 0x06)
                case CIVProtocol.CMD_MISC_1A:
                    if (frame.Length >= 8 && frame[5] == CIVProtocol.SUBCMD_1A_DATA_MODE)
                    {
                        CIVDataMode dataMode = (CIVDataMode)frame[6];
                        CIVFilter filter = frame.Length >= 9 ? (CIVFilter)frame[7] : CIVFilter.FIL2;
                        HandleIncomingDataMode(dataMode, filter);
                    }
                    break;

                // PTT Condition (0x1C 0x00)
                case CIVProtocol.CMD_CONDITION_1C:
                    if (_syncPTT && frame.Length >= 8 && frame[5] == CIVProtocol.SUBCMD_1C_PTT)
                    {
                        bool tx = frame[6] == 0x01;
                        HandleIncomingPtt(tx);
                    }
                    break;

                // Split report (0x0F)
                case CIVProtocol.CMD_SPLIT:
                    if (frame.Length >= 6)
                    {
                        byte splitByte = frame[5];
                        bool radioSplit = (splitByte == CIVProtocol.SPLIT_ON);
                        _actualRadioSplit = radioSplit;
                        _lastSentSplit = radioSplit;

                        // If Thetis requires split but the radio is in simplex,
                        // or Thetis is simplex but the radio is in split, trigger sync
                        if (_syncSplitAndFullDuplex)
                        {
                            bool needSplit = IsSplitRequired();
                            if (needSplit != radioSplit)
                            {
                                _splitChangePending = true;
                            }
                        }
                    }
                    break;
            }
        }

        private bool IsLocalEcho(byte[] frame)
        {
            lock (_echoLock)
            {
                if (_recentSentFrames.Count == 0) return false;

                foreach (var sentFrame in _recentSentFrames)
                {
                    if (sentFrame.Length == frame.Length)
                    {
                        bool match = true;
                        for (int i = 0; i < frame.Length; i++)
                        {
                            if (sentFrame[i] != frame[i])
                            {
                                match = false;
                                break;
                            }
                        }
                        if (match) return true;
                    }
                }
            }
            return false;
        }

        private void HandleIncomingFrequency(double freqMHz)
        {
            if (freqMHz <= 0 || _console == null) return;

            // 0. Guard against incoming frequency reports generated during programmatic VFO swap
            if (_isSwappingVfo) return;
            if (_lastVfoSwapTime > 0)
            {
                double msSinceSwap = (double)(Stopwatch.GetTimestamp() - _lastVfoSwapTime) / Stopwatch.Frequency * 1000.0;
                if (msSinceSwap < 400.0)
                {
                    return;
                }
            }

            // 1. Guard against updates while transmitting:
            // When transmitting, the IC-7100 broadcasts the TX frequency (e.g. VFO B in split).
            // Under no circumstances should transmit broadcasts be treated as receive VFO changes.
            if (_console.MOX) return;

            // 2. Post-TX Settling Window (350 ms):
            // After PTT is released, the transceiver or CI-V bus may still be draining queued frames
            // representing the transmit frequency. Drop any frequency matching the transmit frequency or VFO B.
            if (_lastTxReleaseTime > 0)
            {
                double msSinceRelease = (double)(Stopwatch.GetTimestamp() - _lastTxReleaseTime) / Stopwatch.Frequency * 1000.0;
                if (msSinceRelease < 350.0)
                {
                    if (Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000015 ||
                        Math.Abs(freqMHz - _pendingTxFreq) < 0.0000015 ||
                        Math.Abs(freqMHz - _console.TXFreq) < 0.0000015 ||
                        Math.Abs(freqMHz - _console.VFOBFreq) < 0.0000015 ||
                        (_console.RX2Enabled && Math.Abs(freqMHz - _console.VFOASubFreq) < 0.0000015))
                    {
                        return;
                    }
                }
            }

            // 3. Unselected VFO / Split TX Echo Suppression:
            // When Split is active or TX frequency differs from VFO A, any incoming frequency that matches
            // the TX frequency or VFO B must NOT be applied to VFO A.
            bool isSplitOrDiff = IsSplitRequired();
            if (isSplitOrDiff)
            {
                if (Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000015 ||
                    Math.Abs(freqMHz - _pendingTxFreq) < 0.0000015 ||
                    Math.Abs(freqMHz - _console.TXFreq) < 0.0000015 ||
                    Math.Abs(freqMHz - _console.VFOBFreq) < 0.0000015 ||
                    (_console.RX2Enabled && Math.Abs(freqMHz - _console.VFOASubFreq) < 0.0000015))
                {
                    return;
                }
            }

            // 4. Check if the radio has VFO B selected (operator tuned dial on VFO B)
            if (_currentRadioSelectedVfo == CIVProtocol.VFO_B)
            {
                if (_console.RX2Enabled && _console.VFOSplit && !_console.VFOBTX)
                {
                    if (Math.Abs(freqMHz - _console.VFOASubFreq) < 0.0000015 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000015)
                    {
                        return;
                    }

                    _suppressOutgoingUpdates = true;
                    try
                    {
                        _console.BeginInvoke(new Action(() =>
                        {
                            try
                            {
                                _console.VFOASubFreq = freqMHz;
                            }
                            finally
                            {
                                _suppressOutgoingUpdates = false;
                            }
                        }));
                    }
                    catch
                    {
                        _suppressOutgoingUpdates = false;
                    }
                    return;
                }

                if (Math.Abs(freqMHz - _console.VFOBFreq) < 0.0000015 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000015)
                {
                    return;
                }

                _suppressOutgoingUpdates = true;
                try
                {
                    _console.BeginInvoke(new Action(() =>
                    {
                        try
                        {
                            _console.VFOBFreq = freqMHz;
                        }
                        finally
                        {
                            _suppressOutgoingUpdates = false;
                        }
                    }));
                }
                catch
                {
                    _suppressOutgoingUpdates = false;
                }
                return;
            }

            // 5. VFO A Update:
            // Check if delta is significant (> 1.5 Hz) and not an echo of our last sent VFO A frequency
            if (Math.Abs(freqMHz - _console.VFOAFreq) < 0.0000015 || Math.Abs(freqMHz - _lastSentVfoAFreq) < 0.0000015)
            {
                return;
            }

            _suppressOutgoingUpdates = true;
            try
            {
                _console.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        _console.VFOAFreq = freqMHz;
                    }
                    finally
                    {
                        _suppressOutgoingUpdates = false;
                    }
                }));
            }
            catch
            {
                _suppressOutgoingUpdates = false;
            }
        }

        private void HandleIncomingMode(CIVMode mode, CIVFilter filter)
        {
            if (_console == null) return;

            DSPMode currentMode = _console.RX1DSPMode;
            DSPMode targetMode = currentMode;
            bool modeNeedsChange = false;

            switch (mode)
            {
                case CIVMode.LSB:
                    // Preserve DIGL if already in digital LSB mode
                    if (currentMode != DSPMode.LSB && currentMode != DSPMode.DIGL)
                    {
                        targetMode = DSPMode.LSB;
                        modeNeedsChange = true;
                    }
                    break;

                case CIVMode.USB:
                    // Preserve DIGU, DSB, SPEC, or DRM if already in digital or variant USB mode
                    if (currentMode != DSPMode.USB && currentMode != DSPMode.DIGU &&
                        currentMode != DSPMode.DSB && currentMode != DSPMode.SPEC && currentMode != DSPMode.DRM)
                    {
                        targetMode = DSPMode.USB;
                        modeNeedsChange = true;
                    }
                    break;

                case CIVMode.AM:
                    if (currentMode != DSPMode.AM && currentMode != DSPMode.SAM)
                    {
                        targetMode = DSPMode.AM;
                        modeNeedsChange = true;
                    }
                    break;

                case CIVMode.CW:
                    if (currentMode != DSPMode.CWL)
                    {
                        targetMode = DSPMode.CWL;
                        modeNeedsChange = true;
                    }
                    break;

                case CIVMode.CW_R:
                    if (currentMode != DSPMode.CWU)
                    {
                        targetMode = DSPMode.CWU;
                        modeNeedsChange = true;
                    }
                    break;

                case CIVMode.FM:
                    if (currentMode != DSPMode.FM)
                    {
                        targetMode = DSPMode.FM;
                        modeNeedsChange = true;
                    }
                    break;
            }

            Filter targetFilter = CIVProtocol.MapCIVFilterToThetisFilter(targetMode, filter);
            bool filterNeedsChange = (targetFilter != _console.RX1Filter);

            if (!modeNeedsChange && !filterNeedsChange) return;

            _suppressOutgoingUpdates = true;
            try
            {
                _console.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        if (modeNeedsChange)
                        {
                            _console.RX1DSPMode = targetMode;
                        }
                        if (filterNeedsChange)
                        {
                            _console.RX1Filter = targetFilter;
                        }
                    }
                    finally
                    {
                        _suppressOutgoingUpdates = false;
                    }
                }));
            }
            catch
            {
                _suppressOutgoingUpdates = false;
            }
        }

        private void HandleIncomingDataMode(CIVDataMode dataMode, CIVFilter filter)
        {
            if (_console == null) return;

            DSPMode currentMode = _console.RX1DSPMode;
            DSPMode targetMode = currentMode;
            bool modeNeedsChange = false;

            if (dataMode != CIVDataMode.OFF)
            {
                // Turn digital mode ON: USB -> DIGU, LSB -> DIGL
                if (currentMode == DSPMode.USB)
                {
                    targetMode = DSPMode.DIGU;
                    modeNeedsChange = true;
                }
                else if (currentMode == DSPMode.LSB)
                {
                    targetMode = DSPMode.DIGL;
                    modeNeedsChange = true;
                }
            }
            else
            {
                // Turn digital mode OFF: DIGU -> USB, DIGL -> LSB
                if (currentMode == DSPMode.DIGU)
                {
                    targetMode = DSPMode.USB;
                    modeNeedsChange = true;
                }
                else if (currentMode == DSPMode.DIGL)
                {
                    targetMode = DSPMode.LSB;
                    modeNeedsChange = true;
                }
            }

            Filter targetFilter = CIVProtocol.MapCIVFilterToThetisFilter(targetMode, filter);
            bool filterNeedsChange = (targetFilter != _console.RX1Filter);

            if (!modeNeedsChange && !filterNeedsChange) return;

            _suppressOutgoingUpdates = true;
            try
            {
                _console.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        if (modeNeedsChange)
                        {
                            _console.RX1DSPMode = targetMode;
                        }
                        if (filterNeedsChange)
                        {
                            _console.RX1Filter = targetFilter;
                        }
                    }
                    finally
                    {
                        _suppressOutgoingUpdates = false;
                    }
                }));
            }
            catch
            {
                _suppressOutgoingUpdates = false;
            }
        }

        private void HandleIncomingPtt(bool tx)
        {
            if (_console == null || tx == _console.MOX) return;

            _suppressOutgoingUpdates = true;
            try
            {
                _console.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        _console.MOX = tx;
                    }
                    finally
                    {
                        _suppressOutgoingUpdates = false;
                    }
                }));
            }
            catch
            {
                _suppressOutgoingUpdates = false;
            }
        }

        #endregion

        #region IDisposable

        public void Dispose()
        {
            if (_isDisposed) return;
            _isDisposed = true;

            Stop();

            if (_floodTimer != null)
            {
                _floodTimer.Dispose();
                _floodTimer = null;
            }
        }

        #endregion
    }
}
