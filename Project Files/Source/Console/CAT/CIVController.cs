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
        private byte _currentRadioSelectedVfo = CIVProtocol.VFO_A;
        private long _lastTxReleaseTime = 0;
        private volatile bool _isSwappingVfo = false;
        private volatile bool _radioInitiatedSwapInProgress = false;
        private long _lastVfoSwapTime = 0;
        private long _lastVfoBTuneTime = 0;
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

        private bool IsSplitRequired()
        {
            if (!_syncSplitAndFullDuplex || _console == null) return false;

            return _console.VFOSplit || 
                   _console.FullDuplex || 
                   _console.VFOBTX;
        }

        public void SyncCurrentThetisState()
        {
            if (!IsOpen || _console == null) return;

            lock (_vfoSwapLock)
            {
                _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));

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
            }
        }

        private void OnVFOAFrequencyChanged(Band oldBand, Band newBand, DSPMode oldMode, DSPMode newMode, Filter oldFilter, Filter newFilter, double oldFreq, double newFreq, double oldCentreF, double newCentreF, bool oldCTUN, bool newCTUN, int oldZoomSlider, int newZoomSlider, double offset, int rx)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            lock (_stateLock)
            {
                _pendingVfoAFreq = newFreq;
                _freqChangePending = true;

                if (!IsSplitRequired())
                {
                    _pendingTxFreq = newFreq;
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
                _lastVfoBTuneTime = Stopwatch.GetTimestamp();

                if (_console != null && _console.VFOBTX)
                {
                    _pendingTxFreq = newFreq;
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
                if (IsSplitRequired())
                {
                    _pendingVfoBFreq = txFreq;
                    _vfoBChangePending = true;
                    _lastVfoBTuneTime = Stopwatch.GetTimestamp();
                }
            }
        }

        private void OnTXFrequencyChanged(double old_frequency, double new_frequency, Band old_band, Band new_band, bool rx2_enabled, bool tx_vfob, double centre_freq)
        {
            if (_suppressOutgoingUpdates || !IsOpen) return;

            lock (_stateLock)
            {
                _pendingTxFreq = new_frequency;
                if (IsSplitRequired())
                {
                    _pendingVfoBFreq = new_frequency;
                    _vfoBChangePending = true;
                    _lastVfoBTuneTime = Stopwatch.GetTimestamp();
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
                bool splitRequired = IsSplitRequired();
                _pendingSplit = splitRequired;

                if (splitRequired != _lastSentSplit)
                {
                    _splitChangePending = true;
                }
            }

            try
            {
                _floodTimer?.Change(0, FLOOD_INTERVAL_MS);
            }
            catch { }
        }

        public void NotifyVFOAtoB()
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_vfoSwapLock)
            {
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                // Ensure radio is on VFO A so active VFO A is copied to inactive VFO B
                if (_currentRadioSelectedVfo != CIVProtocol.VFO_A)
                {
                    SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                    Thread.Sleep(40);
                }

                // CI-V 0x07 0xA0: Equalize VFO A -> VFO B
                SendFrame(CIVProtocol.EqualVfoFrame(_radioAddr, _hostAddr));

                lock (_stateLock)
                {
                    _lastSentVfoBFreq = _console.VFOAFreq;
                    _pendingVfoBFreq = _console.VFOAFreq;
                    if (_console.VFOSplit || _console.VFOBTX)
                    {
                        _pendingTxFreq = _console.VFOAFreq;
                    }
                    _vfoBChangePending = false;
                }
            }
        }

        public void NotifyVFOBtoA()
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_vfoSwapLock)
            {
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                // Ensure radio is on VFO A
                if (_currentRadioSelectedVfo != CIVProtocol.VFO_A)
                {
                    SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                    Thread.Sleep(40);
                }

                double freq = _console.VFOBFreq;
                if (freq > 0)
                {
                    SendFrame(CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freq));
                    lock (_stateLock)
                    {
                        _lastSentVfoAFreq = freq;
                        _pendingVfoAFreq = freq;
                        _freqChangePending = false;
                    }
                }
            }
        }

        public void NotifyVFOSwap()
        {
            if (_suppressOutgoingUpdates || !IsOpen || _console == null) return;

            lock (_vfoSwapLock)
            {
                // Toggle between VFO A and VFO B on the IC-7100
                bool selectB = (_currentRadioSelectedVfo == CIVProtocol.VFO_A);
                _currentRadioSelectedVfo = selectB ? CIVProtocol.VFO_B : CIVProtocol.VFO_A;

                SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, selectB));

                lock (_stateLock)
                {
                    _lastSentVfoAFreq = _console.VFOAFreq;
                    _lastSentVfoBFreq = _console.VFOBFreq;

                    _pendingVfoAFreq = _console.VFOAFreq;
                    _pendingVfoBFreq = _console.VFOBFreq;
                    _pendingTxFreq = _console.TXFreq;

                    bool isSplit = (_console != null && (_console.VFOBTX || _console.VFOSplit || _console.FullDuplex));
                    _pendingSplit = isSplit;
                    _lastSentSplit = isSplit;

                    _freqChangePending = false;
                    _vfoBChangePending = false;
                    _splitChangePending = false;
                }
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
            if (!IsOpen || _suppressOutgoingUpdates || _isSwappingVfo || _radioInitiatedSwapInProgress) return;

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

                if (splitRequired != _lastSentSplit || _splitChangePending)
                {
                    doSplit = true;
                    targetSplit = splitRequired;
                    _splitChangePending = false;

                    if (_pendingTxFreq <= 0 && _console != null)
                    {
                        _pendingTxFreq = _console.TXFreq;
                    }
                    if (_pendingVfoAFreq <= 0 && _console != null)
                    {
                        _pendingVfoAFreq = _console.VFOAFreq;
                    }

                    targetVfoBFreq = _pendingTxFreq;
                    targetVfoAFreq = _pendingVfoAFreq;
                }

                if (_freqChangePending)
                {
                    doVfoA = true;
                    targetVfoAFreq = _pendingVfoAFreq;
                    _freqChangePending = false;
                }

                if (_vfoBChangePending)
                {
                    // Dampen VFO B swapping while actively tuning:
                    // On the IC-7100, setting VFO B requires a VFO A -> VFO B -> VFO A swap sequence.
                    // To avoid audio clicks and display flicker on every 1 Hz dial tick, wait 400ms after tuning pauses
                    // before dispatching the swap (unless transmitting).
                    double msSinceTune = _lastVfoBTuneTime > 0 
                        ? (double)(Stopwatch.GetTimestamp() - _lastVfoBTuneTime) / Stopwatch.Frequency * 1000.0 
                        : 9999.0;

                    if (msSinceTune >= 400.0)
                    {
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

                        if (targetVfoBFreq > 0 && Math.Abs(targetVfoBFreq - _lastSentVfoBFreq) > 0.0000015)
                        {
                            doVfoB = true;
                        }
                        _vfoBChangePending = false;
                    }
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
                if (targetSplit)
                {
                    ActivateSplit(targetVfoAFreq, targetVfoBFreq);
                }
                else
                {
                    DeactivateSplit(targetVfoAFreq);
                }
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
                    if (!_freqChangePending && !_vfoBChangePending && !_isSwappingVfo)
                    {
                        PollPttCondition();
                    }
                }
            }
        }

        private void PollPttCondition()
        {
            if (!IsOpen || _suppressOutgoingUpdates || _isSwappingVfo) return;
            byte[] frame = CIVProtocol.ReadPttFrame(_radioAddr, _hostAddr);
            SendFrame(frame);
        }

        private void SendVfoSwap()
        {
            NotifyVFOSwap();
        }

        private void SendVfoAFrequency(double freqMHz)
        {
            if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoAFreq) < 0.0000005) return;

            lock (_vfoSwapLock)
            {
                if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoAFreq) < 0.0000005) return;

                byte[] frame = CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freqMHz);
                SendFrame(frame);
                _lastSentVfoAFreq = freqMHz;
            }
        }

        private void SendVfoBFrequency(double freqMHz)
        {
            if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000005) return;

            lock (_vfoSwapLock)
            {
                if (freqMHz <= 0 || Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000005) return;
                _lastSentVfoBFreq = freqMHz;

                // Set unselected VFO on the IC-7100:
                // 1. Select the unselected VFO
                // 2. Set Frequency
                // 3. Reselect the original selected VFO
                bool selectOther = (_currentRadioSelectedVfo == CIVProtocol.VFO_A);
                byte[] selOther = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, selectOther);
                byte[] setFreq = CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, freqMHz);
                byte[] selOriginal = CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, !selectOther);

                _isSwappingVfo = true;
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                try
                {
                    SendFrame(selOther);
                    Thread.Sleep(60);
                    SendFrame(setFreq);
                    Thread.Sleep(80); // 80ms allows IC-7100 PLL synthesizer to fully lock
                    SendFrame(selOriginal);
                    Thread.Sleep(80);
                    // Fail-safe confirmation: Reselect original VFO a second time
                    SendFrame(selOriginal);
                    Thread.Sleep(40);
                }
                finally
                {
                    _lastVfoSwapTime = Stopwatch.GetTimestamp();
                    _isSwappingVfo = false;
                }
            }
        }

        private void ActivateSplit(double vfoAFreq, double vfoBFreq)
        {
            lock (_vfoSwapLock)
            {
                if (vfoAFreq <= 0 && _console != null) vfoAFreq = _console.VFOAFreq;
                if (vfoBFreq <= 0 && _console != null) vfoBFreq = _console.TXFreq;

                _isSwappingVfo = true;
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                try
                {
                    // 1. Ensure radio is on VFO A as receive VFO
                    if (_currentRadioSelectedVfo != CIVProtocol.VFO_A)
                    {
                        SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));
                        _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                        Thread.Sleep(30);
                    }

                    // 2. Turn Split ON immediately so radio displays SPLIT
                    SendFrame(CIVProtocol.SetSplitFrame(_radioAddr, _hostAddr, true));
                    _lastSentSplit = true;
                    _actualRadioSplit = true;
                    Thread.Sleep(30);

                    // 3. If VFO B frequency needs to be updated on radio:
                    if (vfoBFreq > 0 && Math.Abs(vfoBFreq - _lastSentVfoBFreq) > 0.0000015)
                    {
                        // Select VFO B, set frequency, reselect VFO A
                        SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, true));
                        Thread.Sleep(50);
                        SendFrame(CIVProtocol.SetFrequencyFrame(_radioAddr, _hostAddr, vfoBFreq));
                        _lastSentVfoBFreq = vfoBFreq;
                        Thread.Sleep(60);
                        SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));
                        Thread.Sleep(40);
                    }
                }
                finally
                {
                    _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                    _lastVfoSwapTime = Stopwatch.GetTimestamp();
                    _isSwappingVfo = false;
                }
            }
        }

        private void DeactivateSplit(double vfoAFreq)
        {
            lock (_vfoSwapLock)
            {
                if (vfoAFreq <= 0 && _console != null) vfoAFreq = _console.VFOAFreq;

                _isSwappingVfo = true;
                _lastVfoSwapTime = Stopwatch.GetTimestamp();

                try
                {
                    // 1. Turn Split OFF immediately
                    SendFrame(CIVProtocol.SetSplitFrame(_radioAddr, _hostAddr, false));
                    _lastSentSplit = false;
                    _actualRadioSplit = false;
                    Thread.Sleep(30);

                    // 2. Ensure VFO A is active receiver
                    if (_currentRadioSelectedVfo != CIVProtocol.VFO_A)
                    {
                        SendFrame(CIVProtocol.SelectVfoFrame(_radioAddr, _hostAddr, false));
                        _currentRadioSelectedVfo = CIVProtocol.VFO_A;
                        Thread.Sleep(30);
                    }
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
                        splitRequired = IsSplitRequired();
                    }

                    if (splitRequired)
                    {
                        if (!_actualRadioSplit || !_lastSentSplit)
                        {
                            ActivateSplit(vfoAFreq, txFreq);
                        }
                        else if (txFreq > 0 && Math.Abs(txFreq - _lastSentVfoBFreq) > 0.0000015)
                        {
                            SendVfoBFrequency(txFreq);
                        }
                    }
                    else if (_actualRadioSplit)
                    {
                        DeactivateSplit(vfoAFreq);
                    }
                }

                byte[] frame = CIVProtocol.SetPttFrame(_radioAddr, _hostAddr, tx);
                SendFrame(frame);
                _lastSentPtt = tx;
            }
        }

        private void SendSplit(bool splitOn, bool force = false)
        {
            if (!force && splitOn == _lastSentSplit) return;

            if (splitOn)
            {
                double vfoAFreq = _console != null ? _console.VFOAFreq : 0;
                double vfoBFreq = _console != null ? _console.TXFreq : 0;
                ActivateSplit(vfoAFreq, vfoBFreq);
            }
            else
            {
                double vfoAFreq = _console != null ? _console.VFOAFreq : 0;
                DeactivateSplit(vfoAFreq);
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
            while (true)
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

            // Frame must originate from Radio or Host (local loopback)
            if (fromAddr != _radioAddr && fromAddr != _hostAddr)
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
                    if (frame.Length >= 10 && frame.Length <= 12)
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

                        // Do not process VFO swaps while transmitting
                        if (_console != null && _console.MOX) return;

                        if (_isSwappingVfo || _radioInitiatedSwapInProgress) return;

                        bool isEchoOfOurCommand = false;
                        if (_lastVfoSwapTime > 0)
                        {
                            double msSinceSwap = (double)(Stopwatch.GetTimestamp() - _lastVfoSwapTime) / Stopwatch.Frequency * 1000.0;
                            if (msSinceSwap < 300.0)
                            {
                                isEchoOfOurCommand = true;
                            }
                        }

                        if (vfoId == CIVProtocol.VFO_SWAP)
                        {
                            return;
                        }

                        if (vfoId == CIVProtocol.VFO_EQUAL)
                        {
                            HandleRadioVfoEqual();
                            return;
                        }

                        if (vfoId == CIVProtocol.VFO_A || vfoId == CIVProtocol.VFO_B)
                        {
                            if (vfoId == _currentRadioSelectedVfo || isEchoOfOurCommand)
                            {
                                return; // Echo of our own command or redundant
                            }

                            // Operator physically tapped [A/B] on the IC-7100!
                            lock (_vfoSwapLock)
                            {
                                _currentRadioSelectedVfo = vfoId;
                                _lastVfoSwapTime = Stopwatch.GetTimestamp();
                                _radioInitiatedSwapInProgress = true;

                                lock (_stateLock)
                                {
                                    double temp = _lastSentVfoAFreq;
                                    _lastSentVfoAFreq = _lastSentVfoBFreq;
                                    _lastSentVfoBFreq = temp;

                                    _pendingVfoAFreq = _lastSentVfoAFreq;
                                    _pendingVfoBFreq = _lastSentVfoBFreq;
                                    _pendingTxFreq = IsSplitRequired() ? _lastSentVfoBFreq : _lastSentVfoAFreq;

                                    _freqChangePending = false;
                                    _vfoBChangePending = false;
                                    _splitChangePending = false;
                                }

                                _suppressOutgoingUpdates = true;
                                try
                                {
                                    _console.BeginInvoke(new Action(() =>
                                    {
                                        try
                                        {
                                            _console.VFOSwap();
                                        }
                                        finally
                                        {
                                            _suppressOutgoingUpdates = false;
                                            _radioInitiatedSwapInProgress = false;
                                        }
                                    }));
                                }
                                catch
                                {
                                    _suppressOutgoingUpdates = false;
                                    _radioInitiatedSwapInProgress = false;
                                }
                            }
                            return;
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
                    if (frame.Length >= 7 && fromAddr == _radioAddr)
                    {
                        if (_console != null && _console.MOX) break;

                        byte splitByte = frame[5];
                        if (splitByte == CIVProtocol.SPLIT_ON || splitByte == CIVProtocol.SPLIT_OFF)
                        {
                            bool isSplit = (splitByte == CIVProtocol.SPLIT_ON);
                            _actualRadioSplit = isSplit;
                            _lastSentSplit = isSplit;

                            lock (_stateLock)
                            {
                                _pendingSplit = isSplit;
                                _splitChangePending = false;
                            }

                            if (_console != null)
                            {
                                _suppressOutgoingUpdates = true;
                                try
                                {
                                    _console.BeginInvoke(new Action(() =>
                                    {
                                        try
                                        {
                                            if (isSplit)
                                            {
                                                _console.VFOSplit = true;
                                                _console.VFOBTX = true;
                                            }
                                            else
                                            {
                                                _console.VFOSplit = false;
                                                _console.VFOATX = true;
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
                        }
                    }
                    break;
            }
        }

        private void HandleRadioVfoEqual()
        {
            if (_console == null) return;

            _lastVfoSwapTime = Stopwatch.GetTimestamp();

            lock (_stateLock)
            {
                _lastSentVfoBFreq = _lastSentVfoAFreq;
                _pendingVfoBFreq = _lastSentVfoAFreq;
                _pendingTxFreq = IsSplitRequired() ? _lastSentVfoAFreq : _pendingTxFreq;
                _vfoBChangePending = false;
            }

            _suppressOutgoingUpdates = true;
            try
            {
                _console.BeginInvoke(new Action(() =>
                {
                    try
                    {
                        if (_currentRadioSelectedVfo == CIVProtocol.VFO_B)
                        {
                            _console.CopyVFOBtoA();
                        }
                        else
                        {
                            _console.CopyVFOAtoB();
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

            // Frequency range validation: if not using transverters, prevent out-of-range frequencies
            // (e.g. VHF/UHF frequencies from IC-7100 VFO B clamping to MaxFreq 61.440 MHz)
            if (_console.RX1XVTRIndex < 0)
            {
                if (freqMHz > _console.MaxFreq || freqMHz < _console.MinFreq)
                {
                    return;
                }
            }

            // 0. Guard against incoming frequency reports generated during programmatic VFO swap
            if (_isSwappingVfo || _radioInitiatedSwapInProgress) return;
            if (_lastVfoSwapTime > 0)
            {
                double msSinceSwap = (double)(Stopwatch.GetTimestamp() - _lastVfoSwapTime) / Stopwatch.Frequency * 1000.0;
                if (msSinceSwap < 300.0)
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

            // 3. Detect Radio-Initiated VFO Swap ([A/B] button on IC-7100):
            // Since the IC-7100 does not broadcast a CI-V VFO report (0x07) in transceive mode,
            // pressing [A/B] on the IC-7100 causes it to switch to the other VFO and broadcast
            // that VFO's frequency.
            // If the incoming frequency matches Thetis VFOB (and differs from VFOA),
            // this signals that the operator pressed [A/B] on the IC-7100.
            double targetVfoBFreq = (_console.VFOBTX || _console.VFOSplit) && _console.TXFreq > 0 
                ? _console.TXFreq 
                : _console.VFOBFreq;

            bool matchesVfoB = (targetVfoBFreq > 0 && Math.Abs(freqMHz - targetVfoBFreq) < 0.0000015) ||
                               (_lastSentVfoBFreq > 0 && Math.Abs(freqMHz - _lastSentVfoBFreq) < 0.0000015) ||
                               (_console.VFOBFreq > 0 && Math.Abs(freqMHz - _console.VFOBFreq) < 0.0000015);

            bool differsFromVfoA = Math.Abs(freqMHz - _console.VFOAFreq) > 0.0000015 &&
                                   Math.Abs(freqMHz - _lastSentVfoAFreq) > 0.0000015;

            if (matchesVfoB && differsFromVfoA)
            {
                lock (_vfoSwapLock)
                {
                    bool selectB = (_currentRadioSelectedVfo == CIVProtocol.VFO_A);
                    _currentRadioSelectedVfo = selectB ? CIVProtocol.VFO_B : CIVProtocol.VFO_A;
                    _lastVfoSwapTime = Stopwatch.GetTimestamp();
                    _radioInitiatedSwapInProgress = true;

                    lock (_stateLock)
                    {
                        double temp = _lastSentVfoAFreq;
                        _lastSentVfoAFreq = _lastSentVfoBFreq;
                        _lastSentVfoBFreq = temp;

                        _pendingVfoAFreq = _lastSentVfoAFreq;
                        _pendingVfoBFreq = _lastSentVfoBFreq;
                        _pendingTxFreq = IsSplitRequired() ? _lastSentVfoBFreq : _lastSentVfoAFreq;

                        _freqChangePending = false;
                        _vfoBChangePending = false;
                        _splitChangePending = false;
                    }

                    _suppressOutgoingUpdates = true;
                    try
                    {
                        _console.BeginInvoke(new Action(() =>
                        {
                            try
                            {
                                _console.VFOSwap();
                            }
                            finally
                            {
                                _suppressOutgoingUpdates = false;
                                _radioInitiatedSwapInProgress = false;
                            }
                        }));
                    }
                    catch
                    {
                        _suppressOutgoingUpdates = false;
                        _radioInitiatedSwapInProgress = false;
                    }
                }
                return;
            }

            // 4. VFO A Update:
            // The selected VFO on the IC-7100 (whether VFO A or VFO B) represents the active receiver.
            // Check if delta is significant (> 1.5 Hz) and not an echo of our last sent VFO A frequency
            if (Math.Abs(freqMHz - _console.VFOAFreq) < 0.0000015 || Math.Abs(freqMHz - _lastSentVfoAFreq) < 0.0000015)
            {
                return;
            }

            _lastSentVfoAFreq = freqMHz;
            lock (_stateLock)
            {
                _pendingVfoAFreq = freqMHz;
                if (!IsSplitRequired())
                {
                    _pendingTxFreq = freqMHz;
                }
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
            if (_isSwappingVfo || _radioInitiatedSwapInProgress) return;
            if (_lastVfoSwapTime > 0)
            {
                double msSinceSwap = (double)(Stopwatch.GetTimestamp() - _lastVfoSwapTime) / Stopwatch.Frequency * 1000.0;
                if (msSinceSwap < 300.0)
                {
                    return;
                }
            }

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
